"""titan2.condor: the PAPER NIFTY weekly iron condor tracker (no orders), on a fake Kite and the real calendar."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta

import pytest

from titan2 import charges, condor, laptop
from titan2.clock import IST, RealClock, SessionCalendar
from titan2.condor import Ctx, Q

CAL = SessionCalendar.load()
EXPIRY = date(2026, 10, 19)                    # a Monday: Tuesday 10-20 is a holiday
WED, THU, FRI, NEXT_WED = date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 21)
F0, SIGMA = 22500.0, 0.13
LOT = 65


def at(d: date, hh: int = 15, mm: int = 25, ss: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=IST)


# ---------------------------------------------------------------------------------------------------------------
# Pricing, calendar, strikes
# ---------------------------------------------------------------------------------------------------------------
def test_black76_parity_and_straddle_iv():
    T = 3 / 252
    for K in (21500.0, 22500.0, 23500.0):
        c, p = condor.black76(F0, K, T, SIGMA, "CE"), condor.black76(F0, K, T, SIGMA, "PE")
        assert c - p == pytest.approx(F0 - K, abs=1e-6)                       # put-call parity, rates 0
    straddle = condor.black76(F0, 22500, T, SIGMA, "CE") + condor.black76(F0, 22500, T, SIGMA, "PE")
    assert condor.straddle_iv(straddle, F0, 22500, T) == pytest.approx(SIGMA, abs=1e-6)
    assert condor.straddle_iv(0.0, F0, 22500, T) is None and condor.straddle_iv(1e9, F0, 22500, T) is None


def test_entry_day_is_the_third_trading_day_before_expiry():
    assert condor.entry_day_for(EXPIRY, CAL) == WED                          # Thu, Fri, Mon left
    assert condor.trading_days_left(WED, EXPIRY, CAL) == 3
    assert condor.entry_day_for(date(2026, 10, 27), CAL) == date(2026, 10, 22)   # the holiday on 10-20 skipped
    assert condor.entry_day_for(date(2026, 10, 13), CAL) == date(2026, 10, 8)
    assert condor.entry_day_for(date(2027, 1, 5), CAL) is None              # beyond the calendar: holidays unknown


def test_listed_strikes_move_outward():
    ks = [21900.0, 22000.0, 22100.0, 22300.0]
    assert condor.listed(ks, 22000.0, "down") == 22000.0
    assert condor.listed(ks, 22200.0, "down") == 22100.0 and condor.listed(ks, 22200.0, "up") == 22300.0
    assert condor.listed(ks, 21800.0, "down") is None


def opts(expiry: date = EXPIRY, lo: int = 20000, hi: int = 25000, tag: str = "26OCT") -> list[dict]:
    rows, tok = [], 1000 if tag == "26OCT" else 50000
    for k in range(lo, hi + 1, 50):
        for kind in ("CE", "PE"):
            tok += 1
            rows.append({"instrument_token": tok, "tradingsymbol": f"NIFTY{tag}{k}{kind}", "name": "NIFTY",
                         "instrument_type": kind, "segment": "NFO-OPT", "strike": float(k),
                         "expiry": expiry.isoformat(), "lot_size": LOT, "tick_size": 0.05})
    return rows


FUT_ROW = {"instrument_token": 9, "tradingsymbol": "NIFTY26OCTFUT", "name": "NIFTY", "instrument_type": "FUT",
           "segment": "NFO-FUT", "strike": 0.0, "expiry": "2026-10-27", "lot_size": LOT, "tick_size": 0.05}


def book(mid: float, now: datetime, spread: float | None = None, qty: int = 1800) -> dict:
    s = spread if spread is not None else max(0.10, 0.01 * mid)
    bid = max(0.05, round(math.floor((mid - s / 2) / 0.05) * 0.05, 2))
    ask = round(math.ceil((mid + s / 2) / 0.05) * 0.05, 2)
    return {"last_price": mid, "timestamp": now.replace(tzinfo=None),
            "depth": {"buy": [{"price": bid, "quantity": qty, "orders": 3}] + [{"price": 0, "quantity": 0}] * 4,
                      "sell": [{"price": ask, "quantity": qty, "orders": 3}] + [{"price": 0, "quantity": 0}] * 4}}


def priced(rows: list[dict], now: datetime, F: float = F0, sigma: float = SIGMA, td: int = 3) -> dict[str, dict]:
    """Every option's book at its Black-76 price (T = td/252), plus spot, VIX and the future."""
    out = {}
    for r in rows:
        if r["instrument_type"] not in ("CE", "PE"):
            continue
        mid = condor.black76(F, r["strike"], td / 252, sigma, r["instrument_type"])
        out["NFO:" + r["tradingsymbol"]] = book(max(mid, 0.05), now)
    stamp = now.replace(tzinfo=None)
    out[condor.SPOT_SYMBOL] = {"last_price": F - 2.0, "timestamp": stamp, "depth": {}}
    out[condor.VIX_SYMBOL] = {"last_price": 13.4, "timestamp": stamp, "depth": {}}
    out["NFO:NIFTY26OCTFUT"] = {"last_price": F + 3.0, "timestamp": stamp, "depth": {}}
    return out


