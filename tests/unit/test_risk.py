"""U11: titan2.core.risk (gates G01-G22, divert, QUALITY skips, GATE_ERROR, liquidity, MTM, daily stop, worst case,
halts, cooldowns). Pure and offline: every input is built here."""

from __future__ import annotations

import dataclasses
import inspect
import math
from dataclasses import replace
from datetime import date, datetime, time as dtime, timedelta

import pytest

from titan2.charges import OrderFills, mtm_net, net_pnl, rates_for
from titan2.clock import SessionCalendar, ist_datetime
from titan2.core import risk
from titan2.core.events import MarginsResult
from titan2.core.recon import HaltRequest, ReconMemory
from titan2.core.risk import (
    CLEAR_ACTIONS, HALT_RULES, OPERATOR_HALTS, POST_QUOTE_GATES, PRE_RESERVE_GATES, BookState, Divert, GateContext,
    LiveAccountState, LivePreconditions, book_account, book_mtm, book_policy, books_for, can_clear,
    charges_hurdle_pct, check_daily_stops, daily_stop_room, entry_halts, final_gate, gate_live,
    gate_worst_case, leg_mtm, liquidity, live_account_state, loss_streak_halt, make_halt, note_gate_error,
    parse_book, post_quote_gates, pre_reserve_gates, rollover_clears, worst_case_loss, worst_case_room,
)
from titan2.instruments import InstrumentStore
from titan2.model import (
    EXPIRY_RULE_NEAREST, Account, Direction, FeedHealth, Halt, HaltClears, Instrument, Intent, Level, Margins, Mode,
    Policy, Quote, RECEIPT_SKEW_S, Recheck, Refusal, Side, Sized, StrikePref, StructureType, Underlying,
    UnderlyingTick, floor_tick,
)
from titan2.settings import DEFAULTS, GATE_ERROR_LIMIT, GATE_ERROR_WINDOW_S, WORST_CASE_PAD_RS

S = date(2026, 10, 12)                       # a Monday session
NEXT = date(2026, 10, 13)
NOW_MONO = 5000.0
STRAT = "opening_drive"
VER = "1.abcdef0123"
UND = "NSE:RELIANCE"
SYM = "NFO:RELIANCE26OCT2900CE"
PAPER, LIVE, SHADOW, PLACEBO = (f"{k}:{STRAT}" for k in ("PAPER", "LIVE", "SHADOW", "PLACEBO"))
RATES = rates_for(S)


def W(hms: str, d: date = S) -> datetime:
    return ist_datetime(d, hms)


NOW_WALL = W("10:00:00")
_HOURS = [{"from": "2026-01-01", "open": "09:15", "close": "15:40"}]
CAL = SessionCalendar.from_dict({"years": {"2026": {"holidays": ["2026-10-02", "2026-10-20"]}},
                                 "regular_hours": _HOURS})
# A trading special session with evening hours: the 09:20-14:55 entry window never falls inside it.
CAL_SPECIAL = SessionCalendar.from_dict({"years": {"2026": {"holidays": [], "special_sessions": [
    {"date": "2026-11-08", "open": "18:00", "close": "19:15", "trade": True}]}}, "regular_hours": _HOURS})


class FakeInstruments:
    """The two InstrumentStore methods the gates read (same rules as titan2.instruments)."""

    def __init__(self) -> None:
        self._u = {f"NSE:{n}": Underlying(f"NSE:{n}", i + 1, n, "STOCK") for i, n in enumerate(("RELIANCE", "INFY"))}
        self._u["NSE:NIFTY 50"] = Underlying("NSE:NIFTY 50", 256265, "NIFTY", "INDEX")

    def underlying(self, symbol_or_name: str) -> Underlying | None:
        return self._u.get(symbol_or_name) or next((u for u in self._u.values() if u.name == symbol_or_name), None)

    def ineligible_reason(self, underlying, ban_list, allow=(), deny=()):
        u = self.underlying(underlying)
        if u is None:
            return "NOT_IN_UNIVERSE"
        if u.kind == "INDEX":
            return None
        if ban_list is None:
            return "BAN_LIST_MISSING"
        if u.name in {b.upper().removeprefix("NSE:") for b in ban_list}:
            return "BANNED"
        if allow and u.name not in allow:
            return "NOT_ALLOWLISTED"
        if u.name in deny:
            return "DENYLISTED"
        return None


# ---------------------------------------------------------------------------------------------------------------
# Builders: the defaults form one context in which every gate passes
# ---------------------------------------------------------------------------------------------------------------
def book_state(book: str = PAPER, **kw) -> BookState:
    base = dict(book=book, policy=book_policy(book) or Policy.FULL, open=0, reserved=0, entries=0, attempts=0,
                orders_today=0, entry_orders_60s=0, realized_net=0.0, mtm_net=0.0, first_loss_today=False,
                cooldown_until={}, active_underlyings=frozenset(), exposures_rs=())
    base.update(kw)
    return BookState(**base)


def live_account(**kw) -> LiveAccountState:
    base = dict(open=0, reserved=0, realized_net=0.0, mtm_net=0.0, active_underlyings=frozenset(), exposures_rs=())
    base.update(kw)
    return LiveAccountState(**base)


def live_keys(**kw) -> LivePreconditions:
    base = dict(allow_live=True, unlocked=True, selftest_passed=True, demoted=False, first_week=False)
    base.update(kw)
    return LivePreconditions(**base)


def feed(**kw) -> FeedHealth:
    base = dict(connected=True, connected_since_mono=NOW_MONO - 120.0, last_underlying_tick_mono=NOW_MONO - 0.5,
                reconnects=0, degraded=False)
    base.update(kw)
    return FeedHealth(**base)


def clean(age: float = 5.0) -> ReconMemory:
    return ReconMemory(status="CLEAN", last_clean_mono=NOW_MONO - age, clean_streak=3)


def margins(acct: Account, available: float = 500_000.0, age: float = 5.0, ok: bool = True) -> MarginsResult:
    return MarginsResult(acct, ok, Margins(acct, available, {}) if ok else None, NOW_MONO - age)


_MODE = {"PAPER": Mode.PAPER, "LIVE": Mode.LIVE, "SHADOW": Mode.SHADOW, "PLACEBO": Mode.PAPER}


def ctx(book: str | BookState = PAPER, mode: Mode | None = None, **kw) -> GateContext:
    bs = book_state(book) if isinstance(book, str) else book
    base = dict(now_mono=NOW_MONO, now_wall=NOW_WALL, session=S, settings=DEFAULTS, calendar=CAL, clock_synced=True,
                instruments_loaded=True, rates=RATES, instruments=FakeInstruments(), ban_list=frozenset(),
                blocked_symbols=frozenset(), feed=feed(), signals_degraded=False,
                recon={a: clean() for a in Account}, margins={a: margins(a) for a in Account}, halts=(),
                strategy_modes={STRAT: mode or _MODE[bs.book.split(":")[0]]}, running_versions={STRAT: VER},
                disabled_strategies=frozenset(), structure_types=frozenset({StructureType.LONG_CE,
                                                                             StructureType.LONG_PE}),
                book=bs, live_account=live_account(), live=live_keys())
    base.update(kw)
    return GateContext(**base)


def intent(**kw) -> Intent:
    base = dict(intent_id="a9c31f04e2b7", strategy=STRAT, strategy_version=VER, underlying=UND,
                direction=Direction.UP, structure=StructureType.LONG_CE, strike_pref=StrikePref.ITM1,
                expiry_rule=EXPIRY_RULE_NEAREST, trigger_px=2900.0, recheck=Recheck(2885.5, 2929.0), ttl_s=10.0,
                created_mono=NOW_MONO - 1.0, created_wall=NOW_WALL - timedelta(seconds=1), reason={"ret_pct": 2.7})
    base.update(kw)
    return Intent(**base)


INST = Instrument(SYM, 12345, "RELIANCE26OCT2900CE", "NFO", "RELIANCE", UND, "CE", 2900.0, date(2026, 10, 27), 500,
                  0.05)
BIDS = ((31.95, 600), (31.90, 600), (31.85, 600))
ASKS = ((32.05, 600), (32.10, 600), (32.15, 600))


def quote(bids=BIDS, asks=ASKS, age: float = 0.3, last_trade_age: float | None = 5.0, symbol: str = SYM,
          ltp: float | None = 32.0) -> Quote:
    return Quote(symbol, 12345, tuple(Level(p, q) for p, q in bids), tuple(Level(p, q) for p, q in asks), ltp, 10_000,
                 None if last_trade_age is None else NOW_WALL - timedelta(seconds=last_trade_age), None,
                 NOW_MONO - age, NOW_WALL - timedelta(seconds=age), "REST")


def und_tick(ltp: float = 2905.0, age: float = 0.5, symbol: str = UND) -> UnderlyingTick:
    return UnderlyingTick(symbol, 738561, ltp, 1_000_000, 2890.0, 2850.0, None, NOW_MONO - age,
                          NOW_WALL - timedelta(seconds=age))


def plan_ok(inst: Instrument, q: Quote):
    """Stand-in for exits.entry_limit + sizing.size: min(ask + 2 ticks, floor_tick(mid x 1.01)), one lot."""
    limit = min(round(q.ask + 2 * inst.tick, 2), floor_tick(q.mid() * 1.01, inst.tick))
    return limit, Sized(inst.lot_size, 1, "LOTS", 2500.0)


def boom_plan(inst, q):
    raise RuntimeError("size() blew up")


