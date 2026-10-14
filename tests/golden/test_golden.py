"""U21 golden tests: titan2 against v1 behaviour captured at v1-final (commit b4694f3); spec §15 step 4, DETAIL §16.7.

fixtures/*.json are written by capture_v1.py, which runs the v1 code itself (cut out of the v1 source by AST, never
imported) on fixed scenarios:
  select_strike  options_trader.py 3497-3550 OptionChainFetcher.select_strike
                 -> titan2.instruments.pick_strike and InstrumentStore.select_contract / opposite
  depth_walk     zerodha_tools.py 4633-4657 (_check_option_liquidity) and dashboard.py 4255-4270 (pnl_live)
                 -> titan2.model.walk and titan2.core.exits.realizable_bid
  ratchet        exit_manager.py 445-471 _check_hard_sl, 1321-1330 _apply_breakeven, 1332-1406 _apply_trailing_stop
                 -> titan2.core.exits.decide (rules 4-6)
  spike          kite_ticker.py 1039-2448 BreakoutWatcher (process_tick, _fire_trigger)
                 -> titan2.strategies.price_spike_up.PriceSpikeUp, driven through StrategyRunner as the host does
                 (and its mirror price_spike_down.PriceSpikeDown, 2026-10-12, against v1's PRICE_SPIKE_DOWN fire)

Every case carries its author's expectation: "SAME", or the id of an intended difference. Each test derives the
relation it observes from the v1 and titan2 outputs (with the evidence each difference claims) and asserts that it
equals the expectation. So a divergence is either documented in INTENDED_DIFFERENCES below, with its reason and spec
section, or the test fails; and every documented difference must be exercised by at least one case.

Re-run the v1 capture and compare (needs git history or a v1-final worktree):
    TITAN2_GOLDEN_RECAPTURE=1 python -m pytest tests/golden -q -p no:cacheprovider
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

import pytest

from titan2.charges import OrderFills, RateSet, order_charges, rates_for
from titan2.clock import SessionCalendar, ist_datetime
from titan2.core import exits
from titan2.core.exits import STOP_BREACH_PENDING, EodTimes, compute_exit_params, decide, realizable_bid
from titan2.instruments import InstrumentStore, pick_strike
from titan2.model import (
    Account, Direction, ExitParams, ExitProfile, Instrument, Intent, Leg, LegState, Level, Order, OrderState, Quote,
    Refusal, Role, Side, StrikePref, StructureType, Underlying, UnderlyingTick, make_tag, walk,
)
from titan2.settings import DEFAULTS, THIN_BOOK_PENALTY
from titan2.strategies.base import StrategyRunner, in_window, ist_tod, parse_tod
from titan2.strategies.price_spike_down import PriceSpikeDown
from titan2.strategies.price_spike_up import PriceSpikeUp

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
AREAS = ("select_strike", "depth_walk", "ratchet", "spike")
SESSION = date(2026, 10, 12)          # the capture's session (a Monday)
SAME = "SAME"

V1_FINAL = "b4694f3"                  # the commit tagged v1-final (spec §15); capture_v1.py pins the same blobs
V1_FINAL_BLOBS = {
    "options_trader.py": "45eab1b6eb822a0c6da67c300fa44b093f0c3143",
    "zerodha_tools.py": "91d327b195c9d08ed68bbb0604abf8e2edfbf12b",
    "dashboard.py": "811ef481ce7b38532ebfa2ae89458cc485ca9e84",
    "exit_manager.py": "034e34964817bc5b3475ec82e5423d97755e4ec9",
    "kite_ticker.py": "f9280cae71b2b4bde71d9576318a2d6be9812996",
    "config.py": "606a4ed7d4bc916f4fcf326e3a6a0bd0cf2e9043",
    "settings_manager.py": "cb031250fad783934f82da0f1086cf7be82c46a0",
    "titan_settings.json": "634ff2dbb06f7afe160f473212811af739e545c3",
}

# id -> (area, why titan2 differs from v1 on purpose)
INTENDED_DIFFERENCES = {
    "D_ITM1_EDGE_REFUSED": (
        "select_strike",
        "No ITM1 neighbour (CE at the lowest listed strike, PE at the highest): v1 clamped the index "
        "(options_trader 3529-3531) and silently bought the ATM strike as 'ITM_1'; titan2 refuses with CONTRACT "
        "NO_ITM1_NEIGHBOUR (spec §11.5: a missing neighbour is a Refusal, no clamping)."),
    "D_SPOT_OUTSIDE_REFUSED": (
        "select_strike",
        "Spot half a strike step or more beyond the listed strikes: v1 bought the edge strike, which is not at the "
        "money; titan2 refuses with CONTRACT SPOT_OUTSIDE_STRIKES (§11.5: a missing strike is a Refusal)."),
    "D_ATM_TIE_PE_MORE_ITM": (
        "select_strike",
        "Spot exactly midway between two strikes: v1's min() kept the lower strike for both types, the "
        "out-of-the-money one for a PE; titan2 breaks the tie toward the more in-the-money strike: lower for CE "
        "(as v1), higher for PE (§11.5). ITM1 then moves with the ATM."),
    "D_WALK_95PCT_RULE": (
        "depth_walk",
        "The shown bids cover 95% to 100% of the quantity: v1 priced the exit at the average of the covered part, as "
        "if the rest sold there too (zerodha_tools 4649, dashboard 4269); titan2 values the uncovered rest at the "
        "deepest bid x (1 - THIN_BOOK_PENALTY) and flags the book thin (§7.3), so its realizable bid is lower."),
    "D_WALK_NO_LTP_FALLBACK": (
        "depth_walk",
        "The shown bids cover under 95% of the quantity, or nothing: v1's dashboard showed the LTP as the "
        "realizable price (dashboard 4248); titan2 never decides on the LTP: a thin book is priced by the haircut "
        "walk and an empty bid side gives no realizable bid, which decide() handles as NO_BID (§7.3, §7.4 rule 3)."),
    "D_RATCHET_ARMS_LATER": (
        "ratchet",
        "v1 started trailing at 0.22R (+3.3% with a 15% stop), where its stop at entry + 70% of the gain could still "
        "sit inside the round-trip charges; titan2 keeps the hard stop until the realizable bid reaches the arm "
        "level BE + lock_arm_pct x E (+4%) (§7.1 X5)."),
    "D_RATCHET_FLOOR": (
        "ratchet",
        "Just after arming, titan2's stop is the floor BE + lock_floor_pct x E (+3%) while 70% of the gain is still "
        "below it, so a locked exit nets about 3% after charges; v1's stop was the bare 70% trail (§7.4 rule 5)."),
    "D_RATCHET_BE_ANCHOR": (
        "ratchet",
        "With real charges titan2 trails from the breakeven price, stop = BE + 0.70 x (peak - BE), where v1 trailed "
        "from the entry price (exit_manager 1372-1374): the stop sits 0.30 x (BE - E) higher, so a trail exit keeps "
        "70% of the net gain, not of the gross (§7.1). With BE = E the trails coincide (test_ratchet_be_eq_entry)."),
    "D_STOP_DEBOUNCE": (
        "ratchet",
        "A stop touched by less than stop_confirm_bypass_pct (3%) of E: v1 exited on the first price at or below its "
        "SL; titan2 confirms the breach with a distinct later snapshot at least stop_confirm_ms (400 ms) after the "
        "first (§7.4 rule 6), one tick later in these 1-second paths. A breach 3% or deeper exits on the same tick."),
    "D_SPIKE_SIGNED_SUSTAIN": (
        "spike",
        "An up-spike that fell back through its baseline: v1's sustain measured abs() (kite_ticker 1707, 1726), so a "
        "stock 0.4% BELOW its baseline 'held' the 0.35% bar and v1 queued a CE buy into a falling stock (brief §4.1 "
        "#9); titan2's held move is signed and must stay above 0 (§10.4 step 6)."),
    "D_SPIKE_UP_ONLY": (
        "spike",
        "A down-spike: v1 queued PRICE_SPIKE_DOWN (a PE trade); price_spike_up never emits DOWN (§10.4, E1), "
        "because m = max(mA, mB) is signed."),
    "D_SPIKE_WINDOW": (
        "spike",
        "v1's watcher detected and queued at any time of day (no window gate in process_tick or _fire_trigger, "
        "kite_ticker 2284); titan2 detects only inside the strategy WINDOW 09:20-14:55 and queues nothing outside "
        "it (§10.4 step 3)."),
    "D_SPIKE_COOLDOWN_ON_FAIL": (
        "spike",
        "v1 started the 90 s cooldown only when a trigger was queued (kite_ticker 2332); titan2 starts it on a "
        "failed sustain too, so a new spike within 90 s of a failed one is ignored (§10.4 step 6: pass or fail)."),
    "D_SPIKE_PEAK_FROM_TRIGGER": (
        "spike",
        "v1's retrace peak counted only the ticks after the trigger tick (kite_ticker 1709-1711), so a spike that "
        "topped out on the trigger tick was never judged on its retrace; titan2's peak starts at the trigger move m "
        "(§10.4 step 5), so falling back more than 65% from the trigger tick fails."),
    "D_SPIKE_NO_ROUNDING": (
        "spike",
        "v1 rounded the held move to 0.1% before comparing it (kite_ticker 1726), so 0.96% passed the 1.0% "
        "early-market bar; titan2 compares the unrounded move (§10.4 step 6)."),
}


@lru_cache(maxsize=None)
def fixture(area: str) -> dict:
    return json.loads((FIXTURES / f"{area}.json").read_text(encoding="utf-8"))


def _unexplained(what: str) -> str:
    return f"UNEXPLAINED: {what}"


# ---------------------------------------------------------------------------------------------------------------
# Strike selection
# ---------------------------------------------------------------------------------------------------------------
PREF = {"ATM": StrikePref.ATM, "ITM1": StrikePref.ITM1}
STORE_UNDERLYING = {"STOCK_STEP50": "NSE:GSTEP", "STOCK_MIXED_STEP": "NSE:GMIXED", "NIFTY_STEP50": "NSE:NIFTY 50"}


def _strikes(case: dict) -> list[float]:
    return sorted(fixture("select_strike")["chains"][case["chain"]])


def classify_strike(case: dict, got: float | Refusal) -> str:
    """How titan2's pick relates to v1's, with the evidence each intended difference claims."""
    ks, spot, v1 = _strikes(case), case["spot"], case["v1"]["strike"]
    if not isinstance(got, Refusal):
        if got == v1:
            return SAME
        midway = any(abs((a + b) / 2 - spot) < 1e-9 for a, b in zip(ks, ks[1:]))
        if case["opt_type"] == "PE" and midway and v1 in ks[:-1] and got == ks[ks.index(v1) + 1]:
            return "D_ATM_TIE_PE_MORE_ITM"
        return _unexplained(f"v1 {v1}, titan2 {got}")
    edge = ks[0] if case["opt_type"] == "CE" else ks[-1]
    if (got.detail.startswith("NO_ITM1_NEIGHBOUR") and case["pref"] == "ITM1"
            and v1 == case["v1"]["atm_strike"] == edge):
        return "D_ITM1_EDGE_REFUSED"            # v1 clamped to the edge and bought ATM as "ITM_1"
    outside = spot <= ks[0] - (ks[1] - ks[0]) / 2 or spot >= ks[-1] + (ks[-1] - ks[-2]) / 2
    if got.detail.startswith("SPOT_OUTSIDE_STRIKES") and outside and v1 in ks:
        return "D_SPOT_OUTSIDE_REFUSED"
    return _unexplained(f"v1 {v1}, titan2 {got}")


@lru_cache(maxsize=1)
def golden_store() -> InstrumentStore:
    """Today's dump holding the golden chains (lot and tick from each row), plus the two index names it requires."""
    chains = fixture("select_strike")["chains"]
    tokens = itertools.count(9_100_001)
    contracts: list[Instrument] = []

    def chain(name: str, underlying: str, expiries: tuple[date, ...], strikes: list[float], lot: int) -> None:
        for e in expiries:
            for k in strikes:
                for opt in ("CE", "PE"):
                    ts = f"{name}{e:%y%m%d}{k:g}{opt}"
                    contracts.append(Instrument("NFO:" + ts, next(tokens), ts, "NFO", name, underlying, opt,
                                                float(k), e, lot, 0.05))

    chain("NIFTY", "NSE:NIFTY 50", (date(2026, 10, 13), date(2026, 10, 27)), chains["NIFTY_STEP50"], 65)
    chain("BANKNIFTY", "NSE:NIFTY BANK", (date(2026, 10, 27), date(2026, 11, 23)), [55000.0, 55100.0], 30)
    chain("GSTEP", "NSE:GSTEP", (date(2026, 10, 27),), chains["STOCK_STEP50"], 250)
    chain("GMIXED", "NSE:GMIXED", (date(2026, 10, 27),), chains["STOCK_MIXED_STEP"], 3000)
    unders = [Underlying("NSE:NIFTY 50", 256265, "NIFTY", "INDEX"),
              Underlying("NSE:NIFTY BANK", 260105, "BANKNIFTY", "INDEX"),
              Underlying("NSE:GSTEP", 9_000_001, "GSTEP", "STOCK"),
              Underlying("NSE:GMIXED", 9_000_002, "GMIXED", "STOCK")]
    return InstrumentStore(SESSION, SessionCalendar.load(), contracts, unders, min_stocks=2)


