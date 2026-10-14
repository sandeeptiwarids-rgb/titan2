"""Scenarios: restart, concurrency and lag (spec §16.4; the test names are normative).

A restart is a crash of the bot process (engine, store connection, paper-sim, lanes, feed and queues are lost; the
DB files, the market and the broker stay) followed by a new process booting through recovery (§9.5-9.6). Every test
runs on PAPER and on LIVE; where the spec makes the accounts behave differently (earlier-session leftovers, an
in-process paper exchange that dies with the bot, the LIVE-only WS back-fill) the test says so with dual.diverges()
and asserts each account's own outcome.
"""

from __future__ import annotations

import math

import pytest

from titan2.core import orders
from titan2.core.engine import Engine
from titan2.core.events import FetchOrders, LookupTag, Place, PollOrder
from titan2.model import Account, Role

from fakes import universe as uni
from fakes.scenario import enter, fill_draw_of, leg_of, orders_of, route_of, state_of, struct_of, wait_for

pytestmark = pytest.mark.scenario

REL, INFY, SBIN = "NSE:RELIANCE", "NSE:INFY", "NSE:SBIN"
TICK = 0.05


def _entry_place(acct):
    return lambda c: (isinstance(c, Place) and c.account is acct and c.req.role is Role.ENTRY)


def _exit_place(acct):
    return lambda c: (isinstance(c, Place) and c.account is acct and c.req.role is Role.EXIT)


def _sent(r, cls, pk=None):
    return [s for s in r.sent if isinstance(s.cmd, cls) and (pk is None or getattr(s.cmd, "order_pk", None) == pk)]


# ---------------------------------------------------------------------------------------------------------------
def test_restart_submitting_not_sent_becomes_not_placed(dual):
    r = dual.runner()
    hit = _entry_place(dual.acct)
    r.crash_on = lambda c: "before_dispatch" if hit(c) else None
    i = r.intent(REL)                                 # the SUBMITTING row commits; the process dies before sending
    assert r.proc is None
    (o,) = [x for x in r.rows("SELECT * FROM orders WHERE role = 'ENTRY'") if x["account"] == dual.acct.value]
    assert o["state"] == "SUBMITTING" and o["broker_order_id"] is None
    assert r.broker_orders() == []
    r.boot()
    booted = r.clock.mono()
    wait_for(r, lambda: state_of(r, i) == "VOID", 40, "the unsent entry to resolve")
    (o,) = orders_of(r, o["structure_id"], "ENTRY")
    assert o["state"] == "NOT_PLACED"
    # the restart cannot tell a row never sent from one whose answer died with the process: +1/+3/+7/+15 s
    #   (orders.LOOKUP_SCHEDULE_S), then every 5 s to the end of a whole late-accept window (decision B2; review
    #   round 2: NOT_PLACED came at +15 s, inside the window in which a sent order may still be accepted)
    assert len(_sent(r, LookupTag, o["order_pk"])) == 7
    assert r.clock.mono() - booted >= orders.LATE_ACCEPT_S
    assert not [s for s in r.sent if isinstance(s.cmd, Place) and s.cmd.order_pk == o["order_pk"]]
    assert r.broker_orders() == []                    # never sent, never retried (intents are not replayed)


def test_restart_after_send_before_ack_adopts_by_tag(dual):
    dual.diverges("the entry fills while recovery's reads pay their round trip (decision B1): the paper exchange "
                  "reports the fill at once, while Kite's WS update is lost (the WS connects after recovery) and LIVE "
                  "learns it from the recovery orders() read")
    r = dual.runner()
    hit = _entry_place(dual.acct)
    r.crash_on = lambda c: "after_send" if hit(c) else None
    i = r.intent(REL)                                 # the order reaches the broker; its answer is lost
    assert r.proc is None and len(r.broker_orders()) == 1
    r.boot()
    wait_for(r, lambda: state_of(r, i) not in ("RESERVED", "ENTERING"), 10, "the adopted entry to resolve")
    sid = struct_of(r, i)["structure_id"]
    (o,) = orders_of(r, sid, "ENTRY")
    assert o["state"] == "COMPLETE" and o["broker_order_id"] and o["filled_qty"] == r.contract().lot_size
    assert len(r.broker_orders()) == 1                # adopted, never placed twice
    assert leg_of(r, sid).open_qty == r.broker_net(r.contract().symbol) == o["filled_qty"]


