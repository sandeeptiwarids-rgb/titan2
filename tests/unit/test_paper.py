"""U08: broker/paper.py -- PaperExchange, fill_from_depth, PaperSim (spec §6; DETAIL §6 and §4.4)."""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import math
import queue
import random
import re
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from titan2 import charges
from titan2.broker.kite import BENIGN_CANCEL_RE
from titan2.broker.lanes import lane_for
from titan2.broker.paper import (
    MSG_BAND, MSG_CLOSED, MSG_DUPLICATE, MSG_EXPIRED, MSG_MISSED_SQOFF, MSG_NO_CONTRACT_NOTE, MSG_NO_DEPTH,
    MSG_UNKNOWN, SQOFF_TAG, PaperExchange, PaperSim, PaperStoreError, fill_from_depth, latency_seed,
    read_latency_seed,
)
from titan2.broker.port import BENIGN, LOCAL, OK, REJECTED, ReadFailed, format_error
from titan2.clock import FakeClock, SessionCalendar, iso, ist_datetime, parse_wall
from titan2.core.events import (
    ContractNote, ContractNoteOrder, ContractNoteResult, FetchMargins, FetchOrders, FetchPositions, FetchQuotes,
    LookupResult, LookupTag, MarginsResult, OrderSnapshot, OrdersSnapshot, Place, PlaceResult, PollOrder,
    PositionsSnapshot, Priority,
)
from titan2.core.exits import BAND_REJECT_RE
from titan2.core.recon import is_broker_squareoff
from titan2.market.quotes import QuoteCache
from titan2.model import Account, Instrument, InvariantViolation, Level, OrderRequest, Quote, Role, Side, make_tag
from titan2.settings import DEFAULTS, PAPER_LATENCY_CLIP_MS

CAL = SessionCalendar.load()
DAY = date(2026, 10, 12)          # Monday
PREV = date(2026, 10, 9)          # the Friday before
SYM, TS, TOK = "NFO:RELIANCE26OCT2900CE", "RELIANCE26OCT2900CE", 13001218
LOT, TICK = 50, 0.05
INST = Instrument(SYM, TOK, TS, "NFO", "RELIANCE", "NSE:RELIANCE", "CE", 2900.0, date(2026, 10, 27), LOT, TICK)
PAPER, SHADOW = Account.PAPER, Account.SHADOW
BOOK = {"bids": [(9.95, 1000)], "asks": [(10.0, 1000)], "ltp": 10.0}

_RIGS: list = []


@pytest.fixture(autouse=True)
def _close_rigs():
    yield
    while _RIGS:
        _RIGS.pop().close()


class Rig:
    """A PaperExchange on a FakeClock with a scripted book that run() refreshes every step like a live feed."""

    def __init__(self, db, start: str | datetime = f"{DAY}T10:00:00+05:30", settings=DEFAULTS, seq_start: int = 1,
                 thread_check: bool = False, instruments=None, margin_model=None) -> None:
        self.db = db
        self.clock = FakeClock(start)
        self.cache = QuoteCache(mono=self.clock.mono)
        self.asked: list[FetchQuotes] = []
        self.book: tuple | None = None
        self.seq = itertools.count(seq_start)
        self.px = PaperExchange(db, settings=settings, clock=self.clock, quotes=self.cache, calendar=CAL,
                                instruments=instruments if instruments is not None else {SYM: INST}.get,
                                request_depth=self.asked.append, margin_model=margin_model,
                                thread_check=thread_check)
        _RIGS.append(self)

    def close(self) -> None:
        if not self.px._closed:
            self.px.close()

    def quote(self, bids=(), asks=(), ltp=None, exch_wall=None) -> Quote:
        return Quote(SYM, TOK, tuple(Level(p, q) for p, q in bids), tuple(Level(p, q) for p, q in asks), ltp, None,
                     None, exch_wall, self.clock.mono(), self.clock.now_wall(), "WS")

    def set_book(self, bids=(), asks=(), ltp=None, exch_wall=None) -> None:
        self.book = (tuple(bids), tuple(asks), ltp, exch_wall)
        self.feed()

    def feed(self) -> None:
        if self.book is not None:
            self.cache.put(self.quote(*self.book))

    def req(self, side=Side.BUY, qty=LOT, limit=10.10, account=PAPER, sym=SYM, lot=LOT, tick=TICK,
            book=None) -> OrderRequest:
        role = Role.ENTRY if side is Side.BUY else Role.EXIT
        tag = make_tag(role, account, self.clock.today(), next(self.seq), 1)
        return OrderRequest(account, tag, role, sym, sym.split(":", 1)[1], "NFO", TOK, side, qty, limit, lot, tick,
                            book)

    def place(self, **kw) -> PlaceResult:
        return self.px.place(self.req(**kw))

    def run(self, seconds: float, dt: float = 0.05) -> list:
        events = []
        for _ in range(round(seconds / dt)):
            self.clock.advance(dt)
            self.feed()
            events += self.px.step()
        return events

    def row(self, oid: str) -> dict:
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            c.row_factory = sqlite3.Row
            return dict(c.execute("SELECT * FROM p_orders WHERE broker_order_id = ?", (oid,)).fetchone())

    def fills(self, oid: str) -> list[tuple[float, int, int]]:
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            return [tuple(r) for r in c.execute("SELECT px, qty, level_idx FROM p_fills WHERE broker_order_id = ? "
                                                "ORDER BY trade_id", (oid,))]


def terminal(events, oid: str) -> OrderSnapshot:
    snaps = [e for e in events if isinstance(e, OrderSnapshot) and e.broker_order_id == oid
             and e.status != "WORKING"]
    assert len(snaps) == 1, snaps
    return snaps[0]


# ---------------------------------------------------------------------------------------------------------------
# fill_from_depth
# ---------------------------------------------------------------------------------------------------------------
def test_walk_participation_half(tmp_path):
    asks = [(10.05, 300), (10.0, 100)]                                  # unsorted on purpose: best first
    assert fill_from_depth(asks, Side.BUY, 150, 10.10, LOT, 0.5, 0, TICK, draw=0.0) == [(10.0, 50), (10.05, 100)]
    assert asks == [(10.05, 300), (10.0, 100)]                         # pure: input untouched
    # a level too thin to complete a lot at 0.5 (0.6 of one) fills that lot when the order's draw is under 0.6
    #   (paper model 2); otherwise its share carries to the next level
    thin = [(10.0, 60), (10.05, 200)]
    assert fill_from_depth(thin, Side.BUY, 100, 10.10, LOT, 0.5, 0, TICK, draw=0.6) == [(10.05, 100)]
    assert fill_from_depth(thin, Side.BUY, 100, 10.10, LOT, 0.5, 0, TICK, draw=0.59) == [(10.0, 50), (10.05, 50)]
    # SELL walks the bids from the highest down
    assert fill_from_depth([Level(9.90, 200), Level(9.95, 100)], Side.SELL, 150, 9.85, LOT, 0.5, 0, TICK,
                           draw=0.0) == [(9.95, 50), (9.9, 100)]
    # participation 1.0 takes whole levels; not enough depth leaves the rest unfilled
    assert fill_from_depth([(10.0, 100), (10.05, 100)], Side.BUY, 300, 10.05, LOT, 1.0, 0, TICK, draw=0.0) == [
        (10.0, 100), (10.05, 100)]
    assert fill_from_depth([], Side.BUY, 50, 10.0, LOT, 0.5, 0, TICK, draw=0.0) == []

    # through the exchange: a BUY is the ask walk at 0.5 on the fill-time book, never the LTP
    rig = Rig(tmp_path / "p.db")
    rig.set_book(bids=[(9.95, 500)], asks=[(10.0, 100), (10.05, 100)], ltp=10.5)
    res = rig.place(qty=100, limit=10.10)
    assert res.outcome == OK and res.broker_order_id.startswith("P261012")
    snap = terminal(rig.run(3.1), res.broker_order_id)
    assert (snap.status, snap.filled_qty, snap.source) == ("COMPLETE", 100, "SIM")
    assert snap.avg_px == pytest.approx(10.025)
    assert [(t.px, t.qty) for t in rig.px.trades_today(PAPER)] == [(10.0, 50), (10.05, 50)]
    assert rig.fills(res.broker_order_id) == [(10.0, 50, 0), (10.05, 50, 1)]          # one p_fills row per level


