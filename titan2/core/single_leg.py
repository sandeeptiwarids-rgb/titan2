"""The StructureHandler protocol and the phase-1 SingleLegHandler for LONG_CE / LONG_PE (spec §14, DETAIL §14.1).

The gateway, the engine's exit evaluation and the exit loop reach a structure only through this protocol, so a
phase-2 handler (titan2.multileg, registered by plugins.load_handlers) plugs in without touching them.

Handlers are pure: they read the HandlerCtx snapshot their caller builds and return contracts, plans, refusals and
exit decisions. The caller writes every row. A refusal never raises: whatever cannot be planned exactly is a Refusal
(fails closed), and an exception inside a gate becomes Refusal(<gate>, "GATE_ERROR") with its text in ctx.errors.

Phase 1 (one LONG leg):
  select          the contract for the intent's strike rule around the underlying LTP (<= UNDERLYING_MAX_AGE_S old)
  quote_symbols   (option, underlying); the caller adds a placebo's symbols from its own call
  plan_entry      post-quote gates G14-G21 (risk.post_quote_gates): limit (exits.entry_limit), lots (sizing.size, with
                  the book's realized net and open stop-outs and, for a LIVE book, the LIVE account's terms),
                  liquidity, worst case, funds; the plan carries the decision quote the order is checked against
  evaluate        exits.decide on the newest quote of the leg's symbol
  placebo_intent  the opposite option type on the same underlying, strike rule, expiry rule and snapshot
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from titan2.clock import session_of
from titan2.core import exits, risk, sizing
from titan2.core.exits import Decision, EodTimes
from titan2.core.risk import GateContext
from titan2.model import (
    Account, DecisionQuote, Direction, ExitParams, ExitProfile, Instrument, Intent, Leg, Policy, Quote, Refusal, Role,
    Side, Sized, Structure, StructState, StructureType, walk,
)
from titan2.settings import Settings

__all__ = [
    "ENTRY_STATUSES", "EntryPlan", "HandlerCtx", "LegPlan", "OPEN", "OrderSpec", "SingleLegHandler",
    "StructureHandler", "UNWIND", "VOID", "WAIT", "age_ms", "decision_quote", "instrument_from_json",
    "placebo_intent_id", "quote_json", "spot_quote",
]

CONTRACT = "CONTRACT"                # G15's gate name: every selection Refusal uses it
OPEN, VOID, UNWIND, WAIT = "OPEN", "VOID", "UNWIND", "WAIT"
ENTRY_STATUSES = (OPEN, VOID, UNWIND, WAIT)
MAX_AGE_MS = 86_400_000              # a quote age stored in a decision is capped at one day (never inf)
PLACEBO_SUFFIX = "-P"                # placebo intent_id = <parent intent_id>-P (intents.intent_id is the PK)

_OPT_TYPE = {StructureType.LONG_CE: "CE", StructureType.LONG_PE: "PE"}
_OPPOSITE = {StructureType.LONG_CE: StructureType.LONG_PE, StructureType.LONG_PE: StructureType.LONG_CE}
_DIRECTION = {StructureType.LONG_CE: Direction.UP, StructureType.LONG_PE: Direction.DOWN}


def _num(x: object) -> float | None:
    """A finite number or None (JSON refuses NaN and inf)."""
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


# ---------------------------------------------------------------------------------------------------------------
# Value types of the protocol
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class LegPlan:
    """One leg of an entry plan. decision_quote is the snapshot the order's I13 bounds and X2 walk come from."""

    inst: Instrument
    side: Side
    qty: int
    limit_px: float
    role: Role
    decision_quote: DecisionQuote | None = None


@dataclass(frozen=True, slots=True)
class EntryPlan:
    legs: tuple[LegPlan, ...]
    risk_basis_rs: float             # premium at the planned limit (the booked basis is the premium actually paid)
    decision: Mapping[str, Any]      # JSON: plan, decision quotes (structures.decision_json)


@dataclass(frozen=True, slots=True)
class OrderSpec:
    leg_id: int
    role: Role
    side: Side
    qty: int
    limit_px: float
    band_idx: int | None = None


