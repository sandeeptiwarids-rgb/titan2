"""Paper exchange for accounts PAPER and SHADOW (spec §6; DETAIL §6 and the §4.4 paper_exchange.db).

PaperExchange implements BrokerPort exactly like KiteBroker, so the engine never knows which broker answers it.
Snapshots carry source "SIM". A refusal KiteBroker would also make before sending is REJECTED "LOCAL: ..."; a
rejection by the simulated exchange is REJECTED "SIM: ...".

- place() validates as the exchange would (symbol known, market open, lot and tick of the real contract, price
  band against the LTP, funds through a MarginModel), then accepts with a seeded lognormal latency clipped to
  80-3000 ms. The order is OPEN until its due time; its WORKING snapshot is emitted at once.
- At the due time the order fills against the book AT FILL TIME, never the decision book: only a quote received
  after the order was placed and at most PAPER_BOOK_MAX_AGE_S old counts, else a REST request to lane-reads and up
  to 1.5 s of waiting, else CANCELLED "SIM: no fresh depth". The snapshot the order was decided on never fills it,
  so a book that moves during the latency costs a paper order what it costs a live IOC. Paper model 2 (2026-10-14)
  also treats as missing a crossed or locked book (best bid >= best ask) and a book whose exchange time is in an
  earlier second than the decision quote's (the engine sends that time with the order, decision_exch_wall: a lagged
  WS packet that arrives after the order left). The order waits for the next packet or REST as for a missing book;
  a wait that ends without a usable book is CANCELLED "SIM: crossed or locked book" or "SIM: book older than the
  decision", and the daily report counts them. fill_from_depth() walks best level first, stops at the first level
  beyond the limit and applies participation to the depth walked so far: walked qty x participation / lot lots, the
  whole ones for sure and the leftover fraction of a lot with that probability (one seeded draw per order,
  fill_draw()), each lot filled at the level that completes it. So the expected fill is participation x the walked
  depth, on both sides. A lone one-lot level inside the limit fills with probability participation, half the time
  at 0.5 (paper model 1 rounded the fraction down, so it never filled), where a live IOC usually takes it: on a
  stop into a lone gap bid, paper still misses half the time the loss LIVE books. IOC: whatever does not fill is
  cancelled. Never LTP.
- Options have no pre-open: a quote received or exchange-stamped before the open + 30 s is a stale carry-over and
  is never filled against.
- The log line of an order's fill (COMPLETE, or an IOC's partial or unfilled CANCELLED) names the book it filled
  on (2026-10-14 night, the log only: _book_note): WS or REST, its receipt and exchange times, its age at the due
  time, how long past due the fill came, the walked side's levels down to the limit and the order's draw.
- Own persisted ledger, db/paper_exchange.db, written only by the paper-sim thread: p_orders (each API order with
  its bot book), p_fills (one row per filled level), p_meta. Positions are the signed sum of p_fills; margins are
  capital + sells - buys - charges for the day (charges.net_pnl, the one P&L function) less the funds OPEN orders
  block. Recon compares this ledger with the engine's: two independently kept books.
- Funds per pool (review F-19): the PAPER account is one pool, like the one LIVE Zerodha account (§6.2). On the
  SHADOW account each book has its own: a BUY is checked against PAPER_CAPITAL_RS + that book's realized net today
  - that book's OPEN BUYs, so a busy uncapped SHADOW:<s> book cannot starve PLACEBO:<s>, or the reverse. A SHADOW
  request without a book is checked against the whole account (fails closed). EXCHANGE orders (square-offs) have
  no book and credit no pool. margins() stays the account-wide view (G21 is skipped for QUALITY books).
- Startup: an OPEN order overdue by more than 1 s is CANCELLED ("SIM: expired during downtime"); on a trading day, a
  position left from an earlier session is squared off by the exchange at Rs 0.05 ("SIM: missed square-off").
  squareoffs() serves the exchange's own square-off orders of earlier days (FetchSquareoffs), so a leftover it closed
  on an earlier day that the bot never booked is booked from that record (review round 2).
- At BROKER_SQUAREOFF_AT each non-zero MIS position gets an EXCHANGE order tagged SQOFF that walks every shown level
  at participation 1.0 and fills the rest at the deepest level x 0.95 (sell) / x 1.05 (buy). It waits up to 1.5 s
  for a fresh book that is neither crossed nor locked; if only such a book comes, it fills on that one at its worse
  side (a sell never above the best ask, a buy never under the best bid), never at the no-book Rs 0.05, even when
  that book, received during the wait, is older than PAPER_BOOK_MAX_AGE_S by its end (a thin option's silent feed
  and a REST answer early in the wait). It is never emitted as a snapshot: recon finds it in orders() and books
  BROKER_SQUAREOFF (placed_by EXCHANGE, never "API").
- No virtual contract note: contract_note() always raises ReadFailed (REJECTED SIM). This exchange could only bill
  with titan2.charges, the model the §8.1 check tests, so its "note" would agree with any RateSet, however wrong.
- Broker reads pay a round trip too (decision B1, 2026-10-11): PaperSim answers a positions or orders read only
  after a seeded lognormal delay (PAPER_READ_LATENCY_MEDIAN_MS / _SIGMA, clipped like the order latency; its own
  sequence per session and account, read_latency_seed), one FIFO per account, so the exit cap's read made after
  an exit decision costs paper what positions() costs LIVE. The answer shows the ledger at its due time and is
  stamped with the read's receipt (req_mono), the conservative stamp the LIVE lanes use. Other reads answer at once.
- Tests call step() inline with a FakeClock. In production PaperSim runs it on the paper-sim thread.
"""

from __future__ import annotations

import collections
import hashlib
import heapq
import itertools
import logging
import math
import random
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Iterator, Protocol, Sequence

from titan2 import charges
from titan2.broker.lanes import (
    ACCOUNT_READ_COMMANDS, ORDER_COMMANDS, PAPER_ACCOUNTS, PAPER_READ_COMMANDS, execute_broker, failure_events,
)
from titan2.broker.port import AMBIGUOUS, BENIGN, LOCAL, OK, REJECTED, ReadFailed, format_error, snapshot_of
from titan2.clock import Clock, SessionCalendar, iso, ist_datetime, parse_wall, session_of
from titan2.core.events import (
    CancelResult, Command, ContractNoteOrder, ContractNoteRow, Event, FetchOrders, FetchPositions, FetchQuotes,
    PlaceResult, Priority,
)
from titan2.market.quotes import QuoteCache
from titan2.model import (
    IOC, LIMIT, MIS, NFO, PRICE_EPS, Account, BrokerOrder, BrokerPosition, BrokerTrade, Instrument,
    InvariantViolation, Level, Margins, OrderRequest, Quote, Side, ceil_tick, floor_tick, is_tick_multiple,
    order_request_problem, parse_tag,
)
from titan2.settings import (  # code constants (DETAIL §8.4), hashed into SEMANTIC_CONSTANTS
    PAPER_DEPTH_WAIT_S, PAPER_EXPIRED_GRACE_S, PAPER_LATENCY_CLIP_MS, PAPER_MISSED_SQOFF_PX, PAPER_SQOFF_BUY_FACTOR,
    PAPER_SQOFF_SELL_FACTOR, Settings,
)

__all__ = [
    "DELAYED_READS", "PAPER_SIM_THREAD", "SIM", "SQOFF_TAG", "MarginModel", "PremiumMargin", "PaperExchange",
    "PaperSim", "PaperStoreError", "fill_draw", "fill_from_depth", "fill_seed", "latency_seed", "read_latency_seed",
]

log = logging.getLogger(__name__)

PAPER_SIM_THREAD = "paper-sim"
SIM = "SIM"                         # snapshot source; error_class of exchange-side rejections
API, EXCHANGE = "API", "EXCHANGE"   # p_orders.placed_by
SQOFF_TAG = "SQOFF"
SQOFF_ORDER_TYPE = "SQOFF"          # the square-off crosses the book with no limit; recorded under its own name
SQOFF_VALIDITY = "DAY"
OPEN, COMPLETE, CANCELLED = "OPEN", "COMPLETE", "CANCELLED"   # p_orders.status, plus REJECTED
SCHEMA_VERSION = 2                  # 2: p_orders.book (review F-19); a v1 file is upgraded in place on open
BUSY_TIMEOUT_MS = 5000
LOOP_WAIT_S = 0.010                 # PaperSim: condition wait between steps (DETAIL §6.5)
LOOP_ERROR_BACKOFF_S = 0.5
DELAYED_READS = (FetchPositions, FetchOrders)   # PaperSim answers these after the read delay (B1)

