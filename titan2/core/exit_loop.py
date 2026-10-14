"""The exit loop: exit attempts for EXITING legs, the broker cap, verify-flat, the EOD backstop, STOP_SENDING and the
cover loop (spec §5.5-5.7, §9.4; DETAIL §5.5, §5.7, §9.3-9.4). The engine runs step() for every EXITING or
VERIFY_FLAT leg on each Timer, on QuotesDirty for its symbol and after a REST refresh, and on each Timer for any other
leg holding exit-loop memory: a cover can hang off any leg (an OPEN one, or one whose structure already closed), and
a leg with neither an exit nor a cover running has its memory dropped. Halts never stop it: it keeps trying until
the leg is flat or STOP_SENDING.

One step for one leg (first match ends the step):
  a bot order on the leg is not terminal        wait (I4: one order at a time)
  a cover of a bot-explained short is running   the cover ladder (COVER BUY, never more than the planned qty)
  the bot's own open qty is 0                   EXITING -> VERIFY_FLAT; recon.verify_flat on post-fill reads:
                                                CLOSED or RESIDUAL -> eng.close(); a residual is never sold.
                                                Reads only, so this also runs after STOP_SENDING
  now >= STOP_SENDING                           no order of any kind; CRITICAL once ("broker square-off expected")
  < max(250 ms, the next fresh quote) after the last attempt ended            wait
  recon.broker_cap                              WAIT (positions/orders reads at EXIT priority, requested after this
                                                attempt was decided: the exit decision, or the end of the previous
                                                attempt; none for 10 s: an older post-fill read, CRITICAL);
                                                qty = min(open, cap); 0 -> external_wait: place nothing, ask recon
  a quote <= 1 s old, received after the last attempt ended   else a REST refresh; after STALE_PROTECTIVE_S
                                                without one: protective pricing from the last known bid, band >= 3
  no bids                                       NO_BID: no order into an empty book; REST re-quote at most 1/s;
                                                WARN after 10 s, CRITICAL from 15:20
  EXIT SELL IOC at exits.exit_limit (band >= 3 after EOD_BACKSTOP), or the band-reject re-price
When an EXIT order is terminal: a price-band reject keeps the band and re-prices at max(prev limit, bid - 2 ticks)
(three in a row: WARN); an under-fill raises the band by one (max 6). >= 5 attempts or >= 15 s exiting: EXIT_STUCK
WARN once; >= 10 attempts: HALT(EXIT_DIFFICULTY, BOOK, AUTO) + CRITICAL once.

The engine contract (core/state.py ExitLoopPort, core/engine.py docstring): step(eng, leg), on_order_terminal(eng,
order, leg) and cover(eng, plan) run on the core thread inside the engine's transaction and return the commands to
dispatch. Services used: eng.store settings quotes state now_mono now_wall eod create_order() close()
request_quotes() request_positions() request_orders() symbol_view() event() apply_halt(). RAM memory (re-anchored
after a reload): eng.state.cap_wait (broker_cap's wait start), eng.state.verify (verify-flat), eng.state.exit_memory
(the ladder: retry time, band rejects, alerts sent, the cover job).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import time as dtime
from typing import Any, Iterable

from titan2.clock import to_ist
from titan2.core import exits, positions, recon, risk
from titan2.core.events import Alert, Command, FetchOrders, FetchPositions, Priority
from titan2.core.recon import CoverPlan, LogEvent, VerifyState
from titan2.core.single_leg import decision_quote
from titan2.model import ATTEMPT_MAX, Account, Leg, LegState, Order, OrderState, Quote, Role, Side, Structure
from titan2.settings import BANDS, EXIT_QUOTE_MAX_AGE_S, EXIT_RETRY_MIN_S, PROTECTIVE_BAND

__all__ = [
    "ATTEMPT_PACING", "EXIT_DIFFICULTY_ATTEMPTS", "EXIT_STUCK_AFTER_S", "EXIT_STUCK_ATTEMPTS", "NO_BID_CRITICAL_AT",
    "NO_BID_WARN_AFTER_S", "REJECT_BACKOFF_MAX_S", "attempts_left", "cover", "on_order_terminal", "step",
]

# Fault-handling limits of §5.5 (they decide alerts, halts and pacing, never a price or a quantity).
EXIT_STUCK_ATTEMPTS = 5
EXIT_STUCK_AFTER_S = 15.0
EXIT_DIFFICULTY_ATTEMPTS = 10
BAND_REJECTS_WARN = 3
NO_BID_WARN_AFTER_S = 10.0
NO_BID_CRITICAL_AT = dtime(15, 20)
NO_BID_REQUOTE_S = 1.0
# Consecutive REJECTED exit orders back off 0.25, 0.5, 1, 2, 4 s (capped): a reject that repeats forever (circuit,
# RMS) must not become a 4-orders-a-second loop that burns the broker's order limits.
REJECT_BACKOFF_MAX_S = 4.0
# An order tag carries the leg's attempt number in two base-36 digits (model.ATTEMPT_MAX = 1295 per leg). A ladder
# that never fills (a book too thin for one lot, an LPP reject that never clears) retries every ~0.5 s and would
# use them up in ~15 min; make_tag would then raise inside the core loop on every Timer: an exception storm, exit 3
# and a crash loop that stops every exit. So retries slow down once a leg has made many attempts (the CRITICAL
# EXIT_DIFFICULTY went out at 10): at 20 s from the 240th, the budget lasts a whole session (09:20-15:25), and a leg
# that still uses it up stops sending (CRITICAL once; the broker's MIS square-off is the backstop).
ATTEMPT_PACING = ((240, 20.0), (60, 5.0))      # (attempts on the leg >=, minimum seconds between attempts)
MAX_BAND = len(BANDS) - 1


@dataclass(slots=True)
class _Cover:
    target: int                           # exactly the bot-explained short; only ever lowered by a later plan
    orders: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _LegMem:
    """The ladder's memory for one leg (eng.state.exit_memory[leg_id])."""

    since: float                          # exiting since (monotonic)
    last_attempt: float | None = None     # when the last order was created
    retry_at: float | None = None         # no new order before this (250 ms, longer after repeated rejects)
    fresh_after: float | None = None      # the next order needs a quote received after this (the last terminal)
    band_rejects: int = 0                 # consecutive price-band rejects
    prev_limit: float | None = None       # the limit of the last band-rejected order
    rejects: int = 0                      # consecutive REJECTED orders of any kind (backoff)
    last_requote: float | None = None     # NO_BID re-quote pacing
    no_bid_since: float | None = None
    no_bid_warned: bool = False
    no_bid_critical: bool = False
    stuck_warned: bool = False
    difficulty_raised: bool = False
    stop_sending_alerted: bool = False
    uncapped_reported: bool = False       # this attempt's EXIT_UNCAPPED / EXIT_CAP_BEFORE_DECISION was sent
    attempts_alerted: bool = False        # the leg used up its attempt numbers (CRITICAL sent once)
    cover: _Cover | None = None
    cover_band: int = 0


