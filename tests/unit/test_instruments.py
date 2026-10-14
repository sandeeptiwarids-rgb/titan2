"""U05: titan2.instruments (dump checks, DTE rule, strike selection, eligibility, ban list). Offline only."""

from __future__ import annotations

import dataclasses
import itertools
import math
from datetime import date, timedelta

import pytest

from titan2 import instruments as ins
from titan2.clock import SessionCalendar
from titan2.instruments import (
    BanList, BanListError, InstrumentsError, InstrumentStore, dump_dir, first_eligible_expiry, load_ban_list,
    load_dump, parse_ban_list, pick_strike, prep_instruments, prune_cache, save_dump,
)
from titan2.model import Refusal, StrikePref

SESSION = date(2026, 10, 12)                            # a Monday
D = date
# What NSE lists from mid-October 2026: Tuesday weeklies, last-Tuesday monthlies; a Tuesday holiday moves the expiry
# to the Monday before (Tue 20 Oct Dussehra, Tue 10 Nov Balipratipada, Tue 24 Nov Gurpurb). No weekly in a monthly week.
NIFTY_EXPIRIES = [D(2026, 10, 13), D(2026, 10, 19), D(2026, 10, 27), D(2026, 11, 3), D(2026, 11, 9), D(2026, 11, 17),
                  D(2026, 11, 23), D(2026, 12, 1), D(2026, 12, 8), D(2026, 12, 15), D(2026, 12, 22), D(2026, 12, 29)]
MONTHLY = [D(2026, 10, 27), D(2026, 11, 23), D(2026, 12, 29)]
EXPIRED_MONTHLY = D(2026, 9, 29)
BOM = chr(0xFEFF)
_MON = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


@pytest.fixture(scope="module")
def cal() -> SessionCalendar:
    return SessionCalendar.load()


# ---------------------------------------------------------------------------------------------------------------
# A synthetic dump in kiteconnect's parsed row format (instrument_token int, expiry date, strike/tick float, lot int)
# ---------------------------------------------------------------------------------------------------------------
def _ts(name: str, expiry: date, strike: float, opt: str) -> str:
    k = int(strike) if float(strike).is_integer() else strike
    if expiry in MONTHLY or expiry == EXPIRED_MONTHLY:
        return f"{name}{expiry.year % 100}{_MON[expiry.month - 1]}{k}{opt}"
    return f"{name}{expiry.year % 100}{'123456789OND'[expiry.month - 1]}{expiry.day:02d}{k}{opt}"