def halt(kind: str, scope: str = "GLOBAL", *, operator: bool | None = None, session: date = S,
         hid: int = 1) -> Halt:
    rule = HALT_RULES.get(kind)
    return Halt(hid, scope, kind, "test", kind in OPERATOR_HALTS if operator is None else operator,
                rule.clears if rule else HaltClears.ACK, ist_datetime(session, "09:30"), session)


_DEFAULT = object()


def run(stage: str = "pre", *, book: str = PAPER, mode: Mode | None = None, c: dict | None = None,
        b: dict | None = None, i: dict | None = None, q=_DEFAULT, contract=_DEFAULT, und=_DEFAULT, plan=plan_ok,
        divert: Divert | None = None):
    cx = ctx(book_state(book, **(b or {})), mode, **(c or {}))
    it = intent(**(i or {}))
    if stage == "pre":
        return pre_reserve_gates(cx, it, divert)
    if stage == "final":
        return final_gate(cx, it)
    return post_quote_gates(cx, it, INST if contract is _DEFAULT else contract, quote() if q is _DEFAULT else q,
                            und_tick() if und is _DEFAULT else und, plan)


def gate_of(out) -> str | None:
    return None if out.result is None else out.result.gate


# ---------------------------------------------------------------------------------------------------------------
# Acceptance tests
# ---------------------------------------------------------------------------------------------------------------
# One breaker per gate, in spec order (LIVE book, so G13 applies): with breakers k.. active, gate k must refuse.
PRE_BREAKERS = [
    ("STRATEGY", "i", {"strategy_version": "1.0000000000"}),
    ("STRUCTURE", "c", {"structure_types": frozenset({StructureType.LONG_PE})}),
    ("TTL", "i", {"created_mono": NOW_MONO - 30.0}),
    ("HALTS", "c", {"halts": (halt("DAILY_STOP", f"BOOK:{LIVE}"),)}),
    ("SESSION", "c", {"clock_synced": False}),
    ("FEED", "c", {"feed": feed(connected=False)}),
    ("RECON", "c", {"recon": {**{a: clean() for a in Account}, Account.LIVE: clean(age=120.0)}}),
    ("ELIGIBILITY", "c", {"ban_list": frozenset({"RELIANCE"})}),
    ("BOOK_CAPS", "b", {"attempts": 6}),
    ("UNDERLYING", "b", {"active_underlyings": frozenset({UND})}),
    ("COOLDOWN", "b", {"cooldown_until": {UND: NOW_WALL + timedelta(minutes=5)}}),
    ("DAY_RISK", "b", {"mtm_net": None}),
    ("LIVE", "c", {"live": live_keys(selftest_passed=False)}),
]
POST_BREAKERS = [
    ("TTL", "i", {"created_mono": NOW_MONO - 30.0}),
    ("CONTRACT", "contract", Refusal("CONTRACT", "NO_EXPIRY: no RELIANCE expiry with >= 5 sessions left")),
    ("RECHECK", "und", und_tick(ltp=3000.0)),
    ("QUOTE", "q", {"age": 5.0}),
    ("SIZE", "plan", lambda inst, q: (None, None)),
    ("LIQUIDITY", "q", {"last_trade_age": None}),
    ("WORST_CASE", "b", {"realized_net": -39_000.0}),
    ("FUNDS", "c", {"margins": {**{a: margins(a) for a in Account}, Account.LIVE: margins(Account.LIVE, 1000.0)}}),
]


def _apply(breakers) -> dict:
    kw: dict = {"c": {}, "b": {}, "i": {}, "q": {}}
    for _, where, value in breakers:
        if where in kw:
            kw[where].update(value)
        else:
            kw[where] = value
    qkw = kw.pop("q")
    kw["q"] = quote(**qkw)
    return kw


def test_gate_order_first_refusal():
    assert [g for g, _, _ in PRE_BREAKERS] == list(PRE_RESERVE_GATES)
    assert [g for g, _, _ in POST_BREAKERS] == list(POST_QUOTE_GATES)
    assert [name for _, name in risk.GATES] == [*PRE_RESERVE_GATES, *POST_QUOTE_GATES, "FINAL"]
    for k, (gate, _, _) in enumerate(PRE_BREAKERS):
        kw = _apply(PRE_BREAKERS[k:])
        kw.pop("q")
        out = run("pre", book=LIVE, **kw)
        assert gate_of(out) == gate, (k, out)
    assert run("pre", book=LIVE).passed
    for k, (gate, _, _) in enumerate(POST_BREAKERS):
        out = run("post", book=LIVE, **_apply(POST_BREAKERS[k:]))
        assert gate_of(out) == gate, (k, out)
    out = run("post", book=LIVE)
    assert out.passed and out.limit_px == 32.15 and out.sized == Sized(500, 1, "LOTS", 2500.0)
    # a refused run never hands out a plan
    out = run("post", book=LIVE, b={"realized_net": -39_000.0})
    assert gate_of(out) == "WORST_CASE" and out.limit_px is None and out.sized is None


