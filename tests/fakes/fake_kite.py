"""FakeKite: a KiteConnect 5.0.1 stand-in that simulates the LIVE account's exchange (spec §16.1).

It implements every KiteConnect method broker/kite.py uses (place_order, cancel_order, order_history, orders,
trades, order_trades, positions, margins, quote, ltp, instruments, historical_data, profile,
get_virtual_contract_note, basket_order_margins) with the SDK's signatures and return shapes (dates as the SDK
parses them), and emits KiteTicker-style order updates to the bound Feed. GTT and modify_order exist only to record
that they were (never) called.

Fills mirror the paper exchange so the dual run can compare P&L exactly: at place time + latency an IOC order walks
the ScriptedMarket's TRUE book with titan2.broker.paper.fill_from_depth at PAPER_PARTICIPATION, and the latency is
the paper model's lognormal draw from the PAPER account's seed (latency_seed(session, PAPER)), so the n-th LIVE order
of a session waits as long as the n-th PAPER order. Self-test orders draw from a separate stream. The walk's fill draw
(paper model 2) is the PAPER twin's: fill_draw(session, PAPER, the order's tag with the PAPER account letter), so a
LIVE order fills the leftover fraction of a lot exactly when its PAPER run's order does. The LPP band and funds checks
mirror the paper exchange's too, but as Kite does them: the order is accepted, then REJECTED by RMS.

The broker runs while the bot is down (the runner keeps stepping it), keeps its books across bot restarts, shows
only today's orders and trades, and squares off MIS positions at BROKER_SQUAREOFF_AT (like the paper exchange).
"""

from __future__ import annotations

import heapq
import itertools
import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from typing import Any, Callable, Iterable

import requests

from titan2 import charges
from titan2.broker.paper import fill_draw, fill_from_depth, latency_seed
from titan2.clock import FakeClock, ist_datetime, session_of, to_ist
from titan2.instruments import InstrumentStore
from titan2.model import Account, Level, Role, Side, ceil_tick, floor_tick, is_tick_multiple, make_tag, parse_tag
from titan2.settings import PAPER_LATENCY_CLIP_MS, PAPER_SQOFF_BUY_FACTOR, PAPER_SQOFF_SELL_FACTOR, Settings

from fakes import kite_errors as kex
from fakes import universe as uni
from fakes.faults import BAND_MSG, RACE_MAX_HOLD_S, FaultInjector
from fakes.market import ScriptedMarket

USER_ID = "AB1234"
ADMIN = "ADMINSQF"
GTT_METHODS = ("place_gtt", "modify_gtt", "delete_gtt", "get_gtts", "get_gtt")
ORDER_METHODS = ("place_order", "cancel_order", "modify_order")
TERMINAL = ("COMPLETE", "CANCELLED", "REJECTED")


def _naive_ist(dt: datetime) -> datetime:
    return to_ist(dt).replace(tzinfo=None, microsecond=0)


def _text(dt: datetime | None) -> str | None:
    return None if dt is None else to_ist(dt).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class KOrder:
    order_id: str
    session: date
    tag: str | None
    exchange: str
    tradingsymbol: str
    token: int
    side: str
    qty: int
    price: float | None
    order_type: str
    validity: str
    product: str
    variety: str
    placed_by: str
    lot: int
    tick: float
    order_wall: datetime
    status: str = "OPEN"
    filled: int = 0
    avg: float = 0.0
    status_message: str | None = None
    exch_wall: datetime | None = None
    due_mono: float | None = None
    hold: dict[str, Any] | None = None
    history: list[dict] = field(default_factory=list)

    @property
    def symbol(self) -> str:
        return f"{self.exchange}:{self.tradingsymbol}"

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    def row(self, *, parsed: bool = True) -> dict:
        """One orders() / order_history() row (parsed=True: the SDK's datetimes) or a WS order update (text)."""
        stamp = (lambda d: None if d is None else _naive_ist(d)) if parsed else _text
        return {
            "placed_by": self.placed_by, "order_id": self.order_id, "exchange_order_id": f"X{self.order_id}",
            "parent_order_id": None, "status": self.status, "status_message": self.status_message,
            "status_message_raw": self.status_message, "order_timestamp": stamp(self.order_wall),
            "exchange_update_timestamp": _text(self.exch_wall), "exchange_timestamp": stamp(self.exch_wall),
            "variety": self.variety, "exchange": self.exchange, "tradingsymbol": self.tradingsymbol,
            "instrument_token": self.token, "order_type": self.order_type, "transaction_type": self.side,
            "validity": self.validity, "product": self.product, "quantity": self.qty, "disclosed_quantity": 0,
            "price": self.price or 0.0, "trigger_price": 0.0, "average_price": round(self.avg, 4) if self.filled else 0.0,
            "filled_quantity": self.filled, "pending_quantity": 0 if self.terminal else self.qty - self.filled,
            "cancelled_quantity": self.qty - self.filled if self.status == "CANCELLED" else 0,
            "market_protection": 0, "meta": {}, "tag": self.tag, "guid": f"g{self.order_id}"}


