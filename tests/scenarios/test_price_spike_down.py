"""Scenarios: price_spike_down, the PAPER put experiment (added 2026-10-12 at the user's request, against brief E1),
on the real signals path and the real core.

1. A sharp fall that holds buys an ATM PE in PAPER:price_spike_down, and its placebo (the gateway's opposite option
   type on the same snapshot, PLACEBO:price_spike_down) is a CE on the same strike and expiry. A sharp rise before it
   triggers nothing (never UP, never flipped) and so starts no cooldown. The strategy starts in PAPER, has its own
   G1 trial, and the status file, the G1 scorecards and the daily report list it like the others. Runs on PAPER and
   on LIVE through the `dual` fixture: on LIVE (the fixture unlocks every registered strategy) the LIVE book buys the
   same PE, and the PAPER:<s> book is its twin.
2. The laptop profile treats it like every strategy: even in LIVE mode no LIVE order leaves the laptop; its PAPER
   twin buys the PE, with the CE placebo.
3. What it changes for the other two strategies (review, 2026-10-12; docs/LAPTOP.md sections 4 and 8,
   TITAN_INSTRUCTIONS §11 and §14). Their code and versions are untouched, but the three share the PAPER account's
   G1 controls: the laptop's Ctrl+C with a put open marks every G1 strategy's session IMPAIRED (with nothing open, or
   from 15:15, nobody's), and a MISMATCH on a put halts every PAPER book and, unacknowledged, impairs every G1
   strategy. And the two spike strategies share no cooldown: one stock can hold price_spike_up's CE and
   price_spike_down's PE, each with its placebo, at once.
"""

from __future__ import annotations

import json

import pytest

from titan2 import g1, laptop, ledger, strategies
from titan2.model import Account, Role, Side
from titan2.settings import G1_IMPAIRED_GAP_S

from fakes import universe as uni
from fakes.runner import ScenarioRunner
from fakes.scenario import route_of, state_of, struct_of, wait_for

pytestmark = pytest.mark.scenario

STRAT = "price_spike_down"
REL = "NSE:RELIANCE"
PAPER_BOOK, PLACEBO_BOOK, LIVE_BOOK = (f"{k}:{STRAT}" for k in ("PAPER", "PLACEBO", "LIVE"))
OPEN_SQL = "SELECT * FROM structures WHERE state NOT IN ('CLOSED', 'CLOSED_EXTERNAL', 'VOID') ORDER BY structure_id"


def _placebos(r: ScenarioRunner) -> list[dict]:
    return r.rows("SELECT * FROM structures WHERE book = ? AND is_placebo = 1 ORDER BY structure_id", (PLACEBO_BOOK,))


def _fall_that_holds(r: ScenarioRunner) -> tuple[float, dict]:
    """A sharp rise (no trigger), then 65 s later a 0.7% fall from the risen price on volume that holds for the 12 s
    sustain: (the risen baseline, the strategy's intents row)."""
    strat = r._runners[STRAT].strategy
    u = r.market.unders[REL]
    r.market.tick(REL, round(u.ltp * 1.009, 2), u.cum_volume + 20_000)         # +0.9% on volume
    r.run(15)
    assert strat._syms[REL].pending is None and strat._syms[REL].cooldown_from is None
    assert r.intents_today() == []
    r.run(50)                                       # both baselines have re-anchored at the risen price
    risen = u.ltp
    r.market.tick(REL, round(risen * 0.993, 2), u.cum_volume + 5_000)          # -0.7% on volume: triggers
    r.run(0.5)
    pend = strat._syms[REL].pending
    assert pend is not None and pend.baseline == risen and r.intents_today() == []
    wait_for(r, lambda: r.intents_today("is_placebo = 0"), 14, "the 12 s sustain to pass")
    (it,) = r.intents_today("is_placebo = 0")
    return risen, it


