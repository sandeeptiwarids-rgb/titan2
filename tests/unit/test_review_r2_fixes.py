"""Regression tests for review round 2 (2026-10-14) on titan2's bookkeeping (no paper fill, strategy or setting change).

- A3: a failed PAPER truth check wrote EOD_TRUTH_MISMATCH (no event book, no symbol), which G1 counted against every
  PAPER book, and a second time for the bad book next to its EOD_TRUTH_CHECK. A failure now counts once, through the
  check, and only against the books it names (every book only when the check cannot attribute, DETAIL §9.8).
- A5: a late-filled entry adopted into a RECOVERED structure (Engine._adopt_late_fill) was never subscribed on the
  WS after its VOID released the token, so it exited on REST quotes only and its post-exit tail wrote nothing.
- A6: RECOVERED:<account>[:<seq>] books (recovery's own: a lost DB's imports, an adopted late fill) were no book at
  all for risk: outside the per-book daily stop, the LIVE account's daily stop and the LIVE account guard. They are
  FULL books of their account now, never entry-capped (I11 counts entries, which recovery never makes).
- A7: after a process restart an UNKNOWN order had no late-accept window (decision B2): NOT_PLACED 15 s after the
  restart, a second SELL inside the 30 s. A restart now gives every UNKNOWN order, and every SUBMITTING one (its Place
  may have been sent), a fresh LATE_ACCEPT_S window.
- A2 (attribution): the EOD truth check takes today's ledger and orders by structure session; a leftover of an
  earlier session settled today is checked against its own trade, never against today's ledger.
- Follow-ups from the fidelity review of these fixes: P1, a leftover closed UNRESOLVED at today's start (no broker
  order) was taken into the truth check and failed it; P3, the late start's grace counted from the process start, so
  after a slow start a READY read made before the paper square-off filled released the truth check.
- A4 x N5 (the review of round 3): EOD step rows an old `titan2 laptop --date D` rehearsal wrote into the real
  titan2.db before D made the engine skip D's EOD steps (state.eod_phase_rows now leaves them out); the runtime's side
  (the recovery-only start) is tested in test_runtime_cli.py.

A1's pieces are tested beside their neighbours (test_paper.py: no settlement without a session, the square-off record;
test_recon_review.py: recon.carried_closes; test_runtime_cli.py: the recovery-only run's wait), and A1, A2 and A7 end
to end in tests/scenarios/test_review_r2_recovery.py.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import pytest

import test_ledger_g1 as h
from test_engine import ENTRY_DQ, ENTRY_LIMIT, LOT, FakeGateway, Rig
from test_late_fill_and_selftest import _give_up, _late
from titan2 import g1, ledger
from titan2 import store as store_mod
from titan2.clock import IST, iso, ist_datetime
from titan2.core import invariants, orders, risk
from titan2.core.engine import Engine
from titan2.core.events import RecoveryStart, Subscribe, TradebookSnapshot, Unsubscribe
from titan2.model import (
    Account, BrokerTrade, DecisionQuote, Order, OrderState, PnlStatus, Policy, Role, Side, StructState, make_tag,
)
from titan2.settings import DEFAULTS, FRESH_FILL_GRACE_S
from titan2.store import Store

DAY = date(2026, 10, 14)


@pytest.fixture
def core():
    with store_mod.core_thread():
        yield


# ---------------------------------------------------------------------------------------------------------------
# A3: a failed truth check counts once, for the books it names
# ---------------------------------------------------------------------------------------------------------------
def _paper_trade(s: Store, strategy: str, broker_ids: tuple[str, str], exit_px: float, minute: int) -> int:
    return _trade(s, Account.PAPER, strategy, broker_ids, exit_px, minute)


def _trade(s: Store, account: Account, strategy: str, broker_ids: tuple[str, str], exit_px: float,
           minute: int) -> int:
    """test_ledger_g1.put_trade, but the bot orders carry broker ids (the truth check attributes by broker id)."""
    book = f"{account.value}:{strategy}"
    t_in = datetime.combine(DAY, time(10, 0), IST) + timedelta(minutes=minute)
    t_out = t_in + timedelta(minutes=20)
    with s.tx():
        it = h.make_intent(f"i{strategy[:6]}{minute:05d}", t_in, strategy=strategy)
        s.insert_intent(it, DAY)
        stc = h.make_structure(0, it, book=book, account=account, session=DAY, risk=100.0 * h.LOT,
                               policy=Policy.FULL)
        s.insert_structure(stc)
        lg = h.make_leg(stc, 0, s.next_leg_seq(account, DAY), h.inst_of("CE"))
        s.insert_leg(lg)
        ent = h.make_order(lg, Role.ENTRY, 1, h.LOT, h.LOT, 100.0, OrderState.COMPLETE,
                           DecisionQuote(99.9, 100.0, 99.95, 100, "WS", 100.0, 100.0), limit=100.1, created=t_in,
                           broker_id=broker_ids[0])
        ex = h.make_order(lg, Role.EXIT, 1, h.LOT, h.LOT, exit_px, OrderState.COMPLETE,
                          DecisionQuote(exit_px, exit_px + 0.1, exit_px + 0.05, 100, "WS", exit_px, exit_px),
                          limit=round(exit_px - 0.1, 2), created=t_out - timedelta(seconds=1), broker_id=broker_ids[1])
        for o in (ent, ex):
            s.insert_order(o)
        s.insert_fill(h.fill(ent, h.LOT, 100.0, t_in + timedelta(seconds=1)))
        s.insert_fill(h.fill(ex, h.LOT, exit_px, t_out))
        row = ledger.build_trade_row(stc, [lg], [ent, ex], s.effective_fills([ent.order_pk, ex.order_pk]),
                                     exit_wall=t_out + timedelta(seconds=5))
        s.insert_trade(row)
    return stc.structure_id


def _fake_engine(s: Store, now: datetime) -> SimpleNamespace:
    fake = SimpleNamespace(store=s, session=DAY, now_wall=now, now_mono=1.0)
    fake.event = lambda kind, payload=None, severity="INFO", **refs: Engine.event(fake, kind, payload, severity,
                                                                                  **refs)
    return fake


def test_a_truth_mismatch_in_one_book_counts_once_and_only_for_that_book(tmp_path, core):
    """Ported from the review's R2-money probe: a 5-paise drift in PAPER:opening_drive's exit."""
    s = Store(tmp_path / "db" / "titan2.db", backup_dir=tmp_path / "backups")
    try:
        _paper_trade(s, "opening_drive", ("P1", "P2"), 104.0, 0)
        _paper_trade(s, "price_spike_up", ("P3", "P4"), 104.0, 30)
        sym = h.inst_of("CE").symbol
        w = datetime.combine(DAY, time(11, 0), IST)
        trades = (BrokerTrade(Account.PAPER, "t1", "P1", sym, Side.BUY, h.LOT, 100.0, w, None),
                  BrokerTrade(Account.PAPER, "t2", "P2", sym, Side.SELL, h.LOT, 104.05, w, None),
                  BrokerTrade(Account.PAPER, "t3", "P3", sym, Side.BUY, h.LOT, 100.0, w, None),
                  BrokerTrade(Account.PAPER, "t4", "P4", sym, Side.SELL, h.LOT, 104.0, w, None))
        fake = _fake_engine(s, datetime.combine(DAY, time(15, 33), IST))
        with s.tx():
            Engine._truth_check(fake, Account.PAPER, TradebookSnapshot(Account.PAPER, True, trades, 0.0))
        check = s.events(kind="EOD_TRUTH_CHECK")[-1]["payload"]
        diffs = {b["book"]: b["diff"] for b in check["books"]}
        assert check["ok"] is False and diffs["PAPER:price_spike_up"] == 0.0 and diffs["PAPER:opening_drive"] != 0
        assert s.events(kind="EOD_TRUTH_MISMATCH")[-1]["payload"]["books"] == ["PAPER:opening_drive"]
        d = DAY.isoformat()
        assert g1.count_mismatches(s, "PAPER:opening_drive", "PAPER", d, d) == 1        # once, not twice
        assert g1.count_mismatches(s, "PAPER:price_spike_up", "PAPER", d, d) == 0       # matched to the paisa
    finally:
        s.close()


def test_g1_counts_a_truth_check_failure_through_the_check_only():
    """EOD_TRUTH_MISMATCH is the alert's record of the failed EOD_TRUTH_CHECK beside it: it never counts on its own.
    The check keeps DETAIL §9.8's rule: the books it names, every book when it cannot attribute."""
    book = "PAPER:price_spike_up"
    assert not g1._counts_against_book("EOD_TRUTH_MISMATCH", None, {"account": "PAPER", "books": [book]}, book,
                                       set())
    assert not g1._counts_against_book("EOD_TRUTH_MISMATCH", None, {"account": "PAPER", "books": []}, book, set())
    named = {"ok": False, "broker_net": 1.0, "unattributed": [], "books": [{"book": book, "diff": 0.05}]}
    other = {"ok": False, "broker_net": 1.0, "unattributed": [], "books": [{"book": "PAPER:x", "diff": 0.05}]}
    assert g1._counts_against_book("EOD_TRUTH_CHECK", None, named, book, set())
    assert not g1._counts_against_book("EOD_TRUTH_CHECK", None, other, book, set())
    blind = {"ok": False, "broker_net": None, "unattributed": [], "books": []}
    stray = {"ok": False, "broker_net": 1.0, "unattributed": ["X9"], "books": []}
    assert g1._counts_against_book("EOD_TRUTH_CHECK", None, blind, book, set())         # every book
    assert g1._counts_against_book("EOD_TRUTH_CHECK", None, stray, book, set())


