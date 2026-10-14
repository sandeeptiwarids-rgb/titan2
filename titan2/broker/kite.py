"""Kite Connect adapter (spec §1, §3.2, §3.4, §5.1, §5.8): KiteBroker (LIVE orders) and KiteData (reads only).

- Every order is LIMIT + IOC + MIS, variety regular, on NFO. The payload is built and checked in one place before
  anything is sent; a request that breaks a rule is refused locally (REJECTED, "LOCAL: ...").
- KiteBroker(entries_allowed=False) refuses ENTRY and SELFTEST ("LOCAL: live entries disabled"); EXIT and COVER
  always go through. KiteBroker(orders_allowed=False), the laptop profile's (PAPER only), refuses every place and
  every cancel, whatever the role ("LOCAL: laptop profile: no LIVE orders"); its reads still work. Its methods run
  only on the lane-live-orders thread (I2) unless thread_check=False (tests).
- KiteData has no order methods. It serves lane-reads, the token unit and the self-check.
- Errors are classified once (§5.8). Responses are normalized into titan2.model types; a malformed response fails
  the read (ReadFailed) rather than being guessed at.
- Calls are paced by KiteBuckets, shared blocking token buckets: orders 8/s burst 3 and never more than 8 in any
  rolling second (SEBI's retail-algo threshold is 10 orders/s); quote 1/s; historical 3/s; all else 8/s burst 3.
- One retry (2026-10-14): a quote() call whose connection the other end closed before Kite answered at all (an idle
  keep-alive connection Kite had closed) is made once more at once, paced by the quote bucket
  (dropped_before_response). Nothing else is retried: not a timeout (one while sending the request included), a
  malformed status line or headers, a failed connect, an SSL error or any answer from Kite, and no other read or
  order.

kiteconnect is never imported at module level: its package __init__ imports the twisted ticker, and this module
must import on Windows without twisted. The adapter only needs the client object it is given.
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
import types
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from titan2.broker.lanes import TokenBucket
from titan2.broker.port import (
    AMBIGUOUS, BENIGN, ERROR_TEXT_MAX, FATAL, LOCAL, OK, ORDER_LANE_THREAD, REJECTED, NormalizationError, ReadFailed,
    format_error,
)
from titan2.clock import IST, Clock, RealClock, parse_wall, to_ist
from titan2.core.events import CancelResult, ContractNoteOrder, ContractNoteRow, PlaceResult
from titan2.model import (
    IOC, LIMIT, MIS, NFO, REGULAR, Account, Bar, BrokerOrder, BrokerPosition, BrokerTrade, InvariantViolation, Level,
    Margins, OrderRequest, Quote, Role, Side, order_request_problem,
)

log = logging.getLogger(__name__)

LIVE_ONLY = frozenset({Account.LIVE})
NO_LIVE_ORDERS = "laptop profile: no LIVE orders"     # KiteBroker(orders_allowed=False)'s refusal of a place or cancel
QUOTE_MAX_SYMBOLS = 500
LTP_MAX_SYMBOLS = 1000
MESSAGE_MAX = 500

# ---------------------------------------------------------------------------------------------------------------
# Rate buckets (§3.2). Kite's limits are per user, so one set is shared by every adapter in the process.
# ---------------------------------------------------------------------------------------------------------------
ORDERS_RATE, ORDERS_BURST = 8.0, 3
ORDERS_MAX_PER_ROLLING_S = 8          # SEBI retail-algo threshold is 10/s; 8 leaves room for network jitter
QUOTE_RATE = 1.0                      # quote and ltp
HISTORICAL_RATE = 3.0
OTHER_RATE, OTHER_BURST = 8.0, 3


@dataclass(frozen=True, slots=True)
class KiteBuckets:
    orders: TokenBucket               # place + cancel (a cancel is an order message too)
    quote: TokenBucket
    historical: TokenBucket
    other: TokenBucket

    @classmethod
    def create(cls, mono: Callable[[], float] = time.monotonic,
               sleep: Callable[[float], None] = time.sleep) -> KiteBuckets:
        return cls(
            TokenBucket("orders", ORDERS_RATE, ORDERS_BURST, window_cap=ORDERS_MAX_PER_ROLLING_S, mono=mono,
                        sleep=sleep),
            TokenBucket("quote", QUOTE_RATE, 1, mono=mono, sleep=sleep),
            TokenBucket("historical", HISTORICAL_RATE, 1, mono=mono, sleep=sleep),
            TokenBucket("other", OTHER_RATE, OTHER_BURST, mono=mono, sleep=sleep),
        )


_default_buckets: KiteBuckets | None = None
_default_lock = threading.Lock()


def default_buckets() -> KiteBuckets:
    """The process-wide bucket set, used by any adapter built without explicit buckets."""
    global _default_buckets
    with _default_lock:
        if _default_buckets is None:
            _default_buckets = KiteBuckets.create()
        return _default_buckets


# ---------------------------------------------------------------------------------------------------------------
# Error taxonomy (§5.8). Matched by class name along the MRO, so kiteconnect need not be imported.
# ---------------------------------------------------------------------------------------------------------------
_REJECTED_NAMES = frozenset({"InputException", "OrderException"})
_FATAL_NAMES = frozenset({"TokenException", "PermissionException"})
BENIGN_CANCEL_RE = re.compile(r"(?i)(complete|cancel|reject|not found|already)")


@dataclass(frozen=True, slots=True)
class Classified:
    outcome: str                      # REJECTED | AMBIGUOUS | FATAL
    name: str                         # exception class name
    code: int | None                  # Kite's HTTP code, for Kite errors
    message: str
    throttled: bool                   # HTTP 429: back off before looking the tag up
    from_kite: bool                   # an answer from Kite's servers (a KiteException), not a transport failure


def classify_exception(exc: BaseException) -> Classified:
    """InputException/OrderException -> REJECTED; TokenException/PermissionException -> FATAL; anything else,
    including requests' Timeout/ConnectionError and Network/General/DataException, -> AMBIGUOUS (tag lookup)."""
    names = {c.__name__ for c in type(exc).__mro__}
    from_kite = "KiteException" in names
    code = getattr(exc, "code", None) if from_kite else None
    if from_kite and names & _FATAL_NAMES:
        outcome = FATAL
    elif from_kite and names & _REJECTED_NAMES:
        outcome = REJECTED
    else:
        outcome = AMBIGUOUS
    message = (str(exc) or type(exc).__name__)[:MESSAGE_MAX]
    return Classified(outcome, type(exc).__name__, code, message, outcome == AMBIGUOUS and code == 429, from_kite)


