"""The entry gateway: routing, pre-reserve gates, reservation, the decision-quote fetch, plan -> order, the live
twin and the placebo (spec §5.4, §10.5-10.6; DETAIL §5.4, §8.6, §10.5-10.6).

Flow for one intent (one attempt per intent per book: no chase, no retry, no replay):
  on_intent          route the waiting intents first (_wake: a slot freed since the last Timer is theirs), then
                     validate (strategies.base.intent_problem), insert the intents row, route to books
                     (risk.books_for: LIVE first, then its PAPER twin), run G01-G13 per book and write one
                     intent_routes row each: RESERVED (eng.reserve holds the DB slot), REFUSED, EXPIRED (TTL) or
                     DIVERTED (PAPER:<s> blocked only by operator halts: re-routed to SHADOW:<s>, session IMPAIRED).
                     WAIT (2026-10-14): when the FIRST book's only obstacle is G09 counting pending structures (its
                     RESERVED and not yet opened ENTERING ones: G01-G13 pass on risk.settled(), the same snapshot
                     with them voided), none of the intent's books is routed yet. It waits in entry_memory (stage
                     WAIT, arrival order) holding nothing: its first book's row says REFUSED at G09 (today's answer,
                     so a reload, restart or SIGTERM leaves exactly that) and INTENT_WAITING names what it waits for.
                     Every Timer routes it again as if it had just arrived (every gate on fresh data, TTL included)
                     until it is reserved, refused or EXPIRED. A woken intent that G09 refuses as RUNAWAY raises no
                     halt (the owner's decision, 2026-10-14): only a fresh intent does. G09 counts a reserved
                     structure's entry order before it is sent (ENTRY_ORDERS, which waits like the other pending
                     caps), so one batch of fresh and woken intents never sends more than MAX_ENTRY_ORDERS_PER_MIN
                     entry orders a minute. Deferring the one attempt is not a retry.
                     Then select the contract (handler.select needs the underlying LTP at most UNDERLYING_MAX_AGE_S
                     old; without one the underlying is fetched first, request "<intent_id>:SPOT"). PAPER books also
                     select the placebo's contract. ONE FetchQuotes per intent (request_id = intent_id): option,
                     placebo option, underlying; priority ENTRY_LIVE if a LIVE book is reserved, else PAPER, or
                     SHADOW when only SHADOW books are.
  on_quote_result    every quote is read from the QuoteCache once: that decision snapshot is shared by the LIVE
                     book and its PAPER twin. Each RESERVED structure, in the order LIVE, PAPER, SHADOW:
                     handler.plan_entry (G14-G21), then G22, then eng.begin_entry (leg + ENTRY order SUBMITTING +
                     decision_json -> Place + Subscribe). A refusal voids the structure (void_reason "<gate>:<detail>",
                     which starts its cooldown); a failed fetch voids them all (gate QUOTE), and so does a decision
                     while the bot is stopping (SIGTERM, gate SHUTDOWN). Refusals after the contract was chosen
                     first write an ENTRY_REFUSED event (contract + decision snapshot) for `titan2 whatif`. A PAPER:<s> parent opens its placebo in the
                     same transaction (_open_placebo): planned on the same snapshot (PLACEBO:<s>, QUALITY), its Place
                     goes out with the parent's after one commit, so each pays its own paper latency from the same
                     instant and the price path during the parent's latency never decides which placebos fill.
  on_timer           void RESERVED structures whose intent expired or whose fetch was lost; then route the waiting
                     intents again, in arrival order (_wake).

Placebo (§10.6, with one documented deviation, review F-18): the spec opens it for "every PAPER:<s> entry that
fills"; titan2 opens it with every PAPER:<s> entry sent. A placebo whose parent never fills stays an ordinary
SHADOW trade managed by the normal exits; G1 pairs placebos only with parents in its sample (g1._placebos, by
parent_structure_id), so it never counts it. It can hold its PLACEBO:<s> underlying slot meanwhile and so lower the
coverage, which can only make G1 harder to pass (fails closed).

The engine contract (core/state.py GatewayPort, core/engine.py docstring): every function runs on the core thread
inside the engine's transaction, takes the Engine `eng` and returns the commands to dispatch after the commit.
Services used: eng.store settings quotes instruments handlers state session now_mono now_wall eod gate_context(book)
reserve() void() begin_entry() event() apply_halt() flag_impaired() profile(). RAM memory: eng.state.entry_memory
(intent_id -> the fetch it waits for, or its WAIT); after an engine reload it is empty, a late QuoteResult is then
not the gateway's (None), the Timer voids the orphaned RESERVED structures once their intent expires, and a waiting
intent keeps the REFUSED row it was given when it started waiting.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Mapping

from titan2.clock import iso
from titan2.core import risk
from titan2.core.events import Alert, Command, FetchQuotes, Priority, QuoteResult
from titan2.core.risk import GateContext, GateOutcome
from titan2.core.single_leg import HandlerCtx, StructureHandler, quote_json, spot_quote
from titan2.model import (
    Account, Instrument, Intent, InvariantViolation, Mode, Quote, Refusal, Role, StructState,
    Structure, StructureType,
)
from titan2.settings import GATE_ERROR_LIMIT, GATE_ERROR_WINDOW_S, QUOTE_REQUEST_TIMEOUT_S
from titan2.strategies.base import intent_problem

__all__ = ["SPOT_SUFFIX", "fetch_priority", "handler_ctx", "on_intent", "on_quote_result", "on_timer"]

SPOT_SUFFIX = ":SPOT"                            # request_id of the underlying-only fetch when the LTP is stale
REQUEST_LOST_S = QUOTE_REQUEST_TIMEOUT_S + 2.0   # the reads lane answers within 3 s; past this the fetch was lost
WAIT = "WAIT"                                    # _Pending.stage: not routed yet, waiting for pending structures
_ACCOUNT_ORDER = {Account.LIVE: 0, Account.PAPER: 1, Account.SHADOW: 2}     # §5.4 step 4: LIVE, PAPER, SHADOW


@dataclass(slots=True)
class _Pending:
    """One intent waiting for its fetch: SPOT (the underlying only) or QUOTES (the decision snapshot); or, stage WAIT,
    for its first book's pending structures (no fetch: request_id "", sent_mono = when it started waiting)."""

    intent: Intent
    structure_ids: list[int]
    stage: str
    request_id: str
    sent_mono: float
    symbols: tuple[str, ...]
    legs: list[Instrument] = field(default_factory=list)
    placebo: Intent | None = None
    placebo_legs: list[Instrument] | Refusal | None = None
    books: tuple[str, ...] = ()                  # WAIT: the books it is routed to once it may, LIVE first
    blocked: str = ""                            # WAIT: the G09 refusal it waits behind, "BOOK_CAPS: POSITIONS: 2/2"


