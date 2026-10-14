"""Scenarios: one per brief §4.1 live killer (spec §16.2; the test names are normative).

Every test runs on PAPER and on LIVE through the `dual` fixture (tests/scenarios/conftest.py), which also asserts
that both runs issue the same broker commands and book the same P&L.
"""

from __future__ import annotations

import dataclasses
import inspect
import math
import sqlite3
from datetime import timedelta

import pytest

from titan2.core import sizing
from titan2.core.events import FetchPositions
from titan2.core.recon import net_qty, post_fill
from titan2.dash.app import create_app, hash_password
from titan2.model import MIS, InvariantViolation, Order, OrderState, Role, Side, make_tag
from titan2.settings import FRESH_FILL_GRACE_S, POSITIONS_POST_FILL_LAG_S, RECON_CONFIRM_GAP_S, Settings

from fakes import universe as uni
from fakes.fake_kite import GTT_METHODS
from fakes.scenario import (
    TERMINAL_STRUCT, enter, entry_places, exit_places, leg_of, orders_of, route_of, sells_at_broker,
    state_of, struct_of, wait_for,
)

pytestmark = pytest.mark.scenario

REL = "NSE:RELIANCE"
INFY = "NSE:INFY"
SBIN = "NSE:SBIN"


def _ladder(r, symbol, bid, ask, qty=None, levels=5, **kw):
    r.market.quote(symbol, bid, ask, qty=qty, levels=levels, **kw)


# ---------------------------------------------------------------------------------------------------------------
# k01: recon never exits a fresh entry
# ---------------------------------------------------------------------------------------------------------------
def test_k01_recon_never_exits_fresh_entry(dual):
    """positions() lags every fill by 8 s, the most recon is built to absorb (two confirming reads must both be
    requested later than fill + 8 s, recon.in_grace). The entry is timed so that the periodic 5 s recon reads come
    2.25 s and 7.25 s after its fill: both show the account flat, the first one post-fill but inside the fresh-fill
    grace. Recon must keep no sighting from a read inside the grace and never confirm the difference."""
    r = dual.runner()
    r.faults.positions_lag(8)
    acct = dual.acct
    last = r.engine.state.broker[acct].last_round_mono
    wait_for(r, lambda: r.engine.state.broker[acct].last_round_mono != last, 11, "a periodic recon round")
    # the fill comes ~0.2 s after the intent, so the next round (every 5 s once a leg is held) is fill + 2.25 s
    r.run(r.engine.state.broker[acct].last_round_mono + 2.55 - r.clock.mono())
    i, s = enter(r)
    sid = s["structure_id"]
    leg = leg_of(r, sid)
    sym, lot, fill = leg.inst.symbol, leg.open_qty, leg.last_fill_mono
    shown: dict[float, int] = {}                      # positions reads: request time -> the net they showed

    def recon_holds_off(rr) -> None:
        b = rr.engine.state.broker[acct]
        newest_fill = leg_of(rr, sid).last_fill_mono
        for key, sighting in b.recon.sightings.items():
            assert key == (sym, MIS) and sighting.req_mono > newest_fill + FRESH_FILL_GRACE_S, (
                f"recon kept a sighting from a read inside the fresh-fill grace: {key} {sighting}, fill {newest_fill}")
        assert b.recon.status in ("CLEAN", "PENDING"), f"recon confirmed a difference: {b.recon.status}"
        if b.positions is not None:
            shown.setdefault(b.positions.req_mono, net_qty(b.positions, sym))

    r.after_step.append(recon_holds_off)
    reads_before = len(r.commands(FetchPositions))
    r.run(120)
    # the scenario is as hard as intended: the lagging view was read post-fill inside the grace (what the grace must
    #   keep from counting), and twice, far enough apart to confirm it (re-time the entry if this ever fails)
    lagging = sorted(t - fill for t, net in shown.items() if t >= fill and net != lot)
    assert any(POSITIONS_POST_FILL_LAG_S <= t <= FRESH_FILL_GRACE_S for t in lagging), \
        f"no post-fill read inside the grace saw the lag (reads at fill + {lagging} s)"
    assert len(lagging) >= 2 and lagging[-1] - lagging[0] >= RECON_CONFIRM_GAP_S, \
        f"the lag was not read twice, {RECON_CONFIRM_GAP_S:g} s apart (reads at fill + {lagging} s)"
    assert state_of(r, i) == "OPEN"
    assert exit_places(r) == [] and sells_at_broker(r) == 0
    assert r.halts() == []
    assert not r.events("MISMATCH") and not r.events("CLOSED_EXTERNAL") and not r.events("UNKNOWN_POSITION")
    reads = len(r.commands(FetchPositions)) - reads_before
    assert reads >= 120 // 5 - 1                      # recon kept running (every 5 s with an open leg)
    statuses = {e["payload"]["status"] for e in r.events("RECON") if e["payload"].get("account") == dual.acct.value}
    assert "PENDING" in statuses                      # the lagging reads were seen, waited out, never acted on
    assert r.engine.state.broker[dual.acct].recon.status == "CLEAN"
    assert leg_of(r, sid).open_qty == r.broker_net(r.contract().symbol)


