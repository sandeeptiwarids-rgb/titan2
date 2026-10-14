"""Scenario: a paper order meets the book as it is when the order arrives, as a live IOC does (DETAIL §6.3).

The dual fixture runs this on PAPER and on LIVE and asserts that both runs book the same trade. LIVE fills come from
FakeKite, which walks the market's TRUE book at the due time. Here the option's book lifts one tick the moment the
entry is sent, while the option's WS stays silent (KiteTicker sends nothing for a quiet option), so the only book the
bot ever cached is the REST decision snapshot. Regression: the paper exchange filled against that snapshot (it was
under PAPER_BOOK_MAX_AGE_S old), so the PAPER entry cost one tick less than the LIVE one: paper P&L, which G1
certifies, carried none of the latency slippage a live order pays.
"""

from __future__ import annotations

import math

import pytest

from titan2.core.events import Place
from titan2.model import Role

from fakes.scenario import enter, orders_of

pytestmark = pytest.mark.scenario


def test_entry_fills_on_the_book_that_moved_during_the_latency(dual):
    r = dual.runner()
    inst = r.contract()
    sym, tick = inst.symbol, inst.tick
    r.market.freeze(sym)                                   # no WS tick for the option while the entry is in flight
    decided: dict = {}

    def lift_on_send(runner, cmd) -> None:
        if isinstance(cmd, Place) and cmd.req.role == Role.ENTRY and cmd.req.symbol == sym and not decided:
            b = runner.market.book(sym)
            decided["ask"] = b.asks[0].price
            runner.market.depth(sym, [(lv.price + tick, lv.qty) for lv in b.bids],
                                [(lv.price + tick, lv.qty) for lv in b.asks])

    r.dispatch_hooks.append(lift_on_send)
    _i, s = enter(r)
    r.market.unfreeze(sym)
    (entry,) = orders_of(r, s["structure_id"], "ENTRY")
    assert entry["decision_quote_src"] == "REST" and entry["decision_ask"] == decided["ask"]
    assert entry["decision_walk_px"] == decided["ask"]     # one lot: the walk of the decision book is its ask
    # the order met the lifted book on arrival: one tick of latency slippage, on PAPER exactly as on LIVE
    assert (entry["state"], entry["filled_qty"]) == ("COMPLETE", entry["qty"])
    assert math.isclose(entry["avg_fill_px"], decided["ask"] + tick, abs_tol=1e-9)
    r.run(5)