def test_plan_matches_the_research_example():
    """edge_research_verdict.md: F 22,500 and IV 13% -> sell the 22000 PE and 23000 CE, buy the 21550 PE and 23450
    CE (wings 2% of F = 450)."""
    now = at(WED)
    chain = condor.nifty_chain(opts())[EXPIRY]
    quotes = {s: condor.parse_quote(b, now) for s, b in priced(opts(), now).items()}
    plan = condor.plan_strikes(chain, quotes, F0 - 2.0, 3)
    assert plan.F == pytest.approx(F0, abs=1.0) and plan.sigma == pytest.approx(SIGMA, abs=0.004)
    assert plan.strikes == {"SP": 22000.0, "SC": 23000.0, "LP": 21550.0, "LC": 23450.0}
    assert plan.legs["SP"].symbol == "NFO:NIFTY26OCT22000PE" and plan.legs["LC"].symbol == "NFO:NIFTY26OCT23450CE"
    assert condor.nifty_future(opts() + [FUT_ROW], WED) == "NFO:NIFTY26OCTFUT"


# ---------------------------------------------------------------------------------------------------------------
# Fills and money
# ---------------------------------------------------------------------------------------------------------------
def test_walk_and_fill_prices():
    assert condor.walk(((10.0, 40), (10.5, 40)), 65) == (pytest.approx((40 * 10 + 25 * 10.5) / 65), False)
    assert condor.walk(((10.0, 40),), 65) == (pytest.approx(10.0), True)          # thin: the rest at the last level
    assert condor.walk((), 65) == (None, True)
    q = Q(9.9, 10.1, 10.0, ((9.9, 500),), ((10.1, 500),), 0.0)
    assert condor.fill_prices(q, True, 65, 0.05) == {"real": 9.9, "x2": 9.8, "thin": False}     # sell: bid; - s/2
    assert condor.fill_prices(q, False, 65, 0.05) == {"real": 10.1, "x2": 10.2, "thin": False}  # buy: ask; + s/2
    thin = Q(10.0, 10.1, 10.0, ((10.0, 30), (9.0, 100)), ((10.1, 500),), 0.0)        # the review's example
    f = condor.fill_prices(thin, True, 65, 0.05)
    assert f["real"] == pytest.approx((30 * 10 + 35 * 9) / 65, abs=1e-4) and f["x2"] < f["real"]   # 2x never better
    no_bid = Q(None, 0.10, None, (), ((0.10, 500),), 0.0)
    assert condor.fill_prices(no_bid, True, 65, 0.05) == {"real": 0.0, "x2": 0.0, "thin": True, "abandoned": True}
    assert condor.fill_prices(Q(0.05, None, None, ((0.05, 9),), (), 0.0), False, 65, 0.05)["real"] is None