# ---------------------------------------------------------------------------------------------------------------
# k02: the broker is flat -> the bot never sells
# ---------------------------------------------------------------------------------------------------------------
def test_k02_broker_flat_never_sells(dual):
    """The operator sells the lot in the Kite app at the instant the book collapses through the hard stop, 2.5 s
    after the newest positions read (which showed the lot) and before the next one. Only a read requested after the
    exit decision may cap the exit (recon.broker_cap, decision B1): it shows the account flat, so nothing is sold,
    and recon books the close from the broker's order (CLOSED_EXTERNAL, MISMATCH)."""
    r = dual.runner()
    i, s = enter(r)
    sid, sym = s["structure_id"], r.contract().symbol
    acct, leg_id, lot = dual.acct, leg_of(r, sid).leg_id, leg_of(r, sid).open_qty

    def lot_read() -> bool:
        pos = r.engine.state.broker[acct].positions
        return pos is not None and post_fill(pos, leg_of(r, sid)) and net_qty(pos, sym) == lot

    wait_for(r, lot_read, 15, "a post-fill positions read showing the lot")
    read = r.engine.state.broker[acct].positions.req_mono
    r.run(max(0.0, read + 2.5 - r.clock.mono()))     # 2.5 s: outside any prior-read window, before the next read
    at = r.clock.now_wall()
    r.faults.external_close(sym, at=at, px=36.40)    # the manual close ...
    _ladder(r, sym, 25.0, 25.1, at=at)               # ... and the stop condition, at the same instant
    decisions: list[tuple[float, str | None]] = []

    def never_short(rr) -> None:
        assert rr.broker_net(sym) >= 0, f"naked short: broker net {rr.broker_net(sym)}"
        lg = rr.engine.state.legs.get(leg_id)
        if not decisions and lg is not None and lg.exiting_since_mono is not None:
            decisions.append((lg.exiting_since_mono, lg.exit_reason))

    r.after_step.append(never_short)
    wait_for(r, lambda: state_of(r, i) in TERMINAL_STRUCT, 30, "the external close to be booked")
    # the stop was decided before recon saw the close, and the newest read before the decision was the one that
    #   showed the lot, too old to cap the exit
    ((decided, why),) = decisions
    newest = max(x.mono for x in r.sent if isinstance(x.cmd, FetchPositions) and x.cmd.account == acct
                 and x.mono < decided)
    assert why == "HARD_SL" and newest == pytest.approx(read) and decided - newest >= 1.5
    assert state_of(r, i) == "CLOSED_EXTERNAL"
    assert exit_places(r) == [] and sells_at_broker(r) == 0
    assert r.broker_net(sym) == 0                     # never short
    (tr,) = r.trades(r.book())
    assert (tr["exit_reason"], tr["pnl_status"], tr["exit_avg"]) == ("CLOSED_EXTERNAL", "FINAL", 36.40)
    assert math.isclose(tr["net"], r.net_from_fills(s["structure_id"]), abs_tol=0.005)
    assert r.halts("MISMATCH", f"ACCOUNT:{dual.acct.value}")
    if dual.live:                                     # §10.9: a LIVE mismatch halts globally and demotes LIVE
        assert r.halts("INVARIANT", "GLOBAL")
        assert all(m["mode"] == "PAPER" for m in r.store.strategy_modes())