MSG_NO_DEPTH = "SIM: no fresh depth"
MSG_CROSSED_BOOK = "SIM: crossed or locked book"         # paper model 2: the wait saw only crossed or locked books
MSG_OLDER_BOOK = "SIM: book older than the decision"     # ... or only books stamped before the decision quote
MSG_EXPIRED = "SIM: expired during downtime"
MSG_MISSED_SQOFF = "SIM: missed square-off"
MSG_SQOFF = "SIM: auto square-off"
MSG_BAND = "SIM: price outside band (LPP)"
MSG_FUNDS = "SIM: insufficient funds"
MSG_CLOSED = "SIM: market closed"
MSG_UNKNOWN = "SIM: unknown instrument"
MSG_DUPLICATE = "SIM: duplicate tag"
MSG_NO_CONTRACT_NOTE = ("contract note: the paper exchange is not Zerodha's charges calculator (it could only bill "
                        "with titan2.charges, the model under test); the paper sample needs Kite's virtual "
                        "contract note")
# Reject texts never embed a symbol or tag: engine regexes scan them (§5.7 band, §5.9 blocked) and a name such as
# BANKNIFTY must not read as "ban". MSG_BAND is the spec's text; its "band" is the §5.7 band-reject signal.

Post = Callable[[Event], None]

# DETAIL §4.4, with one change: its table-level UNIQUE (account, tag) would allow a single SQOFF-tagged square-off
# order per account, ever. Tag uniqueness matters for API orders (one paper order per bot tag), so it is a partial
# unique index over them. The triggers keep the ledger append-only and terminal orders immutable.
_DDL = """
CREATE TABLE p_orders(broker_order_id TEXT PRIMARY KEY, account TEXT NOT NULL CHECK (account IN ('PAPER','SHADOW')), tag TEXT,
  symbol TEXT NOT NULL, token INTEGER NOT NULL, side TEXT NOT NULL, product TEXT NOT NULL, order_type TEXT NOT NULL,
  validity TEXT NOT NULL, qty INTEGER NOT NULL, lot_size INTEGER NOT NULL, tick REAL NOT NULL, limit_px REAL,
  placed_ts TEXT NOT NULL, due_ts TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('OPEN','COMPLETE','CANCELLED','REJECTED')),
  filled_qty INTEGER NOT NULL DEFAULT 0, avg_px REAL, status_msg TEXT, terminal_ts TEXT,
  placed_by TEXT NOT NULL CHECK (placed_by IN ('API','EXCHANGE')), book TEXT);
CREATE UNIQUE INDEX p_orders_api_tag ON p_orders(account, tag) WHERE placed_by = 'API';
CREATE INDEX p_orders_account_day ON p_orders(account, placed_ts);
CREATE TRIGGER p_orders_terminal BEFORE UPDATE ON p_orders WHEN OLD.status <> 'OPEN'
  BEGIN SELECT RAISE(ABORT, 'terminal paper order is immutable'); END;
CREATE TABLE p_fills(trade_id TEXT PRIMARY KEY, broker_order_id TEXT NOT NULL REFERENCES p_orders(broker_order_id),
  account TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL CHECK (qty > 0),
  px REAL NOT NULL CHECK (px > 0), fill_ts TEXT NOT NULL, level_idx INTEGER NOT NULL);
CREATE INDEX p_fills_account_symbol ON p_fills(account, symbol);
CREATE TRIGGER p_fills_no_update BEFORE UPDATE ON p_fills BEGIN SELECT RAISE(ABORT, 'p_fills is append-only'); END;
CREATE TRIGGER p_fills_no_delete BEFORE DELETE ON p_fills BEGIN SELECT RAISE(ABORT, 'p_fills is append-only'); END;
CREATE TABLE p_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _finite(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _plain_int(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _crossed(q: Quote) -> bool:
    """A crossed or locked book (best bid >= best ask): one side was left behind by a move of the other, which no
    exchange shows (paper model 2). A one-sided book is neither."""
    return bool(q.bids and q.asks) and q.bids[0].price >= q.asks[0].price - PRICE_EPS


BOOK_LEVELS_LOGGED = 5                  # _book_note: the walked side's levels listed, best first


def _book_note(q: Quote, row: sqlite3.Row, side: Side, now_wall: datetime, draw: float) -> str:
    """The book an order was filled on, for its COMPLETE (or IOC) log line (2026-10-14 night: paper kept no record of
    it, so paper-vs-live could not be audited; log line only, no schema change, no fill change): its source (WS or
    REST), receipt and exchange times, how old it was at the order's due time (negative: it came after it, and the
    order waited for it), how long past due the fill came, the walked side's levels down to the limit (price x qty,
    best first) and the order's paper model 2 draw."""
    due = parse_wall(row["due_ts"])
    age_ms = (due - q.recv_wall).total_seconds() * 1000.0
    late_ms = max(0.0, (now_wall - due).total_seconds() * 1000.0)
    limit = float(row["limit_px"])
    levels = q.asks if side is Side.BUY else q.bids
    walked = [lv for lv in levels if (lv.price <= limit + PRICE_EPS if side is Side.BUY
                                      else lv.price >= limit - PRICE_EPS)]
    shown = " ".join(f"{lv.price:.2f}x{lv.qty}" for lv in walked[:BOOK_LEVELS_LOGGED]) or "none"
    if len(walked) > BOOK_LEVELS_LOGGED:
        shown += f" (+{len(walked) - BOOK_LEVELS_LOGGED} more)"
    exch = f"{q.exch_wall:%H:%M:%S}" if q.exch_wall is not None else "none"
    age = f"{age_ms:.0f} ms old at the due time" if age_ms >= 0 else f"it came {-age_ms:.0f} ms after the due time"
    return (f"book {q.source} received {q.recv_wall:%H:%M:%S}.{q.recv_wall.microsecond // 1000:03d} (exchange "
            f"{exch}), {age}, filled {late_ms:.0f} ms past due; {'asks' if side is Side.BUY else 'bids'} to the "
            f"limit {limit:.2f}: {shown}; draw {draw:.4f}")


# ---------------------------------------------------------------------------------------------------------------
# Margin model (§14.1 hook: phase 2 replaces PremiumMargin with a basket-margin snapshot)
# ---------------------------------------------------------------------------------------------------------------
class MarginModel(Protocol):
    def required(self, req: OrderRequest, rates: charges.RateSet) -> float:
        """Rupees an order blocks while it is open; place() rejects it when this exceeds the available funds."""


class PremiumMargin:
    """Phase 1: a BUY blocks qty x limit plus its estimated charges. A SELL blocks nothing (it closes a long; a sell
    beyond the position is accepted, as Zerodha would with margin, and shows up as a short that recon catches)."""

    def required(self, req: OrderRequest, rates: charges.RateSet) -> float:
        if Side(req.side) is not Side.BUY:
            return 0.0
        value = req.qty * req.limit_px
        return value + charges.order_charges(charges.OrderFills(Side.BUY, req.qty, value), rates).total


# ---------------------------------------------------------------------------------------------------------------
# fill_from_depth: pure, shared with tests/fakes FakeKite
# ---------------------------------------------------------------------------------------------------------------
def _book(levels: Iterable | None, best_is_high: bool) -> list[Level]:
    """Valid levels (finite price > 0, qty > 0), best first. Accepts Level or (price, qty[, orders])."""
    out: list[Level] = []
    for lv in levels or ():
        if isinstance(lv, (tuple, list)):
            lv = Level(*lv)
        elif not isinstance(lv, Level):
            raise TypeError(f"depth level must be a Level or (price, qty[, orders]), got {type(lv).__name__}")
        if _finite(lv.price) and lv.price > 0 and _finite(lv.qty) and lv.qty > 0:
            out.append(lv)
    out.sort(key=lambda lv: lv.price, reverse=best_is_high)
    return out


def _depth_fills(levels: Iterable | None, side: Side, qty: int, limit: float | None, lot: int, participation: float,
                 adverse_ticks: int, tick: float, draw: float) -> list[tuple[int, float, int]]:
    """fill_from_depth() as (level_idx, px, qty) rows; level_idx indexes the normalized (best-first) book."""
    side = Side(side)
    if not (_plain_int(qty) and qty > 0):
        raise ValueError(f"qty must be an int > 0, got {qty!r}")
    if not (_plain_int(lot) and lot > 0) or qty % lot:
        raise ValueError(f"qty {qty!r} is not a positive multiple of lot {lot!r}")
    if not (_finite(participation) and 0 < participation <= 1):
        raise ValueError(f"participation must be in (0, 1], got {participation!r}")
    if not (_plain_int(adverse_ticks) and adverse_ticks >= 0):
        raise ValueError(f"adverse_ticks must be an int >= 0, got {adverse_ticks!r}")
    if not (_finite(tick) and tick > 0):
        raise ValueError(f"tick must be a finite price > 0, got {tick!r}")
    if limit is not None and not (_finite(limit) and limit > 0):
        raise ValueError(f"limit must be None or a finite price > 0, got {limit!r}")
    if not (_finite(draw) and 0 <= draw < 1):
        raise ValueError(f"draw must be a number in [0, 1), got {draw!r}")
    buy = side is Side.BUY
    out: list[tuple[int, float, int]] = []
    walked = filled = 0
    for i, lv in enumerate(_book(levels, best_is_high=not buy)):
        if filled == qty:
            break
        if limit is not None and (lv.price > limit + PRICE_EPS if buy else lv.price < limit - PRICE_EPS):
            break                                        # the book traded through the limit: nothing deeper fills
        # Participation applies to the depth walked so far, not to each level alone: per level, a one-lot level
        # at participation <= 0.5 rounds down to nothing, so a book of one-lot levels (the usual stock-option
        # touch) never filled a one-lot order that a live IOC fills at once.
        # Paper model 2: the share's leftover fraction of a lot is one more lot with that probability (the order's
        # draw, the same at every level), so the expected fill is participation x the depth walked. Rounded down
        # (paper model 1), a lone one-lot bid inside a stop's limit never filled, and the exit retried at the
        # restored touch: better than LIVE, whose IOC takes that bid. Now it fills with probability participation
        # (half the time), so that bias is halved, not gone.
        walked += lv.qty
        share = walked * participation / lot             # lots earned so far, a fraction of one included
        whole = math.floor(share + 1e-9)
        lots = whole + (1 if draw < share - whole - 1e-9 else 0)
        take = min(qty, lots * lot) - filled
        if take <= 0:
            continue                                     # no further lot earned yet: its share carries deeper
        px = lv.price + adverse_ticks * tick if buy else lv.price - adverse_ticks * tick
        if limit is not None:
            px = min(px, limit) if buy else max(px, limit)   # a LIMIT order never fills beyond its limit
        elif not buy:
            px = max(px, tick)                               # an unlimited sell never fills below one tick
        out.append((i, round(px, 2), take))
        filled += take
    return out