# ---------------------------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------------------------
def handler_ctx(eng: Any, **kw: Any) -> HandlerCtx:
    """The HandlerCtx for a handler call at the engine's current time (now_mono / now_wall may be overridden)."""
    base = dict(now_mono=eng.now_mono, now_wall=eng.now_wall, settings=eng.settings, instruments=eng.instruments,
                quotes=eng.quotes, legs=eng.state.legs, refresh=eng.state.refresh, eod=eng.eod)
    base.update(kw)
    return HandlerCtx(**base)


def fetch_priority(accounts: set[Account]) -> int:
    """§5.4 step 3: ENTRY_LIVE if any LIVE book is involved, else PAPER, or SHADOW when only SHADOW books are."""
    if Account.LIVE in accounts:
        return int(Priority.ENTRY_LIVE)
    if accounts and accounts <= {Account.SHADOW}:
        return int(Priority.SHADOW)
    return int(Priority.PAPER)


def _book_kind(book: str) -> str | None:
    parsed = risk.parse_book(book)
    return None if parsed is None else parsed[0]


def _placebo_parent(st: Structure) -> bool:
    """A PAPER:<s> entry: it opens a placebo with itself (§10.6)."""
    return _book_kind(st.book) == "PAPER" and not st.is_placebo


def _live_keys_ok(gc: GateContext) -> bool:
    """I14 for a LIVE ENTRY on this snapshot: ALLOW_LIVE, unlocked, today's self-test passed, not demoted (G13)."""
    lp = gc.live
    return (lp is not None and lp.allow_live is True and lp.unlocked is True and lp.selftest_passed is True
            and lp.demoted is False)


def _strategy_mode(eng: Any, strategy: str) -> Any:
    """The strategy's effective mode this session: EngineState.modes holds strategy_modes rows by strategy."""
    row = eng.state.modes.get(strategy)
    return row.get("mode") if isinstance(row, Mapping) else row


def _reserved(eng: Any, ids: list[int]) -> list[Structure]:
    return [eng.state.structures[i] for i in ids if i in eng.state.structures
            and StructState(eng.state.structures[i].state) is StructState.RESERVED]


