"""Scenarios: the May 13-14 postmortem and the memory incidents (spec §16.3; the test names are normative).

Every test runs on PAPER and on LIVE through the `dual` fixture, which also asserts the same broker commands and
the same booked P&L on both accounts. Incident prices are scaled where today's caps would refuse the original
numbers outright (a premium under Rs 20 is never entered): the code path under test is the same.
"""

from __future__ import annotations

import ast
import inspect
import json
import math
import textwrap
from pathlib import Path

import pytest

from titan2 import charges, ledger
from titan2.core import positions, recon, risk
from titan2.core.engine import Engine
from titan2.core.events import Place
from titan2.model import Account, HaltClears, InvariantViolation, Policy, Role, Side, floor_tick

from fakes.scenario import (
    enter, entry_places, exit_places, fill_draw_of, leg_of, orders_of, route_of, sells_at_broker, state_of, struct_of,
    wait_for,
)

pytestmark = pytest.mark.scenario

REL, INFY, SBIN = "NSE:RELIANCE", "NSE:INFY", "NSE:SBIN"
TICK = 0.05


def _bid_under(px: float, ticks: int = 1) -> float:
    return round(floor_tick(px, TICK) - ticks * TICK, 2)


def _params(r, sid):
    return r.engine.state.structures[sid].exit_params


# ---------------------------------------------------------------------------------------------------------------
def test_pm_a_oversize(dual):
    r = dual.runner()
    _, nmdc = enter(r, "NSE:NMDC", expect="VOID")                  # NMDC-like: lot 6,750 @ Rs 3.5
    assert nmdc["void_reason"].startswith("SIZE:")
    _, ashok = enter(r, "NSE:ASHOKLEY", expect="VOID")             # lot 4,300 @ Rs 25: Rs 1.07 lakh a lot
    assert ashok["void_reason"].startswith("SIZE:")
    i, s = enter(r)
    places = entry_places(r)
    assert len(places) == 1 and all(p.req.qty == p.req.lot_size for p in places)
    assert leg_of(r, s["structure_id"]).open_qty == r.contract().lot_size
    assert all(o["qty"] <= o_lot for o, o_lot in
               ((o, r.engine.state.legs[o["leg_id"]].inst.lot_size) for o in r.orders(role="ENTRY")))


def test_pm_a_one_lot_cap_binds(dual):
    """The 1-lot cap itself (pm_a's contracts are refused or held to 1 lot by the premium term first): NIFTY ATM is
    lot 65 at ~Rs 150, Rs 9.8k a lot, where the risk, premium, depth, units and day-budget terms all allow 2+ lots.
    The entry is exactly 1 lot, MAX_LOTS is the binding term, and nothing after size() changes the quantity."""
    r = dual.runner()
    i, s = enter(r, "NSE:NIFTY 50")
    sid = s["structure_id"]
    leg = leg_of(r, sid)
    (p,) = entry_places(r)
    assert leg.inst.lot_size == 65 and p.req.qty == 65 and leg.open_qty == 65 == r.broker_net(leg.inst.symbol)
    plan = json.loads(r.rows("SELECT decision_json FROM structures WHERE structure_id = ?", (sid,))[0]
                      ["decision_json"])["plan"]
    assert (plan["binding"], plan["lots"], plan["qty"]) == ("LOTS", 1, 65)
    assert plan["premium_rs"] * 2 <= r.settings.MAX_PREMIUM_PER_TRADE_RS              # 2 lots would fit the premium
    assert plan["per_lot_risk_rs"] * 2 <= r.settings.MAX_LOSS_PER_TRADE_RS            # ... and the risk cap


