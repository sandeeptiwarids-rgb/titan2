"""titan2.core.recon: reconciliation, verify-flat, the recovery planner and the EOD truth check (spec §9)."""

from __future__ import annotations

from datetime import date, time as dtime

import pytest

from titan2.clock import ist_datetime
from titan2.core import recon
from titan2.core.events import (
    Command, FetchOrders, FetchPositions, FetchTradebook, FetchTrades, OrdersSnapshot, PollOrder,
    PositionsSnapshot, Priority,
)
from titan2.core.recon import (
    BotView, Cap, CoverPlan, ExternalFill, ReconClass, ReconMemory, SymbolView, VerifyState, broker_cap,
    entries_may_resume, eod_truth_check, external_wait, plan_recovery, post_fill, reconcile, symbol_view,
    verify_flat,
)
from titan2.model import (
    ROLE_SIDE, Account, BrokerOrder, BrokerPosition, BrokerTrade, Direction, HaltClears, Instrument, Intent, Leg,
    LegState, Order, OrderState, PnlStatus, Policy, Recheck, Role, Side, StrikePref, StructState, Structure,
    StructureType, external_order_pk, make_tag,
)
from titan2.settings import DEFAULTS

S = date(2026, 10, 12)               # a Monday session
PREV = date(2026, 10, 9)             # the previous session (Friday)
SYM = "NFO:RELIANCE26OCT2900CE"
SYM2 = "NFO:INFY26OCT1500PE"
T0 = 1000.0                          # monotonic time of the last bot fill in most tests
SQOFF = dtime(15, 26)
ALLOWED_COMMANDS = (FetchTrades, FetchPositions, FetchOrders, FetchTradebook, PollOrder)


def W(hms: str, d: date = S):
    return ist_datetime(d, hms)


def inst(symbol: str = SYM, lot: int = 500) -> Instrument:
    ts = symbol.split(":", 1)[1]
    return Instrument(symbol, 12345, ts, "NFO", ts[:4], "NSE:" + ts[:4], ts[-2:], 100.0, date(2026, 10, 27), lot, 0.05)


class Db:
    """A tiny stand-in for the engine's rows: legs bound to one order index, as EngineState keeps them."""

    def __init__(self, account: Account = Account.PAPER, session: date = S) -> None:
        self.account, self.session = account, session
        self.index: dict[str, Order] = {}
        self.legs: list[Leg] = []

    def leg(self, leg_id: int, symbol: str = SYM, state: LegState = LegState.OPEN, session: date | None = None,
            seq_no: int | None = None, last_fill: float | None = T0, entry_wall=None, book: str | None = None) -> Leg:
        sess = session or self.session
        lg = Leg(leg_id, leg_id, 1, self.account, book or f"{self.account}:opening_drive", sess, seq_no or leg_id,
                 inst(symbol), state=state, last_fill_mono=last_fill,
                 entry_fill_wall=entry_wall if entry_wall is not None else W("10:00", sess))
        lg.bind_orders(self.index)
        self.legs.append(lg)
        return lg

    def order(self, lg: Leg, role: Role, filled: int, qty: int | None = None, state: OrderState = OrderState.COMPLETE,
              broker_id: str | None = None, attempt: int = 1, avg: float = 10.0, side: Side | None = None,
              status_msg: str | None = None) -> Order:
        side = side or ROLE_SIDE.get(role, Side.SELL)
        pk = (external_order_pk(broker_id) if role == Role.EXTERNAL
              else make_tag(role, self.account, lg.session, lg.seq_no, attempt))
        o = Order(pk, lg.leg_id, lg.structure_id, self.account, lg.book, role, side, lg.inst.symbol,
                  qty or max(filled, 500), avg if role != Role.EXTERNAL else None, attempt, 0, state=state,
                  broker_order_id=broker_id, status_msg=status_msg, filled_qty=filled,
                  avg_fill_px=avg if filled else None)
        self.index[pk] = o
        lg.orders.append(pk)
        return o

    def view(self, **kw) -> BotView:
        return BotView(self.account, self.session, SQOFF, tuple(self.legs), **kw)


def row(net: int, symbol: str = SYM, product: str = "MIS", account: Account = Account.PAPER, sell_qty: int = 0,
        sell_value: float = 0.0, buy_value: float = 0.0, exchange: str | None = None) -> BrokerPosition:
    ts = symbol.split(":", 1)[1]
    return BrokerPosition(account, symbol, ts, exchange or symbol.split(":", 1)[0], product, net, net + sell_qty,
                          sell_qty, buy_value, sell_value)


def psnap(*rows: BrokerPosition, req: float, ok: bool = True, account: Account = Account.PAPER) -> PositionsSnapshot:
    return PositionsSnapshot(account, ok, tuple(rows), req, req + 0.2, None if ok else "NetworkException 429")


def bord(oid: str, side: Side, filled: int, symbol: str = SYM, tag: str | None = None, avg: float | None = 10.0,
         wall=None, placed_by: str | None = "API", product: str = "MIS", account: Account = Account.PAPER,
         status: str = "COMPLETE") -> BrokerOrder:
    ts = symbol.split(":", 1)[1]
    return BrokerOrder(account, oid, tag, symbol, ts, "NFO", product, side, "LIMIT", "IOC", max(filled, 1), filled,
                       avg if filled else None, status, status, None, wall, wall, placed_by)


def osnap(*orders: BrokerOrder, req: float, ok: bool = True, account: Account = Account.PAPER) -> OrdersSnapshot:
    return OrdersSnapshot(account, ok, tuple(orders), req, req + 0.2, None if ok else "NetworkException 429")


def entry_bord(db: Db, o: Order) -> BrokerOrder:
    """The broker's copy of a bot order (same tag and filled qty)."""
    return bord(o.broker_order_id or "B" + o.order_pk, o.side, o.filled_qty, symbol=o.symbol, tag=o.order_pk,
                avg=o.avg_fill_px, wall=W("10:00", db.session), account=db.account)


def assert_never_trades(*results) -> None:
    """Recon never emits an order: only reads, polls and trade fetches (a cover is a CoverPlan, not a command)."""
    for r in results:
        for c in getattr(r, "commands", ()):
            assert isinstance(c, ALLOWED_COMMANDS), c


def kinds(halts) -> list[tuple[str, str, HaltClears]]:
    return [(h.kind, h.scope, h.clears) for h in halts]


def open_leg(db: Db, qty: int = 500, leg_id: int = 1, **kw) -> Leg:
    lg = db.leg(leg_id, **kw)
    o = db.order(lg, Role.ENTRY, qty, broker_id=f"B{leg_id}E")
    o.avg_fill_px = 10.0
    return lg


# ---------------------------------------------------------------------------------------------------------------
# Acceptance tests
# ---------------------------------------------------------------------------------------------------------------
def test_post_fill_rule_ignores_lag():
    db = Db()
    lg = open_leg(db)
    lag = psnap(row(0), req=T0 + 1.5)                        # requested < last fill + 2 s: pre-fill positions
    fresh = psnap(row(500), req=T0 + 2.0)
    assert not post_fill(lag, lg) and post_fill(fresh, lg) and post_fill(fresh, T0)
    assert post_fill(lag, None)                              # no fill yet: any ok snapshot counts
    assert not post_fill(psnap(row(500), req=T0 + 9, ok=False), lg)

    # positions_lag(8): every read before T0+8 shows the pre-fill 0. Recon at the 5 s cadence never calls it EXTERNAL.
    mem = ReconMemory()
    for now in (T0 + 1.6, T0 + 3.0, T0 + 5.0, T0 + 10.0, T0 + 15.0, T0 + 120.0):
        net = 0 if now < T0 + 8 else 500
        r = reconcile(db.view(), psnap(row(net), req=now), osnap(req=now), mem, now + 0.2)
        mem = r.memory
        assert r.status in (ReconClass.PENDING, ReconClass.CLEAN), (now, r.status)
        assert not r.halts and not r.alerts and all(not f.closes and f.cap_qty is None for f in r.findings)
        assert_never_trades(r)
    assert r.clean and mem.sightings == {}

    # worst case: reads every 0.5 s (external-wait + periodic recon), a lag that still shows the pre-fill 0 AT
    # fill + 8.0 s, and each read evaluated late. Grace is judged at the request time, so it never confirms.
    mem = ReconMemory()
    for k in range(4, 25):
        req = T0 + 0.5 * k
        net = 0 if req <= T0 + 8.0 else 500
        r = reconcile(db.view(), psnap(row(net), req=req), osnap(req=req), mem, req + 0.45)
        mem = r.memory
        assert r.status in (ReconClass.PENDING, ReconClass.CLEAN) and not r.halts, (req, r.status)
    assert r.clean

    # the exit cap never comes from a pre-fill snapshot: it waits and asks for one requested after T0 + 2
    sv = symbol_view(db.view(), SYM)
    cap = broker_cap(sv, 500, lag, None, None, T0 + 1.8, account=db.account)
    assert cap.wait and cap.commands == ()                   # a read sent now could not count yet
    cap = broker_cap(sv, 500, lag, None, cap.wait_start, T0 + 2.1, account=db.account)
    assert cap.wait and cap.commands == (FetchPositions(Account.PAPER, Priority.EXIT),)
    cap = broker_cap(sv, 500, fresh, None, cap.wait_start, T0 + 2.4, account=db.account)
    assert (cap.qty, cap.source, cap.wait_start) == (500, "POSITIONS", None)

    # verify-flat never reads a pre-exit (lagging) 500 as a residual
    db2 = Db()
    lg2 = db2.leg(1, last_fill=T0)
    db2.order(lg2, Role.ENTRY, 500)
    db2.order(lg2, Role.EXIT, 500, attempt=2)
    sv2 = symbol_view(db2.view(), SYM)
    d = verify_flat(1, sv2, psnap(row(500), req=T0 + 1.0), None, VerifyState(T0), T0 + 1.2, account=db2.account)
    assert d.outcome == "WAIT" and d.halts == ()
    d = verify_flat(1, sv2, psnap(row(0), req=T0 + 2.0), None, d.state, T0 + 2.3, account=db2.account)
    assert (d.outcome, d.via) == ("CLOSED", "POSITIONS")


