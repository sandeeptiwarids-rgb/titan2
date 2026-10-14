"""U10: the order state machine (titan2.core.orders) and the structure/leg state machine (titan2.core.positions).

Real Store (tmp), real ledger/charges/exits; a FakeClock drives the monotonic timers. Rig plays the engine: it keeps
the order index the legs are bound to, the per-order timers, and runs every handler inside store.tx().
"""

from __future__ import annotations

import dataclasses
import itertools
import math
from datetime import date, datetime

import pytest

from titan2 import charges, store as store_mod
from titan2.charges import OrderFills, rates_for
from titan2.clock import FakeClock
from titan2.core import orders, positions
from titan2.core.events import (
    Alert, Cancel, CancelResult, FetchTrades, LookupResult, LookupTag, OrderSnapshot, Place, PlaceResult, PollOrder,
    Subscribe, Timer, TradesResult,
)
from titan2.core.orders import Effects, IllegalTransition, OrderEvent, OrderTimers
from titan2.core.recon import ExternalClose, ExternalFill, ReconClass
from titan2.model import (
    EXPIRY_RULE_NEAREST, Account, BrokerOrder, BrokerTrade, DecisionQuote, Direction, ExitProfile, Instrument,
    Intent, InvariantViolation, Leg, LegState, Order, OrderState, PnlStatus, Policy, Recheck, Role, Side,
    StrikePref, Structure, StructState, StructureType, Urgency,
)
from titan2.store import Store

SESSION = date(2026, 10, 12)
LOT_SIZE = 100                          # NSE fills come in whole lots: partial fills below are lot multiples
QTY = 500                               # a 5-lot order, so an IOC can fill part of it
INST = Instrument("NFO:RELIANCE26OCT2900CE", 12345, "RELIANCE26OCT2900CE", "NFO", "RELIANCE", "NSE:RELIANCE", "CE",
                  2900.0, date(2026, 10, 27), LOT_SIZE, 0.05)
ENTRY_DQ = DecisionQuote(bid=41.90, ask=42.15, mid=42.025, age_ms=180, src="REST", ltp=42.10, walk_px=42.15)
ENTRY_LIMIT = 42.25                     # exits.entry_limit: min(ask + 2 ticks, floor_tick(mid x 1.01))
EXIT_DQ = DecisionQuote(bid=45.00, ask=45.20, mid=45.10, age_ms=90, src="WS", ltp=45.10, walk_px=45.00)
EXIT_LIMIT = 44.90                      # exits.sell_limit at band 0: sweep - 2 ticks
_ids = itertools.count(1)


@pytest.fixture
def core():
    with store_mod.core_thread():
        yield


@pytest.fixture
def store(tmp_path, core):
    s = Store(tmp_path / "db" / "titan2.db", backup_dir=tmp_path / "backups")
    yield s
    s.close()


@pytest.fixture
def clock():
    return FakeClock("2026-10-12T09:20:00+05:30")


def inst_for(underlying: str) -> Instrument:
    """A CE contract of the underlying (RELIANCE gets INST); every field as an instruments-dump row would give it."""
    if underlying == INST.underlying:
        return INST
    name = "NIFTY" if underlying == "NSE:NIFTY 50" else underlying.split(":")[1]
    ts = f"{name}26OCT100CE"
    return Instrument(f"NFO:{ts}", sum(map(ord, ts)), ts, "NFO", name, underlying, "CE", 100.0, date(2026, 10, 27),
                      LOT_SIZE, 0.05)


def make_intent(underlying: str, created: datetime, strategy: str = "opening_drive") -> Intent:
    return Intent(intent_id=f"i{next(_ids):011d}", strategy=strategy, strategy_version="1.0abc", underlying=underlying,
                  direction=Direction.UP, structure=StructureType.LONG_CE, strike_pref=StrikePref.ATM,
                  expiry_rule=EXPIRY_RULE_NEAREST, trigger_px=2900.0, recheck=Recheck(-math.inf, math.inf),
                  ttl_s=10.0, created_mono=1000.0, created_wall=created, reason={"why": "test"})


class Rig:
    """The engine's side of the contract: indexes, timers, one store.tx() per event."""

    def __init__(self, s: Store, clk: FakeClock) -> None:
        self.s, self.clk = s, clk
        self.orders: dict[str, Order] = {}
        self.legs: dict[int, Leg] = {}
        self.timers: dict[str, OrderTimers] = {}
        self._bid = itertools.count(1)

    @property
    def mono(self) -> float:
        return self.clk.mono()

    @property
    def wall(self) -> datetime:
        return self.clk.now_wall()

    def new_bid(self) -> str:
        return f"B{next(_ids):08d}"

    def reserve(self, *, account: Account = Account.PAPER, book: str | None = None,
                underlying: str = "NSE:RELIANCE") -> Structure:
        it = make_intent(underlying, self.wall)
        with self.s.tx():
            self.s.insert_intent(it, SESSION)
            return positions.reserve(self.s, it, book=book or f"{account}:opening_drive", account=account,
                                     policy=Policy.FULL, now_mono=self.mono, now_wall=self.wall)

    def entry(self, *, account: Account = Account.PAPER, book: str | None = None, qty: int = QTY,
              limit: float = ENTRY_LIMIT, role: Role = Role.ENTRY, live_allowed: bool = False,
              underlying: str = "NSE:RELIANCE", dq: DecisionQuote = ENTRY_DQ):
        st = self.reserve(account=account, book=book, underlying=underlying)
        inst = inst_for(underlying)
        with self.s.tx():
            leg, o, fx = positions.begin_entry(self.s, self.orders, st, inst=inst, qty=qty, limit_px=limit,
                                               decision_quote=dq, decision={"limit": limit}, now_mono=self.mono,
                                               now_wall=self.wall, role=role, live_allowed=live_allowed)
        self.legs[leg.leg_id] = leg
        return st, leg, o, fx

    def t(self, o: Order) -> OrderTimers:
        return self.timers.setdefault(o.order_pk, OrderTimers())

    def ev(self, o: Order, event, **kw) -> Effects:
        with self.s.tx():
            return orders.handle(self.s, o, self.legs[o.leg_id], event, self.t(o), self.mono, self.wall, **kw)

    def tick(self, o: Order, **kw) -> Effects:
        return self.ev(o, Timer(self.wall, self.mono), **kw)

    def place(self, o: Order, outcome: str = "OK", bid: str | None = None, **kw) -> Effects:
        if outcome == "OK" and bid is None:
            bid = self.new_bid()
        return self.ev(o, PlaceResult(o.account, o.order_pk, outcome, bid, **kw))

    def snap(self, o: Order, status: str, filled: int, avg: float | None = None, *, bid: str | None = None,
             source: str = "WS", msg: str | None = None) -> Effects:
        bid = bid or o.broker_order_id or self.new_bid()
        return self.ev(o, OrderSnapshot(o.account, bid, o.order_pk, status, filled, avg, msg, None, source,
                                        self.mono))

    def found(self, o: Order, status: str = "WORKING", filled: int = 0, avg: float | None = None,
              bid: str | None = None) -> BrokerOrder:
        return BrokerOrder(o.account, bid or o.broker_order_id or self.new_bid(), o.order_pk, o.symbol,
                           o.symbol.split(":")[1], "NFO", "MIS", o.side, "LIMIT", "IOC", o.qty, filled, avg, status,
                           status, None, self.wall, None, "API")

    def opened(self, *, filled: int = QTY, avg: float = 42.15, **kw):
        """An OPEN position: entry placed, filled (COMPLETE or partial CANCELLED), on_entry_terminal."""
        st, leg, o, _ = self.entry(**kw)
        self.place(o)
        self.snap(o, "COMPLETE" if filled == o.qty else "CANCELLED", filled, avg)
        with self.s.tx():
            positions.on_entry_terminal(self.s, st, leg, profile=ExitProfile(), now_mono=self.mono,
                                        now_wall=self.wall)
        return st, leg, o

    def exiting(self, reason: str = "HARD_SL", **kw):
        st, leg, o = self.opened(**kw)
        with self.s.tx():
            positions.begin_exit(self.s, st, leg, reason, Urgency.PROTECTIVE, now_mono=self.mono, now_wall=self.wall)
        return st, leg

    def exit_order(self, leg: Leg, qty: int, limit: float = EXIT_LIMIT, band: int = 0):
        with self.s.tx():
            return orders.create_order(self.s, self.orders, leg, role=Role.EXIT, qty=qty, limit_px=limit,
                                       now_mono=self.mono, now_wall=self.wall, decision=EXIT_DQ, band_idx=band)


def cmds(fx: Effects, kind: type) -> list:
    return [c for c in fx.commands if isinstance(c, kind)]


def events(s: Store, kind: str, **where) -> list[dict]:
    return [e for e in s.events(kind=kind) if all(e[k] == v for k, v in where.items())]


def fill_rows(s: Store, pk: str) -> list[tuple[str, str, int, float]]:
    return [(f.seq_key, f.source, f.qty, f.px) for f in s.fills(pk)]


# ---------------------------------------------------------------------------------------------------------------
# Acceptance tests
# ---------------------------------------------------------------------------------------------------------------
class _Undo(Exception):
    """Raised to roll a whole scenario back (no commit, no fsync)."""


