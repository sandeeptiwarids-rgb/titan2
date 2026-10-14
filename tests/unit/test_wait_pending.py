"""2026-10-14: an intent refused by G09 only because of pending structures waits for them (bounded by its TTL) and is
then routed normally; and a REST quote() whose connection dropped before any response is retried once (FINAL_SPEC
errata "Waiting for pending entries", "One REST quote retry"). Ported from the approved design's tests; the owner's
decision on a woken intent's RUNAWAY (refused, but no halt) replaces the design's default (the halt).

The 09:20 opening_drive batch of 2026-10-14 is replayed on the real Engine at the end (test_replay_0920_batch), and
end to end on both accounts in tests/scenarios/test_wait_for_pending.py."""

from __future__ import annotations

import dataclasses
import http.client
import itertools
import logging
from datetime import date, timedelta

import pytest
import requests
import urllib3

from titan2 import store as store_mod
from titan2.broker import kite as kite_mod
from titan2.broker.port import ReadFailed
from titan2.clock import FakeClock, ist_datetime
from titan2.core import gateway, risk
from titan2.core.engine import Engine, EngineContext
from titan2.core.events import (
    FetchMargins, FetchOrders, FetchPositions, FetchQuotes, FetchTradebook, IntentReceived, MarginsResult,
    OrdersSnapshot, Place, PositionsSnapshot, QuoteResult, RecoveryStart, Shutdown, Timer, TradebookSnapshot,
)
from titan2.core.single_leg import SingleLegHandler
from titan2.instruments import InstrumentStore
from titan2.market.quotes import QuoteCache
from titan2.model import (
    EXPIRY_RULE_NEAREST, Account, Direction, ExitProfile, FeedHealth, Instrument, Intent, Level, Margins, Mode,
    Quote, Recheck, Refusal, StrikePref, StructState, StructureType, Underlying,
)
from titan2.settings import DEFAULTS
from titan2.store import Store

from test_gateway_exitloop import (  # noqa: F401  (desk / make_desk are fixtures)
    CAL, EXPIRY, INFY, INFY_CE, INFY_PE, LIVE_BOOK, PAPER_BOOK, REL, REL_CE, REL_PE, STRAT, TCS, TCS_CE,
    TCS_PE, TICK, VER, alerts, cmds, desk, enter, events, make_desk, opt, structures_of,
)
from test_kite_lanes import StubKite, data as kite_data

NIFTY, BANK = "NSE:NIFTY 50", "NSE:NIFTY BANK"
WAIT = "WAIT"                                    # gateway.WAIT: the stage of a waiting intent's memory
_ids = itertools.count(1)


def intent_for(d, underlying: str, spot: float, ttl: float = 10.0) -> Intent:
    d.spot(underlying, spot)
    return Intent(intent_id=f"w{next(_ids):011d}", strategy=STRAT, strategy_version=VER, underlying=underlying,
                  direction=Direction.UP, structure=StructureType.LONG_CE, strike_pref=StrikePref.ATM,
                  expiry_rule=EXPIRY_RULE_NEAREST, trigger_px=spot, recheck=Recheck(spot * 0.995, spot * 1.01),
                  ttl_s=ttl, created_mono=d.clock.mono(), created_wall=d.clock.now_wall(), reason={"m": 1.0})


def waiting(d) -> list[str]:
    return [iid for iid, p in d.state.entry_memory.items() if getattr(p, "stage", None) == WAIT]


def routed(d, intent) -> list[dict]:
    return [e for e in events(d, "INTENT_ROUTED") if e["intent_id"] == intent.intent_id]


def route(d, intent, book=PAPER_BOOK):
    (r,) = [x for x in d.store.intent_routes(intent.intent_id) if x["book"] == book]
    return r["outcome"], r["gate"], r["detail"]


def held(d, book=PAPER_BOOK) -> int:
    return sum(1 for x in d.state.live_structures() if x.book == book)


# ---------------------------------------------------------------------------------------------------------------
# The gateway (FakeEngine)
# ---------------------------------------------------------------------------------------------------------------
def test_waits_for_pending_then_routes_normally(desk):
    """Two RESERVED structures hold both slots of PAPER:<s>; a third intent waits holding nothing, its row saying
    today's REFUSED meanwhile. The first reservation is refused at its decision: the next Timer routes the waiting
    intent as if it had just arrived, and it is decided on its own fresh snapshot with its placebo."""
    d = desk
    for u in (REL, INFY, TCS):
        d.spot(u)
    i1, i2 = d.intent(REL), d.intent(INFY)
    (f1,) = cmds(d.receive(i1), FetchQuotes)
    (f2,) = cmds(d.receive(i2), FetchQuotes)
    i3 = d.intent(TCS)
    out = d.receive(i3)
    assert cmds(out, FetchQuotes) == [] and structures_of(d, i3) == [] and held(d) == 2
    assert route(d, i3) == ("REFUSED", "BOOK_CAPS", "POSITIONS: 2/2")         # what a reload would leave
    (w,) = events(d, "INTENT_WAITING")
    assert w["severity"] == "INFO" and w["payload"]["ttl_s"] == i3.ttl_s and gateway.WAIT == WAIT
    p1, p2 = structures_of(d, i1)[0], structures_of(d, i2)[0]
    assert w["intent_id"] == i3.intent_id and w["payload"]["pending"] == [p1.structure_id, p2.structure_id]
    assert (w["payload"]["gate"], w["payload"]["detail"]) == ("BOOK_CAPS", "POSITIONS: 2/2")
    assert routed(d, i3) == [] and waiting(d) == [i3.intent_id]
    d.advance(0.1)
    assert cmds(d.timer(), FetchQuotes) == [] and waiting(d) == [i3.intent_id]  # still both pending: no event
    d.advance(0.1)
    d.book(REL_CE, 41.40, 42.10)                               # 1.7% spread: REL is refused at its decision
    d.book(REL_PE, 38.00, 38.10)
    d.answer(f1)
    assert p1.state is StructState.VOID and held(d) == 1
    d.advance(0.05)
    (f3,) = cmds(d.timer(), FetchQuotes)
    assert f3.request_id == i3.intent_id and set(f3.symbols) == {TCS_CE, TCS_PE, TCS}
    (st3,) = structures_of(d, i3)
    assert route(d, i3) == ("RESERVED", None, None) and st3.state is StructState.RESERVED and held(d) == 2
    (ev,) = routed(d, i3)
    assert ev["payload"]["waited_s"] == pytest.approx(0.25, abs=1e-3)
    assert ev["payload"]["waited_for"] == "BOOK_CAPS: POSITIONS: 2/2" and waiting(d) == []
    d.advance(0.2)
    q = d.book(TCS_CE, 60.00, 60.10)
    d.book(TCS_PE, 55.00, 55.10)
    places = cmds(d.answer(f3), Place)
    assert [p.account for p in places] == [Account.PAPER, Account.SHADOW]      # the parent and its placebo
    assert d.state.orders[places[0].order_pk].decision.ask == q.ask == 60.10   # decided on the fresh snapshot


def test_wait_ends_refused_exactly_as_today(desk):
    """Both pending entries fill: the waiting intent meets two positions and gets today's refusal, gate and detail."""
    d = desk
    _, _, o1, _ = enter(d, REL)
    _, _, o2, _ = enter(d, INFY, ce=(30.00, 30.10), pe=(29.00, 29.10))
    d.spot(TCS)
    i3 = d.intent(TCS)
    assert cmds(d.receive(i3), FetchQuotes) == [] and waiting(d) == [i3.intent_id]     # two ENTERING: pending
    d.advance(0.3)
    d.fill(o1, avg=42.10)
    assert cmds(d.timer(), FetchQuotes) == [] and waiting(d) == [i3.intent_id]          # one still pending
    d.fill(o2, avg=30.10)
    out = d.timer()
    assert cmds(out, FetchQuotes) == [] and structures_of(d, i3) == [] and waiting(d) == []
    assert route(d, i3) == ("REFUSED", "BOOK_CAPS", "POSITIONS: 2/2")
    (ev,) = routed(d, i3)
    assert ev["payload"]["outcome"] == "REFUSED" and ev["payload"]["waited_s"] > 0


