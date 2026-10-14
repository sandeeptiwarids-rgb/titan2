"""Regression tests for post-build review findings on titan2.core.exits, recon, orders, engine, gateway and operator.

- A WS tick received while an event runs (after its now_mono stamp) read as infinitely stale and closed a healthy OPEN
  leg as STALE_QUOTE_PROTECTIVE; it is age 0 now (exits.receipt_age), a gross clock-domain error still stale.
- Decision B1 (2026-10-11, strict): an exit is capped only by a positions read requested at or after its decision,
  however fresh an earlier one (EXIT_CAP_PRIOR_READ_S = 0; a close just before the decision is never sold again),
  and paper-sim pays that read's round trip as LIVE does. The two tests here replace the ones that pinned the 1 s
  window (option a): a read 0.4 s old capped the exit, and a protective exit went out on the read in hand. After the
  review of the B1 batch, every retry needs its own read, requested after the previous attempt ended (a hand sale
  while the ladder runs is not sold again), and failed post-decision reads no longer let an older read cap an exit
  before BROKER_READS_FAIL_UNCAP_S.
- DB-lost recovery reserved every lost seq in RECOVERED:<account>: a second seq on one underlying hit I5 and aborted
  the whole recovery. Flat seqs are now booked and closed at once, a second holding seq gets its own book, and a seq
  that still cannot be rebuilt is alerted without aborting the others.
- exit_loop and gateway were imported at first use, i.e. from whatever release `current` points to then.
- An order of an earlier session still WORKING / CANCEL_REQUESTED in the DB never resolved (the broker serves only
  today's orders), kept its symbol in recon grace and blocked entries: recovery concludes it (orders.session_ended).
- A session the bot never ran in was never marked IMPAIRED; nor was one in which an ACK halt blocked PAPER entries.
- SIGTERM's stopping state was RAM-only (a reload resumed trading). While stopping, no decision may create an ENTRY
  (a SHUTDOWN violation): the gateway voids the decision, and a placebo goes out with its parent at the decision
  (F-18), never at the fill. test_no_placebo_entry_while_stopping replaces the test that pinned the fill-time
  placebo (a PAPER fill while stopping recorded SKIPPED:SHUTDOWN).
- A lock held on the dashboard's command file blocked the core thread 5 s on every command poll.
"""

from __future__ import annotations

import dataclasses
import math
import sqlite3
import subprocess
import sys
import textwrap
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

from test_engine import INST, LOT, PAPER_BOOK, S, STRAT, Rig
from test_exits import EOD, Sim, book as exit_book
from titan2.clock import FakeClock, ist_datetime
from titan2.core import exits, operator, orders, recon
from titan2.core.engine import Engine
from titan2.core.events import (
    FetchOrders, FetchPositions, IntentReceived, OrderSnapshot, OrdersSnapshot, Place, PlaceResult, PositionsSnapshot,
    Priority, Shutdown, Timer,
)
from titan2.core.orders import OrderEvent, OrderTimers
from titan2.core.recon import SymbolView, TagImport, broker_cap
from titan2.model import (
    EXPIRY_RULE_NEAREST, Account, BrokerOrder, BrokerPosition, Direction, Intent, LegState, Order, OrderState, Recheck,
    Role, Side, StrikePref, StructState, StructureType, make_tag,
)
from titan2.settings import BROKER_READS_FAIL_UNCAP_S

TOOL_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------------------------------------------
# A tick received during the event that evaluates it is fresh
# ---------------------------------------------------------------------------------------------------------------
def test_decide_reads_a_tick_received_during_the_event_as_fresh():
    for ahead in (0.001, 0.25, 1.9):                      # kite-ws stamped it after the event's now_mono
        s = Sim()
        q = s.quote(bids=exit_book(32.5), age=-ahead)
        assert q.recv_mono > s.mono
        d = exits.decide(s.leg, q, None, s.mono, s.wall, s.p, EOD, None)
        assert isinstance(d, exits.NoAction), (ahead, d)
        assert exits.receipt_age(q, s.mono) == 0.0
    s = Sim()                                             # a receipt far in the future is a clock-domain bug
    q = s.quote(bids=exit_book(32.5), age=-(exits.RECEIPT_SKEW_S + 5.0))
    d = exits.decide(s.leg, q, None, s.mono, s.wall, s.p, EOD, None)
    assert isinstance(d, exits.Exit) and d.reason == "STALE_QUOTE_PROTECTIVE"
    assert exits.receipt_age(q, s.mono) == math.inf
    assert exits.receipt_age(q, float("nan")) == math.inf
    assert exits.receipt_age(dataclasses.replace(q, recv_mono=s.mono - 1.5), s.mono) == pytest.approx(1.5)


