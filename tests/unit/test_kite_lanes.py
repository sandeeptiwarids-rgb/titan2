"""U07: broker/port.py, broker/kite.py (KiteBroker, KiteData) and broker/lanes.py (lanes, token buckets)."""

from __future__ import annotations

import functools
import inspect
import itertools
import os
import queue
import subprocess
import sys
import threading
import time
import types
from datetime import date, datetime, timedelta, timezone

import pytest
import requests

from titan2.broker.kite import (
    PLACE_PAYLOAD_KEYS, KiteBroker, KiteBuckets, KiteData, KitePatched, assert_kite_unpatched,
    classify_cancel_exception, classify_exception, normalize_status, order_from_kite, payload_problem, place_payload,
)
from titan2.broker.lanes import (
    OrderLane, ReadsLane, TokenBucket, execute_broker, failure_events, lane_for,
)
from titan2.broker.port import (
    ORDER_LANE_THREAD, AccountReads, BrokerPort, DataPort, NormalizationError, ReadFailed, error_outcome,
    format_error,
)
from titan2.clock import FakeClock
from titan2.core.events import (
    Cancel, CancelResult, ContractNote, ContractNoteOrder, ContractNoteResult, FetchHistorical, FetchMargins,
    FetchOrders, FetchPositions, FetchQuotes, FetchTradebook, FetchTrades, HistoricalResult, LookupResult, LookupTag,
    MarginsResult, OrderSnapshot, OrdersSnapshot, Place, PlaceResult, PollOrder, PositionsSnapshot, Prep, PrepResult,
    Priority, QuoteResult, Subscribe, TradebookSnapshot, TradesResult,
)
from titan2.model import ROLE_SIDE, Account, InvariantViolation, OrderRequest, Quote, Role, Side, make_tag

try:
    from kiteconnect import exceptions as kex
except ImportError:  # kiteconnect pulls in twisted; the classifier only looks at class names
    def _kite_exc(name, base, code):
        def init(self, message, code=code):
            base.__init__(self, message)
            self.code = code
        return type(name, (base,), {"__init__": init})

    _base = _kite_exc("KiteException", Exception, 500)
    kex = types.SimpleNamespace(KiteException=_base, **{
        n: _kite_exc(n, _base, c) for n, c in (("GeneralException", 500), ("TokenException", 403),
                                               ("PermissionException", 403), ("OrderException", 500),
                                               ("InputException", 400), ("DataException", 502),
                                               ("NetworkException", 503))})

IST_OFFSET = timezone(timedelta(hours=5, minutes=30))
SESSION = date(2026, 10, 12)
TS = "NIFTY26OCT25000CE"
SYM = f"NFO:{TS}"
LOT = 65                                  # NIFTY lot since the 2026 revision; always taken from the dump in prod


# ---------------------------------------------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------------------------------------------
class StubKite:
    """KiteConnect stand-in with the SDK's signatures. Records (name, args, kwargs); answers from `answers`
    (a value, an exception instance to raise, or a callable taking the call's kwargs)."""

    def __init__(self, **answers):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.answers = answers
        self._ids = itertools.count(1)

    def _answer(self, name, default, args=(), kwargs=None):
        kwargs = kwargs or {}
        self.calls.append((name, args, kwargs))
        ans = self.answers.get(name, default)
        if isinstance(ans, BaseException):
            raise ans
        return ans(*args, **kwargs) if callable(ans) else ans

    def names(self):
        return [c[0] for c in self.calls]

    def place_order(self, variety, exchange, tradingsymbol, transaction_type, quantity, product, order_type,
                    price=None, validity=None, validity_ttl=None, disclosed_quantity=None, trigger_price=None,
                    iceberg_legs=None, iceberg_quantity=None, auction_number=None, tag=None):
        kw = {k: v for k, v in locals().items() if k != "self" and v is not None}
        return self._answer("place_order", f"26101200000{next(self._ids):04d}", kwargs=kw)

    def cancel_order(self, variety, order_id, parent_order_id=None):
        return self._answer("cancel_order", order_id, kwargs={"variety": variety, "order_id": order_id})

    def order_history(self, order_id):
        return self._answer("order_history", [], kwargs={"order_id": order_id})

    def order_trades(self, order_id):
        return self._answer("order_trades", [], kwargs={"order_id": order_id})

    def orders(self):
        return self._answer("orders", [])

    def trades(self):
        return self._answer("trades", [])

    def positions(self):
        return self._answer("positions", {"net": [], "day": []})

    def margins(self, segment=None):
        return self._answer("margins", {"equity": {"net": 500000.0}})

    def quote(self, *instruments):
        return self._answer("quote", {}, args=instruments)

    def ltp(self, *instruments):
        return self._answer("ltp", {}, args=instruments)

    def historical_data(self, instrument_token, from_date, to_date, interval, continuous=False, oi=False):
        return self._answer("historical_data", [], args=(instrument_token, from_date, to_date, interval))

    def instruments(self, exchange=None):
        return self._answer("instruments", [], args=(exchange,))

    def profile(self):
        return self._answer("profile", {"user_id": "AB1234"})

    def get_virtual_contract_note(self, params):
        return self._answer("get_virtual_contract_note", [], args=(params,))

    def basket_order_margins(self, params, consider_positions=True, mode=None):
        return self._answer("basket_order_margins", {}, args=(params,),
                            kwargs={"consider_positions": consider_positions})


def fake_buckets(clock: FakeClock) -> KiteBuckets:
    return KiteBuckets.create(mono=clock.mono, sleep=clock.advance)


def broker(kite=None, clock=None, **kw) -> KiteBroker:
    clock = clock or FakeClock()
    kw.setdefault("thread_check", False)
    return KiteBroker(kite or StubKite(), clock=clock, buckets=fake_buckets(clock), **kw)


def data(kite=None, clock=None) -> KiteData:
    clock = clock or FakeClock()
    return KiteData(kite or StubKite(), clock=clock, buckets=fake_buckets(clock))


def order_req(role=Role.ENTRY, side=None, qty=LOT, limit=12.35, account=Account.LIVE, seq=1, attempt=1, **over):
    tag = over.pop("tag", None) or make_tag(role, account, SESSION, seq, attempt)
    fields = dict(account=account, tag=tag, role=role, symbol=SYM, tradingsymbol=TS, exchange="NFO", token=1234,
                  side=side or ROLE_SIDE[role], qty=qty, limit_px=limit, lot_size=LOT, tick=0.05)
    fields.update(over)
    return OrderRequest(**fields)


def kite_order(order_id="261012000000001", status="COMPLETE", qty=LOT, filled=LOT, avg=12.4, tag=None, side="BUY",
               **over):
    row = {"order_id": order_id, "status": status, "tradingsymbol": TS, "exchange": "NFO",
           "transaction_type": side, "product": "MIS", "order_type": "LIMIT", "validity": "IOC", "quantity": qty,
           "filled_quantity": filled, "average_price": avg, "tag": tag, "status_message": None,
           "status_message_raw": None, "placed_by": "AB1234", "order_timestamp": datetime(2026, 10, 12, 9, 20, 1),
           "exchange_timestamp": datetime(2026, 10, 12, 9, 20, 1), "exchange_update_timestamp": "2026-10-12 09:20:02"}
    row.update(over)
    return row