def _event(eng: Any, kind: str, payload: Mapping, severity: str = "INFO", *, intent: Intent | None = None,
           st: Structure | None = None, book: str | None = None) -> None:
    refs: dict[str, Any] = {}
    if st is not None:
        refs.update(account=st.account, book=st.book, strategy=st.strategy, structure_id=st.structure_id,
                    intent_id=st.intent.intent_id)
    if intent is not None:
        refs.update(strategy=intent.strategy, intent_id=intent.intent_id)
    if book is not None:
        refs["book"] = book
    eng.event(kind, dict(payload), severity, **refs)


def _route_row(eng: Any, intent: Intent, book: str, outcome: str, *, gate: str | None = None,
               detail: str | None = None, structure_id: int | None = None, waited: _Pending | None = None) -> None:
    detail = None if detail is None else detail[:500]
    eng.store.put_intent_route(intent.intent_id, book, outcome, eng.now_wall, gate=gate, detail=detail,
                               structure_id=structure_id)
    payload: dict[str, Any] = {"book": book, "outcome": outcome, "gate": gate, "detail": detail,
                               "structure_id": structure_id}
    if waited is not None:              # routed after a WAIT: for how long, and behind what
        payload.update(waited_s=round(eng.now_mono - waited.sent_mono, 3), waited_for=waited.blocked)
    _event(eng, "INTENT_ROUTED", payload, intent=intent, book=book)
    if outcome == "EXPIRED":
        _event(eng, "INTENT_EXPIRED", {"book": book, "detail": detail}, intent=intent, book=book)


def _gate_error(eng: Any, gate: str, error: str | None, book: str, intent: Intent) -> list[Command]:
    """GATE_ERROR event + WARN; HALT(GATE_ERRORS, GLOBAL, ACK) at GATE_ERROR_LIMIT within the window. The count is
    SQL over the events table, so an engine reload or a restart does not reset it."""
    _event(eng, "GATE_ERROR", {"gate": gate, "error": error, "book": book}, "WARN", intent=intent, book=book)
    out: list[Command] = [Alert("WARN", "GATE_ERROR", f"{book} gate {gate} raised: {error}", f"gate_error:{gate}")]
    since = iso(eng.now_wall - timedelta(seconds=GATE_ERROR_WINDOW_S))
    n = eng.store.scalar("SELECT COUNT(*) FROM events WHERE kind='GATE_ERROR' AND ts >= ?", (since,))
    if n >= GATE_ERROR_LIMIT:
        out += eng.apply_halt(risk.make_halt("GATE_ERRORS", "GLOBAL", f"{n} gate errors within "
                                             f"{GATE_ERROR_WINDOW_S:.0f} s; last {gate}: {error}"))
    return out


def _gate_errors(eng: Any, out: GateOutcome | Refusal, ctx: HandlerCtx | None, book: str,
                 intent: Intent) -> list[Command]:
    if isinstance(out, GateOutcome) and out.gate_error:
        return _gate_error(eng, out.result.gate, out.error, book, intent)
    if isinstance(out, Refusal) and out.detail == risk.GATE_ERROR:
        return _gate_error(eng, out.gate, "; ".join(ctx.errors) if ctx is not None and ctx.errors else None, book,
                           intent)
    return []


def _void(eng: Any, st: Structure, r: Refusal) -> list[Command]:
    return list(eng.void(st, f"{r.gate}:{r.detail}"[:300]))


def _refused(eng: Any, p: _Pending, st: Structure, r: Refusal,
             snap: Mapping[str, Quote | None] | None) -> list[Command]:
    """Void a structure refused after its contract was chosen, first recording what the bot saw (ENTRY_REFUSED: the
    contract, and the decision snapshot when the fetch answered) for the what-if report (titan2.whatif). Information
    only: nothing in the bot reads it back."""
    spot = eng.quotes.last(p.intent.underlying) if eng.quotes is not None else None
    legs = [{"symbol": i.symbol, "token": i.token, "lot_size": i.lot_size, "tick": i.tick, "strike": i.strike,
             "opt_type": i.opt_type, "expiry": i.expiry.isoformat()} for i in p.legs]
    quotes = None if snap is None else {sym: quote_json(q, eng.now_mono) for sym, q in snap.items()}
    _event(eng, "ENTRY_REFUSED", {"gate": r.gate, "detail": r.detail[:300], "direction": str(p.intent.direction),
                                  "trigger_px": p.intent.trigger_px, "spot": None if spot is None else spot.ltp,
                                  "legs": legs, "quotes": quotes}, st=st)
    return _void(eng, st, r)


