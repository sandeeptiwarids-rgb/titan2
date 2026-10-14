"""The 09:20 opening_drive batch of 2026-10-14, replayed end to end on both accounts (the dual run): a signal that G09
refused only because two entries were still pending now waits for them (FINAL_SPEC errata 2026-10-14).

On the day: TATAELXSI (UP, +5.06%) at 09:20:00.190 and POLYCAB (DOWN, -2.72%) at 09:20:00.206 were reserved and held
both of opening_drive's position slots while their decision quotes took 2 s; TCS (UP, +2.51%, volume 17.9x) at
09:20:00.212 was refused 'POSITIONS: 2/2' with nothing open. At 09:20:02.192 and 09:20:02.248 both were refused at
their decisions (SIZE:DEPTH: one lot of buyers near the mid; SIZE:NO_ENTRY_LIMIT: a 2.1% spread) and voided, and TCS
was lost. Now TCS waits, holding nothing, and the next Timer (09:20:02.250) routes it as if it had just arrived: every
gate again, on fresh data. It is reserved and entered on its own decision snapshot, inside its 10 s TTL, and the book
never holds more than 2 (I11 runs after every event: a breach would be recorded as a violation).

For this file only the universe gets the day's names, lots and strikes (TATAELXSI and POLYCAB added, TCS's strikes
around 3100), and the two slow decision fetches are held back until the times they were answered.
"""

from __future__ import annotations

import dataclasses
import itertools
from contextlib import contextmanager
from datetime import date
from typing import Iterator

import pytest

from titan2.core.events import FetchQuotes, Place
from titan2.model import Account, Instrument, Intent, Role, Underlying
from titan2.settings import DEFAULTS

from fakes import universe as uni
from fakes.runner import ScenarioRunner
from fakes.scenario import route_of, state_of, struct_of, wait_for

pytestmark = pytest.mark.scenario

DAY = date(2026, 10, 14)                 # a Wednesday session
OD = "opening_drive"
TATA, POLY, TCS = "NSE:TATAELXSI", "NSE:POLYCAB", "NSE:TCS"
TATA_CE, POLY_PE = "NFO:TATAELXSI26OCT3600CE", "NFO:POLYCAB26OCT8100PE"
TCS_CE, TCS_PE = "NFO:TCS26OCT3080CE", "NFO:TCS26OCT3120PE"      # ITM1 at 3100, and the ITM1 PE placebo
DAY_SPECS = (uni.Spec("TATAELXSI", 873217, 3650.0, 100, 50.0, 0.03),
             uni.Spec("POLYCAB", 2455041, 8050.0, 125, 50.0, 0.02),
             uni.Spec("TCS", 2953217, 3100.0, 175, 20.0, 0.03))


@contextmanager
def the_day() -> Iterator[None]:
    """The fake universe with the day's names: TATAELXSI and POLYCAB added, TCS's strikes around 3100 (stock
    monthlies, as uni builds them). Restored on exit; a ScenarioRunner reads these tables when it is built."""
    names = {s.name for s in DAY_SPECS}
    specs = tuple(s for s in uni.SPECS if s.name not in names) + DAY_SPECS
    tok = itertools.count(29_000_001)
    day = tuple(Instrument(f"NFO:{ts}", next(tok), ts, "NFO", s.name, s.symbol, ot, k, e, s.lot, uni.TICK)
                for s in DAY_SPECS for e in uni.STOCK_EXPIRIES for k in uni.strikes(s) for ot in ("CE", "PE")
                for ts in (uni.tradingsymbol(s.name, e, k, ot),))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(uni, "SPECS", specs)
        mp.setattr(uni, "BY_NAME", {s.name: s for s in specs})
        mp.setattr(uni, "BY_SYMBOL", {s.symbol: s for s in specs})
        mp.setattr(uni, "CONTRACTS", tuple(c for c in uni.CONTRACTS if c.name not in names) + day)
        mp.setattr(uni, "UNDERLYINGS", tuple(Underlying(s.symbol, s.token, s.name, s.kind) for s in specs))
        yield