def nfo_row(token: int, name: str, expiry: date, strike: float, opt: str, lot: int, tick: float = 0.05,
            segment: str = "NFO-OPT") -> dict:
    return {"instrument_token": token, "exchange_token": str(token // 256),
            "tradingsymbol": _ts(name, expiry, strike, opt), "name": name, "last_price": 0.0, "expiry": expiry,
            "strike": float(strike), "tick_size": tick, "lot_size": lot, "instrument_type": opt, "segment": segment,
            "exchange": "NFO"}


def nse_row(token: int, tradingsymbol: str, segment: str = "NSE") -> dict:
    index = segment == "INDICES"
    return {"instrument_token": token, "exchange_token": str(token // 256), "tradingsymbol": tradingsymbol,
            "name": tradingsymbol, "last_price": 0.0, "expiry": "", "strike": 0.0,
            "tick_size": 0.0 if index else 0.05, "lot_size": 0 if index else 1, "instrument_type": "EQ",
            "segment": segment, "exchange": "NSE"}


def steps(lo: float, hi: float, step: float) -> list[float]:
    return [round(lo + i * step, 2) for i in range(int(round((hi - lo) / step)) + 1)]


def make_dump(session: date = SESSION, n_stocks: int = 110, with_expired: bool = False) -> tuple[list, list]:
    tok = itertools.count(5_000_001)
    nfo: list[dict] = []
    nse = [nse_row(256265, "NIFTY 50", "INDICES"), nse_row(260105, "NIFTY BANK", "INDICES"),
           nse_row(257801, "NIFTY FIN SERVICE", "INDICES"), nse_row(779521, "SBIN"), nse_row(2953217, "TCS")]

    def chain(name: str, expiries: list[date], strikes: list[float], lot: int, tick: float = 0.05) -> None:
        for e in expiries:
            if e < session and not (with_expired and e == EXPIRED_MONTHLY):
                continue
            for k in strikes:
                for opt in ("CE", "PE"):
                    nfo.append(nfo_row(next(tok), name, e, k, opt, lot, tick))

    old = [EXPIRED_MONTHLY] if with_expired else []
    chain("NIFTY", old + NIFTY_EXPIRIES, steps(24800, 25200, 50), 65)
    chain("BANKNIFTY", old + MONTHLY, steps(55000, 56000, 100), 30)
    chain("FINNIFTY", MONTHLY, steps(26000, 26400, 100), 60)              # an index option we never trade
    real = {"RELIANCE": (2953217 + 1, steps(2800, 3000, 20), 500), "M&M": (519937, steps(3100, 3300, 50), 200),
            "SAIL": (758529, steps(120, 140, 2.5), 4700)}
    for name, (token, strikes, lot) in real.items():
        nse.append(nse_row(token, name))
        chain(name, old + MONTHLY, strikes, lot)
    for i in range(n_stocks - len(real)):
        name = f"STK{i:03d}"                                           # filler F&O stocks, minimal chains
        nse.append(nse_row(7_000_001 + i, name))
        chain(name, MONTHLY[-1:], [100.0, 105.0], 1000)
    chain("ZZNOEQ", MONTHLY, steps(10, 20, 5), 9000)                     # F&O name without an NSE EQ row
    for e in MONTHLY:                                                     # futures rows are ignored
        nfo.append(nfo_row(next(tok), "RELIANCE", e, 0, "FUT", 500, segment="NFO-FUT") | {"instrument_type": "FUT"})
    return nfo, nse


def clone(rows: list[dict]) -> list[dict]:
    return [dict(r) for r in rows]                                        # rows are flat: a shallow copy suffices


def build(cal: SessionCalendar, session: date = SESSION, dump: tuple[list, list] | None = None,
          **opts) -> InstrumentStore:
    nfo, nse = dump if dump is not None else make_dump(session)
    return InstrumentStore.from_rows(nfo, nse, session, cal, **opts)


def row_of(nfo: list[dict], tradingsymbol: str) -> dict:
    (row,) = [r for r in nfo if r["tradingsymbol"] == tradingsymbol]
    return row


def refused(x: object, detail_prefix: str) -> bool:
    return isinstance(x, Refusal) and x.gate == "CONTRACT" and x.detail.startswith(detail_prefix)


# ---------------------------------------------------------------------------------------------------------------
# Acceptance tests
# ---------------------------------------------------------------------------------------------------------------
def test_sanity_fails_closed(cal, tmp_path):
    nfo, nse = make_dump()
    store = InstrumentStore.from_rows(nfo, nse, SESSION, cal)            # the baseline dump is sane
    assert store.summary["stocks"] == 110 and store.summary["indices"] == 2

    def fails(mutate, expect: str, **opts) -> None:
        n, s = clone(nfo), clone(nse)
        mutate(n, s)
        with pytest.raises(InstrumentsError) as ei:
            InstrumentStore.from_rows(n, s, SESSION, cal, **opts)
        assert expect in str(ei.value), str(ei.value)

    def drop(pred):
        def mutate(n, s):
            n[:] = [r for r in n if not pred(r)]
        return mutate

    def set_first(name, field, value):
        def mutate(n, s):
            next(r for r in n if r["name"] == name and r["segment"] == "NFO-OPT")[field] = value
        return mutate

    # NIFTY and BANKNIFTY must list CE and PE for >= 2 expiries each
    fails(drop(lambda r: r["name"] == "BANKNIFTY" and r["instrument_type"] == "PE" and r["expiry"] != MONTHLY[0]),
          "BANKNIFTY lists CE and PE for 1 expiries")
    fails(drop(lambda r: r["name"] == "NIFTY"), "NIFTY lists CE and PE for 0 expiries")
    # every kept option row has lot, tick and strike > 0 (and a tick the 2-dp price math can use)
    fails(set_first("RELIANCE", "lot_size", 0), "lot_size 0")
    fails(set_first("NIFTY", "lot_size", "abc"), "lot_size")
    fails(set_first("SAIL", "tick_size", 0.0), "tick 0.0")
    fails(set_first("SAIL", "tick_size", 0.001), "tick 0.001")
    fails(set_first("SAIL", "tick_size", float("nan")), "tick nan")
    fails(set_first("M&M", "strike", 0.0), "strike 0.0")
    fails(set_first("M&M", "expiry", "soon"), "expiry 'soon'")
    fails(set_first("M&M", "expiry", "2026-13-45"), "M&M row")
    fails(set_first("NIFTY", "instrument_type", "XX"), "opt_type 'XX'")
    # tokens for NIFTY 50 / NIFTY BANK; >= 100 stocks; no duplicate tokens
    fails(lambda n, s: s.__setitem__(slice(None), [r for r in s if r["tradingsymbol"] != "NIFTY BANK"]),
          "no NSE token for NSE:NIFTY BANK")
    fails(lambda n, s: None, "only 110 stock underlyings (need >= 111)", min_stocks=111)
    small_nfo, small_nse = make_dump(n_stocks=99)
    with pytest.raises(InstrumentsError, match="only 99 stock underlyings"):
        InstrumentStore.from_rows(small_nfo, small_nse, SESSION, cal)

    def dup_token(n, s):
        n[1]["instrument_token"] = n[0]["instrument_token"]
    fails(dup_token, "duplicate")
    # an empty or non-list dump
    fails(lambda n, s: n.clear(), "NFO dump is empty")
    with pytest.raises(InstrumentsError, match="NFO dump is not a list"):
        InstrumentStore.from_rows(None, nse, SESSION, cal)
    with pytest.raises(InstrumentsError, match="NSE dump is empty"):
        InstrumentStore.from_rows(nfo, [], SESSION, cal)

    # rows that are never traded are not checked: FINNIFTY, names without an NSE EQ row, other equities
    n, s = clone(nfo), clone(nse)
    next(r for r in n if r["name"] == "FINNIFTY")["lot_size"] = 0
    next(r for r in n if r["name"] == "ZZNOEQ")["strike"] = "garbage"
    next(r for r in s if r["tradingsymbol"] == "SBIN")["instrument_token"] = "x"
    tolerant = InstrumentStore.from_rows(n, s, SESSION, cal)
    assert tolerant.summary["names_without_eq_row_sample"] == ["ZZNOEQ"]
    assert tolerant.underlying("NSE:ZZNOEQ") is None and tolerant.underlying("FINNIFTY") is None
    # an index listed twice with different tokens fails; an equity listed twice only drops that stock
    fails(lambda n, s: s.append(nse_row(999, "NIFTY 50", "INDICES")), "listed twice")
    n, s = clone(nfo), clone(nse)
    s += [nse_row(256265, "NIFTY 50", "INDICES"), nse_row(4242, "RELIANCE")]   # same-token repeat is harmless
    twice = InstrumentStore.from_rows(n, s, SESSION, cal)
    assert twice.underlying("NSE:RELIANCE") is None and twice.underlying("NIFTY").token == 256265
    assert twice.summary["names_without_eq_row_sample"] == ["RELIANCE", "ZZNOEQ"]

    # prep: a bad dump raises and is never cached; a previous day's cache is never used
    class FakeData:
        def __init__(self, n, s):
            self.rows, self.calls = {"NFO": n, "NSE": s}, []

        def instruments(self, exchange):
            self.calls.append(exchange)
            return clone(self.rows[exchange])

    bad = clone(nfo)
    bad[0]["tick_size"] = -0.05
    with pytest.raises(InstrumentsError, match="tick -0.05"):
        prep_instruments(FakeData(bad, nse), tmp_path, SESSION, cal)
    assert not dump_dir(tmp_path, SESSION).exists()
    yesterday = D(2026, 10, 9)
    save_dump(tmp_path, yesterday, *make_dump(yesterday))
    data = FakeData(nfo, nse)
    store = prep_instruments(data, tmp_path, SESSION, cal)
    assert data.calls == ["NFO", "NSE"] and load_dump(tmp_path, SESSION) is not None
    assert prep_instruments(FakeData([], []), tmp_path, SESSION, cal).summary == store.summary   # today's cache
    # a corrupt cache for today is fetched again, never trusted
    (dump_dir(tmp_path, SESSION) / "NFO.json.gz").write_bytes(b"not gzip")
    with pytest.raises(InstrumentsError, match="unreadable"):
        load_dump(tmp_path, SESSION)
    refetch = FakeData(nfo, nse)
    prep_instruments(refetch, tmp_path, SESSION, cal)
    assert refetch.calls == ["NFO", "NSE"]

    class KiteDown(FakeData):                    # broker errors reach the lane unchanged, so it can classify them
        def instruments(self, exchange):
            raise ConnectionError("kite down")
    with pytest.raises(ConnectionError):
        prep_instruments(KiteDown([], []), tmp_path / "elsewhere", SESSION, cal)
    # a store is only ever used for its own session
    assert refused(store.select_contract("NSE:RELIANCE", "CE", "ATM", 2900.0, D(2026, 10, 13)), "DUMP_NOT_TODAY")
    assert refused(store.select_contract("NSE:RELIANCE", "CE", "ATM", 2900.0, yesterday), "DUMP_NOT_TODAY")


def test_lot_tick_from_row(cal, tmp_path):
    def revised(session: date) -> tuple[list, list]:
        nfo, nse = make_dump(session)
        for r in nfo:
            if r["name"] == "NIFTY" and r["expiry"] >= D(2026, 10, 27):
                r["lot_size"] = 75                     # a semi-annual lot revision on the later contracts
            if r["name"] == "RELIANCE" and r["expiry"] == D(2026, 11, 23):
                r["lot_size"], r["tick_size"] = 250, 0.1
        return nfo, nse

    nfo, nse = revised(SESSION)
    store = build(cal, dump=(nfo, nse))
    inst = store.select_contract("NSE:NIFTY 50", "CE", StrikePref.ATM, 25010.0, SESSION)
    row = row_of(nfo, inst.tradingsymbol)
    assert (inst.symbol, inst.token, inst.exchange, inst.name, inst.underlying) == (
        "NFO:" + row["tradingsymbol"], row["instrument_token"], "NFO", "NIFTY", "NSE:NIFTY 50")
    assert (inst.opt_type, inst.strike, inst.expiry, inst.lot_size, inst.tick) == (
        "CE", 25000.0, D(2026, 10, 19), 65, 0.05)
    bank = store.select_contract("NSE:NIFTY BANK", "PE", "ATM", 55420.0, SESSION)
    assert (bank.lot_size, bank.expiry, bank.strike) == (30, D(2026, 10, 27), 55400.0)
    # Friday 16 Oct: the Monday-19 weekly has 1 session left, so it rolls to 27 Oct, whose row says lot 75
    fri = D(2026, 10, 16)
    rolled = build(cal, fri, dump=revised(fri)).select_contract("NSE:NIFTY 50", "CE", "ATM", 25010.0, fri)
    assert (rolled.expiry, rolled.lot_size) == (D(2026, 10, 27), 75)
    # Wednesday 21 Oct: RELIANCE's October monthly is at E-4 and rolls to November: lot and tick from that row
    wed = D(2026, 10, 21)
    stock = build(cal, wed, dump=revised(wed)).select_contract("NSE:RELIANCE", "PE", "ITM1", 2905.0, wed)
    assert (stock.expiry, stock.lot_size, stock.tick, stock.strike) == (D(2026, 11, 23), 250, 0.1, 2920.0)
    # the JSON cache keeps every field and type
    save_dump(tmp_path, SESSION, nfo, nse)
    cached = InstrumentStore.from_cache(tmp_path, SESSION, cal)
    again = cached.select_contract("NSE:NIFTY 50", "CE", StrikePref.ATM, 25010.0, SESSION)
    assert again == inst and type(again.lot_size) is int and type(again.expiry) is date
    assert InstrumentStore.from_cache(tmp_path, D(2026, 10, 13), cal) is None          # nothing cached for that day


def test_expiry_index2_stock5(cal):
    index_cases = {
        D(2026, 10, 9): D(2026, 10, 13),    # Fri: Tue 13 has 2 sessions (Mon, Tue) -> keep
        D(2026, 10, 12): D(2026, 10, 19),   # Mon before a Tuesday expiry: 1 session -> roll
        D(2026, 10, 15): D(2026, 10, 19),   # Thu: Mon 19 (Tue 20 holiday) has 2 sessions -> keep
        D(2026, 10, 16): D(2026, 10, 27),   # Fri before that Monday expiry -> roll
        D(2026, 10, 23): D(2026, 10, 27),
        D(2026, 10, 26): D(2026, 11, 3),
        D(2026, 11, 5): D(2026, 11, 9),     # Mon 9 Nov (Tue 10 holiday): Thu keeps it ...
        D(2026, 11, 6): D(2026, 11, 17),    # ... Fri rolls
    }
    for today, want in index_cases.items():
        assert first_eligible_expiry(NIFTY_EXPIRIES, today, 2, cal) == want, today
    stock_cases = {
        D(2026, 10, 16): D(2026, 10, 27),   # 6 sessions left
        D(2026, 10, 19): D(2026, 10, 27),   # E-5 (Tue 20 is a holiday): still eligible
        D(2026, 10, 21): D(2026, 11, 23),   # E-4: rolls to the next month (physical-settlement window)
        D(2026, 11, 16): D(2026, 11, 23),   # November monthly is Mon 23 (Tue 24 holiday): E-5 keeps it
        D(2026, 11, 17): D(2026, 12, 29),   # E-4 rolls
    }
    for today, want in stock_cases.items():
        assert first_eligible_expiry(MONTHLY, today, 5, cal) == want, today
    assert first_eligible_expiry(MONTHLY, D(2026, 11, 19), 2, cal) == D(2026, 11, 23)   # BANKNIFTY: index rule
    assert first_eligible_expiry(MONTHLY, D(2026, 11, 20), 2, cal) == D(2026, 12, 29)
    assert first_eligible_expiry(MONTHLY, D(2026, 12, 28), 2, cal) is None              # nothing far enough out
    with pytest.raises(ValueError):
        first_eligible_expiry(MONTHLY, SESSION, 0, cal)                                 # never an expiring contract

    # the store applies MIN_TD by kind, from today's dump only
    store = build(cal)
    assert store.choose_expiry("NIFTY", "INDEX", SESSION) == D(2026, 10, 19)
    assert store.choose_expiry("BANKNIFTY", "INDEX", SESSION) == D(2026, 10, 27)
    assert store.choose_expiry("RELIANCE", "STOCK", SESSION) == D(2026, 10, 27)
    assert store.choose_expiry("RELIANCE", "STOCK", D(2026, 10, 13)) is None            # not this dump's day
    with pytest.raises(ValueError):
        store.choose_expiry("RELIANCE", "INDEX", SESSION)                               # a stock gets the stock rule
    with pytest.raises(ValueError):
        store.choose_expiry("NSE:RELIANCE", "STOCK", SESSION)                           # takes the NFO name
    wed = D(2026, 10, 21)
    store_wed = build(cal, wed)
    assert store_wed.choose_expiry("RELIANCE", "STOCK", wed) == D(2026, 11, 23)
    assert store_wed.choose_expiry("NIFTY", "INDEX", wed) == D(2026, 10, 27)
    assert store_wed.select_contract("NSE:SAIL", "CE", "ATM", 130.0, wed).expiry == D(2026, 11, 23)
    assert store_wed.select_contract("NSE:NIFTY 50", "CE", "ATM", 25000.0, wed).expiry == D(2026, 10, 27)
    # settings may tighten MIN_TD (stock 6 rolls on 19 Oct too) but can never loosen it below 2 / 5
    mon = D(2026, 10, 19)
    assert build(cal, mon).choose_expiry("SAIL", "STOCK", mon) == D(2026, 10, 27)
    assert build(cal, mon, min_td_stock=6).choose_expiry("SAIL", "STOCK", mon) == D(2026, 11, 23)
    for loose in ({"min_td_index": 1}, {"min_td_stock": 4}, {"min_td_stock": True}):
        with pytest.raises(ValueError):
            build(cal, **loose)
    # no expiry satisfies the rule -> Refusal (G15)
    nfo, nse = make_dump(wed)
    nfo = [r for r in nfo if not (r["name"] == "SAIL" and r["expiry"] != D(2026, 10, 27))]
    only_near = build(cal, wed, dump=(nfo, nse))
    assert refused(only_near.select_contract("NSE:SAIL", "CE", "ATM", 130.0, wed), "NO_EXPIRY")


def test_atm_ties_itm1(cal):
    ks = [2800.0, 2850.0, 2900.0, 2950.0, 3000.0]
    # spot exactly between two strikes: CE takes the lower, PE the higher (both more in the money)
    assert pick_strike(ks, 2925.0, "CE", "ATM") == 2900.0
    assert pick_strike(ks, 2925.0, "PE", "ATM") == 2950.0
    assert pick_strike(ks, 2925.0, "CE", "ITM1") == 2850.0
    assert pick_strike(ks, 2925.0, "PE", "ITM1") == 3000.0
    # no tie: both types share ATM; ITM1 is the next lower (CE) or next higher (PE)
    assert pick_strike(ks, 2910.0, "CE", "ATM") == pick_strike(ks, 2910.0, "PE", "ATM") == 2900.0
    assert (pick_strike(ks, 2910.0, "CE", "ITM1"), pick_strike(ks, 2910.0, "PE", "ITM1")) == (2850.0, 2950.0)
    assert (pick_strike(ks, 2900.0, "CE", "ITM1"), pick_strike(ks, 2900.0, "PE", "ITM1")) == (2850.0, 2950.0)
    # uneven listing and unsorted input
    uneven = [110.0, 97.5, 105.0, 95.0, 100.0]
    assert (pick_strike(uneven, 98.75, "CE", "ATM"), pick_strike(uneven, 98.75, "PE", "ATM")) == (97.5, 100.0)
    assert (pick_strike(uneven, 98.75, "CE", "ITM1"), pick_strike(uneven, 98.75, "PE", "ITM1")) == (95.0, 105.0)
    # a decimal tie that binary floats split unevenly is still a tie (odd strikes appear after corporate actions)
    assert abs(100.1 - 100.0) < abs(100.2 - 100.1) and abs(100.15 - 100.0) > abs(100.3 - 100.15)
    assert pick_strike([100.0, 100.2, 100.4], 100.1, "PE", "ATM") == 100.2      # raw floats would give 100.0
    assert pick_strike([100.0, 100.3, 100.6], 100.15, "CE", "ATM") == 100.0     # raw floats would give 100.3
    assert pick_strike([100.0, 100.2, 100.4], 100.1, "CE", "ATM") == 100.0

    # through the store: the same rule picks listed contracts; opposite() mirrors it on the other type
    store = build(cal)
    ce = store.select_contract("NSE:RELIANCE", "CE", StrikePref.ITM1, 2910.0, SESSION)   # 20-pt strikes: tie
    assert (ce.opt_type, ce.strike, ce.expiry) == ("CE", 2880.0, D(2026, 10, 27))
    pe = store.opposite(ce, 2910.0, StrikePref.ITM1)
    assert (pe.opt_type, pe.strike, pe.expiry, pe.name) == ("PE", 2940.0, ce.expiry, "RELIANCE")
    atm_pe = store.opposite(store.select_contract("NSE:RELIANCE", "CE", "ATM", 2910.0, SESSION), 2910.0, "ATM")
    assert atm_pe.strike == 2920.0
    nifty = store.select_contract("NSE:NIFTY 50", "PE", "ITM1", 24990.0, SESSION)
    assert (nifty.strike, nifty.opt_type, nifty.lot_size) == (25050.0, "PE", 65)


def test_missing_neighbor_refuses(cal):
    ks = [100.0, 105.0, 110.0]
    # v1 clamped the index to the edge (options_trader 3529-3531) and so returned ATM for ITM1; v2 refuses
    assert pick_strike(ks, 100.5, "CE", "ATM") == 100.0
    assert refused(pick_strike(ks, 100.5, "CE", "ITM1"), "NO_ITM1_NEIGHBOUR")
    assert pick_strike(ks, 100.5, "PE", "ITM1") == 105.0
    assert refused(pick_strike(ks, 109.9, "PE", "ITM1"), "NO_ITM1_NEIGHBOUR")
    assert pick_strike(ks, 109.9, "CE", "ITM1") == 105.0
    # the nearest listed strike is not ATM once the spot is half a step or more past the listed range
    assert pick_strike(ks, 112.4, "CE", "ATM") == 110.0
    assert refused(pick_strike(ks, 112.5, "CE", "ATM"), "SPOT_OUTSIDE_STRIKES")
    assert refused(pick_strike(ks, 140.0, "PE", "ATM"), "SPOT_OUTSIDE_STRIKES")
    assert refused(pick_strike(ks, 90.0, "CE", "ATM"), "SPOT_OUTSIDE_STRIKES")
    assert 100.0 - 99.9 < (100.2 - 100.0) / 2                             # raw floats miss the exact half step
    assert refused(pick_strike([100.0, 100.2, 100.4], 99.9, "CE", "ATM"), "SPOT_OUTSIDE_STRIKES")
    # nothing listed, or bad input: a Refusal, never an exception
    assert refused(pick_strike([], 100.0, "CE", "ATM"), "NO_STRIKES")
    assert refused(pick_strike([100.0], 100.0, "CE", "ATM"), "NO_STRIKES")
    assert refused(pick_strike([100.0, "x"], 100.0, "CE", "ATM"), "NO_STRIKES")
    assert refused(pick_strike(ks, float("nan"), "CE", "ATM"), "SPOT_INVALID")
    assert refused(pick_strike(ks, 0.0, "CE", "ATM"), "SPOT_INVALID")
    assert refused(pick_strike(ks, 105.0, "XX", "ATM"), "BAD_REQUEST")
    assert refused(pick_strike(ks, 105.0, "CE", "OTM1"), "BAD_REQUEST")

    store = build(cal)                                  # SAIL lists 120..140 in 2.5 steps
    assert refused(store.select_contract("NSE:SAIL", "CE", "ITM1", 120.5, SESSION), "NO_ITM1_NEIGHBOUR")
    assert refused(store.select_contract("NSE:SAIL", "PE", "ITM1", 139.9, SESSION), "NO_ITM1_NEIGHBOUR")
    assert refused(store.select_contract("NSE:SAIL", "CE", "ATM", 150.0, SESSION), "SPOT_OUTSIDE_STRIKES")
    ce = store.select_contract("NSE:SAIL", "CE", "ITM1", 139.9, SESSION)
    assert ce.strike == 137.5
    assert refused(store.opposite(ce, 139.9, "ITM1"), "NO_ITM1_NEIGHBOUR")   # opposite() uses the same rule
    assert store.opposite(ce, 139.9, "ATM").strike == 140.0
    assert refused(store.select_contract("NSE:UNKNOWN", "CE", "ATM", 100.0, SESSION), "UNKNOWN_UNDERLYING")
    assert refused(store.select_contract("RELIANCE", "CE", "ATM", 2900.0, SESSION), "UNKNOWN_UNDERLYING")
    assert refused(store.select_contract("NSE:SAIL", "XX", "ATM", 130.0, SESSION), "BAD_REQUEST")
    assert refused(store.select_contract("NSE:SAIL", "CE", "ATM", math.inf, SESSION), "SPOT_INVALID")
    foreign = dataclasses.replace(ce, lot_size=ce.lot_size + 1)          # e.g. the row from another day's dump
    assert refused(store.opposite(foreign, 139.9, "ATM"), "UNKNOWN_CONTRACT")


def test_ban_list_date_override(cal, tmp_path):
    cache, config = tmp_path / "cache", tmp_path / "config"
    nse_text = "Securities in Ban For Trade Date 12-OCT-2026:\n1,SAIL\n2,LICHSGFIN\n"
    calls = []

    def fetch(text):
        def f():
            calls.append(1)
            if isinstance(text, Exception):
                raise text
            return text
        return f

    # NSE's file for today
    assert parse_ban_list(nse_text, SESSION) == {"SAIL", "LICHSGFIN"}
    got = load_ban_list(SESSION, cache_dir=cache, config_dir=config, fetch=fetch(nse_text))
    assert (got.symbols, got.source, got.ok) == (frozenset({"SAIL", "LICHSGFIN"}), "NSE", True)
    # cached: a restart the same day does not fetch again; another day never reuses it
    calls.clear()
    again = load_ban_list(SESSION, cache_dir=cache, config_dir=config, fetch=fetch(OSError("down")))
    assert (again.symbols, again.source, calls) == (got.symbols, "CACHE", [])
    nxt = D(2026, 10, 13)
    stale = load_ban_list(nxt, cache_dir=cache, config_dir=config, fetch=fetch(nse_text))
    assert stale.symbols is None and stale.source == "NSE" and "DATE_MISMATCH" in stale.detail
    assert not ins.ban_cache_path(cache, nxt).exists()                    # a wrong-date list is never cached
    # the file's trade date must be today
    with pytest.raises(BanListError, match="DATE_MISMATCH"):
        parse_ban_list(nse_text, nxt)
    # missing (3 failed tries inside the fetcher) or unparseable -> None: every stock ineligible, indices unaffected
    missing = load_ban_list(nxt, cache_dir=tmp_path / "c2", config_dir=config,
                            fetch=fetch(BanListError("FETCH_FAILED after 3 tries: HTTP 403")))
    assert (missing.symbols, missing.source) == (None, "MISSING") and "FETCH_FAILED" in missing.detail
    html = load_ban_list(nxt, cache_dir=tmp_path / "c3", config_dir=config, fetch=fetch("<!DOCTYPE html><p>x"))
    assert html.symbols is None and "NO_HEADER" in html.detail
    assert load_ban_list(nxt, cache_dir=tmp_path / "c4", config_dir=config, fetch=None).symbols is None
    store = build(cal)
    assert store.universe(None) == frozenset()
    assert store.ineligible_reason("NSE:RELIANCE", None) == "BAN_LIST_MISSING"
    assert store.ineligible_reason("NSE:NIFTY 50", None) is None
    assert store.ineligible_reason("NSE:SAIL", got.symbols) == "BANNED"
    assert "NSE:SAIL" not in store.universe(got.symbols) and "NSE:RELIANCE" in store.universe(got.symbols)

    # the operator override for today wins over NSE and the cache, and is not bypassed when broken
    override = ins.ban_override_path(config, SESSION)
    override.parent.mkdir(parents=True)
    override.write_text("# manual, after the NSE file was wrong\nRELIANCE\n2,M&M\n", encoding="utf-8")
    calls.clear()
    manual = load_ban_list(SESSION, cache_dir=cache, config_dir=config, fetch=fetch(nse_text))
    assert (manual.symbols, manual.source, calls) == (frozenset({"RELIANCE", "M&M"}), "OVERRIDE", [])
    override.write_text("Securities in Ban For Trade Date 09-OCT-2026:\n1,RELIANCE\n", encoding="utf-8")
    wrong_day = load_ban_list(SESSION, cache_dir=cache, config_dir=config, fetch=fetch(nse_text))
    assert (wrong_day.symbols, wrong_day.source) == (None, "OVERRIDE") and "DATE_MISMATCH" in wrong_day.detail
    override.write_text("", encoding="utf-8")                              # an empty file states nothing
    assert load_ban_list(SESSION, cache_dir=cache, config_dir=config, fetch=None).symbols is None
    override.write_text("NIL\n", encoding="utf-8")
    assert load_ban_list(SESSION, cache_dir=cache, config_dir=config, fetch=None).symbols == frozenset()
    # no-ban days and other date spellings in NSE's own file
    assert parse_ban_list("Securities in Ban For Trade Date 12-OCT-2026: NIL\n", SESSION) == frozenset()
    assert parse_ban_list(BOM + "Securities in Ban For Trade Date 12-Oct-2026:\r\n", SESSION) == frozenset()
    assert parse_ban_list("Securities in Ban For Trade Date 12-10-2026:\n1,SAIL,\n", SESSION) == {"SAIL"}
    assert parse_ban_list("Securities in Ban For Trade Date 2026-10-12:\n1,SAIL\n", SESSION) == {"SAIL"}
    for bad in ("Securities in Ban For Trade Date 12-OCT-2026:\nSr No,Symbol\n1,SAIL\n",
                "Securities in Ban For Trade Date 12-OCT-2026:\n1,SAIL,EXTRA\n",
                "Securities in Ban For Trade Date 12-OCT-2026: NIL\n1,SAIL\n",
                "Securities in Ban For Trade Date 31-XYZ-2026:\n", "1,SAIL\n"):
        with pytest.raises(BanListError):
            parse_ban_list(bad, SESSION)


# ---------------------------------------------------------------------------------------------------------------
# Extra tests
# ---------------------------------------------------------------------------------------------------------------
def test_universe_allow_deny_and_lookups(cal):
    nfo, nse = make_dump(with_expired=True)
    store = build(cal, dump=(nfo, nse))
    assert store.summary["expired_dropped"] > 0                            # expired rows are dropped, not fatal
    assert all(e >= SESSION for name in ("NIFTY", "RELIANCE") for e in store.expiries(name))
    # names: stocks are NSE:X <-> X; only NIFTY / BANKNIFTY among the indices; futures ignored
    assert [u.symbol for u in store.underlyings("INDEX")] == ["NSE:NIFTY 50", "NSE:NIFTY BANK"]
    assert store.underlying("M&M").symbol == "NSE:M&M" and store.underlying("NSE:M&M").kind == "STOCK"
    assert store.underlying("NIFTY").symbol == "NSE:NIFTY 50" and store.underlying_by_token(260105).name == "BANKNIFTY"
    assert len(store.underlyings("STOCK")) == 110 and store.underlying("FINNIFTY") is None
    inst = store.select_contract("NSE:M&M", "CE", "ATM", 3190.0, SESSION)
    assert store.by_symbol(inst.symbol) is inst and store.by_token(inst.token) is inst
    assert store.symbol_for_token(inst.token) == inst.symbol and store.symbol_for_token(256265) == "NSE:NIFTY 50"
    assert store.symbol_for_token(1) is None
    assert store.contract("M&M", inst.expiry, "CE", 3200.0) is inst and inst.strike == 3200.0
    assert store.strikes("SAIL", D(2026, 10, 27), "CE") == tuple(steps(120, 140, 2.5))
    assert all(not r["tradingsymbol"].endswith("FUT") or store.by_symbol("NFO:" + r["tradingsymbol"]) is None
               for r in nfo)
    # eligibility: ban list, allow list (when set), deny list; accepts 'NSE:X' or 'X' spellings
    ban = frozenset({"SAIL"})
    full = store.universe(ban)
    assert len(full) == 109 and "NSE:SAIL" not in full and all(s.startswith("NSE:") for s in full)
    assert store.universe(ban, allow=["NSE:RELIANCE", "sail", "M&M"]) == {"NSE:RELIANCE", "NSE:M&M"}
    assert store.universe(ban, deny=["RELIANCE"]) == full - {"NSE:RELIANCE"}
    assert store.ineligible_reason("NSE:RELIANCE", ban, allow=["M&M"]) == "NOT_ALLOWLISTED"
    assert store.ineligible_reason("RELIANCE", ban, deny=["NSE:RELIANCE"]) == "DENYLISTED"
    assert store.ineligible_reason("NSE:NOPE", ban) == "NOT_IN_UNIVERSE"
    assert store.ineligible_reason("NSE:NIFTY BANK", ban, allow=["M&M"], deny=["NIFTY"]) is None
    with pytest.raises(TypeError):
        store.universe(ban, allow="RELIANCE")                              # one string is not a list


def test_ban_list_fetch_retries_with_browser_headers():
    seen, sleeps = [], []

    class Resp:
        def __init__(self, status, body):
            self.status_code, self.content = status, body

    replies = [OSError("connection reset"), Resp(403, b"denied"),
               Resp(200, (BOM + "Securities in Ban For Trade Date 12-OCT-2026:\n1,SAIL\n").encode("utf-8"))]

    def get(url, headers, timeout):
        seen.append((url, headers, timeout))
        r = replies[len(seen) - 1]
        if isinstance(r, Exception):
            raise r
        return r

    text = ins.fetch_ban_list_text("https://example.invalid/ban.csv", get=get, sleep=sleeps.append)
    assert parse_ban_list(text, SESSION) == {"SAIL"} and len(seen) == 3 and sleeps == [2.0, 4.0]
    assert seen[0][1]["User-Agent"].startswith("Mozilla/5.0") and seen[0][2] == 10.0
    seen.clear()
    replies[:] = [Resp(503, b"")] * 3
    with pytest.raises(BanListError, match="FETCH_FAILED after 3 tries: HTTP 503"):
        ins.fetch_ban_list_text("https://example.invalid/ban.csv", get=get, sleep=sleeps.append)
    assert len(seen) == 3
    assert ins.ban_list_url({}) == ins.DEFAULT_BAN_LIST_URL
    assert ins.ban_list_url({"BAN_LIST_URL": "https://mirror.example/ban.csv"}) == "https://mirror.example/ban.csv"


def test_cache_prune_keeps_recent(cal, tmp_path):
    for days_ago in (0, 3, 11, 30):
        day = SESSION - timedelta(days=days_ago)
        save_dump(tmp_path, day, [], [])
        ins.ban_cache_path(tmp_path, day).parent.mkdir(parents=True, exist_ok=True)
        ins.ban_cache_path(tmp_path, day).write_text("x", encoding="utf-8")
    (tmp_path / "instruments" / "notes").mkdir()
    assert prune_cache(tmp_path, SESSION, keep_days=10) == 4
    left = sorted(p.name for p in (tmp_path / "instruments").iterdir())
    assert left == [(SESSION - timedelta(days=3)).isoformat(), SESSION.isoformat(), "notes"]
    assert len(list((tmp_path / "ban_list").iterdir())) == 2


def test_banlist_value_object():
    b = BanList(SESSION, None, "MISSING", "fetch failed")
    assert not b.ok and BanList(SESSION, frozenset(), "NSE").ok