def _working(leg: Leg) -> bool:
    return any(o.role != Role.EXTERNAL and not o.is_terminal for o in leg.leg_orders())


def _attempt_decided(leg: Leg, mem: _LegMem) -> float | None:
    """When the next exit attempt was decided, for recon.broker_cap's B1 rule: the exit decision, or the end of the
    leg's previous exit or cover attempt if later. Every attempt pays its own positions read: the read that capped an
    unfilled IOC shows nothing of a close made since (a hand sale in the Kite app while the ladder runs, RUNBOOK P7)."""
    marks = [t for t in (_num(leg.exiting_since_mono), mem.fresh_after) if t is not None]
    return max(marks) if marks else None


def attempts_left(leg: Leg) -> int:
    """Order tags still available on the leg (orders.create_order numbers attempts 1 + the highest so far)."""
    return ATTEMPT_MAX - max((o.attempt for o in leg.leg_orders() if o.role != Role.EXTERNAL), default=0)


def _pacing(leg: Leg) -> float:
    used = ATTEMPT_MAX - attempts_left(leg)
    return next((gap for n, gap in ATTEMPT_PACING if used >= n), 0.0)


def _exhausted(eng: Any, st: Structure | None, leg: Leg, mem: _LegMem, what: str) -> list[Command]:
    """No attempt number left on the leg: send nothing more (never an exception in the core loop). CRITICAL once
    plus HALT(EXIT_DIFFICULTY); the broker's MIS square-off is the backstop."""
    if mem.attempts_alerted:
        return []
    mem.attempts_alerted = True
    text = (f"{leg.book} {leg.inst.symbol}: all {ATTEMPT_MAX} order attempts of this leg are used; no more {what} "
            f"orders for it ({leg.open_qty} still open). Close it in the Kite app; else the MIS square-off will")
    _log(eng, st, leg, [LogEvent("EXIT_STUCK", "CRITICAL", {"why": "ATTEMPTS_EXHAUSTED", "role": what,
                                                            "open_qty": leg.open_qty})])
    out = list(eng.apply_halt(risk.make_halt("EXIT_DIFFICULTY", f"BOOK:{leg.book}", text), alert=False))
    return out + [Alert("CRITICAL", "EXIT_ATTEMPTS_EXHAUSTED", text, f"attempts_exhausted:{leg.leg_id}")]


