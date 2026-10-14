"""End-to-end regressions for the orders and money re-review (review round 3, 2026-10-14) on entries, on the real
core and the fakes (PAPER). Ported from the reviewers' reproductions, which asserted the defects.

1. N3: G06 and G16 measured a tick's age as now_mono - receipt, so a tick the WS thread stamped after the event
   began read as stale: an intent REFUSED FEED:STALE_TICK because any of ~200 underlyings ticked during the event, a
   placebo SKIPPED:FEED while its parent traded (G1's placebo control lost exactly the fast-market pairs), an entry
   VOID 'RECHECK:STALE: underlying LTP -0.00 s old'. They use Quote.age's receipt rule now (model.age_since_receipt).
2. N6: DAY_BUDGET never got the open positions' remaining stop risk: after a Rs 10,069 loss, a second position was
   opened beside a first although both stopping out as planned put the day at -Rs 16,626, past D3 (Rs 15,000). Its
   review (follow-up): an open winner's gain was counted twice, so a safe second entry beside it was refused
   SIZE:DAY_BUDGET. The day term is the realized net now, and an open position counts by its stop-out from its entry.
"""

from __future__ import annotations

import dataclasses

import pytest

from titan2.core import sizing
from titan2.core.events import IntentReceived, QuoteResult
from titan2.model import Account, Policy, Refusal, Sized

from fakes.runner import ScenarioRunner
from fakes.scenario import enter, leg_of, route_of, state_of, struct_of, wait_for

pytestmark = pytest.mark.scenario

REL, OTHER = "NSE:RELIANCE", "NSE:INFY"


# ---------------------------------------------------------------------------------------------------------------
# 1. a tick received while the event runs is fresh (G06, G16)
# ---------------------------------------------------------------------------------------------------------------
def _tick(r: ScenarioRunner, symbol: str) -> None:
    """The reactor thread's tick of `symbol`, 2 ms of the core's work after the event began."""
    token = next(t for t, s in r.market._token_symbol.items() if s == symbol)
    r.clock.advance(0.002)
    r.market.feed.on_ticks(None, [r.market._tick_dict(token, symbol, "quote", r.clock.now_wall())])


def test_an_unrelated_tick_during_the_event_never_refuses_the_intent(tmp_path):
    """G06 reads the feed's last underlying tick when the gate context is built, after _publish() and the intent's
    SQL: an INFY tick in between refused a RELIANCE intent FEED:STALE_TICK on a healthy feed."""
    r = ScenarioRunner(tmp_path / "run", Account.PAPER)
    try:
        eng = r.proc.engine
        real_publish, real_handle = eng._publish, eng.handle
        armed = {"on": False, "hits": 0}

        def publish():
            out = real_publish()
            if armed["on"]:
                armed["on"] = False
                armed["hits"] += 1
                _tick(r, OTHER)
            return out

        def handle(ev):
            if isinstance(ev, IntentReceived):
                armed["on"] = True
            return real_handle(ev)

        eng._publish, eng.handle = publish, handle
        i = r.intent(REL)
        r.run(3.0)
        route = route_of(r, i, r.book())
        assert armed["hits"] == 1
        assert route is not None and route["outcome"] == "RESERVED", f"refused on a healthy feed: {route}"
    finally:
        r.close()


def test_a_tick_after_the_parent_entered_never_skips_its_placebo(tmp_path):
    """The placebo's own G01-G13 run later in the same event, after the parent's begin_entry: a tick in between
    skipped the placebo (SKIPPED:FEED) while its parent traded."""
    r = ScenarioRunner(tmp_path / "run", Account.PAPER)
    try:
        eng = r.proc.engine
        real_begin = eng.begin_entry
        hits = {"n": 0}

        def begin_entry(st, **kw):
            out = real_begin(st, **kw)
            if hits["n"] == 0 and st.book.startswith("PAPER:"):
                hits["n"] += 1
                _tick(r, OTHER)
            return out

        eng.begin_entry = begin_entry
        i = r.intent(REL)
        r.run(3.0)
        s = struct_of(r, i)
        assert hits["n"] == 1 and s is not None and s["state"] in ("ENTERING", "OPEN")
        assert s["placebo_status"] == "OPENED", f"placebo skipped on a healthy feed: {s['placebo_status']}"
    finally:
        r.close()


def test_an_underlying_tick_while_the_quote_result_is_handled_never_voids_the_entry(tmp_path):
    """G16 reads the underlying from the shared quote cache in the decision snapshot: a RELIANCE tick the WS thread
    put there after the event began voided the entry 'RECHECK:STALE: underlying LTP -0.00 s old' (and started its
    cooldown)."""
    r = ScenarioRunner(tmp_path / "run", Account.PAPER)
    try:
        q = r.proc.quotes
        orig_last, real_handle = q.last, r.proc.engine.handle
        armed = {"on": False, "hits": 0}
        i = r.make_intent(REL)

        def last(sym):
            if armed["on"] and sym == REL:
                armed["on"] = False
                armed["hits"] += 1
                cur = orig_last(sym)
                r.clock.advance(0.005)
                q.put(dataclasses.replace(cur, recv_mono=r.clock.mono(), recv_wall=r.clock.now_wall(), source="WS"))
            return orig_last(sym)

        def handle(ev):
            if isinstance(ev, QuoteResult) and ev.request_id == i.intent_id:
                armed["on"] = True
            return real_handle(ev)

        q.last, r.proc.engine.handle = last, handle
        r.intent(intent=i)
        wait_for(r, lambda: state_of(r, i) not in (None, "RESERVED", "ENTERING"), 10, "the entry to resolve")
        s = struct_of(r, i)
        assert armed["hits"] == 1
        assert s["state"] == "OPEN", f"entry refused by a fresh tick: {s['state']} {s['void_reason']}"
    finally:
        r.close()


