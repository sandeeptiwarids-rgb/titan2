"""Review fix (exits_risk F3): a slow LIVE account read on lane-reads never starves an EXIT quote refresh.

lane-reads was one thread making one blocking call at a time. The engine reconciles the LIVE account every 10 s,
and positions() / orders() can stall inside Kite's 5 s client timeout; a thin option's REST refresh (sent at quote
age 3 s) queued behind such a call was never sent, was answered TIMEOUT, and decide() closed the healthy leg
STALE_QUOTE_PROTECTIVE at age 5 s. Quote requests now have their own worker, lane-reads-quotes, and step() (inline)
keeps a read's time off the quote requests that waited behind it.
"""

from __future__ import annotations

import queue
import threading

import pytest

from titan2.broker.lanes import QUOTES_LANE_THREAD, ReadsLane
from titan2.broker.port import READS_LANE_THREAD
from titan2.clock import FakeClock
from titan2.core.events import FetchPositions, FetchQuotes, PositionsSnapshot, Priority, QuoteResult
from titan2.model import Account, Quote

SYM = "NFO:RELIANCE26OCT2900CE"
ANSWER_S = 2.0          # a free worker answers in milliseconds; the old single thread never did while blocked


class GatedData:
    """KiteData stand-in whose positions() and quote() can be held inside the call, like Kite stalling."""

    accounts = frozenset({Account.LIVE})

    def __init__(self, hold_positions: bool = False, hold_quote: bool = False) -> None:
        self.clock = FakeClock()
        self.in_positions, self.positions_go = threading.Event(), threading.Event()
        self.in_quote, self.quote_go = threading.Event(), threading.Event()
        if not hold_positions:
            self.positions_go.set()
        if not hold_quote:
            self.quote_go.set()
        self.quote_calls: list[list[str]] = []

    def positions(self, account):
        self.in_positions.set()
        if not self.positions_go.wait(10):
            raise TimeoutError("positions() was never released")
        return ()

    def quote(self, symbols):
        self.quote_calls.append(list(symbols))
        self.in_quote.set()
        if not self.quote_go.wait(10):
            raise TimeoutError("quote() was never released")
        return {s: Quote(s, 1, ((32.5, 500),), ((32.6, 500),), None, None, None, None, self.clock.mono(),
                         self.clock.now_wall(), "REST") for s in symbols}


def _next(out: queue.Queue, what: str):
    try:
        return out.get(timeout=ANSWER_S)
    except queue.Empty:
        pytest.fail(f"{what} was not answered within {ANSWER_S} s")


def test_exit_refresh_is_answered_while_a_positions_call_stalls():
    data = GatedData(hold_positions=True)
    out: queue.Queue = queue.Queue()
    lane = ReadsLane(data, out.put, lambda q: None)
    lane.start()
    try:
        lane.submit(FetchPositions(Account.LIVE, int(Priority.RECON)))     # the 10 s LIVE recon read ...
        assert data.in_positions.wait(5)                                   # ... now stalled inside Kite
        lane.submit(FetchQuotes("q:exit", (SYM,), int(Priority.EXIT)))     # an OPEN leg's refresh at age 3 s
        assert _next(out, "the EXIT refresh") == QuoteResult("q:exit", True, (SYM,), (), None)
        assert data.quote_calls == [[SYM]] and not data.positions_go.is_set()
        data.positions_go.set()
        ev = _next(out, "the released positions read")
        assert isinstance(ev, PositionsSnapshot) and ev.ok
    finally:
        data.positions_go.set()
        lane.stop(join_s=5)


def test_exit_cap_read_is_answered_while_a_quote_call_stalls():
    data = GatedData(hold_quote=True)
    out: queue.Queue = queue.Queue()
    lane = ReadsLane(data, out.put, lambda q: None)
    lane.start()
    try:
        lane.submit(FetchQuotes("q:paper", (SYM,), int(Priority.PAPER)))
        assert data.in_quote.wait(5)
        lane.submit(FetchPositions(Account.LIVE, int(Priority.EXIT)))      # the exit cap / verify-flat read
        ev = _next(out, "the EXIT positions read")
        assert isinstance(ev, PositionsSnapshot) and ev.ok
        data.quote_go.set()
        assert _next(out, "the released quote") == QuoteResult("q:paper", True, (SYM,), (), None)
    finally:
        data.quote_go.set()
        lane.stop(join_s=5)


def test_inline_step_keeps_a_reads_time_off_the_quote_wait():
    """The review's scenario, inline: a refresh submitted while positions() stalls 4 s is still sent."""
    clock = FakeClock()
    calls: list = []

    class SlowPositions:
        accounts = frozenset({Account.LIVE})

        def positions(self, account):
            lane.submit(FetchQuotes("q:e1:7", (SYM,), int(Priority.EXIT)))
            clock.advance(4.0)
            return ()

        def quote(self, symbols):
            calls.append(list(symbols))
            return {}

    posted: list = []
    lane = ReadsLane(SlowPositions(), posted.append, lambda q: None, mono=clock.mono)
    lane.submit(FetchPositions(Account.LIVE, int(Priority.RECON)))
    lane.step()                                                            # positions() stalls 4 s
    lane.step()                                                            # the refresh is sent, not TIMEOUT
    assert calls == [[SYM]]
    assert [e for e in posted if isinstance(e, QuoteResult)] == [QuoteResult("q:e1:7", True, (), (SYM,), None)]


def test_two_named_workers_and_an_optional_quote_client():
    reads, quotes = GatedData(), GatedData()
    out: queue.Queue = queue.Queue()
    lane = ReadsLane(reads, out.put, lambda q: None, quote_data=quotes)
    t = lane.start()
    tq = lane._quote_thread
    try:
        assert (t.name, tq.name) == (READS_LANE_THREAD, QUOTES_LANE_THREAD) == ("lane-reads", "lane-reads-quotes")
        assert t.is_alive() and tq.is_alive()
        lane.submit(FetchQuotes("q", (SYM,), int(Priority.EXIT)))
        lane.submit(FetchPositions(Account.LIVE, int(Priority.RECON)))
        got = {type(_next(out, "a read")).__name__, type(_next(out, "a read")).__name__}
        assert got == {"QuoteResult", "PositionsSnapshot"}
        assert quotes.quote_calls == [[SYM]] and reads.quote_calls == []   # quotes go to the quote client
        with pytest.raises(RuntimeError):
            lane.start()
    finally:
        lane.stop(join_s=5)
    assert not t.is_alive() and not tq.is_alive()

    class WithOrders(GatedData):
        def place(self, req):
            raise AssertionError("never called")

    with pytest.raises(TypeError):
        ReadsLane(GatedData(), out.put, lambda q: None, quote_data=WithOrders())