def test_a_timer_racing_a_ws_tick_keeps_a_healthy_leg_open(tmp_path, core):
    rig = Rig(tmp_path)
    rig.tick()
    leg = rig.open_leg()
    rig.book(42.10, 42.30)
    rig.tick(0.25)
    assert rig.leg(leg.leg_id).state is LegState.OPEN
    # the core loop stamps the next Timer, and a tick lands 2 ms later, while handle() publishes status.json
    t_mono, t_wall = rig.clock.mono() + 0.25, rig.clock.now_wall() + timedelta(seconds=0.25)
    rig.clock.advance(0.252)
    rig.book(42.10, 42.30)
    rig.run(Timer(t_wall, t_mono))
    lg = rig.leg(leg.leg_id)
    assert lg.state is LegState.OPEN and lg.exit_reason is None
    assert not rig.events("QUOTE_STALE")
    assert rig.eng.leg_mtm(lg) is not None                       # the MTM of the daily stop sees the bid too


# ---------------------------------------------------------------------------------------------------------------
# Decision B1: the exit cap needs a read requested after the decision
# ---------------------------------------------------------------------------------------------------------------
def _pos(req: float, net: int = 500) -> PositionsSnapshot:
    row = BrokerPosition(Account.PAPER, INST.symbol, INST.tradingsymbol, "NFO", "MIS", net, net, 0, 0.0, 0.0)
    return PositionsSnapshot(Account.PAPER, True, (row,), req, req)


def _ords(req: float, filled: int = 500) -> OrdersSnapshot:
    b = BrokerOrder(Account.PAPER, "P1", make_tag(Role.ENTRY, Account.PAPER, S, 1, 1), INST.symbol,
                    INST.tradingsymbol, "NFO", "MIS", Side.BUY, "LIMIT", "IOC", filled, filled, 42.0, "COMPLETE",
                    "COMPLETE", None, ist_datetime(S, "10:00:00"), None, "API")
    return OrdersSnapshot(Account.PAPER, True, (b,), req, req)


def test_broker_cap_needs_a_read_requested_after_the_decision():
    """B1 (strict): post-fill positions and orders reads requested 1 ms or 0.4 s before the decision cap nothing;
    the exit waits for a FetchPositions(EXIT). A read requested at or after the decision caps it at the broker net."""
    assert recon.EXIT_CAP_PRIOR_READ_S == 0
    sv = SymbolView(INST.symbol, expected=500, last_fill_mono=100.0)
    decided = 110.0
    for req in (decided - 0.001, decided - 0.4):
        for later in (0.0, 0.3):
            cap = broker_cap(sv, 500, _pos(req), _ords(req), None, decided + later, account=Account.PAPER,
                             decided_mono=decided)
            assert cap.wait and cap.commands == (FetchPositions(Account.PAPER, int(Priority.EXIT)),), (req, later)
    for req in (decided, decided + 0.2):                         # requested at or after the decision
        cap = broker_cap(sv, 500, _pos(req, net=250), None, None, decided + 0.3, account=Account.PAPER,
                         decided_mono=decided)
        assert (cap.qty, cap.source, cap.commands) == (250, "POSITIONS", ()), req
    # never a read from before the post-fill window, however close to the decision
    cap = broker_cap(sv, 500, _pos(101.0), None, None, decided, account=Account.PAPER, decided_mono=101.5)
    assert cap.wait


