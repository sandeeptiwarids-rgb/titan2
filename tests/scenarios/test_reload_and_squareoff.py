"""Scenarios: an endless rollback-and-reload storm, and a broker MIS square-off after the old 15:30 close.

Both run on account LIVE only (the broker report and the square-off come from FakeKite), so they do not use the
dual PAPER/LIVE fixture.

- One contradicting broker report for one bot order makes Engine.handle raise an InvariantViolation on every poll of
  that order; the core then rolls back and reloads about twice a second. A reload must not restart the post-fill
  wait or the exit decision of the OTHER legs (titan2.store.load_state keeps this process's monotonic times), so
  their exits still go out (spec §3.6: a leg-scope violation only moves that leg; exits continue).
- NSE F&O closes at 15:40 since 2026-08-03 and Zerodha's MIS square-off time after it is unverified [V]: a verified
  later time (15:35) must be configurable, and the bot then books that square-off the same day.
"""

from __future__ import annotations

import json
import math
from datetime import time as dtime

import kiteconnect.exceptions as kex
import pytest

from titan2 import settings as settings_mod
from titan2.clock import ist_datetime
from titan2.core.events import FetchPositions, Priority
from titan2.model import Account, Role

from fakes import universe as uni
from fakes.runner import ScenarioRunner
from fakes.scenario import enter, leg_of, orders_of, state_of, struct_of, wait_for

pytestmark = pytest.mark.scenario

REL, INFY = "NSE:RELIANCE", "NSE:INFY"
TICK = 0.05


def _tp_quote(r: ScenarioRunner, sid: int) -> tuple[float, float]:
    """A bid at the structure's take-profit level (plus a margin) and an ask one 0.10 above."""
    p = r.engine.state.structures[sid].exit_params
    bid = round(math.ceil((p.tp_px + 0.5) / TICK) * TICK, 2)
    return bid, round(bid + 0.1, 2)


def test_reload_storm_keeps_other_legs_exiting(tmp_path):
    r = ScenarioRunner(tmp_path / "r", Account.LIVE)
    try:
        _, sa = enter(r, REL)
        ib, sb = enter(r, INFY)
        leg_a = leg_of(r, sa["structure_id"])
        sym_a, sym_b, lot_a = leg_a.inst.symbol, leg_of(r, sb["structure_id"]).inst.symbol, leg_a.inst.lot_size
        r.run(6)
        # A's exit order rests at the broker (its cancel is acknowledged but ignored) ...
        r.faults.order_stuck_open(hold_s=3600, cancel_works=False, symbol=sym_a, role="EXIT")
        r.market.quote(sym_a, *_tp_quote(r, sa["structure_id"]))
        wait_for(r, lambda: any(o["state"] in ("WORKING", "CANCEL_REQUESTED")
                                for o in orders_of(r, sa["structure_id"], "EXIT")), 10, "A's exit order working")
        (xa,) = orders_of(r, sa["structure_id"], "EXIT")
        bid = xa["broker_order_id"]
        ko = r.kite.orders_by_id[bid]
        # ... and from now on every poll of it answers "REJECTED with a fill" (at most twice a second; the order is
        # left out of orders(), so only its own polls bring the report)
        last = {"mono": -1e9}
        real_hist, real_orders = r.kite.order_history, r.kite.orders

        def order_history(order_id):
            if str(order_id) != bid:
                return real_hist(order_id)
            now = r.clock.mono()
            if now - last["mono"] < 0.5:
                raise kex.NetworkException("Too many requests", 429)
            last["mono"] = now
            row = dict(ko.row(parsed=True))
            row.update(status="REJECTED", filled_quantity=lot_a, average_price=float(xa["limit_px"]),
                       pending_quantity=0, status_message="RMS: rejected")
            return [row]

        r.kite.order_history = order_history
        r.kite.orders = lambda: [o for o in real_orders() if o["order_id"] != bid]
        n0 = len(r.violations)
        wait_for(r, lambda: len(r.violations) > n0, 10, "the contradicting report to raise")
        # B (another symbol, nothing wrong with it) now reaches its take-profit
        r.market.quote(sym_b, *_tp_quote(r, sb["structure_id"]))
        r.run(30)
        storm = len(r.violations) - n0                      # ~30 reloads today; 0 once orders.py quarantines it
        b_exits = [c for c in r.places(Role.EXIT) if c.req.symbol == sym_b]
        assert b_exits, (f"B's take-profit exit never went out in 30 s (B is {state_of(r, ib)}); {storm} "
                         "rollback-and-reloads happened meanwhile")
        wait_for(r, lambda: state_of(r, ib) == "CLOSED", 30, "B to close")
        assert r.broker_net(sym_b) == 0
    finally:
        r.close()


def test_squareoff_at_1535_is_configurable_and_booked_same_day(tmp_path):
    late = dtime(15, 35)
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"BROKER_SQUAREOFF_AT": "15:35"}), encoding="utf-8")
    loaded = settings_mod.load(path)
    assert loaded.valid and loaded.settings.BROKER_SQUAREOFF_AT == late, loaded.errors
    r = ScenarioRunner(tmp_path / "r", Account.LIVE, start="14:50:00", warmup_until="14:52:00",
                       session=uni.PREV_SESSION, settings=loaded.settings)
    try:
        r.kite.squareoff_at = late                          # the real broker squares off MIS at 15:35
        i, s = enter(r)
        sym = leg_of(r, s["structure_id"]).inst.symbol
        r.run_until("15:14:00", dt=5.0)
        r.market.quote(sym, None, 30.0)                     # no bids: the bot cannot sell before STOP_SENDING
        r.run_until("15:55:00", dt=1.0)
        exited = [code for _, code in r.exits]
        row = struct_of(r, i)                               # read-only: the process ended at DONE (15:49)
        assert exited == [0], exited
        assert r.broker_net(sym) == 0
        assert row["state"] == "CLOSED_EXTERNAL", (
            f"the broker squared the leg off at 15:35 but the bot left it {row['state']} at its 15:49 exit")
        # the periodic recon (every 5 s with a leg held) runs on to the last EOD recon (15:40), not to 15:31
        lo, hi = (ist_datetime(uni.PREV_SESSION, t) for t in ("15:32:00", "15:35:00"))
        periodic = [s for s in r.sent if isinstance(s.cmd, FetchPositions) and s.cmd.priority == Priority.RECON
                    and lo <= s.wall < hi]
        assert len(periodic) >= 30, f"{len(periodic)} periodic positions reads between 15:32 and 15:35"
    finally:
        r.close()