def test_leg_marks():
    assert condor.leg_mark(Q(9.9, 10.1, None, (), (), 0.0), True) == pytest.approx(10.0)
    assert condor.leg_mark(Q(None, 0.10, None, (), (), 0.0), False) == pytest.approx(0.05)      # half the ask
    assert condor.leg_mark(Q(None, 0.10, None, (), (), 0.0), True) == pytest.approx(0.05)
    assert condor.leg_mark(Q(3.0, None, None, (), (), 0.0), True) is None       # a short with no ask: unknown
    assert condor.leg_mark(Q(3.0, None, None, (), (), 0.0), False) == 3.0
    assert condor.leg_mark(None, True) is None and condor.leg_mark(None, False) == 0.0


def test_settlement_cases():
    r = charges.rates_for(EXPIRY)
    k = {"SP": 22000.0, "SC": 23000.0, "LP": 21550.0, "LC": 23450.0}
    gst_brk = r.brokerage_per_order * (1 + r.gst)
    assert condor.settlement(k, 22400.0, LOT, r) == (0.0, 0.0)                    # inside: all expire worthless
    assert condor.settlement(k, 21800.0, LOT, r) == (pytest.approx(200.0), round(gst_brk, 2))   # short put ITM
    v, c = condor.settlement(k, 21400.0, LOT, r)                                  # beyond the wing: capped at W
    assert v == pytest.approx(450.0) and c == round(2 * gst_brk + charges.exercise_stt(150.0, LOT, r), 2)
    v, c = condor.settlement(k, 23700.0, LOT, r)
    assert v == pytest.approx(450.0)


def test_entry_and_exit_money_signs():
    r = charges.rates_for(WED)
    fills = {"SP": {"real": 10.0, "x2": 9.8}, "SC": {"real": 9.0, "x2": 8.8},
             "LP": {"real": 2.0, "x2": 2.2}, "LC": {"real": 1.5, "x2": 1.7}}
    credit, cost = condor.entry_money(fills, LOT, r, "real")
    assert credit == pytest.approx(15.5) and cost > 4 * r.brokerage_per_order    # two sells pay STT
    credit_2x, _ = condor.entry_money(fills, LOT, r, "x2")
    assert credit_2x == pytest.approx(14.7)
    debit, xcost = condor.exit_money(fills, LOT, r, "real")
    assert debit == pytest.approx(15.5) and xcost > 0
    abandoned = {**fills, "LP": {"real": 0.0, "x2": 0.0, "abandoned": True}}
    debit, xcost3 = condor.exit_money(abandoned, LOT, r, "real")
    assert debit == pytest.approx(17.5) and xcost3 < xcost                       # no order for the abandoned wing


# ---------------------------------------------------------------------------------------------------------------
# The week, on a fake Kite
# ---------------------------------------------------------------------------------------------------------------
class FakeKite:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.book: dict[str, dict] = {}
        self.closes: dict[tuple[int, date], float] = {}
        self.calls: list[str] = []
        self.fail_quote: Exception | None = None

    def instruments(self, exchange):
        self.calls.append("instruments")
        return self.rows

    def quote(self, symbols):
        self.calls.append("quote")
        if self.fail_quote is not None:
            raise self.fail_quote
        return {s: self.book[s] for s in symbols if s in self.book}

    def historical_data(self, token, start, end, interval):
        self.calls.append(f"hist:{token}:{start.date()}:{interval}")
        px = self.closes.get((token, start.date()))
        return [] if px is None else [{"date": datetime.combine(start.date(), datetime.min.time()), "close": px}]

    def basket_order_margins(self, orders, consider_positions=False):
        assert [o["transaction_type"] for o in orders] == ["BUY", "BUY", "SELL", "SELL"]     # wings first
        return {"final": {"total": 84_428.0}}