def _assert_pe_with_ce_placebo(r: ScenarioRunner, it: dict) -> tuple[dict, dict]:
    """The PAPER:<s> entry holds the ATM PE, its placebo the ATM CE of the same strike and expiry, opened together
    from the same snapshot. Returns (the PE leg row, the CE leg row)."""
    def opened() -> bool:
        return state_of(r, it["intent_id"], PAPER_BOOK) == "OPEN" and [p["state"] for p in _placebos(r)] == ["OPEN"]

    wait_for(r, opened, 10, "the PE entry and its CE placebo to fill")
    ps = struct_of(r, it["intent_id"], PAPER_BOOK)
    (pl,) = _placebos(r)
    assert (ps["type"], ps["account"], ps["policy"], ps["is_placebo"], ps["placebo_status"]) == (
        "LONG_PE", "PAPER", "FULL", 0, "OPENED")
    assert (pl["type"], pl["account"], pl["policy"], pl["parent_structure_id"]) == (
        "LONG_CE", "SHADOW", "QUALITY", ps["structure_id"])
    (pe,) = r.rows("SELECT * FROM legs WHERE structure_id = ?", (ps["structure_id"],))
    (ce,) = r.rows("SELECT * FROM legs WHERE structure_id = ?", (pl["structure_id"],))
    assert (pe["opt_type"], ce["opt_type"], pe["underlying"], ce["underlying"]) == ("PE", "CE", REL, REL)
    assert (pe["strike"], pe["expiry"]) == (ce["strike"], ce["expiry"])
    assert (pe["symbol"], ce["symbol"]) == (r.contract(REL, "PE", "ATM").symbol, r.contract(REL, "CE", "ATM").symbol)
    # the placebo's intents row is the parent's mirror image: UP / LONG_CE, same strike rule, trigger and recheck
    (pi,) = r.intents_today("is_placebo = 1")
    assert (pi["strategy"], pi["direction"], pi["structure"], pi["strike_pref"], pi["parent_intent_id"]) == (
        STRAT, "UP", "LONG_CE", "ATM", it["intent_id"])
    assert (pi["trigger_px"], pi["recheck_lo"], pi["recheck_hi"]) == (it["trigger_px"], it["recheck_lo"],
                                                                       it["recheck_hi"])
    (ce_buy,) = r.places(Role.ENTRY, account=Account.SHADOW)
    assert (ce_buy.req.symbol, ce_buy.req.side) == (ce["symbol"], Side.BUY)
    # the strategy never buys a CE of its own
    assert not r.intents_today("structure = 'LONG_CE' AND is_placebo = 0")
    return pe, ce


def test_price_spike_down_buys_a_pe_and_its_placebo_is_a_ce(dual):
    r = dual.runner(start="09:58:00", signals=(STRAT,), warmup_until="09:59:30")
    # registered like the others: PAPER from the engine on a fresh titan2.db (the LIVE run's fixture unlocked it),
    #   with its own G1 trial
    mode = r.store.strategy_mode(STRAT)
    assert mode["version"] == strategies.version(strategies.REGISTRY[STRAT], r.settings)
    assert (mode["mode"], mode["changed_by"]) == (("LIVE", "fixture") if dual.live else ("PAPER", "engine"))
    assert r.store.trial(STRAT, mode["version"])["state"] == ("CERTIFIED" if dual.live else "COLLECTING")

    risen, it = _fall_that_holds(r)
    assert (it["strategy"], it["strategy_version"], it["direction"], it["structure"], it["strike_pref"]) == (
        STRAT, mode["version"], "DOWN", "LONG_PE", "ATM")
    assert it["recheck_lo"] == pytest.approx(it["trigger_px"] * 0.995)
    assert it["recheck_hi"] == pytest.approx(risen * 0.9965)
    assert json.loads(it["reason"])["baseline"] == risen
    pe, ce = _assert_pe_with_ce_placebo(r, it)
    if dual.live:                                   # the LIVE book bought the same PE; PAPER:<s> is its twin
        wait_for(r, lambda: state_of(r, it["intent_id"], LIVE_BOOK) == "OPEN", 10, "the LIVE PE entry")
        assert struct_of(r, it["intent_id"], LIVE_BOOK)["type"] == "LONG_PE"
    (pe_buy,) = r.places(Role.ENTRY)                # the account under test: PAPER, or LIVE
    assert (pe_buy.req.symbol, pe_buy.req.side) == (pe["symbol"], Side.BUY)

    # listed like every strategy: the status file (the dashboard's Books, Positions and Strategies panels), the G1
    #   scorecards (pub/g1.json, `titan2 g1`) and the daily report (its G1 table and Telegram summary)
    status = r.engine.status_doc()
    assert STRAT in [s["name"] for s in status["strategies"]]
    assert {PAPER_BOOK, PLACEBO_BOOK} <= set(status["books"])
    assert {(PAPER_BOOK, pe["symbol"]), (PLACEBO_BOOK, ce["symbol"])} <= {
        (p["book"], p["symbol"]) for p in status["positions"]}
    assert {c["strategy"] for c in g1.scorecards(r.store)} == set(strategies.REGISTRY)
    report = ledger.daily_report(r.store, r.session, r.clock.now_wall())
    assert STRAT in {g["strategy"] for g in report.data["g1"]}
    assert f"| {STRAT} |" in report.markdown and f"G1 {STRAT} " in report.telegram


