"""U06: market/quotes.py (QuoteCache), market/feed.py (Feed, SignalQueue, ticks), market/bars.py (Bars, check)."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from titan2.clock import IST, FakeClock, SessionCalendar
from titan2.core.events import FeedState, OrderSnapshot, QuotesDirty, Subscribe, Timer, Unsubscribe
from titan2.market.bars import (
    BAR_CHECK_SAMPLE, DAY_OPEN_CHANGED, FEED_GAP, HIST, LATE_TICK, LIVE, NO_DAY_OPEN, NO_VOLUME_BASE, Bars,
    bar_check_symbols, check_bar, find_bar, prev_session_window,
)
from titan2.market.feed import (
    WS_THREAD, Feed, FeedLink, SignalQueue, SignalsLost, quote_from_tick, tick_time, underlying_from_tick,
)
from titan2.market.quotes import QuoteCache
from titan2.model import Account, Bar, Level, Quote, UnderlyingTick, make_tag, Role

ROOT = Path(__file__).resolve().parents[2]          # agentic_trader/
SESSION = date(2026, 10, 12)
OPT, OPT2 = "NFO:RELIANCE26OCT2900CE", "NFO:RELIANCE26OCT2900PE"
REL, NIFTY = "NSE:RELIANCE", "NSE:NIFTY 50"
OPT_TOK, OPT2_TOK, REL_TOK, NIFTY_TOK = 13001218, 13001474, 738561, 256265
SYMBOLS = {OPT_TOK: OPT, OPT2_TOK: OPT2, REL_TOK: REL, NIFTY_TOK: NIFTY}
OPEN = datetime(2026, 10, 12, 9, 15, tzinfo=IST)
CLOSE = datetime(2026, 10, 12, 15, 40, tzinfo=IST)     # NSE F&O close since 2026-08-03


def at(hms: str, d: date = SESSION) -> datetime:
    return datetime.combine(d, datetime.strptime(hms, "%H:%M:%S.%f" if "." in hms else "%H:%M:%S").time(),
                            tzinfo=IST)


# ---------------------------------------------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------------------------------------------
class FakeReactor:
    """callFromThread queues work; run_pending() runs it on a dedicated 'reactor' thread, like twisted's."""

    THREAD = "fake-reactor"

    def __init__(self) -> None:
        self.pending: list = []
        self.stopped = False

    def callFromThread(self, fn, *args, **kwargs) -> None:
        self.pending.append((fn, args, kwargs))

    def on_reactor(self, fn, *args):
        """Run fn(*args) on the reactor thread (connection callbacks arrive there) and return its result."""
        out, err = [], []

        def run():
            try:
                out.append(fn(*args))
            except BaseException as exc:                      # surfaced to the test below
                err.append(exc)
        t = threading.Thread(target=run, name=self.THREAD)
        t.start()
        t.join()
        if err:
            raise err[0]
        return out[0] if out else None

    def run_pending(self) -> int:
        def drain():
            while self.pending:
                fn, args, kwargs = self.pending.pop(0)
                fn(*args, **kwargs)
        n = len(self.pending)
        self.on_reactor(drain)
        return n

    def stop(self) -> None:
        self.stopped = True


class FakeTicker:
    """Records every KiteTicker method call with the calling thread's name."""

    def __init__(self, fail: str | None = None) -> None:
        self.calls: list[tuple[str, str, tuple]] = []
        self.subscribed_tokens: dict[int, str] = {}
        self.fail = fail
        self.websocket_thread = None

    def _rec(self, name: str, *args) -> None:
        self.calls.append((threading.current_thread().name, name, args))
        if self.fail == name:
            raise RuntimeError(f"{name} failed")

    def subscribe(self, tokens):
        self._rec("subscribe", list(tokens))
        for t in tokens:
            self.subscribed_tokens[t] = "quote"

    def set_mode(self, mode, tokens):
        self._rec("set_mode", mode, list(tokens))
        for t in tokens:
            self.subscribed_tokens[t] = mode

    def unsubscribe(self, tokens):
        self._rec("unsubscribe", list(tokens))
        for t in tokens:
            self.subscribed_tokens.pop(t, None)

    def connect(self, threaded=False):
        self._rec("connect", threaded)
        self.websocket_thread = threading.Thread(target=lambda: None)   # never started; only renamed

    def close(self):
        self._rec("close")

    def methods(self) -> list[tuple[str, tuple]]:
        return [(name, args) for _, name, args in self.calls]


def make_feed(max_batches: int = 20_000, fail: str | None = None):
    clock = FakeClock("2026-10-12T10:15:55+05:30")
    cache, sq, reactor, ticker, posted = QuoteCache(clock.mono), SignalQueue(max_batches), FakeReactor(), \
        FakeTicker(fail), []
    feed = Feed(ticker, cache, posted.append, sq, SYMBOLS.get, clock=clock, reactor=reactor)
    return feed, clock, cache, sq, reactor, ticker, posted


def depth_side(*levels):
    """Kite always sends 5 levels per side; missing ones are zeros."""
    rows = [{"price": p, "quantity": q, "orders": o} for p, q, o in levels]
    return rows + [{"price": 0.0, "quantity": 0, "orders": 0}] * (5 - len(rows))


