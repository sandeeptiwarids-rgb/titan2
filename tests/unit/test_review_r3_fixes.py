"""Regression tests for the orders and money re-review (review round 3, 2026-10-14), ported from the reviewers'
reproductions, which asserted the defects.

- N1/N0: a rollback reload rebuilt a timed-out order's late-accept window from the submit (submit + 5 + 30 s), not
  from its AMBIGUOUS answer, so an answer that came more than 5 s after the submit (after a wait in the FIFO order
  lane) lost that much of its 30 s (decision B2); and a SUBMITTING order whose Place was still in flight got no
  window at all after a reload. The engine now remembers each window's end for the process
  (EngineContext.late_until), and a reload gives every SUBMITTING and UNKNOWN order a window. The NOT_PLACED moment,
  which counts like a fill for recon's post-fill rule, is paired with the leg's stored fill time, so a reload keeps
  it too.
- N2: NOT_PLACED was decided by when a "not found" answer arrived, not when its orders() read was asked: in a
  brownout a read asked inside the window but answered after it made the order NOT_PLACED while the lookup asked at
  the window's end was still queued. LookupResult.req_mono carries the request time now.
- N4: G1 ordered its sample by structure_id, so a trade booked after a passing LOOK slipped into the look's prefix
  (never assessed) and pushed a look trade into the CONFIRM window. The sample is in booking order now.
- N6: DAY_BUDGET (§8.5) never got the open positions' remaining stop risk or the LIVE account's terms. Its review
  (follow-up): an open winner's gain was counted twice, refused room by min(day net, 0) and charged again as risk
  still to lose, so a safe second entry was refused. The day term is the realized net now, and an open position counts
  by its stop-out measured from its entry (a raised stop lowers it, a loss past the stop raises it).

G06/G16 (N3) and the daily stop (N7) are tested beside their neighbours in test_risk.py, `titan2 laptop --date` (N5)
in test_laptop.py; N3 and N6 also end to end in tests/scenarios/test_review_r3_entries.py.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pytest

import test_ledger_g1 as h
import test_orders_positions as top
import test_risk as tr
from test_engine import PAPER_BOOK, Rig
from test_late_fill_and_selftest import READ_TIMEOUT
from titan2 import g1, ledger, strategies
from titan2 import store as store_mod
from titan2.clock import IST, FakeClock
from titan2.core import orders, sizing
from titan2.core.engine import Engine
from titan2.core.events import LookupResult, LookupTag, PlaceResult
from titan2.core.single_leg import EntryPlan, HandlerCtx, SingleLegHandler
from titan2.model import (
    Account, DecisionQuote, Instrument, Level, OrderState, Policy, Quote, Refusal, Role, Sized, StructState,
)
from titan2.settings import DEFAULTS
from titan2.store import Store


@pytest.fixture
def core():
    with store_mod.core_thread():
        yield


@pytest.fixture
def orig(tmp_path, core):
    """test_orders_positions' rig: the order machine on a real Store, the engine's part played by the test."""
    s = Store(tmp_path / "db" / "titan2.db", backup_dir=tmp_path / "backups")
    yield top.Rig(s, FakeClock("2026-10-12T10:00:00+05:30"))
    s.close()


def _lookups(cmds, pk: str) -> list:
    return [c for c in cmds if isinstance(c, LookupTag) and c.order_pk == pk]


def _miss_until_terminal(r: Rig, pk: str, limit_s: float = 90.0) -> float:
    """Tick the engine rig, answering its LookupTags for pk 'not found', until the order resolves; when it did."""
    start = r.clock.mono()
    while r.clock.mono() - start < limit_s:
        for c in _lookups(r.tick(), pk):
            r.run(LookupResult(c.account, pk, True, None))
        if r.eng.state.orders[pk].is_terminal:
            return r.clock.mono()
    raise AssertionError(f"{pk} never resolved")


# ---------------------------------------------------------------------------------------------------------------
# N1 and N0: a rollback reload keeps the late-accept window; a Place in flight gets one
# ---------------------------------------------------------------------------------------------------------------
def test_a_rollback_reload_keeps_the_window_a_late_answer_set(tmp_path, core):
    """The Place waits 10.5 s on lane-live-orders (other calls ahead of it, or a slow connect: requests applies its
    5 s timeout per phase), then answers AMBIGUOUS: the window runs to answer + 30 s. Another event raises and the
    core loop reloads. The window rebuilt from the submit (submit + 5 + 30 s) ended 5.5 s early, so NOT_PLACED (and
    a second attempt) came 24.5 s after the answer, inside B2's 30 s."""
    r = Rig(tmp_path)
    try:
        r.tick()
        pk = r.intent()
        sub = r.eng.state.orders[pk].submitted_mono
        while r.clock.mono() - sub < 10.5 - 1e-9:      # no answer yet; the lookups the 10 s timer sends queue too
            r.tick()
        r.run(PlaceResult(Account.PAPER, pk, "AMBIGUOUS", None, *READ_TIMEOUT))
        answered = r.clock.mono()
        assert r.eng.state.orders[pk].state is OrderState.UNKNOWN
        assert r.ctx.late_until[pk] == pytest.approx(answered + orders.LATE_ACCEPT_S)
        r.tick(1.0)
        r.eng = Engine.load(r.store, r.ctx)            # an unrelated event raised: rollback and reload
        assert r.eng.state.timers[pk].late_until == pytest.approx(answered + orders.LATE_ACCEPT_S)
        gone = _miss_until_terminal(r, pk)
        assert r.eng.state.orders[pk].state is OrderState.NOT_PLACED
        assert gone - answered >= orders.LATE_ACCEPT_S - 1e-6, f"NOT_PLACED {gone - answered:.2f}s after the answer"
    finally:
        r.store.close()


