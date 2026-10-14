"""Scenarios: a LIVE place that times out and that Kite's OMS takes late (post-build review, broker API lens).

KiteConnect's place_order is one synchronous POST: a client timeout (KITE_TIMEOUT_S = 5 s) says nothing about
whether or when the OMS registers the order. The bot answers AMBIGUOUS with tag lookups and keeps the order UNKNOWN
for orders.LATE_ACCEPT_S after the answer, so no other attempt goes out on its leg (I4) and the late order is
adopted when it shows up. A BUY the bot gave up as NOT_PLACED that the broker still fills is adopted into a
RECOVERED structure that exits at once (Engine._adopt_late_fill): never an unmanaged long.

LIVE only: the late acceptance is FakeKite's (the dual PAPER/LIVE fixture is not used).
"""

from __future__ import annotations

import math

import pytest
import requests

from titan2.model import Account, Role, parse_tag

from fakes.runner import ScenarioRunner
from fakes.scenario import enter, leg_of, orders_of, state_of, struct_of, wait_for

pytestmark = pytest.mark.scenario

REL = "NSE:RELIANCE"


def _accept_late(r: ScenarioRunner, role: Role, delay_s: float) -> None:
    """The first place_order of `role` times out client-side; the OMS registers it delay_s later."""
    real_place = r.kite.place_order
    held: list = []

    def place_order(**kw):
        p = parse_tag(kw.get("tag"))
        if p is not None and p.role is role and not held:
            held.append([r.clock.mono() + delay_s, kw])
            raise requests.exceptions.ReadTimeout("HTTPSConnectionPool(host='api.kite.trade'): Read timed out.")
        return real_place(**kw)

    def release(runner: ScenarioRunner) -> None:
        if held and held[0][1] is not None and runner.clock.mono() >= held[0][0]:
            kw, held[0][1] = held[0][1], None
            real_place(**kw)

    r.kite.place_order = place_order
    r.after_step.append(release)


def _stop_quote(r: ScenarioRunner, sid: int) -> tuple[float, float]:
    """A book 10% under the structure's hard stop."""
    p = r.engine.state.structures[sid].exit_params
    bid = round(math.floor(p.hard_sl_px * 0.9 / 0.05) * 0.05, 2)
    return bid, round(bid + 0.2, 2)


def test_entry_taken_late_inside_the_window_is_adopted_and_managed(tmp_path):
    r = ScenarioRunner(tmp_path / "run", Account.LIVE)
    try:
        _accept_late(r, Role.ENTRY, 20.0)                 # after the +15 s lookup, inside orders.LATE_ACCEPT_S (30 s)
        i = r.intent(REL)
        sym, lot = r.contract().symbol, r.contract().lot_size
        r.run(17.0)
        assert state_of(r, i) == "ENTERING"            # still looked up: not VOIDed while Kite may take it
        wait_for(r, lambda: state_of(r, i) == "OPEN", 15, "the late entry to be adopted")
        sid = struct_of(r, i)["structure_id"]
        (o,) = orders_of(r, sid, "ENTRY")
        assert (o["state"], o["filled_qty"]) == ("COMPLETE", lot) and r.broker_net(sym) == lot
        r.market.quote(sym, *_stop_quote(r, sid))         # the premium falls through the hard stop ...
        wait_for(r, lambda: state_of(r, i) == "CLOSED", 30, "the managed long to be sold")
        assert struct_of(r, i)["exit_reason"] == "HARD_SL" and r.broker_net(sym) == 0      # ... and it is sold
        assert not r.halts("UNKNOWN_POSITION") and not r.structures(book="RECOVERED:LIVE")
    finally:
        r.close()


def test_exit_taken_late_inside_the_window_is_sold_once(tmp_path):
    r = ScenarioRunner(tmp_path / "run", Account.LIVE)
    try:
        i, s = enter(r, REL)
        sid = s["structure_id"]
        leg = leg_of(r, sid)
        sym, lot = leg.inst.symbol, leg.inst.lot_size
        _accept_late(r, Role.EXIT, 20.0)
        r.market.quote(sym, *_stop_quote(r, sid))         # HARD_SL: the first SELL times out, the OMS takes it at +20 s
        net: list[int] = []
        r.after_step.append(lambda rr: net.append(rr.kite.net(sym)))
        r.run(45.0)                                       # well past the late acceptance and the late window
        assert state_of(r, i) == "CLOSED"
        sells = [x for x in r.kite.orders_by_id.values() if x.symbol == sym and x.side == "SELL" and x.filled]
        assert len(sells) == 1 and sells[0].filled == lot      # sold once: no second SELL while the first was unknown
        assert min(net) == 0 and r.broker_net(sym) == 0        # never short (G3)
        assert not r.halts("SHORT_DETECTED") and not r.halts("UNKNOWN_POSITION")
        (x,) = orders_of(r, sid, "EXIT")
        assert (x["state"], x["filled_qty"], x["broker_order_id"]) == ("COMPLETE", lot, sells[0].order_id)
        (tr,) = r.trades(r.book())
        assert tr["exit_reason"] == "HARD_SL" and tr["pnl_status"] == "FINAL"
    finally:
        r.close()


def test_entry_taken_after_the_window_is_adopted_and_exited(tmp_path):
    r = ScenarioRunner(tmp_path / "run", Account.LIVE)
    try:
        _accept_late(r, Role.ENTRY, 45.0)                 # 15 s after orders.LATE_ACCEPT_S (30 s) ran out
        i = r.intent(REL)
        sym, lot = r.contract().symbol, r.contract().lot_size
        wait_for(r, lambda: state_of(r, i) == "VOID", 35, "the AMBIGUOUS entry to be given up")
        (gone,) = orders_of(r, struct_of(r, i)["structure_id"], "ENTRY")
        assert gone["state"] == "NOT_PLACED"
        wait_for(r, lambda: bool(r.structures(book="RECOVERED:LIVE")), 20, "the late fill to be adopted")
        (rec,) = r.structures(book="RECOVERED:LIVE")
        assert rec["strategy"] == "_recovered"
        wait_for(r, lambda: r.structures(book="RECOVERED:LIVE")[0]["state"] == "CLOSED", 30,
                 "the adopted long to be sold at once")
        assert r.broker_net(sym) == 0                         # never left an unmanaged long
        (x,) = orders_of(r, rec["structure_id"], "ENTRY")
        b = r.kite.orders_by_id[x["broker_order_id"]]
        assert b.tag == gone["order_pk"] and b.filled == lot == x["filled_qty"] and x["order_pk"] != b.tag
        (tr,) = r.trades("RECOVERED:LIVE")
        assert tr["exit_reason"] == "ANOMALY_EXIT"
        assert any(e["payload"]["adopted"] == x["order_pk"] for e in r.events("LATE_FILL"))
        assert "LATE_FILL" in r.alert_codes("CRITICAL") and not r.halts("UNKNOWN_POSITION")
    finally:
        r.close()