def test_wait_ends_at_the_ttl(desk):
    d = desk
    for u in (REL, INFY, TCS):
        d.spot(u)
    d.receive(d.intent(REL))
    d.receive(d.intent(INFY))
    i3 = d.intent(TCS, ttl=3.0)
    d.receive(i3)
    d.advance(2.9)
    d.timer()
    assert waiting(d) == [i3.intent_id]
    d.advance(0.2)                                             # 3.1 s: G03 ends it (the fetches are not lost yet)
    out = d.timer()
    outcome, gate, detail = route(d, i3)
    assert (outcome, gate) == ("EXPIRED", "TTL") and detail.startswith("EXPIRED: 3.10 s > 3 s")
    assert [e["intent_id"] for e in events(d, "INTENT_EXPIRED")] == [i3.intent_id]
    assert cmds(out, FetchQuotes) == [] and structures_of(d, i3) == [] and waiting(d) == []


def test_waiting_intents_keep_their_arrival_order(desk):
    """The oldest waiting intent gets the first slot that frees, and a fresh intent never jumps the queue: on_intent
    routes the waiting ones first."""
    d = desk
    for u in (REL, INFY):
        d.spot(u)
    i1, i2 = d.intent(REL), d.intent(INFY)
    (f1,) = cmds(d.receive(i1), FetchQuotes)
    (f2,) = cmds(d.receive(i2), FetchQuotes)
    w1 = d.intent(TCS)
    d.spot(TCS)
    d.receive(w1)
    w2 = intent_for(d, NIFTY, 25000.0)
    d.receive(w2)
    assert waiting(d) == [w1.intent_id, w2.intent_id]
    d.book(REL_CE, 41.40, 42.10)
    d.book(REL_PE, 38.00, 38.10)
    d.answer(f1)                                               # REL's slot frees inside a QuoteResult ...
    n = intent_for(d, BANK, 56000.0)
    out = d.receive(n)                                         # ... and a fresh intent arrives before any Timer
    (fw1,) = cmds(out, FetchQuotes)
    assert fw1.request_id == w1.intent_id and route(d, w1)[0] == "RESERVED"
    assert waiting(d) == [w2.intent_id, n.intent_id]          # the fresh one queues behind
    assert [e["intent_id"] for e in events(d, "INTENT_WAITING")] == [w1.intent_id, w2.intent_id, n.intent_id]
    d.book(INFY_CE, 29.00, 29.70)                              # 2.4% spread: INFY is refused, its slot frees
    d.book(INFY_PE, 29.00, 29.10)
    d.answer(f2)
    d.spot(NIFTY, 25000.0)
    (fw2,) = cmds(d.timer(), FetchQuotes)
    assert fw2.request_id == w2.intent_id and waiting(d) == [n.intent_id]


def test_no_wait_when_the_slots_are_really_taken(desk):
    """Two OPEN positions: nothing pending to wait for, so the third intent is refused at once, as before."""
    d = desk
    _, _, o1, _ = enter(d, REL)
    _, _, o2, _ = enter(d, INFY, ce=(30.00, 30.10), pe=(29.00, 29.10))
    d.advance(0.3)
    d.fill(o1, avg=42.10)
    d.fill(o2, avg=30.10)
    d.spot(TCS)
    i3 = d.intent(TCS)
    d.receive(i3)
    assert route(d, i3) == ("REFUSED", "BOOK_CAPS", "POSITIONS: 2/2") and waiting(d) == []
    assert events(d, "INTENT_WAITING") == [] and len(routed(d, i3)) == 1


RUNAWAY = "RUNAWAY: 3 entry orders in 60 s (max 3)"


def _runaway_minute(d) -> tuple[Intent, list]:
    """A batch whose first IOCs fill nothing: REL's entry order (1) voids, INFY's (2) is out, TCS is reserved, then
    the NIFTY intent `c` arrives with both slots pending. TCS's order (3) fills nothing either, so a slot frees with
    three entry orders in the last 60 s; the next Timer routes `c` again. Returns (c, that Timer's commands)."""
    _, _, o0, _ = enter(d, REL)
    d.advance(0.3)
    d.fill(o0, filled=0)                                       # entry order 1: no fill, VOID
    enter(d, INFY, ce=(30.00, 30.10), pe=(29.00, 29.10))       # entry order 2: ENTERING
    d.spot(TCS)
    (fb,) = cmds(d.receive(d.intent(TCS)), FetchQuotes)       # RESERVED
    c = intent_for(d, NIFTY, 25000.0)
    d.receive(c)
    d.book(TCS_CE, 60.00, 60.10)
    d.book(TCS_PE, 55.00, 55.10)
    pl = [p for p in cmds(d.answer(fb), Place) if p.account is Account.PAPER][0]   # entry order 3
    d.advance(0.3)
    d.fill(d.state.orders[pl.order_pk], filled=0)              # VOID: a slot frees
    d.spot(NIFTY, 25000.0)
    return c, d.timer()


def test_woken_intent_refused_runaway_raises_no_halt(desk):
    """Routed normally means RUNAWAY too: three entry orders went out within 60 s while it waited, so G09 refuses
    the woken intent RUNAWAY, as it would refuse one arriving at that moment. The owner's decision (2026-10-14): a
    woken intent raises no HALT(RUNAWAY), because the orders it counts are those of the intents it waited behind.
    (Today the same intent would have been refused POSITIONS at arrival, with no halt.)"""
    d = desk
    c, out = _runaway_minute(d)
    (w,) = events(d, "INTENT_WAITING")
    assert w["intent_id"] == c.intent_id                       # it waited: 2 entry orders then, no RUNAWAY yet
    assert route(d, c) == ("REFUSED", "BOOK_CAPS", RUNAWAY) and waiting(d) == []
    assert [h.kind for h in d.state.halts] == [] and alerts(out, "HALT_RUNAWAY") == []
    (ev,) = routed(d, c)
    assert ev["payload"]["waited_s"] > 0 and ev["payload"]["waited_for"] == "BOOK_CAPS: POSITIONS: 2/2"
    assert cmds(out, FetchQuotes) == [] and structures_of(d, c) == []


def test_a_fresh_intent_in_that_minute_still_halts_the_book(desk):
    """The order-rate limit itself is unchanged: an intent ARRIVING within that minute is refused RUNAWAY and raises
    HALT(RUNAWAY, BOOK, ACK), as before, and never waits (a refusal with a halt cannot)."""
    d = desk
    _runaway_minute(d)
    n = intent_for(d, BANK, 56000.0)
    out = d.receive(n)
    assert route(d, n) == ("REFUSED", "BOOK_CAPS", RUNAWAY) and waiting(d) == []
    assert [h.kind for h in d.state.halts] == ["RUNAWAY"] and len(alerts(out, "HALT_RUNAWAY")) == 1
    assert n.intent_id not in [e["intent_id"] for e in events(d, "INTENT_WAITING")]