@pytest.mark.parametrize("answer_after_s", [5.0, 10.5])
def test_a_rollback_reload_ends_the_window_where_the_order_had_it(orig, answer_after_s):
    """Ported from the review's order-path probe, at the order machine: timers_after_reload(restart=False) gets the
    deadline the order's timers held (the engine keeps it, EngineContext.late_until)."""
    rig = orig
    rig.s.load_state(rig.mono, rig.wall, session=top.SESSION)      # the process start: Engine.load's first load
    st, leg = rig.exiting()
    x, _ = rig.exit_order(leg, leg.open_qty)
    t0 = rig.mono
    while rig.mono - t0 < answer_after_s:
        rig.clk.advance(0.25)
        rig.tick(x)
    rig.ev(x, PlaceResult(x.account, x.order_pk, "AMBIGUOUS", None, *READ_TIMEOUT))
    answer = rig.mono
    rig.clk.advance(1.0)
    held = rig.t(x).late_until
    ls = rig.s.load_state(rig.mono, rig.wall, session=top.SESSION)  # the core loop's rollback and reload
    rig.orders.clear()
    rig.orders.update(ls.orders)
    rig.legs.clear()
    rig.legs.update(ls.legs)
    rig.timers.clear()
    x = rig.orders[x.order_pk]
    t, _ = orders.timers_after_reload(x, rig.mono, restart=False, late_until=held)
    rig.timers[x.order_pk] = t
    not_placed_at = None
    for _ in range(400):
        rig.clk.advance(0.25)
        if any(isinstance(c, LookupTag) for c in rig.tick(x).commands):
            rig.ev(x, LookupResult(x.account, x.order_pk, True, None))
        if x.state is OrderState.NOT_PLACED:
            not_placed_at = rig.mono
            break
    assert not_placed_at is not None
    assert not_placed_at - answer >= orders.LATE_ACCEPT_S - 1e-6, (
        f"NOT_PLACED only {not_placed_at - answer:.2f}s after the AMBIGUOUS answer (B2 wants >= 30s)")


