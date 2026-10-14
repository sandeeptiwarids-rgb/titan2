"""Runtime invariants I1-I16, checked by the core loop after every event (spec §3.4; DETAIL §3.6).

The database enforces I4, I5, I7, I9 and I10 first (store raises InvariantViolation on a constraint failure); this
module re-checks them in memory and adds the rest:

  I1  core-only mutation                         store.assert_core (the check itself runs on the core thread)
  I2  order endpoints only on lane-live-orders   KiteBroker's thread assert (not visible from the core)
  I3  0 <= open_qty <= entry_filled              every non-terminal leg                         leg scope
  I4  <= 1 non-terminal bot order per leg        every leg                                      leg scope
  I5  <= 1 non-terminal structure per (book, underlying) and per (LIVE, underlying)            GLOBAL
  I6  exit qty <= open qty                       orders created by this event                   GLOBAL
  I7  ENTRY/SELFTEST/COVER BUY, EXIT SELL        every order                                    leg scope
  I8  OPEN legs have sane frozen ExitParams      every OPEN leg                                 leg scope
  I9  filled_qty monotonic, terminal immutable   DB triggers and orders.apply_cum
  I10 P&L booked once                            trades PK and its triggers
  I11 FULL books but RECOVERED: open + reserved <= MAX_POSITIONS_PER_BOOK, entries <= MAX_ENTRIES_PER_DAY  ACCOUNT
  I12 lot multiple, lot <= qty <= caps           orders created by this event                   GLOBAL
  I13 tick grid; ENTRY ask <= limit <= floor_tick(mid x 1.01); EXIT limit >= floor_tick(bid x 0.88)   GLOBAL
  I14 LIVE ENTRY/SELFTEST only with ALLOW_LIVE (ENTRY: mode LIVE, unlock, today's self-test)  GLOBAL
  I15 wall clock jumps > 2 s against monotonic   the engine's Timer: a WARN, never a violation
  I16 settings within LIMITS                     at start: a HALT(INVARIANT), never a violation loop

Two kinds of check (fail closed, but never wedge the core):
- Order checks (I6, I12-I14, and no order after STOP_SENDING or during shutdown) look only at orders created by
  the current event and ALWAYS raise: the event rolls back, so the order row and its Place never exist.
- State checks (I3-I5, I7, I8, I11) raise the first violation not handled yet today. on_invariant_violation records
  each (code, scope, detail) it handles (INVARIANT_VIOLATION event with handled=true), and a known one is not
  raised again: re-raising an unchanged persistent condition would roll back every later event and stop the exits.
  The detail names the leg, book or underlying and the counts, so a new or worsening violation is raised again.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime
from typing import TYPE_CHECKING, Iterable, Sequence

from titan2.core.exits import exit_params_problems
from titan2.core.risk import RECOVERED, book_policy
from titan2.model import (
    PRICE_EPS, ROLE_SIDE, Account, InvariantViolation, LegState, Policy, Role, floor_tick, is_tick_multiple,
    leg_fill_totals,
)
from titan2.settings import ENTRY_MAX_OVER_MID, MAX_EXIT_DISCOUNT, Settings
from titan2.store import assert_core

if TYPE_CHECKING:
    from titan2.core.engine import Engine

__all__ = ["CLOCK_JUMP_S", "check", "clock_jump", "known_key", "order_violations", "settings_problems",
           "state_violations"]

CLOCK_JUMP_S = 2.0


def check(eng: Engine) -> None:
    """Raise the first invariant violation this event caused (see the module docstring)."""
    assert_core("check_invariants")                                         # I1
    new = [eng.state.orders[pk] for pk in eng.new_order_pks() if pk in eng.state.orders]
    found = order_violations(eng, new)
    if found:
        raise found[0]
    for iv in state_violations(eng):
        if known_key(iv) not in eng.state.known_violations:
            raise iv


def known_key(iv: InvariantViolation) -> tuple[str, str, str]:
    """How a handled violation is remembered (and recognised when it persists)."""
    return str(getattr(iv, "code", "?")), str(getattr(iv, "scope", "GLOBAL")), str(getattr(iv, "detail", ""))


def _leg_scope(leg_id: int) -> str:
    return f"LEG:{leg_id}"


def state_violations(eng: Engine) -> list[InvariantViolation]:
    st = eng.state
    out: list[InvariantViolation] = []
    for leg in st.legs.values():
        legs_orders = [st.orders[pk] for pk in leg.orders if pk in st.orders]
        scope = _leg_scope(leg.leg_id)
        for o in legs_orders:                                               # I7
            if o.role != Role.EXTERNAL and ROLE_SIDE.get(Role(o.role)) != o.side:
                out.append(InvariantViolation("I7", f"order {o.order_pk}: {o.role} must be "
                                                    f"{ROLE_SIDE.get(Role(o.role))}, is {o.side}", scope))
        if leg.is_terminal:
            continue
        working = [o.order_pk for o in legs_orders if o.role != Role.EXTERNAL and not o.is_terminal]
        if len(working) > 1:                                                # I4
            out.append(InvariantViolation("I4", f"leg {leg.leg_id} has {len(working)} working orders {working}",
                                          scope))
        entry, exit_ = leg_fill_totals(legs_orders)
        if not 0 <= entry - exit_ <= entry:                                 # I3
            out.append(InvariantViolation("I3", f"leg {leg.leg_id}: entry filled {entry}, exit filled {exit_}",
                                          scope))
        struct = st.structures.get(leg.structure_id)
        if leg.state is LegState.OPEN and struct is not None:              # I8
            problems = exit_params_problems(struct.exit_params)
            if problems:
                out.append(InvariantViolation("I8", f"leg {leg.leg_id}: {'; '.join(problems)}", scope))
    live = st.live_structures()
    pairs = Counter((s.book, s.underlying) for s in live)                   # I5
    live_pairs = Counter(s.underlying for s in live if Account(s.account) is Account.LIVE)
    for (book, u), n in sorted(pairs.items()):
        if n > 1:
            out.append(InvariantViolation("I5", f"{n} non-terminal structures on ({book}, {u})", "GLOBAL"))
    for u, n in sorted(live_pairs.items()):
        if n > 1:
            out.append(InvariantViolation("I5", f"{n} non-terminal LIVE structures on {u}", "GLOBAL"))
    out.extend(_book_caps(eng, live))                                       # I11
    return out


def _book_caps(eng: Engine, live: Sequence) -> list[InvariantViolation]:
    s = eng.settings
    out = []
    books = sorted({x.book for x in live} | {x.book for x in eng.state.structures.values()
                                               if x.session == eng.session and x.opened_wall is not None})
    for book in books:
        # a RECOVERED book is FULL for the daily stop and the account guard, but recovery fills it, never an entry:
        #   the entry caps do not apply (a lost DB may hold more legs than MAX_POSITIONS_PER_BOOK)
        if book_policy(book) is not Policy.FULL or book.startswith(f"{RECOVERED}:"):
            continue
        acct = book.split(":", 1)[0]
        held = sum(1 for x in live if x.book == book)
        entries = sum(1 for x in eng.state.structures.values()
                      if x.book == book and x.session == eng.session and x.opened_wall is not None)
        if held > s.MAX_POSITIONS_PER_BOOK:
            out.append(InvariantViolation("I11", f"{book}: {held} open or reserved > MAX_POSITIONS_PER_BOOK "
                                                 f"{s.MAX_POSITIONS_PER_BOOK}", f"ACCOUNT:{acct}"))
        if entries > s.MAX_ENTRIES_PER_DAY:
            out.append(InvariantViolation("I11", f"{book}: {entries} entries today > MAX_ENTRIES_PER_DAY "
                                                 f"{s.MAX_ENTRIES_PER_DAY}", f"ACCOUNT:{acct}"))
    return out


def order_violations(eng: Engine, new_orders: Iterable) -> list[InvariantViolation]:
    """I6, I12, I13, I14 and the STOP_SENDING / shutdown rule for orders created by the current event."""
    s: Settings = eng.settings
    out: list[InvariantViolation] = []
    for o in new_orders:
        if o.role == Role.EXTERNAL:
            continue
        leg = eng.state.legs.get(o.leg_id)
        st = eng.state.structures.get(o.structure_id)
        what = f"{o.role} order {o.order_pk}"
        if leg is None or st is None:
            out.append(InvariantViolation("I3", f"{what}: its leg or structure is not in the engine"))
            continue
        role = Role(o.role)
        if eng.now_wall >= eng.eod.stop_sending:
            out.append(InvariantViolation("STOP_SENDING", f"{what} created at/after STOP_SENDING"))
        if eng.state.stopping and role in (Role.ENTRY, Role.SELFTEST):
            out.append(InvariantViolation("SHUTDOWN", f"{what} created while shutting down"))
        lot, tick = leg.inst.lot_size, leg.inst.tick
        if not (isinstance(o.qty, int) and o.qty >= lot and o.qty % lot == 0):                      # I12
            out.append(InvariantViolation("I12", f"{what}: qty {o.qty} is not a lot multiple >= lot {lot}"))
        elif role is Role.ENTRY and (o.qty > s.MAX_LOTS * lot or o.qty > s.MAX_UNITS):
            out.append(InvariantViolation("I12", f"{what}: qty {o.qty} above MAX_LOTS {s.MAX_LOTS} x lot {lot} "
                                                 f"or MAX_UNITS {s.MAX_UNITS}"))
        elif role is Role.SELFTEST and o.qty != lot:
            out.append(InvariantViolation("I12", f"{what}: a self-test is exactly 1 lot ({lot}), not {o.qty}"))
        limit = o.limit_px
        d = o.decision
        if limit is None or not is_tick_multiple(limit, tick):                                      # I13
            out.append(InvariantViolation("I13", f"{what}: limit {limit} is off the {tick} tick grid"))
        elif role is Role.ENTRY:
            ok = d is not None and _pos(d.ask) and _pos(d.mid) and \
                d.ask - PRICE_EPS <= limit <= floor_tick(d.mid * (1 + ENTRY_MAX_OVER_MID), tick) + PRICE_EPS
            if not ok:
                out.append(InvariantViolation("I13", f"{what}: limit {limit} outside [ask, floor_tick(mid x 1.01)] "
                                                     f"of its decision quote {d}"))
        elif role is Role.EXIT and d is not None and _pos(d.bid):
            if limit < floor_tick(d.bid * (1 - MAX_EXIT_DISCOUNT), tick) - PRICE_EPS:
                out.append(InvariantViolation("I13", f"{what}: limit {limit} more than "
                                                     f"{MAX_EXIT_DISCOUNT:.0%} under the bid {d.bid}"))
        if role is Role.EXIT:                                                                       # I6
            entry, exit_ = leg_fill_totals([eng.state.orders[pk] for pk in leg.orders if pk in eng.state.orders])
            if o.qty > entry - exit_ + o.filled_qty:
                out.append(InvariantViolation("I6", f"{what}: qty {o.qty} above the open qty {entry - exit_}"))
        if Account(o.account) is Account.LIVE and role in (Role.ENTRY, Role.SELFTEST):              # I14
            if not eng.live_allowed(st, role):
                out.append(InvariantViolation("I14", f"{what}: LIVE {role} without its permissions (ALLOW_LIVE, "
                                                     "mode LIVE, unlock, today's self-test)"))
    return out


def _pos(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def clock_jump(prev: tuple[float, datetime], cur: tuple[float, datetime]) -> float | None:
    """I15: how far the wall clock moved against the monotonic clock between two Timers, when more than 2 s."""
    d_mono = cur[0] - prev[0]
    d_wall = (cur[1] - prev[1]).total_seconds()
    jump = d_wall - d_mono
    return jump if abs(jump) > CLOCK_JUMP_S else None


def settings_problems(s: Settings) -> list[str]:
    """I16: the running Settings break LIMITS (impossible after settings.load(); checked once at start)."""
    return s.problems()