# ---------------------------------------------------------------------------------------------------------------
# k03: a failed exit keeps the position
# ---------------------------------------------------------------------------------------------------------------
def test_k03_failed_exit_keeps_position(dual):
    r = dual.runner()
    i, s = enter(r)
    sid, sym = s["structure_id"], r.contract().symbol
    r.faults.reject("Order rejected by OMS", role="EXIT")
    r.faults.timeout_before_accept(role="EXIT")
    r.faults.reject("Order rejected by OMS", role="EXIT")
    _ladder(r, sym, 41.0, 41.1)                       # take profit
    wait_for(r, lambda: state_of(r, i) != "OPEN", 3, "the take-profit decision")
    seen: list[str] = []

    def failing() -> bool:
        exits = orders_of(r, sid, "EXIT")
        seen[:] = [o["state"] for o in exits]
        if struct_of(r, i)["state"] == "CLOSED":
            return False
        assert struct_of(r, i)["state"] in ("EXITING", "VERIFY_FLAT") and not r.trades(r.book())
        return True

    # the timed-out attempt stays UNKNOWN for orders.LATE_ACCEPT_S (30 s): no other SELL goes out meanwhile
    assert r.run_while(failing, 60), "the exit never completed"
    exits = orders_of(r, sid, "EXIT")
    assert [o["state"] for o in exits] == ["REJECTED", "NOT_PLACED", "REJECTED", "COMPLETE"]
    assert seen[:3] == ["REJECTED", "NOT_PLACED", "REJECTED"]
    (tr,) = r.trades(r.book())
    assert tr["exit_reason"] == "TAKE_PROFIT" and tr["n_executed_orders"] == 2 and tr["brokerage"] == 40.0
    assert math.isclose(tr["net"], r.net_from_fills(sid), abs_tol=0.005)
    assert leg_of(r, sid).open_qty == 0 and r.broker_net(sym) == 0


# ---------------------------------------------------------------------------------------------------------------
# k04: OPEN is not a fill
# ---------------------------------------------------------------------------------------------------------------
def test_k04_open_is_not_fill(dual):
    r = dual.runner()
    sym = r.contract().symbol
    r.faults.order_stuck_open(hold_s=60, cancel_works=True, role="ENTRY", symbol=sym)
    i = r.intent(REL)
    sid = struct_of(r, i)["structure_id"]
    r.run(5.5)                                        # WORKING with nothing filled: polled, never a position
    assert state_of(r, i) == "ENTERING"
    (o,) = orders_of(r, sid, "ENTRY")
    assert (o["state"], o["filled_qty"]) == ("WORKING", 0)
    assert leg_of(r, sid).open_qty == 0
    assert not [e for e in r.events("POSITION_OPENED") if e["structure_id"] == sid]
    wait_for(r, lambda: state_of(r, i) == "VOID", 10, "the stuck entry to be cancelled")
    (o,) = orders_of(r, sid, "ENTRY")
    assert (o["state"], o["filled_qty"]) == ("CANCELLED", 0)
    assert struct_of(r, i)["void_reason"] == "ENTRY_NO_FILL"
    cancels = [s_ for s_ in r.sent if type(s_.cmd).__name__ == "Cancel" and s_.cmd.order_pk == o["order_pk"]]
    assert len(cancels) == 1
    placed = next(s_ for s_ in r.sent if type(s_.cmd).__name__ == "Place" and s_.cmd.order_pk == o["order_pk"])
    assert math.isclose((cancels[0].wall - placed.wall).total_seconds(), 6.0, abs_tol=0.3)
    # qty = filled: a second entry fills; the position is exactly what the broker filled, never the order qty
    i2, s2 = enter(r, INFY)
    (o2,) = orders_of(r, s2["structure_id"], "ENTRY")
    leg = leg_of(r, s2["structure_id"])
    assert leg.open_qty == o2["filled_qty"] == r.broker_net(leg.inst.symbol) > 0
    assert math.isclose(s2["risk_basis_rs"], o2["filled_qty"] * o2["avg_fill_px"], abs_tol=0.01)


