"""Regression tests for post-build review findings on titan2.core.recon.

Phantom external close: a positions() read that showed the bot's option as 0 for >= 3 s, while orders() still showed
the bot's filled BUY and no sale, was booked as EXTERNAL_CLOSE (UNRESOLVED, at the last rb) and the position was
abandoned while the broker still held it. A drop at the broker now needs the sale to show (the positions row's day
sells, or today's orders adding up to the positions net); otherwise it is a CONTRADICTION: HALT(MISMATCH) +
CRITICAL, nothing booked, the leg stays managed.

NOT_PLACED that existed: a bot SELL the DB recorded NOT_PLACED but the broker shows filled now prices the close FINAL
(its fills are real), and an orphan entry of that kind is named in the EXCESS alert.
"""

from __future__ import annotations

from datetime import date, time as dtime

import pytest

from titan2.clock import ist_datetime
from titan2.core import recon
from titan2.core.events import FetchOrders, FetchPositions, FetchTrades, OrdersSnapshot, PollOrder, PositionsSnapshot
from titan2.core.recon import BotView, ReconClass, ReconMemory, reconcile, symbol_view
from titan2.model import (
    ROLE_SIDE, Account, BrokerOrder, BrokerPosition, HaltClears, Instrument, Leg, LegState, Order, OrderState,
    PnlStatus, Role, Side, make_tag,
)

S = date(2026, 10, 12)
PREV = date(2026, 10, 9)
SYM = "NFO:RELIANCE26OCT2900CE"
T0 = 1000.0
INST = Instrument(SYM, 12345, "RELIANCE26OCT2900CE", "NFO", "RELIANCE", "NSE:RELIANCE", "CE", 2900.0,
                  date(2026, 10, 27), 500, 0.05)
READS = (FetchTrades, FetchPositions, FetchOrders, PollOrder)


def W(hms: str, d: date = S):
    return ist_datetime(d, hms)


class Db:
    def __init__(self, account: Account) -> None:
        self.account = account
        self.index: dict[str, Order] = {}
        self.legs: list[Leg] = []

    def leg(self, leg_id: int, *, session: date = S, state: LegState = LegState.OPEN) -> Leg:
        lg = Leg(leg_id, leg_id, 1, self.account, f"{self.account}:opening_drive", session, leg_id, INST, state=state,
                 last_fill_mono=T0, entry_fill_wall=W("10:00", session))
        lg.bind_orders(self.index)
        self.legs.append(lg)
        return lg

    def order(self, lg: Leg, role: Role, filled: int, *, state: OrderState = OrderState.COMPLETE,
              broker_id: str | None = None, attempt: int = 1, avg: float = 30.0) -> Order:
        pk = make_tag(role, self.account, lg.session, lg.seq_no, attempt)
        o = Order(pk, lg.leg_id, lg.structure_id, self.account, lg.book, role, ROLE_SIDE[role], SYM, 500, avg,
                  attempt, 0, state=state, broker_order_id=broker_id, filled_qty=filled,
                  avg_fill_px=avg if filled else None)
        self.index[pk] = o
        lg.orders.append(pk)
        return o

    def view(self) -> BotView:
        return BotView(self.account, S, dtime(15, 26), tuple(self.legs))


def row(account: Account, net: int, sell_qty: int = 0, sell_value: float = 0.0) -> BrokerPosition:
    return BrokerPosition(account, SYM, INST.tradingsymbol, "NFO", "MIS", net, net + sell_qty, sell_qty,
                          (net + sell_qty) * 30.0, sell_value)


def bord(account: Account, oid: str, side: Side, filled: int, *, tag: str | None = None, avg: float | None = 30.0,
         wall=None, placed_by: str = "API") -> BrokerOrder:
    return BrokerOrder(account, oid, tag, SYM, INST.tradingsymbol, "NFO", "MIS", side, "LIMIT", "IOC", 500, filled,
                       avg if filled else None, "COMPLETE", "COMPLETE", None, wall or W("10:00"), wall or W("10:00"),
                       placed_by)


def entry_copy(db: Db, o: Order) -> BrokerOrder:
    return bord(db.account, o.broker_order_id, o.side, o.filled_qty, tag=o.order_pk, avg=o.avg_fill_px)


def twice(db: Db, pos_rows, ords, t: float = T0 + 10, mem: ReconMemory | None = None):
    """Two reconciles RECON_CONFIRM_GAP_S apart on the same broker views (the confirmation)."""
    a = db.account
    first = reconcile(db.view(), PositionsSnapshot(a, True, tuple(pos_rows), t, t + 0.2),
                      OrdersSnapshot(a, True, tuple(ords), t, t + 0.2), mem or ReconMemory(), t + 0.2)
    second = reconcile(db.view(), PositionsSnapshot(a, True, tuple(pos_rows), t + 3, t + 3.2),
                       OrdersSnapshot(a, True, tuple(ords), t + 3, t + 3.2), first.memory, t + 3.2)
    return first, second