# ---------------------------------------------------------------------------------------------------------------
# 2. DAY_BUDGET counts the open position's stop risk (N6)
# ---------------------------------------------------------------------------------------------------------------
def test_a_second_position_is_sized_with_the_first_ones_remaining_stop_risk(tmp_path, monkeypatch):
    """Ported from the review's money probe: TCS loses about Rs 10,100, RELIANCE opens (its planned stop-out about
    Rs 3,600), then INFY. INFY alone would fit the Rs 4,900 left, but not beside RELIANCE's remaining stop risk:
    refused SIZE:DAY_BUDGET (it was opened, and both stopping out put the day near -Rs 16,600)."""
    calls: list = []
    real = sizing.size

    def spy(*a, **kw):
        out = real(*a, **kw)
        calls.append((a, kw, out))
        return out

    monkeypatch.setattr(sizing, "size", spy)
    r = ScenarioRunner(tmp_path / "run", Account.PAPER)
    try:
        t, ts = enter(r, "NSE:TCS")
        leg = leg_of(r, ts["structure_id"])
        e = r.engine.state.structures[ts["structure_id"]].exit_params.entry_avg
        bid = round(int((e - 10_000 / leg.inst.lot_size) / 0.05) * 0.05, 2)
        r.market.quote(leg.inst.symbol, bid, round(bid + 0.10, 2))
        wait_for(r, lambda: state_of(r, t) == "CLOSED", 30, "the TCS loss")
        realized = sum(x["net"] for x in r.trades(r.book()))
        assert realized < -10_000.0
        r.run(61)
        a, sa = enter(r, REL)
        planned_a = r.engine.state.structures[sa["structure_id"]].decision["plan"]["per_lot_risk_rs"]
        r.run(61)
        b, sb = enter(r, OTHER, expect=None)
        (args, kw, out), = [c for c in calls if "INFY" in c[0][0].symbol and c[0][7] is Policy.FULL]   # not the placebo
        assert sb["state"] == "VOID" and sb["void_reason"].startswith("SIZE:DAY_BUDGET"), sb
        assert out == Refusal("SIZE", "DAY_BUDGET") and kw["live_book"] is False
        # the day term is the realized net, and RELIANCE (a little under water) counts its planned stop-out
        assert args[5] == pytest.approx(realized) and kw["open_risk_rs"] == pytest.approx(planned_a)
        alone = real(*args, **{**kw, "open_risk_rs": 0.0})          # without RELIANCE INFY would have been sized
        assert isinstance(alone, Sized) and alone.per_lot_risk_rs <= r.settings.DAILY_STOP_RS + realized
        assert realized - planned_a - alone.per_lot_risk_rs < -r.settings.DAILY_STOP_RS   # both stop-outs pass D3
    finally:
        r.close()


def test_a_winning_first_position_leaves_room_for_a_safe_second_one(tmp_path):
    """The review of round 3 (N6 follow-up), ported end to end from the probe test_n6_plan_entry_both.py: RELIANCE
    opens (Rs 42.5k of premium, a planned stop-out of about Rs 8,660) and rises 13.5% (about Rs 5,500 up at the bid,
    under its 15% target), then TCS (Rs 7k of premium, about Rs 1,470). Both stopping out as planned puts the day near
    -Rs 10,130, inside the Rs 15,000 stop, and their premiums stay inside the Rs 50,000 worst case. TCS was refused
    SIZE:DAY_BUDGET: the open gain got no room from min(day net, 0) and was charged again as risk still to lose."""
    r = ScenarioRunner(tmp_path / "run", Account.PAPER)
    try:
        rel, tcs = r.contract(REL).symbol, r.contract("NSE:TCS").symbol
        r.market.quote(rel, 84.95, 85.05)
        r.market.quote(tcs, 39.95, 40.05)
        r.run(1.0)
        a, sa = enter(r, REL)
        first = r.engine.state.structures[sa["structure_id"]]
        planned = first.decision["plan"]["per_lot_risk_rs"]
        up = round(round(first.exit_params.entry_avg * 1.135 / 0.05) * 0.05, 2)
        r.market.quote(rel, up, round(up + 0.10, 2))
        r.run(3.0)
        assert state_of(r, a) == "OPEN"                                 # under its target: its gain is unrealized
        gain = r.engine.leg_mtm(leg_of(r, sa["structure_id"]))
        assert gain is not None and gain > 5_000.0
        b, sb = enter(r, "NSE:TCS")                                     # opened (it was VOID SIZE:DAY_BUDGET)
        second = r.engine.state.structures[sb["structure_id"]].decision["plan"]
        assert planned + second["per_lot_risk_rs"] + gain > r.settings.DAILY_STOP_RS      # what the double count saw
        assert -planned - second["per_lot_risk_rs"] > -r.settings.DAILY_STOP_RS            # both stop-outs: inside D3
    finally:
        r.close()
