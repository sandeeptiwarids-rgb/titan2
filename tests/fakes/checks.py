"""The chaos invariants of spec §16.5, checked against the engine's DB and the brokers' own ledgers.

Broker positions are always read from the TRUE ledgers (FakeKite's trades, the paper exchange's p_fills), never
through the API, which a fault may distort. Those fills are replayed in time order per account and symbol into a
bot holding and a foreign holding (what a FaultInjector bought outside the bot, external_buy). A third-party sale
(external_close, the broker's square-off) takes the foreign holding first, then the bot's; a bot SELL may only take
the bot holding.

After every step (InvariantChecker.after_step):
  1 no broker net < 0 (the bot never sells what it does not hold; third parties sell only what is there)
  3 at most one non-terminal bot order per leg
  4 every OPEN leg was evaluated within 2 s (while the process runs)
  5 no ENTRY order while an entry-blocking halt applies (checked when the Place is dispatched)
  6 a CLOSED structure's net = net_pnl(its fills)
  7 only LIMIT / IOC / MIS orders were submitted
  8 no SELL quantity exceeded the open quantity (per leg in the DB; at the broker, the bot holding)
At quiescence (InvariantChecker.at_quiescence):
  2 each leg's open_qty = Σ its fills, and Σ open_qty per symbol = the bot holding at the broker
  9 the paper exchange's ledger equals the engine's, per book, to the paisa
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from titan2 import ledger
from titan2.broker.paper import PaperExchange
from titan2.core import recon, risk
from titan2.core.events import Command, Place
from titan2.model import Account, Role, Side, leg_fill_totals

from fakes.runner import ScenarioRunner

ACCOUNTS = (Account.LIVE, Account.PAPER, Account.SHADOW)
_entry_halts = risk.entry_halts          # bound at import: a test that disables the engine's gate keeps this one
EVAL_MAX_GAP_S = 2.0


class InvariantFailure(AssertionError):
    pass


@dataclass
class Holding:
    bot: int = 0
    foreign: int = 0
    oversold: list[str] = field(default_factory=list)


@dataclass
class InvariantChecker:
    runner: ScenarioRunner
    failures: list[str] = field(default_factory=list)
    _open_since: dict[int, float] = field(default_factory=dict)
    _checked_trades: set[tuple] = field(default_factory=set)
    _holdings: dict = field(default_factory=dict)          # account -> (the fill log it was replayed from, result)
    _seen_api: int = 0

    def attach(self) -> InvariantChecker:
        self.runner.after_step.append(lambda r: self.after_step())
        self.runner.dispatch_hooks.append(self._on_dispatch)
        return self

    def fail(self, n: int, text: str) -> None:
        msg = f"invariant {n} at {self.runner.clock.now_wall():%H:%M:%S.%f}: {text}"
        self.failures.append(msg)
        raise InvariantFailure(msg)

    # -- after every step ----------------------------------------------------------------------------------------
    def after_step(self) -> None:
        r = self.runner
        self._broker_side()                                                            # 1, 8 (broker)
        self._order_types()
        if r.proc is None:
            self._open_since.clear()
            return
        rows = r.rows("SELECT leg_id, COUNT(*) AS n FROM orders WHERE role <> 'EXTERNAL' AND state IN "
                      "('NEW','SUBMITTING','UNKNOWN','WORKING','CANCEL_REQUESTED') GROUP BY leg_id HAVING n > 1")
        if rows:                                                                       # 3
            self.fail(3, f"legs with more than one working order: {rows}")
        self._evaluated_recently()                                                     # 4
        self._closed_net()                                                             # 6
        self._legs_never_oversold()                                                    # 8

    def holdings(self, acct: Account) -> dict[str, Holding]:
        """Replay the account's true fills (see the module docstring)."""
        log = self.runner.broker_fill_log(acct)
        hit = self._holdings.get(acct)
        if hit is not None and hit[0] is log:
            return hit[1]
        out = self._replay(log)
        self._holdings[acct] = (log, out)
        return out

    @staticmethod
    def _replay(log: list[tuple[str, str, int, bool]]) -> dict[str, Holding]:
        out: dict[str, Holding] = {}
        for sym, side, qty, bot in log:
            h = out.setdefault(sym, Holding())
            if side == "BUY":
                if bot:
                    h.bot += qty
                else:
                    h.foreign += qty
            elif bot:
                if qty > h.bot:
                    h.oversold.append(f"bot SELL {qty} with {h.bot} held")
                h.bot -= qty
            else:
                take = min(qty, max(h.foreign, 0))
                h.foreign -= take
                h.bot -= qty - take
        return out

    def _broker_side(self) -> None:
        for acct in ACCOUNTS:
            for sym, h in self.holdings(acct).items():
                if h.oversold:                                                         # 8 at the broker
                    self.fail(8, f"{acct} {sym}: {h.oversold[0]} (bot {h.bot}, foreign {h.foreign})")
                if h.bot + h.foreign < 0:                                              # 1
                    self.fail(1, f"{acct} {sym} is short {h.bot + h.foreign} at the broker "
                                 f"(bot {h.bot}, foreign {h.foreign}): not bot-explained")

    def _order_types(self) -> None:
        r = self.runner
        if r.kite.violations:                                                          # 7
            self.fail(7, f"FakeKite refused order payloads: {r.kite.violations}")
        for _, name, kw in r.kite.calls[self._seen_api:]:
            if name == "place_order" and (kw.get("order_type"), kw.get("validity"), kw.get("product"),
                                          kw.get("variety")) != ("LIMIT", "IOC", "MIS", "regular"):
                self.fail(7, f"place_order sent {kw}")
        self._seen_api = len(r.kite.calls)

    def _evaluated_recently(self) -> None:
        r = self.runner
        p = r.proc
        now = r.clock.mono()
        if now - p.started_mono < EVAL_MAX_GAP_S + 0.5:
            return
        open_now = set()
        for leg in p.engine.state.legs.values():
            st = p.engine.state.structures.get(leg.structure_id)
            if st is None or str(leg.state) != "OPEN" or str(st.state) != "OPEN":
                continue
            open_now.add(leg.leg_id)
            since = self._open_since.setdefault(leg.leg_id, now)
            if now - since < EVAL_MAX_GAP_S:
                continue
            last = r.evaluated.get(leg.leg_id)
            if last is None or now - last > EVAL_MAX_GAP_S + r.dt + 1e-6:                # 4
                self.fail(4, f"OPEN leg {leg.leg_id} not evaluated for {now - (last or since):.2f}s")
        for lid in list(self._open_since):
            if lid not in open_now:
                del self._open_since[lid]

    def _closed_net(self) -> None:
        r = self.runner
        for t in r.rows("SELECT t.structure_id, t.net, t.pnl_status, s.state FROM trades t JOIN structures s "
                        "ON s.structure_id = t.structure_id"):
            sid = t["structure_id"]
            key = (sid, t["net"], t["pnl_status"], t["state"])
            if key in self._checked_trades:
                continue
            self._checked_trades.add(key)
            if t["state"] in ("CLOSED", "CLOSED_EXTERNAL") and t["pnl_status"] == "FINAL":   # 6
                want = r.net_from_fills(sid)
                if not math.isclose(t["net"], want, abs_tol=0.01):
                    self.fail(6, f"structure {sid}: booked net {t['net']} != net_pnl(fills) {want}")

    def _legs_never_oversold(self) -> None:
        r = self.runner
        for leg in r.proc.engine.state.legs.values():
            entry, exit_ = leg_fill_totals(leg.leg_orders())
            if exit_ > entry:                                                          # 8
                self.fail(8, f"leg {leg.leg_id}: exit fills {exit_} > entry fills {entry}")

    # -- at dispatch ---------------------------------------------------------------------------------------------
    def _on_dispatch(self, r: ScenarioRunner, c: Command) -> None:
        if not isinstance(c, Place) or c.req.role is not Role.ENTRY:
            return
        o = r.proc.engine.state.orders.get(c.order_pk)
        st = r.proc.engine.state.structures.get(o.structure_id) if o is not None else None
        if st is None:
            return
        blocking = _entry_halts(r.halts_before, account=Account(st.account), book=st.book, strategy=st.strategy,
                                    underlying=st.underlying, policy=risk.book_policy(st.book))
        if blocking:                                                                   # 5
            self.fail(5, f"ENTRY {c.order_pk} on {st.book} while halted by "
                         f"{[(h.kind, h.scope) for h in blocking]}")

    # -- at quiescence -------------------------------------------------------------------------------------------
    def at_quiescence(self) -> None:
        r = self.runner
        if r.open_bot_orders():
            self.fail(2, f"not quiescent: working orders {[(o['order_pk'], o['state']) for o in r.open_bot_orders()]}")
        legs = [lg for lg in r.proc.engine.state.legs.values() if not lg.is_terminal]
        for acct in ACCOUNTS:
            hold = self.holdings(acct)
            nets = r.broker_nets(acct)
            held: dict[str, int] = {}
            for lg in legs:
                if Account(lg.account) is acct:
                    held[lg.inst.symbol] = held.get(lg.inst.symbol, 0) + lg.open_qty
            for sym in set(hold) | set(held):
                h = hold.get(sym, Holding())
                if h.bot + h.foreign != nets.get(sym, 0):
                    raise AssertionError(f"harness: {acct} {sym} replay {h} != broker net {nets.get(sym, 0)}")
                if h.bot != held.get(sym, 0):                                          # 2
                    self.fail(2, f"{acct} {sym}: broker {nets.get(sym, 0)} = bot {h.bot} + foreign {h.foreign}, "
                                 f"but the open legs hold {held.get(sym, 0)}")
        for lg in r.proc.engine.state.legs.values():
            entry_f = exit_f = 0
            for o in lg.leg_orders():
                q = sum(f.qty for f in r.store.effective_fills([o.order_pk]))
                if q != o.filled_qty and o.filled_qty and not (o.role == Role.EXTERNAL and q == 0):
                    self.fail(2, f"order {o.order_pk}: fills {q} != filled_qty {o.filled_qty}")
                if o.role in (Role.ENTRY, Role.SELFTEST):
                    entry_f += q
                elif o.role == Role.EXIT or (o.role == Role.EXTERNAL and o.side == Side.SELL):
                    exit_f += q
            if not lg.is_terminal and entry_f - exit_f != lg.open_qty:
                self.fail(2, f"leg {lg.leg_id}: open_qty {lg.open_qty} != fills {entry_f - exit_f}")
        self._paper_ledger_equals_engine()

    def _paper_ledger_equals_engine(self) -> None:
        r = self.runner
        for acct in (Account.PAPER, Account.SHADOW):
            if r.rows("SELECT 1 FROM structures WHERE account = ? AND state NOT IN ('CLOSED','CLOSED_EXTERNAL','VOID')",
                      (acct.value,)):
                continue                                  # an open position's entry has no trades row yet
            ledger_net: dict[str, float] = {}
            for t in r.rows("SELECT book, net FROM trades WHERE account = ? AND session = ?",
                            (acct.value, r.session.isoformat())):
                ledger_net[t["book"]] = ledger_net.get(t["book"], 0.0) + t["net"]
            books, parts = ledger.truth_attribution(r.rows(        # the engine's own attribution (§9.8)
                "SELECT order_pk, leg_id, book, role, broker_order_id, filled_qty, external_kind FROM orders "
                "WHERE account = ? AND filled_qty > 0", (acct.value,)))
            true = PaperExchange.trades_today(r.proc.paper, acct)       # the exchange's own ledger, no API fault
            trades = [t for t in true if t.broker_order_id in books]   # foreign orders aside
            tc = recon.eod_truth_check(acct, r.session, ledger_net, books, trades, parts=parts)
            if not tc.ok:                                                              # 9
                self.fail(9, f"{acct} paper ledger != engine ledger: "
                             f"{[(b.book, b.ledger_net, b.broker_net) for b in tc.books]}")
