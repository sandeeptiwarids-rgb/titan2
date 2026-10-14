"""Review F-7 end to end: recovery never reports an order sent after its orders read as vanished.

recon.plan_recovery called any non-terminal bot order missing from the recovery orders read "vanished"
(INVARIANT_VIOLATION + HALT(INVARIANT, GLOBAL, ACK) + CRITICAL) without checking when that read was asked. Exits run
during RECOVERING, and the plan waits for the tradebook read and its retries, so an EXIT order sent after the orders
read was taken but before the plan ran was reported as a vanished working order: a false CRITICAL and a global entry
halt that needed an operator ACK. (Ported from the review's proof test; LIVE, on the real core and FakeKite.)
"""

from __future__ import annotations

import math

from titan2.broker.kite import order_from_kite
from titan2.core import recon
from titan2.core.events import OrdersSnapshot
from titan2.model import Account, HaltClears

from fakes.runner import ScenarioRunner
from fakes.scenario import enter, leg_of, orders_of, wait_for

REL = "NSE:RELIANCE"
TICK = 0.05


def test_order_sent_after_the_recovery_read_is_not_vanished(tmp_path):
    r = ScenarioRunner(tmp_path / "r", Account.LIVE)
    try:
        i, s = enter(r, REL)
        sid = s["structure_id"]
        sym = leg_of(r, sid).inst.symbol
        r.run(6)
        read_mono = r.clock.mono()
        before = r.kite.orders()                        # the recovery orders read, taken now ...
        r.faults.order_stuck_open(hold_s=3600, cancel_works=False, symbol=sym, role="EXIT")
        p = r.engine.state.structures[sid].exit_params
        bid = round(math.ceil((p.tp_px + 0.5) / TICK) * TICK, 2)
        r.market.quote(sym, bid, round(bid + 0.1, 2))   # ... then an exit is decided and sent, and stays working
        wait_for(r, lambda: any(o["state"] == "WORKING" for o in orders_of(r, sid, "EXIT")), 10, "exit working")
        snap = OrdersSnapshot(Account.LIVE, True, tuple(order_from_kite(x, Account.LIVE) for x in before),
                              read_mono, read_mono + 0.2)
        eng = r.engine
        (x,) = [o for o in eng.state.orders.values() if o.structure_id == sid and str(o.role) == "EXIT"]
        assert x.submitted_mono > read_mono and x.order_pk not in {b.tag for b in snap.orders}
        plan = recon.plan_recovery(
            account=Account.LIVE, session=eng.session, now_wall=eng.now_wall, eod_flatten_at=eng.eod.flatten,
            structures=[st for st in eng.state.structures.values() if st.account is Account.LIVE],
            legs=[lg for lg in eng.state.legs.values() if lg.account is Account.LIVE], trade_qty={},
            orders=snap, positions=None, tradebook_ok=True)
        assert plan.vanished == (), (f"{plan.vanished} reported vanished (halts "
                                     f"{[(h.kind, h.scope) for h in plan.halts]}) though sent after the orders read")
        assert ("INVARIANT", "GLOBAL", HaltClears.ACK) not in [(h.kind, h.scope, h.clears) for h in plan.halts]
        assert not [a for a in plan.alerts if a.code == "INVARIANT"]
        assert not [e for e in plan.events if e.kind == "INVARIANT_VIOLATION"]
        assert x.order_pk not in plan.lookups and x.order_pk not in {pk for pk, _ in plan.apply}
    finally:
        r.close()