def test_two_snapshot_confirmation():
    db = Db()
    open_leg(db)
    view = db.view()
    t = T0 + 10                                              # outside grace, post-fill
    gone = row(0, sell_qty=500, sell_value=5500.0)           # the account sold the 500 (the row shows the sale)
    r1 = reconcile(view, psnap(gone, req=t), osnap(req=t), ReconMemory(), t + 0.2)
    assert r1.status is ReconClass.PENDING and r1.findings[0].detail == "difference seen once"
    assert not r1.halts and r1.memory.sightings[(SYM, "MIS")].req_mono == t
    r2 = reconcile(view, psnap(gone, req=t + 2.0), osnap(req=t + 2.0), r1.memory, t + 2.2)
    assert r2.status is ReconClass.PENDING and r2.memory.sightings[(SYM, "MIS")].req_mono == t   # first kept
    r3 = reconcile(view, psnap(gone, req=t + 3.0), osnap(req=t + 3.0), r2.memory, t + 3.2)
    assert r3.status is ReconClass.EXTERNAL_CLOSE
    assert kinds(r3.halts) == [("MISMATCH", "ACCOUNT:PAPER", HaltClears.ACK)]

    # the two snapshots must agree: a changed broker qty restarts the confirmation
    r2b = reconcile(view, psnap(row(200), req=t + 3.5), osnap(req=t + 3.5), r1.memory, t + 3.6)
    assert r2b.status is ReconClass.PENDING and r2b.memory.sightings[(SYM, "MIS")].broker_qty == 200

    # in grace (a working bot order on the symbol): nothing is recorded, so nothing can confirm
    db_g = Db()
    lg = open_leg(db_g)
    db_g.order(lg, Role.EXIT, 0, state=OrderState.WORKING, broker_id="BX", attempt=2)
    m = ReconMemory()
    for k in range(4):
        tt = t + 4.0 * k
        r = reconcile(db_g.view(), psnap(row(0), req=tt), osnap(req=tt), m, tt + 0.2)
        m = r.memory
        assert r.status is ReconClass.PENDING and m.sightings == {} and not r.halts
    # a fresh fill (5 s grace) also blocks sightings
    db_f = Db()
    open_leg(db_f, last_fill=t)
    r = reconcile(db_f.view(), psnap(row(0), req=t + 2.5), osnap(req=t + 2.5), ReconMemory(), t + 3.0)
    assert r.status is ReconClass.PENDING and r.memory.sightings == {}

    # a fill after the first sighting makes it pre-fill: it cannot be half of a confirmation
    db_n = Db()
    open_leg(db_n, last_fill=t + 1.0)                        # the bot filled again after the sighting at t
    r = reconcile(db_n.view(), psnap(row(0), req=t + 8.0), osnap(req=t + 8.0), r1.memory, t + 8.2)
    assert r.status is ReconClass.PENDING and r.memory.sightings[(SYM, "MIS")].req_mono == t + 8.0
    assert_never_trades(r1, r2, r3, r2b, r)


def _confirmed_close(db: Db, pos_rows, orders, t: float = T0 + 10, **view_kw):
    view = db.view(**view_kw)
    mem = reconcile(view, psnap(*pos_rows, req=t, account=db.account), osnap(*orders, req=t, account=db.account),
                    ReconMemory(), t).memory
    return reconcile(view, psnap(*pos_rows, req=t + 3, account=db.account),
                     osnap(*orders, req=t + 3, account=db.account), mem, t + 3.1)


def test_external_final_estimated_unresolved():
    # FINAL: a manual SELL after the entry explains the missing 500 exactly; its avg is the price; trades fetched
    db = Db(Account.LIVE)
    open_leg(db)
    manual = bord("M1", Side.SELL, 500, avg=12.5, wall=W("11:00"), placed_by="AB1234", account=Account.LIVE)
    early = bord("M0", Side.SELL, 500, avg=9.0, wall=W("09:30"), placed_by="AB1234", account=Account.LIVE)
    r = _confirmed_close(db, [row(0, account=Account.LIVE, sell_qty=500, sell_value=6250.0)], [early, manual],
                         api_placed_by=frozenset({"AB1234"}))
    (f,) = r.findings
    (c,) = f.closes
    assert f.cls is ReconClass.EXTERNAL_CLOSE and c.pnl_status is PnlStatus.FINAL and c.external_kind == "MANUAL"
    assert c.fills == (ExternalFill("M1", 500, 12.5, W("11:00")),) and c.orders == (manual,)   # M0 predates entry
    assert f.commands == (FetchTrades(Account.LIVE, "EXM1", "M1"),) and r.commands == f.commands
    assert r.live_demote and not r.g1_mismatch

    # an entry fill the bot learned late (WS updates lost, found by the 3 s poll): the square-off placed between the
    #   real fill and its receipt still explains the close, because the window opens when the entry order was
    #   created (an order that closed the position cannot come earlier), not when the fill was received
    db = Db()
    late = db.leg(1, entry_wall=W("10:00:03"))
    db.order(late, Role.ENTRY, 500, broker_id="B1E").created_wall = W("10:00:00")
    sq = bord("Q1", Side.SELL, 500, avg=9.5, wall=W("10:00:01.500"), tag=None, placed_by="AB1234")
    (c,) = _confirmed_close(db, [row(0, sell_qty=500, sell_value=4750.0)], [sq]).findings[0].closes
    assert c.pnl_status is PnlStatus.FINAL and c.fills == (ExternalFill("Q1", 500, 9.5, W("10:00:01.500")),)
    # ... while one placed before the entry order existed explains nothing
    db = Db()
    late = db.leg(1, entry_wall=W("10:00:03"))
    db.order(late, Role.ENTRY, 500, broker_id="B1E").created_wall = W("10:00:00")
    old = bord("Q0", Side.SELL, 500, avg=9.5, wall=W("09:59:59"), tag=None, placed_by="AB1234")
    (c,) = _confirmed_close(db, [row(0, sell_qty=500, sell_value=4750.0)], [old]).findings[0].closes
    assert c.pnl_status is PnlStatus.ESTIMATED and c.orders == ()

    # ESTIMATED: no order visible; the positions row gives (sell_value − bot sells)/(sell_qty − bot sells)
    db = Db()
    lg = db.leg(1)
    db.order(lg, Role.ENTRY, 1000, broker_id="B1E")
    db.order(lg, Role.EXIT, 500, avg=11.0, broker_id="B1X", attempt=2)
    r = _confirmed_close(db, [row(0, sell_qty=1000, sell_value=500 * 11.0 + 500 * 13.0)], [])
    (c,) = r.findings[0].closes
    assert (c.qty, c.pnl_status, c.fills) == (500, PnlStatus.ESTIMATED, (ExternalFill(None, 500, 13.0, None),))
    assert r.g1_mismatch                                     # a MANUAL close on paper is a G1 mismatch

    # orders that do not explain the qty exactly: ESTIMATED, the visible order is still attached
    db = Db()
    open_leg(db)
    part = bord("M2", Side.SELL, 300, avg=12.0, wall=W("11:00"), placed_by="AB1234")
    r = _confirmed_close(db, [row(0, sell_qty=500, sell_value=6100.0)], [part])
    (c,) = r.findings[0].closes
    assert c.pnl_status is PnlStatus.ESTIMATED
    assert c.fills == (ExternalFill("M2", 300, 12.2, W("11:00")), ExternalFill(None, 200, 12.2, None))

    # UNRESOLVED: the sale shows (today's orders hold the bot's BUY and a SELL that took the 500) but nothing prices
    #   it; valued at the last realizable bid (None when unknown)
    db = Db()
    lg = open_leg(db)
    unpriced = bord("M9", Side.SELL, 500, avg=None, wall=W("11:00"), placed_by="AB1234")
    seen = [entry_bord(db, db.index[lg.orders[0]]), unpriced]
    r = _confirmed_close(db, [], seen, last_rb={1: 11.2})
    (c,) = r.findings[0].closes
    assert (c.pnl_status, c.fills, c.orders) == (PnlStatus.UNRESOLVED, (ExternalFill("M9", 500, 11.2, W("11:00")),),
                                                 (unpriced,))
    r = _confirmed_close(db, [], seen)
    assert r.findings[0].closes[0].fills == (ExternalFill("M9", 500, None, W("11:00")),)
    # ... while no sale anywhere (no positions row, no orders) is a CONTRADICTION: nothing is booked
    r = _confirmed_close(db, [], [], last_rb={1: 11.2})
    assert r.findings[0].cls is ReconClass.CONTRADICTION and r.findings[0].closes == ()
    assert_never_trades(r)