def fill_from_depth(levels: Iterable | None, side: Side, qty: int, limit: float | None, lot: int,
                    participation: float, adverse_ticks: int, tick: float, *, draw: float) -> list[tuple[float, int]]:
    """[(px, qty)] an IOC order gets from this book (DETAIL §6.3; paper model 2). Pure.

    Levels are walked best first (asks for a BUY, bids for a SELL); the walk stops at the first level beyond the
    limit (None = no limit, as for the broker's square-off). Through each level the order has earned
    s = walked qty x participation / lot lots, walked qty being the shown qty of the levels walked so far. It may
    hold min(qty, n x lot): n is floor(s), plus one when draw is below the leftover fraction s - floor(s). draw is
    the order's own number in [0, 1) (fill_draw()), the same at every level, so the expected fill is participation
    x the walked depth (up to qty). What a level adds fills at its price moved adverse_ticks against us, never
    beyond the limit. So a lone one-lot level at 0.5 fills when draw < 0.5, and three one-lot levels fill one lot at
    the first level when draw < 0.5, else at the second. Where every share is whole lots the draw never matters and
    this is DETAIL §6.3's per-level floor(level.qty x participation / lot) exactly. Whatever is not listed is
    unfilled.
    """
    return [(px, q) for _, px, q in _depth_fills(levels, side, qty, limit, lot, participation, adverse_ticks, tick,
                                                 draw)]


def latency_seed(session: date, account: Account | str) -> int:
    """seed = int(sha256(f"{session}|{account}")) (DETAIL §6.3): one latency sequence per session and account."""
    text = f"{session.isoformat()}|{Account(account).value}"
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16)


def read_latency_seed(session: date, account: Account | str) -> int:
    """seed = int(sha256(f"{session}|{account}|reads")): the broker-read delay's own sequence per session and
    account, apart from the order latency's, so a read never shifts an order's draw."""
    text = f"{session.isoformat()}|{Account(account).value}|reads"
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16)


def fill_seed(session: date, account: Account | str, tag: str) -> int:
    """seed = int(sha256(f"{session}|{account}|fill|{tag}")) (paper model 2): each order's own fill draw, apart from
    the latency sequences. It follows the order's tag, not how many orders came before it."""
    text = f"{session.isoformat()}|{Account(account).value}|fill|{tag}"
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16)


def fill_draw(session: date, account: Account | str, tag: str) -> float:
    """The order's draw for fill_from_depth(): the first random() of random.Random(fill_seed(...)), in [0, 1). The
    same order always gets the same draw, so a replay of a day gives the same fills."""
    return random.Random(fill_seed(session, account, tag)).random()


# ---------------------------------------------------------------------------------------------------------------
# paper_exchange.db
# ---------------------------------------------------------------------------------------------------------------
class PaperStoreError(Exception):
    """paper_exchange.db cannot be opened as a paper exchange database."""