# The review of the held signals: G09's entry-order rate counted only the orders already made, never a RESERVED
#   structure about to send one, so one batch could send 4 entry orders inside a minute (MAX_ENTRY_ORDERS_PER_MIN 3)
#   with no halt. ENTRY_ORDERS counts them now, as orders still to come: a pending cap, so it waits and never halts.
ENTRY_ORDERS_PENDING = "ENTRY_ORDERS: 2 in 60 s + 1 reserved/3"


def _two_unfilled_iocs(d) -> tuple:
    """REL and INFY are reserved: their decision fetches (answered by _fill_nothing)."""
    for u in (REL, INFY, TCS):
        d.spot(u)
    (f1,) = cmds(d.receive(d.intent(REL)), FetchQuotes)
    (f2,) = cmds(d.receive(d.intent(INFY)), FetchQuotes)
    return f1, f2


def _fill_nothing(d, f1, f2) -> None:
    """REL and INFY send entry orders 1 and 2, and both IOCs fill nothing: both slots free."""
    d.advance(0.2)
    d.book(REL_CE, 42.00, 42.10)
    d.book(REL_PE, 38.00, 38.10)
    d.book(INFY_CE, 30.00, 30.10)
    d.book(INFY_PE, 29.00, 29.10)
    p1 = [p for p in cmds(d.answer(f1), Place) if p.account is Account.PAPER][0]
    p2 = [p for p in cmds(d.answer(f2), Place) if p.account is Account.PAPER][0]
    d.advance(0.3)
    d.fill(d.state.orders[p1.order_pk], filled=0)
    d.fill(d.state.orders[p2.order_pk], filled=0)
    d.advance(0.05)


def _tcs_and_nifty_books(d) -> None:
    d.book(TCS_CE, 60.00, 60.10)
    d.book(TCS_PE, 55.00, 55.10)
    d.book(opt("NIFTY", 25000, "CE"), 120.00, 120.10)
    d.book(opt("NIFTY", 25000, "PE"), 110.00, 110.10)


def _entry_orders(d) -> int:
    return d.store.scalar("SELECT COUNT(*) FROM orders WHERE book=? AND role='ENTRY'", (PAPER_BOOK,))


def test_one_batch_never_sends_more_entry_orders_a_minute_than_the_cap(desk):
    """Ported from the probe test_skc_p6_batch.py: one batch of four. REL and INFY are reserved, TCS and NIFTY wait
    behind them. Both first IOCs fill nothing, so both slots free before the next Timer, which woke TCS and NIFTY in
    one pass: each saw 2 entry orders in 60 s (under 3), both were reserved and both sent, 4 entry orders within a
    second and no halt. Now NIFTY meets TCS's order still to come and keeps waiting; once that order is out it is
    refused RUNAWAY without the halt (woken). Three orders."""
    d = desk
    f1, f2 = _two_unfilled_iocs(d)
    t, n = d.intent(TCS), intent_for(d, NIFTY, 25000.0)
    out = list(d.receive(t)) + list(d.receive(n))                  # the batch's 3rd and 4th signals wait
    assert waiting(d) == [t.intent_id, n.intent_id] and cmds(out, FetchQuotes) == []
    _fill_nothing(d, f1, f2)
    d.spot(TCS)
    d.spot(NIFTY, 25000.0)
    fetches = cmds(d.timer(), FetchQuotes)                         # one pass: TCS is reserved, NIFTY waits on
    woken = ([f.request_id for f in fetches], waiting(d), risk.gate_book_caps(d.gate_context(PAPER_BOOK)))
    d.advance(0.2)
    _tcs_and_nifty_books(d)
    for f in fetches:
        d.answer(f)                                                # TCS: entry order 3
    assert _entry_orders(d) == DEFAULTS.MAX_ENTRY_ORDERS_PER_MIN == 3, f"{_entry_orders(d)} entry orders in 1 s"
    assert woken == ([t.intent_id], [n.intent_id], Refusal("BOOK_CAPS", ENTRY_ORDERS_PENDING))
    assert route(d, n) == ("REFUSED", "BOOK_CAPS", "POSITIONS: 2/2")         # the row it started waiting with
    out = d.timer()
    assert route(d, n) == ("REFUSED", "BOOK_CAPS", RUNAWAY) and waiting(d) == [] and _entry_orders(d) == 3
    assert cmds(out, FetchQuotes) == [] and [h.kind for h in d.state.halts] == []
    assert alerts(out, "HALT_RUNAWAY") == []


def test_fresh_intents_after_unfilled_iocs_wait_for_the_reserved_ones_order(desk):
    """The same gap with fresh intents only (the probe test_skc_p6_fresh.py; it was there before the waits too):
    after two unfilled IOCs a fresh TCS is reserved and a fresh NIFTY was reserved beside it, the minute's 4th entry
    order. NIFTY now waits on TCS's order still to come (ENTRY_ORDERS: only pending structures in the way, so no
    halt), and is refused RUNAWAY, without the halt, once that order is out."""
    d = desk
    f1, f2 = _two_unfilled_iocs(d)
    _fill_nothing(d, f1, f2)
    d.spot(TCS)
    (f3,) = cmds(d.receive(d.intent(TCS)), FetchQuotes)
    n = intent_for(d, NIFTY, 25000.0)
    arrived = d.receive(n)
    d.advance(0.2)
    _tcs_and_nifty_books(d)
    for f in [f3, *cmds(arrived, FetchQuotes)]:
        d.answer(f)                                                # TCS: entry order 3 (and nothing for NIFTY)
    out = d.timer()
    assert _entry_orders(d) == 3, f"{_entry_orders(d)} entry orders within a second"
    (w,) = [e for e in events(d, "INTENT_WAITING") if e["intent_id"] == n.intent_id]
    assert w["payload"]["detail"] == ENTRY_ORDERS_PENDING and alerts(arrived, "HALT_RUNAWAY") == []
    assert route(d, n) == ("REFUSED", "BOOK_CAPS", RUNAWAY) and waiting(d) == []
    assert [h.kind for h in d.state.halts] == [] and alerts(out, "HALT_RUNAWAY") == []


def test_a_reserved_entry_that_voids_unsent_frees_its_order(desk):
    """ENTRY_ORDERS counts a reserved structure's order only while it may still go out: TCS is refused at its
    decision (a 3% spread: no entry limit) and voids without an order, so NIFTY, waiting on it, is reserved by the
    next Timer and sends the minute's third entry order."""
    d = desk
    f1, f2 = _two_unfilled_iocs(d)
    _fill_nothing(d, f1, f2)
    d.spot(TCS)
    t = d.intent(TCS)
    (f3,) = cmds(d.receive(t), FetchQuotes)
    n = intent_for(d, NIFTY, 25000.0)
    d.receive(n)
    assert waiting(d) == [n.intent_id]
    d.advance(0.2)
    d.book(TCS_CE, 59.00, 61.00)
    d.book(TCS_PE, 55.00, 55.10)
    d.answer(f3)
    assert structures_of(d, t)[0].state is StructState.VOID and _entry_orders(d) == 2
    d.spot(NIFTY, 25000.0)
    (f4,) = cmds(d.timer(), FetchQuotes)
    assert f4.request_id == n.intent_id and route(d, n)[0] == "RESERVED" and waiting(d) == []
    d.advance(0.2)
    _tcs_and_nifty_books(d)
    d.answer(f4)
    assert _entry_orders(d) == 3 and [h.kind for h in d.state.halts] == []