def test_broker_squareoff_class():
    # LIVE: an untagged, non-API order at/after BROKER_SQUAREOFF_AT -> BROKER_SQUAREOFF: CRITICAL, no MISMATCH halt
    db = Db(Account.LIVE)
    open_leg(db)
    rows = [row(0, account=Account.LIVE, sell_qty=500, sell_value=5000.0)]
    sq = bord("S1", Side.SELL, 500, avg=10.0, wall=W("15:26:02"), tag=None, placed_by="RMSADMIN",
              account=Account.LIVE)
    r = _confirmed_close(db, rows, [sq], t=T0 + 30, api_placed_by=frozenset({"AB1234"}))
    (f,) = r.findings
    assert f.cls is ReconClass.BROKER_SQUAREOFF and r.status is ReconClass.BROKER_SQUAREOFF
    assert f.closes[0].external_kind == "AUTO_SQUAREOFF" and f.closes[0].pnl_status is PnlStatus.FINAL
    assert r.halts == () and [(a.severity, a.code) for a in r.alerts] == [("CRITICAL", "BROKER_SQUAREOFF")]
    assert not r.live_demote

    # the same order before 15:26 is a MANUAL close: HALT(MISMATCH, ACCOUNT, ACK) + CRITICAL
    early = bord("S1", Side.SELL, 500, wall=W("15:20"), placed_by="RMSADMIN", account=Account.LIVE)
    r = _confirmed_close(db, rows, [early], t=T0 + 30, api_placed_by=frozenset({"AB1234"}))
    assert r.findings[0].cls is ReconClass.EXTERNAL_CLOSE and r.findings[0].closes[0].external_kind == "MANUAL"
    assert kinds(r.halts) == [("MISMATCH", "ACCOUNT:LIVE", HaltClears.ACK)]
    assert [(a.severity, a.code) for a in r.alerts] == [("CRITICAL", "MISMATCH")]

    # a tagged order our own API placed after 15:26 is still MANUAL
    api = bord("A1", Side.SELL, 500, wall=W("15:27"), tag="OTHERAPP", placed_by="AB1234", account=Account.LIVE)
    r = _confirmed_close(db, rows, [api], t=T0 + 30, api_placed_by=frozenset({"AB1234"}))
    assert r.findings[0].cls is ReconClass.EXTERNAL_CLOSE

    # PAPER: the simulated square-off (placed_by EXCHANGE, tag SQOFF) is BROKER_SQUAREOFF and not a G1 mismatch
    db = Db(Account.PAPER)
    open_leg(db)
    sim = bord("P261012000000009", Side.SELL, 500, avg=9.5, wall=W("15:26"), tag="SQOFF", placed_by="EXCHANGE")
    r = _confirmed_close(db, [row(0, sell_qty=500, sell_value=4750.0)], [sim], t=T0 + 30)
    assert r.findings[0].cls is ReconClass.BROKER_SQUAREOFF and not r.g1_mismatch

    # a leftover from an earlier session squared off by the paper exchange at startup today -> BROKER_SQUAREOFF
    db = Db(Account.PAPER)
    lg = db.leg(1, session=PREV, entry_wall=W("10:00", PREV))
    db.order(lg, Role.ENTRY, 500, broker_id="P261009000000001")
    sim = bord("P261012000000001", Side.SELL, 500, avg=0.05, wall=W("08:59:30"), tag="SQOFF", placed_by="EXCHANGE")
    r = _confirmed_close(db, [row(0, sell_qty=500, sell_value=25.0)], [sim], t=T0 + 30)
    assert r.findings[0].cls is ReconClass.BROKER_SQUAREOFF and r.findings[0].legs == (1,)
    assert_never_trades(r)


def test_foreign_ack_rehalt():
    db = Db(Account.LIVE)
    open_leg(db)
    t = T0 + 20
    pos_rows = [row(500, account=Account.LIVE), row(50, symbol=SYM2, account=Account.LIVE)]

    def run(mem, rows_, tt, **kw):
        return reconcile(db.view(**kw), psnap(*rows_, req=tt, account=Account.LIVE),
                         osnap(req=tt, account=Account.LIVE), mem, tt + 0.1)

    r1 = run(ReconMemory(), pos_rows, t)
    assert r1.status is ReconClass.PENDING and not r1.halts
    r2 = run(r1.memory, pos_rows, t + 3)
    (f,) = r2.findings
    assert (f.cls, f.symbol, f.broker_qty) == (ReconClass.FOREIGN, SYM2, 50)
    assert kinds(r2.halts) == [("UNKNOWN_POSITION", "ACCOUNT:LIVE", HaltClears.ACK)]
    assert [a.severity for a in r2.alerts] == ["CRITICAL"] and r2.live_demote
    r3 = run(r2.memory, pos_rows, t + 6)                     # unchanged: no repeated halt, alert or event spam
    assert r3.status is ReconClass.FOREIGN and r3.halts == () and r3.alerts == () and r3.events == ()

    acks = {(SYM2, "MIS"): 50}
    r4 = run(r3.memory, pos_rows, t + 9, foreign_acks=acks)  # an exact ack makes the account clean
    assert r4.clean and r4.findings == () and r4.memory.reported == {}

    changed = [row(500, account=Account.LIVE), row(75, symbol=SYM2, account=Account.LIVE)]
    r5 = run(r4.memory, changed, t + 12, foreign_acks=acks)
    assert r5.status is ReconClass.PENDING
    r6 = run(r5.memory, changed, t + 15, foreign_acks=acks)  # a changed qty re-halts
    assert r6.findings[0].cls is ReconClass.FOREIGN and "differs" in r6.findings[0].detail
    assert kinds(r6.halts) == [("UNKNOWN_POSITION", "ACCOUNT:LIVE", HaltClears.ACK)]

    # a non-MIS row is FOREIGN even on the bot's own symbol, and so is a cash-equity position
    odd = [row(500, account=Account.LIVE), row(65, product="NRML", account=Account.LIVE),
           row(10, symbol="NSE:SBIN", product="CNC", account=Account.LIVE)]
    r = run(run(ReconMemory(), odd, t).memory, odd, t + 3)
    assert {(f.symbol, f.product, f.cls) for f in r.findings} == {
        (SYM, "NRML", ReconClass.FOREIGN), ("NSE:SBIN", "CNC", ReconClass.FOREIGN)}

    # a residual on a bot symbol is EXCESS until acked; the acked qty is then never sellable
    db_r = Db(Account.PAPER)
    lg = db_r.leg(1, state=LegState.CLOSED)
    db_r.order(lg, Role.ENTRY, 500)
    db_r.order(lg, Role.EXIT, 500, attempt=2)
    mem = reconcile(db_r.view(), psnap(row(50), req=t), osnap(req=t), ReconMemory(), t).memory
    r = reconcile(db_r.view(), psnap(row(50), req=t + 3), osnap(req=t + 3), mem, t + 3)
    assert r.findings[0].cls is ReconClass.EXCESS and kinds(r.halts)[0][0] == "UNKNOWN_POSITION"
    acked = db_r.view(foreign_acks={(SYM, "MIS"): 50})
    assert reconcile(acked, psnap(row(50), req=t + 6), osnap(req=t + 6), r.memory, t + 6).clean
    lg2 = db_r.leg(2, last_fill=t)                           # a new bot leg on the same symbol
    db_r.order(lg2, Role.ENTRY, 500, attempt=1)
    acked = db_r.view(foreign_acks={(SYM, "MIS"): 50})
    cap = broker_cap(symbol_view(acked, SYM), 500, psnap(row(50), req=t + 3), None, None, t + 3,
                     account=Account.PAPER)
    assert cap.qty == 0                                      # someone else sold the bot's 500: never sell the 50
    assert_never_trades(r1, r2, r3, r4, r5, r6, r)


def test_short_cover_bot_explained():
    db = Db()
    lg = db.leg(1)
    e = db.order(lg, Role.ENTRY, 500, broker_id="B1E")
    x = db.order(lg, Role.EXIT, 1000, qty=1000, broker_id="B1X", attempt=2)   # the bot oversold by 500
    orders = [entry_bord(db, e), entry_bord(db, x)]
    t = T0 + 10
    view = db.view()

    def run(mem, req, net=-500, orders_=orders, v=view):
        return reconcile(v, psnap(row(net), req=req), osnap(*orders_, req=req), mem, req + 0.1)

    r1 = run(ReconMemory(), t)
    (f,) = r1.findings
    assert f.cls is ReconClass.SHORT and f.cover is None
    assert kinds(r1.halts) == [("SHORT_DETECTED", "GLOBAL", HaltClears.ACK)]
    assert [(a.severity, a.code) for a in r1.alerts] == [("CRITICAL", "SHORT_DETECTED")]
    r2 = run(r1.memory, t + 1.0)                             # < 2 s apart: not yet
    assert r2.findings[0].cover is None and r2.halts == ()
    r3 = run(r2.memory, t + 2.0)
    assert r3.findings[0].cover == CoverPlan(SYM, 500, 1)
    assert_never_trades(r1, r2, r3)                          # the cover is a plan for the cover loop, not an order

    # someone else's extra sell: bot sells − bot buys (0) != |short| (300) -> alert only
    db2 = Db()
    lg2 = db2.leg(1)
    e2 = db2.order(lg2, Role.ENTRY, 500, broker_id="B1E")
    x2 = db2.order(lg2, Role.EXIT, 500, broker_id="B1X", attempt=2)
    o2 = [entry_bord(db2, e2), entry_bord(db2, x2), bord("M9", Side.SELL, 300, wall=W("11:00"))]
    m = run(ReconMemory(), t, -300, o2, db2.view()).memory
    r = run(m, t + 3, -300, o2, db2.view())
    assert r.findings[0].cls is ReconClass.SHORT and r.findings[0].cover is None
    assert "alert only" in r.findings[0].detail

    # a short on a symbol the bot never traded: halt, never a cover
    db3 = Db()
    m = run(ReconMemory(), t, -65, [], db3.view()).memory
    r = run(m, t + 3, -65, [], db3.view())
    assert r.findings[0].cover is None and kinds(r.halts) == [] and r.status is ReconClass.SHORT  # halt already raised

    # a bot tag the DB does not know: no auto-cover
    stranger = bord("B77", Side.SELL, 500, tag=make_tag(Role.EXIT, Account.PAPER, S, 9, 1), wall=W("11:00"))
    m = run(ReconMemory(), t, orders_=orders + [stranger]).memory
    assert run(m, t + 3, orders_=orders + [stranger]).findings[0].cover is None

    # a working bot order on the symbol (e.g. a cover in flight): no new cover plan
    db.order(lg, Role.COVER, 0, state=OrderState.WORKING, broker_id="B1C", attempt=3)
    v = db.view()
    m = run(ReconMemory(), t, v=v).memory
    assert run(m, t + 3, v=v).findings[0].cover is None


def test_short_cover_hangs_off_the_oversold_leg():
    """Two books hold the symbol today: leg 1 oversold (its exit sold 1500 of a 500 entry), leg 2 (newer, another
    book) still holds 500. The broker is 500 short and the bot's fills explain it exactly: the cover hangs off leg 1,
    never off leg 2, whose trade would book it and turn UNRESOLVED (close() books every order of a leg)."""
    db = Db()
    l1 = db.leg(1)
    e1 = db.order(l1, Role.ENTRY, 500, broker_id="B1E")
    x1 = db.order(l1, Role.EXIT, 1500, qty=1500, broker_id="B1X", attempt=2)
    l2 = db.leg(2, book="PAPER:price_spike_up")
    e2 = db.order(l2, Role.ENTRY, 500, broker_id="B2E")
    orders = [entry_bord(db, o) for o in (e1, x1, e2)]
    t = T0 + 10
    r1 = reconcile(db.view(), psnap(row(-500), req=t), osnap(*orders, req=t), ReconMemory(), t)
    r2 = reconcile(db.view(), psnap(row(-500), req=t + 3), osnap(*orders, req=t + 3), r1.memory, t + 3)
    (f,) = r2.findings
    assert f.cls is ReconClass.SHORT and f.cover == CoverPlan(SYM, 500, 1)
    assert_never_trades(r1, r2)


