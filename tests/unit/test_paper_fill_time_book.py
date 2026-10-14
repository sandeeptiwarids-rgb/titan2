"""PaperExchange fills an order only against a book received AFTER the order was placed (spec §6; DETAIL §6.3).

DETAIL §6.3: the due-time book is "the book at fill time, not at decision time, so slippage from latency is
included". Regression for the review finding "paper fills against the decision-time book": _fill_quote() took any
quote at most PAPER_BOOK_MAX_AGE_S old, so at the ~250 ms due time it was nearly always the very snapshot the order
was decided on (the gateway's REST quote; the option's WS stream starts only at ENTERING). Paper entries filled at
the decision ask and exits at the stale bid, with no latency slippage, and G1 certifies on paper P&L.

Each order here is the first PAPER order of its session, so its seeded latency is the first draw (163.5 ms).

Paper model 2 (review round 2, 2026-10-14): a book received after the placement is still no fill-time book when it
is crossed or locked, or when its exchange time is in an earlier second than the decision quote's (a lagged WS
packet). The order waits for the next packet or REST as for a missing book, and a wait that sees nothing usable
ends CANCELLED under that reason. The decision quote's time comes with the order (OrderRequest.decision_exch_wall,
set by the engine), never from the cache at place() (the review of paper model 2).
"""

from __future__ import annotations

import contextlib
import itertools
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from titan2.broker.paper import MSG_NO_DEPTH, PaperExchange
from titan2.clock import FakeClock, SessionCalendar, parse_wall
from titan2.core.events import FetchQuotes, OrderSnapshot, Priority
from titan2.market.quotes import QuoteCache
from titan2.model import Account, Instrument, Level, OrderRequest, Quote, Role, Side, make_tag
from titan2.settings import DEFAULTS, PAPER_DEPTH_WAIT_S

CAL = SessionCalendar.load()
DAY = date(2026, 10, 12)                   # Monday
SYM, TS, TOK = "NFO:RELIANCE26OCT2900CE", "RELIANCE26OCT2900CE", 13001218
LOT, TICK = 50, 0.05
INST = Instrument(SYM, TOK, TS, "NFO", "RELIANCE", "NSE:RELIANCE", "CE", 2900.0, date(2026, 10, 27), LOT, TICK)
PAPER = Account.PAPER
MAX_AGE = DEFAULTS.PAPER_BOOK_MAX_AGE_S
DECISION = {"bids": [(9.95, 1000)], "asks": [(10.00, 1000)]}

_RIGS: list = []


@pytest.fixture(autouse=True)
def _close_rigs():
    yield
    while _RIGS:
        _RIGS.pop().close()


class Rig:
    """A PaperExchange on a FakeClock. Nothing refreshes the cache by itself: the test puts every quote."""

    def __init__(self, db, clock: FakeClock | None = None, cache: QuoteCache | None = None) -> None:
        self.db = db
        self.clock = clock if clock is not None else FakeClock(f"{DAY}T10:00:00+05:30")
        self.cache = cache if cache is not None else QuoteCache(mono=self.clock.mono)   # an empty cache is falsy
        self.asked: list[FetchQuotes] = []
        self.events: list = []
        self.seq = itertools.count(1)
        self.px = PaperExchange(db, settings=DEFAULTS, clock=self.clock, quotes=self.cache, calendar=CAL,
                                instruments={SYM: INST}.get, request_depth=self.asked.append, thread_check=False)
        _RIGS.append(self)

    def close(self) -> None:
        if not self.px._closed:
            self.px.close()

    def put(self, bids=(), asks=(), source: str = "WS", exch_wall: datetime | None = None) -> Quote:
        q = Quote(SYM, TOK, tuple(Level(p, n) for p, n in bids), tuple(Level(p, n) for p, n in asks), None, None,
                  None, exch_wall, self.clock.mono(), self.clock.now_wall(), source)
        assert self.cache.put(q)
        return q

    def place(self, side: Side, limit: float, decided: datetime | None = None) -> tuple[str, float]:
        """Place one lot, decided on a quote the exchange stamped `decided` (the engine sends that time with the order:
        DecisionQuote.exch_wall -> OrderRequest.decision_exch_wall); return (broker_order_id, due mono)."""
        role = Role.ENTRY if side is Side.BUY else Role.EXIT
        tag = make_tag(role, PAPER, DAY, next(self.seq), 1)
        placed = self.clock.mono()
        res = self.px.place(OrderRequest(PAPER, tag, role, SYM, TS, "NFO", TOK, side, LOT, limit, LOT, TICK,
                                         decision_exch_wall=decided))
        assert res.outcome == "OK", res
        self.events += self.px.take_events()
        row = self.row(res.broker_order_id)
        latency_s = (parse_wall(row["due_ts"]) - parse_wall(row["placed_ts"])).total_seconds()   # ms precision
        return res.broker_order_id, placed + latency_s

    def step_to(self, mono: float) -> None:
        self.clock.advance(round(mono - self.clock.mono(), 6))
        self.events += self.px.step()

    def step(self) -> None:
        self.events += self.px.step()

    def terminal(self, oid: str) -> OrderSnapshot | None:
        snaps = [e for e in self.events if isinstance(e, OrderSnapshot) and e.broker_order_id == oid
                 and e.status != "WORKING"]
        assert len(snaps) <= 1, snaps
        return snaps[0] if snaps else None

    def row(self, oid: str) -> dict:
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            c.row_factory = sqlite3.Row
            return dict(c.execute("SELECT * FROM p_orders WHERE broker_order_id = ?", (oid,)).fetchone())


