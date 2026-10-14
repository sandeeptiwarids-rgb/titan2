"""End-to-end regressions for two post-build review findings (incidents lens), on the real core and the fakes.

1. An order Kite accepted, whose answer was lost and which orders() left out for a while, became NOT_PLACED after
   two tag lookups: the exit loop sold the position again (a naked short) or the entry was VOIDed while the broker
   held it (an unmanaged long). titan2.core.orders now looks the tag up at +1/+3/+7/+15 s before NOT_PLACED, and the
   NOT_PLACED moment counts like a fill for recon's post-fill rule (titan2.core.recon), so the next exit is capped
   only by a positions read made after it.
2. A positions() read that contradicted the orders book for >= 3 s booked a phantom EXTERNAL_CLOSE and abandoned a
   position the broker still held. titan2.core.recon now books a drop only when the sale shows; a contradiction
   halts (MISMATCH, CRITICAL) and the leg stays managed.

Single-account runs (the faults are FakeKite's orders() view, LIVE only, or the positions read on either account).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from titan2.clock import to_ist
from titan2.model import Account, Role, parse_tag

from fakes.runner import ScenarioRunner
from fakes.scenario import enter, orders_of, state_of, struct_of


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


def _strip_tags(r: ScenarioRunner) -> None:
    orig = r.kite.orders

    def untagged():
        rows = orig()
        for row in rows:
            row["tag"] = None                 # [V] #4 fails: orders() does not echo the API tag
        return rows

    r.kite.orders = untagged


def _exit_blip(r: ScenarioRunner):
    i, s = enter(r)
    sym = r.contract().symbol
    r.run(6)                                          # a post-fill positions read exists
    r.faults.timeout_after_accept(role="EXIT")        # the SELL is accepted, the HTTP answer is lost ...
    r.faults.drop_ws_updates(role="EXIT", count=2)    # ... and so is its WS update (the same network blip)
    nets: list[int] = []
    r.after_step.append(lambda rr: nets.append(rr.broker_net(sym)))
    return i, s, sym, nets


def _sold(r: ScenarioRunner, sym: str) -> int:
    return sum(o.filled for o in r.kite.api_orders() if o.side == "SELL" and o.symbol == sym)


def test_accepted_exit_hidden_from_orders_is_adopted_not_sold_twice(tmp_path):
    r = ScenarioRunner(tmp_path / "run", Account.LIVE)
    try:
        i, s, sym, nets = _exit_blip(r)
        _hide_new_orders(r, Role.EXIT, 10.0)
        r.market.quote(sym, 41.0, 41.1)               # take profit
        r.run(25)
        assert min(nets) >= 0, f"G3 broken: broker net reached {min(nets)} (bot SELL fills {_sold(r, sym)})"
        assert _sold(r, sym) == r.contract().lot_size
        (x,) = orders_of(r, s["structure_id"], "EXIT")
        assert x["state"] == "COMPLETE"               # found by a later lookup: adopted, never NOT_PLACED
        assert state_of(r, i) == "CLOSED"
        (tr,) = r.trades(r.book())
        assert tr["exit_reason"] == "TAKE_PROFIT" and tr["pnl_status"] == "FINAL" and tr["net"] > 0
    finally:
        r.close()


def test_accepted_exit_without_tag_echo_never_leaves_a_short(tmp_path):
    r = ScenarioRunner(tmp_path / "run", Account.LIVE)
    try:
        i, s, sym, nets = _exit_blip(r)
        _strip_tags(r)
        r.market.quote(sym, 41.0, 41.1)
        r.run(120)
        assert min(nets) >= 0 and r.broker_net(sym) == 0, (
            f"a naked short: net {min(nets)}, now {r.broker_net(sym)}; halts {[(h.kind, h.scope) for h in r.halts()]}")
        assert _sold(r, sym) == r.contract().lot_size
        assert state_of(r, i) == "CLOSED_EXTERNAL"      # the untagged SELL reads as a close outside the bot
        (tr,) = r.trades(r.book())
        assert tr["pnl_status"] == "FINAL" and tr["net"] > 0
    finally:
        r.close()


def test_accepted_exit_hidden_past_every_lookup_is_never_sold_twice(tmp_path):
    r = ScenarioRunner(tmp_path / "run", Account.LIVE)
    try:
        i, s, sym, nets = _exit_blip(r)
        _hide_new_orders(r, Role.EXIT, 75.0)          # longer than every tag lookup: it ends NOT_PLACED
        r.market.quote(sym, 41.0, 41.1)
        r.run(70)
        x = orders_of(r, s["structure_id"], "EXIT")
        assert [o["state"] for o in x] == ["NOT_PLACED"], x     # no second SELL was sent
        assert min(nets) >= 0 and state_of(r, i) == "EXITING"
        assert r.halts("MISMATCH", "ACCOUNT:LIVE")              # positions 0 vs orders 500: a contradiction
        r.run(30)                                               # orders() shows the SELL: booked from it
        assert min(nets) >= 0 and r.broker_net(sym) == 0 and _sold(r, sym) == r.contract().lot_size
        assert state_of(r, i) == "CLOSED_EXTERNAL"
        (tr,) = r.trades(r.book())
        assert tr["pnl_status"] == "FINAL" and tr["net"] > 0
    finally:
        r.close()


def test_accepted_entry_hidden_from_orders_is_managed(tmp_path):
    r = ScenarioRunner(tmp_path / "run", Account.LIVE)
    try:
        sym = r.contract().symbol
        _hide_new_orders(r, Role.ENTRY, 10.0)
        r.faults.timeout_after_accept(role="ENTRY")
        r.faults.drop_ws_updates(role="ENTRY", count=2)
        i = r.intent("NSE:RELIANCE")
        r.run(20)
        st = struct_of(r, i)
        assert r.broker_net(sym) == r.contract().lot_size
        assert st["state"] == "OPEN", f"{st['state']} ({st['void_reason']}) while the broker holds the lot"
    finally:
        r.close()


@pytest.mark.parametrize("acct", [Account.PAPER, Account.LIVE])
def test_positions_contradiction_never_abandons_a_held_position(tmp_path, acct):
    r = ScenarioRunner(tmp_path / "run", acct)
    try:
        i, s = enter(r)
        sym = r.contract().symbol
        r.run(10)
        now = r.clock.now_wall()
        r.faults.positions_contradict(start=now, end=now + timedelta(seconds=12))   # positions() reads 0 for 12 s
        r.run(30)
        assert state_of(r, i) == "OPEN" and r.broker_net(sym) > 0
        assert r.halts("MISMATCH", f"ACCOUNT:{acct.value}")      # CRITICAL, nothing booked
        assert r.trades(r.book()) == []
        r.market.quote(sym, 20.0, 20.1)               # then the option collapses far through the -15% stop
        r.run(30)
        held = r.broker_net(sym)
        assert held == 0, f"the hard stop never sold: {held} still held ({state_of(r, i)})"
        assert state_of(r, i) == "CLOSED"
        (tr,) = r.trades(r.book())
        assert tr["exit_reason"] == "HARD_SL" and tr["pnl_status"] == "FINAL"
    finally:
        r.close()