# ---------------------------------------------------------------------------------------------------------------
# k05: no GTT anywhere
# ---------------------------------------------------------------------------------------------------------------
def test_k05_no_gtt_anywhere(dual):
    r = dual.runner()
    i1, s1 = enter(r)                                 # 09:20 price_spike_up RELIANCE: take profit
    _ladder(r, r.contract().symbol, 41.0, 41.1)
    wait_for(r, lambda: state_of(r, i1) == "CLOSED", 15)
    r.run_until("11:00:00", dt=15.0)
    i2, s2 = enter(r, INFY, strategy="opening_drive")  # held to its 45-minute time stop
    r.run_until("11:44:30", dt=15.0)
    r.run_until("11:50:00", dt=1.0)
    assert state_of(r, i2) == "CLOSED" and struct_of(r, i2)["exit_reason"] == "TIME_STOP"
    r.run_until("14:50:00", dt=15.0)
    i3, s3 = enter(r, SBIN)                           # held into the EOD flatten
    r.run_until("15:14:30", dt=15.0)
    r.run_until("15:17:00", dt=0.5)
    r.run_until("15:41:00", dt=15.0)
    assert struct_of(r, i3)["exit_reason"] == "EOD_FLATTEN" and state_of(r, i3) == "CLOSED"
    assert [code for _, code in r.exits] == [0] and r.proc is None
    for name in GTT_METHODS + ("modify_order",):
        assert r.kite.called(name) == 0, name
    assert r.kite.violations == []


# ---------------------------------------------------------------------------------------------------------------
# k06: only LIMIT / IOC / MIS
# ---------------------------------------------------------------------------------------------------------------
def test_k06_only_limit_ioc(dual):
    r = dual.runner()
    i, s = enter(r)
    sid, sym = s["structure_id"], r.contract().symbol
    p = r.engine.state.structures[sid].exit_params
    arm_bid = math.ceil((p.arm_px + 0.1) / 0.05) * 0.05
    _ladder(r, sym, round(arm_bid, 2), round(arm_bid + 0.1, 2))       # lock armed: no time stop while locked
    wait_for(r, lambda: leg_of(r, sid).lock_armed, 5)
    r.run(3600, dt=5.0)                               # an hour of holding
    assert state_of(r, i) == "OPEN"
    _ladder(r, sym, 41.0, 41.1)
    wait_for(r, lambda: state_of(r, i) == "CLOSED", 15)
    bot = r.rows("SELECT * FROM orders WHERE role <> 'EXTERNAL'")
    assert bot and all((o["order_type"], o["validity"], o["product"], o["variety"]) == ("LIMIT", "IOC", "MIS",
                                                                                        "regular") for o in bot)
    if dual.live:
        places = [kw for _, n, kw in r.kite.calls if n == "place_order"]
        assert places and all((kw["order_type"], kw["validity"], kw["product"], kw["variety"]) ==
                              ("LIMIT", "IOC", "MIS", "regular") and "trigger_price" not in kw for kw in places)
    api = [o for o in r._paper_rows() if o["placed_by"] == "API"]
    assert all((o["order_type"], o["validity"], o["product"]) == ("LIMIT", "IOC", "MIS") for o in api)
    assert r.kite.violations == []
    leg = leg_of(r, sid)
    tag = make_tag(Role.EXIT, dual.acct, r.session, leg.seq_no, 99)
    bad = Order(order_pk=tag, leg_id=leg.leg_id, structure_id=sid, account=dual.acct, book=leg.book,
                role=Role.EXIT, side=Side.SELL, symbol=sym, qty=leg.inst.lot_size, limit_px=30.0, attempt=99,
                band_idx=0, state=OrderState.COMPLETE, filled_qty=0, created_wall=r.clock.now_wall())
    with pytest.raises((InvariantViolation, sqlite3.IntegrityError)):
        with r.store.tx():
            r.store.insert_order(bad, order_type="SL-M")
    assert not r.rows("SELECT 1 FROM orders WHERE order_pk = ?", (tag,))