# ---------------------------------------------------------------------------------------------------------------
def test_pm_b_no_reverse_churn(dual):
    r = dual.runner()
    strat = "opening_drive"
    book = r.book(strat)
    # MAX_ENTRIES_PER_DAY is 3 (2 until 2026-10-13). The day's first entry, closed at its take-profit, makes the
    #   stop-out below the second, so the storm's first trigger is the third and last entry while the book still has
    #   room for a second open position. It is over a minute old by then: three entry orders inside 60 s would trip
    #   the RUNAWAY halt before the entries cap could refuse the fourth.
    i0, s0 = enter(r, "NSE:NIFTY 50", strategy=strat)
    tp = round(math.ceil((_params(r, s0["structure_id"]).tp_px + 0.5) / TICK) * TICK, 2)
    r.market.quote(leg_of(r, s0["structure_id"]).inst.symbol, tp, round(tp + 0.1, 2))
    wait_for(r, lambda: state_of(r, i0, book) == "CLOSED", 20, "the take-profit to close the first entry")
    r.run(60)
    n_exits = len(exit_places(r))
    i, s = enter(r, SBIN, strategy=strat)
    sid = s["structure_id"]
    sym = leg_of(r, sid).inst.symbol
    p = _params(r, sid)
    r.market.tick(SBIN, round(r.spot(SBIN) * 0.977, 2))           # the underlying falls 2.3%
    bid = _bid_under(p.hard_sl_px)                                 # the stop, on an ~8% spread
    r.market.quote(sym, bid, round(floor_tick(bid * 1.08, TICK), 2))
    wait_for(r, lambda: state_of(r, i, book) == "CLOSED", 20, "the stop to close the CE")
    assert struct_of(r, i, book)["exit_reason"] == "HARD_SL"
    assert all(p_.req.side == Side.SELL and str(p_.req.role) == "EXIT" for p_ in exit_places(r))
    assert all(p_.req.side == Side.SELL and str(p_.req.role) == "EXIT" and p_.req.symbol == sym
               for p_ in exit_places(r)[n_exits:])
    assert all(p_.req.side == Side.BUY and str(p_.req.role) == "ENTRY" for p_ in entry_places(r))
    o = r.intent(SBIN, "DOWN", strategy=strat)                     # the "reverse": a PE on the same name
    assert route_of(r, o, book)["outcome"] == "REFUSED" and route_of(r, o, book)["gate"] == "COOLDOWN"
    names = [REL, INFY, "NSE:TCS", "NSE:ZYDUSLIFE", "NSE:SONACOMS", "NSE:HINDPETRO", SBIN]
    storm = []
    for k in range(20):                                            # a storm of triggers
        storm.append(r.intent(names[k % len(names)], strategy=strat))
        r.run(0.2)
    r.run(5)
    opened = [x for x in r.structures(book) if x["opened_ts"] and x["session"] == r.session.isoformat()]
    assert len(opened) <= 3
    assert len([p_ for p_ in entry_places(r) if p_.req.symbol and p_.order_pk]) <= 3
    # the storm's first trigger is the day's third entry; every later one is refused by the entries cap, not by the
    #   positions cap (one position open of the 2 allowed)
    assert [x["underlying"] for x in opened] == ["NSE:NIFTY 50", SBIN, REL]
    assert all((x["outcome"], x["gate"]) == ("REFUSED", "BOOK_CAPS") and x["detail"].startswith("ENTRIES: ")
               for x in (route_of(r, t, book) for t in storm[1:]))
    assert not r.halts("RUNAWAY")
    # never a PE bought on SBIN by this book
    assert not [x for x in r.structures(book) if x["underlying"] == SBIN and x["type"] == "LONG_PE"
                and x["opened_ts"]]


