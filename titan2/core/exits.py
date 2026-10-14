"""Exit engine and exact order pricing (spec §5.7, §7; DETAIL §5.7, §7). Pure: no I/O, no clock reads, no state.

- compute_exit_params(): the levels frozen at the entry fill (§7.1). Nothing changes them afterwards.
- realizable_bid(): what the open quantity would fetch from the shown bids right now (§7.3).
- decide(): one evaluation of an OPEN leg, rules 0-9 in order, first match wins (§7.4). It reads the leg's marks and
  returns a Decision carrying the new marks; the core thread applies them (Marks.apply_to) and persists them.
- entry_limit(), exit_limit(), cover_limit(), selftest_limit() and the band-reject helpers: exact prices (§5.7).

Every rule works on the realizable bid (rb) of a snapshot at most STALE_REFRESH_S old (by receipt time,
receipt_age()). The last traded price is never used for a decision. decide() never reverses, adds, averages down or
exits partially.

Name clash: this module's Exit is an exit *decision*; titan2.core.events.Exit is the process-exit command. Import
this module as a namespace (`from titan2.core import exits`) and write exits.Exit.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, fields, replace
from datetime import date, datetime, timedelta
from typing import Iterable, Union

from titan2.charges import OrderFills, RateSet, breakeven_px, k_exit, order_charges
from titan2.clock import ist_datetime
from titan2.model import (
    EXIT_REASONS, PRICE_EPS, RECEIPT_SKEW_S, ExitParams, ExitProfile, Leg, Quote, Side, Urgency, ceil_tick,
    floor_tick, walk,
)
from titan2.settings import (
    BANDS, ENTRY_MAX_OVER_MID, MAX_EXIT_DISCOUNT, PRICE_EDGE_TICKS, SELFTEST_BID_FACTOR, THIN_BOOK_PENALTY, Settings,
    exit_profile_problems,
)

__all__ = [
    "BAND_REJECT_RE", "Decision", "EodTimes", "Exit", "FORCED_REASONS", "LOCK_ARMED", "MARK_FIELDS", "Marks",
    "NO_BID", "NO_REFRESH", "NoAction", "QUOTE_STALE", "RefreshQuote", "RefreshState", "STOP_BREACH_PENDING",
    "RECEIPT_SKEW_S", "STOP_MOVED", "band_reject_limit", "compute_exit_params", "cover_limit", "decide",
    "entry_limit", "exit_limit", "exit_params_from_fills", "exit_params_problems", "is_band_reject",
    "realizable_bid", "receipt_age", "selftest_limit", "sell_limit",
]

PROTECTIVE = Urgency.PROTECTIVE
NORMAL = Urgency.NORMAL

# Rule 0: flags set outside the exit engine that force a protective exit. decide() also honours any other
# EXIT_REASONS member passed as the flag (e.g. EOD_FLATTEN) and turns anything else into ANOMALY_EXIT.
FORCED_REASONS = frozenset({"KILL_SWITCH", "OPERATOR_FLATTEN", "DAILY_STOP", "ANOMALY_EXIT", "RECOVERY_FLATTEN",
                            "SELFTEST_FILLED"})

# Decision.notes: event kinds (§4.6) one evaluation produced; the engine writes the events rows.
LOCK_ARMED = "LOCK_ARMED"
STOP_MOVED = "STOP_MOVED"
STOP_BREACH_PENDING = "STOP_BREACH_PENDING"
NO_BID = "NO_BID"
QUOTE_STALE = "QUOTE_STALE"

_TIME_EPS = 1e-6        # s: monotonic floats such as 1000.4 - 1000.0 land a hair under the threshold they reach
# kite-ws stamps every snapshot with clock.mono() at receipt while the core evaluates with the event's now_mono,
# stamped when the event began (the Timer before store.tx(); _begin() for the rest). A tick that lands while the
# event runs (status.json, other legs, quote_checks rows) is up to the event's duration "in the future": it is the
# newest data there is, age 0. Further ahead than model.RECEIPT_SKEW_S is a clock-domain bug and reads as
# infinitely stale. Quote.age() applies the same rule, so QuoteCache.fresh, the exit loop's pricing and the entry
# gates agree with decide().


def _finite(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def receipt_age(q: Quote, now_mono: float) -> float:
    """Seconds since q was received, as of now_mono (Quote.age): a receipt up to RECEIPT_SKEW_S after now_mono is a
    tick that arrived during the event (age 0); later, NaN or non-finite times read as infinitely stale."""
    return q.age(now_mono)


def _positive(x: object) -> bool:
    return _finite(x) and x > 0


def _plain_int(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _aware(x: object) -> bool:
    return isinstance(x, datetime) and x.tzinfo is not None and x.utcoffset() is not None


# ---------------------------------------------------------------------------------------------------------------
# Inputs and outputs of decide()
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Marks:
    """The leg fields decide() reads and returns (same names as on Leg). breach_* and no_bid_since are monotonic
    times, never persisted: after a reload they are None, which restarts a pending debounce ("recovery clears")."""

    stop_px: float | None
    lock_armed: bool
    peak_rb: float | None
    peak_wall: datetime | None
    trough_rb: float | None
    trough_wall: datetime | None
    breach_first_mono: float | None
    breach_quote_mono: float | None
    no_bid_since_mono: float | None

    @classmethod
    def of(cls, leg: Leg) -> Marks:
        return cls(*(getattr(leg, name) for name in MARK_FIELDS))

    def apply_to(self, leg: Leg) -> None:
        """Copy the marks onto the leg (core thread only)."""
        for name in MARK_FIELDS:
            setattr(leg, name, getattr(self, name))


MARK_FIELDS = tuple(f.name for f in fields(Marks))


@dataclass(frozen=True, slots=True)
class NoAction:
    marks: Marks
    rb: float | None = None             # this evaluation's realizable bid; None when it was not computed
    thin: bool = False                  # rb valued part of the qty beyond the shown bids
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RefreshQuote:
    """Ask for a REST snapshot of the leg's symbol (FetchQuotes at priority EXIT)."""

    marks: Marks
    rb: float | None = None
    thin: bool = False
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Exit:
    """Move the leg to EXITING with this frozen reason (one of model.EXIT_REASONS)."""

    reason: str
    urgency: Urgency
    marks: Marks
    rb: float | None = None
    thin: bool = False
    notes: tuple[str, ...] = ()