class HeldFetches:
    """The reads lane gets the decision fetch of a held intent only when answer() is called: the 2 s it took that
    day. Every other read goes through at once."""

    def __init__(self, r: ScenarioRunner) -> None:
        self.r, self.ids, self.held = r, set(), {}
        self._submit = r.proc.mdata.submit
        r.proc.mdata.submit = self.submit

    def post(self, i: Intent) -> Intent:
        """Post the intent (as the signals thread would), holding its decision fetch."""
        self.ids.add(i.intent_id)
        return self.r.intent(intent=i)

    def submit(self, cmd, reply=None) -> None:
        if isinstance(cmd, FetchQuotes) and cmd.request_id in self.ids:
            self.held[cmd.request_id] = (cmd, reply)
        else:
            self._submit(cmd, reply)

    def answer(self, request_id: str) -> None:
        cmd, reply = self.held.pop(request_id)
        self._submit(cmd, reply)
        self.r.settle()


def signal(r: ScenarioRunner, sym: str, direction: str, ret_pct: float, vol_mult: float) -> Intent:
    """opening_drive's intent (ITM1, TTL 10 s, its recheck band around the close) at the underlying's LTP."""
    close = r.spot(sym)
    band = (close * 0.995, close * 1.010) if direction == "UP" else (close * 0.990, close * 1.005)
    i = r.make_intent(sym, direction, strategy=OD, strike_pref="ITM1", ttl_s=10.0,
                      recheck=(round(band[0], 2), round(band[1], 2)))
    return dataclasses.replace(i, reason={"ret_pct": ret_pct, "vol_mult": vol_mult})


def held(r: ScenarioRunner, book: str) -> int:
    return sum(1 for x in r.engine.state.live_structures() if x.book == book)


def batch(r: ScenarioRunner) -> tuple[Intent, Intent, Intent, HeldFetches]:
    """09:20:00.190-.212: the batch, strongest |return| first, each with a fresh LTP (so each selects at once)."""
    for sym, ltp in ((TATA, 3641.0), (POLY, 8052.0), (TCS, 3100.0)):
        r.market.tick(sym, ltp)
    fetches = HeldFetches(r)
    r.run_until("09:20:00.190")
    tata = fetches.post(signal(r, TATA, "UP", 5.06, 55.0))
    r.run_until("09:20:00.206")
    poly = fetches.post(signal(r, POLY, "DOWN", -2.72, 7.5))
    r.run_until("09:20:00.212")
    tcs = r.intent(intent=signal(r, TCS, "UP", 2.51, 17.9))
    assert set(fetches.held) == {tata.intent_id, poly.intent_id}
    return tata, poly, tcs, fetches