def _num(x: object) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _mem(eng: Any, leg: Leg) -> _LegMem:
    mem = eng.state.exit_memory.get(leg.leg_id)
    if not isinstance(mem, _LegMem):
        since = leg.exiting_since_mono if _num(leg.exiting_since_mono) is not None else eng.now_mono
        mem = eng.state.exit_memory[leg.leg_id] = _LegMem(since=since)
    return mem


def _log(eng: Any, st: Structure | None, leg: Leg, events: Iterable[LogEvent]) -> None:
    for e in events:
        eng.event(e.kind, dict(e.payload), e.severity, account=leg.account, book=leg.book,
                  strategy=None if st is None else st.strategy, structure_id=leg.structure_id, leg_id=leg.leg_id)


def _in_flight(eng: Any, account: Account) -> tuple[bool, bool]:
    bv = eng.state.broker[account]
    return bv.pos_pending > 0, bv.ords_pending > 0


def _reads(eng: Any, cmds: Iterable[Command]) -> list[Command]:
    """recon's read requests go out through the engine (deduped and counted in flight)."""
    out: list[Command] = []
    for c in cmds:
        if isinstance(c, FetchPositions):
            out += eng.request_positions(c.account, c.priority)
        elif isinstance(c, FetchOrders):
            out += eng.request_orders(c.account, c.priority)
        else:
            out.append(c)
    return out


