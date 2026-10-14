"""titan2.whatif: refused entries replayed on minute bars under the strategy's exit rules, after charges."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from titan2 import charges, store as store_mod, whatif
from titan2.clock import IST
from titan2.core import exits
from titan2.charges import OrderFills
from titan2.model import ExitProfile, Side
from titan2.whatif import Bar, Refused

DAY = date(2026, 10, 13)
RATES = charges.rates_for(DAY)
PROFILE = ExitProfile()                                   # 15% stop, lock 4/3% over breakeven, 15% TP, 45 min
T0 = datetime(2026, 10, 13, 11, 53, 15, tzinfo=IST)
FLATTEN = datetime(2026, 10, 13, 15, 15, tzinfo=IST)
QTY, HALF = 100, 0.5


def bars(*ohlc, start=T0.replace(second=0) + timedelta(minutes=1)):
    return [Bar(start + timedelta(minutes=i), *map(float, x)) for i, x in enumerate(ohlc)]


def levels(entry=100.0):
    c_in = charges.order_charges(OrderFills(Side.BUY, QTY, QTY * entry), RATES).total
    return exits.compute_exit_params(PROFILE, entry, QTY, c_in, RATES, T0)


def run(bs, entry=100.0, at=T0, flatten=FLATTEN):
    return whatif.replay(entry, HALF, QTY, at, bs, PROFILE, RATES, flatten)


def test_hard_stop_at_its_level_and_through_a_gap():
    r = run(bars((100, 101, 99, 100), (99, 99.5, 84, 85)))
    assert (r.reason, r.exit, r.exit_at.minute) == ("HARD_SL", pytest.approx(levels().hard_sl_px), 55)
    r = run(bars((100, 101, 99, 100), (80, 81, 79, 80)))                 # opened below the stop: sold at the open
    assert (r.reason, r.exit) == ("HARD_SL", pytest.approx(79.5))


def test_take_profit_and_worst_first_inside_a_minute():
    r = run(bars((100, 101, 99, 100), (104, 116, 99, 115)))
    assert (r.reason, r.exit) == ("TAKE_PROFIT", pytest.approx(levels().tp_px))
    r = run(bars((100, 117, 84, 100)))                                    # both in one minute: the low counts first
    assert r.reason == "HARD_SL"


def test_lock_arms_then_the_trail_stops_it():
    p = levels()
    r = run(bars((100, 110, 99.8, 109), (108, 108.5, 106, 106.5)))
    trail = p.be_px + PROFILE.trail_retain * (110 - HALF - p.be_px)
    assert r.reason == "LOCK_STOP" and r.exit == pytest.approx(max(trail, p.floor_px))
    assert r.exit > p.be_px                                               # a locked trade never ends below breakeven


def test_time_stop_eod_flatten_and_no_bars():
    flat = bars(*[(100, 100.4, 99.6, 100)] * 60)
    r = run(flat)
    assert r.reason == "TIME_STOP" and r.exit_at >= levels().time_stop_at and r.exit == pytest.approx(99.5)
    late = T0.replace(hour=15, minute=5)
    r = run(bars(*[(100, 100.4, 99.6, 100)] * 20, start=late.replace(second=0) + timedelta(minutes=1)), at=late)
    assert (r.reason, r.exit_at.strftime("%H:%M")) == ("EOD_FLATTEN", "15:15")
    assert run([]) is None and run(bars((100, 101, 99, 100), start=T0.replace(second=0))) is None   # nothing after
    r = run(bars((100, 101, 99, 100.2)))
    assert (r.reason, r.exit) == ("DATA_END", pytest.approx(99.7))


def refused(bid=99.0, ask=100.0, ts=T0):
    return Refused(ts, "price_spike_up", "PAPER:price_spike_up", "abc123", "NSE:KPITTECH", "SIZE",
                   "PREMIUM_FLOOR: Rs 12.40 < Rs 20", "NFO:KPITTECH26OCT1300CE", 4242, QTY, bid, ask)


def test_evaluate_buys_at_the_ask_seen_and_nets_charges():
    row = whatif.evaluate(refused(), bars((100, 116, 99.8, 115)), PROFILE, RATES, FLATTEN)
    o = row["outcome"]
    assert (row["why"], o["entry"], o["reason"], o["assumed_spread"]) == ("SIZE:PREMIUM_FLOOR", 100.0, "TAKE_PROFIT",
                                                                         False)
    pnl = whatif.net_of(100.0, o["exit"], QTY, RATES)
    assert o["net"] == pnl.net < o["gross"] and o["charges"] > 0


def test_evaluate_without_a_quote_assumes_the_spread():
    row = whatif.evaluate(refused(None, None), bars((200, 201, 199, 200), (200, 201, 199, 200)), PROFILE, RATES,
                          FLATTEN)
    o = row["outcome"]
    assert o["assumed_spread"] and o["entry"] == pytest.approx(200 * (1 + whatif.DEFAULT_HALF_SPREAD), abs=0.01)
    assert whatif.evaluate(refused(None, None), [], PROFILE, RATES, FLATTEN)["outcome"] is None


@pytest.mark.parametrize("bid, ask", [(101.0, 100.0), (100.0, 100.0)], ids=["crossed", "locked"])
def test_evaluate_takes_a_crossed_or_locked_quote_as_no_quote(bid, ask):
    """Review of paper model 2: a refused entry whose quote was crossed was bought at its stale ask, under the bid (a
    price nobody offered any more), which flattered exactly the refusals for a crossed quote. A crossed or locked
    quote is no quote now, as for the paper exchange: the next minute's open plus the assumed half-spread."""
    bs = bars((100.5, 101, 100, 100.5), (100.5, 101, 100, 100.5))
    o = whatif.evaluate(refused(bid, ask), bs, PROFILE, RATES, FLATTEN)["outcome"]
    assert o["assumed_spread"] and o["entry"] == pytest.approx(100.5 * (1 + whatif.DEFAULT_HALF_SPREAD), abs=0.01)
    assert o == whatif.evaluate(refused(None, None), bs, PROFILE, RATES, FLATTEN)["outcome"]
    assert whatif.evaluate(refused(bid, ask), [], PROFILE, RATES, FLATTEN)["outcome"] is None
    # a quote with an ask and no bid is still bought at its ask, with the assumed half-spread
    o = whatif.evaluate(refused(None, 100.0), bs, PROFILE, RATES, FLATTEN)["outcome"]
    assert o["entry"] == 100.0 and o["assumed_spread"]