def option_tick(token=OPT_TOK, ltp=84.0, bid=84.0, ask=84.1, volume=125100):
    trade = datetime(2026, 10, 12, 10, 15, 54, tzinfo=IST).timestamp()
    exch = datetime(2026, 10, 12, 10, 15, 55, tzinfo=IST).timestamp()
    return {
        "tradable": True, "mode": "full", "instrument_token": token, "last_price": ltp,
        "last_traded_quantity": 65, "average_traded_price": 83.55, "volume_traded": volume,
        "total_buy_quantity": 2356, "total_sell_quantity": 2440, "change": 0.46,
        "ohlc": {"open": 80.0, "high": 86.0, "low": 79.5, "close": 81.0},
        "last_trade_time": datetime.fromtimestamp(trade),        # kiteconnect: naive host-local time
        "exchange_timestamp": datetime.fromtimestamp(exch),
        "oi": 21845, "oi_day_high": 0, "oi_day_low": 0,
        "depth": {"buy": depth_side((bid, 650, 3), (round(bid - 0.05, 2), 1300, 5), (round(bid - 0.1, 2), 65, 1)),
                  "sell": depth_side((ask, 130, 2), (round(ask + 0.05, 2), 1950, 7))},
    }


def stock_tick(token=REL_TOK, ltp=2915.5, cum=1_234_567, day_open=2900.0, prev_close=2880.0):
    """Quote mode (44-byte packet): no depth, no timestamps."""
    return {"tradable": True, "mode": "quote", "instrument_token": token, "last_price": ltp,
            "last_traded_quantity": 10, "average_traded_price": 2910.2, "volume_traded": cum,
            "total_buy_quantity": 50_000, "total_sell_quantity": 61_000, "change": 1.2,
            "ohlc": {"open": day_open, "high": 2930.0, "low": 2895.0, "close": prev_close}}


def index_tick(ltp=25010.5):
    """Index quote mode (28-byte packet): ohlc only."""
    return {"tradable": False, "mode": "quote", "instrument_token": NIFTY_TOK, "last_price": ltp,
            "ohlc": {"high": 25050.0, "low": 24900.0, "open": 24950.0, "close": 24900.0}, "change": 0.44}


def quote(symbol, recv_mono, *, bids=((10.0, 650),), asks=((10.1, 650),), source="WS"):
    return Quote(symbol, 1, tuple(Level(p, q) for p, q in bids), tuple(Level(p, q) for p, q in asks), 10.05, 100,
                 None, None, recv_mono, at("10:00:00"), source)


def ut(symbol, wall, ltp, cum, day_open=None, prev_close=None, token=1):
    return UnderlyingTick(symbol, token, ltp, cum, day_open, prev_close, None, 0.0, wall)


def of(posted, kind):
    return [e for e in posted if isinstance(e, kind)]


# ---------------------------------------------------------------------------------------------------------------
# QuoteCache
# ---------------------------------------------------------------------------------------------------------------
def test_fresh_none_when_stale():
    clock = FakeClock("2026-10-12T10:00:00+05:30")
    cache = QuoteCache(clock.mono)
    q = quote(OPT, clock.mono())
    assert cache.put(q)
    assert cache.fresh(OPT, 1.0) is q
    clock.advance(1.0)
    assert cache.fresh(OPT, 1.0) is q                     # exactly max_age is still fresh
    clock.advance(0.001)
    assert cache.fresh(OPT, 1.0) is None                  # never a stale quote
    assert cache.fresh(OPT, 3.0) is q                     # the caller's own limit decides
    assert cache.fresh(OPT, 1.0, now_mono=q.recv_mono + 0.5) is q
    assert cache.fresh(OPT, 1.0, now_mono=q.recv_mono - 0.5) is q    # received while the caller's event ran: age 0
    assert cache.last(OPT) is q                           # the explicit any-age accessor (protective pricing)
    assert cache.age(OPT) == pytest.approx(1.001)
    # missing quote, broken limits and clock-domain bugs all read as stale (fail closed)
    assert cache.fresh("NFO:UNKNOWN", 60.0) is None and cache.last("NFO:UNKNOWN") is None
    for bad in (float("nan"), float("inf"), -0.5, True, None, "1"):
        assert cache.fresh(OPT, bad) is None
    assert cache.fresh(OPT, 1.0, now_mono=float("nan")) is None
    future = quote(OPT2, clock.mono() + 5.0)              # receipt time in the future: age is inf
    cache.put(future)
    assert cache.fresh(OPT2, 1e9) is None
    # nothing that is not a Quote gets in
    with pytest.raises(TypeError):
        cache.put({"symbol": OPT})
    with pytest.raises(ValueError):
        cache.put(quote(OPT, float("nan")))
    assert cache.symbols() == frozenset({OPT, OPT2}) and len(cache) == 2


def test_quote_cache_newest_wins():
    cache = QuoteCache(lambda: 1000.0)
    ws = quote(OPT, 1000.5, source="WS")
    assert cache.put(ws)
    assert not cache.put(quote(OPT, 1000.2, source="REST"))    # a slow REST reply never overwrites a newer tick
    assert cache.last(OPT) is ws
    rest = quote(OPT, 1000.9, bids=((9.5, 650),), source="REST")   # SONACOMS: frozen WS, REST shows the real bid
    assert cache.put(rest) and cache.last(OPT) is rest
    same = quote(OPT, 1000.9, source="WS")
    assert cache.put(same) and cache.last(OPT) is same          # same receipt instant: the later put wins