def _reserve(eng: Any, intent: Intent, book: str, *, blocked_by: str | None = None,
             parent_structure_id: int | None = None) -> Structure | Refusal:
    """RESERVED structure for (intent, book). The DB's one-per-(book, underlying) index refusing it (I5) is an
    UNDERLYING refusal here: G10 read a stale view and the database held the line."""
    try:
        return eng.reserve(intent, book=book, account=risk.book_account(book), policy=risk.book_policy(book),
                           parent_structure_id=parent_structure_id, blocked_by=blocked_by)
    except InvariantViolation as iv:
        if iv.code != "I5":
            raise
        return Refusal("UNDERLYING", f"DB_SLOT_TAKEN: {iv.detail}"[:300])


# ---------------------------------------------------------------------------------------------------------------
# IntentReceived
# ---------------------------------------------------------------------------------------------------------------
def on_intent(eng: Any, intent: Intent) -> list[Command]:
    """Validate, insert, route and reserve; then select the contract and request the decision quotes. The intents
    already waiting are routed first: a fresh intent never takes a slot ahead of them."""
    out = _wake(eng)
    problem = intent_problem(intent)
    if problem is None and intent.is_placebo:
        problem = "placebo intents are made by the gateway, never received"
    if problem is None and eng.store.scalar("SELECT 1 FROM intents WHERE intent_id=?", (intent.intent_id,)):
        problem = "duplicate intent_id: intents are never replayed"
    if problem is not None:
        iid, strategy = getattr(intent, "intent_id", None), getattr(intent, "strategy", None)
        iid = iid if isinstance(iid, str) else None
        strategy = strategy if isinstance(strategy, str) else None
        eng.event("INTENT_RECEIVED", {"refused": problem}, "WARN", strategy=strategy, intent_id=iid)
        return out + [Alert("WARN", "INTENT_INVALID", f"intent {iid!r} of {strategy!r} refused: {problem}",
                            f"intent_invalid:{strategy}")]
    eng.store.insert_intent(intent, eng.session)
    _event(eng, "INTENT_RECEIVED", {"underlying": intent.underlying, "direction": str(intent.direction),
                                    "structure": str(intent.structure), "strike_pref": str(intent.strike_pref),
                                    "trigger_px": intent.trigger_px, "ttl_s": intent.ttl_s,
                                    "reason": dict(intent.reason)}, intent=intent)
    mode = _strategy_mode(eng, intent.strategy)
    try:
        books = risk.books_for(intent.strategy, Mode(mode))
    except ValueError:
        books = ()
    if not books:
        _event(eng, "INTENT_ROUTED", {"outcome": "NOT_ROUTED", "mode": None if mode is None else str(mode)},
               intent=intent)
        return out
    cmds, _ = _route_books(eng, intent, tuple(books), None)
    return out + cmds


def _route_books(eng: Any, intent: Intent, books: tuple[str, ...], wait: _Pending | None
                 ) -> tuple[list[Command], bool]:
    """(commands, still waiting). Route the intent to its books in order (LIVE first) and fetch for the reserved
    ones. Only the first book may make it wait: in LIVE mode that is LIVE:<s>, and its PAPER twin waits with it so
    that both decide on one snapshot; the twin itself never waits (refused as before). While it waits no book is
    routed. wait: the intent's WAIT memory when it is routed again (None on arrival)."""
    out: list[Command] = []
    reserved: list[Structure] = []
    for k, book in enumerate(books):
        cmds, blocked = _route(eng, intent, book, None, reserved, wait=wait, may_wait=k == 0)
        out += cmds
        if blocked is not None:
            if wait is None:
                _hold(eng, intent, books, book, blocked)
            return out, True
    if reserved:
        out += _start(eng, intent, [st.structure_id for st in reserved])
    return out, False