def test_adverse_ticks_never_beyond_limit():
    # adverse ticks move the price against us, but a LIMIT order never fills beyond its limit
    assert fill_from_depth([(9.90, 100)], Side.BUY, 50, 10.10, LOT, 0.5, 2, TICK, draw=0.0) == [(10.0, 50)]
    assert fill_from_depth([(9.95, 100)], Side.BUY, 50, 10.0, LOT, 0.5, 2, TICK, draw=0.0) == [(10.0, 50)]
    assert fill_from_depth([(10.0, 100)], Side.SELL, 50, 9.95, LOT, 0.5, 2, TICK, draw=0.0) == [(9.95, 50)]
    # an unlimited (square-off) sell never fills below one tick
    assert fill_from_depth([(0.05, 100)], Side.SELL, 50, None, LOT, 1.0, 3, TICK, draw=0.0) == [(0.05, 50)]
    # the order's draw is a number in [0, 1) (paper model 2)
    for bad in (dict(qty=75), dict(participation=0.0), dict(participation=1.5), dict(adverse_ticks=-1),
                dict(tick=0.0), dict(limit=float("nan")), dict(lot=0), dict(draw=1.0), dict(draw=-0.01),
                dict(draw=float("nan")), dict(draw=True)):
        args = dict(levels=[(10.0, 100)], side=Side.BUY, qty=50, limit=10.0, lot=LOT, participation=0.5,
                    adverse_ticks=0, tick=TICK, draw=0.0)
        args.update(bad)
        with pytest.raises(ValueError):
            fill_from_depth(**args)


# ---------------------------------------------------------------------------------------------------------------
# fills at the due time
# ---------------------------------------------------------------------------------------------------------------
def test_fill_time_book_trade_through(tmp_path):
    rig = Rig(tmp_path / "p.db")
    rig.set_book(bids=[(9.95, 500)], asks=[(10.0, 500)], ltp=10.0)       # the decision book: inside the limit
    res = rig.place(qty=50, limit=10.05)
    assert res.outcome == OK
    rig.set_book(bids=[(10.20, 500)], asks=[(10.25, 500)], ltp=10.2)     # moves through the limit during latency
    snap = terminal(rig.run(3.1), res.broker_order_id)
    assert (snap.status, snap.filled_qty, snap.status_msg) == ("CANCELLED", 0, "SIM: IOC unfilled")
    assert rig.px.trades_today(PAPER) == () and rig.px.positions(PAPER) == ()

    # a book that improves during the latency fills at the better, fill-time price
    rig.set_book(bids=[(9.75, 500)], asks=[(9.80, 500)], ltp=9.8)
    res = rig.place(qty=50, limit=9.85)
    rig.set_book(bids=[(9.55, 500)], asks=[(9.60, 500)], ltp=9.6)
    snap = terminal(rig.run(3.1), res.broker_order_id)
    assert (snap.status, snap.filled_qty, snap.avg_px) == ("COMPLETE", 50, 9.60)


def test_ioc_partial(tmp_path):
    rig = Rig(tmp_path / "p.db")
    rig.set_book(bids=[(9.95, 500)], asks=[(10.0, 100), (10.05, 100), (10.20, 5000)], ltp=10.0)
    res = rig.place(qty=200, limit=10.10)
    events = rig.run(3.1)
    working = [e for e in events if isinstance(e, OrderSnapshot) and e.status == "WORKING"]
    assert len(working) == 1 and working[0].tag == res.order_pk
    snap = terminal(events, res.broker_order_id)
    assert (snap.status, snap.filled_qty) == ("CANCELLED", 100)          # IOC: the rest is cancelled
    assert snap.avg_px == pytest.approx(10.025)
    assert snap.status_msg == "SIM: IOC partial fill 100/200"
    assert rig.fills(res.broker_order_id) == [(10.0, 50, 0), (10.05, 50, 1)]   # 10.20 is beyond the limit
    (pos,) = rig.px.positions(PAPER)
    assert (pos.net_qty, pos.buy_qty, pos.sell_qty, pos.product) == (100, 100, 0, "MIS")
    assert pos.buy_value == pytest.approx(1002.5)
    hist = rig.px.order_history(PAPER, res.broker_order_id)
    assert (hist.status, hist.raw_status, hist.filled_qty, hist.placed_by) == ("CANCELLED", "CANCELLED", 100, "API")
    assert [t.qty for t in rig.px.order_trades(PAPER, res.broker_order_id)] == [50, 50]


def test_no_depth_cancels(tmp_path):
    rig = Rig(tmp_path / "p.db")                                         # no book at all
    res = rig.place(qty=50, limit=10.10)
    assert res.outcome == OK                                             # no LTP: no band check
    for _ in range(80):                                                  # up to the due time (<= 3 s)
        rig.clock.advance(0.05)
        rig.px.step()
        if rig.asked:
            break
    (ask,) = rig.asked                                                   # REST depth requested once, PAPER priority
    assert ask.symbols == (SYM,) and ask.priority == int(Priority.PAPER)
    rig.run(1.45)
    assert rig.px.order_history(PAPER, res.broker_order_id).status == "WORKING"
    snap = terminal(rig.run(0.05), res.broker_order_id)                  # 1.5 s after the first look
    assert (snap.status, snap.filled_qty, snap.status_msg) == ("CANCELLED", 0, MSG_NO_DEPTH)
    assert len(rig.asked) == 1

    # REST depth arriving within the wait (lane-reads' quote sink fills the cache) is used
    res = rig.place(qty=50, limit=10.10, account=SHADOW)
    while len(rig.asked) < 2:
        rig.clock.advance(0.05)
        rig.px.step()
    assert rig.asked[1].priority == int(Priority.SHADOW)
    rig.clock.advance(0.5)
    rig.cache.put(rig.quote(bids=[(9.95, 500)], asks=[(10.0, 500)], ltp=10.0))
    snap = terminal(rig.px.step(), res.broker_order_id)
    assert (snap.status, snap.filled_qty, snap.avg_px) == ("COMPLETE", 50, 10.0)


def test_opening_quotes_never_fill(tmp_path):
    rig = Rig(tmp_path / "p.db", start=f"{DAY}T09:15:02+05:30")
    rig.set_book(**BOOK)
    res = rig.place(qty=50, limit=10.05)
    assert res.outcome == OK                                             # the market is open...
    snap = terminal(rig.run(5.0), res.broker_order_id)
    assert (snap.status, snap.status_msg) == ("CANCELLED", MSG_NO_DEPTH)  # ...but quotes before 09:15:30 are stale

    rig.clock.advance_to("09:16:00")                                     # an exchange stamp before 09:15:30 too
    rig.set_book(**BOOK, exch_wall=ist_datetime(DAY, "09:15:10"))
    res = rig.place(qty=50, limit=10.05)
    assert terminal(rig.run(5.0), res.broker_order_id).status_msg == MSG_NO_DEPTH

    rig.set_book(**BOOK, exch_wall=ist_datetime(DAY, "09:16:00"))
    res = rig.place(qty=50, limit=10.05)
    assert terminal(rig.run(3.1), res.broker_order_id).status == "COMPLETE"