def _decided_and_placed(rig: Rig, side: Side, limit: float, gap_s: float = 0.05) -> tuple[str, float]:
    """The decision snapshot is cached, then the order is placed gap_s later. Returns (oid, due mono)."""
    decision = rig.put(**DECISION, source="REST")              # e.g. the gateway's entry quote (FetchQuotes)
    rig.clock.advance(gap_s)
    oid, due = rig.place(side, limit)
    # still "fresh" at the due time: the old fill rule took it as the fill-time book
    assert due - decision.recv_mono < MAX_AGE
    return oid, due


# ---------------------------------------------------------------------------------------------------------------
# regressions: these fail on the old fill rule (fill on any quote <= PAPER_BOOK_MAX_AGE_S old)
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("gap_s", [0.05, 0.0])   # 0.0: a snapshot stamped at the placement instant is no newer
def test_entry_fills_on_the_fill_time_book_not_the_decision_snapshot(tmp_path, gap_s):
    rig = Rig(tmp_path / "p.db")
    oid, due = _decided_and_placed(rig, Side.BUY, 10.10, gap_s)
    rig.step_to(due - 0.005)
    assert rig.terminal(oid) is None and rig.asked == []
    rig.step_to(due + 0.002)
    # no book received since the order left: still WORKING, REST depth asked once at the PAPER priority
    assert rig.terminal(oid) is None and rig.row(oid)["status"] == "OPEN"
    (ask,) = rig.asked
    assert ask.symbols == (SYM,) and ask.priority == int(Priority.PAPER)

    rig.clock.advance(0.12)                                    # lane-reads' answer lands in the QuoteCache
    rig.put(bids=[(10.00, 1000)], asks=[(10.05, 1000)], source="REST")   # the book lifted during the latency
    rig.step()
    snap = rig.terminal(oid)
    assert (snap.status, snap.filled_qty, snap.avg_px) == ("COMPLETE", LOT, 10.05)   # one tick of latency slippage
    assert len(rig.asked) == 1


def test_book_lifting_through_the_limit_during_the_latency_leaves_the_ioc_unfilled(tmp_path):
    rig = Rig(tmp_path / "p.db")
    oid, due = _decided_and_placed(rig, Side.BUY, 10.05)
    rig.step_to(due + 0.002)
    assert rig.terminal(oid) is None
    rig.clock.advance(0.1)
    rig.put(bids=[(10.15, 1000)], asks=[(10.20, 1000)], source="REST")   # the spike ran through the limit
    rig.step()
    snap = rig.terminal(oid)
    assert (snap.status, snap.filled_qty, snap.status_msg) == ("CANCELLED", 0, "SIM: IOC unfilled")
    assert rig.px.trades_today(PAPER) == () and rig.px.positions(PAPER) == ()