def never_trades(*results) -> None:
    for r in results:
        assert all(isinstance(c, READS) for c in r.commands), r.commands


@pytest.mark.parametrize("acct", [Account.PAPER, Account.LIVE])
def test_positions_contradicting_the_orders_never_book_a_close(acct):
    db = Db(acct)
    lg = db.leg(1)
    e = db.order(lg, Role.ENTRY, 500, broker_id="B1E")
    # positions() reads 0 (no sale on the row) while today's orders still show the bot's BUY and no SELL
    first, r = twice(db, [row(acct, 0)], [entry_copy(db, e)])
    assert first.status is ReconClass.PENDING
    (f,) = r.findings
    assert f.cls is ReconClass.CONTRADICTION and r.status is ReconClass.CONTRADICTION
    assert f.closes == () and f.cap_qty is None and f.legs == (1,) and (f.broker_qty, f.expected) == (0, 500)
    assert "today's orders add up to 500" in f.detail and "nothing inferred" in f.detail
    assert [(h.kind, h.scope, h.clears) for h in r.halts] == [("MISMATCH", f"ACCOUNT:{acct}", HaltClears.ACK)]
    assert [(a.severity, a.code) for a in r.alerts] == [("CRITICAL", "MISMATCH")]
    assert [e.kind for e in r.events if e.kind != "RECON"] == ["MISMATCH"]
    assert r.live_demote == (acct is Account.LIVE) and r.g1_mismatch == (acct is not Account.LIVE)
    never_trades(first, r)
    # the contradiction persists: no repeated halt or alert
    _, again = twice(db, [row(acct, 0)], [entry_copy(db, e)], t=T0 + 20, mem=r.memory)
    assert again.findings[0].cls is ReconClass.CONTRADICTION and again.halts == () and again.alerts == ()
    # positions recover: the leg was never abandoned, the account is CLEAN again
    back = reconcile(db.view(), PositionsSnapshot(acct, True, (row(acct, 500),), T0 + 30, T0 + 30.2),
                     OrdersSnapshot(acct, True, (entry_copy(db, e),), T0 + 30, T0 + 30.2), again.memory, T0 + 30.2)
    assert back.clean and back.findings == ()


def test_contradiction_variants():
    acct = Account.LIVE
    db = Db(acct)
    lg = db.leg(1)
    e = db.order(lg, Role.ENTRY, 500, broker_id="B1E")
    # a partial drop the orders do not back is no PARTIAL_EXTERNAL either (its capped exit would sell 300 short)
    _, r = twice(db, [row(acct, 200)], [entry_copy(db, e)])
    assert r.findings[0].cls is ReconClass.CONTRADICTION and r.findings[0].cap_qty is None
    # both views empty (a read that returns nothing): the orders miss the bot's own BUY, so they prove nothing
    _, r = twice(db, [], [])
    assert r.findings[0].cls is ReconClass.CONTRADICTION and "lack the bot's fills" in r.findings[0].detail
    # complete orders that show more sold than the positions (positions lagging a 2nd manual SELL): a PARTIAL_EXTERNAL
    #   capped at 200 could sell 200 short, so the orders' disagreement wins over the row's own sale figures
    m = bord(acct, "M1", Side.SELL, 500, wall=W("11:00"), placed_by="AB1234")
    _, r = twice(db, [row(acct, 200, sell_qty=300, sell_value=9000.0)], [entry_copy(db, e), m])
    assert r.findings[0].cls is ReconClass.CONTRADICTION and "today's orders add up to 0" in r.findings[0].detail
    # once the positions catch up both views agree: the close is booked from the order
    _, r = twice(db, [row(acct, 0, sell_qty=500, sell_value=15_000.0)], [entry_copy(db, e), m], t=T0 + 20,
                 mem=r.memory)
    assert r.findings[0].cls is ReconClass.EXTERNAL_CLOSE and r.findings[0].closes[0].pnl_status is PnlStatus.FINAL