# ---------------------------------------------------------------------------------------------------------------
# place() validation, funds, cancel
# ---------------------------------------------------------------------------------------------------------------
def test_place_rejections(tmp_path):
    rig = Rig(tmp_path / "p.db")
    rig.set_book(**BOOK)
    res = rig.px.place(rig.req(account=Account.LIVE))                    # refused before "sending"
    assert (res.outcome, res.error_class, res.broker_order_id) == (REJECTED, LOCAL, None)
    assert res.message.startswith("LOCAL: ")
    res = rig.place(sym="NFO:BANKNIFTY26OCT50000CE")
    assert (res.outcome, res.message, res.error_class) == (REJECTED, MSG_UNKNOWN, "SIM")
    res = rig.place(qty=75, lot=25)                                     # the exchange checks the real lot
    assert (res.outcome, res.message) == (REJECTED, "SIM: qty 75 is not a multiple of lot 50")
    res = rig.place(limit=10.03, tick=0.01)                              # ... and the real tick
    assert (res.outcome, res.message) == (REJECTED, "SIM: limit 10.03 is not a multiple of tick 0.05")
    assert rig.place(limit=12.0).outcome == OK                           # exactly 20 % from the LTP
    res = rig.place(limit=12.05)
    assert (res.outcome, res.message) == (REJECTED, MSG_BAND)
    assert BAND_REJECT_RE.search(res.message)                            # the exit ladder keeps its band
    assert rig.place(side=Side.SELL, qty=50, limit=7.95).message == MSG_BAND
    rejected = [o for o in rig.px.orders_today(PAPER) if o.status == "REJECTED"]   # in the order book, like RMS
    assert len(rejected) == 5 and all(o.raw_status == "REJECTED" and o.filled_qty == 0 for o in rejected)
    assert [o.status_msg for o in rejected][-1] == MSG_BAND
    # no reject text (bar the spec's LPP text) can read as §5.9's CONTRACT_BLOCKED pattern
    blocked = re.compile(r"(?i)(block|not allowed|restricted|ban)")
    assert not [o.status_msg for o in rejected if o.status_msg != MSG_BAND and blocked.search(o.status_msg)]

    dup = rig.req()
    assert rig.px.place(dup).outcome == OK
    res = rig.px.place(dup)
    assert (res.outcome, res.message, res.broker_order_id) == (REJECTED, MSG_DUPLICATE, None)

    for hms in ("09:14:59", "15:40:00"):                                 # NSE F&O 09:15-15:40 (from the calendar)
        late = Rig(tmp_path / f"closed{hms[:2]}.db", start=f"{DAY}T{hms}+05:30")
        late.set_book(**BOOK)
        assert late.place().message == MSG_CLOSED
    sat = Rig(tmp_path / "sat.db", start="2026-10-10T11:00:00+05:30")
    assert sat.place().message == MSG_CLOSED


def test_funds_and_margins(tmp_path):
    rig = Rig(tmp_path / "p.db", settings=replace(DEFAULTS, PAPER_CAPITAL_RS=1200.0))
    rig.set_book(**BOOK)
    rates = charges.rates_for(DAY)
    need = 1010.0 + charges.order_charges(charges.OrderFills(Side.BUY, 100, 1010.0), rates).total
    first = rig.place(qty=100, limit=10.10)
    assert first.outcome == OK
    assert rig.px.margins(PAPER).available_rs == pytest.approx(1200.0 - need, abs=0.01)   # blocked while OPEN
    res = rig.place(qty=50, limit=10.10)
    assert res.outcome == REJECTED and res.message.startswith("SIM: insufficient funds")
    assert rig.place(side=Side.SELL, qty=50, limit=9.95).outcome == OK  # a sell blocks nothing
    rig.run(3.1)
    pnl = charges.net_pnl([charges.OrderFills(Side.BUY, 100, 1000.0), charges.OrderFills(Side.SELL, 50, 497.5)],
                          rates)
    m = rig.px.margins(PAPER)
    assert m.available_rs == charges.round_money(1200.0 + pnl.net)      # capital + sells - buys - charges
    assert m.raw["blocked_rs"] == 0.0 and m.raw["charges"] == pnl.charges.total
    assert rig.px.margins(SHADOW).available_rs == 1200.0                # each account keeps its own book

    class ShortMargin:                                                   # the phase-2 hook: any MarginModel
        def required(self, req, rates):
            return 1e9 if req.side is Side.SELL else 0.0
    strict = Rig(tmp_path / "strict.db", margin_model=ShortMargin())
    strict.set_book(**BOOK)
    assert strict.place(qty=50, limit=10.10).outcome == OK
    assert strict.place(side=Side.SELL, qty=50, limit=9.95).message.startswith("SIM: insufficient funds")


def test_shadow_books_have_their_own_capital(tmp_path):
    """Review F-19: each SHADOW-account book is funded from its own pool (PAPER_CAPITAL_RS + its realized net today -
    its OPEN BUYs), so a SHADOW:x book that used its capital cannot starve PLACEBO:x, or the reverse, and a filled
    SELL returns money only to its own pool. Two PAPER books still share the account's one pool (as on the one LIVE
    account); a SHADOW request without a book is checked against the whole account; margins(SHADOW) stays the
    account-wide view."""
    rig = Rig(tmp_path / "p.db", settings=replace(DEFAULTS, PAPER_CAPITAL_RS=1200.0))
    rig.set_book(**BOOK)
    full = dict(qty=100, limit=10.10)                                  # Rs 1,010 + charges: one per pool
    assert rig.place(account=SHADOW, book="SHADOW:x", **full).outcome == OK
    res = rig.place(account=SHADOW, book="SHADOW:x", qty=50, limit=10.10)
    assert res.outcome == REJECTED and res.message.startswith("SIM: insufficient funds")
    assert rig.place(account=SHADOW, book="PLACEBO:x", **full).outcome == OK        # its own pool
    res = rig.place(account=SHADOW, book="PLACEBO:x", qty=50, limit=10.10)
    assert res.outcome == REJECTED and res.message.startswith("SIM: insufficient funds")
    res = rig.place(account=SHADOW, qty=50, limit=10.10)               # no book: the whole account (fails closed)
    assert res.outcome == REJECTED and res.message.startswith("SIM: insufficient funds")
    rig.run(3.1)                                                       # both BUYs fill at 10.00
    assert [p.net_qty for p in rig.px.positions(SHADOW)] == [200]
    assert rig.place(side=Side.SELL, account=SHADOW, book="SHADOW:x", qty=100, limit=9.95).outcome == OK
    rig.run(3.1)                                                       # SHADOW:x sells its lot at 9.95 ...
    assert rig.place(account=SHADOW, book="SHADOW:x", **full).outcome == OK         # ... its pool is back
    res = rig.place(account=SHADOW, book="PLACEBO:x", qty=50, limit=10.10)          # PLACEBO:x's is not
    assert res.outcome == REJECTED and res.message.startswith("SIM: insufficient funds")
    rates = charges.rates_for(DAY)
    fills = [charges.OrderFills(Side.BUY, 100, 1000.0), charges.OrderFills(Side.BUY, 100, 1000.0),
             charges.OrderFills(Side.SELL, 100, 995.0)]
    m = rig.px.margins(SHADOW)                                         # the account-wide view
    assert m.raw["realized_net"] == charges.net_pnl(fills, rates).net and m.raw["blocked_rs"] > 1010.0
    # the PAPER account keeps one pool: a second PAPER book cannot spend what the first one holds
    assert rig.place(book="PAPER:a", **full).outcome == OK
    res = rig.place(book="PAPER:b", qty=50, limit=10.10)
    assert res.outcome == REJECTED and res.message.startswith("SIM: insufficient funds")