def _drive_to(rig: Rig, state: OrderState) -> Order:
    """A fresh ENTRY order brought to `state` through the real handlers."""
    st, leg, o, _ = rig.entry()
    if state is OrderState.SUBMITTING:
        return o
    if state is OrderState.UNKNOWN:
        rig.place(o, "AMBIGUOUS")
    elif state is OrderState.WORKING:
        rig.place(o)
    elif state is OrderState.CANCEL_REQUESTED:
        rig.place(o)
        with rig.s.tx():
            orders.request_cancel(rig.s, o, rig.t(o), rig.mono, rig.wall)
    elif state is OrderState.COMPLETE:
        rig.place(o)
        rig.snap(o, "COMPLETE", QTY, 42.15)
    elif state is OrderState.CANCELLED:
        rig.place(o)
        rig.snap(o, "CANCELLED", 0)
    elif state is OrderState.REJECTED:
        rig.place(o, "REJECTED", message="price out of range")
    elif state in (OrderState.NEW, OrderState.NOT_PLACED):
        rig.place(o, "REJECTED", message="first attempt")          # frees the leg (I4) for a planned order
        with rig.s.tx():
            o, _ = orders.create_order(rig.s, rig.orders, leg, role=Role.ENTRY, qty=QTY, limit_px=ENTRY_LIMIT,
                                       now_mono=rig.mono, now_wall=rig.wall, decision=ENTRY_DQ, planned=True)
        if state is OrderState.NOT_PLACED:
            with rig.s.tx():
                orders.request_cancel(rig.s, o, rig.t(o), rig.mono, rig.wall)
    assert o.state is state
    return o


def _fire(rig: Rig, o: Order, kind: OrderEvent) -> Effects:
    E = OrderEvent
    acct, pk = o.account, o.order_pk
    bid = o.broker_order_id or rig.new_bid()
    leg, t = rig.legs[o.leg_id], rig.t(o)
    if kind is E.SUBMIT:
        with rig.s.tx():
            return orders.submit(rig.s, o, leg, t, rig.mono, rig.wall)
    if kind is E.CANCEL_REQUEST:
        with rig.s.tx():
            return orders.request_cancel(rig.s, o, t, rig.mono, rig.wall)
    if kind.value.startswith("PLACE_"):
        return rig.ev(o, PlaceResult(acct, pk, kind.value[6:], bid if kind is E.PLACE_OK else None))
    if kind.value.startswith("SNAP_"):
        status = kind.value[5:]
        filled = {"WORKING": o.filled_qty, "COMPLETE": o.qty, "CANCELLED": o.filled_qty, "REJECTED": 0}[status]
        return rig.snap(o, status, filled, 42.15 if filled else None, bid=bid)
    if kind is E.LOOKUP_FOUND:
        return rig.ev(o, LookupResult(acct, pk, True, rig.found(o, "WORKING", o.filled_qty, o.avg_fill_px, bid)))
    if kind is E.LOOKUP_NOT_FOUND:
        return rig.ev(o, LookupResult(acct, pk, True, None))
    if kind is E.LOOKUP_ERROR:
        return rig.ev(o, LookupResult(acct, pk, False, None))
    if kind.value.startswith("CANCEL_"):
        return rig.ev(o, CancelResult(acct, pk, kind.value[7:], "test"))
    if kind is E.TRADES:
        return rig.ev(o, TradesResult(acct, pk, True, ()))
    if kind is E.SESSION_ENDED:
        with rig.s.tx():
            return orders.session_ended(rig.s, o, t, rig.mono, rig.wall)
    assert kind is E.TIMER
    return rig.tick(o)


def test_transition_table_exhaustive(store, clock):
    """Every (order state, event) pair goes through the real handlers: a pair the table does not list raises
    IllegalTransition; a listed pair leaves the order in a state the table allows. The structure and leg tables
    are enumerated the same way, and the positions functions refuse transitions the table does not list."""
    S, U, W, CR = OrderState.SUBMITTING, OrderState.UNKNOWN, OrderState.WORKING, OrderState.CANCEL_REQUESTED
    C, X, R, NP, NEW = (OrderState.COMPLETE, OrderState.CANCELLED, OrderState.REJECTED, OrderState.NOT_PLACED,
                        OrderState.NEW)
    E = OrderEvent
    # The spec §5.2 rows, as the table states them
    spec = {(S, E.PLACE_OK): {W, U}, (S, E.PLACE_REJECTED): {R}, (S, E.PLACE_AMBIGUOUS): {U},
            (S, E.PLACE_FATAL): {U}, (S, E.SNAP_COMPLETE): {C}, (U, E.SNAP_WORKING): {W},
            (U, E.LOOKUP_FOUND): {W, C, X, R}, (U, E.LOOKUP_NOT_FOUND): {U, NP}, (U, E.LOOKUP_ERROR): {U},
            (W, E.SNAP_WORKING): {W}, (W, E.SNAP_COMPLETE): {C}, (W, E.SNAP_CANCELLED): {X},
            (W, E.SNAP_REJECTED): {R}, (W, E.TIMER): {W, CR}, (W, E.CANCEL_REQUEST): {CR},
            (CR, E.CANCEL_OK): {CR}, (CR, E.CANCEL_BENIGN): {CR}, (CR, E.SNAP_CANCELLED): {X}, (CR, E.TIMER): {CR},
            (C, E.SNAP_CANCELLED): {C}, (NP, E.SNAP_WORKING): {NP}, (NEW, E.SUBMIT): {S}}
    for pair, to in spec.items():
        assert orders.TRANSITIONS[pair] == frozenset(to), pair
    # pairs that cannot happen are not listed
    for pair in [(NEW, E.PLACE_OK), (U, E.PLACE_OK), (U, E.PLACE_AMBIGUOUS), (W, E.CANCEL_OK), (S, E.CANCEL_OK),
                 (S, E.TRADES), (U, E.TRADES), (NP, E.CANCEL_BENIGN), (W, E.SUBMIT), (C, E.SUBMIT)]:
        assert pair not in orders.TRANSITIONS, pair

    expected_after = {(S, E.PLACE_OK): W, (S, E.PLACE_REJECTED): R, (S, E.PLACE_AMBIGUOUS): U,
                      (S, E.PLACE_FATAL): U, (S, E.LOOKUP_NOT_FOUND): S, (W, E.CANCEL_REQUEST): CR,
                      (NEW, E.SUBMIT): S, (NEW, E.CANCEL_REQUEST): NP, (U, E.SNAP_CANCELLED): X}
    seen_illegal = seen_legal = 0
    for state, kind in itertools.product(OrderState, OrderEvent):
        rig = Rig(store, clock)
        try:
            with store.tx():
                o = _drive_to(rig, state)
                to = orders.allowed(state, kind)
                if to is None:
                    with pytest.raises(IllegalTransition):
                        _fire(rig, o, kind)
                    assert o.state is state, (state, kind)
                    seen_illegal += 1
                else:
                    _fire(rig, o, kind)
                    assert o.state in to, (state, kind, o.state)
                    if (state, kind) in expected_after:
                        assert o.state is expected_after[(state, kind)], (state, kind, o.state)
                    seen_legal += 1
                raise _Undo
        except _Undo:
            pass
    assert seen_legal + seen_illegal == len(OrderState) * len(OrderEvent)
    assert seen_illegal == len(OrderState) * len(OrderEvent) - len(orders.TRANSITIONS)

    # structures and legs
    for frm, to in itertools.product(StructState, StructState):
        legal = to in positions.STRUCT_TRANSITIONS.get(frm, frozenset())
        if legal:
            positions.check_structure(frm, to)
        else:
            with pytest.raises(IllegalTransition):
                positions.check_structure(frm, to)
    assert set(positions.STRUCT_TRANSITIONS[StructState.VERIFY_FLAT]) == {StructState.CLOSED}
    for frm in (StructState.CLOSED, StructState.CLOSED_EXTERNAL, StructState.VOID):
        assert frm not in positions.STRUCT_TRANSITIONS          # terminal structures never move
    for frm, to in itertools.product(LegState, LegState):
        legal = to in positions.LEG_TRANSITIONS.get(frm, frozenset())
        if not legal:
            with pytest.raises(IllegalTransition):
                positions.check_leg(frm, to)

    # the positions functions refuse what the table does not list
    rig = Rig(store, clock)
    st, leg, _ = rig.opened()
    kw = dict(now_mono=rig.mono, now_wall=rig.wall)
    with store.tx():
        with pytest.raises(IllegalTransition):
            positions.close(store, st, [leg], **kw)                     # OPEN -> CLOSED
        with pytest.raises(IllegalTransition):
            positions.void(store, st, [leg], "x", **kw)                 # OPEN -> VOID
        with pytest.raises(IllegalTransition):
            positions.to_verify_flat(store, st, leg, **kw)              # OPEN -> VERIFY_FLAT
    st2, leg2, o2, _ = rig.entry(underlying="NSE:INFY")
    with store.tx():
        # an exit request never moves an ENTERING leg (and never raises: a FLATTEN_ALL walking every leg must not
        # roll back); the caller keeps the forced flag until the leg opens
        assert not positions.begin_exit(store, st2, leg2, "OPERATOR_FLATTEN", Urgency.PROTECTIVE, **kw)
        assert st2.state is StructState.ENTERING and leg2.exit_reason is None
        with pytest.raises(IllegalTransition):
            positions.on_entry_terminal(store, st2, leg2, profile=ExitProfile(), **kw)  # entry still in flight