def test_a_real_external_close_still_books():
    acct = Account.LIVE
    db = Db(acct)
    lg = db.leg(1)
    e = db.order(lg, Role.ENTRY, 500, broker_id="B1E")
    manual = bord(acct, "M1", Side.SELL, 500, avg=36.4, wall=W("11:00"), placed_by="AB1234")
    # backed by today's orders alone (the positions row carries no sale figures)
    _, r = twice(db, [row(acct, 0)], [entry_copy(db, e), manual])
    (f,) = r.findings
    assert f.cls is ReconClass.EXTERNAL_CLOSE and f.closes[0].pnl_status is PnlStatus.FINAL
    assert [(x.broker_order_id, x.qty, x.px) for x in f.closes[0].fills] == [("M1", 500, 36.4)]
    # orders() not caught up with the sale yet (they still add up to the bot's 500): wait, book nothing
    _, r = twice(db, [row(acct, 0, sell_qty=500, sell_value=18_200.0)], [entry_copy(db, e)])
    assert r.findings[0].cls is ReconClass.CONTRADICTION and r.findings[0].closes == ()
    # an orders read that misses the bot's own BUY proves nothing; the positions row's own sale figures back the
    #   drop then: ESTIMATED from the row
    _, r = twice(db, [row(acct, 0, sell_qty=500, sell_value=18_200.0)], [])
    (f,) = r.findings
    assert f.cls is ReconClass.EXTERNAL_CLOSE and f.closes[0].pnl_status is PnlStatus.ESTIMATED
    assert f.closes[0].fills[0].px == 36.4


def test_a_paper_leftover_needs_its_square_off_to_show():
    acct = Account.PAPER
    db = Db(acct)
    lg = db.leg(1, session=PREV)
    db.order(lg, Role.ENTRY, 500, broker_id="P261009000000001")
    sv = symbol_view(db.view(), SYM)
    assert (sv.expected, sv.carried) == (500, 500)    # yesterday's qty: today's orders cannot show it
    sim = bord(acct, "P261012000000001", Side.SELL, 500, tag="SQOFF", avg=0.05, wall=W("08:59:30"),
               placed_by="EXCHANGE")
    _, r = twice(db, [row(acct, 0)], [sim])
    assert r.findings[0].cls is ReconClass.BROKER_SQUAREOFF and not r.g1_mismatch
    _, r = twice(db, [row(acct, 0)], [])              # flat, but no square-off order and no sale on the row
    assert r.findings[0].cls is ReconClass.CONTRADICTION and r.findings[0].closes == ()


def test_a_paper_leftover_closed_on_an_earlier_day_is_planned_from_the_record():
    """Review round 2 (A1 backstop): the CONTRADICTION above stays recon's answer, but when the paper exchange says it
    held nothing of the symbol at the start of today (FetchSquareoffs) the engine books the leftover from the
    exchange's own record of that earlier day (recon.carried_closes): FINAL at the record's price, as a confirmed
    square-off is; UNRESOLVED and never above the record's price when the record does not explain the qty; nothing
    without a record (the engine then closes it UNRESOLVED). Today's legs on the symbol are left to recon."""
    acct = Account.PAPER
    db = Db(acct)
    lg = db.leg(1, session=PREV)
    db.order(lg, Role.ENTRY, 500, broker_id="P261009000000001")
    today = db.leg(2)
    db.order(today, Role.ENTRY, 500, broker_id="P261012000000001")
    sq = bord(acct, "P261009000000009", Side.SELL, 500, tag="SQOFF", avg=12.5, wall=W("15:35:00", PREV),
              placed_by="EXCHANGE")
    (c,) = recon.carried_closes(db.view(), SYM, [sq])
    assert (c.leg_id, c.qty, c.kind, c.pnl_status) == (1, 500, ReconClass.BROKER_SQUAREOFF, PnlStatus.FINAL)
    assert [(f.broker_order_id, f.qty, f.px) for f in c.fills] == [("P261009000000009", 500, 12.5)]
    assert recon.carried_closes(db.view(), SYM, []) == ()
    part = bord(acct, "P261009000000009", Side.SELL, 300, tag="SQOFF", avg=12.5, wall=W("15:35:00", PREV),
                placed_by="EXCHANGE")
    view = BotView(acct, S, dtime(15, 26), tuple(db.legs), last_rb={1: 20.0})
    (c,) = recon.carried_closes(view, SYM, [part])
    assert c.pnl_status is PnlStatus.UNRESOLVED and sum(f.qty for f in c.fills) == 500
    assert {f.px for f in c.fills} == {12.5}            # the last rb (20.0) would book better than the record
    before = bord(acct, "P261009000000003", Side.SELL, 500, tag="SQOFF", avg=12.5, wall=W("09:00:00", PREV),
                  placed_by="EXCHANGE")                 # before the leg's entry: not its close
    assert recon.carried_closes(db.view(), SYM, [before]) == ()


def test_a_not_placed_sell_that_filled_prices_the_close():
    acct = Account.LIVE
    db = Db(acct)
    lg = db.leg(1, state=LegState.EXITING)
    e = db.order(lg, Role.ENTRY, 500, broker_id="B1E")
    x = db.order(lg, Role.EXIT, 0, state=OrderState.NOT_PLACED, attempt=2)
    hidden = bord(acct, "B1X", Side.SELL, 500, tag=x.order_pk, avg=41.05, wall=W("10:30"))
    _, r = twice(db, [row(acct, 0, sell_qty=500, sell_value=20_525.0)], [entry_copy(db, e), hidden])
    (f,) = r.findings
    (c,) = f.closes
    assert f.cls is ReconClass.EXTERNAL_CLOSE and c.pnl_status is PnlStatus.FINAL and c.orders == (hidden,)
    assert [(fl.broker_order_id, fl.qty, fl.px) for fl in c.fills] == [("B1X", 500, 41.05)]
    assert f"{x.order_pk} recorded NOT_PLACED are filled at the broker" in f.detail
    never_trades(r)