Decision = Union[NoAction, RefreshQuote, Exit]


@dataclass(frozen=True, slots=True)
class RefreshState:
    """REST refresh status of one leg's option symbol. The engine keeps it; decide() only reads it.

    in_flight: a FetchQuotes for the symbol is outstanding (decide then asks for no second one).
    last_ok_mono: receipt time of the newest REST snapshot of the symbol.
    last_fail_mono: when the newest REST refresh of the symbol failed (API error, symbol missing, 3 s timeout).
    """

    in_flight: bool = False
    last_ok_mono: float | None = None
    last_fail_mono: float | None = None

    def ok_since(self, t: float) -> bool:
        return self.last_ok_mono is not None and self.last_ok_mono >= t

    def failed_since(self, t: float) -> bool:
        return self.last_fail_mono is not None and self.last_fail_mono >= t


NO_REFRESH = RefreshState()


@dataclass(frozen=True, slots=True)
class EodTimes:
    """The session's exit timing, from the Settings read once at start: the EOD wall times plus the two quote
    staleness limits that rule 2 needs. Build it with for_session()."""

    flatten: datetime                   # EOD_FLATTEN: every open leg exits (rule 1)
    backstop: datetime                  # EOD_BACKSTOP: exit pricing starts at band >= 3 (§5.5)
    stop_sending: datetime              # STOP_SENDING: no more orders of any kind (§5.6)
    stale_refresh_s: float              # STALE_REFRESH_S: an older snapshot is never used; refresh it
    stale_protective_s: float           # STALE_PROTECTIVE_S: still nothing fresh -> protective exit

    def __post_init__(self) -> None:
        if not all(_aware(t) for t in (self.flatten, self.backstop, self.stop_sending)):
            raise ValueError("EodTimes needs timezone-aware datetimes")
        if not (_positive(self.stale_refresh_s) and _finite(self.stale_protective_s)
                and self.stale_protective_s > self.stale_refresh_s):
            raise ValueError(f"bad staleness limits {self.stale_refresh_s!r} / {self.stale_protective_s!r}")

    @classmethod
    def for_session(cls, session: date, s: Settings) -> EodTimes:
        return cls(ist_datetime(session, s.EOD_FLATTEN), ist_datetime(session, s.EOD_BACKSTOP),
                   ist_datetime(session, s.STOP_SENDING), float(s.STALE_REFRESH_S), float(s.STALE_PROTECTIVE_S))