@dataclass(frozen=True, slots=True)
class HandlerCtx:
    """What a handler call may read. The caller builds one per call; each method uses the fields it names.

    errors is a per-call scratch list: plan_entry appends the text of a GATE_ERROR so the caller can log it.
    """

    now_mono: float
    now_wall: datetime
    settings: Settings
    instruments: Any = None                          # today's InstrumentStore (select)
    quotes: Any = None                               # QuoteCache: the spot for select, newest quotes for evaluate
    gates: GateContext | None = None                 # plan_entry: the book's frozen gate snapshot
    profile: ExitProfile = ExitProfile()             # plan_entry: size() uses its hard stop
    legs: Mapping[int, Leg] = field(default_factory=dict)   # the engine's leg index (bound to its orders)
    refresh: Any = None                              # evaluate: REST refresh state per symbol (has .get(symbol))
    eod: EodTimes | None = None                      # evaluate: default EodTimes.for_session(today, settings)
    errors: list[str] = field(default_factory=list)

    @property
    def session(self) -> date:
        return session_of(self.now_wall)


@runtime_checkable
class StructureHandler(Protocol):
    """The only interface the gateway, the exit evaluation and the exit loop use for a structure type."""

    types: frozenset[StructureType]

    def select(self, intent: Intent, ctx: HandlerCtx) -> list[Instrument] | Refusal: ...

    def quote_symbols(self, intent: Intent, legs: Sequence[Instrument], placebo: bool) -> tuple[str, ...]: ...

    def plan_entry(self, intent: Intent, legs: Sequence[Instrument], quotes: Mapping[str, Quote | None],
                   ctx: HandlerCtx) -> EntryPlan | Refusal: ...

    def next_entry_orders(self, st: Structure, ctx: HandlerCtx) -> list[OrderSpec]: ...

    def on_entry_progress(self, st: Structure, ctx: HandlerCtx) -> str: ...

    def exit_params(self, st: Structure, ctx: HandlerCtx) -> ExitParams | None: ...

    def evaluate(self, st: Structure, quotes: Any, now_mono: float, now_wall: datetime, forced: str | None,
                 ctx: HandlerCtx) -> Decision: ...

    def exit_sequence(self, st: Structure) -> list[int]: ...

    def placebo_intent(self, intent: Intent, ctx: HandlerCtx) -> Intent | None: ...


# ---------------------------------------------------------------------------------------------------------------
# Helpers shared with the gateway and the exit loop
# ---------------------------------------------------------------------------------------------------------------
def age_ms(q: Quote, now_mono: float) -> int:
    """Receipt age in whole ms, capped (a future or absurd receipt time is stored as the cap, never as inf)."""
    a = q.age(now_mono)
    return int(round(a * 1000.0)) if math.isfinite(a) and a * 1000.0 < MAX_AGE_MS else MAX_AGE_MS


def decision_quote(q: Quote, qty: int, side: Side, now_mono: float) -> DecisionQuote:
    """The order's decision snapshot (§6.7): walk_px walks the side the order crosses, for qty, unbounded. exch_wall
    is the snapshot's exchange time: the order request carries it, so a broker that simulates fills knows which book
    the order was priced from, whatever the cache holds by the time the order reaches it."""
    side = Side(side)
    w = walk(q.asks if side is Side.BUY else q.bids, qty, None, side)
    return DecisionQuote(bid=q.bid, ask=q.ask, mid=_num(q.mid()), age_ms=age_ms(q, now_mono), src=q.source,
                         ltp=_num(q.ltp), walk_px=_num(w.avg_px), exch_wall=q.exch_wall)


def quote_json(q: Quote | None, now_mono: float) -> dict[str, Any] | None:
    """A quote as stored in decision_json: both books, age and source; LTP for information only."""
    if q is None:
        return None
    return {"symbol": q.symbol, "bid": q.bid, "ask": q.ask, "mid": _num(q.mid()), "ltp": _num(q.ltp),
            "age_ms": age_ms(q, now_mono), "src": q.source,
            "bids": [[lv.price, lv.qty] for lv in q.bids], "asks": [[lv.price, lv.qty] for lv in q.asks],
            "last_trade": q.last_trade_wall, "recv": q.recv_wall}


def instrument_from_json(d: Mapping[str, Any]) -> Instrument:
    """Rebuild an Instrument stored in decision_json (expiry as YYYY-MM-DD)."""
    values = dict(d)
    if isinstance(values.get("expiry"), str):
        values["expiry"] = date.fromisoformat(values["expiry"])
    return Instrument(**values)


def spot_quote(quotes: Any, underlying: str, settings: Settings, now_mono: float) -> Quote | None:
    """The underlying's quote if received at most UNDERLYING_MAX_AGE_S ago and carrying a positive LTP."""
    if quotes is None:
        return None
    q = quotes.fresh(underlying, settings.UNDERLYING_MAX_AGE_S, now_mono)
    if q is None or _num(q.ltp) is None or q.ltp <= 0:
        return None
    return q


def placebo_intent_id(parent_intent_id: str) -> str:
    return f"{parent_intent_id[:62]}{PLACEBO_SUFFIX}"