def _route(eng: Any, intent: Intent, book: str, divert: risk.Divert | None, reserved: list[Structure], *,
           wait: _Pending | None = None, may_wait: bool = False) -> tuple[list[Command], Refusal | None]:
    """(commands, the G09 refusal to wait behind, or None once the route row is written)."""
    gc = eng.gate_context(book)
    res = risk.pre_reserve_gates(gc, intent, divert)
    out: list[Command] = []
    if wait is None:
        # a woken intent that G09 refuses as RUNAWAY raises no halt (the owner's decision, 2026-10-14): the entry
        #   orders it counts are those of the intents it waited behind, not a runaway. A fresh intent still does.
        for h in res.halts:
            out += eng.apply_halt(h)
    out += _gate_errors(eng, res, None, book, intent)
    if res.divert is not None:
        d = res.divert
        _route_row(eng, intent, book, "DIVERTED", gate=d.gate, detail=d.detail, waited=wait)
        eng.flag_impaired(intent.strategy, f"DIVERTED: {d.blocked_by}")
        if d.to_book != book:
            out += _route(eng, intent, d.to_book, d, reserved, wait=wait)[0]
        return out, None
    if res.refusal is not None:
        r = res.refusal
        # G09 counted pending structures (risk.waits_for_pending: with them voided every G01-G13 gate passes), so the
        #   slot may still free up within the TTL. Wait for it instead of refusing; nothing is held meanwhile.
        if may_wait and risk.waits_for_pending(gc, intent, res):
            return out, r
        _route_row(eng, intent, book, "EXPIRED" if r.gate == "TTL" else "REFUSED", gate=r.gate, detail=r.detail,
                   waited=wait)
        return out, None
    st = _reserve(eng, intent, book, blocked_by=None if divert is None else divert.blocked_by)
    if isinstance(st, Refusal):
        _route_row(eng, intent, book, "REFUSED", gate=st.gate, detail=st.detail, waited=wait)
        out.append(Alert("WARN", "RESERVE_REFUSED", f"{book} {intent.underlying}: {st.detail}",
                         f"reserve:{book}:{intent.underlying}"))
        return out, None
    _route_row(eng, intent, book, "RESERVED", structure_id=st.structure_id, waited=wait)
    reserved.append(st)
    return out, None


def _hold(eng: Any, intent: Intent, books: tuple[str, ...], book: str, r: Refusal) -> None:
    """Start the WAIT. The book's route row says REFUSED at G09, today's answer, until the wait ends (the next
    _route_row overwrites it), so an engine reload, a restart or SIGTERM meanwhile leaves exactly that record."""
    eng.store.put_intent_route(intent.intent_id, book, "REFUSED", eng.now_wall, gate=r.gate, detail=r.detail[:500])
    live = risk.book_account(book) is Account.LIVE
    pending = [x.structure_id for x in eng.state.live_structures()
               if (x.book == book or (live and Account(x.account) is Account.LIVE)) and x.session == eng.session
               and x.opened_wall is None and x.state in (StructState.RESERVED, StructState.ENTERING)]
    _event(eng, "INTENT_WAITING", {"book": book, "gate": r.gate, "detail": r.detail[:300], "pending": pending,
                                   "ttl_s": intent.ttl_s}, intent=intent, book=book)
    eng.state.entry_memory[intent.intent_id] = _Pending(intent, [], WAIT, "", eng.now_mono, (), books=books,
                                                        blocked=f"{r.gate}: {r.detail}"[:300])


def _wake(eng: Any) -> list[Command]:
    """Route every waiting intent again, oldest first (entry_memory keeps arrival order), exactly as on arrival: it
    is reserved, refused or EXPIRED (its TTL is G03), or it keeps its place in the queue while its first book is
    still refused only because of pending structures."""
    out: list[Command] = []
    for iid, p in list(eng.state.entry_memory.items()):
        if not (isinstance(p, _Pending) and p.stage == WAIT):
            continue
        cmds, waiting = _route_books(eng, p.intent, p.books, p)
        out += cmds
        if not waiting and eng.state.entry_memory.get(iid) is p:     # routed; a reserved one now holds its fetch
            del eng.state.entry_memory[iid]
    return out


def _start(eng: Any, intent: Intent, ids: list[int]) -> list[Command]:
    """Select now if the underlying LTP is fresh; otherwise fetch the underlying first (§5.4 step 2)."""
    if spot_quote(eng.quotes, intent.underlying, eng.settings, eng.now_mono) is not None:
        return _select_and_fetch(eng, intent, ids)
    rid = intent.intent_id + SPOT_SUFFIX
    eng.state.entry_memory[intent.intent_id] = _Pending(intent, list(ids), "SPOT", rid, eng.now_mono,
                                                        (intent.underlying,))
    return [FetchQuotes(rid, (intent.underlying,), _priority(eng, ids))]


def _priority(eng: Any, ids: list[int]) -> int:
    return fetch_priority({Account(eng.state.structures[i].account) for i in ids if i in eng.state.structures})