# ---------------------------------------------------------------------------------------------------------------
# Tick parsing
# ---------------------------------------------------------------------------------------------------------------
def test_full_tick_parsing():
    mono, wall = 2000.0, at("10:15:55.250")
    q = quote_from_tick(option_tick(), OPT, mono, wall)
    assert (q.symbol, q.token, q.source, q.recv_mono, q.recv_wall) == (OPT, OPT_TOK, "WS", mono, wall)
    assert q.bids == (Level(84.0, 650, 3), Level(83.95, 1300, 5), Level(83.9, 65, 1))   # zero levels dropped
    assert q.asks == (Level(84.1, 130, 2), Level(84.15, 1950, 7))
    assert (q.bid, q.ask, q.mid(), q.ltp, q.volume) == (84.0, 84.1, pytest.approx(84.05), 84.0, 125100)
    assert q.last_trade_wall == datetime(2026, 10, 12, 10, 15, 54, tzinfo=IST)
    assert q.exch_wall == datetime(2026, 10, 12, 10, 15, 55, tzinfo=IST)
    assert q.last_trade_wall.utcoffset() == timedelta(hours=5, minutes=30)

    # quote-mode stock: an LTP-only quote plus an UnderlyingTick with the day open and previous close
    sq = quote_from_tick(stock_tick(), REL, mono, wall)
    assert (sq.bids, sq.asks, sq.ltp, sq.volume, sq.exch_wall) == ((), (), 2915.5, 1_234_567, None)
    u = underlying_from_tick(stock_tick(), REL, mono, wall)
    assert u == UnderlyingTick(REL, REL_TOK, 2915.5, 1_234_567, 2900.0, 2880.0, None, mono, wall)
    # index: no volume at all
    n = underlying_from_tick(index_tick(), NIFTY, mono, wall)
    assert (n.ltp, n.cum_volume, n.day_open, n.prev_close) == (25010.5, 0, 24950.0, 24900.0)
    # no trade yet: zero LTP / open become None, and no UnderlyingTick
    assert underlying_from_tick(stock_tick(ltp=0.0), REL, mono, wall) is None
    assert quote_from_tick(stock_tick(ltp=0.0), REL, mono, wall).ltp is None
    assert underlying_from_tick(stock_tick(day_open=0.0), REL, mono, wall).day_open is None
    # malformed values raise (the feed counts and drops the tick)
    for bad in ({"last_price": "84"}, {"volume_traded": -1}, {"instrument_token": True},
                {"depth": {"buy": [{"price": "x", "quantity": 1}]}}, {"depth": "full"}):
        with pytest.raises(ValueError):
            quote_from_tick({**option_tick(), **bad}, OPT, mono, wall)

    # through the feed: one batch -> cache, signal_q, dirty set
    feed, clock, cache, sq_, reactor, ticker, posted = make_feed()
    feed.on_ticks(ticker, [option_tick(), stock_tick(), index_tick(), {**stock_tick(), "last_price": "bad"},
                           {"instrument_token": 424242, "last_price": 1.0}])
    assert cache.fresh(OPT, 1.0).bid == 84.0 and cache.fresh(REL, 2.0).ltp == 2915.5
    assert cache.fresh(NIFTY, 2.0).ltp == 25010.5
    batch = sq_.get(timeout=0)
    assert [t.symbol for t in batch] == [REL, NIFTY]
    assert all(t.recv_mono == clock.mono() and t.recv_wall == clock.now_wall() for t in batch)
    assert feed.take_dirty() == frozenset({OPT})
    assert feed.stats()["ticks"] == 5 and feed.stats()["bad_ticks"] == 1 and feed.stats()["unknown_tokens"] == 1
    assert feed.health().last_underlying_tick_mono == clock.mono()


def test_tick_time_is_right_on_a_utc_host():
    """kiteconnect's naive fromtimestamp() value is host-local: on a UTC host .replace(tzinfo=IST) is 5.5 h off."""
    instant = datetime(2026, 10, 12, 10, 15, 54, tzinfo=IST)
    assert tick_time(datetime.fromtimestamp(instant.timestamp())) == instant
    assert tick_time(instant.astimezone(IST)) == instant
    assert tick_time(datetime(1970, 1, 1, 5, 30)) is None and tick_time(None) is None and tick_time(0) is None
    code = ("from datetime import datetime; from titan2.clock import IST; from titan2.market.feed import tick_time;"
            "i = datetime(2026, 10, 12, 10, 15, 54, tzinfo=IST); n = datetime.fromtimestamp(i.timestamp());"
            "assert n.hour == 4 and n.minute == 45, n; assert tick_time(n) == i, tick_time(n); print('ok')")
    env = {**os.environ, "TZ": "UTC0", "PYTHONPATH": str(ROOT)}
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr


def test_option_tick_without_depth_never_replaces_the_book():
    feed, clock, cache, sq, reactor, ticker, posted = make_feed()
    feed.on_ticks(ticker, [option_tick()])
    assert feed.take_dirty() == frozenset({OPT})
    clock.advance(0.5)
    no_depth = {k: v for k, v in option_tick(ltp=90.0).items() if k != "depth"}   # quote-mode packet of an option
    feed.on_ticks(ticker, [no_depth])
    assert cache.last(OPT).ltp == 84.0 and cache.last(OPT).bids        # the full book stays; it just ages out
    assert feed.take_dirty() == frozenset() and feed.stats()["no_depth"] == 1
    clock.advance(0.6)
    assert cache.fresh(OPT, 1.0) is None


def test_market_modules_import_without_twisted():
    code = ("import sys; sys.modules['twisted'] = None; sys.modules['kiteconnect'] = None;"
            "import titan2.market, titan2.market.quotes, titan2.market.bars, titan2.market.feed; print('ok')")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr


# ---------------------------------------------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------------------------------------------
def test_subscribe_via_call_from_thread():
    feed, clock, cache, sq, reactor, ticker, posted = make_feed()
    feed.request([REL_TOK, NIFTY_TOK], "quote")
    feed.request([OPT_TOK], "full")
    assert ticker.calls == []                              # nothing touches KiteTicker off the reactor thread
    assert len(reactor.pending) == 1                       # requests coalesce into one scheduled _apply
    reactor.run_pending()
    assert ticker.calls == []                              # not connected yet: on_connect applies everything

    reactor.on_reactor(feed.on_connect, ticker, None)
    assert ticker.methods() == [("subscribe", (sorted([OPT_TOK, NIFTY_TOK, REL_TOK]),)),
                                ("set_mode", ("full", [OPT_TOK])),
                                ("set_mode", ("quote", sorted([NIFTY_TOK, REL_TOK])))]
    ticker.calls.clear()

    feed.request([OPT2_TOK], "full")                       # from the core thread
    feed.request([NIFTY_TOK], None)                        # unsubscribe
    feed.request([REL_TOK], "full")                        # mode change only
    assert ticker.calls == []
    reactor.run_pending()
    assert ticker.methods() == [("unsubscribe", ([NIFTY_TOK],)), ("subscribe", ([OPT2_TOK],)),
                                ("set_mode", ("full", sorted([OPT2_TOK, REL_TOK])))]
    feed.request([REL_TOK], "full")                        # no change: nothing scheduled
    assert reactor.pending == []

    # a reconnect re-applies the whole desired set and drops tokens KiteTicker still lists but nobody wants
    ticker.calls.clear()
    reactor.on_reactor(feed.on_close, ticker, 1006, "connection lost")
    ticker.subscribed_tokens[999] = "quote"
    feed.request([OPT_TOK], None)                          # while down: recorded, applied on the next connect
    reactor.run_pending()
    assert ticker.calls == []
    reactor.on_reactor(feed.on_connect, ticker, None)
    assert ticker.methods() == [("unsubscribe", ([999, OPT_TOK],)), ("subscribe", (sorted([OPT2_TOK, REL_TOK]),)),
                                ("set_mode", ("full", sorted([OPT2_TOK, REL_TOK])))]
    assert all(thread == FakeReactor.THREAD for thread, _, _ in ticker.calls)
    assert feed.desired() == {OPT2_TOK: "full", REL_TOK: "full"}

    # engine commands route through the same path
    feed.dispatch(Subscribe((OPT_TOK,), "full"))
    feed.dispatch(Unsubscribe((OPT2_TOK,)))
    assert feed.desired() == {OPT_TOK: "full", REL_TOK: "full"} and len(reactor.pending) == 1
    with pytest.raises(TypeError):
        feed.dispatch(Timer(clock.now_wall(), clock.mono()))
    for tokens, mode in (([1], "ltp"), ([0], "full"), ([True], "full"), (["1"], "quote")):
        with pytest.raises(ValueError):
            feed.request(tokens, mode)


def test_on_connect_never_raises_and_reapplies_later():
    feed, clock, cache, sq, reactor, ticker, posted = make_feed(fail="subscribe")
    feed.request([REL_TOK], "quote")
    reactor.on_reactor(feed.on_connect, ticker, None)      # subscribe raises inside: logged, never propagated
    assert of(posted, FeedState) == [FeedState(True, False, False, None)]
    assert feed.stats()["subscribe_errors"] == 1
    ticker.fail = None
    reactor.on_reactor(feed.on_close, ticker, 1006, "x")
    reactor.on_reactor(feed.on_connect, ticker, None)
    assert ticker.methods()[-2:] == [("subscribe", ([REL_TOK],)), ("set_mode", ("quote", [REL_TOK]))]


def test_start_and_stop_use_the_reactor_thread():
    feed, clock, cache, sq, reactor, ticker, posted = make_feed()
    feed.start()
    assert ticker.methods() == [("connect", (True,))] and ticker.websocket_thread.name == WS_THREAD
    assert ticker.on_ticks == feed.on_ticks and ticker.on_order_update == feed.on_order_update
    ticker.calls.clear()
    feed.stop()
    assert ticker.calls == []
    reactor.run_pending()
    assert ticker.calls == [(FakeReactor.THREAD, "close", ())] and reactor.stopped