# ---------------------------------------------------------------------------------------------------------------
# k07: caps and the cheap-option bomb
# ---------------------------------------------------------------------------------------------------------------
def test_k07_caps_and_cheap_bomb(dual):
    assert "multiplier" not in inspect.signature(sizing.size).parameters
    zero = dual.runner(settings=Settings(MAX_LOTS=0))
    i0, s0 = enter(zero, expect="VOID")
    assert s0["void_reason"] == "SIZE:INVALID_OR_ZERO_CAP"
    assert entry_places(zero) == []
    r = dual.runner()
    _ladder(r, r.contract("NSE:IDEA").symbol, 3.50, 3.55)             # a one-tick book: the limit clears the ask
    r.run(0.5)
    cheap, sc = enter(r, "NSE:IDEA", expect="VOID")                    # Rs 3.5 x 4,300
    assert sc["void_reason"] == "SIZE:PREMIUM_FLOOR"
    sym = r.contract("NSE:IDEA").symbol                                # the what-if input: contract + quotes seen
    seen = [e["payload"] for e in r.events("ENTRY_REFUSED") if e["intent_id"] == cheap.intent_id]
    assert seen and all(p["gate"] == "SIZE" and p["legs"][0]["symbol"] == sym for p in seen)
    assert (seen[0]["quotes"][sym]["bid"], seen[0]["quotes"][sym]["ask"]) == (3.50, 3.55)
    big, sb = enter(r, "NSE:BHEL", expect="VOID")                      # a 6,000-lot contract
    lot = r.contract("NSE:BHEL").lot_size
    assert lot == 6000 and r.settings.MAX_UNITS // lot == 0           # the UNITS term is 0 ...
    assert sb["void_reason"].split(":")[0] == "SIZE"                  # ... and size() refuses (RISK binds first)
    assert entry_places(r) == [] and r.kite.api_orders() == []


# ---------------------------------------------------------------------------------------------------------------
# k08: gates fail closed
# ---------------------------------------------------------------------------------------------------------------
class _RaisingStore:
    """Today's InstrumentStore, except that eligibility raises for one underlying (a gate exception)."""

    def __init__(self, inner, bad: str) -> None:
        self._inner, self._bad = inner, bad

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def ineligible_reason(self, underlying, *args, **kwargs):
        if underlying == self._bad:
            raise RuntimeError("eligibility lookup exploded")
        return self._inner.ineligible_reason(underlying, *args, **kwargs)


