"""Review fixes (strategy_g1 lens): paper fills as realistic as live (broker/paper.py, DETAIL §6.3).

1. One-lot levels. fill_from_depth took floor(level qty x participation / lot) lots PER LEVEL, so at participation
   0.5 (its maximum) a level holding one lot gave nothing. MAX_LOTS is 1, so every order is one lot: an entry IOC
   was cancelled against the usual stock-option touch that a live IOC takes at once, and a held position's exit
   failed at every band. Participation now applies to the depth walked so far.
2. The fill-time book. Any quote fresh within PAPER_BOOK_MAX_AGE_S was the fill-time book, including the decision
   snapshot received before the order existed: no latency slippage and no REST look at the real book. Only a book
   received after the order was placed counts now.
3. The leftover fraction of a lot (paper model 2, review round 2). The walked share was still rounded down to whole
   lots, so a lone one-lot level inside an IOC's limit (half a lot at 0.5) never filled: on a one-second gap a stop's
   IOC missed the lone bid a live IOC takes, the exit retried and sold at the restored touch, a scratch where LIVE
   loses the gap. The fraction now fills one more lot with that probability, by the order's own seeded draw, so the
   expected fill is participation x the walked depth, on both sides. A lone one-lot level fills with probability
   participation, half the time: half of that bias is left (a live IOC usually takes the lot).
4. The calibration is fixed (the review of paper model 2). No side of PAPER_PARTICIPATION, PAPER_LATENCY_SIGMA or
   PAPER_LATENCY_MEDIAN_MS is safe: a lower participation, a wider sigma and a slower median each flatter paper in
   some case, so a settings file may not change them (nor the other keys that only calibrate the paper exchange).
"""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import random
import sqlite3
from dataclasses import replace
from datetime import date

import pytest

from titan2 import settings as st
from titan2.broker.paper import MSG_NO_DEPTH, PaperExchange, fill_from_depth
from titan2.clock import FakeClock, SessionCalendar, parse_wall
from titan2.core.events import OrderSnapshot, Priority
from titan2.market.quotes import QuoteCache
from titan2.model import Account, Instrument, Level, OrderRequest, Quote, Role, Side, make_tag
from titan2.settings import DEFAULTS, PAPER_LATENCY_CLIP_MS, Settings

CAL = SessionCalendar.load()
DAY = date(2026, 10, 12)                     # a Monday session
SYM, TS, TOK = "NFO:RELIANCE26OCT2900CE", "RELIANCE26OCT2900CE", 13001218
LOT, TICK = 500, 0.05
INST = Instrument(SYM, TOK, TS, "NFO", "RELIANCE", "NSE:RELIANCE", "CE", 2900.0, date(2026, 10, 27), LOT, TICK)
P = DEFAULTS.PAPER_PARTICIPATION             # 0.5

# one lot at each of the top three levels: passes G19 and size() (top-3 depth >= 3 x qty) at one lot
BIDS = [(29.95, LOT), (29.90, LOT), (29.85, LOT)]
ASKS = [(30.00, LOT), (30.05, LOT), (30.10, LOT)]
ENTRY_LIMIT = 30.10                          # ask + 2 ticks (exits.entry_limit)
EXIT_LIMIT_BAND0 = 29.85                     # best bid - 2 ticks (exits.exit_limit, band 0)


def draw_of(tag: str, account: Account = Account.PAPER) -> float:
    """An order's fill draw (paper model 2), from its seed: the first random() of
    random.Random(int(sha256(f"{session}|{account}|fill|{tag}")))."""
    text = f"{DAY.isoformat()}|{Account(account).value}|fill|{tag}"
    return random.Random(int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16)).random()


def seq_where(role: Role, ok) -> int:
    """The first seq_no whose PAPER tag (attempt 1) on DAY draws a number for which ok(draw) holds."""
    return next(n for n in itertools.count(1) if ok(draw_of(make_tag(role, Account.PAPER, DAY, n, 1))))