# ---------------------------------------------------------------------------------------------------------------
# Dirty set, connection state, order updates, signal_q
# ---------------------------------------------------------------------------------------------------------------
def test_dirty_set_coalesces():
    feed, clock, cache, sq, reactor, ticker, posted = make_feed()
    feed.on_ticks(ticker, [option_tick(), option_tick(OPT2_TOK)])
    feed.on_ticks(ticker, [option_tick(ltp=84.05)])
    feed.on_ticks(ticker, [stock_tick()])                  # underlyings are never dirty
    assert len(of(posted, QuotesDirty)) == 1               # one event until the core takes the set
    assert feed.take_dirty() == frozenset({OPT, OPT2})
    assert feed.take_dirty() == frozenset()
    feed.on_ticks(ticker, [option_tick()])
    assert len(of(posted, QuotesDirty)) == 2
    assert feed.take_dirty() == frozenset({OPT})

    # concurrent producer (kite-ws) and consumer (core): no symbol is ever lost
    feed, clock, cache, sq, reactor, ticker, _ = make_feed()
    events: queue.Queue = queue.Queue()
    feed._post = events.put
    symbols = {OPT_TOK + i: f"NFO:SYM{i}" for i in range(400)}
    feed.set_symbol_resolver(symbols.get)
    seen: set[str] = set()
    done = threading.Event()

    def core():
        while not (done.is_set() and events.empty()):
            try:
                ev = events.get(timeout=0.01)
            except queue.Empty:
                continue
            if isinstance(ev, QuotesDirty):
                seen.update(feed.take_dirty())
    consumer = threading.Thread(target=core)
    consumer.start()
    for tok in symbols:
        feed.on_ticks(ticker, [option_tick(tok)])
    done.set()
    consumer.join(10)
    seen.update(feed.take_dirty())
    assert seen == set(symbols.values())


def test_connection_state_events_and_links():
    feed, clock, cache, sq, reactor, ticker, posted = make_feed()
    assert feed.health().connected is False
    feed.on_connect(ticker, None)
    assert posted == [FeedState(True, False, False, None)]
    h = feed.health()
    assert (h.connected, h.connected_since_mono, h.reconnects, h.degraded) == (True, clock.mono(), 0, False)
    assert sq.get(timeout=0) == FeedLink(True, False, clock.now_wall(), clock.mono())
    clock.advance(5)
    feed.on_close(ticker, 1006, "gone")
    feed.on_close(ticker, 1006, "gone")                    # no duplicate events for one drop
    assert posted[1:] == [FeedState(False, False, False, "closed 1006: gone")]
    assert sq.get(timeout=0) == FeedLink(False, False, clock.now_wall(), clock.mono(), "closed 1006: gone")
    assert feed.health().connected is False and feed.health().connected_since_mono is None
    clock.advance(3)
    feed.on_connect(ticker, None)
    assert posted[-1] == FeedState(True, True, False, None)
    assert feed.health().reconnects == 1 and feed.health().connected_since_mono == clock.mono()
    assert sq.get(timeout=0).reconnected is True
    feed.on_close(ticker, None, None)
    feed.on_noreconnect(ticker)                            # always reported, even when already down
    assert posted[-1] == FeedState(False, False, False, "NORECONNECT")
    feed.on_error(ticker, 1006, "boom")
    feed.on_reconnect(ticker, 2)
    assert feed.stats()["ws_errors"] == 1


def test_order_update_posts_ws_snapshot():
    feed, clock, cache, sq, reactor, ticker, posted = make_feed()
    tag = make_tag(Role.EXIT, Account.LIVE, SESSION, 1, 2)
    data = {"order_id": "251012000000123", "status": "COMPLETE", "tradingsymbol": OPT[4:], "exchange": "NFO",
            "transaction_type": "SELL", "quantity": 65, "filled_quantity": 65, "average_price": 84.05, "tag": tag,
            "product": "MIS", "order_type": "LIMIT", "validity": "IOC", "status_message": None,
            "order_timestamp": "2026-10-12 10:15:54", "exchange_update_timestamp": "2026-10-12 10:15:55",
            "placed_by": "AB1234"}
    feed.on_order_update(ticker, data)
    assert posted == [OrderSnapshot(Account.LIVE, "251012000000123", tag, "COMPLETE", 65, 84.05, None,
                                    datetime(2026, 10, 12, 10, 15, 55, tzinfo=IST), "WS", clock.mono())]
    feed.on_order_update(ticker, {**data, "filled_quantity": 70})     # malformed: dropped, the poll timers recover
    feed.on_order_update(ticker, "garbage")
    assert len(posted) == 1 and feed.stats()["order_update_errors"] == 2


def test_signal_queue_drops_oldest_batch_never_links():
    sq = SignalQueue(max_batches=3)
    b = [(ut(REL, at(f"10:00:0{i}"), 100.0 + i, 10 * i),) for i in range(5)]
    link = FeedLink(False, False, at("09:59:59"), 1.0, "x")
    sq.put_link(link)
    assert all(sq.put_ticks(x) for x in b[:3]) and sq.qsize() == 3
    assert sq.put_ticks(b[3]) is False and sq.put_ticks(b[4]) is False
    assert sq.dropped == 2 and sq.qsize() == 3
    assert sq.get(timeout=0) == SignalsLost(at("10:00:00"), at("10:00:01"), 2)   # reported before the next item
    assert sq.get(timeout=0) is link                                               # links are never dropped
    assert [sq.get(timeout=0) for _ in range(3)] == [b[2], b[3], b[4]]
    with pytest.raises(queue.Empty):
        sq.get(timeout=0.01)
    with pytest.raises(ValueError):
        sq.put_ticks(())
    with pytest.raises(TypeError):
        sq.put_link("connected")