def _select(case: dict) -> Instrument | Refusal:
    return golden_store().select_contract(STORE_UNDERLYING[case["chain"]], case["opt_type"], PREF[case["pref"]],
                                          case["spot"], SESSION)


# ---------------------------------------------------------------------------------------------------------------
# Depth walk
# ---------------------------------------------------------------------------------------------------------------
WALK_SYMBOL = "NFO:GOLDWALK26OCT100CE"


def _levels(case: dict) -> tuple[Level, ...]:
    return tuple(Level(p, q, o) for p, q, o in case["book"])     # Kite rows, zero padding included


def _bid_quote(case: dict, ltp: float) -> Quote:
    wall = ist_datetime(SESSION, "10:00:00")
    return Quote(WALK_SYMBOL, 4244, _levels(case), (), ltp, None, None, None, 0.0, wall, "WS")


def classify_rb(case: dict) -> str:
    """How titan2's realizable bid relates to the realizable price v1's dashboard showed."""
    qty, v1 = case["qty"], case["v1"]["dashboard"]
    filled, weighted, shown = v1["filled"], v1["weighted"], v1["realizable"]
    rb, thin = realizable_bid(_bid_quote(case, case["ltp"]), qty)
    if filled == qty:
        return SAME if not thin and rb == pytest.approx(shown, rel=1e-12) else _unexplained(f"rb {rb} vs {shown}")
    worst = walk(_levels(case), qty, None, Side.SELL).worst_px
    if filled >= 0.95 * qty:
        # v1 showed the covered part's average; titan2 haircuts the uncovered rest, so it shows less
        haircut = (weighted + (qty - filled) * worst * (1 - THIN_BOOK_PENALTY)) / qty
        v1_covered_avg = shown == pytest.approx(weighted / filled, rel=1e-12)
        if v1_covered_avg and thin and rb == pytest.approx(haircut, rel=1e-12) and rb < shown:
            return "D_WALK_95PCT_RULE"
        return _unexplained(f"rb {rb} thin {thin} vs {shown}")
    # v1 showed the LTP; titan2's answer must not move with the LTP at all
    other = realizable_bid(_bid_quote(case, case["ltp"] * 3), qty)
    if shown == case["ltp"] and other == (rb, thin) and (rb is None) == (filled == 0):
        return "D_WALK_NO_LTP_FALLBACK"
    return _unexplained(f"rb {rb} thin {thin} vs {shown}")