@pytest.fixture
def week(tmp_path):
    rows = opts() + [FUT_ROW]
    kite = FakeKite(rows)

    def ctx(d: date, hh: int = 15, mm: int = 25) -> Ctx:
        return Ctx(tmp_path, kite, CAL, at(d, hh, mm), out=lambda s: None, sleep=lambda s: None)

    return kite, rows, ctx, tmp_path


def _all(var):
    con = condor.connect(var)
    try:
        return [dict(r) for r in condor._rows(con)]
    finally:
        con.close()


def _settle(ctx):
    con = condor.connect(ctx.var)
    try:
        return condor.settle_due(ctx, con)
    finally:
        con.close()


def test_a_week_held_to_settlement(week):
    kite, rows, ctx, var = week
    kite.book = priced(rows, at(WED))
    assert condor.tick(ctx(date(2026, 10, 13))) == "NOT_ENTRY_DAY"
    assert condor.tick(ctx(WED)) == "ENTERED"
    assert condor.tick(ctx(WED)) == "ALREADY_TICKED"
    (row,) = _all(var)
    e = json.loads(row["entry"])
    assert (row["state"], row["expiry"], row["entry_day"], row["qty"]) == ("OPEN", "2026-10-19", "2026-10-14", LOT)
    assert e["strikes"] == {"SP": 22000.0, "SC": 23000.0, "LP": 21550.0, "LC": 23450.0}
    assert e["margin"] == 84_428.0 and e["td"] == 3 and 0 < row["credit_2x"] < row["credit"]
    assert e["market"]["future"] == "NFO:NIFTY26OCTFUT" and e["market"]["future_px"] == F0 + 3.0
    # Thursday: a quiet mark; the entry day's close of each leg recorded from day bars
    kite.book = priced(rows, at(THU), td=2)
    for leg in e["legs"].values():
        kite.closes[(leg["token"], WED)] = 5.0 if leg["symbol"].endswith(("22000PE", "23000CE")) else 1.0
    assert condor.tick(ctx(THU)) == "HELD,NOT_ENTRY_DAY"
    (row,) = _all(var)
    assert row["close_credit"] == pytest.approx(8.0) and json.loads(row["close_legs"])["SP"] == 5.0
    # Monday (expiry): no stop even far beyond 3x the credit
    kite.book = priced(rows, at(EXPIRY), F=22010.0, td=1)
    assert condor.tick(ctx(EXPIRY)).startswith("MARKED_EXPIRY_DAY")
    assert _all(var)[0]["state"] == "OPEN"
    # the next session settles at NIFTY 50's close on expiry day: 21,800 puts the short put 200 in the money
    assert _settle(ctx(NEXT_WED, 9, 30)) == []                                    # the close is not out yet
    kite.closes[(condor.SPOT_TOKEN, EXPIRY)] = 21800.0
    assert _settle(ctx(NEXT_WED, 9, 30)) == ["2026-10-19"]
    assert _settle(ctx(NEXT_WED, 9, 31)) == []                                    # never twice
    (row,) = _all(var)
    r = charges.rates_for(EXPIRY)
    want = round((row["credit"] - 200.0) * LOT - e["charges"] - round(r.brokerage_per_order * (1 + r.gst), 2), 2)
    assert (row["state"], row["exit_reason"], row["settle_px"], row["net"]) == ("CLOSED", "SETTLED", 21800.0, want)
    assert row["net_nostop"] == row["net"] and condor.reproduces(row) and condor.kill_status([row]) is None