def test_operator_halt_while_waiting_diverts_like_a_fresh_intent(desk):
    d = desk
    for u in (REL, INFY, TCS):
        d.spot(u)
    (f1,) = cmds(d.receive(d.intent(REL)), FetchQuotes)
    d.receive(d.intent(INFY))
    i3 = d.intent(TCS)
    d.receive(i3)
    d.halt("PAUSE", operator=True)
    d.book(REL_CE, 41.40, 42.10)
    d.book(REL_PE, 38.00, 38.10)
    d.answer(f1)
    (f3,) = cmds(d.timer(), FetchQuotes)
    rows = {r["book"]: (r["outcome"], r["gate"]) for r in d.store.intent_routes(i3.intent_id)}
    assert rows == {PAPER_BOOK: ("DIVERTED", "HALTS"), "SHADOW:" + STRAT: ("RESERVED", None)}
    assert f3.request_id == i3.intent_id and d.store.session_flags(strategy=STRAT)[0]["flag"] == "IMPAIRED"


def test_a_daily_stop_raised_while_waiting_refuses_the_woken_intent(desk):
    """The wait never gets round a halt: the book's daily stop trips while the intent waits, and G04 refuses it when
    a slot frees, exactly as it would refuse an intent arriving then. Nothing is reserved for it."""
    d = desk
    for u in (REL, INFY, TCS):
        d.spot(u)
    (f1,) = cmds(d.receive(d.intent(REL)), FetchQuotes)
    d.receive(d.intent(INFY))
    i3 = d.intent(TCS)
    d.receive(i3)
    d.halt("DAILY_STOP", scope=f"BOOK:{PAPER_BOOK}")
    d.book(REL_CE, 41.40, 42.10)
    d.book(REL_PE, 38.00, 38.10)
    d.answer(f1)
    assert cmds(d.timer(), FetchQuotes) == [] and waiting(d) == [] and structures_of(d, i3) == []
    assert route(d, i3) == ("REFUSED", "HALTS", f"DAILY_STOP@BOOK:{PAPER_BOOK}")
    (ev,) = routed(d, i3)
    assert ev["payload"]["waited_for"] == "BOOK_CAPS: POSITIONS: 2/2"


def test_live_twin_waits_with_its_live_book(make_desk):
    """LIVE mode: LIVE:<s> decides the wait and its PAPER twin waits with it (no row of its own meanwhile), so on the
    wake both are reserved in one pass and decide on ONE fetch, one snapshot."""
    d = make_desk(mode=Mode.LIVE)
    for u in (REL, INFY, TCS):
        d.spot(u)
    i1, i2 = d.intent(REL), d.intent(INFY)
    (f1,) = cmds(d.receive(i1), FetchQuotes)
    cmds(d.receive(i2), FetchQuotes)
    i3 = d.intent(TCS)
    d.receive(i3)
    rows = {r["book"]: (r["outcome"], r["gate"]) for r in d.store.intent_routes(i3.intent_id)}
    assert rows == {LIVE_BOOK: ("REFUSED", "BOOK_CAPS")} and waiting(d) == [i3.intent_id]
    d.book(REL_CE, 41.40, 42.10)
    d.book(REL_PE, 38.00, 38.10)
    d.answer(f1)                                               # REL refused in both books: a slot in each
    (f3,) = cmds(d.timer(), FetchQuotes)
    rows = {r["book"]: r["outcome"] for r in d.store.intent_routes(i3.intent_id)}
    assert rows == {LIVE_BOOK: "RESERVED", PAPER_BOOK: "RESERVED"} and f3.request_id == i3.intent_id
    d.book(TCS_CE, 60.00, 60.10)
    d.book(TCS_PE, 55.00, 55.10)
    places = cmds(d.answer(f3), Place)
    assert [p.account for p in places] == [Account.LIVE, Account.PAPER, Account.SHADOW]
    live, paper = (d.state.orders[p.order_pk] for p in places[:2])
    assert live.decision == paper.decision


def test_live_twin_never_waits_on_its_own(make_desk):
    """The PAPER twin of a reserved LIVE book is refused as before when only its own pending structures block it:
    waiting would split the pair across two snapshots (X2 compares them)."""
    d = make_desk(mode=Mode.LIVE)
    for u in (REL, INFY, TCS):
        d.spot(u)
    d.live = dataclasses.replace(d.live, selftest_passed=False)
    i1 = d.intent(REL)
    d.receive(i1)                                              # LIVE refused (G13), the PAPER twin reserved
    d.live = dataclasses.replace(d.live, selftest_passed=True)
    d.receive(d.intent(INFY))                                  # LIVE 1 pending, PAPER 2 pending
    i3 = d.intent(TCS)
    (f3,) = cmds(d.receive(i3), FetchQuotes)
    rows = {r["book"]: (r["outcome"], r["gate"]) for r in d.store.intent_routes(i3.intent_id)}
    assert rows == {LIVE_BOOK: ("RESERVED", None), PAPER_BOOK: ("REFUSED", "BOOK_CAPS")}
    assert events(d, "INTENT_WAITING") == [] and waiting(d) == [] and f3.request_id == i3.intent_id


@pytest.mark.parametrize("blocker_voids", [True, False], ids=["voids", "opens"])
def test_entries_cap_waits_and_ends_both_ways(desk, blocker_voids):
    """'ENTRIES: 0 + 1 pending/1' waits (the same rule as POSITIONS): the pending entry voids -> reserved; it opens ->
    refused 'ENTRIES: 1 + 0 pending/1', exactly what an intent arriving then gets."""
    d = desk
    d.settings = dataclasses.replace(DEFAULTS, MAX_ENTRIES_PER_DAY=1)
    for u in (REL, TCS):
        d.spot(u)
    i1 = d.intent(REL)
    (f1,) = cmds(d.receive(i1), FetchQuotes)
    i2 = d.intent(TCS)
    d.receive(i2)
    (w,) = events(d, "INTENT_WAITING")
    assert w["payload"]["detail"] == "ENTRIES: 0 + 1 pending/1" and waiting(d) == [i2.intent_id]
    d.advance(0.2)
    d.book(REL_CE, 41.40, 42.10) if blocker_voids else d.book(REL_CE, 42.00, 42.10)
    d.book(REL_PE, 38.00, 38.10)
    out = d.answer(f1)
    if blocker_voids:
        assert structures_of(d, i1)[0].state is StructState.VOID
        (f2,) = cmds(d.timer(), FetchQuotes)
        assert f2.request_id == i2.intent_id and route(d, i2) == ("RESERVED", None, None)
    else:
        (pl,) = [p for p in cmds(out, Place) if p.account is Account.PAPER]
        assert cmds(d.timer(), FetchQuotes) == [] and waiting(d) == [i2.intent_id]    # ENTERING: still pending
        d.advance(0.3)
        d.fill(d.state.orders[pl.order_pk], avg=42.10)
        assert cmds(d.timer(), FetchQuotes) == [] and waiting(d) == []
        assert route(d, i2) == ("REFUSED", "BOOK_CAPS", "ENTRIES: 1 + 0 pending/1")


def test_wait_refused_at_the_no_new_after_cutoff(make_desk):
    d = make_desk(start="14:54:58")
    for u in (REL, INFY, TCS):
        d.spot(u)
    d.receive(d.intent(REL))
    d.receive(d.intent(INFY))
    i3 = d.intent(TCS)
    d.receive(i3)
    assert waiting(d) == [i3.intent_id]
    d.advance(2.5)                                             # 14:55:00.5: past NO_NEW_AFTER, inside every TTL
    assert cmds(d.timer(), FetchQuotes) == [] and waiting(d) == []
    assert route(d, i3) == ("REFUSED", "SESSION", "AFTER_NO_NEW_AFTER: 14:55:00 >= 14:55:00")