# ---------------------------------------------------------------------------------------------------------------
# A2: a leftover settled today is checked against its own trade, not today's ledger
# ---------------------------------------------------------------------------------------------------------------
PREV = date(2026, 10, 13)


def _leftover(s: Store, *, book: bool = True) -> int:
    """PAPER:opening_drive bought 50 at 100 on PREV ("PE1") and never came back that day; the paper exchange
    settled it at today's start at Rs 0.05 ("SQ1", an EXCHANGE order), and recon booked it today: the trade belongs
    to PREV, its EXTERNAL row to today (book=False: not booked yet)."""
    t_in = datetime.combine(PREV, time(10, 0), IST)
    t_sq = datetime.combine(DAY, time(9, 18, 30), IST)
    with s.tx():
        it = h.make_intent("ileftover001", t_in, strategy="opening_drive")
        s.insert_intent(it, PREV)
        stc = h.make_structure(0, it, book="PAPER:opening_drive", account=Account.PAPER, session=PREV,
                               risk=100.0 * h.LOT, policy=Policy.FULL, exit_reason="BROKER_SQUAREOFF")
        s.insert_structure(stc)
        lg = h.make_leg(stc, 0, s.next_leg_seq(Account.PAPER, PREV), h.inst_of("CE"))
        s.insert_leg(lg)
        ent = h.make_order(lg, Role.ENTRY, 1, h.LOT, h.LOT, 100.0, OrderState.COMPLETE,
                           DecisionQuote(99.9, 100.0, 99.95, 100, "WS", 100.0, 100.0), limit=100.1, created=t_in,
                           broker_id="PE1")
        sq = h.make_order(lg, Role.EXTERNAL, 0, h.LOT, h.LOT, 0.05, OrderState.COMPLETE, None,
                          ext="AUTO_SQUAREOFF", created=t_sq, broker_id="SQ1")
        for o in (ent, sq):
            s.insert_order(o)
        s.insert_fill(h.fill(ent, h.LOT, 100.0, t_in + timedelta(seconds=1)))
        s.insert_fill(h.fill(sq, h.LOT, 0.05, t_sq))
        if book:
            s.insert_trade(ledger.build_trade_row(stc, [lg], [ent, sq], s.effective_fills([ent.order_pk, sq.order_pk]),
                                                  exit_wall=t_sq + timedelta(seconds=10)))
    return stc.structure_id