def test_a_rollback_reload_gives_a_place_still_in_flight_a_late_window(tmp_path, core):
    """A reload while the Place is still on the lane (SUBMITTING, no answer): it had no window, so after the
    reload's +1/+3/+7/+15 s lookups it was NOT_PLACED 17 s after the submit, and a second attempt could go out while
    the first Place was still to be sent or answered. It gets the window counted from the submit now."""
    r = Rig(tmp_path)
    try:
        r.tick()
        pk = r.intent()
        sub = r.eng.state.orders[pk].submitted_mono
        r.tick(2.0)
        r.eng = Engine.load(r.store, r.ctx)            # rollback and reload: the Place is still in flight
        assert r.eng.state.orders[pk].state is OrderState.SUBMITTING
        until = sub + orders.PLACE_TIMEOUT_S + orders.LATE_ACCEPT_S
        assert r.eng.state.timers[pk].late_until == pytest.approx(until)
        gone = _miss_until_terminal(r, pk)
        assert r.eng.state.orders[pk].state is OrderState.NOT_PLACED and gone >= until - 1e-6
    finally:
        r.store.close()


def test_the_not_placed_post_fill_grace_survives_a_rollback_reload(orig):
    """Ported from the review's order-path probe: at NOT_PLACED the leg's last_fill_mono moves to now, so recon's
    post-fill rule decides nothing from a broker read asked before it (round 1, fix #4). The move was RAM only, and
    Store.load_state rebuilt last_fill_mono from the entry fill: a reload dropped the grace."""
    rig = orig
    rig.s.load_state(rig.mono, rig.wall, session=top.SESSION)      # the process start (first load)
    st, leg = rig.exiting()
    entry_fill = leg.last_fill_mono
    x, _ = rig.exit_order(leg, leg.open_qty)
    rig.ev(x, PlaceResult(x.account, x.order_pk, "AMBIGUOUS", None, *READ_TIMEOUT))
    for _ in range(400):
        if x.is_terminal:
            break
        rig.clk.advance(0.25)
        if any(isinstance(c, LookupTag) for c in rig.tick(x).commands):
            rig.ev(x, LookupResult(x.account, x.order_pk, True, None))
    assert x.state is OrderState.NOT_PLACED
    bumped = leg.last_fill_mono
    assert bumped >= entry_fill + orders.LATE_ACCEPT_S - 1e-6            # the NOT_PLACED moment counts like a fill
    rig.clk.advance(0.5)
    ls = rig.s.load_state(rig.mono, rig.wall, session=top.SESSION)    # rollback and reload in the same process
    assert ls.legs[leg.leg_id].last_fill_mono == pytest.approx(bumped), "the reload dropped the post-fill grace"


# ---------------------------------------------------------------------------------------------------------------
# N2: a miss counts from when its orders() read was asked
# ---------------------------------------------------------------------------------------------------------------
def test_a_miss_asked_inside_the_late_window_never_makes_it_not_placed(orig):
    """Ported from the review's order-path probe. lane-live-orders is one FIFO thread; from +20 s each lookup takes
    5.5 s there (a Kite brownout, when places time out). The read asked at +25.5 s was answered at +31 s, after the
    window: it made the order NOT_PLACED at once, while the lookup sent at the window's end (+30 s) was still
    queued. Now only a read asked at or after the window's end decides, and no other lookup is sent meanwhile: the
    one sent at +30 s is out."""
    rig = orig
    st, leg = rig.exiting()
    x, _ = rig.exit_order(leg, leg.open_qty)
    rig.ev(x, PlaceResult(x.account, x.order_pk, "AMBIGUOUS", None, *READ_TIMEOUT))
    answer = rig.mono
    lane: list[tuple[float, float, float]] = []      # (sent, started: the read's req_mono, answered)
    sent_at: list[float] = []
    busy = answer
    decided = None
    for _ in range(400):
        rig.clk.advance(0.25)
        if any(isinstance(c, LookupTag) for c in rig.tick(x).commands):
            sent = rig.mono
            sent_at.append(round(sent - answer, 2))
            start = max(busy, sent)
            busy = start + (5.5 if sent - answer >= 20.0 - 1e-9 else 0.1)
            lane.append((sent, start, busy))
        while lane and lane[0][2] <= rig.mono + 1e-9:
            sent, start, _ = lane.pop(0)
            rig.ev(x, LookupResult(x.account, x.order_pk, True, None, req_mono=start))    # "not found"
            if x.state is OrderState.NOT_PLACED:
                decided = (sent - answer, start - answer, rig.mono - answer)
                break
        if decided:
            break
    assert decided is not None
    sent, asked, at = decided
    assert asked >= orders.LATE_ACCEPT_S - 1e-6 and sent >= orders.LATE_ACCEPT_S - 1e-6, (
        f"NOT_PLACED at +{at:.2f}s rests on an orders() read asked at +{asked:.2f}s, inside the 30 s window")
    assert sent_at == [1.0, 3.0, 7.0, 15.0, 20.0, 25.0, 30.0], sent_at


