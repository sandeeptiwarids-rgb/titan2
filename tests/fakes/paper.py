"""FaultyPaperExchange: the real PaperExchange with the FaultInjector's broker faults applied to its accounts.

In the PAPER run of a dual-run scenario the faults target the PAPER account, so the paper exchange must misbehave the
way FakeKite does in the LIVE run: the same place outcomes (rejects, ambiguous timeouts, throttling, a dead token),
the same order-update anomalies on its SIM snapshots (duplicated, reordered, dropped, early), orders held open and
cancel races, positions that lag or contradict, failing reads, and orders placed outside the bot (MANUAL closes,
foreign buys, a forced square-off). Everything else is the unmodified PaperExchange: fills, latency, persistence,
the startup rules and the 15:26 square-off.

Account events and held orders use the exchange's protected helpers (_tx, _next_order_id, _insert_fills,
_insert_exchange_order, _finish, _try_fill): this is the one test double allowed to reach into the paper ledger,
and it writes only rows the exchange itself could have written.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from typing import Any, Callable

from titan2.broker.paper import EXCHANGE, MSG_BAND, SIM, PaperExchange
from titan2.broker.port import AMBIGUOUS, FATAL, OK, REJECTED, ReadFailed, format_error
from titan2.clock import iso, session_of
from titan2.core.events import CancelResult, Event, OrderSnapshot, PlaceResult
from titan2.model import LIMIT, MIS, Account, BrokerOrder, BrokerPosition, BrokerTrade, OrderRequest, Side

from fakes.faults import RACE_MAX_HOLD_S, FaultInjector

TOKEN_MSG = "Incorrect `api_key` or `access_token`."
MANUAL_MSG = "SIM: placed outside the bot"


class FaultyPaperExchange(PaperExchange):
    def __init__(self, *args: Any, faults: FaultInjector, emit_early: Callable[[list[Event]], None] | None = None,
                 **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.faults = faults
        self.emit_early = emit_early
        self._held: dict[str, dict[str, Any]] = {}          # broker_order_id -> hold spec
        self._held_back: dict[str, list[Event]] = {}        # out_of_order: non-terminal snapshots delivered late
        self._sqoff_forced: set[Account] = set()

    # -- helpers -------------------------------------------------------------------------------------------------
    def _token_dead(self, account: Account) -> bool:
        return self.faults.find("token_expired", Account(account), consume=False) is not None

    def _read_fault(self, account: Account, mode: str, what: str) -> None:
        acct = Account(account)
        if self._token_dead(acct):
            raise ReadFailed(format_error(FATAL, "TokenException(403)", TOKEN_MSG))
        if mode and self.faults.find(mode, acct, consume=True, detail=what) is not None:
            raise ReadFailed(format_error(AMBIGUOUS, "DataException(502)", f"{what}: bad gateway"))

    # -- orders ----------------------------------------------------------------------------------------------------
    def place(self, req: OrderRequest) -> PlaceResult:
        acct, tag, sym = Account(req.account), req.tag, req.symbol
        if self._token_dead(acct):
            return PlaceResult(acct, tag, FATAL, None, "TokenException", TOKEN_MSG)
        f = self.faults.find(("reject", "http_429", "timeout_before_accept"), acct, symbol=sym, tag=tag)
        if f is not None:
            if f.mode == "reject":
                return PlaceResult(acct, tag, REJECTED, None, "OrderException", f.params.get("msg") or "rejected")
            if f.mode == "http_429":
                return PlaceResult(acct, tag, AMBIGUOUS, None, "NetworkException", "Too many requests", True)
            return PlaceResult(acct, tag, AMBIGUOUS, None, "ReadTimeout", "Read timed out.")
        band = self.faults.find("band_reject", acct, symbol=sym, tag=tag)
        if band is not None:
            return PlaceResult(acct, tag, REJECTED, None, SIM, band.params.get("msg") or MSG_BAND)
        res = super().place(req)
        if res.outcome != OK:
            return res
        hold = self.faults.find(("order_stuck_open", "cancel_race_already_complete"), acct, symbol=sym, tag=tag)
        if hold is not None:
            race = hold.mode == "cancel_race_already_complete"
            self._held[res.broker_order_id] = {
                "race": race, "cancel_works": bool(hold.params.get("cancel_works", True)),
                "fill_on_release": race or bool(hold.params.get("fill_on_release", False)),
                "until": self.clock.mono() + float(hold.params.get("hold_s", RACE_MAX_HOLD_S if race else 30.0))}
        if self.emit_early is not None and self.faults.find("ws_update_before_place_returns", acct, symbol=sym,
                                                              tag=tag) is not None:
            self.emit_early(self._filter(self.take_events()))
        if self.faults.find("timeout_after_accept", acct, symbol=sym, tag=tag) is not None:
            return PlaceResult(acct, tag, AMBIGUOUS, None, "NetworkException", "Read timed out after accept")
        return res

    def cancel(self, account: Account, order_pk: str, broker_order_id: str | None) -> CancelResult:
        acct = Account(account)
        if self._token_dead(acct):
            return CancelResult(acct, order_pk, FATAL, TOKEN_MSG)
        h = self._held.get(broker_order_id or "")
        if h is not None:
            if h["race"]:
                self._release(broker_order_id, fill=True)   # the fill wins the race; the cancel finds it complete
            elif not h["cancel_works"]:
                return CancelResult(acct, order_pk, OK, None)
            else:
                self._held.pop(broker_order_id, None)
        return super().cancel(account, order_pk, broker_order_id)

    def _try_fill(self, oid: str, now_mono: float, now_wall: datetime) -> None:
        if oid in self._held:
            return                                          # held open: released by step()
        super()._try_fill(oid, now_mono, now_wall)

    def _release(self, oid: str, *, fill: bool) -> None:
        self._held.pop(oid, None)
        row = self._row(oid)
        if row is None or row["status"] != "OPEN":
            return
        now_mono, now_wall = self.clock.mono(), self.clock.now_wall()
        if fill:
            super()._try_fill(oid, now_mono, now_wall)
            row = self._row(oid)
            if row is not None and row["status"] == "OPEN":   # no fresh depth: give up at once
                self._waiting.pop(oid, None)
                self._finish(row, [], "SIM: no fresh depth", now_mono, now_wall)
        else:
            self._finish(row, [], "SIM: IOC unfilled", now_mono, now_wall)

    def step(self, now_mono: float | None = None) -> list[Event]:
        self._account_events()
        events = super().step(now_mono)
        mono = self.clock.mono() if now_mono is None else now_mono
        for oid, h in sorted(self._held.items()):
            if h["until"] is not None and mono >= h["until"]:
                self._release(oid, fill=h["fill_on_release"])
        events += self.take_events()
        return self._filter(events)

    def _filter(self, events: list[Event]) -> list[Event]:
        """The order-update faults on the SIM snapshots this exchange pushes (its 'WS')."""
        out: list[Event] = []
        for ev in events:
            if not isinstance(ev, OrderSnapshot) or ev.tag is None:
                out.append(ev)
                continue
            acct, oid = Account(ev.account), ev.broker_order_id or ""
            row = self._row(oid)
            sym = row["symbol"] if row is not None else None
            if self.faults.find("drop_ws_updates", acct, symbol=sym, tag=ev.tag, detail=f"drop {oid}") is not None:
                continue
            terminal = ev.status != "WORKING"
            if not terminal and self.faults.find("out_of_order_updates", acct, symbol=sym, tag=ev.tag,
                                                 consume=False) is not None:
                self._held_back.setdefault(oid, []).append(ev)
                continue
            batch = [ev]
            if terminal and oid in self._held_back:
                batch += self._held_back.pop(oid)
                self.faults.find("out_of_order_updates", acct, symbol=sym, tag=ev.tag, detail=f"reorder {oid}")
            for e in batch:
                out.append(e)
                if self.faults.find("duplicate_updates", acct, symbol=sym, tag=ev.tag, detail=f"dup {oid}"):
                    out.append(e)
        return out

    # -- reads ------------------------------------------------------------------------------------------------------
    def positions(self, account: Account) -> tuple[BrokerPosition, ...]:
        acct = Account(account)
        self._read_fault(acct, "positions_error", "positions")
        rows = list(super().positions(account))
        lag = self.faults.find("positions_lag", acct, consume=True, detail="positions")
        if lag is not None:
            cutoff = self.clock.now_wall() - timedelta(seconds=lag.params["lag_s"])
            recent = [t for t in super().trades_today(account) if t.fill_wall is not None and t.fill_wall > cutoff]
            rows = _without(rows, recent)
        contra = self.faults.find("positions_contradict", acct, consume=True, detail="positions")
        if contra is not None:
            d = contra.params.get("delta")
            rows = [dataclasses.replace(r, net_qty=0 if d is None else r.net_qty + int(d)) if r.net_qty else r
                    for r in rows]
        return tuple(rows)

    def orders_today(self, account: Account) -> tuple[BrokerOrder, ...]:
        self._read_fault(account, "orders_error", "orders")
        return super().orders_today(account)

    def trades_today(self, account: Account) -> tuple[BrokerTrade, ...]:
        self._read_fault(account, "", "trades")
        rows = super().trades_today(account)
        drift = self.faults.find("trades_drift", Account(account), consume=True, detail="trades")
        if drift is not None:
            rows = tuple(dataclasses.replace(t, px=round(t.px + drift.params["px_delta"], 2))
                         if Side(t.side).value == drift.params["side"] else t for t in rows)
        return rows

    def order_history(self, account: Account, broker_order_id: str) -> BrokerOrder | None:
        self._read_fault(account, "", "order_history")
        return super().order_history(account, broker_order_id)

    def order_trades(self, account: Account, broker_order_id: str) -> tuple[BrokerTrade, ...]:
        self._read_fault(account, "", "order_trades")
        return super().order_trades(account, broker_order_id)

    def margins(self, account: Account):
        self._read_fault(account, "", "margins")
        return super().margins(account)

    # -- account events --------------------------------------------------------------------------------------------
    def _no_bot_order_working(self, acct: Account, f) -> bool:
        """external_close waits while a bot order on its symbol is working (see FaultInjector.external_close)."""
        if f.mode != "external_close":
            return True
        return self._conn.execute("SELECT 1 FROM p_orders WHERE account = ? AND symbol = ? AND status = 'OPEN' "
                                  "AND placed_by = 'API' LIMIT 1", (acct.value, f.symbol)).fetchone() is None

    def _account_events(self) -> None:
        now = self.clock.now_wall()
        for acct in (Account.PAPER, Account.SHADOW):
            for f in self.faults.due_events(acct, now, lambda f, a=acct: self._no_bot_order_working(a, f)):
                if f.mode == "external_close":
                    held = self.net(acct, f.symbol)
                    qty = int(f.params.get("qty") or held)
                    if qty > 0:
                        self._manual(acct, f.symbol, Side.SELL, qty, f.params.get("px"))
                elif f.mode == "external_buy":
                    self._manual(acct, f.symbol, Side.BUY, int(f.params["qty"]), f.params.get("px"))
                elif f.mode == "broker_squareoff":
                    self._sqoff_forced.add(acct)
            if acct in self._sqoff_forced and self._force_squareoff(acct):
                self._sqoff_forced.discard(acct)

    def net(self, account: Account, symbol: str) -> int:
        """The true signed position on symbol (the ledger, whatever the API faults show)."""
        return sum(r.net_qty for r in super().positions(account) if r.symbol == symbol)

    def nets(self, account: Account) -> dict[str, int]:
        return {r.symbol: r.net_qty for r in super().positions(account)}

    def _manual(self, account: Account, symbol: str, side: Side, qty: int, px: float | None) -> str:
        """An order placed outside the bot (Kite web / app): untagged, filled at once."""
        q = self._quotes.last(symbol)
        if px is None:
            book = (q.bids if side is Side.SELL else q.asks) if q is not None else ()
            px = book[0].price if book else (q.ltp if q is not None and q.ltp else 1.0)
        token, lot, tick = self._held_contract(symbol)
        now = self.clock.now_wall()
        ts = iso(now)
        with self._tx():
            oid = self._next_order_id(session_of(now))
            self._conn.execute(
                "INSERT INTO p_orders(broker_order_id, account, tag, symbol, token, side, product, order_type, "
                "validity, qty, lot_size, tick, limit_px, placed_ts, due_ts, status, filled_qty, avg_px, status_msg, "
                "terminal_ts, placed_by) VALUES (?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?,'COMPLETE',?,?,?,?,?)",
                (oid, account.value, symbol, int(token), side.value, MIS, LIMIT, "DAY", qty, int(lot), float(tick),
                 float(px), ts, ts, qty, float(px), MANUAL_MSG, ts, EXCHANGE))
            self._insert_fills(oid, account.value, symbol, side.value, [(0, float(px), qty)], ts)
        return oid

    def _force_squareoff(self, account: Account) -> bool:
        """The broker squares off every MIS position of the account now (after its OPEN orders resolve)."""
        if self._conn.execute("SELECT 1 FROM p_orders WHERE account = ? AND status = 'OPEN' LIMIT 1",
                              (account.value,)).fetchone():
            return False
        now = self.clock.now_wall()
        for symbol, net in self._nets(account):
            side = Side.SELL if net > 0 else Side.BUY
            contract = self._held_contract(symbol)
            fills = self._sqoff_fills(symbol, side, abs(net), self._quotes.last(symbol), contract[2])
            with self._tx():
                self._insert_exchange_order(account, symbol, contract, side, abs(net), fills, "SIM: auto square-off",
                                            now)
        return True


def _without(rows: list[BrokerPosition], recent: list[BrokerTrade]) -> list[BrokerPosition]:
    """Positions as they stood before the `recent` fills (positions_lag)."""
    by_sym = {r.symbol: r for r in rows}
    for t in recent:
        r = by_sym.get(t.symbol)
        if r is None:
            continue
        q, v = t.qty, t.qty * t.px
        if Side(t.side) is Side.BUY:
            r = dataclasses.replace(r, net_qty=r.net_qty - q, buy_qty=r.buy_qty - q,
                                    buy_value=round(r.buy_value - v, 2))
        else:
            r = dataclasses.replace(r, net_qty=r.net_qty + q, sell_qty=r.sell_qty - q,
                                    sell_value=round(r.sell_value - v, 2))
        by_sym[t.symbol] = r
    return [by_sym[r.symbol] for r in rows]