def test_apply_cum_idempotent(store, clock):
    """apply_cum: cum == filled is a no-op; cum > filled adds one CUM fill priced from the cumulative averages;
    cum < filled and cum > qty are I9 (leg scope); a derived price > 20% from avg falls back to avg with a WARN;
    duplicate and older snapshots never double count."""
    rig = Rig(store, clock)
    st, leg, o, _ = rig.entry()
    rig.place(o)
    kw = dict(now_mono=rig.mono, now_wall=rig.wall)
    with store.tx():
        fx = orders.apply_cum(store, o, leg, 200, 42.15, **kw)
        assert fx.filled == 200 and o.filled_qty == 200 and o.avg_fill_px == 42.15
        assert leg.last_fill_mono == rig.mono and leg.last_fill_wall is not None
        assert orders.apply_cum(store, o, leg, 200, 42.15, **kw).filled == 0          # idempotent
        clock.advance(0.5)
        fx = orders.apply_cum(store, o, leg, 500, 42.17, now_mono=rig.mono, now_wall=rig.wall)
        assert fx.filled == 300 and not cmds(fx, Alert)
    rows = fill_rows(store, o.order_pk)
    assert [r[:3] for r in rows] == [("cum:200", "CUM", 200), ("cum:500", "CUM", 300)]
    assert rows[1][3] == pytest.approx((42.17 * 500 - 42.15 * 200) / 300)
    assert math.fsum(px * q for _, _, q, px in rows) == pytest.approx(42.17 * 500)   # value = avg x cum, exactly
    assert leg.open_qty == 500

    with store.tx():
        with pytest.raises(InvariantViolation) as lower:
            orders.apply_cum(store, o, leg, 400, 42.17, now_mono=rig.mono, now_wall=rig.wall)
        with pytest.raises(InvariantViolation) as over:
            orders.apply_cum(store, o, leg, 600, 42.17, now_mono=rig.mono, now_wall=rig.wall)
    assert lower.value.code == over.value.code == "I9" and lower.value.leg_id == leg.leg_id
    assert o.filled_qty == 500 and len(store.fills(o.order_pk)) == 2

    # a derived price far from the average falls back to the average, with a WARN
    rig2 = Rig(store, clock)
    st2, leg2, o2, _ = rig2.entry(underlying="NSE:INFY")
    rig2.place(o2)
    with store.tx():
        orders.apply_cum(store, o2, leg2, 100, 10.0, now_mono=rig.mono, now_wall=rig.wall)
        fx = orders.apply_cum(store, o2, leg2, 200, 20.0, now_mono=rig.mono, now_wall=rig.wall)
    assert fill_rows(store, o2.order_pk)[1] == ("cum:200", "CUM", 100, 20.0)
    assert [a.code for a in cmds(fx, Alert)] == ["FILL_PX_FALLBACK"] and cmds(fx, Alert)[0].severity == "WARN"
    assert events(store, "FILL", order_pk=o2.order_pk)[-1]["severity"] == "WARN"

    # no usable average: the qty is recorded (positions stay true), no CUM row is invented
    rig3 = Rig(store, clock)
    st3, leg3, o3, _ = rig3.entry(underlying="NSE:TCS")
    rig3.place(o3)
    with store.tx():
        fx = orders.apply_cum(store, o3, leg3, 500, None, now_mono=rig.mono, now_wall=rig.wall)
    assert o3.filled_qty == 500 and leg3.open_qty == 500 and store.fills(o3.order_pk) == []
    assert cmds(fx, Alert)[0].code == "FILL_PX_FALLBACK"

    # through snapshots: duplicates and late (older) reports never double count or decrease
    rig4 = Rig(store, clock)
    st4, leg4, o4, _ = rig4.entry(underlying="NSE:SBIN")
    rig4.place(o4)
    assert rig4.snap(o4, "WORKING", 200, 42.15).filled == 200
    assert rig4.snap(o4, "WORKING", 200, 42.15).filled == 0          # duplicate
    assert rig4.snap(o4, "WORKING", 100, 42.15).filled == 0          # older report, delivered late
    fx = rig4.snap(o4, "COMPLETE", 500, 42.17)
    assert fx.filled == 300 and fx.became_terminal and o4.state is OrderState.COMPLETE
    assert rig4.snap(o4, "WORKING", 300, 42.16).commands == []        # older, after terminal: no-op
    assert rig4.snap(o4, "COMPLETE", 500, 42.17).commands == []       # duplicate final report
    assert [r[0] for r in fill_rows(store, o4.order_pk)] == ["cum:200", "cum:500"]
    assert not events(store, "INVARIANT_VIOLATION", order_pk=o4.order_pk)


def test_ws_before_place_result(store, clock):
    """A WS order update can beat the PlaceResult: SUBMITTING adopts the broker id and applies it (even a terminal
    one); the late PlaceResult (OK, or AMBIGUOUS from a timeout after acceptance) is a no-op, never illegal."""
    rig = Rig(store, clock)
    st, leg, o, fx = rig.entry()
    assert [type(c) for c in fx.commands] == [Place, Subscribe] and fx.commands[1].mode == "full"
    assert o.state is OrderState.SUBMITTING and o.order_pk == "T2EP26101200101"
    fx = rig.snap(o, "COMPLETE", QTY, 42.15, bid="P261012000000007")
    assert o.state is OrderState.COMPLETE and o.broker_order_id == "P261012000000007" and fx.became_terminal
    assert cmds(fx, FetchTrades) == [FetchTrades(o.account, o.order_pk, "P261012000000007")]
    assert leg.open_qty == QTY

    late = rig.place(o, "OK", "P261012000000007")
    assert o.state is OrderState.COMPLETE and late.commands == [] and not late.became_terminal
    assert store.query_one("SELECT acked_ts FROM orders WHERE order_pk=?", (o.order_pk,))["acked_ts"] is not None
    assert rig.place(o, "AMBIGUOUS").commands == [] and not rig.t(o).lookups_due     # timeout after accept
    assert not events(store, "INVARIANT_VIOLATION")
    with store.tx():
        positions.on_entry_terminal(store, st, leg, profile=ExitProfile(), now_mono=rig.mono, now_wall=rig.wall)
    assert st.state is StructState.OPEN

    # a WORKING update first, then the PlaceResult, then the final update
    st2, leg2, o2, _ = rig.entry(underlying="NSE:INFY")
    rig.snap(o2, "WORKING", 0, bid="P261012000000008")
    assert o2.state is OrderState.WORKING
    assert rig.place(o2, "OK", "P261012000000008").commands == [] and o2.state is OrderState.WORKING
    assert rig.snap(o2, "CANCELLED", 0).became_terminal and o2.state is OrderState.CANCELLED

    # a PlaceResult naming another broker id than the one adopted is a recorded contradiction, not applied
    st3, leg3, o3, _ = rig.entry(underlying="NSE:TCS")
    rig.snap(o3, "WORKING", 0, bid="P261012000000009")
    fx = rig.place(o3, "OK", "P261012000000999")
    assert o3.broker_order_id == "P261012000000009" and fx.unresolved and cmds(fx, PollOrder)
    assert events(store, "INVARIANT_VIOLATION", order_pk=o3.order_pk)