FAIL_CLOSED = [
    # G01 STRATEGY
    ("empty-name", "STRATEGY", dict(i={"strategy": ""})),
    ("name-not-exact", "STRATEGY", dict(i={"strategy": "Opening_drive"})),
    ("version-mismatch", "STRATEGY", dict(i={"strategy_version": "1.0000000000"})),
    ("disabled", "STRATEGY", dict(c={"disabled_strategies": frozenset({STRAT})})),
    ("mode-off", "STRATEGY", dict(mode=Mode.OFF)),
    ("mode-missing", "STRATEGY", dict(c={"strategy_modes": {}})),
    ("mode-routes-elsewhere", "STRATEGY", dict(mode=Mode.SHADOW)),
    ("policy-mismatch", "STRATEGY", dict(b={"policy": Policy.QUALITY})),
    ("another-strategys-book", "STRATEGY", dict(book="PAPER:price_spike_up")),
    ("selftest-book", "STRATEGY", dict(book="LIVE:_selftest")),
    ("is-placebo-not-bool", "STRATEGY", dict(i={"is_placebo": 1})),
    # G02 STRUCTURE
    ("unknown-structure", "STRUCTURE", dict(i={"structure": "IRON_CONDOR"})),
    ("no-handler", "STRUCTURE", dict(c={"structure_types": frozenset()})),
    # G03 TTL
    ("expired", "TTL", dict(i={"created_mono": NOW_MONO - 10.01})),
    ("ttl-nan", "TTL", dict(i={"ttl_s": math.nan})),
    ("ttl-zero", "TTL", dict(i={"ttl_s": 0.0})),
    ("ttl-over-10", "TTL", dict(i={"ttl_s": 11.0})),
    ("created-nan", "TTL", dict(i={"created_mono": math.nan})),
    ("created-in-future", "TTL", dict(i={"created_mono": NOW_MONO + 5.0})),
    # G04 HALTS
    ("global-halt", "HALTS", dict(c={"halts": (halt("EXCEPTION"),)})),
    ("account-halt", "HALTS", dict(c={"halts": (halt("STALE_RECON", "ACCOUNT:PAPER"),)})),
    ("unparseable-scope", "HALTS", dict(c={"halts": (halt("MISMATCH", "ACCOUNT"),)})),
    ("unknown-kind", "HALTS", dict(c={"halts": (halt("SOMETHING_NEW", "GLOBAL"),)})),
    # G05 SESSION
    ("clock-unsynced", "SESSION", dict(c={"clock_synced": False})),
    ("clock-synced-not-bool", "SESSION", dict(c={"clock_synced": 1})),
    ("holiday", "SESSION", dict(c={"session": date(2026, 10, 20), "now_wall": W("10:00", date(2026, 10, 20))})),
    ("weekend", "SESSION", dict(c={"session": date(2026, 10, 10), "now_wall": W("10:00", date(2026, 10, 10))})),
    ("before-entry-start", "SESSION", dict(c={"now_wall": W("09:19:59")})),
    ("at-no-new-after", "SESSION", dict(c={"now_wall": W("14:55:00")})),
    ("wall-not-session", "SESSION", dict(c={"now_wall": W("10:00", NEXT)})),
    ("no-calendar-coverage", "SESSION", dict(c={"calendar": SessionCalendar.empty()})),
    ("odd-session-hours", "SESSION", dict(c={"calendar": CAL_SPECIAL, "session": date(2026, 11, 8),
                                             "now_wall": W("10:00", date(2026, 11, 8))})),
    ("instruments-not-loaded", "SESSION", dict(c={"instruments_loaded": False})),
    # G06 FEED
    ("disconnected", "FEED", dict(c={"feed": feed(connected=False)})),
    ("feed-none", "FEED", dict(c={"feed": None})),
    ("no-tick", "FEED", dict(c={"feed": feed(last_underlying_tick_mono=None)})),
    ("tick-stale", "FEED", dict(c={"feed": feed(last_underlying_tick_mono=NOW_MONO - 5.01)})),
    # further ahead than RECEIPT_SKEW_S is a clock-domain bug (a tick received while the event ran is fresh,
    #   test_underlying_tick_received_during_the_event_is_fresh)
    ("tick-in-future", "FEED", dict(c={"feed": feed(last_underlying_tick_mono=NOW_MONO + RECEIPT_SKEW_S + 0.5)})),
    ("tick-nan", "FEED", dict(c={"feed": feed(last_underlying_tick_mono=math.nan)})),
    ("warming", "FEED", dict(c={"feed": feed(connected_since_mono=NOW_MONO - 59.9)})),
    ("connected-since-unknown", "FEED", dict(c={"feed": feed(connected_since_mono=None)})),
    ("feed-degraded", "FEED", dict(c={"feed": feed(degraded=True)})),
    ("signals-degraded", "FEED", dict(c={"signals_degraded": True})),
    # G07 RECON
    ("no-recon-for-account", "RECON", dict(c={"recon": {Account.LIVE: clean()}})),
    ("recon-too-old", "RECON", dict(c={"recon": {Account.PAPER: clean(age=30.5)}})),
    ("never-clean", "RECON", dict(c={"recon": {Account.PAPER: ReconMemory()}})),
    ("recon-in-future", "RECON", dict(c={"recon": {Account.PAPER: clean(age=-1.0)}})),
    # G08 ELIGIBILITY
    ("ban-list-missing", "ELIGIBILITY", dict(c={"ban_list": None})),
    ("banned", "ELIGIBILITY", dict(c={"ban_list": frozenset({"RELIANCE"})})),
    ("not-in-universe", "ELIGIBILITY", dict(i={"underlying": "NSE:NOSUCH"})),
    ("deny-listed", "ELIGIBILITY", dict(c={"settings": replace(DEFAULTS, STOCK_DENYLIST=("RELIANCE",))})),
    ("not-allow-listed", "ELIGIBILITY", dict(c={"settings": replace(DEFAULTS, STOCK_ALLOWLIST=("INFY",))})),
    ("no-instruments", "ELIGIBILITY", dict(c={"instruments": None})),
    # G09 BOOK_CAPS
    ("positions", "BOOK_CAPS", dict(b={"open": 1, "reserved": 1})),
    ("entries", "BOOK_CAPS", dict(b={"entries": 3})),
    # structures still reserved or entering count toward the caps: a burst of intents cannot open a fourth (I11)
    ("entries-pending", "BOOK_CAPS", dict(b={"entries": 2, "pending": 1})),
    ("attempts-reserved", "BOOK_CAPS", dict(b={"attempts": 5, "reserved": 1, "pending": 1})),
    ("negative-pending", "BOOK_CAPS", dict(b={"pending": -1})),
    ("attempts", "BOOK_CAPS", dict(b={"attempts": 6})),
    ("orders", "BOOK_CAPS", dict(b={"orders_today": 40})),
    ("runaway", "BOOK_CAPS", dict(b={"entry_orders_60s": 3})),
    ("zero-cap", "BOOK_CAPS", dict(c={"settings": replace(DEFAULTS, MAX_POSITIONS_PER_BOOK=0)})),
    ("negative-counter", "BOOK_CAPS", dict(b={"attempts": -1})),
    ("live-first-week", "BOOK_CAPS", dict(book=LIVE, b={"entries": 1}, c={"live": live_keys(first_week=True)})),
    ("live-account-positions", "BOOK_CAPS", dict(book=LIVE, c={"live_account": live_account(open=1, reserved=1)})),
    ("live-account-missing", "BOOK_CAPS", dict(book=LIVE, c={"live_account": None})),
    ("live-keys-missing", "BOOK_CAPS", dict(book=LIVE, c={"live": None})),     # caught before G13
    # G10 UNDERLYING
    ("book-underlying-active", "UNDERLYING", dict(b={"active_underlyings": frozenset({UND})})),
    ("live-underlying-active", "UNDERLYING",
     dict(book=LIVE, c={"live_account": live_account(active_underlyings=frozenset({UND}))})),
    # G11 COOLDOWN
    ("cooldown", "COOLDOWN", dict(b={"cooldown_until": {UND: NOW_WALL + timedelta(seconds=1)}})),
    ("cooldown-naive", "COOLDOWN", dict(b={"cooldown_until": {UND: datetime(2026, 10, 12, 9, 0)}})),
    # G12 DAY_RISK
    ("mtm-unknown", "DAY_RISK", dict(b={"mtm_net": None})),
    ("mtm-nan", "DAY_RISK", dict(b={"mtm_net": math.nan})),
    ("at-daily-stop", "DAY_RISK", dict(b={"realized_net": -15_000.0})),
    ("first-loss", "DAY_RISK",
     dict(b={"first_loss_today": True}, c={"settings": replace(DEFAULTS, STOP_AFTER_FIRST_LOSS=True)})),
    ("live-account-mtm-unknown", "DAY_RISK", dict(book=LIVE, c={"live_account": live_account(mtm_net=None)})),
    ("live-account-daily-stop", "DAY_RISK",
     dict(book=LIVE, c={"live_account": live_account(realized_net=-10_000.0)})),
    # G13 LIVE
    ("allow-live-off", "LIVE", dict(book=LIVE, c={"live": live_keys(allow_live=False)})),
    ("allow-live-truthy-text", "LIVE", dict(book=LIVE, c={"live": live_keys(allow_live="1")})),
    ("not-unlocked", "LIVE", dict(book=LIVE, c={"live": live_keys(unlocked=False)})),
    ("no-selftest", "LIVE", dict(book=LIVE, c={"live": live_keys(selftest_passed=False)})),
    ("demoted", "LIVE", dict(book=LIVE, c={"live": live_keys(demoted=True)})),
    # G14 TTL
    ("post-expired", "TTL", dict(stage="post", i={"created_mono": NOW_MONO - 10.5})),
    # G15 CONTRACT
    ("selection-refused", "CONTRACT", dict(stage="post", contract=Refusal("CONTRACT", "NO_ITM1_NEIGHBOUR"))),
    ("no-contract", "CONTRACT", dict(stage="post", contract=None)),
    ("lot-zero", "CONTRACT", dict(stage="post", contract=replace(INST, lot_size=0))),
    ("lot-float", "CONTRACT", dict(stage="post", contract=replace(INST, lot_size=500.0))),
    ("tick-zero", "CONTRACT", dict(stage="post", contract=replace(INST, tick=0.0))),
    ("strike-nan", "CONTRACT", dict(stage="post", contract=replace(INST, strike=math.nan))),
    ("wrong-underlying", "CONTRACT", dict(stage="post", contract=replace(INST, underlying="NSE:INFY"))),
    ("wrong-opt-type", "CONTRACT", dict(stage="post", contract=replace(INST, opt_type="PE"))),
    ("dte-too-short", "CONTRACT", dict(stage="post", contract=replace(INST, expiry=date(2026, 10, 14)))),
    ("expired-contract", "CONTRACT", dict(stage="post", contract=replace(INST, expiry=date(2026, 10, 9)))),
    ("symbol-halt", "CONTRACT", dict(stage="post", c={"halts": (halt("CONTRACT_BLOCKED", f"SYMBOL:{SYM}"),)})),
    ("blocked-symbols", "CONTRACT", dict(stage="post", c={"blocked_symbols": frozenset({SYM})})),
    ("contract-no-instruments", "CONTRACT", dict(stage="post", c={"instruments": None})),
    # G16 RECHECK
    ("no-underlying-ltp", "RECHECK", dict(stage="post", und=None)),
    ("underlying-wrong-symbol", "RECHECK", dict(stage="post", und=und_tick(symbol="NSE:INFY"))),
    ("underlying-stale", "RECHECK", dict(stage="post", und=und_tick(age=2.01))),
    ("underlying-in-future", "RECHECK", dict(stage="post", und=und_tick(age=-(RECEIPT_SKEW_S + 0.5)))),
    ("underlying-ltp-nan", "RECHECK", dict(stage="post", und=und_tick(ltp=math.nan))),
    ("outside-recheck", "RECHECK", dict(stage="post", und=und_tick(ltp=2929.05))),
    ("recheck-nan", "RECHECK", dict(stage="post", i={"recheck": Recheck(math.nan, math.nan)})),
    # G17 QUOTE
    ("quote-missing", "QUOTE", dict(stage="post", q=None)),
    ("quote-wrong-symbol", "QUOTE", dict(stage="post", q=quote(symbol="NFO:RELIANCE26OCT2900PE"))),
    ("quote-stale", "QUOTE", dict(stage="post", q=quote(age=1.51))),
    # a receipt further ahead than RECEIPT_SKEW_S is a clock-domain bug (0.2 s ahead is a tick received while the
    # event ran: age 0, test_quote_received_during_the_event_is_fresh)
    ("quote-in-future", "QUOTE", dict(stage="post", q=quote(age=-(RECEIPT_SKEW_S + 0.5)))),
    ("no-bids", "QUOTE", dict(stage="post", q=quote(bids=()))),
    ("no-asks", "QUOTE", dict(stage="post", q=quote(asks=()))),
    ("locked", "QUOTE", dict(stage="post", q=quote(bids=((32.0, 600),), asks=((32.0, 600),)))),
    ("crossed", "QUOTE", dict(stage="post", q=quote(bids=((32.1, 600),), asks=((32.0, 600),)))),
    # G18 SIZE
    ("no-entry-limit", "SIZE", dict(stage="post", plan=lambda inst, q: (None, None))),
    ("limit-off-tick", "SIZE", dict(stage="post", plan=lambda inst, q: (32.17, Sized(500, 1, "LOTS", 2500.0)))),
    ("size-refused", "SIZE", dict(stage="post", plan=lambda inst, q: (32.15, Refusal("SIZE", "PREMIUM_FLOOR")))),
    ("not-sized", "SIZE", dict(stage="post", plan=lambda inst, q: (32.15, None))),
    ("qty-not-whole-lots", "SIZE", dict(stage="post", plan=lambda inst, q: (32.15, Sized(600, 1, "LOTS", 2500.0)))),
    ("plan-raises", "SIZE", dict(stage="post", plan=boom_plan)),
    # G19 LIQUIDITY
    ("last-trade-unknown", "LIQUIDITY", dict(stage="post", q=quote(last_trade_age=None))),
    ("wide-spread", "LIQUIDITY", dict(stage="post", q=quote(bids=((30.0, 600), (29.95, 600), (29.9, 600))))),
    # G20 WORST_CASE
    ("worst-case", "WORST_CASE", dict(stage="post", b={"realized_net": -33_900.0})),
    ("bad-exposure", "WORST_CASE", dict(stage="post", b={"exposures_rs": (math.nan,)})),
    # G21 FUNDS
    ("margins-missing", "FUNDS", dict(stage="post", c={"margins": {}})),
    ("margins-failed", "FUNDS", dict(stage="post", c={"margins": {Account.PAPER: margins(Account.PAPER, ok=False)}})),
    ("margins-stale", "FUNDS", dict(stage="post", c={"margins": {Account.PAPER: margins(Account.PAPER, age=60.5)}})),
    ("margins-in-future", "FUNDS",
     dict(stage="post", c={"margins": {Account.PAPER: margins(Account.PAPER, age=-1.0)}})),
    ("margins-nan", "FUNDS", dict(stage="post", c={"margins": {Account.PAPER: margins(Account.PAPER, math.nan)}})),
    ("insufficient", "FUNDS", dict(stage="post", c={"margins": {Account.PAPER: margins(Account.PAPER, 17_000.0)}})),
    # G22 FINAL
    ("final-expired", "FINAL", dict(stage="final", i={"created_mono": NOW_MONO - 11.0})),
    ("final-halt", "FINAL", dict(stage="final", c={"halts": (halt("TOKEN"),)})),
]