def _check(s: Store, settle_px: float) -> dict:
    """Today's 15:33 PAPER check on a tradebook that holds price_spike_up's round trip and the leftover's
    settlement at settle_px."""
    sym = h.inst_of("CE").symbol
    w = datetime.combine(DAY, time(11, 0), IST)
    trades = (BrokerTrade(Account.PAPER, "t3", "P3", sym, Side.BUY, h.LOT, 100.0, w, None),
              BrokerTrade(Account.PAPER, "t4", "P4", sym, Side.SELL, h.LOT, 104.0, w, None),
              BrokerTrade(Account.PAPER, "t9", "SQ1", sym, Side.SELL, h.LOT, settle_px, w, None))
    fake = _fake_engine(s, datetime.combine(DAY, time(15, 33), IST))
    with s.tx():
        Engine._truth_check(fake, Account.PAPER, TradebookSnapshot(Account.PAPER, True, trades, 0.0))
    return s.events(kind="EOD_TRUTH_CHECK")[-1]["payload"]


def test_a_leftover_settled_today_is_checked_against_its_own_trade(tmp_path, core):
    s = Store(tmp_path / "db" / "titan2.db", backup_dir=tmp_path / "backups")
    try:
        sid = _leftover(s)
        _paper_trade(s, "price_spike_up", ("P3", "P4"), 104.0, 30)
        ti = ledger.truth_inputs(s, Account.PAPER, DAY)
        assert set(ti.order_books) == {"P3", "P4"} and set(ti.ledger_by_book) == {"PAPER:price_spike_up"}
        (c,) = ti.carried
        assert (c.session, c.structures, c.today_ids) == (PREV, (sid,), frozenset({"SQ1"}))
        assert [t.broker_order_id for t in c.earlier] == ["PE1"]       # PREV's fill, as titan2.db recorded it
        p = _check(s, 0.05)
        assert p["ok"] is True and p["carried_orders"] == ["SQ1"], p
        rows = {(b["book"], b.get("session")): b["diff"] for b in p["books"]}
        assert rows == {("PAPER:price_spike_up", None): 0.0, ("PAPER:opening_drive", PREV.isoformat()): 0.0}
        assert p["carried"] == [{"session": PREV.isoformat(), "ok": True, "ledger_net": p["carried"][0]["ledger_net"],
                                 "broker_net": p["carried"][0]["broker_net"]}]
        assert not s.events(kind="EOD_TRUTH_MISMATCH")
        # the paper exchange's settlement differs from what was booked: a mismatch for the leftover's book only
        p = _check(s, 0.10)
        assert p["ok"] is False
        assert s.events(kind="EOD_TRUTH_MISMATCH")[-1]["payload"]["books"] == ["PAPER:opening_drive"]
        d = DAY.isoformat()
        assert g1.count_mismatches(s, "PAPER:opening_drive", "PAPER", d, d) == 1
        assert g1.count_mismatches(s, "PAPER:price_spike_up", "PAPER", d, d) == 0
    finally:
        s.close()