# ---------------------------------------------------------------------------------------------------------------
# §7.1 Levels frozen at the entry fill
# ---------------------------------------------------------------------------------------------------------------
def compute_exit_params(profile: ExitProfile, entry_avg: float, qty: int, entry_charges: float, rates: RateSet,
                        entry_fill_wall: datetime) -> ExitParams:
    """ExitParams from the entry fills: E = entry_avg, Q = qty, C_in = the entry orders' actual charges.

    be_px is the exact price at which selling Q in one more order nets zero; the lock levels are anchored to it.
    Raises ValueError on inputs no real fill can produce. A profile outside EXIT_BOUNDS is not refused here (the
    registry already refuses it); exit_params_problems() reports it and decide() then exits protectively.
    """
    if not _positive(entry_avg):
        raise ValueError(f"entry_avg must be a finite price > 0, got {entry_avg!r}")
    if not (_plain_int(qty) and qty > 0):
        raise ValueError(f"qty must be an int > 0, got {qty!r}")
    if not (_finite(entry_charges) and entry_charges >= 0):
        raise ValueError(f"entry_charges must be a finite amount >= 0, got {entry_charges!r}")
    if not _aware(entry_fill_wall):
        raise ValueError(f"entry_fill_wall must be a timezone-aware datetime, got {entry_fill_wall!r}")
    if not isinstance(profile, ExitProfile):
        raise ValueError(f"profile must be an ExitProfile, got {type(profile).__name__}")
    E = float(entry_avg)
    be = breakeven_px(E * qty, qty, float(entry_charges), rates)
    return ExitParams(
        profile=profile, entry_avg=E, qty=qty, entry_charges=float(entry_charges), rates_version=rates.version,
        k_exit=k_exit(rates), be_px=be,
        hard_sl_px=E * (1 - profile.hard_sl_pct / 100),
        arm_px=be + profile.lock_arm_pct / 100 * E,
        floor_px=be + profile.lock_floor_pct / 100 * E,
        tp_px=E * (1 + profile.tp_pct / 100),
        entry_fill_wall=_whole_ms(entry_fill_wall),
        time_stop_at=_whole_ms(entry_fill_wall + timedelta(minutes=profile.time_stop_min)),
    )


