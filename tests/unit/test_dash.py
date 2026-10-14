"""U19: titan2.dash (spec §12; DETAIL §12.1-12.3, §4.4 commands_dash.db, §4.7 pub/)."""

from __future__ import annotations

import ast
import base64
import builtins
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path

import pytest

from titan2.clock import FakeClock
from titan2.dash import app as dash
from titan2.dash.app import (DASH_KINDS, OFFLINE_BANNER, DashConfigError, commands_authorizer, create_app,
                             hash_password, open_commands_db, verify_password)

AT_DIR = Path(__file__).resolve().parents[2]                 # agentic_trader/
DASH_DIR = AT_DIR / "titan2" / "dash"
USER, PASSWORD, SECRET = "operator", "correct horse battery staple", "s3cret-" * 6
BASE = "http://127.0.0.1:8050"
NOW = "2026-10-12T10:00:00+05:30"
FORBIDDEN = ("kiteconnect", "titan2.broker", "titan2.core", "titan2.store")


@pytest.fixture(scope="module")
def pass_hash() -> str:
    return hash_password(PASSWORD, n=2 ** 14)               # the minimum cost the app accepts: keeps tests fast


@pytest.fixture
def env(pass_hash):
    return {"DASH_USER": USER, "DASH_PASS_SCRYPT": pass_hash, "DASH_SECRET": SECRET}


@pytest.fixture
def clock():
    return FakeClock(NOW)


@pytest.fixture
def var(tmp_path):
    (tmp_path / "pub" / "trade").mkdir(parents=True)
    (tmp_path / "dash").mkdir()
    return tmp_path


@pytest.fixture
def app(env, clock, var):
    application = create_app(env, clock=clock, var_dir=var)
    application.testing = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def basic(user=USER, password=PASSWORD):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def get(client, path, base=BASE, auth=True, headers=None):
    h = dict(basic() if auth else {})
    h.update(headers or {})
    return client.get(path, base_url=base, headers=h)


def csrf(day="2026-10-12", user=USER):
    return hmac.new(SECRET.encode(), f"{day}|{user}".encode(), hashlib.sha256).hexdigest()


def post_cmd(client, body, token=None, base=BASE, auth=True, headers=None):
    h = dict(basic() if auth else {})
    if token is not False:
        h["X-Titan-CSRF"] = csrf() if token is None else token
    h.update(headers or {})
    return client.post("/api/command", base_url=base, headers=h, json=body)


def nonce(i):
    return f"nonce-{i:04d}-0123456789"


def db_rows(var):
    """Test-only inspection with a plain connection (the app itself only uses the authorized one)."""
    con = sqlite3.connect(var / "dash" / "commands_dash.db")
    try:
        return con.execute("SELECT cmd_id, created_ts, requested_by, kind, args, confirm_text, nonce "
                           "FROM commands ORDER BY cmd_id").fetchall()
    finally:
        con.close()


def write_pub(var, rel, obj):
    path = var / "pub" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def status_doc(ts):
    """A status.json shaped like DETAIL §4.7."""
    return {
        "ts": ts, "session": "2026-10-12", "phase": "TRADING", "heartbeat_age_s": 0.3, "allow_live": False,
        "entry_halts": [{"halt_id": 3, "scope": "GLOBAL", "kind": "PAUSE", "reason": "operator", "since": ts,
                         "clears": "RESUME"}],
        "feed": {"connected": True, "last_tick_age_s": 0.4, "reconnects": 0, "warm": True, "degraded": False},
        "accounts": {"PAPER": {"recon": "CLEAN", "recon_age_s": 4.1, "broker_positions": [], "available_rs": None}},
        "books": {"PAPER:opening_drive": {"policy": "FULL", "realized_net": 0.0, "mtm_net": None, "entries": 1,
                                          "max_entries": 2, "open": 1, "daily_stop_room": 9879.5,
                                          "worst_case_room": 21200.0, "cooldowns": {"NSE:RELIANCE": "10:05:00"}}},
        "positions": [{"structure_id": 17, "book": "PAPER:opening_drive", "symbol": "NFO:RELIANCE26OCT2900CE",
                       "qty": 500, "entry_avg": 42.15, "bid": 43.0, "ask": 43.2, "age_ms": 300, "src": "WS",
                       "rb": 42.98, "mtm_net": 331.2, "stop": 35.83, "lock_armed": False, "tp": 48.47,
                       "time_stop_in_s": 2340, "mfe_pct": 2.4, "mae_pct": -1.1, "state": "OPEN",
                       "exit_reason": None, "ltp_info": 43.1}],
        "strategies": [{"name": "opening_drive", "version": "1.3f2a9c01de", "mode": "PAPER", "pending_mode": None,
                        "trial": {"state": "COLLECTING", "n": 37, "sessions": 19, "exp_pct": -0.8, "lb_pct": None,
                                  "next_look": 100}}],
        "pending_commands": 0, "unacked_critical": 0,
    }