def test_a_protective_exit_waits_for_its_post_decision_read(tmp_path, core):
    """B1: force_exit 0.5 s after a post-fill read sends FetchPositions(EXIT) and no Place. The Place goes out the
    moment that read lands, and its EXIT_ATTEMPT carries decision_to_order_ms, the time it waited; a retry does not.
    The retry after an unfilled IOC waits for its own read, requested after that attempt ended (this test used to
    pin a retry sent at once on the first attempt's read: the review of the B1 batch)."""
    rig = Rig(tmp_path)
    rig.ctx.exit_loop = None                                     # the real titan2.core.exit_loop
    rig.eng = Engine.load(rig.store, rig.ctx)
    rig.tick()
    a = rig.open_leg()
    rig.clock.advance(3.0)
    with rig.store.tx():
        reads = rig.eng.request_positions(Account.PAPER)
    rig.answer(reads)                                            # a post-fill positions read ...
    rig.auto_reads = False
    rig.tick(0.5)                                                # ... 0.5 s before the decision
    rig.book(42.30, 42.50)
    with rig.store.tx():
        out = rig.eng.force_exit(rig.leg(a.leg_id), "OPERATOR_FLATTEN")
    assert rig.of(FetchPositions, out) == [FetchPositions(Account.PAPER, int(Priority.EXIT))]
    assert not rig.of(Place, out)
    decided = rig.leg(a.leg_id).exiting_since_mono
    rig.clock.advance(0.35)                                      # the positions round trip
    rig.book(42.30, 42.50)
    out = rig.core(PositionsSnapshot(Account.PAPER, True, rig.positions(Account.PAPER), decided, decided + 0.35))
    (pl,) = rig.of(Place, out)
    assert pl.req.role is Role.EXIT and pl.req.qty == LOT
    (ev,) = rig.events("EXIT_ATTEMPT")
    assert ev["payload"]["cap_source"] == "POSITIONS"
    assert ev["payload"]["decision_to_order_ms"] == pytest.approx(350.0, abs=0.1)
    rig.place_ok(pl.order_pk)
    rig.snap(pl.order_pk, "CANCELLED", 0)                        # IOC unfilled: the ladder tries again ...
    ended = rig.clock.mono()
    rig.clock.advance(0.3)
    rig.book(42.30, 42.50)
    out = rig.tick(0.25)
    assert not rig.of(Place, out)                                # ... once a read asked after attempt 1 ended lands
    assert rig.of(FetchPositions, out) == [FetchPositions(Account.PAPER, int(Priority.EXIT))]
    asked = rig.clock.mono()
    assert asked > ended
    rig.clock.advance(0.3)
    rig.book(42.30, 42.50)
    out = rig.core(PositionsSnapshot(Account.PAPER, True, rig.positions(Account.PAPER), asked, asked + 0.3))
    assert [p.req.role for p in rig.of(Place, out)] == [Role.EXIT]
    first, retry = rig.events("EXIT_ATTEMPT")
    assert "decision_to_order_ms" in first["payload"] and "decision_to_order_ms" not in retry["payload"]
    assert retry["payload"]["cap_source"] == "POSITIONS"


def test_a_retry_after_a_hand_sale_sells_nothing(tmp_path, core):
    """B1 for every attempt (review of the B1 batch): an IOC finds no buyer, then the operator sells the lot in the
    Kite app while the ladder still runs (RUNBOOK P7). The read that capped attempt 1 still shows the lot, but it was
    requested before attempt 1 ended, so it caps nothing now: the retry asks for a new positions read, which shows the
    account flat, and no second SELL goes out (external_wait). Before, every retry was capped by that first read and
    sold the lot again: a naked short that recon cannot cover, since the bot's own fills do not explain it."""
    rig = Rig(tmp_path)
    rig.ctx.exit_loop = None                                     # the real titan2.core.exit_loop
    rig.eng = Engine.load(rig.store, rig.ctx)
    rig.tick()
    a = rig.open_leg()
    rig.clock.advance(3.0)                                       # past the post-fill wait
    with rig.store.tx():
        reads = rig.eng.request_positions(Account.PAPER)
    rig.answer(reads)                                            # a post-fill read before the decision
    rig.auto_reads = False
    rig.tick(0.5)
    rig.book(42.30, 42.50)
    with rig.store.tx():
        out = rig.eng.force_exit(rig.leg(a.leg_id), "OPERATOR_FLATTEN")
    assert rig.of(FetchPositions, out)
    decided = rig.leg(a.leg_id).exiting_since_mono
    rig.clock.advance(0.3)
    rig.book(42.30, 42.50)
    out = rig.core(PositionsSnapshot(Account.PAPER, True, rig.positions(Account.PAPER), decided, decided + 0.3))
    (first,) = [p for p in rig.of(Place, out) if p.req.role is Role.EXIT]
    rig.place_ok(first.order_pk)
    rig.snap(first.order_pk, "CANCELLED", 0)                     # the IOC found no buyer
    ended = rig.clock.mono()
    rig.extra_nets[(Account.PAPER, INST.symbol)] = -LOT          # sold by hand in the Kite app: the broker is flat
    sells, asked = [], []
    for _ in range(4):                                           # past the 250 ms retry gap, with fresh quotes
        rig.clock.advance(0.3)
        rig.book(42.30, 42.50)
        out = rig.tick(0.0)
        sells += [p for p in rig.of(Place, out) if p.req.role is Role.EXIT]
        asked += [rig.clock.mono() for c in rig.of(FetchPositions, out) if c.priority == Priority.EXIT]
    assert not sells, f"retry SELL {sells[0].req.qty} capped by the read made before attempt 1 ended: a naked short"
    assert asked and asked[0] > ended                            # the retry asked for its own read ...
    rig.clock.advance(0.25)
    rig.book(42.30, 42.50)
    out = rig.core(PositionsSnapshot(Account.PAPER, True, rig.positions(Account.PAPER), asked[0], rig.clock.mono()))
    assert not [p for p in rig.of(Place, out) if p.req.role is Role.EXIT]   # ... which shows the account flat
    assert len(rig.events("EXIT_ATTEMPT")) == 1 and rig.leg(a.leg_id).open_qty == LOT