def test_a_gate_error_at_g09_never_waits(desk, monkeypatch):
    d = desk
    for u in (REL, INFY, TCS):
        d.spot(u)
    d.receive(d.intent(REL))
    d.receive(d.intent(INFY))
    real = risk.gate_book_caps         # raises on the real view only: settled() alone would let it wait
    monkeypatch.setattr(risk, "gate_book_caps", lambda ctx: 1 / 0 if ctx.book.pending else real(ctx))
    i3 = d.intent(TCS)
    d.receive(i3)
    assert route(d, i3) == ("REFUSED", "BOOK_CAPS", risk.GATE_ERROR) and waiting(d) == []
    assert events(d, "INTENT_WAITING") == [] and [e["payload"]["gate"] for e in events(d, "GATE_ERROR")] == [
        "BOOK_CAPS"]


# ---------------------------------------------------------------------------------------------------------------
# risk.settled
# ---------------------------------------------------------------------------------------------------------------
def _ctx(gc, la=None, **kw):
    return dataclasses.replace(gc, book=dataclasses.replace(gc.book, **kw), live_account=la)


def test_settled_frees_only_what_pending_structures_hold(desk):
    gc = desk.gate_context(PAPER_BOOK)
    s = risk.settled(_ctx(gc, open=1, reserved=1, pending=1, entries=1, attempts=1, orders_today=3,
                          entry_orders_60s=1)).book
    assert (s.open, s.reserved, s.pending, s.entries, s.attempts, s.orders_today, s.entry_orders_60s) == (
        1, 0, 0, 1, 1, 3, 1)
    la = risk.LiveAccountState(open=1, reserved=1, realized_net=0.0, mtm_net=0.0, active_underlyings=frozenset(),
                               pending=1)
    assert risk.settled(_ctx(gc, la, open=0, reserved=0, pending=0)).live_account.open == 1
    # nothing pending, or a malformed count: None (no wait, the refusal stands), never an exception
    assert risk.settled(_ctx(gc, open=2, reserved=0, pending=0)) is None
    for bad in (dict(pending=None), dict(pending=-1), dict(open=True), dict(reserved=1.0), dict(pending=3, open=1,
                                                                                                   reserved=1)):
        assert risk.settled(_ctx(gc, **{"open": 1, "reserved": 1, "pending": 1, **bad})) is None, bad


def test_waits_for_pending_matrix(desk):
    """The one rule: a BOOK_CAPS refusal with no halt and no gate error waits iff G01-G13 pass with the pending
    structures voided. POSITIONS, ENTRIES, ATTEMPTS, ENTRY_ORDERS (the review of the held signals) and
    LIVE_ACCOUNT_POSITIONS can wait; nothing else can."""
    d = desk
    d.spot(TCS)
    i = d.intent(TCS)
    gc = d.gate_context(PAPER_BOOK)

    def waits(ctx):
        res = risk.pre_reserve_gates(ctx, i)
        return res.refusal.detail if res.refusal else None, risk.waits_for_pending(ctx, i, res)

    assert waits(_ctx(gc, open=0, reserved=2, pending=2)) == ("POSITIONS: 2/2", True)          # two RESERVED
    assert waits(_ctx(gc, open=2, reserved=0, pending=2)) == ("POSITIONS: 2/2", True)          # two ENTERING
    assert waits(_ctx(gc, open=1, reserved=0, entries=2, pending=1)) == ("ENTRIES: 2 + 1 pending/3", True)
    assert waits(_ctx(gc, reserved=1, attempts=5, pending=1)) == ("ATTEMPTS: 5 + 1 reserved/6", True)
    assert waits(_ctx(gc, open=2, reserved=0, pending=0)) == ("POSITIONS: 2/2", False)         # two OPEN
    assert waits(_ctx(gc, open=2, reserved=0, pending=1)) == ("POSITIONS: 2/2", True)          # OPEN + ENTERING
    assert waits(_ctx(gc, open=1, entries=3, pending=0)) == ("ENTRIES: 3 + 0 pending/3", False)
    assert waits(_ctx(gc, reserved=1, pending=1, orders_today=40)) == ("ORDERS: 40/40", False)
    assert waits(_ctx(gc, reserved=1, pending=1, attempts=6)) == ("ATTEMPTS: 6 + 1 reserved/6", False)
    runaway = _ctx(gc, reserved=2, pending=2, entry_orders_60s=3)
    res = risk.pre_reserve_gates(runaway, i)
    assert res.halts and not risk.waits_for_pending(runaway, i, res)                          # RUNAWAY: never
    # the entry orders reserved structures are still to send: a pending cap, no halt, so it waits
    coming = _ctx(gc, reserved=1, pending=1, entry_orders_60s=2)
    assert waits(coming) == (ENTRY_ORDERS_PENDING, True) and not risk.pre_reserve_gates(coming, i).halts
    assert waits(_ctx(gc, open=1, pending=1, entry_orders_60s=2)) == (None, False)    # sent already: counted once
    same = _ctx(gc, reserved=2, pending=2, active_underlyings=frozenset({TCS}))
    assert waits(same) == ("POSITIONS: 2/2", False)                                            # G10 on settled
    expired = dataclasses.replace(_ctx(gc, reserved=2, pending=2), now_mono=i.created_mono + 11.0)
    assert waits(expired)[1] is False                                                          # G03 first
    bad = _ctx(gc, open=None, reserved=2, pending=2)
    assert waits(bad) == (f"BAD_COUNTERS: {(None, 2, 0, 0, 0, 0, 2)!r}", False)
    gerr = risk.GateOutcome(risk.Refusal("BOOK_CAPS", risk.GATE_ERROR), error="boom")
    assert not risk.waits_for_pending(_ctx(gc, reserved=2, pending=2), i, gerr)


def test_waits_for_the_live_account_cap(make_desk):
    d = make_desk(mode=Mode.LIVE)
    d.spot(TCS)
    i = d.intent(TCS)
    live = d.gate_context(LIVE_BOOK)
    for la_pending, want in ((1, True), (0, False)):
        la = risk.LiveAccountState(open=1, reserved=1, realized_net=0.0, mtm_net=0.0,
                                   active_underlyings=frozenset(), pending=la_pending)
        ctx = dataclasses.replace(live, book=dataclasses.replace(live.book, open=0, reserved=0, pending=0),
                                  live_account=la)
        res = risk.pre_reserve_gates(ctx, i)
        assert res.refusal.detail == "LIVE_ACCOUNT_POSITIONS: 2/2" and risk.waits_for_pending(ctx, i, res) is want


def test_settled_never_refuses_what_the_real_view_passes(desk):
    """Monotone: settled() only removes counts, so its G09 never refuses where the real G09 passes, and whenever an
    intent may wait the real refusal is one that counts pending structures (ENTRY_ORDERS since the review of the held
    signals: the entry orders reserved structures are still to send)."""
    gc = desk.gate_context(PAPER_BOOK)
    for o, r_, p, e, a, n60, od in itertools.product(range(3), range(3), range(4), range(4), (0, 5, 6), (0, 1, 2, 3),
                                                       (0, 40)):
        if p > o + r_:
            continue
        ctx = _ctx(gc, open=o, reserved=r_, pending=p, entries=e, attempts=a, entry_orders_60s=n60,
                   orders_today=od)
        real, alt = risk.gate_book_caps(ctx), risk.settled(ctx)
        if alt is not None and risk.gate_book_caps(alt) is not None:
            assert real is not None
        if real is not None and alt is not None and risk.gate_book_caps(alt) is None:
            assert real.detail.split(":")[0] in ("POSITIONS", "ENTRIES", "ATTEMPTS", "ENTRY_ORDERS"), real.detail


