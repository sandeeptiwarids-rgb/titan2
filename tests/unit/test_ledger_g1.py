"""U16: titan2.ledger (trade rows from fills, pub exports, daily report) and titan2.g1 (G1 and the live review)."""

from __future__ import annotations

import dataclasses
import gzip
import hashlib
import itertools
import json
import math
import random
from datetime import date, datetime, time, timedelta

import pytest

from titan2 import g1, ledger
from titan2 import store as st
from titan2.charges import rates_for
from titan2.clock import IST, iso
from titan2.model import (
    EXPIRY_RULE_NEAREST, ROLE_SIDE, Account, DecisionQuote, Direction, Fill, Instrument, Intent, InvariantViolation,
    Leg, LegState, Order, OrderState, PnlStatus, Policy, Recheck, Role, Side, StrikePref, Structure, StructState,
    StructureType, external_order_pk, make_tag,
)
from titan2.store import Store

STRAT = "opening_drive"
V = "1.3f2a9c01de"
SESSION = date(2026, 10, 12)
REG = datetime(2026, 5, 1, 9, 0, tzinfo=IST)          # trial registration, before every seeded trade
DAY0 = date(2026, 5, 4)                                # first seeded session (2026-04-01 rates, like NOW)
NOW = datetime(2026, 10, 12, 15, 38, tzinfo=IST)
LOT = 50
_uid = itertools.count(1)


def at(hms: str, d: date = SESSION) -> datetime:
    return datetime.combine(d, datetime.strptime(hms, "%H:%M:%S.%f" if "." in hms else "%H:%M:%S").time(), IST)


@pytest.fixture
def core():
    with st.core_thread():
        yield


@pytest.fixture
def store(tmp_path, core):
    s = Store(tmp_path / "db" / "titan2.db", backup_dir=tmp_path / "backups")
    yield s
    s.close()


def fresh(tmp_path, name: str) -> Store:
    return Store(tmp_path / name / "titan2.db", backup_dir=tmp_path / name / "backups")


# ---------------------------------------------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------------------------------------------
def inst_of(opt: str = "CE", lot: int = LOT) -> Instrument:
    ts = f"RELIANCE26OCT2900{opt}"
    return Instrument(f"NFO:{ts}", 1001 if opt == "CE" else 1002, ts, "NFO", "RELIANCE", "NSE:RELIANCE", opt, 2900.0,
                      date(2026, 10, 27), lot, 0.05)


def make_intent(iid: str, created: datetime, *, strategy: str = STRAT, version: str = V,
                direction: Direction = Direction.UP, is_placebo: bool = False, parent: str | None = None) -> Intent:
    structure = StructureType.LONG_CE if direction is Direction.UP else StructureType.LONG_PE
    return Intent(iid, strategy, version, "NSE:RELIANCE", direction, structure, StrikePref.ITM1, EXPIRY_RULE_NEAREST,
                  2900.0, Recheck(2885.5, 2929.0), 10.0, 0.0, created, {"ret_pct": 2.7}, is_placebo, parent)


def make_structure(sid: int, it: Intent, *, book: str = f"PAPER:{STRAT}", account: Account = Account.PAPER,
                   session: date = SESSION, risk: float | None = None, exit_reason: str | None = "TAKE_PROFIT",
                   state: StructState = StructState.CLOSED, policy: Policy = Policy.FULL,
                   parent: int | None = None, blocked_by: str | None = None,
                   pnl_status: PnlStatus | None = None) -> Structure:
    return Structure(structure_id=sid, session=session, book=book, account=account, policy=policy,
                     strategy=it.strategy, strategy_version=it.strategy_version, intent=it, type=it.structure,
                     underlying=it.underlying, state=state, legs=[], is_placebo=it.is_placebo,
                     parent_structure_id=parent, blocked_by=blocked_by, decision=None, placebo_status=None,
                     exit_params=None, risk_basis_rs=risk, exit_reason=exit_reason, void_reason=None,
                     reserved_wall=it.created_wall, opened_wall=None, closed_wall=None, pnl_status=pnl_status)


def make_leg(stc: Structure, leg_id: int, seq_no: int, inst: Instrument, **kw) -> Leg:
    return Leg(leg_id=leg_id, structure_id=stc.structure_id, leg_no=1, account=stc.account, book=stc.book,
               session=stc.session, seq_no=seq_no, inst=inst, state=LegState.CLOSED, **kw)


def make_order(lg: Leg, role: Role, attempt: int, qty: int, filled: int, avg: float | None, state: OrderState,
               dec: DecisionQuote | None, *, limit: float | None = None, ext: str | None = None,
               created: datetime | None = None, broker_id: str | None = None) -> Order:
    if role is Role.EXTERNAL:
        pk, side = external_order_pk(broker_id), Side.SELL
    else:
        pk, side = make_tag(role, lg.account, lg.session, lg.seq_no, attempt), ROLE_SIDE[role]
    return Order(order_pk=pk, leg_id=lg.leg_id, structure_id=lg.structure_id, account=lg.account, book=lg.book,
                 role=role, side=side, symbol=lg.inst.symbol, qty=qty, limit_px=limit, attempt=attempt,
                 band_idx=0 if role is Role.EXIT else None, state=state, broker_order_id=broker_id,
                 external_kind=ext, filled_qty=filled, avg_fill_px=avg, decision=dec,
                 created_wall=created or at("09:20:00"))


def fill(o: Order, qty: int, px: float, wall: datetime, cum: int | None = None) -> Fill:
    return Fill(o.order_pk, f"cum:{cum if cum is not None else qty}", "CUM", qty, px, wall, None)


def persist(s: Store, stc: Structure, lg: Leg, orders: list[Order], fills: list[Fill], session: date) -> None:
    s.insert_intent(stc.intent, session)
    s.insert_structure(stc)
    s.insert_leg(lg)
    for o in orders:
        s.insert_order(o)
    for f in fills:
        s.insert_fill(f)


def put_trade(s: Store, *, session: date, exit_px: float, entry_px: float = 100.0, strategy: str = STRAT,
              version: str = V, book: str | None = None, account: Account = Account.PAPER, intent_id: str | None = None,
              parent: int | None = None, placebo: bool = False, spread: float = 0.10,
              entry_walk: float | None = None, exit_walk: float | None = None, order_qty: int = LOT,
              filled: int = LOT, pnl_status: PnlStatus = PnlStatus.FINAL, blocked_by: str | None = None,
              minute: int = 0, external_manual: bool = False, booked: bool = False) -> int:
    """Book one closed trade through the real path: DAO rows, effective fills, ledger.build_trade_row. booked: the
    structure carries its booking time (closed_ts), as positions.close stamps it: 5 s after the exit fill."""
    book = book or f"PAPER:{strategy}"
    t_in = datetime.combine(session, time(10, 0), IST) + timedelta(minutes=minute)
    t_out = t_in + timedelta(minutes=20)
    direction = Direction.DOWN if placebo else Direction.UP
    policy = Policy.FULL if book.split(":")[0] in ("PAPER", "LIVE") else Policy.QUALITY
    with s.tx():
        iid = intent_id or f"i{next(_uid):011d}"
        it = s.load_intent(iid, 0.0, t_in)
        if it is None:
            it = make_intent(iid, t_in, strategy=strategy, version=version, direction=direction, is_placebo=placebo)
            s.insert_intent(it, session)
        stc = make_structure(0, it, book=book, account=account, session=session, risk=round(entry_px * filled, 2),
                             policy=policy, parent=parent, blocked_by=blocked_by, pnl_status=pnl_status)
        if booked:
            stc.closed_wall = t_out + timedelta(seconds=5)
        s.insert_structure(stc)
        lg = make_leg(stc, 0, s.next_leg_seq(account, session), inst_of("PE" if placebo else "CE"))
        s.insert_leg(lg)
        ent = make_order(lg, Role.ENTRY, 1, order_qty, filled, entry_px,
                         OrderState.COMPLETE if filled == order_qty else OrderState.CANCELLED,
                         DecisionQuote(entry_px - spread, entry_px, entry_px - spread / 2, 120, "REST", entry_px,
                                       entry_walk if entry_walk is not None else entry_px),
                         limit=round(entry_px + 0.10, 2), created=t_in)
        if external_manual:
            ex = make_order(lg, Role.EXTERNAL, 0, filled, filled, exit_px, OrderState.COMPLETE, None,
                            ext="MANUAL", created=t_out, broker_id=f"9{next(_uid):08d}")
        else:
            ex = make_order(lg, Role.EXIT, 1, filled, filled, exit_px, OrderState.COMPLETE,
                            DecisionQuote(exit_px, exit_px + spread, exit_px + spread / 2, 120, "WS", exit_px,
                                          exit_walk if exit_walk is not None else exit_px),
                            limit=round(exit_px - 0.10, 2), created=t_out - timedelta(seconds=1))
        for o in (ent, ex):
            s.insert_order(o)
        s.insert_fill(fill(ent, filled, entry_px, t_in + timedelta(seconds=1)))
        s.insert_fill(fill(ex, filled, exit_px, t_out))
        row = ledger.build_trade_row(stc, [lg], [ent, ex], s.effective_fills([ent.order_pk, ex.order_pk]),
                                     exit_wall=t_out + timedelta(seconds=5), pnl_status=pnl_status)
        s.insert_trade(row)
    return stc.structure_id