def test_the_3x_stop_closes_shorts_at_the_ask(week):
    kite, rows, ctx, var = week
    kite.book = priced(rows, at(WED))
    assert condor.tick(ctx(WED)) == "ENTERED"
    (row,) = _all(var)
    e = json.loads(row["entry"])
    # Friday: NIFTY falls to 22,050: the short put's value explodes past 3x the credit
    kite.book = priced(rows, at(FRI), F=22050.0, td=1)
    assert condor.tick(ctx(FRI)) == "STOPPED,NOT_ENTRY_DAY"
    (row,) = _all(var)
    ex = json.loads(row["exit"])
    assert (row["state"], row["exit_reason"], row["exit_day"]) == ("CLOSED", "STOP_3X", "2026-10-16")
    assert ex["value_mid"] >= condor.STOP_MULT * row["credit"] and row["net"] < 0 and row["net_2x"] < row["net"]
    sp_ask = kite.book[e["legs"]["SP"]["symbol"]]["depth"]["sell"][0]["price"]
    assert ex["fills"]["SP"]["real"] == pytest.approx(sp_ask)                      # bought back at the ask
    assert condor.reproduces(row) and -row["net"] < row["max_loss"]
    # its no-stop result comes at the next session after expiry
    kite.closes[(condor.SPOT_TOKEN, EXPIRY)] = 22600.0
    _settle(ctx(NEXT_WED, 9, 30))
    (row,) = _all(var)
    assert row["exit_reason"] == "STOP_3X" and row["net_nostop"] > 0 > row["net"]  # it would have recovered


def test_the_stop_fires_with_a_bidless_wing_and_abandons_it(week):
    """The review's case: NIFTY rallies to 23,000 on Friday and the far put wing shows no bid: the stop still fires,
    the wing is left to expire (no order) and the P&L still reproduces."""
    kite, rows, ctx, var = week
    kite.book = priced(rows, at(WED))
    assert condor.tick(ctx(WED)) == "ENTERED"
    kite.book = priced(rows, at(FRI), F=23000.0, td=1)
    kite.book["NFO:NIFTY26OCT21550PE"]["depth"]["buy"] = []
    assert condor.tick(ctx(FRI)) == "STOPPED,NOT_ENTRY_DAY"
    (row,) = _all(var)
    fills = json.loads(row["exit"])["fills"]
    assert fills["LP"] == {"real": 0.0, "x2": 0.0, "thin": True, "abandoned": True}
    assert row["state"] == "CLOSED" and condor.reproduces(row)


def test_a_short_without_an_ask_postpones_the_stop(week):
    kite, rows, ctx, var = week
    kite.book = priced(rows, at(WED))
    assert condor.tick(ctx(WED)) == "ENTERED"
    kite.book = priced(rows, at(FRI), F=22050.0, td=1)
    kite.book["NFO:NIFTY26OCT22000PE"]["depth"]["sell"] = []
    assert condor.tick(ctx(FRI)) == "MARK_INCOMPLETE,NOT_ENTRY_DAY"
    assert _all(var)[0]["state"] == "OPEN"


def test_a_week_is_skipped_without_two_sided_quotes(week):
    kite, rows, ctx, var = week
    kite.book = priced(rows, at(WED))
    kite.book["NFO:NIFTY26OCT23450CE"]["depth"]["sell"] = []                       # the call wing has no ask
    assert condor.tick(ctx(WED)) == "SKIP"
    assert _all(var) == []
    con = condor.connect(var)
    try:
        (n,) = con.execute("SELECT detail FROM notes WHERE kind = 'SKIP'").fetchall()
    finally:
        con.close()
    assert "NO_TWO_SIDED_QUOTE: NFO:NIFTY26OCT23450CE" in n["detail"]