def test_failed_reads_after_the_decision_never_cap_from_an_older_read_at_once():
    """B1 is strict on the failure path too (review of the B1 batch): with the positions read (a 429) and the orders
    read (a timeout) made after the decision both failed, a read from 8 s before the decision caps nothing at
    decided + 0.6 s. The exit keeps waiting and re-asks each read VERIFY_FLAT_RECHECK_S after it failed. Only after
    BROKER_READS_FAIL_UNCAP_S does that older read cap it, CRITICAL, and never above what it shows (EXIT_UNCAPPED
    would sell the whole open qty). Before, two read errors let a read of any age cap the exit at once."""
    sv = SymbolView(INST.symbol, expected=500, last_fill_mono=100.0)
    decided = 130.0
    old = _pos(decided - 8.0)                                    # a post-fill read 8 s before the decision
    fails = dict(account=Account.PAPER, decided_mono=decided, pos_failed_mono=decided + 0.1,
                 ords_failed_mono=decided + 0.3)
    cap = broker_cap(sv, 500, old, None, decided, decided + 0.6, **fails)
    assert cap.wait and cap.commands == ()                       # both failed under 2 s ago: not re-asked yet
    cap = broker_cap(sv, 500, old, None, decided, decided + 2.4, **fails)
    assert cap.wait and cap.commands == (FetchPositions(Account.PAPER, int(Priority.EXIT)),
                                         FetchOrders(Account.PAPER, int(Priority.EXIT)))
    assert broker_cap(sv, 500, old, None, decided, decided + BROKER_READS_FAIL_UNCAP_S - 0.01, **fails).wait
    cap = broker_cap(sv, 500, old, None, decided, decided + BROKER_READS_FAIL_UNCAP_S, **fails)
    assert (cap.qty, cap.source) == (500, "POSITIONS_BEFORE_DECISION")
    assert [(a.severity, a.code) for a in cap.alerts] == [("CRITICAL", "EXIT_CAP_BEFORE_DECISION")]
    assert [(e.kind, e.severity) for e in cap.events] == [("EXIT_CAP_BEFORE_DECISION", "CRITICAL")]
    cap = broker_cap(sv, 500, _pos(decided - 8.0, net=0), None, decided, decided + BROKER_READS_FAIL_UNCAP_S, **fails)
    assert (cap.qty, cap.source) == (0, "POSITIONS_BEFORE_DECISION")       # a close that read saw is never sold


# ---------------------------------------------------------------------------------------------------------------
# DB-lost recovery with several lost seqs on one underlying
# ---------------------------------------------------------------------------------------------------------------
def _bord(acct: Account, role: Role, seq: int, qty: int, px: float, oid: str, minute: int) -> BrokerOrder:
    side = Side.SELL if role is Role.EXIT else Side.BUY
    return BrokerOrder(acct, oid, make_tag(role, acct, S, seq, 1), INST.symbol, INST.tradingsymbol, "NFO", "MIS", side,
                       "LIMIT", "IOC", qty, qty, px, "COMPLETE", "COMPLETE", None,
                       ist_datetime(S, f"10:{minute:02d}:00"), None, "API")


def _import(rig: Rig, acct: Account, *imports: TagImport) -> list:
    with rig.store.tx():
        out = rig.eng._import_tags(acct, imports)
        rig.eng.check_invariants()
    rig.cmds.extend(out)
    return out


def test_two_lost_paper_seqs_on_one_underlying_both_import(tmp_path, core):
    rig = Rig(tmp_path, instruments=True)
    rig.tick()
    acct = Account.PAPER
    book = f"RECOVERED:{acct.value}"
    imports = [TagImport(seq, None, book, (_bord(acct, Role.ENTRY, seq, LOT, 42.0 + seq / 10, f"P{seq}", seq),))
               for seq in (7, 8)]
    _import(rig, acct, *imports)
    rec = sorted((s for s in rig.eng.state.structures.values() if s.strategy == recon.RECOVERED_STRATEGY),
                 key=lambda s: s.structure_id)
    assert [(s.book, s.underlying) for s in rec] == [(book, INST.underlying), (f"{book}:8", INST.underlying)]
    assert all(not s.is_terminal for s in rec)
    assert sum(rig.eng.state.legs[s.legs[0]].open_qty for s in rec) == 2 * LOT
    assert not rig.alerts("RECOVERY_IMPORT")