def test_a_late_answer_asked_too_early_gets_one_more_lookup(orig):
    """The same miss with no lookup out after the window's end (its answer lost, say): one more lookup goes out at
    once, and its miss decides."""
    rig = orig
    st, leg = rig.exiting()
    x, _ = rig.exit_order(leg, leg.open_qty)
    rig.ev(x, PlaceResult(x.account, x.order_pk, "AMBIGUOUS", None, *READ_TIMEOUT))
    answer = rig.mono
    for _ in range(4):                                    # the scheduled lookups miss
        rig.ev(x, LookupResult(x.account, x.order_pk, True, None))
    rig.t(x).lookups_due = []                             # nothing more is due, nothing is out
    rig.clk.advance(orders.LATE_ACCEPT_S + 1.0)
    rig.ev(x, LookupResult(x.account, x.order_pk, True, None, req_mono=answer + 25.0))   # asked inside the window
    assert x.state is OrderState.UNKNOWN and rig.t(x).lookups_due == [pytest.approx(rig.mono)]
    assert [c for c in rig.tick(x).commands if isinstance(c, LookupTag)]
    rig.ev(x, LookupResult(x.account, x.order_pk, True, None, req_mono=rig.mono))
    assert x.state is OrderState.NOT_PLACED


# ---------------------------------------------------------------------------------------------------------------
# N4: G1's sample is in booking order
# ---------------------------------------------------------------------------------------------------------------
def _open_structure(s: Store, session, minute: int):
    """A PAPER:<s> structure entered on `session` and still open (its trade not booked yet)."""
    t_in = datetime.combine(session, time(10, 0), IST) + timedelta(minutes=minute)
    with s.tx():
        it = h.make_intent(f"late{minute:04d}", t_in)
        s.insert_intent(it, session)
        stc = h.make_structure(0, it, session=session, risk=100.0 * h.LOT, state=StructState.OPEN)
        s.insert_structure(stc)
        lg = h.make_leg(stc, 0, s.next_leg_seq(Account.PAPER, session), h.inst_of("CE"))
        s.insert_leg(lg)
        ent = h.make_order(lg, Role.ENTRY, 1, h.LOT, h.LOT, 100.0, OrderState.COMPLETE,
                           DecisionQuote(99.9, 100.0, 99.95, 100, "WS", 100.0, 100.0), limit=100.1, created=t_in)
        s.insert_order(ent)
        s.insert_fill(h.fill(ent, h.LOT, 100.0, t_in + timedelta(seconds=1)))
    return stc, lg, ent


def _book_late(s: Store, stc, lg, ent, exit_px: float, booked_wall: datetime) -> dict:
    """Book the structure's trade now, as close() would a session later (closed_ts = the booking time)."""
    with s.tx():
        ex = h.make_order(lg, Role.EXIT, 1, h.LOT, h.LOT, exit_px, OrderState.COMPLETE,
                          DecisionQuote(exit_px, exit_px + 0.1, exit_px + 0.05, 100, "WS", exit_px, exit_px),
                          limit=max(0.05, round(exit_px - 0.1, 2)), created=booked_wall - timedelta(seconds=1))
        s.insert_order(ex)
        s.insert_fill(h.fill(ex, h.LOT, exit_px, booked_wall))
        row = ledger.build_trade_row(stc, [lg], [ent, ex], s.effective_fills([ent.order_pk, ex.order_pk]),
                                     exit_wall=booked_wall)
        s.insert_trade(row)
        stc.state, stc.closed_wall = StructState.CLOSED, booked_wall
        s.update_structure(stc)
    return row


