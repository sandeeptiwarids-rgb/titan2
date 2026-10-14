"""The single sizing function (spec §8.5, R7).

size() is applied last: the order is built from Sized.qty and nothing changes that quantity afterwards. An Intent
carries no quantity, and there is no scaling factor of any kind. Every term is a whole number of lots; the smallest
one binds, and a binding term below 1 lot refuses the trade (it never rounds up to 1).

A cap that is zero, negative, NaN, infinite, a bool or not a number refuses: it never means "no limit".

Two terms are stricter than the spec body (post-build review; both only ever lower the size):
- DEPTH counts only levels priced within LIQ_MAX_SLIP_VS_MID_PCT of the mid (near_qty). A thin stock option can
  show one real bid at the touch and stub bids far under it (25.00 and 0.05 under a 31.95 touch); counted by
  quantity alone they passed "3x the order on the bid side", although selling into them loses 20-99%.
- DAY_BUDGET is what the daily stop leaves after the day's realized loss and the stop-outs of the positions already
  open (open_risk_rs), and for a LIVE book also what the LIVE account's daily stop leaves. With only the book's day
  net, a second position, or a LIVE book whose sibling already lost most of the account's stop, could be sized so
  that ordinary stop-outs breach D3 or the account cap. single_leg.plan_entry passes them (review round 3; until
  then DAY_BUDGET ran as the spec's formula): the book's realized net and open_risk_rs (Engine.book_state) and, for
  a LIVE book, the same over the LIVE account. An open position counts by its stop-out measured from its entry
  (remaining_stop_risk), never by its MTM: a gain gives no room, realized or not. Counting an open gain as still to
  lose as well charged it twice, and refused a safe second entry once the first position was up more than
  DAILY_STOP_RS less both planned stop-outs (review round 3 follow-up).
"""

from __future__ import annotations

import math
from math import floor
from typing import Sequence

from titan2.charges import RateSet, round_trip_charges
from titan2.model import PRICE_EPS, ExitProfile, Instrument, Level, Policy, Quote, Refusal, Sized
from titan2.settings import LIQ_DEPTH_LEVELS, Settings

__all__ = ["near_qty", "remaining_stop_risk", "size", "top_qty"]


def top_qty(levels: Sequence[Level], n: int = LIQ_DEPTH_LEVELS) -> int:
    """Total quantity on the best n levels of one side of the book."""
    return sum(lv.qty for lv in levels[:n])


def near_qty(levels: Sequence[Level], mid: float | None, max_dev_pct: float, n: int = LIQ_DEPTH_LEVELS) -> int:
    """Quantity on the best n levels of one side of the book priced within max_dev_pct % of mid (for bids: at or
    above mid x (1 - pct/100); for asks: at or below mid x (1 + pct/100)). No mid (a side missing or a crossed
    book) counts nothing. G19 must use the same rule (risk.liquidity)."""
    if not (_number(mid) and mid > 0 and _number(max_dev_pct) and max_dev_pct >= 0):
        return 0
    band = mid * max_dev_pct / 100.0
    return sum(lv.qty for lv in levels[:n] if abs(lv.price - mid) <= band + PRICE_EPS)


