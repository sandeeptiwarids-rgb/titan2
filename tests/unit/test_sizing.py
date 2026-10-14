"""U09: titan2.core.sizing — the single size() (spec §8.5): refusals, every term, and no hidden scaling."""

from __future__ import annotations

import ast
import inspect
from dataclasses import fields, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from titan2.charges import rates_for, round_trip_charges
from titan2.core import exits, sizing
from titan2.core.sizing import size, top_qty
from titan2.model import ExitProfile, Instrument, Intent, Level, Policy, Quote, Refusal, Sized
from titan2.settings import DEFAULTS

IST = timezone(timedelta(hours=5, minutes=30))
WALL = datetime(2026, 10, 12, 9, 25, tzinfo=IST)
R26 = rates_for(date(2026, 10, 12))
PROFILE = ExitProfile()
FULL, QUALITY = Policy.FULL, Policy.QUALITY


def inst(lot=500, tick=0.05, name="RELIANCE"):
    return Instrument(f"NFO:{name}26OCT2900CE", 1001, f"{name}26OCT2900CE", "NFO", name, f"NSE:{name}", "CE",
                      2900.0, date(2026, 10, 27), lot, tick)


def quote(bid=31.95, ask=32.00, bid_qty=2000, ask_qty=2000, bids=None, asks=None):
    bids = [(bid, bid_qty)] if bids is None else bids
    asks = [(ask, ask_qty)] if asks is None else asks
    return Quote("NFO:X", 1001, tuple(bids), tuple(asks), None, 0, None, None, 1000.0, WALL, "WS")


def sz(i=None, q=None, limit=32.05, profile=PROFILE, s=DEFAULTS, day_net=0.0, policy=FULL):
    return size(inst() if i is None else i, quote() if q is None else q, limit, profile, s, day_net, R26, policy)


def risk_per_lot(lot, px, sl_pct=15.0, slip_pct=5.0):
    return lot * px * (sl_pct + slip_pct) / 100 + round_trip_charges(px, lot, R26)


