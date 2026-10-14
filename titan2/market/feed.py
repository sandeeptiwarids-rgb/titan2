"""The KiteTicker feed: the kite-ws side of market data (spec §3.2, §9.7; DETAIL §3.2, §9.7).

Threads
- kite-ws (the Twisted reactor started by KiteTicker.connect(threaded=True)) runs every callback here: on_ticks,
  on_order_update and the connection callbacks. They only update shared containers and enqueue: no blocking, no
  I/O, and they never raise (autobahn drops the connection when on_connect raises).
- Any thread may call request() / dispatch(), take_dirty(), health(), stats() and stop().

Subscriptions
- request(tokens, mode) updates desired: dict[token, mode] under a lock and schedules _apply with
  reactor.callFromThread. KiteTicker methods (subscribe, set_mode, unsubscribe, close) run only on the reactor
  thread: in kiteconnect 5.0.1 they call ws.sendMessage directly, which is not thread-safe.
- _apply diffs desired against what this connection has applied. on_connect re-applies the whole desired set (a
  new connection has none, and autobahn silently drops a send on a closed connection), after unsubscribing tokens
  KiteTicker still lists but nobody wants, so that its own resubscribe() on reconnect cannot revive them.

Ticks (one recv_mono / recv_wall stamp per on_ticks call; every age is a receipt age)
- symbol_of(token) names the instrument (the runtime binds InstrumentStore.symbol_for_token plus held legs);
  "NFO:..." is an option, anything else an underlying. Unknown tokens are counted and dropped.
- An option tick updates the QuoteCache only in full mode. A packet without depth is never cached, so a book is
  never replaced by an empty one (that would read as "no bid"). Every cached option symbol joins the dirty set
  (only held or pending options are subscribed); the first addition after the core last took the set posts one
  QuotesDirty.
- An underlying tick updates the QuoteCache (ltp; depth if present) and becomes an UnderlyingTick. Each on_ticks
  call's UnderlyingTicks go to signal_q as one batch.
- kiteconnect stamps last_trade_time / exchange_timestamp with datetime.fromtimestamp(): naive *host-local* time.
  astimezone(IST) converts that correctly whatever the server's time zone (EC2 usually runs UTC).

signal_q (SignalQueue) holds 20,000 tick batches. On overflow the oldest batch is dropped, the consumer gets a
SignalsLost marker spanning its receipt times, and the feed reports degraded (FeedState) until the backlog is back
under 10 %. FeedLink markers (connect / disconnect, in receipt order with the ticks) are never dropped. The signals
thread passes them to Bars.on_link / Bars.on_gap, and calls every strategy's on_feed_reset() on a reconnect or a
loss.

Posted to core_q: FeedState (connect, disconnect, no-reconnect, degraded changes), QuotesDirty, and
OrderSnapshot(LIVE, source "WS") for every order update, normalized exactly like a REST order row.
"""

# Ported from agentic_trader/kite_ticker.py:274-414 @b4694f3 — changes: no instruments download (tokens resolve
#   through the injected symbol_of); subscriptions are a desired map applied on the reactor thread through
#   reactor.callFromThread, never from the caller's thread; the WS thread is KiteTicker's own reactor thread
# Ported from agentic_trader/kite_ticker.py:645-689 @b4694f3 — changes: ticks become immutable Quote /
#   UnderlyingTick values; no lock is held across detection; no watcher, OI sampler or file I/O on this thread;
#   a tick without depth never replaces an option book
# Ported from agentic_trader/kite_ticker.py:733-796 @b4694f3 — changes: on_connect re-applies the desired set by
#   mode; connection changes post FeedState and a FeedLink marker; order updates post OrderSnapshot(LIVE, WS)
#   instead of filling a list the main loop polls

from __future__ import annotations

import logging
import math
import queue
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping

from titan2.broker.kite import order_from_kite
from titan2.broker.port import snapshot_of
from titan2.clock import IST, Clock, RealClock
from titan2.core.events import FEED_MODES, Command, Event, FeedState, QuotesDirty, Subscribe, Unsubscribe
from titan2.market.quotes import QuoteCache
from titan2.model import Account, FeedHealth, Level, Quote, UnderlyingTick

