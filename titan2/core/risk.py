"""Entry gates, MTM, the daily stop, the worst-case cap and the halt rules (spec §8.2, §8.6-8.8).

Pure functions over frozen snapshots. The engine builds a GateContext per (intent, book) check and applies what comes
back: intent_routes rows, GATE_ERROR events, halts, alerts and forced exits. Nothing here does I/O, and nothing here
can block, delay or size an exit: gates refuse ENTRIES only, and halts never stop the exit loop.

Fail closed. Missing, stale, malformed or contradictory input refuses the entry. Every check is written as "pass only
while the good condition holds" (`if not age <= max_age: refuse`), so a NaN or a wrong type refuses instead of
slipping through a comparison. A gate that raises becomes Refusal(<gate>, "GATE_ERROR") with the exception text in
GateOutcome.error; the engine writes the GATE_ERROR event and note_gate_error() raises HALT(GATE_ERRORS, GLOBAL, ACK)
at 5 within 5 minutes.

Gate order (the first refusal is recorded):
  pre-reserve  G01 STRATEGY  G02 STRUCTURE  G03 TTL  G04 HALTS  G05 SESSION  G06 FEED  G07 RECON  G08 ELIGIBILITY
               G09 BOOK_CAPS  G10 UNDERLYING  G11 COOLDOWN  G12 DAY_RISK  G13 LIVE
  post-quote   G14 TTL  G15 CONTRACT  G16 RECHECK  G17 QUOTE  G18 SIZE  G19 LIQUIDITY  G20 WORST_CASE  G21 FUNDS
  final        G22 FINAL: TTL and halts again, immediately before the order row is written
QUALITY books (SHADOW:<s>, PLACEBO:<s>) skip G09, G11, G12, G20 and G21, and G04 checks only GLOBAL/ACCOUNT halts.
waits_for_pending() tells the gateway when a G09 refusal is due only to pending structures (RESERVED, or ENTERING and
not yet opened: every G01-G13 gate passes on settled(), the same snapshot with them voided), so the intent may wait
for them instead of being refused (FINAL_SPEC errata 2026-10-14).

Operator halts (PAUSE, FLATTEN, KILL, SAFE_FLAG) refuse LIVE entries and DIVERT PAPER:<s> intents to SHADOW:<s> (the
session is marked IMPAIRED) so operator timing cannot censor the G1 sample. QUALITY books ignore PAUSE only: without
that a PAUSE divert could never trade, while FLATTEN, KILL and SAFE_FLAG stop every book, simulated ones included.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, datetime
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, Protocol, Sequence

from titan2.charges import OrderFills, RateSet, breakeven_px, mtm_net, order_charges, round_money
from titan2.clock import SessionCalendar, to_ist
from titan2.core.events import Alert, MarginsResult
# HaltRequest is recon's halt value type, so the engine applies recon's and risk's halts the same way.
from titan2.core.recon import HaltRequest, ReconStatus
from titan2.model import (
    PRICE_EPS, Account, FeedHealth, Halt, HaltClears, Instrument, Intent, Level, Mode, Policy, Quote, Refusal, Side,
    Sized, StructureType, Underlying, UnderlyingTick, age_since_receipt, is_tick_multiple, walk,
)
from titan2.settings import (
    FUNDS_MAX_AGE_S, FUNDS_PREMIUM_FACTOR, GATE_ERROR_LIMIT, GATE_ERROR_WINDOW_S, LAST_TRADE_FUTURE_S,
    LIQ_DEPTH_LEVELS, LIQ_MIN_SPREAD_TICKS, MONO_SKEW_S, WORST_CASE_PAD_RS, Settings,
)

__all__ = [
    "BookState", "CLEAR_ACTIONS", "DailyStopCheck", "Divert", "FINAL_GATE", "GATE_ERROR", "GATES", "GateContext",
    "GateOutcome", "HALT_RULES", "HaltRequest", "HaltRule", "InstrumentsView", "LiveAccountState",
    "LivePreconditions", "OPERATOR_HALTS", "POST_QUOTE_GATES", "PRE_RESERVE_GATES", "QUALITY_IGNORED_HALTS",
    "QUALITY_SKIPS", "RECOVERED", "SELFTEST_BOOK", "StopTrip",
    "book_account", "book_day_net", "book_mtm", "book_policy", "book_realized_net", "books_for", "can_clear",
    "charges_hurdle_pct",
    "check_daily_stops", "daily_stop_room", "entry_charges_estimate", "entry_halts", "final_gate",
    "gate_book_caps", "gate_contract", "gate_cooldown", "gate_day_risk", "gate_eligibility", "gate_feed",
    "gate_funds", "gate_halts", "gate_liquidity", "gate_live", "gate_quote", "gate_recheck", "gate_recon",
    "gate_session", "gate_size", "gate_strategy", "gate_structure", "gate_ttl", "gate_underlying",
    "gate_worst_case", "is_operator_halt", "leg_mtm", "liquidity", "live_account_realized_net", "live_account_state",
    "loss_streak_halt", "make_halt", "note_gate_error", "parse_book", "post_quote_gates", "pre_reserve_gates",
    "rollover_clears", "settled", "waits_for_pending", "worst_case_loss", "worst_case_room",
]

GATE_ERROR = "GATE_ERROR"
GATES = (                                    # (spec id, gate name): the name is what Refusal.gate and intent_routes carry
    ("G01", "STRATEGY"), ("G02", "STRUCTURE"), ("G03", "TTL"), ("G04", "HALTS"), ("G05", "SESSION"),
    ("G06", "FEED"), ("G07", "RECON"), ("G08", "ELIGIBILITY"), ("G09", "BOOK_CAPS"), ("G10", "UNDERLYING"),
    ("G11", "COOLDOWN"), ("G12", "DAY_RISK"), ("G13", "LIVE"),
    ("G14", "TTL"), ("G15", "CONTRACT"), ("G16", "RECHECK"), ("G17", "QUOTE"), ("G18", "SIZE"),
    ("G19", "LIQUIDITY"), ("G20", "WORST_CASE"), ("G21", "FUNDS"), ("G22", "FINAL"),
)
PRE_RESERVE_GATES = tuple(name for gid, name in GATES[:13])
POST_QUOTE_GATES = tuple(name for gid, name in GATES[13:21])
FINAL_GATE = "FINAL"
QUALITY_SKIPS = frozenset({"BOOK_CAPS", "COOLDOWN", "DAY_RISK", "WORST_CASE", "FUNDS"})

MAX_TTL_S = 10.0              # Intent.ttl_s bound (§4.1; also the intents CHECK)
# MONO_SKEW_S (G03/G14) and LAST_TRADE_FUTURE_S (G19) change which entries pass, so they live in
# settings.SEMANTIC_CONSTANTS.
SELFTEST_BOOK = "LIVE:_selftest"


# ---------------------------------------------------------------------------------------------------------------
# Small predicates
# ---------------------------------------------------------------------------------------------------------------
def _finite(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _pos(x: object) -> bool:
    return _finite(x) and x > 0


def _count(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and x >= 0


def _aware(x: object) -> bool:
    return isinstance(x, datetime) and x.tzinfo is not None and x.utcoffset() is not None


def _top_qty(levels: Sequence[Level], n: int) -> int:
    return sum(lv.qty for lv in levels[:n])


def _rs(x: float) -> str:
    return f"{x:,.2f}"


# ---------------------------------------------------------------------------------------------------------------
# Books (§8.3): PAPER:<s> and LIVE:<s> are FULL; SHADOW:<s> and PLACEBO:<s> are QUALITY on the SHADOW account.
# RECOVERED:<account>[:<seq>] is recovery's own book (a lost DB's imports, an adopted late fill; engine
#   _recovered_book, _adopt_late_fill): real positions of that account, so FULL, counted by the daily stop and, on
#   LIVE, by the account guard (review round 2: they were outside both). No intent routes to one, and the entry
#   caps (I11) leave them out: recovery fills them, never the gateway.
# ---------------------------------------------------------------------------------------------------------------
RECOVERED = "RECOVERED"
_BOOK_KINDS: Mapping[str, tuple[Account | None, Policy]] = MappingProxyType({
    "PAPER": (Account.PAPER, Policy.FULL), "LIVE": (Account.LIVE, Policy.FULL),
    "SHADOW": (Account.SHADOW, Policy.QUALITY), "PLACEBO": (Account.SHADOW, Policy.QUALITY),
    RECOVERED: (None, Policy.FULL),             # the account is the name's second field
})
_ACCOUNT_NAMES = frozenset(a.value for a in Account)


def parse_book(book: object) -> tuple[str, str] | None:
    """'PAPER:opening_drive' -> ('PAPER', 'opening_drive'); 'RECOVERED:LIVE:3' -> ('RECOVERED', 'LIVE:3') (an
    account, then an optional seq); None for anything that is not exactly a book name."""
    if not isinstance(book, str):
        return None
    kind, sep, strategy = book.partition(":")
    if kind == RECOVERED:
        acct, sep2, seq = strategy.partition(":")
        if not sep or acct not in _ACCOUNT_NAMES or (sep2 and not (seq.isascii() and seq.isdigit())):
            return None
        return kind, strategy
    if not sep or kind not in _BOOK_KINDS or not strategy or strategy.strip() != strategy or ":" in strategy:
        return None
    return kind, strategy


def book_account(book: object) -> Account | None:
    parsed = parse_book(book)
    if parsed is None:
        return None
    acct = _BOOK_KINDS[parsed[0]][0]
    return Account(parsed[1].partition(":")[0]) if acct is None else acct


def book_policy(book: object) -> Policy | None:
    """FULL or QUALITY; None for the self-test book (it never goes through the gates) and for malformed names."""
    parsed = parse_book(book)
    if parsed is None or book == SELFTEST_BOOK:
        return None
    return _BOOK_KINDS[parsed[0]][1]


def books_for(strategy: str, mode: Mode | str, is_placebo: bool = False) -> tuple[str, ...]:
    """The books an intent routes to (§10.5), LIVE first: OFF none; SHADOW its SHADOW book; PAPER its PAPER book;
    LIVE its LIVE book and the PAPER twin. A placebo routes to PLACEBO:<s> while the strategy has a PAPER book."""
    mode = Mode(mode)
    if is_placebo:
        return (f"PLACEBO:{strategy}",) if mode in (Mode.PAPER, Mode.LIVE) else ()
    return {Mode.OFF: (), Mode.SHADOW: (f"SHADOW:{strategy}",), Mode.PAPER: (f"PAPER:{strategy}",),
            Mode.LIVE: (f"LIVE:{strategy}", f"PAPER:{strategy}")}[mode]


# ---------------------------------------------------------------------------------------------------------------
# Halts (§8.7): sticky DB rows by (scope, kind). They block entries in their scope and never block an exit.
# ---------------------------------------------------------------------------------------------------------------
FLAT = "FLAT"                 # the clear needs verified flat (FLATTEN by RESUME, KILL by ACK)
RECON_CLEAN = "RECON_CLEAN"   # the clear needs the account's recon to be CLEAN at clear time
SAFE_OFF = "SAFE_OFF"         # the clear needs the process to be out of --safe mode (else entries would resume)

CLEAR_ACTIONS = ("RESUME", "ACK", "ACK_FOREIGN", "NEXT_SESSION", "AUTO")


@dataclass(frozen=True, slots=True)
class HaltRule:
    clears: HaltClears
    scopes: tuple[str, ...]       # scope forms this kind is raised with: GLOBAL | ACCOUNT | ACCOUNT:LIVE | BOOK | SYMBOL
    operator: bool = False        # operator halt: PAPER intents divert to SHADOW instead of being refused
    needs: str | None = None      # extra condition for the clear: FLAT | RECON_CLEAN | SAFE_OFF


_G, _A, _AL, _B, _S = ("GLOBAL",), ("ACCOUNT",), ("ACCOUNT:LIVE",), ("BOOK",), ("SYMBOL",)
HALT_RULES: Mapping[str, HaltRule] = MappingProxyType({
    "PAUSE": HaltRule(HaltClears.RESUME, _G, operator=True),
    "FLATTEN": HaltRule(HaltClears.RESUME, _G, operator=True, needs=FLAT),
    "KILL": HaltRule(HaltClears.ACK, _G, operator=True, needs=FLAT),
    "SAFE_FLAG": HaltRule(HaltClears.ACK, _G, operator=True, needs=SAFE_OFF),
    "DAILY_STOP": HaltRule(HaltClears.NEXT_SESSION, ("BOOK", "ACCOUNT:LIVE")),
    "LOSS_STREAK": HaltRule(HaltClears.NEXT_SESSION, _B),
    "CONTRACT_BLOCKED": HaltRule(HaltClears.NEXT_SESSION, _S),
    "LIVE_DISABLED_TODAY": HaltRule(HaltClears.NEXT_SESSION, _AL),
    "MISMATCH": HaltRule(HaltClears.ACK, _A, needs=RECON_CLEAN),
    "UNKNOWN_POSITION": HaltRule(HaltClears.ACK, _A, needs=RECON_CLEAN),      # or ACK_FOREIGN with the exact qty
    "SHORT_DETECTED": HaltRule(HaltClears.ACK, _G, needs=RECON_CLEAN),
    "INVARIANT": HaltRule(HaltClears.ACK, ("GLOBAL", "ACCOUNT")),
    "EXCEPTION": HaltRule(HaltClears.ACK, _G),
    "GATE_ERRORS": HaltRule(HaltClears.ACK, _G),
    "ORDER_STUCK": HaltRule(HaltClears.ACK, _B),
    "REJECTS": HaltRule(HaltClears.ACK, _B),
    "RUNAWAY": HaltRule(HaltClears.ACK, _B),
    "EXIT_DIFFICULTY": HaltRule(HaltClears.AUTO, _B),         # when that leg is flat
    "TOKEN": HaltRule(HaltClears.AUTO, _G),                   # when profile() succeeds
    "STALE_FEED": HaltRule(HaltClears.AUTO, _G),              # after 60 s healthy
    "STALE_RECON": HaltRule(HaltClears.AUTO, _A),             # after 2 good reads
    "SETTINGS_INVALID": HaltRule(HaltClears.AUTO, _G),        # at the next start with a valid file
    "CLOCK_UNSYNCED": HaltRule(HaltClears.AUTO, _G),
    "NO_INSTRUMENTS": HaltRule(HaltClears.AUTO, _G),
    "V1_ACTIVE": HaltRule(HaltClears.AUTO, _G),
})
OPERATOR_HALTS = frozenset(k for k, r in HALT_RULES.items() if r.operator)
QUALITY_IGNORED_HALTS = frozenset({"PAUSE"})   # see the module docstring

_SCOPE_KINDS = ("ACCOUNT", "BOOK", "STRATEGY", "UNDERLYING", "SYMBOL")


def _scope(scope: object) -> tuple[str, str]:
    """('GLOBAL', '') | ('ACCOUNT', 'LIVE') | ('BOOK', 'PAPER:s') | ('STRATEGY', s) | ('UNDERLYING', 'NSE:X') |
    ('SYMBOL', 'NFO:Y'); anything else is ('UNKNOWN', text), which blocks every entry (fail closed)."""
    if scope == "GLOBAL":
        return "GLOBAL", ""
    if isinstance(scope, str):
        kind, sep, value = scope.partition(":")
        if sep and value and kind in _SCOPE_KINDS:
            return kind, value
    return "UNKNOWN", str(scope)


def make_halt(kind: str, scope: str, reason: str) -> HaltRequest:
    """A HaltRequest for a known kind with its catalog clear rule; ValueError for a scope the kind is never raised with."""
    rule = HALT_RULES.get(kind)
    if rule is None:
        raise ValueError(f"unknown halt kind {kind!r}")
    skind, value = _scope(scope)
    if skind == "ACCOUNT" and value not in {a.value for a in Account}:
        skind = "UNKNOWN"
    ok = ((skind == "GLOBAL" and "GLOBAL" in rule.scopes)
          or (skind == "ACCOUNT" and ("ACCOUNT" in rule.scopes or (value == "LIVE" and "ACCOUNT:LIVE" in rule.scopes)))
          or (skind == "BOOK" and "BOOK" in rule.scopes and parse_book(value) is not None)
          or (skind == "SYMBOL" and "SYMBOL" in rule.scopes))
    if not ok:
        raise ValueError(f"halt {kind} cannot have scope {scope!r} (allowed: {', '.join(rule.scopes)})")
    return HaltRequest(kind, scope, rule.clears, reason)


def is_operator_halt(h: Halt) -> bool:
    """A genuine operator halt: flagged operator on its row, an operator kind, and GLOBAL (all three must agree)."""
    return h.operator is True and h.kind in OPERATOR_HALTS and h.scope == "GLOBAL"


def entry_halts(halts: Iterable[Halt], *, account: Account | None, book: str, strategy: str, underlying: str,
                policy: Policy | None) -> tuple[Halt, ...]:
    """The active halts that block an entry into `book` (G04, G22).

    FULL books: GLOBAL, ACCOUNT:<account>, BOOK:<book>, STRATEGY:<strategy> and UNDERLYING:<underlying> halts.
    QUALITY books: GLOBAL and ACCOUNT:<account> only, and an operator PAUSE does not block them.
    SYMBOL halts are G15's. A halt with a scope this code cannot parse blocks every book (fail closed); a policy of
    None is treated as FULL (the strictest).
    """
    acct = None if account is None else Account(account).value
    targeted = {("BOOK", book), ("STRATEGY", strategy), ("UNDERLYING", underlying)}
    quality = policy is Policy.QUALITY
    out = []
    for h in halts:
        kind, value = _scope(h.scope)
        if kind in ("GLOBAL", "UNKNOWN"):
            hit = True
        elif kind == "ACCOUNT":
            hit = value == acct
        else:
            hit = not quality and (kind, value) in targeted
        if hit and quality and h.kind in QUALITY_IGNORED_HALTS and is_operator_halt(h):
            hit = False
        if hit:
            out.append(h)
    return tuple(out)


def can_clear(halt: Halt, action: str, *, today: date, flat: bool = False, recon_clean: bool = False,
              foreign_exact: bool = False, condition_met: bool = False, safe_mode: bool = False) -> tuple[bool, str]:
    """(True, why) when `action` clears this active halt now, else (False, why the halt stays).

    action: RESUME | ACK | ACK_FOREIGN (UNKNOWN_POSITION with the exact broker qty) | NEXT_SESSION (the first Timer of
    a later session) | AUTO (the kind's own condition, condition_met, is now true). The catalog rule of the kind
    decides, not the row's `clears` text. A kind this code does not know can only be cleared by an operator ACK.
    """
    if action not in CLEAR_ACTIONS:
        return False, f"unknown clear action {action!r}"
    rule = HALT_RULES.get(halt.kind)
    if rule is None:
        if action == "ACK":
            return True, f"unknown halt kind {halt.kind!r}: cleared by operator ACK"
        return False, f"unknown halt kind {halt.kind!r}: only an operator ACK clears it"
    if action == "ACK_FOREIGN":
        if halt.kind != "UNKNOWN_POSITION":
            return False, "ACK_FOREIGN clears UNKNOWN_POSITION only"
        if not foreign_exact:
            return False, "the acknowledged qty does not match the broker position exactly"
        return True, "foreign position acknowledged exactly"
    if action != rule.clears.value:
        return False, f"{halt.kind} clears by {rule.clears.value}, not {action}"
    if action == "NEXT_SESSION" and not halt.session < today:
        return False, f"raised in session {halt.session}; clears at the next session"
    if action == "AUTO" and condition_met is not True:
        return False, f"{halt.kind}: its clear condition is not met yet"
    if rule.needs == FLAT and flat is not True:
        return False, f"{halt.kind}: flat is not verified"
    if rule.needs == RECON_CLEAN and recon_clean is not True:
        return False, f"{halt.kind}: recon is not CLEAN"
    if rule.needs == SAFE_OFF and safe_mode is not False:
        return False, "the process still runs in --safe mode: unset TITAN2_SAFE and restart first"
    return True, f"{halt.kind} cleared by {action}"


def rollover_clears(halts: Iterable[Halt], today: date) -> tuple[Halt, ...]:
    """The NEXT_SESSION halts the first Timer of session `today` clears (raised in an earlier session)."""
    return tuple(h for h in halts if can_clear(h, "NEXT_SESSION", today=today)[0])


def note_gate_error(recent: Sequence[float], now_mono: float) -> tuple[tuple[float, ...], HaltRequest | None]:
    """Record one gate error: (the window to keep, HALT(GATE_ERRORS) once 5 fall within 5 minutes, else None)."""
    window = tuple(t for t in recent if now_mono - t < GATE_ERROR_WINDOW_S) + (now_mono,)
    if len(window) >= GATE_ERROR_LIMIT:
        return window, make_halt("GATE_ERRORS", "GLOBAL",
                                 f"{len(window)} gate errors within {GATE_ERROR_WINDOW_S:.0f} s")
    return window, None


def loss_streak_halt(book: str, trade_net: float, s: Settings) -> HaltRequest | None:
    """HALT(LOSS_STREAK, BOOK, NEXT_SESSION) when STOP_AFTER_FIRST_LOSS is set and a FULL book closes a loss."""
    if s.STOP_AFTER_FIRST_LOSS and book_policy(book) is Policy.FULL and _finite(trade_net) and trade_net < 0:
        return make_halt("LOSS_STREAK", f"BOOK:{book}", f"first loss today ({_rs(trade_net)}): STOP_AFTER_FIRST_LOSS")
    return None


# ---------------------------------------------------------------------------------------------------------------
# The snapshot every gate reads
# ---------------------------------------------------------------------------------------------------------------
class InstrumentsView(Protocol):
    """What the gates read from today's titan2.instruments.InstrumentStore (duck-typed, not imported)."""

    def underlying(self, symbol_or_name: str) -> Underlying | None: ...

    def ineligible_reason(self, underlying: str, ban_list: Iterable[str] | None, allow: Iterable[str] = (),
                          deny: Iterable[str] = ()) -> str | None: ...


@dataclass(frozen=True, slots=True)
class BookState:
    """One book's counters for today (§4.5) plus its live MTM; the engine derives it from its rows."""

    book: str
    policy: Policy
    open: int                                # non-terminal structures past RESERVED, any session
    reserved: int                            # RESERVED structures
    entries: int                             # structures opened today
    attempts: int                            # structures with an ENTRY order today
    orders_today: int                        # bot orders today (EXTERNAL excluded)
    entry_orders_60s: int                    # ENTRY orders created in the last 60 s
    realized_net: float                      # Σ trades.net today
    mtm_net: float | None                    # book_mtm() over its OPEN/EXITING legs; None = unknown
    first_loss_today: bool
    cooldown_until: Mapping[str, datetime]   # underlying -> cooldown end (store.cooldowns)
    active_underlyings: frozenset[str]       # underlyings with a non-terminal structure in this book
    exposures_rs: tuple[float, ...] = ()     # G20: premium + entry charges per non-terminal structure with a plan or
    #                                          fills (ENTERING: planned qty x limit and estimated charges)
    pending: int = 0                         # today's RESERVED/ENTERING structures: not yet opened, but they may, so
    #                                          G09 counts them toward the entries cap (I11 entries <= MAX_ENTRIES)
    open_risk_rs: float | None = 0.0         # size()'s DAY_BUDGET input: the open and entering structures'
    #                                          stop-outs measured from their entries (sizing.remaining_stop_risk
    #                                          summed); None when one has no sizing plan or an unknown MTM (size()
    #                                          refuses)


@dataclass(frozen=True, slots=True)
class LiveAccountState:
    """The LIVE account guard: every LIVE book summed (live_account_state() builds it from the BookStates)."""

    open: int
    reserved: int
    realized_net: float
    mtm_net: float | None
    active_underlyings: frozenset[str]
    exposures_rs: tuple[float, ...] = ()
    open_risk_rs: float | None = 0.0         # Σ BookState.open_risk_rs over the LIVE books; None if one is None
    pending: int = 0                         # Σ BookState.pending over the LIVE books (settled())


@dataclass(frozen=True, slots=True)
class LivePreconditions:
    """The two human keys and the daily self-test for one LIVE strategy (§10.8)."""

    allow_live: bool        # the running process has ALLOW_LIVE=1
    unlocked: bool          # mode LIVE + trial CERTIFIED + live_unlock_report_id recorded
    selftest_passed: bool   # today's order-path self-test passed
    demoted: bool
    first_week: bool        # still inside its first LIVE_FIRST_WEEK_SESSIONS live sessions


@dataclass(frozen=True, slots=True)
class GateContext:
    """The frozen snapshot one gate check reads (§8.6); the engine builds one per (intent, book) check."""

    # time
    now_mono: float
    now_wall: datetime
    session: date
    # configuration
    settings: Settings
    calendar: SessionCalendar
    clock_synced: bool
    instruments_loaded: bool                   # today's dump is loaded and passed its sanity checks
    rates: RateSet                             # charges.rates_for(session)
    # eligibility
    instruments: InstrumentsView | None        # today's InstrumentStore
    ban_list: frozenset[str] | None            # today's F&O ban list; None = missing (then no stock is eligible)
    blocked_symbols: frozenset[str]            # option symbols under CONTRACT_BLOCKED
    # market data
    feed: FeedHealth
    signals_degraded: bool
    # broker views
    recon: Mapping[Account, ReconStatus]
    margins: Mapping[Account, MarginsResult]   # the last margins read per account, ok or not
    # halts and modes
    halts: tuple[Halt, ...]                    # active (uncleared) halt rows
    strategy_modes: Mapping[str, Mode]         # the effective mode of each strategy this session
    running_versions: Mapping[str, str]        # registered strategy -> the version this process runs
    disabled_strategies: frozenset[str]        # disabled for this session (a hook raised)
    structure_types: frozenset[StructureType]  # types with a StructureHandler (plugins.load_handlers)
    # the book under check, the LIVE account guard and the LIVE keys
    book: BookState
    live_account: LiveAccountState | None
    live: LivePreconditions | None


@dataclass(frozen=True, slots=True)
class Divert:
    """G04 on PAPER:<s> when every blocking halt is an operator halt: route the intent to SHADOW:<s> instead."""

    gate: str          # "HALTS"
    to_book: str       # "SHADOW:<s>"
    blocked_by: str    # the blocking operator halt kinds, sorted, comma-joined (structures.blocked_by)

    @property
    def detail(self) -> str:
        return f"DIVERT {self.to_book} ({self.blocked_by})"


@dataclass(frozen=True, slots=True)
class GateOutcome:
    """A gate run. result None means every gate passed; otherwise the first Refusal or Divert."""

    result: Refusal | Divert | None = None
    error: str | None = None                  # GATE_ERROR: the exception text for the GATE_ERROR event
    halts: tuple[HaltRequest, ...] = ()       # halts to raise with this refusal (G09 RUNAWAY)
    limit_px: float | None = None             # post_quote_gates, when passed: the planned entry limit ...
    sized: Sized | None = None                # ... and size() result the order is built from

    @property
    def passed(self) -> bool:
        return self.result is None

    @property
    def refusal(self) -> Refusal | None:
        return self.result if isinstance(self.result, Refusal) else None

    @property
    def divert(self) -> Divert | None:
        return self.result if isinstance(self.result, Divert) else None

    @property
    def gate_error(self) -> bool:
        return isinstance(self.result, Refusal) and self.result.detail == GATE_ERROR


# ---------------------------------------------------------------------------------------------------------------
# Pre-reserve gates G01-G13 (on IntentReceived, per routed book)
# ---------------------------------------------------------------------------------------------------------------
def gate_strategy(ctx: GateContext, intent: Intent, divert: Divert | None = None) -> Refusal | None:
    """G01: exactly registered; running version; the mode routes the intent to this book; not disabled."""
    book = ctx.book.book
    parsed, policy = parse_book(book), book_policy(book)
    if parsed is None or policy is None:
        return Refusal("STRATEGY", f"BOOK_INVALID: {book!r}")
    if Policy(ctx.book.policy) is not policy:
        return Refusal("STRATEGY", f"POLICY_MISMATCH: {book} is {policy.value}, context says {ctx.book.policy}")
    name = intent.strategy
    if not isinstance(name, str) or name not in ctx.running_versions:
        return Refusal("STRATEGY", f"NOT_REGISTERED: {name!r}")
    if parsed[1] != name:
        return Refusal("STRATEGY", f"WRONG_BOOK: {book} is not a book of {name}")
    if intent.strategy_version != ctx.running_versions[name]:
        return Refusal("STRATEGY", f"VERSION_MISMATCH: {intent.strategy_version!r} != {ctx.running_versions[name]!r}")
    if name in ctx.disabled_strategies:
        return Refusal("STRATEGY", "DISABLED")
    if not isinstance(intent.is_placebo, bool):
        return Refusal("STRATEGY", f"BAD_INTENT: is_placebo {intent.is_placebo!r}")
    try:
        mode = Mode(ctx.strategy_modes.get(name))
    except ValueError:
        return Refusal("STRATEGY", f"NO_MODE: {ctx.strategy_modes.get(name)!r}")
    routes = books_for(name, mode, intent.is_placebo)
    if book in routes:
        return None
    if (divert is not None and not intent.is_placebo and divert.to_book == book == f"SHADOW:{name}"
            and f"PAPER:{name}" in routes):
        return None
    return Refusal("STRATEGY", f"MODE_ROUTE: mode {mode.value} does not route to {book}")


def gate_structure(ctx: GateContext, intent: Intent) -> Refusal | None:
    """G02: a StructureHandler exists for the type (phase 1: LONG_CE / LONG_PE)."""
    try:
        st = StructureType(intent.structure)
    except ValueError:
        return Refusal("STRUCTURE", f"UNKNOWN_TYPE: {intent.structure!r}")
    if st not in ctx.structure_types:
        return Refusal("STRUCTURE", f"NO_HANDLER: {st.value}")
    return None


def gate_ttl(ctx: GateContext, intent: Intent, gate: str = "TTL") -> Refusal | None:
    """G03 / G14 (and part of G22): now_mono - created_mono <= ttl_s, with 0 < ttl_s <= 10."""
    ttl = intent.ttl_s
    if not (_finite(ttl) and 0 < ttl <= MAX_TTL_S):
        return Refusal(gate, f"BAD_TTL: {ttl!r}")
    if not (_finite(intent.created_mono) and _finite(ctx.now_mono)):
        return Refusal(gate, "BAD_CLOCK: monotonic times missing")
    age = ctx.now_mono - intent.created_mono
    if not age >= -MONO_SKEW_S:
        return Refusal(gate, f"CREATED_IN_FUTURE: {-age:.3f} s ahead")
    if not age <= ttl:
        return Refusal(gate, f"EXPIRED: {age:.2f} s > {ttl:g} s")
    return None


def gate_halts(ctx: GateContext, intent: Intent) -> Refusal | Divert | None:
    """G04: any active halt in the book's scopes refuses, or diverts a PAPER:<s> book blocked only by operator halts."""
    book = ctx.book.book
    blocking = entry_halts(ctx.halts, account=book_account(book), book=book, strategy=intent.strategy,
                           underlying=intent.underlying, policy=book_policy(book))
    if not blocking:
        return None
    parsed = parse_book(book)
    if parsed is not None and parsed[0] == "PAPER" and all(is_operator_halt(h) for h in blocking):
        return Divert("HALTS", f"SHADOW:{parsed[1]}", ",".join(sorted({h.kind for h in blocking})))
    return Refusal("HALTS", ", ".join(f"{h.kind}@{h.scope}" for h in blocking))


def gate_session(ctx: GateContext) -> Refusal | None:
    """G05: a trading session, inside [ENTRY_START, NO_NEW_AFTER) and the session's quoted hours, clock synced,
    today's instruments loaded."""
    s = ctx.settings
    if ctx.clock_synced is not True:
        return Refusal("SESSION", "CLOCK_UNSYNCED")
    now = to_ist(ctx.now_wall)
    if now.date() != ctx.session:
        return Refusal("SESSION", f"SESSION_MISMATCH: wall date {now.date()} != session {ctx.session}")
    if not ctx.calendar.is_trading_day(ctx.session):
        return Refusal("SESSION", f"NOT_TRADING_DAY: {ctx.session}")
    t = now.time()
    if not t >= s.ENTRY_START:
        return Refusal("SESSION", f"BEFORE_ENTRY_START: {t:%H:%M:%S} < {s.ENTRY_START:%H:%M:%S}")
    if not t < s.NO_NEW_AFTER:
        return Refusal("SESSION", f"AFTER_NO_NEW_AFTER: {t:%H:%M:%S} >= {s.NO_NEW_AFTER:%H:%M:%S}")
    # Option quotes right after the open are stale carry-overs: never before open + 30 s, never after the close.
    bounds, valid_from = ctx.calendar.session_bounds(ctx.session), ctx.calendar.quotes_valid_from(ctx.session)
    if bounds is None or valid_from is None or not valid_from <= now < bounds[1]:
        return Refusal("SESSION", "OUTSIDE_SESSION_HOURS")
    if ctx.instruments_loaded is not True:
        return Refusal("SESSION", "NO_INSTRUMENTS")
    return None


def gate_feed(ctx: GateContext) -> Refusal | None:
    """G06: connected; an underlying tick within FEED_MAX_TICK_GAP_S; warm for FEED_WARM_S; nothing degraded.

    The tick's age follows Quote.age (age_since_receipt): the WS thread stamps every underlying tick batch, also
    while this event runs, so a tick received after the event began is fresh (age 0), not stale (review round 3)."""
    s, f, now = ctx.settings, ctx.feed, ctx.now_mono
    if f is None or f.connected is not True:
        return Refusal("FEED", "DISCONNECTED")
    tick = f.last_underlying_tick_mono
    if tick is None or not age_since_receipt(tick, now) <= s.FEED_MAX_TICK_GAP_S:
        return Refusal("FEED", "STALE_TICK" if tick is not None else "NO_TICK")
    since = f.connected_since_mono
    if since is None or not now - since >= s.FEED_WARM_S:
        return Refusal("FEED", "WARMING")
    if f.degraded is not False or ctx.signals_degraded is not False:
        return Refusal("FEED", "DEGRADED")
    return None


def gate_recon(ctx: GateContext) -> Refusal | None:
    """G07: a CLEAN recon of the book's account within RECON_MAX_AGE_ENTRY_S."""
    acct = book_account(ctx.book.book)
    status = ctx.recon.get(acct) if acct is not None else None
    if status is None:
        return Refusal("RECON", f"NO_RECON: {acct}")
    if status.clean_within(ctx.now_mono, ctx.settings.RECON_MAX_AGE_ENTRY_S) is not True:
        return Refusal("RECON", f"NOT_CLEAN: no CLEAN {acct} recon within {ctx.settings.RECON_MAX_AGE_ENTRY_S:g} s")
    return None


def gate_eligibility(ctx: GateContext, intent: Intent) -> Refusal | None:
    """G08: in today's universe; for a stock, today's ban list loaded and the name not banned, allow/deny lists."""
    if ctx.instruments is None:
        return Refusal("ELIGIBILITY", "NO_INSTRUMENTS")
    s = ctx.settings
    reason = ctx.instruments.ineligible_reason(intent.underlying, ctx.ban_list, s.STOCK_ALLOWLIST, s.STOCK_DENYLIST)
    if reason is not None:
        return Refusal("ELIGIBILITY", f"{reason}: {intent.underlying}")
    return None


def gate_book_caps(ctx: GateContext) -> Refusal | None:
    """G09 (FULL): book positions, entries (LIVE first week: 1), attempts, orders/day, entry orders/min (RUNAWAY on
    the orders made; ENTRY_ORDERS with the reserved structures' orders still to come), and the LIVE account's
    positions. RUNAWAY is checked first so its halt is raised whenever it applies."""
    s, b = ctx.settings, ctx.book
    counts = (b.open, b.reserved, b.entries, b.attempts, b.orders_today, b.entry_orders_60s, b.pending)
    if not all(_count(c) for c in counts):
        return Refusal("BOOK_CAPS", f"BAD_COUNTERS: {counts!r}")
    if not b.entry_orders_60s < s.MAX_ENTRY_ORDERS_PER_MIN:
        return Refusal("BOOK_CAPS", f"RUNAWAY: {b.entry_orders_60s} entry orders in 60 s "
                                    f"(max {s.MAX_ENTRY_ORDERS_PER_MIN})")
    if not b.open + b.reserved < s.MAX_POSITIONS_PER_BOOK:
        return Refusal("BOOK_CAPS", f"POSITIONS: {b.open + b.reserved}/{s.MAX_POSITIONS_PER_BOOK}")
    live = book_account(b.book) is Account.LIVE
    max_entries = s.MAX_ENTRIES_PER_DAY
    if live:
        if ctx.live is None:
            return Refusal("BOOK_CAPS", "NO_LIVE_STATE")
        if ctx.live.first_week is not False:
            max_entries = min(max_entries, s.LIVE_FIRST_WEEK_MAX_ENTRIES)
    # structures still entering (or reserved) may open, and each reserved one will add an entry attempt: counting
    #   them keeps a burst of intents inside the caps (I11), not just the ones already opened
    if not b.entries + b.pending < max_entries:
        return Refusal("BOOK_CAPS", f"ENTRIES: {b.entries} + {b.pending} pending/{max_entries}")
    if not b.attempts + b.reserved < s.MAX_ENTRY_ATTEMPTS_PER_DAY:
        return Refusal("BOOK_CAPS", f"ATTEMPTS: {b.attempts} + {b.reserved} reserved/{s.MAX_ENTRY_ATTEMPTS_PER_DAY}")
    if not b.orders_today < s.MAX_ORDERS_PER_DAY_PER_BOOK:
        return Refusal("BOOK_CAPS", f"ORDERS: {b.orders_today}/{s.MAX_ORDERS_PER_DAY_PER_BOOK}")
    # each RESERVED structure sends one entry order at its decision, after this gate: counted as orders still to
    #   come, so no mix of fresh and woken intents puts more than MAX_ENTRY_ORDERS_PER_MIN entry orders inside 60 s
    #   (review of the held signals: one batch sent 4). Only pending structures are in the way (settled() frees them,
    #   so the intent waits) and no halt: RUNAWAY above still halts on the orders actually made
    if not b.entry_orders_60s + b.reserved < s.MAX_ENTRY_ORDERS_PER_MIN:
        return Refusal("BOOK_CAPS", f"ENTRY_ORDERS: {b.entry_orders_60s} in 60 s + {b.reserved} reserved/"
                                    f"{s.MAX_ENTRY_ORDERS_PER_MIN}")
    if live:
        la = ctx.live_account
        if la is None or not (_count(la.open) and _count(la.reserved)):
            return Refusal("BOOK_CAPS", "NO_LIVE_ACCOUNT")
        if not la.open + la.reserved < s.LIVE_ACCOUNT_MAX_POSITIONS:
            return Refusal("BOOK_CAPS", f"LIVE_ACCOUNT_POSITIONS: {la.open + la.reserved}/"
                                        f"{s.LIVE_ACCOUNT_MAX_POSITIONS}")
    return None


def gate_underlying(ctx: GateContext, intent: Intent) -> Refusal | None:
    """G10: one non-terminal structure per (book, underlying), and per (LIVE account, underlying)."""
    u = intent.underlying
    if u in ctx.book.active_underlyings:
        return Refusal("UNDERLYING", f"BOOK_ACTIVE: {u} already has a structure in {ctx.book.book}")
    if book_account(ctx.book.book) is Account.LIVE:
        if ctx.live_account is None:
            return Refusal("UNDERLYING", "NO_LIVE_ACCOUNT")
        if u in ctx.live_account.active_underlyings:
            return Refusal("UNDERLYING", f"LIVE_ACTIVE: {u} already has a LIVE structure")
    return None


def gate_cooldown(ctx: GateContext, intent: Intent) -> Refusal | None:
    """G11 (FULL): cooldown_until(book, underlying) is not after now. The end times come from Store.cooldowns, the
    one place the §4.5 rule lives (Engine.book_state passes it the Settings minutes)."""
    until = ctx.book.cooldown_until.get(intent.underlying)
    if until is None:
        return None
    if not _aware(until):
        return Refusal("COOLDOWN", f"BAD_COOLDOWN: {until!r}")
    if not until <= ctx.now_wall:
        return Refusal("COOLDOWN", f"UNTIL {to_ist(until):%H:%M:%S}")
    return None


def gate_day_risk(ctx: GateContext) -> Refusal | None:
    """G12 (FULL): book MTM known and realized + MTM > -DAILY_STOP_RS; first-loss stop; the same at the LIVE account."""
    s, b = ctx.settings, ctx.book
    if b.mtm_net is None:
        return Refusal("DAY_RISK", "MTM_UNKNOWN")
    day = b.realized_net + b.mtm_net
    if not day > -s.DAILY_STOP_RS:
        return Refusal("DAY_RISK", f"DAILY_STOP: day {_rs(day)} <= -{_rs(s.DAILY_STOP_RS)}")
    if s.STOP_AFTER_FIRST_LOSS and b.first_loss_today is not False:
        return Refusal("DAY_RISK", "FIRST_LOSS: STOP_AFTER_FIRST_LOSS and a loss already today")
    if book_account(b.book) is Account.LIVE:
        la = ctx.live_account
        if la is None or la.mtm_net is None:
            return Refusal("DAY_RISK", "LIVE_ACCOUNT_MTM_UNKNOWN")
        acct_day = la.realized_net + la.mtm_net
        if not acct_day > -s.LIVE_ACCOUNT_DAILY_STOP_RS:
            return Refusal("DAY_RISK", f"LIVE_ACCOUNT_DAILY_STOP: day {_rs(acct_day)} <= "
                                       f"-{_rs(s.LIVE_ACCOUNT_DAILY_STOP_RS)}")
    return None


def gate_live(ctx: GateContext) -> Refusal | None:
    """G13 (LIVE books only): ALLOW_LIVE=1, unlocked, today's self-test passed, not demoted."""
    if book_account(ctx.book.book) is not Account.LIVE:
        return None
    lp = ctx.live
    if lp is None:
        return Refusal("LIVE", "NO_LIVE_STATE")
    if lp.allow_live is not True:
        return Refusal("LIVE", "ALLOW_LIVE_OFF")
    if lp.unlocked is not True:
        return Refusal("LIVE", "NOT_UNLOCKED")
    if lp.selftest_passed is not True:
        return Refusal("LIVE", "NO_SELFTEST_TODAY")
    if lp.demoted is not False:
        return Refusal("LIVE", "DEMOTED")
    return None


# ---------------------------------------------------------------------------------------------------------------
# Post-quote gates G14-G21 (on QuoteResult) and G22
# ---------------------------------------------------------------------------------------------------------------
_PHASE1_OPT = {StructureType.LONG_CE: "CE", StructureType.LONG_PE: "PE"}


def gate_contract(ctx: GateContext, intent: Intent, contract: Instrument | Refusal | None) -> Refusal | None:
    """G15: the selected contract (handler.select / InstrumentStore.select_contract) exists exactly: an expiry that
    meets the DTE rule (re-checked here), the requested strike (never clamped), a valid lot and tick, not blocked."""
    if isinstance(contract, Refusal):
        return Refusal("CONTRACT", contract.detail)
    if not isinstance(contract, Instrument):
        return Refusal("CONTRACT", f"NO_CONTRACT: {contract!r}")
    c, s = contract, ctx.settings
    if c.underlying != intent.underlying:
        return Refusal("CONTRACT", f"UNDERLYING_MISMATCH: {c.underlying} != {intent.underlying}")
    want = _PHASE1_OPT.get(StructureType(intent.structure))
    if want is not None and c.opt_type != want:
        return Refusal("CONTRACT", f"OPT_TYPE_MISMATCH: {c.opt_type} for {intent.structure}")
    if not (isinstance(c.lot_size, int) and not isinstance(c.lot_size, bool) and c.lot_size > 0):
        return Refusal("CONTRACT", f"LOT_INVALID: {c.lot_size!r}")
    if not _pos(c.tick):
        return Refusal("CONTRACT", f"TICK_INVALID: {c.tick!r}")
    if not _pos(c.strike):
        return Refusal("CONTRACT", f"STRIKE_INVALID: {c.strike!r}")
    if ctx.instruments is None:
        return Refusal("CONTRACT", "NO_INSTRUMENTS")
    u = ctx.instruments.underlying(c.underlying)
    min_td = {"STOCK": s.MIN_TD_TO_EXPIRY_STOCK, "INDEX": s.MIN_TD_TO_EXPIRY_INDEX}.get(getattr(u, "kind", None))
    if min_td is None:
        return Refusal("CONTRACT", f"UNKNOWN_UNDERLYING: {c.underlying}")
    if not (isinstance(c.expiry, date) and not isinstance(c.expiry, datetime)
            and ctx.calendar.td(ctx.session, c.expiry) >= min_td):
        return Refusal("CONTRACT", f"DTE: {c.expiry} has < {min_td} sessions left")
    blocked = {f"SYMBOL:{c.symbol}", f"SYMBOL:{c.tradingsymbol}"}
    if c.symbol in ctx.blocked_symbols or any(h.scope in blocked for h in ctx.halts):
        return Refusal("CONTRACT", f"BLOCKED: {c.symbol}")
    return None


def gate_recheck(ctx: GateContext, intent: Intent, underlying: Quote | UnderlyingTick | None) -> Refusal | None:
    """G16: the underlying LTP is at most UNDERLYING_MAX_AGE_S old (receipt time) and inside [recheck.lo, recheck.hi].
    The age follows Quote.age (age_since_receipt): a tick the WS thread put in the quote cache after the event began
    is the newest there is, age 0 (review round 3)."""
    if underlying is None:
        return Refusal("RECHECK", "NO_UNDERLYING_LTP")
    if underlying.symbol != intent.underlying:
        return Refusal("RECHECK", f"WRONG_SYMBOL: {underlying.symbol}")
    age = age_since_receipt(underlying.recv_mono, ctx.now_mono)
    if not age <= ctx.settings.UNDERLYING_MAX_AGE_S:
        return Refusal("RECHECK", f"STALE: underlying LTP {age:.2f} s old")
    ltp = underlying.ltp
    if not _pos(ltp):
        return Refusal("RECHECK", f"NO_LTP: {ltp!r}")
    if not intent.recheck.contains(ltp):
        return Refusal("RECHECK", f"OUTSIDE: {ltp} not in [{intent.recheck.lo}, {intent.recheck.hi}]")
    return None


def gate_quote(ctx: GateContext, contract: Instrument, q: Quote | None) -> Refusal | None:
    """G17: the contract's quote exists, is at most QUOTE_MAX_AGE_ENTRY_S old, has both sides and ask > bid."""
    if q is None:
        return Refusal("QUOTE", "MISSING")
    if q.symbol != contract.symbol:
        return Refusal("QUOTE", f"WRONG_SYMBOL: {q.symbol} != {contract.symbol}")
    age = q.age(ctx.now_mono)
    if not 0.0 <= age <= ctx.settings.QUOTE_MAX_AGE_ENTRY_S:
        return Refusal("QUOTE", f"STALE: {age:.2f} s old")
    if q.bid is None or q.ask is None:
        return Refusal("QUOTE", "ONE_SIDED")
    if not q.ask > q.bid:
        return Refusal("QUOTE", f"CROSSED_OR_LOCKED: bid {q.bid} ask {q.ask}")
    return None


def gate_size(contract: Instrument, limit_px: float | None, sized: Sized | Refusal | None) -> Refusal | None:
    """G18: entry_limit() gave a tick-grid limit and size() gave whole lots (its Refusal passes through)."""
    if limit_px is None:
        return Refusal("SIZE", "NO_ENTRY_LIMIT")
    if not (_pos(limit_px) and is_tick_multiple(limit_px, contract.tick)):
        return Refusal("SIZE", f"BAD_LIMIT: {limit_px!r}")
    if isinstance(sized, Refusal):
        return Refusal("SIZE", sized.detail)
    if not isinstance(sized, Sized):
        return Refusal("SIZE", f"NOT_SIZED: {sized!r}")
    lot = contract.lot_size
    if not (_count(sized.lots) and sized.lots >= 1 and _count(sized.qty) and sized.qty == sized.lots * lot):
        return Refusal("SIZE", f"BAD_SIZE: qty {sized.qty!r} lots {sized.lots!r} lot {lot}")
    return None


def entry_charges_estimate(qty: int, limit_px: float, rates: RateSet) -> float:
    """Charges of one entry BUY of qty filled at limit_px."""
    return order_charges(OrderFills(Side.BUY, qty, qty * limit_px), rates).total


def charges_hurdle_pct(limit_px: float, qty: int, rates: RateSet) -> float:
    """(be_planned / limit - 1) x 100 for qty bought at limit_px (§7.1): the move needed just to cover charges."""
    c_in = entry_charges_estimate(qty, limit_px, rates)
    return (breakeven_px(qty * limit_px, qty, c_in, rates) / limit_px - 1.0) * 100.0


# Ported from agentic_trader/zerodha_tools.py:4547-4669 @b4694f3 — changes: fail closed (a missing quote, side, depth
#   or last-trade time refuses; v1 allowed the trade); no network call (the caller passes the decision quote); the
#   spread cap is max(2 ticks, LIQ_MAX_SPREAD_PCT of mid) instead of 4%; top-3 depth on BOTH sides must be
#   LIQ_DEPTH_MULT x qty (v1: the crossed side at 1x); the walk is bounded by the order's limit, must fill all of qty
#   (no 95% rule) and its average is measured against mid, not LTP; last-trade age and charges-hurdle checks added
def liquidity(q: Quote, qty: int, limit_px: float, tick: float, now_wall: datetime, s: Settings,
              rates: RateSet) -> Refusal | None:
    """G19 for an entry BUY of qty at limit_px on the decision quote q."""
    if not (_count(qty) and qty > 0 and _pos(limit_px) and _pos(tick)):
        return Refusal("LIQUIDITY", f"BAD_INPUT: qty {qty!r} limit {limit_px!r} tick {tick!r}")
    bid, ask, mid = q.bid, q.ask, q.mid()
    if bid is None or ask is None or mid is None or not ask > bid:
        return Refusal("LIQUIDITY", "NO_TWO_SIDED_BOOK")
    max_spread = max(LIQ_MIN_SPREAD_TICKS * tick, s.LIQ_MAX_SPREAD_PCT / 100.0 * mid)
    if not ask - bid <= max_spread + PRICE_EPS:
        return Refusal("LIQUIDITY", f"SPREAD: {ask - bid:.2f} > {max_spread:.2f} ({(ask - bid) / mid * 100:.2f}% of mid)")
    need = s.LIQ_DEPTH_MULT * qty
    for side, levels in (("ASK", q.asks), ("BID", q.bids)):
        top = _top_qty(levels, LIQ_DEPTH_LEVELS)
        if not top >= need:
            return Refusal("LIQUIDITY", f"DEPTH_{side}: top-{LIQ_DEPTH_LEVELS} {top} < {need:g}")
    w = walk(q.asks, qty, limit_px, Side.BUY)
    if w.avg_px is None or not w.fillable_qty >= qty:
        return Refusal("LIQUIDITY", f"WALK_SHORT: {w.fillable_qty}/{qty} fillable at <= {limit_px}")
    max_avg = mid * (1.0 + s.LIQ_MAX_SLIP_VS_MID_PCT / 100.0)
    if not w.avg_px <= max_avg + PRICE_EPS:
        return Refusal("LIQUIDITY", f"SLIPPAGE: walk avg {w.avg_px:.4f} > mid x {1 + s.LIQ_MAX_SLIP_VS_MID_PCT / 100:g}")
    if q.last_trade_wall is None:
        return Refusal("LIQUIDITY", "LAST_TRADE_UNKNOWN")
    age = (to_ist(now_wall) - to_ist(q.last_trade_wall)).total_seconds()
    if not -LAST_TRADE_FUTURE_S <= age <= s.LIQ_MAX_LAST_TRADE_AGE_S:
        return Refusal("LIQUIDITY", f"LAST_TRADE_STALE: {age:.0f} s ago")
    hurdle = charges_hurdle_pct(limit_px, qty, rates)
    if not hurdle <= s.MAX_CHARGES_HURDLE_PCT:
        return Refusal("LIQUIDITY", f"CHARGES_HURDLE: {hurdle:.2f}% > {s.MAX_CHARGES_HURDLE_PCT:g}%")
    return None


def gate_liquidity(ctx: GateContext, contract: Instrument, q: Quote, sized: Sized, limit_px: float) -> Refusal | None:
    """G19 on the context's clock, settings and rates."""
    return liquidity(q, sized.qty, limit_px, contract.tick, ctx.now_wall, ctx.settings, ctx.rates)


def worst_case_loss(realized_net: float, exposures_rs: Iterable[float], new_rs: float | None = None) -> float:
    """The day's loss in rupees (>= 0) if every open position, and the new one, went to zero with no bids:
    -min(realized, 0) + Σ (premium + entry charges + WORST_CASE_PAD_RS). Profits never extend the cap."""
    items = list(exposures_rs) + ([] if new_rs is None else [new_rs])
    if not _finite(realized_net) or not all(_finite(e) and e >= 0 for e in items):
        raise ValueError(f"worst_case_loss: bad inputs realized={realized_net!r} exposures={items!r}")
    return round_money(-min(realized_net, 0.0) + math.fsum(e + WORST_CASE_PAD_RS for e in items))


def gate_worst_case(ctx: GateContext, premium_rs: float, charges_rs: float) -> Refusal | None:
    """G20 (FULL): min(realized,0) - Σ_open(premium + charges + 100) - (new + charges + 100) >= -MAX_WORST_CASE, for
    the book and, for a LIVE book, the LIVE account."""
    cap = ctx.settings.MAX_WORST_CASE_DAY_LOSS_RS
    new = premium_rs + charges_rs
    loss = worst_case_loss(ctx.book.realized_net, ctx.book.exposures_rs, new)
    if not loss <= cap:
        return Refusal("WORST_CASE", f"BOOK: worst case {_rs(loss)} > {_rs(cap)}")
    if book_account(ctx.book.book) is Account.LIVE:
        la = ctx.live_account
        if la is None:
            return Refusal("WORST_CASE", "NO_LIVE_ACCOUNT")
        loss = worst_case_loss(la.realized_net, la.exposures_rs, new)
        if not loss <= cap:
            return Refusal("WORST_CASE", f"LIVE_ACCOUNT: worst case {_rs(loss)} > {_rs(cap)}")
    return None


def gate_funds(ctx: GateContext, premium_rs: float, charges_rs: float) -> Refusal | None:
    """G21 (FULL): the account's margins read is OK, at most 60 s old, and available >= premium x 1.1 + charges."""
    acct = book_account(ctx.book.book)
    m = ctx.margins.get(acct) if acct is not None else None
    if m is None or m.ok is not True or m.margins is None:
        return Refusal("FUNDS", f"MARGINS_UNAVAILABLE: {acct}")
    age = ctx.now_mono - m.recv_mono
    if not 0.0 <= age <= FUNDS_MAX_AGE_S:
        return Refusal("FUNDS", f"MARGINS_STALE: {age:.0f} s old")
    available = m.margins.available_rs
    need = premium_rs * FUNDS_PREMIUM_FACTOR + charges_rs
    if not (_finite(available) and available >= need):
        return Refusal("FUNDS", f"INSUFFICIENT: available {available!r} < {_rs(need)}")
    return None


# ---------------------------------------------------------------------------------------------------------------
# Runners: the gates in order, the first refusal wins, an exception is a GATE_ERROR refusal
# ---------------------------------------------------------------------------------------------------------------
Check = tuple[str, Callable[[], object]]


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


def _halts_for(r: Refusal, book: str) -> tuple[HaltRequest, ...]:
    if r.gate == "BOOK_CAPS" and r.detail.startswith("RUNAWAY") and book_policy(book) is not None:
        return (make_halt("RUNAWAY", f"BOOK:{book}", r.detail),)
    return ()


def _book_name(ctx: GateContext) -> str:
    name = getattr(getattr(ctx, "book", None), "book", "")
    return name if isinstance(name, str) else ""


def _skips(book: str) -> frozenset[str]:
    return QUALITY_SKIPS if book_policy(book) is Policy.QUALITY else frozenset()


def _run(checks: Sequence[Check], skip: frozenset[str], book: str) -> GateOutcome:
    for gate, check in checks:
        if gate in skip:
            continue
        try:
            r = check()
        except Exception as exc:     # a gate that raises refuses the entry; the engine logs GATE_ERROR with `error`
            return GateOutcome(Refusal(gate, GATE_ERROR), error=_error_text(exc))
        if r is None:
            continue
        if isinstance(r, Divert) and gate == "HALTS":
            return GateOutcome(r)
        if not isinstance(r, Refusal):
            return GateOutcome(Refusal(gate, GATE_ERROR), error=f"gate {gate} returned {type(r).__name__}")
        if r.gate != gate:
            r = Refusal(gate, f"{r.gate}: {r.detail}")
        return GateOutcome(r, halts=_halts_for(r, book))
    return GateOutcome(None)


def pre_reserve_gates(ctx: GateContext, intent: Intent, divert: Divert | None = None) -> GateOutcome:
    """G01-G13 for the routed book ctx.book. The first Refusal or Divert ends the run.

    divert: the Divert a PAPER:<s> run returned, when the caller re-runs the gates for its SHADOW:<s> target (G01
    then accepts that SHADOW book for a PAPER- or LIVE-mode strategy). The caller records the PAPER route as DIVERTED,
    marks the strategy's session IMPAIRED and sets structures.blocked_by = divert.blocked_by.
    """
    checks: tuple[Check, ...] = (
        ("STRATEGY", lambda: gate_strategy(ctx, intent, divert)),
        ("STRUCTURE", lambda: gate_structure(ctx, intent)),
        ("TTL", lambda: gate_ttl(ctx, intent)),
        ("HALTS", lambda: gate_halts(ctx, intent)),
        ("SESSION", lambda: gate_session(ctx)),
        ("FEED", lambda: gate_feed(ctx)),
        ("RECON", lambda: gate_recon(ctx)),
        ("ELIGIBILITY", lambda: gate_eligibility(ctx, intent)),
        ("BOOK_CAPS", lambda: gate_book_caps(ctx)),
        ("UNDERLYING", lambda: gate_underlying(ctx, intent)),
        ("COOLDOWN", lambda: gate_cooldown(ctx, intent)),
        ("DAY_RISK", lambda: gate_day_risk(ctx)),
        ("LIVE", lambda: gate_live(ctx)),
    )
    book = _book_name(ctx)
    return _run(checks, _skips(book), book)


def settled(ctx: GateContext) -> GateContext | None:
    """ctx as if every pending structure had voided: today's RESERVED and not yet opened ENTERING structures
    (BookState.pending) of the book and, for a LIVE book, of the LIVE account. The position, entry and reservation
    slots they hold are free, and so are the entry orders the reserved ones would still send (ENTRY_ORDERS); what
    they already did stays counted (their entry orders in attempts, orders, RUNAWAY and ENTRY_ORDERS; their
    underlyings in G10). None when nothing is pending or a count is malformed (fail closed: the refusal stands).
    Only waits_for_pending reads it: nothing is ever reserved on it."""
    b, la = ctx.book, ctx.live_account

    def ok(x: BookState | LiveAccountState) -> bool:
        return _count(x.open) and _count(x.reserved) and _count(x.pending) and x.pending <= x.open + x.reserved

    if not ok(b) or (la is not None and not ok(la)) or not (b.pending or (la is not None and la.pending)):
        return None
    if la is not None:
        la = replace(la, open=la.open + la.reserved - la.pending, reserved=0, pending=0)
    return replace(ctx, book=replace(b, open=b.open + b.reserved - b.pending, reserved=0, pending=0),
                   live_account=la)


def waits_for_pending(ctx: GateContext, intent: Intent, res: GateOutcome) -> bool:
    """G09 refused this route only because of pending structures (FINAL_SPEC errata 2026-10-14): a BOOK_CAPS refusal
    with no halt and no gate error, and G01-G13 all pass on settled(ctx). The gateway then lets the intent wait,
    holding nothing, instead of refusing it. Pure and total: anything unexpected is False (the refusal stands)."""
    r = res.refusal
    if r is None or r.gate != "BOOK_CAPS" or r.detail == GATE_ERROR or res.halts:
        return False
    alt = settled(ctx)
    return alt is not None and pre_reserve_gates(alt, intent).passed


Plan = Callable[[Instrument, Quote], "tuple[float | None, Sized | Refusal | None]"]


def post_quote_gates(ctx: GateContext, intent: Intent, contract: Instrument | Refusal | None, q: Quote | None,
                     underlying: Quote | UnderlyingTick | None, plan: Plan) -> GateOutcome:
    """G14-G21 for one RESERVED structure on its decision snapshot. When everything passes, the outcome carries the
    limit_px and Sized the order must be built from (nothing may change them afterwards).

    plan(contract, q) -> (limit_px, size_result) wraps exits.entry_limit() and sizing.size(); it is called only after
    G14-G17 passed, so it always sees a fresh two-sided quote of the selected contract. If it raises, G18 refuses
    with GATE_ERROR.
    """
    planned: dict[str, object] = {}

    def g18() -> Refusal | None:
        limit_px, sized = plan(contract, q)
        r = gate_size(contract, limit_px, sized)
        if r is None:
            premium = sized.qty * limit_px
            planned.update(limit_px=limit_px, sized=sized, premium=premium,
                           charges=entry_charges_estimate(sized.qty, limit_px, ctx.rates))
        return r

    checks: tuple[Check, ...] = (
        ("TTL", lambda: gate_ttl(ctx, intent)),
        ("CONTRACT", lambda: gate_contract(ctx, intent, contract)),
        ("RECHECK", lambda: gate_recheck(ctx, intent, underlying)),
        ("QUOTE", lambda: gate_quote(ctx, contract, q)),
        ("SIZE", g18),
        ("LIQUIDITY", lambda: gate_liquidity(ctx, contract, q, planned["sized"], planned["limit_px"])),
        ("WORST_CASE", lambda: gate_worst_case(ctx, planned["premium"], planned["charges"])),
        ("FUNDS", lambda: gate_funds(ctx, planned["premium"], planned["charges"])),
    )
    book = _book_name(ctx)
    out = _run(checks, _skips(book), book)
    if out.passed:
        return GateOutcome(None, limit_px=planned["limit_px"], sized=planned["sized"])
    return out


def final_gate(ctx: GateContext, intent: Intent) -> GateOutcome:
    """G22: TTL and halts re-checked immediately before the order row is created.

    A PAPER structure blocked only by operator halts raised after its reservation is refused with detail
    'OPERATOR_HALT: <kinds>' (it is already reserved, so it cannot divert): the caller marks the session IMPAIRED.
    """
    def g22() -> Refusal | None:
        r = gate_ttl(ctx, intent, gate=FINAL_GATE)
        if r is not None:
            return r
        h = gate_halts(ctx, intent)
        if isinstance(h, Divert):
            return Refusal(FINAL_GATE, f"OPERATOR_HALT: {h.blocked_by}")
        if isinstance(h, Refusal):
            return Refusal(FINAL_GATE, f"HALTS: {h.detail}")
        return None

    book = _book_name(ctx)
    return _run(((FINAL_GATE, g22),), frozenset(), book)


# ---------------------------------------------------------------------------------------------------------------
# MTM (§8.2), the daily stop (§8.8, D3) and the worst-case room (§8.8)
# ---------------------------------------------------------------------------------------------------------------
def leg_mtm(done: Sequence[OrderFills], open_qty: int, rb: float | None, quote_age_s: float | None,
            rates: RateSet, max_age_s: float) -> float | None:
    """One leg's MTM: its net if the open qty were sold now, in one order, at the realizable bid (charges.mtm_net).

    done = every executed order of the leg so far (entry fills, partial exits). A flat leg is its realized net and
    needs no quote. Otherwise None (unknown) unless rb is a price from a snapshot received at most max_age_s ago
    (the engine passes Settings.STALE_REFRESH_S, 3 s). Nothing is cached: an unknown MTM is never filled in.
    """
    if open_qty == 0:
        return mtm_net(done, 0, None, rates)
    if not (_pos(rb) and quote_age_s is not None and 0.0 <= quote_age_s <= max_age_s):
        return None
    return mtm_net(done, open_qty, rb, rates)


def book_mtm(leg_mtms: Iterable[float | None]) -> float | None:
    """Σ leg MTM over a book's OPEN/EXITING legs; None if any leg's MTM is unknown. No open legs: 0."""
    vals = list(leg_mtms)
    if not all(_finite(v) for v in vals):
        return None
    return round_money(math.fsum(vals))


def book_day_net(b: BookState) -> float | None:
    """realized + MTM for today; None while MTM is unknown (it blocks entries and the daily-stop decision)."""
    if b.mtm_net is None or not (_finite(b.mtm_net) and _finite(b.realized_net)):
        return None
    return b.realized_net + b.mtm_net


def book_realized_net(b: BookState) -> float | None:
    """size()'s day term (DAY_BUDGET): the book's realized net today. The open positions count there by their
    stop-outs (open_risk_rs), not by their MTM, but an unknown MTM still refuses the entry: None while the day net is
    unknown, as G12 and the daily stop wait for it."""
    return None if book_day_net(b) is None else b.realized_net


def live_account_realized_net(la: LiveAccountState | None) -> float | None:
    """The same over every LIVE book (size()'s LIVE account term); None while the account's day net is unknown."""
    if la is None or la.mtm_net is None or not (_finite(la.mtm_net) and _finite(la.realized_net)):
        return None
    return la.realized_net


def live_account_state(books: Iterable[BookState]) -> LiveAccountState:
    """The LIVE account guard from every LIVE book (the self-test book and RECOVERED:LIVE books included: a filled
    self-test and a recovered leg are real money)."""
    live = [b for b in books if book_account(b.book) is Account.LIVE]
    mtms = [b.mtm_net for b in live]
    risks = [b.open_risk_rs for b in live]
    return LiveAccountState(
        open=sum(b.open for b in live), reserved=sum(b.reserved for b in live),
        realized_net=round_money(math.fsum(b.realized_net for b in live)),
        mtm_net=None if not all(_finite(m) for m in mtms) else round_money(math.fsum(mtms)),
        active_underlyings=frozenset().union(*(b.active_underlyings for b in live)),
        exposures_rs=tuple(e for b in live for e in b.exposures_rs),
        open_risk_rs=None if not all(_finite(r) for r in risks) else round_money(math.fsum(risks)),
        pending=sum(b.pending for b in live))


def daily_stop_room(realized_net: float, mtm_net: float | None, stop_rs: float) -> float | None:
    """How far the book is from its daily stop (status.json); None while MTM is unknown."""
    if mtm_net is None:
        return None
    return round_money(stop_rs + realized_net + mtm_net)


def worst_case_room(realized_net: float, exposures_rs: Iterable[float], cap_rs: float) -> float:
    """How much more worst-case loss the book may take on (status.json)."""
    return round_money(cap_rs - worst_case_loss(realized_net, exposures_rs))


@dataclass(frozen=True, slots=True)
class StopTrip:
    """A new daily-stop trip: raise `halt`, send `alert`, force every open leg of `books` to EXITING(DAILY_STOP)."""

    scope: str                  # "BOOK:<book>" | "ACCOUNT:LIVE"
    books: tuple[str, ...]
    day_net: float
    limit_rs: float
    halt: HaltRequest
    alert: Alert


@dataclass(frozen=True, slots=True)
class DailyStopCheck:
    trips: tuple[StopTrip, ...] = ()
    force_exit: frozenset[str] = frozenset()   # every book under a DAILY_STOP halt, new or already active: its OPEN
    #                                            legs (e.g. an entry that filled after the trip) exit with DAILY_STOP
    unknown: tuple[str, ...] = ()              # scopes whose decision waits for a known MTM (exits are unaffected)


def _trip(scope: str, books: tuple[str, ...], day: float, limit: float, live: bool, session: date, *,
          mtm_known: bool = True) -> StopTrip:
    what = "realized + MTM" if mtm_known else "realized alone (MTM unknown)"
    text = f"daily stop {scope}: {what} {_rs(day)} <= -{_rs(limit)}; open legs exit (DAILY_STOP)"
    return StopTrip(scope, books, day, limit, make_halt("DAILY_STOP", scope, text),
                    Alert("CRITICAL" if live else "WARN", "DAILY_STOP", text, f"DAILY_STOP:{scope}:{session}"))


def check_daily_stops(books: Iterable[BookState], halts: Iterable[Halt], s: Settings, session: date) -> DailyStopCheck:
    """Run on every MTM update. Per FULL book: realized + MTM <= -DAILY_STOP_RS trips HALT(DAILY_STOP, BOOK,
    NEXT_SESSION). LIVE account: Σ over LIVE books <= -LIVE_ACCOUNT_DAILY_STOP_RS trips HALT(DAILY_STOP, ACCOUNT:LIVE)
    and flattens every LIVE book. CRITICAL for LIVE, WARN for paper.

    An unknown MTM decides nothing, except that a realized loss alone at or past the stop trips it: that loss is
    booked, and an MTM that cannot be priced (a leg with no bid, a stale quote) cannot undo it, so it is no reason to
    let the book's other legs run on to their own stops (review round 3; it waited for as long as any leg had no bid).
    """
    books = list(books)
    stopped = {h.scope for h in halts if h.kind == "DAILY_STOP"}
    trips: list[StopTrip] = []
    force: set[str] = set()
    unknown: list[str] = []
    for b in books:
        if book_policy(b.book) is not Policy.FULL:
            continue
        scope = f"BOOK:{b.book}"
        if scope in stopped:
            force.add(b.book)
            continue
        live_book = book_account(b.book) is Account.LIVE
        day = book_day_net(b)
        if day is None:
            if _finite(b.realized_net) and b.realized_net <= -s.DAILY_STOP_RS:
                trips.append(_trip(scope, (b.book,), b.realized_net, s.DAILY_STOP_RS, live_book, session,
                                   mtm_known=False))
                force.add(b.book)
            else:
                unknown.append(scope)
        elif day <= -s.DAILY_STOP_RS:
            trips.append(_trip(scope, (b.book,), day, s.DAILY_STOP_RS, live_book, session))
            force.add(b.book)
    live = tuple(sorted(b.book for b in books if book_account(b.book) is Account.LIVE))
    if live:
        scope = "ACCOUNT:LIVE"
        days = [book_day_net(b) for b in books if b.book in live]
        if scope in stopped:
            force.update(live)
        elif any(d is None for d in days):
            realized = math.fsum(b.realized_net for b in books if b.book in live)
            if _finite(realized) and realized <= -s.LIVE_ACCOUNT_DAILY_STOP_RS:
                trips.append(_trip(scope, live, realized, s.LIVE_ACCOUNT_DAILY_STOP_RS, True, session,
                                   mtm_known=False))
                force.update(live)
            else:
                unknown.append(scope)
        else:
            day = math.fsum(days)
            if day <= -s.LIVE_ACCOUNT_DAILY_STOP_RS:
                trips.append(_trip(scope, live, day, s.LIVE_ACCOUNT_DAILY_STOP_RS, True, session))
                force.update(live)
    return DailyStopCheck(tuple(trips), frozenset(force), tuple(unknown))