# ---------------------------------------------------------------------------------------------------------------
# Acceptance tests
# ---------------------------------------------------------------------------------------------------------------
def test_auth_host_csrf_nonce(client, clock, var):
    # Host must be 127.0.0.1:8050 or localhost:8050 (DNS rebinding), checked before credentials
    for base in ("http://evil.example:8050", "http://localhost", "http://127.0.0.1", "http://127.0.0.1:8051",
                 "http://[::1]:8050", "http://titan-bot:8050"):
        assert get(client, "/api/status", base=base).status_code == 403, base
        assert get(client, "/api/status", base=base, auth=False).status_code == 403, base
    assert get(client, "/api/status", base="http://localhost:8050").status_code == 200

    # HTTP Basic against the scrypt hash, on every route including the page itself
    for path in ("/", "/api/status", "/api/csrf", "/api/trades", "/api/events", "/api/g1", "/api/acks"):
        r = get(client, path, auth=False)
        assert r.status_code == 401 and r.headers["WWW-Authenticate"].startswith("Basic "), path
    for bad in (basic(password="wrong"), basic(user="admin"), basic(user=USER.upper()),
                {"Authorization": "Basic !!!not-base64"}, {"Authorization": "Bearer " + PASSWORD},
                {"Authorization": "Basic " + base64.b64encode(PASSWORD.encode()).decode()}):
        assert client.get("/api/status", base_url=BASE, headers=bad).status_code == 401
    assert get(client, "/").status_code == 200

    # X-Titan-CSRF = HMAC(DASH_SECRET, date|user), served by GET /api/csrf
    token = get(client, "/api/csrf").get_json()
    assert token["csrf"] == csrf() and token["date"] == "2026-10-12" and token["header"] == "X-Titan-CSRF"
    body = {"kind": "PAUSE", "nonce": nonce(1)}
    assert post_cmd(client, body, token=False).status_code == 403                      # header missing
    assert post_cmd(client, body, token="").status_code == 403
    assert post_cmd(client, body, token=csrf(user="someone")).status_code == 403
    assert post_cmd(client, body, token=csrf(day="2026-10-11")).status_code == 403     # yesterday's token
    assert post_cmd(client, body, token=csrf() + "0").status_code == 403
    assert post_cmd(client, body, headers={"Origin": "http://evil.example"}).status_code == 403
    assert post_cmd(client, body, auth=False).status_code == 401                       # auth comes first
    assert post_cmd(client, body, base="http://evil.example:8050").status_code == 403
    assert db_rows(var) == []                                                          # nothing got in
    r = post_cmd(client, body, headers={"Origin": "http://localhost:8050"})
    assert r.status_code == 201
    first = r.get_json()["cmd_id"]

    # a unique, well-formed nonce
    assert post_cmd(client, {"kind": "PAUSE"}).status_code == 400
    for bad in ("short", "x" * 65, "has space in it here", "semi;colon;0123456789", 1234567890123456789):
        assert post_cmd(client, {"kind": "PAUSE", "nonce": bad}).status_code == 400, bad
    replay = post_cmd(client, body)
    assert replay.status_code == 409 and "NOT recorded" in replay.get_json()["error"]
    r = post_cmd(client, {"kind": "PAUSE", "nonce": nonce(2)})
    assert r.status_code == 201 and r.get_json()["cmd_id"] > first
    assert [row[6] for row in db_rows(var)] == [nonce(1), nonce(2)]

    # the date rolls over: the old token stops working, the new one is served
    clock.advance(24 * 3600)
    assert post_cmd(client, {"kind": "PAUSE", "nonce": nonce(3)}).status_code == 403
    assert get(client, "/api/csrf").get_json()["csrf"] == csrf(day="2026-10-13")
    assert post_cmd(client, {"kind": "PAUSE", "nonce": nonce(3)}, token=csrf(day="2026-10-13")).status_code == 201


def test_three_kinds_flatten_confirm(client, var):
    assert DASH_KINDS == ("PAUSE", "RESUME", "FLATTEN_ALL")
    accepted = [{"kind": "PAUSE"}, {"kind": "RESUME", "confirm_text": None},
                {"kind": "FLATTEN_ALL", "confirm_text": "FLATTEN"}]
    for i, body in enumerate(accepted):
        r = post_cmd(client, {**body, "nonce": nonce(i)})
        assert r.status_code == 201, r.get_json()
        assert r.get_json()["kind"] == body["kind"]

    refused = [
        {"kind": "FLATTEN_ALL"}, {"kind": "FLATTEN_ALL", "confirm_text": None},
        {"kind": "FLATTEN_ALL", "confirm_text": "flatten"}, {"kind": "FLATTEN_ALL", "confirm_text": "FLATTEN "},
        {"kind": "FLATTEN_ALL", "confirm_text": " FLATTEN"}, {"kind": "FLATTEN_ALL", "confirm_text": "YES"},
        {"kind": "FLATTEN_ALL", "confirm_text": ["FLATTEN"]},
        {"kind": "KILL", "confirm_text": "KILL"}, {"kind": "SET_MODE"}, {"kind": "ACK_HALT"}, {"kind": "ACK_ALERT"},
        {"kind": "ACK_FOREIGN"}, {"kind": "RUN_RECON"}, {"kind": "pause"}, {"kind": "FLATTEN"}, {"kind": None},
        {"kind": ["PAUSE"]}, {}, {"kind": "PAUSE", "args": {"x": 1}}, {"kind": "PAUSE", "requested_by": "bot"},
        {"kind": "PAUSE", "confirm_text": "x" * 33},
    ]
    for j, body in enumerate(refused):
        r = post_cmd(client, {**body, "nonce": nonce(100 + j)})
        assert r.status_code == 400, body
    for raw, ctype in (("kind=PAUSE&nonce=" + nonce(200), "application/x-www-form-urlencoded"),
                       ("[\"PAUSE\"]", "application/json"), ("{not json", "application/json")):
        r = client.post("/api/command", base_url=BASE, data=raw, content_type=ctype,
                        headers={**basic(), "X-Titan-CSRF": csrf()})
        assert r.status_code == 400, raw
    assert get(client, "/api/command").status_code == 405                              # no GET way in

    rows = db_rows(var)
    assert [r[3] for r in rows] == ["PAUSE", "RESUME", "FLATTEN_ALL"]
    assert all(r[2] == USER and r[4] == "{}" for r in rows)
    assert rows[0][1] == "2026-10-12T10:00:00.000+05:30"
    assert [r[5] for r in rows] == [None, None, "FLATTEN"]

    # the file's own CHECK refuses every other kind too, even through the app's authorized connection
    con = open_commands_db(var / "dash" / "commands_dash.db")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            con.execute(dash.INSERT_SQL, ("t", USER, "KILL", "KILL", nonce(300)))
    finally:
        con.close()
    assert len(db_rows(var)) == 3