# ---------------------------------------------------------------------------------------------------------------
# step: one pass for an EXITING or VERIFY_FLAT leg, or for any leg a cover hangs off
# ---------------------------------------------------------------------------------------------------------------
def step(eng: Any, leg: Leg) -> list[Command]:
    st = eng.state.structures.get(leg.structure_id)
    exiting = leg.state in (LegState.EXITING, LegState.VERIFY_FLAT)
    held = eng.state.exit_memory.get(leg.leg_id)
    covering = isinstance(held, _LegMem) and held.cover is not None
    if not exiting and not covering:
        if held is not None and not _working(leg):
            del eng.state.exit_memory[leg.leg_id]        # a finished cover's memory must not seed a later exit
        return []
    if st is None or _working(leg):
        return []
    mem = _mem(eng, leg)
    out: list[Command] = []
    if mem.cover is not None:                            # a cover hangs off any leg (§9.3): it runs first
        out += _cover_step(eng, st, leg, mem)
        if mem.cover is not None or not exiting:
            return out
    open_qty = leg.open_qty
    if open_qty <= 0:
        return out + _verify(eng, st, leg)
    if eng.now_wall >= eng.eod.stop_sending:
        if not mem.stop_sending_alerted:
            mem.stop_sending_alerted = True
            text = (f"{leg.book} {leg.inst.symbol}: {open_qty} not flat at STOP_SENDING; no more orders, "
                    "the broker's MIS square-off is expected")
            out.append(Alert("CRITICAL", "NOT_FLAT_STOP_SENDING", text, f"stop_sending:{leg.leg_id}"))
            _log(eng, st, leg, [LogEvent("EXIT_STUCK", "CRITICAL", {"why": "STOP_SENDING", "open_qty": open_qty})])
        return out
    out += _stuck_checks(eng, st, leg, mem)
    if mem.retry_at is not None and eng.now_mono < mem.retry_at:
        return out

    account = Account(leg.account)
    bv = eng.state.broker[account]
    pos_busy, ords_busy = _in_flight(eng, account)
    sv = eng.symbol_view(account, leg.inst.symbol)
    cap = recon.broker_cap(sv, open_qty, bv.positions, bv.orders, eng.state.cap_wait.get(leg.leg_id), eng.now_mono,
                           account=account, pos_in_flight=pos_busy, ords_in_flight=ords_busy,
                           decided_mono=_attempt_decided(leg, mem), pos_failed_mono=bv.pos_failed,
                           ords_failed_mono=bv.ords_failed)
    out += _reads(eng, cap.commands)
    if cap.wait:
        eng.state.cap_wait[leg.leg_id] = cap.wait_start
        return out
    if (cap.alerts or cap.events) and not mem.uncapped_reported:
        mem.uncapped_reported = True                     # a fallback cap is reported once per attempt
        out += list(cap.alerts)
        _log(eng, st, leg, cap.events)
    qty = min(open_qty, cap.qty)
    qty -= qty % leg.inst.lot_size
    if qty <= 0:
        return out + _reads(eng, recon.external_wait(sv, bv.positions, bv.orders, eng.now_mono, account=account,
                                                     pos_in_flight=pos_busy, ords_in_flight=ords_busy,
                                                     pos_failed_mono=bv.pos_failed, ords_failed_mono=bv.ords_failed))

    q, protective, ask = _quote(eng, leg.inst.symbol, mem)
    out += ask
    if q is None:
        return out
    if not q.bids:
        return out + _no_bid(eng, st, leg, mem, q)
    mem.no_bid_since, mem.no_bid_warned, mem.no_bid_critical = None, False, False

    band = min(leg.exit_band_idx, MAX_BAND)
    if protective or eng.now_wall >= eng.eod.backstop:
        band = max(band, PROTECTIVE_BAND)
    tick = leg.inst.tick
    if mem.band_rejects and mem.prev_limit is not None:
        limit = exits.band_reject_limit(mem.prev_limit, q.bid, tick)
    else:
        limit = exits.exit_limit(q, qty, tick, band, protective=protective)
    if limit is None:
        return out
    if attempts_left(leg) <= 0:
        return out + _exhausted(eng, st, leg, mem, "EXIT")
    if band != leg.exit_band_idx:
        leg.exit_band_idx = band
        eng.store.update_leg(leg)
    first = not any(o.role == Role.EXIT for o in leg.leg_orders())
    order, cmds = eng.create_order(leg, Role.EXIT, qty, limit, decision=decision_quote(q, qty, Side.SELL, eng.now_mono),
                                   band_idx=band)
    out += list(cmds)
    mem.last_attempt, mem.uncapped_reported = eng.now_mono, False
    eng.state.cap_wait.pop(leg.leg_id, None)
    payload = {
        "order_pk": order.order_pk, "attempt": order.attempt, "qty": qty, "open_qty": open_qty, "cap": cap.qty,
        "cap_source": cap.source, "limit": order.limit_px, "band": band, "protective": protective, "bid": q.bid,
        "ask": q.ask, "quote_src": q.source, "reason": leg.exit_reason}
    since = _num(leg.exiting_since_mono)
    if first and since is not None:
        # decision -> order dispatch, the Stage 2 exit criterion (p95 < 1.5 s); it includes the post-decision
        #   positions read decision B1 waits for (the daily report's exit_decision_to_submit_ms is the wall-time twin)
        payload["decision_to_order_ms"] = round(max(0.0, eng.now_mono - since) * 1000.0, 1)
    _log(eng, st, leg, [LogEvent("EXIT_ATTEMPT", "INFO", payload)])
    return out + _stuck_checks(eng, st, leg, mem)