def _number(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def remaining_stop_risk(risk_at_entry_rs: float | None, mtm_net: float | None,
                        stop_raise_rs: float | None = 0.0) -> float | None:
    """One position's part of size()'s open_risk_rs: what it loses if it stops out, measured from its entry. Its
    stop risk as sized at entry (Sized.per_lot_risk_rs x lots) less what its stop has been raised since (a lock or a
    trail: open qty x (stop - hard stop), stop_raise_rs), floored at 0; or the loss on it now (-MTM) when that is
    larger (a gap past its stop). An unrealized gain neither adds to it nor takes from it: the day budget credits no
    gain, so counting a gain as still to lose charged it twice (review round 3 follow-up). An entry still working
    counts in full (mtm_net 0). None when an input is unknown or malformed (a RECOVERED leg has no sizing plan): the
    caller then passes open_risk_rs=None and size() refuses."""
    if not (_number(risk_at_entry_rs) and risk_at_entry_rs >= 0 and _number(mtm_net)
            and _number(stop_raise_rs) and stop_raise_rs >= 0):
        return None
    return max(0.0, risk_at_entry_rs - stop_raise_rs, -mtm_net)


def size(inst: Instrument, q: Quote, limit_px: float, profile: ExitProfile, s: Settings,
         realized_net: float | None, rates: RateSet, policy: Policy, *, open_risk_rs: float | None = 0.0,
         live_book: bool = False, account_realized_net: float | None = None,
         account_open_risk_rs: float | None = 0.0) -> Sized | Refusal:
    """Lots for one entry at limit_px: the smallest of the RISK, DEPTH, LOTS, UNITS, PREMIUM and (FULL books only)
    DAY_BUDGET terms.

    realized_net: the book's realized net today (with nothing open, the spec's day net); None (the day's P&L is
      unknown: an MTM unknown) refuses a FULL book.
    open_risk_rs: Σ remaining_stop_risk() over the book's open (and entering) positions, in rupees: each one's
      stop-out measured from its entry, or its loss now when larger. None (unknown) refuses a FULL book.
    live_book: the book trades the LIVE account. Then account_realized_net (Σ realized over every LIVE book today)
      and account_open_risk_rs (the same Σ of open stop-outs over every LIVE book) are required, and the
      LIVE_ACCOUNT_DAILY_STOP_RS room caps DAY_BUDGET too.
    DAY_BUDGET = floor(min(DAILY_STOP_RS + min(realized_net, 0) - open_risk_rs, LIVE account room) / per_lot_risk),
    so a stop-out of this trade together with every open one cannot breach D3 (or the LIVE account cap). A gain,
    realized or open, never adds room.

    Beyond the spec body, inputs it cannot size safely refuse instead of raising: a bad setting, lot, limit,
    profile stop or policy, or a non-finite realized net or open risk.
    """
    caps = (s.MAX_LOTS, s.MAX_UNITS, s.MAX_LOSS_PER_TRADE_RS, s.MAX_PREMIUM_PER_TRADE_RS)
    if any(isinstance(c, bool) or not isinstance(c, (int, float)) or not math.isfinite(c) or c <= 0 for c in caps):
        return Refusal("SIZE", "INVALID_OR_ZERO_CAP")       # 0 / negative / NaN / non-numeric never means "disabled"
    if not (all(_number(x) and x >= 0 for x in (s.MIN_PREMIUM_RS, s.STOP_SLIP_ALLOW_PCT, s.DAILY_STOP_RS,
                                                 s.LIVE_ACCOUNT_DAILY_STOP_RS))
            and _number(s.LIQ_DEPTH_MULT) and s.LIQ_DEPTH_MULT > 0
            and _number(s.LIQ_MAX_SLIP_VS_MID_PCT) and s.LIQ_MAX_SLIP_VS_MID_PCT > 0):
        return Refusal("SIZE", "INVALID_SETTING")
    lot = inst.lot_size
    if isinstance(lot, bool) or not isinstance(lot, int) or lot <= 0:
        return Refusal("SIZE", "LOT_INVALID")
    if not (_number(limit_px) and limit_px > 0):
        return Refusal("SIZE", "LIMIT_INVALID")
    if q.ask is None or min(q.ask, limit_px) < s.MIN_PREMIUM_RS:
        return Refusal("SIZE", "PREMIUM_FLOOR")             # S1
    if not (_number(profile.hard_sl_pct) and profile.hard_sl_pct > 0):
        return Refusal("SIZE", "PROFILE_INVALID")
    if policy not in (Policy.FULL, Policy.QUALITY):         # str Enum: the plain "FULL"/"QUALITY" match too
        return Refusal("SIZE", "POLICY_INVALID")
    per_lot_risk = lot * limit_px * (profile.hard_sl_pct + s.STOP_SLIP_ALLOW_PCT) / 100 + round_trip_charges(
        limit_px, lot, rates)
    mid, pct = q.mid(), s.LIQ_MAX_SLIP_VS_MID_PCT
    terms = {"RISK": floor(s.MAX_LOSS_PER_TRADE_RS / per_lot_risk),
             "DEPTH": floor(min(near_qty(q.asks, mid, pct), near_qty(q.bids, mid, pct))
                            / (s.LIQ_DEPTH_MULT * lot)),    # entry AND exit side, near the mid only
             "LOTS": int(s.MAX_LOTS),
             "UNITS": floor(s.MAX_UNITS / lot),
             "PREMIUM": floor(s.MAX_PREMIUM_PER_TRADE_RS / (lot * limit_px))}
    if policy == Policy.FULL:
        if realized_net is None or not _number(realized_net):
            return Refusal("SIZE", "DAY_PNL_UNKNOWN")
        if not (_number(open_risk_rs) and open_risk_rs >= 0):
            return Refusal("SIZE", "OPEN_RISK_UNKNOWN")
        room = s.DAILY_STOP_RS + min(realized_net, 0.0) - open_risk_rs
        if live_book is not False:
            if not (_number(account_realized_net) and _number(account_open_risk_rs) and account_open_risk_rs >= 0):
                return Refusal("SIZE", "LIVE_DAY_PNL_UNKNOWN")
            room = min(room, s.LIVE_ACCOUNT_DAILY_STOP_RS + min(account_realized_net, 0.0) - account_open_risk_rs)
        terms["DAY_BUDGET"] = floor(room / per_lot_risk)    # stop-outs of this and every open trade cannot breach D3
    binding = min(terms, key=terms.get)
    lots = terms[binding]
    if lots < 1:
        return Refusal("SIZE", binding)                     # skip if 1 lot breaches any cap
    return Sized(qty=lots * lot, lots=lots, binding=binding, per_lot_risk_rs=per_lot_risk)
