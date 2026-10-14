"""End-to-end regressions for post-build review findings (state lens) on the real core and the fakes.

1. An ENTRY Kite took after its place timed out, whose WS updates were lost and which orders() showed only after the
   last tag lookup, ended NOT_PLACED with its structure VOID while the broker held the lot: an unmanaged long. The
   next orders read imports it into a RECOVERED structure that exits at once (ANOMALY_EXIT).
2. A positions() lag beyond the 5 s grace + 3 s confirmation booked a phantom EXTERNAL_CLOSE although the orders
   view showed the bot's filled BUY and no sale; the real position was then orphaned.
3. After a lost titan2.db, two bot legs on one underlying made recovery reserve two RECOVERED:<account> structures
   on it: I5 aborted the whole recovery and nothing was imported.
4. A structure of an earlier session whose exit order was still CANCEL_REQUESTED in the DB never settled (the
   broker serves only today's orders): recon stayed PENDING for good and every entry was refused.

Single-account runs: the faults are FakeKite's (LIVE) or the account's own reads.
"""

from __future__ import annotations

import math
from datetime import timedelta

import pytest

from titan2.clock import to_ist
from titan2.core import orders as order_machine
from titan2.model import Account, Role, parse_tag

from fakes import universe as uni
from fakes.runner import ScenarioRunner
from fakes.scenario import enter, leg_of, orders_of, state_of, struct_of, wait_for

pytestmark = pytest.mark.scenario

REL, INFY = "NSE:RELIANCE", "NSE:INFY"
TICK = 0.05


def _hide_new_orders(r: ScenarioRunner, role: Role, hide_s: float) -> None:
    """orders() leaves out bot orders of `role` for hide_s after they were placed (orderbook lag)."""
    orig = r.kite.orders

    def lagging():
        now = to_ist(r.clock.now_wall()).replace(tzinfo=None)
        out = []
        for row in orig():
            p = parse_tag(row.get("tag"))
            ts = row.get("order_timestamp")
            if p is not None and p.role is role and ts is not None and (now - ts).total_seconds() < hide_s:
                continue
            out.append(row)
        return out

    r.kite.orders = lagging


def _last_lookup_s() -> float:
    """When the order machine gives an AMBIGUOUS order up as NOT_PLACED at the latest (its lookup schedule)."""
    return max(order_machine.LOOKUP_AT_S[-1], getattr(order_machine, "NOT_PLACED_AFTER_S", 0.0),
               getattr(order_machine, "LATE_ACCEPT_S", 0.0) + 5.0)


def test_an_entry_filled_after_every_tag_lookup_is_adopted_and_sold(tmp_path):
    r = ScenarioRunner(tmp_path / "run", Account.LIVE)
    try:
        sym, lot = r.contract(REL).symbol, r.contract(REL).lot_size
        hide = _last_lookup_s() + 15.0                                 # orders() shows it after the last lookup
        r.faults.timeout_after_accept(role="ENTRY")                   # Kite takes it; the HTTP answer is lost ...
        r.faults.drop_ws_updates(end=r.clock.now_wall() + timedelta(seconds=hide + 60))   # ... and its WS updates
        _hide_new_orders(r, Role.ENTRY, hide)
        i = r.intent(REL)
        wait_for(r, lambda: state_of(r, i) == "VOID", hide, "the entry to be given up")
        (o,) = orders_of(r, struct_of(r, i)["structure_id"], "ENTRY")
        assert o["state"] == "NOT_PLACED" and r.broker_net(sym) == lot
        wait_for(r, lambda: r.broker_net(sym) == 0, 60, "the bot's own long to be sold")
        rec = [s for s in r.structures() if s["strategy"] == "_recovered"]
        assert len(rec) == 1 and rec[0]["exit_reason"] == "ANOMALY_EXIT"
        sells = [c for c in r.places(Role.EXIT) if c.req.symbol == sym]
        assert sells and sum(c.req.qty for c in sells) >= lot             # the bot's own SELL, not the broker's
        assert r.alert_codes("CRITICAL")
    finally:
        r.close()