def test_price_spike_down_on_the_laptop_is_paper_only(tmp_path):
    """The laptop profile (allow_live False, no LIVE order of any role) treats price_spike_down like every strategy:
    in LIVE mode (as EC2's titan2.db might say) its LIVE book is refused and no LIVE order leaves, while its PAPER
    twin buys the PE and the placebo buys the CE."""
    r = ScenarioRunner(tmp_path / "run", Account.LIVE, start="09:58:00", signals=(STRAT,), warmup_until="09:59:30",
                       laptop=True)
    try:
        assert r.store.strategy_mode(STRAT)["mode"] == "LIVE" and r.allow_live is False
        _risen, it = _fall_that_holds(r)
        pe, _ce = _assert_pe_with_ce_placebo(r, it)
        route = route_of(r, it["intent_id"], LIVE_BOOK)
        assert (route["outcome"], route["gate"], route["detail"]) == ("REFUSED", "LIVE", "ALLOW_LIVE_OFF"), route
        assert route_of(r, it["intent_id"], PAPER_BOOK)["outcome"] == "RESERVED"
        assert r.places(account=Account.LIVE) == [] and r.orders(account=Account.LIVE) == []
        assert not r.kite.api_orders()
        (pe_buy,) = r.places(Role.ENTRY, account=Account.PAPER)
        assert (pe_buy.req.symbol, pe_buy.req.side) == (pe["symbol"], Side.BUY)
    finally:
        r.close()


# ---------------------------------------------------------------------------------------------------------------
# 3. What it changes for the other two strategies (review, 2026-10-12)
# ---------------------------------------------------------------------------------------------------------------
EVERY_G1_STRATEGY = tuple(sorted(strategies.REGISTRY))           # all three run in PAPER from the first session


def _impaired(r: ScenarioRunner) -> list[tuple[str, str]]:
    """(strategy, detail) of today's IMPAIRED session flags, by strategy."""
    return sorted((f["strategy"], f["detail"]) for f in r.rows(
        "SELECT strategy, detail FROM session_flags WHERE flag = 'IMPAIRED' AND session = ?",
        (r.session.isoformat(),)))


def _laptop_ctrl_c(r: ScenarioRunner) -> None:
    """The laptop's first Ctrl+C: the FLATTEN_ALL its Supervisor writes (by laptop.REQUESTED_BY), and its halt."""
    r.command("FLATTEN_ALL", confirm="FLATTEN", by=laptop.REQUESTED_BY)
    wait_for(r, lambda: bool(r.halts("FLATTEN", "GLOBAL")), 5, "the Ctrl+C FLATTEN halt")
    r.run(5)


def _late_laptop_session(tmp_path, name: str) -> ScenarioRunner:
    r = ScenarioRunner(tmp_path / name, Account.PAPER, start="14:50:00", signals=(STRAT,), warmup_until="14:51:30",
                       laptop=True)
    assert {m["strategy"]: m["mode"] for m in r.store.strategy_modes()} == dict.fromkeys(EVERY_G1_STRATEGY, "PAPER")
    return r


def test_a_laptop_ctrl_c_with_a_put_open_impairs_every_g1_strategy(tmp_path):
    """The three strategies share the PAPER account's G1 controls. The laptop's first Ctrl+C at 14:58, when nobody can
    enter any more, with a price_spike_down PE and its CE placebo open, marks the session IMPAIRED for every G1
    strategy: opening_drive and price_spike_up hold nothing, yet their looks count that session. The same Ctrl+C with
    nothing open marks nobody (a stop is the heartbeat gap's to judge), and from the 15:15 EOD flatten on it marks
    nobody even with the put still open (here the PE has no bids left, so the EOD exit cannot fill it)."""
    r = _late_laptop_session(tmp_path, "open")
    try:
        _risen, it = _fall_that_holds(r)
        _assert_pe_with_ce_placebo(r, it)
        r.run_until("14:58:00", dt=1.0)
        assert {x["book"] for x in r.rows(OPEN_SQL)} == {PAPER_BOOK, PLACEBO_BOOK} and _impaired(r) == []
        _laptop_ctrl_c(r)
        assert _impaired(r) == [(s, "operator halt FLATTEN") for s in EVERY_G1_STRATEGY]
    finally:
        r.close()

    r = _late_laptop_session(tmp_path, "flat")
    try:
        r.run_until("14:58:00", dt=1.0)
        assert r.rows(OPEN_SQL) == []
        _laptop_ctrl_c(r)
        assert _impaired(r) == []
    finally:
        r.close()

    r = _late_laptop_session(tmp_path, "after_eod")
    try:
        _risen, it = _fall_that_holds(r)
        pe, _ce = _assert_pe_with_ce_placebo(r, it)
        r.market.depth(pe["symbol"], (), r.market.book(pe["symbol"]).asks, at="15:00:00")    # no bid from 15:00
        r.run_until("15:16:00", dt=1.0)
        assert "FLATTEN" in {e["payload"].get("step") for e in r.events("PHASE")}             # the EOD flatten ran
        assert state_of(r, it["intent_id"], PAPER_BOOK) not in ("CLOSED", "CLOSED_EXTERNAL", "VOID")
        _laptop_ctrl_c(r)
        assert _impaired(r) == []
    finally:
        r.close()