def test_api_error_no_inference():
    db = Db()
    open_leg(db)
    t = T0 + 10
    good_mem = reconcile(db.view(), psnap(row(0), req=t), osnap(req=t), ReconMemory(), t).memory
    assert good_mem.sightings                                # one sighting of a (real) external close

    mem = good_mem
    results = []
    for k in range(1, 5):
        tt = t + 3 * k
        bad_pos = psnap(row(0), req=tt, ok=False)            # even rows on a failed read are never looked at
        r = reconcile(db.view(), bad_pos, osnap(req=tt), mem, tt)
        results.append(r)
        mem = r.memory
        assert r.status is ReconClass.BROKER_ERROR and r.findings == () and r.commands == ()
        assert mem.sightings == good_mem.sightings           # no inference, and nothing forgotten
        if k < 3:
            assert r.halts == ()
    assert kinds(results[2].halts) == [("STALE_RECON", "ACCOUNT:PAPER", HaltClears.AUTO)]
    assert [(a.severity, a.code) for a in results[2].alerts] == [("WARN", "STALE_RECON")]
    assert results[3].halts == ()                            # raised once per streak
    assert results[0].g1_mismatch

    # an orders failure is a broker error too (the orders view explains repairs and external closes)
    r = reconcile(db.view(), psnap(row(500), req=t), osnap(req=t, ok=False), ReconMemory(), t)
    assert r.status is ReconClass.BROKER_ERROR and r.findings == ()
    r = reconcile(db.view(), None, None, ReconMemory(), t)
    assert r.status is ReconClass.BROKER_ERROR

    # two good reads in a row clear STALE_RECON (AUTO)
    r1 = reconcile(db.view(), psnap(row(500), req=t + 20), osnap(req=t + 20), mem, t + 20)
    r2 = reconcile(db.view(), psnap(row(500), req=t + 25), osnap(req=t + 25), r1.memory, t + 25)
    assert r1.clear_halts == () and kinds(r2.clear_halts) == [("STALE_RECON", "ACCOUNT:PAPER", HaltClears.AUTO)]
    assert r2.memory.error_streak == 0 and r2.clean

    # caps and verify-flat ignore failed reads too
    sv = symbol_view(db.view(), SYM)
    cap = broker_cap(sv, 500, psnap(row(0), req=t, ok=False), osnap(req=t, ok=False), t - 4, t, account=db.account)
    assert cap.wait
    d = verify_flat(1, SymbolView(SYM, last_fill_mono=T0), psnap(row(0), req=t, ok=False), None, VerifyState(t - 1),
                    t, account=db.account)
    assert d.outcome == "WAIT"


def _struct(sid: int, state: StructState, account: Account = Account.LIVE, session: date = S,
            book: str | None = None) -> Structure:
    intent = Intent(f"i{sid}", "opening_drive", "1.0", "NSE:RELI", Direction.UP, StructureType.LONG_CE,
                    StrikePref.ATM, "NEAREST_ELIGIBLE", 100.0, Recheck(0.0, 1e9), 5.0, 0.0, W("09:20", session), {})
    return Structure(sid, session, book or f"{account}:opening_drive", account, Policy.FULL, "opening_drive", "1.0",
                     intent, StructureType.LONG_CE, "NSE:RELI", state, [sid], False, None, None, None, None, None,
                     None, None, None, W("09:20", session))


def test_recovery_planner():
    db = Db(Account.LIVE)
    L = Account.LIVE
    # 1 OPEN: entry filled, no TRADE fills yet -> FetchTrades
    l1 = db.leg(1)
    o1 = db.order(l1, Role.ENTRY, 500, broker_id="B1")
    # 2 ENTERING: the entry was sent but never acked (SUBMITTING, no broker id); the broker has it by tag
    l2 = db.leg(2, state=LegState.ENTERING, last_fill=None)
    o2 = db.order(l2, Role.ENTRY, 0, state=OrderState.SUBMITTING)
    # 3 EXITING: a WORKING exit the broker does not show -> INVARIANT + poll
    l3 = db.leg(3, state=LegState.EXITING)
    db.order(l3, Role.ENTRY, 500, broker_id="B3E")
    o3 = db.order(l3, Role.EXIT, 0, state=OrderState.WORKING, broker_id="B3X", attempt=2)
    # 4 ENTERING: SUBMITTING and not at the broker -> two lookups -> NOT_PLACED (order state machine)
    l4 = db.leg(4, state=LegState.ENTERING, last_fill=None)
    o4 = db.order(l4, Role.ENTRY, 0, state=OrderState.SUBMITTING)
    # 6 ENTERING with a terminal REJECTED entry -> VOID("REJECTED:...")
    l6 = db.leg(6, state=LegState.ENTERING, last_fill=None)
    db.order(l6, Role.ENTRY, 0, state=OrderState.REJECTED, broker_id="B6", status_msg="RMS: margin")
    # 7 ENTERING with a terminal partial fill -> OPEN
    l7 = db.leg(7, state=LegState.ENTERING)
    db.order(l7, Role.ENTRY, 200, state=OrderState.CANCELLED, broker_id="B7")
    # 8 from the previous session, still OPEN -> CLOSED_EXTERNAL UNRESOLVED + HALT(MISMATCH)
    l8 = db.leg(8, symbol=SYM2, session=PREV)
    db.order(l8, Role.ENTRY, 500, broker_id="OLD8")
    structs = [_struct(1, StructState.OPEN), _struct(2, StructState.ENTERING), _struct(3, StructState.EXITING),
               _struct(4, StructState.ENTERING), _struct(5, StructState.RESERVED), _struct(6, StructState.ENTERING),
               _struct(7, StructState.ENTERING), _struct(8, StructState.OPEN, session=PREV),
               _struct(9, StructState.CLOSED)]
    lost_exit = make_tag(Role.EXIT, L, S, 2, 2)              # a later attempt on leg 2 whose row was lost
    orphan = make_tag(Role.ENTRY, L, S, 99, 1)               # a leg the DB never saw
    broker = osnap(
        bord("B1", Side.BUY, 500, tag=o1.order_pk, wall=W("09:30"), account=L),
        bord("B2", Side.BUY, 500, tag=o2.order_pk, wall=W("09:31"), account=L),
        bord("B2X", Side.SELL, 0, tag=lost_exit, wall=W("09:40"), account=L, status="CANCELLED"),
        bord("B99", Side.BUY, 500, tag=orphan, wall=W("09:35"), account=L),
        bord("M1", Side.BUY, 50, symbol=SYM2, tag=None, wall=W("09:36"), account=L),                 # foreign
        bord("P1", Side.BUY, 500, tag=make_tag(Role.ENTRY, Account.PAPER, S, 5, 1), account=L),     # other letter
        bord("Y1", Side.BUY, 500, tag=make_tag(Role.ENTRY, L, PREV, 7, 1), account=L),              # other day
        req=T0, account=L)
    positions = psnap(row(500, account=L), req=T0, account=L)
    kw = dict(account=L, session=S, eod_flatten_at=W("15:15"), structures=structs, legs=db.legs,
              trade_qty={}, orders=broker, positions=positions)
    plan = plan_recovery(now_wall=W("10:00"), **kw)

    assert plan.apply == ((o2.order_pk, broker.orders[1]),)
    assert plan.lookups == (o4.order_pk,)
    assert plan.vanished == (o3.order_pk,)
    assert PollOrder(L, o3.order_pk, "B3X") in plan.commands
    assert [(i.seq_no, i.leg_id, i.book, [b.broker_order_id for b in i.orders]) for i in plan.imports] == [
        (2, 2, "LIVE:opening_drive", ["B2X"]), (99, None, "RECOVERED:LIVE", ["B99"])]
    fetches = [c for c in plan.commands if isinstance(c, FetchTrades)]
    assert FetchTrades(L, o1.order_pk, "B1") in fetches and all(c.broker_order_id != "OLD8" for c in fetches)
    assert plan.open_legs == (7,) and plan.void_legs == ((6, "REJECTED:RMS: margin"),)
    assert plan.void_structures == ((5, "RESTART"),)
    assert plan.flatten_legs == ()
    assert [(s.structure_id, s.action, s.broker_qty) for s in plan.stale] == [(8, "CLOSE_UNRESOLVED", 0)]
    assert ("INVARIANT", "GLOBAL", HaltClears.ACK) in kinds(plan.halts)
    assert ("MISMATCH", "ACCOUNT:LIVE", HaltClears.ACK) in kinds(plan.halts)
    assert {a.code for a in plan.alerts} == {"INVARIANT", "RECOVERED_ORDERS", "MISMATCH"}
    assert all(a.severity == "CRITICAL" for a in plan.alerts)
    assert_never_trades(plan)

    # restart at/after EOD_FLATTEN: OPEN legs (and legs opening now) exit with RECOVERY_FLATTEN; EXITING keep theirs
    late = plan_recovery(now_wall=W("15:16"), **kw)
    assert late.flatten_legs == (1, 7)
    # TRADE fills already explain the entry: no fetch
    done = plan_recovery(now_wall=W("10:00"), **{**kw, "trade_qty": {o1.order_pk: 500, "x": 1}})
    assert FetchTrades(L, o1.order_pk, "B1") not in done.commands

    # reads still failing after 3 tries: STALE_RECON, each order resolved on its own, nothing imported
    blind = plan_recovery(now_wall=W("10:00"), **{**kw, "orders": osnap(req=T0, ok=False, account=L)})
    assert ("STALE_RECON", "ACCOUNT:LIVE", HaltClears.AUTO) in kinds(blind.halts)
    assert PollOrder(L, o3.order_pk, "B3X") in blind.commands and set(blind.lookups) == {o2.order_pk, o4.order_pk}
    assert blind.apply == () and blind.imports == () and blind.vanished == ()

    # PAPER leftovers wait for the paper exchange's startup square-off; no LIVE-only trade fetches
    pdb = Db(Account.PAPER)
    p1 = pdb.leg(1, session=PREV)
    pdb.order(p1, Role.ENTRY, 500, broker_id="P1")
    p2 = pdb.leg(2)
    pdb.order(p2, Role.ENTRY, 500, broker_id="P2")
    pplan = plan_recovery(account=Account.PAPER, session=S, now_wall=W("09:00"), eod_flatten_at=W("15:15"),
                          structures=[_struct(1, StructState.OPEN, Account.PAPER, PREV),
                                      _struct(2, StructState.OPEN, Account.PAPER)],
                          legs=pdb.legs, trade_qty={}, orders=osnap(req=T0), positions=psnap(req=T0))
    assert [(s.structure_id, s.action) for s in pplan.stale] == [(1, "AWAIT_PAPER_SQUAREOFF")]
    assert pplan.halts == () and pplan.commands == ()

    # entries resume only after 2 consecutive CLEAN recons on every account, a warm feed and finished prep
    clean2 = ReconMemory(status="CLEAN", clean_streak=2, last_clean_mono=T0)
    clean1 = ReconMemory(status="CLEAN", clean_streak=1, last_clean_mono=T0)
    accts = (Account.LIVE, Account.PAPER)
    ok_kw = dict(feed_warm=True, prep_done=True, entry_halted=False)
    assert entries_may_resume({Account.LIVE: clean2, Account.PAPER: clean2}, accts, **ok_kw)
    assert not entries_may_resume({Account.LIVE: clean2, Account.PAPER: clean1}, accts, **ok_kw)
    assert not entries_may_resume({Account.LIVE: clean2}, accts, **ok_kw)
    assert not entries_may_resume({Account.LIVE: clean2, Account.PAPER: clean2}, accts,
                                  **{**ok_kw, "feed_warm": False})