def test_restart_mid_exit_partial_resumes(dual):
    r = dual.runner()
    i, s = enter(r, "NSE:HINDPETRO")
    sid = s["structure_id"]
    leg = leg_of(r, sid)
    sym, lot = leg.inst.symbol, leg.inst.lot_size
    p = r.engine.state.structures[sid].exit_params
    top = round(math.floor(p.hard_sl_px / TICK) * TICK - TICK, 2)
    # a one-lot touch (half a lot at 0.5 participation) and no other bid within bands 0-1, then real depth
    thin = [(top, lot), (round(top - 4 * TICK, 2), 3 * lot)]
    r.market.depth(sym, thin, [(round(top + 0.5, 2), 4 * lot)])
    wait_for(r, lambda: len(orders_of(r, sid, "EXIT")) == 1 and orders_of(r, sid, "EXIT")[0]["state"] == "CANCELLED",
             10, "the first, unfilled exit attempt")
    assert fill_draw_of(orders_of(r, sid, "EXIT")[0]) >= 0.5      # paper model 2: outside the touch's half lot
    reason = struct_of(r, i)["exit_reason"]
    assert state_of(r, i) == "EXITING" and reason == "HARD_SL"
    r.restart()
    wait_for(r, lambda: state_of(r, i) == "CLOSED", 30, "the exit to resume after the restart")
    exits = orders_of(r, sid, "EXIT")
    assert len(exits) >= 2 and exits[-1]["filled_qty"] == lot and len({o["order_pk"] for o in exits}) == len(exits)
    (tr,) = r.trades(r.book())
    assert tr["exit_reason"] == reason and tr["qty"] == lot
    assert math.isclose(tr["net"], r.net_from_fills(sid), abs_tol=0.005)


def test_restart_unknown_broker_position_halts_no_orders(dual):
    dual.diverges("§10.9: an unknown position on LIVE also demotes every LIVE strategy (its reads then differ)")
    r = dual.runner()
    foreign = r.contract(SBIN).symbol
    lot = r.contract(SBIN).lot_size
    r.faults.external_buy(foreign, lot, at=r.clock.now_wall())
    r.step(0.05)                                      # the broker books it ...
    r.crash()                                         # ... and the bot dies before any recon saw it
    assert r.broker_net(foreign) == lot
    r.boot()
    wait_for(r, lambda: bool(r.halts("UNKNOWN_POSITION", f"ACCOUNT:{dual.acct.value}")), 15, "UNKNOWN_POSITION")
    r.run(10)
    assert not [c for c in r.places() if c.req.symbol == foreign]
    assert r.broker_net(foreign) == lot               # never traded
    j = r.intent(REL)
    routes = r.rows("SELECT * FROM intent_routes WHERE intent_id = ?", (j.intent_id,))
    assert routes and all(x["outcome"] == "REFUSED" for x in routes)
    if dual.live:                                     # §10.9: demoted to PAPER, whose account is not halted
        assert all(m["mode"] == "PAPER" for m in r.store.strategy_modes())
    else:
        assert route_of(r, j, r.book())["gate"] == "HALTS"


def test_restart_db_lost_imports_bot_tags(dual):
    dual.diverges("the LIVE unlock lived in the lost DB: after it LIVE is off (no LIVE margins reads)")
    r = dual.runner()
    i, s = enter(r)
    sym, lot = r.contract().symbol, r.contract().lot_size
    r.crash()
    for f in r.db_path.parent.glob("titan2.db*"):
        f.unlink()                                    # the DB (and its WAL) is lost; the broker still has the orders
    r.boot()
    r.run(2)
    rec = [x for x in r.structures() if x["book"] == f"RECOVERED:{dual.acct.value}"]
    assert len(rec) == 1 and rec[0]["strategy"] == "_recovered"
    (o,) = orders_of(r, rec[0]["structure_id"], "ENTRY")
    assert (o["state"], o["filled_qty"]) == ("COMPLETE", lot)
    assert {"RECOVERED_ORDERS"} <= set(r.alert_codes("CRITICAL"))
    leg = leg_of(r, rec[0]["structure_id"])
    assert leg.open_qty + sum(x["filled_qty"] for x in orders_of(r, rec[0]["structure_id"], "EXIT")) == lot
    assert r.broker_net(sym) >= 0 and r.broker_net(sym) == leg.open_qty     # never oversold