# ---------------------------------------------------------------------------------------------------------------
# 1. participation over the walked depth
# ---------------------------------------------------------------------------------------------------------------
def test_one_lot_levels_fill_a_one_lot_order():
    assert P == 0.5
    # entry: half a lot per one-lot level. The touch's half fills when the order's draw is under 0.5; otherwise the
    #   walk earns its lot at the second ask, one tick over the touch
    assert fill_from_depth(ASKS, Side.BUY, LOT, ENTRY_LIMIT, LOT, P, 0, TICK, draw=0.49) == [(30.00, LOT)]
    assert fill_from_depth(ASKS, Side.BUY, LOT, ENTRY_LIMIT, LOT, P, 0, TICK, draw=0.5) == [(30.05, LOT)]
    # exit at band 0, the first attempt: filled at the touch or one tick under it
    assert fill_from_depth(BIDS, Side.SELL, LOT, EXIT_LIMIT_BAND0, LOT, P, 0, TICK, draw=0.49) == [(29.95, LOT)]
    assert fill_from_depth(BIDS, Side.SELL, LOT, EXIT_LIMIT_BAND0, LOT, P, 0, TICK, draw=0.5) == [(29.90, LOT)]
    # a thin touch then 2 lots 6.5% down: the exit still fills near the touch, not at the deep level
    deep = BIDS + [(29.80, LOT), (28.00, 2 * LOT)]
    assert fill_from_depth(deep, Side.SELL, LOT, EXIT_LIMIT_BAND0, LOT, P, 0, TICK, draw=0.999) == [(29.90, LOT)]
    assert fill_from_depth(deep, Side.SELL, LOT, EXIT_LIMIT_BAND0, LOT, P, 0, TICK, draw=0.0) == [(29.95, LOT)]


def test_participation_still_caps_the_walked_share():
    # an IOC at the touch alone has earned half a lot: it fills half the time (a live IOC usually takes it). Paper
    #   model 1 rounded the half down, so it never filled (review round 2)
    assert fill_from_depth(BIDS, Side.SELL, LOT, 29.95, LOT, P, 0, TICK, draw=0.49) == [(29.95, LOT)]
    assert fill_from_depth(BIDS, Side.SELL, LOT, 29.95, LOT, P, 0, TICK, draw=0.5) == []
    # never more than participation x the depth within the limit, rounded to a whole lot by the draw; the IOC
    #   remainder is unfilled
    assert fill_from_depth(ASKS, Side.BUY, 2 * LOT, ENTRY_LIMIT, LOT, P, 0, TICK, draw=0.5) == [(30.05, LOT)]
    assert fill_from_depth(ASKS, Side.BUY, 2 * LOT, ENTRY_LIMIT, LOT, P, 0, TICK, draw=0.49) == [
        (30.00, LOT), (30.10, LOT)]
    # levels beyond the limit add nothing: only the touch's half lot counts
    beyond = ASKS + [(30.15, 50 * LOT)]
    assert fill_from_depth(beyond, Side.BUY, LOT, 30.00, LOT, P, 0, TICK, draw=0.5) == []
    assert fill_from_depth(beyond, Side.BUY, LOT, 30.00, LOT, P, 0, TICK, draw=0.0) == [(30.00, LOT)]
    # whole-lot shares per level: exactly DETAIL §6.3's per-level floor(level qty x participation / lot), whatever
    #   the draw
    two = [(30.00, 2 * LOT), (30.05, 2 * LOT), (30.10, 2 * LOT)]
    for draw in (0.0, 0.5, 0.999):
        assert fill_from_depth(two, Side.BUY, 3 * LOT, ENTRY_LIMIT, LOT, P, 0, TICK, draw=draw) == [
            (30.00, LOT), (30.05, LOT), (30.10, LOT)]
    # the lowest participation setting (0.1): a tenth of a lot per one-lot level. The lot fills at the level where
    #   the walked share passes the draw, and ten levels earn it whatever the draw
    ten = [(round(30.00 + 0.05 * k, 2), LOT) for k in range(10)]
    assert fill_from_depth(ten[:9], Side.BUY, LOT, 31.00, LOT, 0.1, 0, TICK, draw=0.95) == []
    assert fill_from_depth(ten[:9], Side.BUY, LOT, 31.00, LOT, 0.1, 0, TICK, draw=0.35) == [(30.15, LOT)]
    assert fill_from_depth(ten, Side.BUY, LOT, 31.00, LOT, 0.1, 0, TICK, draw=0.999) == [(30.45, LOT)]
    # the square-off walk (lot 1, participation 1.0) still takes every shown unit level by level
    assert fill_from_depth(BIDS, Side.SELL, 2 * LOT, None, 1, 1.0, 0, TICK, draw=0.0) == [(29.95, LOT), (29.90, LOT)]