def test_authorizer_denies_writes(app, var):
    path = var / "dash" / "commands_dash.db"
    con = open_commands_db(path)
    try:
        assert con.execute(dash.INSERT_SQL, ("2026-10-12T10:00:00.000+05:30", USER, "FLATTEN_ALL", "FLATTEN",
                                             nonce(1))).lastrowid == 1
        assert con.execute("SELECT cmd_id, kind FROM commands").fetchall() == [(1, "FLATTEN_ALL")]
        before = db_rows(var)
        denied = [
            "UPDATE commands SET kind='RESUME'", "DELETE FROM commands", "DROP TABLE commands",
            "DROP TRIGGER c_no_del", "CREATE TABLE x(a)", "CREATE TEMP TABLE x(a)", "CREATE VIEW v AS SELECT 1",
            "CREATE TRIGGER t AFTER INSERT ON commands BEGIN SELECT 1; END", "CREATE INDEX i ON commands(kind)",
            "ALTER TABLE commands ADD COLUMN y", "ATTACH DATABASE ':memory:' AS other", "PRAGMA journal_mode=WAL",
            "PRAGMA writable_schema=ON", "PRAGMA recursive_triggers=OFF", "PRAGMA user_version=7", "BEGIN",
            "BEGIN IMMEDIATE", "SAVEPOINT s", "VACUUM", "ANALYZE", "REINDEX", "SELECT * FROM sqlite_master",
            "SELECT count(*) FROM commands", "SELECT load_extension('x')",
            "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) SELECT n FROM r",
            "INSERT INTO commands(created_ts, requested_by, kind, args, confirm_text, nonce) "
            "VALUES ('t', 'u', 'PAUSE', '{}', NULL, 'n-0001') ON CONFLICT(nonce) DO UPDATE SET kind='RESUME'",
        ]
        for sql in denied:
            with pytest.raises(sqlite3.DatabaseError, match="not authorized|authorization denied|prohibited"):
                con.execute(sql)
        # REPLACE resolves a conflict by deleting the old row; recursive_triggers makes that fire c_no_del
        for sql in ("INSERT OR REPLACE INTO commands(created_ts, requested_by, kind, args, confirm_text, nonce) "
                    f"VALUES ('t', 'u', 'RESUME', '{{}}', NULL, '{nonce(1)}')",
                    "REPLACE INTO commands(cmd_id, created_ts, requested_by, kind, args, confirm_text, nonce) "
                    "VALUES (1, 't', 'u', 'RESUME', '{}', NULL, 'fresh-nonce-0123456')"):
            with pytest.raises(sqlite3.IntegrityError, match="insert-only"):
                con.execute(sql)
    finally:
        con.close()
    assert db_rows(var) == before                                     # the FLATTEN_ALL row is untouched

    ok, deny = sqlite3.SQLITE_OK, sqlite3.SQLITE_DENY
    assert commands_authorizer(sqlite3.SQLITE_INSERT, "commands", None, "main", None) == ok
    assert commands_authorizer(sqlite3.SQLITE_SELECT, None, None, None, None) == ok
    assert commands_authorizer(sqlite3.SQLITE_READ, "commands", "kind", "main", None) == ok
    assert commands_authorizer(sqlite3.SQLITE_INSERT, "other", None, "main", None) == deny
    assert commands_authorizer(sqlite3.SQLITE_INSERT, "commands", None, "temp", None) == deny
    assert commands_authorizer(sqlite3.SQLITE_INSERT, "commands", None, "main", "some_trigger") == deny
    assert commands_authorizer(sqlite3.SQLITE_READ, "sqlite_master", "sql", "main", None) == deny
    for action in (sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE, sqlite3.SQLITE_DROP_TABLE,
                   sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_PRAGMA, sqlite3.SQLITE_ATTACH,
                   sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_ALTER_TABLE):
        assert commands_authorizer(action, "commands", None, "main", None) == deny, action