def _select_and_fetch(eng: Any, intent: Intent, ids: list[int]) -> list[Command]:
    out: list[Command] = []
    live = _reserved(eng, ids)
    handler = eng.handlers.get(StructureType(intent.structure))
    if handler is None:
        for st in live:
            out += _void(eng, st, Refusal("STRUCTURE", f"NO_HANDLER: {intent.structure}"))
        return out
    ctx = handler_ctx(eng)
    legs = handler.select(intent, ctx)
    if isinstance(legs, Refusal):
        for st in live:
            out += _void(eng, st, legs)
        return out
    legs = list(legs)
    placebo, placebo_legs = None, None
    if any(_placebo_parent(st) for st in live):
        placebo = handler.placebo_intent(intent, ctx)
        if placebo is not None:
            placebo_legs = handler.select(placebo, ctx)
            if not isinstance(placebo_legs, Refusal):
                placebo_legs = list(placebo_legs)
    symbols = list(handler.quote_symbols(intent, legs, False))
    if placebo is not None and isinstance(placebo_legs, list):
        symbols += handler.quote_symbols(placebo, placebo_legs, True)
    symbols_t = tuple(dict.fromkeys(symbols))
    rid = intent.intent_id
    eng.state.entry_memory[intent.intent_id] = _Pending(intent, [st.structure_id for st in live], "QUOTES", rid,
                                                        eng.now_mono, symbols_t, legs, placebo, placebo_legs)
    out.append(FetchQuotes(rid, symbols_t, _priority(eng, ids)))
    return out


# ---------------------------------------------------------------------------------------------------------------
# QuoteResult
# ---------------------------------------------------------------------------------------------------------------
def on_quote_result(eng: Any, ev: QuoteResult) -> list[Command] | None:
    """The decision fetch answered. None when the request is not one of the gateway's (the engine ignores it)."""
    rid = ev.request_id
    iid = rid[:-len(SPOT_SUFFIX)] if isinstance(rid, str) and rid.endswith(SPOT_SUFFIX) else rid
    p = eng.state.entry_memory.get(iid)
    if not isinstance(p, _Pending) or p.request_id != rid:
        return None
    del eng.state.entry_memory[iid]
    live = _reserved(eng, p.structure_ids)
    out: list[Command] = []
    if not live:
        return out
    if eng.state.stopping:
        # SIGTERM stops entries (§9.5 step 17): an ENTRY order now is a SHUTDOWN violation (rolled back, HALT)
        for st in live:
            out += _void(eng, st, Refusal("SHUTDOWN", "STOPPING: the bot is stopping (SIGTERM), no new entries"))
        return out
    if not ev.ok:
        _event(eng, "QUOTE_TIMEOUT", {"request_id": rid, "error": ev.error, "missing": list(ev.missing)}, "WARN",
               intent=p.intent)
        for st in live:
            r = Refusal("QUOTE", f"FETCH_FAILED: {ev.error or 'error'}")
            out += _refused(eng, p, st, r, None) if p.legs else _void(eng, st, r)
        return out
    if p.stage == "SPOT":
        if spot_quote(eng.quotes, p.intent.underlying, eng.settings, eng.now_mono) is None:
            for st in live:
                out += _void(eng, st, Refusal("QUOTE", f"NO_SPOT: {p.intent.underlying}"))
            return out
        return _select_and_fetch(eng, p.intent, [st.structure_id for st in live])
    # the decision snapshot: each symbol read once, shared by every book of this intent (live twin, placebo)
    snap: dict[str, Quote | None] = {sym: eng.quotes.last(sym) for sym in p.symbols}
    handler = eng.handlers.get(StructureType(p.intent.structure))
    for st in sorted(live, key=lambda s: (_ACCOUNT_ORDER.get(Account(s.account), 9), s.book)):
        out += _place(eng, p, st, handler, snap)
    return out


def _place(eng: Any, p: _Pending, st: Structure, handler: StructureHandler | None,
           snap: Mapping[str, Quote | None]) -> list[Command]:
    if handler is None:
        return _void(eng, st, Refusal("STRUCTURE", "NO_HANDLER"))
    gc = eng.gate_context(st.book)
    ctx = handler_ctx(eng, gates=gc, profile=eng.profile(st.strategy))
    plan = handler.plan_entry(p.intent, p.legs, snap, ctx)
    if isinstance(plan, Refusal):
        return _gate_errors(eng, plan, ctx, st.book, p.intent) + _refused(eng, p, st, plan, snap)
    fin = risk.final_gate(gc, p.intent)
    if not fin.passed:
        out = _gate_errors(eng, fin, None, st.book, p.intent)
        r = fin.refusal
        if r.detail.startswith("OPERATOR_HALT") and _book_kind(st.book) == "PAPER":
            eng.flag_impaired(st.strategy, f"REFUSED AFTER RESERVE: {r.detail}")
        return out + _refused(eng, p, st, r, snap)
    if Account(st.account) is Account.LIVE and not _live_keys_ok(gc):
        return _void(eng, st, Refusal("LIVE", "LIVE_KEYS_CHANGED_SINCE_RESERVE"))
    out: list[Command] = []
    decision: dict[str, Any] = {"intent_id": p.intent.intent_id, "book": st.book, "decided_at": eng.now_wall,
                                "plan": plan.decision,
                                "quotes": {sym: quote_json(q, eng.now_mono) for sym, q in snap.items()}}
    pi, placebo = None, None
    if _placebo_parent(st):
        pi, placebo, perr = _placebo_plan(eng, p, handler, snap)
        decision["placebo"] = placebo
        out += perr
    lp = plan.legs[0]
    _leg, _order, cmds = eng.begin_entry(st, inst=lp.inst, qty=lp.qty, limit_px=lp.limit_px,
                                         decision_quote=lp.decision_quote, decision=decision, role=lp.role)
    out += list(cmds)
    if placebo is not None:
        out += _open_placebo(eng, st, pi, placebo)
    return out