def test_recovery_planner_never_imports_another_titan2s_live_orders():
    """import_tags=False (LIVE in the laptop profile, which sends no LIVE order): today's titan2 LIVE orders that the
    DB lacks were placed by another titan2 (EC2) on the shared Kite account. None is imported, so nothing here would
    manage or sell them: one HALT(UNKNOWN_POSITION, ACCOUNT:LIVE) and one CRITICAL alert instead."""
    L = Account.LIVE
    ec2_entry, ec2_selftest = make_tag(Role.ENTRY, L, S, 1, 1), make_tag(Role.SELFTEST, L, S, 2, 1)
    broker = osnap(bord("E1", Side.BUY, 500, tag=ec2_entry, wall=W("09:30"), account=L),
                   bord("E2", Side.BUY, 0, symbol=SYM2, tag=ec2_selftest, wall=W("09:15:30"), account=L,
                        status="CANCELLED"),
                   bord("M1", Side.BUY, 50, symbol=SYM2, tag=None, wall=W("09:36"), account=L),      # a manual trade
                   req=T0, account=L)
    kw = dict(account=L, session=S, now_wall=W("10:00"), eod_flatten_at=W("15:15"), structures=[], legs=[],
              trade_qty={}, orders=broker, positions=psnap(row(500, account=L), req=T0, account=L))
    assert [(i.seq_no, i.book) for i in plan_recovery(**kw).imports] == [(1, "RECOVERED:LIVE"), (2, "RECOVERED:LIVE")]
    plan = plan_recovery(**kw, import_tags=False)                 # the laptop: the same reads, nothing imported
    assert plan.imports == () and plan.apply == () and plan.lookups == () and plan.commands == ()
    assert kinds(plan.halts) == [("UNKNOWN_POSITION", "ACCOUNT:LIVE", HaltClears.ACK)]
    (alert,) = plan.alerts
    assert (alert.severity, alert.code, alert.dedup_key) == ("CRITICAL", "UNKNOWN_POSITION",
                                                             "recovery:LIVE:not_imported")
    assert alert.text.startswith("titan2 LIVE orders exist in this Kite account today (2 order(s) on "
                                 f"{SYM2}, {SYM}) that this titan2.db did not place")
    assert alert.text.endswith("manage them in Kite or on EC2") and plan.halts[0].reason == alert.text
    (ev,) = plan.events
    assert (ev.kind, ev.severity, ev.payload["not_imported"]) == ("UNKNOWN_POSITION", "CRITICAL",
                                                                  [ec2_entry, ec2_selftest])     # by seq
    assert_never_trades(plan)
    # no titan2 tag of today missing from the DB (a manual order only): nothing to say
    quiet = plan_recovery(**{**kw, "orders": osnap(broker.orders[2], req=T0, account=L)}, import_tags=False)
    assert quiet.halts == () and quiet.alerts == () and quiet.imports == ()


# ---------------------------------------------------------------------------------------------------------------
# Further cases
# ---------------------------------------------------------------------------------------------------------------
def test_recovery_stale_structures():
    """Earlier-session leftovers are never traded again; each gets the one honest disposition."""
    L = Account.LIVE
    db = Db(L)
    held = db.leg(1, session=PREV)                            # LIVE, still holding -> CLOSE_UNRESOLVED + MISMATCH
    db.order(held, Role.ENTRY, 500, broker_id="O1")
    done = db.leg(2, symbol=SYM2, session=PREV, state=LegState.VERIFY_FLAT)   # fully exited -> CLOSE
    db.order(done, Role.ENTRY, 500, broker_id="O2")
    db.order(done, Role.EXIT, 500, broker_id="O2X", attempt=2)
    unsent = db.leg(3, symbol="NFO:TCS26OCT4000CE", session=PREV, state=LegState.ENTERING, last_fill=None)
    u = db.order(unsent, Role.ENTRY, 0, state=OrderState.SUBMITTING)       # outcome unknowable today
    structs = [_struct(1, StructState.OPEN, session=PREV), _struct(2, StructState.VERIFY_FLAT, session=PREV),
               _struct(3, StructState.ENTERING, session=PREV), _struct(4, StructState.RESERVED, session=PREV)]
    plan = plan_recovery(account=L, session=S, now_wall=W("09:05"), eod_flatten_at=W("15:15"), structures=structs,
                         legs=db.legs, trade_qty={}, orders=osnap(req=T0, account=L),
                         positions=psnap(req=T0, account=L))
    assert [(s.structure_id, s.action, s.working) for s in plan.stale] == [
        (1, "CLOSE_UNRESOLVED", ()), (2, "CLOSE", ()), (3, "CLOSE_UNRESOLVED", (u.order_pk,)), (4, "VOID", ())]
    assert kinds(plan.halts) == [("MISMATCH", "ACCOUNT:LIVE", HaltClears.ACK)] * 2
    assert plan.lookups == () and plan.void_structures == ()   # leftovers are not matched against today's orders

    pdb = Db(Account.PAPER)
    pheld = pdb.leg(1, session=PREV)
    pdb.order(pheld, Role.ENTRY, 500, broker_id="P1")
    pnone = pdb.leg(2, symbol=SYM2, session=PREV, state=LegState.ENTERING, last_fill=None)
    pdb.order(pnone, Role.ENTRY, 0, state=OrderState.SUBMITTING)
    pplan = plan_recovery(account=Account.PAPER, session=S, now_wall=W("09:05"), eod_flatten_at=W("15:15"),
                          structures=[_struct(1, StructState.OPEN, Account.PAPER, PREV),
                                      _struct(2, StructState.ENTERING, Account.PAPER, PREV)],
                          legs=pdb.legs, trade_qty={}, orders=osnap(req=T0), positions=psnap(req=T0))
    assert [(s.structure_id, s.action) for s in pplan.stale] == [(1, "AWAIT_PAPER_SQUAREOFF"), (2, "VOID")]
    assert pplan.halts == ()


def test_terminal_order_fills_are_never_repaired():
    """REPAIRED needs a bot order that can still take the broker's fills. A terminal order is immutable (I9, the DB
    trigger; orders.handle records the contradiction and applies nothing), so a terminal order the broker shows with
    more fills leaves the gap unexplained: EXCESS with HALT(UNKNOWN_POSITION) once confirmed, never traded. (A
    REPAIRED that cannot be applied kept the account non-CLEAN forever, with no halt and no alert.)"""
    db = Db()
    lg = db.leg(1)
    e = db.order(lg, Role.ENTRY, 300, qty=500, state=OrderState.CANCELLED, broker_id="B1E")
    broker = bord("B1E", Side.BUY, 500, tag=e.order_pk, wall=W("10:00"))
    t = T0 + 10
    first = reconcile(db.view(), psnap(row(500), req=t), osnap(broker, req=t), ReconMemory(), t)
    assert [(f.cls, f.repairs) for f in first.findings] == [(ReconClass.PENDING, ())] and first.halts == ()
    r = reconcile(db.view(), psnap(row(500), req=t + 3), osnap(broker, req=t + 3), first.memory, t + 3)
    (f,) = r.findings
    assert (f.cls, f.repairs, f.broker_qty, f.expected) == (ReconClass.EXCESS, (), 500, 300)
    assert kinds(r.halts) == [("UNKNOWN_POSITION", "ACCOUNT:PAPER", HaltClears.ACK)] and r.g1_mismatch
    assert [a.severity for a in r.alerts] == ["CRITICAL"]
    assert_never_trades(first, r)

    # a phantom exit (recorded NOT_PLACED, filled at the broker): the EXITING leg cannot wait on it forever. Once
    # confirmed it is an external close priced FINAL from the broker's copy of that very order (its fills are real;
    # it becomes an EXTERNAL row), with HALT(MISMATCH)
    db2 = Db()
    lg2 = open_leg(db2, state=LegState.EXITING)
    x = db2.order(lg2, Role.EXIT, 0, qty=500, state=OrderState.NOT_PLACED, attempt=2)
    sold = bord("B1X", Side.SELL, 500, tag=x.order_pk, avg=12.0, wall=W("10:05"))
    flat = row(0, sell_qty=500, sell_value=6000.0, buy_value=5000.0)
    ords = (entry_bord(db2, db2.index[lg2.orders[0]]), sold)
    mem = reconcile(db2.view(), psnap(flat, req=t), osnap(*ords, req=t), ReconMemory(), t).memory
    r = reconcile(db2.view(), psnap(flat, req=t + 3), osnap(*ords, req=t + 3), mem, t + 3)
    (f,) = r.findings
    assert f.cls is ReconClass.EXTERNAL_CLOSE and f.repairs == ()
    (ext,) = f.closes
    assert (ext.qty, ext.pnl_status, [(fl.broker_order_id, fl.qty, fl.px) for fl in ext.fills]) == (
        500, PnlStatus.FINAL, [("B1X", 500, 12.0)])
    assert ext.orders == (sold,) and f.commands == (FetchTrades(Account.PAPER, "EXB1X", "B1X"),)
    assert x.order_pk in f.detail and "NOT_PLACED" in f.detail
    assert kinds(r.halts) == [("MISMATCH", "ACCOUNT:PAPER", HaltClears.ACK)]
    assert_never_trades(r)
    # an excess the bot's orders do not explain is EXCESS (confirmed), never traded
    mem = reconcile(db.view(), psnap(row(800), req=t), osnap(broker, req=t), ReconMemory(), t).memory
    r = reconcile(db.view(), psnap(row(800), req=t + 3), osnap(broker, req=t + 3), mem, t + 3)
    assert r.findings[0].cls is ReconClass.EXCESS and kinds(r.halts)[0][0] == "UNKNOWN_POSITION"
    assert_never_trades(r)


