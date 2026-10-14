"""titan2.db: connection, migrations, transactions, the typed DAO and the derived counters.

Spec §1 (one SQLite writer per file), §3.3-3.4 (write-ahead core loop, @core_only), §4.3 (the DDL in
migrations/0001_init.sql is DETAIL §4.3 verbatim), §4.5 (counters are SQL over rows; nothing is stored twice).

What this module enforces:
- One writer. Every write goes through Store._w(), which refuses unless it runs on the core thread (I1), inside
  tx() (write-ahead: rows commit before any command is dispatched) and on a writable store.
- The database is the first line of invariants (CHECKs, partial unique indexes, triggers). A constraint failure
  is raised as InvariantViolation (I4, I5, I7, I9, I10, else DB_CONSTRAINT; scope GLOBAL) so the core loop rolls
  back, reloads and halts entries; exits continue. The DAO adds checks the DDL cannot express: an order's tag
  encodes its own leg and attempt (I7), lot multiple (I12), tick grid (I13), ledger rows add up
  (LEDGER_ARITHMETIC), a fill never changes once recorded (I9).
- Persisted wall times are IST ISO-8601 with milliseconds (clock.iso), so text order is time order; sessions are
  'YYYY-MM-DD'; ledger money is rounded to 2 dp. Monotonic times are never stored: load_state re-anchors them at a
  process start and keeps this process's own across a rollback-and-reload.
- Nothing is deleted except by prune() (quote_checks older than 180 days, after a CSV.gz export; broker_snapshots
  older than 30 days). v1's titan_state.db is refused, never opened as a titan2 store.
"""

from __future__ import annotations

import csv
import dataclasses
import functools
import gzip
import io
import json
import math
import os
import re
import sqlite3
import threading
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, ContextManager, Iterable, Iterator, Sequence, TypeVar

from titan2.clock import IST, iso, parse_wall, session_of
from titan2.model import (
    IOC, LIMIT, MIS, REGULAR, SEQ_MAX, TERMINAL_STRUCT, Account, Bar, DecisionQuote, Direction, ExitParams,
    ExitProfile, Fill, Halt, HaltClears, Instrument, Intent, InvariantViolation, Leg, LegState, Order, OrderState,
    PnlStatus, Policy, Recheck, Role, Side, StrikePref, Structure, StructState, StructureType, Urgency,
    is_tick_multiple, parse_tag,
)

DB_FILENAME = "titan2.db"
BUSY_TIMEOUT_MS = 5000
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
QUOTE_CHECK_RETENTION_DAYS = 180
BROKER_SNAPSHOT_RETENTION_DAYS = 30
_IN_CHUNK = 500                      # ids per IN (...) list (SQLite's oldest limit is 999 host parameters)
_TERMINAL_SQL = "(" + ",".join(f"'{s.value}'" for s in sorted(TERMINAL_STRUCT)) + ")"

# Ledger money columns, rounded to 2 dp when persisted (spec §0 conventions).
TRADE_MONEY_COLUMNS = ("risk_basis_rs", "gross", "brokerage", "stt", "txn", "sebi", "ipft", "gst", "stamp",
                       "sqoff_fee", "charges", "net", "hs_cost_rs", "crossing_cost_rs")
CHARGE_COMPONENTS = ("brokerage", "stt", "txn", "sebi", "ipft", "gst", "stamp", "sqoff_fee")   # model.Charges
LEDGER_TOLERANCE_RS = 0.05           # 2-dp rounding of gross/charges/net never moves them this far apart
# JSON columns decoded by the dict readers.
_JSON_COLUMNS = {"events": ("payload",), "intents": ("reason",), "structures": ("decision_json", "exit_params"),
                 "trades": ("exit_profile",), "g1_trials": ("version_inputs", "looks_done"),
                 "g1_reports": ("detail",), "broker_snapshots": ("payload",)}
# NFO names of the two index underlyings (spec §11); a stock's name is its NSE symbol.
_INDEX_NAMES = {"NSE:NIFTY 50": "NIFTY", "NSE:NIFTY BANK": "BANKNIFTY"}


class StoreError(Exception):
    """The store cannot be opened, migrated or used as asked. At startup this is fatal (exit 78)."""


# ---------------------------------------------------------------------------------------------------------------
# The core-thread guard (I1). The runtime sets CORE_TID to the core thread at start; the test harness sets it too.
# ---------------------------------------------------------------------------------------------------------------
CORE_TID: int | None = None


def set_core_thread(tid: int | None = None) -> int | None:
    """Make tid (default: the calling thread) the only thread allowed to write. Returns the previous CORE_TID."""
    global CORE_TID
    previous = CORE_TID
    CORE_TID = threading.get_ident() if tid is None else tid
    return previous


def clear_core_thread() -> None:
    global CORE_TID
    CORE_TID = None


@contextmanager
def core_thread(tid: int | None = None) -> Iterator[int]:
    """Tests: run a block with tid (default: this thread) as the core thread, then restore the previous one."""
    global CORE_TID
    previous = set_core_thread(tid)
    try:
        yield CORE_TID  # type: ignore[misc]
    finally:
        CORE_TID = previous


def assert_core(what: str) -> None:
    if CORE_TID is None or threading.get_ident() != CORE_TID:
        raise InvariantViolation("I1", f"{what} called off the core thread")


F = TypeVar("F", bound=Callable[..., Any])


def core_only(fn: F) -> F:
    """Decorator for every Engine and Store writer: raises InvariantViolation("I1") off the core thread."""
    @functools.wraps(fn)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        assert_core(fn.__qualname__)
        return fn(*args, **kwargs)

    guarded.__core_only__ = True  # type: ignore[attr-defined]  (inspected by tests/arch)
    return guarded  # type: ignore[return-value]