def test_ambiguous_found_not_placed(store, clock):
    """AMBIGUOUS -> UNKNOWN with tag lookups at +1/+3/+7/+15 s and then every 5 s up to +30 s (the late-accept
    window): found -> adopt and apply; not found by all of them, >= 15 s since submit and not before +30 s ->
    NOT_PLACED (the entry then VOIDs; two misses are not enough: orders() can lag an order Kite accepted, and the OMS
    can take a timed-out place late); lookup errors retry every 2 s and turn ORDER_STUCK after 30 s; FATAL halts
    TOKEN and looks up once the token works; a SUBMITTING row found after a restart is looked up at +1/+3/+7/+15 s
    and becomes NOT_PLACED (§5.10)."""
    rig = Rig(store, clock)
    late = [5.0, 5.0, 5.0]                                  # +20/+25/+30 s: orders.LATE_LOOKUP_EVERY_S to LATE_ACCEPT_S

    # found
    st, leg, o, _ = rig.entry()
    fx = rig.place(o, "AMBIGUOUS", message="ReadTimeout")
    assert o.state is OrderState.UNKNOWN and fx.commands == [] and rig.t(o).lookups_due == [
        rig.mono + 1, rig.mono + 3, rig.mono + 7, rig.mono + 15, rig.mono + 20, rig.mono + 25, rig.mono + 30]
    assert rig.tick(o).commands == []
    clock.advance(1.0)
    assert rig.tick(o).commands == [LookupTag(o.account, o.order_pk)]
    fx = rig.ev(o, LookupResult(o.account, o.order_pk, True, rig.found(o, "COMPLETE", QTY, 42.15, "B-found")))
    assert o.state is OrderState.COMPLETE and o.broker_order_id == "B-found" and fx.became_terminal
    assert leg.open_qty == QTY and cmds(fx, FetchTrades)
    clock.advance(2.0)
    assert rig.ev(o, LookupResult(o.account, o.order_pk, True, None)).commands == []   # the +3 s answer, late
    assert o.state is OrderState.COMPLETE

    # not found at +1/+3/+7/+15/+20/+25/+30 s -> NOT_PLACED -> VOID (two misses within 3 s are no longer enough)
    st2, leg2, o2, _ = rig.entry(underlying="NSE:INFY")
    rig.place(o2, "AMBIGUOUS")
    for k, gap in enumerate((1.0, 2.0, 4.0, 8.0, *late), 1):
        clock.advance(gap)
        assert cmds(rig.tick(o2), LookupTag)
        fx = rig.ev(o2, LookupResult(o2.account, o2.order_pk, True, None))
        assert o2.lookups == k and (o2.state is OrderState.UNKNOWN) == (k < 7), k
    assert o2.state is OrderState.NOT_PLACED and fx.became_terminal and o2.broker_status is None
    assert leg2.last_fill_mono == rig.mono        # a possible fill for recon's post-fill rule
    with store.tx():
        positions.on_entry_terminal(store, st2, leg2, profile=ExitProfile(), now_mono=rig.mono, now_wall=rig.wall)
    assert st2.state is StructState.VOID and st2.void_reason == "ENTRY_NO_FILL" and leg2.state is LegState.VOID

    # quick misses are not enough: one more lookup at submit + 15 s decides
    st3, leg3, o3, _ = rig.entry(underlying="NSE:TCS")
    sub = o3.submitted_mono
    rig.place(o3, "AMBIGUOUS")
    rig.t(o3).lookups_due = []
    for _ in range(4):
        clock.advance(0.5)
        rig.ev(o3, LookupResult(o3.account, o3.order_pk, True, None))
    assert o3.state is OrderState.UNKNOWN and o3.lookups == 4 and rig.t(o3).lookups_due == [sub + 2.5]
    clock.advance(0.5)
    assert cmds(rig.tick(o3), LookupTag)
    rig.ev(o3, LookupResult(o3.account, o3.order_pk, True, None))
    assert o3.state is OrderState.UNKNOWN and rig.t(o3).lookups_due == [sub + 15.0]
    clock.advance(12.5)
    for at in (20.0, 25.0, 30.0):                           # inside the late window a miss only books the next one
        assert cmds(rig.tick(o3), LookupTag)
        rig.ev(o3, LookupResult(o3.account, o3.order_pk, True, None))
        assert o3.state is OrderState.UNKNOWN and rig.t(o3).lookups_due == [sub + at]
        clock.advance(5.0)
    assert cmds(rig.tick(o3), LookupTag)
    rig.ev(o3, LookupResult(o3.account, o3.order_pk, True, None))
    assert o3.state is OrderState.NOT_PLACED

    # throttled (HTTP 429): back off 1/2/4/8 s, then the late window
    st4, leg4, o4, _ = rig.entry(underlying="NSE:SBIN")
    rig.place(o4, "AMBIGUOUS", throttled=True)
    assert rig.t(o4).lookups_due == [rig.mono + 1, rig.mono + 3, rig.mono + 7, rig.mono + 15, rig.mono + 20,
                                     rig.mono + 25, rig.mono + 30]

    # lookup errors: retry every 2 s; ORDER_STUCK 30 s after lookups began; still UNKNOWN; no new attempt (I4)
    st5, leg5, o5, _ = rig.entry(underlying="NSE:HDFCBANK")
    rig.place(o5, "AMBIGUOUS")
    began = rig.mono
    stuck = []
    for _ in range(40):
        clock.advance(1.0)
        fx = rig.tick(o5)
        if cmds(fx, LookupTag):
            rig.ev(o5, LookupResult(o5.account, o5.order_pk, False, None))
            assert rig.t(o5).lookups_due and rig.t(o5).lookups_due[0] <= rig.mono + 2.0 + 1e-6
        if fx.halts:
            stuck.append((rig.mono - began, fx))
    assert len(stuck) == 1 and stuck[0][0] == pytest.approx(30.0)
    fx = stuck[0][1]
    assert fx.halts[0].kind == "ORDER_STUCK" and fx.halts[0].scope == f"BOOK:{o5.book}"
    assert str(fx.halts[0].clears) == "ACK" and [a.severity for a in cmds(fx, Alert)] == ["CRITICAL"]
    assert o5.stuck and o5.state is OrderState.UNKNOWN and events(store, "ORDER_STUCK", order_pk=o5.order_pk)
    with store.tx():
        with pytest.raises(InvariantViolation) as busy:
            orders.create_order(store, rig.orders, leg5, role=Role.ENTRY, qty=QTY, limit_px=ENTRY_LIMIT,
                                now_mono=rig.mono, now_wall=rig.wall, decision=ENTRY_DQ)
    assert busy.value.code == "I4"

    # FATAL: HALT(TOKEN, GLOBAL, AUTO) + CRITICAL; lookups wait for the token
    st6, leg6, o6, _ = rig.entry(underlying="NSE:NIFTY 50")
    fx = rig.place(o6, "FATAL", message="TokenException: invalid token")
    assert o6.state is OrderState.UNKNOWN and fx.halts[0].kind == "TOKEN" and fx.halts[0].scope == "GLOBAL"
    assert str(fx.halts[0].clears) == "AUTO" and cmds(fx, Alert)[0].severity == "CRITICAL"
    clock.advance(5.0)
    assert rig.tick(o6, token_ok=False).commands == [] and rig.t(o6).await_token
    rig.tick(o6, token_ok=True)
    assert rig.t(o6).lookups_due == [rig.mono + 1, rig.mono + 3, rig.mono + 7, rig.mono + 15]

    # restart: a SUBMITTING row (sent or not: its Place may have reached Kite before the crash) is looked up at
    #   +1/+3/+7/+15 s and through a whole late-accept window from the restart, then NOT_PLACED (decision B2; review
    #   round 2: it was NOT_PLACED at +15 s)
    st7, leg7, o7, _ = rig.entry(underlying="NSE:LT")
    clock.advance(30.0)
    t, first = orders.timers_after_reload(o7, rig.mono)
    rig.timers[o7.order_pk] = t
    assert first == [] and t.lookups_due == [rig.mono + s for s in (1, 3, 7, 15, 20, 25, 30)]
    assert t.late_until == rig.mono + orders.LATE_ACCEPT_S
    o7.submitted_mono = rig.mono                         # Engine.load re-anchors monotonic times to "now"
    for gap in (1.0, 2.0, 4.0, 8.0, 5.0, 5.0):
        clock.advance(gap)
        assert cmds(rig.tick(o7), LookupTag)
        rig.ev(o7, LookupResult(o7.account, o7.order_pk, True, None))
    assert o7.state is OrderState.SUBMITTING             # +25 s: still inside the window
    clock.advance(5.0)
    assert cmds(rig.tick(o7), LookupTag)
    rig.ev(o7, LookupResult(o7.account, o7.order_pk, True, None))
    assert o7.state is OrderState.NOT_PLACED


def test_stuck_keeps_exiting(store, clock):
    """An EXIT IOC that never resolves: polls from +3 s, Cancel at +6 s, ORDER_STUCK at cancel + 10 s (CRITICAL,
    HALT(ORDER_STUCK, BOOK, ACK), polls every 2 s). The leg stays EXITING with no trade row and no UNRESOLVED
    flag; once the order resolves the ladder resumes on the remaining quantity."""
    rig = Rig(store, clock)
    st, leg = rig.exiting()
    assert st.state is StructState.EXITING and leg.exit_reason == "HARD_SL" and leg.open_qty == QTY
    x, fx = rig.exit_order(leg, QTY)
    assert x.order_pk == "T2XP26101200102" and x.attempt == 2 and x.band_idx == 0 and cmds(fx, Place)
    rig.place(x)
    polls = []
    for _ in range(6):
        clock.advance(1.0)
        fx = rig.tick(x)
        polls.append((round(rig.mono - x.submitted_mono, 3), [type(c).__name__ for c in fx.commands]))
    assert polls == [(1.0, []), (2.0, []), (3.0, ["PollOrder"]), (4.0, ["PollOrder"]), (5.0, ["PollOrder"]),
                     (6.0, ["Cancel"])]
    assert x.state is OrderState.CANCEL_REQUESTED
    fx = rig.ev(x, CancelResult(x.account, x.order_pk, "OK"))
    assert cmds(fx, PollOrder)
    cancel_at = rig.mono
    halts = []
    for _ in range(14):
        clock.advance(1.0)
        fx = rig.tick(x)
        halts += fx.halts
        if fx.halts:
            assert rig.mono - cancel_at == pytest.approx(10.0)
            assert [a.severity for a in cmds(fx, Alert)] == ["CRITICAL"]
    assert [(h.kind, h.scope, str(h.clears)) for h in halts] == [("ORDER_STUCK", f"BOOK:{st.book}", "ACK")]
    assert x.stuck and x.state is OrderState.CANCEL_REQUESTED
    assert store.query_one("SELECT stuck FROM orders WHERE order_pk=?", (x.order_pk,))["stuck"] == 1
    gaps = []
    last = None
    for _ in range(6):
        clock.advance(1.0)
        if cmds(rig.tick(x), PollOrder):
            if last is not None:
                gaps.append(rig.mono - last)
            last = rig.mono
    assert gaps and all(g == pytest.approx(2.0) for g in gaps)          # every 2 s once stuck
    # the position is still being exited: EXITING, no trade row, P&L not UNRESOLVED, no second order (I4)
    assert st.state is StructState.EXITING and leg.state is LegState.EXITING and st.pnl_status is None
    assert store.trade(st.structure_id) is None
    with store.tx():
        with pytest.raises(InvariantViolation):
            orders.create_order(store, rig.orders, leg, role=Role.EXIT, qty=QTY, limit_px=EXIT_LIMIT,
                                now_mono=rig.mono, now_wall=rig.wall, decision=EXIT_DQ)

    # the stuck order resolves (a partial IOC fill): the ladder resumes on the rest
    fx = rig.snap(x, "CANCELLED", 200, 45.00, source="POLL")
    assert fx.became_terminal and x.state is OrderState.CANCELLED and leg.open_qty == 300
    assert st.state is StructState.EXITING and cmds(fx, FetchTrades)
    x2, _ = rig.exit_order(leg, 300, band=1)
    assert x2.attempt == 3 and x2.order_pk.endswith("03")
    rig.place(x2)
    rig.snap(x2, "COMPLETE", 300, 44.95)
    assert leg.open_qty == 0
    with store.tx():
        positions.to_verify_flat(store, st, leg, now_mono=rig.mono, now_wall=rig.wall)
        row, _ = positions.close(store, st, [leg], now_mono=rig.mono, now_wall=rig.wall)
    assert row["qty"] == QTY and row["n_exit_orders"] == 2 and row["pnl_status"] is PnlStatus.FINAL