# ---------------------------------------------------------------------------------------------------------------
# the leftover fraction of a lot (item 3: paper model 2)
# ---------------------------------------------------------------------------------------------------------------
def test_expected_fill_is_participation_times_the_walked_depth():
    """Over an even grid of draws the walk fills, on average, participation x the depth within the limit, level by
    level (so the average value is participation x the walked depth's value too), on both sides. Paper model 1's
    whole-lot rounding filled 1 lot here every time, where the share is 1.3 lots."""
    n = 4000
    draws = [(k + 0.5) / n for k in range(n)]
    books = ((Side.BUY, [(30.00, LOT), (30.05, int(1.6 * LOT)), (30.10, 2 * LOT)], 30.05),
             (Side.SELL, [(29.95, LOT), (29.90, int(1.6 * LOT)), (29.85, 2 * LOT)], 29.90))
    for side, book, limit in books:
        inside = book[:2]                                          # 2.6 lots within the limit: a share of 1.3 lots
        runs = [fill_from_depth(book, side, 2 * LOT, limit, LOT, P, 0, TICK, draw=d) for d in draws]
        assert sum(q for r in runs for _, q in r) / n == pytest.approx(P * sum(q for _, q in inside))
        assert sum(px * q for r in runs for px, q in r) / n == pytest.approx(P * sum(px * q for px, q in inside))
        assert {sum(q for _, q in r) for r in runs} == {LOT, 2 * LOT}                   # whole lots, within qty


def test_fill_draw_is_the_orders_own_seeded_number():
    """seed = int(sha256(f"{session}|{account}|fill|{tag}")): one draw per order, its own stream (not the latency's
    or the read delay's), so it follows the order and not how many orders came before it."""
    from titan2.broker.paper import fill_draw, fill_seed, latency_seed, read_latency_seed
    tag = make_tag(Role.EXIT, Account.PAPER, DAY, 7, 2)
    assert fill_seed(DAY, "PAPER", tag) == int(hashlib.sha256(f"2026-10-12|PAPER|fill|{tag}".encode()).hexdigest(), 16)
    assert fill_draw(DAY, Account.PAPER, tag) == draw_of(tag) and 0.0 <= draw_of(tag) < 1.0
    assert fill_seed(DAY, Account.PAPER, tag) not in (latency_seed(DAY, Account.PAPER),
                                                      read_latency_seed(DAY, Account.PAPER))
    assert fill_draw(DAY, Account.SHADOW, tag) == draw_of(tag, Account.SHADOW) != draw_of(tag)


# ---------------------------------------------------------------------------------------------------------------
# through the exchange
# ---------------------------------------------------------------------------------------------------------------
class Rig:
    """A PaperExchange on a FakeClock; book() puts a quote received now (a WS tick, or a REST answer)."""

    def __init__(self, db, settings: Settings = DEFAULTS) -> None:
        self.db = db
        self.clock = FakeClock(f"{DAY}T10:00:00+05:30")
        self.cache = QuoteCache(mono=self.clock.mono)
        self.asked: list = []
        self.n = itertools.count(1)
        self.px = PaperExchange(db, settings=settings, clock=self.clock, quotes=self.cache, calendar=CAL,
                                instruments={SYM: INST}.get, request_depth=self.asked.append, thread_check=False)

    def close(self) -> None:
        self.px.close()

    def book(self, bids, asks, source: str = "WS") -> None:
        self.cache.put(Quote(SYM, TOK, tuple(Level(p, q) for p, q in bids), tuple(Level(p, q) for p, q in asks),
                             30.0, None, None, None, self.clock.mono(), self.clock.now_wall(), source))

    def place(self, side: Side, limit: float, seq: int | None = None):
        role = Role.ENTRY if side is Side.BUY else Role.EXIT
        tag = make_tag(role, Account.PAPER, self.clock.today(), next(self.n) if seq is None else seq, 1)
        res = self.px.place(OrderRequest(Account.PAPER, tag, role, SYM, TS, "NFO", TOK, side, LOT, limit, LOT, TICK))
        assert res.outcome == "OK", res
        return res

    def run(self, seconds: float, feed=None, dt: float = 0.05) -> list:
        events = []
        for _ in range(round(seconds / dt)):
            self.clock.advance(dt)
            if feed is not None:
                feed()
            events += self.px.step()
        return events

    def latency_s(self, oid: str) -> float:
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            placed, due = c.execute("SELECT placed_ts, due_ts FROM p_orders WHERE broker_order_id = ?",
                                    (oid,)).fetchone()
        return (parse_wall(due) - parse_wall(placed)).total_seconds()


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path / "paper_exchange.db")
    yield r
    r.close()