def test_restart_after_1515_recovery_flatten(dual):
    r = dual.runner(start="14:50:00", warmup_until="14:52:00")
    i, s = enter(r)
    r.run_until("15:14:00", dt=5.0)
    assert state_of(r, i) == "OPEN"
    r.crash()
    r.run_until("15:16:00", dt=5.0)                   # down across EOD_FLATTEN
    r.boot()
    wait_for(r, lambda: state_of(r, i) == "CLOSED", 30)
    # the first Timer after boot runs decide() (rule 1 flattens from 15:15) while recovery's reads pay their round
    #   trip (decision B1), so with the default read delays the leg is already EXITING(EOD_FLATTEN) when the plan
    #   lands (EXITING keeps its reason, §9.6 step 7); either reason leaves it flat
    assert struct_of(r, i)["exit_reason"] in ("RECOVERY_FLATTEN", "EOD_FLATTEN")


def test_restart_previous_session_live_unresolved(dual):
    dual.diverges("§9.6 step 7: a LIVE leftover closes UNRESOLVED; a paper one is squared off by the simulator")
    r = dual.runner(session=uni.PREV_SESSION)
    i, s = enter(r)
    sid = s["structure_id"]
    r.run(5)
    r.next_session(uni.SESSION)                       # down from mid-day to the next session
    wait_for(r, lambda: struct_of(r, i) is not None and struct_of(r, i)["state"] == "CLOSED_EXTERNAL", 30)
    (tr,) = [t for t in r.trades() if t["structure_id"] == sid]
    if dual.live:
        assert tr["pnl_status"] == "UNRESOLVED"
        assert r.halts("MISMATCH", "ACCOUNT:LIVE")
    else:
        assert (tr["exit_reason"], tr["exit_avg"], tr["pnl_status"]) == ("BROKER_SQUAREOFF", 0.05, "FINAL")
        assert not r.halts("MISMATCH", "ACCOUNT:PAPER")


def test_paper_restart_settles_expired_and_missed_squareoff(dual):
    dual.diverges("the paper exchange runs inside the bot: an order in flight at a crash expires there, while "
                  "the real broker fills it")
    r = dual.runner()
    hit = _entry_place(dual.acct)
    r.crash_on = lambda c: "after_send" if hit(c) else None
    i = r.intent(REL)
    assert r.proc is None
    r.run(5)                                          # down for 5 s: past the order's due time + 1 s
    r.boot()
    wait_for(r, lambda: state_of(r, i) not in ("RESERVED", "ENTERING"), 15)
    sid = struct_of(r, i)["structure_id"]
    (o,) = orders_of(r, sid, "ENTRY")
    if dual.live:
        assert (o["state"], o["filled_qty"]) == ("COMPLETE", r.contract().lot_size)      # the broker filled it
    else:
        assert (o["state"], o["filled_qty"], o["status_msg"]) == ("CANCELLED", 0, "SIM: expired during downtime")
        assert state_of(r, i) == "VOID"
    # a position left from an earlier session (the bot died before 15:26 and never came back that day)
    r2 = dual.runner(session=uni.PREV_SESSION)
    j, t = enter(r2, INFY)
    r2.next_session(uni.SESSION)
    wait_for(r2, lambda: state_of(r2, j) == "CLOSED_EXTERNAL", 30)
    (tr,) = [x for x in r2.trades() if x["structure_id"] == t["structure_id"]]
    if dual.live:
        assert tr["pnl_status"] == "UNRESOLVED"       # Zerodha squared it off at 15:26; that day's orders are gone
    else:
        assert (tr["exit_reason"], tr["exit_avg"]) == ("BROKER_SQUAREOFF", 0.05)        # "SIM: missed square-off"


def test_exception_reload_equals_db_state(dual):
    r = dual.runner(strict=False)
    i, s = enter(r)
    sid = s["structure_id"]
    r.raise_in_evaluate = RuntimeError("a bug inside an event")
    r.run(0.5)
    assert r.exceptions and r.halts("EXCEPTION", "GLOBAL")
    eng = r.engine
    fresh = Engine.load(r.store, r.proc.ctx)
    assert _comparable(eng.state) == _comparable(fresh.state)
    assert state_of(r, i) == "OPEN"                   # exits continue: the position is still managed
    r.market.quote(r.contract().symbol, 41.0, 41.1)
    wait_for(r, lambda: state_of(r, i) == "CLOSED", 15)
    assert math.isclose(r.trades(r.book())[0]["net"], r.net_from_fills(sid), abs_tol=0.005)