def kite_trade(trade_id="T1", order_id="261012000000001", qty=LOT, px=12.4, side="BUY"):
    return {"trade_id": trade_id, "order_id": order_id, "exchange": "NFO", "tradingsymbol": TS, "product": "MIS",
            "average_price": px, "quantity": qty, "transaction_type": side,
            "fill_timestamp": datetime(2026, 10, 12, 9, 20, 2), "exchange_timestamp": datetime(2026, 10, 12, 9, 20, 2)}


# ---------------------------------------------------------------------------------------------------------------
# KiteBroker.place: payload, local refusals, entries gate, thread check
# ---------------------------------------------------------------------------------------------------------------
def test_place_payload():
    kite = StubKite()
    kb = broker(kite, entries_allowed=True)
    r = order_req(Role.ENTRY, limit=12.35)
    assert kb.place(r) == PlaceResult(Account.LIVE, r.tag, "OK", "261012000000001")
    assert kite.calls == [("place_order", (), {
        "variety": "regular", "exchange": "NFO", "tradingsymbol": TS, "transaction_type": "BUY", "quantity": LOT,
        "product": "MIS", "order_type": "LIMIT", "price": 12.35, "validity": "IOC", "tag": r.tag})]
    sent = kite.calls[0][2]
    assert type(sent["transaction_type"]) is str and type(sent["quantity"]) is int and type(sent["price"]) is float
    assert len(sent["tag"]) == 15 and sent["tag"].startswith("T2EL")

    # a float with representation noise is sent as the clean 2-dp tick price
    r2 = order_req(Role.EXIT, limit=3 * 0.05, seq=2)
    assert kb.place(r2).outcome == "OK"
    assert repr(kite.calls[-1][2]["price"]) == "0.15" and kite.calls[-1][2]["transaction_type"] == "SELL"

    # anything that breaks the order rules is refused locally and never sent
    bad = [
        order_req(limit=12.33),                                    # off the tick grid
        order_req(qty=LOT + 5),                                    # not a lot multiple
        order_req(qty=0),
        order_req(Role.EXIT, side=Side.BUY),                       # I7: EXIT must be SELL
        order_req(account=Account.PAPER),                          # Kite is the LIVE account only
        order_req(symbol="NFO:OTHER26OCT100CE"),                   # symbol/tradingsymbol mismatch
        order_req(exchange="NSE", symbol=f"NSE:{TS}"),
        order_req(tag="T2EP26101200101"),                          # tag account letter is not L
        order_req(Role.EXTERNAL, side=Side.SELL, tag="T2XL26101200101"),   # the bot never places EXTERNAL
    ]
    before = len(kite.calls)
    for b in bad:
        res = kb.place(b)
        assert (res.outcome, res.error_class, res.broker_order_id) == ("REJECTED", "LOCAL", None), b
        assert res.message.startswith("LOCAL: ")
    assert len(kite.calls) == before

    # the payload builder only ever produces LIMIT/IOC/MIS/regular, and the last check catches edits
    p = place_payload(r)
    assert set(p) == PLACE_PAYLOAD_KEYS and payload_problem(p) is None
    assert payload_problem({**p, "order_type": "SL"}) is not None
    assert payload_problem({**p, "validity": "DAY"}) is not None
    assert payload_problem({**p, "product": "NRML"}) is not None
    assert payload_problem({**p, "variety": "amo"}) is not None
    assert payload_problem({**p, "trigger_price": 12.0}) is not None
    assert payload_problem({k: v for k, v in p.items() if k != "tag"}) is not None
    assert payload_problem({**p, "price": 0.0}) is not None

    # an empty order id is not an acknowledgement
    kb2 = broker(StubKite(place_order=""), entries_allowed=True)
    assert kb2.place(r).outcome == "AMBIGUOUS"


def test_entries_disallowed():
    kite = StubKite()
    kb = broker(kite)                                              # entries_allowed defaults to False
    assert kb.entries_allowed is False
    for role in (Role.ENTRY, Role.SELFTEST):
        res = kb.place(order_req(role))
        assert res == PlaceResult(Account.LIVE, res.order_pk, "REJECTED", None, "LOCAL",
                                  "LOCAL: live entries disabled")
    assert kite.calls == []
    # exits and covers always go through: they reduce risk
    assert kb.place(order_req(Role.EXIT, seq=2)).outcome == "OK"
    assert kb.place(order_req(Role.COVER, seq=3)).outcome == "OK"
    assert kb.cancel(Account.LIVE, "T2XL26101200201", "261012000000009").outcome == "OK"
    assert kite.names() == ["place_order", "place_order", "cancel_order"]
    # with ALLOW_LIVE=1 the entry is sent
    assert broker(kite, entries_allowed=True).place(order_req(Role.ENTRY, seq=4)).outcome == "OK"

    # only a real bool is accepted (bool("0") is True), and the flag cannot be flipped later
    for bad in ("0", "1", 1, None):
        with pytest.raises(TypeError):
            KiteBroker(StubKite(), entries_allowed=bad)
    with pytest.raises(AttributeError):
        kb.entries_allowed = True


def test_orders_disallowed_refuses_every_place_and_cancel():
    """The laptop profile (orders_allowed=False): no LIVE order of any role and no cancel reaches Kite, whatever
    entries_allowed says; the reads still work (recovery and recon read the LIVE account)."""
    kite = StubKite(orders=[kite_order(tag="T2XL26101200101", side="SELL")])
    kb = broker(kite, entries_allowed=True, orders_allowed=False)
    assert kb.orders_allowed is False and broker(StubKite()).orders_allowed is True      # on by default (EC2)
    for seq, role in enumerate((Role.ENTRY, Role.SELFTEST, Role.EXIT, Role.COVER), start=1):
        res = kb.place(order_req(role, seq=seq))
        assert res == PlaceResult(Account.LIVE, res.order_pk, "REJECTED", None, "LOCAL",
                                  "LOCAL: laptop profile: no LIVE orders"), role
    res = kb.cancel(Account.LIVE, "T2XL26101200201", "261012000000009")
    assert res == CancelResult(Account.LIVE, "T2XL26101200201", "REJECTED", "LOCAL: laptop profile: no LIVE orders")
    assert kite.calls == []                                        # nothing was sent
    assert [o.tag for o in kb.orders_today(Account.LIVE)] == ["T2XL26101200101"]
    assert kite.names() == ["orders"]
    # through the order lane, as the bot sends them: every answer is the local refusal, Kite sees no order call
    posted: list = []
    lane = OrderLane(kb, posted.append)
    r = order_req(Role.EXIT, seq=5)
    lane.submit(Place(Account.LIVE, r.tag, r))
    lane.submit(Cancel(Account.LIVE, r.tag, "261012000000001"))
    lane.drain()
    assert [(type(e).__name__, e.outcome) for e in posted] == [("PlaceResult", "REJECTED"),
                                                               ("CancelResult", "REJECTED")]
    assert kite.names() == ["orders"]
    for bad in ("0", 0, None):
        with pytest.raises(TypeError):
            KiteBroker(StubKite(), orders_allowed=bad)
    with pytest.raises(AttributeError):
        kb.orders_allowed = True