@pytest.mark.parametrize("case,gate,kw", FAIL_CLOSED, ids=[c[0] for c in FAIL_CLOSED])
def test_each_gate_fails_closed(case, gate, kw):
    kw = dict(kw)
    out = run(kw.pop("stage", "pre"), **kw)
    assert isinstance(out.result, Refusal), (case, out)
    assert out.result.gate == gate, (case, out.result)
    assert isinstance(out.result.detail, str) and out.result.detail


# Every gate, pre-reserve, post-quote and final, turns an exception into Refusal(<gate>, "GATE_ERROR").
GATE_FUNCTIONS = [
    ("pre", "STRATEGY", "gate_strategy"), ("pre", "STRUCTURE", "gate_structure"), ("pre", "TTL", "gate_ttl"),
    ("pre", "HALTS", "gate_halts"), ("pre", "SESSION", "gate_session"), ("pre", "FEED", "gate_feed"),
    ("pre", "RECON", "gate_recon"), ("pre", "ELIGIBILITY", "gate_eligibility"),
    ("pre", "BOOK_CAPS", "gate_book_caps"), ("pre", "UNDERLYING", "gate_underlying"),
    ("pre", "COOLDOWN", "gate_cooldown"), ("pre", "DAY_RISK", "gate_day_risk"), ("pre", "LIVE", "gate_live"),
    ("post", "TTL", "gate_ttl"), ("post", "CONTRACT", "gate_contract"), ("post", "RECHECK", "gate_recheck"),
    ("post", "QUOTE", "gate_quote"), ("post", "SIZE", "gate_size"), ("post", "LIQUIDITY", "gate_liquidity"),
    ("post", "WORST_CASE", "gate_worst_case"), ("post", "FUNDS", "gate_funds"),
    ("final", "FINAL", "gate_ttl"), ("final", "FINAL", "gate_halts"),
]


@pytest.mark.parametrize("stage,gate,fn", GATE_FUNCTIONS, ids=[f"{s}-{g}-{f}" for s, g, f in GATE_FUNCTIONS])
def test_gate_exception_is_gate_error(monkeypatch, stage, gate, fn):
    def boom(*a, **k):
        raise RuntimeError("bug in gate")

    monkeypatch.setattr(risk, fn, boom)
    out = run(stage, book=LIVE)          # LIVE: G13 really runs
    assert out.result == Refusal(gate, risk.GATE_ERROR)
    assert out.gate_error and "RuntimeError: bug in gate" in out.error
    assert out.limit_px is None and out.sized is None


def test_gate_returning_garbage_is_gate_error(monkeypatch):
    monkeypatch.setattr(risk, "gate_feed", lambda ctx: "looks fine")
    out = run("pre")
    assert out.result == Refusal("FEED", risk.GATE_ERROR) and "returned str" in out.error
    # only G04 may divert: a Divert from any other gate is a bug
    monkeypatch.setattr(risk, "gate_feed", lambda ctx: Divert("HALTS", SHADOW, "PAUSE"))
    assert run("pre").result == Refusal("FEED", risk.GATE_ERROR)


def test_operator_divert_paper_only():
    pause = halt("PAUSE")
    out = run("pre", c={"halts": (pause,)})
    assert out.divert == Divert("HALTS", SHADOW, "PAUSE") and out.refusal is None and not out.passed
    # the caller re-runs the gates for SHADOW:<s> with that Divert: PAUSE does not block the QUALITY book
    assert run("pre", book=SHADOW, mode=Mode.PAPER, c={"halts": (pause,)}, divert=out.divert).passed
    # without the Divert a PAPER-mode intent never routes to SHADOW, and a mismatched Divert is not accepted
    assert gate_of(run("pre", book=SHADOW, mode=Mode.PAPER)) == "STRATEGY"
    assert gate_of(run("pre", book=SHADOW, mode=Mode.PAPER,
                       divert=Divert("HALTS", "SHADOW:other", "PAUSE"))) == "STRATEGY"
    # a placebo is never diverted (its book is QUALITY)
    assert gate_of(run("pre", book=SHADOW, mode=Mode.PAPER, i={"is_placebo": True}, divert=out.divert)) == "STRATEGY"
    # LIVE books are refused, never diverted; the PAPER twin of a LIVE strategy diverts
    live = run("pre", book=LIVE, c={"halts": (pause,)})
    assert live.refusal == Refusal("HALTS", "PAUSE@GLOBAL")
    assert run("pre", book=PAPER, mode=Mode.LIVE, c={"halts": (pause,)}).divert == Divert("HALTS", SHADOW, "PAUSE")
    assert run("pre", book=SHADOW, mode=Mode.LIVE, c={"halts": (pause,)},
               divert=Divert("HALTS", SHADOW, "PAUSE")).passed
    # any non-operator halt among the blocking ones: refused
    mixed = run("pre", c={"halts": (pause, halt("DAILY_STOP", f"BOOK:{PAPER}", hid=2))})
    assert mixed.refusal is not None and mixed.refusal.gate == "HALTS"
    assert "PAUSE@GLOBAL" in mixed.refusal.detail and f"DAILY_STOP@BOOK:{PAPER}" in mixed.refusal.detail
    # every operator kind diverts PAPER; FLATTEN, KILL and SAFE_FLAG then stop the SHADOW route as well
    for kind in ("FLATTEN", "KILL", "SAFE_FLAG"):
        d = run("pre", c={"halts": (halt(kind),)}).divert
        assert d == Divert("HALTS", SHADOW, kind)
        assert run("pre", book=SHADOW, mode=Mode.PAPER, c={"halts": (halt(kind),)}, divert=d).refusal.gate == "HALTS"
    both = run("pre", c={"halts": (pause, halt("KILL", hid=2))})
    assert both.divert.blocked_by == "KILL,PAUSE" and both.divert.detail == f"DIVERT {SHADOW} (KILL,PAUSE)"
    # all three marks must agree for an operator halt: the row flag, an operator kind, GLOBAL scope
    for fake in (halt("MISMATCH", "ACCOUNT:PAPER", operator=True), halt("PAUSE", operator=False),
                 halt("PAUSE", "ACCOUNT:PAPER")):
        assert run("pre", c={"halts": (fake,)}).refusal.gate == "HALTS", fake
    # G22: an operator halt raised after the reservation refuses the PAPER structure (it can no longer divert)
    assert run("final", c={"halts": (pause,)}).result == Refusal("FINAL", "OPERATOR_HALT: PAUSE")
    assert run("final", book=LIVE, c={"halts": (pause,)}).result == Refusal("FINAL", "HALTS: PAUSE@GLOBAL")