def test_tcs_waits_for_the_two_pending_entries_and_enters_on_its_own_snapshot(dual):
    with the_day():
        r = dual.runner(session=DAY)
        book = r.book(OD)
        tata, poly, tcs, fetches = batch(r)
        assert route_of(r, tata, book)["outcome"] == route_of(r, poly, book)["outcome"] == "RESERVED"
        row = route_of(r, tcs, book)
        assert (row["outcome"], row["gate"], row["detail"]) == ("REFUSED", "BOOK_CAPS", "POSITIONS: 2/2")
        assert struct_of(r, tcs) is None and held(r, book) == 2
        (w,) = r.events("INTENT_WAITING")
        pending = sorted(struct_of(r, i)["structure_id"] for i in (tata, poly))
        assert w["intent_id"] == tcs.intent_id and w["book"] == book and w["payload"]["pending"] == pending
        if dual.live:                                          # the PAPER twin waits with LIVE:<s>, no row of its own
            assert route_of(r, tcs, f"PAPER:{OD}") is None
        r.run_until("09:20:00.250")                            # the core loop's Timers: every 250 ms from here
        r.run_until("09:20:02.100")
        assert route_of(r, tcs, book)["outcome"] == "REFUSED" and struct_of(r, tcs) is None   # both still pending
        # 09:20:02.192 TATAELXSI 3600CE: one lot of buyers within 1% of the mid (the rule needs 3)
        r.market.depth(TATA_CE, [(65.00, 100), (60.00, 500)], [(65.50, 1000), (65.55, 1000), (65.60, 1000)])
        r.market.tick(TATA, 3642.0)
        r.run_until("09:20:02.192")
        fetches.answer(tata.intent_id)
        assert (state_of(r, tata), struct_of(r, tata)["void_reason"]) == ("VOID", "SIZE:DEPTH")
        # TCS's own market moves meanwhile: the snapshot it is decided on is the one it fetches when routed
        r.market.depth(TCS_CE, [(61.00, 5000), (60.95, 5000), (60.90, 5000)], [(61.10, 5000), (61.15, 5000),
                                                                               (61.20, 5000)])
        r.market.depth(TCS_PE, [(40.00, 5000), (39.95, 5000), (39.90, 5000)], [(40.10, 5000), (40.15, 5000),
                                                                               (40.20, 5000)])
        r.market.tick(TCS, 3101.0)
        # 09:20:02.248 POLYCAB 8100PE: a 2.1% spread
        r.market.depth(POLY_PE, [(98.00, 1250), (97.95, 1250), (97.90, 1250)], [(100.10, 1250), (100.15, 1250),
                                                                                (100.20, 1250)])
        r.market.tick(POLY, 8050.0)
        r.run_until("09:20:02.248")
        fetches.answer(poly.intent_id)
        assert (state_of(r, poly), struct_of(r, poly)["void_reason"]) == ("VOID", "SIZE:NO_ENTRY_LIMIT")
        assert held(r, book) == 0 and route_of(r, tcs, book)["outcome"] == "REFUSED"      # until the next Timer
        # 09:20:02.250, the next Timer: TCS goes through G01-G13 again, is reserved and fetches its own snapshot
        places = len(r.places(Role.ENTRY))
        r.run_until("09:20:02.250")
        row = route_of(r, tcs, book)
        assert (row["outcome"], row["gate"], row["detail"]) == ("RESERVED", None, None)
        (ev,) = [e for e in r.events("INTENT_ROUTED") if e["intent_id"] == tcs.intent_id and e["book"] == book]
        assert ev["payload"]["waited_s"] == pytest.approx(2.038, abs=1e-3)
        assert ev["payload"]["waited_for"] == "BOOK_CAPS: POSITIONS: 2/2"
        if dual.live:
            assert route_of(r, tcs, f"PAPER:{OD}")["outcome"] == "RESERVED"               # one pass, one fetch
        (f,) = [s.cmd for s in r.sent if isinstance(s.cmd, FetchQuotes) and s.cmd.request_id == tcs.intent_id]
        assert set(f.symbols) == {TCS_CE, TCS_PE, TCS}
        (p,) = r.places(Role.ENTRY)[places:]                   # the account under test's entry
        assert (p.req.symbol, p.req.qty) == (TCS_CE, 175)
        st = r.engine.state.structures[struct_of(r, tcs)["structure_id"]]
        assert st.decision["quotes"][TCS_CE]["ask"] == 61.10 and st.decision["quotes"][TCS]["ltp"] == 3101.0
        assert [c.req.symbol for c in r.places(Role.ENTRY, account=Account.SHADOW)] == [TCS_PE]   # its placebo
        r.run(2.0)
        assert state_of(r, tcs) == "OPEN" and held(r, book) == 1
        assert r.violations == [] and r.exceptions == [] and r.halts() == []


def test_a_restart_while_tcs_waits_keeps_todays_refusal(dual):
    """The wait lives in RAM: a crash and restart while TCS waits drops it. Its row stays the REFUSED it was given
    when it started waiting, and nothing is ever reserved or sent for it (intents are never replayed)."""
    with the_day():
        r = dual.runner(session=DAY)
        book = r.book(OD)
        tata, poly, tcs, _ = batch(r)
        r.run_until("09:20:01.000")
        r.restart()
        r.run(15.0)
        row = route_of(r, tcs, book)
        assert (row["outcome"], row["gate"], row["detail"]) == ("REFUSED", "BOOK_CAPS", "POSITIONS: 2/2")
        assert r.rows("SELECT * FROM structures WHERE intent_id = ?", (tcs.intent_id,)) == []
        assert not [c for c in r.places() if c.req.symbol.startswith("NFO:TCS")]
        assert r.violations == []