def test_lost_live_seqs_close_the_flat_one_first(tmp_path, core):
    rig = Rig(tmp_path, instruments=True, allow_live=True)
    rig.tick()
    acct = Account.LIVE
    book = f"RECOVERED:{acct.value}"
    held = TagImport(3, None, book, (_bord(acct, Role.ENTRY, 3, LOT, 40.0, "L3", 5),))
    flat = TagImport(4, None, book, (_bord(acct, Role.ENTRY, 4, LOT, 41.0, "L4E", 6),
                                     _bord(acct, Role.EXIT, 4, LOT, 43.0, "L4X", 7)))
    _import(rig, acct, held, flat)                 # the held seq comes first by number: the flat one goes first
    rec = {s.legs[0]: s for s in rig.eng.state.structures.values() if s.strategy == recon.RECOVERED_STRATEGY}
    by_seq = {rig.eng.state.legs[lid].seq_no: s for lid, s in rec.items()}
    assert by_seq[4].state is StructState.CLOSED and by_seq[3].book == book and not by_seq[3].is_terminal
    trade = rig.store.trade(by_seq[4].structure_id)
    assert trade["qty"] == LOT and trade["exit_avg"] == 43.0 and trade["exit_reason"] == "RECOVERY_FLATTEN"
    # a second holding seq on the same LIVE underlying cannot be rebuilt (one LIVE structure per underlying): it is
    #   alerted and left to recon, and nothing aborts
    other = TagImport(5, None, book, (_bord(acct, Role.ENTRY, 5, LOT, 40.5, "L5", 8),))
    _import(rig, acct, other)
    (a,) = rig.alerts("RECOVERY_IMPORT")
    assert a.severity == "CRITICAL" and "seq 5" in a.text and "I5" in a.text