def test_an_unbooked_leftover_with_a_settlement_today_fails_its_own_check(tmp_path, core):
    s = Store(tmp_path / "db" / "titan2.db", backup_dir=tmp_path / "backups")
    try:
        _leftover(s, book=False)
        _paper_trade(s, "price_spike_up", ("P3", "P4"), 104.0, 30)
        p = _check(s, 0.05)
        assert p["ok"] is False and p["carried"][0]["ok"] is False
        assert s.events(kind="EOD_TRUTH_MISMATCH")[-1]["payload"]["books"] == ["PAPER:opening_drive"]
        d = DAY.isoformat()
        assert g1.count_mismatches(s, "PAPER:price_spike_up", "PAPER", d, d) == 0
    finally:
        s.close()


def _unresolved_leftover(s: Store, account: Account, *, priced: bool, strategy: str = "opening_drive",
                         entry_id: str = "PE1") -> int:
    """<account>:<strategy> bought 50 at 100 on PREV (entry_id) and was still held at today's start, but the broker
    held none of it: recovery closed it UNRESOLVED (CLOSE_UNRESOLVED, or the paper backstop with no record) with an
    EXTERNAL row EX:<leg_id>:1 that has no broker order, at the last realizable bid 60.0 (priced) or unpriced."""
    t_in = datetime.combine(PREV, time(10, 0), IST)
    t_close = datetime.combine(DAY, time(9, 18, 30), IST)
    with s.tx():
        it = h.make_intent(f"iunres{strategy[:6]}", t_in, strategy=strategy)
        s.insert_intent(it, PREV)
        stc = h.make_structure(0, it, book=f"{account.value}:{strategy}", account=account, session=PREV,
                               risk=100.0 * h.LOT, policy=Policy.FULL, exit_reason="CLOSED_EXTERNAL")
        s.insert_structure(stc)
        lg = h.make_leg(stc, 0, s.next_leg_seq(account, PREV), h.inst_of("PE"))
        s.insert_leg(lg)
        ent = h.make_order(lg, Role.ENTRY, 1, h.LOT, h.LOT, 100.0, OrderState.COMPLETE,
                           DecisionQuote(99.9, 100.0, 99.95, 100, "WS", 100.0, 100.0), limit=100.1, created=t_in,
                           broker_id=entry_id)
        ex = Order(order_pk=f"EX:{lg.leg_id}:1", leg_id=lg.leg_id, structure_id=lg.structure_id, account=account,
                   book=lg.book, role=Role.EXTERNAL, side=Side.SELL, symbol=lg.inst.symbol, qty=h.LOT, limit_px=None,
                   attempt=0, band_idx=None, state=OrderState.COMPLETE, broker_order_id=None, external_kind="MANUAL",
                   filled_qty=h.LOT, avg_fill_px=60.0 if priced else None, decision=None, created_wall=t_close)
        for o in (ent, ex):
            s.insert_order(o)
        s.insert_fill(h.fill(ent, h.LOT, 100.0, t_in + timedelta(seconds=1)))
        if priced:
            s.insert_fill(h.fill(ex, h.LOT, 60.0, t_close))
        s.insert_trade(ledger.build_trade_row(stc, [lg], [ent, ex], s.effective_fills([ent.order_pk, ex.order_pk]),
                                              exit_wall=t_close, pnl_status=PnlStatus.UNRESOLVED,
                                              exit_reason="CLOSED_EXTERNAL"))
    return stc.structure_id


def _truth(s: Store, account: Account, trades: tuple[BrokerTrade, ...]) -> tuple[dict, list]:
    """Today's 15:33 check of `account` on this tradebook: the EOD_TRUTH_CHECK payload and the alerts."""
    fake = _fake_engine(s, datetime.combine(DAY, time(15, 33), IST))
    with s.tx():
        alerts = Engine._truth_check(fake, account, TradebookSnapshot(account, True, trades, 0.0))
    return s.events(kind="EOD_TRUTH_CHECK")[-1]["payload"], alerts


