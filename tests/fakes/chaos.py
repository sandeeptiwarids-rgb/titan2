"""Seeded chaos runs (spec §16.5): random intents, market moves, FaultInjector modes, ScriptedMarket faults and
restarts at random points, with the §16.5 invariants checked after every step and at quiescence.

A run is fully determined by its seed (account, start time, timeline). To reproduce a failure:
    python -c "import sys; sys.path[:0]=['.', 'tests']; from fakes.chaos import replay; replay(SEED)"
"""

from __future__ import annotations

import random
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from titan2.clock import ist_datetime
from titan2.model import Account

from fakes import universe as uni
from fakes.checks import InvariantChecker
from fakes.runner import ScenarioRunner

LIQUID = ("NSE:RELIANCE", "NSE:INFY", "NSE:SBIN", "NSE:TCS", "NSE:ZYDUSLIFE")
STRATEGIES = ("price_spike_up", "opening_drive")
ACTIVE_S = 40.0                  # scripted chaos per seed, then the clean-up flatten
SETTLE_TIMEOUT_S = 150.0


@dataclass
class ChaosResult:
    seed: int
    account: Account
    start: datetime
    timeline: list[str] = field(default_factory=list)
    restarts: int = 0
    entries: int = 0
    exits: int = 0
    structures: list[tuple] = field(default_factory=list)      # (book, state, exit_reason) at the end
    applied: list[str] = field(default_factory=list)           # the faults that actually fired
    halts: list[str] = field(default_factory=list)


def _start_time(rng: random.Random) -> str:
    minutes = rng.randrange(0, 240)                 # 09:18 .. 13:18
    t = datetime(2000, 1, 1, 9, 18, 30) + timedelta(minutes=minutes, seconds=rng.randrange(0, 60))
    return t.strftime("%H:%M:%S")


def _book(r: ScenarioRunner, rng: random.Random, symbol: str, inst, x: float) -> None:
    """A book around the default fair price moved by x (fraction), with a random spread and depth."""
    bids, asks, _ = uni.default_book(inst)
    fair = (bids[0].price + asks[0].price) / 2
    bid = max(uni.TICK, round(int(fair * (1 + x) / uni.TICK) * uni.TICK, 2))
    spread = rng.choice((1, 2, 2, 3)) * uni.TICK
    lots = rng.choice((0.5, 1, 2, 4, 4, 6))
    qty = max(1, int(lots * inst.lot_size))
    levels = rng.choice((2, 3, 5))
    if rng.random() < 0.08:
        r.market.quote(symbol, None, round(bid + spread, 2), qty=qty, levels=levels)      # no bids for a while
    else:
        r.market.quote(symbol, bid, round(bid + spread, 2), qty=qty, levels=levels)