def test_pub_only_offline_banner(client, clock, var, monkeypatch):
    status = status_doc("2026-10-12T09:59:57.000+05:30")
    status_path = write_pub(var, "status.json", status)
    trades_today = [{"structure_id": 17, "book": "PAPER:opening_drive", "net": -312.55, "charges": 61.2,
                     "exit_reason": "HARD_SL", "ltp_exit": 40.1}]
    trades_90d = trades_today + [{"structure_id": 3, "net": 120.0}]
    trade17 = {"structure_id": 17, "orders": [{"order_pk": "T2EP26101200101", "side": "BUY"}],
               "fills": [{"qty": 500, "px": 42.15}], "path": [{"ts": "09:21", "bid": 42.0, "ask": 42.3}]}
    events = [{"seq": 1, "kind": "BOOT", "severity": "INFO", "payload": {}}]
    g1 = {"strategies": [{"strategy": "opening_drive", "n": 37, "lb_pct": None}]}
    acks = [{"source": "DASH", "cmd_id": 1, "kind": "PAUSE", "status": "DONE"}]
    for rel, obj in (("trades_today.json", trades_today), ("trades_90d.json", trades_90d),
                     ("trade/17.json", trade17), ("events_tail.json", events), ("g1.json", g1), ("acks.json", acks)):
        write_pub(var, rel, obj)
    for rel in ("db/titan2.db", "session/kite_session.json", "config/settings.json", "dash/notes.json"):
        (var / rel).parent.mkdir(exist_ok=True)
        (var / rel).write_text('{"secret": true}', encoding="utf-8")       # decoys the dashboard must not read

    opened = []
    real_open = builtins.open

    def spy(file, *args, **kwargs):
        opened.append(Path(file).resolve())
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)

    # fresh status: online, and passed through untouched (the dashboard computes nothing; null MTM stays null)
    r = get(client, "/api/status").get_json()
    assert (r["offline"], r["banner"], r["error"], r["age_s"]) == (False, None, None, 3.0)
    assert r["data"] == status
    assert r["data"]["books"]["PAPER:opening_drive"]["mtm_net"] is None
    # older than 10 s: offline with the banner
    clock.advance(7.0)
    assert get(client, "/api/status").get_json()["offline"] is False                  # exactly 10 s: still fresh
    clock.advance(1.0)
    r = get(client, "/api/status").get_json()
    assert r["offline"] is True and r["banner"] == OFFLINE_BANNER == "bot offline — use the Kite app"
    assert r["age_s"] == 11.0 and "11.0 s old" in r["error"] and r["data"] == status
    # a stamp far in the future is not trusted either
    write_pub(var, "status.json", status_doc("2026-10-12T10:05:00.000+05:30"))
    assert get(client, "/api/status").get_json()["offline"] is True
    # an unusable ts falls back to the file's own mtime
    bad_ts = status_doc("not-a-time")
    write_pub(var, "status.json", bad_ts)
    now_epoch = clock.now_wall().timestamp()
    os.utime(status_path, (now_epoch - 2, now_epoch - 2))
    r = get(client, "/api/status").get_json()
    assert (r["offline"], r["age_s"], r["data"]) == (False, 2.0, bad_ts)
    os.utime(status_path, (now_epoch - 30, now_epoch - 30))
    assert get(client, "/api/status").get_json()["offline"] is True
    # missing, corrupt, non-finite or non-object status: offline, never a guess
    for text in ("{not json", '{"ts": "2026-10-12T10:00:08.000+05:30", "mtm_net": NaN}', "[1, 2]", "null"):
        status_path.write_text(text, encoding="utf-8")
        r = get(client, "/api/status").get_json()
        assert r["offline"] is True and r["banner"] == OFFLINE_BANNER and r["data"] is None, text
    status_path.unlink()
    r = get(client, "/api/status").get_json()
    assert r["offline"] is True and "not published yet" in r["error"]

    # every other route is a pass-through of one pub file
    def data(path):
        resp = get(client, path)
        assert resp.status_code == 200, path
        return resp.get_json()["data"]

    assert data("/api/trades") == trades_today
    assert data("/api/trades?scope=today") == trades_today
    assert data("/api/trades?scope=90d") == trades_90d
    assert data("/api/trade/17") == trade17
    assert data("/api/events") == events
    assert data("/api/g1") == g1
    assert data("/api/acks") == acks
    assert get(client, "/api/trades?scope=all").status_code == 400
    for path in ("/api/trade/18", "/api/trade/abc", "/api/trade/-1", "/api/trade/..%2F..%2Fdb%2Ftitan2",
                 "/api/trade/17.json", "/api/trade/1%2F..%2F17"):
        assert get(client, path).status_code == 404, path

    # everything the dashboard opened while serving all of the above is a pub/*.json file
    pub = (var / "pub").resolve()
    assert opened
    assert all(p.suffix == ".json" and pub in p.parents for p in opened), sorted(set(opened))