def test_paper_db_v1_gains_the_book_column(tmp_path):
    """Schema v2 (F-19): a paper_exchange.db made with the v1 DDL opens, gains p_orders.book (NULL on its old rows) and
    user_version 2 in one transaction, with its rows and funds intact, and takes new orders with their book. Without
    the upgrade every paper INSERT would fail, exits included. A file newer than v2 is refused."""
    import titan2.broker.paper as paper_mod
    v1 = paper_mod._DDL.replace(", book TEXT);", ");", 1)
    assert v1 != paper_mod._DDL and paper_mod.SCHEMA_VERSION == 2
    db = tmp_path / "v1.db"
    ts = iso(ist_datetime(DAY, "09:40:00"))
    tag = make_tag(Role.ENTRY, PAPER, DAY, 1, 1)
    with contextlib.closing(sqlite3.connect(db, isolation_level=None)) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(f"BEGIN;\n{v1}\nPRAGMA user_version=1;\nCOMMIT;")
        c.execute("INSERT INTO p_orders(broker_order_id, account, tag, symbol, token, side, product, order_type, "
                  "validity, qty, lot_size, tick, limit_px, placed_ts, due_ts, status, filled_qty, avg_px, "
                  "status_msg, terminal_ts, placed_by) VALUES ('P261012000000001', 'PAPER', ?, ?, ?, 'BUY', 'MIS', "
                  "'LIMIT', 'IOC', 50, 50, 0.05, 10.1, ?, ?, 'COMPLETE', 50, 10.0, NULL, ?, 'API')",
                  (tag, SYM, TOK, ts, ts, ts))
        c.execute("INSERT INTO p_fills VALUES ('P261012000000001-00', 'P261012000000001', 'PAPER', ?, 'BUY', 50, "
                  "10.0, ?, 0)", (SYM, ts))
        c.execute("INSERT INTO p_meta VALUES ('order_seq:261012', '1')")
    rig = Rig(db, seq_start=2)
    rig.set_book(**BOOK)
    (pos,) = rig.px.positions(PAPER)                                   # the old rows are intact
    assert (pos.net_qty, pos.buy_value) == (50, 500.0)
    rates = charges.rates_for(DAY)
    assert rig.px.margins(PAPER).raw["realized_net"] == charges.net_pnl(
        [charges.OrderFills(Side.BUY, 50, 500.0)], rates).net
    with contextlib.closing(sqlite3.connect(db)) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 2
        assert "book" in [r[1] for r in c.execute("PRAGMA table_info(p_orders)")]
        assert c.execute("SELECT book FROM p_orders WHERE broker_order_id = 'P261012000000001'").fetchone() == (None,)
    res = rig.place(book="PAPER:x", qty=50, limit=10.10)               # a new order, with its book
    assert res.outcome == OK and rig.row(res.broker_order_id)["book"] == "PAPER:x"
    rig.close()
    with contextlib.closing(sqlite3.connect(db)) as c:
        c.execute("PRAGMA user_version=3")
        c.commit()
    newer = PaperExchange(db, settings=DEFAULTS, clock=FakeClock(f"{DAY}T10:00:00+05:30"), quotes=QuoteCache(),
                          calendar=CAL, thread_check=False)
    with pytest.raises(PaperStoreError, match="newer"):
        newer.start()


def test_cancel(tmp_path):
    rig = Rig(tmp_path / "p.db")
    res = rig.place(qty=50, limit=10.10)
    rig.px.take_events()
    out = rig.px.cancel(PAPER, res.order_pk, res.broker_order_id)
    assert (out.outcome, out.message) == (OK, None)
    snap = terminal(rig.px.take_events(), res.broker_order_id)
    assert (snap.status, snap.filled_qty) == ("CANCELLED", 0)
    again = rig.px.cancel(PAPER, res.order_pk, res.broker_order_id)
    assert again.outcome == BENIGN and BENIGN_CANCEL_RE.search(again.message)
    missing = rig.px.cancel(PAPER, "T2EP26101200901", "P261012999999999")
    assert missing.outcome == BENIGN and BENIGN_CANCEL_RE.search(missing.message)
    assert rig.px.cancel(PAPER, res.order_pk, None).outcome == REJECTED
    assert rig.px.cancel(Account.LIVE, res.order_pk, res.broker_order_id).outcome == REJECTED
    rig.set_book(**BOOK)
    rig.run(3.1)
    assert rig.px.trades_today(PAPER) == ()                              # a cancelled order never fills


# ---------------------------------------------------------------------------------------------------------------
# startup and square-off
# ---------------------------------------------------------------------------------------------------------------
def test_restart_settlement(tmp_path):
    db = tmp_path / "paper_exchange.db"
    fri = Rig(db, start=f"{PREV}T14:00:00+05:30")                       # Friday: a position, then the host dies
    fri.set_book(**BOOK)
    held = fri.place(qty=100, limit=10.05)
    fri.run(3.1)
    assert fri.px.positions(PAPER)[0].net_qty == 100
    stuck = fri.place(qty=50, limit=10.05)                               # OPEN when the process dies
    fri.close()

    mon = Rig(db, start=f"{DAY}T09:05:00+05:30", seq_start=10)
    events = mon.px.step()
    expired = mon.row(stuck.broker_order_id)
    assert (expired["status"], expired["filled_qty"], expired["status_msg"]) == ("CANCELLED", 0, MSG_EXPIRED)
    assert terminal(events, stuck.broker_order_id).status_msg == MSG_EXPIRED
    assert mon.px.order_history(PAPER, held.broker_order_id) is None     # earlier days are not served
    (sq,) = mon.px.orders_today(PAPER)                                   # the missed square-off, at Rs 0.05
    assert (sq.tag, sq.placed_by, sq.side, sq.qty, sq.filled_qty, sq.status) == (
        SQOFF_TAG, "EXCHANGE", Side.SELL, 100, 100, "COMPLETE")
    assert (sq.avg_px, sq.status_msg, sq.product) == (0.05, MSG_MISSED_SQOFF, "MIS")
    assert not any(isinstance(e, OrderSnapshot) and e.broker_order_id == sq.broker_order_id for e in events)
    assert is_broker_squareoff(sq, ist_datetime(PREV, DEFAULTS.BROKER_SQUAREOFF_AT), frozenset({"API"}))
    (pos,) = mon.px.positions(PAPER)
    assert (pos.net_qty, pos.buy_qty, pos.sell_qty, pos.sell_value) == (0, 0, 100, 5.0)
    pnl = charges.net_pnl([charges.OrderFills(Side.SELL, 100, 5.0, charges.AUTO_SQUAREOFF)], charges.rates_for(DAY))
    assert pnl.charges.sqoff_fee == 50.0                                 # Rs 50 + GST, like Zerodha's
    assert mon.px.margins(PAPER).available_rs == charges.round_money(DEFAULTS.PAPER_CAPITAL_RS + pnl.net)
    assert mon.px.positions(SHADOW) == () and mon.px.orders_today(SHADOW) == ()
    mon.set_book(**BOOK)
    mon.clock.advance_to("09:30:00")
    mon.set_book(**BOOK)
    quick = mon.place(qty=50, limit=10.05)                               # OPEN at a quick restart: re-armed
    mon.close()

    again = Rig(db, start=mon.clock.now_wall() + timedelta(milliseconds=20), seq_start=20)
    assert len([o for o in again.px.orders_today(PAPER) if o.placed_by == "EXCHANGE"]) == 1   # never twice
    again.set_book(**BOOK)
    snap = terminal(again.run(3.1), quick.broker_order_id)
    assert (snap.status, snap.filled_qty) == ("COMPLETE", 50)


