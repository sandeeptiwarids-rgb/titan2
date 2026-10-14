"""Structure and leg state machine, close() and close_external() (spec §5.3; DETAIL §5.3, §9.3-9.6).

Phase 1 has one LONG leg per structure; the leg's state follows the structure's (a RESERVED structure has no leg
yet). Every transition is checked against STRUCT_TRANSITIONS / LEG_TRANSITIONS (IllegalTransition otherwise),
persisted, and written to the events table inside the caller's transaction (core thread, store.tx()).

  reserve()            -                  -> RESERVED     pre-reserve gates passed; the row holds the (book, underlying)
                                                          slot (unique index, I5) and counts toward MAX_POSITIONS
  void()               RESERVED, ENTERING -> VOID         refusal, TTL or quote timeout; an entry that filled nothing
  begin_entry()        RESERVED           -> ENTERING     leg + ENTRY (or SELFTEST) order + Place + Subscribe(full)
  on_entry_terminal()  ENTERING           -> OPEN | VOID  ExitParams frozen from the fills, stop = hard_sl,
                                                          risk_basis = premium paid; no top-up order
  begin_exit()         OPEN               -> EXITING      the first reason wins (EXITING again can only raise urgency)
  to_verify_flat()     EXITING            -> VERIFY_FLAT  the bot's own open qty is 0
  close()              VERIFY_FLAT        -> CLOSED       post-fill broker net confirmed; a confirmed residual is only
                                                          recorded (never sold)
  close_external()     OPEN, EXITING      -> CLOSED_EXTERNAL  recon confirmed the qty left the account; also from
                                                          ENTERING for a structure left over from an earlier session

close() and close_external() are the only callers of ledger.build_trade_row (net from charges.net_pnl over the
effective fills) and the only writers of `trades`. Each books inside its own terminal transition, in one
transaction (a savepoint inside the core's), so P&L is booked once (I10: trades PK plus the immutability trigger).
Quantities are never stored on a leg: open_qty is derived from its orders' broker-confirmed fills (model.Leg).
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Iterable, Mapping, MutableMapping, Sequence

from titan2.charges import AUTO_SQUAREOFF, OrderFills, OrderPart, RateSet, rates_for, round_money
from titan2.clock import session_of
from titan2.core import orders as om
from titan2.core.events import Alert, Subscribe
from titan2.core.exits import exit_params_from_fills, exit_params_problems
from titan2.core.orders import Effects, IllegalTransition, whole_ms
from titan2.core.recon import ExternalClose, ExternalFill, HaltRequest
from titan2.core.risk import HALT_RULES
from titan2.ledger import build_trade_row, value_order, worse_status
from titan2.model import (
    EXIT_REASONS, MIS, REGULAR, Account, DecisionQuote, ExitProfile, Fill, Instrument, Intent, InvariantViolation,
    Leg, LegState, Order, OrderState, PnlStatus, Policy, Role, Side, Structure, StructState, StructureType, Urgency,
    external_order_pk,
)

from titan2.store import db_json

if TYPE_CHECKING:
    from titan2.store import Store

__all__ = [
    "LEG_TRANSITIONS", "STRUCT_TRANSITIONS", "begin_entry", "begin_exit", "check_leg", "check_structure", "close",
    "close_external", "mark_unresolved", "on_entry_terminal", "reserve", "to_verify_flat", "void",
]

_ST = StructState
STRUCT_TRANSITIONS: Mapping[StructState, frozenset[StructState]] = MappingProxyType({
    _ST.RESERVED: frozenset({_ST.ENTERING, _ST.VOID}),
    # ENTERING -> CLOSED_EXTERNAL only for a structure left over from an earlier session (§9.6 CLOSE_UNRESOLVED,
    # or the simulated square-off of a leg whose entry never advanced); a current entry resolves to OPEN or VOID.
    _ST.ENTERING: frozenset({_ST.OPEN, _ST.VOID, _ST.CLOSED_EXTERNAL}),
    _ST.OPEN: frozenset({_ST.EXITING, _ST.CLOSED_EXTERNAL}),
    _ST.EXITING: frozenset({_ST.VERIFY_FLAT, _ST.CLOSED_EXTERNAL}),
    _ST.VERIFY_FLAT: frozenset({_ST.CLOSED}),
})
LEG_TRANSITIONS: Mapping[LegState, frozenset[LegState]] = MappingProxyType({
    LegState.PLANNED: frozenset({LegState.ENTERING, LegState.VOID}),          # phase-2 basket legs
    **{LegState(frm.value): frozenset(LegState(t.value) for t in to)
       for frm, to in STRUCT_TRANSITIONS.items() if frm is not _ST.RESERVED},
})
_TYPE_OPT = {StructureType.LONG_CE: "CE", StructureType.LONG_PE: "PE"}


def _positive(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0



def check_structure(frm: StructState | str, to: StructState | str, what: str = "structure") -> None:
    """IllegalTransition unless STRUCT_TRANSITIONS allows frm -> to."""
    if StructState(to) not in STRUCT_TRANSITIONS.get(StructState(frm), frozenset()):
        raise IllegalTransition(what, frm, f"-> {to}")


def check_leg(frm: LegState | str, to: LegState | str, what: str = "leg") -> None:
    """IllegalTransition unless LEG_TRANSITIONS allows frm -> to."""
    if LegState(to) not in LEG_TRANSITIONS.get(LegState(frm), frozenset()):
        raise IllegalTransition(what, frm, f"-> {to}")


# ---------------------------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------------------------
def _legs_of(st: Structure, legs: Iterable[Leg]) -> list[Leg]:
    """legs must be exactly the structure's legs (bound to the engine's order index)."""
    legs = sorted(legs, key=lambda lg: lg.leg_no)
    if sorted(lg.leg_id for lg in legs) != sorted(st.legs) or any(lg.structure_id != st.structure_id for lg in legs):
        raise ValueError(f"structure {st.structure_id}: legs {[lg.leg_id for lg in legs]} are not its legs "
                         f"{st.legs}")
    return legs


def _check(st: Structure, legs: Sequence[Leg], to: StructState) -> None:
    check_structure(st.state, to, f"structure {st.structure_id}")
    for leg in legs:
        check_leg(leg.state, LegState(to.value), f"leg {leg.leg_id}")


def _set_state(store: Store, st: Structure, legs: Sequence[Leg], to: StructState, *,
               exiting_since_wall: datetime | None = None) -> None:
    for leg in legs:
        leg.state = LegState(to.value)
        store.update_leg(leg, exiting_since_wall=exiting_since_wall)
    st.state = to
    store.update_structure(st)


def _working(leg: Leg) -> list[Order]:
    return [o for o in leg.leg_orders() if o.role != Role.EXTERNAL and not o.is_terminal]


def _event(store: Store, kind: str, st: Structure, now_mono: float, now_wall: datetime, payload: Mapping,
           severity: str = "INFO", leg_id: int | None = None) -> None:
    store.append_event(kind, wall=now_wall, mono=now_mono, payload=payload, severity=severity, account=st.account,
                       book=st.book, strategy=st.strategy, structure_id=st.structure_id, leg_id=leg_id,
                       intent_id=st.intent.intent_id)


def _pnl_payload(row: Mapping) -> dict:
    return {k: row[k] for k in ("gross", "charges", "net", "net_pct", "pnl_status", "rates_version", "qty",
                                "entry_avg", "exit_avg", "exit_reason", "n_executed_orders")}


def _closed_alert(st: Structure, row: Mapping) -> Alert:
    text = (f"{st.book} {row['symbol']} closed ({row['exit_reason']}): net Rs {row['net']:,.2f} "
            f"[{row['pnl_status']}]")
    return Alert("INFO", "POSITION_CLOSED", text, f"closed:{st.structure_id}")


# ---------------------------------------------------------------------------------------------------------------
# Entry side
# ---------------------------------------------------------------------------------------------------------------
def reserve(store: Store, intent: Intent, *, book: str, account: Account | str, policy: Policy | str,
            now_mono: float, now_wall: datetime, is_placebo: bool | None = None,
            parent_structure_id: int | None = None, blocked_by: str | None = None) -> Structure:
    """- -> RESERVED (the intents row must exist). The partial unique indexes refuse a second non-terminal
    structure on (book, underlying) and on (LIVE, underlying) with InvariantViolation I5."""
    st = Structure(structure_id=0, session=session_of(now_wall), book=book, account=Account(account),
                   policy=Policy(policy), strategy=intent.strategy, strategy_version=intent.strategy_version,
                   intent=intent, type=StructureType(intent.structure), underlying=intent.underlying,
                   state=StructState.RESERVED, legs=[],
                   is_placebo=bool(intent.is_placebo if is_placebo is None else is_placebo),
                   parent_structure_id=parent_structure_id, blocked_by=blocked_by, decision=None, placebo_status=None,
                   exit_params=None, risk_basis_rs=None, exit_reason=None, void_reason=None,
                   reserved_wall=whole_ms(now_wall))
    with store.tx():
        store.insert_structure(st)
        _event(store, "STRUCTURE_RESERVED", st, now_mono, now_wall,
               {"underlying": st.underlying, "policy": str(st.policy), "type": str(st.type),
                "is_placebo": st.is_placebo, "parent_structure_id": parent_structure_id, "blocked_by": blocked_by})
    return st


def void(store: Store, st: Structure, legs: Iterable[Leg], reason: str, *, now_mono: float,
         now_wall: datetime) -> Effects:
    """RESERVED/ENTERING -> VOID with void_reason (gate name, "ENTRY_NO_FILL", "REJECTED:<msg>", "RESTART", ...).
    closed_ts starts the cooldown (VOID, or REJECT for a 'REJECTED...' reason). A leg holding a fill, or with an
    order still in flight, cannot be voided."""
    legs = _legs_of(st, legs)
    _check(st, legs, StructState.VOID)
    for leg in legs:
        busy = _working(leg)
        if busy:
            raise IllegalTransition(f"leg {leg.leg_id}", leg.state, "-> VOID", f"order {busy[0].order_pk} is in flight")
        if leg.entry_filled > 0:
            raise IllegalTransition(f"leg {leg.leg_id}", leg.state, "-> VOID", f"it holds {leg.entry_filled} filled")
    with store.tx():
        st.void_reason = str(reason)[:300]
        st.closed_wall = whole_ms(now_wall)
        _set_state(store, st, legs, StructState.VOID)
        _event(store, "STRUCTURE_VOID", st, now_mono, now_wall, {"reason": st.void_reason},
               leg_id=legs[0].leg_id if legs else None)
    return Effects()


def begin_entry(store: Store, orders: MutableMapping[str, Order], st: Structure, *, inst: Instrument, qty: int,
                limit_px: float, decision_quote: DecisionQuote, decision: Mapping | None, now_mono: float,
                now_wall: datetime, role: Role | str = Role.ENTRY,
                live_allowed: bool = False) -> tuple[Leg, Order, Effects]:
    """RESERVED -> ENTERING: the leg row, its ENTRY (or SELFTEST) order as SUBMITTING, decision_json, and the
    commands [Place, Subscribe(token, "full")]. orders is the engine's order index; the new leg is bound to it.
    On any refusal (orders.create_order's I4/I12/I13/I14 checks) nothing is written and the structure is left
    RESERVED in memory too, so the caller can void it."""
    role = Role(role)
    if role not in (Role.ENTRY, Role.SELFTEST):
        raise ValueError(f"an entry is placed with role ENTRY or SELFTEST, not {role}")
    if st.legs:
        raise IllegalTransition(f"structure {st.structure_id}", st.state, "begin_entry", f"already has legs {st.legs}")
    _check(st, [], StructState.ENTERING)
    if inst.underlying != st.underlying or inst.opt_type != _TYPE_OPT.get(StructureType(st.type)):
        raise ValueError(f"structure {st.structure_id} ({st.type} on {st.underlying}) cannot hold {inst.symbol}")
    saved = (list(st.legs), st.decision, st.state)
    try:
        with store.tx():
            leg = Leg(leg_id=0, structure_id=st.structure_id, leg_no=1, account=Account(st.account), book=st.book,
                      session=st.session, seq_no=store.next_leg_seq(st.account, st.session), inst=inst, side="LONG",
                      state=LegState.ENTERING)
            store.insert_leg(leg)
            leg.bind_orders(orders)
            st.legs.append(leg.leg_id)
            # held as the stored JSON reads back, so a reload reproduces it exactly
            st.decision = None if decision is None else json.loads(db_json(decision))
            st.state = StructState.ENTERING
            store.update_structure(st)
            _event(store, "STRUCTURE_ENTERING", st, now_mono, now_wall,
                   {"symbol": inst.symbol, "role": str(role), "qty": qty, "limit": limit_px, "seq_no": leg.seq_no},
                   leg_id=leg.leg_id)
            order, fx = om.create_order(store, orders, leg, role=role, qty=qty, limit_px=limit_px,
                                        now_mono=now_mono, now_wall=now_wall, decision=decision_quote,
                                        live_allowed=live_allowed)
    except BaseException:
        st.legs, st.decision, st.state = saved
        raise
    fx.commands.append(Subscribe((inst.token,), "full"))
    return leg, order, fx


def on_entry_terminal(store: Store, st: Structure, leg: Leg, *, profile: ExitProfile, now_mono: float,
                      now_wall: datetime, rates: RateSet | None = None) -> Effects:
    """ENTERING -> OPEN or VOID once the entry order is terminal (call it when that order's Effects.became_terminal).

    Nothing filled: VOID with "REJECTED:<msg>" or "ENTRY_NO_FILL". Filled (fully or partly): OPEN with ExitParams
    from the entry fills (E, Q = filled, C_in = their charges), stop = hard_sl_px, risk_basis = premium paid; no
    top-up order. Levels that cannot be computed or fail I8 send the leg straight to EXITING(ANOMALY_EXIT); a filled
    SELFTEST goes to EXITING(SELFTEST_FILLED). A PAPER:<s> entry's placebo was opened by the gateway at the decision
    (gateway._open_placebo, review F-18).
    """
    legs = _legs_of(st, [leg])
    if st.state is not StructState.ENTERING or leg.state is not LegState.ENTERING:
        raise IllegalTransition(f"structure {st.structure_id}", st.state, "entry terminal", f"leg is {leg.state}")
    busy = _working(leg)
    if busy:
        raise IllegalTransition(f"leg {leg.leg_id}", leg.state, "entry terminal",
                                f"order {busy[0].order_pk} is still {busy[0].state}")
    entries = [o for o in leg.leg_orders() if o.role in (Role.ENTRY, Role.SELFTEST)]
    filled = [o for o in entries if o.filled_qty > 0]
    if not filled:
        last = entries[-1] if entries else None
        reason = (f"REJECTED:{last.status_msg or ''}" if last is not None and last.state is OrderState.REJECTED
                  else "ENTRY_NO_FILL")
        return void(store, st, legs, reason, now_mono=now_mono, now_wall=now_wall)

    r = rates if rates is not None else rates_for(st.session)
    fills = store.effective_fills([o.order_pk for o in filled])
    by_pk: dict[str, list[Fill]] = {}
    for f in fills:
        by_pk.setdefault(f.order_pk, []).append(f)
    values = [value_order(o.order_pk, o.role, o.side, o.filled_qty, o.limit_px, o.external_kind,
                          by_pk.get(o.order_pk, ())) for o in filled]
    qty = sum(v.filled_qty for v in values)
    premium = math.fsum(v.value for v in values)
    entry_wall = whole_ms(min(f.wall for f in fills) if fills else (leg.last_fill_wall or now_wall))
    params, problems = None, []
    priced = [v.order_fills() for v in values]
    if any(p is None for p in priced):
        problems = ["an entry fill has no price"]
    else:
        try:
            params = exit_params_from_fills(profile, priced, r, entry_wall)
            problems = exit_params_problems(params)
        except (TypeError, ValueError) as exc:
            problems = [f"exit params: {exc}"]
    with store.tx():
        st.exit_params = params
        st.risk_basis_rs = round(premium, 2)
        st.opened_wall = entry_wall
        leg.entry_fill_wall = entry_wall
        leg.stop_px = params.hard_sl_px if params is not None else None
        _set_state(store, st, legs, StructState.OPEN)
        _event(store, "POSITION_OPENED", st, now_mono, now_wall,
               {"symbol": leg.inst.symbol, "qty": qty, "entry_avg": round(premium / qty, 4) if qty else None,
                "risk_basis_rs": st.risk_basis_rs, "exit_params": params, "problems": problems,
                "partial": qty < sum(o.qty for o in entries)}, "CRITICAL" if problems else "INFO", leg.leg_id)
    text = f"{st.book} {leg.inst.symbol} opened: {qty} @ {premium / qty:.2f}"
    fx = Effects(commands=[Alert("INFO", "POSITION_OPENED", text, f"opened:{st.structure_id}")])
    selftest = any(o.role is Role.SELFTEST for o in filled)
    if selftest:
        fx.commands.append(Alert("CRITICAL", "SELFTEST_FILLED", f"{text}: the self-test filled; exiting now",
                                 f"selftest_filled:{st.structure_id}"))
    if problems:
        fx.commands.append(Alert("CRITICAL", "EXIT_PARAMS_INVALID", f"{text}; exiting: {'; '.join(problems)}",
                                 f"exit_params:{st.structure_id}"))
    if selftest or problems:
        begin_exit(store, st, leg, "SELFTEST_FILLED" if selftest else "ANOMALY_EXIT", Urgency.PROTECTIVE,
                   now_mono=now_mono, now_wall=now_wall)
    return fx


# ---------------------------------------------------------------------------------------------------------------
# Exit side
# ---------------------------------------------------------------------------------------------------------------
def begin_exit(store: Store, st: Structure, leg: Leg, reason: str, urgency: Urgency | str, *, now_mono: float,
               now_wall: datetime, quote: Mapping | None = None) -> bool:
    """OPEN -> EXITING with a frozen reason (decide()'s Exit, a forced flag, recon PARTIAL_EXTERNAL ->
    ANOMALY_EXIT, RECOVERY_FLATTEN). Returns True on that transition, False when nothing moved:
    - EXITING: the first reason stays; the only change is NORMAL -> PROTECTIVE urgency;
    - ENTERING: it cannot exit before its entry resolves (the caller keeps the forced flag; decide() rule 0 exits
      it once OPEN);
    - VERIFY_FLAT or terminal: already flat.
    It never raises for the leg's state, so a FLATTEN_ALL or KILL that walks every leg cannot be rolled back by a
    leg that is entering or already flat."""
    if reason not in EXIT_REASONS:
        raise ValueError(f"unknown exit reason {reason!r}")
    urgency = Urgency(urgency)
    legs = _legs_of(st, [leg])
    if st.state is StructState.EXITING and leg.state is LegState.EXITING:
        if urgency is Urgency.PROTECTIVE and leg.exit_urgency is not Urgency.PROTECTIVE:
            with store.tx():
                leg.exit_urgency = Urgency.PROTECTIVE
                store.update_leg(leg)
                _event(store, "EXIT_DECISION", st, now_mono, now_wall,
                       {"reason": reason, "urgency": str(urgency), "frozen_reason": leg.exit_reason,
                        "escalated": True}, leg_id=leg.leg_id)
        return False
    if st.state is not StructState.OPEN or leg.state is not LegState.OPEN:
        return False
    _check(st, legs, StructState.EXITING)
    with store.tx():
        leg.exit_reason = leg.exit_reason or reason
        st.exit_reason = st.exit_reason or reason
        leg.exit_urgency = urgency
        leg.exiting_since_mono = now_mono
        _set_state(store, st, legs, StructState.EXITING, exiting_since_wall=now_wall)
        _event(store, "EXIT_DECISION", st, now_mono, now_wall,
               {"reason": leg.exit_reason, "urgency": str(urgency), "quote": dict(quote) if quote else None},
               "WARN" if urgency is Urgency.PROTECTIVE else "INFO", leg.leg_id)
    return True


def _own_net(leg: Leg) -> int:
    """Σ BUY fills − Σ SELL fills over every order of the leg, COVER buys included (open_qty leaves covers out, so
    the exit loop never sells a cover again). 0 when the leg's own fills are flat."""
    return sum(o.filled_qty if o.side == Side.BUY else -o.filled_qty for o in leg.leg_orders())


def to_verify_flat(store: Store, st: Structure, leg: Leg, *, now_mono: float, now_wall: datetime) -> None:
    """EXITING -> VERIFY_FLAT once the bot holds nothing it could still sell (open_qty <= 0) and no bot order is in
    flight. The exit loop then asks recon.verify_flat for a post-fill broker read (§9.4) and calls close() on its
    CLOSED/RESIDUAL outcome. An oversold leg (open_qty < 0) is let through, CRITICAL, so verify_flat can find the
    short and the cover path (§9.3) can repair it; the runtime I3 check reports it too."""
    legs = _legs_of(st, [leg])
    _check(st, legs, StructState.VERIFY_FLAT)
    busy = _working(leg)
    if busy:
        raise IllegalTransition(f"leg {leg.leg_id}", leg.state, "-> VERIFY_FLAT", f"order {busy[0].order_pk} in flight")
    q = leg.open_qty
    if q > 0:
        raise IllegalTransition(f"leg {leg.leg_id}", leg.state, "-> VERIFY_FLAT", f"open qty is {q}")
    with store.tx():
        _set_state(store, st, legs, StructState.VERIFY_FLAT)
        _event(store, "VERIFY_FLAT", st, now_mono, now_wall,
               {"outcome": "STARTED", "symbol": leg.inst.symbol, "open_qty": q}, "CRITICAL" if q < 0 else "INFO",
               leg.leg_id)


def close(store: Store, st: Structure, legs: Iterable[Leg], *, now_mono: float, now_wall: datetime,
          residual_qty: int = 0, mode: str | None = None, rates: RateSet | None = None) -> tuple[dict, Effects]:
    """VERIFY_FLAT -> CLOSED: book the trade from the effective fills (ledger.build_trade_row -> charges.net_pnl)
    and insert it in the same transaction as the transition. residual_qty > 0 records a confirmed broker residual
    (recon.verify_flat RESIDUAL; its MISMATCH halt comes with that decision); it is never sold. mode is the
    strategy mode written on the ledger row (default: the account). Returns (trades row, Effects).

    Refused (I3) while the bot still holds sellable qty or its own fills show an uncovered short. Fills that do
    not balance otherwise (a COVER bought back a short the DB never saw sold) close with the ledger's UNRESOLVED."""
    legs = _legs_of(st, legs)
    _check(st, legs, StructState.CLOSED)
    if not (isinstance(residual_qty, int) and not isinstance(residual_qty, bool) and residual_qty >= 0):
        raise ValueError(f"residual_qty must be an int >= 0, got {residual_qty!r}")
    for leg in legs:
        busy = _working(leg)
        if busy:
            raise IllegalTransition(f"leg {leg.leg_id}", leg.state, "-> CLOSED", f"order {busy[0].order_pk} in flight")
        if leg.open_qty > 0 or _own_net(leg) < 0:
            raise InvariantViolation("I3", f"leg {leg.leg_id}: closing with open qty {leg.open_qty}, own net "
                                           f"{_own_net(leg)}", f"LEG:{leg.leg_id}")
    all_orders = [o for leg in legs for o in leg.leg_orders()]
    with store.tx():
        fills = store.effective_fills([o.order_pk for o in all_orders])
        row = build_trade_row(st, legs, all_orders, fills, exit_wall=whole_ms(now_wall),
                              pnl_status=st.pnl_status or PnlStatus.FINAL, mode=mode, rates=rates)
        store.insert_trade(row)
        if residual_qty:
            legs[0].broker_residual_qty = residual_qty
        st.closed_wall = whole_ms(now_wall)
        st.pnl_status = PnlStatus(row["pnl_status"])
        st.exit_reason = st.exit_reason or row["exit_reason"]
        _set_state(store, st, legs, StructState.CLOSED)
        _event(store, "POSITION_CLOSED", st, now_mono, now_wall,
               {"pnl": _pnl_payload(row), "residual_qty": residual_qty}, "WARN" if residual_qty else "INFO",
               legs[0].leg_id)
    return row, Effects(commands=[_closed_alert(st, row)])


def close_external(store: Store, orders: MutableMapping[str, Order], st: Structure, leg: Leg, ext: ExternalClose,
                   *, now_mono: float, now_wall: datetime, mode: str | None = None,
                   rates: RateSet | None = None) -> tuple[dict, Effects]:
    """OPEN/EXITING (or a stale ENTERING) -> CLOSED_EXTERNAL from a confirmed recon close (recon.ExternalClose).

    Writes one role-EXTERNAL SELL row per broker order in ext.fills (order_pk 'EX' + broker id; a broker order shared
    with another leg gets 'EX<id>:<leg_id>' without the id, and qty with no broker order 'EX:<leg_id>:<n>'), with
    CUM fills at the given prices (an unpriced fill gets no row: the ledger books it at 0, UNRESOLVED). A leg's
    share of a broker order that closed several legs (ExternalFill.part) is booked as a charges.OrderPart: that
    share of the order's value and charges, so the order's brokerage and square-off fee are booked once. Then books
    the trade with ext.pnl_status (FINAL / ESTIMATED / UNRESOLVED, only made worse) in the same transaction.
    BROKER_SQUAREOFF (AUTO_SQUAREOFF orders, Rs 50 + GST each in net_pnl) raises no halt; a MANUAL close returns
    HALT(MISMATCH, ACCOUNT, ACK) (recon raises it too; halts are sticky by scope and kind)."""
    legs = _legs_of(st, [leg])
    _check(st, legs, StructState.CLOSED_EXTERNAL)
    if ext.leg_id != leg.leg_id:
        raise ValueError(f"ExternalClose for leg {ext.leg_id} applied to leg {leg.leg_id}")
    held = leg.open_qty
    if not (isinstance(ext.qty, int) and not isinstance(ext.qty, bool) and 0 < ext.qty == held):
        raise InvariantViolation("I3", f"leg {leg.leg_id}: external close of {ext.qty!r} but the bot holds {held}",
                                 f"LEG:{leg.leg_id}")
    if sum(f.qty for f in ext.fills) != ext.qty or any(f.qty <= 0 for f in ext.fills):
        raise ValueError(f"leg {leg.leg_id}: external fills {[f.qty for f in ext.fills]} do not add up to {ext.qty}")
    reason = "BROKER_SQUAREOFF" if ext.external_kind == AUTO_SQUAREOFF else "CLOSED_EXTERNAL"
    with store.tx():
        made, parts = _external_orders(store, orders, leg, ext, now_wall)
        all_orders = leg.leg_orders()
        fills = store.effective_fills([o.order_pk for o in all_orders])
        status = worse_status(ext.pnl_status, st.pnl_status or PnlStatus.FINAL)
        row = build_trade_row(st, legs, all_orders, fills, exit_wall=whole_ms(now_wall), pnl_status=status,
                              exit_reason=reason, mode=mode, rates=rates, parts=parts)
        store.insert_trade(row)
        st.closed_wall = whole_ms(now_wall)
        st.pnl_status = PnlStatus(row["pnl_status"])
        st.exit_reason = st.exit_reason or reason
        leg.exit_reason = leg.exit_reason or reason
        _set_state(store, st, legs, StructState.CLOSED_EXTERNAL)
        _event(store, reason, st, now_mono, now_wall,
               {"pnl": _pnl_payload(row), "external_kind": ext.external_kind, "qty": ext.qty,
                "orders": [o.order_pk for o in made]}, "CRITICAL", leg.leg_id)
    fx = Effects(commands=[_closed_alert(st, row)])
    if reason == "CLOSED_EXTERNAL":
        text = f"{st.account} {leg.inst.symbol}: {ext.qty} closed outside the bot (structure {st.structure_id})"
        fx.halts.append(HaltRequest("MISMATCH", f"ACCOUNT:{st.account}", HALT_RULES["MISMATCH"].clears, text))
    return row, fx


def _external_orders(store: Store, orders: MutableMapping[str, Order], leg: Leg, ext: ExternalClose,
                     now_wall: datetime) -> tuple[list[Order], dict[str, OrderPart]]:
    by_id = {b.broker_order_id: b for b in ext.orders}
    groups: dict[str | None, list[ExternalFill]] = {}
    for f in ext.fills:
        groups.setdefault(f.broker_order_id, []).append(f)
    made: list[Order] = []
    parts: dict[str, OrderPart] = {}
    for n, (bid, fs) in enumerate(groups.items(), 1):
        b = by_id.get(bid) if bid else None
        if b is not None and (Side(b.side) is not Side.SELL or b.symbol != leg.inst.symbol):
            raise ValueError(f"leg {leg.leg_id} ({leg.inst.symbol}, LONG) cannot be closed by {b.side} order {bid} "
                             f"on {b.symbol}")
        qty = sum(f.qty for f in fs)
        pk, broker_id = _external_pk(store, leg, bid, n)
        part = _order_part(fs, ext.external_kind) if bid else None
        if part is not None:              # the share's price: its part of the order's value (paisa-exact)
            fs = [replace(fs[0], px=part.value / qty)]
            parts[pk] = part
        priced = [f for f in fs if _positive(f.px)]
        avg = (math.fsum(f.px * f.qty for f in priced) / qty) if len(priced) == len(fs) else None
        o = Order(order_pk=pk, leg_id=leg.leg_id, structure_id=leg.structure_id, account=Account(leg.account),
                  book=leg.book, role=Role.EXTERNAL, side=Side.SELL, symbol=leg.inst.symbol, qty=qty, limit_px=None,
                  attempt=0, band_idx=None, state=OrderState.COMPLETE, broker_order_id=broker_id,
                  broker_status=b.status if b is not None else None,
                  status_msg=b.status_msg if b is not None else None, external_kind=ext.external_kind,
                  filled_qty=qty, avg_fill_px=avg, decision=None,
                  created_wall=whole_ms(b.order_wall if b is not None and b.order_wall else now_wall),
                  terminal_wall=whole_ms(now_wall))
        store.insert_order(o, order_type=b.order_type if b is not None else "UNKNOWN",
                           validity=b.validity if b is not None else "UNKNOWN",
                           product=b.product if b is not None else MIS, variety=REGULAR)
        cum = 0
        for f in priced:
            cum += f.qty
            store.insert_fill(Fill(pk, f"cum:{cum}", "CUM", f.qty, float(f.px), whole_ms(f.wall or now_wall), None))
        orders[pk] = o
        leg.orders.append(pk)
        made.append(o)
    return made, parts


def _order_part(fs: Sequence[ExternalFill], external_kind: str) -> OrderPart | None:
    """This leg's share of a broker order several legs share (recon marks it on the one fill the leg takes)."""
    if len(fs) != 1 or fs[0].part is None:
        return None
    start, end, total, value = fs[0].part
    if end - start != fs[0].qty or not _positive(value):
        raise ValueError(f"external fill {fs[0]!r}: its part does not match its qty")
    return OrderPart(OrderFills(Side.SELL, total, round_money(value), external_kind), start, end)


def _external_pk(store: Store, leg: Leg, bid: str | None, n: int) -> tuple[str, str | None]:
    """(order_pk, broker_order_id) for one EXTERNAL row (orders.order_pk and orders.broker_order_id are unique)."""
    if not bid:
        return f"EX:{leg.leg_id}:{n}", None
    pk = external_order_pk(bid)
    if store.query_one("SELECT order_pk FROM orders WHERE order_pk=? OR broker_order_id=?", (pk, bid)) is None:
        return pk, bid
    return f"{pk}:{leg.leg_id}", None          # the same broker order also closed another leg on this symbol


def mark_unresolved(store: Store, st: Structure, *, now_mono: float, now_wall: datetime, detail: str = "") -> bool:
    """Flag the structure's P&L UNRESOLVED (an order report contradicted its fills: orders Effects.unresolved).
    A trade already booked keeps its ledger row (immutable); the flag and the event still record the doubt.
    Returns False when it was already UNRESOLVED."""
    if st.pnl_status is PnlStatus.UNRESOLVED:
        return False
    with store.tx():
        st.pnl_status = PnlStatus.UNRESOLVED
        store.update_structure(st)
        _event(store, "PNL_UNRESOLVED", st, now_mono, now_wall, {"detail": detail, "state": str(st.state)}, "WARN")
    return True