def test_entry_partial_zero(store, clock):
    """An entry IOC that fills nothing VOIDs (ENTRY_NO_FILL, or REJECTED:<msg> with the reject cooldown); a partial
    fill opens with qty = filled, ExitParams from those fills, stop = hard SL, risk basis = premium paid, and no
    top-up order. A filled self-test exits at once."""
    rig = Rig(store, clock)
    kw = dict(profile=ExitProfile())

    # zero fill
    st, leg, o, _ = rig.entry()
    rig.place(o)
    assert rig.snap(o, "CANCELLED", 0).became_terminal
    with store.tx():
        fx = positions.on_entry_terminal(store, st, leg, now_mono=rig.mono, now_wall=rig.wall, **kw)
    assert fx.commands == [] and st.state is StructState.VOID and leg.state is LegState.VOID
    assert st.void_reason == "ENTRY_NO_FILL" and st.closed_wall is not None and store.trade(st.structure_id) is None
    assert events(store, "STRUCTURE_VOID", structure_id=st.structure_id)[0]["payload"]["reason"] == "ENTRY_NO_FILL"

    # rejected: REJECTED:<msg>; margin/RMS text is a WARN; the reject cooldown applies
    st2, leg2, o2, _ = rig.entry(underlying="NSE:INFY")
    fx = rig.place(o2, "REJECTED", message="RMS:Margin Exceeds, Required:21085, Available:100")
    assert o2.state is OrderState.REJECTED and fx.became_terminal and fx.halts == []
    assert [(a.severity, a.code) for a in cmds(fx, Alert)] == [("WARN", "MARGIN_REJECT")]
    with store.tx():
        positions.on_entry_terminal(store, st2, leg2, now_mono=rig.mono, now_wall=rig.wall, **kw)
    assert st2.state is StructState.VOID and st2.void_reason.startswith("REJECTED:RMS:Margin")
    cool = store.cooldowns(st2.book, SESSION, reentry_min=30, reject_min=10, void_min=5)
    assert (cool["NSE:INFY"] - st2.closed_wall).total_seconds() == 600
    assert (cool["NSE:RELIANCE"] - st.closed_wall).total_seconds() == 300

    # partial fill: OPEN with the filled qty only
    st3, leg3, o3, _ = rig.entry(underlying="NSE:TCS")
    rig.place(o3)
    rig.snap(o3, "WORKING", 200, 42.15)
    clock.advance(0.4)
    assert rig.snap(o3, "CANCELLED", 200, 42.15).became_terminal
    with store.tx():
        fx = positions.on_entry_terminal(store, st3, leg3, now_mono=rig.mono, now_wall=rig.wall, **kw)
    p = st3.exit_params
    assert st3.state is StructState.OPEN and leg3.state is LegState.OPEN and leg3.open_qty == 200
    assert p.qty == 200 and p.entry_avg == pytest.approx(42.15) and leg3.stop_px == p.hard_sl_px
    assert p.hard_sl_px == pytest.approx(42.15 * 0.85) and st3.risk_basis_rs == pytest.approx(8430.0)
    c_in = charges.order_charges(OrderFills(Side.BUY, 200, 8430.0), rates_for(SESSION)).total
    assert p.entry_charges == pytest.approx(c_in)
    assert leg3.orders == [o3.order_pk] and not [c for c in fx.commands if isinstance(c, Place)]   # no top-up
    assert [a.code for a in cmds(fx, Alert)] == ["POSITION_OPENED"]
    stored = store.load_state(rig.mono, rig.wall).structures[st3.structure_id]
    assert stored.exit_params == p and stored.state is StructState.OPEN and stored.opened_wall == st3.opened_wall
    assert store.entries(st3.book, SESSION) == 1

    # the self-test (LIVE) needs live_allowed (I14); if it ever fills it exits at once
    with pytest.raises(InvariantViolation) as i14:
        rig.entry(account=Account.LIVE, book="LIVE:_selftest", role=Role.SELFTEST, limit=40.60,
                  underlying="NSE:SBIN")
    assert i14.value.code == "I14"
    st4, leg4, o4, _ = rig.entry(account=Account.LIVE, book="LIVE:_selftest", role=Role.SELFTEST, limit=40.60,
                                 live_allowed=True, underlying="NSE:HDFCBANK")
    assert o4.order_pk.startswith("T2TL") and o4.side is Side.BUY
    rig.place(o4)
    rig.snap(o4, "COMPLETE", QTY, 40.60)
    with store.tx():
        fx = positions.on_entry_terminal(store, st4, leg4, now_mono=rig.mono, now_wall=rig.wall, **kw)
    assert st4.state is StructState.EXITING and leg4.exit_reason == "SELFTEST_FILLED"
    assert leg4.exit_urgency is Urgency.PROTECTIVE
    assert ("CRITICAL", "SELFTEST_FILLED") in [(a.severity, a.code) for a in cmds(fx, Alert)]


def test_close_books_once(store, clock, monkeypatch):
    """close() books the trade through ledger.build_trade_row inside the transition's own transaction: one trades
    row whose net is charges.net_pnl over the fills; a second close (or close_external) is illegal and the ledger
    refuses a second row (I10); a rolled-back close leaves neither a trade row nor a state change."""
    calls = []
    real = positions.build_trade_row

    def spy(*args, **kw):
        calls.append(store.in_tx)
        return real(*args, **kw)

    monkeypatch.setattr(positions, "build_trade_row", spy)
    rig = Rig(store, clock)
    st, leg = rig.exiting("TAKE_PROFIT")
    x, _ = rig.exit_order(leg, QTY)
    rig.place(x)
    clock.advance(0.8)
    rig.snap(x, "COMPLETE", QTY, 45.00)
    kw = dict(now_mono=rig.mono, now_wall=rig.wall)
    with store.tx():
        positions.to_verify_flat(store, st, leg, **kw)
    assert st.state is StructState.VERIFY_FLAT and events(store, "VERIFY_FLAT", structure_id=st.structure_id)

    # a close rolled back with its transaction leaves nothing behind
    with pytest.raises(RuntimeError):
        with store.tx():
            positions.close(store, st, [leg], **kw)
            raise RuntimeError("crash before commit")
    assert store.trade(st.structure_id) is None and calls == [True]
    loaded = store.load_state(rig.mono, rig.wall)
    st, leg = loaded.structures[st.structure_id], loaded.legs[leg.leg_id]
    assert st.state is StructState.VERIFY_FLAT and leg.open_qty == 0

    row, fx = positions.close(store, st, [leg], **kw)          # its own transaction when none is open
    assert calls == [True, True]
    expected = charges.net_pnl([OrderFills(Side.BUY, QTY, QTY * 42.15), OrderFills(Side.SELL, QTY, QTY * 45.00)],
                               rates_for(SESSION))
    db = store.trade(st.structure_id)
    assert row["net"] == db["net"] == expected.net and db["gross"] == expected.gross == 1425.0
    assert db["charges"] == expected.charges.total and db["pnl_status"] == "FINAL"
    assert db["exit_reason"] == "TAKE_PROFIT" and db["qty"] == QTY and db["entry_avg"] == 42.15
    assert st.state is StructState.CLOSED and leg.state is LegState.CLOSED and st.pnl_status is PnlStatus.FINAL
    assert st.closed_wall is not None and [a.code for a in cmds(fx, Alert)] == ["POSITION_CLOSED"]
    ev = events(store, "POSITION_CLOSED", structure_id=st.structure_id)
    assert len(ev) == 1 and ev[0]["payload"]["pnl"]["net"] == expected.net
    assert store.query_one("SELECT state FROM structures WHERE structure_id=?", (st.structure_id,))["state"] == "CLOSED"

    # booked once
    with store.tx():
        with pytest.raises(IllegalTransition):
            positions.close(store, st, [leg], **kw)
        ext = ExternalClose(leg.leg_id, QTY, ReconClass.EXTERNAL_CLOSE, PnlStatus.FINAL,
                            (ExternalFill("B1", QTY, 44.0, rig.wall),), ())
        with pytest.raises(IllegalTransition):
            positions.close_external(store, rig.orders, st, leg, ext, **kw)
    with store.tx():
        with pytest.raises(InvariantViolation) as twice:
            store.insert_trade(row)
    assert twice.value.code == "I10"
    assert store.scalar("SELECT COUNT(*) FROM trades") == 1 and calls == [True, True]

    # close_external books through the same function, in its transaction too
    st2, leg2, _ = rig.opened(underlying="NSE:INFY")
    sym = leg2.inst.symbol
    b = BrokerOrder(Account.PAPER, "B-manual", None, sym, leg2.inst.tradingsymbol, "NFO", "MIS", Side.SELL, "LIMIT",
                    "DAY", QTY, QTY, 41.0, "COMPLETE", "COMPLETE", None, rig.wall, None, "WEB")
    ext = ExternalClose(leg2.leg_id, QTY, ReconClass.EXTERNAL_CLOSE, PnlStatus.FINAL,
                        (ExternalFill("B-manual", QTY, 41.0, rig.wall),), (b,))
    with store.tx():
        row2, fx2 = positions.close_external(store, rig.orders, st2, leg2, ext, **kw)
    assert calls == [True, True, True] and store.trade(st2.structure_id)["net"] == row2["net"]
    assert st2.state is StructState.CLOSED_EXTERNAL and row2["exit_reason"] == "CLOSED_EXTERNAL"