def test_signal_queue_overflow_marks_feed_degraded():
    feed, clock, cache, sq, reactor, ticker, posted = make_feed(max_batches=10)
    feed.on_connect(ticker, None)
    sq.get(timeout=0)                                      # the FeedLink
    for i in range(11):
        feed.on_ticks(ticker, [stock_tick(cum=1000 + i)])
    assert of(posted, FeedState)[-1] == FeedState(True, False, True, "SIGNAL_Q_OVERFLOW")
    assert feed.health().degraded is True and feed.stats()["signal_batches_dropped"] == 1
    for _ in range(9):
        sq.get(timeout=0)                                  # the SignalsLost marker, then 8 of the 10 batches
    feed.on_ticks(ticker, [stock_tick(cum=4000)])          # backlog 3 > 10 % of 10: still degraded
    assert feed.health().degraded is True
    for _ in range(3):
        sq.get(timeout=0)
    feed.on_ticks(ticker, [stock_tick(cum=5000)])          # backlog back to 1 <= 10 %: recovered
    assert of(posted, FeedState)[-1] == FeedState(True, False, False, "SIGNAL_Q_RECOVERED")
    assert feed.health().degraded is False
    assert len(of(posted, FeedState)) == 3


# ---------------------------------------------------------------------------------------------------------------
# Bars
# ---------------------------------------------------------------------------------------------------------------
def test_bar_0915_gap_invalid():
    bars = Bars(OPEN, CLOSE)
    bars.on_link(True, at("09:05:00"))                     # watched from before the open
    for sym in (REL, "NSE:SBIN"):
        bars.on_tick(ut(sym, at("09:08:30"), 2890.0, 5_000))   # pre-open: no bar, volume stays cumulative
    assert bars.on_tick(ut(REL, at("09:15:00.200"), 2905.0, 12_000, day_open=2900.0)) == []
    bars.on_tick(ut(REL, at("09:16:10"), 2950.0, 20_000, day_open=2900.0))
    bars.on_tick(ut(REL, at("09:17:00"), 2895.0, 26_000, day_open=2900.0))
    bars.on_tick(ut(REL, at("09:19:59.900"), 2960.0, 31_000, day_open=2900.0))
    bars.on_tick(ut("NSE:SBIN", at("09:16:00"), 801.0, 9_000, day_open=800.0))
    bars.on_tick(ut("NSE:SBIN", at("09:18:00"), 805.0, 12_000))                    # ticks without ohlc.open are fine
    closed = bars.on_tick(ut(REL, at("09:20:00"), 2961.0, 31_500, day_open=2900.0))  # the first item >= 09:20 closes
    rel = Bar(REL, OPEN, 2900.0, 2960.0, 2895.0, 2960.0, 31_000, True, LIVE)      # open = ohlc.open, vol = cumulative
    assert closed == [rel, Bar("NSE:SBIN", OPEN, 800.0, 805.0, 800.0, 805.0, 12_000, True, LIVE)]   # by symbol
    assert bars.first_bar(REL) == rel and bars.bars(REL, 5) == (rel,)

    # a disconnect at any moment of 09:15:00-09:20:00 invalidates the bar
    bars = Bars(OPEN, CLOSE)
    bars.on_link(True, at("09:05:00"))
    bars.on_tick(ut(REL, at("09:15:01"), 2905.0, 12_000, day_open=2900.0))
    bars.on_link(False, at("09:17:30"))
    bars.on_link(True, at("09:17:35"))
    bars.on_tick(ut(REL, at("09:19:00"), 2930.0, 22_000, day_open=2900.0))
    [bar] = bars.close_due(at("09:20:00"))
    assert bar.valid is False and bars.why_invalid(REL, OPEN) == FEED_GAP
    assert (bar.open, bar.close, bar.volume) == (2900.0, 2930.0, 22_000)

    # not watched from the open (bot started 09:16), or still down: invalid
    for links in ([(True, "09:16:00")], [(True, "09:05:00"), (False, "09:14:00")], []):
        bars = Bars(OPEN, CLOSE)
        for connected, hms in links:
            bars.on_link(connected, at(hms))
        bars.on_tick(ut(REL, at("09:16:30"), 2905.0, 12_000, day_open=2900.0))
        [bar] = bars.close_due(at("09:20:00"))
        assert bar.valid is False and bars.why_invalid(REL, OPEN) == FEED_GAP

    # no day open, a changed day open, or no ticks at all
    bars = Bars(OPEN, CLOSE)
    bars.on_link(True, at("09:00:00"))
    bars.on_tick(ut(REL, at("09:16:00"), 2905.0, 12_000))
    bars.on_tick(ut("NSE:SBIN", at("09:16:00"), 801.0, 9_000, day_open=800.0))
    bars.on_tick(ut("NSE:SBIN", at("09:17:00"), 802.0, 9_500, day_open=799.0))
    rel, sbin = bars.close_due(at("09:20:00.001"))
    assert (rel.valid, bars.why_invalid(REL, OPEN), rel.open) == (False, NO_DAY_OPEN, 2905.0)
    assert (sbin.valid, bars.why_invalid("NSE:SBIN", OPEN)) == (False, DAY_OPEN_CHANGED)
    assert bars.first_bar("NSE:TCS") is None               # no ticks, no bar: no signal