def test_thread_check_and_order_lane_safety_net():
    kite = StubKite()
    clock = FakeClock()
    kb = KiteBroker(kite, entries_allowed=True, clock=clock, buckets=fake_buckets(clock))   # thread_check on
    r = order_req(Role.ENTRY)
    with pytest.raises(InvariantViolation) as ei:                  # I2: not on lane-live-orders
        kb.place(r)
    assert ei.value.code == "I2"
    with pytest.raises(InvariantViolation):
        kb.cancel(Account.LIVE, r.tag, "1")
    with pytest.raises(InvariantViolation):
        kb.positions(Account.LIVE)
    assert kite.calls == []

    # on the lane thread it works
    out: queue.Queue = queue.Queue()
    lane = OrderLane(kb, out.put, mono=clock.mono)
    lane.start()
    try:
        lane.submit(Place(Account.LIVE, r.tag, r))
        assert out.get(timeout=5) == PlaceResult(Account.LIVE, r.tag, "OK", "261012000000001")
    finally:
        lane.stop(join_s=5)

    # the same lane loop on a wrongly named thread: the I2 violation is answered AMBIGUOUS, never dropped
    lane2 = OrderLane(kb, out.put, mono=clock.mono)
    t = threading.Thread(target=lane2.run, name="not-the-order-lane", daemon=True)
    t.start()
    try:
        r2 = order_req(Role.EXIT, seq=2)
        lane2.submit(Place(Account.LIVE, r2.tag, r2))
        res = out.get(timeout=5)
        assert (res.order_pk, res.outcome, res.error_class) == (r2.tag, "AMBIGUOUS", "InvariantViolation")
        lane2.submit(LookupTag(Account.LIVE, r2.tag))
        assert out.get(timeout=5) == LookupResult(Account.LIVE, r2.tag, False, None)
    finally:
        lane2.stop(join_s=5)
    assert kite.names() == ["place_order"]


def test_order_lane_routing():
    kb = broker(entries_allowed=True)
    posted = []
    lane = OrderLane(kb, posted.append)
    # mis-routed commands are refused at once, never queued or dropped
    rp = order_req(account=Account.PAPER)
    lane.submit(Place(Account.PAPER, rp.tag, rp))
    lane.submit(FetchPositions(Account.LIVE, Priority.EXIT))
    assert [type(e) for e in posted] == [PlaceResult, PositionsSnapshot]
    assert posted[0].outcome == "REJECTED" and posted[0].error_class == "LOCAL"
    assert posted[1].ok is False and posted[1].error.startswith("LOCAL: ")
    # inline drain runs queued commands in order
    r = order_req(Role.EXIT)
    lane.submit(Place(Account.LIVE, r.tag, r))
    lane.submit(Cancel(Account.LIVE, r.tag, "261012000000001"))
    events = lane.drain()
    assert [type(e) for e in events] == [PlaceResult, CancelResult] and posted[-2:] == events

    assert lane_for(Place(Account.LIVE, r.tag, r)) == "orders"
    assert lane_for(FetchTrades(Account.LIVE, r.tag, "1")) == "orders"
    assert lane_for(Place(Account.SHADOW, r.tag, r)) == "paper"
    assert lane_for(FetchPositions(Account.LIVE, 0)) == "reads"
    assert lane_for(FetchPositions(Account.PAPER, 0)) == "paper"
    assert lane_for(FetchQuotes("q", (SYM,), 3)) == "reads"
    assert lane_for(Prep("INSTRUMENTS")) == "reads"
    assert lane_for(Subscribe((1,), "full")) is None


# ---------------------------------------------------------------------------------------------------------------
# Error taxonomy (§5.8)
# ---------------------------------------------------------------------------------------------------------------
class InputExceptionLookalike(Exception):
    """Same name as Kite's, but not a KiteException: not a Kite answer, so not a definite reject."""


InputExceptionLookalike.__name__ = "InputException"


def test_error_classification():
    r = order_req(Role.EXIT)
    cases = [
        (kex.InputException("Invalid price"), "REJECTED", False),
        (kex.OrderException("RMS: margin exceeds"), "REJECTED", False),
        (kex.TokenException("Incorrect `api_key` or `access_token`."), "FATAL", False),
        (kex.PermissionException("IP not allowed: not a registered static IP"), "FATAL", False),
        (kex.NetworkException("Too many requests", code=429), "AMBIGUOUS", True),
        (kex.NetworkException("Gateway timed out", code=503), "AMBIGUOUS", False),
        (kex.GeneralException("Something went wrong"), "AMBIGUOUS", False),
        (kex.DataException("Couldn't parse the JSON response"), "AMBIGUOUS", False),
        (requests.Timeout("Read timed out"), "AMBIGUOUS", False),
        (requests.ConnectionError("Connection reset"), "AMBIGUOUS", False),
        (KeyError("content-type"), "AMBIGUOUS", False),
        (InputExceptionLookalike("bad"), "AMBIGUOUS", False),
    ]
    for exc, outcome, throttled in cases:
        res = broker(StubKite(place_order=exc)).place(r)
        assert (res.outcome, res.throttled, res.error_class, res.broker_order_id) == \
            (outcome, throttled, type(exc).__name__, None), exc
        assert res.message == str(exc)

    # cancels: a Kite answer that the order is already terminal is BENIGN; FATAL always wins
    cancels = [
        (kex.InputException("Order cannot be cancelled as it is already complete."), "BENIGN"),
        (kex.OrderException("Order not found"), "BENIGN"),
        (kex.InputException("Invalid `order_id`."), "REJECTED"),
        (kex.TokenException("Session expired; cancel failed"), "FATAL"),
        (kex.NetworkException("Too many requests", code=429), "AMBIGUOUS"),
        (requests.Timeout("cancel request timed out"), "AMBIGUOUS"),     # not a Kite answer: unknown
    ]
    for exc, outcome in cancels:
        assert classify_cancel_exception(exc)[0] == outcome, exc
        res = broker(StubKite(cancel_order=exc)).cancel(Account.LIVE, "T2XL26101200101", "1")
        assert (res.outcome, res.message) == (outcome, str(exc))
    kite = StubKite()
    assert broker(kite).cancel(Account.LIVE, "T2XL26101200101", None).outcome == "REJECTED"
    assert kite.calls == []

    # reads: the class survives into the event text, so the engine can see a dead token
    assert classify_exception(kex.TokenException("x")).outcome == "FATAL"
    for exc, head in [(kex.TokenException("expired"), "FATAL:TokenException(403): "),
                      (kex.NetworkException("Too many requests", code=429), "AMBIGUOUS:NetworkException(429): "),
                      (requests.ConnectionError("down"), "AMBIGUOUS:ConnectionError: ")]:
        kd = data(StubKite(positions=exc))
        with pytest.raises(ReadFailed) as ei:
            kd.positions(Account.LIVE)
        assert ei.value.text.startswith(head)
        ev, = execute_broker(FetchPositions(Account.LIVE, Priority.RECON), kd, FakeClock().mono)
        assert isinstance(ev, PositionsSnapshot) and ev.ok is False and ev.rows == ()
        assert ev.error.startswith(head) and error_outcome(ev.error) == head.split(":")[0]
    assert error_outcome(format_error("FATAL", "TokenException", "m")) == "FATAL"
    assert error_outcome("TIMEOUT") is None and error_outcome(None) is None
    # Kite refuses other accounts on reads too
    with pytest.raises(ReadFailed, match="^REJECTED:LOCAL"):
        data().positions(Account.PAPER)