# ---------------------------------------------------------------------------------------------------------------
# More order-machine behaviour
# ---------------------------------------------------------------------------------------------------------------
def test_create_order_invariants(store, clock):
    rig = Rig(store, clock)
    # I13: an ENTRY limit above floor_tick(mid x 1.01), off the tick grid, or without a decision quote
    st, leg, o, _ = rig.entry()
    rig.place(o, "REJECTED", message="test")              # frees the leg
    kw = dict(role=Role.ENTRY, qty=QTY, now_mono=rig.mono, now_wall=rig.wall)
    for limit, dq, code in [(42.45, ENTRY_DQ, "I13"), (42.27, ENTRY_DQ, "I13"), (42.10, ENTRY_DQ, "I13"),
                            (ENTRY_LIMIT, None, "I13")]:
        with store.tx():
            with pytest.raises(InvariantViolation) as bad:
                orders.create_order(store, rig.orders, leg, limit_px=limit, decision=dq, **kw)
        assert bad.value.code == code, limit
    with store.tx():
        with pytest.raises(InvariantViolation) as lot:
            orders.create_order(store, rig.orders, leg, limit_px=ENTRY_LIMIT, decision=ENTRY_DQ,
                                **{**kw, "qty": 250})
    assert lot.value.code == "I12"
    with store.tx():
        with pytest.raises(IllegalTransition):            # EXIT needs an EXITING leg
            orders.create_order(store, rig.orders, leg, role=Role.EXIT, qty=QTY, limit_px=EXIT_LIMIT,
                                now_mono=rig.mono, now_wall=rig.wall, decision=EXIT_DQ)
    assert leg.orders == [o.order_pk] and len(rig.orders) == 1    # refusals leave nothing behind

    # I6: never an EXIT above the open qty (G3)
    st2, leg2 = rig.exiting(underlying="NSE:INFY", filled=300)
    with store.tx():
        with pytest.raises(InvariantViolation) as i6:
            orders.create_order(store, rig.orders, leg2, role=Role.EXIT, qty=QTY, limit_px=EXIT_LIMIT,
                                now_mono=rig.mono, now_wall=rig.wall, decision=EXIT_DQ)
    assert i6.value.code == "I6"
    x, fx = rig.exit_order(leg2, 300, limit=39.60, band=6)            # exactly ceil_tick(45 x 0.88): as asked
    req = fx.commands[0].req
    assert (req.role, req.side, req.qty, req.limit_px, req.tag) == (Role.EXIT, Side.SELL, 300, 39.60, x.order_pk)
    assert not cmds(fx, Alert)
    stored = store.query_one("SELECT order_type, validity, product, variety, decision_bid FROM orders "
                             "WHERE order_pk=?", (x.order_pk,))
    assert tuple(stored) == ("LIMIT", "IOC", "MIS", "regular", 45.00)
    sub = events(store, "ORDER_SUBMITTED", order_pk=x.order_pk)[0]["payload"]
    assert sub["limit"] == 39.60 and sub["bid"] == 45.00 and sub["attempt"] == 2 and sub["band"] == 6

    # I13 on a closing order never blocks the exit: a limit more than 12% under the bid is raised onto the bound
    rig.place(x, "REJECTED", message="price band")
    x2, fx = rig.exit_order(leg2, 300, limit=30.00, band=6)
    assert x2.limit_px == 39.60 and fx.commands[0].req.limit_px == 39.60
    assert [(a.severity, a.code) for a in cmds(fx, Alert)] == [("WARN", "LIMIT_BOUNDED")]
    warn = events(store, "ORDER_UPDATE", order_pk=x2.order_pk)[-1]
    assert warn["severity"] == "WARN" and warn["payload"]["asked_limit"] == 30.0

    # I14: no LIVE entry without the caller's explicit permission
    st3 = rig.reserve(account=Account.LIVE, underlying="NSE:TCS")
    with store.tx():
        with pytest.raises(InvariantViolation) as i14:
            positions.begin_entry(store, rig.orders, st3, inst=inst_for("NSE:TCS"), qty=QTY, limit_px=ENTRY_LIMIT,
                                  decision_quote=ENTRY_DQ, decision=None, now_mono=rig.mono, now_wall=rig.wall)
    assert i14.value.code == "I14"
    # the refusal left the structure RESERVED (memory and DB) with no leg, so the gateway can void it
    assert st3.state is StructState.RESERVED and st3.legs == [] and st3.decision is None
    assert store.scalar("SELECT COUNT(*) FROM legs WHERE structure_id=?", (st3.structure_id,)) == 0
    with store.tx():
        positions.void(store, st3, [], "LIVE", now_mono=rig.mono, now_wall=rig.wall)
    assert st3.state is StructState.VOID

    # a structure cannot hold a contract of the other type or underlying
    st4 = rig.reserve(underlying="NSE:SBIN")
    pe = Instrument("NFO:SBIN26OCT800PE", 777, "SBIN26OCT800PE", "NFO", "SBIN", "NSE:SBIN", "PE", 800.0,
                    date(2026, 10, 27), 750, 0.05)
    with store.tx():
        with pytest.raises(ValueError):
            positions.begin_entry(store, rig.orders, st4, inst=pe, qty=750, limit_px=ENTRY_LIMIT,
                                  decision_quote=ENTRY_DQ, decision=None, now_mono=rig.mono, now_wall=rig.wall)


def test_order_request_carries_the_leg_book(store, clock):
    """Review F-19: every bot order's request carries its leg's book (the paper exchange funds a SHADOW-account book
    from its own pool), for entries and exits alike; KiteBroker's place payload never sends it."""
    from titan2.broker import kite
    rig = Rig(store, clock)
    _, leg, _, fx = rig.entry(account=Account.SHADOW, book="PLACEBO:opening_drive")
    (pl,) = cmds(fx, Place)
    assert pl.req.book == leg.book == "PLACEBO:opening_drive"
    _, leg2 = rig.exiting(underlying="NSE:INFY")
    _, fx = rig.exit_order(leg2, QTY)
    (x,) = cmds(fx, Place)
    assert x.req.book == leg2.book == "PAPER:opening_drive"
    payload = kite.place_payload(x.req)
    assert set(payload) == kite.PLACE_PAYLOAD_KEYS and "book" not in payload
    assert kite.payload_problem(payload) is None
    assert payload == kite.place_payload(dataclasses.replace(x.req, book=None))       # the book changes nothing


def test_reject_classification(store, clock):
    """§5.9: a price-band reject never blocks the contract (the spec's bare 'ban' also matched 'band'); a ban or
    block message raises HALT(CONTRACT_BLOCKED, SYMBOL, NEXT_SESSION); the 3rd entry reject of the session in a
    book raises HALT(REJECTS, BOOK, ACK); an exit reject only warns (the ladder re-prices it)."""
    rig = Rig(store, clock)
    st, leg, o, _ = rig.entry()
    fx = rig.place(o, "REJECTED", message="Order price is outside the price band (LPP). BANKNIFTY")
    assert fx.halts == [] and [a.code for a in cmds(fx, Alert)] == ["ORDER_REJECTED"]
    st2, leg2, o2, _ = rig.entry(underlying="NSE:INFY")
    fx = rig.snap(o2, "REJECTED", 0, msg="INFY is in the F&O ban period")
    assert [(h.kind, h.scope, str(h.clears)) for h in fx.halts] == [
        ("CONTRACT_BLOCKED", f"SYMBOL:{o2.symbol}", "NEXT_SESSION")]
    st3, leg3, o3, _ = rig.entry(underlying="NSE:TCS")
    fx = rig.place(o3, "REJECTED", message="Trading is not allowed in this contract")
    kinds = sorted(h.kind for h in fx.halts)
    assert kinds == ["CONTRACT_BLOCKED", "REJECTS"]
    rejects = next(h for h in fx.halts if h.kind == "REJECTS")
    assert rejects.scope == f"BOOK:{st3.book}" and str(rejects.clears) == "ACK"
    # another book has its own count
    st4, leg4, o4, _ = rig.entry(underlying="NSE:TCS", book="PAPER:price_spike_up")
    assert rig.place(o4, "REJECTED", message="price").halts == []
    # an exit reject: WARN only
    st5, leg5 = rig.exiting(underlying="NSE:SBIN")
    x, _ = rig.exit_order(leg5, QTY)
    fx = rig.place(x, "REJECTED", message="Order price outside circuit; ban on illiquid")
    assert fx.halts == [] and [a.severity for a in cmds(fx, Alert)] == ["WARN"] and st5.state is StructState.EXITING