def test_a_mismatch_on_a_put_halts_and_impairs_every_g1_strategy(tmp_path):
    """A reconciliation finding on a price_spike_down PE is the whole PAPER account's. A close outside the bot books
    CLOSED_EXTERNAL and raises HALT(MISMATCH, ACCOUNT:PAPER), cleared only by an ACK: it refuses price_spike_up's next
    PAPER entry, and left for more than G1_IMPAIRED_GAP_S of the entry window it marks every G1 strategy's session
    IMPAIRED. As a mismatch it counts against the books that traded the PE (g1.count_mismatches attributes by book
    or traded symbol), so against price_spike_down's and not against price_spike_up's, which never bought it."""
    r = ScenarioRunner(tmp_path / "run", Account.PAPER, start="09:58:00", signals=(STRAT,), warmup_until="09:59:30")
    try:
        _risen, it = _fall_that_holds(r)
        pe, _ce = _assert_pe_with_ce_placebo(r, it)
        r.faults.external_close(pe["symbol"], at=r.clock.now_wall(), px=r.market.book(pe["symbol"]).bids[0].price)
        wait_for(r, lambda: bool(r.halts("MISMATCH", "ACCOUNT:PAPER")), 30, "the MISMATCH halt")
        assert state_of(r, it["intent_id"], PAPER_BOOK) == "CLOSED_EXTERNAL"
        up = r.intent(REL, "UP", strategy="price_spike_up")
        route = route_of(r, up, "PAPER:price_spike_up")
        assert (route["outcome"], route["gate"]) == ("REFUSED", "HALTS"), route
        r.run(G1_IMPAIRED_GAP_S - 90, dt=1.0)
        assert _impaired(r) == []
        r.run(180, dt=1.0)
        flags = _impaired(r)
        assert [s for s, _ in flags] == list(EVERY_G1_STRATEGY), flags
        assert all(d.startswith("MISMATCH [ACCOUNT:PAPER] (ACK) blocked PAPER entries") for _, d in flags), flags
        day = r.session.isoformat()
        assert g1.count_mismatches(r.store, PAPER_BOOK, "PAPER", day, day) >= 1
        assert g1.count_mismatches(r.store, "PAPER:price_spike_up", "PAPER", day, day) == 0
    finally:
        r.close()


def test_one_stock_holds_both_spike_strategies_trades_at_once(tmp_path):
    """The two spike strategies share no cooldown (v1's watcher had one per stock): a jump that holds buys
    price_spike_up's CE, with its PE placebo, and the drop back 65 s later, inside price_spike_up's 90 s cooldown, buys
    price_spike_down's PE, with its CE placebo, on the same stock: four structures on one underlying, one per book.
    (The harness's option books do not reprice with the stock, so the contracts at the risen ATM strike are quoted
    for the risen price first, as the market would quote them.)"""
    r = ScenarioRunner(tmp_path / "run", Account.PAPER, start="09:58:00", signals=("price_spike_up", STRAT),
                       warmup_until="09:59:30")
    try:
        u = r.market.unders[REL]
        risen = round(u.ltp * 1.009, 2)
        for opt in ("CE", "PE"):
            inst = r.contract(REL, opt, "ATM", spot=risen)
            bids, asks, ltp = uni.default_book(inst, spot=risen)
            r.market.depth(inst.symbol, bids, asks, ltp=ltp)
        r.market.tick(REL, risen, u.cum_volume + 20_000)                         # +0.9% on volume
        wait_for(r, lambda: r.intents_today("strategy = 'price_spike_up' AND is_placebo = 0"), 15, "the CE intent")
        r.run(65)                                                                 # the baselines re-anchor
        up = r._runners["price_spike_up"].strategy._syms[REL]                    # price_spike_up is cooling down
        assert r.clock.mono() - up.cooldown_from < strategies.REGISTRY["price_spike_up"].PARAMS["cooldown_s"]
        r.market.tick(REL, round(risen * 0.993, 2), u.cum_volume + 5_000)        # -0.7% on volume
        wait_for(r, lambda: r.intents_today("strategy = ? AND is_placebo = 0", (STRAT,)), 15, "the PE intent")
        r.run(10)
        held = {(x["book"], x["type"]) for x in r.rows(OPEN_SQL) if x["underlying"] == REL}
        assert held == {("PAPER:price_spike_up", "LONG_CE"), ("PLACEBO:price_spike_up", "LONG_PE"),
                        (PAPER_BOOK, "LONG_PE"), (PLACEBO_BOOK, "LONG_CE")}, held
    finally:
        r.close()