def test_partial_external_caps_at_broker_qty():
    db = Db()
    open_leg(db)
    t = T0 + 10
    part = row(200, sell_qty=300, sell_value=3300.0)          # the row shows the 300 that were sold
    mem = reconcile(db.view(), psnap(part, req=t), osnap(req=t), ReconMemory(), t).memory
    r = reconcile(db.view(), psnap(part, req=t + 3), osnap(req=t + 3), mem, t + 3)
    (f,) = r.findings
    assert (f.cls, f.cap_qty, f.legs) == (ReconClass.PARTIAL_EXTERNAL, 200, (1,))
    assert kinds(r.halts) == [("MISMATCH", "ACCOUNT:PAPER", HaltClears.ACK)]
    cap = broker_cap(symbol_view(db.view(), SYM), 500, psnap(part, req=t + 3), None, None, t + 3,
                     account=Account.PAPER)
    assert (cap.qty, cap.source) == (200, "POSITIONS")
    # the same drop backed by today's orders (the bot's BUY and a manual SELL of 300) is PARTIAL_EXTERNAL too
    lg = db.legs[0]
    ords = (entry_bord(db, db.index[lg.orders[0]]), bord("M3", Side.SELL, 300, wall=W("11:00"), placed_by="AB1234"))
    mem = reconcile(db.view(), psnap(row(200), req=t), osnap(*ords, req=t), ReconMemory(), t).memory
    r = reconcile(db.view(), psnap(row(200), req=t + 3), osnap(*ords, req=t + 3), mem, t + 3)
    assert (r.findings[0].cls, r.findings[0].cap_qty) == (ReconClass.PARTIAL_EXTERNAL, 200)
    # ... but a drop nothing backs (no sale on the row, the orders still show the bot's 500) is a CONTRADICTION
    mem = reconcile(db.view(), psnap(row(200), req=t), osnap(ords[0], req=t), ReconMemory(), t).memory
    r = reconcile(db.view(), psnap(row(200), req=t + 3), osnap(ords[0], req=t + 3), mem, t + 3)
    assert (r.findings[0].cls, r.findings[0].cap_qty) == (ReconClass.CONTRADICTION, None)


def test_clean_status_streak_and_events():
    db = Db()
    open_leg(db)
    mem = ReconMemory()
    r = None
    for k in range(3):
        t = T0 + 10 + 5 * k
        r = reconcile(db.view(), psnap(row(500), req=t), osnap(req=t), mem, t + 0.3)
        assert r.clean and r.findings == () and r.memory.last_clean_mono == t + 0.3
        assert [e.kind for e in r.events] == (["RECON"] if k == 0 else [])
        mem = r.memory
    assert mem.clean_streak == 3 and mem.clean_within(T0 + 30, 30.0) and not mem.clean_within(T0 + 61, 30.0)
    r = reconcile(db.view(), psnap(row(0), req=T0 + 40), osnap(req=T0 + 40), mem, T0 + 40)
    assert r.memory.clean_streak == 0 and r.memory.last_clean_mono == mem.last_clean_mono


def test_broker_cap_sources():
    sv = SymbolView(SYM, expected=500, last_fill_mono=T0, legs=(1,))
    acct = Account.PAPER
    stale_pos = psnap(row(500), req=T0 - 5)
    cap = broker_cap(sv, 500, stale_pos, None, None, T0 + 2.5, account=acct)
    assert cap.wait and cap.wait_start == T0 + 2.5 and cap.commands == (FetchPositions(acct, Priority.EXIT),)
    # a read is in flight: no duplicate request
    cap = broker_cap(sv, 500, stale_pos, None, cap.wait_start, T0 + 3, account=acct, pos_in_flight=True)
    assert cap.wait and cap.commands == ()
    # after 5 s the post-fill orders view caps (all orders on the symbol, MIS only)
    ords = osnap(bord("X", Side.BUY, 500, wall=W("10:00")), bord("Y", Side.SELL, 100, wall=W("10:05")),
                 bord("Z", Side.BUY, 900, product="NRML"), req=T0 + 3)
    cap = broker_cap(sv, 500, psnap(req=T0 + 7, ok=False), ords, T0 + 2.5, T0 + 7.6, account=acct,
                     pos_in_flight=True)
    assert (cap.qty, cap.source) == (400, "ORDERS")
    # between 5 s and 10 s with no usable orders view either: still WAIT, and the orders view is requested
    cap = broker_cap(sv, 500, psnap(req=T0 + 7, ok=False), osnap(req=T0 + 1, ok=False), T0 + 2.5, T0 + 9.0,
                     account=acct, pos_in_flight=True)
    assert cap.wait and cap.wait_start == T0 + 2.5 and cap.commands == (FetchOrders(acct, Priority.EXIT),)
    # both reads failing for 10 s: exit on the bot's own fills, with EXIT_UNCAPPED (WARN)
    cap = broker_cap(sv, 500, psnap(req=T0 + 12, ok=False), osnap(req=T0 + 12, ok=False), T0 + 2.5, T0 + 12.6,
                     account=acct)
    assert (cap.qty, cap.source) == (500, "UNCAPPED") and cap.alerts[0].code == "EXIT_UNCAPPED"
    assert cap.alerts[0].severity == "WARN" and cap.events[0].kind == "EXIT_UNCAPPED"
    # another bot order on the symbol is working: exits on one symbol never overlap
    busy = SymbolView(SYM, expected=500, last_fill_mono=T0, in_flight=True)
    assert broker_cap(busy, 500, psnap(row(500), req=T0 + 9), None, None, T0 + 10, account=acct).wait
    # a short broker row never yields a negative cap
    assert broker_cap(sv, 500, psnap(row(-50), req=T0 + 3), None, None, T0 + 3, account=acct).qty == 0
    # external_wait places nothing: it only asks for fresh reads
    cmds = external_wait(sv, psnap(row(0), req=T0 + 3), osnap(req=T0 - 1), T0 + 5.5, account=acct)
    assert cmds == (FetchPositions(acct, Priority.EXIT), FetchOrders(acct, Priority.EXIT))
    assert isinstance(Cap(None, "WAIT", None), Cap)

    # G3: a post-fill read made BEFORE the exit was decided is not used. The position may have been closed in
    #   between (a manual close in the Kite app, an RMS square-off): selling it again would open a naked short.
    old_read = psnap(row(500), req=T0 + 20)                   # post-fill, but 3 s older than the decision
    cap = broker_cap(sv, 500, old_read, None, None, T0 + 23, account=acct, decided_mono=T0 + 23)
    assert cap.wait and cap.commands == (FetchPositions(acct, Priority.EXIT),)
    cap = broker_cap(sv, 500, psnap(row(0), req=T0 + 23.1), None, cap.wait_start, T0 + 23.4, account=acct,
                     decided_mono=T0 + 23)
    assert (cap.qty, cap.source) == (0, "POSITIONS")          # flat at the broker: external_wait, never a sell
    assert broker_cap(sv, 500, old_read, None, None, T0 + 23, account=acct).qty == 500    # without the rule
    # the orders-view fallback obeys the same rule
    ords_old = osnap(bord("X", Side.BUY, 500, wall=W("10:00")), req=T0 + 20)
    cap = broker_cap(sv, 500, psnap(req=T0 + 28, ok=False), ords_old, T0 + 23, T0 + 28.5, account=acct,
                     pos_in_flight=True, decided_mono=T0 + 23)
    assert cap.wait and cap.commands == (FetchOrders(acct, Priority.EXIT),)