# ---------------------------------------------------------------------------------------------------------------
# Acceptance tests
# ---------------------------------------------------------------------------------------------------------------
def test_size_refusals_terms():
    # the default 1-lot trade: every term is computed, the first smallest one binds
    ok = sz()
    assert ok == Sized(qty=500, lots=1, binding="DEPTH", per_lot_risk_rs=pytest.approx(risk_per_lot(500, 32.05)))
    assert ok.per_lot_risk_rs == pytest.approx(500 * 32.05 * 0.20 + round_trip_charges(32.05, 500, R26))

    # a zero, negative, NaN, infinite, bool or non-numeric cap refuses: it never means "no limit"
    for key in ("MAX_LOTS", "MAX_UNITS", "MAX_LOSS_PER_TRADE_RS", "MAX_PREMIUM_PER_TRADE_RS"):
        for bad in (0, 0.0, -1, float("nan"), float("inf"), float("-inf"), True, False, "1", None):
            assert sz(s=replace(DEFAULTS, **{key: bad})) == Refusal("SIZE", "INVALID_OR_ZERO_CAP"), (key, bad)
    for key, bad in (("LIQ_DEPTH_MULT", 0.0), ("LIQ_DEPTH_MULT", float("nan")), ("DAILY_STOP_RS", float("nan")),
                     ("DAILY_STOP_RS", -1.0), ("MIN_PREMIUM_RS", float("nan")), ("STOP_SLIP_ALLOW_PCT", -5.0),
                     ("STOP_SLIP_ALLOW_PCT", None)):
        assert sz(s=replace(DEFAULTS, **{key: bad})) == Refusal("SIZE", "INVALID_SETTING"), (key, bad)
    for lot in (0, -500, 500.0, True, None, "500"):
        assert sz(i=inst(lot=lot)) == Refusal("SIZE", "LOT_INVALID"), lot
    for limit in (None, float("nan"), float("inf"), 0.0, -32.0, True):
        assert sz(limit=limit) == Refusal("SIZE", "LIMIT_INVALID"), limit
    for sl in (0.0, float("nan")):
        assert sz(profile=replace(PROFILE, hard_sl_pct=sl)) == Refusal("SIZE", "PROFILE_INVALID")
    for policy in ("PAPER", None, 1):
        assert sz(policy=policy) == Refusal("SIZE", "POLICY_INVALID"), policy
    assert sz(policy="FULL") == ok and isinstance(sz(policy="QUALITY", day_net=None), Sized)

    # S1 premium floor (Rs 20): no ask, a cheap ask, or a cheap limit; the cheap-lot bombs (k07, pm_a)
    assert sz(q=quote(asks=[])) == Refusal("SIZE", "PREMIUM_FLOOR")
    assert sz(q=quote(bid=19.90, ask=19.95), limit=20.0) == Refusal("SIZE", "PREMIUM_FLOOR")
    assert sz(q=quote(bid=19.95, ask=20.00), limit=19.95) == Refusal("SIZE", "PREMIUM_FLOOR")
    deep = dict(bid_qty=100_000, ask_qty=100_000)
    assert sz(i=inst(lot=4300), q=quote(bid=3.45, ask=3.50, **deep), limit=3.55) == Refusal("SIZE", "PREMIUM_FLOOR")
    assert sz(i=inst(lot=6750), q=quote(bid=3.45, ask=3.50, **deep), limit=3.55) == Refusal("SIZE", "PREMIUM_FLOOR")
    # lot 4,300 at Rs 25: one lot risks Rs 21.5k > Rs 15k -> refused (RISK binds first; PREMIUM is 0 too)
    assert sz(i=inst(lot=4300), q=quote(bid=24.95, ask=25.0, **deep), limit=25.05) == Refusal("SIZE", "RISK")

    # RISK: with the defaults one lot passes only while premium x 20% + charges <= Rs 15,000 (~Rs 73.9k premium);
    #   the Rs 50,000 PREMIUM cap refuses long before that (past both, RISK is named: it is checked first)
    assert sz(q=quote(bid=99.95, ask=100.0), limit=100.0) == Sized(500, 1, "RISK", pytest.approx(
        risk_per_lot(500, 100.0)))
    assert sz(q=quote(bid=100.0, ask=100.05), limit=100.05) == Refusal("SIZE", "PREMIUM")
    assert risk_per_lot(500, 147.80) > 15000 and sz(q=quote(bid=147.75, ask=147.80), limit=147.80) == Refusal(
        "SIZE", "RISK")
    # ... and one tick lower RISK still allows the lot, so the Rs 15,000 edge sits between 147.75 and 147.80
    assert risk_per_lot(500, 147.75) <= 15000 and sz(q=quote(bid=147.70, ask=147.75), limit=147.75) == Refusal(
        "SIZE", "PREMIUM")

    # DEPTH: top-3 levels on BOTH sides must hold LIQ_DEPTH_MULT x lot (3 x 500 = 1,500)
    thin_bids = [(31.95, 500), (31.90, 500), (31.85, 400), (31.80, 100_000)]     # the 4th level does not count
    assert sz(q=quote(bids=thin_bids)) == Refusal("SIZE", "DEPTH")
    assert sz(q=quote(asks=[(32.0, 1499)], bid_qty=100_000)) == Refusal("SIZE", "DEPTH")
    assert isinstance(sz(q=quote(bids=thin_bids[:2] + [(31.85, 500)])), Sized)  # exactly 1,500 passes
    assert sz(q=quote(bid_qty=0)) == Refusal("SIZE", "DEPTH")              # a 0-qty level is no bid at all
    assert sz(q=quote(bids=[])) == Refusal("SIZE", "DEPTH")                 # no bids: nothing to exit into

    # LOTS: a fractional cap truncates to 0 lots and refuses (never rounds up)
    assert sz(s=replace(DEFAULTS, MAX_LOTS=0.5)) == Refusal("SIZE", "LOTS")
    nifty, nq = inst(lot=65, name="NIFTY"), quote(bid=99.95, ask=100.0, bid_qty=100_000, ask_qty=100_000)
    assert sz(i=nifty, q=nq, limit=100.0) == Sized(65, 1, "LOTS", pytest.approx(risk_per_lot(65, 100.0)))

    # beyond the phase-1 ceiling (Settings are built unvalidated here) each term can bind on its own
    big = replace(DEFAULTS, MAX_LOTS=10, MAX_LOSS_PER_TRADE_RS=5000.0, MAX_PREMIUM_PER_TRADE_RS=25000.0,
                  DAILY_STOP_RS=10000.0)             # the caps before 2026-10-13, so each term binds in turn
    assert sz(i=nifty, q=nq, limit=100.0, s=big) == Sized(195, 3, "RISK", pytest.approx(risk_per_lot(65, 100.0)))
    tight_sl = replace(PROFILE, hard_sl_pct=5.0)
    assert sz(i=nifty, q=nq, limit=100.0, s=big, profile=tight_sl) == Sized(
        195, 3, "PREMIUM", pytest.approx(risk_per_lot(65, 100.0, sl_pct=5.0)))
    assert sz(i=nifty, q=nq, limit=100.0, s=big, day_net=-7000.0) == Sized(
        130, 2, "DAY_BUDGET", pytest.approx(risk_per_lot(65, 100.0)))

    # UNITS: a 6,000-lot contract can never fit MAX_UNITS 5,000 (isolated here by lifting the rupee caps)
    huge = inst(lot=6000)
    hq = quote(bid=19.95, ask=20.0, bid_qty=20_000, ask_qty=20_000)
    loose = replace(DEFAULTS, MAX_LOSS_PER_TRADE_RS=1e9, MAX_PREMIUM_PER_TRADE_RS=1e9)
    assert sz(i=huge, q=hq, limit=20.05, s=loose) == Refusal("SIZE", "UNITS")
    assert sz(i=huge, q=hq, limit=20.05) == Refusal("SIZE", "RISK")          # with the defaults RISK binds first

    # DAY_BUDGET (FULL only): what is left of the daily stop must cover one lot's worst case
    assert sz(day_net=-12_000.0) == Refusal("SIZE", "DAY_BUDGET")
    assert sz(day_net=+50_000.0) == ok                                      # profits never add budget
    for unknown in (None, float("nan"), float("inf")):
        assert sz(day_net=unknown) == Refusal("SIZE", "DAY_PNL_UNKNOWN")
    # QUALITY books have no day budget and need no day P&L
    assert sz(day_net=-14_999.0, policy=QUALITY) == ok          # Rs 1 of the Rs 15,000 stop left: FULL would refuse
    assert sz(day_net=-14_999.0) == Refusal("SIZE", "DAY_BUDGET")
    assert sz(day_net=None, policy=QUALITY) == ok

    # top_qty sums the best n levels
    assert top_qty((Level(1.0, 10), Level(0.9, 20), Level(0.8, 30), Level(0.7, 40))) == 60
    assert top_qty((Level(1.0, 10),), 3) == 10 and top_qty((), 3) == 0


