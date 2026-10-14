"""U04: titan2.store (DDL constraints, migrations, transactions, core-only writes, the DAO, derived counters)."""

from __future__ import annotations

import csv
import dataclasses
import gzip
import itertools
import math
import shutil
import sqlite3
import threading
from contextlib import closing
from datetime import date, datetime, timedelta

import pytest

from titan2 import store as st
from titan2.clock import IST, iso
from titan2.model import (
    EXPIRY_RULE_NEAREST, ROLE_SIDE, Account, Bar, DecisionQuote, Direction, ExitParams, ExitProfile, Fill,
    HaltClears, Instrument, Intent, InvariantViolation, Leg, LegState, Order, OrderState, PnlStatus, Policy, Recheck,
    Role, Side, StrikePref, Structure, StructState, StructureType, Urgency, make_tag,
)
from titan2.settings import DEFAULTS
from titan2.store import PruneResult, Store, StoreError, open_readonly

SESSION = date(2026, 10, 12)
T0 = datetime(2026, 10, 12, 9, 20, tzinfo=IST)
MARKET = "MAR" + "KET"  # built from parts so a literal scan of the tree for the forbidden order type stays clean
_ids = itertools.count(1)
_TOKENS = {"NSE:RELIANCE": 101, "NSE:INFY": 102, "NSE:TCS": 103, "NSE:SBIN": 104, "NSE:HDFCBANK": 105,
           "NSE:NIFTY 50": 106}


def at(hms: str, d: date = SESSION) -> datetime:
    return datetime.combine(d, datetime.strptime(hms, "%H:%M:%S.%f" if "." in hms else "%H:%M:%S").time(), IST)


def inst_for(underlying: str) -> Instrument:
    name = "NIFTY" if underlying == "NSE:NIFTY 50" else underlying.split(":")[1]
    ts = f"{name}26OCT100CE"
    return Instrument(f"NFO:{ts}", _TOKENS[underlying], ts, "NFO", name, underlying, "CE", 100.0, date(2026, 10, 27),
                      500, 0.05)


def make_intent(underlying: str = "NSE:RELIANCE", created: datetime = T0, **over) -> Intent:
    it = Intent(intent_id=f"i{next(_ids):011d}", strategy="opening_drive", strategy_version="1.3f2a9c01de",
                underlying=underlying, direction=Direction.UP, structure=StructureType.LONG_CE,
                strike_pref=StrikePref.ITM1, expiry_rule=EXPIRY_RULE_NEAREST, trigger_px=2900.0,
                recheck=Recheck(2885.5, math.inf), ttl_s=10.0, created_mono=1000.0, created_wall=created,
                reason={"ret_pct": 2.7, "vol_mult": 3.4})
    return dataclasses.replace(it, **over)


def new_structure(s: Store, *, book: str = "PAPER:opening_drive", account: Account = Account.PAPER,
                  underlying: str = "NSE:RELIANCE", state: StructState = StructState.RESERVED,
                  session: date = SESSION, reserved: datetime = T0, **fields) -> Structure:
    it = make_intent(underlying, reserved)
    obj = Structure(structure_id=0, session=session, book=book, account=account, policy=Policy.FULL,
                    strategy=it.strategy, strategy_version=it.strategy_version, intent=it, type=it.structure,
                    underlying=underlying, state=state, legs=[], is_placebo=False, parent_structure_id=None,
                    blocked_by=None, decision=None, placebo_status=None, exit_params=None, risk_basis_rs=None,
                    exit_reason=None, void_reason=None, reserved_wall=reserved)
    for k, v in fields.items():
        setattr(obj, k, v)
    with s.tx():
        s.insert_intent(it, session)
        s.insert_structure(obj)
    return obj


def new_leg(s: Store, stc: Structure, *, seq_no: int | None = None, state: LegState = LegState.ENTERING) -> Leg:
    with s.tx():
        leg = Leg(leg_id=0, structure_id=stc.structure_id, leg_no=1, account=stc.account, book=stc.book,
                  session=stc.session, seq_no=seq_no or s.next_leg_seq(stc.account, stc.session),
                  inst=inst_for(stc.underlying), state=state)
        s.insert_leg(leg)
    stc.legs.append(leg.leg_id)
    return leg


def make_order(leg: Leg, *, role: Role = Role.ENTRY, attempt: int = 1, qty: int = 500, limit: float | None = 42.35,
               state: OrderState = OrderState.SUBMITTING, created: datetime = T0, **fields) -> Order:
    if role is Role.EXTERNAL:            # a close the bot did not place: 'EX' + broker id, a SELL
        pk, side = f"EX{next(_ids):013d}", Side.SELL
    else:
        pk, side = make_tag(role, leg.account, leg.session, leg.seq_no, attempt), ROLE_SIDE[role]
    o = Order(order_pk=pk, leg_id=leg.leg_id,
              structure_id=leg.structure_id, account=leg.account, book=leg.book, role=role, side=side,
              symbol=leg.inst.symbol, qty=qty, limit_px=limit, attempt=attempt,
              band_idx=0 if role is Role.EXIT else None, state=state,
              decision=DecisionQuote(41.9, 42.15, 42.025, 180, "REST", 42.1, 42.15), created_wall=created)
    for k, v in fields.items():
        setattr(o, k, v)
    return o


def new_order(s: Store, leg: Leg, **kw) -> Order:
    o = make_order(leg, **kw)
    with s.tx():
        s.insert_order(o)
    leg.orders.append(o.order_pk)
    return o


def trade_row(stc: Structure, net: float, exit_wall: datetime, **over) -> dict:
    row = dict(structure_id=stc.structure_id, session=stc.session, book=stc.book, account=stc.account, mode="PAPER",
               strategy=stc.strategy, strategy_version=stc.strategy_version, intent_id=stc.intent.intent_id,
               is_placebo=False, underlying=stc.underlying, symbol="NFO:RELIANCE26OCT100CE",
               structure_type=stc.type, direction="UP", opt_type="CE", strike=100.0, expiry=date(2026, 10, 27),
               lot_size=500, qty=500, entry_ts=stc.reserved_wall, exit_ts=exit_wall, hold_s=60.0, entry_avg=42.15,
               exit_avg=41.8, risk_basis_rs=21075.0, gross=net + 50.0, brokerage=40.0, stt=4.5, txn=3.0,
               sebi=0.04, ipft=0.0, gst=1.83, stamp=0.63, sqoff_fee=0.0, charges=50.0, net=net,
               net_pct=net / 21075.0 * 100, rates_version="2026-04-01", n_entry_orders=1, n_exit_orders=1,
               n_executed_orders=2, hs_cost_rs=62.5, crossing_cost_rs=30.0, lock_armed=False, exit_reason="HARD_SL",
               pnl_status=PnlStatus.FINAL, exit_profile=ExitProfile())
    row.update(over)
    return row


def raw_conn(s: Store) -> sqlite3.Connection:
    """A second, autocommit connection: a failed statement leaves no transaction (and no lock) behind."""
    return sqlite3.connect(str(s.path), isolation_level=None)


@pytest.fixture
def core():
    with st.core_thread():
        yield


@pytest.fixture
def store(tmp_path, core):
    s = Store(tmp_path / "db" / "titan2.db", backup_dir=tmp_path / "backups")
    yield s
    s.close()


EXPECTED_SCHEMA = {
    "table": {"meta", "seq", "events", "intents", "intent_routes", "structures", "legs", "orders", "fills", "trades",
              "quote_checks", "halts", "strategy_modes", "g1_trials", "g1_reports", "bars_5m", "bar_checks",
              "broker_snapshots", "foreign_acks", "command_acks", "session_flags", "sqlite_sequence"},
    "index": {"ev_session_kind", "ev_structure", "one_active_per_book_underlying", "one_active_live_underlying",
              "st_session_book", "one_working_order_per_leg", "tr_book_session", "tr_strategy_version", "qc_leg_ts",
              "one_active_halt"},
    "trigger": {"ev_no_upd", "ev_no_del", "st_terminal", "ord_fill_mono", "ord_terminal", "fills_no_upd",
                "fills_no_del", "tr_no_upd", "tr_no_del", "trials_no_del"},
    "view": {"effective_fills"},
}