# ---------------------------------------------------------------------------------------------------------------
# The 09:20 batch of 2026-10-14 on the real Engine
# ---------------------------------------------------------------------------------------------------------------
D = date(2026, 10, 14)
OD, OD_VER = "opening_drive", "1.replay0920"
TATA, POLY = "NSE:TATAELXSI", "NSE:POLYCAB"


def _replay_store() -> InstrumentStore:
    unders = [Underlying(NIFTY, 256265, "NIFTY", "INDEX"), Underlying(BANK, 260105, "BANKNIFTY", "INDEX"),
              Underlying(TATA, 873217, "TATAELXSI", "STOCK"), Underlying(POLY, 2455041, "POLYCAB", "STOCK"),
              Underlying(TCS, 2953217, "TCS", "STOCK")]
    out, tok = [], itertools.count(80_000)
    for name, und, strikes, lot, expiries in (
            ("NIFTY", NIFTY, (25000, 25050), 65, (EXPIRY, date(2026, 11, 24))),
            ("BANKNIFTY", BANK, (56000, 56100), 30, (EXPIRY, date(2026, 11, 24))),
            ("TATAELXSI", TATA, range(3500, 3801, 50), 100, (EXPIRY,)),
            ("POLYCAB", POLY, range(7900, 8201, 50), 125, (EXPIRY,)),
            ("TCS", TCS, range(3040, 3161, 20), 175, (EXPIRY,))):
        for e in expiries:
            for k in strikes:
                for ot in ("CE", "PE"):
                    ts = f"{name}{e:%y%b}{k}{ot}".upper()
                    out.append(Instrument(f"NFO:{ts}", next(tok), ts, "NFO", name, und, ot, float(k), e, lot, TICK))
    return InstrumentStore(D, CAL, out, unders, min_stocks=1)


class Replay:
    """The real Engine and gateway, the §3.3 core loop inline (every event in one transaction, then the invariants),
    account reads answered at once, Timers every 0.25 s."""

    def __init__(self, tmp_path):
        self.clock = FakeClock(ist_datetime(D, "09:19:00"))
        self.cache = QuoteCache(mono=self.clock.mono)
        self.inst = _replay_store()
        h = SingleLegHandler()
        self.ctx = EngineContext(
            settings=DEFAULTS, calendar=CAL, clock=self.clock, quotes=self.cache, versions={OD: OD_VER},
            exit_profiles={OD: ExitProfile()}, handlers={StructureType.LONG_CE: h, StructureType.LONG_PE: h},
            instruments=lambda: self.inst, ban_list=lambda: frozenset(), pub_dir=tmp_path / "pub",
            feed_health=lambda: FeedHealth(True, self.clock.mono() - 600.0, self.clock.mono() - 0.1, 0, False),
            start_mono=self.clock.mono())
        self.ctx.prep.update({"INSTRUMENTS": True, "BAN_LIST": True, "PREV_BARS": True})
        self.store = Store(tmp_path / "titan2.db", backup_dir=tmp_path / "backups")
        self.eng = Engine.load(self.store, self.ctx)
        self.sent: list = []
        self.max_held = 0
        self.next_timer = self.clock.mono()

    def handle(self, ev) -> list:
        with self.store.tx():
            out = self.eng.handle(ev)
            self.eng.check_invariants()                        # I11 among them, after every event
        self.max_held = max(self.max_held, self.held())
        more = []
        for c in out:
            if isinstance(c, FetchPositions):
                more += self.handle(PositionsSnapshot(c.account, True, (), self.clock.mono(), self.clock.mono()))
            elif isinstance(c, FetchOrders):
                more += self.handle(OrdersSnapshot(c.account, True, (), self.clock.mono(), self.clock.mono()))
            elif isinstance(c, FetchTradebook):
                more += self.handle(TradebookSnapshot(c.account, True, (), self.clock.mono()))
            elif isinstance(c, FetchMargins):
                more += self.handle(MarginsResult(c.account, True, Margins(c.account, 500_000.0, {}),
                                                  self.clock.mono()))
        self.sent += out
        return out + more

    def held(self, book=f"PAPER:{OD}") -> int:
        return sum(1 for x in self.eng.state.live_structures() if x.book == book)

    def to(self, hhmmss: str) -> list:
        """Run the clock to hh:mm:ss.fff, a Timer every 0.25 s on the way (the core loop's cadence)."""
        target = ist_datetime(D, hhmmss[:8]) + timedelta(milliseconds=int(hhmmss[9:] or 0) if "." in hhmmss else 0)
        out = []
        while True:
            left = (target - self.clock.now_wall()).total_seconds()
            due = self.next_timer - self.clock.mono()
            if left <= 1e-9 and due > 1e-9:
                return out
            if due <= left + 1e-9:
                self.clock.advance(max(0.0, due))
                self.next_timer = self.clock.mono() + 0.25
                out += self.handle(Timer(self.clock.now_wall(), self.clock.mono()))
            else:
                self.clock.advance(left)

    def spot(self, sym: str, ltp: float) -> None:
        now = self.clock.now_wall()
        tok = self.inst.underlying(sym).token
        self.cache.put(Quote(sym, tok, (), (), ltp, 1_000_000, now, now, self.clock.mono(), now, "WS"))

    def book(self, sym: str, bids, asks) -> Quote:
        now = self.clock.now_wall()
        q = Quote(sym, self.inst.by_symbol(sym).token, tuple(Level(p, n) for p, n in bids),
                  tuple(Level(p, n) for p, n in asks), asks[0][0], 50_000, now - timedelta(seconds=2), now,
                  self.clock.mono(), now, "REST")
        self.cache.put(q)
        return q

    def intent(self, sym: str, direction: Direction, close: float, ret: float, vol: float) -> Intent:
        up = direction is Direction.UP
        lo, hi = (close * 0.995, close * 1.010) if up else (close * 0.990, close * 1.005)
        i = Intent(intent_id=f"od{next(_ids):010d}", strategy=OD, strategy_version=OD_VER, underlying=sym,
                   direction=direction, structure=StructureType.LONG_CE if up else StructureType.LONG_PE,
                   strike_pref=StrikePref.ITM1, expiry_rule=EXPIRY_RULE_NEAREST, trigger_px=close,
                   recheck=Recheck(lo, hi), ttl_s=10.0, created_mono=self.clock.mono(),
                   created_wall=self.clock.now_wall(), reason={"ret_pct": ret, "vol_mult": vol})
        self.handle(IntentReceived(i))
        return i

    def routes(self, i: Intent) -> list[tuple]:
        return [(r["outcome"], r["gate"], r["detail"]) for r in self.store.intent_routes(i.intent_id)]

    def structure(self, i: Intent):
        (st,) = [x for x in self.eng.state.structures.values() if x.intent.intent_id == i.intent_id
                 and not x.is_placebo]
        return st

    def of(self, kind, cmds_=None) -> list:
        return [c for c in (self.sent if cmds_ is None else cmds_) if isinstance(c, kind)]


@pytest.fixture
def replay(tmp_path):
    with store_mod.core_thread():
        r = Replay(tmp_path)
        try:
            r.handle(RecoveryStart())
            r.to("09:20:00")
            assert r.eng.state.entries_resumed and r.eng.phase == "TRADING"
            yield r
        finally:
            r.store.close()