# ---------------------------------------------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------------------------------------------
def test_normalization():
    assert [normalize_status(s) for s in ("OPEN", "TRIGGER PENDING", "VALIDATION PENDING", "PUT ORDER REQ RECEIVED",
                                          "OPEN PENDING", "CANCEL PENDING", "MODIFY PENDING", "AMO REQ RECEIVED",
                                          "SOMETHING NEW")] == ["WORKING"] * 9
    assert [normalize_status(s) for s in ("COMPLETE", "complete ", "CANCELLED", "CANCELLED AMO", "REJECTED")] == \
        ["COMPLETE", "COMPLETE", "CANCELLED", "CANCELLED", "REJECTED"]
    with pytest.raises(NormalizationError):
        normalize_status("")

    # orders
    o = order_from_kite(kite_order(status="CANCELLED", filled=0, avg=0, tag="T2EL26101200101",
                                   status_message=None, status_message_raw="17070 : price outside LPP"),
                        Account.LIVE)
    assert (o.broker_order_id, o.status, o.raw_status, o.filled_qty, o.avg_px, o.tag) == \
        ("261012000000001", "CANCELLED", "CANCELLED", 0, None, "T2EL26101200101")
    assert (o.symbol, o.tradingsymbol, o.exchange, o.side, o.product, o.placed_by) == \
        (SYM, TS, "NFO", Side.BUY, "MIS", "AB1234")
    assert o.status_msg == "17070 : price outside LPP"
    assert o.order_wall == datetime(2026, 10, 12, 9, 20, 1, tzinfo=IST_OFFSET)
    assert o.exch_wall == datetime(2026, 10, 12, 9, 20, 2, tzinfo=IST_OFFSET)      # exchange_update_timestamp
    assert o.exch_wall.utcoffset() == timedelta(hours=5, minutes=30)
    partial = order_from_kite(kite_order(status="CANCELLED", qty=130, filled=65, avg=12.45, tag=""), Account.LIVE)
    assert (partial.filled_qty, partial.avg_px, partial.tag) == (65, 12.45, None)
    for bad in (kite_order(filled=70), kite_order(filled=65, avg=0), kite_order(side="BUYY"),
                kite_order(order_id=""), kite_order(quantity="65"), kite_order(order_timestamp="yesterday")):
        with pytest.raises(NormalizationError):
            order_from_kite(bad, Account.LIVE)

    # a malformed row fails the whole read: nothing is inferred from a partial order book
    clock = FakeClock()
    kd = data(StubKite(orders=[kite_order(), kite_order(order_id="2", filled=99)]), clock)
    ev, = execute_broker(FetchOrders(Account.LIVE, Priority.RECON), kd, clock.mono)
    assert isinstance(ev, OrdersSnapshot) and ev.ok is False and ev.orders == ()
    assert ev.error.startswith("AMBIGUOUS:NormalizationError: ")
    kd = data(StubKite(orders=[kite_order(), kite_order(order_id="2", status="OPEN", filled=0, avg=0)]), clock)
    ev, = execute_broker(FetchOrders(Account.LIVE, Priority.RECON), kd, clock.mono)
    assert ev.ok is True and [x.status for x in ev.orders] == ["COMPLETE", "WORKING"]

    # order_history: the latest entry; empty history is None
    hist = [kite_order(status="OPEN", filled=0, avg=0), kite_order(status="COMPLETE")]
    kb = broker(StubKite(order_history=hist))
    last = kb.order_history(Account.LIVE, "261012000000001")
    assert (last.status, last.filled_qty, last.avg_px) == ("COMPLETE", LOT, 12.4)
    assert broker(StubKite(order_history=[])).order_history(Account.LIVE, "261012000000001") is None
    with pytest.raises(ReadFailed):
        broker(StubKite(order_history=hist)).order_history(Account.LIVE, "999")   # another order's history

    # trades
    kb = broker(StubKite(order_trades=[kite_trade("T1", qty=65, px=12.4), kite_trade("T2", qty=65, px=12.45)]))
    t1, t2 = kb.order_trades(Account.LIVE, "261012000000001")
    assert (t1.trade_id, t1.broker_order_id, t1.symbol, t1.side, t1.qty, t1.px) == \
        ("T1", "261012000000001", SYM, Side.BUY, 65, 12.4)
    assert t1.fill_wall == datetime(2026, 10, 12, 9, 20, 2, tzinfo=IST_OFFSET) and t2.px == 12.45
    with pytest.raises(ReadFailed):
        broker(StubKite(order_trades=[kite_trade(order_id="other")])).order_trades(Account.LIVE, "261012000000001")

    # positions: the signed "net" book, every product
    net = [{"tradingsymbol": TS, "exchange": "NFO", "product": "MIS", "quantity": -65, "buy_quantity": 0,
            "sell_quantity": 65, "buy_value": 0, "sell_value": 806.0},
           {"tradingsymbol": "RELIANCE", "exchange": "NSE", "product": "CNC", "quantity": 10, "buy_quantity": 10,
            "sell_quantity": 0, "buy_value": 29000.0, "sell_value": 0}]
    rows = data(StubKite(positions={"net": net, "day": []})).positions(Account.LIVE)
    assert [(p.symbol, p.product, p.net_qty, p.sell_value) for p in rows] == \
        [(SYM, "MIS", -65, 806.0), ("NSE:RELIANCE", "CNC", 10, 0.0)]
    with pytest.raises(ReadFailed):
        data(StubKite(positions={"day": net})).positions(Account.LIVE)

    # margins: the smaller of the two available figures [V]
    m = data(StubKite(margins={"equity": {"net": 245000.5, "available": {"live_balance": 240000.0}}})).margins(
        Account.LIVE)
    assert (m.account, m.available_rs) == (Account.LIVE, 240000.0) and m.raw["equity"]["net"] == 245000.5
    with pytest.raises(ReadFailed):
        data(StubKite(margins={"equity": {"enabled": True}})).margins(Account.LIVE)

    # REST quotes: depth -> Levels (empty levels dropped, best first), age from receipt, exchange stamp kept
    raw = {
        SYM: {"instrument_token": 1234, "timestamp": datetime(2026, 10, 12, 9, 20, 5),
              "last_trade_time": datetime(2026, 10, 12, 9, 20, 4), "last_price": 12.4, "volume": 150000,
              "depth": {"buy": [{"price": 12.3, "quantity": 1300, "orders": 5},
                                {"price": 12.35, "quantity": 650, "orders": 3},
                                {"price": 0, "quantity": 0, "orders": 0}],
                        "sell": [{"price": 12.45, "quantity": 325, "orders": 2},
                                 {"price": 0, "quantity": 0, "orders": 0}]}},
        "NSE:NIFTY 50": {"instrument_token": 256265, "timestamp": datetime(2026, 10, 12, 9, 20, 5),
                         "last_price": 25012.5},
        "NFO:BROKEN": {"instrument_token": "x"},
    }
    clock = FakeClock("2026-10-12T09:20:06+05:30")
    kite = StubKite(quote=raw)
    quotes = data(kite, clock).quote([SYM, "NSE:NIFTY 50", "NFO:BROKEN", "NFO:ABSENT", SYM])
    assert kite.calls == [("quote", ([SYM, "NSE:NIFTY 50", "NFO:BROKEN", "NFO:ABSENT"],), {})]
    assert set(quotes) == {SYM, "NSE:NIFTY 50"}                    # malformed and omitted symbols are absent
    q = quotes[SYM]
    assert (q.bid, q.ask, len(q.bids), len(q.asks), q.bids[0].qty, q.bids[0].orders) == (12.35, 12.45, 2, 1, 650, 3)
    assert (q.token, q.ltp, q.volume, q.source) == (1234, 12.4, 150000, "REST")
    assert (q.recv_mono, q.recv_wall) == (clock.mono(), clock.now_wall())
    assert q.exch_wall == datetime(2026, 10, 12, 9, 20, 5, tzinfo=IST_OFFSET)
    assert q.last_trade_wall == datetime(2026, 10, 12, 9, 20, 4, tzinfo=IST_OFFSET)
    idx = quotes["NSE:NIFTY 50"]
    assert (idx.bids, idx.asks, idx.volume, idx.ltp) == ((), (), None, 25012.5)
    with pytest.raises(ValueError):
        data().quote([f"NFO:S{i}" for i in range(501)])
    assert data(StubKite(ltp={SYM: {"instrument_token": 1234, "last_price": 12.4}})).ltp([SYM, "NFO:X"]) == \
        {SYM: 12.4}

    # historical candles -> Bars in IST; an inconsistent candle is kept but invalid
    candles = [{"date": datetime(2026, 10, 9, 9, 15, tzinfo=IST_OFFSET), "open": 100.0, "high": 101.0,
                "low": 99.5, "close": 100.5, "volume": 12000},
               {"date": datetime(2026, 10, 9, 9, 20, tzinfo=IST_OFFSET), "open": 100.0, "high": 99.0,
                "low": 99.5, "close": 100.5, "volume": 9000}]
    kite = StubKite(historical_data=candles)
    frm = datetime(2026, 10, 9, 9, 15, tzinfo=IST_OFFSET)
    bars = data(kite).historical("NSE:RELIANCE", 738561, frm, frm + timedelta(hours=6))
    assert kite.calls[0] == ("historical_data", (738561, "2026-10-09 09:15:00", "2026-10-09 15:15:00", "5minute"), {})
    assert [(b.start.hour, b.start.minute, b.valid, b.source) for b in bars] == [(9, 15, True, "HIST"),
                                                                                 (9, 20, False, "HIST")]
    with pytest.raises(ValueError):                                # a naive datetime is refused
        data().historical("NSE:RELIANCE", 1, datetime(2026, 10, 9, 9, 15), frm)

    # virtual contract note: positional rows, totals and components
    cn = [ContractNoteOrder("261012000000001", "NFO", TS, Side.SELL, 65, 13.0)]
    kite = StubKite(get_virtual_contract_note=[{"tradingsymbol": TS, "charges": {
        "transaction_tax": 1.27, "exchange_turnover_charge": 0.3, "sebi_turnover_charge": 0.0, "brokerage": 20,
        "stamp_duty": 0, "gst": {"total": 3.65}, "total": 25.22}}])
    row, = data(kite).contract_note(Account.LIVE, cn)
    assert (row.broker_order_id, row.total, row.components["brokerage"], row.components["gst"]) == \
        ("261012000000001", 25.22, 20.0, 3.65)
    sent = kite.calls[0][1][0][0]
    assert sent == {"order_id": "261012000000001", "exchange": "NFO", "tradingsymbol": TS,
                    "transaction_type": "SELL", "variety": "regular", "product": "MIS", "order_type": "LIMIT",
                    "quantity": 65, "average_price": 13.0}
    with pytest.raises(ReadFailed):                                # row count must match the request
        data(StubKite(get_virtual_contract_note=[])).contract_note(Account.LIVE, cn)
    assert data(StubKite()).contract_note(Account.LIVE, []) == ()