def test_quality_skips():
    assert risk.QUALITY_SKIPS == {"BOOK_CAPS", "COOLDOWN", "DAY_RISK", "WORST_CASE", "FUNDS"}
    busy = dict(open=1, reserved=1, entries=3, attempts=6, orders_today=40, entry_orders_60s=3,
                realized_net=-50_000.0, mtm_net=None, first_loss_today=True,
                cooldown_until={UND: NOW_WALL + timedelta(minutes=30)}, exposures_rs=(25_000.0,))
    s = replace(DEFAULTS, STOP_AFTER_FIRST_LOSS=True)
    for book, mode, placebo in ((SHADOW, Mode.SHADOW, False), (PLACEBO, Mode.PAPER, True)):
        c = {"settings": s, "margins": {}}
        assert run("pre", book=book, mode=mode, b=busy, c=c, i={"is_placebo": placebo}).passed, book
        out = run("post", book=book, mode=mode, b=busy, c=c, i={"is_placebo": placebo})
        assert out.passed and out.sized.qty == 500, book
    # the same state on a FULL book: refused at the first of those gates
    assert gate_of(run("pre", book=PAPER, b=busy, c={"settings": s})) == "BOOK_CAPS"
    assert gate_of(run("post", book=PAPER, b=busy, c={"settings": s, "margins": {}})) == "WORST_CASE"
    assert gate_of(run("post", book=PAPER, c={"margins": {}})) == "FUNDS"
    # QUALITY keeps every other gate: one structure per underlying, eligibility, feed, liquidity, size
    assert gate_of(run("pre", book=SHADOW, b={"active_underlyings": frozenset({UND})})) == "UNDERLYING"
    assert gate_of(run("pre", book=SHADOW, c={"ban_list": None})) == "ELIGIBILITY"
    assert gate_of(run("pre", book=SHADOW, c={"feed": feed(degraded=True)})) == "FEED"
    assert gate_of(run("post", book=SHADOW, q=quote(bids=((30.0, 600), (29.95, 600), (29.9, 600))))) == "LIQUIDITY"
    assert gate_of(run("post", book=SHADOW, plan=lambda i, q: (32.15, Refusal("SIZE", "DEPTH")))) == "SIZE"
    # G04 on QUALITY books: GLOBAL and ACCOUNT halts only (and not PAUSE)
    for h in (halt("DAILY_STOP", f"BOOK:{SHADOW}"), halt("RUNAWAY", f"BOOK:{SHADOW}"),
              halt("DAILY_STOP", f"BOOK:{PAPER}"), Halt(7, f"STRATEGY:{STRAT}", "X", "t", False, HaltClears.ACK,
                                                       NOW_WALL, S),
              Halt(8, f"UNDERLYING:{UND}", "X", "t", False, HaltClears.ACK, NOW_WALL, S), halt("PAUSE"),
              halt("MISMATCH", "ACCOUNT:PAPER")):
        assert run("pre", book=SHADOW, c={"halts": (h,)}).passed, h
    for h in (halt("EXCEPTION"), halt("KILL"), halt("MISMATCH", "ACCOUNT:SHADOW"), halt("X", "ODD_SCOPE")):
        assert gate_of(run("pre", book=SHADOW, c={"halts": (h,)})) == "HALTS", h


def test_quote_received_during_the_event_is_fresh():
    """G17 reads Quote.age: a snapshot kite-ws stamped 0.2 s after the event's now_mono (it landed while the core
    handled the event) is the newest book there is, age 0, not a stale or future quote (review round 1)."""
    out = run("post", book=LIVE, q=quote(age=-0.2))
    assert out.passed and out.limit_px == 32.15


def test_underlying_tick_received_during_the_event_is_fresh():
    """G06 and G16 read receipt times the WS thread keeps writing while the event runs: the feed's last underlying
    tick (any of ~200 underlyings) and the underlying's tick in the quote cache. One stamped after the event's
    now_mono is fresh (age 0), as Quote.age has it; they refused it as FEED STALE_TICK and RECHECK 'STALE: underlying
    LTP -0.00 s old' (review round 3, ported from its unit probes). Beyond RECEIPT_SKEW_S both still refuse."""
    assert risk.gate_feed(ctx(feed=feed(last_underlying_tick_mono=NOW_MONO + 0.002))) is None
    assert risk.gate_feed(ctx(feed=feed(last_underlying_tick_mono=NOW_MONO + RECEIPT_SKEW_S))) is None
    assert run("pre", c={"feed": feed(last_underlying_tick_mono=NOW_MONO + 0.002)}).passed
    u = und_tick(age=-0.005)
    assert risk.gate_recheck(ctx(), intent(), u) is None and run("post", und=u).passed
    cached = Quote(UND, 738561, (), (), 2905.0, None, None, None, NOW_MONO + 0.005, NOW_WALL, "WS")
    assert risk.gate_recheck(ctx(), intent(), cached) is None                 # a Quote from the cache, too
    assert risk.gate_feed(ctx(feed=feed(last_underlying_tick_mono=NOW_MONO + RECEIPT_SKEW_S + 0.01))) == \
        Refusal("FEED", "STALE_TICK")
    far = risk.gate_recheck(ctx(), intent(), und_tick(age=-(RECEIPT_SKEW_S + 0.01)))
    assert far is not None and far.gate == "RECHECK" and far.detail.startswith("STALE")