def _round_trip(account: Account) -> tuple[BrokerTrade, ...]:
    """The tradebook rows of _trade(s, account, "price_spike_up", ("P3", "P4"), 104.0, 30)."""
    sym, w = h.inst_of("CE").symbol, datetime.combine(DAY, time(11, 0), IST)
    return (BrokerTrade(account, "t3", "P3", sym, Side.BUY, h.LOT, 100.0, w, None),
            BrokerTrade(account, "t4", "P4", sym, Side.SELL, h.LOT, 104.0, w, None))


@pytest.mark.parametrize("account,priced", [(Account.PAPER, True), (Account.PAPER, False), (Account.LIVE, True)])
def test_a_leftover_closed_unresolved_today_is_never_compared_with_the_tradebook(tmp_path, core, account, priced):
    """Ported from the fidelity review's probe (review round 2 follow-up, P1): a leftover closed UNRESOLVED at today's
    start (its EX:<leg_id>:1 row has no broker order) became a carried truth check. Its exit value went on the ledger
    side with nothing on the broker side, so a close priced at the last realizable bid failed the whole 15:33 check:
    a false CRITICAL EOD_TRUTH_MISMATCH ("a leftover of ... settled today") on LIVE, a failed check and a G1
    mismatch on PAPER. No tradebook can show such a close: only a broker order executed today makes a leftover
    carried, and the close stays reported as unresolved (CLOSED_EXTERNAL, UNRESOLVED, MISMATCH)."""
    s = Store(tmp_path / "db" / "titan2.db", backup_dir=tmp_path / "backups")
    try:
        _unresolved_leftover(s, account, priced=priced)
        _trade(s, account, "price_spike_up", ("P3", "P4"), 104.0, 30)
        p, alerts = _truth(s, account, _round_trip(account))
        assert p["ok"] is True and alerts == [], (p, alerts)
        assert not p.get("carried") and [b["book"] for b in p["books"]] == [f"{account.value}:price_spike_up"], p
        assert not s.events(kind="EOD_TRUTH_MISMATCH")
        ti = ledger.truth_inputs(s, account, DAY)
        assert ti.carried == () and set(ti.order_books) == {"P3", "P4"}
        if account is not Account.LIVE:
            d = DAY.isoformat()
            assert g1.count_mismatches(s, "PAPER:price_spike_up", "PAPER", d, d) == 0
    finally:
        s.close()


def test_a_real_settlement_is_still_checked_beside_a_close_without_a_broker_order(tmp_path, core):
    """P1's other half: on the same day, a leftover the paper exchange settled at today's start ("SQ1") is still
    checked against its own trade, and a wrong settlement still fails, for its book only; the leftover closed
    without a broker order stays out of the check."""
    s = Store(tmp_path / "db" / "titan2.db", backup_dir=tmp_path / "backups")
    try:
        settled = _leftover(s)
        _unresolved_leftover(s, Account.PAPER, priced=True, strategy="price_spike_down", entry_id="PE2")
        _paper_trade(s, "price_spike_up", ("P3", "P4"), 104.0, 30)
        (c,) = ledger.truth_inputs(s, Account.PAPER, DAY).carried
        assert (c.structures, c.today_ids, set(c.ledger_by_book)) == ((settled,), frozenset({"SQ1"}),
                                                                     {"PAPER:opening_drive"})
        p = _check(s, 0.05)
        assert p["ok"] is True and p["carried_orders"] == ["SQ1"], p
        assert {(b["book"], b.get("session")) for b in p["books"]} == {("PAPER:price_spike_up", None),
                                                                        ("PAPER:opening_drive", PREV.isoformat())}
        p = _check(s, 0.10)
        assert p["ok"] is False
        assert s.events(kind="EOD_TRUTH_MISMATCH")[-1]["payload"]["books"] == ["PAPER:opening_drive"]
    finally:
        s.close()


# ---------------------------------------------------------------------------------------------------------------
# A5: the adopted late fill is subscribed on the WS
# ---------------------------------------------------------------------------------------------------------------
@pytest.fixture
def rig(tmp_path, core):
    r = Rig(tmp_path)
    r.tick()
    yield r
    r.store.close()


