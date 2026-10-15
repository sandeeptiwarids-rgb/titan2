"""Network helpers for every titan2 process.

- force_ipv4(): Kite's REST calls (kiteconnect -> requests -> urllib3) connect over IPv4 only. On 2026-10-12 the
  laptop's ISP routed IPv6 to api.kite.trade with about half the packets lost while IPv4 was clean, and Windows
  prefers IPv6: each loss became a 5 s read timeout and a refused entry (QUOTE:FETCH_FAILED). KiteTicker (Twisted)
  already resolves IPv4 only. TITAN2_IPV6=1 turns this off. __main__.main() calls it once, before any connection.
  It also installs remember_ipv4(): a host whose DNS lookup fails is connected to at its last good IPv4 address.
- tcp_probe(): timed IPv4 TCP connects to api.kite.trade:443, the laptop self-check's `network` row (WARN only).
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import statistics
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

log = logging.getLogger(__name__)

KITE_API_HOST = "api.kite.trade"
PROBE_TRIES = 8
PROBE_TIMEOUT_S = 1.5
SLOW_CONNECT_MS = 900.0          # a lost SYN is resent after about 1 s: a connect slower than this lost a packet
SLOW_MEDIAN_MS = 250.0
FALLBACK_LOG_EVERY = 20          # while a host's lookups fail: its first connect to the last good address is logged,
#                                  then every 20th
_LAST_GOOD: dict[str, str] = {}  # host -> the IPv4 address its last connect reached (this process)
_FALLBACKS: dict[str, int] = {}  # host -> connects made to that address since its lookups began to fail
_LOCK = threading.Lock()


def _ipv4_only() -> int:
    return socket.AF_INET


def force_ipv4(env: Mapping[str, str] | None = None) -> bool:
    """Make urllib3 (so requests and KiteConnect) resolve IPv4 addresses only, and survive a failed lookup on the
    host's last good address (remember_ipv4); False when TITAN2_IPV6=1 keeps the system's choice (and neither is
    installed). Idempotent and process-wide."""
    if str((env or {}).get("TITAN2_IPV6", "")).strip() == "1":
        return False
    import urllib3.util.connection as conn            # lazy: the operator verbs that never connect stay light

    conn.allowed_gai_family = _ipv4_only
    conn.create_connection = remember_ipv4(conn.create_connection)     # urllib3 looks it up on every connect
    return True


def last_good(host: str) -> str | None:
    """The IPv4 address the last connect to host reached in this process, or None."""
    with _LOCK:
        return _LAST_GOOD.get(host)


def _is_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _remember(host: str, sock: Any) -> None:
    try:
        if getattr(sock, "family", None) != socket.AF_INET or _is_address(host):
            return
        ip = sock.getpeername()[0]
    except (OSError, AttributeError, IndexError, TypeError):
        return
    with _LOCK:
        _LAST_GOOD[host] = ip
        fallbacks = _FALLBACKS.pop(host, 0)
    if fallbacks:
        log.info("DNS for %s answers again (%d connect(s) used its last good address meanwhile)", host, fallbacks)


def remember_ipv4(create: Callable[..., Any]) -> Callable[..., Any]:
    """urllib3's create_connection, remembering each host's last good IPv4 address and connecting to it when the
    host's DNS lookup fails (socket.gaierror). 2026-10-14 14:22:08-14:22:53: every new HTTPS connection failed with
    'getaddrinfo failed' while the Kite WebSocket stayed up: 13 account reads lost, a STALE_RECON halt and 3 phone
    alerts lost. Only the TCP connect uses the address: urllib3 still names the real host for TLS (SNI) and checks the
    certificate against it, so a stale address fails the TLS check instead of reaching another server. A host never
    reached in this process, or an address that does not answer either, fails with the lookup's own error, as
    before. Applied once (the wrapper is marked)."""
    if getattr(create, "_titan2_last_good", False):
        return create

    def create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        host, port = address[0], address[1]
        try:
            sock = create(address, *args, **kwargs)
        except socket.gaierror as exc:
            ip = last_good(host)
            if ip is None:
                raise
            with _LOCK:
                n = _FALLBACKS[host] = _FALLBACKS.get(host, 0) + 1
            if n == 1 or n % FALLBACK_LOG_EVERY == 0:
                log.warning("DNS lookup of %s failed (%s): connecting to its last good address %s (TLS still checks "
                            "the name; %d such connect(s) so far)", host, exc, ip, n)
            try:
                return create((ip, port), *args, **kwargs)
            except OSError as again:
                log.warning("%s's last good address %s did not answer either: %s", host, ip, again)
                raise exc from None
        _remember(host, sock)
        return sock

    create_connection._titan2_last_good = True          # type: ignore[attr-defined]
    create_connection.__wrapped__ = create              # type: ignore[attr-defined]
    return create_connection


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