def test_broker_cap_when_the_reads_after_the_decision_fail():
    """The exit waits for one read made after its decision (G3), and a failing broker API never leaves it worse off
    than the spec's rule: a failed positions read sends it to the orders view at once, failed reads are re-asked no
    faster than every VERIFY_FLAT_RECHECK_S (a broken API never becomes a read storm), and only once
    BROKER_READS_FAIL_UNCAP_S has passed without a read made after the decision does the newest post-fill read from
    before it cap the exit (CRITICAL), never above what that read shows. Decision B1 is strict on this path too: this
    test used to pin a cap from the older reads the moment both post-decision reads had failed (review of the B1
    batch: two read errors re-opened the naked-short window)."""
    sv = SymbolView(SYM, expected=500, last_fill_mono=T0, legs=(1,))
    acct = Account.PAPER
    old_pos = psnap(row(500), req=T0 + 20)                    # post-fill, 3 s before the decision
    old_ords = osnap(bord("X", Side.BUY, 500, wall=W("10:00")), req=T0 + 19)
    # the positions read made after the decision failed: ask the orders view at once (no 5 s wait), and do not
    #   ask positions again within 2 s of the failure
    cap = broker_cap(sv, 500, old_pos, old_ords, T0 + 23, T0 + 23.3, account=acct, decided_mono=T0 + 23,
                     pos_failed_mono=T0 + 23.1)
    assert cap.wait and cap.commands == (FetchOrders(acct, Priority.EXIT),)
    # a post-decision orders read that shows the position sold outside the bot caps the exit at 0: never a sell
    sold = osnap(bord("X", Side.BUY, 500, wall=W("10:00")), bord("M", Side.SELL, 500, wall=W("10:05")),
                 req=T0 + 23.3)
    cap = broker_cap(sv, 500, old_pos, sold, cap.wait_start, T0 + 23.5, account=acct, decided_mono=T0 + 23,
                     pos_failed_mono=T0 + 23.1)
    assert (cap.qty, cap.source) == (0, "ORDERS")
    # both reads made after the decision failed: the older reads cap nothing yet; the exit waits, re-asking each
    #   read 2 s after it failed ...
    fails = dict(account=acct, decided_mono=T0 + 23, pos_failed_mono=T0 + 23.1, ords_failed_mono=T0 + 23.3)
    cap = broker_cap(sv, 500, old_pos, old_ords, T0 + 23, T0 + 23.5, **fails)
    assert cap.wait and cap.commands == ()
    cap = broker_cap(sv, 500, old_pos, old_ords, T0 + 23, T0 + 25.3, **fails)
    assert cap.wait and cap.commands == (FetchPositions(acct, Priority.EXIT), FetchOrders(acct, Priority.EXIT))
    assert broker_cap(sv, 500, old_pos, old_ords, T0 + 23, T0 + 32.9, **fails).wait
    # ... until BROKER_READS_FAIL_UNCAP_S: then the newest post-fill positions read caps it, CRITICAL
    cap = broker_cap(sv, 500, old_pos, old_ords, T0 + 23, T0 + 33, **fails)
    assert (cap.qty, cap.source) == (500, "POSITIONS_BEFORE_DECISION")
    assert [(e.kind, e.severity) for e in cap.events] == [("EXIT_CAP_BEFORE_DECISION", "CRITICAL")]
    assert [(a.severity, a.code) for a in cap.alerts] == [("CRITICAL", "EXIT_CAP_BEFORE_DECISION")]
    # ... else the newest post-fill orders view
    cap = broker_cap(sv, 500, None, old_ords, T0 + 23, T0 + 33, **fails)
    assert (cap.qty, cap.source) == (500, "ORDERS_BEFORE_DECISION")
    # reads that hang (in flight, no failure seen) wait the same 10 s; then the older read caps the exit, so a close
    #   it saw is never sold (EXIT_UNCAPPED, which this replaces when such a read exists, would sell all 500)
    hung = dict(account=acct, decided_mono=T0 + 23, pos_in_flight=True, ords_in_flight=True)
    assert broker_cap(sv, 500, old_pos, old_ords, T0 + 23, T0 + 32.9, **hung).wait
    cap = broker_cap(sv, 500, psnap(row(0), req=T0 + 20), old_ords, T0 + 23, T0 + 33, **hung)
    assert (cap.qty, cap.source, cap.commands) == (0, "POSITIONS_BEFORE_DECISION", ())
    # failures from before the decision do not count: the exit still waits for its own read
    cap = broker_cap(sv, 500, old_pos, old_ords, None, T0 + 23, account=acct, decided_mono=T0 + 23,
                     pos_failed_mono=T0 + 21, ords_failed_mono=T0 + 21)
    assert cap.wait and cap.commands == (FetchPositions(acct, Priority.EXIT),)
    # nor does one a later read answered (that read predates a new fill): wait for positions, no early orders view
    refilled = SymbolView(SYM, expected=500, last_fill_mono=T0 + 23.4, legs=(1,))
    cap = broker_cap(refilled, 500, psnap(row(500), req=T0 + 23.5), old_ords, T0 + 25.5, T0 + 26, account=acct,
                     decided_mono=T0 + 23, pos_failed_mono=T0 + 23.1)
    assert cap.wait and cap.commands == (FetchPositions(acct, Priority.EXIT),)
    # no post-fill read at all: the spec's waits and EXIT_UNCAPPED stay as they were
    fresh_fill = SymbolView(SYM, expected=500, last_fill_mono=T0 + 22, legs=(1,))
    cap = broker_cap(fresh_fill, 500, old_pos, old_ords, T0 + 24, T0 + 26, account=acct, decided_mono=T0 + 23,
                     pos_failed_mono=T0 + 24.1, ords_failed_mono=T0 + 24.2)
    assert cap.wait
    cap = broker_cap(fresh_fill, 500, old_pos, old_ords, T0 + 24, T0 + 34.1, account=acct, decided_mono=T0 + 23,
                     pos_failed_mono=T0 + 33, ords_failed_mono=T0 + 33)
    assert (cap.qty, cap.source) == (500, "UNCAPPED")
    # read pacing after a failure, also for external_wait and verify_flat
    assert external_wait(sv, old_pos, old_ords, T0 + 24, account=acct, pos_failed_mono=T0 + 23,
                         ords_failed_mono=T0 + 23) == ()
    assert external_wait(sv, old_pos, old_ords, T0 + 25.1, account=acct, pos_failed_mono=T0 + 23,
                         ords_failed_mono=T0 + 23) == (FetchPositions(acct, Priority.EXIT),
                                                       FetchOrders(acct, Priority.EXIT))
    flat = SymbolView(SYM, expected=0, last_fill_mono=T0, legs=(1,))
    d = verify_flat(1, flat, None, None, VerifyState(T0), T0 + 4, account=acct, pos_failed_mono=T0 + 3)
    assert d.outcome == "WAIT" and d.commands == ()
    d = verify_flat(1, flat, None, None, VerifyState(T0), T0 + 5, account=acct, pos_failed_mono=T0 + 3)
    assert d.commands == (FetchPositions(acct, Priority.EXIT),)


def test_verify_flat_outcomes():
    acct = Account.PAPER
    sv = SymbolView(SYM, expected=0, last_fill_mono=T0)
    st = VerifyState(T0)
    # residual: confirmed by two post-fill snapshots >= 3 s apart, both requested > 5 s after the fill; never sold
    d = verify_flat(1, sv, psnap(row(100), req=T0 + 3), None, st, T0 + 3.2, account=acct)
    assert d.outcome == "WAIT" and d.state.residual is None   # within 5 s of the fill: not a sighting yet
    d = verify_flat(1, sv, psnap(row(100), req=T0 + 5), None, d.state, T0 + 5.6, account=acct)
    assert d.outcome == "WAIT" and d.state.residual is None   # the boundary is still grace
    d = verify_flat(1, sv, psnap(row(100), req=T0 + 5.5), None, d.state, T0 + 5.7, account=acct)
    assert d.outcome == "WAIT" and d.state.residual.req_mono == T0 + 5.5
    d = verify_flat(1, sv, psnap(row(100), req=T0 + 7.5), None, d.state, T0 + 7.7, account=acct)
    assert d.outcome == "WAIT" and d.commands == ()          # just read: re-ask no faster than every 2 s
    d = verify_flat(1, sv, psnap(row(100), req=T0 + 7.5), None, d.state, T0 + 9.6, account=acct)
    assert d.outcome == "WAIT" and d.commands == (FetchPositions(acct, Priority.EXIT),)
    d = verify_flat(1, sv, psnap(row(100), req=T0 + 8.5), None, d.state, T0 + 9.8, account=acct)
    assert (d.outcome, d.residual_qty) == ("RESIDUAL", 100)
    assert kinds(d.halts) == [("MISMATCH", "ACCOUNT:PAPER", HaltClears.ACK)] and d.alerts[0].severity == "CRITICAL"
    assert_never_trades(d)
    # short -> the SHORT path (recon raises the halt)
    assert verify_flat(1, sv, psnap(row(-100), req=T0 + 3), None, st, T0 + 3, account=acct).outcome == "SHORT"
    # another open leg on the same symbol holds 500: a broker net of 500 is flat for this leg
    other = SymbolView(SYM, expected=500, last_fill_mono=T0, legs=(2,))
    assert verify_flat(1, other, psnap(row(500), req=T0 + 3), None, st, T0 + 3, account=acct).outcome == "CLOSED"
    # positions failing for > 10 s: the post-fill orders view may close it
    ords = osnap(bord("X", Side.BUY, 500), bord("Y", Side.SELL, 500), req=T0 + 11)
    d = verify_flat(1, sv, psnap(req=T0 + 12, ok=False), ords, st, T0 + 12.5, account=acct)
    assert (d.outcome, d.via) == ("CLOSED", "ORDERS") and d.events[0].payload["outcome"] == "VERIFY_FLAT_VIA_ORDERS"
    ords_open = osnap(bord("X", Side.BUY, 500), req=T0 + 11)
    d = verify_flat(1, sv, psnap(req=T0 + 12, ok=False), ords_open, st, T0 + 12.5, account=acct)
    assert d.outcome == "WAIT"
    # still unresolved after 30 s: CRITICAL once
    d = verify_flat(1, sv, psnap(req=T0 + 31, ok=False), ords_open, d.state, T0 + 31, account=acct)
    assert d.outcome == "WAIT" and d.alerts[0].code == "VERIFY_FLAT_STUCK" and d.state.critical_sent
    d = verify_flat(1, sv, psnap(req=T0 + 33, ok=False), ords_open, d.state, T0 + 33, account=acct)
    assert d.alerts == ()