def test_an_adopted_late_fill_is_subscribed_in_full_mode(rig):
    """Ported from the review's probe 05 (exits lens), which pinned the missing Subscribe."""
    rig.book(41.90, 42.15)
    pk = rig.intent()
    token = rig.eng.state.legs[rig.eng.state.orders[pk].leg_id].inst.token
    _give_up(rig, pk)
    assert rig.eng.state.orders[pk].state is OrderState.NOT_PLACED
    assert [c for c in rig.of(Unsubscribe) if token in c.tokens], "the VOID released the token"
    rig.book(42.30, 42.50)
    out = _late(rig, pk, "COMPLETE", LOT, 42.20)
    (rec,) = [s for s in rig.eng.state.structures.values() if s.book == "RECOVERED:PAPER"]
    assert [c for c in out if isinstance(c, Subscribe) and token in c.tokens and c.mode == "full"]
    assert token in rig.eng.state.held_tokens()


# ---------------------------------------------------------------------------------------------------------------
# A6: RECOVERED books are FULL books of their account
# ---------------------------------------------------------------------------------------------------------------
def _bs(book: str, realized: float, mtm: float = 0.0, open_: int = 0) -> risk.BookState:
    return risk.BookState(book=book, policy=risk.book_policy(book) or Policy.FULL, open=open_, reserved=0, entries=0,
                          attempts=0, orders_today=0, entry_orders_60s=0, realized_net=realized, mtm_net=mtm,
                          first_loss_today=False, cooldown_until={}, active_underlyings=frozenset(),
                          exposures_rs=(25_000.0,) if open_ else ())


def test_recovered_books_are_full_books_of_their_account():
    for book, acct in (("RECOVERED:LIVE", Account.LIVE), ("RECOVERED:PAPER", Account.PAPER),
                       ("RECOVERED:SHADOW", Account.SHADOW), ("RECOVERED:LIVE:3", Account.LIVE),
                       ("RECOVERED:PAPER:12", Account.PAPER)):
        assert risk.book_account(book) is acct and risk.book_policy(book) is Policy.FULL, book
        assert risk.make_halt("EXIT_DIFFICULTY", f"BOOK:{book}", "x").scope == f"BOOK:{book}"
    for bad in ("RECOVERED:", "RECOVERED:FOO", "RECOVERED:LIVE:", "RECOVERED:LIVE:x", "RECOVERED:LIVE:3:4",
                "RECOVERED: LIVE", "RECOVERED:LIVE:-1"):
        assert risk.parse_book(bad) is None and risk.book_account(bad) is None and risk.book_policy(bad) is None, bad
    assert risk.book_account("PAPER:opening_drive") is Account.PAPER                     # strategy books unchanged
    assert risk.book_policy(risk.SELFTEST_BOOK) is None


def test_a_recovered_live_loss_trips_the_live_account_daily_stop():
    """Ported from the review's recon/recovery probe."""
    loss = -(DEFAULTS.LIVE_ACCOUNT_DAILY_STOP_RS + 1000.0)
    books = [_bs("LIVE:price_spike_up", 0.0), _bs("RECOVERED:LIVE", loss)]
    chk = risk.check_daily_stops(books, (), DEFAULTS, date(2026, 10, 12))
    assert any(t.halt.scope == "ACCOUNT:LIVE" for t in chk.trips)
    assert "LIVE:price_spike_up" in chk.force_exit and "RECOVERED:LIVE" in chk.force_exit


def test_the_live_account_guard_counts_a_recovered_live_book():
    """Ported from the review's R2-money probe: a recovered LIVE leg Rs 9,000 down and a LIVE book Rs 1,500 down."""
    rec = _bs("RECOVERED:LIVE", -6_000.0, -3_000.0, open_=1)
    live = _bs("LIVE:price_spike_up", -1_500.0)
    la = risk.live_account_state([rec, live])                                # what Engine.gate_context builds
    assert la.realized_net + la.mtm_net <= -DEFAULTS.LIVE_ACCOUNT_DAILY_STOP_RS
    assert la.open == 1 and la.exposures_rs == (25_000.0,)
    chk = risk.check_daily_stops([rec, live], (), DEFAULTS, date(2026, 10, 14))    # _risk_tick passes it now
    assert [t.halt.scope for t in chk.trips] == ["ACCOUNT:LIVE"]


class _RecoveredGateway(FakeGateway):
    """Enters into a RECOVERED:PAPER book with the account and policy given, as recovery creates its structures."""

    def on_intent(self, eng, intent):
        eng.store.insert_intent(intent, eng.session)
        st = eng.reserve(intent, book="RECOVERED:PAPER", account=Account.PAPER, policy=Policy.FULL)
        leg, o, cmds = eng.begin_entry(st, inst=self.inst[intent.underlying], qty=LOT, limit_px=ENTRY_LIMIT,
                                       decision_quote=ENTRY_DQ, decision={"limit": ENTRY_LIMIT})
        self.placed.append(o.order_pk)
        return cmds


def _recovered_rig(tmp_path, **settings) -> Rig:
    r = Rig(tmp_path)
    r.gateway = r.ctx.gateway = _RecoveredGateway()
    r.ctx.settings = dataclasses.replace(DEFAULTS, **settings)
    r.eng = Engine.load(r.store, r.ctx)
    r.tick()
    return r