def test_a_woken_signal_refused_runaway_raises_no_halt_but_a_fresh_one_does(dual):
    """The owner's decision on the RUNAWAY of a woken signal (2026-10-14), on the real core. A batch of four whose
    entry IOCs fill nothing (the book moves away from each order): TATAELXSI sends entry order 1 at once, POLYCAB is
    reserved and its fetch held, TCS and INFY wait. TATAELXSI's slot goes to TCS (order 2), POLYCAB's fetch is then
    answered (order 3), and INFY, woken when TCS's slot frees, meets three entry orders in 60 s: refused RUNAWAY
    exactly as a fresh signal would be, but with no halt (the orders it counts are its batch's). The limit itself is
    unchanged: RELIANCE, arriving in that minute, is refused RUNAWAY and halts the book until `titan2 ack-halt`."""
    with the_day():
        r = dual.runner(session=DAY)
        book = r.book(OD)
        away = {TATA_CE, POLY_PE, TCS_CE}

        def nothing_fills(rr: ScenarioRunner, c) -> None:
            if isinstance(c, Place) and c.req.role is Role.ENTRY and c.req.symbol in away:
                px = c.req.limit_px
                rr.market.depth(c.req.symbol, [(round(px - 1.0, 2), 5000)], [(round(px + 1.0, 2), 5000)])

        r.dispatch_hooks.append(nothing_fills)
        for sym, ltp in ((TATA, 3641.0), (POLY, 8052.0), (TCS, 3100.0)):
            r.market.tick(sym, ltp)
        fetches = HeldFetches(r)
        r.run_until("09:20:00.190")
        tata = r.intent(intent=signal(r, TATA, "UP", 5.06, 55.0))          # decided at once: entry order 1
        r.run_until("09:20:00.206")
        poly = fetches.post(signal(r, POLY, "DOWN", -2.72, 7.5))           # reserved, its decision fetch held
        r.run_until("09:20:00.212")
        tcs = r.intent(intent=signal(r, TCS, "UP", 2.51, 17.9))
        r.run_until("09:20:00.218")
        infy = r.intent(intent=signal(r, "NSE:INFY", "UP", 2.6, 4.2))
        assert [e["intent_id"] for e in r.events("INTENT_WAITING")] == [tcs.intent_id, infy.intent_id]
        wait_for(r, lambda: state_of(r, tata) == "VOID" and state_of(r, tcs) is not None, 5.0,
                 "TATAELXSI's IOC to fill nothing and TCS to take its slot")
        assert len(r.orders(role="ENTRY", book=book)) == 2     # TCS decided at once: entry order 2
        fetches.answer(poly.intent_id)                         # entry order 3, before TCS's IOC is answered
        assert len(r.orders(role="ENTRY", book=book)) == 3
        wait_for(r, lambda: route_of(r, infy, book)["detail"] != "POSITIONS: 2/2", 5.0, "INFY to be routed again")
        row = route_of(r, infy, book)
        assert (row["outcome"], row["gate"], row["detail"]) == (
            "REFUSED", "BOOK_CAPS", "RUNAWAY: 3 entry orders in 60 s (max 3)")
        (ev,) = [e for e in r.events("INTENT_ROUTED") if e["intent_id"] == infy.intent_id and e["book"] == book]
        assert ev["payload"]["waited_s"] > 0 and struct_of(r, infy) is None
        assert r.halts("RUNAWAY") == [] and "HALT_RUNAWAY" not in r.alert_codes()
        rel = signal(r, "NSE:RELIANCE", "UP", 2.7, 5.1)
        r.intent(intent=rel)                                   # a fresh signal in that minute
        row = route_of(r, rel, book)
        assert (row["outcome"], row["gate"], row["detail"]) == (
            "REFUSED", "BOOK_CAPS", "RUNAWAY: 3 entry orders in 60 s (max 3)")
        assert sorted(h.scope for h in r.halts("RUNAWAY")) == sorted({f"BOOK:{book}", f"BOOK:PAPER:{OD}"})
        assert "HALT_RUNAWAY" in r.alert_codes() and r.violations == []