def test_k08_gates_fail_closed(dual):
    r = dual.runner()
    book = r.book()
    # quote error: the decision fetch fails -> VOID (gate QUOTE) with a QUOTE_TIMEOUT event
    r.market.rest_fail()
    q = r.intent(REL)
    wait_for(r, lambda: state_of(r, q) == "VOID", 5)
    assert struct_of(r, q)["void_reason"].startswith("QUOTE:") and r.events("QUOTE_TIMEOUT")
    r.market.rest_ok()
    # an empty strategy name: refused before routing, with an event and an alert
    bad = dataclasses.replace(r.make_intent(INFY), strategy="", intent_id="scnoname0001")
    r.intent(intent=bad)
    assert any(e["payload"].get("refused") for e in r.events("INTENT_RECEIVED"))
    assert not r.rows("SELECT 1 FROM structures WHERE intent_id = 'scnoname0001'")
    assert "INTENT_INVALID" in r.alert_codes()
    # unknown last-trade time on the option: G19 refuses
    infy_ce = r.contract(INFY).symbol
    r.market.depth(infy_ce, *uni.default_book(r.contract(INFY))[:2], last_trade_age_s=None)
    u = r.intent(INFY)
    wait_for(r, lambda: state_of(r, u) == "VOID", 5)
    assert struct_of(r, u)["void_reason"] == "LIQUIDITY:LAST_TRADE_UNKNOWN"
    # a stale feed: no underlying tick for > 5 s -> G06
    for spec in uni.SPECS:
        r.market.freeze(spec.symbol)
    r.run(6)
    f = r.intent(SBIN)
    assert route_of(r, f, book)["outcome"] == "REFUSED" and route_of(r, f, book)["gate"] == "FEED"
    for spec in uni.SPECS:
        r.market.unfreeze(spec.symbol)
    r.run(2)
    # a gate exception: GATE_ERROR refusal and event
    r._stores[r.session] = _RaisingStore(r.instruments, "NSE:TCS")
    g = r.intent("NSE:TCS")
    assert route_of(r, g, book)["gate"] == "ELIGIBILITY" and route_of(r, g, book)["outcome"] == "REFUSED"
    assert route_of(r, g, book)["detail"] == "GATE_ERROR" and r.events("GATE_ERROR")
    assert entry_places(r) == []


# ---------------------------------------------------------------------------------------------------------------
# k09: signed sustain, no flip (the real price_spike_up on the signals path)
# ---------------------------------------------------------------------------------------------------------------
def test_k09_signed_sustain_no_flip(dual):
    r = dual.runner(start="09:58:00", signals=("price_spike_up",), warmup_until="09:59:30")
    strat = r._runners["price_spike_up"].strategy
    u = r.market.unders[REL]
    base = u.ltp
    r.market.tick(REL, round(base * 1.007, 2), u.cum_volume + 5_000)     # +0.7% on volume: triggers
    r.run(0.5)
    sym_state = strat._syms[REL]
    assert sym_state.pending is not None
    r.market.tick(REL, round(base * 0.996, 2))                           # -0.4%: back through the baseline
    r.run(13)
    assert sym_state.pending is None and sym_state.cooldown_from is not None
    assert r.intents_today() == []
    r.run(95)                                                            # cooldown over
    r.market.tick(REL, round(base * 0.98, 2), u.cum_volume + 20_000)     # a DOWN tape
    r.run(15)
    assert strat._syms[REL].pending is None
    assert r.intents_today() == []
    assert entry_places(r) == []
    # the same strategy does fire on a move that holds (the path above was not vacuous)
    r.run(65)
    low = r.market.unders[REL].ltp
    r.market.tick(REL, round(low * 1.009, 2), r.market.unders[REL].cum_volume + 20_000)
    r.run(13)
    rows = r.intents_today("strategy = 'price_spike_up' AND is_placebo = 0")
    assert len(rows) == 1 and rows[0]["direction"] == "UP" and rows[0]["structure"] == "LONG_CE"
    assert not r.intents_today("structure = 'LONG_PE' AND is_placebo = 0")