def _batch(r: Replay) -> tuple[Intent, Intent, Intent, FetchQuotes, FetchQuotes]:
    """09:20:00.190-.212: opening_drive's batch, sorted by |ret|: TATAELXSI UP +5.06%, POLYCAB DOWN -2.72%, TCS UP
    +2.51% (vol 17.9x). Fresh LTPs for each, so each selects its ITM1 contract at once."""
    for sym, ltp in ((TATA, 3641.0), (POLY, 8052.0), (TCS, 3100.0)):
        r.spot(sym, ltp)
    r.to("09:20:00.190")
    tata = r.intent(TATA, Direction.UP, 3641.0, 5.06, 55.0)
    r.to("09:20:00.206")
    poly = r.intent(POLY, Direction.DOWN, 8052.0, -2.72, 7.5)
    r.to("09:20:00.212")
    tcs = r.intent(TCS, Direction.UP, 3100.0, 2.51, 17.9)
    f_tata, f_poly = r.of(FetchQuotes)
    assert (f_tata.request_id, f_poly.request_id) == (tata.intent_id, poly.intent_id)
    return tata, poly, tcs, f_tata, f_poly


def test_replay_0920_batch(replay):
    """2026-10-14 09:20: TATAELXSI and POLYCAB held both PAPER:opening_drive slots for 2 s and were then refused at
    their decisions (SIZE:DEPTH, SIZE:NO_ENTRY_LIMIT); TCS, refused 'POSITIONS: 2/2' at 09:20:00.212 with nothing
    open, now waits and is entered on its own fresh snapshot, inside its TTL. At no moment does PAPER:opening_drive
    hold more than 2 (I11 is checked after every event)."""
    r = replay
    tata, poly, tcs, f_tata, f_poly = _batch(r)
    book = f"PAPER:{OD}"
    assert r.routes(tata) == [("RESERVED", None, None)] and r.routes(poly) == [("RESERVED", None, None)]
    assert r.routes(tcs) == [("REFUSED", "BOOK_CAPS", "POSITIONS: 2/2")] and r.held() == 2
    (w,) = r.store.events(kind="INTENT_WAITING")
    assert w["intent_id"] == tcs.intent_id and w["payload"]["pending"] == sorted(
        [r.structure(tata).structure_id, r.structure(poly).structure_id])
    r.to("09:20:02.100")
    assert r.routes(tcs) == [("REFUSED", "BOOK_CAPS", "POSITIONS: 2/2")]       # still waiting: both pending
    # 09:20:02.192 TATAELXSI 3600CE: one lot of buyers within 1% of the mid (the rule needs 3)
    r.to("09:20:02.150")
    tata_ce = "NFO:TATAELXSI26OCT3600CE"
    r.book(tata_ce, [(65.00, 100), (60.00, 500)], [(65.50, 1000), (65.55, 1000), (65.60, 1000)])
    r.spot(TATA, 3642.0)
    r.to("09:20:02.192")
    r.handle(QuoteResult(f_tata.request_id, True, f_tata.symbols))
    assert r.structure(tata).state is StructState.VOID and r.structure(tata).void_reason == "SIZE:DEPTH"
    # 09:20:02.248 POLYCAB 8100PE: a 2.1% spread
    poly_pe = "NFO:POLYCAB26OCT8100PE"
    r.book(poly_pe, [(98.00, 1250), (97.95, 1250), (97.90, 1250)], [(100.10, 1250), (100.15, 1250), (100.20, 1250)])
    r.spot(POLY, 8050.0)
    r.to("09:20:02.248")
    r.handle(QuoteResult(f_poly.request_id, True, f_poly.symbols))
    assert r.structure(poly).void_reason == "SIZE:NO_ENTRY_LIMIT" and r.held() == 0
    assert [e["payload"]["gate"] for e in r.store.events(kind="ENTRY_REFUSED")] == ["SIZE", "SIZE"]
    # the next Timer (09:20:02.250) routes TCS as if it had just arrived: G01-G13 on fresh data, a fresh spot
    r.spot(TCS, 3101.0)
    out = r.to("09:20:02.250")
    (f_tcs,) = r.of(FetchQuotes, out)
    assert f_tcs.request_id == tcs.intent_id
    assert set(f_tcs.symbols) == {"NFO:TCS26OCT3080CE", "NFO:TCS26OCT3120PE", TCS}  # ITM1 CE, the ITM1 PE placebo
    assert r.routes(tcs) == [("RESERVED", None, None)]
    (ev,) = [e for e in r.store.events(kind="INTENT_ROUTED") if e["intent_id"] == tcs.intent_id]
    assert ev["payload"]["waited_s"] == pytest.approx(2.038, abs=1e-3)
    # its own decision snapshot: liquid books, the underlying inside the recheck band (G16)
    r.to("09:20:02.400")
    q = r.book("NFO:TCS26OCT3080CE", [(61.00, 5000), (60.95, 5000), (60.90, 5000)],
               [(61.10, 5000), (61.15, 5000), (61.20, 5000)])
    r.book("NFO:TCS26OCT3120PE", [(40.00, 5000), (39.95, 5000), (39.90, 5000)],
           [(40.10, 5000), (40.15, 5000), (40.20, 5000)])
    r.spot(TCS, 3102.0)
    out = r.handle(QuoteResult(f_tcs.request_id, True, f_tcs.symbols))
    parent, placebo = r.of(Place, out)
    assert (parent.account, parent.req.symbol, parent.req.qty) == (Account.PAPER, "NFO:TCS26OCT3080CE", 175)
    assert (placebo.account, placebo.req.symbol) == (Account.SHADOW, "NFO:TCS26OCT3120PE")
    st = r.structure(tcs)
    assert st.state is StructState.ENTERING and st.decision["quotes"]["NFO:TCS26OCT3080CE"]["ask"] == q.ask
    assert st.decision["quotes"][TCS]["ltp"] == 3102.0                     # G16 read the fresh LTP
    assert r.max_held <= 2 and r.eng.state.entry_memory.get(tcs.intent_id) is None


def test_replay_sigterm_or_reload_while_waiting_keeps_todays_refusal(replay):
    """A reload (rollback) or SIGTERM during the wait drops the RAM wait; the row stays REFUSED POSITIONS 2/2 and
    nothing is routed or placed for TCS afterwards."""
    r = replay
    tata, poly, tcs, f_tata, f_poly = _batch(r)
    r.eng = Engine.load(r.store, r.ctx)                        # what the core loop does after a rollback
    assert r.eng.state.entry_memory == {}
    r.to("09:20:01.000")
    r.handle(Shutdown("SIGTERM"))
    r.to("09:20:03.000")
    assert r.routes(tcs) == [("REFUSED", "BOOK_CAPS", "POSITIONS: 2/2")]
    assert [f.request_id for f in r.of(FetchQuotes)] == [tata.intent_id, poly.intent_id]


def test_engine_refreshes_book_counters_before_the_gateway_timer(replay, monkeypatch):
    """The waiting intents' G09 must see this Timer's own orders and closes: the engine drops its per-event counter
    cache (filled by status.json at the start of the event) before gateway.on_timer."""
    r = replay
    seen = []
    real = gateway.on_timer
    monkeypatch.setattr(gateway, "on_timer", lambda eng: (seen.append(dict(eng._counters)), real(eng))[1])
    r.to("09:20:02.000")                                       # status.json (1/s) fills the cache on some Timers
    assert len(seen) == 8 and all(s == {} for s in seen)


# ---------------------------------------------------------------------------------------------------------------
# GOAL B: one retry of a REST quote whose connection dropped before any response
# ---------------------------------------------------------------------------------------------------------------
def dropped() -> requests.exceptions.ConnectionError:
    """What requests raises when Kite has closed an idle keep-alive connection (urllib3 2.x, requests 2.32)."""
    return requests.exceptions.ConnectionError(urllib3.exceptions.ProtocolError(
        "Connection aborted.", http.client.RemoteDisconnected("Remote end closed connection without response")))