def _placebo_plan(eng: Any, p: _Pending, handler: StructureHandler, snap: Mapping[str, Quote | None]
                  ) -> tuple[Intent | None, dict[str, Any], list[Command]]:
    """(the placebo intent, its plan, gate-error commands): G14-G21 for the placebo on the parent's decision snapshot
    (PLACEBO:<s>, QUALITY), stored in the parent's decision_json and opened with the parent (_open_placebo). The
    placebo intent is stamped now, at the decision; its contract was selected with the parent's."""
    pi = None if p.placebo is None else handler.placebo_intent(p.intent, handler_ctx(eng))
    base: dict[str, Any] = {"created_wall": None if pi is None else pi.created_wall,
                            "intent_id": None if pi is None else pi.intent_id}
    if pi is None:
        return None, {**base, "refused": {"gate": "STRUCTURE", "detail": "NO_PLACEBO"}}, []
    if isinstance(p.placebo_legs, Refusal) or not p.placebo_legs:
        r = p.placebo_legs if isinstance(p.placebo_legs, Refusal) else Refusal("CONTRACT", "NO_CONTRACT")
        return pi, {**base, "refused": {"gate": r.gate, "detail": r.detail}}, []
    book = f"PLACEBO:{pi.strategy}"
    ctx = handler_ctx(eng, gates=eng.gate_context(book), profile=eng.profile(pi.strategy))
    plan = handler.plan_entry(pi, p.placebo_legs, snap, ctx)
    if isinstance(plan, Refusal):
        return (pi, {**base, "refused": {"gate": plan.gate, "detail": plan.detail}},
                _gate_errors(eng, plan, ctx, book, p.intent))
    lp = plan.legs[0]
    return pi, {**base, "book": book, "inst": lp.inst, "qty": lp.qty, "limit": lp.limit_px,
                "dq": lp.decision_quote, "plan": plan.decision}, []