log = logging.getLogger(__name__)

WS_THREAD = "kite-ws"
SIGNAL_Q_MAX = 20_000                  # tick batches (§3.2)
SIGNAL_Q_RECOVER_FRAC = 0.10           # degraded clears once the backlog is at or below 10 % of the bound
OPTION_PREFIX = "NFO:"
_LOG_FIRST = 5                         # anomalies: log the first few of each kind, then only count


# ---------------------------------------------------------------------------------------------------------------
# Tick parsing (pure)
# ---------------------------------------------------------------------------------------------------------------
def _price(v: Any) -> float | None:
    """A finite number >= 0, else None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        return None
    return float(v)


def _count(v: Any) -> int | None:
    """An int >= 0, else None."""
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        return None
    return v


def tick_time(v: Any) -> datetime | None:
    """A KiteTicker timestamp -> IST-aware datetime; None when missing or epoch-0 ("no trade yet").

    kiteconnect builds it with datetime.fromtimestamp(), a naive host-local time; astimezone() reads a naive value
    as host-local, so the conversion is right on any server time zone.
    """
    if not isinstance(v, datetime) or v.year < 2000:
        return None
    try:
        return v.astimezone(IST)
    except (OverflowError, OSError, ValueError):
        return None


def _token(tick: Any) -> int:
    token = tick.get("instrument_token") if isinstance(tick, Mapping) else None
    if isinstance(token, bool) or not isinstance(token, int) or token <= 0:
        raise ValueError(f"tick without a valid instrument_token: {tick!r}")
    return token


def _levels(side: Any, what: str) -> tuple[Level, ...]:
    if side is None:
        return ()
    if not isinstance(side, (list, tuple)):
        raise ValueError(f"{what}: expected a list of levels, got {type(side).__name__}")
    out = []
    for lv in side:
        if not isinstance(lv, Mapping):
            raise ValueError(f"{what}: bad level {lv!r}")
        px, qty, orders = _price(lv.get("price")), _count(lv.get("quantity")), _count(lv.get("orders", 0))
        if px is None or qty is None or orders is None:
            raise ValueError(f"{what}: bad level {lv!r}")
        out.append(Level(px, qty, orders))
    return tuple(out)                  # Quote drops empty (zero) levels and sorts best first


def quote_from_tick(tick: Mapping, symbol: str, recv_mono: float, recv_wall: datetime) -> Quote:
    """One KiteTicker tick -> Quote(source="WS"). depth.buy are the bids, depth.sell the asks; outside full mode
    there is no depth and the book is empty. Raises ValueError for a malformed tick."""
    token = _token(tick)
    ltp_raw, vol_raw, depth = tick.get("last_price"), tick.get("volume_traded"), tick.get("depth")
    ltp = None if ltp_raw is None else _price(ltp_raw)
    if ltp_raw is not None and ltp is None:
        raise ValueError(f"{symbol}: bad last_price {ltp_raw!r}")
    volume = None if vol_raw is None else _count(vol_raw)
    if vol_raw is not None and volume is None:
        raise ValueError(f"{symbol}: bad volume_traded {vol_raw!r}")
    bids = asks = ()
    if depth is not None:
        if not isinstance(depth, Mapping):
            raise ValueError(f"{symbol}: bad depth {depth!r}")
        bids = _levels(depth.get("buy"), f"{symbol} depth.buy")
        asks = _levels(depth.get("sell"), f"{symbol} depth.sell")
    return Quote(symbol, token, bids, asks, ltp or None, volume, tick_time(tick.get("last_trade_time")),
                 tick_time(tick.get("exchange_timestamp")), recv_mono, recv_wall, "WS")


def underlying_from_tick(tick: Mapping, symbol: str, recv_mono: float, recv_wall: datetime) -> UnderlyingTick | None:
    """One KiteTicker tick of a stock or index -> UnderlyingTick; None when it has no positive LTP.

    cum_volume is volume_traded (0 for indices, which have none); day_open / prev_close are ohlc.open / ohlc.close
    when positive. Raises ValueError for a malformed tick.
    """
    token = _token(tick)
    ltp = _price(tick.get("last_price"))
    if not ltp:
        return None
    vol_raw = tick.get("volume_traded", 0)
    cum = _count(vol_raw)
    if cum is None:
        raise ValueError(f"{symbol}: bad volume_traded {vol_raw!r}")
    ohlc = tick.get("ohlc") or {}
    if not isinstance(ohlc, Mapping):
        raise ValueError(f"{symbol}: bad ohlc {ohlc!r}")
    return UnderlyingTick(symbol, token, ltp, cum, _price(ohlc.get("open")) or None,
                          _price(ohlc.get("close")) or None, tick_time(tick.get("exchange_timestamp")),
                          recv_mono, recv_wall)


# ---------------------------------------------------------------------------------------------------------------
# signal_q
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class FeedLink:
    """signal_q marker: the WS connected or disconnected (in receipt order with the tick batches)."""

    connected: bool
    reconnected: bool
    recv_wall: datetime
    recv_mono: float
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class SignalsLost:
    """signal_q marker: tick batches received in [first_wall, last_wall] were dropped on overflow."""

    first_wall: datetime
    last_wall: datetime
    batches: int


class SignalQueue:
    """kite-ws -> signals: UnderlyingTick batches plus FeedLink markers, bounded at max_batches tick batches.

    On overflow the oldest tick batch is dropped and put_ticks() returns False; get() then returns one SignalsLost
    covering every batch dropped since the last get(), before the next item. FeedLink markers are never dropped.
    """

    def __init__(self, max_batches: int = SIGNAL_Q_MAX) -> None:
        if isinstance(max_batches, bool) or not isinstance(max_batches, int) or max_batches < 1:
            raise ValueError(f"max_batches must be an int >= 1, got {max_batches!r}")
        self.max_batches = max_batches
        self._items: deque = deque()
        self._batches = 0
        self._lost: tuple[datetime, datetime, int] | None = None
        self._cv = threading.Condition()
        self.dropped = 0

    def put_ticks(self, batch: tuple[UnderlyingTick, ...]) -> bool:
        if not isinstance(batch, tuple) or not batch:
            raise ValueError("a tick batch is a non-empty tuple of UnderlyingTick")
        with self._cv:
            kept = self._batches < self.max_batches
            if not kept:
                self._drop_oldest_batch()
            self._items.append(batch)
            self._batches += 1
            self._cv.notify()
        return kept

    def put_link(self, link: FeedLink) -> None:
        if not isinstance(link, FeedLink):
            raise TypeError(f"put_link needs a FeedLink, got {type(link).__name__}")
        with self._cv:
            self._items.append(link)
            self._cv.notify()

    def get(self, timeout: float | None = None) -> tuple[UnderlyingTick, ...] | FeedLink | SignalsLost:
        """The next item; raises queue.Empty after timeout seconds (None: wait forever)."""
        with self._cv:
            if not self._cv.wait_for(lambda: bool(self._items) or self._lost is not None, timeout):
                raise queue.Empty
            if self._lost is not None:
                lost, self._lost = SignalsLost(*self._lost), None
                return lost
            item = self._items.popleft()
            if isinstance(item, tuple):
                self._batches -= 1
            return item

    def qsize(self) -> int:
        """Tick batches waiting."""
        with self._cv:
            return self._batches

    def _drop_oldest_batch(self) -> None:
        i = next(i for i, item in enumerate(self._items) if isinstance(item, tuple))
        batch = self._items[i]
        del self._items[i]
        self._batches -= 1
        self.dropped += 1
        lo, hi = min(t.recv_wall for t in batch), max(t.recv_wall for t in batch)
        if self._lost is not None:
            lo, hi = min(lo, self._lost[0]), max(hi, self._lost[1])
        self._lost = (lo, hi, 1 if self._lost is None else self._lost[2] + 1)


# ---------------------------------------------------------------------------------------------------------------
# Feed
# ---------------------------------------------------------------------------------------------------------------
class Feed:
    """KiteTicker wrapper. ticker is a kiteconnect.KiteTicker built by the runtime (it holds the session token);
    reactor defaults to twisted's global reactor, imported only when first needed."""

    def __init__(self, ticker: Any, quotes: QuoteCache, post: Callable[[Event], None], signal_q: SignalQueue,
                 symbol_of: Callable[[int], str | None], *, clock: Clock | None = None, reactor: Any = None) -> None:
        self._ticker = ticker
        self._quotes = quotes
        self._post = post
        self._signal_q = signal_q
        self._symbol_of = symbol_of
        self._clock = clock if clock is not None else RealClock()
        self._reactor_obj = reactor
        self._lock = threading.Lock()                # desired, apply flag, health
        self._desired: dict[int, str] = {}
        self._apply_pending = False
        self._connected = False
        self._ever_connected = False
        self._connected_since: float | None = None
        self._last_underlying: float | None = None
        self._reconnects = 0
        self._degraded = False
        self._applied: dict[int, str] = {}          # reactor thread only: this connection's subscriptions
        self._dirty_lock = threading.Lock()
        self._dirty: set[str] = set()
        self._dirty_posted = False
        self._stats: dict[str, int] = {}            # written on the reactor thread only

    # -- any thread -----------------------------------------------------------------------------------------
    def set_symbol_resolver(self, symbol_of: Callable[[int], str | None]) -> None:
        """Swap the token -> symbol resolver (e.g. once the day's InstrumentStore is loaded)."""
        self._symbol_of = symbol_of

    def request(self, tokens: Iterable[int], mode: str | None) -> None:
        """Want tokens in mode ("quote" | "full"), or not at all (mode None). Applied on the reactor thread."""
        if mode is not None and mode not in FEED_MODES:
            raise ValueError(f"feed mode must be one of {FEED_MODES} or None, got {mode!r}")
        toks = []
        for tok in tokens:
            if isinstance(tok, bool) or not isinstance(tok, int) or tok <= 0:
                raise ValueError(f"bad instrument token {tok!r}")
            toks.append(tok)
        with self._lock:
            changed = False
            for tok in toks:
                if mode is None:
                    changed |= self._desired.pop(tok, None) is not None
                elif self._desired.get(tok) != mode:
                    self._desired[tok] = mode
                    changed = True
            if not changed or self._apply_pending:
                return
            self._apply_pending = True
        try:
            self._reactor().callFromThread(self._apply)
        except Exception:
            with self._lock:
                self._apply_pending = False
            raise

    def dispatch(self, cmd: Command) -> None:
        """Route an engine Subscribe / Unsubscribe command."""
        if isinstance(cmd, Subscribe):
            self.request(cmd.tokens, cmd.mode)
        elif isinstance(cmd, Unsubscribe):
            self.request(cmd.tokens, None)
        else:
            raise TypeError(f"the feed cannot run {type(cmd).__name__}")

    def desired(self) -> dict[int, str]:
        with self._lock:
            return dict(self._desired)

    def take_dirty(self) -> frozenset[str]:
        """Option symbols with new WS quotes since the last call (core thread, on QuotesDirty)."""
        with self._dirty_lock:
            out = frozenset(self._dirty)
            self._dirty.clear()
            self._dirty_posted = False
        return out

    def health(self) -> FeedHealth:
        with self._lock:
            return FeedHealth(self._connected, self._connected_since, self._last_underlying, self._reconnects,
                              self._degraded)

    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def start(self) -> None:
        """Register the callbacks and connect (KiteTicker starts the reactor in its own thread, named kite-ws).

        A reactor that is already running belongs to an earlier run in this process (the laptop runner restarts the
        bot in-process, and a Twisted reactor cannot be started twice): KiteTicker then starts no thread, and its
        connect (connectWS) must run on the reactor thread, so it goes through callFromThread."""
        t = self._ticker
        t.on_ticks = self.on_ticks
        t.on_connect = self.on_connect
        t.on_close = self.on_close
        t.on_error = self.on_error
        t.on_reconnect = self.on_reconnect
        t.on_noreconnect = self.on_noreconnect
        t.on_order_update = self.on_order_update
        if getattr(self._reactor(), "running", False) is True:
            self._reactor().callFromThread(t.connect, threaded=True)
            return
        t.connect(threaded=True)
        thread = getattr(t, "websocket_thread", None)
        if isinstance(thread, threading.Thread):
            thread.name = WS_THREAD

    def stop(self, stop_reactor: bool = True) -> None:
        """Close the WS without reconnecting, on the reactor thread; then stop the reactor unless told not to."""
        def close() -> None:
            try:
                self._ticker.close()
            except Exception:
                log.exception("feed: closing the ticker failed")
            if stop_reactor:
                try:
                    self._reactor().stop()
                except Exception:
                    log.exception("feed: stopping the reactor failed")
        self._reactor().callFromThread(close)

    # -- reactor thread: subscriptions ------------------------------------------------------------------------
    def _reactor(self) -> Any:
        if self._reactor_obj is None:
            from twisted.internet import reactor  # lazy: twisted is a Linux runtime dependency; tests inject a fake
            self._reactor_obj = reactor
        return self._reactor_obj

    def _apply(self) -> None:
        with self._lock:
            self._apply_pending = False
            want = dict(self._desired)
            connected = self._connected
        if connected:                                # otherwise on_connect applies the whole set
            self._sync(want)

    def _sync(self, want: Mapping[int, str]) -> None:
        """Make this connection's subscriptions equal want: unsubscribe, subscribe, then set_mode per mode."""
        t, applied = self._ticker, self._applied
        try:
            gone = sorted(tok for tok in applied if tok not in want)
            if gone:
                t.unsubscribe(gone)
                for tok in gone:
                    del applied[tok]
            new = sorted(tok for tok in want if tok not in applied)
            if new:
                t.subscribe(new)
            by_mode: dict[str, list[int]] = {}
            for tok in sorted(want):
                if applied.get(tok) != want[tok]:
                    by_mode.setdefault(want[tok], []).append(tok)
            for mode in sorted(by_mode):
                t.set_mode(mode, by_mode[mode])
                for tok in by_mode[mode]:
                    applied[tok] = mode
        except Exception:
            self._bump("subscribe_errors")
            log.exception("feed: applying subscriptions failed; the next connect re-applies them")

    # -- reactor thread: connection callbacks -----------------------------------------------------------------
    def on_connect(self, ws: Any, response: Any = None) -> None:
        try:
            mono, wall = self._clock.mono(), self._clock.now_wall()
            with self._lock:
                reconnected = self._ever_connected
                self._ever_connected = True
                self._connected = True
                self._connected_since = mono
                if reconnected:
                    self._reconnects += 1
                want = dict(self._desired)
                degraded = self._degraded
            self._applied = {}                       # a new connection starts with no subscriptions
            try:
                listed = getattr(self._ticker, "subscribed_tokens", None) or {}
                stale = sorted(tok for tok in list(listed) if tok not in want)
                if stale:
                    self._ticker.unsubscribe(stale)
            except Exception:
                self._bump("subscribe_errors")
                log.exception("feed: dropping stale subscriptions failed")
            self._sync(want)
            self._signal_q.put_link(FeedLink(True, reconnected, wall, mono))
            self._post(FeedState(True, reconnected, degraded, None))
            log.info("feed: WS %s; %d instruments wanted", "reconnected" if reconnected else "connected", len(want))
        except Exception:
            self._bump("callback_errors")
            log.exception("feed: on_connect failed")

    def on_close(self, ws: Any, code: Any = None, reason: Any = None) -> None:
        self._went_down(f"closed {code}: {reason}", always_post=False)

    def on_noreconnect(self, ws: Any) -> None:
        log.critical("feed: KiteTicker gave up reconnecting")
        self._went_down("NORECONNECT", always_post=True)

    def on_error(self, ws: Any, code: Any = None, reason: Any = None) -> None:
        self._bump("ws_errors")
        log.warning("feed: WS error %s: %s", code, reason)

    def on_reconnect(self, ws: Any, attempts: Any = None) -> None:
        log.warning("feed: WS reconnect attempt %s", attempts)

    def _went_down(self, reason: str, always_post: bool) -> None:
        try:
            mono, wall = self._clock.mono(), self._clock.now_wall()
            with self._lock:
                was = self._connected
                self._connected = False
                self._connected_since = None
                degraded = self._degraded
            if was:
                self._signal_q.put_link(FeedLink(False, False, wall, mono, reason))
                log.warning("feed: WS down (%s)", reason)
            if was or always_post:
                self._post(FeedState(False, False, degraded, reason))
        except Exception:
            self._bump("callback_errors")
            log.exception("feed: handling a disconnect failed")

    # -- reactor thread: order updates --------------------------------------------------------------------------
    def on_order_update(self, ws: Any, data: Any) -> None:
        """A Kite order postback -> OrderSnapshot(LIVE, WS). A malformed one is dropped: the poll timers recover."""
        try:
            order = order_from_kite(data, Account.LIVE)
        except Exception as exc:
            self._bump("order_update_errors")
            log.error("feed: order update dropped as malformed (%s): %r", exc, data)
            return
        try:
            self._post(snapshot_of(order, "WS", self._clock.mono()))
        except Exception:
            self._bump("callback_errors")
            log.exception("feed: posting order update %s failed", order.broker_order_id)

    # -- reactor thread: ticks ------------------------------------------------------------------------------------
    def on_ticks(self, ws: Any, ticks: Any) -> None:
        try:
            self._on_ticks(ticks)
        except Exception:
            self._bump("callback_errors")
            log.exception("feed: on_ticks failed")

    def _on_ticks(self, ticks: Iterable[Any]) -> None:
        recv_mono, recv_wall = self._clock.mono(), self._clock.now_wall()
        batch: list[UnderlyingTick] = []
        dirty: list[str] = []
        n = 0
        for tick in ticks or ():
            n += 1
            try:
                self._one_tick(tick, recv_mono, recv_wall, batch, dirty)
            except Exception as exc:
                self._anomaly("bad_ticks", "tick dropped: %s", exc)
        self._bump("ticks", n)
        if batch:
            with self._lock:
                self._last_underlying = recv_mono
            self._push(tuple(batch))
        if dirty:
            self._mark_dirty(dirty)

    def _one_tick(self, tick: Any, recv_mono: float, recv_wall: datetime, batch: list[UnderlyingTick],
                  dirty: list[str]) -> None:
        token = _token(tick)
        symbol = self._symbol_of(token)
        if not symbol:
            self._anomaly("unknown_tokens", "tick for unknown token %s dropped", token)
            return
        if symbol.startswith(OPTION_PREFIX):
            if tick.get("depth") is None:
                self._anomaly("no_depth", "%s tick without depth (not full mode) not cached", symbol)
                return
            self._quotes.put(quote_from_tick(tick, symbol, recv_mono, recv_wall))
            dirty.append(symbol)
            return
        self._quotes.put(quote_from_tick(tick, symbol, recv_mono, recv_wall))
        u = underlying_from_tick(tick, symbol, recv_mono, recv_wall)
        if u is not None:
            batch.append(u)

    def _push(self, batch: tuple[UnderlyingTick, ...]) -> None:
        if not self._signal_q.put_ticks(batch):
            self._bump("signal_batches_dropped")
            self._set_degraded(True, "SIGNAL_Q_OVERFLOW")
        elif self._degraded and self._signal_q.qsize() <= self._signal_q.max_batches * SIGNAL_Q_RECOVER_FRAC:
            self._set_degraded(False, "SIGNAL_Q_RECOVERED")

    def _set_degraded(self, degraded: bool, reason: str) -> None:
        with self._lock:
            if self._degraded == degraded:
                return
            self._degraded = degraded
            connected = self._connected
        (log.critical if degraded else log.warning)("feed: degraded=%s (%s)", degraded, reason)
        self._post(FeedState(connected, False, degraded, reason))

    def _mark_dirty(self, symbols: Iterable[str]) -> None:
        with self._dirty_lock:
            self._dirty.update(symbols)
            if self._dirty_posted:
                return
            self._dirty_posted = True
        self._post(QuotesDirty())

    def _bump(self, key: str, n: int = 1) -> None:
        self._stats[key] = self._stats.get(key, 0) + n

    def _anomaly(self, key: str, msg: str, *args: Any) -> None:
        self._bump(key)
        if self._stats[key] <= _LOG_FIRST:
            log.warning("feed: " + msg, *args)