# ---------------------------------------------------------------------------------------------------------------
# k10: bid not LTP, the cost lock, REST age, the wall-clock time stop
# ---------------------------------------------------------------------------------------------------------------
def test_k10_bid_not_ltp_and_cost_lock(dual):
    r = dual.runner()
    i, s = enter(r)
    j, t = enter(r, INFY)                             # stays unlocked: its time stop is checked below
    sid, sym = s["structure_id"], r.contract().symbol
    st = r.engine.state.structures[sid]
    p = st.exit_params
    e = p.entry_avg
    # LTP +6% while the realizable bid is -1%: no take profit, no lock
    bid = math.floor(e * 0.99 / 0.05) * 0.05
    _ladder(r, sym, round(bid, 2), round(bid + 0.1, 2), ltp=round(e * 1.06, 2))
    r.run(5)
    assert state_of(r, i) == "OPEN" and not leg_of(r, sid).lock_armed and exit_places(r) == []
    # the lock: armed at be + 4% E, the floor at be + 3% E
    assert math.isclose(p.floor_px, p.be_px + 0.03 * e, abs_tol=1e-6)
    assert math.isclose(p.arm_px, p.be_px + 0.04 * e, abs_tol=1e-6)
    arm_bid = round(math.ceil((p.arm_px + 0.01) / 0.05) * 0.05, 2)
    _ladder(r, sym, arm_bid, round(arm_bid + 0.1, 2))
    wait_for(r, lambda: leg_of(r, sid).lock_armed, 5)
    leg = leg_of(r, sid)
    assert leg.stop_px >= p.floor_px - 1e-9
    assert math.isclose(leg.stop_px, max(p.floor_px, p.be_px + p.profile.trail_retain * (leg.peak_rb - p.be_px)),
                        abs_tol=1e-6)
    # REST age is the receipt time: the WS freezes, REST serves a book stamped minutes ago -> still fresh
    r.faults.stale_depth(sym)
    r.run(12)
    assert state_of(r, i) == "OPEN"
    q = r.proc.quotes.last(sym)
    assert q.source == "REST" and q.age(r.clock.mono()) <= 3.0
    assert (r.clock.now_wall() - q.exch_wall).total_seconds() >= 10
    r.market.unstale(sym)
    # the time stop runs on the wall clock: entry fill wall time + 45 min, frozen at the fill
    infy = r.engine.state.structures[t["structure_id"]]
    stop_at = infy.exit_params.time_stop_at
    assert stop_at == infy.exit_params.entry_fill_wall + timedelta(minutes=infy.exit_params.profile.time_stop_min)
    r.run_until(stop_at - timedelta(seconds=20), dt=10.0)
    r.run_until(stop_at - timedelta(seconds=1), dt=1.0)
    assert state_of(r, j) == "OPEN"
    r.run(2, dt=0.25)
    assert struct_of(r, j)["exit_reason"] == "TIME_STOP"
    assert struct_of(r, i)["exit_reason"] is None and state_of(r, i) == "OPEN"     # locked: no time stop
    wait_for(r, lambda: state_of(r, j) == "CLOSED", 15)


# ---------------------------------------------------------------------------------------------------------------
# k11: one order path (the dashboard's FLATTEN goes command -> engine -> exits)
# ---------------------------------------------------------------------------------------------------------------
def test_k11_single_order_path(dual):
    r = dual.runner(pub=True)
    i, s = enter(r)
    r.enable_commands()
    env = {"DASH_USER": "op", "DASH_PASS_SCRYPT": hash_password("pw", n=2 ** 14), "DASH_SECRET": "s" * 32}
    app = create_app(env, clock=r.clock, var_dir=r.tmp)
    client = app.test_client()
    import base64
    auth = {"Authorization": "Basic " + base64.b64encode(b"op:pw").decode(), "Host": "127.0.0.1:8050"}
    csrf = client.get("/api/csrf", headers=auth).get_json()["csrf"]
    resp = client.post("/api/command", json={"kind": "FLATTEN_ALL", "confirm_text": "FLATTEN", "nonce": "k11-flatten-nonce-0001"},
                       headers={**auth, "X-Titan-CSRF": csrf})
    assert resp.status_code == 201
    cmd_id = resp.get_json()["cmd_id"]
    wait_for(r, lambda: state_of(r, i) == "CLOSED", 20, "the flatten to close the position")
    assert struct_of(r, i)["exit_reason"] == "OPERATOR_FLATTEN"
    wait_for(r, lambda: any(a["status"] == "DONE" for a in r.rows(
        "SELECT * FROM command_acks WHERE source = 'DASH' AND cmd_id = ?", (cmd_id,))), 15, "the DONE ack")
    assert r.halts("FLATTEN", "GLOBAL")
    # every order the broker saw came from an engine Place
    placed = {c.order_pk for c in r.places()}
    if dual.live:
        assert {o.tag for o in r.kite.api_orders()} == placed
    else:
        assert {o["tag"] for o in r.broker_orders()} == placed
    assert all(r.rows("SELECT 1 FROM orders WHERE order_pk = ?", (pk,)) for pk in placed)