def test_one_batch_never_sends_more_entry_orders_a_minute_than_the_cap(dual):
    """The review of the held signals, ported from the probe test_skc_batch_orders.py, on the real core: a batch of
    four opening_drive signals whose first two IOCs fill nothing (the book moves away from each order). TATAELXSI and
    POLYCAB send entry orders 1 and 2, TCS and INFY wait. Both IOCs free their slots before the next Timer, which
    woke TCS and INFY in one pass: each saw 2 entry orders in 60 s, both were reserved and both sent, 4 entry orders
    in 0.33 s (on LIVE, 4 real orders) with MAX_ENTRY_ORDERS_PER_MIN 3 and no halt. G09 now counts the reserved TCS
    as an order still to come: INFY keeps waiting (ENTRY_ORDERS), and once TCS's order is out it is refused RUNAWAY
    without the halt (a woken signal). Three entry orders in each book, the PAPER twin's included."""
    with the_day():
        r = dual.runner(session=DAY)
        book = r.book(OD)
        away = {TATA_CE, POLY_PE}

        def nothing_fills(rr: ScenarioRunner, c) -> None:
            if isinstance(c, Place) and c.req.role is Role.ENTRY and c.req.symbol in away:
                px = c.req.limit_px
                rr.market.depth(c.req.symbol, [(round(px - 1.0, 2), 5000)], [(round(px + 1.0, 2), 5000)])

        r.dispatch_hooks.append(nothing_fills)
        for sym, ltp in ((TATA, 3641.0), (POLY, 8052.0), (TCS, 3100.0)):
            r.market.tick(sym, ltp)
        r.run_until("09:20:00.190")
        tata = r.intent(intent=signal(r, TATA, "UP", 5.06, 55.0))
        r.run_until("09:20:00.192")
        poly = r.intent(intent=signal(r, POLY, "DOWN", -2.72, 7.5))
        r.run_until("09:20:00.212")
        tcs = r.intent(intent=signal(r, TCS, "UP", 2.51, 17.9))
        r.run_until("09:20:00.218")
        infy = r.intent(intent=signal(r, "NSE:INFY", "UP", 2.6, 4.2))
        assert [e["intent_id"] for e in r.events("INTENT_WAITING")] == [tcs.intent_id, infy.intent_id]
        r.run(6.0)
        r.run(3.0)
        books = (book, f"PAPER:{OD}") if dual.live else (book,)
        for b in books:
            orders = r.orders(role="ENTRY", book=b, account=Account(b.split(":")[0]))
            assert len(orders) == DEFAULTS.MAX_ENTRY_ORDERS_PER_MIN == 3, \
                f"{b}: {len(orders)} entry orders in one minute: {[o['created_ts'][11:23] for o in orders]}"
            assert {o["structure_id"] for o in orders} == {struct_of(r, i, b)["structure_id"]
                                                          for i in (tata, poly, tcs)}
        row = route_of(r, infy, book)
        assert (row["outcome"], row["gate"], row["detail"]) == (
            "REFUSED", "BOOK_CAPS", "RUNAWAY: 3 entry orders in 60 s (max 3)")
        (ev,) = [e for e in r.events("INTENT_ROUTED") if e["intent_id"] == infy.intent_id and e["book"] == book]
        assert ev["payload"]["waited_s"] > 0 and ev["payload"]["waited_for"] == "BOOK_CAPS: POSITIONS: 2/2"
        assert struct_of(r, infy) is None and r.halts() == [] and "HALT_RUNAWAY" not in r.alert_codes()
        assert r.violations == [] and r.exceptions == []