def test_short_detail_and_refused_from():
    assert whatif.short_detail("FETCH_FAILED: AMBIGUOUS:ReadTimeout: x") == "FETCH_FAILED"
    assert whatif.short_detail("") == "?"
    ev = {"ts": T0.isoformat(), "strategy": "s", "book": "PAPER:s", "intent_id": "i1",
          "payload": {"gate": "QUOTE", "detail": "FETCH_FAILED: x", "quotes": None,
                      "legs": [{"symbol": "NFO:X", "token": 7, "lot_size": 50}]}}
    r = whatif.refused_from(ev)
    assert (r.symbol, r.token, r.lot, r.bid, r.ask, r.why) == ("NFO:X", 7, 50, None, None, "QUOTE:FETCH_FAILED")
    assert whatif.refused_from({**ev, "payload": {"legs": []}}) is None


class FakeKite:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def historical_data(self, token, start, end, interval):
        self.calls.append((token, start, end, interval))
        return self.rows


def _db_with_refusal(var, payload, book="PAPER:price_spike_up"):
    path = var / "db" / store_mod.DB_FILENAME
    path.parent.mkdir(parents=True)
    st = store_mod.Store(path)
    try:
        with store_mod.core_thread(), st.tx():
            st.append_event("ENTRY_REFUSED", wall=T0, mono=1.0, payload=payload, book=book,
                            strategy="price_spike_up", intent_id="abc123")
            st.append_event("ENTRY_REFUSED", wall=T0, mono=1.0, payload=payload, book="SHADOW:price_spike_up",
                            strategy="price_spike_up", intent_id="abc123")   # same intent: once
    finally:
        st.close()


def test_run_replays_saves_and_totals(tmp_path):
    sym = "NFO:KPITTECH26OCT1300CE"
    _db_with_refusal(tmp_path, {"gate": "SIZE", "detail": "PREMIUM_FLOOR", "direction": "UP", "trigger_px": 1290.0,
                                "spot": 1290.5, "legs": [{"symbol": sym, "token": 4242, "lot_size": 100}],
                                "quotes": {sym: {"bid": 12.3, "ask": 12.4}}})
    rows = [{"date": (T0.replace(second=0) + timedelta(minutes=m)), "open": 12.4, "high": 14.5, "low": 12.3,
             "close": 14.4} for m in range(0, 5)]
    kite, out = FakeKite(rows), []
    assert whatif.run(tmp_path, DAY, {}, out=out.append, kite=kite, sleep=lambda s: None) == 0
    assert len(kite.calls) == 1 and kite.calls[0][0] == 4242 and kite.calls[0][3] == "minute"
    saved = json.loads(whatif.report_path(tmp_path, DAY).read_text(encoding="utf-8"))
    assert saved["day"] == "2026-10-13" and len(saved["rows"]) == 1
    o = saved["rows"][0]["outcome"]
    assert o["entry"] == 12.4 and o["reason"] == "TAKE_PROFIT" and o["net"] > 0
    assert "KPITTECH26OCT1300CE" in out[0] and "SIZE:PREMIUM_FLOOR" in out[0]
    assert "SIZE:PREMIUM_FLOOR" in out[1] and "1 trades,   1 winners" in out[1]


def test_run_without_db_or_refusals(tmp_path):
    out: list[str] = []
    assert whatif.run(tmp_path, DAY, {}, out=out.append, kite=FakeKite([])) == 2
    _db_with_refusal(tmp_path / "v", {"gate": "SIZE", "detail": "X", "legs": []})       # no contract: skipped
    assert whatif.run(tmp_path / "v", DAY, {}, out=out.append, kite=FakeKite([])) == 0
    assert out[-1] == "What-if for 2026-10-13: no refused entry with a recorded contract"