def test_terminal_contradiction_recorded_once(store, clock):
    """A report that contradicts a terminal order is never applied: one INVARIANT_VIOLATION event, one CRITICAL alert
    and one PollOrder per distinct contradiction (a poll that returns it again does not loop), and the fills are
    flagged so the structure's P&L becomes UNRESOLVED."""
    rig = Rig(store, clock)
    st, leg, o, _ = rig.entry()
    rig.place(o)
    rig.snap(o, "COMPLETE", QTY, 42.15)
    fx = rig.snap(o, "CANCELLED", 300, 42.15, source="POLL")
    assert o.state is OrderState.COMPLETE and o.filled_qty == QTY and fx.unresolved
    assert cmds(fx, PollOrder) and [a.severity for a in cmds(fx, Alert)] == ["CRITICAL"]
    again = rig.snap(o, "CANCELLED", 300, 42.15, source="POLL")
    assert again.commands == [] and len(events(store, "INVARIANT_VIOLATION", order_pk=o.order_pk)) == 1
    # NOT_PLACED but the broker shows the order: a contradiction even with no fill
    st2, leg2, o2, _ = rig.entry(underlying="NSE:INFY")
    rig.place(o2, "AMBIGUOUS")
    o2.lookups = orders.NOT_PLACED_LOOKUPS - 1
    clock.advance(max(orders.NOT_PLACED_AFTER_S, orders.LATE_ACCEPT_S))     # past the late-accept window too
    rig.ev(o2, LookupResult(o2.account, o2.order_pk, True, None))
    assert o2.state is OrderState.NOT_PLACED
    fx = rig.place(o2, "OK", "B-late")                    # (only possible if a lookup raced a lost place)
    assert fx.unresolved and o2.state is OrderState.NOT_PLACED

    with store.tx():
        positions.on_entry_terminal(store, st, leg, profile=ExitProfile(), now_mono=rig.mono, now_wall=rig.wall)
        assert positions.mark_unresolved(store, st, now_mono=rig.mono, now_wall=rig.wall, detail="conflict")
        assert not positions.mark_unresolved(store, st, now_mono=rig.mono, now_wall=rig.wall)
        positions.begin_exit(store, st, leg, "OPERATOR_FLATTEN", Urgency.PROTECTIVE, now_mono=rig.mono,
                             now_wall=rig.wall)
    x, _ = rig.exit_order(leg, QTY)
    rig.place(x)
    rig.snap(x, "COMPLETE", QTY, 43.0)
    with store.tx():
        positions.to_verify_flat(store, st, leg, now_mono=rig.mono, now_wall=rig.wall)
        row, _ = positions.close(store, st, [leg], now_mono=rig.mono, now_wall=rig.wall)
    assert row["pnl_status"] is PnlStatus.UNRESOLVED and store.trade(st.structure_id)["pnl_status"] == "UNRESOLVED"


def test_trades_result_refines_fills(store, clock):
    rig = Rig(store, clock)
    st, leg, o, _ = rig.entry()
    rig.place(o, bid="B-t")
    rig.snap(o, "WORKING", 200, 42.15)
    fx = rig.snap(o, "COMPLETE", QTY, 42.17)
    assert cmds(fx, FetchTrades) == [FetchTrades(o.account, o.order_pk, "B-t")]
    trades = (BrokerTrade(o.account, "T1", "B-t", o.symbol, Side.BUY, 200, 42.15, rig.wall, None),
              BrokerTrade(o.account, "T2", "B-t", o.symbol, Side.BUY, 300, 42.18, rig.wall, None))
    rig.ev(o, TradesResult(o.account, o.order_pk, True, trades))
    eff = store.effective_fills(o.order_pk)
    assert [(f.seq_key, f.qty, f.px) for f in eff] == [("trade:T1", 200, 42.15), ("trade:T2", 300, 42.18)]
    rig.ev(o, TradesResult(o.account, o.order_pk, True, trades))            # duplicate: dropped by UNIQUE
    assert len(store.fills(o.order_pk, "TRADE")) == 2
    # trades that do not belong to the order, or exceed its filled qty, are refused
    st2, leg2, o2, _ = rig.entry(underlying="NSE:INFY")
    rig.place(o2, bid="B-u")
    rig.snap(o2, "CANCELLED", 100, 42.15)
    too_many = (BrokerTrade(o2.account, "T9", "B-u", o2.symbol, Side.BUY, 200, 42.15, rig.wall, None),)
    rig.ev(o2, TradesResult(o2.account, o2.order_pk, True, too_many))
    wrong = (BrokerTrade(o2.account, "T8", "B-other", o2.symbol, Side.BUY, 100, 42.15, rig.wall, None),)
    rig.ev(o2, TradesResult(o2.account, o2.order_pk, True, wrong))
    assert store.fills(o2.order_pk, "TRADE") == []
    # a failed read is retried 2 s later, TRADES_MAX_TRIES requests in all
    sent = 1
    for _ in range(5):
        rig.ev(o2, TradesResult(o2.account, o2.order_pk, False))
        clock.advance(2.0)
        sent += len(cmds(rig.tick(o2), FetchTrades))
    assert sent == orders.TRADES_MAX_TRIES and not orders.needs_timer(o2, rig.t(o2))


def test_timers_after_reload_and_cancel_requests(store, clock):
    rig = Rig(store, clock)
    st, leg, o, _ = rig.entry()
    rig.place(o, bid="B-w")
    t, first = orders.timers_after_reload(o, rig.mono)
    assert first == [PollOrder(o.account, o.order_pk, "B-w")]
    with store.tx():
        orders.request_cancel(store, o, rig.t(o), rig.mono, rig.wall)
    assert o.state is OrderState.CANCEL_REQUESTED
    t, first = orders.timers_after_reload(o, rig.mono)
    assert first == [Cancel(o.account, o.order_pk, "B-w"), PollOrder(o.account, o.order_pk, "B-w")]
    assert t.cancel_sent == rig.mono
    # a late cancel answer after the order resolved is a no-op
    rig.snap(o, "CANCELLED", 0)
    assert rig.ev(o, CancelResult(o.account, o.order_pk, "BENIGN", "already cancelled")).commands == []
    # SUBMITTING / UNKNOWN have no broker id to cancel: the IOC resolves on its own
    st2, leg2, o2, _ = rig.entry(underlying="NSE:INFY")
    with store.tx():
        assert orders.request_cancel(store, o2, rig.t(o2), rig.mono, rig.wall).commands == []
    assert o2.state is OrderState.SUBMITTING
    # a SUBMITTING order whose PlaceResult never comes is looked up after PLACE_ANSWER_WAIT_S, through the late-accept
    # window (it was sent: the OMS may still take it)
    clock.advance(orders.PLACE_ANSWER_WAIT_S)
    rig.tick(o2)
    assert rig.t(o2).lookups_due == [rig.mono + 1, rig.mono + 3, rig.mono + 7, rig.mono + 15, rig.mono + 20,
                                     rig.mono + 25, rig.mono + 30]
    assert rig.t(o2).late_until == rig.mono + orders.LATE_ACCEPT_S
    assert events(store, "ORDER_UPDATE", order_pk=o2.order_pk)[-1]["severity"] == "WARN"
    # a planned (phase-2) order is sent by submit()
    rig.place(o2, "REJECTED", message="x")
    with store.tx():
        p, fx = orders.create_order(store, rig.orders, leg2, role=Role.ENTRY, qty=QTY, limit_px=ENTRY_LIMIT,
                                    now_mono=rig.mono, now_wall=rig.wall, decision=ENTRY_DQ, planned=True)
        assert p.state is OrderState.NEW and fx.commands == []
        fx = orders.submit(store, p, leg2, rig.t(p), rig.mono, rig.wall)
    assert p.state is OrderState.SUBMITTING and cmds(fx, Place)[0].req.tag == p.order_pk


def test_find_order_by_tag_then_id(store, clock):
    rig = Rig(store, clock)
    st, leg, o, _ = rig.entry()
    rig.place(o, bid="B-f")
    assert orders.find_order(rig.orders, Account.PAPER, tag=o.order_pk) is o
    assert orders.find_order(rig.orders, Account.PAPER, broker_order_id="B-f") is o
    assert orders.find_order(rig.orders, Account.LIVE, tag=o.order_pk) is None
    assert orders.find_order(rig.orders, Account.PAPER, tag="SQOFF", broker_order_id="nope") is None
    with store.tx():
        with pytest.raises(IllegalTransition):            # an event for another account never applies
            orders.handle(store, o, leg, OrderSnapshot(Account.LIVE, "B-f", o.order_pk, "COMPLETE", QTY, 42.0, None,
                                                       None, "WS", rig.mono), rig.t(o), rig.mono, rig.wall)
        with pytest.raises(IllegalTransition):            # a report for another broker order
            orders.handle(store, o, leg, OrderSnapshot(Account.PAPER, "B-zzz", o.order_pk, "COMPLETE", QTY, 42.0,
                                                       None, None, "WS", rig.mono), rig.t(o), rig.mono, rig.wall)
    assert o.state is OrderState.WORKING and o.filled_qty == 0


# ---------------------------------------------------------------------------------------------------------------
# More structure-machine behaviour
# ---------------------------------------------------------------------------------------------------------------
def test_begin_exit_first_reason_wins(store, clock):
    rig = Rig(store, clock)
    st, leg, _ = rig.opened()
    kw = dict(now_mono=rig.mono, now_wall=rig.wall)
    with store.tx():
        assert positions.begin_exit(store, st, leg, "TIME_STOP", Urgency.NORMAL, quote={"bid": 42.0}, **kw)
        assert not positions.begin_exit(store, st, leg, "KILL_SWITCH", Urgency.PROTECTIVE, **kw)
        with pytest.raises(ValueError):
            positions.begin_exit(store, st, leg, "NOT_A_REASON", Urgency.NORMAL, **kw)
    assert st.exit_reason == leg.exit_reason == "TIME_STOP" and leg.exit_urgency is Urgency.PROTECTIVE
    assert leg.exiting_since_mono == rig.mono
    row = store.query_one("SELECT state, exit_reason, exit_urgency, exiting_since_ts FROM legs WHERE leg_id=?",
                          (leg.leg_id,))
    assert tuple(row)[:3] == ("EXITING", "TIME_STOP", "PROTECTIVE") and row["exiting_since_ts"] is not None
    decisions = events(store, "EXIT_DECISION", structure_id=st.structure_id)
    assert [d["payload"]["reason"] for d in decisions] == ["TIME_STOP", "KILL_SWITCH"]
    assert decisions[1]["payload"]["frozen_reason"] == "TIME_STOP"