# ---------------------------------------------------------------------------------------------------------------
# Values in and out of SQLite
# ---------------------------------------------------------------------------------------------------------------
def db_json(obj: Any) -> str:
    """Stable JSON for every JSON column: sorted keys, no spaces, NaN/inf refused."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False, default=_json_default)


def _json_default(o: Any) -> Any:
    if isinstance(o, Enum):
        return o.value
    if isinstance(o, datetime):
        return iso(o)
    if isinstance(o, date):
        return o.isoformat()
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return {f.name: getattr(o, f.name) for f in dataclasses.fields(o)}
    if isinstance(o, Mapping):
        return dict(o)
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    raise TypeError(f"{type(o).__name__} is not JSON serializable")


def _sql(v: Any) -> Any:
    """A value as stored: enums by value, bool 0/1, datetime IST ISO (ms), date YYYY-MM-DD, containers as JSON."""
    if v is None:
        return None
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float, str)):
        return v
    if isinstance(v, datetime):
        return iso(v)
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (Mapping, list, tuple)) or (dataclasses.is_dataclass(v) and not isinstance(v, type)):
        return db_json(v)
    raise StoreError(f"cannot store a {type(v).__name__}")


def _wall(text: str | None) -> datetime | None:
    return None if text is None else parse_wall(text)


def _loads(text: str | None) -> Any:
    return None if text is None else json.loads(text)


def _money(x: float | None) -> float | None:
    return None if x is None else round(float(x), 2)


def _as_dict(table: str, row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for col in _JSON_COLUMNS.get(table, ()):
        if d.get(col) is not None:
            d[col] = json.loads(d[col])
    return d


_VIOLATIONS = (                       # SQLite message fragment -> invariant code (spec §3.6)
    ("filled_qty decreased", "I9"), ("terminal order immutable", "I9"), ("filled_qty <= qty", "I9"),
    ("UNIQUE constraint failed: orders.leg_id", "I4"), ("UNIQUE constraint failed: structures.", "I5"),
    ("role='ENTRY' AND side='BUY'", "I7"), ("trades.structure_id", "I10"), ("ledger immutable", "I10"),
)


def _violation(exc: sqlite3.IntegrityError) -> InvariantViolation:
    msg = str(exc)
    code = next((c for needle, c in _VIOLATIONS if needle in msg), "DB_CONSTRAINT")
    return InvariantViolation(code, msg)


# ---------------------------------------------------------------------------------------------------------------
# Rows <-> model objects
# ---------------------------------------------------------------------------------------------------------------
def exit_params_from_json(text: str | None) -> ExitParams | None:
    if text is None:
        return None
    d = json.loads(text)
    d["profile"] = ExitProfile(**d["profile"])
    d["entry_fill_wall"] = parse_wall(d["entry_fill_wall"])
    d["time_stop_at"] = parse_wall(d["time_stop_at"])
    return ExitParams(**d)


def _intent_from_row(r: sqlite3.Row, now_mono: float, now_wall: datetime) -> Intent:
    created = parse_wall(r["created_ts"])
    # created_mono keeps the intent's true age, so a reloaded intent expires on time (TTL gates fail closed).
    age_s = max(0.0, (now_wall - created).total_seconds())
    return Intent(
        intent_id=r["intent_id"], strategy=r["strategy"], strategy_version=r["strategy_version"],
        underlying=r["underlying"], direction=Direction(r["direction"]), structure=StructureType(r["structure"]),
        strike_pref=StrikePref(r["strike_pref"]), expiry_rule=r["expiry_rule"], trigger_px=r["trigger_px"],
        recheck=Recheck(r["recheck_lo"], r["recheck_hi"]), ttl_s=r["ttl_s"], created_mono=now_mono - age_s,
        created_wall=created, reason=json.loads(r["reason"]), is_placebo=bool(r["is_placebo"]),
        parent_intent_id=r["parent_intent_id"])


def _structure_values(st: Structure) -> dict[str, Any]:
    return {
        "session": st.session, "book": st.book, "account": Account(st.account), "policy": Policy(st.policy),
        "strategy": st.strategy, "strategy_version": st.strategy_version, "intent_id": st.intent.intent_id,
        "type": st.type, "underlying": st.underlying, "state": StructState(st.state), "is_placebo": st.is_placebo,
        "parent_structure_id": st.parent_structure_id, "blocked_by": st.blocked_by, "decision_json": st.decision,
        "placebo_status": st.placebo_status, "exit_params": st.exit_params, "risk_basis_rs": _money(st.risk_basis_rs),
        "exit_reason": st.exit_reason, "void_reason": st.void_reason, "reserved_ts": st.reserved_wall,
        "opened_ts": st.opened_wall, "closed_ts": st.closed_wall, "pnl_status": st.pnl_status,
    }


_STRUCT_FIXED = ("session", "book", "account", "policy", "strategy", "strategy_version", "intent_id", "type",
                 "underlying", "is_placebo", "parent_structure_id", "reserved_ts")
_STRUCT_MUTABLE = ("state", "blocked_by", "decision_json", "placebo_status", "exit_params", "risk_basis_rs",
                   "exit_reason", "void_reason", "opened_ts", "closed_ts", "pnl_status")


def _structure_from_row(r: sqlite3.Row, intent: Intent, leg_ids: list[int]) -> Structure:
    return Structure(
        structure_id=r["structure_id"], session=date.fromisoformat(r["session"]), book=r["book"],
        account=Account(r["account"]), policy=Policy(r["policy"]), strategy=r["strategy"],
        strategy_version=r["strategy_version"], intent=intent, type=StructureType(r["type"]),
        underlying=r["underlying"], state=StructState(r["state"]), legs=list(leg_ids),
        is_placebo=bool(r["is_placebo"]), parent_structure_id=r["parent_structure_id"], blocked_by=r["blocked_by"],
        decision=_loads(r["decision_json"]), placebo_status=r["placebo_status"],
        exit_params=exit_params_from_json(r["exit_params"]), risk_basis_rs=r["risk_basis_rs"],
        exit_reason=r["exit_reason"], void_reason=r["void_reason"], reserved_wall=parse_wall(r["reserved_ts"]),
        opened_wall=_wall(r["opened_ts"]), closed_wall=_wall(r["closed_ts"]),
        pnl_status=None if r["pnl_status"] is None else PnlStatus(r["pnl_status"]))


def _leg_values(leg: Leg) -> dict[str, Any]:
    i = leg.inst
    return {
        "structure_id": leg.structure_id, "leg_no": leg.leg_no, "session": leg.session,
        "account": Account(leg.account), "book": leg.book, "seq_no": leg.seq_no, "symbol": i.symbol,
        "tradingsymbol": i.tradingsymbol, "exchange": i.exchange, "token": i.token, "underlying": i.underlying,
        "side": leg.side, "opt_type": i.opt_type, "strike": i.strike, "expiry": i.expiry, "lot_size": i.lot_size,
        "tick": i.tick, "state": LegState(leg.state), "stop_px": leg.stop_px, "lock_armed": leg.lock_armed,
        "peak_rb": leg.peak_rb, "peak_ts": leg.peak_wall, "trough_rb": leg.trough_rb, "trough_ts": leg.trough_wall,
        "entry_fill_ts": leg.entry_fill_wall, "last_fill_ts": leg.last_fill_wall, "exit_reason": leg.exit_reason,
        "exit_urgency": leg.exit_urgency, "exit_band_idx": leg.exit_band_idx,
        "broker_residual_qty": leg.broker_residual_qty,
    }


_LEG_FIXED = ("structure_id", "leg_no", "session", "account", "book", "seq_no", "symbol", "token", "lot_size",
              "tick")
_LEG_MUTABLE = ("state", "stop_px", "lock_armed", "peak_rb", "peak_ts", "trough_rb", "trough_ts", "entry_fill_ts",
                "last_fill_ts", "exit_reason", "exit_urgency", "exit_band_idx", "broker_residual_qty")


# The monotonic fields a loader re-anchors, each named by the persisted wall time it pairs with (Store.load_state).
_FILL, _EXITING, _SUBMITTED = "last_fill", "exiting_since", "submitted"
# mono_of(kind, row key, persisted wall text or None) -> the monotonic time to load
MonoOf = Callable[[str, Any, "str | None"], "float | None"]


def _leg_from_row(r: sqlite3.Row, order_pks: list[str], mono_of: MonoOf) -> Leg:
    underlying = r["underlying"]
    inst = Instrument(
        symbol=r["symbol"], token=r["token"], tradingsymbol=r["tradingsymbol"], exchange=r["exchange"],
        name=_INDEX_NAMES.get(underlying, underlying.split(":", 1)[-1]),  # legs have no name column (DDL)
        underlying=underlying, opt_type=r["opt_type"], strike=r["strike"],
        expiry=date.fromisoformat(r["expiry"]), lot_size=r["lot_size"], tick=r["tick"])
    return Leg(
        leg_id=r["leg_id"], structure_id=r["structure_id"], leg_no=r["leg_no"], account=Account(r["account"]),
        book=r["book"], session=date.fromisoformat(r["session"]), seq_no=r["seq_no"], inst=inst, side=r["side"],
        state=LegState(r["state"]), orders=list(order_pks), stop_px=r["stop_px"], lock_armed=bool(r["lock_armed"]),
        peak_rb=r["peak_rb"], peak_wall=_wall(r["peak_ts"]), trough_rb=r["trough_rb"],
        trough_wall=_wall(r["trough_ts"]), entry_fill_wall=_wall(r["entry_fill_ts"]),
        # spec §9.2: after a restart every leg's last_fill_mono is the process start (Store.load_state: reloads)
        last_fill_mono=mono_of(_FILL, r["leg_id"], r["last_fill_ts"]),
        last_fill_wall=_wall(r["last_fill_ts"]), exit_reason=r["exit_reason"],
        exit_urgency=None if r["exit_urgency"] is None else Urgency(r["exit_urgency"]),
        exit_band_idx=r["exit_band_idx"],
        exiting_since_mono=(None if r["exiting_since_ts"] is None
                            else mono_of(_EXITING, r["leg_id"], r["exiting_since_ts"])),
        broker_residual_qty=r["broker_residual_qty"])


def _order_values(o: Order) -> dict[str, Any]:
    d = o.decision
    return {
        "leg_id": o.leg_id, "structure_id": o.structure_id, "account": Account(o.account), "book": o.book,
        "role": Role(o.role), "side": o.side, "symbol": o.symbol, "qty": o.qty, "limit_px": o.limit_px,
        "attempt": o.attempt, "band_idx": o.band_idx, "state": OrderState(o.state),
        "broker_order_id": o.broker_order_id, "broker_status": o.broker_status, "status_msg": o.status_msg,
        "external_kind": o.external_kind, "filled_qty": o.filled_qty, "avg_fill_px": o.avg_fill_px,
        "decision_bid": d.bid if d else None, "decision_ask": d.ask if d else None,
        "decision_mid": d.mid if d else None, "decision_quote_age_ms": d.age_ms if d else None,
        "decision_quote_src": d.src if d else None, "decision_ltp": d.ltp if d else None,
        "decision_walk_px": d.walk_px if d else None, "created_ts": o.created_wall, "terminal_ts": o.terminal_wall,
        "stuck": o.stuck,
    }


_ORDER_FIXED = ("leg_id", "structure_id", "account", "book", "role", "side", "symbol", "qty", "limit_px", "attempt",
                "band_idx")
_ORDER_MUTABLE = ("state", "broker_order_id", "broker_status", "status_msg", "external_kind", "filled_qty",
                  "avg_fill_px", "terminal_ts", "stuck")


def _order_from_row(r: sqlite3.Row, mono_of: MonoOf) -> Order:
    decision = None
    if r["decision_quote_src"] is not None:
        decision = DecisionQuote(bid=r["decision_bid"], ask=r["decision_ask"], mid=r["decision_mid"],
                                 age_ms=r["decision_quote_age_ms"], src=r["decision_quote_src"],
                                 ltp=r["decision_ltp"], walk_px=r["decision_walk_px"])
    state = OrderState(r["state"])
    return Order(
        order_pk=r["order_pk"], leg_id=r["leg_id"], structure_id=r["structure_id"], account=Account(r["account"]),
        book=r["book"], role=Role(r["role"]), side=Side(r["side"]), symbol=r["symbol"], qty=r["qty"],
        limit_px=r["limit_px"], attempt=r["attempt"], band_idx=r["band_idx"], state=state,
        broker_order_id=r["broker_order_id"], broker_status=r["broker_status"],
        status_msg=r["status_msg"], external_kind=r["external_kind"], filled_qty=r["filled_qty"],
        avg_fill_px=r["avg_fill_px"], decision=decision, created_wall=parse_wall(r["created_ts"]),
        # the order's timers count from this (Store.load_state: a restart re-anchors it, a reload keeps it)
        submitted_mono=None if state is OrderState.NEW else mono_of(_SUBMITTED, r["order_pk"], r["submitted_ts"]),
        terminal_wall=_wall(r["terminal_ts"]), stuck=bool(r["stuck"]))


def _fill_from_row(r: sqlite3.Row) -> Fill:
    return Fill(order_pk=r["order_pk"], seq_key=r["seq_key"], source=r["source"], qty=r["qty"], px=r["px"],
                wall=parse_wall(r["ts"]), exch_wall=_wall(r["exch_ts"]))


def _halt_from_row(r: sqlite3.Row) -> Halt:
    return Halt(halt_id=r["halt_id"], scope=r["scope"], kind=r["kind"], reason=r["reason"],
                operator=bool(r["operator"]), clears=HaltClears(r["clears"]), created_wall=parse_wall(r["created_ts"]),
                session=date.fromisoformat(r["session"]))


def _bar_from_row(r: sqlite3.Row) -> Bar:
    return Bar(symbol=r["symbol"], start=parse_wall(r["start_ts"]), open=r["open"], high=r["high"], low=r["low"],
               close=r["close"], volume=r["volume"], valid=bool(r["valid"]), source=r["source"])


@dataclass(slots=True)
class LoadedState:
    """What Engine.load rebuilds from: every non-terminal structure (plus a session's terminal ones if asked),
    their legs and all their orders. Every Leg is bound to the shared orders map."""

    structures: dict[int, Structure]
    legs: dict[int, Leg]
    orders: dict[str, Order]


@dataclass(frozen=True, slots=True)
class BookCounters:
    """One book's derived counters for one session (§4.5): SQL over rows, recomputed on every Engine.load."""

    book: str
    session: date
    entries: int                      # structures opened today
    attempts: int                     # structures with an ENTRY order today
    orders_today: int                 # bot orders today (EXTERNAL excluded)
    entry_orders_60s: int             # ENTRY orders created in the last 60 s
    realized_net: float               # Σ trades.net today
    first_loss_today: bool
    active: int                       # non-terminal structures (open + reserved) of any session: the book's slots


@dataclass(frozen=True, slots=True)
class PruneResult:
    quote_checks_exported: int
    quote_checks_deleted: int
    broker_snapshots_deleted: int
    export_path: Path | None


# ---------------------------------------------------------------------------------------------------------------
# Connections, migrations, backups
# ---------------------------------------------------------------------------------------------------------------
# Ported from agentic_trader/state_db.py:226-237 @b4694f3 — changes: synchronous=FULL (was NORMAL); autocommit
#   connection driven by explicit BEGIN IMMEDIATE; every pragma is verified after it is set; JSON1 is required.
def _connect(path: Path) -> sqlite3.Connection:
    # check_same_thread=False: the store may be built on one thread and used by the core thread; writes are
    # guarded by core_only instead, and other threads/processes read through open_readonly().
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        conn.row_factory = sqlite3.Row
        # Refuse a foreign database (e.g. v1's titan_state.db) before changing anything in it.
        if (conn.execute("PRAGMA user_version").fetchone()[0] == 0
                and conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]):
            raise StoreError(f"{path}: has tables but no titan2 schema version; not a titan2 database")
        journal = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        # The row a REPLACE deletes fires DELETE triggers only with recursive_triggers on: without it an
        # INSERT OR REPLACE would silently rewrite an append-only row (fills, trades, events, g1_trials).
        conn.execute("PRAGMA recursive_triggers=ON")
        got = (str(journal).lower(), conn.execute("PRAGMA synchronous").fetchone()[0],
               conn.execute("PRAGMA foreign_keys").fetchone()[0], conn.execute("PRAGMA busy_timeout").fetchone()[0],
               conn.execute("PRAGMA recursive_triggers").fetchone()[0])
        if got != ("wal", 2, 1, BUSY_TIMEOUT_MS, 1):
            raise StoreError(f"{path}: connection settings not applied "
                             f"(journal, synchronous, fk, busy, recursive_triggers) = {got}")
        conn.execute("SELECT json_valid('{}')")         # the DDL's CHECKs need JSON1
    except BaseException:
        conn.close()
        raise
    return conn


