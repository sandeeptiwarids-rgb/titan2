"""ScriptedMarket: the exchange's books and the WS/REST views of them (spec §16.1).

The market keeps one TRUE book per option (what the exchange would fill against: FakeKite fills from it) and one
LTP per underlying. The bot sees it two ways:
- WS: KiteTicker-shaped tick dicts handed to the real titan2.market.feed.Feed (which fills the real QuoteCache, posts
  QuotesDirty and queues UnderlyingTicks for the signals). Only tokens the Feed subscribed are sent (options only in
  full mode), on every change and otherwise every `heartbeat_s`. freeze(sym) stops a symbol's WS ticks.
- REST: FakeKite.quote() rows from rest_row(): the true book unless rest() overrides it, rest_fail() makes the call
  fail, or stale_depth() serves the frozen book with its old timestamps.

Every mutator takes an optional `at` (a datetime or "HH:MM[:SS[.fff]]" on the current session date): the change is
then applied by step() once the clock reaches it, so a scenario can script a whole timeline up front.
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable

from titan2.clock import IST, FakeClock, ist_datetime, to_ist
from titan2.instruments import InstrumentStore
from titan2.model import Level

from fakes import universe as uni

KITE_DEPTH_LEVELS = 5


class ImmediateReactor:
    """twisted's reactor.callFromThread, run inline: the seam under test is the data, not the threading."""

    def callFromThread(self, fn: Callable, *args: Any, **kwargs: Any) -> None:
        fn(*args, **kwargs)

    def stop(self) -> None:
        pass


class FakeTicker:
    """The KiteTicker methods titan2.market.feed.Feed calls on the reactor thread: subscribe / set_mode / unsubscribe
    (default mode quote, as in kiteconnect 5.0.1) and the subscribed_tokens it reads on connect."""

    def __init__(self) -> None:
        self.subscribed_tokens: dict[int, str] = {}
        self.calls: list[tuple[str, tuple]] = []
        self.websocket_thread = None

    def subscribe(self, tokens: Iterable[int]) -> None:
        tokens = list(tokens)
        self.calls.append(("subscribe", tuple(tokens)))
        for t in tokens:
            self.subscribed_tokens.setdefault(t, "quote")

    def set_mode(self, mode: str, tokens: Iterable[int]) -> None:
        tokens = list(tokens)
        self.calls.append(("set_mode", (mode, tuple(tokens))))
        for t in tokens:
            if t in self.subscribed_tokens:
                self.subscribed_tokens[t] = mode

    def unsubscribe(self, tokens: Iterable[int]) -> None:
        tokens = list(tokens)
        self.calls.append(("unsubscribe", tuple(tokens)))
        for t in tokens:
            self.subscribed_tokens.pop(t, None)

    def close(self, *args: Any) -> None:
        self.calls.append(("close", ()))


@dataclass
class Book:
    """The exchange's book for one option. Timestamps are ages relative to the moment a view is taken."""

    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    ltp: float | None
    volume: int = 100_000
    last_trade_age_s: float | None = 1.0     # None: no trade known (G19 refuses LAST_TRADE_UNKNOWN)
    version: int = 0


@dataclass
class Under:
    ltp: float
    cum_volume: int
    day_open: float
    prev_close: float
    volume_step: int = 100                  # added on every heartbeat emission (strategies see a moving tape)
    version: int = 0


@dataclass
class Frozen:
    """A view frozen at the moment of freezing: what stale_depth serves over REST."""

    book: Book
    exch_wall: datetime
    last_trade_wall: datetime | None


@dataclass(order=True)
class _Due:
    at: datetime
    seq: int
    fn: Callable[[], None] = field(compare=False)


def _naive_local(dt: datetime) -> datetime:
    """kiteconnect's ticker stamps times with datetime.fromtimestamp(): naive HOST-local time."""
    return dt.astimezone().replace(tzinfo=None)


def _naive_ist(dt: datetime) -> datetime:
    """KiteConnect REST rows carry 'YYYY-MM-DD HH:MM:SS' IST text, parsed by the SDK into naive datetimes."""
    return to_ist(dt).replace(tzinfo=None, microsecond=0)


def _kite_depth(levels: Iterable[Level]) -> list[dict]:
    rows = [{"price": lv.price, "quantity": lv.qty, "orders": lv.orders} for lv in levels][:KITE_DEPTH_LEVELS]
    while len(rows) < KITE_DEPTH_LEVELS:
        rows.append({"price": 0.0, "quantity": 0, "orders": 0})
    return rows