def test_exit_fills_on_the_falling_bid_not_the_stale_one(tmp_path):
    rig = Rig(tmp_path / "p.db")
    oid, due = _decided_and_placed(rig, Side.SELL, 9.50)
    rig.step_to(due + 0.002)
    assert rig.terminal(oid) is None and len(rig.asked) == 1
    rig.clock.advance(0.1)
    rig.put(bids=[(9.70, 1000)], asks=[(9.75, 1000)], source="REST")
    rig.step()
    snap = rig.terminal(oid)
    assert (snap.status, snap.filled_qty, snap.avg_px) == ("COMPLETE", LOT, 9.70)    # not the decision bid 9.95


def test_no_book_after_placement_within_the_wait_cancels_no_fresh_depth(tmp_path):
    rig = Rig(tmp_path / "p.db")
    oid, due = _decided_and_placed(rig, Side.BUY, 10.10)
    rig.step_to(due + 0.002)                                   # the first look: REST asked, wait 1.5 s
    rig.step_to(due + 0.002 + PAPER_DEPTH_WAIT_S - 0.01)
    assert rig.terminal(oid) is None
    rig.step_to(due + 0.002 + PAPER_DEPTH_WAIT_S + 0.001)
    snap = rig.terminal(oid)
    assert (snap.status, snap.filled_qty, snap.status_msg) == ("CANCELLED", 0, MSG_NO_DEPTH)
    assert rig.px.trades_today(PAPER) == () and len(rig.asked) == 1


def test_order_rearmed_after_a_restart_never_fills_on_its_decision_snapshot(tmp_path):
    clock = FakeClock(f"{DAY}T10:00:00+05:30")
    cache = QuoteCache(mono=clock.mono)                        # survives the restart: the decision snapshot stays
    first = Rig(tmp_path / "p.db", clock=clock, cache=cache)
    oid, due = _decided_and_placed(first, Side.BUY, 10.10)
    first.close()
    clock.advance(0.02)                                        # paper-sim restarts before the due time
    again = Rig(tmp_path / "p.db", clock=clock, cache=cache)
    again.px.start()                                           # re-arms the OPEN order
    again.step_to(due + 0.002)
    assert again.terminal(oid) is None and len(again.asked) == 1
    clock.advance(0.1)
    again.put(bids=[(10.00, 1000)], asks=[(10.05, 1000)], source="REST")
    again.step()
    snap = again.terminal(oid)
    assert (snap.status, snap.filled_qty, snap.avg_px) == ("COMPLETE", LOT, 10.05)


# ---------------------------------------------------------------------------------------------------------------
# what still fills at the due time
# ---------------------------------------------------------------------------------------------------------------
def test_a_tick_received_after_placement_is_the_fill_time_book(tmp_path):
    rig = Rig(tmp_path / "p.db")
    oid, due = _decided_and_placed(rig, Side.BUY, 10.10)
    rig.clock.advance(0.03)
    rig.put(bids=[(10.00, 1000)], asks=[(10.05, 1000)])        # the WS tick after the order left
    rig.step_to(due + 0.002)
    snap = rig.terminal(oid)
    assert (snap.status, snap.avg_px) == ("COMPLETE", 10.05) and rig.asked == []    # no REST round trip


def test_a_post_placement_book_older_than_the_max_age_is_not_used(tmp_path):
    rig = Rig(tmp_path / "p.db")
    rig.clock.advance(0.05)
    oid, due = rig.place(Side.BUY, 10.10)
    rig.clock.advance(0.01)
    rig.put(**DECISION)                                        # after the placement...
    rig.step_to(due + MAX_AGE)                                 # ...but more than 1 s old once the order is looked at
    assert rig.terminal(oid) is None and len(rig.asked) == 1


# ---------------------------------------------------------------------------------------------------------------
# paper model 2: a crossed or locked book, or one older than the decision, is no fill-time book either
# ---------------------------------------------------------------------------------------------------------------
def _stamped_and_placed(rig: Rig, side: Side, limit: float, stamp: datetime | None = None) -> tuple[str, float]:
    """The decision snapshot (REST, exchange-stamped `stamp`, default now) is cached, then the order decided on it
    is placed 30 ms later, carrying its stamp. Returns (oid, due mono)."""
    decision = rig.put(**DECISION, source="REST", exch_wall=stamp or rig.clock.now_wall())
    rig.clock.advance(0.03)
    return rig.place(side, limit, decided=decision.exch_wall)