def _quote(eng: Any, symbol: str, mem: _LegMem) -> tuple[Quote | None, bool, list[Command]]:
    """(quote to price from, protective, refresh request). A quote at most 1 s old and received after the last
    attempt ended; else a REST refresh, and after STALE_PROTECTIVE_S without one the last known quote,
    protectively."""
    fq = eng.quotes.fresh(symbol, EXIT_QUOTE_MAX_AGE_S, eng.now_mono)
    if fq is not None and mem.fresh_after is not None and fq.recv_mono <= mem.fresh_after:
        fq = None
    if fq is not None:
        return fq, False, []
    ask = list(eng.request_quotes((symbol,), Priority.EXIT))
    start = mem.since if mem.last_attempt is None else max(mem.since, mem.last_attempt)
    if eng.now_mono - start < eng.settings.STALE_PROTECTIVE_S:
        return None, False, ask
    return eng.quotes.last(symbol), True, ask


def _no_bid(eng: Any, st: Structure, leg: Leg, mem: _LegMem, q: Quote) -> list[Command]:
    """No order into an empty book: re-quote over REST at most 1/s, WARN after 10 s, CRITICAL from 15:20."""
    out: list[Command] = []
    now = eng.now_mono
    if mem.no_bid_since is None:
        mem.no_bid_since = now
        _log(eng, st, leg, [LogEvent("NO_BID", "INFO", {"symbol": leg.inst.symbol, "ask": q.ask,
                                                        "quote_src": q.source})])
    if mem.last_requote is None or now - mem.last_requote >= NO_BID_REQUOTE_S:
        ask = list(eng.request_quotes((leg.inst.symbol,), Priority.EXIT))
        if ask:
            mem.last_requote = now
            out += ask
    waited = now - mem.no_bid_since
    text = f"{leg.book} {leg.inst.symbol}: no bids for {waited:.0f}s while exiting {leg.open_qty}"
    if not mem.no_bid_warned and waited >= NO_BID_WARN_AFTER_S:
        mem.no_bid_warned = True
        out.append(Alert("WARN", "NO_BID", text, f"no_bid:{leg.leg_id}"))
        _log(eng, st, leg, [LogEvent("NO_BID", "WARN", {"symbol": leg.inst.symbol, "waited_s": round(waited, 1)})])
    if not mem.no_bid_critical and to_ist(eng.now_wall).time() >= NO_BID_CRITICAL_AT:
        mem.no_bid_critical = True
        out.append(Alert("CRITICAL", "NO_BID", text, f"no_bid_critical:{leg.leg_id}"))
    return out


def _stuck_checks(eng: Any, st: Structure, leg: Leg, mem: _LegMem) -> list[Command]:
    """>= 5 attempts or >= 15 s exiting: EXIT_STUCK WARN once; >= 10 attempts: HALT(EXIT_DIFFICULTY) + CRITICAL."""
    out: list[Command] = []
    attempts = sum(1 for o in leg.leg_orders() if o.role == Role.EXIT)
    exiting_s = eng.now_mono - mem.since
    if not mem.stuck_warned and (attempts >= EXIT_STUCK_ATTEMPTS or exiting_s >= EXIT_STUCK_AFTER_S):
        mem.stuck_warned = True
        text = f"{leg.book} {leg.inst.symbol}: still {leg.open_qty} open after {attempts} attempts, {exiting_s:.0f}s"
        out.append(Alert("WARN", "EXIT_STUCK", text, f"exit_stuck:{leg.leg_id}"))
        _log(eng, st, leg, [LogEvent("EXIT_STUCK", "WARN", {"attempts": attempts, "exiting_s": round(exiting_s, 1)})])
    if not mem.difficulty_raised and attempts >= EXIT_DIFFICULTY_ATTEMPTS:
        mem.difficulty_raised = True
        text = f"{leg.book} {leg.inst.symbol}: {attempts} exit attempts and still {leg.open_qty} open"
        cmds = list(eng.apply_halt(risk.make_halt("EXIT_DIFFICULTY", f"BOOK:{leg.book}", text)))
        if not any(isinstance(c, Alert) for c in cmds):     # the book's halt was already up: report this leg too
            cmds.append(Alert("CRITICAL", "EXIT_DIFFICULTY", text, f"exit_difficulty:{leg.leg_id}"))
        out += cmds
    return out