def test_no_forbidden_imports(pass_hash):
    # static: no import (absolute or relative) of kiteconnect, broker, core or store; no dynamic imports
    for py in sorted(DASH_DIR.glob("*.py")):
        source = py.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py), feature_version=(3, 10))
        package = "titan2.dash"
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parent = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                    base = f"{parent}.{base}" if base else parent
                names = [base] + [f"{base}.{a.name}" for a in node.names]
            for name in names:
                assert not any(name == f or name.startswith(f + ".") for f in FORBIDDEN), (py.name, name)
        assert "importlib" not in source and "__import__" not in source, py.name
        for word in ("titan2.db", "kite_session", "settings.json", "commands_cli", "titan2.env", "place_order"):
            assert word not in source, (py.name, word)

    # runtime: importing the module and building the app pulls in none of them (nor twisted)
    code = textwrap.dedent(f"""
        import json, os, sys, tempfile
        import titan2.dash.app as dash
        with tempfile.TemporaryDirectory() as var:
            os.mkdir(os.path.join(var, "dash"))
            dash.create_app({{"DASH_USER": "op", "DASH_PASS_SCRYPT": {pass_hash!r}, "DASH_SECRET": "s" * 32}},
                            var_dir=var)
        prefixes = {FORBIDDEN!r} + ("twisted",)
        print(json.dumps(sorted(m for m in sys.modules if any(m == p or m.startswith(p + ".") for p in prefixes))))
    """)
    done = subprocess.run([sys.executable, "-c", code], cwd=AT_DIR, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.strip().splitlines()[-1]) == []

    # the page loads nothing from outside the dashboard and never writes untrusted text as HTML
    page = (DASH_DIR / "templates" / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"""(src|href)\s*=\s*["']?(https?:)?//""", page)
    for word in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert word not in page, word