def test_the_risk_tick_runs_the_daily_stop_on_a_recovered_book(tmp_path, core):
    r = _recovered_rig(tmp_path, DAILY_STOP_RS=1000.0)
    try:
        r.exit_loop.act = False                           # the leg stays open: its MTM is what the stop reads
        leg = r.open_leg()
        assert leg.book == "RECOVERED:PAPER" and leg.open_qty == LOT
        for _ in range(8):
            r.book(30.00, 30.20)                          # about Rs 1,240 down on 100 at 42.20
            r.tick()
        (stop,) = r.halts("DAILY_STOP")
        assert stop.scope == "BOOK:RECOVERED:PAPER"
        assert r.leg(leg.leg_id).state.value == "EXITING"
    finally:
        r.store.close()


def test_recovered_books_are_never_entry_capped(tmp_path, core):
    """I11 counts the gateway's entries: recovery may put two positions into one RECOVERED book (a lost DB's imports
    on two underlyings) whatever MAX_POSITIONS_PER_BOOK says, and that is no invariant violation."""
    r = _recovered_rig(tmp_path, MAX_POSITIONS_PER_BOOK=1)
    try:
        r.exit_loop.act = False
        a = r.open_leg("NSE:RELIANCE")
        b = r.open_leg("NSE:INFY")
        assert {r.struct(x.leg_id).state for x in (a, b)} == {StructState.OPEN}
        assert not [v for v in invariants.state_violations(r.eng) if v.code == "I11"]
        assert not r.halts("INVARIANT") and not r.events("INVARIANT_VIOLATION")
    finally:
        r.store.close()


# ---------------------------------------------------------------------------------------------------------------
# A7: a restart gives an order that may have reached Kite a fresh late-accept window
# ---------------------------------------------------------------------------------------------------------------
def _order(state: OrderState) -> Order:
    pk = make_tag(Role.EXIT, Account.LIVE, date(2026, 10, 12), 1, 2)
    return Order(order_pk=pk, leg_id=1, structure_id=1, account=Account.LIVE, book="LIVE:x", role=Role.EXIT,
                 side=Side.SELL, symbol="NFO:X", qty=50, limit_px=10.0, attempt=2, band_idx=0, state=state,
                 decision=None, created_wall=datetime(2026, 10, 12, 10, tzinfo=IST), submitted_mono=None)


@pytest.mark.parametrize("state", [OrderState.UNKNOWN, OrderState.SUBMITTING])
def test_a_restart_restarts_the_late_accept_window(state):
    """Ported from the review's recon/recovery probe (UNKNOWN), and the same for a SUBMITTING row whose Place may
    have been sent before the crash."""
    t, first = orders.timers_after_reload(_order(state), 1000.0, restart=True)
    assert first == [] and t.late_until == 1000.0 + orders.LATE_ACCEPT_S
    assert t.lookups_due == [1001.0, 1003.0, 1007.0, 1015.0, 1020.0, 1025.0, 1030.0]


def test_a_restart_still_leaves_working_orders_and_planned_ones_alone():
    t, first = orders.timers_after_reload(dataclasses.replace(_order(OrderState.WORKING), broker_order_id="B1"),
                                          1000.0, restart=True)
    assert t.late_until is None and [type(c).__name__ for c in first] == ["PollOrder"]
    t, first = orders.timers_after_reload(_order(OrderState.NEW), 1000.0, restart=True)
    assert t.late_until is None and t.lookups_due == [] and first == []


# ---------------------------------------------------------------------------------------------------------------
# Follow-up P3: a late start's grace counts from READY
# ---------------------------------------------------------------------------------------------------------------
def test_a_late_starts_grace_counts_from_ready_not_from_the_process_start(tmp_path, core):
    """Review round 2 follow-up (P3): the late hold ended on the first CLEAN recon whose positions read was asked
    FRESH_FILL_GRACE_S after ctx.start_mono, which BotProcess takes before the store open, the backup, the token
    check and recovery. After a slow start the READY recon was already past that grace, so a CLEAN read made before
    the paper exchange's square-off filled (it waits up to 1.5 s for depth) released the 15:33 truth check, which
    then found that square-off unattributed (a G1 mismatch for every PAPER book). The grace counts from READY now."""
    r = Rig(tmp_path, act=False)
    try:
        r.tick()
        leg = r.open_leg()
        r.clock.advance_to(ist_datetime(r.clock.now_wall().date(), "15:34:00"))   # back after TRUTH's 15:33
        r.ctx = dataclasses.replace(r.ctx, recovered=False, recovery_started=False,
                                    start_mono=r.clock.mono() - 10.0)    # 10 s of store open, backup, token check
        r.eng = Engine.load(r.store, r.ctx)
        r.run(RecoveryStart())
        ready = r.clock.mono()
        assert r.eng.recovery_done and r.events("LATE_START") and r.leg(leg.leg_id).open_qty == LOT
        assert r.eng.state.broker[Account.PAPER].recon.status == "CLEAN"       # read before any square-off
        r.tick()
        assert r.eng.late_recon_holding and "TRUTH" not in r.eng.state.eod_done
        while "TRUTH" not in r.eng.state.eod_done:
            assert r.clock.mono() - ready < 15.0, "the late hold never ended"
            r.tick()
        assert r.eng.state.broker[Account.PAPER].recon_req > ready + FRESH_FILL_GRACE_S
    finally:
        r.store.close()