def terminal(events, oid: str) -> OrderSnapshot:
    snaps = [e for e in events if isinstance(e, OrderSnapshot) and e.broker_order_id == oid
             and e.status != "WORKING"]
    assert len(snaps) == 1, snaps
    return snaps[0]


def test_one_lot_entry_and_exit_complete_on_a_one_lot_book(rig):
    def feed():
        rig.book(BIDS, ASKS)

    feed()
    entry = rig.place(Side.BUY, ENTRY_LIMIT)
    snap = terminal(rig.run(3.1, feed), entry.broker_order_id)
    # at the touch when the order's draw is under 0.5 (the touch's half lot), else at the second level
    assert (snap.status, snap.filled_qty, snap.avg_px) == ("COMPLETE", LOT, 30.00 if draw_of(entry.order_pk) < P
                                                           else 30.05)
    exit_ = rig.place(Side.SELL, EXIT_LIMIT_BAND0)                  # the first exit attempt, band 0
    snap = terminal(rig.run(3.1, feed), exit_.broker_order_id)
    assert (snap.status, snap.filled_qty, snap.avg_px) == ("COMPLETE", LOT, 29.95 if draw_of(exit_.order_pk) < P
                                                           else 29.90)
    assert rig.px.positions(Account.PAPER)[0].net_qty == 0


def test_a_lone_one_lot_bid_inside_the_limit_fills_by_the_order_draw(rig):
    """Review round 2 (exits probes p02, p04): one snapshot shows a lone one-lot bid 19% under the touch and the
    stop's IOC is priced 2 ticks under it. The exchange matches that bid. Paper model 1 never filled it (half a lot
    at 0.5), so the exit retried at the restored touch and booked a scratch. Now it fills at that bid when the
    order's draw is under 0.5, half the time; otherwise it is unfilled and the ladder goes on, where a live IOC
    usually takes the bid (test_a_lower_participation_is_no_tightening counts both)."""
    gap = [(24.30, LOT)]

    def feed():
        rig.book(gap, [(30.00, 4 * LOT)])

    feed()
    hit = rig.place(Side.SELL, 24.20, seq=seq_where(Role.EXIT, lambda d: d < P))
    snap = terminal(rig.run(3.1, feed), hit.broker_order_id)
    assert (snap.status, snap.filled_qty, snap.avg_px) == ("COMPLETE", LOT, 24.30)
    miss = rig.place(Side.SELL, 24.20, seq=seq_where(Role.EXIT, lambda d: d >= P))
    snap = terminal(rig.run(3.1, feed), miss.broker_order_id)
    assert (snap.status, snap.filled_qty, snap.status_msg) == ("CANCELLED", 0, "SIM: IOC unfilled")


def test_a_replay_gives_the_same_fills_whatever_the_order_of_placement(tmp_path):
    """The draw is each order's own (its session, account and tag), not the n-th of a sequence: replaying the same
    orders on a fresh exchange, even in another order, fills each one the same."""
    seqs = (seq_where(Role.ENTRY, lambda d: d < P), seq_where(Role.ENTRY, lambda d: d >= P))
    fills = []
    for name, order in (("a", seqs), ("b", seqs[::-1])):
        r = Rig(tmp_path / f"{name}.db")
        try:
            r.book(BIDS, ASKS)
            got = {}
            for seq in order:
                res = r.place(Side.BUY, ENTRY_LIMIT, seq=seq)
                got[seq] = terminal(r.run(3.1, lambda: r.book(BIDS, ASKS)), res.broker_order_id).avg_px
            fills.append(got)
        finally:
            r.close()
    assert fills[0] == fills[1] == {seqs[0]: 30.00, seqs[1]: 30.05}