# ---------------------------------------------------------------------------------------------------------------
# Ratchet
# ---------------------------------------------------------------------------------------------------------------
ZERO_RATES = RateSet("golden-zero", date(2000, 1, 1), 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
EOD = EodTimes.for_session(SESSION, DEFAULTS)
STOP_TOL = 0.005 + 1e-9               # v1 rounded its stop to 2 dp


def run_titan2_ratchet(fx: dict, path: dict) -> tuple[ExitParams, list[tuple]]:
    """decide() on each tick of the path as the engine runs it: one OPEN leg, marks applied after every call.
    Returns the frozen levels and per tick (decision, stop_px, lock_armed, peak_rb), up to titan2's exit."""
    E, Q = fx["entry"], fx["qty"]
    rates = ZERO_RATES if path["charges"] == "zero" else rates_for(SESSION)
    start = ist_datetime(SESSION, fx["start"])
    c_in = order_charges(OrderFills(Side.BUY, Q, E * Q), rates).total
    p = compute_exit_params(ExitProfile(), E, Q, c_in, rates, start)
    inst = Instrument(fx["symbol"], 4242, fx["symbol"][4:], "NFO", "GOLDSTK", "NSE:GOLDSTK", "CE", 2500.0,
                      date(2026, 10, 27), Q, 0.05)
    pk = make_tag(Role.ENTRY, Account.PAPER, SESSION, 1, 1)
    order = Order(order_pk=pk, leg_id=1, structure_id=1, account=Account.PAPER, book="PAPER:golden", role=Role.ENTRY,
                  side=Side.BUY, symbol=inst.symbol, qty=Q, limit_px=E, attempt=1, band_idx=None,
                  state=OrderState.COMPLETE, filled_qty=Q, avg_fill_px=E)
    leg = Leg(leg_id=1, structure_id=1, leg_no=1, account=Account.PAPER, book="PAPER:golden", session=SESSION,
              seq_no=1, inst=inst, state=LegState.OPEN, orders=[pk], stop_px=p.hard_sl_px,
              entry_fill_wall=p.entry_fill_wall).bind_orders({pk: order})
    steps = []
    for i, (px, _label) in enumerate(path["ticks"]):
        mono, wall = 1000.0 + i * fx["step_s"], start + timedelta(seconds=i * fx["step_s"])
        q = Quote(inst.symbol, inst.token, ((px, 100 * Q),), ((round(px + 0.05, 2), 100 * Q),), px, None, wall,
                  None, mono, wall, "WS")
        d = decide(leg, q, None, mono, wall, p, EOD, None)
        d.marks.apply_to(leg)
        steps.append((d, leg.stop_px, leg.lock_armed, leg.peak_rb))
        if isinstance(d, exits.Exit):
            break
    return p, steps


def classify_stop(p: ExitParams, v1_sl: float, v1_trailing: bool, stop: float, armed: bool, peak: float) -> str:
    """How titan2's stop after a tick relates to v1's SL after the same tick."""
    trail = p.be_px + p.profile.trail_retain * (peak - p.be_px)
    if not armed:
        if stop != p.hard_sl_px:
            return _unexplained(f"unarmed stop {stop} is not the hard stop {p.hard_sl_px}")
        if not v1_trailing:
            return SAME if abs(stop - v1_sl) <= STOP_TOL else _unexplained(f"hard stops {stop} vs {v1_sl}")
        return "D_RATCHET_ARMS_LATER" if v1_sl > stop else _unexplained(f"v1 trails at {v1_sl} below {stop}")
    if not v1_trailing:
        return _unexplained("titan2 armed before v1 started to trail")
    if abs(stop - p.floor_px) <= 1e-9 and stop > trail:
        return "D_RATCHET_FLOOR" if stop > v1_sl else _unexplained(f"floor {stop} under v1 {v1_sl}")
    if abs(stop - trail) <= 1e-9:
        if abs(stop - v1_sl) <= STOP_TOL:
            return SAME
        anchor = (1 - p.profile.trail_retain) * (p.be_px - p.entry_avg)
        if anchor > 0 and abs(stop - v1_sl - anchor) <= STOP_TOL:
            return "D_RATCHET_BE_ANCHOR"
    return _unexplained(f"stop {stop}, trail {trail}, v1 {v1_sl}")


def classify_exit(path: dict, steps: list[tuple]) -> str:
    k = path["v1"]["exit_tick"]
    j = next((i for i, s in enumerate(steps) if isinstance(s[0], exits.Exit)), None)
    if k == j:
        if k is None:
            return SAME
        reason = steps[j][0].reason
        ok = path["v1"]["exit_type"] == "SL_HIT" and reason == ("LOCK_STOP" if steps[j][2] else "HARD_SL")
        return SAME if ok else _unexplained(f"v1 {path['v1']['exit_type']}, titan2 {reason}")
    if k is not None and j is not None and j > k and STOP_BREACH_PENDING in steps[k][0].notes:
        return "D_STOP_DEBOUNCE"
    return _unexplained(f"v1 exits at tick {k}, titan2 at tick {j}")


def check_ratchet_path(fx: dict, path: dict) -> ExitParams:
    p, steps = run_titan2_ratchet(fx, path)
    v1 = path["v1"]
    for i, (px, label) in enumerate(path["ticks"]):
        if label is None:                  # v1 has already exited
            continue
        assert i < len(steps), f"{path['id']}: titan2 exited before tick {i}"
        _, stop, armed, peak = steps[i]
        got = classify_stop(p, v1["sl"][i], v1["trailing"][i], stop, armed, peak)
        assert got == label, f"{path['id']} tick {i} @ {px}: {got} (v1 SL {v1['sl'][i]}, titan2 stop {stop})"
    assert classify_exit(path, steps) == path["expect_exit"], path["id"]
    return p


# ---------------------------------------------------------------------------------------------------------------
# Spike detection
# ---------------------------------------------------------------------------------------------------------------
class OneStockView:
    """MarketView with one eligible stock (PriceSpikeUp reads only universe())."""

    def __init__(self, symbol: str) -> None:
        self._universe = frozenset({symbol})

    def universe(self) -> frozenset[str]:
        return self._universe

    def ltp(self, underlying: str) -> None:
        return None

    def last_tick(self, underlying: str) -> None:
        return None

    def bars(self, underlying: str, n: int) -> tuple:
        return ()

    def prev_session_bars(self, underlying: str, n: int) -> tuple:
        return ()

    def feed_ok(self) -> bool:
        return True

    def now_mono(self) -> float:
        return 0.0


def _scenario(sid: str) -> dict:
    (sc,) = [s for s in fixture("spike")["scenarios"] if s["id"] == sid]
    return sc


def _tick_wall(sc: dict, tick: int) -> datetime:
    return ist_datetime(SESSION, sc["start"]) + timedelta(seconds=sc["ticks"][tick][0])


def run_titan2_spike(sc: dict, strategy_cls: type = PriceSpikeUp) -> list[tuple[int, Intent]]:
    """The scenario's ticks through StrategyRunner(strategy_cls), PriceSpikeUp by default: [(tick index, intent)]."""
    symbol = fixture("spike")["symbol"]
    runner = StrategyRunner(strategy_cls("1.golden"))
    view = OneStockView(symbol)
    out: list[tuple[int, Intent]] = []
    for i, (t, ltp, cum) in enumerate(sc["ticks"]):
        tick = UnderlyingTick(symbol, 4243, float(ltp), int(cum), None, None, None, 1000.0 + t, _tick_wall(sc, i))
        res = runner.on_tick(tick, view)
        assert res.error is None and not res.dropped, (sc["id"], i, res)
        out.extend((i, intent) for intent in res.intents)
    return out


def _held(fire: dict) -> float:
    return (fire["ltp"] - fire["baseline"]) / fire["baseline"] * 100.0


def _same_fire(fire: dict, intent: Intent) -> bool:
    """titan2's intent repeats v1's fire: same tick (the caller pairs them), price, baseline and moves. Only spec
    fields are read (§10.4): the baseline through Recheck(lo = baseline x (1 + hold_pct), hi = ltp x 1.005)."""
    p, r = PriceSpikeUp.PARAMS, intent.reason
    return (fire["type"] == "PRICE_SPIKE_UP" and intent.direction is Direction.UP
            and intent.strike_pref is StrikePref.ATM and intent.trigger_px == fire["ltp"]
            and intent.recheck.lo == pytest.approx(fire["baseline"] * (1 + p["hold_pct"] / 100), rel=1e-12)
            and intent.recheck.hi == pytest.approx(fire["ltp"] * (1 + p["recheck_chase_pct"] / 100), rel=1e-12)
            and abs(r["m"] - fire["move_pct"]) <= 1e-4 and abs(r["held"] - _held(fire)) <= 1e-4)


def _why_v1_only(sc: dict, fire: dict) -> str:
    """Which intended difference explains a v1 fire that titan2 does not repeat; the evidence is checked."""
    p = PriceSpikeUp.PARAMS
    tod = ist_tod(_tick_wall(sc, fire["tick"]))
    held = _held(fire)
    if fire["type"] == "PRICE_SPIKE_DOWN":
        return "D_SPIKE_UP_ONLY"
    if fire["type"] != "PRICE_SPIKE_UP":
        return _unexplained(f"v1 trigger type {fire['type']}")
    if not in_window(tod, (parse_tod(p["window"][0]), parse_tod(p["window"][1]))):
        return "D_SPIKE_WINDOW"
    if held <= 0:
        return "D_SPIKE_SIGNED_SUSTAIN"
    detect = [d for d in sc["v1"]["detections"] if d["tick"] < fire["tick"]][-1]
    failed = [s for s in sc["v1"]["sustains"] if not s["passed"] and s["tick"] <= detect["tick"]]
    if failed and sc["ticks"][detect["tick"]][0] - sc["ticks"][failed[-1]["tick"]][0] < p["cooldown_s"]:
        return "D_SPIKE_COOLDOWN_ON_FAIL"
    need = max(p["hold_pct"], p["early_min_hold_pct"] if tod < parse_tod(p["early_end"]) else 0.0)
    if held < need <= round(held, 1):
        return "D_SPIKE_NO_ROUNDING"
    v1_peak = fire["peak_pct"]
    retrace_from_trigger = (1 - held / max(detect["move_pct"], v1_peak or 0.0)) * 100
    v1_retrace = (1 - round(held, 1) / v1_peak) * 100 if v1_peak else 0.0
    if retrace_from_trigger > p["retrace_max_pct"] >= v1_retrace:
        return "D_SPIKE_PEAK_FROM_TRIGGER"
    return _unexplained(f"v1 fire at tick {fire['tick']}")


def classify_spike(sc: dict, intents: list[tuple[int, Intent]]) -> str:
    pending = dict(intents)
    labels = set()
    for fire in sc["v1"]["fires"]:
        intent = pending.pop(fire["tick"], None)
        if intent is None:
            labels.add(_why_v1_only(sc, fire))
        elif not _same_fire(fire, intent):
            return _unexplained(f"tick {fire['tick']}: v1 {fire} vs titan2 {intent.trigger_px} {dict(intent.reason)}")
    if pending:
        return _unexplained(f"titan2-only intents at ticks {sorted(pending)}")
    if len(labels) > 1:
        return _unexplained(f"several differences {sorted(labels)}")
    return labels.pop() if labels else SAME


# ---------------------------------------------------------------------------------------------------------------
# Acceptance tests
# ---------------------------------------------------------------------------------------------------------------
def test_select_strike_interior():
    cases = [c for c in fixture("select_strike")["cases"] if c["expect"] == SAME]
    assert len(cases) >= 60
    for c in cases:
        got = pick_strike(_strikes(c), c["spot"], c["opt_type"], PREF[c["pref"]])
        assert classify_strike(c, got) == SAME, c["id"]
        # the gateway's path (today's dump -> eligible expiry -> strike) buys the contract v1 bought
        inst = _select(c)
        assert isinstance(inst, Instrument), (c["id"], inst)
        assert (inst.strike, inst.opt_type) == (c["v1"]["strike"], c["opt_type"]), c["id"]
    # the placebo's opposite() applies the same rule: the CE pick's opposite is v1's PE pick
    by_id = {c["id"]: c for c in cases}
    for c in cases:
        twin = by_id.get(c["id"].replace("/CE/", "/PE/"))
        if c["opt_type"] == "CE" and twin is not None:
            other = golden_store().opposite(_select(c), c["spot"], PREF[c["pref"]])
            assert isinstance(other, Instrument) and other.strike == twin["v1"]["strike"], c["id"]


def test_itm1_edge_documented():
    cases = [c for c in fixture("select_strike")["cases"] if c["expect"] != SAME]
    seen = set()
    for c in cases:
        got = pick_strike(_strikes(c), c["spot"], c["opt_type"], PREF[c["pref"]])
        assert classify_strike(c, got) == c["expect"], (c["id"], got)
        seen.add(c["expect"])
        via = _select(c)                   # the store refuses (or picks) exactly as the pure rule does
        if isinstance(got, Refusal):
            assert isinstance(via, Refusal) and via.gate == got.gate == "CONTRACT", (c["id"], via)
            assert via.detail.startswith(got.detail.split(":")[0]), (c["id"], via)
        else:
            assert isinstance(via, Instrument) and via.strike == got, (c["id"], via)
    assert seen == {"D_ITM1_EDGE_REFUSED", "D_SPOT_OUTSIDE_REFUSED", "D_ATM_TIE_PE_MORE_ITM"}
    # the ITM1 edge on both sides of the chain: v1 bought the ATM strike under the name ITM_1; titan2 refuses
    edge = [c for c in cases if c["expect"] == "D_ITM1_EDGE_REFUSED"]
    assert {c["opt_type"] for c in edge} == {"CE", "PE"}
    assert all(c["pref"] == "ITM1" and c["v1"]["strike"] == c["v1"]["atm_strike"] for c in edge)


def test_depth_walk():
    cases = fixture("depth_walk")["cases"]
    sides = set()
    for c in cases:
        sides.add(c["side"])
        levels, qty, v1 = _levels(c), c["qty"], c["v1"]["tools"]
        w = walk(levels, qty, None, Side(c["side"]))
        # the walk itself is v1's: same quantity, same average, same deepest level touched (zero rows skipped)
        assert w.fillable_qty == v1["filled"], c["id"]
        if v1["filled"]:
            assert w.avg_px == pytest.approx(v1["weighted"] / v1["filled"], rel=1e-12), c["id"]
            shown = [lv for lv in levels if lv.price > 0 and lv.qty > 0]
            assert w.worst_px == shown[v1["levels_crossed"] - 1].price, c["id"]
        else:
            assert (w.avg_px, w.worst_px, v1["levels_crossed"]) == (None, None, 0), c["id"]
        # v1's 95% rule: an average only when the book covered 95% of the quantity; titan2 has no such rule
        assert (v1["avg_fill"] is not None) == (0 < v1["filled"] >= 0.95 * qty), c["id"]
        if c["side"] == "SELL":
            assert classify_rb(c) == c["expect"], c["id"]
        else:
            assert c["expect"] is None and "dashboard" not in c["v1"], c["id"]
    assert sides == {"BUY", "SELL"}
    assert {c["expect"] for c in cases if c["side"] == "SELL"} == {SAME, "D_WALK_95PCT_RULE", "D_WALK_NO_LTP_FALLBACK"}


def test_ratchet_be_eq_entry():
    fx = fixture("ratchet")
    # titan2's default profile is v1's run zone: retain 0.70 of the gain (exit_manager 150) and the 15% stop the
    # v1 SL was set at
    assert ExitProfile().trail_retain == fx["v1_constants"]["trailing_run_pct"]
    assert ExitProfile().hard_sl_pct == fx["sl_pct"] and fx["initial_sl"] == fx["entry"] * (1 - fx["sl_pct"] / 100)
    paths = [p for p in fx["paths"] if p["charges"] == "zero"]
    assert len(paths) >= 4
    both_trailing = 0
    for path in paths:
        p = check_ratchet_path(fx, path)
        assert p.be_px == p.entry_avg                    # zero charges: BE == entry
        labels = [label for _, label in path["ticks"]]
        both_trailing += sum(1 for label, tr in zip(labels, path["v1"]["trailing"]) if label == SAME and tr)
    assert both_trailing >= 10                           # ticks where both trail and agree to the paisa


def test_spike_monotonic_vs_reversal():
    # a clean up-spike that holds: titan2 emits on v1's tick, at v1's price, from v1's baseline and trigger move
    mono = _scenario("S1_monotonic_up")
    (fire,) = mono["v1"]["fires"]
    ((tick, intent),) = run_titan2_spike(mono)
    assert tick == fire["tick"] and intent.trigger_px == fire["ltp"] and intent.direction is Direction.UP
    assert intent.recheck.lo == pytest.approx(fire["baseline"] * 1.0035)        # §10.4: lo = baseline x 1.0035
    assert intent.reason["m"] == pytest.approx(fire["move_pct"], abs=1e-4)
    assert classify_spike(mono, [(tick, intent)]) == SAME
    # the same kind of spike reversing through its baseline: v1's abs() sustain passed it and queued a CE buy on a
    # stock trading below its pre-spike price (brief §4.1 #9); titan2 emits nothing
    rev = _scenario("S2_reversal_through_baseline")
    (fire,) = rev["v1"]["fires"]
    assert fire["type"] == "PRICE_SPIKE_UP" and fire["ltp"] < fire["baseline"]
    assert run_titan2_spike(rev) == []
    assert classify_spike(rev, []) == rev["expect"] == "D_SPIKE_SIGNED_SUSTAIN"


# ---------------------------------------------------------------------------------------------------------------
# Further golden checks
# ---------------------------------------------------------------------------------------------------------------
def test_ratchet_real_charges_anchor_at_be():
    fx = fixture("ratchet")
    paths = [p for p in fx["paths"] if p["charges"] == "real"]
    assert paths
    for path in paths:
        p = check_ratchet_path(fx, path)
        assert p.be_px > p.entry_avg
        assert p.arm_px == pytest.approx(p.be_px + p.profile.lock_arm_pct / 100 * p.entry_avg)
        assert sum(1 for _, label in path["ticks"] if label == "D_RATCHET_BE_ANCHOR") >= 5


def test_spike_scenarios_match_or_documented():
    for sc in fixture("spike")["scenarios"]:
        assert classify_spike(sc, run_titan2_spike(sc)) == sc["expect"], sc["id"]


def test_spike_params_are_v1_effective_values():
    """PriceSpikeUp.PARAMS are the values v1's watcher really ran with (§10.4): construction-time copies of
    config.py (sustain 12 s, cooldown 90 s) plus the JSON value read at run time (retrace 65%)."""
    eff, p = fixture("spike")["v1_effective"], PriceSpikeUp.PARAMS
    for key in ("spike_pct_open", "spike_pct", "sustain_s", "hold_pct", "early_min_hold_pct", "retrace_max_pct",
                "cooldown_s"):
        assert p[key] == eff[key], key
    for key in ("open_until", "early_end"):
        assert parse_tod(p[key]) == parse_tod(eff[key]), key
    assert eff["json_never_applied"] == {"sustain_seconds": 15, "cooldown_seconds": 120}


def test_spike_down_mirror_repeats_v1_down_fire():
    """price_spike_down (2026-10-12, a PAPER experiment against brief E1) on v1's own down-spike tape S3, where
    price_spike_up shows D_SPIKE_UP_ONLY: it fires where v1 queued PRICE_SPIKE_DOWN, on the same tick, at the same
    price, from the same baseline, with the same trigger and held moves (v1's move is signed, its held move abs()).
    On every other v1 tape (up-spikes, flat, dry volume, after the window) it emits nothing."""
    sc = _scenario("S3_spike_down")
    (fire,) = sc["v1"]["fires"]
    assert fire["type"] == "PRICE_SPIKE_DOWN" and sc["expect"] == "D_SPIKE_UP_ONLY" and run_titan2_spike(sc) == []
    ((tick, intent),) = run_titan2_spike(sc, PriceSpikeDown)
    p, r = PriceSpikeDown.PARAMS, intent.reason
    assert tick == fire["tick"] and intent.trigger_px == fire["ltp"] and intent.strategy == "price_spike_down"
    assert (intent.direction, intent.structure, intent.strike_pref) == (
        Direction.DOWN, StructureType.LONG_PE, StrikePref.ATM)
    assert intent.recheck.lo == pytest.approx(fire["ltp"] * (1 - p["recheck_chase_pct"] / 100), rel=1e-12)
    assert intent.recheck.hi == pytest.approx(fire["baseline"] * (1 - p["hold_pct"] / 100), rel=1e-12)
    assert (r["baseline"], r["detect_px"]) == (fire["baseline"], fire["trigger_price"])
    assert abs(r["m"] + fire["move_pct"]) <= 1e-4 and abs(r["held"] - fire["held_pct"]) <= 1e-4
    for other in fixture("spike")["scenarios"]:
        if other["id"] != sc["id"]:
            assert run_titan2_spike(other, PriceSpikeDown) == [], other["id"]


def test_intended_differences_documented_and_exercised():
    used: dict[str, set[str]] = defaultdict(set)
    for c in fixture("select_strike")["cases"]:
        used[c["expect"]].add("select_strike")
    for c in fixture("depth_walk")["cases"]:
        if c["expect"] is not None:
            used[c["expect"]].add("depth_walk")
    for path in fixture("ratchet")["paths"]:
        for _, label in path["ticks"]:
            if label is not None:
                used[label].add("ratchet")
        used[path["expect_exit"]].add("ratchet")
    for sc in fixture("spike")["scenarios"]:
        used[sc["expect"]].add("spike")
    for label, areas in used.items():
        if label != SAME:
            assert label in INTENDED_DIFFERENCES, f"undocumented difference {label}"
            assert areas == {INTENDED_DIFFERENCES[label][0]}, label
    assert set(INTENDED_DIFFERENCES) == set(used) - {SAME}, "a documented difference no case exercises"


def test_fixtures_come_from_v1_final():
    spans = {}
    for area in AREAS:
        fx = fixture(area)
        assert (fx["area"], fx["format"], fx["v1"]["rev"], fx["v1"]["tag"]) == (area, 1, V1_FINAL, "v1-final")
        for name, meta in fx["v1"]["files"].items():
            assert meta["blob"] == V1_FINAL_BLOBS[name], (area, name)
            spans.update({f"{name}:{k}": v for k, v in meta["spans"].items()})
    # the captured code is the code the titan2 port headers cite
    assert spans["options_trader.py:OptionChainFetcher.select_strike"] == [3497, 3550]
    assert spans["zerodha_tools.py:ZerodhaTools._check_option_liquidity:block"] == [4633, 4657]
    assert spans["dashboard.py:pnl_live:block"] == [4255, 4270]
    assert spans["exit_manager.py:ExitManager._check_hard_sl"] == [445, 471]
    assert spans["exit_manager.py:ExitManager._apply_trailing_stop"] == [1332, 1406]
    lo, hi = spans["kite_ticker.py:BreakoutWatcher"]
    assert lo <= 1407 and 2108 <= hi


@pytest.mark.skipif(os.environ.get("TITAN2_GOLDEN_RECAPTURE") != "1",
                    reason="TITAN2_GOLDEN_RECAPTURE=1 re-runs the v1 capture (git history or a v1-final worktree)")
def test_fixtures_reproducible_from_v1():
    spec = importlib.util.spec_from_file_location("titan2_golden_capture_v1", HERE / "capture_v1.py")
    capture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(capture)
    source = capture.V1Source(rev=os.environ.get("TITAN2_GOLDEN_REV", capture.V1_REV),
                              root=os.environ.get("TITAN2_GOLDEN_V1_ROOT"))
    for area, text in capture.capture_all(source).items():
        assert json.loads(text) == fixture(area), area