@pytest.mark.parametrize("ask_ticks", [0, -1], ids=["locked", "crossed"])     # the ask at the bid, or a tick under
def test_a_week_is_skipped_on_a_locked_or_crossed_leg(week, ask_ticks):
    """Review of paper model 2 (probe test_probe_condor_locked): a leg whose bid equals its ask had a mid, so the
    entry passed it and booked it at a zero spread, in its real fill, its 2x-spreads fill and the kill rule's median
    spread. A locked book, like a crossed one, has a side left behind by a move of the other, which no exchange
    shows (the paper exchange's rule, paper model 2): no two-sided quote, so the week is skipped as for a missing
    side. A mark still values a locked leg at its price (leg_mark), as before."""
    kite, rows, ctx, var = week
    kite.book = priced(rows, at(WED))
    depth = kite.book["NFO:NIFTY26OCT23450CE"]["depth"]                         # the call wing
    depth["sell"][0]["price"] = round(depth["buy"][0]["price"] + 0.05 * ask_ticks, 2)
    assert condor.tick(ctx(WED)) == "SKIP"
    assert _all(var) == []
    con = condor.connect(var)
    try:
        (n,) = con.execute("SELECT detail FROM notes WHERE kind = 'SKIP'").fetchall()
    finally:
        con.close()
    assert "NO_TWO_SIDED_QUOTE: NFO:NIFTY26OCT23450CE" in n["detail"]


def test_a_locked_quote_is_no_two_sided_quote():
    """The probe's own quote: bid = ask = 12.00, fresh and stamped. It passed the entry check and would have filled at
    {"real": 12.0, "x2": 12.0}, no spread at all. A mark still uses its price."""
    raw = {"depth": {"buy": [{"price": 12.0, "quantity": 750}], "sell": [{"price": 12.0, "quantity": 750}]},
           "timestamp": at(WED, 15, 25, 4).replace(tzinfo=None), "last_price": 12.0}
    q = condor.parse_quote(raw, at(WED, 15, 25, 5))
    assert (q.bid, q.ask, q.age_s) == (12.0, 12.0, 1.0)
    assert condor._stale({"NFO:NIFTY": q}) == "NO_TWO_SIDED_QUOTE: NFO:NIFTY"
    assert condor.leg_mark(q, True) == 12.0 and condor.leg_mark(q, False) == 12.0


def test_stale_or_unstamped_quotes_skip_the_week(week):
    kite, rows, ctx, var = week
    kite.book = priced(rows, at(WED, 15, 20))                                       # 5 minutes old at 15:25
    assert condor.tick(ctx(WED)) == "SKIP" and _all(var) == []
    kite.book = priced(rows, at(WED))
    for sym in ("NFO:NIFTY26OCT22000PE",):
        kite.book[sym].pop("timestamp")
    assert condor.tick(ctx(WED), force=True) == "SKIP" and _all(var) == []


def test_a_high_volatility_week_quotes_its_far_wings(week):
    """The review's case: at sigma 0.35 the call wing is 8%+ above spot; the legs get their own snapshot."""
    kite, rows, ctx, var = week
    kite.book = priced(rows, at(WED), sigma=0.35)
    assert condor.tick(ctx(WED)) == "ENTERED"
    e = json.loads(_all(var)[0]["entry"])
    assert e["strikes"]["LC"] / F0 - 1 > 0.08


def test_a_failed_tick_is_not_recorded_so_it_can_be_retried(week):
    kite, rows, ctx, var = week
    kite.book = priced(rows, at(WED))
    kite.fail_quote = ValueError("Kite says no")
    assert condor.tick(ctx(WED)) == "ENTRY_FAILED"
    kite.fail_quote = None
    assert condor.tick(ctx(WED)) == "ENTERED"


def test_an_entry_day_on_the_last_expiry_day_still_enters(week, tmp_path):
    """A condor expiring today needs no more decisions: it does not block the next expiry's entry (here a made-up
    Friday 10-16 expiry, whose entry day is Tuesday 10-13, itself an expiry day)."""
    kite, rows, ctx, var = week
    this = date(2026, 10, 13)
    kite.rows = opts(this) + opts(FRI, tag="FRI") + [FUT_ROW]
    kite.book = {**priced(opts(this), at(this)), **priced(opts(FRI, tag="FRI"), at(this))}
    con = condor.connect(var)
    try:
        con.execute("INSERT INTO condors(expiry, entry_day, state, qty, credit, credit_2x, max_loss, entry) VALUES "
                    "('2026-10-13', '2026-10-08', 'OPEN', 65, 10.0, 9.0, 28000.0, ?)",
                    (json.dumps({"legs": {r: {"symbol": f"NFO:NIFTY26OCT2{i}000PE", "tick": 0.05}
                                          for i, r in enumerate(condor.LEG_ROLES)}}),))
        con.commit()
    finally:
        con.close()
    assert condor.tick(ctx(this)) == "MARKED_EXPIRY_DAY,ENTERED"
    assert [r["expiry"] for r in _all(var)] == ["2026-10-13", "2026-10-16"]