# ---------------------------------------------------------------------------------------------------------------
# 2. the fill-time book is received after the order was placed
# ---------------------------------------------------------------------------------------------------------------
def test_the_decision_snapshot_never_fills_its_own_order(rig):
    deep_bids, deep_asks = [(29.95, 4 * LOT)], [(30.00, 4 * LOT)]
    rig.book(deep_bids, deep_asks, source="REST")                   # the decision fetch: ask 30.00
    res = rig.place(Side.BUY, ENTRY_LIMIT)
    oid = res.broker_order_id
    assert rig.latency_s(oid) < DEFAULTS.PAPER_BOOK_MAX_AGE_S       # the snapshot is still "fresh" when it is due
    events = []
    for _ in range(80):                                             # up to the due time; the feed sends nothing
        rig.clock.advance(0.05)
        events += rig.px.step()
        if rig.asked:
            break
    assert not [e for e in events if isinstance(e, OrderSnapshot) and e.status != "WORKING"], events
    (ask,) = rig.asked                                              # it asked lane-reads for the real book
    assert ask.symbols == (SYM,) and ask.priority == int(Priority.PAPER)
    rig.clock.advance(0.2)
    rig.book(deep_bids, [(30.05, 4 * LOT)], source="REST")         # the answer: the ask moved a tick meanwhile
    snap = terminal(events + rig.px.step(), oid)
    assert (snap.status, snap.filled_qty, snap.avg_px) == ("COMPLETE", LOT, 30.05)   # latency slippage included


def test_only_a_pre_order_book_and_no_answer_cancels(rig):
    rig.book([(29.95, 4 * LOT)], [(30.00, 4 * LOT)], source="REST")
    res = rig.place(Side.BUY, ENTRY_LIMIT)
    snap = terminal(rig.run(4.6), res.broker_order_id)              # latency (<= 3 s) + the 1.5 s depth wait
    assert (snap.status, snap.filled_qty, snap.status_msg) == ("CANCELLED", 0, MSG_NO_DEPTH)
    assert len(rig.asked) == 1 and rig.px.trades_today(Account.PAPER) == ()


# ---------------------------------------------------------------------------------------------------------------
# 4. the calibration is fixed: no side of it is safe (the review of paper model 2)
# ---------------------------------------------------------------------------------------------------------------
GAP_BID = 24.30                              # a stop's gap: one second of a lone bid ~19% under the touch
STOP_LIMIT = 24.20                           # the stop's IOC: 2 ticks under that bid
TOUCH = ([(29.95, 4 * LOT)], [(30.00, 4 * LOT)])   # the book before and after the gap (4 lots: no draw involved)


def _refused(key: str, value: float) -> None:
    """A settings file that sets `key` to `value` is invalid now: the key is fixed at its default."""
    s, errors = st.from_mapping({key: value})
    assert s is None and errors == [f"{key}: {value!r} is not {st.LIMITS[key].default!r} (fixed: only a code change "
                                    "can change it)"], errors


def test_a_wider_latency_sigma_is_no_tightening(tmp_path):
    """Review of paper model 2 (probe test_probe_sigma): PAPER_LATENCY_SIGMA counted as 'higher is tighter' and a
    file could set it up to 2.0. Each order draws a fixed seeded z (latency = median x exp(sigma x z)), so a wider
    sigma makes every order with z < 0 faster: 90 of these 200, 70 of them at the 80 ms floor (12 at the default).
    Faster paper orders meet a book closer to their decision snapshot. So the key is fixed at its default."""
    def latencies(settings: Settings, name: str) -> list[float]:
        r = Rig(tmp_path / name, settings=settings)
        try:
            r.book(*TOUCH)
            for _ in range(200):
                r.place(Side.SELL, 29.00)                      # the n-th order draws the session's n-th z
        finally:
            r.close()
        with contextlib.closing(sqlite3.connect(tmp_path / name)) as c:
            rows = c.execute("SELECT placed_ts, due_ts FROM p_orders ORDER BY broker_order_id").fetchall()
        return [(parse_wall(due) - parse_wall(placed)).total_seconds() * 1000 for placed, due in rows]

    base, wide = latencies(DEFAULTS, "d.db"), latencies(replace(DEFAULTS, PAPER_LATENCY_SIGMA=2.0), "w.db")
    floor = PAPER_LATENCY_CLIP_MS[0] + 1e-6
    faster = sum(1 for a, b in zip(base, wide) if b < a - 1e-6)
    print(f"sigma 2.0: {faster}/200 orders faster; at the 80 ms floor {sum(b <= floor for b in wide)} "
          f"(default {sum(a <= floor for a in base)})")
    assert faster > 50 and sum(b <= floor for b in wide) > 2 * sum(a <= floor for a in base)
    _refused("PAPER_LATENCY_SIGMA", 2.0)