@dataclass
class KTrade:
    trade_id: str
    order_id: str
    session: date
    exchange: str
    tradingsymbol: str
    token: int
    side: str
    qty: int
    px: float
    wall: datetime

    @property
    def symbol(self) -> str:
        return f"{self.exchange}:{self.tradingsymbol}"

    def row(self, px_delta: float = 0.0) -> dict:
        return {"trade_id": self.trade_id, "order_id": self.order_id, "exchange_order_id": f"X{self.order_id}",
                "exchange": self.exchange, "tradingsymbol": self.tradingsymbol, "instrument_token": self.token,
                "product": "MIS", "average_price": round(self.px + px_delta, 2), "quantity": self.qty,
                "transaction_type": self.side, "fill_timestamp": _naive_ist(self.wall),
                "order_timestamp": _naive_ist(self.wall), "exchange_timestamp": _naive_ist(self.wall)}


class FakeKite:
    def __init__(self, clock: FakeClock, market: ScriptedMarket, store: InstrumentStore, settings: Settings,
                 faults: FaultInjector, *, capital: float = 500_000.0, squareoff_at: dtime | None = None) -> None:
        self.clock = clock
        self.market = market
        self.store = store
        self.settings = settings
        self.faults = faults
        self.capital = float(capital)
        self.squareoff_at = settings.BROKER_SQUAREOFF_AT if squareoff_at is None else squareoff_at
        self.calls: list[tuple[datetime, str, dict]] = []
        self.violations: list[str] = []
        self.order_monos: list[float] = []          # place/cancel call times (SEBI: <= 10 order messages/s)
        self.orders_by_id: dict[str, KOrder] = {}
        self.trades_all: list[KTrade] = []
        self.on_order_update: Callable[[dict], None] | None = None
        self.hist_candles: dict[int, list[dict]] = {}
        self._outbox: list[tuple[str, dict, bool]] = []      # (order_id, ws row, terminal)
        self._held_back: dict[str, list[dict]] = {}          # out_of_order: non-terminal updates delivered late
        self._due: list[tuple[float, int, str]] = []
        self._seq = itertools.count()
        self._ids = itertools.count(1)
        self._tids = itertools.count(1)
        self._rngs: dict[tuple[date, str], random.Random] = {}
        self._sqoff_done: set[date] = set()
        self._sqoff_forced = False
        self._contracts = {c.symbol: c for c in uni.CONTRACTS}

    # -- plumbing ------------------------------------------------------------------------------------------------
    def _call(self, name: str, **kw: Any) -> None:
        self.calls.append((self.clock.now_wall(), name, kw))
        if self.faults.token_dead():
            raise kex.TokenException("Incorrect `api_key` or `access_token`.")

    def called(self, name: str) -> int:
        return sum(1 for _, n, _ in self.calls if n == name)

    def _today(self) -> date:
        return session_of(self.clock.now_wall())

    def _contract(self, symbol: str):
        """The contract row (the universe holds every contract, expired or not)."""
        return self._contracts.get(symbol)

    def _latency_ms(self, session: date, selftest: bool) -> float:
        key = (session, "SELFTEST" if selftest else "PAPER")
        rng = self._rngs.get(key)
        if rng is None:
            seed = latency_seed(session, Account.LIVE if selftest else Account.PAPER)
            rng = self._rngs[key] = random.Random(seed)
        s = self.settings
        ms = rng.lognormvariate(math.log(s.PAPER_LATENCY_MEDIAN_MS), s.PAPER_LATENCY_SIGMA)
        lo, hi = PAPER_LATENCY_CLIP_MS
        return min(max(ms, lo), hi)

    def _fill_draw(self, o: KOrder) -> float:
        """The fill draw of the order's PAPER twin (the same tag with the PAPER account letter); a self-test or
        untagged order draws on its own LIVE seed."""
        p = parse_tag(o.tag)
        if p is not None and p.role is not Role.SELFTEST:
            return fill_draw(p.session, Account.PAPER, make_tag(p.role, Account.PAPER, p.session, p.seq_no, p.attempt))
        return fill_draw(o.session, Account.LIVE, o.tag or o.order_id)

    def _emit(self, o: KOrder) -> None:
        o.history.append(o.row(parsed=True))
        self._outbox.append((o.order_id, o.row(parsed=False), o.terminal))

    def _set_terminal(self, o: KOrder, status: str, msg: str | None) -> None:
        o.status = status
        o.status_message = msg
        o.exch_wall = self.clock.now_wall()
        o.due_mono = None
        o.hold = None
        self._emit(o)

    # -- orders ----------------------------------------------------------------------------------------------------
    def place_order(self, variety, exchange, tradingsymbol, transaction_type, quantity, product, order_type,
                    price=None, validity=None, validity_ttl=None, disclosed_quantity=None, trigger_price=None,
                    iceberg_legs=None, iceberg_quantity=None, auction_number=None, tag=None):
        kw = {k: v for k, v in dict(variety=variety, exchange=exchange, tradingsymbol=tradingsymbol,
                                     transaction_type=transaction_type, quantity=quantity, product=product,
                                     order_type=order_type, price=price, validity=validity, validity_ttl=validity_ttl,
                                     disclosed_quantity=disclosed_quantity, trigger_price=trigger_price,
                                     iceberg_legs=iceberg_legs, iceberg_quantity=iceberg_quantity,
                                     auction_number=auction_number, tag=tag).items() if v is not None}
        self.order_monos.append(self.clock.mono())
        self._call("place_order", **kw)
        rule = self._payload_problem(kw)
        if rule is not None:
            self.violations.append(f"{tag}: {rule}")
            raise kex.InputException(rule)
        symbol = f"{exchange}:{tradingsymbol}"
        acct = Account.LIVE
        f = self.faults.find(("reject", "http_429", "timeout_before_accept"), acct, symbol=symbol, tag=tag)
        if f is not None:
            if f.mode == "reject":
                raise kex.OrderException(f.params.get("msg") or "Order rejected by OMS")
            if f.mode == "http_429":
                raise kex.NetworkException("Too many requests", 429)
            raise requests.exceptions.ReadTimeout("HTTPSConnectionPool(host='api.kite.trade'): Read timed out.")
        inst = self._contract(symbol)
        if inst is None:
            raise kex.InputException("Invalid `tradingsymbol`.")
        if quantity % inst.lot_size or not is_tick_multiple(float(price), inst.tick):
            raise kex.InputException(f"Quantity {quantity} or price {price} is not valid for {tradingsymbol}")
        now = self.clock.now_wall()
        session = session_of(now)
        o = KOrder(order_id=f"{session:%y%m%d}{next(self._ids):09d}", session=session, tag=tag, exchange=exchange,
                   tradingsymbol=tradingsymbol, token=inst.token, side=transaction_type, qty=int(quantity),
                   price=float(price), order_type=order_type, validity=validity, product=product, variety=variety,
                   placed_by=USER_ID, lot=inst.lot_size, tick=inst.tick, order_wall=now)
        self.orders_by_id[o.order_id] = o
        reason = self._rms_problem(o)
        band = self.faults.find("band_reject", acct, symbol=symbol, tag=tag)
        if band is not None:
            reason = band.params.get("msg") or BAND_MSG
        if reason is not None:
            self._emit(o)                                   # OPEN, then RMS rejects it
            self._set_terminal(o, "REJECTED", reason)
        else:
            selftest = (parse_tag(tag) is not None and parse_tag(tag).role.value == "SELFTEST")
            latency = self._latency_ms(session, selftest)
            o.due_mono = self.clock.mono() + latency / 1000.0
            hold = self.faults.find(("order_stuck_open", "cancel_race_already_complete"), acct, symbol=symbol,
                                    tag=tag)
            if hold is not None:
                race = hold.mode == "cancel_race_already_complete"
                o.hold = {"race": race, "cancel_works": bool(hold.params.get("cancel_works", True)),
                          "fill_on_release": race or bool(hold.params.get("fill_on_release", False)),
                          "until": self.clock.mono() + float(hold.params.get("hold_s", RACE_MAX_HOLD_S if race
                                                                              else 30.0))}
            heapq.heappush(self._due, (o.due_mono, next(self._seq), o.order_id))
            self._emit(o)
        if self.faults.find("ws_update_before_place_returns", acct, symbol=symbol, tag=tag) is not None:
            self.flush()                                    # the update beats the place answer
        if self.faults.find("timeout_after_accept", acct, symbol=symbol, tag=tag) is not None:
            raise kex.NetworkException("Read timed out after the order was accepted")
        return o.order_id

    def _payload_problem(self, kw: dict) -> str | None:
        """The fixed order rules (§5.1): anything else is a bot bug, recorded as a violation and refused."""
        if kw.get("order_type") != "LIMIT":
            return f"order_type {kw.get('order_type')!r} is not LIMIT"
        if kw.get("validity") != "IOC":
            return f"validity {kw.get('validity')!r} is not IOC"
        if kw.get("product") != "MIS":
            return f"product {kw.get('product')!r} is not MIS"
        if kw.get("variety") != "regular":
            return f"variety {kw.get('variety')!r} is not regular"
        if kw.get("exchange") != "NFO":
            return f"exchange {kw.get('exchange')!r} is not NFO"
        if kw.get("price") is None or "trigger_price" in kw:
            return "a LIMIT order needs a price and no trigger price"
        if not isinstance(kw.get("quantity"), int) or kw["quantity"] <= 0:
            return f"bad quantity {kw.get('quantity')!r}"
        return None

    def _rms_problem(self, o: KOrder) -> str | None:
        """What Zerodha's RMS / the exchange would reject after accepting the request (mirrors the paper checks)."""
        bounds = self.market.store.calendar.session_bounds(o.session) if hasattr(self.market.store, "calendar") \
            else None
        if bounds is None or not bounds[0] <= o.order_wall < bounds[1]:
            return "Markets are closed right now. Use GTT for placing long standing orders instead."
        ltp = self.market.ltp(o.symbol)
        if ltp and abs(o.price / ltp - 1.0) > self.settings.PAPER_PRICE_BAND_PCT / 100.0 + 1e-12:
            return BAND_MSG
        if o.side == "BUY":
            need = o.qty * o.price + charges.order_charges(
                charges.OrderFills(Side.BUY, o.qty, o.qty * o.price), charges.rates_for(o.session)).total
            if need > self.available() + 1e-9:
                return f"RMS:Margin Exceeds, Required:{need:.2f}, Available:{self.available():.2f}"
        return None

    def cancel_order(self, variety, order_id, parent_order_id=None):
        self.order_monos.append(self.clock.mono())
        self._call("cancel_order", variety=variety, order_id=order_id)
        o = self.orders_by_id.get(str(order_id))
        if o is None or o.session != self._today():
            raise kex.InputException("Order not found.")
        if o.hold is not None and o.hold.get("race"):
            self._fill(o)                                   # the fill wins the race
        if o.terminal:
            raise kex.InputException(f"Order cannot be cancelled as it is already {o.status.lower()}.")
        if o.hold is not None and not o.hold.get("cancel_works", True):
            return o.order_id                               # acknowledged, but the order stays open
        self._set_terminal(o, "CANCELLED", "Cancelled by user")
        return o.order_id

    def modify_order(self, variety, order_id, parent_order_id=None, quantity=None, price=None, order_type=None,
                     trigger_price=None, validity=None, disclosed_quantity=None):
        self._call("modify_order", order_id=order_id)
        self.violations.append(f"modify_order({order_id}) called")
        raise kex.InputException("modify_order is never used by titan2")

    def order_history(self, order_id):
        self._call("order_history", order_id=order_id)
        o = self.orders_by_id.get(str(order_id))
        if o is None or o.session != self._today():
            raise kex.InputException("Couldn't find that `order_id`.")
        return [dict(h) for h in o.history]

    def orders(self):
        self._call("orders")
        if self.faults.find("orders_error", Account.LIVE, consume=True, detail="orders") is not None:
            raise kex.NetworkException("Gateway timed out")
        today = self._today()
        return [o.row(parsed=True) for o in self.orders_by_id.values() if o.session == today]

    def trades(self):
        self._call("trades")
        drift = self.faults.find("trades_drift", Account.LIVE, consume=True, detail="trades")
        today = self._today()
        return [t.row(drift.params["px_delta"] if drift is not None and t.side == drift.params["side"] else 0.0)
                for t in self.trades_all if t.session == today]

    def order_trades(self, order_id):
        self._call("order_trades", order_id=order_id)
        o = self.orders_by_id.get(str(order_id))
        if o is None or o.session != self._today():
            raise kex.InputException("Couldn't find that `order_id`.")
        return [t.row() for t in self.trades_all if t.order_id == o.order_id]

    def positions(self):
        self._call("positions")
        acct = Account.LIVE
        if self.faults.find("positions_error", acct, consume=True, detail="positions") is not None:
            raise kex.DataException("Unknown content type ()")
        lag = self.faults.find("positions_lag", acct, consume=True, detail="positions")
        cutoff = None
        if lag is not None:
            cutoff = self.clock.now_wall().timestamp() - lag.params["lag_s"]
        rows = self._position_rows(cutoff)
        contra = self.faults.find("positions_contradict", acct, consume=True, detail="positions")
        if contra is not None:
            for r in rows:
                if r["quantity"] != 0:
                    d = contra.params.get("delta")
                    r["quantity"] = 0 if d is None else r["quantity"] + int(d)
        return {"net": rows, "day": [dict(r) for r in rows]}

    def _position_rows(self, cutoff: float | None = None) -> list[dict]:
        today = self._today()
        agg: dict[str, dict] = {}
        for t in self.trades_all:
            if cutoff is not None and t.wall.timestamp() > cutoff:
                continue                                    # positions_lag: not visible yet
            a = agg.setdefault(t.symbol, {"t": t, "net": 0, "today": False, "BUY": [0, 0.0], "SELL": [0, 0.0]})
            a["net"] += t.qty if t.side == "BUY" else -t.qty
            if t.session == today:
                a["today"] = True
                a[t.side][0] += t.qty
                a[t.side][1] += t.qty * t.px
        rows = []
        for sym in sorted(agg):
            a = agg[sym]
            if a["net"] == 0 and not a["today"]:
                continue
            t = a["t"]
            bq, bv = a["BUY"]
            sq, sv = a["SELL"]
            rows.append({"tradingsymbol": t.tradingsymbol, "exchange": t.exchange, "instrument_token": t.token,
                         "product": "MIS", "quantity": a["net"], "overnight_quantity": 0, "multiplier": 1,
                         "average_price": round(bv / bq, 4) if bq else 0.0, "close_price": 0.0,
                         "last_price": self.market.ltp(sym) or 0.0, "value": round(sv - bv, 2), "pnl": 0.0,
                         "m2m": 0.0, "unrealised": 0.0, "realised": 0.0, "buy_quantity": bq,
                         "buy_price": round(bv / bq, 4) if bq else 0.0, "buy_value": round(bv, 2),
                         "sell_quantity": sq, "sell_price": round(sv / sq, 4) if sq else 0.0,
                         "sell_value": round(sv, 2), "day_buy_quantity": bq, "day_sell_quantity": sq,
                         "day_buy_value": round(bv, 2), "day_sell_value": round(sv, 2)})
        return rows

    def net(self, symbol: str) -> int:
        """The true signed net position on symbol (all sessions), whatever the API shows."""
        return sum(t.qty if t.side == "BUY" else -t.qty for t in self.trades_all if t.symbol == symbol)

    def nets(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for t in self.trades_all:
            out[t.symbol] = out.get(t.symbol, 0) + (t.qty if t.side == "BUY" else -t.qty)
        return out

    def available(self) -> float:
        """capital + today's realized cash flow - charges - funds blocked by OPEN buy orders (the paper formula)."""
        today = self._today()
        rates = charges.rates_for(today)
        executed = []
        for o in self.orders_by_id.values():
            if o.session == today and o.filled > 0:
                kind = charges.AUTO_SQUAREOFF if o.placed_by == ADMIN else None
                executed.append(charges.OrderFills(Side(o.side), o.filled, o.filled * o.avg, kind))
        pnl = charges.net_pnl(executed, rates) if executed else None
        blocked = sum(o.qty * (o.price or 0.0) for o in self.orders_by_id.values()
                      if not o.terminal and o.side == "BUY" and o.session == today)
        return self.capital + (pnl.net if pnl is not None else 0.0) - blocked

    def margins(self, segment=None):
        self._call("margins")
        avail = round(self.available(), 2)
        eq = {"enabled": True, "net": avail,
              "available": {"adhoc_margin": 0.0, "cash": self.capital, "opening_balance": self.capital,
                            "live_balance": avail, "collateral": 0.0, "intraday_payin": 0.0},
              "utilised": {"debits": 0.0, "exposure": 0.0, "m2m_realised": 0.0, "m2m_unrealised": 0.0}}
        doc = {"equity": eq, "commodity": {"enabled": False, "net": 0.0, "available": {"live_balance": 0.0},
                                           "utilised": {}}}
        return doc[segment] if segment else doc

    # -- market data ---------------------------------------------------------------------------------------------
    @staticmethod
    def _instruments_arg(instruments: tuple) -> list[str]:
        if len(instruments) == 1 and isinstance(instruments[0], (list, tuple)):
            return list(instruments[0])
        return list(instruments)

    def quote(self, *instruments):
        syms = self._instruments_arg(instruments)
        self._call("quote", instruments=tuple(syms))
        if self.market.rest_fails(syms):
            raise kex.NetworkException("Gateway timed out (quote)")
        out = {}
        for s in syms:
            row = self.market.rest_row(s)
            if row is not None:
                out[s] = row
        return out

    def ltp(self, *instruments):
        syms = self._instruments_arg(instruments)
        self._call("ltp", instruments=tuple(syms))
        if self.market.rest_fails(syms):
            raise kex.NetworkException("Gateway timed out (ltp)")
        out = {}
        for s in syms:
            row = self.market.rest_row(s)
            if row is not None:
                out[s] = {"instrument_token": row["instrument_token"], "last_price": row["last_price"]}
        return out

    def instruments(self, exchange=None):
        self._call("instruments", exchange=exchange)
        if exchange == "NSE":
            return [{"instrument_token": s.token, "exchange_token": str(s.token // 256),
                     "tradingsymbol": s.symbol.split(":", 1)[1], "name": s.name, "last_price": 0.0, "expiry": "",
                     "strike": 0.0, "tick_size": 0.05, "lot_size": 1, "instrument_type": "EQ",
                     "segment": "INDICES" if s.kind == "INDEX" else "NSE", "exchange": "NSE"} for s in uni.SPECS]
        return [{"instrument_token": c.token, "exchange_token": str(c.token // 256), "tradingsymbol": c.tradingsymbol,
                 "name": c.name, "last_price": 0.0, "expiry": c.expiry, "strike": c.strike, "tick_size": c.tick,
                 "lot_size": c.lot_size, "instrument_type": c.opt_type, "segment": "NFO-OPT", "exchange": "NFO"}
                for c in uni.CONTRACTS]

    def historical_data(self, instrument_token, from_date, to_date, interval, continuous=False, oi=False):
        self._call("historical_data", instrument_token=instrument_token, from_date=from_date, to_date=to_date,
                   interval=interval)
        return [dict(c) for c in self.hist_candles.get(int(instrument_token), [])]

    def profile(self):
        self._call("profile")
        return {"user_id": USER_ID, "user_name": "Harness", "user_shortname": "Harness", "email": "",
                "user_type": "individual", "broker": "ZERODHA", "exchanges": ["NSE", "NFO"],
                "products": ["MIS", "CNC", "NRML"], "order_types": ["LIMIT", "SL"], "meta": {"demat_consent": ""}}

    def get_virtual_contract_note(self, params):
        self._call("get_virtual_contract_note", n=len(params))
        out = []
        for p in params:
            o = self.orders_by_id.get(str(p.get("order_id")))
            fills = [t for t in self.trades_all if o is not None and t.order_id == o.order_id]
            if o is None or not fills:
                raise kex.InputException(f"order {p.get('order_id')} not found")
            kind = charges.AUTO_SQUAREOFF if o.placed_by == ADMIN else None
            of = charges.OrderFills(Side(o.side), sum(t.qty for t in fills), sum(t.qty * t.px for t in fills), kind)
            c = charges.order_charges(of, charges.rates_for(o.session))
            out.append({"transaction_type": o.side, "tradingsymbol": o.tradingsymbol, "exchange": o.exchange,
                        "variety": o.variety, "product": o.product, "order_type": o.order_type,
                        "quantity": of.filled_qty, "price": round(of.value / of.filled_qty, 4),
                        "charges": {"transaction_tax": c.stt, "transaction_tax_type": "stt",
                                    "exchange_turnover_charge": c.txn, "sebi_turnover_charge": c.sebi,
                                    "brokerage": c.brokerage, "stamp_duty": c.stamp,
                                    "gst": {"igst": c.gst, "cgst": 0.0, "sgst": 0.0, "total": c.gst},
                                    "total": c.total}})
        return out

    def basket_order_margins(self, params, consider_positions=True, mode=None):
        self._call("basket_order_margins", n=len(params))
        total = sum(float(p.get("quantity", 0)) * float(p.get("price") or 0.0) for p in params)
        return {"initial": {"total": total}, "final": {"total": total}, "orders": []}

    # GTT: titan2 never uses them (k05). They only record the call.
    def place_gtt(self, *args, **kwargs):
        return self._never("place_gtt")

    def modify_gtt(self, *args, **kwargs):
        return self._never("modify_gtt")

    def delete_gtt(self, *args, **kwargs):
        return self._never("delete_gtt")

    def get_gtts(self, *args, **kwargs):
        return self._never("get_gtts")

    def get_gtt(self, *args, **kwargs):
        return self._never("get_gtt")

    def _never(self, name: str):
        self.calls.append((self.clock.now_wall(), name, {}))
        self.violations.append(f"{name} called")
        raise kex.GeneralException(f"{name} is never used by titan2")

    # -- the exchange --------------------------------------------------------------------------------------------
    def _fill(self, o: KOrder) -> None:
        """Walk the true book at fill time (the paper rule), then COMPLETE or CANCELLED with the partial."""
        bids, asks = self.market.true_levels(o.symbol)
        s = self.settings
        rows = fill_from_depth(asks if o.side == "BUY" else bids, Side(o.side), o.qty, o.price, o.lot,
                               s.PAPER_PARTICIPATION, s.PAPER_ADVERSE_TICKS, o.tick, draw=self._fill_draw(o))
        self._book_fills(o, rows)
        filled = sum(q for _, q in rows)
        msg = None if filled == o.qty else ("IOC order cancelled: unfilled" if filled == 0
                                            else f"IOC order partially filled {filled}/{o.qty}")
        self._set_terminal(o, "COMPLETE" if filled == o.qty else "CANCELLED", msg)

    def _book_fills(self, o: KOrder, rows: Iterable[tuple[float, int]]) -> None:
        now = self.clock.now_wall()
        value = o.avg * o.filled
        for px, q in rows:
            self.trades_all.append(KTrade(f"{next(self._tids):08d}", o.order_id, o.session, o.exchange,
                                          o.tradingsymbol, o.token, o.side, int(q), float(px), now))
            value += px * q
            o.filled += int(q)
        if o.filled:
            o.avg = value / o.filled

    def _sqoff_rows(self, symbol: str, side: str, qty: int, tick: float) -> list[tuple[float, int]]:
        """The paper exchange's square-off fill rule: every shown level at participation 1.0, the rest at the deepest
        level x 0.95 (sell) / x 1.05 (buy); no book side: Rs 0.05 / 1.05 x the highest price known."""
        bids, asks = self.market.true_levels(symbol)
        book = list(bids if side == "SELL" else asks)
        # lot 1 at participation 1.0: every share is whole units, so the draw never matters
        rows = fill_from_depth(book, Side(side), qty, None, 1, 1.0, self.settings.PAPER_ADVERSE_TICKS, tick, draw=0.0)
        left = qty - sum(q for _, q in rows)
        if left > 0:
            if book and side == "SELL":
                px = max(tick, floor_tick(min(lv.price for lv in book) * PAPER_SQOFF_SELL_FACTOR, tick))
            elif book:
                px = ceil_tick(max(lv.price for lv in book) * PAPER_SQOFF_BUY_FACTOR, tick)
            else:
                known = [t.px for t in self.trades_all if t.symbol == symbol] + [lv.price for lv in bids + asks]
                px = 0.05 if side == "SELL" else ceil_tick(max(known, default=0.05) * PAPER_SQOFF_BUY_FACTOR, tick)
            rows.append((round(px, 2), left))
        return rows

    def _external(self, symbol: str, side: str, qty: int, rows: list[tuple[float, int]], placed_by: str,
                  order_type: str = "LIMIT", validity: str = "DAY", msg: str | None = None) -> KOrder:
        inst = self._contract(symbol)
        now = self.clock.now_wall()
        session = session_of(now)
        exchange, ts = symbol.split(":", 1)
        o = KOrder(order_id=f"{session:%y%m%d}{next(self._ids):09d}", session=session, tag=None, exchange=exchange,
                   tradingsymbol=ts, token=inst.token, side=side, qty=int(qty), price=None, order_type=order_type,
                   validity=validity, product="MIS", variety="regular", placed_by=placed_by, lot=inst.lot_size,
                   tick=inst.tick, order_wall=now)
        self.orders_by_id[o.order_id] = o
        self._emit(o)
        self._book_fills(o, rows)
        self._set_terminal(o, "COMPLETE", msg)
        return o

    def _no_bot_order_working(self, f) -> bool:
        """external_close waits while a bot order on its symbol is working (see FaultInjector.external_close)."""
        if f.mode != "external_close":
            return True
        return not any(not o.terminal and o.tag is not None and o.symbol == f.symbol
                       for o in self.orders_by_id.values())

    def _events(self, now: datetime) -> None:
        for f in self.faults.due_events(Account.LIVE, now, self._no_bot_order_working):
            sym = f.symbol
            if f.mode == "external_close":
                held = self.net(sym)
                qty = int(f.params.get("qty") or held)
                if qty <= 0:
                    continue
                bids, _ = self.market.true_levels(sym)
                px = f.params.get("px") or (bids[0].price if bids else self.market.ltp(sym))
                self._external(sym, "SELL", qty, [(float(px), qty)], USER_ID)
            elif f.mode == "external_buy":
                _, asks = self.market.true_levels(sym)
                px = f.params.get("px") or (asks[0].price if asks else self.market.ltp(sym))
                self._external(sym, "BUY", int(f.params["qty"]), [(float(px), int(f.params["qty"]))], USER_ID)
            elif f.mode == "broker_squareoff":
                self._sqoff_forced = True
        if self._sqoff_forced and self._squareoff(force=True):
            self._sqoff_forced = False

    def _squareoff(self, force: bool = False) -> bool:
        """Zerodha's MIS auto square-off: once per session at squareoff_at (or now when forced), after the account's
        open orders resolve. Returns True when done."""
        today = self._today()
        if not force and today in self._sqoff_done:
            return True
        if any(not o.terminal and o.session == today for o in self.orders_by_id.values()):
            return False
        for symbol, net in sorted(self.nets().items()):
            if net == 0:
                continue
            side = "SELL" if net > 0 else "BUY"
            inst = self._contract(symbol)
            rows = self._sqoff_rows(symbol, side, abs(net), inst.tick)
            self._external(symbol, side, abs(net), rows, ADMIN, order_type="MARKET",
                           msg="Auto square-off: MIS position squared off by the RMS")
        self._sqoff_done.add(today)
        return True

    def step(self, now_mono: float | None = None) -> None:
        """Run the exchange up to now: account events, fills at their due time, held orders, the square-off; then
        send the order updates (subject to the WS faults)."""
        now = self.clock.now_wall()
        mono = self.clock.mono() if now_mono is None else now_mono
        self._events(now)
        while self._due and self._due[0][0] <= mono + 1e-9:
            _, _, oid = heapq.heappop(self._due)
            o = self.orders_by_id[oid]
            if o.terminal:
                continue
            if o.hold is not None:
                continue                                    # held: released below
            self._fill(o)
        for o in list(self.orders_by_id.values()):
            h = o.hold
            if o.terminal or h is None or h.get("until") is None or mono < h["until"]:
                continue
            o.hold = None
            if h.get("fill_on_release"):
                self._fill(o)
            else:
                self._set_terminal(o, "CANCELLED", "IOC order cancelled: unfilled")
        sq = ist_datetime(session_of(now), self.squareoff_at)
        if now >= sq and uni.CALENDAR.is_trading_day(session_of(now)) and session_of(now) not in self._sqoff_done:
            self._squareoff()
        self.flush()

    def flush(self) -> None:
        """Send queued order updates over the WS (lost while it is down), applying the update faults."""
        out, self._outbox = self._outbox, []
        deliver: list[dict] = []
        acct = Account.LIVE
        for oid, row, terminal in out:
            o = self.orders_by_id[oid]
            tag, sym = o.tag, o.symbol
            if tag is None:
                deliver.append(row)                         # manual / admin orders: never faulted
                continue
            if self.faults.find("drop_ws_updates", acct, symbol=sym, tag=tag, detail=f"drop {oid}") is not None:
                continue
            ooo = self.faults.find("out_of_order_updates", acct, symbol=sym, tag=tag, consume=False)
            if ooo is not None and not terminal:
                self._held_back.setdefault(oid, []).append(row)
                continue
            rows = [row]
            if terminal and oid in self._held_back:
                rows += self._held_back.pop(oid)            # the terminal first, the stale ones after it
                self.faults.find("out_of_order_updates", acct, symbol=sym, tag=tag, detail=f"reorder {oid}")
            for r in rows:
                deliver.append(r)
                if self.faults.find("duplicate_updates", acct, symbol=sym, tag=tag, detail=f"dup {oid}"):
                    deliver.append(dict(r))
        if not self.market.connected or self.on_order_update is None:
            return
        for r in deliver:
            self.on_order_update(r)

    # -- inspection ------------------------------------------------------------------------------------------------
    def api_orders(self) -> list[KOrder]:
        """Every order placed through the API (tagged by the bot)."""
        return [o for o in self.orders_by_id.values() if o.placed_by == USER_ID and o.tag is not None]

    def open_orders(self) -> list[KOrder]:
        return [o for o in self.orders_by_id.values() if not o.terminal]

    def max_orders_per_second(self) -> int:
        """The most order messages (place + cancel) sent in any rolling second."""
        ts = sorted(self.order_monos)
        best, j = 0, 0
        for i, t in enumerate(ts):
            while ts[j] < t - 1.0 + 1e-9:
                j += 1
            best = max(best, i - j + 1)
        return best


def level_rows(levels: Iterable[Level]) -> list[tuple[float, int]]:
    return [(lv.price, lv.qty) for lv in levels]