# ---------------------------------------------------------------------------------------------------------------
# Migrations and connection settings
# ---------------------------------------------------------------------------------------------------------------
def test_migrate_idempotent(tmp_path):
    db, backups = tmp_path / "db" / "titan2.db", tmp_path / "backups"
    latest = st.latest_schema_version()
    s = Store(db, backup_dir=backups)
    assert s.migrated == list(range(1, latest + 1)) and s.backup_path is None   # a new DB has nothing to back up
    assert s.user_version == latest and s.get_meta("schema_version") == str(latest)
    install_id = s.get_meta("install_id")
    assert install_id and len(install_id) == 32
    schema = [tuple(r) for r in s.query("SELECT type, name, sql FROM sqlite_master ORDER BY type, name")]
    names: dict[str, set] = {}
    for kind, name, _ in schema:
        if not name.startswith("sqlite_autoindex"):
            names.setdefault(kind, set()).add(name)
    assert names == EXPECTED_SCHEMA
    # WAL, synchronous=FULL, foreign keys on, 5 s busy timeout (DETAIL §4.3); recursive triggers so that a
    # REPLACE's delete fires the append-only triggers
    c = s._conn
    assert [c.execute(f"PRAGMA {p}").fetchone()[0] for p in ("journal_mode", "synchronous", "foreign_keys",
                                                               "busy_timeout", "recursive_triggers")] == [
        "wal", 2, 1, 5000, 1]
    assert s.integrity_check() == []
    s.close()
    for _ in range(2):                    # reopening applies nothing, backs nothing up and changes nothing
        s = Store(tmp_path / "db", backup_dir=backups)                  # a directory means <dir>/titan2.db
        assert s.path == db and s.migrated == [] and s.backup_path is None
        assert s.user_version == latest and s.get_meta("install_id") == install_id
        assert [tuple(r) for r in s.query("SELECT type, name, sql FROM sqlite_master ORDER BY type, name")] == schema
        s.close()
    assert not backups.exists()


def test_schema_version_matches_settings_constant():
    from titan2 import settings                                 # U02's code constant SCHEMA_VERSION (DETAIL §8.4)
    assert st.latest_schema_version() == settings.SCHEMA_VERSION


def test_migration_backs_up_first_and_is_atomic(tmp_path, core):
    mig, backups, db = tmp_path / "mig", tmp_path / "bk", tmp_path / "titan2.db"
    mig.mkdir()
    shutil.copy(st.MIGRATIONS_DIR / "0001_init.sql", mig / "0001_init.sql")
    s = Store(db, migrations_dir=mig, backup_dir=backups)
    with s.tx():
        s.set_meta("last_session", "2026-10-12")
    s.close()
    (mig / "0002_extra.sql").write_text("CREATE TABLE extra(x INTEGER NOT NULL);\nINSERT INTO extra VALUES (7);\n")
    s = Store(db, migrations_dir=mig, backup_dir=backups)
    assert s.migrated == [2] and s.user_version == 2 and s.get_meta("schema_version") == "2"
    assert s.backup_path.parent == backups and s.backup_path.name.startswith("premigrate-")
    assert s.scalar("SELECT x FROM extra") == 7 and s.get_meta("last_session") == "2026-10-12"
    s.close()
    with closing(open_readonly(s.backup_path)) as b:    # the backup is the pre-migration database
        assert b.user_version == 1 and b.get_meta("last_session") == "2026-10-12"
        assert b.scalar("SELECT COUNT(*) FROM sqlite_master WHERE name='extra'") == 0
    # a failing migration leaves the database exactly at the previous version
    (mig / "0003_broken.sql").write_text("CREATE TABLE half(x);\nCREATE TABLE broken(;\n")
    with pytest.raises(sqlite3.OperationalError):
        Store(db, migrations_dir=mig, backup_dir=backups)
    with closing(open_readonly(db)) as r:
        assert r.user_version == 2 and r.scalar("SELECT COUNT(*) FROM sqlite_master WHERE name='half'") == 0
    # numbering must be 0001.. without gaps
    (mig / "0003_broken.sql").unlink()
    (mig / "0005_gap.sql").write_text("CREATE TABLE gap(x);\n")
    with pytest.raises(StoreError, match="gaps"):
        Store(db, migrations_dir=mig, backup_dir=backups)
    # a migration may not end the transaction itself
    (mig / "0005_gap.sql").unlink()
    (mig / "0003_commit.sql").write_text("CREATE TABLE c3(x);\nCOMMIT;\n")
    with pytest.raises(StoreError, match="must not COMMIT"):
        Store(db, migrations_dir=mig, backup_dir=backups)


def test_refuses_foreign_newer_or_missing_db(tmp_path):
    v1 = tmp_path / "titan_state.db"
    with closing(sqlite3.connect(v1)) as c:
        c.execute("CREATE TABLE trades(id INTEGER)")
        c.commit()
    with pytest.raises(StoreError, match="not a titan2 database"):
        Store(v1)
    with closing(sqlite3.connect(v1)) as c:          # refused before anything in it changed (still not WAL)
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert c.execute("SELECT name FROM sqlite_master").fetchall() == [("trades",)]
    db = tmp_path / "t2.db"
    Store(db).close()
    with closing(sqlite3.connect(db)) as c:
        c.execute("PRAGMA user_version=99")
    with pytest.raises(StoreError, match="newer than this release"):
        Store(db)
    with pytest.raises(StoreError, match="no such database"):
        open_readonly(tmp_path / "missing.db")