def test_an_orphan_entry_is_named_in_the_excess_alert():
    acct = Account.LIVE
    db = Db(acct)
    lg = db.leg(1, state=LegState.VOID)
    o = db.order(lg, Role.ENTRY, 0, state=OrderState.NOT_PLACED)
    late = bord(acct, "B1E", Side.BUY, 500, tag=o.order_pk, avg=34.85)
    _, r = twice(db, [row(acct, 500)], [late])
    (f,) = r.findings
    assert f.cls is ReconClass.EXCESS and f"{o.order_pk} recorded NOT_PLACED are filled" in f.detail
    assert [(h.kind, h.clears) for h in r.halts] == [("UNKNOWN_POSITION", HaltClears.ACK)]
    assert o.order_pk in r.alerts[0].text
    # once the engine adopted that broker order (a new row holding its broker id, on a managed leg) it is neither
    #   lost nor excess: the account is CLEAN
    adopted = db.leg(2)
    db.order(adopted, Role.ENTRY, 500, broker_id="B1E")
    assert recon._lost_orders(acct, symbol_view(db.view(), SYM), OrdersSnapshot(acct, True, (late,), T0, T0),
                              Side.BUY) == []
    _, r = twice(db, [row(acct, 500)], [late])
    assert r.clean and r.findings == ()


def test_recovery_ignores_orders_sent_after_its_read():
    """Review F-7: recovery's orders read could not show a bot order sent after the read was asked (exits run during
    RECOVERING while the plan waits for the tradebook read). Such a WORKING exit is not vanished (no INVARIANT event,
    no HALT(INVARIANT, GLOBAL, ACK), no CRITICAL) and is neither looked up nor applied: its own timers resolve it.
    A WORKING order sent before the read and missing from it is still a vanished order."""
    acct = Account.LIVE
    read_at = T0 + 30.0
    db = Db(acct)
    late_leg = db.leg(1, state=LegState.EXITING)
    entry = db.order(late_leg, Role.ENTRY, 500, broker_id="B1E")
    late = db.order(late_leg, Role.EXIT, 0, state=OrderState.WORKING, broker_id="B1X", attempt=2)
    late.submitted_mono = read_at + 0.4                        # sent after the read was asked
    old_leg = db.leg(2, state=LegState.EXITING)
    old_entry = db.order(old_leg, Role.ENTRY, 500, broker_id="B2E")
    old = db.order(old_leg, Role.EXIT, 0, state=OrderState.WORKING, broker_id="B2X", attempt=2)
    kw = dict(account=acct, session=S, now_wall=W("10:00"), eod_flatten_at=W("15:15"),
              structures=[], trade_qty={entry.order_pk: 500, old_entry.order_pk: 500},
              positions=PositionsSnapshot(acct, True, (row(acct, 1000),), read_at, read_at + 0.2))
    orders = OrdersSnapshot(acct, True, (entry_copy(db, entry), entry_copy(db, old_entry)), read_at, read_at + 0.3)

    old.submitted_mono = read_at - 5.0                         # the other one was working before the read
    plan = recon.plan_recovery(orders=orders, legs=db.legs, **kw)
    assert plan.vanished == (old.order_pk,)
    assert [(h.kind, h.scope, h.clears) for h in plan.halts] == [("INVARIANT", "GLOBAL", HaltClears.ACK)]
    assert [(a.severity, a.code) for a in plan.alerts] == [("CRITICAL", "INVARIANT")]
    assert [e.payload["order_pk"] for e in plan.events if e.kind == "INVARIANT_VIOLATION"] == [old.order_pk]
    assert late.order_pk not in plan.lookups and late.order_pk not in {pk for pk, _ in plan.apply}
    assert PollOrder(acct, late.order_pk, "B1X") not in plan.commands

    old.submitted_mono = read_at                               # the same instant as the read: still counted
    assert recon.plan_recovery(orders=orders, legs=db.legs, **kw).vanished == (old.order_pk,)
    old.submitted_mono = read_at + 0.1                         # both sent after the read: nothing vanished
    plan = recon.plan_recovery(orders=orders, legs=db.legs, **kw)
    assert plan.vanished == () and plan.halts == () and plan.alerts == ()
    assert not [e for e in plan.events if e.kind == "INVARIANT_VIOLATION"]