class ScriptedMarket:
    def __init__(self, clock: FakeClock, store: InstrumentStore, *, heartbeat_s: float = 0.5,
                 underlying_heartbeat_s: float = 1.0) -> None:
        self.clock = clock
        self.store = store
        self.heartbeat_s = heartbeat_s
        self.underlying_heartbeat_s = underlying_heartbeat_s
        self.books: dict[str, Book] = {}
        self.unders: dict[str, Under] = {}
        self.frozen: set[str] = set()
        self.stale: dict[str, Frozen] = {}
        self.rest_books: dict[str, Book] = {}
        self.rest_down: set[str] = set()       # symbols whose REST quote fails; "*" fails every REST quote
        self.connected = False
        self.feed: Any = None                  # the current process's Feed (rebound on every boot)
        self.ticker: FakeTicker | None = None
        self.emitted = 0
        self._last_emit: dict[str, tuple[float, int]] = {}
        self._due: list[_Due] = []
        self._seq = itertools.count()
        self._token_symbol = {}
        for inst in uni.CONTRACTS:
            self._token_symbol[inst.token] = inst.symbol
        for spec in uni.SPECS:
            self._token_symbol[spec.token] = spec.symbol
            self.unders[spec.symbol] = Under(spec.spot, 1_000_000 if spec.kind == "STOCK" else 0, spec.spot, spec.spot)
        self._contracts = {c.symbol: c for c in uni.CONTRACTS}

    # -- time --------------------------------------------------------------------------------------------------
    def _when(self, at: datetime | str | None) -> datetime | None:
        if at is None:
            return None
        if isinstance(at, str):
            return ist_datetime(self.clock.now_wall().date(), at)
        return at if at.tzinfo is not None else at.replace(tzinfo=IST)

    def _later(self, at: datetime | str | None, fn: Callable[[], None]) -> None:
        when = self._when(at)
        if when is None or when <= self.clock.now_wall():
            fn()
        else:
            heapq.heappush(self._due, _Due(when, next(self._seq), fn))

    def run_due(self, now: datetime) -> None:
        while self._due and self._due[0].at <= now:
            heapq.heappop(self._due).fn()

    # -- books --------------------------------------------------------------------------------------------------
    def book(self, symbol: str) -> Book:
        """The true book of an option (created from the universe's default on first use)."""
        b = self.books.get(symbol)
        if b is None:
            inst = self._contracts[symbol]
            bids, asks, ltp = uni.default_book(inst)
            b = self.books[symbol] = Book(bids, asks, ltp)
        return b

    def depth(self, symbol: str, bids: Iterable = (), asks: Iterable = (), *, at: datetime | str | None = None,
              ltp: float | None | str = "mid", last_trade_age_s: float | None | str = "keep",
              volume: int | None = None) -> None:
        """Replace the true book of an option. bids/asks: Level or (price, qty[, orders]) rows."""
        def apply() -> None:
            b = self.book(symbol)
            b.bids = tuple(lv if isinstance(lv, Level) else Level(*lv) for lv in bids)
            b.asks = tuple(lv if isinstance(lv, Level) else Level(*lv) for lv in asks)
            if ltp == "mid":
                pts = [lv.price for lv in (b.bids[:1] + b.asks[:1])]
                b.ltp = round(sum(pts) / len(pts), 2) if pts else b.ltp
            else:
                b.ltp = ltp
            if last_trade_age_s != "keep":
                b.last_trade_age_s = last_trade_age_s
            if volume is not None:
                b.volume = volume
            b.version += 1
        self._later(at, apply)

    def quote(self, symbol: str, bid: float | None, ask: float | None, *, qty: int | None = None, levels: int = 5,
              at: datetime | str | None = None, ltp: float | None | str = "mid",
              last_trade_age_s: float | None | str = "keep") -> None:
        """A simple ladder book: `levels` levels each side of qty (default 4 lots), one tick apart."""
        inst = self._contracts[symbol]
        q = qty if qty is not None else 4 * inst.lot_size
        bids = uni.ladder(bid, "bid", q, levels) if bid is not None else ()
        asks = uni.ladder(ask, "ask", q, levels) if ask is not None else ()
        self.depth(symbol, bids, asks, at=at, ltp=ltp, last_trade_age_s=last_trade_age_s)

    def tick(self, underlying: str, ltp: float, cum_volume: int | None = None, *,
             at: datetime | str | None = None) -> None:
        """Move an underlying's LTP (and optionally its cumulative volume). Option books do not reprice."""
        def apply() -> None:
            u = self.unders[underlying]
            u.ltp = float(ltp)
            if cum_volume is not None:
                u.cum_volume = int(cum_volume)
            u.version += 1
        self._later(at, apply)

    def freeze(self, symbol: str, *, at: datetime | str | None = None) -> None:
        """No more WS ticks for symbol (an option that stops trading: KiteTicker sends nothing)."""
        self._later(at, lambda: self.frozen.add(symbol))

    def unfreeze(self, symbol: str, *, at: datetime | str | None = None) -> None:
        def apply() -> None:
            self.frozen.discard(symbol)
            self._last_emit.pop(symbol, None)
        self._later(at, apply)

    def rest(self, symbol: str, bids: Iterable = (), asks: Iterable = (), *, at: datetime | str | None = None,
             ltp: float | None = None) -> None:
        """REST quote() serves this book for symbol instead of the true one (until rest_clear)."""
        def apply() -> None:
            bl = tuple(lv if isinstance(lv, Level) else Level(*lv) for lv in bids)
            al = tuple(lv if isinstance(lv, Level) else Level(*lv) for lv in asks)
            pts = [lv.price for lv in (bl[:1] + al[:1])]
            self.rest_books[symbol] = Book(bl, al, ltp if ltp is not None else (round(sum(pts) / len(pts), 2)
                                                                                 if pts else None))
        self._later(at, apply)

    def rest_clear(self, symbol: str, *, at: datetime | str | None = None) -> None:
        self._later(at, lambda: self.rest_books.pop(symbol, None))

    def rest_fail(self, symbol: str | None = None, *, at: datetime | str | None = None) -> None:
        """REST quote() fails for symbol (None: every REST quote fails)."""
        self._later(at, lambda: self.rest_down.add(symbol or "*"))

    def rest_ok(self, symbol: str | None = None, *, at: datetime | str | None = None) -> None:
        def apply() -> None:
            if symbol is None:
                self.rest_down.clear()
            else:
                self.rest_down.discard(symbol)
        self._later(at, apply)

    def stale_depth(self, symbol: str, *, at: datetime | str | None = None) -> None:
        """Freeze what the bot can see of symbol (WS and REST) at this moment, timestamps included, while the
        true book keeps moving: REST then returns an old book with a fresh receipt time."""
        def apply() -> None:
            b = self.book(symbol)
            now = self.clock.now_wall()
            lt = None if b.last_trade_age_s is None else now - timedelta(seconds=b.last_trade_age_s)
            self.stale[symbol] = Frozen(Book(b.bids, b.asks, b.ltp, b.volume, b.last_trade_age_s), now, lt)
            self.frozen.add(symbol)
        self._later(at, apply)

    def unstale(self, symbol: str, *, at: datetime | str | None = None) -> None:
        def apply() -> None:
            self.stale.pop(symbol, None)
            self.frozen.discard(symbol)
            self._last_emit.pop(symbol, None)
        self._later(at, apply)

    def disconnect(self, *, at: datetime | str | None = None) -> None:
        """The WS drops (KiteTicker on_close). Order updates sent meanwhile are lost."""
        def apply() -> None:
            if self.connected:
                self.connected = False
                if self.feed is not None:
                    self.feed.on_close(None, 1006, "harness disconnect")
        self._later(at, apply)

    def reconnect(self, *, at: datetime | str | None = None) -> None:
        """The WS comes back (KiteTicker on_connect: the Feed re-applies its subscriptions)."""
        def apply() -> None:
            if not self.connected and self.feed is not None:
                self.connected = True
                self._last_emit.clear()
                self.feed.on_connect(None)
        self._later(at, apply)

    # -- the bot's views ----------------------------------------------------------------------------------------
    def true_levels(self, symbol: str) -> tuple[tuple[Level, ...], tuple[Level, ...]]:
        b = self.book(symbol)
        return b.bids, b.asks

    def ltp(self, symbol: str) -> float | None:
        if symbol in self.unders:
            return self.unders[symbol].ltp
        return self.book(symbol).ltp

    def rest_row(self, symbol: str) -> dict | None:
        """One KiteConnect.quote() entry (as the SDK returns it), or None for an unknown symbol."""
        now = self.clock.now_wall()
        if symbol in self.unders:
            u = self.unders[symbol]
            token = next(s.token for s in uni.SPECS if s.symbol == symbol)
            return {"instrument_token": token, "timestamp": _naive_ist(now), "last_trade_time": _naive_ist(now),
                    "last_price": u.ltp, "last_quantity": 1, "buy_quantity": 0, "sell_quantity": 0,
                    "volume": u.cum_volume, "average_price": u.ltp, "oi": 0, "net_change": 0.0,
                    "ohlc": {"open": u.day_open, "high": max(u.day_open, u.ltp), "low": min(u.day_open, u.ltp),
                             "close": u.prev_close},
                    "depth": {"buy": _kite_depth(()), "sell": _kite_depth(())}}
        inst = self._contracts.get(symbol)
        if inst is None:
            return None
        exch, last = now, None
        if symbol in self.stale:
            f = self.stale[symbol]
            b, exch, last = f.book, f.exch_wall, f.last_trade_wall
        else:
            b = self.rest_books.get(symbol) or self.book(symbol)
            if b.last_trade_age_s is not None:
                last = now - timedelta(seconds=b.last_trade_age_s)
        return {"instrument_token": inst.token, "timestamp": _naive_ist(exch),
                "last_trade_time": None if last is None else _naive_ist(last), "last_price": b.ltp or 0.0,
                "last_quantity": inst.lot_size, "buy_quantity": sum(lv.qty for lv in b.bids),
                "sell_quantity": sum(lv.qty for lv in b.asks), "volume": b.volume, "average_price": b.ltp or 0.0,
                "oi": 0, "net_change": 0.0, "ohlc": {"open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0},
                "depth": {"buy": _kite_depth(b.bids), "sell": _kite_depth(b.asks)}}

    def rest_fails(self, symbols: Iterable[str]) -> bool:
        return "*" in self.rest_down or any(s in self.rest_down for s in symbols)

    def _tick_dict(self, token: int, symbol: str, mode: str, now: datetime) -> dict | None:
        local = _naive_local(now)
        if symbol in self.unders:
            u = self.unders[symbol]
            return {"tradable": True, "mode": mode, "instrument_token": token, "last_price": u.ltp,
                    "last_traded_quantity": 1, "average_traded_price": u.ltp, "volume_traded": u.cum_volume,
                    "total_buy_quantity": 0, "total_sell_quantity": 0,
                    "ohlc": {"open": u.day_open, "high": max(u.day_open, u.ltp), "low": min(u.day_open, u.ltp),
                             "close": u.prev_close},
                    "change": 0.0, "last_trade_time": local, "exchange_timestamp": local}
        if mode != "full":
            return None                       # an option is cached only with depth (full mode)
        b = self.book(symbol)
        last = None if b.last_trade_age_s is None else _naive_local(now - timedelta(seconds=b.last_trade_age_s))
        return {"tradable": True, "mode": "full", "instrument_token": token, "last_price": b.ltp or 0.0,
                "last_traded_quantity": 1, "average_traded_price": b.ltp or 0.0, "volume_traded": b.volume,
                "total_buy_quantity": sum(lv.qty for lv in b.bids), "total_sell_quantity": sum(lv.qty for lv in b.asks),
                "ohlc": {"open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0}, "change": 0.0,
                "last_trade_time": last, "oi": 0, "oi_day_high": 0, "oi_day_low": 0, "exchange_timestamp": local,
                "depth": {"buy": _kite_depth(b.bids), "sell": _kite_depth(b.asks)}}

    def step(self, now: datetime, now_mono: float) -> int:
        """Apply due script changes, then send the WS ticks that are due. Returns how many ticks were sent."""
        self.run_due(now)
        if not self.connected or self.feed is None or self.ticker is None:
            return 0
        ticks = []
        for token, mode in sorted(self.ticker.subscribed_tokens.items()):
            symbol = self._token_symbol.get(token)
            if symbol is None or symbol in self.frozen:
                continue
            obj = self.unders.get(symbol) or self.book(symbol)
            last = self._last_emit.get(symbol)
            beat = self.underlying_heartbeat_s if symbol in self.unders else self.heartbeat_s
            if last is not None and last[1] == obj.version and now_mono - last[0] < beat - 1e-9:
                continue
            if symbol in self.unders and last is not None and last[1] == obj.version:
                obj.cum_volume += obj.volume_step   # a heartbeat on a live tape carries some volume
            tick = self._tick_dict(token, symbol, mode, now)
            if tick is None:
                continue
            self._last_emit[symbol] = (now_mono, obj.version)
            ticks.append(tick)
        if ticks:
            self.emitted += len(ticks)
            self.feed.on_ticks(None, ticks)
        return len(ticks)