def _night(day_index: int) -> datetime:
    return datetime.combine(h.DAY0 + timedelta(days=day_index), time(15, 38), IST)


def test_g1_assesses_a_trade_booked_after_a_passing_look_in_the_confirm_window(tmp_path, core):
    """Ported from the review's money probe, with every trade booked at its own close: 100 trades over sessions
    0-33 pass LOOK 100 at session 33's nightly run while a structure entered on session 20 is still open. It is
    booked the next morning at Rs 0.05 (a missed square-off settled), then 25 modest winners close. By structure_id
    the late loss sat inside the look's prefix (never assessed), CONFIRM re-used a look trade and the version was
    CERTIFIED; in booking order CONFIRM judges it, and fails."""
    s = h.fresh(tmp_path, "late")
    h.start_trial(s)
    h.seed_paper(s, 60, placebos=51, booked=True)                       # sessions 0..19
    late = _open_structure(s, h.DAY0 + timedelta(days=20), 59)         # entered on session 20, not booked
    h.seed_paper(s, 40, first=60, placebos=34, booked=True)            # sessions 20..33: 100 booked trades
    h._bar_days(s, range(34), signal_ok=lambda k: True, control_ok=True, controls=0)
    run = h.nightly(s, _night(33), 1.0)
    lk = next(r for r in run.reports if r["kind"] == "LOOK")
    assert lk["passed"] and s.trial(h.STRAT, h.V)["state"] == "CONFIRMING"
    reg = s.trial(h.STRAT, h.V)["registered_ts"]
    look_ids = {x.structure_id for x in g1._population(s, h.STRAT, h.V, reg, h.rates_for(h.SESSION))}
    assert len(look_ids) == 100

    row = _book_late(s, *late, 0.05, datetime.combine(h.DAY0 + timedelta(days=34), time(9, 15), IST))
    assert row["net"] < -0.9 * row["risk_basis_rs"]
    h.seed_paper(s, 25, first=102, exits=(102.0, 103.0, 104.0), booked=True)   # sessions 34..42
    h._bar_days(s, range(34, 43), signal_ok=lambda k: True, control_ok=True, controls=0)
    run = h.nightly(s, _night(42), 2.0)
    cf = next(r for r in run.reports if r["kind"] == "CONFIRM")
    pop = g1._population(s, h.STRAT, h.V, reg, h.rates_for(h.SESSION))
    assert {x.structure_id for x in pop[:100]} == look_ids                # the judged prefix is unchanged
    confirm = [x.structure_id for x in pop[100:125]]
    assert not set(confirm) & look_ids and late[0].structure_id in confirm
    assert not cf["passed"] and "MEAN_R" in cf["fail_reasons"]
    assert s.trial(h.STRAT, h.V)["state"] != "CERTIFIED"
    s.close()


def test_g1_orders_its_sample_by_booking_time(tmp_path, core):
    """The population is in the order the trades were booked (structures.closed_ts), then structure_id; a trade
    booked later never lands before one booked earlier, whatever their ids."""
    s = h.fresh(tmp_path, "order")
    h.start_trial(s)
    early = _open_structure(s, h.DAY0, 1)                                # reserved first: the smaller id
    first, second = h.seed_paper(s, 2, booked=True)                    # booked on DAY0 at 10:20:05 and 10:21:05
    _book_late(s, *early, 101.0, datetime.combine(h.DAY0 + timedelta(days=1), time(9, 15), IST))
    reg = s.trial(h.STRAT, h.V)["registered_ts"]
    ids = [x.structure_id for x in g1._population(s, h.STRAT, h.V, reg, h.rates_for(h.SESSION))]
    assert early[0].structure_id < first < second and ids == [first, second, early[0].structure_id]
    s.close()


# ---------------------------------------------------------------------------------------------------------------
# N6: DAY_BUDGET gets the open positions' stop risk and the LIVE account's terms
# ---------------------------------------------------------------------------------------------------------------
NIFTY_CE = Instrument("NFO:NIFTY26OCT25000CE", 4242, "NIFTY26OCT25000CE", "NFO", "NIFTY", "NSE:NIFTY 50", "CE",
                      25000.0, tr.INST.expiry, 65, 0.05)