def _comparable(state) -> dict:
    return {
        "structures": {k: (str(v.state), v.exit_reason, v.void_reason, tuple(v.legs), v.exit_params)
                       for k, v in state.structures.items()},
        "legs": {k: (str(v.state), v.stop_px, v.lock_armed, v.exit_reason, tuple(v.orders), v.exit_band_idx)
                 for k, v in state.legs.items()},
        "orders": {k: (str(v.state), v.filled_qty, v.avg_fill_px, v.broker_order_id) for k, v in state.orders.items()},
        "halts": sorted((h.kind, h.scope) for h in state.halts),
        "modes": {k: (v["mode"], v["version"]) for k, v in state.modes.items()},
    }


def test_duplicate_and_out_of_order_updates(dual):
    r = dual.runner()
    r.faults.duplicate_updates()
    r.faults.out_of_order_updates(count=None)
    i, s = enter(r)
    sid = s["structure_id"]
    r.market.quote(r.contract().symbol, 41.0, 41.1)
    wait_for(r, lambda: state_of(r, i) == "CLOSED", 15)
    assert r.faults.count("duplicate_updates") >= 2 and r.faults.count("out_of_order_updates") >= 2
    for o in orders_of(r, sid):
        assert o["state"] == "COMPLETE" and o["filled_qty"] == o["qty"]
    assert not r.events("INVARIANT_VIOLATION") and "ORDER_CONFLICT" not in r.alert_codes()
    fills = r.rows("SELECT * FROM fills WHERE order_pk IN (SELECT order_pk FROM orders WHERE structure_id = ?)",
                   (sid,))
    assert sum(f["qty"] for f in fills if f["source"] == "CUM") == 2 * r.contract().lot_size   # applied once each
    (tr,) = r.trades(r.book())
    assert math.isclose(tr["net"], r.net_from_fills(sid), abs_tol=0.005)


def test_ws_update_before_place_result(dual):
    r = dual.runner()
    r.faults.ws_update_before_place_returns(role="ENTRY")
    i, s = enter(r)
    sid = s["structure_id"]
    assert r.faults.count("ws_update_before_place_returns") == 1
    (o,) = orders_of(r, sid, "ENTRY")
    assert o["state"] == "COMPLETE" and o["broker_order_id"]
    assert not r.events("INVARIANT_VIOLATION") and "ORDER_CONFLICT" not in r.alert_codes()
    assert leg_of(r, sid).open_qty == o["filled_qty"]


def test_positions_lag_never_external(dual):
    r = dual.runner()
    r.faults.positions_lag(8)
    i, s = enter(r)
    sid = s["structure_id"]
    r.run(30)
    assert state_of(r, i) == "OPEN"
    r.market.quote(r.contract().symbol, 41.0, 41.1)
    wait_for(r, lambda: state_of(r, i) == "CLOSED", 30)
    assert struct_of(r, i)["exit_reason"] == "TAKE_PROFIT"
    for kind in ("CLOSED_EXTERNAL", "MISMATCH", "UNKNOWN_POSITION", "BROKER_SQUAREOFF"):
        assert not r.events(kind), kind
    assert not r.halts()
    (tr,) = r.trades(r.book())
    assert tr["pnl_status"] == "FINAL" and math.isclose(tr["net"], r.net_from_fills(sid), abs_tol=0.005)
    assert len(r.places(Role.EXIT)) == 1


def test_verify_flat_residual_never_sold(dual):
    dual.diverges("§10.9: the residual is a LIVE mismatch, which also demotes every LIVE strategy")
    r = dual.runner()
    i, s = enter(r)
    sid = s["structure_id"]
    sym, lot = r.contract().symbol, r.contract().lot_size
    r.run(6)
    r.market.quote(sym, 41.0, 41.1)
    wait_for(r, lambda: any(o["state"] == "COMPLETE" for o in orders_of(r, sid, "EXIT")), 10)
    r.faults.external_buy(sym, lot, at=r.clock.now_wall())            # the broker shows a lot the bot never bought
    wait_for(r, lambda: state_of(r, i) == "CLOSED", 30)
    assert leg_of(r, sid).broker_residual_qty == lot or r.rows(
        "SELECT broker_residual_qty FROM legs WHERE structure_id = ?", (sid,))[0]["broker_residual_qty"] == lot
    assert r.halts("MISMATCH", f"ACCOUNT:{dual.acct.value}")
    r.run(15)
    assert len(r.places(Role.EXIT)) == 1                              # the residual is never sold
    assert r.broker_net(sym) == lot