# ---------------------------------------------------------------------------------------------------------------
# The engine imports the exit loop and the gateway with itself, never at first use
# ---------------------------------------------------------------------------------------------------------------
def test_importing_the_engine_loads_the_exit_loop_and_the_gateway():
    code = textwrap.dedent("""
        import sys
        import titan2.core.engine as e
        missing = [m for m in ("titan2.core.exit_loop", "titan2.core.gateway") if m not in sys.modules]
        print(",".join(missing) or "OK")
    """)
    r = subprocess.run([sys.executable, "-c", code], cwd=str(TOOL_ROOT), capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.strip().splitlines()[-1] == "OK"


def test_the_engine_calls_the_modules_it_was_imported_with(tmp_path, core):
    import titan2.core.exit_loop as exit_loop_module
    import titan2.core.gateway as gateway_module
    rig = Rig(tmp_path)
    rig.ctx.exit_loop = rig.ctx.gateway = None
    eng = Engine.load(rig.store, rig.ctx)
    assert eng.exit_loop is exit_loop_module and eng.gateway is gateway_module
    assert "importlib" not in Path(sys.modules["titan2.core.engine"].__file__).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------------------------------------
# Orders of an earlier session
# ---------------------------------------------------------------------------------------------------------------
def test_session_ended_concludes_an_open_order_without_the_broker(tmp_path, core):
    rig = Rig(tmp_path)
    rig.tick()
    pk = rig.intent()
    o = rig.eng.state.orders[pk]
    t = OrderTimers()
    with rig.store.tx():
        fx = orders.session_ended(rig.store, o, t, rig.clock.mono(), rig.clock.now_wall(), session=S)
    assert o.state is OrderState.NOT_PLACED and o.broker_status is None      # never sent as far as anyone knows
    assert fx.unresolved and fx.became_terminal and [a.code for a in fx.commands] == ["STALE_STRUCTURE"]
    rig.place_ok(rig.intent("NSE:INFY"))
    w = rig.eng.state.orders[rig.gateway.placed[-1]]
    assert w.state is OrderState.WORKING
    with rig.store.tx():
        fx = orders.session_ended(rig.store, w, OrderTimers(), rig.clock.mono(), rig.clock.now_wall(), session=S)
    assert w.state is OrderState.CANCELLED and w.broker_status == "WORKING" and "outcome unknown" in w.status_msg
    with rig.store.tx():                                          # a terminal order is left as it is
        again = orders.session_ended(rig.store, w, OrderTimers(), rig.clock.mono(), rig.clock.now_wall())
    assert not again.became_terminal and not again.unresolved and again.commands == []
    assert orders.allowed(OrderState.CANCEL_REQUESTED, OrderEvent.SESSION_ENDED) == {OrderState.CANCELLED}
    assert orders.allowed(OrderState.UNKNOWN, OrderEvent.SESSION_ENDED) == {OrderState.NOT_PLACED}


def _working_leg(session: date, state: OrderState = OrderState.WORKING):
    from titan2.model import Leg
    pk = make_tag(Role.EXIT, Account.LIVE, session, 1, 2)
    entry = make_tag(Role.ENTRY, Account.LIVE, session, 1, 1)
    idx = {entry: Order(order_pk=entry, leg_id=1, structure_id=1, account=Account.LIVE, book="LIVE:x",
                        role=Role.ENTRY, side=Side.BUY, symbol=INST.symbol, qty=LOT, limit_px=42.0, attempt=1,
                        band_idx=None, state=OrderState.COMPLETE, filled_qty=LOT, avg_fill_px=42.0,
                        broker_order_id="E1"),
           pk: Order(order_pk=pk, leg_id=1, structure_id=1, account=Account.LIVE, book="LIVE:x", role=Role.EXIT,
                     side=Side.SELL, symbol=INST.symbol, qty=LOT, limit_px=40.0, attempt=2, band_idx=0, state=state,
                     broker_order_id="X1")}
    leg = Leg(leg_id=1, structure_id=1, leg_no=1, account=Account.LIVE, book="LIVE:x", session=session, seq_no=1,
              inst=INST, state=LegState.EXITING, orders=[entry, pk])
    return leg.bind_orders(idx)


def test_an_earlier_sessions_open_order_holds_no_symbol_in_grace():
    prev, today = date(2026, 10, 9), S
    for session, in_flight in ((prev, False), (today, True)):
        bot = recon.BotView(Account.LIVE, today, dtime_squareoff(), (_working_leg(session),), start_mono=1000.0)
        assert recon.symbol_view(bot, INST.symbol).in_flight is in_flight
    leg = _working_leg(prev)
    assert recon.stale_action(Account.LIVE, [leg]) == "CLOSE_UNRESOLVED"
    assert recon.stale_action(Account.PAPER, [leg]) == "AWAIT_PAPER_SQUAREOFF"


def dtime_squareoff():
    from titan2.settings import DEFAULTS
    return DEFAULTS.BROKER_SQUAREOFF_AT


# ---------------------------------------------------------------------------------------------------------------
# Missed sessions and long ACK halts are IMPAIRED
# ---------------------------------------------------------------------------------------------------------------
def test_a_session_the_bot_never_ran_in_is_impaired(tmp_path, core):
    rig = Rig(tmp_path, start="2026-10-09T15:00:00+05:30")             # Friday: heartbeats written
    rig.tick()
    rig.store.close()
    rig2 = Rig(tmp_path, clock=FakeClock("2026-10-13T08:45:00+05:30"))  # Tuesday pre-open; Monday was skipped
    rig2.tick()
    flags = {f["session"]: f["detail"] for f in rig2.store.session_flags(strategy=STRAT)}
    assert list(flags) == ["2026-10-12"] and "heartbeat gap" in flags["2026-10-12"]
    (ev,) = [e for e in rig2.events("SESSION_IMPAIRED")]
    assert ev["payload"]["session"] == "2026-10-12"
    rig2.store.close()


def test_a_start_one_minute_into_the_window_is_not_impaired(tmp_path, core):
    rig = Rig(tmp_path, start="2026-10-09T15:39:00+05:30")
    rig.tick()
    rig.store.close()
    rig2 = Rig(tmp_path, clock=FakeClock("2026-10-12T09:21:00+05:30"))  # 1 min of Monday's entry window missed
    rig2.tick()
    assert rig2.store.session_flags(strategy=STRAT) == []
    rig2.store.close()


def test_an_ack_halt_blocking_paper_entries_impairs_the_session(tmp_path, core):
    rig = Rig(tmp_path)
    rig.tick()
    with rig.store.tx():
        rig.eng.add_halt("STALE_FEED", "GLOBAL", "feed degraded")              # AUTO: no operator choice
        rig.eng.add_halt("ORDER_STUCK", f"BOOK:{PAPER_BOOK}", "stuck")           # ACK: waits for the operator
    for _ in range(9):
        rig.tick(60.0)
    assert rig.store.session_flags(strategy=STRAT, session=S) == []             # 9 minutes: not yet
    for _ in range(3):
        rig.tick(60.0)
    (flag,) = rig.store.session_flags(strategy=STRAT, session=S)
    assert flag["detail"].startswith("ORDER_STUCK") and "(ACK)" in flag["detail"]


# ---------------------------------------------------------------------------------------------------------------
# SIGTERM survives a reload; no placebo while stopping
# ---------------------------------------------------------------------------------------------------------------
def test_stopping_survives_an_engine_reload(tmp_path, core):
    rig = Rig(tmp_path)
    rig.tick()
    rig.run(Shutdown("SIGTERM"))
    stop = rig.eng.state.stop_mono
    assert rig.eng.state.stopping and stop is not None
    rig.clock.advance(2.0)
    rig.eng = Engine.load(rig.store, rig.ctx)                      # a rollback inside the shutdown window
    assert rig.eng.state.stopping and rig.eng.state.stop_mono == stop
    placed = len(rig.gateway.placed)
    rig.run(IntentReceived(dataclasses.replace(_intent(rig), intent_id="i-after-reload")))
    assert len(rig.gateway.placed) == placed                       # entries stay stopped
    (ev,) = [e for e in rig.events("INTENT_RECEIVED") if e["payload"].get("refused") == "STOPPING"]


def _intent(rig: Rig) -> Intent:
    return Intent(intent_id="i-shutdown", strategy=STRAT, strategy_version="1.0abc", underlying="NSE:RELIANCE",
                  direction=Direction.UP, structure=StructureType.LONG_CE, strike_pref=StrikePref.ATM,
                  expiry_rule=EXPIRY_RULE_NEAREST, trigger_px=2900.0, recheck=Recheck(-math.inf, math.inf),
                  ttl_s=10.0, created_mono=rig.clock.mono(), created_wall=rig.clock.now_wall(), reason={})


class _RealGateway:
    """The real Engine with the real gateway, SingleLegHandler and exit loop (tests/unit/test_gateway_exitloop.py's
    round trip); every account read is answered at once."""

    def __init__(self, tmp_path: Path) -> None:
        from test_gateway_exitloop import CAL, INSTRUMENTS, REL, REL_CE, REL_PE, STRAT as G_STRAT, TICK, VER
        from titan2.core.single_leg import SingleLegHandler
        from titan2.core.state import EngineContext
        from titan2.market.quotes import QuoteCache
        from titan2.model import ExitProfile, FeedHealth
        from titan2.settings import DEFAULTS
        from titan2.store import Store
        self.REL, self.REL_CE, self.REL_PE, self.STRAT, self.VER, self.TICK = REL, REL_CE, REL_PE, G_STRAT, VER, TICK
        self.inst = INSTRUMENTS
        self.clock = clock = FakeClock(ist_datetime(S, "09:58:00"))
        self.cache = QuoteCache(mono=clock.mono)
        h = SingleLegHandler()
        self.ctx = EngineContext(settings=DEFAULTS, calendar=CAL, clock=clock, quotes=self.cache,
                                 versions={G_STRAT: VER}, exit_profiles={G_STRAT: ExitProfile()},
                                 handlers={StructureType.LONG_CE: h, StructureType.LONG_PE: h},
                                 instruments=lambda: INSTRUMENTS, ban_list=lambda: frozenset(),
                                 feed_health=lambda: FeedHealth(True, clock.mono() - 600.0, clock.mono() - 0.1, 0,
                                                                False),
                                 start_mono=clock.mono())
        self.ctx.prep.update({"INSTRUMENTS": True, "BAN_LIST": True, "PREV_BARS": True})
        self.store = Store(tmp_path / "titan2.db", backup_dir=tmp_path / "backups")
        self.eng = Engine.load(self.store, self.ctx)

    def book(self, sym: str, bid: float, ask: float) -> None:
        from titan2.model import Level, Quote
        now = self.clock.now_wall()
        t = self.TICK
        self.cache.put(Quote(sym, self.inst.by_symbol(sym).token,
                             tuple(Level(round(bid - i * t, 2), 5000) for i in range(3)),
                             tuple(Level(round(ask + i * t, 2), 5000) for i in range(3)), ask, 1,
                             now - timedelta(seconds=2), now, self.clock.mono(), now, "WS"))

    def handle(self, ev) -> list:
        from titan2.core.events import (
            FetchMargins, FetchOrders, FetchTradebook, MarginsResult, OrdersSnapshot, TradebookSnapshot,
        )
        from titan2.model import Margins
        with self.store.tx():
            out = self.eng.handle(ev)
            self.eng.check_invariants()
        m = self.clock.mono()
        for c in list(out):
            if isinstance(c, FetchPositions):
                out += self.handle(PositionsSnapshot(c.account, True, (), m, m))
            elif isinstance(c, FetchOrders):
                out += self.handle(OrdersSnapshot(c.account, True, (), m, m))
            elif isinstance(c, FetchTradebook):
                out += self.handle(TradebookSnapshot(c.account, True, (), m))
            elif isinstance(c, FetchMargins):
                out += self.handle(MarginsResult(c.account, True, Margins(c.account, 500_000.0, {}), m))
        return out

    def ready(self) -> None:
        from titan2.core.events import RecoveryStart
        self.handle(RecoveryStart())
        for _ in range(40):
            self.clock.advance(0.5)
            self.handle(Timer(self.clock.now_wall(), self.clock.mono()))
        assert self.eng.state.entries_resumed

    def receive(self, underlying: str, intent_id: str, spot: float = 2900.0):
        """An intent on underlying (its spot fresh): returns the decision fetch (FetchQuotes)."""
        from titan2.core.events import FetchQuotes
        from titan2.model import Quote
        now = self.clock.now_wall()
        self.cache.put(Quote(underlying, self.inst.underlying(underlying).token, (), (), spot, 1, now, now,
                             self.clock.mono(), now, "WS"))
        intent = Intent(intent_id, self.STRAT, self.VER, underlying, Direction.UP, StructureType.LONG_CE,
                        StrikePref.ATM, EXPIRY_RULE_NEAREST, spot, Recheck(spot * 0.995, spot * 1.01), 10.0,
                        self.clock.mono(), now, {"m": 1.0})
        (f,) = [c for c in self.handle(IntentReceived(intent)) if isinstance(c, FetchQuotes)]
        return f

    def decide(self, f) -> list:
        """The decision snapshot lands: returns the Places it sent."""
        from titan2.core.events import QuoteResult
        self.clock.advance(0.2)
        self.book(self.REL_CE, 42.00, 42.10)
        self.book(self.REL_PE, 38.00, 38.10)
        return [c for c in self.handle(QuoteResult(f.request_id, True, f.symbols)) if isinstance(c, Place)]

    def fill(self, pl: Place, px: float = 42.10, broker_id: str = "P000000001") -> list:
        self.clock.advance(0.3)
        out = self.handle(PlaceResult(pl.account, pl.order_pk, "OK", broker_id))
        return out + self.handle(OrderSnapshot(pl.account, broker_id, pl.order_pk, "COMPLETE", pl.req.qty, px, None,
                                               None, "WS", self.clock.mono()))


def test_no_placebo_entry_while_stopping(tmp_path, core):
    """SIGTERM stops entries (§9.5 step 17): after Shutdown no decision creates a parent or a placebo ENTRY (the
    gateway voids the RESERVED structures, SHUTDOWN:...) and there is no SHUTDOWN violation. A parent decided before
    the Shutdown already sent its placebo with it (F-18), and its fill during stopping sends nothing new."""
    h = _RealGateway(tmp_path)
    try:
        h.ready()
        parent, placebo = h.decide(h.receive(h.REL, "rv0000000001"))   # one decision, both orders
        assert (parent.account, placebo.account, placebo.req.symbol) == (Account.PAPER, Account.SHADOW, h.REL_PE)
        late = h.receive("NSE:INFY", "rv0000000002", 1500.0)            # a second intent waits for its snapshot ...
        h.handle(Shutdown("SIGTERM"))                                    # ... when a deploy lands
        out = h.fill(parent)                                             # the first parent fills while stopping
        assert not [c for c in out if isinstance(c, Place)]
        st = h.eng.state.structures[h.eng.state.orders[parent.order_pk].structure_id]
        assert st.state is StructState.OPEN and st.placebo_status == "OPENED"
        assert not h.decide(late)                                        # its snapshot lands: nothing is sent
        voided = [s for s in h.eng.state.structures.values() if s.intent.intent_id == "rv0000000002"]
        assert [(s.state, s.void_reason.split(":", 1)[0]) for s in voided] == [(StructState.VOID, "SHUTDOWN")]
        entries = [o for o in h.eng.state.orders.values() if o.role is Role.ENTRY]
        assert sorted(o.order_pk for o in entries) == sorted((parent.order_pk, placebo.order_pk))
        assert not h.store.events(kind="INVARIANT_VIOLATION") and not h.store.events(kind="PLACEBO_SKIPPED")
        assert h.eng.state.stopping
    finally:
        h.store.close()


# ---------------------------------------------------------------------------------------------------------------
# A lock on the dashboard's command file never stalls the core thread
# ---------------------------------------------------------------------------------------------------------------
def test_a_locked_command_file_is_skipped_at_once(tmp_path, core):
    rig = Rig(tmp_path)
    rig.tick()
    holder = sqlite3.connect(str(rig.dash), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")                             # a wedged or hostile dashboard process
    try:
        t0 = time.monotonic()
        with pytest.raises(sqlite3.OperationalError):
            operator.read_new(rig.dash, "DASH", 0)
        assert time.monotonic() - t0 < 0.25
        t0 = time.monotonic()
        rig.tick(1.0)                                              # the Timer's 1 Hz poll
        assert time.monotonic() - t0 < 1.0
        assert [a.severity for a in rig.alerts("COMMANDS_UNREADABLE")] == ["WARN"]
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    rig.tick(1.0)                                                  # readable again: polled as usual
    assert len(rig.alerts("COMMANDS_UNREADABLE")) == 1