def _spot(symbol: str, ltp: float) -> Quote:
    """The underlying as the decision snapshot holds it: the quote cache's newest tick, 0.5 s old."""
    return Quote(symbol, 1, (), (), ltp, None, None, None, tr.NOW_MONO - 0.5, tr.NOW_WALL, "WS")


def _plan(book, *, inst: Instrument = tr.INST, q: Quote | None = None, spot: float = 2905.0, **ctx_kw):
    gc = tr.ctx(book, **ctx_kw)
    intent = tr.intent(underlying=inst.underlying, recheck=tr.Recheck(1.0, 1e9))
    snap = {inst.symbol: q or tr.quote(), inst.underlying: _spot(inst.underlying, spot)}
    return SingleLegHandler().plan_entry(intent, [inst], snap, HandlerCtx(tr.NOW_MONO, tr.NOW_WALL, DEFAULTS,
                                                                           gates=gc))


def test_plan_entry_budgets_open_positions_and_the_live_account():
    """Ported from the review's money probes: plan_entry called size() with the book's day net only, so a second
    position was sized as if the first could not stop out (D3 breached when both do), and a LIVE trade ignored the
    LIVE account's own daily stop. One lot here risks about Rs 3,260 (Rs 32.15 x 500 at the 15% stop + 5% slip,
    with charges)."""
    paper = tr.book_state(tr.PAPER, realized_net=-10_000.0)
    assert isinstance(_plan(paper), EntryPlan)                                         # Rs 5,000 of room left
    second = tr.book_state(tr.PAPER, realized_net=-10_000.0, open_risk_rs=3_000.0)     # another position is open
    assert _plan(second) == Refusal("SIZE", "DAY_BUDGET")
    assert _plan(tr.book_state(tr.PAPER, open_risk_rs=None)) == Refusal("SIZE", "OPEN_RISK_UNKNOWN")
    # LIVE: the account's realized net and open stop-outs over every LIVE book cap it too
    live = tr.book_state(tr.LIVE)
    assert isinstance(_plan(live, live_account=tr.live_account()), EntryPlan)
    assert _plan(live, live_account=tr.live_account(realized_net=-9_900.0)) == Refusal("SIZE", "DAY_BUDGET")
    assert _plan(live, live_account=tr.live_account(open_risk_rs=8_000.0)) == Refusal("SIZE", "DAY_BUDGET")
    assert _plan(live, live_account=tr.live_account(open_risk_rs=None)) == Refusal("SIZE", "LIVE_DAY_PNL_UNKNOWN")
    assert _plan(live, live_account=tr.live_account(mtm_net=None)) == Refusal("SIZE", "LIVE_DAY_PNL_UNKNOWN")
    # a 1-lot NIFTY call at Rs 766.60 (premium Rs 49,829) plans a Rs 10,131 stop-out: inside the PAPER book's
    #   Rs 15,000, past the LIVE account's Rs 10,000 even with the account flat
    q = Quote(NIFTY_CE.symbol, NIFTY_CE.token, (Level(766.40, 6500),), (Level(766.50, 6500),), 766.45, 1000,
              tr.NOW_WALL - timedelta(seconds=5), None, tr.NOW_MONO - 0.3, tr.NOW_WALL, "REST")
    paper_plan = _plan(tr.book_state(tr.PAPER), inst=NIFTY_CE, q=q, spot=25010.0)
    assert isinstance(paper_plan, EntryPlan) and paper_plan.decision["per_lot_risk_rs"] > 10_000.0
    assert _plan(live, inst=NIFTY_CE, q=q, spot=25010.0, live_account=tr.live_account()) == \
        Refusal("SIZE", "DAY_BUDGET")