def build(r: ScenarioRunner, rng: random.Random, res: ChaosResult) -> list[tuple[float, str, Callable[[], None]]]:
    """The run's timeline: (seconds after the start, label, action)."""
    t: list[tuple[float, str, Callable[[], None]]] = []
    fi = r.faults
    unders = rng.sample(LIQUID, 3)
    contracts = {}
    for u in unders:
        for ot in ("CE", "PE"):
            c = uni.symbol_of(r.instruments, u, ot, "ATM", r.spot(u), r.session)
            contracts[c.symbol] = c
    syms = sorted(contracts)
    for _ in range(rng.randint(1, 3)):
        u = rng.choice(unders)
        strat = rng.choice(STRATEGIES)
        direction = "DOWN" if strat == "opening_drive" and rng.random() < 0.3 else "UP"
        t.append((rng.uniform(0.5, ACTIVE_S * 0.6), f"intent {strat} {u} {direction}",
                  lambda u=u, s=strat, d=direction: r.intent(u, d, strategy=s)))
    for _ in range(rng.randint(3, 10)):
        sym = rng.choice(syms)
        x = rng.choice((-0.45, -0.25, -0.16, -0.05, 0.0, 0.03, 0.06, 0.12, 0.2, 0.3)) * rng.uniform(0.6, 1.2)
        t.append((rng.uniform(1.0, ACTIVE_S), f"book {sym} {x:+.2f}",
                  lambda sym=sym, x=x: _book(r, rng, sym, contracts[sym], x)))

    def at(lo: float, hi: float) -> datetime:
        return r.clock.now_wall() + timedelta(seconds=rng.uniform(lo, hi))

    modes = [
        ("reject", lambda: fi.reject(role=rng.choice(("ENTRY", "EXIT")), count=rng.randint(1, 2))),
        ("band_reject", lambda: fi.band_reject(role="EXIT", count=rng.randint(1, 3))),
        ("timeout_before_accept", lambda: fi.timeout_before_accept(role=rng.choice(("ENTRY", "EXIT")))),
        ("timeout_after_accept", lambda: fi.timeout_after_accept(role=rng.choice(("ENTRY", "EXIT")))),
        ("http_429", lambda: fi.http_429(role=rng.choice(("ENTRY", "EXIT")))),
        ("token_expired", lambda: fi.token_expired(start=at(0, 10), end=at(12, 18))),
        ("duplicate_updates", lambda: fi.duplicate_updates(end=at(5, 30))),
        ("out_of_order_updates", lambda: fi.out_of_order_updates(count=rng.randint(1, 3))),
        ("drop_ws_updates", lambda: fi.drop_ws_updates(start=at(0, 20), end=at(21, 26))),
        ("ws_update_before_place_returns", lambda: fi.ws_update_before_place_returns(count=rng.randint(1, 2))),
        ("cancel_race_already_complete", lambda: fi.cancel_race_already_complete(role=rng.choice(("ENTRY", "EXIT")))),
        ("order_stuck_open", lambda: fi.order_stuck_open(hold_s=rng.uniform(2, 14), cancel_works=rng.random() < 0.6,
                                                         fill_on_release=rng.random() < 0.5,
                                                         role=rng.choice(("ENTRY", "EXIT")))),
        ("positions_lag", lambda: fi.positions_lag(rng.uniform(1, 8), end=at(15, 40))),
        ("positions_contradict", lambda: _contradict(fi, at(2, 30), rng.uniform(0.5, 2.5))),
        ("positions_error", lambda: fi.positions_error(start=at(0, 20), end=at(21, 30))),
        ("orders_error", lambda: fi.orders_error(start=at(0, 20), end=at(21, 30))),
        ("external_close", lambda: fi.external_close(rng.choice(syms), at=at(5, ACTIVE_S))),
        ("external_buy", lambda: _external_buy(fi, rng.choice(syms), contracts, at(1, ACTIVE_S))),
        ("broker_squareoff", lambda: fi.broker_squareoff(at=at(10, ACTIVE_S))),
    ]
    for name, add in rng.sample(modes, rng.randint(1, 4)):
        t.append((0.0, f"fault {name}", add))
    for _ in range(rng.randint(0, 2)):
        sym = rng.choice(syms)
        kind = rng.choice(("freeze", "rest_fail", "disconnect", "stale_depth"))
        t0, dur = rng.uniform(1, ACTIVE_S - 5), rng.uniform(0.5, 4.0)
        if kind == "freeze":
            t += [(t0, f"freeze {sym}", lambda s=sym: r.market.freeze(s)),
                  (t0 + dur, f"unfreeze {sym}", lambda s=sym: r.market.unfreeze(s))]
        elif kind == "rest_fail":
            t += [(t0, f"rest_fail {sym}", lambda s=sym: r.market.rest_fail(s)),
                  (t0 + dur, f"rest_ok {sym}", lambda s=sym: r.market.rest_ok(s))]
        elif kind == "disconnect":
            t += [(t0, "ws disconnect", lambda: r.market.disconnect()),
                  (t0 + dur, "ws reconnect", lambda: r.market.reconnect())]
        else:
            t += [(t0, f"stale_depth {sym}", lambda s=sym: r.faults.stale_depth(s)),
                  (t0 + dur, f"unstale {sym}", lambda s=sym: r.market.unstale(s))]
    if rng.random() < 0.3:
        down = rng.uniform(0.0, 2.5)
        t.append((rng.uniform(2.0, ACTIVE_S - 3), f"restart (down {down:.1f}s)",
                  lambda d=down: _restart(r, res, d)))
    return sorted(((round(off, 3), label, fn) for off, label, fn in t), key=lambda x: x[0])


def _restart(r: ScenarioRunner, res: ChaosResult, downtime: float) -> None:
    res.restarts += 1
    r.restart(downtime_s=downtime, dt=0.05)


def _contradict(fi, start: datetime, dur_s: float) -> None:
    """positions_contradict lasts at most 2.5 s here: a sustained contradiction is a different, documented hazard
    (a confirmed 'flat' read that the orders contradict closes the leg UNRESOLVED and halts; see the scenarios)."""
    fi.positions_contradict(start=start, end=start + timedelta(seconds=dur_s))