# ---------------------------------------------------------------------------------------------------------------
# execute_broker: lookups, polls, account reads
# ---------------------------------------------------------------------------------------------------------------
def test_lookup_poll_and_account_reads():
    clock = FakeClock()
    tag = make_tag(Role.EXIT, Account.LIVE, SESSION, 1, 2)
    book = [kite_order(order_id="1", tag="T2EL26101200101"), kite_order(order_id="2", tag=tag, side="SELL")]
    kb = broker(StubKite(orders=book, order_history=[kite_order(order_id="2", tag=tag, side="SELL")]), clock)

    ev, = execute_broker(LookupTag(Account.LIVE, tag), kb, clock.mono)
    assert ev.ok is True and ev.found.broker_order_id == "2" and ev.found.tag == tag
    assert execute_broker(LookupTag(Account.LIVE, "T2XL26101200999"), kb, clock.mono) == \
        [LookupResult(Account.LIVE, "T2XL26101200999", True, None, clock.mono())]  # looked, not there (asked now)
    dup = broker(StubKite(orders=book + [kite_order(order_id="3", tag=tag)]), clock)
    assert execute_broker(LookupTag(Account.LIVE, tag), dup, clock.mono) == \
        [LookupResult(Account.LIVE, tag, False, None, clock.mono())]
    boom = kex.NetworkException("Too many requests", code=429)
    err = broker(StubKite(orders=boom, order_history=boom, order_trades=boom), clock)
    assert execute_broker(LookupTag(Account.LIVE, tag), err, clock.mono) == [LookupResult(Account.LIVE, tag, False)]

    snap, = execute_broker(PollOrder(Account.LIVE, tag, "2"), kb, clock.mono)
    assert snap == OrderSnapshot(Account.LIVE, "2", tag, "COMPLETE", LOT, 12.4, None,
                                 datetime(2026, 10, 12, 9, 20, 2, tzinfo=IST_OFFSET), "POLL", clock.mono())
    snap, = execute_broker(PollOrder(Account.LIVE, tag, None), kb, clock.mono, poll_source="SIM")  # by tag
    assert (snap.broker_order_id, snap.source) == ("2", "SIM")
    assert execute_broker(PollOrder(Account.LIVE, tag, "2"), err, clock.mono) == []   # the poll timer retries

    tr, = execute_broker(FetchTrades(Account.LIVE, tag, "2"), broker(StubKite(order_trades=[
        kite_trade(order_id="2", side="SELL")]), clock), clock.mono)
    assert tr.ok is True and tr.trades[0].side == Side.SELL
    assert execute_broker(FetchTrades(Account.LIVE, tag, "2"), err, clock.mono) == \
        [TradesResult(Account.LIVE, tag, False, ())]

    # a port never serves another account's command
    ev, = execute_broker(FetchMargins(Account.SHADOW), kb, clock.mono)
    assert ev == MarginsResult(Account.SHADOW, False, None, clock.mono())
    r = order_req(account=Account.PAPER)
    ev, = execute_broker(Place(Account.PAPER, r.tag, r), kb, clock.mono)
    assert (ev.outcome, ev.error_class) == ("REJECTED", "LOCAL")

    # account reads: req_mono is taken before the call, recv_mono after it
    kd = data(StubKite(positions=lambda: clock.advance(0.4) or {"net": []}), clock)
    t0 = clock.mono()
    ev, = execute_broker(FetchPositions(Account.LIVE, Priority.EXIT), kd, clock.mono)
    assert (ev.ok, ev.rows, ev.req_mono, ev.recv_mono) == (True, (), t0, pytest.approx(t0 + 0.4))
    ev, = execute_broker(FetchTradebook(Account.LIVE), data(StubKite(trades=[kite_trade()]), clock), clock.mono)
    assert isinstance(ev, TradebookSnapshot) and ev.ok and ev.trades[0].trade_id == "T1"
    ev, = execute_broker(FetchMargins(Account.LIVE), data(StubKite(), clock), clock.mono)
    assert ev.ok and ev.margins.available_rs == 500000.0
    ev, = execute_broker(ContractNote(Account.LIVE, ()), data(StubKite(), clock), clock.mono)
    assert ev == ContractNoteResult(Account.LIVE, True, ())

    # every command type has a conservative failure answer
    now_wall = clock.now_wall()
    for cmd in (Place(Account.LIVE, r.tag, r), Cancel(Account.LIVE, "pk", "1"), LookupTag(Account.LIVE, "pk"),
                FetchTrades(Account.LIVE, "pk", "1"), FetchPositions(Account.LIVE, 0), FetchOrders(Account.LIVE, 0),
                FetchTradebook(Account.LIVE), FetchMargins(Account.LIVE), ContractNote(Account.LIVE, ()),
                FetchQuotes("q", (SYM,), 0), FetchHistorical("h", SYM, 1, now_wall, now_wall),
                Prep("BAN_LIST")):
        ev, = failure_events(cmd, "AMBIGUOUS:X: boom", clock.mono())
        assert getattr(ev, "ok", None) is False or getattr(ev, "outcome", None) == "AMBIGUOUS"
    assert failure_events(PollOrder(Account.LIVE, "pk", "1"), "x", 0.0) == []