def test_pm_b_storm_within_a_minute_runaway(dual):
    """test_pm_b_no_reverse_churn as staged until 2026-10-13: the stop-out is the day's first entry and the storm of
    triggers comes seconds later. With MAX_ENTRIES_PER_DAY 3 the storm's first two triggers are the day's second and
    third entries, so its third finds three entry orders sent within 60 s: G09 checks RUNAWAY before the positions
    and entries caps, refuses it as RUNAWAY and raises HALT(RUNAWAY, BOOK, ACK) (FINAL_SPEC errata, 2026-10-13); G04
    then refuses every later trigger until the halt is acked. Still at most 3 entries, and never a PE on SBIN."""
    r = dual.runner()
    strat = "opening_drive"
    book = r.book(strat)
    i, s = enter(r, SBIN, strategy=strat)
    sid = s["structure_id"]
    sym = leg_of(r, sid).inst.symbol
    p = _params(r, sid)
    r.market.tick(SBIN, round(r.spot(SBIN) * 0.977, 2))           # the underlying falls 2.3%
    bid = _bid_under(p.hard_sl_px)                                 # the stop, on an ~8% spread
    r.market.quote(sym, bid, round(floor_tick(bid * 1.08, TICK), 2))
    wait_for(r, lambda: state_of(r, i, book) == "CLOSED", 20, "the stop to close the CE")
    assert struct_of(r, i, book)["exit_reason"] == "HARD_SL"
    assert all(p_.req.side == Side.SELL and str(p_.req.role) == "EXIT" and p_.req.symbol == sym
               for p_ in exit_places(r))
    assert all(p_.req.side == Side.BUY and str(p_.req.role) == "ENTRY" for p_ in entry_places(r))
    o = r.intent(SBIN, "DOWN", strategy=strat)                     # the "reverse": a PE on the same name
    assert route_of(r, o, book)["outcome"] == "REFUSED" and route_of(r, o, book)["gate"] == "COOLDOWN"
    names = [REL, INFY, "NSE:TCS", "NSE:ZYDUSLIFE", "NSE:SONACOMS", "NSE:HINDPETRO", SBIN]
    storm = []
    for k in range(20):                                            # a storm of triggers, seconds after the stop
        storm.append(r.intent(names[k % len(names)], strategy=strat))
        r.run(0.2)
    r.run(5)
    opened = [x for x in r.structures(book) if x["opened_ts"] and x["session"] == r.session.isoformat()]
    assert len(opened) <= 3
    assert len([p_ for p_ in entry_places(r) if p_.req.symbol and p_.order_pk]) <= 3
    # the storm's first two triggers are the day's second and third entries ...
    assert [x["underlying"] for x in opened] == [SBIN, REL, INFY]
    assert [x["intent_id"] for x in opened[1:]] == [t.intent_id for t in storm[:2]]
    # ... so its third meets three entry orders within 60 s: refused as RUNAWAY, and the book halts until an ack
    routes = [route_of(r, t, book) for t in storm]
    runaway = routes[2]
    assert (runaway["outcome"], runaway["gate"], runaway["detail"]) == (
        "REFUSED", "BOOK_CAPS", "RUNAWAY: 3 entry orders in 60 s (max 3)")
    (h,) = r.halts("RUNAWAY", f"BOOK:{book}")
    assert (h.clears, h.reason) == (HaltClears.ACK, runaway["detail"])
    assert {x.scope for x in r.halts("RUNAWAY")} == {f"BOOK:{book}", f"BOOK:PAPER:{strat}"}   # on LIVE, its twin too
    assert "HALT_RUNAWAY" in r.alert_codes()
    # every later trigger is refused by that halt
    assert all((x["outcome"], x["gate"], x["detail"]) == ("REFUSED", "HALTS", f"RUNAWAY@BOOK:{book}")
               for x in routes[3:])
    # never a PE bought on SBIN by this book
    assert not [x for x in r.structures(book) if x["underlying"] == SBIN and x["type"] == "LONG_PE"
                and x["opened_ts"]]


# ---------------------------------------------------------------------------------------------------------------
def _names(node: ast.AST) -> set[str]:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.add(n.value)
    return out


def _ltp_free(fn) -> bool:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    return not any("ltp" in name.lower() for name in _names(tree))


def test_pm_c_pnl_from_fills_only(dual):
    r = dual.runner(pub=True)
    i, s = enter(r)
    sid, sym, book = s["structure_id"], r.contract().symbol, r.book()
    r.market.quote(sym, 36.0, 36.1, ltp=44.0)                      # the LTP path runs far above the bids
    r.run(3)
    r.market.quote(sym, 41.0, 41.1, ltp=48.0)                      # take profit, on the bid
    wait_for(r, lambda: state_of(r, i) == "CLOSED", 15)
    (tr,) = r.trades(book)
    assert tr["exit_avg"] == 41.0 and tr["ltp_exit"] != tr["exit_avg"]
    net = r.net_from_fills(sid)
    assert math.isclose(tr["net"], net, abs_tol=0.005)
    counters = r.store.book_counters(book, r.session, r.clock.now_wall())      # what the risk gates read
    assert math.isclose(counters.realized_net, net, abs_tol=0.005)
    r.run(2)                                                       # status.json is rewritten every second
    status = json.loads((r.pub_dir / "status.json").read_text(encoding="utf-8"))
    assert math.isclose(status["books"][book]["realized_net"], net, abs_tol=0.005)
    # no LTP read anywhere on the P&L path (AST)
    src = Path(charges.__file__).read_text(encoding="utf-8")
    assert not any("ltp" in n.lower() for n in _names(ast.parse(src)))
    for fn in (risk.leg_mtm, risk.book_mtm, risk.book_day_net, risk.daily_stop_room, Engine.leg_mtm,
               ledger.value_order):
        assert _ltp_free(fn), f"{fn.__qualname__} reads an LTP"
    tree = ast.parse(textwrap.dedent(inspect.getsource(ledger.build_trade_row)))
    pnl = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
           and any(isinstance(t, ast.Name) and t.id in ("pnl", "values") for t in n.targets)]
    assert pnl and all("ltp" not in name.lower() for n in pnl for name in _names(n))