def test_the_book_state_carries_the_open_positions_remaining_stop_risk(tmp_path, core):
    """Engine.book_state's open_risk_rs: each structure past RESERVED adds sizing.remaining_stop_risk of its plan's
    per-lot risk x lots and its legs' MTM now, its stop-out measured from its entry (an entry still working counts in
    full); one with no sizing plan (a recovered position, the self-test, here the stand-in gateway's entries) makes it
    unknown, so size() refuses. The leg is a little under water, well inside its stop, so it counts its whole planned
    stop-out: this pinned 950 + MTM until the review of round 3, while the day term still carried the MTM."""
    r = Rig(tmp_path)
    try:
        r.tick()
        leg = r.open_leg()
        assert r.eng.book_state(PAPER_BOOK).open_risk_rs is None
        st = r.struct(leg.leg_id)
        st.decision = {**(st.decision or {}), "plan": {"per_lot_risk_rs": 950.0, "lots": 1}}
        mtm = r.eng.leg_mtm(leg)
        assert mtm is not None and -950.0 < mtm < 0.0
        assert r.eng.book_state(PAPER_BOOK).open_risk_rs == pytest.approx(950.0)
        pk = r.intent("NSE:INFY")                                       # an entry still working
        st2 = r.eng.state.structures[r.eng.state.orders[pk].structure_id]
        assert st2.state is StructState.ENTERING
        st2.decision = {"plan": {"per_lot_risk_rs": 400.0, "lots": 2}}
        assert r.eng.book_state(PAPER_BOOK).open_risk_rs == pytest.approx(950.0 + 800.0)
        la = tr.risk.live_account_state([tr.book_state(tr.LIVE, open_risk_rs=1_000.0),
                                         tr.book_state("LIVE:price_spike_up", open_risk_rs=2_500.0),
                                         tr.book_state(tr.PAPER, open_risk_rs=9_999.0)])
        assert la.open_risk_rs == 3_500.0
        assert tr.risk.live_account_state([tr.book_state(tr.LIVE, open_risk_rs=None)]).open_risk_rs is None
    finally:
        r.store.close()


# The review of round 3 (N6 follow-up): an open winner's gain was counted twice. size()'s min(day net, 0) gave it no
#   room and remaining_stop_risk added it as risk still to lose, so a safe second entry was refused once the first
#   position was up more than DAILY_STOP_RS less both planned stop-outs. Ported from the reviewer's probes.
def test_a_second_entry_beside_a_winning_first_one_is_sized_as_beside_a_flat_one():
    """Ported from test_n6_plan_entry_both.py and test_n6_gain_double_count.py: the book's first position (a planned
    stop-out of Rs 6,000) is open and up. Both stopping out as planned puts the day at about -Rs 9,260 however far
    up it is, so the second entry is as safe as beside a flat first one: it gets the same plan. Up Rs 8,000 it was
    refused SIZE:DAY_BUDGET. The flat and losing cases did not change."""
    at_entry = 6_000.0

    def beside(realized: float, mtm: float):
        return _plan(tr.book_state(tr.PAPER, open=1, realized_net=realized, mtm_net=mtm,
                                   open_risk_rs=sizing.remaining_stop_risk(at_entry, mtm)))

    flat = beside(0.0, 0.0)
    assert isinstance(flat, EntryPlan) and flat.decision["lots"] == 1
    for gain in (1_000.0, 3_000.0, 5_000.0, 8_000.0):
        win = beside(0.0, gain)
        assert isinstance(win, EntryPlan), (gain, win)
        assert (win.decision["lots"], win.decision["per_lot_risk_rs"]) == \
            (flat.decision["lots"], flat.decision["per_lot_risk_rs"]), gain
        assert -at_entry - win.decision["per_lot_risk_rs"] * win.decision["lots"] > -DEFAULTS.DAILY_STOP_RS
    # a loss inside the stop counts the planned stop-out, as before: Rs 7,000 realized with the first position
    #   Rs 2,000 down leaves Rs 2,000, under one lot's Rs 3,260; Rs 5,000 realized leaves Rs 4,000
    assert beside(-7_000.0, -2_000.0) == Refusal("SIZE", "DAY_BUDGET")
    assert isinstance(beside(-5_000.0, -2_000.0), EntryPlan)
    # a first position gapped past its stop counts its loss now (Rs 8,000, not its Rs 6,000 plan), as before: with
    #   Rs 4,000 realized Rs 3,000 is left, under one lot
    assert sizing.remaining_stop_risk(at_entry, -8_000.0) == 8_000.0
    assert beside(-4_000.0, -8_000.0) == Refusal("SIZE", "DAY_BUDGET")