def test_ws_reconnect_backfills_orders(dual):
    dual.diverges("the WS order back-fill (§9.7) is LIVE-only: paper orders are answered by paper-sim, not the WS")
    r = dual.runner()
    i, s = enter(r)
    sid = s["structure_id"]
    sym = r.contract().symbol
    r.run(6)
    r.market.quote(sym, 41.0, 41.1)
    wait_for(r, lambda: bool(orders_of(r, sid, "EXIT")), 5)
    (x,) = orders_of(r, sid, "EXIT")
    r.market.disconnect()                             # the fill happens while the WS is down: its update is lost
    r.run(1.2)
    r.market.reconnect()
    wait_for(r, lambda: orders_of(r, sid, "EXIT")[0]["state"] == "COMPLETE", 3)
    assert not _sent(r, PollOrder, x["order_pk"])     # resolved before its +3 s poll
    backfill = [c for c in r.sent if isinstance(c.cmd, FetchOrders) and c.cause == "FeedState"]
    if dual.live:
        assert backfill and backfill[0].cmd.account is Account.LIVE
    wait_for(r, lambda: state_of(r, i) == "CLOSED", 15)


def test_token_rotation_controlled_restart(dual):
    r = dual.runner()
    i, s = enter(r)
    sid = s["structure_id"]
    r.run(6)
    r.faults.token_expired()
    r.market.quote(r.contract().symbol, 41.0, 41.1)
    # §9.7: a token error on any call raises HALT(TOKEN); the exit cap's positions and orders reads (made after the
    #   exit decision) fail first. Decision B1 is strict on that path too: the exit waits and re-asks for
    #   BROKER_READS_FAIL_UNCAP_S (10 s; this test used to see the SELL at once), then goes out on the newest
    #   post-fill read (CRITICAL EXIT_CAP_BEFORE_DECISION) and fails FATAL too
    wait_for(r, lambda: bool(r.halts("TOKEN")) and bool(orders_of(r, sid, "EXIT")), 15,
             "HALT(TOKEN) and the FATAL exit")
    (x,) = orders_of(r, sid, "EXIT")
    assert x["state"] == "UNKNOWN"
    assert {"TOKEN", "EXIT_CAP_BEFORE_DECISION"} <= set(r.alert_codes("CRITICAL"))
    r.run(40)                                         # the token re-check fails meanwhile; nothing else is sent
    assert len(r.places(Role.EXIT)) == 1 and r.proc is not None
    r.faults.rotate_token()                           # titan2-token wrote a newer session token
    wait_for(r, lambda: r.proc is None, 40, "the controlled restart")
    assert [code for _, code in r.exits] == [75]
    r.boot()
    wait_for(r, lambda: state_of(r, i) == "CLOSED", 40)
    assert not r.halts("TOKEN")
    assert orders_of(r, sid, "EXIT")[0]["state"] == "NOT_PLACED"
    assert struct_of(r, i)["exit_reason"] == "TAKE_PROFIT"


def test_mis_squareoff_handoff_1525_1526(dual):
    r = dual.runner(start="14:50:00", warmup_until="14:52:00")
    i, s = enter(r)
    sid = s["structure_id"]
    sym = r.contract().symbol
    r.run_until("15:14:00", dt=5.0)
    r.market.quote(sym, None, 30.0)                   # no bids from 15:14: nothing can be sold before 15:25
    r.run_until("15:28:30", dt=1.0)
    assert not [c for c in r.sent if isinstance(c.cmd, Place)
                and c.wall >= uni.CALENDAR.session_bounds(r.session)[0].replace(hour=15, minute=25, second=0)]
    assert "NOT_FLAT_STOP_SENDING" in r.alert_codes("CRITICAL")
    wait_for(r, lambda: state_of(r, i) == "CLOSED_EXTERNAL", 300, "the broker square-off to be booked", dt=1.0)
    (tr,) = r.trades(r.book())
    assert (tr["exit_reason"], tr["sqoff_fee"], tr["pnl_status"]) == ("BROKER_SQUAREOFF", 50.0, "FINAL")
    assert math.isclose(tr["net"], r.net_from_fills(sid), abs_tol=0.005)    # the Rs 50 + GST fee is in net_pnl
    assert not r.halts("MISMATCH")
    assert "BROKER_SQUAREOFF" in r.alert_codes("CRITICAL")
    assert r.broker_net(sym) == 0