def test_pm_c_both_legs_impossible(dual):
    r = dual.runner()
    strat = "opening_drive"
    book = r.book(strat)
    i, s = enter(r, SBIN, strategy=strat)
    sid = s["structure_id"]
    sym = leg_of(r, sid).inst.symbol
    r.market.quote(sym, None, 30.0)                                # no bids: exiting, but nothing to sell into
    wait_for(r, lambda: state_of(r, i, book) == "EXITING", 20)
    pe = r.intent(SBIN, "DOWN", strategy=strat)
    route = route_of(r, pe, book)
    assert (route["outcome"], route["gate"]) == ("REFUSED", "UNDERLYING")
    other = r.make_intent(SBIN, "DOWN", strategy=strat)
    with pytest.raises(InvariantViolation) as err:                # the DB index holds the line on its own
        with r.store.tx():
            r.store.insert_intent(other, r.session)
            positions.reserve(r.store, other, book=book, account=dual.acct, policy=Policy.FULL,
                              now_mono=r.clock.mono(), now_wall=r.clock.now_wall())
    assert err.value.code == "I5"
    assert not r.rows("SELECT 1 FROM intents WHERE intent_id = ?", (other.intent_id,))
    r.market.quote(sym, 26.0, 26.1)
    wait_for(r, lambda: state_of(r, i, book) == "CLOSED", 20)
    assert [x["type"] for x in r.structures(book) if x["opened_ts"]] == ["LONG_CE"]


# ---------------------------------------------------------------------------------------------------------------
def test_pm_phantom_sl_exit(dual):
    r = dual.runner()
    i, s = enter(r, "NSE:ZYDUSLIFE")
    sid = s["structure_id"]
    sym = leg_of(r, sid).inst.symbol
    p = _params(r, sid)
    r.faults.positions_lag(6)                                     # positions() still shows the qty for 6 s
    r.market.quote(sym, _bid_under(p.hard_sl_px * 0.9), round(_bid_under(p.hard_sl_px * 0.9) + 0.1, 2))
    wait_for(r, lambda: any(o["state"] == "COMPLETE" for o in orders_of(r, sid, "EXIT")), 10, "the stop's SELL fill")
    (sell,) = [o for o in orders_of(r, sid, "EXIT") if o["state"] == "COMPLETE"]
    filled_at = r.clock.now_wall()
    assert sells_at_broker(r) == 1 and r.broker_net(sym) == 0      # a real SELL reached the broker
    while state_of(r, i) != "CLOSED":
        assert state_of(r, i) in ("EXITING", "VERIFY_FLAT")       # not closed until the broker shows net 0
        r.step(0.25)
        assert (r.clock.now_wall() - filled_at).total_seconds() < 20
    assert (r.clock.now_wall() - filled_at).total_seconds() >= 6.0 - 0.5
    (tr,) = r.trades(r.book())
    assert (tr["exit_reason"], tr["pnl_status"], tr["qty"]) == ("HARD_SL", "FINAL", sell["filled_qty"])
    assert not r.halts("MISMATCH")