def test_today_s_caps_a_winning_first_position_never_refuses_a_safe_second_entry():
    """Ported from test_n6_gain_double_count.py, size() with today's defaults: a Rs 30k 1-lot price_spike_up entry
    plans a Rs 6,128.46 stop-out. With the book's first position (the same plan) up Rs 3,000 or Rs 5,000 the second
    was refused SIZE:DAY_BUDGET, although both stopping out as planned puts the day at -Rs 12,256.92 every time."""
    inst = Instrument("NFO:TCS26OCT3100CE", 777, "TCS26OCT3100CE", "NFO", "TCS", "NSE:TCS", "CE", 3100.0,
                      tr.INST.expiry, 500, 0.05)
    q = Quote(inst.symbol, inst.token, (Level(59.95, 5000),), (Level(60.05, 5000),), 60.0, 1000,
              tr.NOW_WALL - timedelta(seconds=5), None, tr.NOW_MONO - 0.3, tr.NOW_WALL, "REST")
    prof = strategies.REGISTRY["price_spike_up"].EXIT_PROFILE

    def size(open_risk):
        return sizing.size(inst, q, 60.10, prof, DEFAULTS, 0.0, tr.RATES, Policy.FULL, open_risk_rs=open_risk)

    first = size(0.0)
    assert isinstance(first, Sized) and first.lots == 1
    at_entry = first.per_lot_risk_rs * first.lots
    assert at_entry == pytest.approx(6_128.46, abs=0.01)
    for gain in (0.0, 1_000.0, 2_000.0, 3_000.0, 5_000.0):
        second = size(sizing.remaining_stop_risk(at_entry, gain))
        assert isinstance(second, Sized) and second.lots == 1, (gain, second)
        assert -at_entry - second.per_lot_risk_rs * second.lots == pytest.approx(-12_256.92, abs=0.01)


def test_an_open_winner_counts_its_stop_out_from_its_entry_not_its_gain(tmp_path, core):
    """Engine.book_state for the probes' first position: up about Rs 950 it counts its planned Rs 950 stop-out, not
    Rs 1,900 (the gain as risk still to lose). A trailed stop takes what it moved off it; a stop locked above the
    entry leaves nothing to lose; a position gapped past its stop and not exited yet counts its loss now."""
    r = Rig(tmp_path)
    try:
        r.tick()
        leg = r.open_leg()                                              # 100 bought at 42.20
        st = r.struct(leg.leg_id)
        st.decision = {**(st.decision or {}), "plan": {"per_lot_risk_rs": 950.0, "lots": 1}}
        r.book(52.00, 52.20)                                            # up Rs 9.80 a unit at the bid
        gain = r.eng.leg_mtm(leg)
        assert gain is not None and gain > 900.0
        bs = r.eng.book_state(PAPER_BOOK)
        assert bs.open_risk_rs == pytest.approx(950.0), "the open gain was counted as risk still to lose"
        assert bs.realized_net == 0.0 and tr.risk.book_day_net(bs) == pytest.approx(gain)
        hard = st.exit_params.hard_sl_px
        leg.stop_px = round(hard + 4.0, 2)                              # trailed 4.00 above the hard stop
        assert r.eng.book_state(PAPER_BOOK).open_risk_rs == pytest.approx(950.0 - 100 * 4.0, abs=1.0)
        leg.stop_px = round(hard + 12.0, 2)                             # locked above the entry
        assert r.eng.book_state(PAPER_BOOK).open_risk_rs == 0.0
        r.book(30.00, 30.20)                                            # gapped past its stop, still open
        loss = -r.eng.leg_mtm(leg)
        assert loss > 1_200.0 and r.eng.book_state(PAPER_BOOK).open_risk_rs == pytest.approx(loss)
    finally:
        r.store.close()