def test_watchdog_only_with_core_progress(dual):
    """§3.3: WATCHDOG=1 goes out only while the core loop makes progress (the last Timer finished < 2 s ago). A core
    that stops finishing Timers stops the pings, and systemd's watchdog then kills and restarts it."""
    from titan2 import alerts, runtime
    r = dual.runner()
    hb = alerts.Heartbeat()                              # the runtime marks it after every Timer the core finishes
    r.timer_hooks.append(hb.core_progress)
    sent: list[bytes] = []
    wd = runtime.Watchdog(runtime.Notifier(send=sent.append), hb, every_s=1.0)
    r.after_step.append(lambda rr: wd.tick(rr.clock.mono()))
    enter(r)
    r.run(5)
    assert sent and set(sent) == {b"WATCHDOG=1"}
    assert len(sent) >= 4                                # about one ping a second while Timers finish
    pings = len(sent)

    # the core is stuck in one event from here on: no Timer finishes while the clock runs
    r.clock.advance(round(1.9 - hb.age(r.clock.mono()), 6))
    assert hb.age(r.clock.mono()) < runtime.WATCHDOG_FRESH_S
    r.clock.advance(0.1)
    assert hb.age(r.clock.mono()) >= runtime.WATCHDOG_FRESH_S
    for _ in range(20):
        r.clock.advance(0.5)
        assert wd.tick(r.clock.mono()) is False          # no WATCHDOG=1 without core progress
    assert len(sent) == pings

    r.run(3)                                             # the loop runs again: pings resume
    assert len(sent) > pings
    assert hb.age(r.clock.mono()) < runtime.WATCHDOG_FRESH_S


# ---------------------------------------------------------------------------------------------------------------
# Beyond the §16.4 list: §9.6 step 7 restores OPEN legs (params, stop, lock, peak); a mid-session restart must keep
# managing them, not dump them. The first evaluation after a restart comes before any WS or REST quote exists.
# ---------------------------------------------------------------------------------------------------------------
def test_restart_keeps_open_leg_managed(dual):
    r = dual.runner()
    i, s = enter(r)
    sid = s["structure_id"]
    sym = r.contract().symbol
    p = r.engine.state.structures[sid].exit_params
    arm_bid = round(math.ceil((p.arm_px + 0.01) / TICK) * TICK, 2)
    r.market.quote(sym, arm_bid, round(arm_bid + 0.1, 2))
    wait_for(r, lambda: leg_of(r, sid).lock_armed, 5)
    before = leg_of(r, sid)
    stop, peak = before.stop_px, before.peak_rb
    r.run(2)
    r.restart()
    r.run(10)
    assert state_of(r, i) == "OPEN", f"exited after the restart: {struct_of(r, i)['exit_reason']}"
    after = leg_of(r, sid)
    assert after.lock_armed and after.stop_px >= stop - 1e-9 and after.peak_rb >= peak - 1e-9


# ---------------------------------------------------------------------------------------------------------------
# Beyond the §16.4 list, from the chaos runs (minimal repros): an external close must book FINAL and the EOD truth
# check (§9.8, the engine's own mapping and recon.eod_truth_check) must pass when the broker's orders explain it.
# ---------------------------------------------------------------------------------------------------------------
def _truth_check(r, account=None):
    """§9.8 for an account (default: the one under test) on the broker's TRUE tradebook, with the engine's own
    order -> book map (engine.py's EOD truth check)."""
    from titan2 import ledger
    from titan2.broker.kite import KiteData
    from titan2.broker.paper import PaperExchange
    from titan2.core import recon
    from fakes.runner import fast_buckets

    acct = account or r.acct
    session = r.session.isoformat()
    nets = {t["book"]: t["n"] for t in r.rows(
        "SELECT book, SUM(net) AS n FROM trades WHERE account = ? AND session = ? GROUP BY book", (acct.value, session))}
    books, parts = ledger.truth_attribution(r.rows(
        "SELECT order_pk, leg_id, book, role, broker_order_id, filled_qty, external_kind FROM orders WHERE "
        "account = ? AND filled_qty > 0 AND substr(created_ts, 1, 10) = ?", (acct.value, session)))
    if acct is Account.LIVE:
        trades = KiteData(r.kite, clock=r.clock, buckets=fast_buckets(r.clock)).trades_today(acct)
    else:
        trades = PaperExchange.trades_today(r.proc.paper, acct)
    return recon.eod_truth_check(acct, r.session, nets, books, trades, parts=parts)