# ---------------------------------------------------------------------------------------------------------------
def test_pm_d_stale_quote(dual):
    r = dual.runner()
    # SONACOMS (x2): the WS freezes at 28.00 while the book falls; REST shows 22.80 -> HARD_SL, debounced
    i, s = enter(r, "NSE:SONACOMS")
    sid = s["structure_id"]
    sym = leg_of(r, sid).inst.symbol
    p = _params(r, sid)
    r.market.quote(sym, 28.0, 28.1)
    r.run(1)
    r.market.freeze(sym)
    low = _bid_under(p.hard_sl_px, 3)
    assert p.hard_sl_px - p.profile.stop_confirm_bypass_pct / 100 * p.entry_avg < low <= p.hard_sl_px
    r.market.quote(sym, low, round(low + 0.1, 2))                 # the true book (and REST) fall; the WS is silent
    wait_for(r, lambda: state_of(r, i) != "OPEN", 12, "the REST-confirmed stop")
    r.market.unfreeze(sym)
    decisions = [e for e in r.events("EXIT_DECISION") if e["structure_id"] == sid]
    assert decisions[0]["payload"]["reason"] == "HARD_SL" and decisions[0]["payload"]["quote"]["src"] == "REST"
    assert [e for e in r.events("STOP_BREACH_PENDING") if e["structure_id"] == sid]    # debounced first
    wait_for(r, lambda: state_of(r, i) == "CLOSED", 15)
    assert struct_of(r, i)["exit_reason"] == "HARD_SL"
    # REST down as well: a protective exit within 5 s of the last quote
    j, t = enter(r, "NSE:TCS")
    tid = t["structure_id"]
    tsym = leg_of(r, tid).inst.symbol
    r.run(1)
    r.market.freeze(tsym)
    r.market.rest_fail(tsym)
    last_quote = r.clock.now_wall()
    wait_for(r, lambda: state_of(r, j) != "OPEN", 6, "the protective exit")
    decided = [e for e in r.events("EXIT_DECISION") if e["structure_id"] == tid][0]
    assert decided["payload"]["reason"] == "STALE_QUOTE_PROTECTIVE"
    assert (r.clock.now_wall() - last_quote).total_seconds() <= 5.0 + 0.1
    wait_for(r, lambda: bool(orders_of(r, tid, "EXIT")), 10, "the protective SELL")
    (x,) = orders_of(r, tid, "EXIT")
    assert x["band_idx"] >= 3                                      # protective pricing from the last bid
    r.market.unfreeze(tsym)
    r.market.rest_ok(tsym)
    wait_for(r, lambda: state_of(r, j) == "CLOSED", 20)


# ---------------------------------------------------------------------------------------------------------------
def test_pm_partial_fills_walk(dual):
    r = dual.runner()
    i, s = enter(r, "NSE:HINDPETRO")
    sid = s["structure_id"]
    leg = leg_of(r, sid)
    sym, lot = leg.inst.symbol, leg.inst.lot_size
    p = _params(r, sid)
    top = _bid_under(p.hard_sl_px)
    # a thin touch: one lot (half a lot at 0.5 participation) and no other bid within bands 0-1, then real depth
    bids = [(top, lot), (round(top - 4 * TICK, 2), 3 * lot)]
    r.market.depth(sym, bids, [(round(top + 0.5, 2), 4 * lot)])
    wait_for(r, lambda: state_of(r, i) == "CLOSED", 30, "the exit to walk the book")
    exits = orders_of(r, sid, "EXIT")
    # paper model 2: the touch's half lot fills an IOC whose draw is under 0.5; these three draw above it, so the
    #   walk down the book is the one under test
    assert [fill_draw_of(o) >= 0.5 for o in exits] == [True] * 3
    assert len(exits) == 3 and [o["filled_qty"] for o in exits] == [0, 0, lot]
    assert [o["band_idx"] for o in exits] == [0, 1, 2]
    assert exits[-1]["avg_fill_px"] == bids[-1][0]                # swept down to the deep level
    (tr,) = r.trades(r.book())
    assert math.isclose(tr["net"], r.net_from_fills(sid), abs_tol=0.005)
    assert tr["n_executed_orders"] == 2 and tr["brokerage"] == 2 * 20.0      # unfilled IOCs cost nothing
    assert tr["thin_book"] == 1


def test_pm_spread_spiral_blocked(dual):
    r = dual.runner()
    c = r.contract("NSE:SAMMAANCAP")
    r.market.quote(c.symbol, 21.20, 23.00)                         # an 8% spread
    r.run(0.5)
    i, s = enter(r, "NSE:SAMMAANCAP", expect="VOID")
    # §5.7's entry limit floor_tick(mid x 1.01) sits below the ask once the spread passes ~2%, so an 8% book is
    # refused at G18 before G19 sees it; a 1.8% book reaches the liquidity gate itself
    assert s["void_reason"] in ("SIZE:NO_ENTRY_LIMIT",) or s["void_reason"].startswith("LIQUIDITY:SPREAD")
    r.run(5 * 60 + 1, dt=5.0)                                      # the VOID cooldown
    r.market.quote(c.symbol, 22.10, 22.50)                         # a 1.8% spread: over the 1.5% cap
    r.run(0.5)
    j, t = enter(r, "NSE:SAMMAANCAP", expect="VOID")
    assert t["void_reason"].startswith("LIQUIDITY:SPREAD")
    assert entry_places(r) == []