# ---------------------------------------------------------------------------------------------------------------
# k12: sticky halts and the kill switch
# ---------------------------------------------------------------------------------------------------------------
def test_k12_sticky_halts_and_kill(dual):
    # the daily stop trips, survives a restart and clears at the next session
    r = dual.runner()
    i, s = enter(r)
    sym = r.contract().symbol
    _ladder(r, sym, 3.0, 3.1)
    wait_for(r, lambda: state_of(r, i) == "CLOSED", 20)
    book_halt = f"BOOK:{r.book()}"
    wait_for(r, lambda: bool(r.halts("DAILY_STOP", book_halt)), 5)
    assert r.trades(r.book())[0]["net"] <= -r.settings.DAILY_STOP_RS
    r.restart()
    r.run(2)
    assert r.halts("DAILY_STOP", book_halt)
    j = r.intent(INFY)
    paper_book = f"PAPER:{j.strategy}"
    assert route_of(r, j, paper_book)["gate"] == "HALTS" and route_of(r, j, paper_book)["outcome"] == "REFUSED"
    assert len(entry_places(r)) == 1
    r.next_session(uni.NEXT_SESSION)
    r.run(2)
    assert not r.halts("DAILY_STOP")
    # unknown MTM blocks entries; FLATTEN_ALL ends verified flat
    r2 = dual.runner()
    a, sa = enter(r2)
    ce = r2.contract().symbol
    r2.market.freeze(ce)
    r2.market.rest_fail(ce)
    r2.run(3.6)                                       # no bid <= 3 s old: the book's MTM is unknown
    c = r2.intent(INFY)
    assert route_of(r2, c, r2.book())["gate"] == "DAY_RISK"
    assert route_of(r2, c, r2.book())["detail"] == "MTM_UNKNOWN"
    r2.market.unfreeze(ce)
    r2.market.rest_ok(ce)
    wait_for(r2, lambda: state_of(r2, a) == "CLOSED", 20)
    assert struct_of(r2, a)["exit_reason"] == "STALE_QUOTE_PROTECTIVE"
    b, sb = enter(r2, SBIN)
    cmd = r2.command("FLATTEN_ALL", confirm="FLATTEN")
    wait_for(r2, lambda: state_of(r2, b) == "CLOSED", 20)
    wait_for(r2, lambda: any(x["status"] == "DONE" for x in r2.rows(
        "SELECT * FROM command_acks WHERE source = 'CLI' AND cmd_id = ?", (cmd,))), 20)
    assert all(n == 0 for n in r2.broker_nets().values())
    assert not r2.open_bot_orders()
    # the LIVE book and its PAPER twin get the same routing decisions in this run (the LIVE account's own Rs 10,000
    #   stop is below the books' Rs 15,000 since 2026-10-13, but every routing here happens before either is reached)
    if dual.live:
        for it in r2.rows("SELECT intent_id FROM intents WHERE is_placebo = 0"):
            live = route_of(r2, it["intent_id"], "LIVE:price_spike_up")
            paper = route_of(r2, it["intent_id"], "PAPER:price_spike_up")
            if live is not None and paper is not None:
                assert (live["outcome"], live["gate"]) == (paper["outcome"], paper["gate"])