def test_two_books_one_symbol():
    db = Db()
    l1 = db.leg(1, book="PAPER:opening_drive", entry_wall=W("10:00"))
    db.order(l1, Role.ENTRY, 500, broker_id="B1")
    l2 = db.leg(2, book="PAPER:price_spike_up", entry_wall=W("10:05"))
    db.order(l2, Role.ENTRY, 500, broker_id="B2")
    sv = symbol_view(db.view(), SYM)
    assert (sv.expected, sv.legs) == (1000, (1, 2))
    assert reconcile(db.view(), psnap(row(1000), req=T0 + 9), osnap(req=T0 + 9), ReconMemory(), T0 + 9).clean
    # the paper square-off closes both legs with one order: FIFO per leg, oldest leg first
    sim = bord("P1", Side.SELL, 1000, avg=9.0, wall=W("15:26"), tag="SQOFF", placed_by="EXCHANGE")
    r = _confirmed_close(db, [row(0, sell_qty=1000, sell_value=9000.0)], [sim], t=T0 + 30)
    (f,) = r.findings
    assert f.cls is ReconClass.BROKER_SQUAREOFF and [(c.leg_id, c.qty) for c in f.closes] == [(1, 500), (2, 500)]
    # each leg's fill is its share of the one order (close_external books it as a charges.OrderPart: the order's
    #   brokerage and square-off fee are booked once, split between the books)
    assert [c.fills for c in f.closes] == [(ExternalFill("P1", 500, 9.0, W("15:26"), (0, 500, 1000, 9000.0)),),
                                           (ExternalFill("P1", 500, 9.0, W("15:26"), (500, 1000, 1000, 9000.0)),)]
    assert f.commands == (FetchTrades(Account.PAPER, "EXP1", "P1"),)
    # an order one leg takes whole is no share
    db1 = Db()
    solo = db1.leg(1, book="PAPER:opening_drive", entry_wall=W("10:00"))
    db1.order(solo, Role.ENTRY, 500, broker_id="B1")
    reconcile(db1.view(), psnap(row(500), req=T0 + 9), osnap(req=T0 + 9), ReconMemory(), T0 + 9)
    r1 = _confirmed_close(db1, [row(0, sell_qty=500, sell_value=4500.0)],
                          [bord("P1", Side.SELL, 500, avg=9.0, wall=W("15:26"), tag="SQOFF", placed_by="EXCHANGE")],
                          t=T0 + 30)
    assert r1.findings[0].closes[0].fills == (ExternalFill("P1", 500, 9.0, W("15:26")),)


def test_eod_truth_check():
    def net_fn(orders):                                       # a stand-in charges function: ₹20 per order
        return sum((o.value if o.side is Side.SELL else -o.value) - 20.0 for o in orders)

    trades = [BrokerTrade(Account.LIVE, "t1", "B1", SYM, Side.BUY, 300, 10.0, None, None),
              BrokerTrade(Account.LIVE, "t2", "B1", SYM, Side.BUY, 200, 10.5, None, None),
              BrokerTrade(Account.LIVE, "t3", "B2", SYM, Side.SELL, 500, 11.0, None, None),
              BrokerTrade(Account.LIVE, "t4", "M9", SYM2, Side.BUY, 50, 3.0, None, None)]   # a manual trade
    books = {"B1": ("LIVE:od", None), "B2": ("LIVE:od", None)}
    broker = 500 * 11.0 - (300 * 10.0 + 200 * 10.5) - 40.0   # 360.0
    ok = eod_truth_check(Account.LIVE, S, {"LIVE:od": broker + 0.6}, books, trades, net_fn)
    assert ok.ok and ok.broker_net == 360.0 and ok.unattributed == ("M9",) and ok.alerts == ()
    bad = eod_truth_check(Account.LIVE, S, {"LIVE:od": broker + 1.5}, books, trades, net_fn)
    assert not bad.ok and bad.alerts[0].code == "EOD_TRUTH_MISMATCH" and bad.alerts[0].severity == "CRITICAL"
    assert bad.events[0].kind == "EOD_TRUTH_CHECK" and bad.g1_mismatch_books == ()

    # paper: exact per book; a 1-paisa drift is a G1 mismatch for that book only
    ptr = [BrokerTrade(Account.PAPER, "p1", "P1", SYM, Side.BUY, 500, 10.0, None, None),
           BrokerTrade(Account.PAPER, "p2", "P2", SYM, Side.SELL, 500, 11.0, None, None),
           BrokerTrade(Account.PAPER, "p3", "P3", SYM2, Side.BUY, 50, 2.0, None, None),
           BrokerTrade(Account.PAPER, "p4", "P4", SYM2, Side.SELL, 50, 2.0, None, None)]
    pbooks = {"P1": ("PAPER:a", None), "P2": ("PAPER:a", None), "P3": ("PAPER:b", None),
              "P4": ("PAPER:b", "AUTO_SQUAREOFF")}
    led = {"PAPER:a": 460.0, "PAPER:b": -40.0}
    assert eod_truth_check(Account.PAPER, S, led, pbooks, ptr, net_fn).ok
    drift = eod_truth_check(Account.PAPER, S, {**led, "PAPER:b": -40.01}, pbooks, ptr, net_fn)
    assert not drift.ok and drift.g1_mismatch_books == ("PAPER:b",)
    # a paper trade the ledger cannot attribute taints every book of the account
    stray = ptr + [BrokerTrade(Account.PAPER, "p5", "P5", SYM, Side.SELL, 50, 1.0, None, None)]
    assert eod_truth_check(Account.PAPER, S, led, pbooks, stray, net_fn).g1_mismatch_books == ("PAPER:a", "PAPER:b")
    # no tradebook: fails closed
    blind = eod_truth_check(Account.PAPER, S, led, pbooks, None, net_fn)
    assert not blind.ok and blind.broker_net is None and blind.g1_mismatch_books == ("PAPER:a", "PAPER:b")

    # the default recompute is charges.net_pnl with the session's rates (the one P&L function)
    from titan2 import charges
    fills = [charges.OrderFills(Side.BUY, 500, 5000.0), charges.OrderFills(Side.SELL, 500, 5500.0)]
    want = charges.net_pnl(fills, charges.rates_for(S)).net
    real = eod_truth_check(Account.LIVE, S, {"LIVE:od": want}, {"B1": ("LIVE:od", None), "B2": ("LIVE:od", None)},
                           [BrokerTrade(Account.LIVE, "a", "B1", SYM, Side.BUY, 500, 10.0, None, None),
                            BrokerTrade(Account.LIVE, "b", "B2", SYM, Side.SELL, 500, 11.0, None, None)])
    assert real.ok and real.broker_net == want

    # one broker order shared by two books (one square-off of a contract both hold): each book is compared on its
    #   share, value and charges allocated as close_external booked them (charges.OrderPart)
    r = charges.rates_for(S)
    sq = [BrokerTrade(Account.PAPER, "s1", "Q1", SYM, Side.SELL, 600, 9.95, None, None),
          BrokerTrade(Account.PAPER, "s2", "Q1", SYM, Side.SELL, 400, 9.90, None, None)]
    whole = charges.OrderFills(Side.SELL, 1000, 600 * 9.95 + 400 * 9.90, "AUTO_SQUAREOFF")
    led2 = {"PAPER:a": charges.net_pnl([charges.OrderPart(whole, 0, 500)], r).net,
            "PAPER:b": charges.net_pnl([charges.OrderPart(whole, 500, 1000)], r).net}
    qbooks = {"Q1": ("PAPER:a", "AUTO_SQUAREOFF")}
    split = {"Q1": [("PAPER:a", 500), ("PAPER:b", 500)]}
    tc = eod_truth_check(Account.PAPER, S, led2, qbooks, sq, parts=split)
    assert tc.ok and [b.book for b in tc.books] == ["PAPER:a", "PAPER:b"]
    assert round(sum(b.broker_net for b in tc.books), 2) == charges.net_pnl([whole], r).net   # booked once
    # without the shares the whole order lands on one book: a mismatch for both
    assert eod_truth_check(Account.PAPER, S, led2, qbooks, sq).g1_mismatch_books == ("PAPER:a", "PAPER:b")
    # shares that do not add up to the order are not trusted
    short = {"Q1": [("PAPER:a", 500), ("PAPER:b", 400)]}
    assert not eod_truth_check(Account.PAPER, S, led2, qbooks, sq, parts=short).ok


def test_recon_due_cadence():
    assert recon.recon_due(T0, W("08:59:59"), None, False) is False
    assert recon.recon_due(T0, W("09:00"), None, False)
    assert not recon.recon_due(T0 + 9, W("10:00"), T0, False) and recon.recon_due(T0 + 10, W("10:00"), T0, False)
    assert recon.recon_due(T0 + 5, W("10:00"), T0, True)
    assert not recon.recon_due(T0 + 60, W("15:31:01"), T0, True)
    # the engine's window end (its last EOD recon, BROKER_SQUAREOFF_AT + 5 min) extends the window, never shortens it
    assert recon.recon_due(T0 + 60, W("15:38"), T0, True, window_end=dtime(15, 44))      # a 15:39 square-off
    assert not recon.recon_due(T0 + 60, W("15:44:01"), T0, True, window_end=dtime(15, 44))
    assert recon.recon_due(T0 + 60, W("15:30"), T0, True, window_end=dtime(15, 25))      # a 15:20 square-off
    # the interval is the RECON_INTERVAL_S setting (the engine passes its Settings; the fallback is the default)
    assert recon.RECON_INTERVAL_S == DEFAULTS.RECON_INTERVAL_S
    assert not recon.recon_due(T0 + 7, W("10:00"), T0, False) and recon.recon_due(T0 + 7, W("10:00"), T0, False, 7.0)
    assert recon.recon_fetches(Account.LIVE) == (FetchPositions(Account.LIVE, Priority.RECON),
                                                 FetchOrders(Account.LIVE, Priority.RECON))
    assert recon.recovery_fetches(Account.LIVE)[2] == FetchTradebook(Account.LIVE)


@pytest.mark.parametrize("account", [Account.LIVE, Account.PAPER, Account.SHADOW])
def test_same_code_every_account(account):
    """Paper and live reconcile through identical code: only the scope letter of the halt differs."""
    db = Db(account)
    open_leg(db)
    t = T0 + 10
    gone = row(0, account=account, sell_qty=500, sell_value=5000.0)
    mem = reconcile(db.view(), psnap(gone, req=t, account=account),
                    osnap(req=t, account=account), ReconMemory(), t).memory
    r = reconcile(db.view(), psnap(gone, req=t + 3, account=account),
                  osnap(req=t + 3, account=account), mem, t + 3)
    assert r.status is ReconClass.EXTERNAL_CLOSE and kinds(r.halts) == [
        ("MISMATCH", f"ACCOUNT:{account.value}", HaltClears.ACK)]
    assert r.g1_mismatch == (account is not Account.LIVE) and r.live_demote == (account is Account.LIVE)
    assert isinstance(r.commands, tuple) and all(isinstance(c, Command) for c in r.commands)