def test_pm_e_same_path(dual):
    """The dual-run parity assertion itself: one round trip, the same commands and P&L on both accounts (the
    fixture compares them), and each account's broker ledger equals the engine's."""
    r = dual.runner()
    i, s = enter(r)
    r.market.quote(r.contract().symbol, 41.0, 41.1)
    wait_for(r, lambda: state_of(r, i) == "CLOSED", 15)
    rows = r.trades()
    book_nets = {}
    for row in rows:
        book_nets[row["book"]] = book_nets.get(row["book"], 0.0) + row["net"]
    order_books = {o["broker_order_id"]: (o["book"], o["external_kind"]) for o in r.orders()
                   if o["broker_order_id"] and o["filled_qty"]}
    if dual.live:
        trades = r.proc.reads.data.trades_today(Account.LIVE)
    else:
        trades = r.proc.paper.trades_today(Account.PAPER)
    tc = recon.eod_truth_check(dual.acct, r.session, book_nets, order_books, trades)
    assert tc.ok and tc.unattributed == ()
    assert len(r.places()) == 2


def test_pm_f_paper_fills_from_depth(dual):
    r = dual.runner()
    c = r.contract()
    lot = c.lot_size
    i = r.intent(REL)                                              # decided on the default book: ask 34.85
    r.market.depth(c.symbol, [(34.70, 4 * lot)], [(34.90, int(1.6 * lot)), (34.95, int(2.4 * lot)),
                                                   (35.00, 10 * lot)], ltp=33.00)       # the fill-time book
    wait_for(r, lambda: state_of(r, i) == "OPEN", 5)
    sid = struct_of(r, i)["structure_id"]
    (e,) = orders_of(r, sid, "ENTRY")
    # 1.6 lots at 34.90 are 0.8 of a lot at 0.5 participation: that lot fills there when the order's draw is under
    #   0.8 (paper model 2), else at 34.95, the level that completes it; never at the LTP
    assert e["limit_px"] == 34.95 and e["avg_fill_px"] == (34.90 if fill_draw_of(e) < 0.8 else 34.95)
    r.run(6)                                                       # the entry settles

    def book_moves_as_the_exit_goes_out(rr, cmd) -> None:
        # the exit order is sent after its post-decision positions read (decision B1); the book it meets has moved
        if isinstance(cmd, Place) and cmd.account is rr.acct and cmd.req.role is Role.EXIT:
            rr.market.depth(c.symbol, [(41.05, int(1.6 * lot)), (41.00, int(2.4 * lot)), (40.95, 10 * lot)],
                            [(41.20, 4 * lot)], ltp=45.0)

    r.dispatch_hooks.append(book_moves_as_the_exit_goes_out)
    r.market.quote(c.symbol, 41.10, 41.20)                         # take profit decided on 41.10 ...
    wait_for(r, lambda: state_of(r, i) == "CLOSED", 10)
    (x,) = orders_of(r, sid, "EXIT")
    # ... filled on the fill-time bids (41.05 holds 0.8 of a lot, like the entry's 34.90), never LTP
    assert x["avg_fill_px"] == (41.05 if fill_draw_of(x) < 0.8 else 41.00)
    assert x["decision_bid"] == 41.10


def test_pm_ledger_matches_broker(dual):
    def day(drift: float | None):
        r = dual.runner(start="14:41:00", warmup_until="14:42:30")
        i, s = enter(r)
        r.market.quote(r.contract().symbol, 41.0, 41.1)
        wait_for(r, lambda: state_of(r, i) == "CLOSED", 15)
        if drift is not None:
            r.faults.trades_drift(drift)
        r.run_until("15:34:00", dt=10.0)
        return r

    clean = day(None)
    checks = [e["payload"] for e in clean.events("EOD_TRUTH_CHECK") if e["payload"]["account"] == dual.acct.value]
    assert checks and checks[0]["ok"] and abs(checks[0]["ledger_net"] - checks[0]["broker_net"]) <= 1.0
    assert "EOD_TRUTH_MISMATCH" not in clean.alert_codes()
    drifted = day(0.10)
    checks = [e["payload"] for e in drifted.events("EOD_TRUTH_CHECK") if e["payload"]["account"] == dual.acct.value]
    assert checks and not checks[0]["ok"]
    assert "EOD_TRUTH_MISMATCH" in drifted.alert_codes()
