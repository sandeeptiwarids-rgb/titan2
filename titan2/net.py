"""Network helpers for every titan2 process.

- force_ipv4(): Kite's REST calls (kiteconnect -> requests -> urllib3) connect over IPv4 only. On 2026-10-12 the
  laptop's ISP routed IPv6 to api.kite.trade with about half the packets lost while IPv4 was clean, and Windows
  prefers IPv6: each loss became a 5 s read timeout and a refused entry (QUOTE:FETCH_FAILED). KiteTicker (Twisted)
  already resolves IPv4 only. TITAN2_IPV6=1 turns this off. __main__.main() calls it once, before any connection.
- tcp_probe(): timed IPv4 TCP connects to api.kite.trade:443, the laptop self-check's `network` row (WARN only).
"""

from __future__ import annotations

import socket
import statistics
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

KITE_API_HOST = "api.kite.trade"
PROBE_TRIES = 8
PROBE_TIMEOUT_S = 1.5
SLOW_CONNECT_MS = 900.0          # a lost SYN is resent after about 1 s: a connect slower than this lost a packet
SLOW_MEDIAN_MS = 250.0


def _ipv4_only() -> int:
    return socket.AF_INET


def force_ipv4(env: Mapping[str, str] | None = None) -> bool:
    """Make urllib3 (so requests and KiteConnect) resolve IPv4 addresses only; False when TITAN2_IPV6=1 keeps the
    system's choice. Idempotent and process-wide."""
    if str((env or {}).get("TITAN2_IPV6", "")).strip() == "1":
        return False
    import urllib3.util.connection as conn            # lazy: the operator verbs that never connect stay light

    conn.allowed_gai_family = _ipv4_only
    return True


@dataclass(frozen=True, slots=True)
class Probe:
    host: str
    tries: int
    ok: int                      # connects that completed
    slow: int                    # of those, slower than SLOW_CONNECT_MS (a packet was lost and resent)
    median_ms: float | None
    worst_ms: float | None
    error: str | None = None     # the last failure, if any

    @property
    def clean(self) -> bool:
        return (self.ok == self.tries and self.slow == 0 and self.median_ms is not None
                and self.median_ms <= SLOW_MEDIAN_MS)


def tcp_probe(host: str = KITE_API_HOST, port: int = 443, *, tries: int = PROBE_TRIES,
              timeout_s: float = PROBE_TIMEOUT_S, resolve: Callable[..., Any] = socket.getaddrinfo,
              connect: Callable[..., Any] = socket.create_connection,
              clock: Callable[[], float] = time.perf_counter) -> Probe:
    """`tries` TCP connects to the first IPv4 address of host (resolved once), each timed; nothing is sent."""
    try:
        addr = resolve(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4][:2]
    except (OSError, IndexError) as exc:
        return Probe(host, tries, 0, 0, None, None, f"cannot resolve {host}: {exc}")
    times: list[float] = []
    error = None
    for _ in range(tries):
        t0 = clock()
        try:
            connect(addr, timeout=timeout_s).close()
        except OSError as exc:
            error = f"{type(exc).__name__}: {exc}"
            continue
        times.append((clock() - t0) * 1000.0)
    if not times:
        return Probe(host, tries, 0, 0, None, None, error)
    return Probe(host, tries, len(times), sum(1 for t in times if t > SLOW_CONNECT_MS), statistics.median(times),
                 max(times), error)