_EXITS = (102.0, 104.0, 106.0, 108.0, 110.0)          # u ~ +0.8% .. +8.8% after charges on a Rs 5,000 premium


def seed_paper(s: Store, n: int, *, first: int = 0, per_session: int = 3, exits=_EXITS, placebos: int = 0,
               strategy: str = STRAT, booked: bool = False) -> list[int]:
    """n PAPER:<s> trades (3 per session from DAY0), plus a losing placebo for the first `placebos` of them.
    booked: each structure carries its booking time (put_trade)."""
    ids = []
    with s.tx():                                       # one commit: each put_trade is a savepoint inside it
        for i in range(first, first + n):
            day = DAY0 + timedelta(days=i // per_session)
            sid = put_trade(s, session=day, exit_px=exits[i % len(exits)], strategy=strategy, minute=i % per_session,
                            booked=booked)
            ids.append(sid)
            if i - first < placebos:
                put_trade(s, session=day, exit_px=97.0, strategy=strategy, book=f"PLACEBO:{strategy}",
                          account=Account.SHADOW, parent=sid, placebo=True, minute=i % per_session, booked=booked)
    return ids


def add_bar_checks(s: Store, sessions: int, bad: int = 1) -> None:
    with s.tx():
        for k in range(sessions):
            d = DAY0 + timedelta(days=k)
            s.put_bar_check(d, "NSE:RELIANCE", datetime.combine(d, time(9, 15), IST), k >= bad, "{}")


def start_trial(s: Store, strategy: str = STRAT, mode: str = "PAPER") -> None:
    with s.tx():
        s.put_strategy_mode(strategy, version=V, mode=mode, changed_wall=REG, changed_by="test")
        g1.ensure_trial(s, strategy, V, {"params": {"x": 1}}, REG, 0.0)


def samples(n: int, sessions: int, *, u: float = 0.05, cost: float = 5.0, risk: float = 5000.0, first_id: int = 1,
            status: str = "FINAL") -> list[g1.Sample]:
    return [g1.Sample(first_id + i, (DAY0 + timedelta(days=i * sessions // n)).isoformat(),
                      u * risk * (1 + 0.2 * (i % 5 - 2)), risk, cost, status) for i in range(n)]


def placebo_map(sample: list[g1.Sample], k: int, *, u: float = -0.03, cost: float = 5.0) -> dict[int, g1.Sample]:
    return {x.structure_id: g1.Sample(10_000 + x.structure_id, x.session, u * x.risk, x.risk, cost,
                                      parent_id=x.structure_id) for x in sample[:k]}


def nightly(s: Store, now: datetime = NOW, mono: float = 1.0) -> g1.G1Run:
    with s.tx():
        return g1.run_nightly(s, now, mono)


def review(s: Store, now: datetime = NOW, mono: float = 1.0) -> g1.LiveReview:
    with s.tx():
        return g1.live_review(s, now, mono)


def look(sample, pl, **kw) -> g1.Assessment:
    kw.setdefault("family_m", 4)
    kw.setdefault("rates_version", "2026-04-01")
    return g1.assess_look(STRAT, V, sample, pl, **kw)


# ---------------------------------------------------------------------------------------------------------------
# The ledger row
# ---------------------------------------------------------------------------------------------------------------
def test_trade_row_from_fills(store):
    """Net comes from the fills through net_pnl (brokerage per executed order, STT on sells, stamp on buys); LTP
    never moves money; the row satisfies the DDL and the store's ledger arithmetic check."""
    it = make_intent("a9c31f04e2b7", at("09:20:00"))
    stc = make_structure(17, it, risk=21085.0, exit_reason="TAKE_PROFIT")
    lg = make_leg(stc, 17, 1, inst_of("CE", 100), peak_rb=45.60, peak_wall=at("09:41:00"), trough_rb=41.00,
                  trough_wall=at("09:22:00"), lock_armed=True)
    ent = make_order(lg, Role.ENTRY, 1, 500, 500, 42.17, OrderState.COMPLETE,
                     DecisionQuote(41.90, 42.15, 42.025, 180, "REST", 42.10, 42.17), limit=42.35)
    x1 = make_order(lg, Role.EXIT, 1, 500, 200, 45.00, OrderState.CANCELLED,
                    DecisionQuote(45.00, 45.20, 45.10, 90, "WS", 45.50, 44.98), limit=44.80)
    x2 = make_order(lg, Role.EXIT, 2, 300, 0, None, OrderState.CANCELLED,
                    DecisionQuote(44.95, 45.15, 45.05, 70, "WS", 44.20, 44.92), limit=44.70)
    x3 = make_order(lg, Role.EXIT, 3, 300, 300, 44.90, OrderState.COMPLETE,
                    DecisionQuote(44.95, 45.15, 45.05, 60, "WS", 44.00, 44.92), limit=44.70)
    orders = [ent, x1, x2, x3]
    fills = [fill(ent, 300, 42.15, at("09:20:01.200"), 300), fill(ent, 200, 42.20, at("09:20:01.400"), 500),
             fill(x1, 200, 45.00, at("09:45:00.500")), fill(x3, 300, 44.90, at("09:45:02.100"))]
    row = ledger.build_trade_row(stc, [lg], orders, fills, exit_wall=at("09:45:10"))

    # Hand-computed at the 2026-04-01 rates: entry 21,085 (BUY), exits 9,000 and 13,470 (SELL); x2 never filled.
    assert row["qty"] == 500 and row["entry_avg"] == 42.17 and row["exit_avg"] == 44.94
    assert row["gross"] == 1385.0
    assert (row["brokerage"], row["stt"], row["txn"], row["sebi"], row["ipft"], row["gst"], row["stamp"],
            row["sqoff_fee"]) == (60.0, 33.71, 15.48, 0.04, 0.0, 13.59, 0.63, 0.0)
    assert row["charges"] == 123.45 and row["net"] == 1261.55
    assert row["n_executed_orders"] == 3 and row["n_entry_orders"] == 1 and row["n_exit_orders"] == 3
    assert row["rates_version"] == "2026-04-01" and row["pnl_status"] is PnlStatus.FINAL
    assert row["risk_basis_rs"] == 21085.0 and row["net_pct"] == pytest.approx(1261.55 / 21085 * 100, abs=1e-4)
    assert row["entry_ts"] == at("09:20:01.200") and row["exit_ts"] == at("09:45:02.100")
    assert row["hold_s"] == pytest.approx(1500.9)
    # G1's C: half-spread at every filled decision, and the crossing versus the decision mid
    assert row["hs_cost_rs"] == 112.5 and row["crossing_cost_rs"] == 137.5
    assert row["entry_mid"] == 42.025 and row["entry_spread_pct"] == pytest.approx(0.25 / 42.025 * 100, abs=1e-4)
    assert row["entry_slip_vs_mid_pct"] == pytest.approx((42.17 / 42.025 - 1) * 100, abs=1e-4)
    assert row["exit_mid_w"] == 45.07
    assert row["exit_slip_vs_mid_pct"] == pytest.approx(65.0 / 22535.0 * 100, abs=1e-4)
    assert row["mfe_pct"] == pytest.approx((45.60 / 42.17 - 1) * 100, abs=1e-4)
    assert row["mae_pct"] == pytest.approx((41.00 / 42.17 - 1) * 100, abs=1e-4)
    assert row["thin_book"] is True and row["lock_armed"] is True and row["exit_reason"] == "TAKE_PROFIT"
    assert row["ltp_entry"] == 42.10 and row["ltp_exit"] == 44.00          # logged only
    assert row["mode"] == "PAPER" and row["direction"] is Direction.UP and row["exit_profile"]["hard_sl_pct"] == 15.0

    # LTP never enters a money figure
    wild = [dataclasses.replace(o, decision=dataclasses.replace(o.decision, ltp=999.0)) for o in orders]
    row2 = ledger.build_trade_row(stc, [lg], wild, fills, exit_wall=at("09:45:10"))
    assert {k: row2[k] for k in ("gross", "charges", "net", "hs_cost_rs", "crossing_cost_rs")} == \
           {k: row[k] for k in ("gross", "charges", "net", "hs_cost_rs", "crossing_cost_rs")}

    # The row fits the DDL and the store's arithmetic check, and reads back the same
    with store.tx():
        persist(store, stc, lg, orders, fills, SESSION)
        assert len(store.effective_fills([o.order_pk for o in orders])) == 4
        store.insert_trade(row)
        with pytest.raises(InvariantViolation):          # booked once (I10)
            store.insert_trade(row)
    back = store.trade(17)
    assert back["net"] == 1261.55 and back["pnl_status"] == "FINAL" and back["entry_ts"] == iso(at("09:20:01.200"))

    # The broker's MIS square-off closed the rest: Rs 50 + GST on that order; the caller's exit reason wins
    it2 = make_intent("b9c31f04e2b7", at("10:00:00"))
    stc2 = make_structure(18, it2, risk=21075.0, exit_reason="EOD_FLATTEN")
    lg2 = make_leg(stc2, 18, 2, inst_of("CE", 500))
    e2 = make_order(lg2, Role.ENTRY, 1, 500, 500, 42.15, OrderState.COMPLETE,
                    DecisionQuote(41.90, 42.15, 42.025, 180, "REST", 42.1, 42.15), limit=42.35)
    sq = make_order(lg2, Role.EXTERNAL, 0, 500, 500, 39.00, OrderState.COMPLETE, None, ext="AUTO_SQUAREOFF",
                    broker_id="260512000777")
    f2 = [fill(e2, 500, 42.15, at("10:00:01")), fill(sq, 500, 39.00, at("15:26:01"))]
    r2 = ledger.build_trade_row(stc2, [lg2], [e2, sq], f2, exit_wall=at("15:31:00"), exit_reason="BROKER_SQUAREOFF")
    assert r2["sqoff_fee"] == 50.0 and r2["exit_reason"] == "BROKER_SQUAREOFF" and r2["n_exit_orders"] == 1
    assert r2["gst"] == pytest.approx(round(0.18 * (40 + 21075 * 0.0003553 + 19500 * 0.0003553 + 0.0406 + 50), 2),
                                      abs=0.02)
    assert r2["hs_cost_rs"] == 62.5 and r2["thin_book"] is False   # EXTERNAL orders carry no decision quote

    # Unpriced: an EXTERNAL sell with no fill books no proceeds and the row is UNRESOLVED (never optimistic)
    lost = make_order(lg2, Role.EXTERNAL, 0, 500, 500, None, OrderState.COMPLETE, None, ext="MANUAL",
                      broker_id="260512000778")
    r3 = ledger.build_trade_row(stc2, [lg2], [e2, lost], [f2[0]], exit_wall=at("11:00:00"))
    assert r3["pnl_status"] is PnlStatus.UNRESOLVED and r3["exit_reason"] == "EOD_FLATTEN"
    assert r3["gross"] == -21075.0 and r3["net"] < -21075.0 and r3["exit_ts"] == at("11:00:00")
    # Bought and sold quantities that differ: UNRESOLVED; partial fills priced at their VWAP: ESTIMATED
    half = make_order(lg2, Role.EXIT, 1, 500, 200, 40.0, OrderState.CANCELLED, None, limit=39.0)
    r4 = ledger.build_trade_row(stc2, [lg2], [e2, half], [f2[0], fill(half, 200, 40.0, at("11:00:00"))],
                                exit_wall=at("11:00:05"))
    assert r4["pnl_status"] is PnlStatus.UNRESOLVED
    part = make_order(lg2, Role.EXIT, 1, 500, 500, 40.0, OrderState.COMPLETE, None, limit=39.0)
    r5 = ledger.build_trade_row(stc2, [lg2], [e2, part], [f2[0], fill(part, 300, 40.0, at("11:00:00"))],
                                exit_wall=at("11:00:05"))
    assert r5["pnl_status"] is PnlStatus.ESTIMATED and r5["exit_avg"] == 40.0

    # Every order of the leg must be passed (a partial list would book a wrong, immutable P&L)
    listed = dataclasses.replace(lg, orders=[o.order_pk for o in orders])
    with pytest.raises(ValueError, match="were not passed"):
        ledger.build_trade_row(stc, [listed], orders[:3], fills, exit_wall=at("09:45:10"))
    assert ledger.build_trade_row(stc, [listed], orders, fills, exit_wall=at("09:45:10"))["net"] == 1261.55

    # Nothing to book without an entry fill; foreign orders are refused
    void = make_order(lg2, Role.ENTRY, 1, 500, 0, None, OrderState.CANCELLED, None, limit=42.35)
    with pytest.raises(ValueError, match="no entry fills"):
        ledger.build_trade_row(stc2, [lg2], [void], [], exit_wall=at("11:00:00"))
    with pytest.raises(ValueError, match="not an order of structure"):
        ledger.build_trade_row(stc, [lg], [e2], f2, exit_wall=at("11:00:00"))


def test_value_order_fallbacks_are_conservative():
    f = [Fill("T2XP26101200101", "cum:100", "CUM", 100, 40.0, at("10:00:00"), None)]
    v = ledger.value_order("T2XP26101200101", Role.EXIT, Side.SELL, 200, 39.5, None, f)
    assert v.status is PnlStatus.ESTIMATED and v.value == 8000.0
    v = ledger.value_order("T2XP26101200101", Role.EXIT, Side.SELL, 200, 39.5, None, [])
    assert v.status is PnlStatus.UNRESOLVED and v.value == 7900.0          # a LIMIT sell never gets less
    v = ledger.value_order("T2EP26101200101", Role.ENTRY, Side.BUY, 200, 42.5, None, [])
    assert v.status is PnlStatus.UNRESOLVED and v.value == 8500.0          # a LIMIT buy never pays more
    v = ledger.value_order("EX1", Role.EXTERNAL, Side.SELL, 200, None, "MANUAL", [])
    assert v.status is PnlStatus.UNRESOLVED and v.value == 0.0 and v.order_fills() is None
    with pytest.raises(ValueError):
        ledger.value_order("EX2", Role.EXTERNAL, Side.BUY, 200, None, "MANUAL", [])


def test_shared_broker_order_attribution():
    """One broker order that closed the legs of two books: rows 'EX<id>' (with the broker id) and 'EX<id>:<leg>'
    (without: orders.broker_order_id is unique). The truth check attributes it by share, in leg_id order."""
    from titan2.charges import OrderFills, OrderPart
    assert [ledger.external_root(pk) for pk in ("EXP1", "EXP1:7", "EX:7:1", "EX", "T2XP26101200101", None)] == [
        "P1", "P1", None, None, None, None]
    rows = [dict(order_pk="EXP1:9", leg_id=9, book="PAPER:b", role="EXTERNAL", broker_order_id=None, filled_qty=250,
                 external_kind="AUTO_SQUAREOFF"),
            dict(order_pk="EXP1", leg_id=4, book="PAPER:a", role="EXTERNAL", broker_order_id="P1", filled_qty=500,
                 external_kind="AUTO_SQUAREOFF"),
            dict(order_pk="T2EP26101200101", leg_id=4, book="PAPER:a", role="ENTRY", broker_order_id="P0",
                 filled_qty=500, external_kind=None),
            dict(order_pk="EXP2", leg_id=5, book="PAPER:c", role="EXTERNAL", broker_order_id="P2", filled_qty=100,
                 external_kind="MANUAL"),
            dict(order_pk="T2XP26101200201", leg_id=6, book="PAPER:d", role="EXIT", broker_order_id="P3",
                 filled_qty=0, external_kind=None)]
    books, parts = ledger.truth_attribution(rows)
    assert books == {"P1": ("PAPER:a", "AUTO_SQUAREOFF"), "P0": ("PAPER:a", None), "P2": ("PAPER:c", "MANUAL")}
    assert parts == {"P1": [("PAPER:a", 500), ("PAPER:b", 250)]}
    # an OrderValue carrying its share books the share's value and charges
    whole = OrderFills(Side.SELL, 750, 750 * 40.0, "AUTO_SQUAREOFF")
    f = [Fill("EXP1:9", "cum:250", "CUM", 250, 40.0, at("15:26:00"), None)]
    v = ledger.value_order("EXP1:9", Role.EXTERNAL, Side.SELL, 250, None, "AUTO_SQUAREOFF", f,
                           OrderPart(whole, 500, 750))
    assert v.status is PnlStatus.FINAL and v.value == 10000.0 and v.order_fills() == OrderPart(whole, 500, 750)
    # a share that does not match the row is ignored (the row books as a plain order)
    v = ledger.value_order("EXP1:9", Role.EXTERNAL, Side.SELL, 250, None, "AUTO_SQUAREOFF", f,
                           OrderPart(whole, 0, 500))
    assert v.part is None and isinstance(v.order_fills(), OrderFills)


# ---------------------------------------------------------------------------------------------------------------
# G1: multiple testing, bootstrap, placebo, books, confirm, futility
# ---------------------------------------------------------------------------------------------------------------
def test_family_m_alpha(store):
    assert g1.family_m(store) == 4                                     # the floor, with no trial yet
    with store.tx():
        g1.ensure_trial(store, STRAT, "1.v0", {}, REG, 0.0)
        g1.ensure_trial(store, STRAT, "1.v1", {}, REG + timedelta(days=1), 0.0)
    assert store.trial(STRAT, "1.v0")["state"] == "RETIRED"            # a new version retires the old trial ...
    assert store.trial_count() == 2 and g1.family_m(store) == 4       # ... which still counts toward m
    assert g1.alpha_look(4) == 0.05 / 16 == 0.003125
    with store.tx():
        for k in range(4):
            g1.ensure_trial(store, "price_spike_up", f"1.p{k}", {}, REG + timedelta(days=2 + k), 0.0)
        assert g1.ensure_trial(store, STRAT, "1.v1", {}, NOW, 0.0)[1] is False   # registered once
    assert g1.family_m(store) == 6 and g1.alpha_look(6) == pytest.approx(0.05 / 24)
    with pytest.raises(InvariantViolation):                            # the trial registry is append-only
        with store.tx():
            store.execute("DELETE FROM g1_trials WHERE version = '1.v0'")
    assert g1.family_m(store) == 6                                     # so m never decreases

    a = look(samples(100, 34), placebo_map(samples(100, 34), 85), family_m=6)
    assert a.passed and a.family_m == 6 and a.alpha == pytest.approx(0.05 / 24)
    assert a.detail["boot_b"] == 24000 and a.detail["boot_rank"] == 50  # B = ceil(50/alpha), LB = 50th statistic
    row = a.row(STRAT, V, NOW)
    assert row["family_m"] == 6 and row["alpha"] == pytest.approx(0.05 / 24) and row["kind"] == "LOOK"


def test_bootstrap_deterministic():
    rnd = random.Random(3)
    by = {f"2026-05-{d:02d}": [rnd.gauss(0.02, 0.08) for _ in range(rnd.randint(1, 4))] for d in range(1, 31)}
    alpha = g1.alpha_look(4)
    assert g1.boot_size(alpha) == 16000 and g1.boot_rank(alpha, 16000) == 50
    assert g1.boot_size(g1.G1_FUTILITY_ALPHA) == 10000 and g1.boot_rank(0.10, 10000) == 1000
    seed = g1.boot_seed(STRAT, V, 123, "LOOK")
    assert seed == int(hashlib.sha256(f"{STRAT}|{V}|123|LOOK".encode()).hexdigest()[:16], 16)
    assert g1.boot_seed(STRAT, V, 123, "PROGRESS") != seed and g1.boot_seed(STRAT, V, 124, "LOOK") != seed

    lb, ub = g1.bootstrap_bounds(by, alpha, seed)
    assert g1.bootstrap_bounds(dict(reversed(list(by.items()))), alpha, seed) == (lb, ub)   # input order is moot
    # The algorithm is pinned (§10.7): D sessions drawn with replacement, pooled Σr/count, k-th order statistics
    keys = sorted(by)
    sums, counts = [math.fsum(by[k]) for k in keys], [len(by[k]) for k in keys]
    draw = random.Random(seed).random
    stats = []
    for _ in range(16000):
        idx = [int(draw() * len(keys)) for _ in keys]
        stats.append(sum(sums[i] for i in idx) / sum(counts[i] for i in idx))
    stats.sort()
    assert (lb, ub) == pytest.approx((stats[49], stats[15950]), rel=1e-12)
    pooled = math.fsum(sums) / sum(counts)
    assert lb < pooled < ub
    assert g1.bootstrap_bounds(by, alpha, seed + 1) != (lb, ub)
    assert g1.bootstrap_bounds({"2026-05-01": [0.01, 0.03]}, alpha, 1) == (0.02, 0.02)
    assert g1.bootstrap_bounds({}, alpha, 1) is None

    # Whole assessments reproduce: the same samples in another order give the same report
    xs = samples(100, 34)
    pl = placebo_map(xs, 85)
    a1, a2 = look(xs, pl), look(list(reversed(xs)), pl)
    assert (a1.lb, a1.ub, a1.inputs_hash, a1.detail["boot_seed"]) == (a2.lb, a2.ub, a2.inputs_hash,
                                                                       a2.detail["boot_seed"])
    assert a1.inputs_hash == g1.inputs_hash(STRAT, V, "2026-04-01", xs, pl.values())


def test_placebo_rules():
    xs = samples(100, 34)
    base = look(xs, placebo_map(xs, 85))
    assert base.passed and base.fail_reasons == ()
    assert base.placebo_n == 85 and base.placebo_coverage == 0.85 and base.placebo_mean_u == pytest.approx(-0.03)

    # coverage: a refused placebo (SKIPPED:<gate>) has no trade and counts against coverage
    a = look(xs, placebo_map(xs, 79))
    assert a.fail_reasons == ("PLACEBO_COVERAGE",) and not a.passed
    assert look(xs, placebo_map(xs, 80)).passed                        # 0.8 exactly is enough
    # the placebo is judged unstressed (1.0x): +0.2% fails although its stressed r is negative
    pl = placebo_map(xs, 85, u=0.002, cost=100.0)
    assert all(p.r < 0 < p.u for p in pl.values())
    a = look(xs, pl)
    assert a.fail_reasons == ("PLACEBO_EDGE",) and a.placebo_mean_u == pytest.approx(0.002)
    # a placebo floor of 50 trades, independent of coverage (49/60 = 82% coverage)
    small = samples(60, 34)
    a = look(small, placebo_map(small, 49))
    assert "PLACEBO_N" in a.fail_reasons and "PLACEBO_COVERAGE" not in a.fail_reasons
    # only placebos whose parent is in the sample count
    extra = {5000 + k: g1.Sample(20_000 + k, xs[0].session, -150.0, 5000.0, 5.0, parent_id=5000 + k)
             for k in range(30)}
    assert look(xs, {**placebo_map(xs, 79), **extra}).fail_reasons == ("PLACEBO_COVERAGE",)


def test_mismatch_unresolved_fail(tmp_path, core, monkeypatch):
    # Pure: any UNRESOLVED or ESTIMATED trade, or one whose fills do not explain it, fails G1
    xs = samples(100, 34)
    pl = placebo_map(xs, 85)
    for bad in (dataclasses.replace(xs[7], pnl_status="ESTIMATED"), dataclasses.replace(xs[7], pnl_status="UNRESOLVED"),
                dataclasses.replace(xs[7], complete=False)):
        a = look(xs[:7] + [bad] + xs[8:], pl)
        assert a.fail_reasons == ("UNRESOLVED",) and a.unresolved == 1
    assert look(xs, pl, mismatches=1).fail_reasons == ("MISMATCH",)
    assert look(xs, pl, impaired_sessions=4).fail_reasons == ("IMPAIRED",)          # 4/34 > 10%
    assert look(xs, pl, impaired_sessions=3).passed                                  # 3/34 <= 10%
    assert look(xs, pl, needs_bar=True).fail_reasons == ("BAR_CHECKS_MISSING",)
    assert look(xs, pl, needs_bar=True, bar_checks=(40, 5)).fail_reasons == ("BAR_MISMATCH",)
    assert look(xs, pl, needs_bar=True, bar_checks=(40, 4)).passed

    # DB: a PAPER mismatch on this book's symbol inside the window fails the look; the trial keeps collecting
    s = fresh(tmp_path, "mismatch")
    start_trial(s)
    seed_paper(s, 102, placebos=85)
    add_bar_checks(s, 34)
    with s.tx():
        s.append_event("MISMATCH", wall=datetime.combine(DAY0 + timedelta(days=5), time(11), IST), mono=0.0,
                       severity="CRITICAL", account=Account.PAPER,
                       payload={"account": "PAPER", "class": "EXTERNAL_CLOSE", "symbol": inst_of().symbol})
    run = nightly(s, NOW, 1.0)
    rep = [r for r in run.reports if r["kind"] == "LOOK"]
    assert len(rep) == 1 and rep[0]["fail_reasons"] == ["MISMATCH"] and not rep[0]["passed"]
    trial = s.trial(STRAT, V)
    assert trial["state"] == "COLLECTING" and trial["looks_done"] == [100]
    assert nightly(s, NOW + timedelta(days=1), 2.0).reports[-1]["kind"] == "PROGRESS"   # a look runs once
    assert len(s.g1_reports(strategy=STRAT, version=V, kind="LOOK")) == 1

    # What counts: by event book or traded symbol, account-wide findings, failed truth checks, manual closes
    book, d1, d9 = f"PAPER:{STRAT}", DAY0.isoformat(), (DAY0 + timedelta(days=9)).isoformat()
    base = g1.count_mismatches(s, book, "PAPER", d1, d9)
    assert base == 1

    def ev(kind, payload, account=Account.PAPER, day=3, book_col=None):
        with s.tx():
            s.append_event(kind, wall=datetime.combine(DAY0 + timedelta(days=day), time(12), IST), mono=0.0,
                           account=account, book=book_col, payload=payload)
        return g1.count_mismatches(s, book, "PAPER", d1, d9)

    assert ev("MISMATCH", {"symbol": "NFO:INFY26OCT1500CE"}) == base                 # another book's symbol
    assert ev("UNKNOWN_POSITION", {"symbol": inst_of().symbol}, account=Account.SHADOW) == base
    assert ev("MISMATCH", {"symbol": inst_of().symbol}, day=20) == base             # outside the window
    assert ev("VERIFY_FLAT", {"outcome": "CLOSED", "symbol": inst_of().symbol}) == base
    assert ev("EOD_TRUTH_CHECK", {"ok": True, "broker_net": 1.0, "books": []}) == base
    assert ev("EOD_TRUTH_CHECK", {"ok": False, "broker_net": 5.0, "unattributed": [],
                                  "books": [{"book": "PAPER:price_spike_up", "diff": 2.0}]}) == base
    assert ev("EOD_TRUTH_CHECK", {"ok": False, "broker_net": 5.0, "unattributed": [],
                                  "books": [{"book": book, "diff": 0.5}]}) == base + 1
    assert ev("VERIFY_FLAT", {"outcome": "RESIDUAL", "symbol": inst_of().symbol}) == base + 2
    assert ev("RECON", {"account": "PAPER", "status": "BROKER_ERROR"}) == base + 3          # account-wide
    assert ev("FILLS_REPAIRED", {"symbol": "NFO:OTHER"}, book_col=book) == base + 4       # by event book
    put_trade(s, session=DAY0 + timedelta(days=4), exit_px=99.0, external_manual=True)
    assert g1.count_mismatches(s, book, "PAPER", d1, d9) == base + 5

    # The last look failing ends the trial: FAILED, mode SHADOW, an alert
    monkeypatch.setattr(g1, "G1_LOOKS", (100,))
    s2 = fresh(tmp_path, "lastlook")
    start_trial(s2)
    put_trade(s2, session=DAY0, exit_px=108.0, pnl_status=PnlStatus.ESTIMATED)      # inside the look's sample
    seed_paper(s2, 99, placebos=85)
    add_bar_checks(s2, 34)
    run = nightly(s2, NOW, 1.0)
    assert s2.trial(STRAT, V)["state"] == "FAILED" and s2.strategy_mode(STRAT)["mode"] == "SHADOW"
    assert run.reports[0]["kind"] == "LOOK" and run.reports[0]["fail_reasons"] == ["UNRESOLVED"]
    assert any(a.code == "G1_FAILED" and a.severity == "WARN" for a in run.alerts)
    assert run.mode_changes == [(STRAT, "PAPER", "SHADOW", run.mode_changes[0][3])]
    assert nightly(s2, NOW + timedelta(days=1), 2.0).reports == []           # FAILED is final
    s.close()
    s2.close()


def test_confirm_futility(tmp_path, core):
    # LOOK 100 passes -> CONFIRMING; the next 25 trades confirm -> CERTIFIED; key 2 is bound to its hash
    s = fresh(tmp_path, "confirm")
    start_trial(s)
    seed_paper(s, 100, placebos=85)
    add_bar_checks(s, 44)                                    # the look's sessions 0..33 and the confirm's 34..43
    run = nightly(s, NOW, 1.0)
    lk = next(r for r in run.reports if r["kind"] == "LOOK")
    assert lk["passed"] and lk["look_n"] == 100 and lk["n"] == 100 and lk["sessions"] == 34
    rep = s.g1_report(lk["report_id"])
    assert rep["family_m"] == 4 and rep["alpha"] == 0.003125 and rep["lb_pct"] > 0 and rep["placebo_n"] == 85
    assert rep["placebo_coverage"] == 0.85 and rep["placebo_exp_pct"] < 0 and rep["mismatches"] == 0
    assert rep["exp_pct_stressed"] < rep["exp_pct"]                      # r pays 0.5x C more than u
    trial = s.trial(STRAT, V)
    assert trial["state"] == "CONFIRMING" and trial["pass_report_id"] == lk["report_id"]
    assert [t[2:] for t in run.transitions] == [("COLLECTING", "CONFIRMING")]
    assert run.reports[-1]["kind"] == "PROGRESS" and not run.reports[-1]["passed"]
    assert not g1.verify_unlock(s, STRAT, V, lk["inputs_hash"], NOW).ok

    seed_paper(s, 25, first=102)                                         # sessions 34..42, after the look sample
    later = NOW + timedelta(days=1)
    run = nightly(s, later, 2.0)
    cf = next(r for r in run.reports if r["kind"] == "CONFIRM")
    assert cf["passed"] and cf["n"] == 25
    trial = s.trial(STRAT, V)
    assert trial["state"] == "CERTIFIED" and trial["cert_report_id"] == cf["report_id"]
    cert = next(a for a in run.alerts if a.code == "G1_CERTIFIED")
    assert cf["inputs_hash"] in cert.text
    ok = g1.verify_unlock(s, STRAT, V, cf["inputs_hash"].upper(), later + timedelta(days=1))
    assert ok.ok and ok.cert_report_id == cf["report_id"]
    assert g1.verify_unlock(s, STRAT, V, "0" * 64, later).reason == "HASH_MISMATCH"
    assert g1.verify_unlock(s, STRAT, V, cf["inputs_hash"], later + timedelta(days=8)).reason == "CERTIFICATE_EXPIRED"
    assert g1.verify_unlock(s, STRAT, "1.other", cf["inputs_hash"], later).reason == "VERSION_NOT_CURRENT"
    # the certificate binds all the evidence: look sample + confirm sample + their placebos
    assert s.g1_report(cf["report_id"])["detail"]["evidence_n"] == 125
    card = next(c for c in g1.scorecards(s) if c["strategy"] == STRAT)
    assert card["state"] == "CERTIFIED" and card["certificate"]["inputs_hash"] == cf["inputs_hash"]
    assert g1.trial_summary(s, STRAT, V)["n"] == 125

    # A failed confirmation goes back to COLLECTING while looks remain
    s2 = fresh(tmp_path, "confirm_fail")
    start_trial(s2)
    seed_paper(s2, 100, placebos=85)
    add_bar_checks(s2, 44)
    nightly(s2, NOW, 1.0)
    s2.backup(tmp_path / "gap" / "titan2.db")                           # the same CONFIRMING trial, cloned
    s2b = Store(tmp_path / "gap" / "titan2.db", backup_dir=tmp_path / "gap" / "backups")
    seed_paper(s2, 25, first=102, exits=(97.0, 99.0))
    run = nightly(s2, NOW + timedelta(days=1), 2.0)
    cf = next(r for r in run.reports if r["kind"] == "CONFIRM")
    assert not cf["passed"] and cf["fail_reasons"] == ["MEAN_R"]
    assert s2.trial(STRAT, V)["state"] == "COLLECTING" and g1.next_look(s2.trial(STRAT, V)) == 150
    # A mismatch on a session between the look sample (ends on session 33) and the confirm trades (from 35)
    seed_paper(s2b, 25, first=105)
    with s2b.tx():
        s2b.append_event("MISMATCH", wall=datetime.combine(DAY0 + timedelta(days=34), time(11), IST), mono=0.0,
                         account=Account.PAPER, payload={"account": "PAPER", "symbol": inst_of().symbol})
    cf = next(r for r in nightly(s2b, NOW + timedelta(days=1), 2.0).reports if r["kind"] == "CONFIRM")
    assert cf["fail_reasons"] == ["MISMATCH"] and s2b.trial(STRAT, V)["state"] == "COLLECTING"
    s2b.close()

    # FUTILITY: from n = 50, every 25 trades; the 90% upper bound of u below 0 retires the version
    s3 = fresh(tmp_path, "futile")
    start_trial(s3)
    seed_paper(s3, 50, exits=(92.0, 94.0, 96.0, 98.0, 100.0))
    run = nightly(s3, NOW, 1.0)
    fut = next(r for r in run.reports if r["kind"] == "FUTILITY")
    assert fut["look_n"] == 50 and not fut["passed"] and fut["ub_pct"] < 0
    assert s3.trial(STRAT, V)["state"] == "FUTILE" and s3.strategy_mode(STRAT)["mode"] == "SHADOW"
    assert any(a.code == "G1_FUTILE" for a in run.alerts)
    assert not any(r["kind"] == "LOOK" for r in run.reports)
    # ... while a version that may still have an edge survives its checkpoint
    s4 = fresh(tmp_path, "survive")
    start_trial(s4)
    seed_paper(s4, 50)
    run = nightly(s4, NOW, 1.0)
    assert next(r for r in run.reports if r["kind"] == "FUTILITY")["passed"]
    assert s4.trial(STRAT, V)["state"] == "COLLECTING" and s4.strategy_mode(STRAT)["mode"] == "PAPER"
    assert nightly(s4, NOW + timedelta(days=1), 2.0).reports[0]["kind"] == "PROGRESS"   # checkpoint done
    for x in (s, s2, s3, s4):
        x.close()


def _bar_days(s: Store, sessions: range, *, signal_ok, control_ok: bool, controls: int = 20) -> None:
    """Each session's bar_checks rows as the 15:36 check writes them: the symbol opening_drive fired on (every seeded
    trade is on NSE:RELIANCE) plus `controls` seeded control symbols."""
    with s.tx():
        for k in sessions:
            d = DAY0 + timedelta(days=k)
            start = datetime.combine(d, time(9, 15), IST)
            s.put_bar_check(d, "NSE:RELIANCE", start, signal_ok(k), '{"reasons": []}')
            for j in range(controls):
                s.put_bar_check(d, f"NSE:CTRL{j:02d}", start, control_ok, "{}")


def test_bar_mismatch_counts_the_samples_signal_bars_only(tmp_path, core):
    """§10.3: "G1 requires that at most 10% of the sample's signal bars mismatch". The 20 control symbols the 15:36
    check adds every day are a bar-builder health figure, never pooled in: half the signal bars mismatching must
    fail the look even though 20 matching control bars a day would dilute that to 2.4%; and failing controls never
    fail a sample whose own signal bars match."""
    s = fresh(tmp_path, "dilution")
    start_trial(s)
    seed_paper(s, 100, placebos=85)
    _bar_days(s, range(34), signal_ok=lambda k: k % 2 == 1, control_ok=True)       # 17 of 34 signal bars bad
    look = next(r for r in nightly(s, NOW, 1.0).reports if r["kind"] == "LOOK")
    rep = s.g1_report(look["report_id"])
    assert look["fail_reasons"] == ["BAR_MISMATCH"] and not look["passed"]
    assert rep["bar_mismatch_frac"] == pytest.approx(17 / 34)
    assert rep["detail"]["bar_checks"] == [34, 17] and rep["detail"]["bar_checks_other"] == [680, 0]
    assert s.trial(STRAT, V)["state"] == "COLLECTING"
    s.close()

    s2 = fresh(tmp_path, "noisy_controls")
    start_trial(s2)
    seed_paper(s2, 100, placebos=85)
    _bar_days(s2, range(34), signal_ok=lambda k: True, control_ok=False)           # every control bar bad
    look = next(r for r in nightly(s2, NOW, 1.0).reports if r["kind"] == "LOOK")
    assert look["passed"] and s2.g1_report(look["report_id"])["detail"]["bar_checks_other"] == [680, 680]
    s2.close()


def _confirming(tmp_path, name: str) -> Store:
    """A trial whose LOOK 100 passed (sessions 0..33, clean signal bars): it waits for its 25 confirm trades."""
    s = fresh(tmp_path, name)
    start_trial(s)
    seed_paper(s, 100, placebos=85)
    _bar_days(s, range(34), signal_ok=lambda k: True, control_ok=True, controls=0)
    nightly(s, NOW, 1.0)
    assert s.trial(STRAT, V)["state"] == "CONFIRMING"
    seed_paper(s, 25, first=102)                                                   # sessions 34..42
    return s


def test_confirm_applies_the_impaired_and_bar_criteria(tmp_path, core):
    """CONFIRM issues the certificate key 2 is bound to, so the confirm window passes the look's operator-selection
    control and bar criterion too (spec clarification): an operator who PAUSEs on the days that look bad (PAPER
    intents divert to SHADOW, the session is IMPAIRED) cannot certify on a censored confirm sample, and confirm
    trades whose signal bars disagree with Kite's candles are not the backtested rule."""
    later = NOW + timedelta(days=1)
    s = _confirming(tmp_path, "impaired")
    _bar_days(s, range(34, 43), signal_ok=lambda k: True, control_ok=True, controls=0)
    with s.tx():
        for k in range(34, 43, 2):                                                 # 5 of the 9 confirm sessions
            s.flag_session(DAY0 + timedelta(days=k), STRAT, "DIVERTED: PAUSE")
    cf = next(r for r in nightly(s, later, 2.0).reports if r["kind"] == "CONFIRM")
    rep = s.g1_report(cf["report_id"])
    assert cf["fail_reasons"] == ["IMPAIRED"] and not cf["passed"]
    assert rep["impaired_frac"] == pytest.approx(5 / 9)
    assert s.trial(STRAT, V)["state"] == "COLLECTING" and s.trial(STRAT, V)["cert_report_id"] is None
    s.close()

    s = _confirming(tmp_path, "bars")
    _bar_days(s, range(34, 43), signal_ok=lambda k: False, control_ok=True)        # every confirm signal bar bad
    cf = next(r for r in nightly(s, later, 2.0).reports if r["kind"] == "CONFIRM")
    assert cf["fail_reasons"] == ["BAR_MISMATCH"] and s.trial(STRAT, V)["state"] == "COLLECTING"
    assert s.g1_report(cf["report_id"])["bar_mismatch_frac"] == 1.0
    s.close()

    s = _confirming(tmp_path, "unchecked")                                         # the 15:36 check never ran
    cf = next(r for r in nightly(s, later, 2.0).reports if r["kind"] == "CONFIRM")
    assert cf["fail_reasons"] == ["BAR_CHECKS_MISSING"] and s.trial(STRAT, V)["state"] == "COLLECTING"
    s.close()

    s = _confirming(tmp_path, "clean")                     # a clean window certifies (failing controls do not count)
    _bar_days(s, range(34, 43), signal_ok=lambda k: True, control_ok=False)
    cf = next(r for r in nightly(s, later, 2.0).reports if r["kind"] == "CONFIRM")
    assert cf["passed"] and s.trial(STRAT, V)["state"] == "CERTIFIED"
    rep = s.g1_report(cf["report_id"])
    assert rep["impaired_frac"] == 0.0 and rep["bar_mismatch_frac"] == 0.0
    assert rep["detail"]["bar_checks_other"] == [181, 180]       # the controls + the look's last signal bar (ok)
    s.close()


# ---------------------------------------------------------------------------------------------------------------
# Live review (§10.9)
# ---------------------------------------------------------------------------------------------------------------
def go_live(s: Store, strategy: str = STRAT, since: date = DAY0) -> None:
    with s.tx():
        s.put_strategy_mode(strategy, version=V, mode="LIVE", changed_wall=REG, changed_by="test",
                            live_since_session=since)
        g1.ensure_trial(s, strategy, V, {}, REG, 0.0)
        s.update_trial(strategy, V, state="CERTIFIED", state_ts=REG)


def live_trade(s: Store, i: int, exit_px: float, **kw) -> int:
    return put_trade(s, session=DAY0 + timedelta(days=i // 3), exit_px=exit_px, book=f"LIVE:{STRAT}",
                     account=Account.LIVE, minute=i % 3, **kw)


def test_live_review(tmp_path, core):
    # Healthy: a LIVE_REVIEW report, no demotion
    s = fresh(tmp_path, "healthy")
    go_live(s)
    for i in range(3):
        live_trade(s, i, 106.0)
    rv = review(s, NOW, 1.0)
    assert rv.triggers == {STRAT: ()} and rv.demotions == [] and rv.alerts == []
    rep = s.g1_reports(strategy=STRAT, kind="LIVE_REVIEW")[-1]
    assert rep["passed"] and rep["n"] == 3 and s.strategy_mode(STRAT)["mode"] == "LIVE"

    # Probation: cumulative net <= -6,000 within the first 20 live trades
    s1 = fresh(tmp_path, "probation")
    go_live(s1)
    for i in range(3):
        live_trade(s1, i, 50.0)                                        # about -2,550 each
    rv = review(s1, NOW, 1.0)
    assert rv.triggers[STRAT] == ("PROBATION",)
    m = s1.strategy_mode(STRAT)
    assert m["mode"] == "PAPER" and m["demoted_reason"] == "PROBATION" and m["pending_mode"] is None
    assert s1.trial(STRAT, V)["state"] == "DEMOTED"
    assert [a.code for a in rv.alerts] == ["LIVE_DEMOTED"] and rv.alerts[0].severity == "CRITICAL"
    assert s1.events(kind="LIVE_DEMOTED")[0]["severity"] == "CRITICAL"
    assert not review(s1, NOW, 2.0).reports                    # demoted: nothing LIVE to review
    assert g1.demote(s1, STRAT, "KILL", NOW, 3.0) is None              # demotion is idempotent

    # Drawdown: cumulative net since unlock <= -15,000 (probation passed)
    s2 = fresh(tmp_path, "drawdown")
    go_live(s2)
    with s2.tx():
        for i in range(20):
            live_trade(s2, i, 95.5)                                        # about -285 each: -5,700 after 20
        for i in range(20, 24):
            live_trade(s2, i, 50.0)
    rv = review(s2, NOW, 1.0)
    assert rv.triggers[STRAT] == ("DRAWDOWN",) and s2.strategy_mode(STRAT)["mode"] == "PAPER"

    # Rolling-50: with >= 50 live trades the mean of the last 50 is below 0
    s3 = fresh(tmp_path, "rolling")
    go_live(s3)
    with s3.tx():
        for i in range(55):
            live_trade(s3, i, 101.0)                                       # gross +50 does not pay the charges
    rv = review(s3, NOW, 1.0)
    assert rv.triggers[STRAT] == ("ROLLING_50",)

    # X2: live fills 1 pp of premium worse than the paper twin on 20 pairs -> PAPER_MODEL_OPTIMISTIC, demote all
    s4 = fresh(tmp_path, "x2slip")
    go_live(s4)
    go_live(s4, "price_spike_up")
    with s4.tx():
        for i in range(20):
            iid = f"x{i:011d}"
            live_trade(s4, i, 105.0, intent_id=iid, entry_walk=99.5, exit_walk=105.5)
            put_trade(s4, session=DAY0 + timedelta(days=i // 3), exit_px=105.0, intent_id=iid, minute=i % 3)
    x2 = g1.x2_falsifier(s4)
    assert x2.trade_pairs == 20 and x2.median_gap_pp == pytest.approx(1.0) and x2.reasons == ("SLIP_GAP",)
    rv = review(s4, NOW, 1.0)
    assert {d.strategy for d in rv.demotions} == {STRAT, "price_spike_up"}
    assert all(s4.strategy_mode(x)["mode"] == "PAPER" for x in (STRAT, "price_spike_up"))
    assert any(a.code == "PAPER_MODEL_OPTIMISTIC" and a.severity == "CRITICAL" for a in rv.alerts)
    assert len(s4.events(kind="PAPER_MODEL_OPTIMISTIC")) == 1

    # X2, fill rate: live IOC fills 67% of what it asks for, paper 100%
    s5 = fresh(tmp_path, "x2fill")
    go_live(s5)
    with s5.tx():
        for i in range(19):
            iid = f"f{i:011d}"
            live_trade(s5, i, 105.0, intent_id=iid, order_qty=100, filled=50)
            put_trade(s5, session=DAY0 + timedelta(days=i // 3), exit_px=105.0, intent_id=iid, order_qty=100,
                      filled=100, minute=i % 3)
    assert not g1.x2_falsifier(s5).fired                              # 19 pairs: not yet
    live_trade(s5, 19, 105.0, intent_id="f00000000019", order_qty=100, filled=50)
    put_trade(s5, session=DAY0 + timedelta(days=6), exit_px=105.0, intent_id="f00000000019", order_qty=100,
              filled=100, minute=1)
    x2 = g1.x2_falsifier(s5)
    assert x2.order_pairs == 20 and x2.live_fill_rate_pct == pytest.approx(100 * 100 / 150, abs=1e-3)
    assert x2.paper_fill_rate_pct == 100.0 and x2.reasons == ("FILL_RATE_GAP",)
    assert review(s5, NOW, 1.0).triggers[STRAT] == ("PAPER_MODEL_OPTIMISTIC",)

    # Any LIVE mismatch, unknown position or short since going live demotes (one from before does not)
    s6 = fresh(tmp_path, "recon")
    go_live(s6, since=DAY0 + timedelta(days=10))
    live_trade(s6, 33, 106.0)
    with s6.tx():
        s6.append_event("UNKNOWN_POSITION", wall=datetime.combine(DAY0, time(11), IST), mono=0.0,
                        account=Account.LIVE, payload={"account": "LIVE", "symbol": "NFO:XYZ"})
    assert review(s6, NOW, 1.0).triggers[STRAT] == ()
    with s6.tx():
        s6.append_event("SHORT_DETECTED", wall=datetime.combine(DAY0 + timedelta(days=11), time(11), IST),
                        mono=0.0, account=Account.LIVE, payload={"account": "LIVE", "symbol": "NFO:XYZ"})
    rv = review(s6, NOW, 2.0)
    assert rv.triggers[STRAT] == ("LIVE_RECON",) and s6.strategy_mode(STRAT)["mode"] == "PAPER"

    # KILL demotes every LIVE strategy, including one only pending LIVE
    s7 = fresh(tmp_path, "kill")
    go_live(s7)
    with s7.tx():
        s7.put_strategy_mode("price_spike_up", version=V, mode="PAPER", pending_mode="LIVE", changed_wall=REG,
                             changed_by="test")
        ds = g1.demote_all_live(s7, "KILL", NOW, 1.0, changed_by="operator")
    assert sorted((d.strategy, d.old_mode, d.new_mode) for d in ds) == [
        (STRAT, "LIVE", "PAPER"), ("price_spike_up", "PAPER", "PAPER")]
    assert s7.strategy_mode("price_spike_up")["pending_mode"] is None
    for x in (s, s1, s2, s3, s4, s5, s6, s7):
        x.close()


# ---------------------------------------------------------------------------------------------------------------
# pub/ exports and the daily report
# ---------------------------------------------------------------------------------------------------------------
def test_pub_exports_and_daily_report(store, tmp_path):
    pub = tmp_path / "pub"
    day = DAY0
    start_trial(store)
    win = put_trade(store, session=day, exit_px=106.0)
    loss = put_trade(store, session=day, exit_px=96.0, minute=1)
    with store.tx():
        store.add_quote_check(1, datetime.combine(day, time(10, 5, 1), IST), "HOLD", bid=101.0, ask=101.1, rb=101.0,
                              ltp=101.05, stop_px=85.0, lock_armed=False, decision="HOLD")
        store.add_quote_check(1, datetime.combine(day, time(10, 5, 40), IST), "HOLD", bid=102.5, ask=102.6,
                              rb=102.5, ltp=102.5, stop_px=85.0, lock_armed=False, decision="HOLD")
        store.add_halt("BOOK:PAPER:opening_drive", "DAILY_STOP", "test", "NEXT_SESSION",
                       datetime.combine(day, time(12), IST))
        store.append_event("HEARTBEAT", wall=datetime.combine(day, time(10), IST), mono=0.0)
        store.append_event("HEARTBEAT", wall=datetime.combine(day, time(10, 5), IST), mono=300.0)
        store.append_event("NO_BID", wall=datetime.combine(day, time(10, 6), IST), mono=360.0, severity="WARN")
    now = datetime.combine(day, time(15, 38), IST)

    p = ledger.publish_trades_today(store, pub, day, now)
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert [t["structure_id"] for t in doc["trades"]] == [win, loss] and doc["session"] == day.isoformat()
    assert not list(pub.glob(".*.tmp"))                                 # atomic: no temp file left behind
    d = json.loads(ledger.publish_trade_detail(store, pub, win, now).read_text(encoding="utf-8"))
    assert d["trade"]["net"] > 0 and [o["role"] for o in d["orders"]] == ["ENTRY", "EXIT"]
    assert all(f["effective"] for f in d["fills"])
    assert d["path_1m"] == [{"leg_id": 1, "minute": f"{day.isoformat()}T10:05", "phase": "HOLD", "n": 2,
                             "bid_open": 101.0, "bid_high": 102.5, "bid_low": 101.0, "bid_close": 102.5,
                             "ask_close": 102.6, "rb_close": 102.5, "stop_px": 85.0, "lock_armed": 0,
                             "decision": "HOLD", "ltp_info": 102.5}]
    seq = ledger.publish_events_tail(store, pub, now)
    (pub / "events_tail.json").unlink()
    assert ledger.publish_events_tail(store, pub, now, last_seq=seq) == seq and not (pub / "events_tail.json").exists()
    ledger.publish_acks(store, pub, now)
    ledger.publish_g1(pub, g1.scorecards(store), now)
    cards = json.loads((pub / "g1.json").read_text(encoding="utf-8"))["strategies"]
    assert cards[0]["strategy"] == STRAT and cards[0]["state"] == "COLLECTING" and cards[0]["next_look"] == 100
    ledger.publish_trades_90d(store, pub, day + timedelta(days=89), now)
    assert len(json.loads((pub / "trades_90d.json").read_text(encoding="utf-8"))["trades"]) == 2
    ledger.publish_trades_90d(store, pub, day + timedelta(days=90), now)
    assert json.loads((pub / "trades_90d.json").read_text(encoding="utf-8"))["trades"] == []
    for bad in ("../x.json", "/etc/x.json", "a\\b.json", "C:x.json", ""):
        with pytest.raises(ValueError):
            ledger.write_pub_json(pub, bad, {})

    paths = ledger.export_session(store, tmp_path / "exports", day)
    lines = gzip.decompress(paths["trades"].read_bytes()).decode().splitlines()
    assert [json.loads(x)["structure_id"] for x in lines] == [win, loss]
    events = [json.loads(x) for x in gzip.decompress(paths["events"].read_bytes()).decode().splitlines()]
    assert {e["kind"] for e in events} >= {"HEARTBEAT", "NO_BID"} and all("mono" not in e for e in events)
    again = ledger.export_session(store, tmp_path / "exports", day)
    assert again["trades"].read_bytes() == paths["trades"].read_bytes()       # re-export: same bytes

    nightly(store, now, 1.0)
    rep = ledger.daily_report(store, day, now)
    b = rep.data["books"][0]
    nets = [store.trade(win)["net"], store.trade(loss)["net"]]
    assert b["book"] == f"PAPER:{STRAT}" and b["n"] == 2 and b["wins"] == 1 and b["wr_net_pct"] == 50.0
    assert b["net_rs"] == round(sum(nets), 2) and b["worst"]["structure_id"] == loss
    assert b["exit_mix"] == {"TAKE_PROFIT": 2} and b["slip_vs_walk_pct"] == 0.0 and b["ioc_fill_rate_pct"] == 100.0
    assert rep.data["halts"][0]["kind"] == "DAILY_STOP"
    assert rep.data["events"] == [{"kind": "NO_BID", "severity": "WARN", "n": 1}]
    assert rep.data["latency"]["loop_stalls"] == {"n": 1, "max_gap_s": 300.0}
    assert rep.data["g1"][0]["n"] == 2 and rep.data["g1"][0]["next_look"] == 100
    for section in ("## Books", "## Refusals by gate", "## Halts", "## G1 progress", "## Latencies"):
        assert section in rep.markdown
    assert f"PAPER:{STRAT}: n=2 WR 50.0%" in rep.telegram
    path = ledger.write_daily_report(rep, tmp_path / "reports")
    assert path.name == f"{day.isoformat()}.md" and path.read_text(encoding="utf-8") == rep.markdown
    assert rates_for(day).version == rep.data["rates_version"]


def test_daily_report_exit_decision_latency(store):
    """Decision B1: the daily report's latency table has exit_decision_to_submit_ms (n, p50, p95) for the session:
    each leg's exit decision (legs.exiting_since_ts) to its FIRST EXIT order sent (orders.submitted_ts), in wall time
    so a restart in between cannot hide the gap (the Stage 2 criterion: p95 under 1.5 s). Retries, legs that never
    sent an EXIT (closed outside the bot) and other sessions do not count."""
    def leg_with_exits(sid: int, session: date, decided: datetime | None, sent: list[datetime]) -> None:
        it = make_intent(f"lat{sid:09d}", decided or at("10:00:00", session))
        stc = make_structure(sid, it, session=session)
        lg = make_leg(stc, sid, sid, inst_of())
        store.insert_intent(it, session)
        store.insert_structure(stc)
        store.insert_leg(lg, exiting_since_wall=decided)
        for n, when in enumerate(sent, start=2):
            store.insert_order(make_order(lg, Role.EXIT, n, LOT, 0, None, OrderState.CANCELLED, None, limit=99.0,
                                          created=when), submitted_wall=when)

    with store.tx():
        leg_with_exits(1, SESSION, at("10:20:00"), [at("10:20:00.350"), at("10:20:02.000")])   # 350 ms; a retry
        leg_with_exits(2, SESSION, at("11:00:00"), [at("11:00:01.200")])                       # 1,200 ms
        leg_with_exits(3, SESSION, at("12:00:00"), [])                                          # no EXIT order sent
        leg_with_exits(4, SESSION, None, [])                                                    # never exited
        leg_with_exits(5, DAY0, at("10:00:00", DAY0), [at("10:00:09", DAY0)])                   # another session
    rep = ledger.daily_report(store, SESSION, NOW)
    assert rep.data["latency"]["exit_decision_to_submit_ms"] == {"n": 2, "p50": 350.0, "p95": 1200.0}
    assert "| exit_decision_to_submit_ms | 2 | 350.00 | 1200.00 |" in rep.markdown


def test_daily_report_counts_paper_orders_not_filled_by_sim_reason(store):
    """Paper model 2 (review round 2): the report counts the session's paper orders that did not fill, or filled in
    part, by the paper exchange's reason without the order's own numbers. The two new waits that end with no usable
    book ("SIM: crossed or locked book", "SIM: book older than the decision") sit next to "SIM: no fresh depth", IOC
    unfilled and partial fills, and place rejects. Filled orders, Kite's texts on LIVE, EXTERNAL orders (the
    square-off's "SIM: auto square-off") and other sessions do not count."""
    def leg_with_orders(sid: int, session: date, book: str, account: Account, policy: Policy,
                        orders: list[tuple[Role, OrderState, int, int, str | None]]) -> None:
        it = make_intent(f"sim{sid:09d}", at("10:00:00", session))
        stc = make_structure(sid, it, book=book, account=account, session=session, policy=policy)
        lg = make_leg(stc, sid, sid, inst_of())
        store.insert_intent(it, session)
        store.insert_structure(stc)
        store.insert_leg(lg)
        for n, (role, state, qty, filled, msg) in enumerate(orders, start=1):
            o = make_order(lg, role, n, qty, filled, 99.0 if filled else None, state, None, limit=99.0,
                           created=at("10:00:01", session), broker_id=f"P{sid:03d}{n:03d}")
            o.status_msg = msg
            store.insert_order(o)

    cancelled, rejected, complete = OrderState.CANCELLED, OrderState.REJECTED, OrderState.COMPLETE
    paper, shadow = f"PAPER:{STRAT}", f"SHADOW:{STRAT}"
    with store.tx():
        leg_with_orders(1, SESSION, paper, Account.PAPER, Policy.FULL, [
            (Role.ENTRY, cancelled, LOT, 0, "SIM: no fresh depth"),
            (Role.ENTRY, cancelled, LOT, 0, "SIM: crossed or locked book"),
            (Role.ENTRY, complete, LOT, LOT, None)])
        leg_with_orders(2, SESSION, paper, Account.PAPER, Policy.FULL, [
            (Role.EXIT, cancelled, LOT, 0, "SIM: book older than the decision"),
            (Role.EXIT, cancelled, 2 * LOT, LOT, f"SIM: IOC partial fill {LOT}/{2 * LOT}"),
            (Role.EXIT, cancelled, LOT, 0, "SIM: IOC unfilled"),
            (Role.EXTERNAL, complete, LOT, LOT, "SIM: auto square-off")])
        leg_with_orders(3, SESSION, shadow, Account.SHADOW, Policy.QUALITY, [
            (Role.ENTRY, rejected, LOT, 0, "SIM: insufficient funds: need Rs 4,955.00, available Rs 120.00"),
            (Role.ENTRY, cancelled, LOT, 0, "SIM: no fresh depth")])
        leg_with_orders(4, SESSION, f"LIVE:{STRAT}", Account.LIVE, Policy.FULL, [
            (Role.ENTRY, cancelled, LOT, 0, "IOC order cancelled: unfilled")])
        leg_with_orders(5, DAY0, paper, Account.PAPER, Policy.FULL, [
            (Role.ENTRY, cancelled, LOT, 0, "SIM: crossed or locked book")])
    rep = ledger.daily_report(store, SESSION, NOW)
    assert rep.data["refusals"]["sim"] == [
        {"book": paper, "reason": "SIM: IOC partial fill", "n": 1},
        {"book": paper, "reason": "SIM: IOC unfilled", "n": 1},
        {"book": paper, "reason": "SIM: book older than the decision", "n": 1},
        {"book": paper, "reason": "SIM: crossed or locked book", "n": 1},
        {"book": paper, "reason": "SIM: no fresh depth", "n": 1},
        {"book": shadow, "reason": "SIM: insufficient funds", "n": 1},
        {"book": shadow, "reason": "SIM: no fresh depth", "n": 1}]
    section = rep.markdown.split("Paper orders not filled, by SIM reason:", 1)[1].split("## ", 1)[0]
    assert f"| {paper} | SIM: crossed or locked book | 1 |" in section
    assert f"| {paper} | SIM: book older than the decision | 1 |" in section


def test_pub_replace_retries_a_briefly_locked_target(tmp_path, monkeypatch):
    """Windows: a reader holding pub/status.json (the dashboard, an indexer) makes os.replace fail with
    PermissionError for a moment; the write retries a few times before giving up (POSIX makes one attempt: its
    rename never needs it)."""
    real, calls = ledger.os.replace, []

    def locked(src, dst):
        calls.append(dst)
        if len(calls) <= 2:
            raise PermissionError(13, "Access is denied")
        return real(src, dst)

    monkeypatch.setattr(ledger, "_REPLACE_TRIES", 3)
    monkeypatch.setattr(ledger, "_REPLACE_BACKOFF_S", 0.0)
    monkeypatch.setattr(ledger.os, "replace", locked)
    path = ledger.write_pub_json(tmp_path / "pub", "status.json", {"ok": True})
    assert len(calls) == 3 and json.loads(path.read_text(encoding="utf-8")) == {"ok": True}
    calls.clear()
    monkeypatch.setattr(ledger, "_REPLACE_TRIES", 2)                   # still locked after the last try: it raises
    with pytest.raises(PermissionError):
        ledger.write_pub_json(tmp_path / "pub", "status.json", {"ok": False})
    assert len(calls) == 2 and json.loads(path.read_text(encoding="utf-8")) == {"ok": True}
    assert not [f for f in (tmp_path / "pub").iterdir() if f.name.endswith(".tmp")]   # no temp file left behind