def _whole_ms(t: datetime) -> datetime:
    """Truncate to whole milliseconds, the precision titan2.db stores, so frozen params equal their reloaded copy."""
    return t.replace(microsecond=t.microsecond // 1000 * 1000)


def exit_params_from_fills(profile: ExitProfile, entry_orders: Iterable[OrderFills], rates: RateSet,
                           entry_fill_wall: datetime) -> ExitParams:
    """compute_exit_params() from the entry orders' effective fills: E = value / qty, C_in = their charges."""
    orders = list(entry_orders)
    if any(o.side is not Side.BUY for o in orders):
        raise ValueError("entry orders must all be BUY")
    qty = sum(o.filled_qty for o in orders)
    if qty <= 0:
        raise ValueError("no entry fills")
    value = math.fsum(o.value for o in orders)
    c_in = round(math.fsum(order_charges(o, rates).total for o in orders), 2)
    return compute_exit_params(profile, value / qty, qty, c_in, rates, entry_fill_wall)


def exit_params_problems(p: ExitParams | None) -> list[str]:
    """Every reason frozen levels cannot be trusted ([] when they can): the I8 check and its preconditions.

    Covers a missing object, a profile outside EXIT_BOUNDS, non-finite levels (a NaN stop would never fire),
    I8 (0 < hard_sl_px < entry_avg < tp_px and floor_px > be_px), tp_px > arm_px, the qty and the time stop.
    """
    if not isinstance(p, ExitParams):
        return ["no frozen ExitParams"]
    if not isinstance(p.profile, ExitProfile):
        return ["ExitParams.profile is not an ExitProfile"]
    out = [f"profile: {x}" for x in exit_profile_problems(p.profile)]
    levels = {"entry_avg": p.entry_avg, "be_px": p.be_px, "hard_sl_px": p.hard_sl_px, "arm_px": p.arm_px,
              "floor_px": p.floor_px, "tp_px": p.tp_px}
    bad = [k for k, v in levels.items() if not _finite(v)]
    if bad:
        return out + [f"non-finite levels: {', '.join(bad)}"]
    if not 0 < p.hard_sl_px < p.entry_avg < p.tp_px:
        out.append(f"I8: need 0 < hard_sl_px {p.hard_sl_px} < entry_avg {p.entry_avg} < tp_px {p.tp_px}")
    if not p.floor_px > p.be_px:
        out.append(f"I8: floor_px {p.floor_px} must be above be_px {p.be_px}")
    if not p.tp_px > p.arm_px:
        out.append(f"tp_px {p.tp_px} must be above arm_px {p.arm_px}")
    if not (_plain_int(p.qty) and p.qty > 0):
        out.append(f"qty {p.qty!r} is not an int > 0")
    if not _aware(p.time_stop_at):
        out.append("time_stop_at is not a timezone-aware datetime")
    return out


# ---------------------------------------------------------------------------------------------------------------
# §7.3 Realizable bid
# ---------------------------------------------------------------------------------------------------------------
def realizable_bid(q: Quote | None, qty: int) -> tuple[float | None, bool]:
    """(rb, thin): the average price of selling qty into q's bids now. Quantity beyond the shown bids is valued at
    the deepest bid x (1 - THIN_BOOK_PENALTY) and flagged thin. No bids (or nothing to sell) -> (None, False)."""
    if q is None or not q.bids or not (_plain_int(qty) and qty > 0):
        return None, False
    w = walk(q.bids, qty, None, Side.SELL)
    if w.fillable_qty >= qty:
        return w.avg_px, False
    short = qty - w.fillable_qty
    return (w.avg_px * w.fillable_qty + short * w.worst_px * (1 - THIN_BOOK_PENALTY)) / qty, True


# ---------------------------------------------------------------------------------------------------------------
# §7.4 decide()
# ---------------------------------------------------------------------------------------------------------------
def _num(x: object) -> float | None:
    return x if _finite(x) else None


def _clean(m: Marks) -> Marks:
    """Marks with non-finite numbers dropped and paired fields kept consistent, so a corrupt stored stop can never
    disable the hard stop (decide() falls back to hard_sl_px)."""
    peak, trough = _num(m.peak_rb), _num(m.trough_rb)
    first, seen = _num(m.breach_first_mono), _num(m.breach_quote_mono)
    if first is None or seen is None:
        first = seen = None
    return Marks(_num(m.stop_px), bool(m.lock_armed), peak, m.peak_wall if peak is not None else None,
                 trough, m.trough_wall if trough is not None else None, first, seen, _num(m.no_bid_since_mono))


def _refresh(refresh: RefreshState, m: Marks, notes: tuple[str, ...] = (), rb: float | None = None,
             thin: bool = False) -> Decision:
    """RefreshQuote, or NoAction while a refresh of this symbol is already in flight."""
    if refresh.in_flight:
        return NoAction(m, rb, thin, notes)
    return RefreshQuote(m, rb, thin, notes)


# Ported from agentic_trader/exit_manager.py:445-485 @b4694f3 — changes: the hard stop compares the realizable bid
#   (never LTP) with a stop frozen at the entry fill, confirmed by a distinct later snapshot (bypassed on a deep
#   breach); a missing price is a REST refresh or a protective exit instead of a skipped check; long side only
# Ported from agentic_trader/exit_manager.py:1332-1406 @b4694f3 — changes: one trail re-anchored to the breakeven
#   price, armed at be + lock_arm_pct: stop = max(stop, floor_px, be + trail_retain * (peak_rb - be)); ratchets up
#   only; no R-multiple zones, VIX adjustment, manual-setup or short-side branches
def decide(leg: Leg, q: Quote | None, refresh: RefreshState | None, now_mono: float, now_wall: datetime,
           p: ExitParams | None, eod: EodTimes, forced: str | None) -> Decision:
    """One evaluation of an OPEN leg (§7.4). The first matching rule wins and its reason is frozen by the caller.

    q must be the newest snapshot of the leg's symbol whatever its age (QuoteCache's latest, not fresh()): rule 2
    does the ageing, by receipt time. q None (no snapshot yet) is aged from leg.last_fill_mono, the start of the bot's
    management of the leg (its last fill, or the process start after a restart), and counts as infinitely old when
    that is unknown. forced is the leg's forced-exit flag or None.
    """
    m = _clean(Marks.of(leg))
    # 0 forced flag (an unknown flag still exits: a requested exit is never dropped)
    if forced:
        return Exit(forced if forced in EXIT_REASONS else "ANOMALY_EXIT", PROTECTIVE, m)
    # 1 end of day
    if now_wall >= eod.flatten:
        return Exit("EOD_FLATTEN", PROTECTIVE, m)
    # frozen levels that cannot be trusted (I8), or another contract's book, are an anomaly: exit rather than hold
    #   without a working stop
    if exit_params_problems(p) or (q is not None and q.symbol != leg.inst.symbol):
        return Exit("ANOMALY_EXIT", PROTECTIVE, m)
    qty = leg.open_qty
    if qty <= 0:
        return NoAction(m)
    refresh = refresh or NO_REFRESH
    prof = p.profile

    # 2 stale snapshot: receipt age, never last-trade age. A stale quote feeds nothing below.
    if q is None:
        # No snapshot since the bot began managing the leg: a leg restored after a restart (§9.5 step 8, §9.6
        #   step 7: recovery runs on REST quotes; last_fill_mono is the process start, §9.2). G5: REST first, then
        #   protective at STALE_PROTECTIVE_S from that anchor or as soon as the refresh fails. No anchor (no fill
        #   known) counts as infinitely old.
        since = leg.last_fill_mono if _finite(leg.last_fill_mono) else -math.inf
        if now_mono - since > eod.stale_protective_s or refresh.failed_since(since):
            return Exit("STALE_QUOTE_PROTECTIVE", PROTECTIVE, m, notes=(QUOTE_STALE,))
        return _refresh(refresh, m, (QUOTE_STALE,))
    age = receipt_age(q, now_mono)
    if age > eod.stale_refresh_s:
        stale_since = q.recv_mono + eod.stale_refresh_s
        if age > eod.stale_protective_s or refresh.failed_since(stale_since):
            return Exit("STALE_QUOTE_PROTECTIVE", PROTECTIVE, m, notes=(QUOTE_STALE,))
        return _refresh(refresh, m, (QUOTE_STALE,))

    # 3 empty bid side: confirm with REST first. A REST refresh that failed also counts as tried, so a REST outage
    #   cannot hold the leg OPEN forever; the exit loop still never sends an order into an empty book.
    if not q.bids:
        since = m.no_bid_since_mono if m.no_bid_since_mono is not None else now_mono
        m = replace(m, no_bid_since_mono=since)
        if not (refresh.ok_since(since) or refresh.failed_since(since)):
            return _refresh(refresh, m, (NO_BID,))
        if now_mono - since >= prof.no_bid_exit_s - _TIME_EPS:
            return Exit("NO_BID_PROTECTIVE", PROTECTIVE, m, notes=(NO_BID,))
        return NoAction(m, notes=(NO_BID,))
    m = replace(m, no_bid_since_mono=None)

    # 4 realizable bid; bid-based MFE/MAE with the snapshot's receipt wall time
    rb, thin = realizable_bid(q, qty)
    notes: list[str] = []
    peak, peak_wall, trough, trough_wall = m.peak_rb, m.peak_wall, m.trough_rb, m.trough_wall
    if peak is None or rb > peak:
        peak, peak_wall = rb, q.recv_wall
    if trough is None or rb < trough:
        trough, trough_wall = rb, q.recv_wall

    # 5 lock and ratchet: the stop never moves down and never sits below the hard stop
    old_stop = m.stop_px
    stop = p.hard_sl_px if old_stop is None else max(old_stop, p.hard_sl_px)
    armed = m.lock_armed
    if not armed and rb >= p.arm_px - PRICE_EPS:
        armed = True
        notes.append(LOCK_ARMED)
        stop = max(stop, p.floor_px)
    if armed:
        stop = max(stop, p.floor_px, p.be_px + prof.trail_retain * (peak - p.be_px))
    if old_stop is None or stop > old_stop:
        notes.append(STOP_MOVED)
    m = replace(m, stop_px=stop, lock_armed=armed, peak_rb=peak, peak_wall=peak_wall, trough_rb=trough,
                trough_wall=trough_wall)

    # 6 stop, debounced: stops are irreversible, so one snapshot is not enough unless the breach is deep
    if rb <= stop + PRICE_EPS:
        reason = "LOCK_STOP" if armed else "HARD_SL"
        if rb <= stop - prof.stop_confirm_bypass_pct / 100 * p.entry_avg + PRICE_EPS:
            return Exit(reason, PROTECTIVE, m, rb, thin, tuple(notes))
        if m.breach_first_mono is None:
            m = replace(m, breach_first_mono=now_mono, breach_quote_mono=q.recv_mono)
            return NoAction(m, rb, thin, tuple(notes) + (STOP_BREACH_PENDING,))
        held = now_mono - m.breach_first_mono
        if q.recv_mono > m.breach_quote_mono and held >= prof.stop_confirm_ms / 1000 - _TIME_EPS:
            return Exit(reason, PROTECTIVE, m, rb, thin, tuple(notes))
        if held >= prof.stop_confirm_max_ms / 1000 - _TIME_EPS:
            return _refresh(refresh, m, tuple(notes), rb, thin)        # a REST snapshot is a distinct snapshot
        return NoAction(m, rb, thin, tuple(notes))
    m = replace(m, breach_first_mono=None, breach_quote_mono=None)

    # 7 take profit
    if rb >= p.tp_px - PRICE_EPS:
        return Exit("TAKE_PROFIT", NORMAL, m, rb, thin, tuple(notes))
    # 8 time stop, wall clock; not once the lock is armed unless the profile says so
    if now_wall >= p.time_stop_at and (not armed or prof.time_stop_when_locked):
        return Exit("TIME_STOP", NORMAL, m, rb, thin, tuple(notes))
    # 9
    return NoAction(m, rb, thin, tuple(notes))


# ---------------------------------------------------------------------------------------------------------------
# §5.7 Exact pricing. Every limit is on the contract's tick grid. None means "no price": send no order.
# ---------------------------------------------------------------------------------------------------------------
BAND_REJECT_RE = re.compile(r"(?i)(price|lpp|band|range|circuit|protection|outside)")


def _band(band_idx: int) -> int:
    """The band index clamped to the BANDS range: an out-of-range index never blocks an exit."""
    return min(max(int(band_idx), 0), len(BANDS) - 1)


def entry_limit(q: Quote | None, tick: float, qty: int | None = None) -> float | None:
    """ENTRY BUY limit = min(ask + 2 ticks, floor_tick(mid x 1.01)).

    None (refuse) when a side is missing or the book is crossed, when that limit is below the ask, or, given qty,
    when the ask walk within the limit cannot fill qty. Gateway order: limit = entry_limit(q, tick) -> size() ->
    entry_limit(q, tick, sized.qty).
    """
    if q is None or not _positive(tick):
        return None
    ask, mid = q.ask, q.mid()
    if ask is None or mid is None:
        return None
    limit = min(floor_tick(ask + PRICE_EDGE_TICKS * tick, tick), floor_tick(mid * (1 + ENTRY_MAX_OVER_MID), tick))
    if limit < ask - PRICE_EPS:
        return None
    if qty is not None:
        if not (_plain_int(qty) and qty > 0) or walk(q.asks, qty, limit, Side.BUY).fillable_qty < qty:
            return None
    return limit


def sell_limit(ref_bid: float, sweep: float, tick: float, band_idx: int) -> float | None:
    """EXIT SELL limit at band b = max(tick, floor_tick(min(sweep - 2 ticks, ref_bid x (1 - BANDS[b]))),
    ceil_tick(ref_bid x (1 - MAX_EXIT_DISCOUNT))): never more than 12% under ref_bid (I13)."""
    if not (_positive(ref_bid) and _positive(sweep) and _positive(tick)):
        return None
    raw = min(sweep - PRICE_EDGE_TICKS * tick, ref_bid * (1 - BANDS[_band(band_idx)]))
    return max(tick, floor_tick(raw, tick), ceil_tick(ref_bid * (1 - MAX_EXIT_DISCOUNT), tick))


def exit_limit(q: Quote | None, qty: int, tick: float, band_idx: int, protective: bool = False) -> float | None:
    """EXIT SELL limit for qty at band band_idx, priced from q's bids.

    Fresh pricing (q at most 1 s old): ref_bid = best bid; sweep = the worst bid level needed to fill qty, or the
    lowest shown bid when depth is short. Protective pricing (no fresh quote: pass the last known quote):
    ref_bid = sweep = its best bid. The caller owns the band: start at 0 when fresh, at PROTECTIVE_BAND (3) when
    protective or after EOD_BACKSTOP. None when q has no bids: no order into an empty book.
    """
    if q is None or not q.bids:
        return None
    if protective:
        return sell_limit(q.bid, q.bid, tick, band_idx)
    if not (_plain_int(qty) and qty > 0):
        return None
    return sell_limit(q.bid, walk(q.bids, qty, None, Side.SELL).worst_px, tick, band_idx)


def cover_limit(q: Quote | None, qty: int, tick: float, band_idx: int, protective: bool = False) -> float | None:
    """COVER BUY limit (a bot-explained short only, §9.3), the exit ladder mirrored:
    min(ceil_tick(max(sweep_ask + 2 ticks, ref_ask x (1 + BANDS[b]))),
        floor_tick(ref_ask x (1 + MAX_EXIT_DISCOUNT)))."""
    if q is None or not q.asks or not _positive(tick):
        return None
    ref_ask = q.ask
    if protective:
        sweep = ref_ask
    elif _plain_int(qty) and qty > 0:
        sweep = walk(q.asks, qty, None, Side.BUY).worst_px
    else:
        return None
    up = max(sweep + PRICE_EDGE_TICKS * tick, ref_ask * (1 + BANDS[_band(band_idx)]))
    return min(ceil_tick(up, tick), floor_tick(ref_ask * (1 + MAX_EXIT_DISCOUNT), tick))


def selftest_limit(q: Quote | None, tick: float) -> float | None:
    """SELFTEST BUY limit = max(tick, floor_tick(best bid x 0.97)). None unless it sits strictly below the best bid
    (a tiny premium cannot be priced under the bid), so a self-test order can never match."""
    if q is None or q.bid is None or not _positive(tick):
        return None
    limit = max(tick, floor_tick(q.bid * SELFTEST_BID_FACTOR, tick))
    return limit if limit < q.bid - PRICE_EPS else None


def is_band_reject(msg: str | None) -> bool:
    """True for an exchange price-band / LPP style reject: the next attempt keeps its band."""
    return isinstance(msg, str) and BAND_REJECT_RE.search(msg) is not None


def band_reject_limit(prev_limit: float, fresh_bid: float | None, tick: float) -> float:
    """Re-price after a band reject: max(prev_limit, floor_tick(fresh_bid - 2 ticks)); prev_limit without a bid.

    Also never below ceil_tick(fresh_bid x (1 - MAX_EXIT_DISCOUNT)), sell_limit's own floor: under a bid of about
    Rs 0.83, bid - 2 ticks is more than 12% under the bid, and I13 bounds every exit limit there.
    """
    if not (_positive(fresh_bid) and _positive(tick)):
        return prev_limit
    return max(prev_limit, floor_tick(fresh_bid - PRICE_EDGE_TICKS * tick, tick),
               ceil_tick(fresh_bid * (1 - MAX_EXIT_DISCOUNT), tick))