def test_quality_skipped_gates_are_not_evaluated(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a skipped gate was called")

    for fn in ("gate_book_caps", "gate_cooldown", "gate_day_risk", "gate_worst_case", "gate_funds"):
        monkeypatch.setattr(risk, fn, boom)
    assert run("pre", book=SHADOW).passed
    assert run("post", book=SHADOW).passed
    assert run("pre", book=PAPER).result == Refusal("BOOK_CAPS", risk.GATE_ERROR)


def test_daily_stop_mtm_unknown():
    entry = OrderFills(Side.BUY, 500, 16_000.0)
    assert leg_mtm([entry], 500, 31.0, 0.5, RATES, 3.0) == mtm_net([entry], 500, 31.0, RATES)
    assert leg_mtm([entry], 500, 31.0, 3.0, RATES, 3.0) is not None          # exactly 3 s old is still fresh
    for rb, age in ((31.0, 3.01), (None, 0.1), (31.0, None), (31.0, -0.5), (0.0, 0.1), (math.nan, 0.1),
                    (31.0, math.inf), (31.0, math.nan)):
        assert leg_mtm([entry], 500, rb, age, RATES, 3.0) is None, (rb, age)
    sold = OrderFills(Side.SELL, 500, 15_000.0)
    assert leg_mtm([entry, sold], 0, None, None, RATES, 3.0) == net_pnl([entry, sold], RATES).net   # flat: no quote
    assert book_mtm([]) == 0.0 and book_mtm([120.5, None]) is None and book_mtm([100.0, -50.25]) == 49.75
    assert book_mtm([1.0, math.nan]) is None
    # unknown MTM blocks entries (G12) ...
    assert run("pre", b={"mtm_net": None}).result == Refusal("DAY_RISK", "MTM_UNKNOWN")
    # ... and blocks the daily-stop decision while the realized loss alone is inside the stop: exits are not touched
    chk = check_daily_stops([book_state(PAPER, realized_net=-14_999.99, mtm_net=None)], (), DEFAULTS, S)
    assert chk.trips == () and chk.force_exit == frozenset() and chk.unknown == (f"BOOK:{PAPER}",)
    # a realized loss alone past the stop trips it whatever the unknown MTM (review round 3: it decided nothing,
    #   test_a_realized_loss_past_the_stop_trips_it_with_an_unknown_mtm)
    chk = check_daily_stops([book_state(PAPER, realized_net=-25_000.0, mtm_net=None)], (), DEFAULTS, S)
    assert [t.scope for t in chk.trips] == [f"BOOK:{PAPER}"] and chk.force_exit == {PAPER} and chk.unknown == ()
    # realized + MTM <= -DAILY_STOP_RS trips HALT(DAILY_STOP, BOOK, NEXT_SESSION); WARN on paper; legs forced out
    tripping = book_state(PAPER, realized_net=-13_000.0, mtm_net=-2_000.0)
    chk = check_daily_stops([tripping], (), DEFAULTS, S)
    (trip,) = chk.trips
    assert trip.scope == f"BOOK:{PAPER}" and trip.books == (PAPER,) and trip.day_net == -15_000.0
    assert (trip.halt.kind, trip.halt.scope, trip.halt.clears) == ("DAILY_STOP", f"BOOK:{PAPER}",
                                                                   HaltClears.NEXT_SESSION)
    assert trip.alert.severity == "WARN" and trip.alert.code == "DAILY_STOP" and chk.force_exit == {PAPER}
    assert check_daily_stops([book_state(PAPER, realized_net=-13_000.0, mtm_net=-1_999.99)], (), DEFAULTS, S).trips == ()
    # LIVE: CRITICAL; a lone LIVE book past its stop is also past the LIVE account's stop
    live_trips = check_daily_stops([book_state(LIVE, realized_net=-15_500.0)], (), DEFAULTS, S).trips
    assert [t.scope for t in live_trips] == [f"BOOK:{LIVE}", "ACCOUNT:LIVE"]
    assert all(t.alert.severity == "CRITICAL" for t in live_trips)
    # the halt is sticky: no second trip, its book stays forced (an entry that filled after the trip exits too), and
    # MTM recovery changes nothing
    stopped = (halt("DAILY_STOP", f"BOOK:{PAPER}"),)
    for b in (tripping, book_state(PAPER, realized_net=-8_000.0, mtm_net=+5_000.0), book_state(PAPER, mtm_net=None)):
        chk = check_daily_stops([b], stopped, DEFAULTS, S)
        assert chk.trips == () and chk.force_exit == {PAPER}
    # QUALITY books have no daily stop
    assert check_daily_stops([book_state(SHADOW, realized_net=-90_000.0)], (), DEFAULTS, S) == risk.DailyStopCheck()
    # LIVE account: each book inside its stop, the sum beyond LIVE_ACCOUNT_DAILY_STOP_RS -> every LIVE book exits
    l1 = book_state(LIVE, realized_net=-6_000.0, mtm_net=0.0)
    l2 = book_state("LIVE:price_spike_up", realized_net=-3_000.0, mtm_net=-1_500.0)
    chk = check_daily_stops([l1, l2, book_state(PAPER)], (), DEFAULTS, S)
    (acct,) = chk.trips
    assert acct.scope == "ACCOUNT:LIVE" and acct.alert.severity == "CRITICAL" and acct.day_net == -10_500.0
    assert acct.halt == make_halt("DAILY_STOP", "ACCOUNT:LIVE", acct.halt.reason)
    assert chk.force_exit == {LIVE, "LIVE:price_spike_up"}
    unknown = check_daily_stops([l1, replace(l2, mtm_net=None)], (), DEFAULTS, S)
    assert unknown.trips == () and "ACCOUNT:LIVE" in unknown.unknown
    held = check_daily_stops([book_state(LIVE), book_state("LIVE:price_spike_up")], (halt("DAILY_STOP", "ACCOUNT:LIVE"),),
                             DEFAULTS, S)
    assert held.trips == () and held.force_exit == {LIVE, "LIVE:price_spike_up"}
    # the account guard G12 reads the same sums
    la = live_account_state([l1, l2, book_state(PAPER, realized_net=-99_000.0)])
    assert (la.realized_net, la.mtm_net) == (-9_000.0, -1_500.0)
    assert gate_of(run("pre", book=LIVE, c={"live_account": la})) == "DAY_RISK"
    assert live_account_state([l1, replace(l2, mtm_net=None)]).mtm_net is None
    assert daily_stop_room(0.0, -120.5, 10_000.0) == 9_879.5 and daily_stop_room(0.0, None, 10_000.0) is None


def test_a_realized_loss_past_the_stop_trips_it_with_an_unknown_mtm():
    """Review round 3 (round 1's exits_risk probe f6, its -12,000 premise moved past today's Rs 15,000 stop): the
    daily stop decided nothing while any leg's MTM was unknown (a leg with no bids, a stale quote), even with the
    realized loss alone past the stop, so the book's other legs ran on to their own stops. A realized loss is booked;
    an unknown MTM cannot undo it: the stop trips and the book's legs exit. Inside the stop it still waits."""
    stop = DEFAULTS.DAILY_STOP_RS
    for realized, mtm, trips in ((-16_000.0, None, True), (-stop, None, True), (-14_000.0, None, False),
                                 (-16_000.0, 500.0, True), (-14_000.0, -1_500.0, True), (-14_000.0, -500.0, False)):
        chk = check_daily_stops([book_state(PAPER, realized_net=realized, mtm_net=mtm)], (), DEFAULTS, S)
        assert bool(chk.trips) is trips and (PAPER in chk.force_exit) is trips, (realized, mtm)
        assert chk.unknown == ((f"BOOK:{PAPER}",) if mtm is None and not trips else ()), (realized, mtm)
    (trip,) = check_daily_stops([book_state(PAPER, realized_net=-16_000.0, mtm_net=None)], (), DEFAULTS, S).trips
    assert trip.day_net == -16_000.0 and "realized alone (MTM unknown)" in trip.alert.text
    assert (trip.halt.kind, trip.halt.scope, trip.halt.clears) == ("DAILY_STOP", f"BOOK:{PAPER}",
                                                                   HaltClears.NEXT_SESSION)
    # the LIVE account: its books' realized sum past LIVE_ACCOUNT_DAILY_STOP_RS trips it with a book's MTM unknown
    a = book_state(LIVE, realized_net=-6_000.0, mtm_net=0.0)
    b = book_state("LIVE:price_spike_up", realized_net=-4_500.0, mtm_net=None)
    chk = check_daily_stops([a, b], (), DEFAULTS, S)
    assert [t.scope for t in chk.trips] == ["ACCOUNT:LIVE"] and chk.trips[0].alert.severity == "CRITICAL"
    assert chk.force_exit == {LIVE, "LIVE:price_spike_up"} and chk.unknown == ("BOOK:LIVE:price_spike_up",)
    inside = check_daily_stops([a, replace(b, realized_net=-3_999.99)], (), DEFAULTS, S)
    assert inside.trips == () and "ACCOUNT:LIVE" in inside.unknown


def test_worst_case_cap():
    pad = WORST_CASE_PAD_RS
    assert worst_case_loss(-30_000.0, [16_000.0], 3_800.0) == 30_000.0 + 16_000.0 + pad + 3_800.0 + pad == 50_000.0
    c = ctx(book_state(PAPER, realized_net=-30_000.0, exposures_rs=(16_000.0,)))
    assert gate_worst_case(c, 3_700.0, 100.0) is None                         # exactly -50k: allowed
    assert gate_worst_case(c, 3_701.0, 100.0).gate == "WORST_CASE"
    # profits never extend the cap
    rich = ctx(book_state(PAPER, realized_net=+50_000.0, exposures_rs=(25_000.0,)))
    assert gate_worst_case(rich, 24_800.0, 0.0) is None
    assert gate_worst_case(rich, 24_801.0, 0.0) is not None
    # two max-premium positions can never both be open
    one_open = ctx(book_state(PAPER, exposures_rs=(50_000.0 + 31.0,)))
    assert gate_worst_case(one_open, 50_000.0, 31.0).gate == "WORST_CASE"
    # ... nor even one: premium + entry charges + the Rs 100 pad pass the Rs 50,000 cap at the Rs 50,000 premium cap
    assert gate_worst_case(ctx(book_state(PAPER)), 50_000.0, 31.0).gate == "WORST_CASE"
    # a LIVE book is also checked against the whole LIVE account
    acct = ctx(book_state(LIVE), live_account=live_account(exposures_rs=(25_000.0, 20_000.0)))
    r = gate_worst_case(acct, 5_000.0, 30.0)
    assert r.gate == "WORST_CASE" and r.detail.startswith("LIVE_ACCOUNT")
    assert gate_worst_case(ctx(book_state(LIVE), live_account=None), 5_000.0, 30.0).detail == "NO_LIVE_ACCOUNT"
    # through the post-quote chain, on the planned premium (500 x 32.15) plus estimated entry charges
    assert gate_of(run("post", b={"realized_net": -33_900.0})) == "WORST_CASE"
    assert run("post", b={"realized_net": -33_700.0}).passed
    assert gate_of(run("post", c={"settings": replace(DEFAULTS, MAX_WORST_CASE_DAY_LOSS_RS=0.0)})) == "WORST_CASE"
    # status.json room; malformed exposures raise (G20 then refuses with GATE_ERROR)
    assert worst_case_room(0.0, [18_600.0], 40_000.0) == 21_300.0
    with pytest.raises(ValueError):
        worst_case_loss(0.0, [-1.0])
    assert run("post", b={"exposures_rs": (math.inf,)}).result == Refusal("WORST_CASE", risk.GATE_ERROR)


def test_liquidity_rules():
    def liq(q=None, qty=500, limit=32.15, tick=0.05, s=DEFAULTS, now=NOW_WALL):
        return liquidity(quote() if q is None else q, qty, limit, tick, now, s, RATES)

    def detail(r):
        return None if r is None else r.detail

    assert liq() is None
    assert 0.4 < charges_hurdle_pct(32.15, 500, RATES) < 0.7
    # spread: max(2 ticks, LIQ_MAX_SPREAD_PCT of mid)
    two_tick = [(4.95, 50_000), (4.90, 50_000), (4.85, 50_000)]
    assert not (detail(liq(quote(bids=two_tick, asks=[(5.05, 50_000)] * 3), 10_000, 5.15)) or "").startswith("SPREAD")
    assert detail(liq(quote(bids=two_tick, asks=[(5.10, 50_000)] * 3), 10_000, 5.15)).startswith("SPREAD")
    wide_ok = quote(bids=[(199.0, 900)] * 3, asks=[(202.0, 900)] * 3)
    assert not (detail(liq(wide_ok, 100, 202.10)) or "").startswith("SPREAD")
    assert detail(liq(quote(bids=[(199.0, 900)] * 3, asks=[(202.05, 900)] * 3), 100, 202.10)).startswith("SPREAD")
    sammaan = quote(bids=[(4.60, 90_000)] * 3, asks=[(5.00, 90_000)] * 3)                 # ~8% spread
    assert detail(liq(sammaan, 6_000, 5.05)).startswith("SPREAD")
    # depth: top 3 levels of BOTH sides must hold LIQ_DEPTH_MULT x qty; deeper levels do not count
    assert detail(liq(quote(asks=((32.05, 600), (32.10, 600), (32.15, 299))))).startswith("DEPTH_ASK")
    assert detail(liq(quote(bids=((31.95, 600), (31.90, 600), (31.85, 299))))).startswith("DEPTH_BID")
    deep4 = quote(asks=((32.05, 100), (32.10, 100), (32.15, 100), (32.20, 99_999)))
    assert detail(liq(deep4)).startswith("DEPTH_ASK")
    # the walk must fill all of qty within the limit (a trade-through fills nothing more)
    gap = quote(asks=((32.05, 200), (32.40, 5_000), (32.45, 5_000)))
    assert detail(liq(gap)).startswith("WALK_SHORT")
    # average fill vs mid (never LTP)
    assert detail(liq(s=replace(DEFAULTS, LIQ_MAX_SLIP_VS_MID_PCT=0.1))).startswith("SLIPPAGE")
    assert liq(quote(ltp=99.0)) is None and liq(quote(ltp=None)) is None
    # last trade: known and at most LIQ_MAX_LAST_TRADE_AGE_S old (a small lead of the exchange clock is tolerated)
    assert detail(liq(quote(last_trade_age=None))) == "LAST_TRADE_UNKNOWN"
    assert liq(quote(last_trade_age=60.0)) is None
    assert detail(liq(quote(last_trade_age=60.5))).startswith("LAST_TRADE_STALE")
    assert liq(quote(last_trade_age=-2.0)) is None
    assert detail(liq(quote(last_trade_age=-10.0))).startswith("LAST_TRADE_STALE")
    # charges hurdle: (breakeven / limit - 1) x 100 <= MAX_CHARGES_HURDLE_PCT
    small = quote(bids=((19.95, 300), (19.90, 300), (19.85, 300)), asks=((20.05, 300), (20.10, 300), (20.15, 300)))
    assert charges_hurdle_pct(20.15, 250, RATES) > 1.0
    assert detail(liq(small, 250, 20.15)).startswith("CHARGES_HURDLE")
    assert detail(liq(s=replace(DEFAULTS, MAX_CHARGES_HURDLE_PCT=0.5))).startswith("CHARGES_HURDLE")
    # malformed input refuses
    for kw in ({"qty": 0}, {"qty": 1.5}, {"qty": True}, {"limit": math.nan}, {"limit": 0.0}, {"tick": 0.0}):
        assert detail(liq(**kw)).startswith("BAD_INPUT"), kw
    assert detail(liq(quote(bids=()))) == "NO_TWO_SIDED_BOOK"


def scope_for(kind: str) -> str:
    form = HALT_RULES[kind].scopes[0]
    return {"GLOBAL": "GLOBAL", "ACCOUNT": "ACCOUNT:PAPER", "ACCOUNT:LIVE": "ACCOUNT:LIVE", "BOOK": f"BOOK:{PAPER}",
            "SYMBOL": f"SYMBOL:{SYM}"}[form]


def test_halt_clear_matrix():
    # the catalog is the spec's §8.7 table
    by_clear = {c: {k for k, r in HALT_RULES.items() if r.clears is c} for c in HaltClears}
    assert by_clear[HaltClears.RESUME] == {"PAUSE", "FLATTEN"}
    assert by_clear[HaltClears.NEXT_SESSION] == {"DAILY_STOP", "LOSS_STREAK", "CONTRACT_BLOCKED",
                                                 "LIVE_DISABLED_TODAY"}
    assert by_clear[HaltClears.ACK] == {"KILL", "SAFE_FLAG", "MISMATCH", "UNKNOWN_POSITION", "SHORT_DETECTED",
                                        "INVARIANT", "EXCEPTION", "GATE_ERRORS", "ORDER_STUCK", "REJECTS", "RUNAWAY"}
    assert by_clear[HaltClears.AUTO] == {"EXIT_DIFFICULTY", "TOKEN", "STALE_FEED", "STALE_RECON", "SETTINGS_INVALID",
                                         "CLOCK_UNSYNCED", "NO_INSTRUMENTS", "V1_ACTIVE"}
    assert OPERATOR_HALTS == {"PAUSE", "FLATTEN", "KILL", "SAFE_FLAG"}
    # with every condition met, each kind clears by exactly its own action (UNKNOWN_POSITION also by ACK_FOREIGN)
    good = dict(today=NEXT, flat=True, recon_clean=True, foreign_exact=True, condition_met=True, safe_mode=False)
    for kind, rule in HALT_RULES.items():
        h = halt(kind, scope_for(kind), session=S)
        for action in CLEAR_ACTIONS:
            allowed, why = can_clear(h, action, **good)
            expected = action == rule.clears.value or (action == "ACK_FOREIGN" and kind == "UNKNOWN_POSITION")
            assert allowed is expected, (kind, action, why)
            assert isinstance(why, str) and why
    # the extra conditions
    assert not can_clear(halt("FLATTEN"), "RESUME", today=NEXT, flat=False)[0]
    assert can_clear(halt("PAUSE"), "RESUME", today=NEXT)[0]                           # PAUSE needs nothing else
    assert not can_clear(halt("KILL"), "ACK", today=NEXT, flat=False)[0]
    assert can_clear(halt("KILL"), "ACK", today=NEXT, flat=True)[0]
    for kind in ("MISMATCH", "UNKNOWN_POSITION", "SHORT_DETECTED"):
        assert not can_clear(halt(kind, scope_for(kind)), "ACK", today=NEXT, recon_clean=False)[0], kind
        assert can_clear(halt(kind, scope_for(kind)), "ACK", today=NEXT, recon_clean=True)[0], kind
    unknown_pos = halt("UNKNOWN_POSITION", "ACCOUNT:LIVE")
    assert not can_clear(unknown_pos, "ACK_FOREIGN", today=NEXT, foreign_exact=False)[0]
    assert can_clear(unknown_pos, "ACK_FOREIGN", today=NEXT, foreign_exact=True)[0]
    assert not can_clear(halt("MISMATCH", "ACCOUNT:LIVE"), "ACK_FOREIGN", today=NEXT, foreign_exact=True)[0]
    assert not can_clear(halt("SAFE_FLAG"), "ACK", today=NEXT, safe_mode=True)[0]
    assert can_clear(halt("SAFE_FLAG"), "ACK", today=NEXT, safe_mode=False)[0]
    # the daily stop can never be acked or resumed, and a same-session rollover keeps it
    dstop = halt("DAILY_STOP", f"BOOK:{PAPER}", session=S)
    for action in ("ACK", "RESUME", "AUTO", "ACK_FOREIGN"):
        assert not can_clear(dstop, action, **{**good, "today": S})[0], action
    assert not can_clear(dstop, "NEXT_SESSION", today=S)[0]
    assert can_clear(dstop, "NEXT_SESSION", today=NEXT)[0]
    # AUTO only when its own condition holds; an operator cannot ACK it away
    for kind in ("TOKEN", "STALE_FEED", "EXIT_DIFFICULTY", "V1_ACTIVE"):
        h = halt(kind, scope_for(kind))
        assert not can_clear(h, "AUTO", today=NEXT, condition_met=False)[0]
        assert not can_clear(h, "ACK", today=NEXT, condition_met=True)[0]
    # a kind this code does not know: only an operator ACK
    odd = halt("BRAND_NEW", "GLOBAL")
    assert [a for a in CLEAR_ACTIONS if can_clear(odd, a, **good)[0]] == ["ACK"]
    assert not can_clear(halt("PAUSE"), "UNPAUSE", today=NEXT)[0]
    # the first Timer of a new session clears exactly the earlier sessions' NEXT_SESSION halts
    active = (dstop, halt("LOSS_STREAK", f"BOOK:{PAPER}", hid=2), halt("CONTRACT_BLOCKED", f"SYMBOL:{SYM}", hid=3),
              halt("LIVE_DISABLED_TODAY", "ACCOUNT:LIVE", hid=4), halt("DAILY_STOP", f"BOOK:{LIVE}", session=NEXT,
                                                                        hid=5),
              halt("PAUSE", hid=6), halt("EXCEPTION", hid=7), halt("TOKEN", hid=8))
    assert [h.halt_id for h in rollover_clears(active, NEXT)] == [1, 2, 3, 4]


# ---------------------------------------------------------------------------------------------------------------
# Further unit tests
# ---------------------------------------------------------------------------------------------------------------
def test_make_halt_scopes():
    for kind in HALT_RULES:
        h = make_halt(kind, scope_for(kind), "why")
        assert h == HaltRequest(kind, scope_for(kind), HALT_RULES[kind].clears, "why")
    assert make_halt("DAILY_STOP", "ACCOUNT:LIVE", "x").clears is HaltClears.NEXT_SESSION
    assert make_halt("INVARIANT", "ACCOUNT:SHADOW", "x").scope == "ACCOUNT:SHADOW"
    for kind, scope in (("DAILY_STOP", "ACCOUNT:PAPER"), ("DAILY_STOP", "GLOBAL"), ("LIVE_DISABLED_TODAY",
                        "ACCOUNT:PAPER"), ("PAUSE", f"BOOK:{PAPER}"), ("RUNAWAY", "BOOK:NOT_A_BOOK"),
                        ("MISMATCH", "ACCOUNT:NOPE"), ("CONTRACT_BLOCKED", "GLOBAL"), ("NO_SUCH_KIND", "GLOBAL")):
        with pytest.raises(ValueError):
            make_halt(kind, scope, "x")


def test_entry_halt_scopes():
    halts = (halt("DAILY_STOP", f"BOOK:{PAPER}", hid=1), halt("DAILY_STOP", "BOOK:PAPER:price_spike_up", hid=2),
             halt("STALE_RECON", "ACCOUNT:LIVE", hid=3), Halt(4, f"UNDERLYING:{UND}", "X", "t", False,
                                                              HaltClears.ACK, NOW_WALL, S),
             Halt(5, "UNDERLYING:NSE:INFY", "X", "t", False, HaltClears.ACK, NOW_WALL, S),
             halt("CONTRACT_BLOCKED", f"SYMBOL:{SYM}", hid=6), Halt(7, f"STRATEGY:{STRAT}", "X", "t", False,
                                                                     HaltClears.ACK, NOW_WALL, S))

    def ids(**kw):
        base = dict(account=Account.PAPER, book=PAPER, strategy=STRAT, underlying=UND, policy=Policy.FULL)
        base.update(kw)
        return [h.halt_id for h in entry_halts(halts, **base)]

    assert ids() == [1, 4, 7]
    assert ids(account=Account.LIVE, book=LIVE) == [3, 4, 7]
    assert ids(underlying="NSE:INFY") == [1, 5, 7]
    assert ids(account=Account.SHADOW, book=SHADOW, policy=Policy.QUALITY) == []
    assert ids(policy=None) == [1, 4, 7]                                       # unknown policy: as strict as FULL
    # the SYMBOL halt blocks at G15 only
    assert run("pre", c={"halts": (halts[5],)}).passed
    assert gate_of(run("post", c={"halts": (halts[5],)})) == "CONTRACT"


def test_runaway_refusal_raises_halt():
    out = run("pre", b={"entry_orders_60s": 3, "open": 2})        # checked before the positions cap
    assert out.refusal.gate == "BOOK_CAPS" and out.refusal.detail.startswith("RUNAWAY")
    assert out.halts == (HaltRequest("RUNAWAY", f"BOOK:{PAPER}", HaltClears.ACK, out.refusal.detail),)
    assert run("pre", b={"attempts": 6}).halts == ()


def test_entries_cap_edge():
    """MAX_ENTRIES_PER_DAY is 3 per FULL book (the user's decision, 2026-10-13; it was 2): the third entry of the day
    passes G09 and a fourth is refused, a structure still reserved or entering counting as one. A LIVE book keeps 1
    a day in its first week. G09 checks RUNAWAY before ENTRIES, so once three entry orders went out within 60 s a
    fourth intent is refused as RUNAWAY, and halts the book, even when the entries cap alone would refuse it (the
    FINAL_SPEC errata of 2026-10-13)."""
    assert DEFAULTS.MAX_ENTRIES_PER_DAY == 3
    assert run("pre", b={"entries": 2}).passed and run("pre", b={"entries": 1, "pending": 1}).passed
    assert run("pre", b={"entries": 3}).result == Refusal("BOOK_CAPS", "ENTRIES: 3 + 0 pending/3")
    assert run("pre", b={"entries": 2, "pending": 1}).result == Refusal("BOOK_CAPS", "ENTRIES: 2 + 1 pending/3")
    assert run("pre", book=LIVE, b={"entries": 2}).passed
    assert run("pre", book=LIVE, b={"entries": 1}, c={"live": live_keys(first_week=True)}).result == Refusal(
        "BOOK_CAPS", "ENTRIES: 1 + 0 pending/1")
    # the gate order: the day's entries used up AND three entry orders in the last 60 s -> RUNAWAY, with its halt
    out = run("pre", b={"entries": 3, "entry_orders_60s": 3})
    assert out.result == Refusal("BOOK_CAPS", "RUNAWAY: 3 entry orders in 60 s (max 3)")
    assert out.halts == (HaltRequest("RUNAWAY", f"BOOK:{PAPER}", HaltClears.ACK, out.refusal.detail),)
    out = run("pre", b={"entries": 3, "entry_orders_60s": 2})          # one order fewer: the entries cap, no halt
    assert out.result == Refusal("BOOK_CAPS", "ENTRIES: 3 + 0 pending/3") and out.halts == ()


def test_note_gate_error_halts_at_five():
    window: tuple[float, ...] = (NOW_MONO - GATE_ERROR_WINDOW_S - 1.0,)          # too old: dropped
    for k in range(GATE_ERROR_LIMIT - 1):
        window, h = note_gate_error(window, NOW_MONO + k)
        assert h is None
    assert len(window) == GATE_ERROR_LIMIT - 1
    window, h = note_gate_error(window, NOW_MONO + 10.0)
    assert h == make_halt("GATE_ERRORS", "GLOBAL", h.reason) and h.clears is HaltClears.ACK
    # spread out over more than 5 minutes: never five at once
    w: tuple[float, ...] = ()
    for k in range(10):
        w, h = note_gate_error(w, NOW_MONO + k * 80.0)
        assert h is None


def test_session_window_edges():
    assert run("pre", c={"now_wall": W("09:20:00")}).passed
    assert run("pre", c={"now_wall": W("14:54:59.999")}).passed
    late = replace(DEFAULTS, ENTRY_START=dtime(9, 30))
    assert gate_of(run("pre", c={"settings": late, "now_wall": W("09:29:59")})) == "SESSION"
    assert run("pre", c={"settings": late, "now_wall": W("09:30:00")}).passed
    # the 15:40 close is never assumed: entries stop at NO_NEW_AFTER regardless of the session close
    assert gate_of(run("pre", c={"now_wall": W("15:30:00")})) == "SESSION"


def test_final_gate():
    assert run("final").passed
    assert run("final", i={"created_mono": NOW_MONO - 11.0}).result.detail.startswith("EXPIRED")
    assert run("final", c={"halts": (halt("TOKEN"),)}).result == Refusal("FINAL", "HALTS: TOKEN@GLOBAL")
    # QUALITY books are not stopped by PAUSE at G22 either
    assert run("final", book=SHADOW, c={"halts": (halt("PAUSE"),)}).passed


def test_contract_dte_and_index():
    nifty = Instrument("NFO:NIFTY26O1425000CE", 9, "NIFTY26O1425000CE", "NFO", "NIFTY", "NSE:NIFTY 50", "CE",
                       25000.0, date(2026, 10, 14), 65, 0.05)
    i = intent(underlying="NSE:NIFTY 50", recheck=Recheck(-math.inf, math.inf))
    c = ctx()
    assert risk.gate_contract(c, i, nifty) is None                              # td 2 >= MIN_TD_TO_EXPIRY_INDEX
    tight = ctx(settings=replace(DEFAULTS, MIN_TD_TO_EXPIRY_INDEX=3))
    assert risk.gate_contract(tight, i, nifty).detail.startswith("DTE")
    assert risk.gate_contract(c, intent(structure=StructureType.LONG_PE), INST).detail.startswith("OPT_TYPE")


def test_cooldown_gate_boundary():
    # G11 refuses while cooldown_until(book, underlying) > now: at the end time itself the gate passes. The end times
    #   come from Store.cooldowns (test_store.py::test_cooldowns_are_the_rule_the_gates_use); risk keeps no copy.
    end = W("10:00:00")                                                  # == NOW_WALL
    assert run("pre", b={"cooldown_until": {UND: end}}).passed
    late = run("pre", b={"cooldown_until": {UND: end + timedelta(microseconds=1)}})
    assert late.result == Refusal("COOLDOWN", "UNTIL 10:00:00")
    assert run("pre", b={"cooldown_until": {"NSE:INFY": end + timedelta(minutes=5)}}).passed   # per underlying
    assert not hasattr(risk, "cooldown_end") and "cooldown_end" not in risk.__all__


def test_books_and_routing():
    assert parse_book(PAPER) == ("PAPER", STRAT) and parse_book("LIVE:_selftest") == ("LIVE", "_selftest")
    for bad in ("PAPER:", "PAPER", "paper:x", "PAPER:a:b", "PAPER: x", None, 7, "FOO:x"):
        assert parse_book(bad) is None, bad
    assert [book_account(b) for b in (PAPER, LIVE, SHADOW, PLACEBO)] == [Account.PAPER, Account.LIVE,
                                                                         Account.SHADOW, Account.SHADOW]
    assert [book_policy(b) for b in (PAPER, LIVE, SHADOW, PLACEBO)] == [Policy.FULL, Policy.FULL, Policy.QUALITY,
                                                                        Policy.QUALITY]
    assert book_policy("LIVE:_selftest") is None and book_account("LIVE:_selftest") is Account.LIVE
    assert books_for(STRAT, Mode.OFF) == () and books_for(STRAT, "SHADOW") == (SHADOW,)
    assert books_for(STRAT, Mode.PAPER) == (PAPER,) and books_for(STRAT, Mode.LIVE) == (LIVE, PAPER)
    assert books_for(STRAT, Mode.PAPER, True) == (PLACEBO,) and books_for(STRAT, Mode.LIVE, True) == (PLACEBO,)
    assert books_for(STRAT, Mode.SHADOW, True) == ()
    # a placebo routes only to PLACEBO:<s>
    assert run("pre", book=PLACEBO, i={"is_placebo": True}).passed
    assert gate_of(run("pre", book=PAPER, i={"is_placebo": True})) == "STRATEGY"


def test_loss_streak_halt():
    on = replace(DEFAULTS, STOP_AFTER_FIRST_LOSS=True)
    assert loss_streak_halt(PAPER, -0.01, on) == make_halt("LOSS_STREAK", f"BOOK:{PAPER}",
                                                           loss_streak_halt(PAPER, -0.01, on).reason)
    assert loss_streak_halt(PAPER, -500.0, DEFAULTS) is None
    assert loss_streak_halt(PAPER, 0.0, on) is None and loss_streak_halt(SHADOW, -500.0, on) is None


def test_gate_context_names_are_the_specs():
    fields = {f.name for f in dataclasses.fields(GateContext)}
    assert {"now_mono", "now_wall", "session", "settings", "calendar", "clock_synced", "instruments_loaded",
            "ban_list", "blocked_symbols", "feed", "signals_degraded", "recon", "halts", "strategy_modes",
            "running_versions", "book", "live_account", "live"} <= fields
    assert {"policy", "open", "reserved", "entries", "attempts", "orders_today", "entry_orders_60s", "realized_net",
            "mtm_net", "first_loss_today", "cooldown_until", "active_underlyings"} <= {
        f.name for f in dataclasses.fields(BookState)}
    assert [f.name for f in dataclasses.fields(LivePreconditions)] == ["allow_live", "unlocked", "selftest_passed",
                                                                        "demoted", "first_week"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx().now_mono = 1.0


def test_instruments_protocol_matches_store():
    # the gates call InstrumentStore duck-typed: these two methods with these parameters
    sig = inspect.signature(InstrumentStore.ineligible_reason)
    assert list(sig.parameters)[:5] == ["self", "underlying", "ban_list", "allow", "deny"]
    assert list(inspect.signature(InstrumentStore.underlying).parameters)[:2] == ["self", "symbol_or_name"]


def test_gate_live_direct():
    assert gate_live(ctx(book_state(LIVE), live=None)) == Refusal("LIVE", "NO_LIVE_STATE")
    assert gate_live(ctx(book_state(PAPER), live=None)) is None                 # not a LIVE book