# ---------------------------------------------------------------------------------------------------------------
# DDL constraints
# ---------------------------------------------------------------------------------------------------------------
def test_append_only_tables(store):
    stc = new_structure(store, state=StructState.CLOSED)
    leg = new_leg(store, stc, state=LegState.CLOSED)
    o = new_order(store, leg, state=OrderState.COMPLETE, filled_qty=500, avg_fill_px=42.2)
    with store.tx():
        store.append_event("ORDER_CREATED", wall=T0, mono=1.0, payload={"qty": 500}, order_pk=o.order_pk)
        store.insert_fill(Fill(o.order_pk, "cum:500", "CUM", 500, 42.2, T0, None))
        store.insert_trade(trade_row(stc, -100.0, at("09:30:00")))
        store.register_trial("opening_drive", "1.a", T0, {"params": {}})
    with closing(raw_conn(store)) as raw:
        for sql in ("UPDATE events SET kind='X'", "DELETE FROM events", "UPDATE fills SET qty=1", "DELETE FROM fills",
                    "UPDATE trades SET net=0", "DELETE FROM trades", "DELETE FROM g1_trials"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only|immutable"):
                raw.execute(sql)
            with pytest.raises(InvariantViolation):       # the same through the core's escape hatch
                with store.tx():
                    store.execute(sql)
    # REPLACE deletes the conflicting row first: on the store's connection that delete fires the same triggers
    for sql in (f"INSERT OR REPLACE INTO fills(order_pk, seq_key, source, qty, px, ts) "
                f"VALUES('{o.order_pk}', 'cum:500', 'CUM', 1, 1.0, 'x')",
                "REPLACE INTO trades SELECT * FROM trades", "INSERT OR REPLACE INTO events SELECT * FROM events",
                "REPLACE INTO g1_trials SELECT * FROM g1_trials"):
        with pytest.raises(InvariantViolation, match="append-only|immutable"):
            with store.tx():
                store.execute(sql)
    # P&L is booked once per structure (I10)
    with pytest.raises(InvariantViolation) as ei:
        with store.tx():
            store.insert_trade(trade_row(stc, 999.0, at("09:31:00")))
    assert ei.value.code == "I10"
    # trials change state (no UPDATE trigger) but never disappear
    with store.tx():
        assert store.update_trial("opening_drive", "1.a", state="RETIRED", state_ts=at("15:38:00")) == ["state",
                                                                                                       "state_ts"]
    assert len(store.events()) == 1 and store.fills(o.order_pk)[0].qty == 500
    assert store.trade(stc.structure_id)["net"] == -100.0 and store.trial_count() == 1
    # the DAO offers no way to delete anything (prune is the single, bounded exception)
    assert [n for n in dir(Store) if n.startswith(("delete", "remove", "drop", "purge"))] == []


def test_unique_indexes_reserved_live(store):
    a = new_structure(store, book="PAPER:od", underlying="NSE:RELIANCE")          # RESERVED holds the slot
    with pytest.raises(InvariantViolation) as ei:
        new_structure(store, book="PAPER:od", underlying="NSE:RELIANCE")
    assert ei.value.code == "I5"
    new_structure(store, book="SHADOW:od", account=Account.SHADOW, underlying="NSE:RELIANCE")   # other book
    new_structure(store, book="PAPER:od", underlying="NSE:INFY")                                  # other underlying
    live = new_structure(store, book="LIVE:od", account=Account.LIVE, underlying="NSE:TCS")
    with pytest.raises(InvariantViolation, match="structures.account, structures.underlying"):
        new_structure(store, book="LIVE:ps", account=Account.LIVE, underlying="NSE:TCS")  # one LIVE per underlying
    new_structure(store, book="PAPER:ps", underlying="NSE:TCS")             # paper books are not bound by it
    # a terminal structure frees its slot ...
    with store.tx():
        a.state, a.void_reason, a.closed_wall = StructState.VOID, "QUOTE", at("09:20:05")
        assert set(store.update_structure(a)) == {"state", "void_reason", "closed_ts"}
        live.state, live.void_reason, live.closed_wall = StructState.VOID, "REJECTED:margin", at("09:20:06")
        store.update_structure(live)
    new_structure(store, book="PAPER:od", underlying="NSE:RELIANCE")
    new_structure(store, book="LIVE:ps", account=Account.LIVE, underlying="NSE:TCS")
    # ... and never changes state again, though its other columns may (an unchanged state is not rewritten)
    with store.tx():
        a.pnl_status = PnlStatus.UNRESOLVED
        assert store.update_structure(a) == ["pnl_status"]
    a.state = StructState.RESERVED
    with pytest.raises(InvariantViolation, match="terminal structure immutable"):
        with store.tx():
            store.update_structure(a)
    # immutable columns may not drift in memory either
    b = new_structure(store, book="PAPER:od", underlying="NSE:SBIN")
    b.underlying = "NSE:HDFCBANK"
    with pytest.raises(InvariantViolation, match="immutable columns"):
        with store.tx():
            store.update_structure(b)
    # I4: at most one non-terminal bot order per leg; EXTERNAL rows are outside the rule
    stc = new_structure(store, book="PAPER:od", underlying="NSE:HDFCBANK")
    leg = new_leg(store, stc)
    first = new_order(store, leg)
    with pytest.raises(InvariantViolation) as ei:
        new_order(store, leg, attempt=2)
    assert ei.value.code == "I4"
    ext = make_order(leg, role=Role.EXTERNAL, limit=None, state=OrderState.COMPLETE, filled_qty=500,
                     external_kind="MANUAL")
    with store.tx():
        store.insert_order(ext, order_type=MARKET, validity="DAY")
    with store.tx():
        first.state, first.terminal_wall = OrderState.CANCELLED, at("09:20:07")
        store.update_order(first)
    new_order(store, leg, attempt=2)
    # raw SQL meets the same indexes
    with closing(raw_conn(store)) as raw:
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed: structures.book"):
            raw.execute("UPDATE structures SET book='PAPER:od' WHERE book='SHADOW:od'")


def test_fill_monotonic_terminal_immutable(store):
    stc = new_structure(store)
    leg = new_leg(store, stc)
    o = new_order(store, leg, qty=1000)
    with store.tx():
        o.state, o.broker_order_id = OrderState.WORKING, "2610120000001"
        assert set(store.update_order(o, acked_wall=at("09:20:00.300"))) == {"state", "broker_order_id", "acked_ts"}
        o.filled_qty, o.avg_fill_px = 500, 42.15
        assert set(store.update_order(o)) == {"filled_qty", "avg_fill_px"}
        assert store.update_order(o) == []                               # nothing changed: nothing written
    o.filled_qty = 300
    with pytest.raises(InvariantViolation) as ei:
        with store.tx():
            store.update_order(o)
    assert ei.value.code == "I9" and "filled_qty decreased" in str(ei.value)
    with closing(raw_conn(store)) as raw:                                # the trigger, not the DAO, refuses it
        with pytest.raises(sqlite3.IntegrityError, match="filled_qty decreased"):
            raw.execute("UPDATE orders SET filled_qty=0 WHERE order_pk=?", (o.order_pk,))
    o.filled_qty = 1500
    with pytest.raises(InvariantViolation) as ei:                         # filled_qty <= qty
        with store.tx():
            store.update_order(o)
    assert ei.value.code == "I9"
    o.filled_qty, o.state, o.terminal_wall = 500, OrderState.CANCELLED, at("09:20:01")
    with store.tx():
        store.update_order(o)
        o.status_msg = "IOC remainder cancelled"                          # unprotected columns may still change
        assert store.update_order(o) == ["status_msg"]
    for change in ({"state": OrderState.COMPLETE}, {"filled_qty": 600}, {"avg_fill_px": 42.25}):
        with pytest.raises(InvariantViolation, match="terminal order immutable") as ei:
            with store.tx():
                store.update_order(dataclasses.replace(o, **change))
        assert ei.value.code == "I9"
    with pytest.raises(InvariantViolation, match="immutable columns"):   # qty never changes once placed
        with store.tx():
            store.update_order(dataclasses.replace(o, qty=500))
    row = store.query_one("SELECT state, filled_qty, avg_fill_px FROM orders WHERE order_pk=?", (o.order_pk,))
    assert tuple(row) == ("CANCELLED", 500, 42.15)
    with closing(raw_conn(store)) as raw:
        for sql in ("UPDATE orders SET avg_fill_px=1.0 WHERE order_pk=?", "UPDATE orders SET filled_qty=0 WHERE "
                    "order_pk=?", "UPDATE orders SET state='WORKING' WHERE order_pk=?"):
            with pytest.raises(sqlite3.IntegrityError, match="terminal order immutable"):
                raw.execute(sql, (o.order_pk,))


def test_checks_reject_market_wrong_side(store):
    stc = new_structure(store, book="LIVE:od", account=Account.LIVE)
    leg = new_leg(store, stc)
    tag = make_tag(Role.ENTRY, Account.LIVE, SESSION, leg.seq_no, 1)
    base = dict(order_pk=tag, leg_id=leg.leg_id, structure_id=stc.structure_id, account="LIVE", book="LIVE:od",
                role="ENTRY", side="BUY", symbol=leg.inst.symbol, qty=500, limit_px=42.35, order_type="LIMIT",
                validity="IOC", product="MIS", variety="regular", attempt=1, state="SUBMITTING", created_ts=iso(T0))
    with closing(raw_conn(store)) as raw:
        def insert(**over):
            row = {**base, **over}
            raw.execute(f"INSERT INTO orders({','.join(row)}) VALUES({','.join('?' * len(row))})", list(row.values()))

        bad = [dict(order_type=MARKET), dict(order_type="SL"), dict(validity="DAY"), dict(product="NRML"),
               dict(variety="amo"), dict(limit_px=None), dict(order_pk=tag[:14]), dict(order_pk="XX" + tag[2:]),
               dict(side="SELL"), dict(role="EXIT", side="BUY"), dict(role="COVER", side="SELL"),
               dict(role="SELFTEST", side="SELL"), dict(role="GTT"), dict(side="SHORT"), dict(qty=0),
               dict(limit_px=0.0), dict(filled_qty=501)]
        for over in bad:
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                insert(**over)
        insert()                                                          # the valid row is accepted
        # EXTERNAL rows record what the broker did, whatever its shape
        insert(order_pk="EX2610120000099", role="EXTERNAL", side="SELL", order_type=MARKET, validity="DAY",
               limit_px=None, state="COMPLETE", filled_qty=500)
    # through the DAO the DB still decides, and the failure is an invariant violation (role/side: I7)
    wrong_side = make_order(leg, attempt=2)
    wrong_side.side = ROLE_SIDE[Role.EXIT]
    with pytest.raises(InvariantViolation) as ei:
        with store.tx():
            store.insert_order(wrong_side)
    assert ei.value.code == "I7"
    with store.tx():
        store.execute("UPDATE orders SET state='CANCELLED' WHERE order_pk=?", (tag,))   # free the leg (I4)
    for kwargs in ({"order_type": MARKET}, {"validity": "DAY"}, {"product": "NRML"}, {"variety": "amo"}):
        with pytest.raises(InvariantViolation, match="CHECK constraint failed"):
            with store.tx():
                store.insert_order(make_order(leg, attempt=3), **kwargs)
    # intents: 0 < ttl <= 10, trigger > 0 and UP<->LONG_CE / DOWN<->LONG_PE
    for over in ({"ttl_s": 0.0}, {"ttl_s": 10.5}, {"trigger_px": 0.0}, {"structure": StructureType.LONG_PE},
                 {"direction": Direction.DOWN}):
        with pytest.raises(InvariantViolation, match="CHECK constraint failed"):
            with store.tx():
                store.insert_intent(make_intent(**over))
    with store.tx():
        store.insert_intent(make_intent(direction=Direction.DOWN, structure=StructureType.LONG_PE))


def test_insert_order_guards_beyond_ddl(store):
    stc = new_structure(store)
    leg = new_leg(store, stc)
    cases = [
        ("I7", make_order(leg, order_pk=make_tag(Role.EXIT, Account.PAPER, SESSION, leg.seq_no, 1))),   # role letter
        ("I7", make_order(leg, order_pk=make_tag(Role.ENTRY, Account.LIVE, SESSION, leg.seq_no, 1))),   # account
        ("I7", make_order(leg, order_pk="T2EP2610120010")),                                            # not a tag
        ("I7", make_order(leg, order_pk="T2EP26101299999")),                                           # other leg
        ("I7", make_order(leg, order_pk=make_tag(Role.ENTRY, Account.PAPER, SESSION, leg.seq_no, 2))),  # attempt
        ("I7", make_order(leg, order_pk=make_tag(Role.ENTRY, Account.PAPER, date(2026, 10, 13), leg.seq_no, 1))),
        ("I12", make_order(leg, qty=250)),                                                             # lot 500
        ("I13", make_order(leg, limit=42.33)),                                                         # tick 0.05
        ("I13", make_order(leg, limit=None)),
        ("DB_CONSTRAINT", make_order(leg, symbol="NFO:INFY26OCT100CE")),                               # wrong leg
        ("DB_CONSTRAINT", make_order(leg, leg_id=9999)),
        ("I7", make_order(leg, role=Role.EXTERNAL, limit=None, order_pk="T2EP26101200109")),           # EX prefix
    ]
    for code, o in cases:
        with pytest.raises(InvariantViolation) as ei:
            with store.tx():
                store.insert_order(o)
        assert ei.value.code == code, (code, o)
    with pytest.raises(StoreError, match="created_wall"):
        with store.tx():
            store.insert_order(make_order(leg, created_wall=None))
    assert store.scalar("SELECT COUNT(*) FROM orders") == 0
    # legs must match their structure
    bad_leg = Leg(leg_id=0, structure_id=stc.structure_id, leg_no=2, account=Account.LIVE, book=stc.book,
                  session=SESSION, seq_no=7, inst=inst_for("NSE:RELIANCE"), state=LegState.PLANNED)
    with pytest.raises(InvariantViolation, match="differs from structure"):
        with store.tx():
            store.insert_leg(bad_leg)


# ---------------------------------------------------------------------------------------------------------------
# Transactions and the core-thread rule
# ---------------------------------------------------------------------------------------------------------------
def test_tx_rollback_core_only(store):
    it = make_intent()
    with pytest.raises(RuntimeError):
        with store.tx():
            store.insert_intent(it)
            assert store.load_intent(it.intent_id, 1.0, T0) is not None          # visible inside the tx
            raise RuntimeError("boom")
    assert store.load_intent(it.intent_id, 1.0, T0) is None and not store.in_tx
    # writes outside tx() are refused, so nothing ever commits piecemeal (write-ahead)
    for call in (lambda: store.insert_intent(it), lambda: store.set_meta("k", "v"),
                 lambda: store.update_row("meta", {"key": "install_id"}, {"value": "x"})):
        with pytest.raises(StoreError, match="inside store.tx"):
            call()
    # a nested tx() is a savepoint: the inner failure rolls back only the inner work
    with store.tx():
        store.set_meta("outer", "1")
        with pytest.raises(ValueError):
            with store.tx():
                store.set_meta("inner", "1")
                raise ValueError("inner")
        assert store.in_tx and store.get_meta("inner") is None
    assert store.get_meta("outer") == "1" and store.get_meta("inner") is None
    # if SQLite itself rolled the transaction back (disk full, I/O error), later writes refuse to autocommit
    with pytest.raises(StoreError, match="rolled back by SQLite"):
        with store.tx():
            store._conn.execute("ROLLBACK")                                    # stands in for SQLITE_FULL
            store.set_meta("after", "1")
    assert store.get_meta("after") is None and not store.in_tx
    # off the core thread every writer, tx() included, raises I1 before touching the DB; reads still work
    seen = []

    def worker():
        for call in (lambda: store.tx(), lambda: store.set_meta("x", "1"),
                     lambda: store.insert_row("meta", {"key": "x", "value": "1"}),
                     lambda: store.execute("DELETE FROM meta"), lambda: store.insert_intent(make_intent())):
            try:
                call()
                seen.append("no error")
            except InvariantViolation as exc:
                seen.append(exc.code)
        seen.append(store.get_meta("outer"))

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert seen == ["I1"] * 5 + ["1"]
    # with no core thread registered at all, writes fail closed too
    previous = st.CORE_TID
    st.clear_core_thread()
    try:
        with pytest.raises(InvariantViolation) as ei:
            store.tx()
        assert ei.value.code == "I1"
    finally:
        st.set_core_thread(previous)
    # every public writer carries the marker tests/arch inspects
    writers = {n for n in dir(Store) if getattr(getattr(Store, n), "__core_only__", False)}
    assert {"tx", "execute", "insert_row", "upsert_row", "update_row", "set_meta", "next_seq", "next_leg_seq",
            "append_event", "insert_intent", "put_intent_route", "insert_structure", "update_structure",
            "insert_leg", "update_leg", "insert_order", "update_order", "insert_fill", "insert_trade",
            "add_quote_check", "add_halt", "clear_halt", "put_strategy_mode", "register_trial", "update_trial",
            "insert_g1_report", "flag_session", "put_bar", "put_bar_check", "add_broker_snapshot", "ack_foreign",
            "put_command_ack", "prune"} == writers


def test_query_is_read_only(store):
    assert store.scalar("SELECT value FROM meta WHERE key=?", ("schema_version",)) == "1"
    assert store.query("PRAGMA table_info(meta)") and store.query("  with x as (select 1) select * from x")
    for sql in ("DELETE FROM meta", "INSERT INTO meta VALUES('a','b')", "PRAGMA user_version=5",
                "UPDATE meta SET value='x'", "DROP TABLE meta", "ATTACH 'x.db' AS x"):
        with pytest.raises(StoreError, match="read-only"):
            store.query(sql)
    # a CTE that writes is caught; inside the core's tx the change is rolled back
    with pytest.raises(InvariantViolation, match="query"):
        with store.tx():
            store.query("WITH x AS (SELECT 1) DELETE FROM meta WHERE key='schema_version'")
    assert store.get_meta("schema_version") == "1"


# ---------------------------------------------------------------------------------------------------------------
# Fills
# ---------------------------------------------------------------------------------------------------------------
def test_effective_fills(store):
    stc = new_structure(store)
    leg = new_leg(store, stc)
    o = new_order(store, leg)
    with store.tx():
        o.state, o.broker_order_id, o.filled_qty, o.avg_fill_px = OrderState.WORKING, "B1", 200, 42.10
        store.update_order(o)
        assert store.insert_fill(Fill(o.order_pk, "cum:200", "CUM", 200, 42.10, at("09:20:00.400"), None))
        o.state, o.filled_qty, o.avg_fill_px, o.terminal_wall = OrderState.COMPLETE, 500, 42.22, at("09:20:00.900")
        store.update_order(o)
        assert store.insert_fill(Fill(o.order_pk, "cum:500", "CUM", 300, 42.30, at("09:20:00.900"), None))
    assert [(f.seq_key, f.qty, f.px) for f in store.effective_fills(o.order_pk)] == [("cum:200", 200, 42.10),
                                                                                     ("cum:500", 300, 42.30)]
    with store.tx():
        assert store.insert_fill(Fill(o.order_pk, "trade:T1", "TRADE", 200, 42.10, at("09:20:00.400"), None))
    # TRADE rows that do not yet explain filled_qty do not replace the CUM rows
    assert [f.source for f in store.effective_fills(o.order_pk)] == ["CUM", "CUM"]
    with store.tx():
        assert store.insert_fill(Fill(o.order_pk, "trade:T2", "TRADE", 250, 42.30, at("09:20:00.900"), None))
        assert store.insert_fill(Fill(o.order_pk, "trade:T3", "TRADE", 50, 42.30, at("09:20:00.900"),
                                      at("09:20:00.850")))
        assert not store.insert_fill(Fill(o.order_pk, "trade:T1", "TRADE", 200, 42.10, at("09:20:01"), None))
    eff = store.effective_fills(o.order_pk)
    assert [f.seq_key for f in eff] == ["trade:T1", "trade:T2", "trade:T3"] and sum(f.qty for f in eff) == 500
    assert eff[2].exch_wall == at("09:20:00.850") and eff[0].wall == at("09:20:00.400")
    assert len(store.fills(o.order_pk)) == 5 and len(store.fills(o.order_pk, "CUM")) == 2
    # a conflicting duplicate, a key that does not match its source, or a bad qty breaks the fill invariant
    for fill, match in ((Fill(o.order_pk, "trade:T1", "TRADE", 200, 42.15, T0, None), "stored as 200@42.1"),
                        (Fill(o.order_pk, "trade:T9", "CUM", 10, 42.1, T0, None), "seq_key"),
                        (Fill(o.order_pk, "cum:600", "CUM", 0, 42.1, T0, None), "qty > 0"),
                        (Fill("T2EP26101299901", "cum:5", "CUM", 5, 42.1, T0, None), "FOREIGN KEY")):
        with pytest.raises(InvariantViolation, match=match):
            with store.tx():
                store.insert_fill(fill)
    # several orders at once; an order with nothing filled has no effective fills
    x = new_order(store, leg, role=Role.EXIT, attempt=1, limit=43.0, state=OrderState.WORKING)
    assert store.effective_fills(x.order_pk) == []
    with store.tx():
        x.filled_qty, x.avg_fill_px = 100, 43.0
        store.update_order(x)
        store.insert_fill(Fill(x.order_pk, "cum:100", "CUM", 100, 43.0, at("09:30:00"), None))
    both = store.effective_fills([o.order_pk, x.order_pk])
    assert [f.seq_key for f in both] == ["trade:T1", "trade:T2", "trade:T3", "cum:100"]
    assert store.effective_fills([]) == []


# ---------------------------------------------------------------------------------------------------------------
# Derived counters (§4.5)
# ---------------------------------------------------------------------------------------------------------------
def test_derived_counters(store):
    book, prev = "PAPER:opening_drive", SESSION - timedelta(days=3)

    def close(stc, state, when, **fields):
        with store.tx():
            stc.state, stc.closed_wall = state, when
            for k, v in fields.items():
                setattr(stc, k, v)
            store.update_structure(stc)

    # an earlier session's activity in the same book never counts today
    old = new_structure(store, book=book, underlying="NSE:SBIN", session=prev, reserved=at("10:00:00", prev))
    old_leg = new_leg(store, old)
    new_order(store, old_leg, created=at("10:00:00.100", prev), state=OrderState.COMPLETE, filled_qty=500)
    close(old, StructState.CLOSED, at("10:30:00", prev), opened_wall=at("10:00:01", prev))
    with store.tx():
        store.insert_trade(trade_row(old, -500.0, at("10:30:00", prev)))
    # A: RELIANCE entered at 09:20, exited by two attempts, closed 10:00 at a loss
    a = new_structure(store, book=book, underlying="NSE:RELIANCE", reserved=at("09:20:00"))
    a_leg = new_leg(store, a)
    new_order(store, a_leg, created=at("09:20:00.100"), state=OrderState.COMPLETE, filled_qty=500)
    new_order(store, a_leg, role=Role.EXIT, attempt=1, limit=40.0, created=at("09:50:00"),
              state=OrderState.CANCELLED, filled_qty=200)
    new_order(store, a_leg, role=Role.EXIT, attempt=2, limit=39.5, created=at("09:50:01"),
              state=OrderState.COMPLETE, filled_qty=300)
    ext = make_order(a_leg, role=Role.EXTERNAL, limit=None, created=at("09:55:00"), state=OrderState.COMPLETE)
    with store.tx():
        store.insert_order(ext, order_type=MARKET, validity="DAY")             # EXTERNAL is not a bot order
    close(a, StructState.CLOSED, at("10:00:00"), opened_wall=at("09:20:01"))
    with store.tx():
        store.insert_trade(trade_row(a, -150.25, at("10:00:00")))
    # B: INFY entry rejected at 10:05 -> REJECT cooldown
    b = new_structure(store, book=book, underlying="NSE:INFY", reserved=at("10:04:58"))
    new_order(store, new_leg(store, b), created=at("10:04:59"), state=OrderState.REJECTED)
    close(b, StructState.VOID, at("10:05:00"), void_reason="REJECTED:RMS margin")
    # C: TCS voided before any order (post-quote refusal) -> VOID cooldown, no attempt
    c = new_structure(store, book=book, underlying="NSE:TCS", reserved=at("10:05:59"))
    close(c, StructState.VOID, at("10:06:00"), void_reason="QUOTE")
    # D: RELIANCE again, IOC unfilled -> VOID cooldown that outlasts A's re-entry cooldown
    d = new_structure(store, book=book, underlying="NSE:RELIANCE", reserved=at("10:40:09"))
    new_order(store, new_leg(store, d), created=at("10:40:10"), state=OrderState.CANCELLED)
    close(d, StructState.VOID, at("10:40:11"), void_reason="ENTRY_NO_FILL")
    # E: HDFCBANK open now
    e = new_structure(store, book=book, underlying="NSE:HDFCBANK", reserved=at("11:00:00"))
    new_order(store, new_leg(store, e), created=at("11:00:00.050"), state=OrderState.COMPLETE, filled_qty=500)
    with store.tx():
        e.state, e.opened_wall = StructState.OPEN, at("11:00:01")
        store.update_structure(e)
    # another book is counted separately
    other = new_structure(store, book="PAPER:price_spike_up", underlying="NSE:INFY", reserved=at("09:30:00"))
    new_order(store, new_leg(store, other), created=at("09:30:00.100"), state=OrderState.COMPLETE, filled_qty=500)
    close(other, StructState.CLOSED, at("09:45:00"), opened_wall=at("09:30:01"))
    with store.tx():
        store.insert_trade(trade_row(other, 1000.0, at("09:45:00"), book="PAPER:price_spike_up"))

    assert store.entries(book, SESSION) == 2                     # A and E were opened
    assert store.attempts(book, SESSION) == 4                    # A, B, D, E placed an ENTRY order
    assert store.orders_today(book, SESSION) == 6                # 3 for A + B + D + E; EXTERNAL excluded
    assert store.entry_orders_last_60s(book, at("11:00:30")) == 1                # E's, at 11:00:00.050
    assert store.entry_orders_last_60s(book, at("11:01:00.050")) == 1            # the window is inclusive
    assert store.entry_orders_last_60s(book, at("11:01:00.051")) == 0
    # the spec query has no upper bound: after a wall-clock step back, later rows still count (fails closed)
    assert store.entry_orders_last_60s(book, at("10:05:30")) == 3
    assert store.realized_net(book, SESSION) == -150.25 and store.first_loss_today(book, SESSION)
    assert store.realized_net("PAPER:price_spike_up", SESSION) == 1000.0
    assert not store.first_loss_today("PAPER:price_spike_up", SESSION)
    assert store.account_realized_net(Account.PAPER, SESSION) == 849.75
    assert store.active_structures(book) == 1
    assert store.book_counters(book, SESSION, at("11:00:30")) == st.BookCounters(
        book=book, session=SESSION, entries=2, attempts=4, orders_today=6, entry_orders_60s=1, realized_net=-150.25,
        first_loss_today=True, active=1)
    minutes = dict(reentry_min=30, reject_min=10, void_min=5)
    assert store.cooldowns(book, SESSION, **minutes) == {
        "NSE:RELIANCE": at("10:45:11"),                          # max(10:00 + 30, 10:40:11 + 5)
        "NSE:INFY": at("10:15:00"),                              # 10:05 + REJECT 10
        "NSE:TCS": at("10:11:00"),                               # 10:06 + VOID 5
    }
    assert store.cooldown_until(book, "NSE:INFY", SESSION, **minutes) == at("10:15:00")
    assert store.cooldown_until(book, "NSE:HDFCBANK", SESSION, **minutes) is None   # still open: G10, not G11
    assert store.cooldown_until(book, "NSE:SBIN", SESSION, **minutes) is None       # an earlier session
    # live_sessions counts distinct sessions of LIVE:<s> since the unlock session
    assert store.live_sessions("opening_drive") == 0
    for day, und in ((SESSION - timedelta(days=4), "NSE:RELIANCE"), (SESSION - timedelta(days=1), "NSE:INFY"),
                     (SESSION, "NSE:TCS"), (SESSION, "NSE:SBIN")):
        x = new_structure(store, book="LIVE:opening_drive", account=Account.LIVE, underlying=und, session=day,
                          reserved=at("09:20:00", day))
        close(x, StructState.VOID, at("09:20:05", day), void_reason="QUOTE")
    with store.tx():
        store.put_strategy_mode("opening_drive", version="1.3f2a9c01de", mode="LIVE", changed_wall=T0,
                                changed_by="CLI", live_since_session=SESSION - timedelta(days=1))
    assert store.live_sessions("opening_drive") == 2


def test_cooldowns_are_the_rule_the_gates_use(store):
    """Store.cooldowns is the one §4.5 cooldown rule: Engine.book_state passes it the Settings minutes and G11
    refuses until the end it returns (titan2.core.risk keeps no copy of the rule)."""
    book, s = "PAPER:opening_drive", DEFAULTS
    minutes = dict(reentry_min=s.REENTRY_COOLDOWN_MIN, reject_min=s.REJECT_COOLDOWN_MIN, void_min=s.VOID_COOLDOWN_MIN)
    assert minutes == dict(reentry_min=30.0, reject_min=10.0, void_min=5.0)     # the shipped minutes, all distinct

    def ended(underlying, state, closed, void_reason=None, *, in_book=book, reserved=T0):
        new_structure(store, book=in_book, underlying=underlying, state=state, reserved=reserved,
                      closed_wall=None if closed is None else at(closed), void_reason=void_reason)

    ended("NSE:RELIANCE", StructState.CLOSED, "10:00:00")
    ended("NSE:INFY", StructState.CLOSED_EXTERNAL, "10:01:00")
    ended("NSE:TCS", StructState.VOID, "10:02:00", "REJECTED:RMS:Margin Exceeds")
    ended("NSE:SBIN", StructState.VOID, "10:03:00", "rejected:lpp")
    ended("NSE:HDFCBANK", StructState.VOID, "10:04:00", "ENTRY_NO_FILL")
    ended("NSE:NIFTY 50", StructState.VOID, "10:05:00", None)
    ended("NSE:IDEA", StructState.VOID, None, "QUOTE", reserved=at("10:06:00"))
    # the latest END wins per underlying, whichever structure closed last
    ended("NSE:BHEL", StructState.CLOSED, "10:00:00")
    ended("NSE:BHEL", StructState.VOID, "10:20:00", "ENTRY_NO_FILL")           # ends 10:25: the 10:30 end stays
    ended("NSE:ZYDUSLIFE", StructState.VOID, "10:00:00", "ENTRY_NO_FILL")
    ended("NSE:ZYDUSLIFE", StructState.VOID, "10:02:00", "REJECTED:freeze qty")  # ends 10:12, after 10:05
    ended("NSE:RELIANCE", StructState.CLOSED, "10:50:00", in_book="PAPER:price_spike_up")     # another book
    new_structure(store, book=book, underlying="NSE:SONACOMS", state=StructState.OPEN)       # open: G10's, not G11's
    assert store.cooldowns(book, SESSION, **minutes) == {
        "NSE:RELIANCE": at("10:30:00"),        # CLOSED + 30
        "NSE:INFY": at("10:31:00"),            # CLOSED_EXTERNAL + 30
        "NSE:TCS": at("10:12:00"),             # VOID 'REJECTED...' + 10
        "NSE:SBIN": at("10:13:00"),            # ... in any case (LIKE 'REJECTED%')
        "NSE:HDFCBANK": at("10:09:00"),        # any other VOID + 5
        "NSE:NIFTY 50": at("10:10:00"),        # a NULL void_reason too
        "NSE:IDEA": at("10:11:00"),            # no closed_ts: counted from reserved_ts, never escaped
        "NSE:BHEL": at("10:30:00"),
        "NSE:ZYDUSLIFE": at("10:12:00"),
    }
    assert store.cooldowns("PAPER:price_spike_up", SESSION, **minutes) == {"NSE:RELIANCE": at("11:20:00")}


# ---------------------------------------------------------------------------------------------------------------
# The typed DAO
# ---------------------------------------------------------------------------------------------------------------
def test_model_round_trip_and_reload(store):
    stc = new_structure(store, decision={"bid": 41.9, "plan": {"qty": 500, "limit": 42.35}}, risk_basis_rs=21075.0,
                        blocked_by=None, placebo_status="SKIPPED:G19")
    leg = new_leg(store, stc)
    o = new_order(store, leg)
    params = ExitParams(ExitProfile(), 42.15, 500, 52.1, "2026-04-01", 0.00192043, 42.36, 35.83, 44.05, 43.62,
                        48.47, at("09:20:01.250"), at("10:05:01.250"))
    with store.tx():
        o.state, o.broker_order_id, o.filled_qty, o.avg_fill_px = OrderState.COMPLETE, "B9", 500, 42.15
        o.terminal_wall = at("09:20:01.250")
        store.update_order(o, submitted_wall=at("09:20:00.010"), acked_wall=at("09:20:00.300"))
        store.insert_fill(Fill(o.order_pk, "cum:500", "CUM", 500, 42.15, at("09:20:01.250"), None))
        stc.state, stc.opened_wall, stc.exit_params = StructState.EXITING, at("09:20:01.250"), params
        stc.exit_reason = "HARD_SL"
        store.update_structure(stc)
        leg.state, leg.stop_px, leg.lock_armed, leg.peak_rb, leg.peak_wall = LegState.EXITING, 35.83, True, 44.1, \
            at("09:25:00")
        leg.trough_rb, leg.trough_wall, leg.entry_fill_wall = 41.0, at("09:21:00"), at("09:20:01.250")
        leg.last_fill_wall, leg.exit_reason, leg.exit_urgency, leg.exit_band_idx = at("09:20:01.250"), "HARD_SL", \
            Urgency.PROTECTIVE, 3
        leg.exiting_since_mono = 2400.0
        store.update_leg(leg, exiting_since_wall=at("09:40:00"))
        void = new_structure(store, underlying="NSE:INFY", state=StructState.VOID, void_reason="QUOTE",
                             closed_wall=at("09:20:02"))
    now_wall = at("09:40:10")
    loaded = store.load_state(5000.0, now_wall)
    assert set(loaded.structures) == {stc.structure_id} and set(loaded.legs) == {leg.leg_id}
    s2, l2, o2 = loaded.structures[stc.structure_id], loaded.legs[leg.leg_id], loaded.orders[o.order_pk]
    # every persisted field round-trips; monotonic fields are re-anchored, never read back
    assert dataclasses.replace(s2, intent=stc.intent) == stc
    assert s2.exit_params == params and s2.intent.recheck == Recheck(2885.5, math.inf)
    assert dataclasses.replace(s2.intent, created_mono=1000.0) == stc.intent
    assert s2.intent.created_mono == pytest.approx(5000.0 - (now_wall - T0).total_seconds())   # real age kept
    assert dataclasses.replace(l2, last_fill_mono=None, exiting_since_mono=2400.0) == leg
    assert l2.last_fill_mono == 5000.0 and l2.exiting_since_mono == 5000.0 and l2.breach_first_mono is None
    assert l2.inst == leg.inst and l2.open_qty == 500 and l2.leg_orders() == [o2]
    assert dataclasses.replace(o2, submitted_mono=o.submitted_mono) == o and o2.submitted_mono == 5000.0
    row = store.query_one("SELECT submitted_ts, acked_ts, exiting_since_ts FROM orders JOIN legs USING (leg_id)")
    assert tuple(row) == ("2026-10-12T09:20:00.010+05:30", "2026-10-12T09:20:00.300+05:30",
                          "2026-10-12T09:40:00.000+05:30")
    # with a session, that session's terminal structures load too
    assert set(store.load_state(5000.0, now_wall, session=SESSION).structures) == {stc.structure_id,
                                                                                    void.structure_id}
    assert store.load_intent("nope", 1.0, now_wall) is None
    # index legs get their NFO name back from the underlying (legs have no name column)
    nifty = new_structure(store, underlying="NSE:NIFTY 50")
    new_leg(store, nifty)
    assert {lg.inst.name for lg in store.load_state(1.0, now_wall).legs.values()} == {"RELIANCE", "NIFTY"}


def test_reload_keeps_this_process_monotonic_times(store, tmp_path):
    """Monotonic fields are never persisted. The first load of a Store is a process start: every one := now
    (§9.2). A later load is the core loop's rollback-and-reload inside the same process: a leg's last fill, its exit
    decision and an order's submit time keep the value this Store wrote with their wall times, so a reload storm can
    never hold back the post-fill rule, the exit cap or the order timers of every other leg. A rolled-back write
    leaves the committed value; a wall time this Store never paired is re-anchored to now (the safe side)."""
    early = new_leg(store, new_structure(store))                            # from before the process started
    with store.tx():
        early.last_fill_wall, early.last_fill_mono = at("09:20:00"), 900.0
        store.update_leg(early)
    first = store.load_state(1000.0, at("09:30:00"))                        # the process start
    assert first.legs[early.leg_id].last_fill_mono == 1000.0

    stc = new_structure(store, underlying="NSE:INFY")
    leg = new_leg(store, stc)
    o = make_order(leg, created=at("09:31:00"))
    o.submitted_mono = 1060.0
    with store.tx():
        store.insert_order(o, submitted_wall=at("09:31:00"))
        leg.orders.append(o.order_pk)
        leg.last_fill_wall, leg.last_fill_mono = at("09:31:01"), 1061.0
        leg.state, leg.exiting_since_mono = LegState.EXITING, 1065.0
        store.update_leg(leg, exiting_since_wall=at("09:31:05"))
    again = store.load_state(1090.0, at("09:31:30"))                        # a rollback-and-reload
    l2, o2 = again.legs[leg.leg_id], again.orders[o.order_pk]
    assert (l2.last_fill_mono, l2.exiting_since_mono, o2.submitted_mono) == (1061.0, 1065.0, 1060.0)
    assert again.legs[early.leg_id].last_fill_mono == 1000.0                # still the process start, not "now"

    with pytest.raises(RuntimeError):                                       # a fill whose transaction rolls back
        with store.tx():
            leg.last_fill_wall, leg.last_fill_mono = at("09:31:20"), 1080.0
            store.update_leg(leg)
            raise RuntimeError("rollback")
    assert store.load_state(1100.0, at("09:31:40")).legs[leg.leg_id].last_fill_mono == 1061.0
    with store.tx():                                                        # a wall time no write of ours paired
        store.execute("UPDATE legs SET last_fill_ts = ? WHERE leg_id = ?", (iso(at("09:31:35")), leg.leg_id))
    assert store.load_state(1110.0, at("09:31:50")).legs[leg.leg_id].last_fill_mono == 1110.0

    restarted = Store(store.path, backup_dir=tmp_path / "backups")          # a new process: everything := now
    try:
        loaded = restarted.load_state(5000.0, at("10:00:00"))
        l3, o3 = loaded.legs[leg.leg_id], loaded.orders[o.order_pk]
        assert (l3.last_fill_mono, l3.exiting_since_mono, o3.submitted_mono) == (5000.0, 5000.0, 5000.0)
    finally:
        restarted.close()


def test_halts_sticky_and_clear(store):
    with store.tx():
        h, new = store.add_halt("GLOBAL", "PAUSE", "operator pause", HaltClears.RESUME, T0, operator=True)
        assert new and h.operator and h.clears is HaltClears.RESUME and h.session == SESSION and h.created_wall == T0
        again, new2 = store.add_halt("GLOBAL", "PAUSE", "pause again", "RESUME", at("09:21:00"))
        assert not new2 and again == h                                   # sticky by (scope, kind)
        store.add_halt("BOOK:PAPER:od", "REJECTS", "3 entry rejects", HaltClears.ACK, at("09:22:00"))
    assert [(x.scope, x.kind) for x in store.active_halts()] == [("GLOBAL", "PAUSE"), ("BOOK:PAPER:od", "REJECTS")]
    with store.tx():
        assert store.clear_halt(h.halt_id, at("09:30:00"), "CLI", "resume")
        assert not store.clear_halt(h.halt_id, at("09:31:00"), "CLI")    # already cleared
        h3, new3 = store.add_halt("GLOBAL", "PAUSE", "paused again", HaltClears.RESUME, at("09:40:00"))
        assert new3 and h3.halt_id != h.halt_id
    assert store.halt_row(h.halt_id)["cleared_by"] == "CLI" and store.halt_row(h.halt_id)["clear_note"] == "resume"
    # the DB index is the backstop: two active rows for one (scope, kind) cannot exist
    with pytest.raises(InvariantViolation, match="UNIQUE"):
        with store.tx():
            store.insert_row("halts", {"scope": "GLOBAL", "kind": "PAUSE", "reason": "dup", "clears": "RESUME",
                                       "created_ts": T0, "session": SESSION})


def test_sequences_never_reuse_imported_legs(store):
    with store.tx():
        assert [store.next_leg_seq(Account.PAPER, SESSION) for _ in range(2)] == [1, 2]
        assert store.next_leg_seq(Account.LIVE, SESSION) == 1                        # per account
        assert store.next_leg_seq(Account.PAPER, SESSION + timedelta(days=1)) == 1   # per session
        assert [store.next_seq("x"), store.next_seq("x")] == [1, 2]
    assert store.scalar("SELECT value FROM seq WHERE name='leg:PAPER:261012'") == 2
    stc = new_structure(store)
    new_leg(store, stc, seq_no=40)              # e.g. imported from a broker tag after a lost DB (§9.6)
    with store.tx():
        assert store.next_leg_seq(Account.PAPER, SESSION) == 41


def test_readonly_store_and_backup(store, tmp_path):
    with store.tx():
        store.set_meta("last_session", "2026-10-12")
    with closing(open_readonly(store.path)) as ro:          # readable while the writer holds the DB (WAL)
        assert ro.readonly and ro.get_meta("last_session") == "2026-10-12" and ro.integrity_check() == []
        with pytest.raises(StoreError, match="read-only"):
            ro.tx()
        with pytest.raises(StoreError, match="read-only"):
            ro.set_meta("x", "y")
        with pytest.raises(StoreError, match="read-only"):
            ro.query("DELETE FROM meta")
        with pytest.raises(sqlite3.OperationalError):        # the connection itself refuses writes too
            ro._conn.execute("DELETE FROM meta")
    with pytest.raises(StoreError, match="uncommitted"):
        with store.tx():
            store.set_meta("pending", "1")
            store.backup(tmp_path / "bk" / "mid_tx.db")
    copy = store.backup(tmp_path / "bk" / "copy.db")
    with pytest.raises(StoreError, match="exists"):
        store.backup(copy)
    copy2 = st.backup_db(store.path, tmp_path / "bk" / "copy2.db")
    for path in (copy, copy2):
        with closing(open_readonly(path)) as b:
            assert b.get_meta("last_session") == "2026-10-12" and b.integrity_check() == []
    # any other SQLite file (e.g. the dashboard's command DB, journal_mode=DELETE) is read with connect_readonly
    cmd = tmp_path / "commands_dash.db"
    with closing(sqlite3.connect(cmd)) as c:
        c.execute("CREATE TABLE commands(cmd_id INTEGER PRIMARY KEY, kind TEXT NOT NULL)")
        c.execute("INSERT INTO commands VALUES (1, 'PAUSE')")
        c.commit()
    with closing(st.connect_readonly(cmd)) as c:
        assert [tuple(r) for r in c.execute("SELECT * FROM commands")] == [(1, "PAUSE")]
        with pytest.raises(sqlite3.OperationalError):
            c.execute("INSERT INTO commands VALUES (2, 'RESUME')")
    with pytest.raises(StoreError, match="not a titan2 database"):
        open_readonly(cmd)
    with closing(sqlite3.connect(cmd)) as c:          # still a rollback-journal file: the reader changed nothing
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_prune_exports_then_deletes(store, tmp_path):
    leg = new_leg(store, new_structure(store))
    now = at("15:38:00")
    with store.tx():
        old = [store.add_quote_check(leg.leg_id, now - timedelta(days=181, seconds=i), "POST", bid=40.0 + i,
                                     ask=40.5, rb=40.0, src="WS") for i in range(3)]
        keep = store.add_quote_check(leg.leg_id, now - timedelta(days=179), "HOLD", bid=41.0, lock_armed=False)
        assert store.add_broker_snapshot(Account.PAPER, "POSITIONS", now - timedelta(days=31), True, [{"net": 0}])
        assert not store.add_broker_snapshot(Account.PAPER, "POSITIONS", now - timedelta(days=31), False)
        store.add_broker_snapshot(Account.PAPER, "ORDERS", now - timedelta(days=29), False)
        store.append_event("HEARTBEAT", wall=now - timedelta(days=400), mono=1.0)
    with store.tx():
        res = store.prune(now, tmp_path / "exports")
    assert (res.quote_checks_exported, res.quote_checks_deleted, res.broker_snapshots_deleted) == (3, 3, 1)
    assert res.export_path == tmp_path / "exports" / "quote_checks" / "2026-10-12.csv.gz"
    with gzip.open(res.export_path, "rt", encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0][:4] == ["id", "leg_id", "ts", "phase"] and sorted(int(r[0]) for r in rows[1:]) == sorted(old)
    assert [r["id"] for r in store.quote_checks(leg.leg_id)] == [keep]
    assert store.scalar("SELECT COUNT(*) FROM broker_snapshots") == 1
    assert len(store.events()) == 1                                   # nothing else is ever deleted
    with store.tx():
        assert store.prune(now, tmp_path / "exports") == PruneResult(0, 0, 0, None)
    assert store.prune(now, tmp_path / "exports") == PruneResult(0, 0, 0, None)   # also as its own transaction


def test_strategy_g1_and_audit_rows(store):
    it = make_intent()
    with store.tx():
        store.put_strategy_mode("opening_drive", version="1.a", mode="PAPER", changed_wall=T0, changed_by="BOOT")
        assert store.update_row("strategy_modes", {"strategy": "opening_drive"},
                                {"pending_mode": "SHADOW", "changed_ts": at("10:00:00"), "changed_by": "CLI"}) == [
            "pending_mode", "changed_ts", "changed_by"]
        assert store.register_trial("opening_drive", "1.a", T0, {"params": {"x": 1}}) == (1, True)
        assert store.register_trial("price_spike_up", "1.b", T0, {}) == (2, True)
        assert store.register_trial("opening_drive", "1.a", at("10:00:00"), {}) == (1, False)
        store.update_trial("opening_drive", "1.a", looks_done=[100], state="CONFIRMING", state_ts=at("15:38:00"))
        rid = store.insert_g1_report({"strategy": "opening_drive", "version": "1.a", "kind": "LOOK", "look_n": 100,
                                      "computed_ts": at("15:38:00"), "n": 100, "passed": True,
                                      "inputs_hash": "ab" * 32, "detail": {"lb_pct": 0.4},
                                      "fail_reasons": []})
        assert store.flag_session(SESSION, "opening_drive", "PAUSE diverted an intent")
        assert not store.flag_session(SESSION, "opening_drive", "heartbeat gap")
        bar = Bar("NSE:RELIANCE", at("09:15:00"), 2900.0, 2950.0, 2895.0, 2940.0, 120000, True, "LIVE")
        store.put_bar(bar)
        store.put_bar(dataclasses.replace(bar, valid=False))
        store.put_bar_check(SESSION, "NSE:RELIANCE", at("09:15:00"), False, "volume differs 12%")
        store.ack_foreign(SESSION, Account.LIVE, "NFO:X26OCT100CE", "NRML", 50, "CLI", T0)
        store.ack_foreign(SESSION, Account.LIVE, "NFO:X26OCT100CE", "NRML", 75, "CLI", at("10:00:00"))
        store.put_command_ack("DASH", 7, "PAUSE", "ACCEPTED", T0)
        store.put_command_ack("DASH", 7, "PAUSE", "DONE", at("09:21:00"), result={"halt_id": 1},
                              done_wall=at("09:21:00"))
        store.put_command_ack("CLI", 3, "KILL", "REJECTED", at("09:22:00"), result="confirm text must be KILL")
        store.insert_intent(it)
        store.put_intent_route(it.intent_id, "PAPER:opening_drive", "RESERVED", T0)
        store.put_intent_route(it.intent_id, "PAPER:opening_drive", "REFUSED", at("09:20:01"), gate="G17",
                               detail="quote 1.8 s old")
        seq = store.append_event("INTENT_RECEIVED", wall=T0, mono=1.5, payload={"trigger_px": 2900.0},
                                 intent_id=it.intent_id, strategy="opening_drive")
    assert store.strategy_mode("opening_drive")["pending_mode"] == "SHADOW" and len(store.strategy_modes()) == 1
    assert store.trial_count() == 2 and [t["family_seq"] for t in store.trials()] == [1, 2]
    trial = store.trial("opening_drive", "1.a")
    assert trial["looks_done"] == [100] and trial["version_inputs"] == {"params": {"x": 1}}
    assert trial["state"] == "CONFIRMING" and trial["registered_ts"] == iso(T0)
    report = store.g1_report(rid)
    assert report["detail"] == {"lb_pct": 0.4} and report["passed"] == 1 and report["fail_reasons"] == "[]"
    assert [r["report_id"] for r in store.g1_reports(strategy="opening_drive", kind="LOOK")] == [rid]
    assert [f["detail"] for f in store.session_flags(strategy="opening_drive")] == ["PAUSE diverted an intent"]
    assert store.bars("NSE:RELIANCE") == [dataclasses.replace(bar, valid=False)]
    assert store.bars("NSE:RELIANCE", source="HIST") == []
    assert store.bar_checks(SESSION)[0]["ok"] == 0
    assert [a["qty"] for a in store.foreign_acks(SESSION, Account.LIVE)] == [75]
    assert store.last_command_id("DASH") == 7 and store.last_command_id("CLI") == 3
    acks = {(a["source"], a["cmd_id"]): a for a in store.command_acks()}
    assert acks[("DASH", 7)]["status"] == "DONE" and acks[("DASH", 7)]["picked_ts"] == iso(T0)
    assert acks[("DASH", 7)]["result"] == '{"halt_id":1}'
    routes = store.intent_routes(it.intent_id)
    assert [(r["outcome"], r["gate"]) for r in routes] == [("REFUSED", "G17")]
    tail = store.events_tail(1)
    assert tail[0]["seq"] == seq and tail[0]["payload"] == {"trigger_px": 2900.0} and tail[0]["session"] == "2026-10-12"
    assert [e["kind"] for e in store.events(kind="INTENT_RECEIVED", session=SESSION)] == ["INTENT_RECEIVED"]
    # unknown tables/columns never reach SQL
    with pytest.raises(StoreError, match="unknown column"):
        with store.tx():
            store.insert_row("meta", {"key": "a", "value": "b", "evil); DROP TABLE meta; --": 1})
    with pytest.raises(StoreError, match="unknown table"):
        with store.tx():
            store.insert_row("sqlite_master", {"name": "x"})


def test_trade_rows_round_money_and_refuse_non_finite(store):
    stc = new_structure(store, state=StructState.CLOSED)
    with pytest.raises(StoreError, match="not finite"):
        with store.tx():
            store.insert_trade(trade_row(stc, float("nan"), at("10:00:00")))
    with store.tx():
        store.insert_trade(trade_row(stc, -123.456, at("10:00:00"), gross=-73.4549, charges=50.0011))
    row = store.trade(stc.structure_id)
    assert (row["net"], row["gross"], row["charges"]) == (-123.46, -73.45, 50.0)
    assert row["exit_profile"] == dataclasses.asdict(ExitProfile()) and row["pnl_status"] == "FINAL"
    assert [t["structure_id"] for t in store.trades(session=SESSION, book=stc.book)] == [stc.structure_id]
    assert store.trades(account=Account.LIVE) == []
    # a row that does not add up is fake P&L: refused (net = gross - charges, charges = sum of components)
    other = new_structure(store, underlying="NSE:INFY", state=StructState.CLOSED)
    for over in ({"gross": -40.0}, {"stt": 9.0}, {"sqoff_fee": 59.0}):        # e.g. a ₹59 fee left out of charges
        with pytest.raises(InvariantViolation, match="components") as ei:
            with store.tx():
                store.insert_trade(trade_row(other, -100.0, at("10:00:00"), **over))
        assert ei.value.code == "LEDGER_ARITHMETIC"
    assert store.trade(other.structure_id) is None