# ---------------------------------------------------------------------------------------------------------------
# Extra tests
# ---------------------------------------------------------------------------------------------------------------
def test_refuses_to_start_without_credentials(env, var, monkeypatch):
    for key in ("DASH_USER", "DASH_PASS_SCRYPT", "DASH_SECRET"):
        for value in (None, ""):
            broken = dict(env)
            if value is None:
                del broken[key]
            else:
                broken[key] = value
            with pytest.raises(DashConfigError):
                create_app(broken, var_dir=var)
    weak = hash_password(PASSWORD, n=2 ** 14).replace("scrypt:16384:", "scrypt:1024:")
    for stored in ("plaintext-password", "pbkdf2:sha256:600000$abcdefgh$" + "ab" * 32, weak,
                   "scrypt:16384:8:1$salty-salt$" + "zz" * 32, "scrypt:16384:8:1$" + "x" * 16 + "$abc"):
        with pytest.raises(DashConfigError):
            create_app({**env, "DASH_PASS_SCRYPT": stored}, var_dir=var)
    for user in ("op:x", " op", "op\n"):
        with pytest.raises(DashConfigError):
            create_app({**env, "DASH_USER": user}, var_dir=var)
    with pytest.raises(DashConfigError, match="at least 16"):
        create_app({**env, "DASH_SECRET": "short"}, var_dir=var)
    with pytest.raises(DashConfigError, match="ZERODHA_API_KEY"):
        create_app({**env, "ZERODHA_API_KEY": "k"}, var_dir=var)              # the dashboard holds no broker keys
    with pytest.raises(DashConfigError):
        create_app(env, var_dir=var / "no-such-dir")                       # dash/ must exist (bootstrap makes it)

    # gunicorn's titan2.dash.app:app is built lazily: importing never needs secrets, booting does
    for key in ("DASH_USER", "DASH_PASS_SCRYPT", "DASH_SECRET"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(dash, "_app", None)
    with pytest.raises(DashConfigError):
        getattr(dash, "app")
    with pytest.raises(AttributeError):
        getattr(dash, "no_such_name")
    monkeypatch.setenv("DASH_USER", USER)
    monkeypatch.setenv("DASH_PASS_SCRYPT", env["DASH_PASS_SCRYPT"])
    monkeypatch.setenv("DASH_SECRET", SECRET)
    monkeypatch.setenv("TITAN2_VAR", str(var))
    for key in [k for k in os.environ if k.startswith("ZERODHA_")]:
        monkeypatch.delenv(key)
    built = dash.app
    assert built is dash.app and built.config["TITAN2_COMMANDS_DB"] == var / "dash" / "commands_dash.db"


def test_password_hash_werkzeug_compatible(pass_hash):
    assert verify_password(PASSWORD, pass_hash)
    assert not verify_password(PASSWORD + " ", pass_hash)
    assert pass_hash.startswith("scrypt:16384:8:1$") and pass_hash != hash_password(PASSWORD, n=2 ** 14)  # salted
    security = pytest.importorskip("werkzeug.security")
    assert security.check_password_hash(pass_hash, PASSWORD)            # werkzeug accepts our hash ...
    try:
        theirs = security.generate_password_hash(PASSWORD, method="scrypt:16384:8:1")
    except ValueError:
        pytest.skip("this werkzeug has no scrypt")
    assert verify_password(PASSWORD, theirs)                             # ... and we accept werkzeug's


def test_scrypt_runs_once_per_good_credential(client, monkeypatch):
    calls = []
    real = hashlib.scrypt

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(hashlib, "scrypt", counting)
    for _ in range(5):
        assert get(client, "/api/status").status_code == 200
    assert len(calls) == 1                                  # the 2 s polling does not pay scrypt every time
    for _ in range(2):
        assert client.get("/api/status", base_url=BASE, headers=basic(password="nope")).status_code == 401
    assert len(calls) == 3                                  # failures are never cached
    assert client.get("/api/status", base_url=BASE, headers=basic(user="nobody")).status_code == 401
    assert len(calls) == 4                                  # a wrong user name still costs a full scrypt


def test_index_csp_nonce_and_labels(client):
    r = get(client, "/")
    assert r.status_code == 200 and r.mimetype == "text/html"
    csp = r.headers["Content-Security-Policy"]
    page_nonce = re.search(r"script-src 'nonce-([A-Za-z0-9_-]+)'", csp).group(1)
    page = r.get_data(as_text=True)
    assert page.count(f'nonce="{page_nonce}"') == 2 and "__CSP_NONCE__" not in page
    assert "frame-ancestors 'none'" in csp and "connect-src 'self'" in csp
    assert get(client, "/").headers["Content-Security-Policy"] != csp          # a fresh nonce per response
    assert (r.headers["X-Frame-Options"], r.headers["Cache-Control"], r.headers["X-Content-Type-Options"]) == \
        ("DENY", "no-store", "nosniff")
    assert "const POLL_MS = 2000;" in page                                     # polls every 2 s
    assert OFFLINE_BANNER in page and "LTP (info only)" in page and "MTM net @ bid" in page
    assert "'unknown'" in page                                                 # null MTM is shown as unknown
    api = get(client, "/api/status")
    assert api.headers["Cache-Control"] == "no-store" and "default-src 'none'" in api.headers["Content-Security-Policy"]


def test_commands_db_schema_exact_and_never_recreated(app, client, env, var):
    path = var / "dash" / "commands_dash.db"
    con = sqlite3.connect(path)
    try:
        objects = {(t, n): s for t, n, s in con.execute("SELECT type, name, sql FROM sqlite_master")}
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        con.close()
    assert set(objects) == {("table", "commands"), ("index", "sqlite_autoindex_commands_1"),
                            ("trigger", "c_no_upd"), ("trigger", "c_no_del")}
    assert "kind TEXT NOT NULL CHECK (kind IN ('PAUSE','RESUME','FLATTEN_ALL'))" in objects[("table", "commands")]
    assert post_cmd(client, {"kind": "PAUSE", "nonce": nonce(1)}).status_code == 201
    create_app(env, var_dir=var)                                                # a restart keeps the file as is
    assert len(db_rows(var)) == 1

    # an extra object in the file (here a trigger that would run on every insert) stops the dashboard starting
    con = sqlite3.connect(path)
    con.execute("CREATE TRIGGER extra AFTER INSERT ON commands BEGIN SELECT 1; END")
    con.close()
    with pytest.raises(DashConfigError, match="unexpected schema objects"):
        create_app(env, var_dir=var)

    # a vanished file is never silently recreated by a request (cmd_id would restart at 1 behind the bot's back)
    path.unlink()
    r = post_cmd(client, {"kind": "FLATTEN_ALL", "confirm_text": "FLATTEN", "nonce": nonce(2)})
    assert r.status_code == 503 and "NOT recorded" in r.get_json()["error"] and not path.exists()
    sent = get(client, "/api/acks").get_json()
    assert sent["sent"] is None and "unreadable" in sent["sent_error"] and not path.exists()

    # same columns, index and triggers, but without the CHECKs (KILL would get in): refused as differing from §4.4
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE commands(cmd_id INTEGER PRIMARY KEY, created_ts TEXT NOT NULL, requested_by TEXT NOT "
                "NULL, kind TEXT NOT NULL, args TEXT NOT NULL DEFAULT '{}', confirm_text TEXT, nonce TEXT NOT NULL "
                "UNIQUE)")
    con.close()
    with pytest.raises(DashConfigError, match="differs from DETAIL"):
        create_app(env, var_dir=var)


def test_commands_db_leaves_wal_mode(env, var, monkeypatch):
    # the bot reads commands_dash.db as another user, so it must be in rollback (DELETE) mode, never WAL
    path = var / "dash" / "commands_dash.db"
    con = sqlite3.connect(path)
    assert con.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    con.close()
    create_app(env, var_dir=var)
    con = sqlite3.connect(path)
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("SELECT 1 FROM commands").fetchall()
        with pytest.raises(DashConfigError):                 # held open in WAL by someone else: refuse to start
            create_app(env, var_dir=var)
    finally:
        con.close()

    real_connect = sqlite3.connect

    class StuckInWal:                                        # SQLite returns the old mode when it cannot switch
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *args):
            return self._inner.execute("SELECT 'wal'" if sql.startswith("PRAGMA journal_mode") else sql, *args)

        def close(self):
            self._inner.close()

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: StuckInWal(real_connect(*a, **k)))
    with pytest.raises(DashConfigError, match="journal_mode is wal"):
        create_app(env, var_dir=var)