SYM = "NFO:NIFTY26OCT25000CE"
RAW = {SYM: {"instrument_token": 1234, "last_price": 12.4, "volume": 10,
             "depth": {"buy": [{"price": 12.35, "quantity": 65, "orders": 1}],
                       "sell": [{"price": 12.45, "quantity": 65, "orders": 1}]}}}


def test_quote_retried_once_after_a_dropped_connection(caplog):
    clock = FakeClock()
    answers = iter([dropped(), RAW])

    def answer(*_a):
        a = next(answers)
        if isinstance(a, BaseException):
            raise a
        return a

    kite = StubKite(quote=answer)
    kd = kite_data(kite, clock)
    t0 = clock.mono()
    with caplog.at_level(logging.WARNING, logger="titan2.broker.kite"):
        quotes = kd.quote([SYM])
    assert set(quotes) == {SYM} and kite.names() == ["quote", "quote"] and kd.dropped_retries == 1
    assert clock.mono() - t0 == pytest.approx(1.0, abs=0.01)  # the retry took a quote-bucket token (1/s): paced
    (rec,) = [x for x in caplog.records if "retrying once" in x.getMessage()]
    assert "Connection aborted." in rec.getMessage() and "(1 so far)" in rec.getMessage()


def test_quote_dropped_twice_fails_once_with_the_note():
    kite = StubKite(quote=dropped())
    kd = kite_data(kite)
    with pytest.raises(ReadFailed) as e:
        kd.quote([SYM])
    assert kite.names() == ["quote", "quote"] and kd.dropped_retries == 1
    assert e.value.text.startswith("AMBIGUOUS:ConnectionError: ") and e.value.text.endswith(
        "[retried once: the first try's connection dropped]") and len(e.value.text) <= 300


@pytest.mark.parametrize("exc", [
    requests.exceptions.ReadTimeout("HTTPSConnectionPool(host='api.kite.trade'): Read timed out."),
    requests.exceptions.ConnectTimeout("connect timeout"),
    requests.exceptions.ConnectionError(urllib3.exceptions.MaxRetryError(None, "/quote", reason=OSError("no route"))),
    requests.exceptions.SSLError(urllib3.exceptions.ProtocolError("Connection aborted.", OSError("ssl"))),
])
def test_quote_not_retried_otherwise(exc):
    kite = StubKite(quote=exc)
    kd = kite_data(kite)
    with pytest.raises(ReadFailed):
        kd.quote([SYM])
    assert kite.names() == ["quote"] and kd.dropped_retries == 0 and not kite_mod.dropped_before_response(exc)


def test_only_quote_retries():
    """positions(), orders() and margins() keep failing at once: the engine already asks again on its own cadence."""
    kite = StubKite(positions=dropped(), orders=dropped(), margins=dropped())
    kd = kite_data(kite)
    for read in (kd.positions, kd.orders_today, kd.margins):
        with pytest.raises(ReadFailed):
            read(Account.LIVE)
    assert kite.names() == ["positions", "orders", "margins"] and kite_mod.dropped_before_response(dropped())


@pytest.mark.parametrize("kind, retried", [("remote_closed", True), ("reset", True), ("read_timeout", False),
                                           ("refused", False), ("connect_timeout", False)])
def test_the_pinned_libraries_raise_what_the_retry_expects(monkeypatch, kind, retried):
    """The retry rests on the exception chain requests and urllib3 build (both pinned in requirements.lock). Here the
    real libraries build it: their connection methods are stubbed, so no socket is opened and nothing is resolved.
    A dropped connection (closed or reset before any answer) is retried; a read timeout and a failed or slow connect
    are not. If an upgrade changes the chain, this fails instead of the retry silently stopping."""
    import socket
    import urllib3.connection as uc

    def connect(self):
        if kind == "refused":
            raise urllib3.exceptions.NewConnectionError(self, "Failed to establish a new connection: [WinError 10061]")
        if kind == "connect_timeout":
            raise urllib3.exceptions.ConnectTimeoutError(self, "Connection to api.kite.trade timed out")
        self.sock = None

    def getresponse(self, *_a, **_k):
        if kind == "remote_closed":
            raise http.client.RemoteDisconnected("Remote end closed connection without response")
        if kind == "reset":
            raise ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host")
        raise socket.timeout("timed out")

    monkeypatch.setattr(uc.HTTPConnection, "connect", connect)
    monkeypatch.setattr(uc.HTTPConnection, "getresponse", getresponse)
    if kind not in ("refused", "connect_timeout"):
        monkeypatch.setattr(uc.HTTPConnection, "request", lambda self, *a, **k: None)
    session = requests.Session()
    session.trust_env = False                                  # no proxy from the environment
    with pytest.raises(requests.exceptions.RequestException) as e:
        session.get("http://api.kite.invalid/quote", timeout=5)
    assert kite_mod.dropped_before_response(e.value) is retried, repr(e.value)


def _pinned_error(monkeypatch, kind: str) -> BaseException:
    """What the pinned requests and urllib3 raise for one failure of a request whose connection is up (no socket is
    opened): the request's send or the status line's read fails as `kind` says."""
    import socket
    import ssl
    import urllib3.connection as uc

    def request(self, *_a, **_k):
        if kind == "send_timeout":
            raise socket.timeout("timed out")                  # a write timeout while sending the request
        if kind == "send_reset":
            raise ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host")

    def getresponse(self, *_a, **_k):
        if kind == "bad_status":
            raise http.client.BadStatusLine("garbage")
        if kind == "incomplete_headers":
            raise http.client.IncompleteRead(b"HTTP/1.1 200")
        if kind == "ssl_eof":
            raise ssl.SSLEOFError(8, "EOF occurred in violation of protocol")
        if kind == "aborted":
            raise ConnectionAbortedError(10053, "An established connection was aborted by the software in your host "
                                                "machine")
        raise http.client.RemoteDisconnected("Remote end closed connection without response")

    with monkeypatch.context() as mp:
        mp.setattr(uc.HTTPConnection, "connect", lambda self: setattr(self, "sock", None))
        mp.setattr(uc.HTTPConnection, "request", request)
        mp.setattr(uc.HTTPConnection, "getresponse", getresponse)
        session = requests.Session()
        session.trust_env = False
        with pytest.raises(requests.exceptions.RequestException) as e:
            session.get("http://api.kite.invalid/quote", timeout=5)
    return e.value


@pytest.mark.parametrize("kind, retried", [("send_timeout", False), ("bad_status", False),
                                           ("incomplete_headers", False), ("ssl_eof", False), ("send_reset", True),
                                           ("aborted", True)])
def test_only_a_connection_closed_before_any_answer_is_retried(monkeypatch, kind, retried):
    """The review of the retry (ported from the probe test_skc_probe_retry.py): urllib3 wraps more than a dropped
    connection in ProtocolError('Connection aborted.', ...). A timeout while the request was being sent (the request
    may be running, and the quote lane stayed blocked up to about 11 s instead of 5) and a malformed status line or
    headers (something did answer) were retried too, against the docs. Now only the other end closing the connection
    before any answer is (RemoteDisconnected after a reset while sending, or an abort, Windows' WSAECONNABORTED)."""
    exc = _pinned_error(monkeypatch, kind)
    assert kite_mod.dropped_before_response(exc) is retried, repr(exc)
    kite = StubKite(quote=exc)
    kd = kite_data(kite)
    with pytest.raises(ReadFailed) as e:
        kd.quote([SYM])
    assert kite.names() == ["quote", "quote"][:1 + retried] and kd.dropped_retries == int(retried)
    assert e.value.text.endswith(kite_mod.RETRIED_NOTE) is retried