def _connect(path: str | Path) -> sqlite3.Connection:
    # check_same_thread=False: built on one thread, used by paper-sim; PaperExchange's thread check is the guard.
    memory = str(path) == ":memory:"
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        conn.row_factory = sqlite3.Row
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0 and conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]:
            raise PaperStoreError(f"{path}: has tables but no paper schema version; not a paper exchange database")
        if version > SCHEMA_VERSION:
            raise PaperStoreError(f"{path}: schema version {version} is newer than this code ({SCHEMA_VERSION})")
        journal = str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
        if journal != ("memory" if memory else "wal"):
            raise PaperStoreError(f"{path}: journal_mode is {journal}, not WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        if version < SCHEMA_VERSION:
            # a new file gets the whole schema; a v1 file gains p_orders.book in one transaction (without it every
            #   paper INSERT would fail, exits included). Old rows keep book NULL: they count for the account only
            script = _DDL if version == 0 else "ALTER TABLE p_orders ADD COLUMN book TEXT;"
            try:
                conn.executescript(f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
    except BaseException:
        conn.close()
        raise
    return conn


def _split_symbol(symbol: str) -> tuple[str, str]:
    """'NFO:RELIANCE26OCT2900CE' -> ('NFO', 'RELIANCE26OCT2900CE')."""
    exchange, sep, tradingsymbol = symbol.partition(":")
    return (exchange, tradingsymbol) if sep else (NFO, symbol)


def _broker_order(row: sqlite3.Row) -> BrokerOrder:
    exchange, tradingsymbol = _split_symbol(row["symbol"])
    status = "WORKING" if row["status"] == OPEN else row["status"]
    filled = row["filled_qty"]
    return BrokerOrder(
        Account(row["account"]), row["broker_order_id"], row["tag"], row["symbol"], tradingsymbol, exchange,
        row["product"], Side(row["side"]), row["order_type"], row["validity"], row["qty"], filled,
        row["avg_px"] if filled > 0 else None, status, row["status"], row["status_msg"],
        parse_wall(row["placed_ts"]), parse_wall(row["terminal_ts"]) if row["terminal_ts"] else None,
        row["placed_by"],
    )


def _request_of(row: sqlite3.Row) -> OrderRequest:
    """The OrderRequest an API row was accepted from (its tag passed order_request_problem, so it parses)."""
    exchange, tradingsymbol = _split_symbol(row["symbol"])
    return OrderRequest(Account(row["account"]), row["tag"], parse_tag(row["tag"]).role, row["symbol"],
                        tradingsymbol, exchange, row["token"], Side(row["side"]), row["qty"], row["limit_px"],
                        row["lot_size"], row["tick"], row["book"])


def _broker_trade(row: sqlite3.Row) -> BrokerTrade:
    wall = parse_wall(row["fill_ts"])
    return BrokerTrade(Account(row["account"]), row["trade_id"], row["broker_order_id"], row["symbol"],
                       Side(row["side"]), row["qty"], row["px"], wall, wall)


# ---------------------------------------------------------------------------------------------------------------
# PaperExchange
# ---------------------------------------------------------------------------------------------------------------
class PaperExchange:
    """BrokerPort for accounts PAPER and SHADOW. Only the paper-sim thread may call it (thread_check=False in tests).

    instruments: symbol -> Instrument (the day's dump, e.g. InstrumentStore.by_symbol). A symbol it does not know
    is still known through this exchange's own accepted orders on it, so exits keep working before prep loads the
    dump; otherwise it is unknown and rejected. request_depth: posts FetchQuotes to lane-reads when the fill-time
    book is missing (the runtime binds it to ReadsLane.submit with a reply that wakes PaperSim); None = only wait.
    """

    accounts = PAPER_ACCOUNTS

    def __init__(self, db_path: str | Path, *, settings: Settings, clock: Clock, quotes: QuoteCache,
                 calendar: SessionCalendar, instruments: Callable[[str], Instrument | None] | None = None,
                 request_depth: Callable[[FetchQuotes], None] | None = None,
                 margin_model: MarginModel | None = None, thread_check: bool = True) -> None:
        if not isinstance(settings, Settings):
            raise TypeError(f"settings must be a Settings, got {type(settings).__name__}")
        if type(thread_check) is not bool:
            raise TypeError("thread_check must be a bool")
        self._path = db_path
        self._settings = settings
        self._clock = clock
        self._quotes = quotes
        self._calendar = calendar
        self._instruments = instruments
        self._request_depth = request_depth
        self._margin: MarginModel = margin_model if margin_model is not None else PremiumMargin()
        self._thread_check = thread_check
        self._conn: sqlite3.Connection | None = None
        self._started = False
        self._closed = False
        self._due: list[tuple[float, int, str]] = []          # heap of (due_mono, seq, broker_order_id)
        self._seq = itertools.count()
        self._waiting: dict[str, float] = {}                  # order past due without fresh depth -> give-up mono
        self._placed: dict[str, float] = {}                   # OPEN order -> placement mono (RAM only)
        # paper model 2, RAM only like _placed: an OPEN order's decision-quote exchange time, from its request (None:
        #   unstamped, or re-armed after a restart) and, while it waits, why the last book it saw was unusable (its
        #   SIM reason)
        self._decided_exch: dict[str, datetime | None] = {}
        self._unusable: dict[str, str] = {}
        self._sqoff_wait: dict[tuple[Account, str], float] = {}   # square-off waiting for depth -> wait's start mono
        self._sqoff_done: set[tuple[Account, date]] = set()   # mirrors p_meta last_squareoff_session
        self._rngs: dict[tuple[Account, date], random.Random] = {}
        self._read_rngs: dict[tuple[Account, date], random.Random] = {}
        self._outbox: list[Event] = []
        self._request_ids = itertools.count(1)

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def settings(self) -> Settings:
        return self._settings

    # -- lifecycle ----------------------------------------------------------------------------------------------
    def start(self) -> None:
        """Open the DB and apply the startup rules (DETAIL §6.4) once. Every other public method calls it first."""
        self._ensure("start")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
        self._conn = None
        self._closed = True

    def _ensure(self, what: str) -> None:
        if self._thread_check and threading.current_thread().name != PAPER_SIM_THREAD:
            raise InvariantViolation("PAPER_THREAD", f"PaperExchange.{what} called on thread "
                                                     f"{threading.current_thread().name!r}; paper state is owned by "
                                                     f"{PAPER_SIM_THREAD}")
        if self._closed:
            raise RuntimeError("PaperExchange is closed")
        if not self._started:
            self._startup()

    def _startup(self) -> None:
        if self._conn is None:
            self._conn = _connect(self._path)
        now_wall, now_mono = self._clock.now_wall(), self._clock.mono()
        for row in self._conn.execute("SELECT * FROM p_orders WHERE status = 'OPEN' "
                                      "ORDER BY placed_ts, broker_order_id").fetchall():
            due_wall = parse_wall(row["due_ts"])
            if due_wall + timedelta(seconds=PAPER_EXPIRED_GRACE_S) < now_wall:
                log.warning("paper order %s (%s) expired while the simulator was down", row["broker_order_id"],
                            row["tag"])
                self._finish(row, [], MSG_EXPIRED, now_mono, now_wall)
            else:
                # mono is never persisted: re-anchor the due time from the wall clock. Only a book received after
                # this restart is known to post-date the placement (the decision quote's exchange time is RAM only
                # too: a re-armed order has none, and that rule alone applies).
                self._arm(row["broker_order_id"], now_mono + max(0.0, (due_wall - now_wall).total_seconds()),
                          now_mono)
        session = session_of(now_wall)
        for account in (Account.PAPER, Account.SHADOW):
            self._settle_carried(account, session, now_wall)
        self._started = True

    def _settle_carried(self, account: Account, session: date, now_wall: datetime) -> None:
        """A position from an earlier session missed its square-off: the exchange closes it now at Rs 0.05 (a
        near-total loss; a short is bought back at 1.05 x the highest price known). Once per session, on a trading
        day only (review round 2): a weekend or weekday-holiday start settles nothing, since no exchange runs and
        a settlement dated that day is in no trading day's orders, where recon books it; the next trading day's
        start settles it."""
        if not self._calendar.is_trading_day(session):
            return
        key = f"startup_settled:{account}"
        if self._meta_get(key) == session.isoformat():
            return
        with self._tx():
            for symbol, net in self._nets(account, before=session):
                side = Side.SELL if net > 0 else Side.BUY
                contract = self._held_contract(symbol)
                px = PAPER_MISSED_SQOFF_PX if side is Side.SELL else self._buyback_px(symbol, contract[2])
                oid = self._insert_exchange_order(account, symbol, contract, side, abs(net), [(0, px, abs(net))],
                                                  MSG_MISSED_SQOFF, now_wall)
                log.warning("%s %s: %d left from an earlier session; settled by %s %s @ %.2f (%s)", account,
                            symbol, net, oid, side, px, MSG_MISSED_SQOFF)
            self._meta_set(key, session.isoformat())

    # -- orders (BrokerPort) ------------------------------------------------------------------------------------
    def place(self, req: OrderRequest) -> PlaceResult:
        """Accept (OK, WORKING snapshot queued) or reject; never raises for a broker-side reason."""
        self._ensure("place")
        if not isinstance(req, OrderRequest):
            raise TypeError(f"place() needs an OrderRequest, got {type(req).__name__}")
        problem = self._local_problem(req)
        if problem is not None:
            log.error("paper order %s refused locally: %s", req.tag, problem)
            return PlaceResult(req.account, req.tag, REJECTED, None, LOCAL, f"{LOCAL}: {problem}")
        account, side = Account(req.account), Side(req.side)
        now_wall, now_mono = self._clock.now_wall(), self._clock.mono()
        session = session_of(now_wall)
        if self._conn.execute("SELECT 1 FROM p_orders WHERE account = ? AND tag = ? AND placed_by = 'API'",
                              (account.value, req.tag)).fetchone():
            log.error("paper order %s: %s", req.tag, MSG_DUPLICATE)
            return PlaceResult(account, req.tag, REJECTED, None, SIM, MSG_DUPLICATE)
        contract = self._contract(req.symbol)
        reason = self._exchange_problem(req, contract, now_wall, session)
        _, lot, tick = contract if contract is not None else (req.token, req.lot_size, req.tick)
        latency_ms = 0.0
        try:
            with self._tx():
                oid = self._next_order_id(session)
                if reason is None:
                    latency_ms = self._latency_ms(account, session)
                    status, due_ts, terminal_ts = OPEN, iso(now_wall + timedelta(milliseconds=latency_ms)), None
                else:
                    status, due_ts, terminal_ts = REJECTED, iso(now_wall), iso(now_wall)
                self._conn.execute(
                    "INSERT INTO p_orders(broker_order_id, account, tag, symbol, token, side, product, order_type, "
                    "validity, qty, lot_size, tick, limit_px, placed_ts, due_ts, status, filled_qty, avg_px, "
                    "status_msg, terminal_ts, placed_by, book) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,?,?,?,?)",
                    (oid, account.value, req.tag, req.symbol, int(req.token), side.value, MIS, LIMIT, IOC,
                     int(req.qty), int(lot), float(tick), float(req.limit_px), iso(now_wall), due_ts, status,
                     reason, terminal_ts, API, req.book))
        except BaseException:
            self._rngs.pop((account, session), None)     # rebuilt from the persisted draw count on next use
            raise
        if reason is not None:
            log.warning("paper order %s %s on %s rejected: %s", oid, req.tag, req.symbol, reason)
            return PlaceResult(account, req.tag, REJECTED, oid, SIM, reason)
        self._arm(oid, now_mono + latency_ms / 1000.0, now_mono)
        # the exchange time of the quote the engine priced the order from, carried by the request. Not the newest
        #   quote in the cache now: a lagged WS packet received between the decision and this call would pass for
        #   the decision, and a later lagged one, still older than the real decision, would fill the order
        stamp = req.decision_exch_wall
        if stamp is not None and not (isinstance(stamp, datetime) and stamp.tzinfo is not None):
            log.warning("paper order %s %s: decision time %r is not an aware datetime; ignored", oid, req.tag, stamp)
            stamp = None
        self._decided_exch[oid] = stamp
        self._outbox.append(snapshot_of(_broker_order(self._row(oid)), SIM, now_mono))
        log.info("paper order %s %s %s %d %s @ %.2f accepted, due in %.0f ms", oid, req.tag, side, req.qty,
                 req.symbol, req.limit_px, latency_ms)
        return PlaceResult(account, req.tag, OK, oid)

    def cancel(self, account: Account, order_pk: str, broker_order_id: str | None) -> CancelResult:
        """An OPEN order becomes CANCELLED with 0 filled; anything else is BENIGN (then poll), as on Kite."""
        self._ensure("cancel")
        if account not in self.accounts:
            return CancelResult(account, order_pk, REJECTED, f"{LOCAL}: PaperExchange cancels PAPER and SHADOW only")
        if not broker_order_id:
            return CancelResult(account, order_pk, REJECTED, f"{LOCAL}: no broker_order_id to cancel")
        row = self._row(broker_order_id)
        if row is None or row["account"] != Account(account).value:
            return CancelResult(account, order_pk, BENIGN, f"SIM: order {broker_order_id} not found")
        if row["status"] != OPEN:
            return CancelResult(account, order_pk, BENIGN, f"SIM: order already {row['status'].lower()}")
        self._waiting.pop(broker_order_id, None)
        self._finish(row, [], "SIM: cancelled by user", self._clock.mono(), self._clock.now_wall())
        return CancelResult(account, order_pk, OK, None)

    # -- the simulation -----------------------------------------------------------------------------------------
    def step(self, now_mono: float | None = None) -> list[Event]:
        """Process everything due, then the square-off; return every event queued since the last call (the WORKING
        snapshots of new orders first). The caller delivers them: PaperSim posts them to core_q."""
        self._ensure("step")
        now = self._clock.mono() if now_mono is None else now_mono
        now_wall = self._clock.now_wall()
        while self._due and self._due[0][0] <= now:
            _, _, oid = heapq.heappop(self._due)
            self._try_fill(oid, now, now_wall)
        for oid in list(self._waiting):
            self._try_fill(oid, now, now_wall)
        self._maybe_squareoff(now, now_wall)
        return self.take_events()

    def take_events(self) -> list[Event]:
        out, self._outbox = self._outbox, []
        return out

    def _arm(self, oid: str, due_mono: float, placed_mono: float) -> None:
        self._placed[oid] = placed_mono
        heapq.heappush(self._due, (due_mono, next(self._seq), oid))

    def _try_fill(self, oid: str, now_mono: float, now_wall: datetime) -> None:
        row = self._row(oid)
        if row is None or row["status"] != OPEN:
            self._waiting.pop(oid, None)
            self._placed.pop(oid, None)
            self._decided_exch.pop(oid, None)
            self._unusable.pop(oid, None)
            return
        # never armed here (cannot happen: place() and startup arm every OPEN order): only a book from now on counts
        placed = self._placed.setdefault(oid, now_mono)
        q, why = self._fill_quote(row["symbol"], now_mono, newer_than=placed, decided_exch=self._decided_exch.get(oid))
        if q is None:
            if why is not None:
                self._unusable[oid] = why                # a book came, but it cannot fill: wait as for none
            give_up = self._waiting.get(oid)
            if give_up is None:
                self._waiting[oid] = now_mono + PAPER_DEPTH_WAIT_S
                self._ask_depth(row["symbol"], Account(row["account"]), oid)
            elif now_mono >= give_up:
                del self._waiting[oid]
                msg = self._unusable.get(oid, MSG_NO_DEPTH)
                log.warning("paper order %s %s: no usable depth for %s within %.1f s (%s)", oid, row["tag"],
                            row["symbol"], PAPER_DEPTH_WAIT_S, msg)
                self._finish(row, [], msg, now_mono, now_wall)
            return
        self._waiting.pop(oid, None)
        side, s = Side(row["side"]), self._settings
        draw = fill_draw(session_of(parse_wall(row["placed_ts"])), row["account"], row["tag"])
        fills = _depth_fills(q.asks if side is Side.BUY else q.bids, side, row["qty"], row["limit_px"],
                             row["lot_size"], s.PAPER_PARTICIPATION, s.PAPER_ADVERSE_TICKS, row["tick"], draw)
        filled = sum(f[2] for f in fills)
        msg = (None if filled == row["qty"] else "SIM: IOC unfilled" if filled == 0
               else f"SIM: IOC partial fill {filled}/{row['qty']}")
        self._finish(row, fills, msg, now_mono, now_wall, book=_book_note(q, row, side, now_wall, draw))

    def _finish(self, row: sqlite3.Row, fills: Sequence[tuple[int, float, int]], msg: str | None,
                now_mono: float, now_wall: datetime, *, book: str | None = None) -> None:
        """Make an OPEN order terminal with these fills: p_fills rows and the order in one transaction, then queue
        its terminal snapshot (API orders). book: the fill book's note for the log line (_book_note)."""
        oid = row["broker_order_id"]
        filled = sum(q for _, _, q in fills)
        value = math.fsum(px * q for _, px, q in fills)
        status = COMPLETE if filled == row["qty"] else CANCELLED
        ts = iso(now_wall)
        with self._tx():
            self._insert_fills(oid, row["account"], row["symbol"], row["side"], fills, ts)
            cur = self._conn.execute(
                "UPDATE p_orders SET status = ?, filled_qty = ?, avg_px = ?, status_msg = ?, terminal_ts = ? "
                "WHERE broker_order_id = ? AND status = 'OPEN'",
                (status, filled, value / filled if filled else None, msg, ts, oid))
            if cur.rowcount != 1:
                raise RuntimeError(f"paper order {oid} was not OPEN when finishing it")
        self._placed.pop(oid, None)
        self._decided_exch.pop(oid, None)
        self._unusable.pop(oid, None)
        log.info("paper order %s %s %s: %d/%d filled%s%s", oid, row["tag"], status, filled, row["qty"],
                 f" avg {value / filled:.4f}" if filled else "", f"; {book}" if book else "")
        if row["placed_by"] == API:
            self._outbox.append(snapshot_of(_broker_order(self._row(oid)), SIM, now_mono))

    def _fill_quote(self, symbol: str, now_mono: float, newer_than: float | None = None,
                    decided_exch: datetime | None = None, crossed_ok: bool = False,
                    max_age_s: float | None = None) -> tuple[Quote | None, str | None]:
        """(the book at fill time, None), or (None, why) when there is none: why is None when no fresh book was
        received after newer_than, else the SIM reason the newest one cannot be used.

        The book at fill time is fresh within PAPER_BOOK_MAX_AGE_S, received after newer_than (an order's placement
        mono: the snapshot it was decided on is never its fill-time book) and stamped at or after the open + 30 s.
        Paper model 2 adds two rules. It is neither crossed nor locked (best bid >= best ask: a side left behind by
        a move of the other, which no exchange shows). And its exchange time is not in an earlier second than
        decided_exch, that of the quote the order was decided on: a WS packet that left the exchange before that
        quote but reached us after the order (feed lag) shows a market that was already gone. Kite stamps whole
        seconds, so two quotes within one second are never told apart. crossed_ok: a crossed or locked book counts
        too (the square-off's last resort, which fills it at its worse side). max_age_s replaces PAPER_BOOK_MAX_AGE_S
        (the square-off's give-up: any book received since its wait began)."""
        max_age = self._settings.PAPER_BOOK_MAX_AGE_S if max_age_s is None else max_age_s
        q = self._quotes.fresh(symbol, max_age, now_mono)
        if q is None or (newer_than is not None and q.recv_mono <= newer_than):
            return None, None
        valid_from = self._calendar.quotes_valid_from(session_of(q.recv_wall))
        if valid_from is None or q.recv_wall < valid_from or (q.exch_wall is not None and q.exch_wall < valid_from):
            return None, None                            # an opening carry-over (or a non-trading day)
        if _crossed(q) and not crossed_ok:
            return None, MSG_CROSSED_BOOK
        if decided_exch is not None and q.exch_wall is not None and q.exch_wall < decided_exch.replace(microsecond=0):
            return None, MSG_OLDER_BOOK
        return q, None

    def _ask_depth(self, symbol: str, account: Account, key: str) -> None:
        if self._request_depth is None:
            return
        prio = Priority.PAPER if account is Account.PAPER else Priority.SHADOW
        cmd = FetchQuotes(f"paper:{key}:{next(self._request_ids)}", (symbol,), int(prio))
        try:
            self._request_depth(cmd)
        except Exception:
            log.exception("paper: REST depth request for %s failed; waiting for the feed only", symbol)

    # -- square-off ---------------------------------------------------------------------------------------------
    def _maybe_squareoff(self, now_mono: float, now_wall: datetime) -> None:
        """At BROKER_SQUAREOFF_AT, once per session and account: every non-zero MIS position is closed by the
        exchange (DETAIL §6.6). It waits for the account's OPEN orders to resolve and for depth (up to 1.5 s). A
        crossed or locked book is waited out like a missing one; if the wait ends with only such a book, received
        since the wait began however old it is by then, the position is closed on it at its worse side, never at
        the no-book fallback (Rs 0.05 for a long)."""
        session = session_of(now_wall)
        if not self._calendar.is_trading_day(session):
            return
        if now_wall < ist_datetime(session, self._settings.BROKER_SQUAREOFF_AT):
            return
        for account in (Account.PAPER, Account.SHADOW):
            key = f"last_squareoff_session:{account}"
            if (account, session) in self._sqoff_done:
                continue
            if self._meta_get(key) == session.isoformat():
                self._sqoff_done.add((account, session))
                continue
            if self._conn.execute("SELECT 1 FROM p_orders WHERE account = ? AND status = 'OPEN' LIMIT 1",
                                  (account.value,)).fetchone():
                continue                                   # the bot's last IOC orders resolve within ~4.5 s
            pending = False
            for symbol, net in self._nets(account):
                q, _ = self._fill_quote(symbol, now_mono)      # a crossed or locked book is waited out, as none
                wkey = (account, symbol)
                if q is None:
                    began = self._sqoff_wait.get(wkey)
                    if began is None:
                        self._sqoff_wait[wkey] = now_mono
                        self._ask_depth(symbol, account, f"sqoff:{account}:{symbol}")
                        pending = True
                        continue
                    if now_mono < began + PAPER_DEPTH_WAIT_S:
                        pending = True
                        continue
                    # the wait is over: a book that is only crossed or locked is still a book, filled at its worse
                    #   side (_sqoff_fills); a stale side is no reason to sell a long at Rs 0.05. Any book received
                    #   since the wait began counts, also when it is older than PAPER_BOOK_MAX_AGE_S by now: on a thin
                    #   option's silent feed the only book can be the REST answer early in the wait (review round 4).
                    #   A book from before the wait never does.
                    q, _ = self._fill_quote(symbol, now_mono, crossed_ok=True, max_age_s=now_mono - began)
                    if q is None:
                        log.warning("%s square-off of %s: no fresh depth; conservative fill", account, symbol)
                    else:
                        log.warning("%s square-off of %s: only a crossed or locked book; filled at its worse side",
                                    account, symbol)
                self._sqoff_wait.pop(wkey, None)
                side = Side.SELL if net > 0 else Side.BUY
                contract = self._held_contract(symbol)
                fills = self._sqoff_fills(symbol, side, abs(net), q, contract[2])
                with self._tx():
                    oid = self._insert_exchange_order(account, symbol, contract, side, abs(net), fills, MSG_SQOFF,
                                                      now_wall)
                log.warning("%s %s: auto square-off %s %s %d (avg %.4f)", account, symbol, oid, side, abs(net),
                            math.fsum(px * qty for _, px, qty in fills) / abs(net))
            if not pending:
                with self._tx():
                    self._meta_set(key, session.isoformat())
                self._sqoff_done.add((account, session))

    def _sqoff_fills(self, symbol: str, side: Side, qty: int, q: Quote | None,
                     tick: float) -> list[tuple[int, float, int]]:
        """Walk every shown level at participation 1.0; the rest at the deepest level x 0.95 (sell) / x 1.05 (buy).
        A crossed or locked book fills at its worse side: no level counts better than the other side's best, so a
        sell walks the bids at most at the best ask and a buy the asks at least at the best bid (min / max of bid and
        ask at the touch). No book side at all: a sell gets Rs 0.05, a buy 1.05 x the highest price known."""
        sell = side is Side.SELL
        shown = (q.bids if sell else q.asks) if q is not None else ()
        book = _book(shown, best_is_high=sell)
        if q is not None and _crossed(q):
            other = q.asks[0].price if sell else q.bids[0].price
            book = [Level(min(lv.price, other) if sell else max(lv.price, other), lv.qty, lv.orders) for lv in book]
        # lot 1 at participation 1.0: every share is whole units, so the draw never matters
        fills = _depth_fills(book, side, qty, None, 1, 1.0, self._settings.PAPER_ADVERSE_TICKS, tick, 0.0)
        left = qty - sum(f[2] for f in fills)
        if left > 0:
            if book and sell:
                px = max(tick, floor_tick(book[-1].price * PAPER_SQOFF_SELL_FACTOR, tick))
            elif book:
                px = ceil_tick(book[-1].price * PAPER_SQOFF_BUY_FACTOR, tick)
            else:
                px = PAPER_MISSED_SQOFF_PX if sell else self._buyback_px(symbol, tick)
            fills.append((len(book), round(px, 2), left))
        return fills

    def _buyback_px(self, symbol: str, tick: float) -> float:
        """A conservative buy-back price with no asks shown: 1.05 x the highest price known for the symbol."""
        prices = [r[0] for r in self._conn.execute("SELECT px FROM p_fills WHERE symbol = ?", (symbol,))]
        last = self._quotes.last(symbol)
        if last is not None:
            prices += [lv.price for lv in last.asks + last.bids] + ([last.ltp] if last.ltp else [])
        top = max((p for p in prices if _finite(p) and p > 0), default=PAPER_MISSED_SQOFF_PX)
        return ceil_tick(top * PAPER_SQOFF_BUY_FACTOR, tick)

    def _held_contract(self, symbol: str) -> tuple[int, int, float]:
        """The contract of a held symbol. Its fills came from an accepted order, so it is always known."""
        contract = self._contract(symbol)
        if contract is None:
            raise RuntimeError(f"paper position in {symbol} has no accepted order to take its contract from")
        return contract

    def _insert_exchange_order(self, account: Account, symbol: str, contract: tuple[int, int, float], side: Side,
                               qty: int, fills: Sequence[tuple[int, float, int]], msg: str, now_wall: datetime) -> str:
        """An EXCHANGE order filled at once (caller holds the transaction). Returns its id."""
        token, lot, tick = contract
        oid = self._next_order_id(session_of(now_wall))
        value = math.fsum(px * q for _, px, q in fills)
        ts = iso(now_wall)
        self._conn.execute(
            "INSERT INTO p_orders(broker_order_id, account, tag, symbol, token, side, product, order_type, validity, "
            "qty, lot_size, tick, limit_px, placed_ts, due_ts, status, filled_qty, avg_px, status_msg, terminal_ts, "
            "placed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,?,?,?,?)",
            (oid, account.value, SQOFF_TAG, symbol, int(token), side.value, MIS, SQOFF_ORDER_TYPE, SQOFF_VALIDITY,
             qty, int(lot), float(tick), ts, ts, COMPLETE, qty, value / qty, msg, ts, EXCHANGE))
        self._insert_fills(oid, account.value, symbol, side.value, fills, ts)
        return oid

    # -- account reads (BrokerPort) -----------------------------------------------------------------------------
    def order_history(self, account: Account, broker_order_id: str) -> BrokerOrder | None:
        """Today's order with this id on this account, else None (like Kite, earlier days are not served)."""
        acct = self._read_account(account, "order_history")
        row = self._row(broker_order_id)
        if row is None or row["account"] != acct.value or row["placed_ts"][:10] != self._today():
            return None
        return _broker_order(row)

    def order_trades(self, account: Account, broker_order_id: str) -> tuple[BrokerTrade, ...]:
        acct = self._read_account(account, "order_trades")
        if self.order_history(acct, broker_order_id) is None:
            return ()
        rows = self._conn.execute("SELECT * FROM p_fills WHERE broker_order_id = ? ORDER BY trade_id",
                                  (broker_order_id,))
        return tuple(_broker_trade(r) for r in rows)

    def orders_today(self, account: Account) -> tuple[BrokerOrder, ...]:
        acct = self._read_account(account, "orders")
        rows = self._conn.execute("SELECT * FROM p_orders WHERE account = ? AND substr(placed_ts, 1, 10) = ? "
                                  "ORDER BY placed_ts, broker_order_id", (acct.value, self._today()))
        return tuple(_broker_order(r) for r in rows)

    def trades_today(self, account: Account) -> tuple[BrokerTrade, ...]:
        acct = self._read_account(account, "trades")
        rows = self._conn.execute("SELECT * FROM p_fills WHERE account = ? AND substr(fill_ts, 1, 10) = ? "
                                  "ORDER BY fill_ts, trade_id", (acct.value, self._today()))
        return tuple(_broker_trade(r) for r in rows)

    def positions(self, account: Account) -> tuple[BrokerPosition, ...]:
        """Per (symbol, MIS): net_qty = signed sum of every fill; buy/sell qty and value are today's. Rows for every
        symbol traded today or still held."""
        acct = self._read_account(account, "positions")
        today = self._today()
        agg: dict[str, dict] = {}
        for r in self._conn.execute("SELECT symbol, side, qty, px, fill_ts FROM p_fills WHERE account = ? "
                                    "ORDER BY trade_id", (acct.value,)):
            a = agg.setdefault(r["symbol"], {"net": 0, "today": False, "BUY": [0, []], "SELL": [0, []]})
            a["net"] += r["qty"] if r["side"] == Side.BUY.value else -r["qty"]
            if r["fill_ts"][:10] == today:
                a["today"] = True
                a[r["side"]][0] += r["qty"]
                a[r["side"]][1].append(r["qty"] * r["px"])
        out = []
        for symbol in sorted(agg):
            a = agg[symbol]
            if a["net"] == 0 and not a["today"]:
                continue
            exchange, tradingsymbol = _split_symbol(symbol)
            # fill prices have at most 2 decimals, so rounding to paise only removes float noise
            out.append(BrokerPosition(acct, symbol, tradingsymbol, exchange, MIS, a["net"], a["BUY"][0],
                                      a["SELL"][0], charges.round_money(math.fsum(a["BUY"][1])),
                                      charges.round_money(math.fsum(a["SELL"][1]))))
        return tuple(out)

    def squareoffs(self, account: Account, symbols: Sequence[str],
                   since: date) -> tuple[tuple[BrokerOrder, ...], dict[str, int]]:
        """FetchSquareoffs (the §9.6 step 7 backstop, review round 2): this exchange's own square-off orders (placed
        by EXCHANGE: the BROKER_SQUAREOFF_AT square-off and the startup settlement) on `symbols`, placed on session
        `since` or later but before today, and each symbol's net at the start of today. Kite serves no earlier day's
        orders; this exchange keeps them, so a leftover it closed on an earlier day that the bot never booked can be
        booked from its own record."""
        acct = self._read_account(account, "squareoffs")
        today = self._today()
        syms = sorted(set(symbols))
        if not syms:
            return (), {}
        rows = self._conn.execute(
            f"SELECT * FROM p_orders WHERE account = ? AND placed_by = 'EXCHANGE' AND symbol IN "
            f"({','.join('?' * len(syms))}) AND substr(placed_ts, 1, 10) >= ? AND substr(placed_ts, 1, 10) < ? "
            "ORDER BY placed_ts, broker_order_id", (acct.value, *syms, since.isoformat(), today)).fetchall()
        carried = dict.fromkeys(syms, 0)
        for symbol, net in self._nets(acct, before=date.fromisoformat(today)):
            if symbol in carried:
                carried[symbol] = net
        return tuple(_broker_order(r) for r in rows), carried

    def margins(self, account: Account) -> Margins:
        acct = self._read_account(account, "margins")
        funds = self._funds(acct, session_of(self._clock.now_wall()))
        return Margins(acct, charges.round_money(funds["available"]), funds)

    def contract_note(self, account: Account, orders: Sequence[ContractNoteOrder]) -> tuple[ContractNoteRow, ...]:
        """Always refused: ReadFailed REJECTED SIM, which the lanes turn into ContractNoteResult(ok=False).

        The contract-note check (§8.1) tests the RateSet against Zerodha's own calculator before G1 certifies an edge
        "after costs". A note from this exchange could only come from titan2.charges, the engine's own model, so it
        would match a wrong RateSet to the paisa and CHARGES_DRIFT could never fire. The refusal makes the engine
        record VCN_PAPER_UNSUPPORTED instead of a pass. §8.1 prices the PAPER sample with
        kite.get_virtual_contract_note on lane-reads (synthetic ids); paper fills are cross-checked by recon and the
        EOD truth check (§9.8), not here.
        """
        self._read_account(account, "contract note")
        raise ReadFailed(format_error(REJECTED, SIM, MSG_NO_CONTRACT_NOTE))      # the lanes log the failed read

    def _read_account(self, account: Account, what: str) -> Account:
        self._ensure(what)
        if account not in self.accounts:
            raise ReadFailed(format_error(REJECTED, LOCAL, f"{what}: PaperExchange serves PAPER and SHADOW, "
                                                           f"not {account}"))
        return Account(account)

    # -- validation and funds -----------------------------------------------------------------------------------
    def _local_problem(self, req: OrderRequest) -> str | None:
        """What KiteBroker would refuse before sending anything."""
        if req.account not in self.accounts:
            return f"PaperExchange places accounts PAPER and SHADOW only, not {req.account}"
        problem = order_request_problem(req)
        if problem is not None:
            return problem
        if req.symbol != f"{req.exchange}:{req.tradingsymbol}":
            return f"symbol {req.symbol!r} is not {req.exchange}:{req.tradingsymbol}"
        return None

    def _exchange_problem(self, req: OrderRequest, contract: tuple[int, int, float] | None, now_wall: datetime,
                          session: date) -> str | None:
        """What the exchange or the broker's RMS would reject (DETAIL §6.2), checked on the real contract."""
        if contract is None:
            return MSG_UNKNOWN
        bounds = self._calendar.session_bounds(session)
        if bounds is None or not bounds[0] <= now_wall < bounds[1]:
            return MSG_CLOSED
        _, lot, tick = contract
        if req.qty % lot:
            return f"SIM: qty {req.qty} is not a multiple of lot {lot}"
        if not is_tick_multiple(req.limit_px, tick):
            return f"SIM: limit {req.limit_px} is not a multiple of tick {tick}"
        last = self._quotes.last(req.symbol)
        ltp = last.ltp if last is not None else None
        if (_finite(ltp) and ltp > 0
                and abs(req.limit_px / ltp - 1.0) > self._settings.PAPER_PRICE_BAND_PCT / 100.0 + 1e-12):
            return MSG_BAND
        need = self._margin.required(req, charges.rates_for(session))
        if need > 0:
            account = Account(req.account)
            # SHADOW books each have their own pool (F-19); a SHADOW request without a book: the whole account
            book = req.book if account is Account.SHADOW and isinstance(req.book, str) and req.book else None
            have = self._funds(account, session, book)["available"]
            if need > have + 1e-9:
                return f"{MSG_FUNDS}: need Rs {need:.2f}, available Rs {have:.2f}"
        return None

    def _contract(self, symbol: str) -> tuple[int, int, float] | None:
        """(token, lot, tick) of the real contract: the instruments dump, else this exchange's accepted orders."""
        if self._instruments is not None:
            try:
                inst = self._instruments(symbol)
            except Exception:
                log.exception("paper: instruments lookup of %s failed; using order history", symbol)
                inst = None
            if inst is not None:
                return int(inst.token), int(inst.lot_size), float(inst.tick)
        row = self._conn.execute("SELECT token, lot_size, tick FROM p_orders WHERE symbol = ? AND status <> 'REJECTED' "
                                 "ORDER BY placed_ts DESC, broker_order_id DESC LIMIT 1", (symbol,)).fetchone()
        return (int(row["token"]), int(row["lot_size"]), float(row["tick"])) if row else None

    def _funds(self, account: Account, session: date, book: str | None = None) -> dict:
        """available = capital + today's sells - buys - charges (net_pnl) - funds blocked by OPEN orders: of the whole
        account, or with book of that book's pool only (its own fills and OPEN orders; EXCHANGE orders have no book
        and count for no pool)."""
        rates = charges.rates_for(session)
        per_order: dict[str, tuple[str, list[BrokerTrade]]] = {}
        fills_sql = ("SELECT f.*, o.placed_by FROM p_fills f JOIN p_orders o ON o.broker_order_id = f.broker_order_id "
                     "WHERE f.account = ? AND substr(f.fill_ts, 1, 10) = ?")
        open_sql = "SELECT * FROM p_orders WHERE account = ? AND status = 'OPEN' AND placed_by = 'API'"
        args: list = [account.value, session.isoformat()]
        if book is not None:
            fills_sql += " AND o.book = ?"
            open_sql += " AND book = ?"
            args.append(book)
        for r in self._conn.execute(fills_sql + " ORDER BY f.trade_id", args):
            per_order.setdefault(r["broker_order_id"], (r["placed_by"], []))[1].append(_broker_trade(r))
        executed = [charges.OrderFills.from_fills(fills[0].side, fills,
                                                  charges.AUTO_SQUAREOFF if by == EXCHANGE else None)
                    for by, fills in per_order.values()]
        pnl = charges.net_pnl(executed, rates)
        blocked = math.fsum(self._margin.required(_request_of(r), rates)
                            for r in self._conn.execute(open_sql, [account.value] + args[2:]))
        capital = float(self._settings.PAPER_CAPITAL_RS)
        return {"session": session.isoformat(), "capital_rs": capital,
                "buy_value": charges.round_money(math.fsum(o.value for o in executed if o.side is Side.BUY)),
                "sell_value": charges.round_money(math.fsum(o.value for o in executed if o.side is Side.SELL)),
                "charges": pnl.charges.total, "realized_net": pnl.net, "blocked_rs": charges.round_money(blocked),
                "available": capital + pnl.net - blocked}

    # -- latency ------------------------------------------------------------------------------------------------
    def _latency_ms(self, account: Account, session: date) -> float:
        """The next draw of this session's and account's lognormal latency (caller holds the transaction). The draw
        count is persisted, so a restart continues the sequence instead of replaying it."""
        key = (account, session)
        draws_key = f"rng_draws:{account}:{session.isoformat()}"
        rng = self._rngs.get(key)
        if rng is None:
            seed = latency_seed(session, account)
            rng = random.Random(seed)
            for _ in range(int(self._meta_get(draws_key) or 0)):
                rng.lognormvariate(0.0, 1.0)             # uses the same uniforms whatever mu and sigma are
            self._rngs[key] = rng
            self._meta_set(f"rng_seed:{account}:{session.isoformat()}", str(seed))
        s = self._settings
        ms = rng.lognormvariate(math.log(s.PAPER_LATENCY_MEDIAN_MS), s.PAPER_LATENCY_SIGMA)
        self._meta_set(draws_key, str(int(self._meta_get(draws_key) or 0) + 1))
        lo, hi = PAPER_LATENCY_CLIP_MS
        return min(max(ms, lo), hi)

    def read_latency_ms(self, account: Account) -> float:
        """The next draw of this session's and account's broker-read delay: how long PaperSim holds a positions or
        orders read (decision B1). Lognormal PAPER_READ_LATENCY_MEDIAN_MS / _SIGMA clipped to PAPER_LATENCY_CLIP_MS,
        from its own sequence (read_latency_seed), so the order latency's draws are untouched. The draw count is
        persisted (p_meta read_draws:<account>:<day>), so a restart continues the sequence instead of replaying it."""
        self._ensure("read_latency_ms")
        acct = Account(account)
        session = session_of(self._clock.now_wall())
        key = (acct, session)
        draws_key = f"read_draws:{acct}:{session.isoformat()}"
        try:
            with self._tx():
                rng = self._read_rngs.get(key)
                if rng is None:
                    seed = read_latency_seed(session, acct)
                    rng = random.Random(seed)
                    for _ in range(int(self._meta_get(draws_key) or 0)):
                        rng.lognormvariate(0.0, 1.0)     # the same uniforms whatever mu and sigma are
                    self._read_rngs[key] = rng
                    self._meta_set(f"read_seed:{acct}:{session.isoformat()}", str(seed))
                s = self._settings
                ms = rng.lognormvariate(math.log(s.PAPER_READ_LATENCY_MEDIAN_MS), s.PAPER_READ_LATENCY_SIGMA)
                self._meta_set(draws_key, str(int(self._meta_get(draws_key) or 0) + 1))
        except BaseException:
            self._read_rngs.pop(key, None)               # rebuilt from the persisted draw count on next use
            raise
        lo, hi = PAPER_LATENCY_CLIP_MS
        return min(max(ms, lo), hi)

    # -- storage helpers ----------------------------------------------------------------------------------------
    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def _row(self, broker_order_id: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM p_orders WHERE broker_order_id = ?", (broker_order_id,)).fetchone()

    def _today(self) -> str:
        return session_of(self._clock.now_wall()).isoformat()

    def _meta_get(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM p_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _meta_set(self, key: str, value: str) -> None:
        self._conn.execute("INSERT INTO p_meta(key, value) VALUES (?, ?) "
                           "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def _next_order_id(self, session: date) -> str:
        """P<yymmdd><n:09d>; n counts per day across both accounts (caller holds the transaction)."""
        yymmdd = session.strftime("%y%m%d")
        key = f"order_seq:{yymmdd}"
        n = int(self._meta_get(key) or 0) + 1
        self._meta_set(key, str(n))
        return f"P{yymmdd}{n:09d}"

    def _insert_fills(self, oid: str, account: str, symbol: str, side: str,
                      fills: Sequence[tuple[int, float, int]], ts: str) -> None:
        for k, (level_idx, px, qty) in enumerate(fills):
            self._conn.execute("INSERT INTO p_fills(trade_id, broker_order_id, account, symbol, side, qty, px, "
                               "fill_ts, level_idx) VALUES (?,?,?,?,?,?,?,?,?)",
                               (f"{oid}-{k:02d}", oid, account, symbol, side, int(qty), float(px), ts, int(level_idx)))

    def _nets(self, account: Account, before: date | None = None) -> list[tuple[str, int]]:
        """[(symbol, signed net)] of the account's non-zero positions; with before, as they stood at its start."""
        sql = ("SELECT symbol, SUM(CASE WHEN side = 'BUY' THEN qty ELSE -qty END) AS net FROM p_fills "
               "WHERE account = ?")
        args: list = [account.value]
        if before is not None:
            sql += " AND substr(fill_ts, 1, 10) < ?"
            args.append(before.isoformat())
        sql += " GROUP BY symbol HAVING net <> 0 ORDER BY symbol"
        return [(r["symbol"], int(r["net"])) for r in self._conn.execute(sql, args)]


# ---------------------------------------------------------------------------------------------------------------
# PaperSim: the paper-sim thread
# ---------------------------------------------------------------------------------------------------------------
class PaperSim:
    """Runs PAPER/SHADOW broker commands FIFO through lanes.execute_broker (so paper answers with exactly the
    events Kite's lanes produce, source SIM) and steps the exchange between them, with a 10 ms condition wait.

    Positions and orders reads are held, not answered at once: each waits the account's next read delay
    (PaperExchange.read_latency_ms) from its receipt, behind the account's earlier reads (one FIFO per account, so
    answers never overtake each other, like one lane-reads per account). Each pass steps the exchange, then answers
    the reads now due: the ledger as it stands then, stamped with the receipt (req_mono). Every other command
    (orders, PollOrder, LookupTag, FetchTrades, FetchTradebook, FetchMargins, FetchSquareoffs) is answered at once.

    Inline mode (tests): submit() then drain(); exceptions propagate. Thread mode: start(); a command that raises is
    answered with the conservative failure event, never with silence.
    """

    def __init__(self, exchange: PaperExchange, post: Post, *, wait_s: float = LOOP_WAIT_S) -> None:
        self.exchange = exchange
        self._post = post
        self._mono = exchange.clock.mono
        self._wait_s = wait_s
        self._cv = threading.Condition()
        self._q: collections.deque = collections.deque()      # (command, receipt mono)
        self._reads: list[tuple[float, int, Command, float]] = []   # held reads: heap of (due, seq, read, receipt)
        self._read_seq = itertools.count()
        self._last_due: dict[Account, float] = {}             # per account: the newest held read's due time
        self._stopping = False
        self._thread: threading.Thread | None = None

    def submit(self, cmd: Command) -> None:
        """Queue a command (any thread), stamped with its receipt time. A command paper-sim does not serve is
        answered at once with a refusal."""
        problem = None
        if not isinstance(cmd, ORDER_COMMANDS + ACCOUNT_READ_COMMANDS + PAPER_READ_COMMANDS):
            problem = f"{type(cmd).__name__} is not a paper-sim command"
        elif cmd.account not in self.exchange.accounts:
            problem = f"paper-sim serves PAPER and SHADOW, not {cmd.account}"
        if problem is not None:
            msg = f"{LOCAL}: {problem}"
            log.error("%s (%r)", msg, cmd)
            self._deliver(failure_events(cmd, msg, self._mono(), outcome=REJECTED))
            return
        with self._cv:
            self._q.append((cmd, self._mono()))
            self._cv.notify()

    def wake(self, _event: object = None) -> None:
        """Wake the loop early (the reply hook for REST depth requests)."""
        with self._cv:
            self._cv.notify()

    def drain(self) -> list[Event]:
        """Inline: run every queued command (stepping after each; positions and orders reads are held), then one
        more step and the held reads now due; return what was posted."""
        return self._run_once(safe=False)

    def _run_once(self, safe: bool) -> list[Event]:
        out: list[Event] = []
        while True:
            with self._cv:
                if not self._q:
                    break
                cmd, receipt = self._q.popleft()
            if isinstance(cmd, DELAYED_READS):
                out += self._deliver(self._hold(cmd, receipt, safe))
                continue
            out += self._deliver(self._execute(cmd, safe))
            out += self._deliver(self.exchange.step(self._mono()))
        out += self._deliver(self.exchange.step(self._mono()))
        now = self._mono()
        while self._reads and self._reads[0][0] <= now:
            _, _, cmd, receipt = heapq.heappop(self._reads)
            out += self._deliver(self._execute(cmd, safe, req_mono=receipt))
        return out

    def _hold(self, cmd: Command, receipt: float, safe: bool) -> list[Event]:
        """Hold a positions or orders read until its receipt (or the account's previous read, if later) plus the
        account's next read delay."""
        try:
            delay_ms = self.exchange.read_latency_ms(cmd.account)
        except Exception as exc:
            if not safe:
                raise
            error = format_error(AMBIGUOUS, type(exc).__name__, str(exc))
            log.critical("paper-sim: the read delay for %s %s failed; answering conservatively: %s",
                         type(cmd).__name__, cmd.account, error, exc_info=True)
            return failure_events(cmd, error, self._mono(), error_class=type(exc).__name__)
        acct = Account(cmd.account)
        due = max(receipt, self._last_due.get(acct, receipt)) + delay_ms / 1000.0
        self._last_due[acct] = due
        heapq.heappush(self._reads, (due, next(self._read_seq), cmd, receipt))
        return []

    def _execute(self, cmd: Command, safe: bool, req_mono: float | None = None) -> list[Event]:
        if not safe:
            return execute_broker(cmd, self.exchange, self._mono, poll_source=SIM, req_mono=req_mono)
        try:
            return execute_broker(cmd, self.exchange, self._mono, poll_source=SIM, req_mono=req_mono)
        except Exception as exc:
            error = format_error(AMBIGUOUS, type(exc).__name__, str(exc))
            log.critical("paper-sim: %s %s raised; answering conservatively: %s", type(cmd).__name__,
                         getattr(cmd, "order_pk", cmd.account), error, exc_info=True)
            return failure_events(cmd, error, self._mono(), error_class=type(exc).__name__)

    def _deliver(self, events: Iterable[Event]) -> list[Event]:
        out = []
        for ev in events:
            try:
                self._post(ev)
            except Exception:
                log.exception("paper-sim: delivering %s failed", type(ev).__name__)
            out.append(ev)
        return out

    def run(self) -> None:
        while True:
            with self._cv:
                if self._stopping:
                    return
            try:
                self._run_once(safe=True)
            except Exception:
                log.critical("paper-sim: step failed; retrying in %.1f s", LOOP_ERROR_BACKOFF_S, exc_info=True)
                with self._cv:
                    self._cv.wait(LOOP_ERROR_BACKOFF_S)
                continue
            with self._cv:
                if not self._q and not self._stopping:
                    self._cv.wait(self._wait_s)

    def start(self) -> threading.Thread:
        if self._thread is not None:
            raise RuntimeError("paper-sim already started")
        self._thread = threading.Thread(target=self.run, name=PAPER_SIM_THREAD, daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, join_s: float | None = None) -> None:
        with self._cv:
            self._stopping = True
            self._cv.notify_all()
        if self._thread is not None and join_s is not None:
            self._thread.join(join_s)