def test_restart_settles_short_conservatively(tmp_path):
    db = tmp_path / "p.db"
    fri = Rig(db, start=f"{PREV}T14:00:00+05:30")
    fri.set_book(**BOOK)
    fri.place(side=Side.SELL, qty=50, limit=9.95, account=SHADOW)        # a sell beyond the position is accepted
    fri.run(3.1)
    assert fri.px.positions(SHADOW)[0].net_qty == -50
    fri.close()
    mon = Rig(db, start=f"{DAY}T09:05:00+05:30", seq_start=10)
    (sq,) = mon.px.orders_today(SHADOW)                                  # bought back at 1.05 x the highest price
    assert (sq.side, sq.qty, sq.avg_px) == (Side.BUY, 50, 10.45)
    assert mon.px.positions(SHADOW)[0].net_qty == 0


def test_a_start_on_a_day_without_a_session_settles_nothing(tmp_path):
    """Review round 2 (A1): a weekend or weekday-holiday start settled an earlier session's position, dated that day,
    where no trading day's orders show it for recon to book. Only a trading day's start settles it, dated that day."""
    db = tmp_path / "p.db"
    fri = Rig(db, start=f"{PREV}T14:00:00+05:30")
    fri.set_book(**BOOK)
    fri.place(qty=100, limit=10.05)
    fri.run(3.1)
    fri.close()
    sat = Rig(db, start=f"{date(2026, 10, 10)}T09:05:00+05:30", seq_start=10)     # a Saturday: no session
    sat.px.step()
    assert sat.px.orders_today(PAPER) == () and sat.px.positions(PAPER)[0].net_qty == 100
    sat.close()
    mon = Rig(db, start=f"{DAY}T09:05:00+05:30", seq_start=20)
    (sq,) = mon.px.orders_today(PAPER)
    assert (sq.status_msg, sq.order_wall.date(), sq.avg_px) == (MSG_MISSED_SQOFF, DAY, 0.05)


def test_the_square_off_record_of_earlier_days(tmp_path):
    """FetchSquareoffs (review round 2, the leftover backstop): the exchange's own square-off orders on the symbols
    from `since` up to yesterday, and each symbol's net at the start of today; Kite serves none of this, so the
    read is paper-only (lane_for routes it to paper-sim for PAPER and SHADOW, nowhere for LIVE)."""
    db = tmp_path / "p.db"
    fri = Rig(db, start=f"{PREV}T15:20:00+05:30")
    fri.set_book(bids=[(10.0, 500)], asks=[(10.10, 1000)], ltp=10.0)
    fri.place(qty=100, limit=10.15)
    fri.run(3.1)
    fri.clock.advance_to("15:26:00")
    fri.run(0.5)                                                         # the 15:26 square-off of Friday
    (sq,) = [o for o in fri.px.orders_today(PAPER) if o.placed_by == "EXCHANGE"]
    fri.close()
    mon = Rig(db, start=f"{DAY}T09:05:00+05:30", seq_start=10)
    orders, carried = mon.px.squareoffs(PAPER, (SYM, "NFO:OTHER26OCT100CE"), PREV)
    assert [o.broker_order_id for o in orders] == [sq.broker_order_id] and orders[0].avg_px == sq.avg_px
    assert carried == {SYM: 0, "NFO:OTHER26OCT100CE": 0}
    assert mon.px.squareoffs(PAPER, (SYM,), DAY) == ((), {SYM: 0})       # nothing of today or later
    assert mon.px.squareoffs(SHADOW, (SYM,), PREV) == ((), {SYM: 0})
    from titan2.broker.lanes import failure_events
    from titan2.core.events import FetchSquareoffs, SquareoffsResult
    ask = FetchSquareoffs(PAPER, "sqoff:e1:1", (SYM,), PREV)
    assert lane_for(ask) == "paper" and lane_for(replace(ask, account=SHADOW)) == "paper"
    assert lane_for(replace(ask, account=Account.LIVE)) is None
    assert failure_events(ask, "x", 1.0) == [SquareoffsResult(PAPER, "sqoff:e1:1", False)]
    seen: list = []
    sim = PaperSim(mon.px, seen.append)
    sim.submit(ask)
    sim.drain()
    (res,) = [e for e in seen if isinstance(e, SquareoffsResult)]
    assert res.ok and [o.broker_order_id for o in res.orders] == [sq.broker_order_id] and res.carried == ((SYM, 0),)


def test_squareoff_1526(tmp_path):
    db = tmp_path / "p.db"
    rig = Rig(db, start=f"{DAY}T15:20:00+05:30")
    rig.set_book(bids=[(10.0, 50), (9.5, 50)], asks=[(10.10, 1000)], ltp=10.0)
    paper = rig.place(qty=150, limit=10.15)
    shadow = rig.place(qty=50, limit=10.15, account=SHADOW)
    rig.run(3.1)
    assert [p.net_qty for p in rig.px.positions(PAPER) + rig.px.positions(SHADOW)] == [150, 50]
    assert {paper.outcome, shadow.outcome} == {OK}

    rig.clock.advance_to("15:25:59.950")
    rig.feed()
    rig.px.step()
    assert all(o.placed_by == "API" for o in rig.px.orders_today(PAPER))    # not before BROKER_SQUAREOFF_AT

    rig.clock.advance(0.05)                                               # 15:26:00
    rig.feed()
    events = rig.px.step()
    (sq,) = [o for o in rig.px.orders_today(PAPER) if o.placed_by == "EXCHANGE"]
    assert (sq.tag, sq.side, sq.qty, sq.status, sq.order_type) == (SQOFF_TAG, Side.SELL, 150, "COMPLETE", "SQOFF")
    # every shown bid at participation 1.0, the rest at the deepest bid x 0.95 (floored to the tick)
    assert rig.fills(sq.broker_order_id) == [(10.0, 50, 0), (9.5, 50, 1), (9.0, 50, 2)]
    assert sq.avg_px == pytest.approx(9.5)
    assert not [e for e in events if isinstance(e, OrderSnapshot)]       # recon finds it, as on Kite
    assert is_broker_squareoff(sq, ist_datetime(DAY, DEFAULTS.BROKER_SQUAREOFF_AT), frozenset({"API"}))
    (sq_shadow,) = [o for o in rig.px.orders_today(SHADOW) if o.placed_by == "EXCHANGE"]
    assert (sq_shadow.qty, sq_shadow.avg_px) == (50, 10.0)
    assert [p.net_qty for p in rig.px.positions(PAPER) + rig.px.positions(SHADOW)] == [0, 0]

    rates = charges.rates_for(DAY)
    pnl = charges.net_pnl([charges.OrderFills(Side.BUY, 150, 1515.0),
                           charges.OrderFills(Side.SELL, 150, 1425.0, charges.AUTO_SQUAREOFF)], rates)
    assert rig.px.margins(PAPER).available_rs == charges.round_money(DEFAULTS.PAPER_CAPITAL_RS + pnl.net)

    rig.run(60.0)                                                         # once per session...
    rig.close()
    later = Rig(db, start=f"{DAY}T15:28:00+05:30", seq_start=20)         # ...also across a restart
    later.set_book(bids=[(10.0, 500)], asks=[(10.10, 500)], ltp=10.0)
    later.run(1.0)
    assert len([o for o in later.px.orders_today(PAPER) if o.placed_by == "EXCHANGE"]) == 1