def test_a_lookup_carries_when_its_orders_read_was_asked():
    """Review round 3: a tag lookup's miss decides NOT_PLACED by when its orders() read was asked, so the answer
    carries that time (req_mono), taken before the call like a positions read's, never when the answer is ready.
    Paper-sim passes its own receipt time."""
    clock = FakeClock()
    tag = make_tag(Role.EXIT, Account.LIVE, SESSION, 1, 2)
    kb = broker(StubKite(orders=lambda: clock.advance(5.5) or []), clock)    # a slow orders() in a brownout
    asked = clock.mono()
    ev, = execute_broker(LookupTag(Account.LIVE, tag), kb, clock.mono)
    assert (ev.ok, ev.found, ev.req_mono) == (True, None, asked) and clock.mono() == pytest.approx(asked + 5.5)
    ev, = execute_broker(LookupTag(Account.LIVE, tag), kb, clock.mono, req_mono=asked - 1.0)
    assert ev.req_mono == asked - 1.0


# ---------------------------------------------------------------------------------------------------------------
# ReadsLane: batching, priority, timeout
# ---------------------------------------------------------------------------------------------------------------
class StubData:
    """Market-data/account-read stand-in for the reads lane (no order methods)."""

    accounts = frozenset({Account.LIVE})

    def __init__(self, clock: FakeClock, journal: list):
        self.clock = clock
        self.journal = journal
        self.quote_calls: list[list[str]] = []
        self.omit: set[str] = set()
        self.fail_next: Exception | None = None
        self.read_delay = 0.0
        self.quote_delay = 0.0
        self.during_quote = None                 # one-shot hook run inside the next quote() call

    def quote(self, symbols):
        self.quote_calls.append(list(symbols))
        hook, self.during_quote = self.during_quote, None
        if hook is not None:
            hook()
        self.clock.advance(self.quote_delay)
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc
        return {s: Quote(s, 1, ((10.0, 100),), ((10.1, 100),), None, None, None, None, self.clock.mono(),
                         self.clock.now_wall(), "REST") for s in symbols if s not in self.omit}

    def positions(self, account):
        self.journal.append("positions")
        self.clock.advance(self.read_delay)
        return ()

    def orders_today(self, account):
        self.journal.append("orders")
        return ()

    def historical(self, symbol, token, frm, to, interval):
        self.journal.append(f"historical {symbol}")
        return ()


def test_reads_batch_priority_timeout():
    clock = FakeClock()
    journal: list = []
    sd = StubData(clock, journal)
    lane = ReadsLane(sd, lambda ev: journal.append(ev), lambda q: journal.append(("cache", q.symbol)),
                     mono=clock.mono)
    paper = tuple(f"NFO:P{i}" for i in range(300))
    exits = tuple(f"NFO:X{i}" for i in range(300))
    lane.submit(FetchQuotes("paper", paper, Priority.PAPER))
    lane.submit(FetchQuotes("exit", exits + paper[:10], Priority.EXIT))
    lane.submit(FetchPositions(Account.LIVE, Priority.RECON))

    # 1st call: EXIT first, then the batch fills to 500 in priority order (shared symbols asked once)
    ev = lane.step()
    assert sd.quote_calls == [list(exits) + list(paper[:200])]
    assert ev == [QuoteResult("exit", True, exits + paper[:10], (), None)]
    assert journal.index(("cache", "NFO:X0")) < journal.index(ev[0])          # cache filled before the result
    # 2nd: RECON positions beat the remaining PAPER quotes
    ev = lane.step()
    assert [type(e) for e in ev] == [PositionsSnapshot] and ev[0].ok
    # 3rd: the rest of the paper request
    ev = lane.step()
    assert sd.quote_calls[1] == list(paper[200:])
    assert ev == [QuoteResult("paper", True, paper, (), None)]
    assert lane.backlog() == 0 and lane.step() == []

    # a request that has waited 3 s is answered TIMEOUT, without a call: here one queued during a slow quote call
    sd.quote_delay = 3.05
    sd.during_quote = lambda: lane.submit(FetchQuotes("shadow", ("NFO:S1",), Priority.SHADOW))
    lane.submit(FetchQuotes("slow", ("NFO:Q1",), Priority.EXIT))
    assert lane.step() == [QuoteResult("slow", True, ("NFO:Q1",), (), None)]  # a slow 3 s quote call
    sd.quote_delay = 0.0
    calls = len(sd.quote_calls)
    assert lane.step() == [QuoteResult("shadow", False, (), ("NFO:S1",), "TIMEOUT")]
    assert len(sd.quote_calls) == calls
    # ... but 2.9 s is not a timeout
    sd.quote_delay = 2.9
    sd.during_quote = lambda: lane.submit(FetchQuotes("shadow2", ("NFO:S2",), Priority.SHADOW))
    lane.submit(FetchQuotes("slow2", ("NFO:Q2",), Priority.EXIT))
    lane.step()
    sd.quote_delay = 0.0
    assert lane.step() == [QuoteResult("shadow2", True, ("NFO:S2",), (), None)]
    # a slow account read is not a quote request's wait: on the threads quotes never queue behind it
    sd.read_delay = 3.05
    lane.submit(FetchQuotes("shadow3", ("NFO:S3",), Priority.SHADOW))
    lane.submit(FetchPositions(Account.LIVE, Priority.EXIT))
    assert [type(e) for e in lane.step()] == [PositionsSnapshot]              # a slow 3 s positions call
    assert lane.step() == [QuoteResult("shadow3", True, ("NFO:S3",), (), None)]
    sd.read_delay = 0.0

    # omitted symbols are reported missing; duplicates asked once
    sd.omit = {"NFO:GONE"}
    lane.submit(FetchQuotes("m", ("NFO:GONE", "NFO:OK1", "NFO:OK1"), Priority.EXIT))
    assert lane.step() == [QuoteResult("m", True, ("NFO:OK1",), ("NFO:GONE",), None)]
    assert sd.quote_calls[-1] == ["NFO:GONE", "NFO:OK1"]
    # a failed call fails its requests (nothing inferred), keeping the classified error
    sd.fail_next = ReadFailed("AMBIGUOUS:NetworkException(429): Too many requests")
    lane.submit(FetchQuotes("f", ("NFO:A", "NFO:B"), Priority.EXIT))
    assert lane.step() == [QuoteResult("f", False, (), ("NFO:A", "NFO:B"),
                                       "AMBIGUOUS:NetworkException(429): Too many requests")]
    # one request larger than a batch spans calls; an empty request is answered without a call
    big = tuple(f"NFO:B{i}" for i in range(650))
    lane.submit(FetchQuotes("big", big, Priority.ENTRY_LIVE))
    lane.submit(FetchQuotes("empty", (), Priority.EXIT))
    calls = len(sd.quote_calls)
    events = lane.drain()
    assert [len(c) for c in sd.quote_calls[calls:]] == [500, 150]
    assert events == [QuoteResult("empty", True, (), (), None), QuoteResult("big", True, big, (), None)]
    # inline execute: same batching, one answer
    calls = len(sd.quote_calls)
    assert lane.execute(FetchQuotes("inline", big, Priority.EXIT)) == [QuoteResult("inline", True, big, (), None)]
    assert [len(c) for c in sd.quote_calls[calls:]] == [500, 150]

    # bad commands are answered at once, never queued
    got = []
    lane.submit(FetchQuotes("bad", (SYM,), 9), reply=got.append)
    lane.submit(FetchPositions(Account.PAPER, Priority.RECON), reply=got.append)
    r = order_req(Role.EXIT)
    lane.submit(Place(Account.LIVE, r.tag, r), reply=got.append)
    assert [(type(e).__name__, getattr(e, "ok", getattr(e, "outcome", None))) for e in got] == \
        [("QuoteResult", False), ("PositionsSnapshot", False), ("PlaceResult", "REJECTED")]
    assert lane.backlog() == 0
    # the reads lane never gets an object with order methods
    with pytest.raises(TypeError):
        ReadsLane(broker(), journal.append, journal.append)
    with pytest.raises(ValueError):
        ReadsLane(sd, journal.append, journal.append, max_batch=501)