def test_a_slower_latency_median_is_no_tightening(tmp_path):
    """Review of paper model 2 (probe gap/test_probe_gap_latency, there end to end on PAPER): PAPER_LATENCY_MEDIAN_MS
    counted as 'higher is tighter' and a file could set it up to 3000 ms. A stop's IOC is priced into a one-second gap
    (2 lots bid, so the order's draw plays no part). At the default delay it sells the gap bid, as the exchange does;
    with a 3000 ms median the same order lands after the gap and sells at the restored touch: the probe's -Rs 146
    scratch instead of LIVE's -Rs 3,170. So the key is fixed at its default."""
    sold = {}
    for name, settings in (("default", DEFAULTS), ("slow", replace(DEFAULTS, PAPER_LATENCY_MEDIAN_MS=3000.0))):
        r = Rig(tmp_path / f"{name}.db", settings=settings)
        try:
            gap = ([(GAP_BID, 2 * LOT)], TOUCH[1])

            def feed(book=gap):
                r.book(*book)

            feed()
            oid = r.place(Side.SELL, STOP_LIMIT).broker_order_id    # decided on the gap's first packet
            events = r.run(1.0, feed) + r.run(3.0, lambda: r.book(*TOUCH))   # the gap for a second, then the touch
            snap = terminal(events, oid)
            sold[name] = (snap.status, snap.filled_qty, snap.avg_px, round(r.latency_s(oid), 3))
        finally:
            r.close()
    print("stop into a one-second gap:", sold)
    assert sold["default"][:3] == ("COMPLETE", LOT, GAP_BID) and sold["default"][3] < 1.0
    assert sold["slow"][:3] == ("COMPLETE", LOT, TOUCH[0][0][0]) and sold["slow"][3] > 1.0   # flattered
    _refused("PAPER_LATENCY_MEDIAN_MS", 3000.0)


def test_a_lower_participation_is_no_tightening(tmp_path):
    """Review of paper model 2 (probe test_probe_lone_bid): a stop's IOC into a lone 1-lot gap bid fills only when
    its draw is under participation x 1 lot. At the default 0.5 that is about half the orders, where a live IOC
    usually takes the bid: paper model 2 halves that bias, it does not remove it. At 0.1, which a file could set as
    'lower is tighter', about one order in ten: paper books the scratch instead of the gap's loss more often still.
    So the key is fixed at its default."""
    n, rates = 200, {}
    for p in (P, 0.1):
        r = Rig(tmp_path / f"p{p}.db", settings=replace(DEFAULTS, PAPER_PARTICIPATION=p))
        try:
            gap = ([(GAP_BID, LOT)], TOUCH[1])
            filled = 0
            for k in range(1, n + 1):
                r.book(*gap)
                res = r.place(Side.SELL, STOP_LIMIT, seq=k)
                filled += terminal(r.run(3.1, lambda: r.book(*gap)), res.broker_order_id).filled_qty == LOT
            rates[p] = filled / n
        finally:
            r.close()
    print(f"lone 1-lot gap bid, stop IOCs filled: participation 0.5 -> {rates[P]:.3f}, 0.1 -> {rates[0.1]:.3f}")
    assert 0.4 <= rates[P] <= 0.6                                    # about half the time, not always
    assert rates[0.1] < rates[P] / 2
    _refused("PAPER_PARTICIPATION", 0.1)