def test_squareoff_without_bids_is_conservative(tmp_path):
    rig = Rig(tmp_path / "p.db", start=f"{DAY}T15:20:00+05:30")
    rig.set_book(**BOOK)
    rig.place(qty=100, limit=10.05)
    rig.run(3.1)
    rig.clock.advance_to("15:26:00")
    rig.set_book(asks=[(10.0, 1000)], ltp=10.0)                           # a fresh book with no bids at all
    rig.px.step()
    (sq,) = [o for o in rig.px.orders_today(PAPER) if o.placed_by == "EXCHANGE"]
    assert (sq.qty, sq.avg_px) == (100, 0.05)

    rig2 = Rig(tmp_path / "p2.db", start=f"{DAY}T15:20:00+05:30")
    rig2.set_book(**BOOK)
    rig2.place(qty=100, limit=10.05)
    rig2.run(3.1)
    rig2.book = None                                                      # feed and REST both silent at 15:26
    rig2.clock.advance_to("15:26:00")
    rig2.px.step()
    assert rig2.asked and rig2.asked[-1].symbols == (SYM,)
    rig2.run(1.45)
    assert not [o for o in rig2.px.orders_today(PAPER) if o.placed_by == "EXCHANGE"]   # waits up to 1.5 s
    rig2.run(0.1)
    (sq,) = [o for o in rig2.px.orders_today(PAPER) if o.placed_by == "EXCHANGE"]
    assert (sq.qty, sq.avg_px) == (100, 0.05)


def test_squareoff_waits_out_a_crossed_book(tmp_path):
    """Paper model 2: a crossed or locked book is no book for the 15:26 square-off either (its bid left above the
    ask would sell too well). It waits for depth as for a missing book, then walks the next usable one."""
    rig = Rig(tmp_path / "p.db", start=f"{DAY}T15:20:00+05:30")
    rig.set_book(**BOOK)
    rig.place(qty=100, limit=10.05)
    rig.run(3.1)
    rig.clock.advance_to("15:26:00")
    asked = len(rig.asked)
    rig.set_book(bids=[(10.50, 1000)], asks=[(10.0, 1000)], ltp=10.0)       # crossed: a stale bid over the ask
    rig.px.step()
    assert len(rig.asked) == asked + 1 and rig.asked[-1].symbols == (SYM,)  # REST depth asked, as for no book
    assert not [o for o in rig.px.orders_today(PAPER) if o.placed_by == "EXCHANGE"]
    rig.clock.advance(0.2)
    rig.set_book(bids=[(9.90, 1000)], asks=[(9.95, 1000)], ltp=9.9)         # the REST answer: a real book
    rig.px.step()
    (sq,) = [o for o in rig.px.orders_today(PAPER) if o.placed_by == "EXCHANGE"]
    assert (sq.qty, sq.avg_px) == (100, 9.90)


@pytest.mark.parametrize("side, bids, asks, px", [
    (Side.BUY, [(10.50, 1000)], [(10.0, 1000)], 10.0),       # a long, crossed: sold at the ask, never above it
    (Side.BUY, [(10.0, 1000)], [(10.0, 1000)], 10.0),        # a long, locked: at that price
    (Side.SELL, [(10.50, 1000)], [(10.0, 1000)], 10.50),     # a short, crossed: bought back at the bid, never under
])
def test_squareoff_on_a_book_that_stays_crossed_takes_its_worse_side(tmp_path, side, bids, asks, px):
    """Review of paper model 2 (probe test_probe_sqoff_crossed): a book that stayed crossed for the whole 1.5 s wait,
    REST answer included, counted as no book, so a long was sold at the no-book Rs 0.05, a near-total loss (bc2d740
    sold at the 10.50 bid; LIVE sells into the real book, near Rs 10). The square-off now fills on that book at its
    worse side: a sell takes no bid above the best ask, a buy no ask under the best bid (min / max of bid and ask)."""
    rig = Rig(tmp_path / "p.db", start=f"{DAY}T15:20:00+05:30")
    rig.set_book(**BOOK)
    rig.place(side=side, qty=100, limit=10.05 if side is Side.BUY else 9.90)  # a long, or a short (a SELL beyond it)
    rig.run(3.1)
    assert rig.px.positions(PAPER)[0].net_qty == (100 if side is Side.BUY else -100)
    rig.clock.advance_to("15:26:00")
    rig.set_book(bids=bids, asks=asks, ltp=10.0)              # a stale side, on the feed and in REST alike
    rig.px.step()
    rig.run(1.45)
    assert not [o for o in rig.px.orders_today(PAPER) if o.placed_by == "EXCHANGE"]   # it still waits 1.5 s first
    rig.run(0.1)
    (sq,) = [o for o in rig.px.orders_today(PAPER) if o.placed_by == "EXCHANGE"]
    assert (sq.side, sq.qty, sq.status, sq.avg_px) == (Side.SELL if side is Side.BUY else Side.BUY, 100, "COMPLETE",
                                                      px)
    assert rig.px.positions(PAPER)[0].net_qty == 0


# ---------------------------------------------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------------------------------------------
def _latencies_ms(rig: Rig, n: int, account=PAPER) -> list[float]:
    out = []
    for _ in range(n):
        res = rig.place(qty=50, limit=10.10, account=account)
        assert res.outcome == OK
        row = rig.row(res.broker_order_id)
        out.append((parse_wall(row["due_ts"]) - parse_wall(row["placed_ts"])).total_seconds() * 1000.0)
    return out


def test_deterministic_seed(tmp_path):
    a = Rig(tmp_path / "a.db")
    b = Rig(tmp_path / "b.db")
    la = _latencies_ms(a, 4)
    lb = _latencies_ms(b, 2)
    b.close()
    b2 = Rig(tmp_path / "b.db", seq_start=3)                              # a restart continues the sequence
    lb += _latencies_ms(b2, 2)
    assert la == lb

    assert latency_seed(DAY, "PAPER") == int(hashlib.sha256(b"2026-10-12|PAPER").hexdigest(), 16)
    rng = random.Random(latency_seed(DAY, PAPER))
    lo, hi = PAPER_LATENCY_CLIP_MS
    want = [min(max(rng.lognormvariate(math.log(250.0), 0.6), lo), hi) for _ in range(4)]
    assert all(abs(got - w) < 1.0 for got, w in zip(la, want))          # due_ts keeps milliseconds
    assert all(lo <= x <= hi for x in la)

    assert _latencies_ms(a, 4, account=SHADOW) != la                     # each account has its own sequence
    with contextlib.closing(sqlite3.connect(tmp_path / "a.db")) as c:
        meta = dict(c.execute("SELECT key, value FROM p_meta"))
    assert meta["rng_seed:PAPER:2026-10-12"] == str(latency_seed(DAY, PAPER))
    assert meta["rng_draws:PAPER:2026-10-12"] == "4"