def _verify(eng: Any, st: Structure, leg: Leg) -> list[Command]:
    """The bot's own qty is 0: verify flat on post-fill broker reads (§9.4). CLOSED/RESIDUAL books the trade
    (eng.close); SHORT waits for recon's cover; WAIT asks for the reads."""
    if leg.state is LegState.EXITING:
        positions.to_verify_flat(eng.store, st, leg, now_mono=eng.now_mono, now_wall=eng.now_wall)
    state = eng.state.verify.get(leg.leg_id)
    if not isinstance(state, VerifyState):
        state = VerifyState(since_mono=eng.now_mono)
    account = Account(leg.account)
    bv = eng.state.broker[account]
    pos_busy, ords_busy = _in_flight(eng, account)
    d = recon.verify_flat(leg.leg_id, eng.symbol_view(account, leg.inst.symbol), bv.positions, bv.orders, state,
                          eng.now_mono, account=account, pos_in_flight=pos_busy, ords_in_flight=ords_busy,
                          pos_failed_mono=bv.pos_failed, ords_failed_mono=bv.ords_failed)
    eng.state.verify[leg.leg_id] = d.state
    _log(eng, st, leg, d.events)
    out: list[Command] = list(d.alerts)
    for h in d.halts:
        out += eng.apply_halt(h, alert=False)            # recon's own CRITICAL alert says it once
    out += _reads(eng, d.commands)
    if d.outcome not in ("CLOSED", "RESIDUAL"):
        return out
    if any(lg.state is not LegState.VERIFY_FLAT for lg in eng.state.legs_of(st)):
        return out                                       # phase 2: every leg of the structure verified first
    return out + list(eng.close(st, residual_qty=d.residual_qty))


# ---------------------------------------------------------------------------------------------------------------
# An exit or cover order is terminal
# ---------------------------------------------------------------------------------------------------------------
def on_order_terminal(eng: Any, order: Order, leg: Leg) -> list[Command]:
    """Set the next attempt's band and earliest time: a price-band reject keeps the band (the next limit is
    max(prev limit, bid - 2 ticks)); an under-fill raises it by one; the retry waits max(250 ms, a new quote)."""
    if Role(order.role) not in (Role.EXIT, Role.COVER):
        return []
    mem = _mem(eng, leg)
    cover = Role(order.role) is Role.COVER
    if cover and (mem.cover is None or order.order_pk not in mem.cover.orders):
        return []
    out: list[Command] = []
    state = OrderState(order.state)
    mem.fresh_after = eng.now_mono
    mem.rejects = mem.rejects + 1 if state is OrderState.REJECTED else 0
    mem.retry_at = eng.now_mono + max(min(EXIT_RETRY_MIN_S * 2 ** max(0, mem.rejects - 1), REJECT_BACKOFF_MAX_S),
                                      _pacing(leg))
    if state is OrderState.REJECTED and exits.is_band_reject(order.status_msg):
        mem.band_rejects += 1
        mem.prev_limit = order.limit_px
        if mem.band_rejects % BAND_REJECTS_WARN == 0:
            text = (f"{leg.book} {leg.inst.symbol}: {mem.band_rejects} price-band rejects in a row "
                    f"(last limit {order.limit_px}: {order.status_msg})")
            out.append(Alert("WARN", "EXIT_BAND_REJECTS", text, f"band_rejects:{leg.leg_id}"))
        return out
    mem.band_rejects, mem.prev_limit = 0, None
    if state is OrderState.NOT_PLACED or order.filled_qty >= order.qty:
        return out                                       # never reached the book, or fully filled: same band
    base = order.band_idx if order.band_idx is not None else (mem.cover_band if cover else leg.exit_band_idx)
    new = min(int(base) + 1, MAX_BAND)
    if cover:
        mem.cover_band = max(mem.cover_band, new)
    elif new > leg.exit_band_idx:
        leg.exit_band_idx = new
        eng.store.update_leg(leg)
    return out


