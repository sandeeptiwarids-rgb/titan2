"""titan2.net: Kite REST over IPv4 only, and the timed TCP probe behind the laptop's `network` self-check row."""

from __future__ import annotations

import socket
import types

import pytest
import urllib3.util.connection as conn

from titan2 import net


@pytest.fixture
def keep_gai(monkeypatch):
    """Whatever a test does to urllib3's address family, and to its create_connection (remember_ipv4), is undone
    after it."""
    monkeypatch.setattr(conn, "allowed_gai_family", conn.allowed_gai_family)
    monkeypatch.setattr(conn, "create_connection", conn.create_connection)


def test_force_ipv4_makes_urllib3_resolve_ipv4_only(keep_gai):
    assert net.force_ipv4({}) is True
    assert conn.allowed_gai_family() == socket.AF_INET
    assert net.force_ipv4(None) is True and conn.allowed_gai_family() == socket.AF_INET     # idempotent


def test_force_ipv4_opt_out_keeps_the_system_choice(keep_gai):
    before = conn.allowed_gai_family
    assert net.force_ipv4({"TITAN2_IPV6": "1"}) is False
    assert conn.allowed_gai_family is before


def test_cli_forces_ipv4_before_any_verb(keep_gai, tmp_path):
    from titan2.__main__ import main

    out: list[str] = []
    main(["is-trading-day", "--date", "2026-10-13"], env={"TITAN2_VAR": str(tmp_path)}, out=out.append)
    assert conn.allowed_gai_family() == socket.AF_INET


def _resolve(host, port, family, kind):
    assert (host, family, kind) == ("api.kite.trade", socket.AF_INET, socket.SOCK_STREAM)
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.18.10.10", port))]


def _scripted(delays_ms):
    """A clock and a connect(): each connect takes the next delay, or raises it when it is an exception."""
    now, calls, it = [0.0], [], iter(delays_ms)

    def connect(addr, timeout):
        calls.append((addr, timeout))
        d = next(it)
        if isinstance(d, BaseException):
            raise d
        now[0] += d / 1000.0
        return types.SimpleNamespace(close=lambda: None)

    return (lambda: now[0]), connect, calls


def test_probe_clean_line():
    clock, connect, calls = _scripted([20, 25, 30, 22, 21, 24, 23, 26])
    p = net.tcp_probe(resolve=_resolve, connect=connect, clock=clock)
    assert (p.host, p.tries, p.ok, p.slow, p.error) == ("api.kite.trade", 8, 8, 0, None)
    assert p.median_ms == pytest.approx(23.5) and p.worst_ms == pytest.approx(30) and p.clean
    assert calls == [(("104.18.10.10", 443), net.PROBE_TIMEOUT_S)] * 8      # resolved once, IPv4, nothing sent


def test_probe_counts_lost_and_resent_packets():
    clock, connect, _ = _scripted([20, TimeoutError("timed out"), 1020, 22, 21, 24, 23, 26])
    p = net.tcp_probe(resolve=_resolve, connect=connect, clock=clock)
    assert (p.ok, p.slow, p.error) == (7, 1, "TimeoutError: timed out") and not p.clean
    assert p.worst_ms == pytest.approx(1020)


def test_probe_slow_line_is_not_clean():
    clock, connect, _ = _scripted([300] * 8)
    p = net.tcp_probe(resolve=_resolve, connect=connect, clock=clock)
    assert (p.ok, p.slow) == (8, 0) and p.median_ms == pytest.approx(300) and not p.clean


def test_probe_unreachable_and_unresolvable():
    clock, connect, _ = _scripted([OSError("network is unreachable")] * 8)
    p = net.tcp_probe(resolve=_resolve, connect=connect, clock=clock)
    assert (p.ok, p.median_ms, p.worst_ms, p.error) == (0, None, None, "OSError: network is unreachable")

    def no_dns(*a):
        raise socket.gaierror(11001, "getaddrinfo failed")

    p = net.tcp_probe(resolve=no_dns, connect=connect, clock=clock)
    assert p.ok == 0 and not p.clean and p.error.startswith("cannot resolve api.kite.trade:")