# ---------------------------------------------------------------------------------------------------------------
# reads, contract note, PaperSim, ownership, storage
# ---------------------------------------------------------------------------------------------------------------
def test_reads_and_contract_note(tmp_path):
    rig = Rig(tmp_path / "p.db")
    rig.set_book(bids=[(10.0, 1000)], asks=[(10.05, 1000)], ltp=10.0)
    buy = rig.place(qty=100, limit=10.10)
    rig.run(3.1)
    sell = rig.place(side=Side.SELL, qty=50, limit=9.95)
    rig.run(3.1)
    (pos,) = rig.px.positions(PAPER)
    assert (pos.symbol, pos.tradingsymbol, pos.exchange) == (SYM, TS, "NFO")
    assert (pos.net_qty, pos.buy_qty, pos.sell_qty, pos.buy_value, pos.sell_value) == (50, 100, 50, 1005.0, 500.0)
    assert [o.broker_order_id for o in rig.px.orders_today(PAPER)] == [buy.broker_order_id, sell.broker_order_id]
    notes = [ContractNoteOrder(buy.broker_order_id, "NFO", TS, Side.BUY, 100, 10.05),
             ContractNoteOrder(sell.broker_order_id, "NFO", TS, Side.SELL, 50, 10.0)]
    for account, orders in ((PAPER, notes), (PAPER, []), (SHADOW, notes)):   # no contract note, however asked
        with pytest.raises(ReadFailed) as err:
            rig.px.contract_note(account, orders)
        assert err.value.text == format_error(REJECTED, "SIM", MSG_NO_CONTRACT_NOTE)
    with pytest.raises(ReadFailed, match="^REJECTED:LOCAL: contract note: PaperExchange serves PAPER and SHADOW"):
        rig.px.contract_note(Account.LIVE, notes)
    for read in (rig.px.positions, rig.px.orders_today, rig.px.trades_today, rig.px.margins):
        with pytest.raises(ReadFailed):
            read(Account.LIVE)
    assert rig.px.positions(SHADOW) == () and rig.px.order_history(SHADOW, buy.broker_order_id) is None


def test_contract_note_cannot_vouch_for_a_wrong_rateset(tmp_path, monkeypatch):
    """Review M1 (§8.1): the PAPER contract-note sample is there to check the [V] RateSet against Zerodha's own
    calculator before G1 certifies an edge after costs. Billed by the paper exchange through titan2.charges, the
    note matched a RateSet with no STT, transaction charge or stamp to the paisa, so CHARGES_DRIFT could never fire.
    The sample now goes to Kite on lane-reads (§4.2), and the exchange refuses any contract note: a ContractNote
    mis-routed to paper-sim is answered ok=False (VCN_PAPER_UNSUPPORTED), never a pass."""
    rig = Rig(tmp_path / "p.db")
    rig.set_book(**BOOK)
    buy = rig.place(qty=100, limit=10.10)
    rig.run(3.1)
    sell = rig.place(side=Side.SELL, qty=100, limit=9.90)
    rig.run(3.1)
    assert [(o.status, o.filled_qty) for o in rig.px.orders_today(PAPER)] == [("COMPLETE", 100), ("COMPLETE", 100)]
    wrong = tuple(replace(r, stt_sell=0.0, txn=0.0, ipft=0.0, stamp_buy=0.0) for r in charges.RATESETS)
    monkeypatch.setattr(charges, "RATESETS", wrong)
    orders = (ContractNoteOrder(buy.broker_order_id, "NFO", TS, Side.BUY, 100, 10.0),
              ContractNoteOrder(sell.broker_order_id, "NFO", TS, Side.SELL, 100, 9.95))
    posted: list = []
    sim = PaperSim(rig.px, posted.append)
    assert lane_for(ContractNote(PAPER, orders)) == "reads"     # the engine's 15:33 sample goes to Kite's calculator
    sim.submit(ContractNote(PAPER, orders))                     # paper-sim, asked anyway, never prices a note
    sim.drain()
    assert posted == [ContractNoteResult(PAPER, False, ())]
    with pytest.raises(ReadFailed) as err:
        rig.px.contract_note(PAPER, orders)
    assert err.value.outcome == REJECTED and err.value.text == format_error(REJECTED, "SIM", MSG_NO_CONTRACT_NOTE)


def test_paper_sim_inline_answers_like_the_lanes(tmp_path):
    rig = Rig(tmp_path / "p.db")
    rig.set_book(**BOOK)
    posted: list = []
    sim = PaperSim(rig.px, posted.append)
    req = rig.req(qty=50, limit=10.10)
    sim.submit(Place(PAPER, req.tag, req))
    out = sim.drain()
    assert isinstance(out[0], PlaceResult) and out[0].outcome == OK      # the PlaceResult, then WORKING
    assert isinstance(out[1], OrderSnapshot) and (out[1].status, out[1].source) == ("WORKING", "SIM")
    oid = out[0].broker_order_id
    for _ in range(62):
        rig.clock.advance(0.05)
        rig.feed()
        out += sim.drain()
    assert terminal(out, oid).status == "COMPLETE"
    sim.submit(PollOrder(PAPER, req.tag, oid))
    sim.submit(LookupTag(PAPER, req.tag))
    sim.submit(FetchPositions(PAPER, int(Priority.RECON)))
    sim.submit(FetchMargins(PAPER))
    asked = rig.clock.mono()
    got = sim.drain()
    assert isinstance(got[0], OrderSnapshot) and (got[0].source, got[0].status) == ("SIM", "COMPLETE")
    assert isinstance(got[1], LookupResult) and got[1].ok and got[1].found.broker_order_id == oid
    assert isinstance(got[2], MarginsResult) and got[2].ok
    while not any(isinstance(e, PositionsSnapshot) for e in got):   # decision B1: positions pay the read delay
        assert rig.clock.mono() < asked + 3.1
        rig.clock.advance(0.05)
        got += sim.drain()
    pos = got[-1]
    assert isinstance(pos, PositionsSnapshot) and pos.ok and pos.rows[0].net_qty == 50 and pos.req_mono == asked
    assert posted == out + got

    live = rig.req(account=Account.LIVE)
    sim.submit(Place(Account.LIVE, live.tag, live))                     # never queued: refused at once
    assert posted[-1] == PlaceResult(Account.LIVE, live.tag, REJECTED, None, LOCAL, posted[-1].message)
    assert sim.drain() == []


def _read_delays(n: int, account=PAPER, settings=DEFAULTS) -> list[float]:
    """The first n read delays (seconds) of DAY's read sequence for account."""
    rng = random.Random(read_latency_seed(DAY, account))
    lo, hi = PAPER_LATENCY_CLIP_MS
    return [min(max(rng.lognormvariate(math.log(settings.PAPER_READ_LATENCY_MEDIAN_MS),
                                       settings.PAPER_READ_LATENCY_SIGMA), lo), hi) / 1000.0 for _ in range(n)]


def _until_answered(rig: Rig, sim: PaperSim, kinds: tuple, step_s: float = 0.002, limit_s: float = 7.0) -> dict:
    """Step the clock until one event of each kind arrives; {kind: (the event, the drain's clock)}."""
    got: dict = {}
    end = rig.clock.mono() + limit_s
    while len(got) < len(kinds):
        assert rig.clock.mono() < end, f"no answer for {[k.__name__ for k in kinds if k not in got]}"
        rig.clock.advance(step_s)
        rig.feed()
        for e in sim.drain():
            if isinstance(e, kinds) and type(e) not in got:
                got[type(e)] = (e, rig.clock.mono())
    return got