def _external_buy(fi, symbol: str, contracts: dict, at: datetime) -> None:
    fi.external_buy(symbol, contracts[symbol].lot_size, at=at)


def run(seed: int, tmp: Path, *, active_s: float = ACTIVE_S) -> ChaosResult:
    rng = random.Random(seed)
    acct = Account.PAPER if seed % 2 == 0 else Account.LIVE
    start = _start_time(rng)
    start_wall = ist_datetime(uni.SESSION, start)
    warm = max(start_wall + timedelta(seconds=65), ist_datetime(uni.SESSION, "09:20:00"))
    r = ScenarioRunner(tmp, acct, start=start, warmup_until=warm.strftime("%H:%M:%S"))
    res = ChaosResult(seed, acct, start_wall)
    checker = InvariantChecker(r).attach()
    try:
        timeline = build(r, rng, res)
        res.timeline = [f"{off:6.2f}s {label}" for off, label, _ in timeline]
        t0 = r.clock.mono()
        for off, label, fn in timeline:
            if r.clock.mono() < t0 + off:
                r.run(t0 + off - r.clock.mono())
            if r.proc is None and not label.startswith("restart"):
                r.boot()
            fn()
        if r.clock.mono() < t0 + active_s:
            r.run(t0 + active_s - r.clock.mono())
        _clean_up(r)
        checker.at_quiescence()
        res.entries = len(r.places("ENTRY"))
        res.exits = len(r.places("EXIT"))
        res.structures = [(x["book"], x["state"], x["exit_reason"]) for x in r.rows(
            "SELECT book, state, exit_reason FROM structures WHERE session = ? ORDER BY structure_id",
            (r.session.isoformat(),))]
        res.applied = sorted({f"{m}:{a}" for _, m, a, _ in r.faults.applied})
        res.halts = sorted({f"{h['kind']}:{h['scope']}" for h in r.rows("SELECT kind, scope FROM halts")})
        return res
    except AssertionError as exc:
        raise AssertionError(f"chaos seed {seed} ({acct.value}, start {start}) failed: {exc}\n  timeline:\n    "
                             + "\n    ".join(res.timeline)) from exc
    finally:
        r.close()


def _clean_up(r: ScenarioRunner) -> None:
    """End every fault, restore a liquid two-sided market, flatten everything and run to quiescence."""
    r.faults.clear()
    for sym in list(r.market.frozen):
        r.market.unfreeze(sym)
    for sym in list(r.market.stale):
        r.market.unstale(sym)
    r.market.rest_ok()
    for sym in list(r.market.books):
        inst = next(c for c in uni.CONTRACTS if c.symbol == sym)
        bids, asks, ltp = uni.default_book(inst)
        r.market.depth(sym, bids, asks, ltp=ltp, last_trade_age_s=1.0)
    if r.proc is None:
        r.boot()
    r.market.reconnect()
    r.run(2)
    r.command("FLATTEN_ALL", confirm="FLATTEN")
    end = r.clock.mono() + SETTLE_TIMEOUT_S

    def quiet() -> bool:
        if r.proc is None:
            r.boot()
            return False
        live = r.rows("SELECT 1 FROM structures WHERE state NOT IN ('CLOSED','CLOSED_EXTERNAL','VOID')")
        return not live and not r.open_bot_orders() and not r.kite.open_orders()
    while not quiet():
        if r.clock.mono() > end:
            left = r.rows("SELECT structure_id, book, state, exit_reason FROM structures "
                          "WHERE state NOT IN ('CLOSED','CLOSED_EXTERNAL','VOID')")
            raise AssertionError(f"not flat {SETTLE_TIMEOUT_S:.0f}s after the clean-up FLATTEN_ALL: {left}; "
                                 f"working {[(o['order_pk'], o['state']) for o in r.open_bot_orders()]}")
        r.step(0.25)
    r.run(12, dt=0.25)                                 # past the positions lag and every grace window


def replay(seed: int) -> ChaosResult:
    """Run one seed outside pytest (prints the timeline)."""
    res = run(seed, Path(tempfile.mkdtemp(prefix=f"chaos{seed}_")))
    print(f"seed {seed} {res.account.value} start {res.start:%H:%M:%S} restarts {res.restarts} entries "
          f"{res.entries} exits {res.exits}")
    print("\n".join(res.timeline))
    return res
