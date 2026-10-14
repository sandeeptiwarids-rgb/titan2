"""Chaos invariants (spec §16.5): seeded random sequences of intents, book moves, FaultInjector modes,
ScriptedMarket faults and restarts, with the nine invariants checked after every step and at quiescence
(fakes/checks.py). 200 seeds by default (CI); the nightly run sets TITAN2_CHAOS_SEEDS=500.

Seeds alternate between the PAPER and the LIVE account. A failing seed prints its timeline; replay it with
    python -c "import sys; sys.path[:0]=['.', 'tests']; from fakes.chaos import replay; replay(SEED)"

The checker tests below break one thing on purpose and expect its invariant to fail, so a quiet chaos run means
the invariants held, not that the checker is blind.
"""

from __future__ import annotations

import os

import pytest

from titan2.core import positions as positions_mod
from titan2.core import risk
from titan2.core.engine import Engine
from titan2.model import Account, OrderRequest, Role, Side, make_tag

from fakes import universe as uni
from fakes.chaos import run
from fakes.checks import InvariantChecker, InvariantFailure
from fakes.kite_errors import InputException
from fakes.runner import ScenarioRunner
from fakes.scenario import TERMINAL_STRUCT, enter, wait_for

pytestmark = pytest.mark.chaos

SEEDS = int(os.environ.get("TITAN2_CHAOS_SEEDS", "200"))


@pytest.mark.parametrize("seed", range(SEEDS))
def test_chaos_invariants(seed, tmp_path):
    run(seed, tmp_path)


# -- the checker has teeth ----------------------------------------------------------------------------------------
@pytest.fixture
def runner(tmp_path, request):
    r = ScenarioRunner(tmp_path, request.param)
    yield r
    r.close()


def _checked(r: ScenarioRunner) -> InvariantChecker:
    return InvariantChecker(r).attach()


def _rogue_sell(r: ScenarioRunner, symbol: str, qty: int) -> None:
    """A bot-tagged SELL sent straight to the broker, past the engine: what an engine bug that oversells would do."""
    inst = next(c for c in uni.CONTRACTS if c.symbol == symbol)
    bids, _, _ = uni.default_book(inst)
    tag = make_tag(Role.EXIT, r.acct, r.session, 900, 1)
    if r.acct is Account.LIVE:
        r.kite.place_order(variety="regular", exchange="NFO", tradingsymbol=inst.tradingsymbol,
                           transaction_type="SELL", quantity=qty, product="MIS", order_type="LIMIT",
                           price=bids[0].price, validity="IOC", tag=tag)
    else:
        r.proc.paper.place(OrderRequest(r.acct, tag, Role.EXIT, symbol, inst.tradingsymbol, "NFO", inst.token,
                                        Side.SELL, qty, bids[0].price, inst.lot_size, inst.tick))


@pytest.mark.parametrize("runner", ["PAPER", "LIVE"], indirect=True)
def test_checker_catches_a_bot_oversell(runner):
    r = runner
    checker = _checked(r)
    _, st = enter(r)
    sym = r.rows("SELECT symbol FROM legs WHERE structure_id = ?", (st["structure_id"],))[0]["symbol"]
    held = r.broker_net(sym)
    _rogue_sell(r, sym, 2 * held)
    with pytest.raises(InvariantFailure, match=r"invariant 8 .*bot SELL"):
        r.run(5)
    assert checker.failures


@pytest.mark.parametrize("runner", ["LIVE"], indirect=True)
def test_checker_catches_a_market_order(runner):
    r = runner
    _checked(r)
    inst = r.contract()
    with pytest.raises(InputException):
        r.kite.place_order(variety="regular", exchange="NFO", tradingsymbol=inst.tradingsymbol,
                           transaction_type="BUY", quantity=inst.lot_size, product="MIS", order_type="MARKET",
                           validity="DAY", tag=make_tag(Role.ENTRY, Account.LIVE, r.session, 901, 1))
    with pytest.raises(InvariantFailure, match="invariant 7"):
        r.step()