def test_bars_volume_delta_needs_an_unbroken_baseline():
    bars = Bars(OPEN, CLOSE)
    bars.on_link(True, at("09:00:00"))
    bars.on_tick(ut(REL, at("09:19:00"), 100.0, 31_000, day_open=99.0))
    [first] = bars.on_tick(ut(REL, at("09:20:00"), 101.0, 31_500))          # closes the 09:15 bar
    bars.on_tick(ut(REL, at("09:23:00"), 103.0, 36_000))
    bars.on_tick(ut(REL, at("09:24:59"), 102.0, 40_000))
    [b0920] = bars.close_due(at("09:30:00"))
    assert first.start == OPEN and first.volume == 31_000
    assert b0920 == Bar(REL, at("09:20:00"), 101.0, 103.0, 101.0, 102.0, 9_000, True, LIVE)   # 40,000 - 31,000

    # T: last tick 09:21 (09:20 bar), feed down 09:22-09:23, next tick 09:27 -> the 09:25 bar's volume delta spans
    # the gap, so it is invalid although the gap lies before the bar started
    bars = Bars(OPEN, CLOSE)
    bars.on_link(True, at("09:00:00"))
    bars.on_tick(ut(REL, at("09:21:00"), 100.0, 1_000))
    bars.on_link(False, at("09:22:00"))
    bars.on_link(True, at("09:23:00"))
    bars.on_tick(ut(REL, at("09:27:00"), 101.0, 1_800))
    bars.on_tick(ut("NSE:SBIN", at("09:27:00"), 800.0, 500))
    b0920 = bars.bars(REL, 1)[0]
    # no baseline AND overlapping the gap: the gap is the reported reason
    assert (b0920.start, b0920.valid, bars.why_invalid(REL, b0920.start)) == (at("09:20:00"), False, FEED_GAP)
    rel, sbin = bars.close_due(at("09:30:00"))
    assert (rel.volume, rel.valid, bars.why_invalid(REL, at("09:25:00"))) == (800, False, FEED_GAP)
    assert (sbin.valid, bars.why_invalid("NSE:SBIN", at("09:25:00"))) == (False, NO_VOLUME_BASE)

    # cumulative volume running backwards
    bars = Bars(OPEN, CLOSE)
    bars.on_link(True, at("09:00:00"))
    bars.on_tick(ut(REL, at("09:21:00"), 100.0, 5_000))
    bars.on_tick(ut(REL, at("09:26:00"), 100.0, 4_000))
    [bar] = bars.close_due(at("09:30:00"))
    assert bar.valid is False and bar.volume == 0


def test_bars_late_tick_and_late_gap_correct_closed_bars():
    bars = Bars(OPEN, CLOSE)
    bars.on_link(True, at("09:00:00"))
    bars.on_tick(ut(REL, at("09:16:00"), 100.0, 1_000, day_open=100.0))
    [bar] = bars.close_due(at("09:20:00"))
    assert bar.valid
    # a tick stamped before 09:20 that the signals thread sees only now: the closed bar is re-emitted invalid
    [fix] = bars.on_tick(ut(REL, at("09:19:59.990"), 101.0, 1_100, day_open=100.0))
    assert fix == replace(bar, valid=False) and bars.why_invalid(REL, OPEN) == LATE_TICK
    assert bars.late_ticks == 1 and bars.bars(REL, 1) == (fix,)
    assert bars.on_tick(ut(REL, at("09:19:59.995"), 101.0, 1_100)) == []   # already invalid: no repeat

    # ticks lost on signal_q overflow inside an already closed bar
    bars = Bars(OPEN, CLOSE)
    bars.on_link(True, at("09:00:00"))
    bars.on_tick(ut(REL, at("09:16:00"), 100.0, 1_000, day_open=100.0))
    bars.on_tick(ut(REL, at("09:21:00"), 100.5, 1_500))
    [b0915] = bars.close_due(at("09:20:00.5")) or bars.bars(REL, 1)
    assert bars.on_gap(at("09:18:00"), at("09:18:01")) == [replace(b0915, valid=False)]
    assert bars.why_invalid(REL, OPEN) == FEED_GAP
    [b0920] = bars.close_due(at("09:25:00"))
    assert b0920.valid is False                            # its baseline (09:16) predates the lost ticks