def connect_readonly(path: str | os.PathLike) -> sqlite3.Connection:
    """Read-only connection (mode=ro + query_only, sqlite3.Row rows) to ANY SQLite file: the bot uses it for the
    command DBs (another unit's schema; dash/commands_dash.db is owned by titandash), the CLI for reports."""
    path = Path(path)
    if not path.is_file():
        raise StoreError(f"{path}: no such database")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None,
                           check_same_thread=False, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA query_only=ON")
    return conn


def migration_files(directory: str | os.PathLike | None = None) -> list[tuple[int, Path]]:
    """[(number, path)] of NNNN_name.sql files, numbered 1..n with no gaps or duplicates."""
    d = Path(directory) if directory is not None else MIGRATIONS_DIR
    found = []
    try:
        for p in d.iterdir():
            m = re.fullmatch(r"(\d{4})_\w+\.sql", p.name)
            if m is not None:
                found.append((int(m.group(1)), p))
    except OSError as exc:
        raise StoreError(f"cannot list migrations in {d}: {exc}") from exc
    found.sort()
    numbers = [n for n, _ in found]
    if not numbers or numbers != list(range(1, len(numbers) + 1)):
        raise StoreError(f"{d}: migrations must be numbered 0001.. with no gaps or duplicates, got {numbers}")
    return found


def latest_schema_version(directory: str | os.PathLike | None = None) -> int:
    return migration_files(directory)[-1][0]


def _backup(conn: sqlite3.Connection, dest: Path) -> Path:
    """Consistent copy through SQLite's online backup (a reader); written to a temp name, then renamed."""
    if dest.exists():
        raise StoreError(f"backup target already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    if tmp.exists():
        tmp.unlink()
    target = sqlite3.connect(str(tmp))
    try:
        conn.backup(target)
    finally:
        target.close()
    os.replace(tmp, dest)
    return dest


def backup_db(src: str | os.PathLike, dest: str | os.PathLike) -> Path:
    """Back up any SQLite file (titan2.db, paper_exchange.db, the command DBs) without writing to it."""
    conn = connect_readonly(Path(src))
    try:
        return _backup(conn, Path(dest))
    finally:
        conn.close()


def _premigrate_path(backup_dir: Path) -> Path:
    stamp = datetime.now(IST).strftime("%Y%m%dT%H%M%S")
    path, n = backup_dir / f"premigrate-{stamp}.db", 1
    while path.exists():
        path, n = backup_dir / f"premigrate-{stamp}-{n}.db", n + 1
    return path


def _apply_migration(conn: sqlite3.Connection, number: int, sql: str) -> None:
    """One migration = one transaction, including user_version and meta.schema_version. Foreign keys are off
    while it runs (table rebuilds in later migrations) and must check clean before the commit."""
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript("BEGIN IMMEDIATE;\n" + sql)
        if not conn.in_transaction:
            raise StoreError(f"migration {number:04d} ended its own transaction; migrations must not COMMIT")
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise StoreError(f"migration {number:04d} breaks foreign keys: {[tuple(r) for r in broken[:5]]}")
        conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(number),))
        conn.execute("INSERT INTO meta(key, value) VALUES('install_id', ?) ON CONFLICT(key) DO NOTHING",
                     (uuid.uuid4().hex,))
        conn.execute(f"PRAGMA user_version={int(number)}")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _migrate(conn: sqlite3.Connection, path: Path, backup_dir: Path,
             migrations_dir: str | os.PathLike | None) -> tuple[list[int], Path | None]:
    files = migration_files(migrations_dir)
    latest = files[-1][0]
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > latest:
        raise StoreError(f"{path}: schema version {current} is newer than this release ({latest}); "
                         "deploy the newer release or restore the premigrate backup")
    pending = [(n, f) for n, f in files if n > current]
    backup = _backup(conn, _premigrate_path(backup_dir)) if pending and current > 0 else None
    for n, f in pending:
        _apply_migration(conn, n, f.read_text(encoding="utf-8"))
    return [n for n, _ in pending], backup


_READ_SQL = re.compile(r"\s*(SELECT|WITH|EXPLAIN|PRAGMA\s+\w+\s*(\(|;|$))", re.IGNORECASE)