# ---------------------------------------------------------------------------------------------------------------
# The cover ladder: BUY back a bot-explained short (recon CoverPlan, §9.3)
# ---------------------------------------------------------------------------------------------------------------
def cover(eng: Any, plan: CoverPlan) -> list[Command]:
    """Start (or advance) the cover of exactly plan.qty on plan.leg_id. A later plan can only lower what is still
    to buy (the latest confirmed short); it never adds to a running cover."""
    leg = eng.state.legs.get(plan.leg_id)
    if (leg is None or leg.inst.symbol != plan.symbol or not isinstance(plan.qty, int) or plan.qty <= 0
            or plan.qty % leg.inst.lot_size):
        text = f"cover of {plan.qty} {plan.symbol} on leg {plan.leg_id} cannot be placed: leg or qty mismatch"
        return [Alert("CRITICAL", "COVER_REFUSED", text, f"cover_refused:{plan.leg_id}")]
    st = eng.state.structures.get(leg.structure_id)
    mem = _mem(eng, leg)
    if mem.cover is None:
        mem.cover = _Cover(plan.qty)
        _log(eng, st, leg, [LogEvent("EXIT_DECISION", "CRITICAL", {"reason": "COVER", "qty": plan.qty,
                                                                    "symbol": plan.symbol})])
    else:
        done = _cover_done(eng, mem.cover)
        mem.cover.target = done + min(mem.cover.target - done, plan.qty)
    if _working(leg):
        return []
    return _cover_step(eng, st, leg, mem)


def _cover_done(eng: Any, job: _Cover) -> int:
    return sum(eng.state.orders[pk].filled_qty for pk in job.orders if pk in eng.state.orders)


def _cover_step(eng: Any, st: Structure | None, leg: Leg, mem: _LegMem) -> list[Command]:
    job = mem.cover
    done = _cover_done(eng, job)
    qty = job.target - done
    qty -= qty % leg.inst.lot_size
    if qty <= 0:
        mem.cover = None
        # the BUY ladder's reject memory must not price the leg's next EXIT SELL (a BUY limit sits above the bid)
        mem.band_rejects, mem.prev_limit, mem.rejects, mem.cover_band = 0, None, 0, 0
        _log(eng, st, leg, [LogEvent("EXIT_DECISION", "WARN", {"reason": "COVER", "done": done,
                                                                "target": job.target})])
        return []
    if eng.now_wall >= eng.eod.stop_sending:
        if not mem.stop_sending_alerted:
            mem.stop_sending_alerted = True
            return [Alert("CRITICAL", "NOT_FLAT_STOP_SENDING", f"{leg.inst.symbol}: short of {qty} not covered at "
                          "STOP_SENDING; the broker's MIS square-off is expected", f"cover_stop_sending:{leg.leg_id}")]
        return []
    if mem.retry_at is not None and eng.now_mono < mem.retry_at:
        return []
    q, protective, out = _quote(eng, leg.inst.symbol, mem)
    if q is None or not q.asks:
        return out                                       # no order into an empty book
    band = min(mem.cover_band, MAX_BAND)
    if protective or eng.now_wall >= eng.eod.backstop:
        band = max(band, PROTECTIVE_BAND)
    mem.cover_band = band
    limit = exits.cover_limit(q, qty, leg.inst.tick, band, protective=protective)
    if limit is None:
        return out
    if attempts_left(leg) <= 0:
        return out + _exhausted(eng, st, leg, mem, "COVER")
    order, cmds = eng.create_order(leg, Role.COVER, qty, limit, decision=decision_quote(q, qty, Side.BUY, eng.now_mono),
                                   band_idx=band)
    job.orders.append(order.order_pk)
    mem.last_attempt = eng.now_mono
    _log(eng, st, leg, [LogEvent("EXIT_ATTEMPT", "WARN", {
        "order_pk": order.order_pk, "role": "COVER", "qty": qty, "target": job.target, "limit": order.limit_px,
        "band": band, "protective": protective, "ask": q.ask})])
    return out + list(cmds)