def test_bars_session_close_and_prev_session():
    cal = SessionCalendar.from_dict({
        "years": {"2026": {"holidays": ["2026-10-02"]}},
        "regular_hours": [{"from": "2026-01-01", "open": "09:15", "close": "15:30"},
                          {"from": "2026-08-03", "open": "09:15", "close": "15:40"}]})
    bars = Bars.for_session(cal, SESSION)
    assert (bars.session_open, bars.session_close) == (OPEN, CLOSE)
    assert Bars.for_session(cal, date(2026, 10, 11)) is None                  # Sunday
    assert prev_session_window(cal, date(2026, 10, 5)) == (at("09:15:00", date(2026, 10, 1)),
                                                            at("15:40:00", date(2026, 10, 1)))   # skips Oct 2
    assert prev_session_window(cal, date(2026, 8, 3))[1] == at("15:30:00", date(2026, 7, 31))  # old close

    bars.on_link(True, at("09:00:00"))
    bars.on_tick(ut(REL, at("15:30:10"), 100.0, 1_000))
    bars.on_tick(ut(REL, at("15:39:59"), 101.0, 1_200))
    assert [b.start for b in bars.bars(REL, 5)] == [at("15:30:00")]
    [last] = bars.on_tick(ut(REL, at("15:40:00"), 102.0, 1_300))           # after the close: closes, builds nothing
    assert last.start == at("15:35:00") and last.close == 101.0
    assert bars.close_due(at("15:45:00")) == []

    short = Bars(OPEN, at("10:02:00"))                     # a special session that ends off the 5-minute grid
    short.on_link(True, at("09:00:00"))
    short.on_tick(ut(REL, at("10:01:00"), 100.0, 10))
    assert short.close_due(at("10:01:59")) == [] and len(short.close_due(at("10:02:00"))) == 1
    with pytest.raises(ValueError):
        Bars(OPEN, OPEN)

    hist = [Bar(REL, at("09:15:00", date(2026, 10, 9)) + timedelta(minutes=5 * i), 100.0, 101.0, 99.0, 100.5,
                1_000 + i, True, HIST) for i in range(76)]
    bars.set_prev_session(REL, reversed(hist))
    last20 = bars.prev_session_bars(REL, 20)
    assert last20 == tuple(hist[-20:]) and last20[0].start < last20[-1].start
    assert bars.prev_session_bars("NSE:SBIN", 20) == () and bars.prev_session_bars(REL, 0) == ()
    with pytest.raises(ValueError):
        bars.set_prev_session(REL, [replace(hist[0], source=LIVE)])
    assert find_bar(hist, at("09:20:00", date(2026, 10, 9))) == hist[1] and find_bar(hist, OPEN) is None


# ---------------------------------------------------------------------------------------------------------------
# Bar check
# ---------------------------------------------------------------------------------------------------------------
def test_bar_check_tolerances():
    live = Bar(REL, OPEN, 100.0, 103.0, 99.0, 102.0, 10_000, True, LIVE)
    hist = replace(live, source=HIST)
    assert check_bar(live, hist).ok and check_bar(live, hist).reasons == ()
    # open and close: within 0.1 % passes (boundary included), beyond fails
    for field, ok_vals, bad_vals in (("open", (100.1, 99.9), (100.11, 99.89)),
                                     ("close", (102.102, 101.898), (102.11, 101.89))):
        for v in ok_vals:
            assert check_bar(replace(live, **{field: v}), hist).ok, (field, v)
        for v in bad_vals:
            c = check_bar(replace(live, **{field: v}), hist)
            assert not c.ok and c.reasons == (field.upper(),), (field, v, c)
    # volume: within 5 %
    for v in (10_500, 9_500, 10_000):
        assert check_bar(replace(live, volume=v), hist).ok
    for v in (10_501, 9_499, 0):
        assert check_bar(replace(live, volume=v), hist).reasons == ("VOLUME",)
    zero = replace(hist, volume=0)
    assert check_bar(replace(live, volume=0), zero).ok and not check_bar(live, zero).ok
    # missing or invalid bars never pass
    assert check_bar(None, hist).reasons == ("LIVE_MISSING",) and check_bar(live, None).reasons == ("HIST_MISSING",)
    assert check_bar(None, None).ok is False
    assert check_bar(replace(live, valid=False), hist).reasons == ("LIVE_INVALID",)
    assert check_bar(live, replace(hist, valid=False)).reasons == ("HIST_INVALID",)
    assert "NOT_THE_SAME_BAR" in check_bar(live, replace(hist, start=at("09:20:00"))).reasons
    # several reasons at once, and a JSON detail (inf -> null)
    c = check_bar(replace(live, open=101.0, volume=20_000), zero)
    assert c.reasons == ("OPEN", "VOLUME")
    d = json.loads(c.detail())
    assert d["reasons"] == ["OPEN", "VOLUME"] and d["volume_dev"] is None and d["open_dev"] == pytest.approx(0.01)


def test_bar_check_sample_is_seeded():
    universe = [f"NSE:S{i:03d}" for i in range(150)]
    fired = ["NSE:S007", "NSE:S100"]
    a = bar_check_symbols(fired, universe, SESSION)
    assert a == bar_check_symbols(list(reversed(fired)), reversed(universe), SESSION)   # deterministic
    assert a[:2] == ("NSE:S007", "NSE:S100") and len(a) == 2 + BAR_CHECK_SAMPLE and len(set(a)) == len(a)
    assert a != bar_check_symbols(fired, universe, SESSION + timedelta(days=1))
    assert bar_check_symbols([], universe[:5], SESSION) == tuple(universe[:5])