def test_close_with_residual_never_sold(store, clock):
    rig = Rig(store, clock)
    st, leg = rig.exiting()
    x, _ = rig.exit_order(leg, QTY)
    rig.place(x)
    rig.snap(x, "COMPLETE", QTY, 41.5)
    with store.tx():
        positions.to_verify_flat(store, st, leg, now_mono=rig.mono, now_wall=rig.wall)
        row, fx = positions.close(store, st, [leg], residual_qty=500, now_mono=rig.mono, now_wall=rig.wall)
    assert st.state is StructState.CLOSED and leg.broker_residual_qty == 500 and fx.halts == []
    assert not [c for c in fx.commands if isinstance(c, Place)]                 # never sold
    assert store.query_one("SELECT broker_residual_qty FROM legs WHERE leg_id=?", (leg.leg_id,))[0] == 500
    assert events(store, "POSITION_CLOSED", structure_id=st.structure_id)[0]["severity"] == "WARN"
    with store.tx():                        # a later exit request for a closed leg changes nothing
        assert not positions.begin_exit(store, st, leg, "KILL_SWITCH", Urgency.PROTECTIVE, now_mono=rig.mono,
                                        now_wall=rig.wall)
    assert st.state is StructState.CLOSED and st.exit_reason == "HARD_SL"


def test_close_external_squareoff_and_shared_broker_order(store, clock):
    """The broker's MIS square-off closes two PAPER books on one contract with ONE order: the first leg's EXTERNAL
    row carries the broker id, the second gets 'EX<id>:<leg>' without it (ids are unique); AUTO_SQUAREOFF rows pay
    the Rs 50 + GST fee in net_pnl; BROKER_SQUAREOFF raises no MISMATCH halt. An unpriced external close is
    booked UNRESOLVED at zero proceeds."""
    rig = Rig(store, clock)
    st1, leg1, _ = rig.opened()
    st2, leg2, _ = rig.opened(book="PAPER:price_spike_up")
    clock.advance_to("15:26:05")
    sq = BrokerOrder(Account.PAPER, "SQ1", "SQOFF", INST.symbol, INST.tradingsymbol, "NFO", "MIS", Side.SELL,
                     "LIMIT", "DAY", 1000, 1000, 39.0, "COMPLETE", "COMPLETE", None, rig.wall, None, "EXCHANGE")
    kw = dict(now_mono=rig.mono, now_wall=rig.wall)
    rows = []
    for leg, st in ((leg1, st1), (leg2, st2)):
        ext = ExternalClose(leg.leg_id, QTY, ReconClass.BROKER_SQUAREOFF, PnlStatus.FINAL,
                            (ExternalFill("SQ1", QTY, 39.0, rig.wall),), (sq,))
        with store.tx():
            row, fx = positions.close_external(store, rig.orders, st, leg, ext, **kw)
        assert fx.halts == [] and st.state is StructState.CLOSED_EXTERNAL and leg.state is LegState.CLOSED_EXTERNAL
        rows.append(row)
    pks = [pk for pk in leg1.orders + leg2.orders if pk.startswith("EX")]
    assert pks == ["EXSQ1", f"EXSQ1:{leg2.leg_id}"]
    assert rig.orders["EXSQ1"].broker_order_id == "SQ1" and rig.orders[pks[1]].broker_order_id is None
    assert all(rig.orders[pk].external_kind == "AUTO_SQUAREOFF" for pk in pks)
    for row in rows:
        assert row["exit_reason"] == "BROKER_SQUAREOFF" and row["sqoff_fee"] == 50.0
        assert row["pnl_status"] is PnlStatus.FINAL and row["exit_avg"] == 39.0
    assert events(store, "BROKER_SQUAREOFF", structure_id=st1.structure_id)[0]["severity"] == "CRITICAL"

    # MANUAL and unpriced: UNRESOLVED, zero proceeds booked, MISMATCH halt
    st3, leg3, _ = rig.opened(underlying="NSE:INFY")
    ext = ExternalClose(leg3.leg_id, QTY, ReconClass.EXTERNAL_CLOSE, PnlStatus.UNRESOLVED,
                        (ExternalFill(None, QTY, None, None),), ())
    with store.tx():
        row, fx = positions.close_external(store, rig.orders, st3, leg3, ext, **kw)
    assert row["pnl_status"] is PnlStatus.UNRESOLVED and row["gross"] == -QTY * 42.15
    assert [(h.kind, h.scope, str(h.clears)) for h in fx.halts] == [("MISMATCH", "ACCOUNT:PAPER", "ACK")]
    assert f"EX:{leg3.leg_id}:1" in leg3.orders and store.fills(f"EX:{leg3.leg_id}:1") == []

    # the external qty must be exactly what the leg holds
    st4, leg4, _ = rig.opened(underlying="NSE:TCS")
    bad = ExternalClose(leg4.leg_id, 300, ReconClass.EXTERNAL_CLOSE, PnlStatus.FINAL,
                        (ExternalFill(None, 300, 40.0, None),), ())
    with store.tx():
        with pytest.raises(InvariantViolation):
            positions.close_external(store, rig.orders, st4, leg4, bad, **kw)
    assert st4.state is StructState.OPEN and store.trade(st4.structure_id) is None


def test_cover_repairs_unrecorded_oversell_and_closes_unresolved(store, clock):
    """An exit fill the DB never saw (recorded CANCELLED 0; the broker later reports COMPLETE) plus the next exit
    leave the broker short. The COVER (recon's bot-explained short) hangs off the VERIFY_FLAT leg and never becomes
    sellable qty; once flat the structure closes UNRESOLVED (bought != sold), never FINAL."""
    rig = Rig(store, clock)
    st, leg = rig.exiting()
    a, _ = rig.exit_order(leg, QTY)
    rig.place(a)
    rig.snap(a, "CANCELLED", 0)
    fx = rig.snap(a, "COMPLETE", QTY, 45.0, source="POLL")          # the broker's later word: it all sold
    assert fx.unresolved and a.state is OrderState.CANCELLED and a.filled_qty == 0
    with store.tx():
        positions.mark_unresolved(store, st, now_mono=rig.mono, now_wall=rig.wall, detail="exit fill unrecorded")
    b, _ = rig.exit_order(leg, QTY)
    rig.place(b)
    rig.snap(b, "COMPLETE", QTY, 44.90)
    assert leg.open_qty == 0
    with store.tx():
        positions.to_verify_flat(store, st, leg, now_mono=rig.mono, now_wall=rig.wall)
    cover_dq = DecisionQuote(bid=44.80, ask=45.00, mid=44.90, age_ms=50, src="WS", ltp=44.90, walk_px=45.00)
    with store.tx():
        c, fx = orders.create_order(store, rig.orders, leg, role=Role.COVER, qty=QTY, limit_px=45.10,
                                    now_mono=rig.mono, now_wall=rig.wall, decision=cover_dq)
    assert c.side is Side.BUY and c.order_pk.startswith("T2CP") and cmds(fx, Place)[0].req.side is Side.BUY
    rig.place(c)
    rig.snap(c, "COMPLETE", QTY, 45.00)
    assert leg.open_qty == 0                                          # a cover never becomes sellable qty
    with store.tx():
        row, _ = positions.close(store, st, [leg], now_mono=rig.mono, now_wall=rig.wall)
    assert row["pnl_status"] is PnlStatus.UNRESOLVED and st.state is StructState.CLOSED


def test_reload_reproduces_memory(store, clock):
    """What these functions hold in memory is exactly what titan2.db reads back (monotonic and RAM-only fields
    aside: they are re-anchored on load), so an Engine.load after a rollback continues from identical objects."""
    rig = Rig(store, clock)
    st, leg = rig.exiting(filled=300)
    assert st.decision == {"limit": ENTRY_LIMIT}                # begin_entry's decision_json
    x, _ = rig.exit_order(leg, 200)
    rig.place(x)
    clock.advance(0.3)
    rig.snap(x, "WORKING", 100, 44.95)
    loaded = store.load_state(rig.mono + 5.0, rig.wall)
    for pk in leg.orders:
        mem, db = rig.orders[pk], loaded.orders[pk]
        for f in dataclasses.fields(Order):
            if f.name not in ("submitted_mono", "lookups"):
                assert getattr(db, f.name) == getattr(mem, f.name), (pk, f.name)
    for f in dataclasses.fields(Leg):
        if not f.name.endswith("_mono"):
            assert getattr(loaded.legs[leg.leg_id], f.name) == getattr(leg, f.name), f.name
    for f in dataclasses.fields(Structure):
        if f.name != "intent":
            assert getattr(loaded.structures[st.structure_id], f.name) == getattr(st, f.name), f.name
    assert loaded.legs[leg.leg_id].open_qty == leg.open_qty == 200


def test_untrusted_exit_levels_exit_at_once(store, clock):
    """Levels that fail I8 or EXIT_BOUNDS are never held: the leg opens (it holds a real fill) and goes straight to
    EXITING(ANOMALY_EXIT, PROTECTIVE) with a CRITICAL alert, instead of sitting OPEN without a trusted stop."""
    rig = Rig(store, clock)
    st, leg, o, _ = rig.entry()
    rig.place(o)
    rig.snap(o, "COMPLETE", QTY, 42.15)
    with store.tx():
        fx = positions.on_entry_terminal(store, st, leg, profile=ExitProfile(hard_sl_pct=50.0), now_mono=rig.mono,
                                         now_wall=rig.wall)
    assert st.state is StructState.EXITING and leg.exit_reason == "ANOMALY_EXIT"
    assert leg.exit_urgency is Urgency.PROTECTIVE and st.opened_wall is not None
    assert ("CRITICAL", "EXIT_PARAMS_INVALID") in [(a.severity, a.code) for a in cmds(fx, Alert)]
    assert events(store, "POSITION_OPENED", structure_id=st.structure_id)[0]["severity"] == "CRITICAL"