def test_kite_calls_are_paced():
    slept: list[float] = []
    clock = [100.0]
    ctx = Ctx(None, None, CAL, at(WED), out=lambda s: None, sleep=lambda s: slept.append(s) or None,
              mono=lambda: clock[0])
    ctx.call(lambda: 1, "a")
    clock[0] += 0.3
    ctx.call(lambda: 2, "b")
    assert slept == [pytest.approx(condor.KITE_PAUSE_S - 0.3)]
    with pytest.raises(condor.CondorError, match="c failed: ValueError: no"):
        ctx.call(lambda: (_ for _ in ()).throw(ValueError("no")), "c")


# ---------------------------------------------------------------------------------------------------------------
# Kill rules
# ---------------------------------------------------------------------------------------------------------------
def _closed(i: int, net: float, net_2x: float | None = None, max_loss: float = 28_000.0,
            close_credit: float | None = None, spread=(0.5, 0.5)) -> dict:
    legs = {r: {"fill": {"real": 1.0, "x2": 1.0}, "spread": {"real": spread[0], "model": spread[1]}}
            for r in condor.LEG_ROLES}
    return {"expiry": (date(2026, 1, 6) + timedelta(weeks=i)).isoformat(), "state": "CLOSED", "net": net,
            "net_2x": net if net_2x is None else net_2x, "max_loss": max_loss, "credit": 10.0,
            "close_credit": close_credit, "entry": json.dumps({"legs": legs}), "qty": LOT}


def test_kill_rules(monkeypatch):
    monkeypatch.setattr(condor, "reproduces", lambda r: True)
    ok = [_closed(i, 300.0) for i in range(30)]
    assert condor.kill_status(ok) is None
    assert condor.kill_status(ok + [_closed(31, -30_000.0)]).startswith("LOSS_BEYOND_MAX")
    few = [_closed(i, 300.0 if i < 19 else -500.0) for i in range(26)]              # 19 wins in the first 26
    assert condor.kill_status(few).startswith("TOO_FEW_WINS: 19 wins in the first 26")
    deep = [_closed(i, 300.0, net_2x=-3_000.0) for i in range(15)]
    assert condor.kill_status(deep).startswith("DRAWDOWN")
    short = [_closed(i, 300.0, close_credit=12.5) for i in range(12)]               # 10 / 12.5 = 80% < 85%
    assert condor.kill_status(short).startswith("CREDIT_SHORT")
    wide = [_closed(i, 300.0, close_credit=10.0, spread=(1.1, 0.5)) for i in range(12)]
    assert condor.kill_status(wide).startswith("SPREAD_WIDE")
    monkeypatch.setattr(condor, "reproduces", lambda r: False)
    assert condor.kill_status(ok).startswith("PNL_MISMATCH")


def test_a_killed_test_enters_no_more(week, monkeypatch):
    kite, rows, ctx, var = week
    kite.book = priced(rows, at(WED))
    monkeypatch.setattr(condor, "kill_status", lambda rows: "DRAWDOWN: test")
    assert condor.tick(ctx(WED)) == "SKIP_KILLED" and _all(var) == []


# ---------------------------------------------------------------------------------------------------------------
# The laptop job and the CLI
# ---------------------------------------------------------------------------------------------------------------
class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def now_wall(self) -> datetime:
        return self.t