def error_text(exc: BaseException) -> str:
    """format_error() text carrying the §5.8 class, e.g. 'FATAL:TokenException(403): ...'."""
    c = classify_exception(exc)
    return format_error(c.outcome, c.name if c.code is None else f"{c.name}({c.code})", c.message)


def classify_cancel_exception(exc: BaseException) -> tuple[str, str]:
    """(outcome, message) of a failed cancel. Kite saying the order is already terminal is BENIGN (then poll)."""
    c = classify_exception(exc)
    if c.outcome != FATAL and c.from_kite and BENIGN_CANCEL_RE.search(c.message):
        return BENIGN, c.message
    return c.outcome, c.message


# ---------------------------------------------------------------------------------------------------------------
# Normalization: Kite dicts -> titan2.model values. A malformed field raises NormalizationError.
# ---------------------------------------------------------------------------------------------------------------
def _num(v: Any, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise NormalizationError(f"{what}: expected a number, got {v!r}")
    return float(v)


def _int(v: Any, what: str) -> int:
    if isinstance(v, bool) or not (isinstance(v, int) or (isinstance(v, float) and v.is_integer())):
        raise NormalizationError(f"{what}: expected an integer, got {v!r}")
    return int(v)


def _text(v: Any, what: str) -> str:
    if isinstance(v, int) and not isinstance(v, bool):          # ids sometimes arrive as JSON numbers
        v = str(v)
    if not isinstance(v, str) or not v.strip():
        raise NormalizationError(f"{what}: expected text, got {v!r}")
    return v.strip()


def _opt_text(v: Any) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def _wall(v: Any, what: str) -> datetime | None:
    """Kite timestamps are IST: naive datetimes (as parsed by the SDK) or 'YYYY-MM-DD HH:MM:SS' text."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=IST) if v.tzinfo is None else to_ist(v)
    if isinstance(v, str):
        try:
            return parse_wall(v, assume_ist=True)
        except ValueError:
            raise NormalizationError(f"{what}: bad timestamp {v!r}") from None
    raise NormalizationError(f"{what}: bad timestamp {v!r}")


def _side(v: Any, what: str) -> Side:
    try:
        return Side(v)
    except ValueError:
        raise NormalizationError(f"{what}: bad transaction_type {v!r}") from None


def _mapping(v: Any, what: str) -> Mapping:
    if not isinstance(v, Mapping):
        raise NormalizationError(f"{what}: expected an object, got {type(v).__name__}")
    return v


def _rows(v: Any, what: str) -> list[Mapping]:
    if not isinstance(v, (list, tuple)) or not all(isinstance(r, Mapping) for r in v):
        raise NormalizationError(f"{what}: expected a list of objects, got {type(v).__name__}")
    return list(v)


def _symbol(row: Mapping) -> tuple[str, str, str]:
    exchange = _text(row.get("exchange"), "exchange")
    tradingsymbol = _text(row.get("tradingsymbol"), "tradingsymbol")
    return f"{exchange}:{tradingsymbol}", tradingsymbol, exchange


def normalize_status(raw: Any) -> str:
    """Kite order status -> WORKING | COMPLETE | CANCELLED | REJECTED. Only known terminal statuses are terminal:
    anything else (OPEN, TRIGGER PENDING, VALIDATION PENDING, PUT ORDER REQ RECEIVED, ...) is WORKING."""
    s = _text(raw, "status").upper()
    if s in ("COMPLETE", "REJECTED"):
        return s
    if s == "CANCELLED" or s.startswith("CANCELLED "):            # e.g. "CANCELLED AMO"
        return "CANCELLED"
    return "WORKING"


# Ported from agentic_trader/zerodha_tools.py:75-92 @b4694f3 — changes: the post-place reject read (status
#   REJECTED + status_message) becomes a pure normalization of any order row; no sleeping poll inside place()
def order_from_kite(row: Mapping, account: Account) -> BrokerOrder:
    """One orders() / order_history() / WS order-update row -> BrokerOrder."""
    row = _mapping(row, "order")
    oid = _text(row.get("order_id"), "order_id")
    raw_status = _text(row.get("status"), f"order {oid} status")
    qty = _int(row.get("quantity"), f"order {oid} quantity")
    filled = _int(row.get("filled_quantity") or 0, f"order {oid} filled_quantity")
    if qty < 0 or not 0 <= filled <= qty:
        raise NormalizationError(f"order {oid}: filled {filled} of {qty}")
    avg = None
    if filled > 0:
        avg = _num(row.get("average_price"), f"order {oid} average_price")
        if avg <= 0:
            raise NormalizationError(f"order {oid}: filled {filled} at average_price {avg}")
    symbol, tradingsymbol, exchange = _symbol(row)
    return BrokerOrder(
        account, oid, _opt_text(row.get("tag")), symbol, tradingsymbol, exchange, str(row.get("product") or ""),
        _side(row.get("transaction_type"), f"order {oid}"), str(row.get("order_type") or ""),
        str(row.get("validity") or ""), qty, filled, avg, normalize_status(raw_status), raw_status,
        _opt_text(row.get("status_message")) or _opt_text(row.get("status_message_raw")),
        _wall(row.get("order_timestamp"), f"order {oid} order_timestamp"),
        _wall(row.get("exchange_update_timestamp") or row.get("exchange_timestamp"), f"order {oid} exchange time"),
        _opt_text(row.get("placed_by")),
    )


def trade_from_kite(row: Mapping, account: Account) -> BrokerTrade:
    row = _mapping(row, "trade")
    tid = _text(row.get("trade_id"), "trade_id")
    qty = _int(row.get("quantity"), f"trade {tid} quantity")
    px = _num(row.get("average_price"), f"trade {tid} average_price")
    if qty <= 0 or px <= 0:
        raise NormalizationError(f"trade {tid}: {qty} @ {px}")
    symbol, _, _ = _symbol(row)
    return BrokerTrade(account, tid, _text(row.get("order_id"), f"trade {tid} order_id"), symbol,
                       _side(row.get("transaction_type"), f"trade {tid}"), qty, px,
                       _wall(row.get("fill_timestamp"), f"trade {tid} fill_timestamp"),
                       _wall(row.get("exchange_timestamp"), f"trade {tid} exchange_timestamp"))


def position_from_kite(row: Mapping, account: Account) -> BrokerPosition:
    """One positions()["net"] row; net_qty is signed (< 0 is a short)."""
    row = _mapping(row, "position")
    symbol, tradingsymbol, exchange = _symbol(row)
    buy_qty = _int(row.get("buy_quantity"), f"{symbol} buy_quantity")
    sell_qty = _int(row.get("sell_quantity"), f"{symbol} sell_quantity")
    if buy_qty < 0 or sell_qty < 0:
        raise NormalizationError(f"{symbol}: negative buy/sell quantity")
    return BrokerPosition(account, symbol, tradingsymbol, exchange, _text(row.get("product"), f"{symbol} product"),
                          _int(row.get("quantity"), f"{symbol} quantity"), buy_qty, sell_qty,
                          _num(row.get("buy_value"), f"{symbol} buy_value"),
                          _num(row.get("sell_value"), f"{symbol} sell_value"))


def margins_from_kite(raw: Any, account: Account) -> Margins:
    """available_rs = the smaller of equity.net and equity.available.live_balance [V §17.2 #11: which one Kite
    means by available cash is unverified, so the conservative figure is used]."""
    raw = _mapping(raw, "margins")
    eq = _mapping(raw.get("equity"), "margins.equity")
    figures = []
    if eq.get("net") is not None:
        figures.append(_num(eq.get("net"), "margins.equity.net"))
    avail = eq.get("available")
    if isinstance(avail, Mapping) and avail.get("live_balance") is not None:
        figures.append(_num(avail.get("live_balance"), "margins.equity.available.live_balance"))
    if not figures:
        raise NormalizationError("margins: neither equity.net nor equity.available.live_balance present")
    return Margins(account, min(figures), dict(raw))


def _levels(side: Any, what: str) -> tuple[Level, ...]:
    if side is None:
        return ()
    out = []
    for i, lv in enumerate(_rows(side, what)):
        out.append(Level(_num(lv.get("price") or 0, f"{what}[{i}].price"),
                         _int(lv.get("quantity") or 0, f"{what}[{i}].quantity"),
                         _int(lv.get("orders") or 0, f"{what}[{i}].orders")))
    return tuple(out)                 # Quote drops empty (zero) levels and sorts best first


def quote_from_kite(symbol: str, row: Mapping, recv_mono: float, recv_wall: datetime) -> Quote:
    """One quote() entry -> Quote(source="REST"). Its age is measured from receipt (recv_mono), never from Kite's
    timestamp. depth.buy are the bids, depth.sell the asks; indices have no depth."""
    row = _mapping(row, symbol)
    depth = row.get("depth") or {}
    depth = _mapping(depth, f"{symbol} depth")
    ltp = row.get("last_price")
    ltp = _num(ltp, f"{symbol} last_price") if ltp is not None else None
    volume = row.get("volume")
    return Quote(symbol, _int(row.get("instrument_token"), f"{symbol} instrument_token"),
                 _levels(depth.get("buy"), f"{symbol} depth.buy"), _levels(depth.get("sell"), f"{symbol} depth.sell"),
                 ltp if ltp is not None and ltp > 0 else None,
                 _int(volume, f"{symbol} volume") if volume is not None else None,
                 _wall(row.get("last_trade_time"), f"{symbol} last_trade_time"),
                 _wall(row.get("timestamp"), f"{symbol} timestamp"), recv_mono, recv_wall, "REST")


def bar_from_kite(symbol: str, row: Mapping) -> Bar:
    """One historical_data() candle -> Bar(source="HIST"); an inconsistent candle is kept but marked invalid."""
    row = _mapping(row, f"{symbol} candle")
    start = _wall(row.get("date"), f"{symbol} candle date")
    if start is None:
        raise NormalizationError(f"{symbol}: candle without a date")
    o, h, lo, c = (_num(row.get(k), f"{symbol} {k}") for k in ("open", "high", "low", "close"))
    vol = _int(row.get("volume") or 0, f"{symbol} volume")
    valid = min(o, h, lo, c) > 0 and lo <= min(o, c) and h >= max(o, c) and vol >= 0
    return Bar(symbol, start, o, h, lo, c, vol, valid, "HIST")


def contract_note_params(orders: Sequence[ContractNoteOrder]) -> list[dict]:
    """get_virtual_contract_note request rows (§8.1) [V §17.2 #5]."""
    return [{"order_id": o.broker_order_id, "exchange": o.exchange, "tradingsymbol": o.tradingsymbol,
             "transaction_type": Side(o.side).value, "variety": o.variety, "product": o.product,
             "order_type": o.order_type, "quantity": int(o.qty), "average_price": float(o.avg_px)} for o in orders]


def contract_note_rows(orders: Sequence[ContractNoteOrder], resp: Any) -> tuple[ContractNoteRow, ...]:
    """The response is positional (one entry per requested order, echoing its tradingsymbol)."""
    items = _rows(resp, "contract note")
    if len(items) != len(orders):
        raise NormalizationError(f"contract note: {len(items)} rows for {len(orders)} orders")
    rows = []
    for o, item in zip(orders, items):
        if item.get("tradingsymbol") not in (None, o.tradingsymbol):
            raise NormalizationError(f"contract note row {item.get('tradingsymbol')!r} is not {o.tradingsymbol!r}")
        ch = _mapping(item.get("charges"), f"contract note {o.broker_order_id} charges")
        gst = ch.get("gst")
        gst_total = gst.get("total", 0) if isinstance(gst, Mapping) else (gst or 0)
        components = {
            "brokerage": _num(ch.get("brokerage") or 0, "brokerage"),
            "stt": _num(ch.get("transaction_tax") or 0, "transaction_tax"),
            "txn": _num(ch.get("exchange_turnover_charge") or 0, "exchange_turnover_charge"),
            "sebi": _num(ch.get("sebi_turnover_charge") or 0, "sebi_turnover_charge"),
            "stamp": _num(ch.get("stamp_duty") or 0, "stamp_duty"),
            "gst": _num(gst_total, "gst.total"),
        }
        rows.append(ContractNoteRow(o.broker_order_id, _num(ch.get("total"), "charges.total"), components))
    return tuple(rows)


def _kite_time(dt: datetime) -> str:
    """historical_data() takes IST wall-clock text; a naive datetime is refused."""
    return to_ist(dt).strftime("%Y-%m-%d %H:%M:%S")


DROPPED = "Connection aborted."       # urllib3's ProtocolError text: the connection died before any response
# what that ProtocolError wraps when the other end closed the connection before any answer: RemoteDisconnected (a
#   ConnectionResetError: closed before a status line), a reset, an abort (Windows' WSAECONNABORTED) or a broken pipe
CLOSED_BEFORE_ANSWER = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)
RETRIED_NOTE = " [retried once: the first try's connection dropped]"


def dropped_before_response(exc: BaseException) -> bool:
    """requests' ConnectionError(ProtocolError('Connection aborted.', <one of CLOSED_BEFORE_ANSWER>)): the other end
    closed the connection before any response, typically an idle keep-alive connection Kite had closed. urllib3 wraps
    more in that same ProtocolError, and none of it is retried (review of the retry): a timeout while the request was
    being sent (the request may be running, and the quote lane would block for another token.KITE_TIMEOUT_S) and a
    malformed status line or headers (BadStatusLine, IncompleteRead: something did answer). Never a read or connect
    timeout, a failed connect ('Max retries exceeded ...'), an SSL error or an answer from Kite (429, 5xx, token)."""
    names = {c.__name__ for c in type(exc).__mro__}
    if "ConnectionError" not in names or names & {"Timeout", "SSLError", "KiteException"}:
        return False
    inner = exc.args[0] if exc.args else None
    args = getattr(inner, "args", None)
    return (isinstance(args, tuple) and len(args) >= 2 and args[0] == DROPPED
            and isinstance(args[1], CLOSED_BEFORE_ANSWER))


def _read(bucket: TokenBucket, call: Callable[[], Any], normalize: Callable[[Any], Any], *,
          on_dropped: Callable[[BaseException], None] | None = None) -> Any:
    """Pace, call, normalize. Any failure becomes ReadFailed carrying its §5.8 class; nothing is guessed.
    on_dropped (an idempotent read only): a call whose connection dropped before any response is made once more at
    once, paced by the same bucket like every call (it never fails open); on_dropped logs and counts it first."""
    bucket.acquire()
    try:
        raw = call()
    except Exception as exc:
        if on_dropped is None or not dropped_before_response(exc):
            raise ReadFailed(error_text(exc)) from exc
        on_dropped(exc)
        bucket.acquire()
        try:
            raw = call()
        except Exception as exc2:
            text = error_text(exc2)
            raise ReadFailed(text[:ERROR_TEXT_MAX - len(RETRIED_NOTE)] + RETRIED_NOTE) from exc2
    try:
        return normalize(raw)
    except Exception as exc:
        raise ReadFailed(error_text(exc)) from exc


def _require_live(account: Any, what: str) -> None:
    if account != Account.LIVE:
        raise ReadFailed(format_error(REJECTED, LOCAL, f"{what}: Kite serves account LIVE only, not {account}"))


# ---------------------------------------------------------------------------------------------------------------
# KiteData: reads only
# ---------------------------------------------------------------------------------------------------------------
class KiteData:
    """Market data and LIVE account reads. It deliberately has no order methods (§3.4)."""

    accounts = LIVE_ONLY

    def __init__(self, kite: Any, *, clock: Clock | None = None, buckets: KiteBuckets | None = None) -> None:
        if kite is None:
            raise ValueError("KiteData needs a Kite client")
        self._kite = kite
        self._clock = clock if clock is not None else RealClock()
        self._buckets = buckets if buckets is not None else default_buckets()
        self.dropped_retries = 0          # quote() calls retried after a dropped connection (this process)

    def _quote_dropped(self, n: int) -> Callable[[BaseException], None]:
        def note(exc: BaseException) -> None:
            self.dropped_retries += 1
            log.warning("REST quote of %d symbol(s): the connection dropped before Kite answered (%s); retrying once "
                        "(%d so far)", n, error_text(exc), self.dropped_retries)
        return note

    # -- account reads (AccountReads) ---------------------------------------------------------------------------
    def orders_today(self, account: Account) -> tuple[BrokerOrder, ...]:
        _require_live(account, "orders")
        return _read(self._buckets.other, self._kite.orders,
                     lambda rows: tuple(order_from_kite(r, Account.LIVE) for r in _rows(rows, "orders")))

    def trades_today(self, account: Account) -> tuple[BrokerTrade, ...]:
        _require_live(account, "trades")
        return _read(self._buckets.other, self._kite.trades,
                     lambda rows: tuple(trade_from_kite(r, Account.LIVE) for r in _rows(rows, "trades")))

    def positions(self, account: Account) -> tuple[BrokerPosition, ...]:
        """The "net" book: every exchange and product, closed rows included (recon needs them all, signed)."""
        _require_live(account, "positions")
        return _read(self._buckets.other, self._kite.positions,
                     lambda raw: tuple(position_from_kite(r, Account.LIVE)
                                       for r in _rows(_mapping(raw, "positions").get("net"), "positions.net")))

    def margins(self, account: Account) -> Margins:
        _require_live(account, "margins")
        return _read(self._buckets.other, self._kite.margins, lambda raw: margins_from_kite(raw, Account.LIVE))

    def contract_note(self, account: Account, orders: Sequence[ContractNoteOrder]) -> tuple[ContractNoteRow, ...]:
        """Zerodha's virtual contract note for these orders' parameters (§8.1), on any account: it reads no account
        state, so the PAPER sample (the paper exchange's synthetic ids) is priced by the same calculator [V §17.2 #5].
        A refusal raises ReadFailed like any read (for the PAPER sample the engine logs VCN_PAPER_UNSUPPORTED once)."""
        if not isinstance(account, Account):
            raise ReadFailed(format_error(REJECTED, LOCAL, f"contract note: not an account: {account!r}"))
        orders = tuple(orders)
        if not orders:
            return ()
        params = contract_note_params(orders)
        return _read(self._buckets.other, lambda: self._kite.get_virtual_contract_note(params),
                     lambda resp: contract_note_rows(orders, resp))

    # -- market data (DataPort) ---------------------------------------------------------------------------------
    def quote(self, symbols: Sequence[str]) -> dict[str, Quote]:
        """Depth quotes. A symbol Kite omits, or sends malformed (logged), is absent from the result."""
        syms = list(dict.fromkeys(symbols))
        if not syms:
            return {}
        if len(syms) > QUOTE_MAX_SYMBOLS:
            raise ValueError(f"quote() takes at most {QUOTE_MAX_SYMBOLS} symbols, got {len(syms)}")

        def normalize(raw: Any) -> dict[str, Quote]:
            recv_mono, recv_wall = self._clock.mono(), self._clock.now_wall()
            rows = _mapping(raw, "quote")
            out = {}
            for sym in syms:
                if rows.get(sym) is None:
                    continue
                try:
                    out[sym] = quote_from_kite(sym, rows[sym], recv_mono, recv_wall)
                except NormalizationError as exc:
                    log.warning("REST quote %s dropped as malformed: %s", sym, exc)
            return out

        return _read(self._buckets.quote, lambda: self._kite.quote(syms), normalize,
                     on_dropped=self._quote_dropped(len(syms)))

    def ltp(self, symbols: Sequence[str]) -> dict[str, float]:
        syms = list(dict.fromkeys(symbols))
        if not syms:
            return {}
        if len(syms) > LTP_MAX_SYMBOLS:
            raise ValueError(f"ltp() takes at most {LTP_MAX_SYMBOLS} symbols, got {len(syms)}")

        def normalize(raw: Any) -> dict[str, float]:
            rows = _mapping(raw, "ltp")
            out = {}
            for sym in syms:
                row = rows.get(sym)
                if row is None:
                    continue
                px = _num(_mapping(row, sym).get("last_price"), f"{sym} last_price")
                if px > 0:
                    out[sym] = px
            return out

        return _read(self._buckets.quote, lambda: self._kite.ltp(syms), normalize)

    def historical(self, symbol: str, token: int, frm: datetime, to: datetime,
                   interval: str = "5minute") -> tuple[Bar, ...]:
        frm_s, to_s = _kite_time(frm), _kite_time(to)
        return _read(self._buckets.historical,
                     lambda: self._kite.historical_data(int(token), frm_s, to_s, interval),
                     lambda rows: tuple(bar_from_kite(symbol, r) for r in _rows(rows, f"{symbol} historical")))

    def instruments(self, exchange: str) -> list[dict]:
        """The day's dump for one exchange (never the all-exchange dump)."""
        exchange = _text(exchange, "exchange")
        return _read(self._buckets.other, lambda: self._kite.instruments(exchange),
                     lambda rows: [dict(r) for r in _rows(rows, f"instruments {exchange}")])

    def profile(self) -> Mapping:
        return _read(self._buckets.other, self._kite.profile, lambda raw: dict(_mapping(raw, "profile")))

    def basket_margins(self, params: Sequence[Mapping]) -> Mapping:
        """Read-only basket margin check (phase 2) [V §17.2 #13]."""
        orders = [dict(p) for p in params]
        return _read(self._buckets.other, lambda: self._kite.basket_order_margins(orders, consider_positions=True),
                     lambda raw: dict(_mapping(raw, "basket margins")))


# ---------------------------------------------------------------------------------------------------------------
# KiteBroker: the LIVE BrokerPort
# ---------------------------------------------------------------------------------------------------------------
PLACE_PAYLOAD_KEYS = frozenset({"variety", "exchange", "tradingsymbol", "transaction_type", "quantity", "product",
                                "order_type", "price", "validity", "tag"})


def place_payload(req: OrderRequest) -> dict:
    """The exact place_order kwargs: LIMIT, IOC, MIS, regular, NFO. Nothing else is ever sent."""
    return {"variety": REGULAR, "exchange": NFO, "tradingsymbol": req.tradingsymbol,
            "transaction_type": Side(req.side).value, "quantity": int(req.qty), "product": MIS,
            "order_type": LIMIT, "price": round(float(req.limit_px), 2), "validity": IOC, "tag": req.tag}


def payload_problem(p: Mapping) -> str | None:
    """Last check before sending; guards place_payload against edits."""
    if set(p) != PLACE_PAYLOAD_KEYS:
        return f"payload keys {sorted(p)} are not {sorted(PLACE_PAYLOAD_KEYS)}"
    fixed = {"order_type": LIMIT, "validity": IOC, "product": MIS, "variety": REGULAR, "exchange": NFO}
    for key, want in fixed.items():
        if p[key] != want:
            return f"payload {key}={p[key]!r}, must be {want}"
    if p["transaction_type"] not in ("BUY", "SELL"):
        return f"payload transaction_type={p['transaction_type']!r}"
    if isinstance(p["quantity"], bool) or not isinstance(p["quantity"], int) or p["quantity"] <= 0:
        return f"payload quantity={p['quantity']!r}"
    if not (isinstance(p["price"], float) and math.isfinite(p["price"]) and p["price"] > 0):
        return f"payload price={p['price']!r}"
    return None


class KiteBroker:
    """BrokerPort for account LIVE, on the lane-live-orders thread (I2). Always built; entries gated by ALLOW_LIVE,
    and every order and cancel by orders_allowed (False in the laptop profile, which never sends a LIVE order)."""

    accounts = LIVE_ONLY

    def __init__(self, kite: Any, *, entries_allowed: bool = False, orders_allowed: bool = True,
                 thread_check: bool = True, clock: Clock | None = None, buckets: KiteBuckets | None = None) -> None:
        if kite is None:
            raise ValueError("KiteBroker needs a Kite client")
        if type(entries_allowed) is not bool or type(orders_allowed) is not bool or type(thread_check) is not bool:
            raise TypeError("entries_allowed, orders_allowed and thread_check must be bools (entries_allowed = "
                            "ALLOW_LIVE == '1')")
        self._kite = kite
        self._entries_allowed = entries_allowed
        self._orders_allowed = orders_allowed
        self._thread_check = thread_check
        self._buckets = buckets if buckets is not None else default_buckets()
        self._data = KiteData(kite, clock=clock, buckets=self._buckets)

    @property
    def entries_allowed(self) -> bool:
        return self._entries_allowed

    @property
    def orders_allowed(self) -> bool:
        return self._orders_allowed

    def _on_lane(self, what: str) -> None:
        if self._thread_check:
            name = threading.current_thread().name
            if name != ORDER_LANE_THREAD:
                raise InvariantViolation("I2", f"KiteBroker.{what} called on thread {name!r}; Kite order calls "
                                               f"run only on {ORDER_LANE_THREAD}")

    def _refusal(self, req: OrderRequest) -> str | None:
        if req.account != Account.LIVE:
            return f"KiteBroker places account LIVE only, not {req.account}"
        if not self._orders_allowed:
            return NO_LIVE_ORDERS
        if req.role in (Role.ENTRY, Role.SELFTEST) and not self._entries_allowed:
            return "live entries disabled"
        problem = order_request_problem(req)
        if problem is not None:
            return problem
        if req.symbol != f"{req.exchange}:{req.tradingsymbol}":
            return f"symbol {req.symbol!r} is not {req.exchange}:{req.tradingsymbol}"
        return None

    def place(self, req: OrderRequest) -> PlaceResult:
        """One LIMIT IOC MIS order. Never raises for a broker error; never retries (an AMBIGUOUS result is
        resolved by the engine's tag lookup)."""
        self._on_lane("place")
        if not isinstance(req, OrderRequest):
            raise TypeError(f"place() needs an OrderRequest, got {type(req).__name__}")
        problem = self._refusal(req)
        payload = None
        if problem is None:
            payload = place_payload(req)
            problem = payload_problem(payload)
        if problem is not None:
            log.error("LIVE order %s refused locally: %s", req.tag, problem)
            return PlaceResult(req.account, req.tag, REJECTED, None, LOCAL, f"{LOCAL}: {problem}")
        self._buckets.orders.acquire()
        try:
            order_id = self._kite.place_order(**payload)
        except Exception as exc:
            c = classify_exception(exc)
            log.warning("LIVE order %s %s: %s %s", req.tag, c.outcome, c.name, c.message)
            return PlaceResult(Account.LIVE, req.tag, c.outcome, None, c.name, c.message, c.throttled)
        if isinstance(order_id, bool) or not isinstance(order_id, (str, int)) or not str(order_id).strip():
            log.error("LIVE order %s: place_order returned %r; treating as AMBIGUOUS", req.tag, order_id)
            return PlaceResult(Account.LIVE, req.tag, AMBIGUOUS, None, "NoOrderId",
                               f"place_order returned {order_id!r}")
        return PlaceResult(Account.LIVE, req.tag, OK, str(order_id).strip())

    def cancel(self, account: Account, order_pk: str, broker_order_id: str | None) -> CancelResult:
        self._on_lane("cancel")
        if account != Account.LIVE:
            return CancelResult(account, order_pk, REJECTED, f"{LOCAL}: KiteBroker cancels account LIVE only")
        if not self._orders_allowed:
            log.error("LIVE cancel %s (%s) refused locally: %s", order_pk, broker_order_id, NO_LIVE_ORDERS)
            return CancelResult(account, order_pk, REJECTED, f"{LOCAL}: {NO_LIVE_ORDERS}")
        if not broker_order_id:
            return CancelResult(account, order_pk, REJECTED, f"{LOCAL}: no broker_order_id to cancel")
        self._buckets.orders.acquire()
        try:
            self._kite.cancel_order(variety=REGULAR, order_id=str(broker_order_id))
        except Exception as exc:
            outcome, message = classify_cancel_exception(exc)
            log.warning("LIVE cancel %s (%s) %s: %s", order_pk, broker_order_id, outcome, message)
            return CancelResult(Account.LIVE, order_pk, outcome, message)
        return CancelResult(Account.LIVE, order_pk, OK, None)

    # Ported from agentic_trader/zerodha_tools.py:1383-1405 @b4694f3 — changes: one call per poll (no sleep loop;
    #   the engine's timer repeats it); the latest history entry is normalized whole, not just its average_price
    def order_history(self, account: Account, broker_order_id: str) -> BrokerOrder | None:
        self._on_lane("order_history")
        _require_live(account, "order_history")
        oid = _text(broker_order_id, "broker_order_id")

        def latest(rows: Any) -> BrokerOrder | None:
            rows = _rows(rows, f"order_history {oid}")
            if not rows:
                return None
            order = order_from_kite(rows[-1], Account.LIVE)
            if order.broker_order_id != oid:
                raise NormalizationError(f"order_history {oid} returned order {order.broker_order_id}")
            return order

        return _read(self._buckets.other, lambda: self._kite.order_history(order_id=oid), latest)

    def order_trades(self, account: Account, broker_order_id: str) -> tuple[BrokerTrade, ...]:
        self._on_lane("order_trades")
        _require_live(account, "order_trades")
        oid = _text(broker_order_id, "broker_order_id")

        def trades(rows: Any) -> tuple[BrokerTrade, ...]:
            out = tuple(trade_from_kite(r, Account.LIVE) for r in _rows(rows, f"order_trades {oid}"))
            if any(t.broker_order_id != oid for t in out):
                raise NormalizationError(f"order_trades {oid} returned another order's trade")
            return out

        return _read(self._buckets.other, lambda: self._kite.order_trades(order_id=oid), trades)

    def orders_today(self, account: Account) -> tuple[BrokerOrder, ...]:
        self._on_lane("orders_today")
        return self._data.orders_today(account)

    def trades_today(self, account: Account) -> tuple[BrokerTrade, ...]:
        self._on_lane("trades_today")
        return self._data.trades_today(account)

    def positions(self, account: Account) -> tuple[BrokerPosition, ...]:
        self._on_lane("positions")
        return self._data.positions(account)

    def margins(self, account: Account) -> Margins:
        self._on_lane("margins")
        return self._data.margins(account)

    def contract_note(self, account: Account, orders: Sequence[ContractNoteOrder]) -> tuple[ContractNoteRow, ...]:
        self._on_lane("contract_note")
        _require_live(account, "contract note")      # the LIVE account's port; the PAPER sample uses KiteData
        return self._data.contract_note(account, orders)


# ---------------------------------------------------------------------------------------------------------------
# Startup assert: no monkey-patched order path (§1.5, §3.4; v1 patched place_order process-wide)
# ---------------------------------------------------------------------------------------------------------------
class KitePatched(RuntimeError):
    """KiteConnect's order path is not the library's own code."""


GUARDED_METHODS = ("place_order", "modify_order", "cancel_order", "_request", "_post", "_put", "_delete")


def assert_kite_unpatched(cls: type | None = None, instance: Any = None) -> None:
    """Raise KitePatched unless every KiteConnect method on the order path is the library's own function.

    The spec's check (__module__ == "kiteconnect.connect", __qualname__ == "KiteConnect.place_order") is applied to
    the other order methods and the HTTP helpers they call too, and functools.wraps-style patches (which copy
    __module__ and __qualname__) are caught through __wrapped__ and the code object's file. With instance, its
    own attributes must not shadow those methods either.
    """
    if cls is None:
        from kiteconnect.connect import KiteConnect as cls          # lazy: kiteconnect imports twisted
    tail = os.sep + os.path.normcase(os.path.join("kiteconnect", "connect.py"))
    problems = []
    for name in GUARDED_METHODS:
        where = f"KiteConnect.{name}"
        fn = vars(cls).get(name)
        if not isinstance(fn, types.FunctionType):
            problems.append(f"{where} is a {type(fn).__name__}, not the library function")
        elif fn.__module__ != "kiteconnect.connect" or fn.__qualname__ != where:
            problems.append(f"{where} is {fn.__module__}.{fn.__qualname__}")
        elif hasattr(fn, "__wrapped__"):
            problems.append(f"{where} wraps {fn.__wrapped__!r}")
        elif fn.__code__.co_name != name or not os.path.normcase(fn.__code__.co_filename).endswith(tail):
            problems.append(f"{where} runs {fn.__code__.co_name} from {fn.__code__.co_filename}")
    if instance is not None:
        shadowed = [n for n in GUARDED_METHODS if n in getattr(instance, "__dict__", {})]
        if shadowed:
            problems.append(f"instance attributes shadow {shadowed}")
    if problems:
        raise KitePatched("; ".join(problems))