def test_size_no_multiplier_param():
    forbidden = "multi" + "plier"                    # never spelled out, so no scan of this file can trip on it
    params = list(inspect.signature(size).parameters)
    # the day term is the realized net (review round 3 follow-up: the open positions count by their stop-outs)
    assert params == ["inst", "q", "limit_px", "profile", "s", "realized_net", "rates", "policy",
                      "open_risk_rs", "live_book", "account_realized_net", "account_open_risk_rs"]
    assert all(inspect.signature(size).parameters[p].kind is inspect.Parameter.KEYWORD_ONLY for p in params[8:])
    assert not any("mult" in p.lower() or "scale" in p.lower() for p in params)
    with pytest.raises(TypeError):
        size(inst(), quote(), 32.05, PROFILE, DEFAULTS, 0.0, R26, FULL, **{forbidden: 2})
    # no such identifier (or word) in the sizing or exit code
    for mod in (sizing, exits):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert forbidden not in src.lower(), mod.__name__
        idents = set()
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.Name):
                idents.add(n.id)
            elif isinstance(n, ast.Attribute):
                idents.add(n.attr)
            elif isinstance(n, ast.arg):
                idents.add(n.arg)
            elif isinstance(n, ast.keyword) and n.arg:
                idents.add(n.arg)
        assert not [i for i in idents if forbidden in i.lower()], mod.__name__
    # an Intent carries no quantity: size() alone decides it, as a whole number of lots
    assert not {f.name for f in fields(Intent)} & {"qty", "quantity", "lots", "size"}
    r = size(inst(), quote(), 32.05, PROFILE, replace(DEFAULTS, MAX_LOTS=1), 0.0, R26, FULL)
    assert isinstance(r, Sized) and r.qty == r.lots * 500 and r.lots == 1