def test_reads_lane_background_prep_and_reply():
    clock = FakeClock()
    journal: list = []
    sd = StubData(clock, journal)
    calls = []

    def prep_ok():
        calls.append("instruments")
        return True, "NFO 85000 rows"

    def prep_boom():
        raise RuntimeError("NSE down")

    lane = ReadsLane(sd, journal.append, lambda q: None, mono=clock.mono,
                     prep={"INSTRUMENTS": prep_ok, "BAN_LIST": prep_boom})
    frm = clock.now_wall()
    lane.submit(Prep("INSTRUMENTS"))
    lane.submit(FetchHistorical("h1", "NSE:RELIANCE", 738561, frm, frm))
    lane.submit(Prep("BAN_LIST"))
    lane.submit(Prep("PREV_BARS"))
    lane.submit(FetchOrders(Account.LIVE, Priority.EXIT))       # submitted last, served first
    paper_answers = []
    lane.submit(FetchQuotes("paper-depth", (SYM,), Priority.PAPER), reply=paper_answers.append)
    events = lane.drain()
    assert [type(e).__name__ for e in events] == ["OrdersSnapshot", "QuoteResult", "PrepResult", "HistoricalResult",
                                                  "PrepResult", "PrepResult"]
    assert paper_answers == [events[1]] and events[1] not in journal          # the paper reply went to its caller
    assert events[2] == PrepResult("INSTRUMENTS", True, "NFO 85000 rows")
    assert events[3] == HistoricalResult("h1", "NSE:RELIANCE", True, ())
    assert events[4].ok is False and "NSE down" in events[4].detail
    assert events[5] == PrepResult("PREV_BARS", False, "LOCAL: no prep handler for PREV_BARS")
    assert calls == ["instruments"]


def test_reads_lane_always_answers():
    """A taken request is answered even when the read blows up or another caller's reply callback fails."""
    clock = FakeClock()
    sd = StubData(clock, [])

    def broken_positions(account):
        raise InvariantViolation("I2", "wired wrong")              # escapes execute_broker by design

    sd.positions = broken_positions
    posted = []
    lane = ReadsLane(sd, posted.append, lambda q: None, mono=clock.mono)

    def bad_reply(ev):
        raise RuntimeError("caller gone")

    lane.submit(FetchQuotes("a", (SYM,), Priority.EXIT), reply=bad_reply)
    lane.submit(FetchQuotes("b", ("NFO:Y",), Priority.EXIT))
    lane.submit(FetchPositions(Account.LIVE, Priority.RECON))
    lane.submit(FetchQuotes("bad", None, Priority.EXIT))          # malformed: refused at once, no exception
    events = lane.drain()
    assert [type(e).__name__ for e in events] == ["QuoteResult", "QuoteResult", "PositionsSnapshot"]
    assert posted[0] == QuoteResult("bad", False, (), (), "LOCAL: bad symbols None")
    assert posted[1] == QuoteResult("b", True, ("NFO:Y",), (), None)          # delivered despite a's failure
    pos = posted[2]
    assert isinstance(pos, PositionsSnapshot) and pos.ok is False and "InvariantViolation" in pos.error


def test_paper_contract_note_is_priced_by_kite_on_lane_reads():
    """§4.2 / §8.1 (review M1): ContractNote is a market-data command. lane-reads asks Kite's calculator on every
    account, so the PAPER sample (the paper exchange's synthetic ids) is checked by Zerodha, never billed by the
    paper exchange with titan2's own charges; a refusal is ok=False (the engine's VCN_PAPER_UNSUPPORTED)."""
    clock = FakeClock()
    cn = (ContractNoteOrder("P261012000000001", "NFO", TS, Side.BUY, 65, 12.0),)
    for acct in (Account.LIVE, Account.PAPER, Account.SHADOW):
        assert lane_for(ContractNote(acct, cn)) == "reads"
    priced = [{"tradingsymbol": TS, "charges": {"total": 20.5}}]
    kite = StubKite(get_virtual_contract_note=priced)
    row, = data(kite, clock).contract_note(Account.PAPER, cn)
    assert (row.broker_order_id, row.total) == ("P261012000000001", 20.5)
    assert kite.calls[0][1][0][0]["order_id"] == "P261012000000001"      # the synthetic id goes to Kite as it is
    with pytest.raises(ReadFailed, match="LIVE only"):                   # the order port stays the LIVE account's
        broker(StubKite(get_virtual_contract_note=priced), clock).contract_note(Account.PAPER, cn)

    posted: list = []
    lane = ReadsLane(data(kite, clock), posted.append, lambda q: None, mono=clock.mono)
    lane.submit(ContractNote(Account.PAPER, cn))
    lane.drain()
    assert posted == [ContractNoteResult(Account.PAPER, True, (row,))]
    refusing = StubKite(get_virtual_contract_note=kex.InputException("order P261012000000001 not found"))
    posted.clear()
    lane = ReadsLane(data(refusing, clock), posted.append, lambda q: None, mono=clock.mono)
    lane.submit(ContractNote(Account.PAPER, cn))
    lane.drain()
    assert posted == [ContractNoteResult(Account.PAPER, False, ())]


def test_reads_lane_thread():
    clock = FakeClock()
    sd = StubData(clock, [])
    out: queue.Queue = queue.Queue()
    lane = ReadsLane(sd, out.put, lambda q: None, mono=clock.mono)
    t = lane.start()
    try:
        assert t.name == "lane-reads"
        lane.submit(FetchQuotes("q", (SYM,), Priority.EXIT))
        assert out.get(timeout=5) == QuoteResult("q", True, (SYM,), (), None)
    finally:
        lane.stop(join_s=5)
    assert not t.is_alive()


