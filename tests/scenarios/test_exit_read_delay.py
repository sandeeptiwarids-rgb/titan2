"""Decision B1 (2026-10-11) end to end: an exit is capped only by a broker positions read requested at or after the
exit decision (settings.EXIT_CAP_PRIOR_READ_S = 0), and paper pays that read's round trip as LIVE does (the seeded
paper read delay, PAPER_READ_LATENCY_*; tests/fakes/runner.py mirrors it on LIVE).

Both run on PAPER and on LIVE (the dual fixture), which also asserts that both runs send the same broker commands at
the same times and book the same P&L.
"""

from __future__ import annotations

import math

import pytest

from titan2 import ledger
from titan2.core.events import FetchPositions, Place, Priority
from titan2.model import Role

from fakes.scenario import TERMINAL_STRUCT, enter, exit_places, leg_of, sells_at_broker, state_of, wait_for

pytestmark = pytest.mark.scenario


def _fresh_read(r, symbol: str, qty: int, *, max_rtt: float = 0.6, timeout_s: float = 30.0):
    """Step until a positions read of the account that shows `qty` on symbol lands in the step just run, with a round
    trip under max_rtt; return that read."""
    def landed() -> bool:
        p = r.engine.state.broker[r.acct].positions
        return (p is not None and p.ok and math.isclose(p.recv_mono, r.clock.mono(), abs_tol=1e-9)
                and p.recv_mono - p.req_mono <= max_rtt
                and sum(x.net_qty for x in p.rows if x.symbol == symbol) == qty)

    wait_for(r, landed, timeout_s, f"a fresh positions read showing {qty} {symbol}")
    return r.engine.state.broker[r.acct].positions


def test_close_moments_before_a_stop_is_never_sold(dual):
    """A position closed by hand just after a positions read, then the book collapses through the hard stop: the read
    in hand at the decision shows the lot and is under 1 s old, but it was asked before the decision, so it caps
    nothing. The exit waits for a read made after the decision, which shows the account flat: no bot SELL reaches the
    broker, the broker never goes short, and recon books CLOSED_EXTERNAL. (With the old 1 s window that read capped
    the exit and the bot sold the lot a second time: a naked short.)"""
    r = dual.runner()
    i, s = enter(r)
    sid, sym, lot = s["structure_id"], r.contract().symbol, r.contract().lot_size

    def never_short(rr) -> None:
        assert rr.broker_net(sym) >= 0, f"G3 broken: the broker is short {rr.broker_net(sym)} {sym} (naked short)"

    r.after_step.append(never_short)
    r.run(6)                                          # past the post-fill wait: the reads count for the cap
    read = _fresh_read(r, sym, lot)
    r.faults.external_close(sym)                      # sold in the Kite app right after that read ...
    r.step()
    assert r.broker_net(sym) == 0 and state_of(r, i) == "OPEN"
    r.market.quote(sym, 20.0, 20.1)                   # ... then the book collapses through the hard stop
    wait_for(r, lambda: state_of(r, i) != "OPEN", 2, "the hard-stop decision")
    decided = leg_of(r, sid).exiting_since_mono
    assert 0.0 < decided - read.req_mono < 1.0        # the newest read at the decision: < 1 s old, shows the lot
    assert r.engine.state.broker[r.acct].positions is read      # ... and is still the newest one
    wait_for(r, lambda: state_of(r, i) in TERMINAL_STRUCT, 40, "the manual close to be booked")
    assert state_of(r, i) == "CLOSED_EXTERNAL"
    assert exit_places(r) == [] and sells_at_broker(r) == 0
    assert r.broker_net(sym) == 0
    asked = [x for x in r.sent if isinstance(x.cmd, FetchPositions) and x.cmd.account is r.acct
             and x.cmd.priority == Priority.EXIT and x.mono >= decided]
    assert asked                                      # the exit asked for a read made after its decision
    (tr,) = r.trades(r.book())
    assert (tr["exit_reason"], tr["pnl_status"]) == ("CLOSED_EXTERNAL", "FINAL")
    assert math.isclose(tr["net"], r.net_from_fills(sid), abs_tol=0.005)


def test_exit_order_waits_for_its_post_decision_read(dual):
    """A take profit with a post-fill read in hand: the first EXIT Place is dispatched only once a positions read
    requested after the decision has been answered, on PAPER (the paper read delay) and on LIVE (the mirror). Its
    EXIT_ATTEMPT carries decision_to_order_ms, at least that read's round trip and under the 1.5 s Stage 2 criterion
    at the defaults, and the daily report's exit_decision_to_submit_ms row counts it."""
    r = dual.runner()
    i, s = enter(r)
    sid, sym = s["structure_id"], r.contract().symbol
    r.run(6)                                          # a post-fill positions read exists before the decision
    seen: dict = {}

    def on_dispatch(rr, c) -> None:
        if isinstance(c, Place) and c.account is rr.acct and c.req.role is Role.EXIT and "placed" not in seen:
            seen.update(placed=rr.clock.mono(), read=rr.proc.engine.state.broker[rr.acct].positions,
                        decided=leg_of(rr, sid).exiting_since_mono)

    r.dispatch_hooks.append(on_dispatch)
    r.market.quote(sym, 41.0, 41.1)                   # take profit
    wait_for(r, lambda: state_of(r, i) in TERMINAL_STRUCT, 30, "the take-profit exit")
    assert state_of(r, i) == "CLOSED"
    read, decided, placed = seen["read"], seen["decided"], seen["placed"]
    assert read.ok and read.req_mono >= decided - 1e-9          # the cap's read was asked at or after the decision
    rtt = read.recv_mono - read.req_mono
    assert rtt > 0 and read.recv_mono <= placed + 1e-9          # ... and answered (after its delay) before the Place
    (attempt, *_) = [e for e in r.events("EXIT_ATTEMPT") if e["leg_id"] == leg_of(r, sid).leg_id]
    ms = attempt["payload"]["decision_to_order_ms"]
    assert attempt["payload"]["cap_source"] == "POSITIONS"
    assert ms == pytest.approx((placed - decided) * 1000.0, abs=0.1)
    assert rtt * 1000.0 - 0.1 <= ms < 1500.0
    lat = ledger.daily_report(r.store, r.session, r.clock.now_wall()).data["latency"]["exit_decision_to_submit_ms"]
    assert lat["n"] >= 1 and lat["p95"] == pytest.approx(ms, abs=1.0)    # (a LIVE run also exits its PAPER twin)
    (tr,) = r.trades(r.book())
    assert tr["exit_reason"] == "TAKE_PROFIT" and tr["pnl_status"] == "FINAL"
