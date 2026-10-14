"""Small helpers shared by the scenario, chaos and restart tests (assertions read the DB and the fakes)."""

from __future__ import annotations

from typing import Callable

from titan2.broker.paper import fill_draw
from titan2.model import Account, Intent, Role, make_tag, parse_tag

from fakes.runner import DEFAULT_STRATEGY, ScenarioRunner

TERMINAL_STRUCT = ("CLOSED", "CLOSED_EXTERNAL", "VOID")


def wait_for(r: ScenarioRunner, cond: Callable[[], bool], timeout_s: float = 10.0, what: str = "",
             dt: float | None = None) -> None:
    assert r.run_until_true(cond, timeout_s, dt), f"timed out after {timeout_s}s waiting for {what or cond}"


def struct_of(r: ScenarioRunner, intent: Intent | str, book: str | None = None) -> dict | None:
    iid = intent if isinstance(intent, str) else intent.intent_id
    book = book or r.book(intent.strategy if isinstance(intent, Intent) else DEFAULT_STRATEGY)
    rows = r.rows("SELECT * FROM structures WHERE intent_id = ? AND book = ?", (iid, book))
    return rows[-1] if rows else None


def state_of(r: ScenarioRunner, intent: Intent | str, book: str | None = None) -> str | None:
    s = struct_of(r, intent, book)
    return None if s is None else s["state"]


def enter(r: ScenarioRunner, underlying: str = "NSE:RELIANCE", direction: str = "UP", *,
          strategy: str = DEFAULT_STRATEGY, timeout_s: float = 6.0, expect: str | None = "OPEN",
          **kw) -> tuple[Intent, dict]:
    """Post an intent and run until its structure in the account's book leaves RESERVED/ENTERING."""
    i = r.intent(underlying, direction, strategy=strategy, **kw)
    book = r.book(strategy)
    wait_for(r, lambda: state_of(r, i, book) not in (None, "RESERVED", "ENTERING"), timeout_s,
             f"{book} {underlying} entry to resolve")
    s = struct_of(r, i, book)
    if expect is not None:
        assert s is not None and s["state"] == expect, f"{book} {underlying}: {s and s['state']} " \
                                                        f"({s and s['void_reason']}), expected {expect}"
    return i, s


def leg_of(r: ScenarioRunner, structure_id: int):
    """The engine's live Leg object (bound to its orders) of a structure."""
    st = r.engine.state.structures[structure_id]
    return r.engine.state.legs[st.legs[0]]


def leg_row(r: ScenarioRunner, structure_id: int) -> dict:
    return r.rows("SELECT * FROM legs WHERE structure_id = ?", (structure_id,))[0]


def orders_of(r: ScenarioRunner, structure_id: int, role: str | None = None) -> list[dict]:
    sql = "SELECT * FROM orders WHERE structure_id = ?"
    params: list = [structure_id]
    if role is not None:
        sql += " AND role = ?"
        params.append(role)
    return r.rows(sql + " ORDER BY created_ts, order_pk", params)


def fill_draw_of(order: dict) -> float:
    """A bot order's fill draw (paper model 2): its PAPER tag's, which FakeKite gives a LIVE order too, so an
    expectation built on it holds in both runs of the dual fixture."""
    p = parse_tag(order["order_pk"])
    return fill_draw(p.session, Account.PAPER, make_tag(p.role, Account.PAPER, p.session, p.seq_no, p.attempt))


def route_of(r: ScenarioRunner, intent: Intent | str, book: str) -> dict | None:
    iid = intent if isinstance(intent, str) else intent.intent_id
    rows = r.rows("SELECT * FROM intent_routes WHERE intent_id = ? AND book = ?", (iid, book))
    return rows[0] if rows else None


def exit_places(r: ScenarioRunner) -> list:
    return r.places(Role.EXIT)


def entry_places(r: ScenarioRunner) -> list:
    return r.places(Role.ENTRY)


def sells_at_broker(r: ScenarioRunner) -> int:
    """Bot SELL orders that reached the account's broker."""
    out = 0
    for o in r.broker_orders():
        side = o.side if hasattr(o, "side") else o["side"]
        out += side == "SELL"
    return out