def test_job_settles_once_then_ticks_once_in_the_window(tmp_path, monkeypatch):
    seen: list[tuple[str, datetime]] = []
    monkeypatch.setattr(condor, "tick", lambda ctx, force=False: seen.append(("tick", ctx.now)) or "X")
    monkeypatch.setattr(condor, "settle_due", lambda ctx, con: seen.append(("settle", ctx.now)) or [])
    clock = Clock(at(WED, 9, 10))
    condor.connect(tmp_path).close()                          # a condor.db exists: there may be something to settle
    job = condor.CondorJob(tmp_path, {}, clock, CAL, out=lambda s: None, kite_factory=lambda: object())
    for t in (at(WED, 9, 10), at(WED, 12, 0), at(WED, 15, 24), at(WED, 15, 25), at(WED, 15, 26), at(WED, 15, 31)):
        clock.t = t
        job.poll()
        job.join(5)
    assert seen == [("settle", at(WED, 9, 10)), ("tick", at(WED, 15, 25))]
    fresh = condor.CondorJob(tmp_path / "new", {}, Clock(at(WED, 15, 25)), CAL, out=lambda s: None,
                             kite_factory=lambda: object())
    fresh.poll()                                              # no condor.db: no settle, straight to the tick
    fresh.join(5)
    assert seen[-1] == ("tick", at(WED, 15, 25)) and len(seen) == 3


def test_job_retries_a_failed_tick_twice_45_s_apart(tmp_path, monkeypatch):
    outcomes = iter(["ENTRY_FAILED", "MARK_FAILED,NOT_ENTRY_DAY", "ENTERED", "never"])
    seen: list[datetime] = []
    monkeypatch.setattr(condor, "tick", lambda ctx, force=False: seen.append(ctx.now) or next(outcomes))
    clock = Clock(at(WED, 15, 25))
    job = condor.CondorJob(tmp_path, {}, clock, CAL, out=lambda s: None, kite_factory=lambda: object())
    for t in (at(WED, 15, 25), at(WED, 15, 25, 30), at(WED, 15, 25, 46), at(WED, 15, 26, 20), at(WED, 15, 26, 40),
              at(WED, 15, 27, 40), at(WED, 15, 28, 40)):
        clock.t = t
        job.poll()
        job.join(5)
    assert seen == [at(WED, 15, 25), at(WED, 15, 25, 46), at(WED, 15, 26, 40)]   # 3 attempts, the last succeeds


def test_laptop_attaches_the_job_only_to_real_time_sessions(tmp_path):
    out: list[str] = []
    assert laptop._condor_job({}, tmp_path, RealClock(), CAL, out.append, real_time=False) is None   # --date
    assert laptop._condor_job({"TITAN2_CONDOR": "0"}, tmp_path, RealClock(), CAL, out.append, True) is None
    job = laptop._condor_job({}, tmp_path, RealClock(), CAL, out.append, real_time=True)
    assert isinstance(job, condor.CondorJob) and out == []


def test_cli_status_tick_window_and_the_laptop_lock(tmp_path):
    out: list[str] = []
    assert condor.cli("status", tmp_path, {}, out.append) == 0 and "no condor yet" in out[-1]
    free = dict(kite=object(), cal=CAL, laptop_runs=lambda v: False)
    assert condor.cli("tick", tmp_path, {}, out.append, now=at(WED, 11, 0), **free) == 2
    assert out[-1].startswith("refused: the condor tick runs only between 15:25 and 15:29:30")
    assert condor.cli("tick", tmp_path, {}, out.append, now=at(WED), kite=object(), cal=CAL,
                      laptop_runs=lambda v: True) == 2
    assert out[-1] == "refused: a titan2 session is running; it does the condor's tick itself"
    assert condor.cli("bogus", tmp_path, {}, out.append, now=at(WED), **free) == 2
    odd = tmp_path / "a#b%c"                                  # the read-only URI copes with # and %
    condor.connect(odd).close()
    assert condor.cli("status", odd, {}, out.append) == 0 and "test: running (0 of 52 weeks entered)" in out[-1]