# ---------------------------------------------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------------------------------------------
class Store:
    """titan2.db. Store(path) opens it (path may be the file or its directory), creating it if needed; when a
    migration is pending it first backs the DB up to <backup_dir>/premigrate-<ts>.db (default: <db dir>/backups;
    the runtime passes /var/lib/titan2/backups). open_readonly(path) gives a query-only store for CLI/self-check.

    Writes: core thread only, inside tx(). Reads: any method without @core_only, from the owning thread."""

    def __init__(self, path: str | os.PathLike, *, backup_dir: str | os.PathLike | None = None,
                 migrations_dir: str | os.PathLike | None = None, readonly: bool = False) -> None:
        p = Path(path)
        if p.is_dir():
            p = p / DB_FILENAME
        self.path = p
        self.readonly = readonly
        self.migrated: list[int] = []
        self.backup_path: Path | None = None
        self._depth = 0
        # The monotonic time this process held for each persisted wall time it wrote (load_state reads it back):
        #   (kind, row key, wall text or None) -> monotonic time or None. RAM only, one Store per process.
        self._mono_seen: dict[tuple[str, Any, str | None], float | None] = {}
        self._first_load: tuple[str, float] | None = None      # (wall text, monotonic) of the first load_state
        if readonly:
            self._conn = connect_readonly(p)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            self._conn = _connect(p)
        try:
            if not readonly:
                bdir = Path(backup_dir) if backup_dir is not None else p.parent / "backups"
                self.migrated, self.backup_path = _migrate(self._conn, p, bdir, migrations_dir)
            if self.user_version < 1:
                raise StoreError(f"{p}: not a titan2 database (user_version 0)")
            self._columns = {
                name: tuple(r[1] for r in self._conn.execute(f'PRAGMA table_info("{name}")'))
                for (name,) in self._conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                                                  "AND name NOT LIKE 'sqlite_%'").fetchall()}
        except BaseException:
            self._conn.close()
            raise

    # -- lifecycle ------------------------------------------------------------------------------------------
    @property
    def user_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    @property
    def in_tx(self) -> bool:
        return self._depth > 0

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"Store({str(self.path)!r}, readonly={self.readonly})"

    def integrity_check(self) -> list[str]:
        """Problems found by PRAGMA integrity_check and foreign_key_check; [] means clean."""
        rows = [r[0] for r in self._conn.execute("PRAGMA integrity_check").fetchall()]
        problems = [] if rows == ["ok"] else rows
        problems += [f"foreign key: {tuple(r)}" for r in self._conn.execute("PRAGMA foreign_key_check").fetchall()]
        return problems

    def backup(self, dest: str | os.PathLike) -> Path:
        """Consistent copy of this database (the store keeps running). Never mid-transaction: the copy would
        include this connection's uncommitted pages."""
        if self._conn.in_transaction:
            raise StoreError("backup() inside a transaction would copy uncommitted rows")
        return _backup(self._conn, Path(dest))

    # -- transactions ---------------------------------------------------------------------------------------
    @core_only
    def tx(self) -> ContextManager[Store]:
        """BEGIN IMMEDIATE ... COMMIT (rollback on any exception). A nested tx() is a SAVEPOINT."""
        if self.readonly:
            raise StoreError(f"{self.path}: read-only store")
        return self._tx()

    @contextmanager
    def _tx(self) -> Iterator[Store]:
        conn = self._conn
        if self._depth:
            name = f"sp{self._depth}"
            conn.execute(f"SAVEPOINT {name}")
            self._depth += 1
            try:
                yield self
            except BaseException:
                if conn.in_transaction:          # SQLite may already have rolled everything back (I/O error)
                    conn.execute(f"ROLLBACK TO {name}")
                    conn.execute(f"RELEASE {name}")
                raise
            else:
                conn.execute(f"RELEASE {name}")
            finally:
                self._depth -= 1
            return
        if conn.in_transaction:
            raise StoreError("a transaction is open outside tx()")
        conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield self
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        else:
            try:
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            self._depth = 0

    # -- write plumbing (the only path to a write) ----------------------------------------------------------
    def _check_write(self) -> None:
        assert_core("store write")
        if self.readonly:
            raise StoreError(f"{self.path}: read-only store")
        if not self._depth:
            raise StoreError("store writes run only inside store.tx() (rows commit before commands dispatch)")
        if not self._conn.in_transaction:   # SQLite rolled the whole tx back (disk full, I/O): never autocommit
            raise StoreError("the transaction was rolled back by SQLite; refusing to write outside it")

    def _w(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        self._check_write()
        try:
            return self._conn.execute(sql, params)
        except sqlite3.IntegrityError as exc:
            raise _violation(exc) from exc

    def _cols(self, table: str, names: Iterable[str]) -> list[str]:
        known = self._columns.get(table)
        if known is None:
            raise StoreError(f"unknown table {table!r}")
        names = list(names)
        unknown = [n for n in names if n not in known]
        if unknown:
            raise StoreError(f"{table}: unknown column(s) {unknown}")
        return names

    def _insert(self, table: str, values: Mapping[str, Any], conflict: str = "") -> sqlite3.Cursor:
        self._check_write()
        cols = self._cols(table, values)
        sql = f"INSERT INTO {table}({', '.join(cols)}) VALUES({', '.join('?' * len(cols))}){conflict}"
        return self._w(sql, [_sql(values[c]) for c in cols])

    def _upsert(self, table: str, values: Mapping[str, Any], key: Sequence[str],
                update: Sequence[str] | None = None) -> sqlite3.Cursor:
        """INSERT, or on a key conflict update `update` (default: every non-key column given); () = do nothing.
        Never INSERT OR IGNORE: that would also skip CHECK failures silently."""
        self._cols(table, key)
        upd = [c for c in values if c not in key] if update is None else self._cols(table, update)
        action = ("DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in upd)) if upd else "DO NOTHING"
        return self._insert(table, values, f" ON CONFLICT({', '.join(key)}) {action}")

    def _update_diff(self, table: str, key: Mapping[str, Any], values: Mapping[str, Any],
                     fixed: Mapping[str, Any] | None = None) -> list[str]:
        """UPDATE only the columns whose stored value differs (a terminal structure's unchanged state is never
        rewritten, which its trigger would refuse). `fixed` columns must already equal the stored row."""
        self._check_write()
        fixed = fixed or {}
        self._cols(table, key)
        cols = self._cols(table, list(values) + list(fixed))
        if not cols:
            return []
        where = " AND ".join(f"{c}=?" for c in key)
        kv = [_sql(v) for v in key.values()]
        row = self._conn.execute(f"SELECT {', '.join(cols)} FROM {table} WHERE {where}", kv).fetchone()
        if row is None:
            raise StoreError(f"{table}: no row {dict(key)}")
        drift = {c: (row[c], _sql(v)) for c, v in fixed.items() if row[c] != _sql(v)}
        if drift:
            raise InvariantViolation("DB_CONSTRAINT", f"{table} {dict(key)}: immutable columns changed {drift}")
        new = {c: _sql(v) for c, v in values.items()}
        changed = [c for c in values if row[c] != new[c]]
        if changed:
            self._w(f"UPDATE {table} SET {', '.join(f'{c}=?' for c in changed)} WHERE {where}",
                    [new[c] for c in changed] + kv)
        return changed

    def _in(self, sql_template: str, ids: Iterable[Any]) -> list[sqlite3.Row]:
        """Run SELECT ... IN ({}) over ids in chunks; returns all rows."""
        ids = list(ids)
        rows: list[sqlite3.Row] = []
        for i in range(0, len(ids), _IN_CHUNK):
            chunk = ids[i:i + _IN_CHUNK]
            rows += self._conn.execute(sql_template.format(",".join("?" * len(chunk))), chunk).fetchall()
        return rows

    # -- generic access -------------------------------------------------------------------------------------
    @core_only
    def insert_row(self, table: str, values: Mapping[str, Any]) -> int:
        """INSERT one row (columns checked against the schema, values normalized). Returns its rowid."""
        return self._insert(table, values).lastrowid

    @core_only
    def upsert_row(self, table: str, values: Mapping[str, Any], key: Sequence[str],
                   update: Sequence[str] | None = None) -> int:
        """INSERT ... ON CONFLICT(key) DO UPDATE (or DO NOTHING when update=()). Returns the rowcount."""
        return self._upsert(table, values, key, update).rowcount

    @core_only
    def update_row(self, table: str, key: Mapping[str, Any], values: Mapping[str, Any]) -> list[str]:
        """Diff-based UPDATE of one row; returns the columns that changed."""
        return self._update_diff(table, key, values)

    @core_only
    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        """Escape hatch for a write the DAO does not cover: core thread, inside tx(), constraint errors mapped."""
        return self._w(sql, params)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        """Read rows. Write statements are refused here: writes go through the core-only DAO."""
        if not _READ_SQL.match(sql):
            raise StoreError(f"query() is read-only: {sql.strip()[:60]!r}")
        before = self._conn.total_changes
        rows = self._conn.execute(sql, [_sql(p) for p in params]).fetchall()
        if self._conn.total_changes != before:
            raise InvariantViolation("I1", f"query() modified the database: {sql.strip()[:60]!r}")
        return rows

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self.query_one(sql, params)
        return None if row is None else row[0]

    # -- meta and sequences ---------------------------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else row[0]

    @core_only
    def set_meta(self, key: str, value: str) -> None:
        self._upsert("meta", {"key": key, "value": str(value)}, ("key",))

    @core_only
    def next_seq(self, name: str) -> int:
        self._w("INSERT INTO seq(name, value) VALUES(?, 1) ON CONFLICT(name) DO UPDATE SET value = value + 1",
                (name,))
        return self._conn.execute("SELECT value FROM seq WHERE name=?", (name,)).fetchone()[0]

    @core_only
    def next_leg_seq(self, account: Account, session: date) -> int:
        """Next leg seq_no for (account, session): above both the counter and every stored leg, so legs imported
        from broker tags after a lost DB (§9.6) can never collide with a new tag."""
        self._check_write()
        account = Account(account)
        name = f"leg:{account.value}:{session:%y%m%d}"
        counter = self._conn.execute("SELECT value FROM seq WHERE name=?", (name,)).fetchone()
        stored = self._conn.execute("SELECT COALESCE(MAX(seq_no), 0) FROM legs WHERE account=? AND session=?",
                                    (account.value, _sql(session))).fetchone()[0]
        nxt = max(counter[0] if counter else 0, stored) + 1
        if nxt > SEQ_MAX:
            raise StoreError(f"{name}: leg sequence exhausted ({SEQ_MAX})")
        self._upsert("seq", {"name": name, "value": nxt}, ("name",))
        return nxt

    # -- events ---------------------------------------------------------------------------------------------
    @core_only
    def append_event(self, kind: str, *, wall: datetime, mono: float, payload: Mapping[str, Any] | None = None,
                     severity: str = "INFO", session: date | None = None, account: Account | None = None,
                     book: str | None = None, strategy: str | None = None, structure_id: int | None = None,
                     leg_id: int | None = None, order_pk: str | None = None, intent_id: str | None = None) -> int:
        """One audit row, in the same transaction as the change it records (§4.6). Returns its seq."""
        return self._insert("events", {
            "ts": wall, "mono": float(mono), "session": session or session_of(wall), "kind": kind,
            "severity": severity, "account": account, "book": book, "strategy": strategy,
            "structure_id": structure_id, "leg_id": leg_id, "order_pk": order_pk, "intent_id": intent_id,
            "payload": {} if payload is None else payload}).lastrowid

    def events(self, *, session: date | None = None, since_seq: int = 0, structure_id: int | None = None,
               kind: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        where, params = ["seq > ?"], [since_seq]
        for col, val in (("session", session), ("structure_id", structure_id), ("kind", kind)):
            if val is not None:
                where.append(f"{col} = ?")
                params.append(_sql(val))
        sql = f"SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY seq"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return [_as_dict("events", r) for r in self._conn.execute(sql, params).fetchall()]

    def events_tail(self, n: int = 500) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM (SELECT * FROM events ORDER BY seq DESC LIMIT ?) ORDER BY seq",
                                  (int(n),)).fetchall()
        return [_as_dict("events", r) for r in rows]

    # -- intents and routes ---------------------------------------------------------------------------------
    @core_only
    def insert_intent(self, intent: Intent, session: date | None = None) -> None:
        self._insert("intents", {
            "intent_id": intent.intent_id, "session": session or session_of(intent.created_wall),
            "strategy": intent.strategy, "strategy_version": intent.strategy_version,
            "underlying": intent.underlying, "direction": intent.direction, "structure": intent.structure,
            "strike_pref": intent.strike_pref, "expiry_rule": intent.expiry_rule, "trigger_px": intent.trigger_px,
            "recheck_lo": intent.recheck.lo, "recheck_hi": intent.recheck.hi, "created_ts": intent.created_wall,
            "ttl_s": intent.ttl_s, "reason": dict(intent.reason), "is_placebo": intent.is_placebo,
            "parent_intent_id": intent.parent_intent_id})

    def load_intent(self, intent_id: str, now_mono: float, now_wall: datetime) -> Intent | None:
        row = self._conn.execute("SELECT * FROM intents WHERE intent_id=?", (intent_id,)).fetchone()
        return None if row is None else _intent_from_row(row, now_mono, now_wall)

    @core_only
    def put_intent_route(self, intent_id: str, book: str, outcome: str, decided_wall: datetime, *,
                         gate: str | None = None, detail: str | None = None,
                         structure_id: int | None = None) -> None:
        """The (intent, book) routing outcome: RESERVED | REFUSED | DIVERTED | EXPIRED (a later call updates it)."""
        self._upsert("intent_routes", {"intent_id": intent_id, "book": book, "outcome": outcome, "gate": gate,
                                       "detail": detail, "structure_id": structure_id, "decided_ts": decided_wall},
                     ("intent_id", "book"))

    def intent_routes(self, intent_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM intent_routes WHERE intent_id=? ORDER BY book", (intent_id,))
        return [dict(r) for r in rows.fetchall()]

    # -- structures, legs, orders ---------------------------------------------------------------------------
    @core_only
    def insert_structure(self, st: Structure) -> int:
        """Insert st (normally RESERVED). A falsy st.structure_id is assigned here and set on st."""
        values = _structure_values(st)
        if st.structure_id:
            values["structure_id"] = st.structure_id
        st.structure_id = self._insert("structures", values).lastrowid
        return st.structure_id

    @core_only
    def update_structure(self, st: Structure) -> list[str]:
        """Persist st's mutable fields (only changed columns are written); returns the columns changed."""
        v = _structure_values(st)
        return self._update_diff("structures", {"structure_id": st.structure_id},
                                 {c: v[c] for c in _STRUCT_MUTABLE}, {c: v[c] for c in _STRUCT_FIXED})

    @core_only
    def insert_leg(self, leg: Leg, *, exiting_since_wall: datetime | None = None) -> int:
        """Insert leg; a falsy leg.leg_id is assigned here and set on leg. Must match its structure's account,
        book and session."""
        self._check_write()
        st = self._conn.execute("SELECT account, book, session FROM structures WHERE structure_id=?",
                                (leg.structure_id,)).fetchone()
        values = _leg_values(leg)
        if st is not None and tuple(st) != (values["account"].value, leg.book, _sql(leg.session)):
            raise InvariantViolation("DB_CONSTRAINT", f"leg (account, book, session) differs from structure "
                                                      f"{leg.structure_id}: {tuple(st)}")
        values["exiting_since_ts"] = exiting_since_wall
        if leg.leg_id:
            values["leg_id"] = leg.leg_id
        leg.leg_id = self._insert("legs", values).lastrowid
        self._seen_leg(leg, exiting_since_wall)
        return leg.leg_id

    @core_only
    def update_leg(self, leg: Leg, *, exiting_since_wall: datetime | None = None) -> list[str]:
        """Persist leg's mutable fields. The model has no wall time for exiting_since: pass exiting_since_wall when
        the leg enters EXITING (None leaves the stored value unchanged)."""
        v = _leg_values(leg)
        values = {c: v[c] for c in _LEG_MUTABLE}
        if exiting_since_wall is not None:
            values["exiting_since_ts"] = exiting_since_wall
        changed = self._update_diff("legs", {"leg_id": leg.leg_id}, values, {c: v[c] for c in _LEG_FIXED})
        self._seen_leg(leg, exiting_since_wall)
        return changed

    def _seen(self, kind: str, key: Any, wall: datetime | None, mono: float | None) -> None:
        self._mono_seen[(kind, key, None if wall is None else _sql(wall))] = mono

    def _seen_leg(self, leg: Leg, exiting_since_wall: datetime | None) -> None:
        self._seen(_FILL, leg.leg_id, leg.last_fill_wall, leg.last_fill_mono)
        if exiting_since_wall is not None:
            self._seen(_EXITING, leg.leg_id, exiting_since_wall, leg.exiting_since_mono)

    @core_only
    def insert_order(self, order: Order, *, order_type: str = LIMIT, validity: str = IOC, product: str = MIS,
                     variety: str = REGULAR, submitted_wall: datetime | None = None,
                     acked_wall: datetime | None = None) -> None:
        """Insert an order row (bot orders: SUBMITTING, in the same tx that returns Place). Beyond the DDL it
        checks: the order matches its leg (symbol, account, book, structure); a bot order_pk is the tag of this
        role/account/leg session/leg seq_no/attempt (I7); qty is a lot multiple (I12); the limit is on the leg's
        tick grid (I13). EXTERNAL rows pass the broker's own order_type/validity/product/variety and use
        order_pk 'EX' + broker id."""
        self._check_write()
        if order.created_wall is None:
            raise StoreError(f"order {order.order_pk}: created_wall is required")
        role, account = Role(order.role), Account(order.account)
        leg = self._conn.execute("SELECT symbol, account, book, structure_id, lot_size, tick, session, seq_no "
                                 "FROM legs WHERE leg_id=?", (order.leg_id,)).fetchone()
        if leg is None:
            raise InvariantViolation("DB_CONSTRAINT", f"order {order.order_pk}: leg {order.leg_id} does not exist")
        if (leg["symbol"], leg["account"], leg["book"], leg["structure_id"]) != (
                order.symbol, account.value, order.book, order.structure_id):
            raise InvariantViolation("DB_CONSTRAINT", f"order {order.order_pk} does not match leg {order.leg_id}")
        if role is Role.EXTERNAL:
            if not str(order.order_pk).startswith("EX"):
                raise InvariantViolation("I7", f"EXTERNAL order_pk {order.order_pk!r} must be 'EX' + broker id")
        else:
            # The tag is the order's identity at the broker: recovery maps it back to its leg by (account,
            # session, seq_no) (§9.6), so it must encode exactly this leg and attempt.
            tag = parse_tag(order.order_pk)
            if tag is None or tag.role is not role or tag.account is not account:
                raise InvariantViolation("I7", f"order_pk {order.order_pk!r} is not a {role}/{account} bot tag")
            if (_sql(tag.session), tag.seq_no, tag.attempt) != (leg["session"], leg["seq_no"], order.attempt):
                raise InvariantViolation("I7", f"order_pk {order.order_pk!r} does not encode leg {order.leg_id} "
                                               f"({leg['session']}, seq {leg['seq_no']}) attempt {order.attempt}")
            if order.qty % leg["lot_size"]:
                raise InvariantViolation("I12", f"order {order.order_pk}: qty {order.qty} is not a multiple of "
                                                f"lot {leg['lot_size']}")
            if order.limit_px is None or not is_tick_multiple(order.limit_px, leg["tick"]):
                raise InvariantViolation("I13", f"order {order.order_pk}: limit {order.limit_px} is off the "
                                                f"{leg['tick']} tick grid")
        values = _order_values(order)
        values.update(order_pk=order.order_pk, order_type=order_type, validity=validity, product=product,
                      variety=variety, submitted_ts=submitted_wall, acked_ts=acked_wall)
        self._insert("orders", values)
        self._seen(_SUBMITTED, order.order_pk, submitted_wall, order.submitted_mono)

    @core_only
    def update_order(self, order: Order, *, submitted_wall: datetime | None = None,
                     acked_wall: datetime | None = None) -> list[str]:
        """Persist order's mutable fields; the DB refuses a lower filled_qty or any change to a terminal order's
        state/filled/avg (I9). submitted/acked walls are written only when given."""
        v = _order_values(order)
        values = {c: v[c] for c in _ORDER_MUTABLE}
        if submitted_wall is not None:
            values["submitted_ts"] = submitted_wall
        if acked_wall is not None:
            values["acked_ts"] = acked_wall
        changed = self._update_diff("orders", {"order_pk": order.order_pk}, values, {c: v[c] for c in _ORDER_FIXED})
        if submitted_wall is not None:
            self._seen(_SUBMITTED, order.order_pk, submitted_wall, order.submitted_mono)
        return changed

    def load_state(self, now_mono: float, now_wall: datetime, *, session: date | None = None) -> LoadedState:
        """Every non-terminal structure (and, with session, all of that session's structures) with legs and orders.
        Intent.created_mono keeps the intent's real age; breach/no-bid marks start cleared.

        Monotonic fields (last_fill_mono, exiting_since_mono, submitted_mono) are never persisted. The first load of
        this Store is a process start: each := now_mono (§9.2: after a restart, last_fill_mono is the process start).
        A later load is the core loop's rollback-and-reload in the same process: each field keeps the time this
        Store was handed with the wall time it persisted, so a reload never restarts a leg's post-fill wait, its exit
        decision or an order's timers (a storm of reloads would otherwise hold back every exit). A row from before
        this process counts from the first load; a wall time this Store never paired is re-anchored to now_mono
        (later is the safe side of the post-fill rule)."""
        first = self._first_load is None
        if first:
            self._first_load = (iso(now_wall), float(now_mono))
        start_text, start_mono = self._first_load

        def mono_of(kind: str, key: Any, text: str | None) -> float | None:
            if first:
                return now_mono
            seen = (kind, key, text)
            if seen in self._mono_seen:
                m = self._mono_seen[seen]
                return None if m is None else min(now_mono, max(start_mono, m))
            return start_mono if text is None or text < start_text else now_mono

        sql, params = f"SELECT * FROM structures WHERE state NOT IN {_TERMINAL_SQL}", []
        if session is not None:
            sql += " OR session = ?"
            params.append(_sql(session))
        srows = self._conn.execute(sql + " ORDER BY structure_id", params).fetchall()
        intents = {r["intent_id"]: _intent_from_row(r, now_mono, now_wall) for r in
                   self._in("SELECT * FROM intents WHERE intent_id IN ({})", {r["intent_id"] for r in srows})}
        lrows = sorted(self._in("SELECT * FROM legs WHERE structure_id IN ({})", [r["structure_id"] for r in srows]),
                       key=lambda r: (r["structure_id"], r["leg_no"]))
        orows = sorted(self._in("SELECT rowid AS _rowid, * FROM orders WHERE leg_id IN ({})",
                                [r["leg_id"] for r in lrows]), key=lambda r: r["_rowid"])
        orders = {r["order_pk"]: _order_from_row(r, mono_of) for r in orows}
        pks: dict[int, list[str]] = {}
        for r in orows:
            pks.setdefault(r["leg_id"], []).append(r["order_pk"])
        legs: dict[int, Leg] = {}
        leg_ids: dict[int, list[int]] = {}
        for r in lrows:
            legs[r["leg_id"]] = _leg_from_row(r, pks.get(r["leg_id"], []), mono_of).bind_orders(orders)
            leg_ids.setdefault(r["structure_id"], []).append(r["leg_id"])
        structures = {r["structure_id"]: _structure_from_row(r, intents[r["intent_id"]],
                                                             leg_ids.get(r["structure_id"], [])) for r in srows}
        return LoadedState(structures, legs, orders)

    # -- fills ----------------------------------------------------------------------------------------------
    @core_only
    def insert_fill(self, fill: Fill) -> bool:
        """Append a fill. False when the same (order_pk, seq_key) is already stored with the same qty/px/source
        (duplicate snapshot or trade report); a conflicting duplicate raises (I9). Fills are never updated."""
        self._check_write()
        prefix = {"CUM": "cum:", "TRADE": "trade:"}.get(fill.source)
        if prefix is None or not str(fill.seq_key).startswith(prefix):
            raise InvariantViolation("I9", f"fill {fill.order_pk}: seq_key {fill.seq_key!r} vs source {fill.source}")
        cur = self._insert("fills", {"order_pk": fill.order_pk, "seq_key": fill.seq_key, "source": fill.source,
                                     "qty": fill.qty, "px": fill.px, "ts": fill.wall, "exch_ts": fill.exch_wall},
                           " ON CONFLICT(order_pk, seq_key) DO NOTHING")
        if cur.rowcount == 1:
            return True
        old = self._conn.execute("SELECT qty, px, source FROM fills WHERE order_pk=? AND seq_key=?",
                                 (fill.order_pk, fill.seq_key)).fetchone()
        if (old["qty"], old["px"], old["source"]) != (fill.qty, fill.px, fill.source):
            raise InvariantViolation("I9", f"fill {fill.order_pk}/{fill.seq_key} stored as {old['qty']}@{old['px']},"
                                           f" now reported as {fill.qty}@{fill.px}")
        return False

    def fills(self, order_pk: str, source: str | None = None) -> list[Fill]:
        sql, params = "SELECT * FROM fills WHERE order_pk=?", [order_pk]
        if source is not None:
            sql += " AND source=?"
            params.append(source)
        return [_fill_from_row(r) for r in self._conn.execute(sql + " ORDER BY fill_id", params).fetchall()]

    def effective_fills(self, order_pks: str | Iterable[str]) -> list[Fill]:
        """Per order: its TRADE fills once they sum to filled_qty, otherwise its CUM fills (view effective_fills)."""
        pks = [order_pks] if isinstance(order_pks, str) else list(order_pks)
        rows = self._in("SELECT * FROM effective_fills WHERE order_pk IN ({})", pks)
        return [_fill_from_row(r) for r in sorted(rows, key=lambda r: r["fill_id"])]

    # -- the ledger -----------------------------------------------------------------------------------------
    @core_only
    def insert_trade(self, row: Mapping[str, Any]) -> None:
        """Book one closed structure (only close()/close_external() call this, via ledger.build_trade_row). Money
        columns are rounded to 2 dp; a non-finite number is refused; the row must add up (charges = Σ components,
        net = gross − charges, as charges.net_pnl defines them); a second row for a structure raises I10."""
        values = dict(row)
        for col, val in values.items():
            if isinstance(val, float) and not math.isfinite(val):
                raise StoreError(f"trades.{col} is not finite: {val!r}")
        for col in TRADE_MONEY_COLUMNS:
            if values.get(col) is not None:
                values[col] = _money(values[col])
        if all(values.get(c) is not None for c in CHARGE_COMPONENTS + ("gross", "charges", "net")):
            parts = round(sum(values[c] for c in CHARGE_COMPONENTS), 2)
            if (abs(parts - values["charges"]) > LEDGER_TOLERANCE_RS
                    or abs(values["gross"] - values["charges"] - values["net"]) > LEDGER_TOLERANCE_RS):
                raise InvariantViolation("LEDGER_ARITHMETIC", f"structure {values.get('structure_id')}: components "
                                         f"{parts} vs charges {values['charges']}; gross {values['gross']} - charges "
                                         f"!= net {values['net']}")
        self._insert("trades", values)

    def trade(self, structure_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM trades WHERE structure_id=?", (structure_id,)).fetchone()
        return None if row is None else _as_dict("trades", row)

    def trades(self, *, session: date | None = None, since: date | None = None, until: date | None = None,
               book: str | None = None, account: Account | None = None, strategy: str | None = None,
               version: str | None = None) -> list[dict[str, Any]]:
        where, params = ["1=1"], []
        for clause, val in (("session = ?", session), ("session >= ?", since), ("session <= ?", until),
                            ("book = ?", book), ("account = ?", account), ("strategy = ?", strategy),
                            ("strategy_version = ?", version)):
            if val is not None:
                where.append(clause)
                params.append(_sql(val))
        rows = self._conn.execute(f"SELECT * FROM trades WHERE {' AND '.join(where)} ORDER BY exit_ts, structure_id",
                                  params).fetchall()
        return [_as_dict("trades", r) for r in rows]

    # -- quote checks ---------------------------------------------------------------------------------------
    @core_only
    def add_quote_check(self, leg_id: int, wall: datetime, phase: str, **fields: Any) -> int:
        """HOLD/POST row; fields: bid ask bid_qty ask_qty ltp rb age_ms src stop_px lock_armed decision."""
        return self._insert("quote_checks", {"leg_id": leg_id, "ts": wall, "phase": phase, **fields}).lastrowid

    def quote_checks(self, leg_id: int, phase: str | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM quote_checks WHERE leg_id=?", [leg_id]
        if phase is not None:
            sql += " AND phase=?"
            params.append(phase)
        return [dict(r) for r in self._conn.execute(sql + " ORDER BY ts, id", params).fetchall()]

    # -- halts ----------------------------------------------------------------------------------------------
    @core_only
    def add_halt(self, scope: str, kind: str, reason: str, clears: HaltClears | str, created_wall: datetime, *,
                 session: date | None = None, operator: bool = False) -> tuple[Halt, bool]:
        """Raise a halt. Halts are sticky by (scope, kind): if one is active it is returned unchanged with False."""
        self._check_write()
        row = self._conn.execute("SELECT * FROM halts WHERE scope=? AND kind=? AND cleared_ts IS NULL",
                                 (scope, kind)).fetchone()
        if row is not None:
            return _halt_from_row(row), False
        halt_id = self._insert("halts", {"scope": scope, "kind": kind, "reason": reason, "operator": operator,
                                         "clears": HaltClears(clears), "created_ts": created_wall,
                                         "session": session or session_of(created_wall)}).lastrowid
        return _halt_from_row(self._conn.execute("SELECT * FROM halts WHERE halt_id=?", (halt_id,)).fetchone()), True

    @core_only
    def clear_halt(self, halt_id: int, cleared_wall: datetime, cleared_by: str, note: str | None = None) -> bool:
        """Clear an active halt; False if it is unknown or already cleared."""
        cur = self._w("UPDATE halts SET cleared_ts=?, cleared_by=?, clear_note=? WHERE halt_id=? "
                      "AND cleared_ts IS NULL", (_sql(cleared_wall), cleared_by, note, halt_id))
        return cur.rowcount == 1

    def active_halts(self) -> list[Halt]:
        rows = self._conn.execute("SELECT * FROM halts WHERE cleared_ts IS NULL ORDER BY halt_id").fetchall()
        return [_halt_from_row(r) for r in rows]

    def halt_row(self, halt_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM halts WHERE halt_id=?", (halt_id,)).fetchone()
        return None if row is None else dict(row)

    # -- strategy modes and G1 ------------------------------------------------------------------------------
    @core_only
    def put_strategy_mode(self, strategy: str, *, version: str, mode: str, changed_wall: datetime, changed_by: str,
                          pending_mode: str | None = None, live_unlock_report_id: int | None = None,
                          live_since_session: date | None = None, demoted_reason: str | None = None) -> None:
        """Write the whole strategy_modes row (use update_row for a partial change)."""
        self._upsert("strategy_modes", {
            "strategy": strategy, "version": version, "mode": mode, "pending_mode": pending_mode,
            "live_unlock_report_id": live_unlock_report_id, "live_since_session": live_since_session,
            "demoted_reason": demoted_reason, "changed_ts": changed_wall, "changed_by": changed_by}, ("strategy",))

    def strategy_mode(self, strategy: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM strategy_modes WHERE strategy=?", (strategy,)).fetchone()
        return None if row is None else dict(row)

    def strategy_modes(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM strategy_modes ORDER BY strategy").fetchall()]

    @core_only
    def register_trial(self, strategy: str, version: str, registered_wall: datetime,
                       version_inputs: Mapping[str, Any]) -> tuple[int, bool]:
        """Register (strategy, version) once, as COLLECTING with the next family_seq. Returns (family_seq, new)."""
        self._check_write()
        row = self._conn.execute("SELECT family_seq FROM g1_trials WHERE strategy=? AND version=?",
                                 (strategy, version)).fetchone()
        if row is not None:
            return row[0], False
        seq = self._conn.execute("SELECT COALESCE(MAX(family_seq), 0) + 1 FROM g1_trials").fetchone()[0]
        self._insert("g1_trials", {"strategy": strategy, "version": version, "family_seq": seq,
                                   "registered_ts": registered_wall, "state": "COLLECTING",
                                   "version_inputs": version_inputs, "state_ts": registered_wall})
        return seq, True

    @core_only
    def update_trial(self, strategy: str, version: str, **changes: Any) -> list[str]:
        """Diff-update a trial: state, state_ts, looks_done (list), pass_report_id, cert_report_id."""
        return self._update_diff("g1_trials", {"strategy": strategy, "version": version}, changes)

    def trial(self, strategy: str, version: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM g1_trials WHERE strategy=? AND version=?",
                                 (strategy, version)).fetchone()
        return None if row is None else _as_dict("g1_trials", row)

    def trials(self, strategy: str | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM g1_trials", []
        if strategy is not None:
            sql, params = sql + " WHERE strategy=?", [strategy]
        return [_as_dict("g1_trials", r) for r in self._conn.execute(sql + " ORDER BY family_seq", params)]

    def trial_count(self) -> int:
        """Rows ever registered (trials are never deleted, so this never decreases): G1's family size input."""
        return self._conn.execute("SELECT COUNT(*) FROM g1_trials").fetchone()[0]

    @core_only
    def insert_g1_report(self, row: Mapping[str, Any]) -> int:
        return self._insert("g1_reports", row).lastrowid

    def g1_report(self, report_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM g1_reports WHERE report_id=?", (report_id,)).fetchone()
        return None if row is None else _as_dict("g1_reports", row)

    def g1_reports(self, *, strategy: str | None = None, version: str | None = None,
                   kind: str | None = None) -> list[dict[str, Any]]:
        where, params = ["1=1"], []
        for col, val in (("strategy", strategy), ("version", version), ("kind", kind)):
            if val is not None:
                where.append(f"{col} = ?")
                params.append(val)
        rows = self._conn.execute(f"SELECT * FROM g1_reports WHERE {' AND '.join(where)} ORDER BY report_id", params)
        return [_as_dict("g1_reports", r) for r in rows.fetchall()]

    @core_only
    def flag_session(self, session: date, strategy: str, detail: str, flag: str = "IMPAIRED") -> bool:
        """Mark a strategy's session (idempotent; the first detail is kept). True when newly flagged."""
        return self._upsert("session_flags", {"session": session, "strategy": strategy, "flag": flag,
                                              "detail": detail}, ("session", "strategy", "flag"), ()).rowcount == 1

    def session_flags(self, *, strategy: str | None = None, session: date | None = None) -> list[dict[str, Any]]:
        where, params = ["1=1"], []
        for col, val in (("strategy", strategy), ("session", session)):
            if val is not None:
                where.append(f"{col} = ?")
                params.append(_sql(val))
        rows = self._conn.execute(f"SELECT * FROM session_flags WHERE {' AND '.join(where)} "
                                  "ORDER BY session, strategy", params)
        return [dict(r) for r in rows.fetchall()]

    # -- bars -----------------------------------------------------------------------------------------------
    @core_only
    def put_bar(self, bar: Bar) -> None:
        """Insert or replace one 5-min bar (a later call may mark it invalid)."""
        self._upsert("bars_5m", {"symbol": bar.symbol, "start_ts": bar.start, "source": bar.source, "open": bar.open,
                                 "high": bar.high, "low": bar.low, "close": bar.close, "volume": bar.volume,
                                 "valid": bar.valid}, ("symbol", "start_ts", "source"))

    def bars(self, symbol: str, *, source: str | None = None, start_from: datetime | None = None,
             start_to: datetime | None = None) -> list[Bar]:
        where, params = ["symbol = ?"], [symbol]
        for clause, val in (("source = ?", source), ("start_ts >= ?", start_from), ("start_ts <= ?", start_to)):
            if val is not None:
                where.append(clause)
                params.append(_sql(val))
        rows = self._conn.execute(f"SELECT * FROM bars_5m WHERE {' AND '.join(where)} ORDER BY start_ts, source",
                                  params)
        return [_bar_from_row(r) for r in rows.fetchall()]

    @core_only
    def put_bar_check(self, session: date, symbol: str, start_wall: datetime, ok: bool, detail: str) -> None:
        self._upsert("bar_checks", {"session": session, "symbol": symbol, "start_ts": start_wall, "ok": ok,
                                    "detail": detail}, ("session", "symbol", "start_ts"))

    def bar_checks(self, session: date) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM bar_checks WHERE session=? ORDER BY symbol, start_ts",
                                  (_sql(session),))
        return [dict(r) for r in rows.fetchall()]

    # -- broker snapshots, foreign acks, command acks -------------------------------------------------------
    @core_only
    def add_broker_snapshot(self, account: Account, kind: str, req_wall: datetime, ok: bool,
                            payload: Any = None) -> bool:
        """Audit copy of a broker read (≤1/min per (account, kind) + every non-OK + every recon finding, chosen by
        the caller). False if one is already stored for the same (account, kind, req_ts)."""
        return self._upsert("broker_snapshots", {"account": account, "kind": kind, "req_ts": req_wall, "ok": ok,
                                                 "payload": payload},
                            ("account", "kind", "req_ts"), ()).rowcount == 1

    @core_only
    def ack_foreign(self, session: date, account: Account, symbol: str, product: str, qty: int, acked_by: str,
                    acked_wall: datetime) -> None:
        """Record (or replace) the exact acknowledged foreign position for this session (§9.3 FOREIGN)."""
        self._upsert("foreign_acks", {"session": session, "account": account, "symbol": symbol, "product": product,
                                      "qty": qty, "acked_by": acked_by, "acked_ts": acked_wall},
                     ("session", "account", "symbol", "product"))

    def foreign_acks(self, session: date, account: Account | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM foreign_acks WHERE session=?", [_sql(session)]
        if account is not None:
            sql += " AND account=?"
            params.append(_sql(account))
        return [dict(r) for r in self._conn.execute(sql + " ORDER BY account, symbol, product", params)]

    @core_only
    def put_command_ack(self, source: str, cmd_id: int, kind: str, status: str, picked_wall: datetime, *,
                        result: Any = None, done_wall: datetime | None = None) -> None:
        """Record a command's processing; a later call updates status/result/done_ts (picked_ts is kept)."""
        self._upsert("command_acks", {"source": source, "cmd_id": cmd_id, "kind": kind, "status": status,
                                      "result": result, "picked_ts": picked_wall, "done_ts": done_wall},
                     ("source", "cmd_id"), ("status", "result", "done_ts"))

    def last_command_id(self, source: str) -> int:
        """Highest cmd_id already picked from this source (DASH | CLI); 0 if none."""
        return self._conn.execute("SELECT COALESCE(MAX(cmd_id), 0) FROM command_acks WHERE source=?",
                                  (source,)).fetchone()[0]

    def command_acks(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM command_acks ORDER BY picked_ts DESC, source, cmd_id DESC LIMIT ?",
                                  (int(limit),))
        return [dict(r) for r in rows.fetchall()]

    # -- derived counters (§4.5: SQL over rows; session = today) --------------------------------------------
    def entries(self, book: str, session: date) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM structures WHERE book=? AND session=? AND opened_ts IS NOT "
                                  "NULL", (book, _sql(session))).fetchone()[0]

    def attempts(self, book: str, session: date) -> int:
        return self._conn.execute("SELECT COUNT(DISTINCT structure_id) FROM orders WHERE book=? AND role='ENTRY' "
                                  "AND substr(created_ts,1,10)=?", (book, _sql(session))).fetchone()[0]

    def orders_today(self, book: str, session: date) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM orders WHERE book=? AND role<>'EXTERNAL' "
                                  "AND substr(created_ts,1,10)=?", (book, _sql(session))).fetchone()[0]

    def entry_orders_last_60s(self, book: str, now_wall: datetime) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM orders WHERE book=? AND role='ENTRY' AND created_ts >= ?",
                                  (book, iso(now_wall - timedelta(seconds=60)))).fetchone()[0]

    def realized_net(self, book: str, session: date) -> float:
        net = self._conn.execute("SELECT COALESCE(SUM(net), 0) FROM trades WHERE book=? AND session=?",
                                 (book, _sql(session))).fetchone()[0]
        return round(float(net), 2)

    def account_realized_net(self, account: Account, session: date) -> float:
        net = self._conn.execute("SELECT COALESCE(SUM(net), 0) FROM trades WHERE account=? AND session=?",
                                 (_sql(account), _sql(session))).fetchone()[0]
        return round(float(net), 2)

    def first_loss_today(self, book: str, session: date) -> bool:
        return bool(self._conn.execute("SELECT EXISTS(SELECT 1 FROM trades WHERE book=? AND session=? AND net<0)",
                                       (book, _sql(session))).fetchone()[0])

    def active_structures(self, book: str) -> int:
        return self._conn.execute(f"SELECT COUNT(*) FROM structures WHERE book=? AND state NOT IN {_TERMINAL_SQL}",
                                  (book,)).fetchone()[0]

    def book_counters(self, book: str, session: date, now_wall: datetime) -> BookCounters:
        return BookCounters(
            book=book, session=session, entries=self.entries(book, session), attempts=self.attempts(book, session),
            orders_today=self.orders_today(book, session),
            entry_orders_60s=self.entry_orders_last_60s(book, now_wall),
            realized_net=self.realized_net(book, session), first_loss_today=self.first_loss_today(book, session),
            active=self.active_structures(book))

    def cooldowns(self, book: str, session: date, *, reentry_min: float, reject_min: float,
                  void_min: float) -> dict[str, datetime]:
        """underlying -> cooldown end: max over today's terminal structures of closed_ts + REENTRY (CLOSED,
        CLOSED_EXTERNAL) | REJECT (VOID 'REJECTED%') | VOID (other VOID) minutes. A terminal row missing closed_ts
        counts from reserved_ts rather than escaping its cooldown."""
        minutes = {"REENTRY": reentry_min, "REJECT": reject_min, "VOID": void_min}
        rows = self._conn.execute(
            "SELECT underlying, COALESCE(closed_ts, reserved_ts) AS t, "
            "CASE WHEN state IN ('CLOSED','CLOSED_EXTERNAL') THEN 'REENTRY' "
            "WHEN void_reason LIKE 'REJECTED%' THEN 'REJECT' ELSE 'VOID' END AS kind "
            f"FROM structures WHERE book=? AND session=? AND state IN {_TERMINAL_SQL}", (book, _sql(session)))
        out: dict[str, datetime] = {}
        for r in rows.fetchall():
            until = parse_wall(r["t"]) + timedelta(minutes=minutes[r["kind"]])
            if r["underlying"] not in out or until > out[r["underlying"]]:
                out[r["underlying"]] = until
        return out

    def cooldown_until(self, book: str, underlying: str, session: date, *, reentry_min: float, reject_min: float,
                       void_min: float) -> datetime | None:
        return self.cooldowns(book, session, reentry_min=reentry_min, reject_min=reject_min,
                              void_min=void_min).get(underlying)

    def live_sessions(self, strategy: str) -> int:
        """Distinct sessions with a LIVE:<strategy> structure since strategy_modes.live_since_session (0 if unset)."""
        return self._conn.execute(
            "SELECT COUNT(DISTINCT s.session) FROM structures s JOIN strategy_modes m ON m.strategy = ? "
            "WHERE s.book = 'LIVE:' || ? AND m.live_since_session IS NOT NULL AND s.session >= m.live_since_session",
            (strategy, strategy)).fetchone()[0]

    # -- retention ------------------------------------------------------------------------------------------
    @core_only
    def prune(self, now_wall: datetime, export_dir: str | os.PathLike, *,
              quote_check_days: int = QUOTE_CHECK_RETENTION_DAYS,
              snapshot_days: int = BROKER_SNAPSHOT_RETENTION_DAYS) -> PruneResult:
        """Weekly EOD prune (§4.3): export quote_checks older than quote_check_days to
        <export_dir>/quote_checks/<session>.csv.gz, delete exactly those rows, then delete broker_snapshots older
        than snapshot_days. Nothing else is ever deleted. Runs in its own tx (a savepoint inside the core's)."""
        qc_cut = iso(now_wall - timedelta(days=quote_check_days))
        snap_cut = iso(now_wall - timedelta(days=snapshot_days))
        with self.tx():
            n_old = self._conn.execute("SELECT COUNT(*) FROM quote_checks WHERE ts < ?", (qc_cut,)).fetchone()[0]
            path, exported = None, 0
            if n_old:
                cur = self._conn.execute("SELECT * FROM quote_checks WHERE ts < ? ORDER BY id", (qc_cut,))
                header = [d[0] for d in cur.description]
                path, exported = _export_csv_gz(Path(export_dir) / "quote_checks", str(session_of(now_wall)),
                                                header, cur)
            deleted = self._w("DELETE FROM quote_checks WHERE ts < ?", (qc_cut,)).rowcount if n_old else 0
            if not exported == deleted == n_old:
                raise StoreError(f"prune: counted {n_old} old quote_checks, exported {exported}, deleted {deleted}")
            snaps = self._w("DELETE FROM broker_snapshots WHERE req_ts < ?", (snap_cut,)).rowcount
        return PruneResult(exported, deleted, snaps, path)


def _export_csv_gz(directory: Path, stem: str, header: list[str], rows: Iterable[Sequence[Any]]) -> tuple[Path, int]:
    """Write header + rows to a new <stem>[-n].csv.gz (temp file, fsync, rename). Returns (path, rows written)."""
    directory.mkdir(parents=True, exist_ok=True)
    path, n = directory / f"{stem}.csv.gz", 1
    while path.exists():
        path, n = directory / f"{stem}-{n}.csv.gz", n + 1
    tmp = path.with_name(path.name + ".part")
    count = 0
    with open(tmp, "wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz, \
                io.TextIOWrapper(gz, encoding="utf-8", newline="") as text:
            writer = csv.writer(text)
            writer.writerow(header)
            for r in rows:
                writer.writerow(tuple(r))
                count += 1
        raw.flush()
        os.fsync(raw.fileno())
    os.replace(tmp, path)
    return path, count


def open_readonly(path: str | os.PathLike) -> Store:
    """A query-only Store (SQLite mode=ro + query_only) for the CLI, reports and the self-check."""
    return Store(path, readonly=True)