def test_acks_list_sent_commands(client, var):
    for i, body in enumerate(({"kind": "PAUSE"}, {"kind": "FLATTEN_ALL", "confirm_text": "FLATTEN"})):
        assert post_cmd(client, {**body, "nonce": nonce(i)}).status_code == 201
    acks = [{"source": "DASH", "cmd_id": 1, "kind": "PAUSE", "status": "DONE", "result": "paused",
             "picked_ts": "2026-10-12T10:00:00.500+05:30", "done_ts": "2026-10-12T10:00:00.600+05:30"}]
    write_pub(var, "acks.json", acks)
    body = get(client, "/api/acks").get_json()
    assert body["data"] == acks
    assert [(s["cmd_id"], s["kind"], s["requested_by"]) for s in body["sent"]] == \
        [(2, "FLATTEN_ALL", USER), (1, "PAUSE", USER)]                          # newest first


def test_hash_password_helper(monkeypatch, capsys):
    answers = iter(["a-long-enough-pw", "a-long-enough-pw"])
    monkeypatch.setattr(dash.getpass, "getpass", lambda prompt="": next(answers))
    assert dash.main(["hash-password"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("DASH_PASS_SCRYPT='scrypt:32768:8:1$") and line.endswith("'")
    assert verify_password("a-long-enough-pw", line.split("'")[1])
    answers = iter(["short", "short"])
    assert dash.main(["hash-password"]) == 2
    assert dash.main(["new-secret"]) == 0
    assert re.fullmatch(r"DASH_SECRET=[0-9a-f]{64}", capsys.readouterr().out.strip())
    assert dash.main([]) == 2


# Runs the page's own script against a minimal DOM stub and a scripted fetch (no browser needed).
PAGE_HARNESS = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const script = html.match(/<script nonce="__CSP_NONCE__">([\s\S]*?)<\/script>/)[1];
const fx = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
class El {
  constructor(tag) {
    Object.assign(this, {tag, children: [], className: '', _text: '', hidden: false, dataset: {}, listeners: {},
      disabled: false, value: ''});
    const names = () => new Set(this.className.split(' ').filter(Boolean));
    this.classList = {
      add: (c) => { const s = names(); s.add(c); this.className = [...s].join(' '); },
      toggle: (c, on) => { const s = names(); if (on === undefined ? !s.has(c) : on) s.add(c); else s.delete(c);
        this.className = [...s].join(' '); },
    };
  }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return [this._text, ...this.children.map((c) => c.textContent)].filter(Boolean).join(' '); }
  append(...n) { this.children.push(...n); }
  replaceChildren(...n) { this._text = ''; this.children = n; }
  addEventListener(ev, fn) { this.listeners[ev] = fn; }
  focus() {}
  showModal() { this.open = true; }
  close() { this.open = false; }
}
const ids = {};
const tabs = ['today', '90d', 'events', 'g1', 'commands'].map((t) => {
  const b = new El('button');
  b.dataset.tab = t;
  return b;
});
const document = {getElementById: (id) => (ids[id] = ids[id] || new El('div')), createElement: (t) => new El(t),
  querySelectorAll: () => tabs, body: new El('body')};
const routes = {'/api/csrf': {csrf: 'tok-123'}, '/api/trades?scope=today': fx.trades, '/api/acks': fx.acks};
const posted = [];
async function fetch(url, opts) {
  if (opts && opts.method === 'POST') {
    posted.push({headers: opts.headers, body: JSON.parse(opts.body)});
    return {ok: true, status: 201, json: async () => ({ok: true, cmd_id: 7})};
  }
  const body = url === '/api/status' ? routes.status : routes[url];
  if (body === 'THROW') throw new TypeError('Failed to fetch');
  if (body === undefined) return {ok: false, status: 404, json: async () => ({error: 'no ' + url})};
  return {ok: true, status: 200, json: async () => body};
}
const settle = () => new Promise((r) => setTimeout(r, 30));
function cell(id, label, row = 0) {
  const holder = ids[id].children[0] && ids[id].children[0].tag === 'div' ? ids[id].children[0] : ids[id];
  const t = holder.children.filter((c) => c.tag === 'table').pop();
  if (!t) return null;
  const i = t.children[0].children.map((c) => c.textContent).indexOf(label);
  if (i < 0) return null;
  const c = t.children[row + 1].children[i];
  return {text: c.textContent, cls: c.className, head: t.children[0].children[i].className};
}
(async () => {
  const out = {};
  routes.status = fx.online;
  new Function('document', 'fetch', 'setInterval', 'confirm', 'crypto',
    script + '\nglobalThis.api = {poll, sendCommand};')(document, fetch, () => 0, () => true, globalThis.crypto);
  await settle();
  out.online = {banner: ids.banner.hidden, stale: document.body.className, summary: ids.summary.textContent,
    bookMtm: cell('books', 'MTM net @ bid'), posMtm: cell('positions', 'MTM net @ bid'),
    posLtp: cell('positions', 'LTP (info only)'), net: cell('tab', 'net'),
    ltpExit: cell('tab', 'ltp_exit (info only)')};
  routes.status = fx.offline;
  await api.poll();
  out.offline = {banner: ids.banner.hidden, text: ids.banner.textContent, stale: document.body.className,
    positions: ids.positions.textContent};
  routes.status = fx.unknown;
  await api.poll();
  out.unknown = {banner: ids.banner.hidden, positions: ids.positions.textContent, books: ids.books.textContent};
  routes.status = 'THROW';
  await api.poll();
  out.unreachable = {banner: ids.banner.textContent, positions: ids.positions.textContent};
  routes.status = fx.online;
  ids.flatten.listeners.click();
  ids['flatten-text'].value = 'FLATTE';
  ids['flatten-text'].listeners.input();
  out.lockedAtFLATTE = ids['flatten-send'].disabled;
  ids['flatten-text'].value = 'FLATTEN';
  ids['flatten-text'].listeners.input();
  out.unlockedAtFLATTEN = !ids['flatten-send'].disabled;
  ids['flatten-send'].listeners.click();
  await settle();
  ids.pause.listeners.click();
  await settle();
  out.posted = posted;
  out.msg = ids.msg.textContent;
  out.commands = ids.tab.textContent;
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed (CI's ubuntu runner has it)")
def test_page_script_renders_honestly(tmp_path):
    status = status_doc("2026-10-12T09:59:59.000+05:30")
    fixtures = {
        "online": {"ok": True, "offline": False, "banner": None, "error": None, "age_s": 1.0, "data": status},
        "offline": {"ok": False, "offline": True, "banner": OFFLINE_BANNER, "error": "status.json is 12.0 s old",
                    "age_s": 12.0, "data": status},
        "unknown": {"ok": False, "offline": True, "banner": OFFLINE_BANNER, "error": "status.json not published yet",
                    "age_s": None, "data": None},
        "trades": {"ok": True, "file": "trades_today.json", "age_s": 5.0,
                   "data": [{"structure_id": 17, "net": -312.55, "charges": 61.2, "ltp_exit": 40.1}]},
        "acks": {"ok": True, "file": "acks.json", "age_s": 1.0,
                 "data": [{"source": "DASH", "cmd_id": 1, "kind": "PAUSE", "status": "DONE"}],
                 "sent": [{"cmd_id": 2, "kind": "FLATTEN_ALL", "created_ts": "t2", "requested_by": USER},
                          {"cmd_id": 1, "kind": "PAUSE", "created_ts": "t1", "requested_by": USER}]},
    }
    (tmp_path / "fixtures.json").write_text(json.dumps(fixtures), encoding="utf-8")
    (tmp_path / "harness.js").write_text(PAGE_HARNESS, encoding="utf-8")
    done = subprocess.run(["node", str(tmp_path / "harness.js"), str(DASH_DIR / "templates" / "index.html"),
                           str(tmp_path / "fixtures.json")], capture_output=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)

    online = out["online"]
    assert online["banner"] is True and "stale" not in online["stale"] and "phase TRADING" in online["summary"]
    assert online["bookMtm"]["text"] == "unknown"                    # null book MTM: unknown, never 0
    assert online["posMtm"]["text"] == "331.20" and "pos" in online["posMtm"]["cls"]
    assert online["posLtp"]["text"] == "43.10"
    assert "info" in online["posLtp"]["cls"] and "info" in online["posLtp"]["head"]       # LTP greyed: info only
    assert online["net"]["text"] == "-312.55" and "neg" in online["net"]["cls"]
    assert "info" in online["ltpExit"]["cls"]
    assert out["offline"]["banner"] is False and "stale" in out["offline"]["stale"]
    assert out["offline"]["text"].startswith(OFFLINE_BANNER) and "12.0 s old" in out["offline"]["text"]
    assert "331.20" in out["offline"]["positions"]                     # last data kept, dimmed under the banner
    assert out["unknown"]["banner"] is False
    assert out["unknown"]["positions"].startswith("unknown:") and out["unknown"]["books"].startswith("unknown:")
    assert "dashboard unreachable" in out["unreachable"]["banner"] and "Kite app" in out["unreachable"]["banner"]
    assert out["unreachable"]["positions"].startswith("unknown:")

    assert out["lockedAtFLATTE"] is True and out["unlockedAtFLATTEN"] is True
    flatten, pause = out["posted"]
    assert flatten["headers"]["X-Titan-CSRF"] == "tok-123" and flatten["headers"]["Content-Type"] == "application/json"
    assert flatten["body"]["kind"] == "FLATTEN_ALL" and flatten["body"]["confirm_text"] == "FLATTEN"
    assert pause["body"] == {"kind": "PAUSE", "confirm_text": None, "nonce": pause["body"]["nonce"]}
    for sent in (flatten, pause):
        assert dash.command_problem(sent["body"]) is None              # what the page sends, the server accepts
    assert flatten["body"]["nonce"] != pause["body"]["nonce"]
    assert "#7" in out["msg"]
    assert "waiting for the bot" in out["commands"] and "DONE" in out["commands"]


def test_status_age_uses_bot_timestamp(client, clock, var):
    stamped = status_doc("2026-10-12T09:59:59.500+05:30")
    write_pub(var, "status.json", stamped)
    r = get(client, "/api/status").get_json()
    assert r["age_s"] == 0.5 and r["offline"] is False
    assert isinstance(datetime.fromisoformat(stamped["ts"]), datetime)