# ---------------------------------------------------------------------------------------------------------------
# Token buckets
# ---------------------------------------------------------------------------------------------------------------
def test_bucket_never_fails_open():
    clock = FakeClock()
    slept: list[float] = []

    def sleep(s):
        slept.append(s)
        clock.advance(s)

    b = TokenBucket("orders", rate=8, burst=3, mono=clock.mono, sleep=sleep)
    assert all(b.acquire() for _ in range(3)) and slept == []          # the burst goes at once
    assert b.acquire(timeout=0) is False                              # no token: refuse, never pass
    assert b.acquire(timeout=0.1) is False and slept == []            # the next token is 0.125 s away
    assert b.acquire() is True
    assert sum(slept) == pytest.approx(0.125, abs=1e-6)

    # a sleep that fails propagates; no token is granted
    def broken_sleep(s):
        raise RuntimeError("sleep failed")

    b2 = TokenBucket("x", rate=1, burst=1, mono=clock.mono, sleep=broken_sleep)
    assert b2.acquire()
    with pytest.raises(RuntimeError):
        b2.acquire()

    # a clock that runs backwards adds no tokens
    t = [100.0]
    b3 = TokenBucket("y", rate=10, burst=1, mono=lambda: t[0], sleep=lambda s: None)
    assert b3.acquire(timeout=0)
    t[0] = 50.0
    assert b3.acquire(timeout=0) is False
    t[0] = 100.05
    assert b3.acquire(timeout=0) is False
    t[0] = 100.1
    assert b3.acquire(timeout=0) is True

    # bad configurations are refused
    for kwargs in ({"rate": 0}, {"rate": float("nan")}, {"rate": -1}, {"rate": 1, "burst": 0},
                   {"rate": 1, "window_cap": 0}, {"rate": True}):
        with pytest.raises(ValueError):
            TokenBucket("bad", **kwargs)

    # threads sharing one bucket on the real clock never beat its rate
    rb = TokenBucket("shared", rate=100, burst=2)
    start = time.monotonic()
    workers = [threading.Thread(target=lambda: [rb.acquire() for _ in range(5)]) for _ in range(4)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(5)
    assert time.monotonic() - start >= (20 - 2) / 100 - 1e-3


def test_orders_bucket_sebi_window():
    clock = FakeClock()
    buckets = fake_buckets(clock)
    grants = []
    for _ in range(40):
        assert buckets.orders.acquire()
        grants.append(clock.mono())
    assert grants[0] == grants[2] < grants[3]                         # burst of 3
    worst = max(sum(1 for g in grants if t <= g <= t + 1.0) for t in grants)
    assert worst <= 8                                                 # SEBI limit is 10/s; never above 8
    # quote (1/s) and historical (3/s) are paced too
    q0 = clock.mono()
    for _ in range(3):
        buckets.quote.acquire()
    assert clock.mono() - q0 == pytest.approx(2.0, abs=1e-3)


# ---------------------------------------------------------------------------------------------------------------
# Monkey-patch assert, SDK compatibility, structure
# ---------------------------------------------------------------------------------------------------------------
def test_monkeypatch_detected(monkeypatch):
    connect = pytest.importorskip("kiteconnect.connect")
    kc = connect.KiteConnect
    assert_kite_unpatched()                                           # the installed library is clean
    assert_kite_unpatched(kc)

    def place_order(self, **kwargs):                                  # v1-style replacement
        return "1"

    monkeypatch.setattr(kc, "place_order", place_order)
    with pytest.raises(KitePatched, match="place_order"):
        assert_kite_unpatched()
    monkeypatch.undo()
    assert_kite_unpatched()

    original = kc.__dict__["place_order"]

    @functools.wraps(original)                                        # copies __module__ and __qualname__
    def wrapped(self, **kwargs):
        return original(self, **kwargs)

    monkeypatch.setattr(kc, "place_order", wrapped)
    with pytest.raises(KitePatched, match="wraps"):
        assert_kite_unpatched()
    monkeypatch.undo()

    def disguised(self, **kwargs):
        return "1"

    disguised.__module__, disguised.__qualname__ = "kiteconnect.connect", "KiteConnect.cancel_order"
    disguised.__name__ = "cancel_order"
    monkeypatch.setattr(kc, "cancel_order", disguised)
    with pytest.raises(KitePatched, match="cancel_order"):
        assert_kite_unpatched()
    monkeypatch.undo()

    # code compiled to look like it came from kiteconnect/connect.py still fails the spec's module/qualname check
    namespace = {"__name__": "titan_v1_patch"}
    exec(compile("def place_order(self, **kwargs):\n    return '1'\n",
                 os.path.join("site-packages", "kiteconnect", "connect.py"), "exec"), namespace)
    monkeypatch.setattr(kc, "place_order", namespace["place_order"])
    with pytest.raises(KitePatched, match="titan_v1_patch"):
        assert_kite_unpatched()
    monkeypatch.undo()

    monkeypatch.setattr(kc, "_post", lambda self, *a, **k: {"order_id": "1"})
    with pytest.raises(KitePatched, match="_post"):
        assert_kite_unpatched()
    monkeypatch.undo()

    shadowing = types.SimpleNamespace(place_order=lambda **kw: "1")
    with pytest.raises(KitePatched, match="shadow"):
        assert_kite_unpatched(instance=shadowing)
    assert_kite_unpatched()


def test_calls_bind_to_installed_sdk():
    """Every call the adapter makes binds to kiteconnect 5.0.1's real signatures."""
    connect = pytest.importorskip("kiteconnect.connect")
    clock = FakeClock()
    kite = StubKite(order_history=[kite_order()], order_trades=[kite_trade()], orders=[kite_order()],
                    trades=[kite_trade()], quote={}, ltp={},
                    get_virtual_contract_note=[{"tradingsymbol": TS, "charges": {"total": 20.0}}])
    kb = broker(kite, clock, entries_allowed=True)
    kd = KiteData(kite, clock=clock, buckets=fake_buckets(clock))
    r = order_req(Role.ENTRY)
    kb.place(r)
    kb.cancel(Account.LIVE, r.tag, "261012000000001")
    kb.order_history(Account.LIVE, "261012000000001")
    kb.order_trades(Account.LIVE, "261012000000001")
    kd.orders_today(Account.LIVE)
    kd.trades_today(Account.LIVE)
    kd.positions(Account.LIVE)
    kd.margins(Account.LIVE)
    kd.quote([SYM])
    kd.ltp([SYM])
    kd.historical(SYM, 1234, clock.now_wall(), clock.now_wall())
    kd.instruments("NFO")
    kd.profile()
    kd.contract_note(Account.LIVE, [ContractNoteOrder("1", "NFO", TS, Side.BUY, 65, 12.0)])
    kd.basket_margins([{"exchange": "NFO", "tradingsymbol": TS}])
    seen = set()
    for name, args, kwargs in kite.calls:
        inspect.signature(getattr(connect.KiteConnect, name)).bind(object(), *args, **kwargs)
        seen.add(name)
    assert seen == {"place_order", "cancel_order", "order_history", "order_trades", "orders", "trades", "positions",
                    "margins", "quote", "ltp", "historical_data", "instruments", "profile",
                    "get_virtual_contract_note", "basket_order_margins"}


def test_structure_kitedata_has_no_order_methods_and_imports_without_twisted():
    for name in ("place", "cancel", "order_history", "order_trades", "place_order", "cancel_order", "modify_order"):
        assert not hasattr(KiteData, name), name
    kb, kd = broker(), data()
    assert isinstance(kb, BrokerPort) and isinstance(kd, AccountReads) and isinstance(kd, DataPort)
    assert not isinstance(kd, BrokerPort)
    assert ORDER_LANE_THREAD == "lane-live-orders"
    # the adapter modules import without twisted or kiteconnect (Windows developer machines, the arch rule)
    code = ("import sys\n"
            "for m in ('twisted', 'autobahn', 'kiteconnect', 'fcntl'):\n"
            "    sys.modules[m] = None\n"
            "import titan2.broker.port, titan2.broker.lanes, titan2.broker.kite\n")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    res = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