def _working(leg: Leg) -> bool:
    return any(o.role != Role.EXTERNAL and not o.is_terminal for o in leg.leg_orders())


def _newest(quotes: Any, symbol: str) -> Quote | None:
    """The newest quote whatever its age: a QuoteCache (last) or a plain mapping symbol -> Quote."""
    if quotes is None:
        return None
    last = getattr(quotes, "last", None)
    return last(symbol) if callable(last) else quotes.get(symbol)


# ---------------------------------------------------------------------------------------------------------------
# Phase 1: one LONG option leg
# ---------------------------------------------------------------------------------------------------------------
class SingleLegHandler:
    """StructureHandler for LONG_CE and LONG_PE: one BUY entry, one SELL exit ladder (§5.4-5.5, §7)."""

    types = frozenset({StructureType.LONG_CE, StructureType.LONG_PE})

    def _type(self, intent: Intent) -> StructureType | None:
        try:
            t = StructureType(intent.structure)
        except ValueError:
            return None
        return t if t in self.types else None

    def select(self, intent: Intent, ctx: HandlerCtx) -> list[Instrument] | Refusal:
        """[the contract] for the intent's strike rule around a fresh underlying LTP, or Refusal("CONTRACT")."""
        t = self._type(intent)
        if t is None:
            return Refusal(CONTRACT, f"NO_HANDLER: {intent.structure!r}")
        if ctx.instruments is None:
            return Refusal(CONTRACT, "NO_INSTRUMENTS")
        spot = spot_quote(ctx.quotes, intent.underlying, ctx.settings, ctx.now_mono)
        if spot is None:
            return Refusal(CONTRACT, f"NO_SPOT: no {intent.underlying} LTP within "
                                     f"{ctx.settings.UNDERLYING_MAX_AGE_S:g} s")
        c = ctx.instruments.select_contract(intent.underlying, _OPT_TYPE[t], intent.strike_pref, spot.ltp,
                                            ctx.session)
        if isinstance(c, Refusal):
            return c if c.gate == CONTRACT else Refusal(CONTRACT, f"{c.gate}: {c.detail}")
        if not isinstance(c, Instrument):
            return Refusal(CONTRACT, f"NO_CONTRACT: {c!r}")
        return [c]

    def quote_symbols(self, intent: Intent, legs: Sequence[Instrument], placebo: bool) -> tuple[str, ...]:
        """The decision fetch for these legs: every leg's option and the underlying (for G16)."""
        return tuple(dict.fromkeys([*(inst.symbol for inst in legs), intent.underlying]))

    def plan_entry(self, intent: Intent, legs: Sequence[Instrument], quotes: Mapping[str, Quote | None],
                   ctx: HandlerCtx) -> EntryPlan | Refusal:
        """G14-G21 on the decision snapshot `quotes`; the plan when all pass. The order is built from it as is."""
        if len(legs) != 1:
            return Refusal(CONTRACT, f"ONE_LEG_ONLY: {len(legs)} legs")
        gc = ctx.gates
        if gc is None:
            return Refusal(risk.FINAL_GATE, "NO_GATE_CONTEXT")
        contract = legs[0]
        q = quotes.get(contract.symbol)
        u = quotes.get(intent.underlying)
        policy = Policy(gc.book.policy)
        realized = risk.book_realized_net(gc.book)
        profile = ctx.profile
        # DAY_BUDGET's inputs (review round 3: only the day net was passed, so a second position, or a LIVE trade
        #   beside a sibling book's losses, was sized as if nothing else could stop out): the book's realized net and
        #   its open positions' stop-outs measured from their entries and, for a LIVE book, the same over every LIVE
        #   book (RECOVERED:LIVE included). An open gain neither adds room nor takes it (review round 3 follow-up)
        live_book = risk.book_account(gc.book.book) is Account.LIVE
        la = gc.live_account if live_book else None
        terms: dict[str, Any] = {"open_risk_rs": gc.book.open_risk_rs, "live_book": live_book,
                                 "account_realized_net": risk.live_account_realized_net(la),
                                 "account_open_risk_rs": None if la is None else la.open_risk_rs}

        def plan(inst: Instrument, quote: Quote) -> tuple[float | None, Sized | Refusal | None]:
            # the gateway's G18 plan: limit -> size() -> limit again for the sized qty (the ask walk must fill it)
            limit = exits.entry_limit(quote, inst.tick)
            if limit is None:
                return None, None
            sized = sizing.size(inst, quote, limit, profile, gc.settings, realized, gc.rates, policy, **terms)
            if not isinstance(sized, Sized):
                return limit, sized
            return exits.entry_limit(quote, inst.tick, sized.qty), sized

        out = risk.post_quote_gates(gc, intent, contract, q, u, plan)
        if not out.passed:
            if out.error:
                ctx.errors.append(f"{out.result.gate}: {out.error}")
            return out.result if isinstance(out.result, Refusal) else Refusal(risk.FINAL_GATE, repr(out.result))
        sized, limit = out.sized, out.limit_px
        dq = decision_quote(q, sized.qty, Side.BUY, ctx.now_mono)
        premium = round(sized.qty * limit, 2)
        decision = {"symbol": contract.symbol, "qty": sized.qty, "lots": sized.lots, "binding": sized.binding,
                    "per_lot_risk_rs": round(sized.per_lot_risk_rs, 2), "limit": limit, "premium_rs": premium,
                    "walk_px": dq.walk_px, "quote": quote_json(q, ctx.now_mono),
                    "underlying": quote_json(u, ctx.now_mono)}
        leg = LegPlan(contract, Side.BUY, sized.qty, limit, Role.ENTRY, dq)
        return EntryPlan((leg,), premium, decision)

    def next_entry_orders(self, st: Structure, ctx: HandlerCtx) -> list[OrderSpec]:
        """The single ENTRY of an ENTERING structure whose leg has no entry order yet (from its stored plan).

        positions.begin_entry writes the leg and its ENTRY together, so in phase 1 this is normally empty."""
        plan = (st.decision or {}).get("plan") if isinstance(st.decision, Mapping) else None
        if StructState(st.state) is not StructState.ENTERING or not plan:
            return []
        out = []
        for lid in st.legs:
            leg = ctx.legs.get(lid)
            if leg is not None and not any(o.role in (Role.ENTRY, Role.SELFTEST) for o in leg.leg_orders()):
                out.append(OrderSpec(lid, Role.ENTRY, Side.BUY, int(plan["qty"]), float(plan["limit"])))
        return out

    def on_entry_progress(self, st: Structure, ctx: HandlerCtx) -> str:
        """WAIT while the entry is in flight; then OPEN if anything filled, else VOID."""
        if not st.legs:
            return WAIT if StructState(st.state) is StructState.RESERVED else VOID
        leg = ctx.legs.get(st.legs[0])
        if leg is None:
            return WAIT
        entries = [o for o in leg.leg_orders() if o.role in (Role.ENTRY, Role.SELFTEST)]
        if not entries or _working(leg):
            return WAIT
        return OPEN if any(o.filled_qty > 0 for o in entries) else VOID

    def exit_params(self, st: Structure, ctx: HandlerCtx) -> ExitParams | None:
        """The levels frozen at the entry fill (None before it; decide() then exits as an anomaly)."""
        return st.exit_params

    def evaluate(self, st: Structure, quotes: Any, now_mono: float, now_wall: datetime, forced: str | None,
                 ctx: HandlerCtx) -> Decision:
        """exits.decide for the structure's leg on the newest quote of its symbol (rule 2 ages it)."""
        leg = ctx.legs[st.legs[0]]
        sym = leg.inst.symbol
        refresh = ctx.refresh.get(sym) if ctx.refresh is not None else None
        eod = ctx.eod or EodTimes.for_session(session_of(now_wall), ctx.settings)
        return exits.decide(leg, _newest(quotes, sym), refresh, now_mono, now_wall, self.exit_params(st, ctx), eod,
                            forced)

    def exit_sequence(self, st: Structure) -> list[int]:
        return list(st.legs)

    def placebo_intent(self, intent: Intent, ctx: HandlerCtx) -> Intent | None:
        """The opposite option type on the same underlying, strike rule, expiry rule, trigger and recheck (the same
        snapshot passes it), created at ctx.now (the parent's decision time). None for a placebo's placebo."""
        t = self._type(intent)
        if t is None or intent.is_placebo is not False:
            return None
        opp = _OPPOSITE[t]
        return Intent(intent_id=placebo_intent_id(intent.intent_id), strategy=intent.strategy,
                      strategy_version=intent.strategy_version, underlying=intent.underlying,
                      direction=_DIRECTION[opp], structure=opp, strike_pref=intent.strike_pref,
                      expiry_rule=intent.expiry_rule, trigger_px=intent.trigger_px, recheck=intent.recheck,
                      ttl_s=intent.ttl_s, created_mono=ctx.now_mono, created_wall=ctx.now_wall,
                      reason={"placebo_of": intent.intent_id}, is_placebo=True, parent_intent_id=intent.intent_id)