def test_a_lagged_packet_older_than_the_decision_is_not_the_fill_time_book(tmp_path):
    """Review round 2 (paper realism): the fill-time book was picked by receipt time only. A WS packet the exchange
    sent 2 s before the decision snapshot, stamped by on_ticks only after the order left (feed lag), held the cheaper
    ask of a market already gone, and the paper BUY filled under the decision ask. It is no book now: the order asks
    REST and fills on the answer."""
    rig = Rig(tmp_path / "p.db")
    decided = rig.clock.now_wall()
    oid, due = _stamped_and_placed(rig, Side.BUY, 10.10, decided)
    rig.clock.advance(0.02)
    rig.put(bids=[(9.85, 1000)], asks=[(9.90, 1000)], exch_wall=decided - timedelta(seconds=2))   # newest received
    rig.step_to(due + 0.002)
    assert rig.terminal(oid) is None and len(rig.asked) == 1          # waiting, as for a missing book
    rig.clock.advance(0.1)
    rig.put(bids=[(10.00, 1000)], asks=[(10.05, 1000)], source="REST", exch_wall=rig.clock.now_wall())
    rig.step()
    snap = rig.terminal(oid)
    assert (snap.status, snap.filled_qty, snap.avg_px) == ("COMPLETE", LOT, 10.05)   # never under the decision ask


def test_a_late_packet_received_just_before_the_order_is_not_the_decision(tmp_path):
    """Review of paper model 2 (probe test_probe_lag_before_place): the decision's exchange time was read from the
    newest quote in the cache when place() ran. The cache keeps the newest by receipt, so a burst of lagged packets
    (stamped late by on_ticks while the core held the GIL) that starts between the engine's decision and place()
    passed for the decision, and a later lagged packet, still older than the snapshot the engine priced from, filled
    the BUY under the decision ask (350 @ 25.80 under 26.05 in the probe). The anchor is the decision quote's own
    time now, which comes with the order: both packets are older books, waited out (test_seams.py drives the same
    case through the engine's request)."""
    rig = Rig(tmp_path / "p.db")
    decided = rig.clock.now_wall()
    decision = rig.put(**DECISION, source="REST", exch_wall=decided)              # what the engine decided on
    rig.clock.advance(0.02)
    rig.put(bids=[(9.80, 1000)], asks=[(9.85, 1000)], exch_wall=decided - timedelta(seconds=3))   # the burst begins
    rig.clock.advance(0.01)
    oid, due = rig.place(Side.BUY, 10.10, decided=decision.exch_wall)
    rig.clock.advance(0.02)
    rig.put(bids=[(9.85, 1000)], asks=[(9.90, 1000)], exch_wall=decided - timedelta(seconds=2))   # still older
    rig.step_to(due + 0.002)
    assert rig.terminal(oid) is None and len(rig.asked) == 1         # an older book: waited out, REST asked
    rig.clock.advance(0.1)
    rig.put(bids=[(10.00, 1000)], asks=[(10.05, 1000)], source="REST", exch_wall=rig.clock.now_wall())
    rig.step()
    snap = rig.terminal(oid)
    assert (snap.status, snap.filled_qty, snap.avg_px) == ("COMPLETE", LOT, 10.05)   # never under the decision ask


def test_a_decision_time_that_is_not_an_aware_datetime_is_ignored(tmp_path, caplog):
    """A malformed decision time (a naive datetime, only a coding error can send one) must not break step() with a
    naive/aware comparison: the order is accepted and checked by receipt only, with a warning."""
    rig = Rig(tmp_path / "p.db")
    rig.put(**DECISION, source="REST", exch_wall=rig.clock.now_wall())
    rig.clock.advance(0.03)
    oid, due = rig.place(Side.BUY, 10.10, decided=datetime(2026, 10, 12, 10, 0, 0))
    assert "is not an aware datetime" in caplog.text
    rig.clock.advance(0.02)
    rig.put(bids=[(10.00, 1000)], asks=[(10.05, 1000)], exch_wall=rig.clock.now_wall() - timedelta(seconds=5))
    rig.step_to(due + 0.002)
    snap = rig.terminal(oid)
    assert (snap.status, snap.avg_px) == ("COMPLETE", 10.05) and rig.asked == []