@pytest.mark.parametrize("lag_s", [12.0, 16.0, 20.0])
def test_a_positions_lag_never_books_an_external_close(tmp_path, lag_s):
    r = ScenarioRunner(tmp_path / "run", Account.LIVE)
    try:
        r.faults.positions_lag(lag_s)
        i, s = enter(r, REL)
        sym = leg_of(r, s["structure_id"]).inst.symbol
        r.run(lag_s + 15)
        st = struct_of(r, i)
        assert st["state"] == "OPEN", f"{st['state']} ({st['exit_reason']}, pnl {st['pnl_status']})"
        assert r.trades(r.book()) == [] and not r.events("CLOSED_EXTERNAL")
        assert r.broker_net(sym) == r.contract(REL).lot_size
        r.market.quote(sym, 5.0, 5.1)                                  # the leg is still managed: a crash is sold
        wait_for(r, lambda: r.broker_net(sym) == 0, 45, "the hard stop to sell")
        assert [o["state"] for o in orders_of(r, s["structure_id"], "EXIT")][-1] == "COMPLETE"
    finally:
        r.close()


def test_a_lost_db_with_two_legs_on_one_underlying_imports_both(tmp_path):
    r = ScenarioRunner(tmp_path / "run", Account.PAPER)
    try:
        i1 = r.intent(REL, strategy="price_spike_up")
        i2 = r.intent(REL, strategy="opening_drive")
        books = ("PAPER:price_spike_up", "PAPER:opening_drive")
        wait_for(r, lambda: all(state_of(r, i, b) == "OPEN" for i, b in zip((i1, i2), books)), 10, "both entries")
        sym = r.rows("SELECT symbol FROM legs WHERE book = ?", (books[0],))[0]["symbol"]
        held = r.broker_net(sym, Account.PAPER)
        r.run(3)
        r.crash()
        for f in r.db_path.parent.glob("titan2.db*"):
            f.unlink()                                  # the DB is lost; the paper exchange still has the orders
        r.boot()
        r.run(5)
        assert not [e for e in r.events("READY") if e["payload"].get("recovery") == "ABORTED"]
        rec = [x for x in r.structures() if x["strategy"] == "_recovered" and x["account"] == "PAPER"]
        assert sorted(x["book"] for x in rec) == ["RECOVERED:PAPER", "RECOVERED:PAPER:2"]
        assert sum(leg_of(r, x["structure_id"]).open_qty for x in rec) == held
        assert not r.halts("UNKNOWN_POSITION") and not r.halts("INVARIANT")
    finally:
        r.close()


def _tp_quote(r: ScenarioRunner, sid: int) -> tuple[float, float]:
    p = r.engine.state.structures[sid].exit_params
    bid = round(math.ceil((p.tp_px + 0.5) / TICK) * TICK, 2)
    return bid, round(bid + 0.1, 2)


def test_an_earlier_sessions_working_exit_settles_at_the_next_start(tmp_path):
    r = ScenarioRunner(tmp_path / "run", Account.LIVE, session=uni.PREV_SESSION)
    try:
        i, s = enter(r, REL)
        sid = s["structure_id"]
        sym = leg_of(r, sid).inst.symbol
        r.run(6)
        r.faults.order_stuck_open(hold_s=3600, cancel_works=False, symbol=sym, role="EXIT")
        r.market.quote(sym, *_tp_quote(r, sid))
        wait_for(r, lambda: any(o["state"] in ("WORKING", "CANCEL_REQUESTED") for o in orders_of(r, sid, "EXIT")),
                 10, "the exit order working")
        r.next_session(uni.SESSION)                     # the bot dies mid-exit and only comes back the next session
        r.run(30)
        st = struct_of(r, i)
        assert st["state"] == "CLOSED_EXTERNAL" and st["pnl_status"] == "UNRESOLVED"
        (x,) = orders_of(r, sid, "EXIT")
        assert x["state"] == "CANCELLED" and "outcome unknown" in x["status_msg"]
        assert not r.halts("ORDER_STUCK")
        assert r.engine.state.broker[Account.LIVE].recon.status == "CLEAN"     # the symbol is out of grace
        assert "STALE_STRUCTURE" in r.alert_codes("CRITICAL")
    finally:
        r.close()
