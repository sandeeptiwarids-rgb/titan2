"""FaultInjector: every broker-side fault mode of spec §16.1, for both accounts of the dual run.

A fault targets one account. Left unset it targets the runner's account under test, so one scenario script puts
the same fault on PAPER (FaultyPaperExchange) in the PAPER run and on LIVE (FakeKite) in the LIVE run: both runs then
see the same broker behaviour through their own adapter (R4). FakeKite also consults the global `token_expired`
state for every call, market data included (the Kite session serves both runs' market data).

Modes (the spec's names):
  rejects            reject(msg), band_reject
  submit ambiguity   timeout_after_accept (the order exists, then a NetworkException), timeout_before_accept,
                     http_429, token_expired
  order updates      duplicate_updates, out_of_order_updates, drop_ws_updates, ws_update_before_place_returns,
                     cancel_race_already_complete, order_stuck_open
  positions/orders   positions_lag(s), positions_contradict, positions_error, orders_error
  account events     external_close(sym, at, px), external_buy(sym, qty), broker_squareoff(at)
  data               stale_depth (delegated to ScriptedMarket: the bot's WS and REST views freeze, the book moves)

Filters: symbol ("NFO:..."), role ("ENTRY" | "EXIT" | "COVER" | "SELFTEST", read from the order tag), count (how many
applications; None = every match while active) and an active window [start, end) in wall time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from titan2.clock import IST, FakeClock, ist_datetime
from titan2.model import Account, parse_tag

PLACE_MODES = ("reject", "band_reject", "timeout_before_accept", "timeout_after_accept", "http_429")
ORDER_MODES = ("order_stuck_open", "cancel_race_already_complete", "ws_update_before_place_returns",
               "duplicate_updates", "out_of_order_updates", "drop_ws_updates")
READ_MODES = ("positions_lag", "positions_contradict", "positions_error", "orders_error")
EVENT_MODES = ("external_close", "external_buy", "broker_squareoff")
ALL_MODES = PLACE_MODES + ORDER_MODES + READ_MODES + EVENT_MODES + ("token_expired", "stale_depth",
                                                                   "trades_drift")

BAND_MSG = "RMS:Rule: Order price is outside the LPP range"
RACE_MAX_HOLD_S = 10.0       # a race-held IOC that no cancel reaches fills at this age (an IOC never rests for long)
RMS_MSG = "RMS:Margin Exceeds, Required:0, Available:0 for entity account"


def role_of(tag: str | None) -> str | None:
    p = parse_tag(tag)
    return None if p is None else p.role.value


@dataclass
class Fault:
    mode: str
    account: Account | None
    symbol: str | None = None
    role: str | None = None
    count: int | None = 1
    start: datetime | None = None
    end: datetime | None = None
    params: dict[str, Any] = field(default_factory=dict)
    hits: int = 0
    done: bool = False                       # account events fire once

    def active(self, now: datetime) -> bool:
        if self.count is not None and self.hits >= self.count:
            return False
        if self.start is not None and now < self.start:
            return False
        return self.end is None or now < self.end


class FaultInjector:
    def __init__(self, account: Account | str | None = None) -> None:
        self.account = None if account is None else Account(account)
        self.clock: FakeClock | None = None
        self.market: Any = None              # ScriptedMarket, for stale_depth
        self.faults: list[Fault] = []
        self.applied: list[tuple[datetime, str, str, str]] = []   # (wall, mode, account, detail)
        self.token_expired_on = False
        self.token_rotated = False
        # (account, symbol | None) -> True while the bot is already exiting there (a leg EXITING with qty open; None:
        #   any symbol). The ScenarioRunner sets it. A manual close or a square-off is held back meanwhile: a sale
        #   racing an exit the bot has already decided makes a short no engine can prevent (§9.3 only detects it).
        self.exit_in_progress: Any = None

    def bind(self, account: Account, clock: FakeClock, market: Any) -> None:
        self.account = Account(account)
        self.clock = clock
        self.market = market
        for f in self.faults:
            if f.account is None:
                f.account = self.account

    # -- time ----------------------------------------------------------------------------------------------------
    def _now(self) -> datetime:
        if self.clock is None:
            raise RuntimeError("FaultInjector is not bound to a runner yet")
        return self.clock.now_wall()

    def _when(self, at: datetime | str | None) -> datetime | None:
        if at is None:
            return None
        if isinstance(at, str):
            if self.clock is None:
                raise RuntimeError("bind the FaultInjector before using 'HH:MM' times")
            return ist_datetime(self.clock.now_wall().date(), at)
        return at if at.tzinfo is not None else at.replace(tzinfo=IST)

    # -- registration --------------------------------------------------------------------------------------------
    def add(self, mode: str, *, account: Account | str | None = None, symbol: str | None = None,
            role: str | None = None, count: int | None = 1, start: datetime | str | None = None,
            end: datetime | str | None = None, **params: Any) -> Fault:
        if mode not in ALL_MODES:
            raise ValueError(f"unknown fault mode {mode!r}")
        acct = Account(account) if account is not None else self.account
        f = Fault(mode, acct, symbol, role, count, self._when(start), self._when(end), dict(params))
        self.faults.append(f)
        return f

    def reject(self, msg: str = "Order rejected by OMS", **kw: Any) -> Fault:
        """The next matching place is rejected by the broker with msg (an API-level OrderException)."""
        return self.add("reject", msg=msg, **kw)

    def band_reject(self, msg: str = BAND_MSG, **kw: Any) -> Fault:
        """The next matching place is rejected as outside the price band / LPP (§5.7 band-type reject)."""
        return self.add("band_reject", msg=msg, **kw)

    def timeout_before_accept(self, **kw: Any) -> Fault:
        """place_order times out and the order was never created (AMBIGUOUS, then NOT_PLACED by lookups)."""
        return self.add("timeout_before_accept", **kw)

    def timeout_after_accept(self, **kw: Any) -> Fault:
        """The order is created at the broker, then place_order raises NetworkException (AMBIGUOUS, adopted)."""
        return self.add("timeout_after_accept", **kw)

    def http_429(self, **kw: Any) -> Fault:
        """place_order is throttled (NetworkException code 429): never created; lookups back off 1/2/4 s."""
        return self.add("http_429", **kw)

    def token_expired(self, *, start: datetime | str | None = None, end: datetime | str | None = None) -> Fault:
        """Every Kite call raises TokenException (FATAL) while active; the account under test's order path too.
        rotate_token() then makes the session file hold a newer working token (the engine exits 75)."""
        return self.add("token_expired", count=None, start=start, end=end)

    def rotate_token(self) -> None:
        self.token_rotated = True
        for f in self.faults:
            if f.mode == "token_expired" and f.end is None:
                f.end = self._now()

    def duplicate_updates(self, **kw: Any) -> Fault:
        """Every order update of the matching orders is delivered twice."""
        kw.setdefault("count", None)
        return self.add("duplicate_updates", **kw)

    def out_of_order_updates(self, **kw: Any) -> Fault:
        """A matching order's non-terminal updates are held back and delivered after its terminal one."""
        return self.add("out_of_order_updates", **kw)

    def drop_ws_updates(self, **kw: Any) -> Fault:
        """Order updates are lost (the poll timers and the reconnect back-fill must recover them)."""
        kw.setdefault("count", None)
        return self.add("drop_ws_updates", **kw)

    def ws_update_before_place_returns(self, **kw: Any) -> Fault:
        """The order's first update arrives before place_order returns (before the PlaceResult)."""
        return self.add("ws_update_before_place_returns", **kw)

    def cancel_race_already_complete(self, **kw: Any) -> Fault:
        """The order rests unfilled until the engine cancels it; the cancel then loses the race: the order fills
        and the cancel is refused as already complete (BENIGN, then poll). A cancel that never reaches the
        broker (a dead token, say) leaves it resting at most hold_s (default RACE_MAX_HOLD_S); then it fills."""
        return self.add("cancel_race_already_complete", **kw)

    def order_stuck_open(self, hold_s: float = 30.0, *, cancel_works: bool = True, fill_on_release: bool = False,
                         **kw: Any) -> Fault:
        """The order stays OPEN with nothing filled for hold_s. A cancel ends it at once when cancel_works, else it
        is acknowledged and ignored; at release it fills against the book (fill_on_release) or is cancelled."""
        return self.add("order_stuck_open", hold_s=hold_s, cancel_works=cancel_works,
                        fill_on_release=fill_on_release, **kw)

    def positions_lag(self, s: float, **kw: Any) -> Fault:
        """positions() shows the pre-fill position for s seconds after every fill."""
        kw.setdefault("count", None)
        return self.add("positions_lag", lag_s=float(s), **kw)

    def positions_contradict(self, *, delta: int | None = None, **kw: Any) -> Fault:
        """positions() contradicts the orders: every non-zero net reads 0 (delta None) or is shifted by delta."""
        kw.setdefault("count", None)
        return self.add("positions_contradict", delta=delta, **kw)

    def positions_error(self, **kw: Any) -> Fault:
        kw.setdefault("count", None)
        return self.add("positions_error", **kw)

    def orders_error(self, **kw: Any) -> Fault:
        kw.setdefault("count", None)
        return self.add("orders_error", **kw)

    def external_close(self, symbol: str, at: datetime | str | None = None, px: float | None = None, *,
                       qty: int | None = None, **kw: Any) -> Fault:
        """Someone sells the account's whole long position on symbol (or qty) at px outside the bot (MANUAL).
        It waits while a bot order on symbol is working or the bot is exiting symbol: a sale racing a bot exit
        already decided or on its way makes a short that no bot can prevent (§9.3 detects it: SHORT, alert only),
        which is outside what the invariants test."""
        return self.add("external_close", symbol=symbol, at=self._when(at), px=px, qty=qty, **kw)

    def external_buy(self, symbol: str, qty: int, at: datetime | str | None = None, px: float | None = None,
                     **kw: Any) -> Fault:
        """Someone buys qty of symbol in the account outside the bot (a foreign / unexplained position)."""
        return self.add("external_buy", symbol=symbol, qty=int(qty), at=self._when(at), px=px, **kw)

    def broker_squareoff(self, at: datetime | str | None = None, **kw: Any) -> Fault:
        """FakeKite squares off every MIS position at `at` (Zerodha's auto square-off; paper runs its own at
        BROKER_SQUAREOFF_AT, so this is the LIVE side of that behaviour). Like the real one (15:26, after the bot
        stopped sending at 15:25) it waits while the bot is exiting on the account."""
        return self.add("broker_squareoff", at=self._when(at), **kw)

    def stale_depth(self, symbol: str, at: datetime | str | None = None) -> None:
        """The bot's WS and REST views of symbol freeze (old timestamps), while the true book moves on."""
        if self.market is None:
            raise RuntimeError("bind the FaultInjector to a runner before stale_depth")
        self.applied.append((self._now(), "stale_depth", "*", symbol))
        self.market.stale_depth(symbol, at=at)

    def trades_drift(self, px_delta: float, *, side: str = "SELL", **kw: Any) -> Fault:
        """The broker's tradebook shows the prices of its `side` trades shifted by px_delta (a ledger the bot does
        not hold). One side only: the same shift on a buy and its sell would cancel out."""
        kw.setdefault("count", None)
        return self.add("trades_drift", px_delta=float(px_delta), side=side, **kw)

    # -- matching ------------------------------------------------------------------------------------------------
    def find(self, modes: tuple[str, ...] | str, account: Account, *, symbol: str | None = None,
             tag: str | None = None, consume: bool = True, detail: str = "") -> Fault | None:
        """The first active fault of one of `modes` matching (account, symbol, role of tag); counted if consume."""
        modes = (modes,) if isinstance(modes, str) else modes
        now = self._now()
        role = role_of(tag)
        for f in self.faults:
            if f.mode not in modes or f.account is not Account(account) or not f.active(now):
                continue
            if f.symbol is not None and f.symbol != symbol:
                continue
            if f.role is not None and f.role != role:
                continue
            if consume:
                f.hits += 1
                self.applied.append((now, f.mode, Account(account).value, detail or (tag or symbol or "")))
            return f
        return None

    def token_dead(self) -> bool:
        """The Kite session token is rejected now (FakeKite raises TokenException for every call)."""
        now = self._now()
        return any(f.mode == "token_expired" and f.active(now) for f in self.faults)

    def due_events(self, account: Account, now: datetime, ready: Any = None) -> list[Fault]:
        """Account events of `account` whose time has come (each fires once). ready(fault) -> bool can hold one
        back for now: it is then offered again at the next step."""
        out = []
        for f in self.faults:
            if f.mode in EVENT_MODES and f.account is Account(account) and not f.done:
                at = f.params.get("at")
                exiting = (f.mode in ("external_close", "broker_squareoff") and self.exit_in_progress is not None
                           and self.exit_in_progress(Account(account),
                                                     f.symbol if f.mode == "external_close" else None))
                if (at is None or at <= now) and not exiting and (ready is None or ready(f)):
                    f.done = True
                    f.hits += 1
                    self.applied.append((now, f.mode, Account(account).value, f.symbol or ""))
                    out.append(f)
        return out

    def clear(self) -> None:
        """End every fault now (scheduled account events that have not fired are dropped)."""
        now = self._now()
        for f in self.faults:
            f.end = now if f.end is None or f.end > now else f.end
            f.done = True

    def count(self, mode: str) -> int:
        return sum(1 for _, m, _, _ in self.applied if m == mode)