@pytest.mark.parametrize("runner", ["PAPER"], indirect=True)
def test_checker_catches_an_unevaluated_open_leg(runner, monkeypatch):
    r = runner
    _checked(r)
    enter(r)
    monkeypatch.setattr(Engine, "_evaluate", lambda self, leg: [])
    with pytest.raises(InvariantFailure, match="invariant 4"):
        r.run(5)


@pytest.mark.parametrize("runner", ["PAPER"], indirect=True)
def test_checker_catches_an_entry_while_halted(runner, monkeypatch):
    r = runner
    _checked(r)
    r.command("PAUSE")
    r.run(1.5)
    assert r.halts(), "PAUSE should have raised a halt"
    monkeypatch.setattr(risk, "entry_halts", lambda *a, **k: ())          # the engine's gate is gone
    with pytest.raises(InvariantFailure, match="invariant 5"):
        r.intent()
        r.run(3)


def _pad_booked_net(monkeypatch) -> None:
    """Book every trade Rs 1 better than its fills say."""
    real = positions_mod.build_trade_row

    def padded(*a, **k):
        row = dict(real(*a, **k))
        row["gross"] = round(row["gross"] + 1.0, 2)
        row["net"] = round(row["net"] + 1.0, 2)
        return row

    monkeypatch.setattr(positions_mod, "build_trade_row", padded)


def _flatten_and_settle(r: ScenarioRunner, st: dict) -> None:
    r.command("FLATTEN_ALL", confirm="FLATTEN")
    wait_for(r, lambda: r.rows("SELECT state FROM structures WHERE structure_id = ?",
                               (st["structure_id"],))[0]["state"] in TERMINAL_STRUCT, 30, "the flatten")
    r.run(12, dt=0.25)


@pytest.mark.parametrize("runner", ["PAPER"], indirect=True)
def test_checker_catches_a_booked_net_that_is_not_net_pnl(runner, monkeypatch):
    r = runner
    _checked(r)
    _pad_booked_net(monkeypatch)
    _, st = enter(r)
    with pytest.raises(InvariantFailure, match="invariant 6"):
        _flatten_and_settle(r, st)


@pytest.mark.parametrize("runner", ["PAPER"], indirect=True)
def test_checker_catches_a_ledger_the_exchange_does_not_hold(runner, monkeypatch):
    r = runner
    checker = InvariantChecker(r)            # quiescence checks only (invariant 6 would fire first)
    _pad_booked_net(monkeypatch)
    _, st = enter(r)
    _flatten_and_settle(r, st)
    with pytest.raises(InvariantFailure, match="invariant 9"):
        checker.at_quiescence()


@pytest.mark.parametrize("runner", ["PAPER", "LIVE"], indirect=True)
def test_checker_catches_a_position_the_legs_do_not_hold(runner):
    r = runner
    checker = _checked(r)
    inst = r.contract("NSE:INFY")
    _, asks, _ = uni.default_book(inst)
    tag = make_tag(Role.ENTRY, r.acct, r.session, 902, 1)
    if r.acct is Account.LIVE:            # a bot-tagged BUY the engine never placed (an untracked bot position)
        r.kite.place_order(variety="regular", exchange="NFO", tradingsymbol=inst.tradingsymbol,
                           transaction_type="BUY", quantity=inst.lot_size, product="MIS", order_type="LIMIT",
                           price=asks[0].price, validity="IOC", tag=tag)
    else:
        r.proc.paper.place(OrderRequest(r.acct, tag, Role.ENTRY, inst.symbol, inst.tradingsymbol, "NFO", inst.token,
                                        Side.BUY, inst.lot_size, asks[0].price, inst.lot_size, inst.tick))
    r.run(4)
    with pytest.raises(InvariantFailure, match="invariant 2"):
        checker.at_quiescence()


@pytest.mark.parametrize("runner", ["PAPER", "LIVE"], indirect=True)
def test_checker_passes_a_clean_round_trip(runner):
    """The control: one entry, a flatten, quiescence, and every invariant holds."""
    r = runner
    checker = _checked(r)
    _, st = enter(r)
    r.run(3)
    _flatten_and_settle(r, st)
    checker.at_quiescence()
    assert not checker.failures