# ---------------------------------------------------------------------------------------------------------------
# The placebo (§10.6): opened with its PAPER:<s> parent, in the parent's decision transaction
# ---------------------------------------------------------------------------------------------------------------
def _open_placebo(eng: Any, parent: Structure, pi: Intent | None, plan: Mapping[str, Any]) -> list[Command]:
    """Open the placebo of a PAPER:<s> parent from the plan _placebo_plan just made on the parent's decision
    snapshot: the placebo's intents row, G01-G13 for PLACEBO:<s>, the reservation (parent_structure_id), G22, then
    eng.begin_entry with the decision-time qty, limit and DecisionQuote. Its Place goes out after the same commit as
    the parent's. A refused or impossible placebo is recorded on the parent at once as placebo_status
    SKIPPED:<gate> (G1 coverage); an opened one as OPENED."""
    if eng.state.stopping:
        # SIGTERM stops entries (§9.5 step 17). on_quote_result refuses a decision while stopping; this is the
        #   second line: an ENTRY order now is a SHUTDOWN violation
        return _skip_placebo(eng, parent, "SHUTDOWN", "the bot is stopping (SIGTERM): no new entries")
    if pi is None:
        return _skip_placebo(eng, parent, "STRUCTURE", "NO_PLACEBO_INTENT")
    problem = intent_problem(pi)
    if problem is not None or eng.store.scalar("SELECT 1 FROM intents WHERE intent_id=?", (pi.intent_id,)):
        return _skip_placebo(eng, parent, "STRATEGY", problem or "PLACEBO_EXISTS")
    eng.store.insert_intent(pi, eng.session)
    _event(eng, "INTENT_RECEIVED", {"underlying": pi.underlying, "direction": str(pi.direction),
                                    "structure": str(pi.structure), "is_placebo": True,
                                    "parent_intent_id": pi.parent_intent_id,
                                    "parent_structure_id": parent.structure_id}, intent=pi)
    book = f"PLACEBO:{pi.strategy}"
    refused = plan.get("refused")
    if isinstance(refused, Mapping):
        gate, detail = str(refused.get("gate")), f"AT_DECISION: {refused.get('detail')}"
        _route_row(eng, pi, book, "REFUSED", gate=gate, detail=detail)
        return _skip_placebo(eng, parent, gate, detail)
    gc = eng.gate_context(book)
    res = risk.pre_reserve_gates(gc, pi)
    out: list[Command] = []
    for h in res.halts:
        out += eng.apply_halt(h)
    out += _gate_errors(eng, res, None, book, pi)
    if not res.passed:
        r = res.refusal if res.refusal is not None else Refusal("HALTS", "DIVERT_NOT_ALLOWED")
        _route_row(eng, pi, book, "EXPIRED" if r.gate == "TTL" else "REFUSED", gate=r.gate, detail=r.detail)
        return out + _skip_placebo(eng, parent, r.gate, r.detail)
    pst = _reserve(eng, pi, book, parent_structure_id=parent.structure_id)
    if isinstance(pst, Refusal):
        _route_row(eng, pi, book, "REFUSED", gate=pst.gate, detail=pst.detail)
        return out + _skip_placebo(eng, parent, pst.gate, pst.detail)
    _route_row(eng, pi, book, "RESERVED", structure_id=pst.structure_id)
    fin = risk.final_gate(gc, pi)
    if not fin.passed:
        out += _void(eng, pst, fin.refusal)
        return out + _skip_placebo(eng, parent, fin.refusal.gate, fin.refusal.detail)
    inst, qty, limit = plan["inst"], int(plan["qty"]), float(plan["limit"])
    decision = {"placebo_of": parent.structure_id, "parent_intent_id": parent.intent.intent_id,
                "decided_at": eng.now_wall, "plan": plan.get("plan")}
    _leg, _order, cmds = eng.begin_entry(pst, inst=inst, qty=qty, limit_px=limit, decision_quote=plan["dq"],
                                         decision=decision, role=Role.ENTRY)
    out += list(cmds)
    parent.placebo_status = "OPENED"
    eng.store.update_structure(parent)
    _event(eng, "PLACEBO_OPENED", {"placebo_structure_id": pst.structure_id, "symbol": inst.symbol, "qty": qty,
                                   "limit": limit}, st=parent)
    return out


def _skip_placebo(eng: Any, parent: Structure, gate: str, detail: str) -> list[Command]:
    parent.placebo_status = f"SKIPPED:{gate}"
    eng.store.update_structure(parent)
    _event(eng, "PLACEBO_SKIPPED", {"gate": gate, "detail": detail}, st=parent)
    return []


# ---------------------------------------------------------------------------------------------------------------
# Timer
# ---------------------------------------------------------------------------------------------------------------
def on_timer(eng: Any) -> list[Command]:
    """Void every RESERVED structure whose intent expired (G14/G22 would refuse it anyway: its fetch was lost, or
    an engine reload dropped the gateway's memory) or whose decision fetch went unanswered for REQUEST_LOST_S; then
    route the waiting intents again (_wake), after those voids so the slots they free count."""
    out: list[Command] = []
    lost: dict[int, str] = {}
    for iid, p in list(eng.state.entry_memory.items()):
        if not isinstance(p, _Pending) or p.stage == WAIT:
            continue
        waiting = _reserved(eng, p.structure_ids)
        if not waiting:
            del eng.state.entry_memory[iid]
        elif eng.now_mono - p.sent_mono >= REQUEST_LOST_S:
            del eng.state.entry_memory[iid]
            for st in waiting:
                lost[st.structure_id] = p.request_id
    for sid, st in sorted(eng.state.structures.items()):
        if StructState(st.state) is not StructState.RESERVED or st.book == risk.SELFTEST_BOOK:
            continue
        it = st.intent
        age = eng.now_mono - it.created_mono
        if sid in lost:
            _event(eng, "QUOTE_TIMEOUT", {"request_id": lost[sid], "error": "LOST"}, "WARN", st=st)
            out += _void(eng, st, Refusal("QUOTE", f"FETCH_LOST: no answer in {REQUEST_LOST_S:g} s"))
        elif not (isinstance(it.ttl_s, (int, float)) and math.isfinite(age) and age <= it.ttl_s):
            _event(eng, "INTENT_EXPIRED", {"book": st.book, "age_s": round(age, 3) if math.isfinite(age) else None},
                   st=st)
            out += _void(eng, st, Refusal("TTL", f"EXPIRED: {age:.2f} s > {it.ttl_s} s before a decision"))
    return out + _wake(eng)