def test_paper_position_and_order_reads_pay_a_seeded_latency(tmp_path):
    """Decision B1: paper-sim answers a positions or orders read only after the seeded read delay, never in the drain
    that received it. The answer is stamped with the receipt (req_mono), arrives at the due time and shows the ledger
    as it stands then; the account's next read waits behind it (FIFO: never overtakes). PollOrder, LookupTag and
    FetchMargins still answer at once. A new PaperExchange on the same DB continues the read sequence, which leaves
    the order latency's draws alone."""
    db = tmp_path / "p.db"
    rig = Rig(db)
    rig.set_book(**BOOK)
    sim = PaperSim(rig.px, lambda ev: None)
    req = rig.req(qty=50, limit=10.10)
    sim.submit(Place(PAPER, req.tag, req))
    placed = sim.drain()
    oid = placed[0].broker_order_id
    rig.clock.advance(0.010)                                              # the order is still working
    asked = rig.clock.mono()
    sim.submit(FetchPositions(PAPER, int(Priority.EXIT)))
    sim.submit(FetchOrders(PAPER, int(Priority.RECON)))
    sim.submit(PollOrder(PAPER, req.tag, oid))
    sim.submit(LookupTag(PAPER, req.tag))
    sim.submit(FetchMargins(PAPER))
    first = sim.drain()
    assert [type(e) for e in first] == [OrderSnapshot, LookupResult, MarginsResult]   # the reads are held
    assert first[0].status == "WORKING"
    d1, d2, d3 = _read_delays(3)
    assert d2 < d1                                                        # the seed lets the 2nd read overtake ...
    got = _until_answered(rig, sim, (PositionsSnapshot, OrdersSnapshot))
    (pos, pos_at), (ords, ords_at) = got[PositionsSnapshot], got[OrdersSnapshot]
    assert pos.ok and pos.req_mono == asked and pos.recv_mono == pos_at
    assert pos_at - asked == pytest.approx(d1, abs=0.0021)               # the first draw of read_latency_seed
    assert ords.ok and ords.req_mono == asked
    assert ords_at - asked == pytest.approx(d1 + d2, abs=0.0021)          # ... but waits behind the 1st (FIFO)
    # read at the due time, stamped with the receipt: the fill (due ~163 ms after the place) shows in both reads
    # though they were asked before it, the conservative side of the post-fill rule (§9.2)
    assert [(p.symbol, p.net_qty) for p in pos.rows] == [(SYM, 50)]
    assert [(o.broker_order_id, o.status) for o in ords.orders] == [(oid, "COMPLETE")]
    rig.close()

    again = Rig(db, start=rig.clock.now_wall(), seq_start=50)            # a restart continues the read sequence
    sim2 = PaperSim(again.px, lambda ev: None)
    asked = again.clock.mono()
    sim2.submit(FetchPositions(PAPER, int(Priority.RECON)))
    assert sim2.drain() == []
    pos, pos_at = _until_answered(again, sim2, (PositionsSnapshot,))[PositionsSnapshot]
    assert pos.req_mono == asked and pos_at - asked == pytest.approx(d3, abs=0.0021)
    again.close()
    with contextlib.closing(sqlite3.connect(db)) as c:
        meta = dict(c.execute("SELECT key, value FROM p_meta"))
    assert meta["read_seed:PAPER:2026-10-12"] == str(read_latency_seed(DAY, PAPER))
    assert meta["read_draws:PAPER:2026-10-12"] == "3"
    assert meta["rng_draws:PAPER:2026-10-12"] == "1"                      # the one place: order draws untouched
    assert read_latency_seed(DAY, PAPER) == int(hashlib.sha256(b"2026-10-12|PAPER|reads").hexdigest(), 16)
    assert read_latency_seed(DAY, SHADOW) != read_latency_seed(DAY, PAPER) != latency_seed(DAY, PAPER)


def test_paper_read_latency_defaults():
    """The read delay at the defaults: median about 250 ms, p95 about 1 s, every draw within PAPER_LATENCY_CLIP_MS."""
    assert (DEFAULTS.PAPER_READ_LATENCY_MEDIAN_MS, DEFAULTS.PAPER_READ_LATENCY_SIGMA) == (250.0, 0.85)
    rig = Rig(":memory:")
    draws = sorted(rig.px.read_latency_ms(PAPER) for _ in range(2000))
    lo, hi = PAPER_LATENCY_CLIP_MS
    assert lo <= draws[0] and draws[-1] <= hi
    median, p95 = draws[1000], draws[1900]
    assert 225.0 <= median <= 275.0, median
    assert 850.0 <= p95 <= 1200.0, p95
    assert draws == pytest.approx(sorted(x * 1000.0 for x in _read_delays(2000)))   # the seeded sequence


def test_shadow_reads_pay_their_own_read_delay(tmp_path):
    """Placebo symmetry (review of the B1 batch): the SHADOW account, where every PLACEBO:<s> book trades, pays the
    paper read delay like the PAPER account of its parents, from its own seeded sequence (read_latency_seed(day,
    SHADOW)) and its own FIFO. Reads of both accounts sent at the same instant are held, and each is answered after
    its own first draw counted from its own receipt, never queued behind the other account's read, so a placebo's exit
    waits for its post-decision read exactly as long as its parent's model says."""
    rig = Rig(tmp_path / "p.db")
    sim = PaperSim(rig.px, lambda ev: None)
    asked = rig.clock.mono()
    sim.submit(FetchPositions(PAPER, int(Priority.EXIT)))               # a parent's exit read ...
    sim.submit(FetchPositions(SHADOW, int(Priority.EXIT)))              # ... and its placebo's, at the same instant
    assert sim.drain() == []                                            # neither is answered at once
    got: dict = {}
    while len(got) < 2:
        assert rig.clock.mono() - asked < 7.0, f"no answer for {[a for a in (PAPER, SHADOW) if a not in got]}"
        rig.clock.advance(0.002)
        for e in sim.drain():
            if isinstance(e, PositionsSnapshot):
                assert e.ok and e.req_mono == asked and Account(e.account) not in got
                got[Account(e.account)] = rig.clock.mono() - asked
    (paper_delay,), (shadow_delay,) = _read_delays(1, PAPER), _read_delays(1, SHADOW)
    assert got[PAPER] == pytest.approx(paper_delay, abs=0.0021)
    assert got[SHADOW] == pytest.approx(shadow_delay, abs=0.0021)       # not paper_delay + shadow_delay (one FIFO)


def test_thread_ownership(tmp_path):
    rig = Rig(tmp_path / "p.db", thread_check=True)
    with pytest.raises(InvariantViolation):
        rig.px.positions(PAPER)                                          # only paper-sim may touch it
    q: queue.Queue = queue.Queue()
    sim = PaperSim(rig.px, q.put)
    sim.start()
    try:
        sim.submit(FetchMargins(PAPER))
        ev = q.get(timeout=5)
        while not isinstance(ev, MarginsResult):
            ev = q.get(timeout=5)
        assert ev.ok and ev.margins.available_rs == DEFAULTS.PAPER_CAPITAL_RS
    finally:
        sim.stop(join_s=5)


def test_storage_is_an_independent_append_only_ledger(tmp_path):
    rig = Rig(tmp_path / "p.db")
    rig.set_book(**BOOK)
    res = rig.place(qty=50, limit=10.05)
    rig.run(3.1)
    with contextlib.closing(sqlite3.connect(tmp_path / "p.db")) as c:
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE p_fills SET px = 1.0")
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("DELETE FROM p_fills")
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE p_orders SET filled_qty = 0 WHERE broker_order_id = ?", (res.broker_order_id,))
    foreign = tmp_path / "foreign.db"
    with contextlib.closing(sqlite3.connect(foreign)) as c:
        c.execute("CREATE TABLE t(x)")
        c.commit()
    px = PaperExchange(foreign, settings=DEFAULTS, clock=FakeClock(), quotes=QuoteCache(), calendar=CAL,
                       thread_check=False)
    with pytest.raises(PaperStoreError):
        px.start()