# ---------------------------------------------------------------------------------------------------------------
# A4 x N5 (the review of round 3): the EOD rows an old rehearsal left in the real state folder
# ---------------------------------------------------------------------------------------------------------------
def test_a_rehearsals_eod_rows_never_end_the_real_days_timeline(tmp_path, core, caplog):
    """Before `titan2 laptop --date D` had its own folders (N5), a rehearsal wrote D's whole EOD timeline into the
    real titan2.db, each step stamped at its time on D. EngineState.load rebuilt eod_done from every PHASE row of the
    session, so on the real D the engine called the day DONE from the morning and skipped D's EOD steps: the 15:10
    flatten, the truth check, the nightly files, the exit. Rows stamped later than now were not written by a run on
    D: ignored with a WARN, and every step runs at its time."""
    r = Rig(tmp_path)
    try:
        r.tick()
        session = r.eng.session
        with r.store.tx():
            stale = [r.store.append_event("PHASE", wall=at, mono=1.0, session=session,
                                          payload={"phase": "DONE" if step == "DONE" else "EOD", "step": step,
                                                   "at": iso(at)})
                     for step, at in r.eng.eod_steps()]
        with caplog.at_level("WARNING", logger="titan2.core.state"):
            r.eng = Engine.load(r.store, r.ctx)
        assert r.eng.state.eod_done == set() and r.eng.phase == "TRADING"
        assert any(f"ignoring {len(stale)} EOD step row(s)" in m.getMessage() for m in caplog.records)
        flatten = dict(r.eng.eod_steps())["FLATTEN"]
        r.clock.advance_to(flatten - timedelta(seconds=1))
        r.tick()
        r.tick(1.5)
        ran = [e["payload"].get("step") for e in r.events("PHASE") if e["seq"] > max(stale)]
        assert ran == ["FLATTEN"] and r.eng.state.eod_done == {"FLATTEN"} and r.eng.phase == "EOD"
    finally:
        r.store.close()


def test_eod_phase_rows_keep_only_what_a_run_on_that_day_wrote(tmp_path, core):
    """state.eod_phase_rows, the rule EngineState.load and the runtime's recovery-only decision share: a PHASE row
    stamped later than now, or than a start (BOOT) written after it, is ignored. A rehearsal's rows come before the
    real day's first start, which is stamped earlier than they are; a row the day's own run wrote comes after its
    start and before now, and a later restart's BOOT is stamped after it."""
    from titan2.core.state import eod_phase_rows

    s = Store(tmp_path / "db" / "titan2.db", backup_dir=tmp_path / "backups")
    try:
        def add(kind: str, hhmmss: str, step: str | None = None) -> int:
            payload = {"phase": "EOD", "step": step} if step else {"phase": "TRADING"}
            return s.append_event(kind, wall=ist_datetime(DAY, hhmmss), mono=1.0, session=DAY, payload=payload)

        with s.tx():
            stale = [add("PHASE", "15:10:00", "FLATTEN"), add("PHASE", "15:40:00", "DONE")]   # a rehearsal
            add("BOOT", "08:50:00")                                     # the real day's start
            add("PHASE", "09:15:30")                                    # no step: not an EOD row
            real = [add("PHASE", "15:10:01", "FLATTEN")]

        def seqs(now: str) -> tuple[list[int], list[int]]:
            kept, ignored = eod_phase_rows(s, DAY, ist_datetime(DAY, now))
            return [e["seq"] for e in kept], [e["seq"] for e in ignored]

        assert seqs("15:20:00") == (real, stale)
        assert seqs("15:05:00") == ([], stale + real)                  # stamped later than now: not yet written
        with s.tx():
            real.append(add("PHASE", "15:40:01", "DONE"))
            add("BOOT", "15:42:00")                                     # a restart after the day's own DONE
        assert seqs("15:43:00") == (real, stale)
        assert eod_phase_rows(s, date(2026, 10, 15), ist_datetime(DAY, "15:43:00")) == ([], [])
    finally:
        s.close()