def test_external_close_after_a_late_entry_fill_books_final(dual):
    """The WS loses the entry's updates and the orders read fails for a few seconds, so the engine learns of the
    fill from its 3 s poll; the broker squares the position off in between. That square-off order explains the close
    (§9.3 EXTERNAL_CLOSE: non-bot SELL orders at or after the entry fill, an exchange time), so the trade is FINAL
    and the order is attributed in the EOD truth check (chaos seed 80)."""
    from datetime import timedelta
    r = dual.runner()
    r.faults.drop_ws_updates()
    r.faults.orders_error(end=r.clock.now_wall() + timedelta(seconds=3.5))
    i = r.intent(REL)
    r.faults.broker_squareoff(at=r.clock.now_wall() + timedelta(seconds=1.5))
    wait_for(r, lambda: state_of(r, i) in ("CLOSED", "CLOSED_EXTERNAL", "VOID"), 40, "the external close")
    s = struct_of(r, i)
    assert s["state"] == "CLOSED_EXTERNAL"
    ext = orders_of(r, s["structure_id"], "EXTERNAL")
    (tr,) = r.trades(r.book())
    assert (tr["pnl_status"], [bool(o["broker_order_id"]) for o in ext]) == ("FINAL", [True]), \
        f"booked {tr['pnl_status']} from {[(o['order_pk'], o['broker_order_id']) for o in ext]}"
    tc = _truth_check(r)
    assert tc.ok, [(b.book, b.ledger_net, b.broker_net) for b in tc.books]


def test_one_broker_order_closing_two_books_is_split_by_book(dual):
    """Two strategies hold the same contract in the PAPER account; one broker order (a manual close of the whole
    position) closes both legs. The EOD truth check must attribute that order to both books, each by its share, and
    book the order's charges once, not once per leg (chaos seed 172; tests/unit/test_seams.py has it as a known
    gap). A LIVE account holds one structure per underlying, so in the LIVE run this is the PAPER twins' account."""
    dual.diverges("one LIVE structure per underlying: LIVE:opening_drive is refused, its PAPER book still enters")
    r = dual.runner()
    i1 = r.intent(REL, strategy="price_spike_up")
    i2 = r.intent(REL, strategy="opening_drive")
    books = ("PAPER:price_spike_up", "PAPER:opening_drive")
    wait_for(r, lambda: all(state_of(r, i, b) == "OPEN" for i, b in zip((i1, i2), books)), 10, "both PAPER entries")
    s1, s2 = struct_of(r, i1, books[0]), struct_of(r, i2, books[1])
    sym = r.rows("SELECT symbol FROM legs WHERE structure_id = ?", (s1["structure_id"],))[0]["symbol"]
    assert r.rows("SELECT symbol FROM legs WHERE structure_id = ?", (s2["structure_id"],))[0]["symbol"] == sym
    lot = uni.symbol_of(r.instruments, REL, "CE", "ATM", r.spot(REL), r.session).lot_size
    assert r.broker_net(sym, Account.PAPER) == 2 * lot
    r.run(3)
    r.faults.external_close(sym, at=r.clock.now_wall(), account=Account.PAPER)
    sids = (s1["structure_id"], s2["structure_id"])
    wait_for(r, lambda: [x["state"] for x in r.rows("SELECT state FROM structures WHERE structure_id IN (?, ?)",
                                                     sids)] == ["CLOSED_EXTERNAL"] * 2, 40,
             "both legs to close externally")
    assert r.broker_net(sym, Account.PAPER) == 0
    tc = _truth_check(r, Account.PAPER)
    assert tc.ok, [(b.book, b.ledger_net, b.broker_net) for b in tc.books]