@pytest.mark.parametrize("bids, asks", [([(10.20, 1000)], [(9.95, 1000)]),      # crossed: a stale ask under the bid
                                        ([(10.00, 1000)], [(10.00, 1000)])])    # locked
def test_a_crossed_or_locked_book_is_not_filled_against(tmp_path, bids, asks):
    """Review round 2 (paper realism): a crossed book filled a paper BUY at its stale ask, under the book's own best
    bid, a price nobody offered any more (the gateway already refuses such a decision book). Crossed or locked, it
    is no fill-time book now: the order waits for the next one."""
    rig = Rig(tmp_path / "p.db")
    oid, due = _decided_and_placed(rig, Side.BUY, 10.25)
    rig.clock.advance(0.02)
    rig.put(bids=bids, asks=asks)
    rig.step_to(due + 0.002)
    assert rig.terminal(oid) is None and len(rig.asked) == 1
    rig.clock.advance(0.1)
    rig.put(bids=[(10.10, 1000)], asks=[(10.15, 1000)], source="REST")
    rig.step()
    snap = rig.terminal(oid)
    assert (snap.status, snap.filled_qty, snap.avg_px) == ("COMPLETE", LOT, 10.15)


@pytest.mark.parametrize("kind, reason", [("crossed", "SIM: crossed or locked book"),
                                          ("older", "SIM: book older than the decision")])
def test_a_wait_that_sees_only_unusable_books_cancels_under_their_sim_reason(tmp_path, kind, reason):
    """No usable book within PAPER_DEPTH_WAIT_S: CANCELLED, 0 filled, as for a missing book, but under its own SIM
    reason, which the daily report counts next to "SIM: no fresh depth"."""
    rig = Rig(tmp_path / "p.db")
    decided = rig.clock.now_wall()
    oid, due = _stamped_and_placed(rig, Side.BUY, 10.10, decided)

    def unusable() -> None:
        if kind == "crossed":
            rig.put(bids=[(10.20, 1000)], asks=[(9.95, 1000)], exch_wall=rig.clock.now_wall())
        else:
            rig.put(**DECISION, exch_wall=decided - timedelta(seconds=1))

    rig.clock.advance(0.02)
    unusable()
    rig.step_to(due + 0.002)                                   # REST asked
    rig.clock.advance(0.4)
    unusable()                                                 # the REST answer is no better
    rig.step()
    rig.step_to(due + 0.002 + PAPER_DEPTH_WAIT_S - 0.01)
    assert rig.terminal(oid) is None
    rig.step_to(due + 0.002 + PAPER_DEPTH_WAIT_S + 0.001)
    snap = rig.terminal(oid)
    assert (snap.status, snap.filled_qty, snap.status_msg) == ("CANCELLED", 0, reason)
    assert rig.px.trades_today(PAPER) == () and len(rig.asked) == 1


@pytest.mark.parametrize("stamp_ms", [100, None])      # earlier within the decision's second; no exchange stamp
def test_a_book_stamped_in_the_decision_second_or_not_stamped_still_counts(tmp_path, stamp_ms):
    """Kite stamps exchange times in whole seconds, so only a book from an earlier second is older content; within
    the decision's second, or with no stamp at all, the two cannot be told apart and the book counts."""
    rig = Rig(tmp_path / "p.db")
    decided = rig.clock.now_wall() + timedelta(milliseconds=700)            # 10:00:00.700
    oid, due = _stamped_and_placed(rig, Side.BUY, 10.10, decided)
    rig.clock.advance(0.02)
    stamp = None if stamp_ms is None else decided.replace(microsecond=stamp_ms * 1000)
    rig.put(bids=[(10.00, 1000)], asks=[(10.05, 1000)], exch_wall=stamp)
    rig.step_to(due + 0.002)
    snap = rig.terminal(oid)
    assert (snap.status, snap.avg_px) == ("COMPLETE", 10.05) and rig.asked == []
