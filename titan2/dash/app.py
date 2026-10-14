"""titan2 dashboard (spec §12; DETAIL §12.1-12.3, §4.4, §4.7).

- Process: on EC2 `gunicorn -w 1 --threads 4 -k gthread -b 127.0.0.1:8050 titan2.dash.app:app` as user titandash,
  reached over an SSH tunnel. On a Windows laptop `titan2 dash` (or `python -m titan2.dash.app serve`) runs serve():
  werkzeug's own threaded server (it ships with Flask; requirements.lock pins nothing else), on 127.0.0.1:5000 by
  default; any other address needs the explicit allow_public flag. Every request must carry Host 127.0.0.1:<port> or
  localhost:<port> (DNS-rebinding guard).
- Paths: <var> is TITAN2_VAR, else %LOCALAPPDATA%\\titan2\\var on Windows, else /var/lib/titan2.
- Isolation: imports no kiteconnect, broker, core or store code; reads only <var>/pub/*.json; writes only
  <var>/dash/commands_dash.db, through a SQLite authorizer that allows INSERT into `commands` and SELECT.
- Auth: HTTP Basic against DASH_USER + DASH_PASS_SCRYPT from dash.env (on EC2 /etc/titan2/dash.env, the unit's
  EnvironmentFile; serve() reads the file itself, on Windows %APPDATA%\\titan2\\dash.env). A POST also needs
  X-Titan-CSRF = HMAC(DASH_SECRET, date|user), as served by GET /api/csrf, and a unique nonce; FLATTEN_ALL also
  needs confirm_text == "FLATTEN". The app refuses to start without DASH_USER, DASH_PASS_SCRYPT and DASH_SECRET.
- It computes nothing: every number on the page comes from files the bot writes with net_pnl (MTM at the
  realizable bid; LTP is info only).
"""
# Ported from agentic_trader/dashboard.py:2033-2066 @b4694f3 — changes: only the meaning of the broker-truth fields
# is kept (broker positions are shown as the bot publishes them); no Kite session, no P&L sums, no cache.

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import sqlite3
import string
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple

from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import HTTPException

from titan2.clock import Clock, RealClock, apply_env_offset, iso, parse_wall

log = logging.getLogger("titan2.dash")

DASH_PORT = 8050                                     # gunicorn's bind on EC2 (titan2-dash.service)
LAPTOP_PORT = 5000                                   # serve(): `titan2 dash`
LOOPBACK = "127.0.0.1"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")          # all serve() binds without allow_public
MIN_PORT = 1024
LISTEN_BACKLOG = 64
EXIT_OK, EXIT_REFUSED = 0, 2                         # as the titan2 CLI
DASH_KINDS = ("PAUSE", "RESUME", "FLATTEN_ALL")      # KILL, ACK_*, SET_MODE and RUN_RECON are CLI-only
FLATTEN_CONFIRM = "FLATTEN"
CSRF_HEADER = "X-Titan-CSRF"
OFFLINE_AFTER_S = 10.0
OFFLINE_BANNER = "bot offline — use the Kite app"
DEFAULT_VAR_DIR = "/var/lib/titan2"
DASH_ENV_FILE = "/etc/titan2/dash.env"               # EC2: titan2-dash.service's EnvironmentFile
WINDOWS = os.name == "nt"
TRADE_FILES = {"today": "trades_today.json", "90d": "trades_90d.json"}
MAX_PUB_BYTES = 32 * 1024 * 1024
MAX_BODY_BYTES = 4096
SENT_LIMIT = 20
NONCE_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")
MIN_SECRET_LEN = 16
TEMPLATE = Path(__file__).resolve().parent / "templates" / "index.html"
CSP_NONCE_SLOT = "__CSP_NONCE__"


class DashConfigError(RuntimeError):
    """The dashboard cannot start safely: missing or malformed credentials, or a bad commands_dash.db."""


# ---------------------------------------------------------------------------------------------------------------
# Password hash. werkzeug.security's scrypt string format, "scrypt:N:r:p$salt$hexhash", so a hash from
# generate_password_hash(pw, method="scrypt") also works; verified with hashlib alone (no werkzeug version coupling).
# ---------------------------------------------------------------------------------------------------------------
_SCRYPT_RE = re.compile(r"scrypt:(\d{1,8}):(\d{1,3}):(\d{1,3})\$([^$]{8,128})\$((?:[0-9a-fA-F]{2}){16,64})")
SALT_CHARS = string.ascii_letters + string.digits


def _scrypt(password: str, salt: str, n: int, r: int, p: int, dklen: int) -> bytes:
    # scrypt needs 128*N*r*p bytes; OpenSSL's default 32 MiB cap is too small for N=2^15, r=8 (werkzeug's margin)
    return hashlib.scrypt(password.encode("utf-8"), salt=salt.encode("utf-8"), n=n, r=r, p=p,
                          maxmem=132 * n * r * p, dklen=dklen)


def parse_scrypt_hash(stored: object) -> tuple[int, int, int, str, bytes]:
    m = _SCRYPT_RE.fullmatch(stored.strip()) if isinstance(stored, str) else None
    if m is None:
        raise DashConfigError("DASH_PASS_SCRYPT must look like scrypt:N:r:p$salt$hexhash "
                              "(make one with: python -m titan2.dash.app hash-password)")
    n, r, p = (int(g) for g in m.group(1, 2, 3))
    if n < 2 ** 14 or n & (n - 1) or not 8 <= r <= 32 or not 1 <= p <= 16 or 132 * n * r * p > 2 ** 30:
        raise DashConfigError(f"DASH_PASS_SCRYPT parameters N={n} r={r} p={p} are too weak or too costly")
    return n, r, p, m.group(4), bytes.fromhex(m.group(5))


def hash_password(password: str, *, n: int = 2 ** 15, r: int = 8, p: int = 1, salt: str | None = None) -> str:
    """The DASH_PASS_SCRYPT value for a password."""
    if salt is None:
        salt = "".join(secrets.choice(SALT_CHARS) for _ in range(16))
    stored = f"scrypt:{n}:{r}:{p}${salt}${_scrypt(password, salt, n, r, p, 64).hex()}"
    parse_scrypt_hash(stored)                       # never hand out a hash the app would refuse at start
    return stored


def verify_password(password: str, stored: str) -> bool:
    n, r, p, salt, expected = parse_scrypt_hash(stored)
    return hmac.compare_digest(_scrypt(password, salt, n, r, p, len(expected)), expected)


def parse_basic(header: str | None) -> tuple[str, str] | None:
    """'Basic base64(user:password)' -> (user, password); None when absent or malformed."""
    if not header or len(header) > 1024:
        return None
    scheme, _, value = header.strip().partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        user, sep, password = base64.b64decode(value.strip(), validate=True).decode("utf-8").partition(":")
    except ValueError:                              # binascii.Error and UnicodeDecodeError are ValueErrors
        return None
    return (user, password) if sep else None


class BasicAuth:
    """HTTP Basic against the scrypt hash.

    scrypt costs ~50 ms and 32 MiB by design and the page polls every 2 s, so an Authorization header that verified
    once is remembered, as an HMAC under a per-process random key (never in clear). Failures are never remembered.
    """

    def __init__(self, user: str, stored_hash: str) -> None:
        parse_scrypt_hash(stored_hash)
        self.user = user
        self._hash = stored_hash
        self._key = secrets.token_bytes(32)
        self._good: set[bytes] = set()
        self._lock = threading.Lock()

    def check(self, header: str | None) -> bool:
        if not header:
            return False
        mark = hmac.new(self._key, header.encode("utf-8", "replace"), hashlib.sha256).digest()
        with self._lock:
            if mark in self._good:
                return True
        creds = parse_basic(header)
        if creds is None:
            return False
        user_ok = hmac.compare_digest(creds[0].encode("utf-8"), self.user.encode("utf-8"))
        ok = verify_password(creds[1], self._hash) and user_ok    # scrypt runs even for a wrong user name
        if ok:
            with self._lock:
                if len(self._good) >= 16:
                    self._good.clear()
                self._good.add(mark)
        return ok


def load_credentials(env: Mapping[str, str]) -> tuple[str, str, str]:
    """(DASH_USER, DASH_PASS_SCRYPT, DASH_SECRET), or DashConfigError: the app refuses to start without them."""
    broker_keys = sorted(k for k in env if k.startswith("ZERODHA_"))
    if broker_keys:
        raise DashConfigError(f"broker credentials in the dashboard's environment ({', '.join(broker_keys)}); "
                              "dash.env must hold only DASH_USER, DASH_PASS_SCRYPT and DASH_SECRET")
    user, stored, secret = (env.get(k) or "" for k in ("DASH_USER", "DASH_PASS_SCRYPT", "DASH_SECRET"))
    if not user or not stored:
        raise DashConfigError("DASH_USER and DASH_PASS_SCRYPT must both be set (dash.env)")
    if ":" in user or not user.isprintable() or user != user.strip():
        raise DashConfigError("DASH_USER must be printable, with no ':' and no surrounding spaces")
    parse_scrypt_hash(stored)
    if len(secret) < MIN_SECRET_LEN:
        raise DashConfigError(f"DASH_SECRET must be set, at least {MIN_SECRET_LEN} characters "
                              "(make one with: python -m titan2.dash.app new-secret)")
    return user, stored, secret


# ---------------------------------------------------------------------------------------------------------------
# Paths, the dashboard's env file and the Host guard. EC2 is unchanged: TITAN2_VAR from the unit, port 8050.
# ---------------------------------------------------------------------------------------------------------------
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def resolve_var_dir(env: Mapping[str, str]) -> Path:
    """<var>: TITAN2_VAR; else %LOCALAPPDATA%\\titan2\\var on Windows; else /var/lib/titan2."""
    if env.get("TITAN2_VAR"):
        return Path(env["TITAN2_VAR"])
    if not WINDOWS:
        return Path(DEFAULT_VAR_DIR)
    if not env.get("LOCALAPPDATA"):
        raise DashConfigError("neither TITAN2_VAR nor LOCALAPPDATA is set: cannot tell where the bot's files are")
    return Path(env["LOCALAPPDATA"]) / "titan2" / "var"


def dash_env_path(env: Mapping[str, str]) -> Path | None:
    """The env file serve() reads: TITAN2_DASH_ENV_FILE; else %APPDATA%\\titan2\\dash.env on Windows (outside the repo,
    beside the bot's env file; None without APPDATA); else /etc/titan2/dash.env."""
    if env.get("TITAN2_DASH_ENV_FILE"):
        return Path(env["TITAN2_DASH_ENV_FILE"])
    if WINDOWS:
        return Path(env["APPDATA"]) / "titan2" / "dash.env" if env.get("APPDATA") else None
    return Path(DASH_ENV_FILE)


def parse_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines as systemd's EnvironmentFile (and titan2.token.parse_env) read them: comments and malformed
    lines skipped, an optional `export `, optional matching quotes. UTF-8 with or without a BOM, or UTF-16 with a BOM
    (what `>` writes in Windows PowerShell 5.1). Raises OSError or ValueError."""
    raw = path.read_bytes()
    text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig")
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        key, value = key.strip().removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if sep and _ENV_KEY_RE.fullmatch(key):
            out[key] = value
    return out


def load_env(environ: Mapping[str, str] | None = None) -> tuple[dict[str, str], str]:
    """(the dashboard env file's values overridden by the process environment, a note naming the file). A missing or
    unreadable file leaves just the process environment. No value is ever logged or printed."""
    environ = os.environ if environ is None else environ
    path = dash_env_path(environ)
    merged: dict[str, str] = {}
    if path is None:
        note = "no dashboard env file (APPDATA is not set)"
    else:
        try:
            merged.update(parse_env_file(path))
            note = f"dashboard env file {path}"
        except FileNotFoundError:
            note = f"dashboard env file {path} (not found)"
            if path.with_name(path.name + ".txt").is_file():
                note = f"dashboard env file {path} (not found, but {path.name}.txt is: rename it)"
        except (OSError, ValueError) as exc:
            note = f"dashboard env file {path} (unreadable: {type(exc).__name__})"
    merged.update(environ)
    return merged, note


def allowed_hosts(port: int) -> frozenset[str]:
    """The Host headers a dashboard served on `port` accepts."""
    return frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})


def ip_literal_host(host: str, port: int) -> bool:
    """'<IPv4>:<port>' or '[<IPv6>]:<port>'. DNS rebinding needs a DNS name, so a non-loopback bind (allow_public)
    may also accept these, never a name."""
    name, sep, given = host.rpartition(":")
    if not sep or given != str(port):
        return False
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------------------------------------------
# commands_dash.db (DETAIL §4.4). The dashboard is the file's only writer, so it creates the schema at startup.
# ---------------------------------------------------------------------------------------------------------------
COMMANDS_SCHEMA = {                                  # DETAIL §4.4 verbatim
    ("table", "commands"): (
        "CREATE TABLE commands(cmd_id INTEGER PRIMARY KEY, created_ts TEXT NOT NULL, requested_by TEXT NOT NULL,\n"
        "  kind TEXT NOT NULL CHECK (kind IN ('PAUSE','RESUME','FLATTEN_ALL')),\n"
        "  args TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(args)), confirm_text TEXT, nonce TEXT NOT NULL UNIQUE)"),
    ("trigger", "c_no_upd"):
        "CREATE TRIGGER c_no_upd BEFORE UPDATE ON commands BEGIN SELECT RAISE(ABORT,'insert-only'); END",
    ("trigger", "c_no_del"):
        "CREATE TRIGGER c_no_del BEFORE DELETE ON commands BEGIN SELECT RAISE(ABORT,'insert-only'); END",
}
SCHEMA_OBJECTS = frozenset(COMMANDS_SCHEMA) | {("index", "sqlite_autoindex_commands_1")}   # UNIQUE(nonce)
INSERT_SQL = ("INSERT INTO commands(created_ts, requested_by, kind, args, confirm_text, nonce) "
              "VALUES (?, ?, ?, '{}', ?, ?)")
SENT_COLUMNS = ("cmd_id", "created_ts", "requested_by", "kind", "confirm_text", "nonce")
SENT_SQL = f"SELECT {', '.join(SENT_COLUMNS)} FROM commands ORDER BY cmd_id DESC LIMIT ?"


def _squash(sql: str | None) -> str:
    return " ".join((sql or "").split())


def ensure_commands_db(path: Path) -> None:
    """Create the commands table and its insert-only triggers if absent; verify the file holds exactly them.

    Startup only, on a connection without the authorizer (it needs CREATE). Requests use open_commands_db().
    """
    created = not path.exists()
    try:
        con = sqlite3.connect(path, isolation_level=None, timeout=5.0)
        try:
            mode = con.execute("PRAGMA journal_mode=DELETE").fetchone()[0]   # the bot reads it as another user
            if str(mode).lower() != "delete":
                raise DashConfigError(f"{path}: journal_mode is {mode}, expected delete")
            con.execute("BEGIN IMMEDIATE")
            for sql in COMMANDS_SCHEMA.values():
                con.execute(re.sub(r"^CREATE (TABLE|TRIGGER) ", r"CREATE \1 IF NOT EXISTS ", sql))
            con.execute("COMMIT")
            found = {(kind, name): sql for kind, name, sql in con.execute("SELECT type, name, sql FROM sqlite_master")}
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise DashConfigError(f"{path}: {exc}") from exc
    if set(found) != SCHEMA_OBJECTS:
        raise DashConfigError(f"{path}: unexpected schema objects {sorted(set(found) ^ SCHEMA_OBJECTS)}")
    for key, sql in COMMANDS_SCHEMA.items():
        if _squash(found[key]) != _squash(sql):
            raise DashConfigError(f"{path}: {key[0]} {key[1]} differs from DETAIL §4.4")
    if created and os.name == "posix":
        os.chmod(path, 0o640)                       # the bot reads it through group titan (setgid dash/ dir)


def commands_authorizer(action: int, arg1: str | None, arg2: str | None, db_name: str | None,
                        source: str | None) -> int:
    """sqlite3 authorizer for every request connection: INSERT into `commands`, SELECT, and reading the columns of
    `commands` from a top-level statement. Everything else (UPDATE, DELETE, DDL, PRAGMA, ATTACH, BEGIN, VACUUM,
    functions, other tables) gets SQLITE_DENY."""
    if action == sqlite3.SQLITE_SELECT:
        return sqlite3.SQLITE_OK
    if (action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_READ) and arg1 == "commands" and db_name == "main"
            and source is None):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def open_commands_db(path: Path) -> sqlite3.Connection:
    """A request connection to commands_dash.db.

    - mode=rw never creates the file: a vanished DB must not be recreated with cmd_id restarting at 1.
    - Autocommit, so no BEGIN/COMMIT needs authorizing.
    - recursive_triggers=ON makes INSERT OR REPLACE fire c_no_del; without it a REPLACE on a used nonce or cmd_id
      silently deletes and rewrites a command row (e.g. a FLATTEN_ALL turned into a RESUME).
    """
    con = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=rw", uri=True, isolation_level=None, timeout=5.0)
    try:
        con.execute("PRAGMA recursive_triggers=ON")
        con.set_authorizer(commands_authorizer)
    except BaseException:
        con.close()
        raise
    return con


def command_problem(body: Any) -> str | None:
    """Why a POST /api/command body is refused, or None. Only the three dashboard kinds pass."""
    if not isinstance(body, dict):
        return "body must be a JSON object {kind, confirm_text, nonce}"
    extra = set(body) - {"kind", "confirm_text", "nonce"}
    if extra:
        return f"unknown fields: {', '.join(sorted(extra))}"
    kind, confirm, nonce = body.get("kind"), body.get("confirm_text"), body.get("nonce")
    if kind not in DASH_KINDS:
        return f"kind must be one of {', '.join(DASH_KINDS)}"
    if not isinstance(nonce, str) or not NONCE_RE.fullmatch(nonce):
        return "nonce must be 16-64 characters of A-Z a-z 0-9 _ -"
    if confirm is not None and (not isinstance(confirm, str) or len(confirm) > 32):
        return "confirm_text must be a short string"
    if kind == "FLATTEN_ALL" and confirm != FLATTEN_CONFIRM:
        return f'FLATTEN_ALL needs confirm_text "{FLATTEN_CONFIRM}"'
    return None


# ---------------------------------------------------------------------------------------------------------------
# pub/*.json: the only files the dashboard reads
# ---------------------------------------------------------------------------------------------------------------
class PubRead(NamedTuple):
    data: Any
    mtime: float | None
    error: str | None
    missing: bool = False


def _no_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name}")


def read_pub(pub_dir: Path, rel: str) -> PubRead:
    try:
        with open(pub_dir / rel, "rb") as fh:
            st = os.fstat(fh.fileno())
            if st.st_size > MAX_PUB_BYTES:
                return PubRead(None, st.st_mtime, f"{rel} is too large ({st.st_size} bytes)")
            raw = fh.read()
    except FileNotFoundError:
        return PubRead(None, None, f"{rel} not published yet", missing=True)
    except OSError as exc:
        return PubRead(None, None, f"cannot read {rel}: {exc.strerror or exc}")
    try:
        return PubRead(json.loads(raw, parse_constant=_no_constant), st.st_mtime, None)
    except ValueError as exc:
        return PubRead(None, st.st_mtime, f"{rel} is not valid JSON: {exc}")


def pub_view(pub_dir: Path, rel: str, now: datetime) -> dict[str, Any]:
    r = read_pub(pub_dir, rel)
    age = None if r.mtime is None else round(now.timestamp() - r.mtime, 1)
    return {"file": rel, "ok": r.error is None, "missing": r.missing, "error": r.error, "age_s": age, "data": r.data}


def _ts_age(now: datetime, ts: object) -> float | None:
    if not isinstance(ts, str):
        return None
    try:
        return (now - parse_wall(ts)).total_seconds()
    except ValueError:
        log.warning("status.json ts %r does not parse; using the file mtime", ts)
        return None


def status_view(pub_dir: Path, now: datetime) -> dict[str, Any]:
    """status.json plus its age; offline (with the banner) when missing, unreadable or more than 10 s old.

    The age is taken from the bot's own `ts`, or from the file mtime when `ts` is unusable."""
    r = read_pub(pub_dir, "status.json")
    data, error, age = r.data, r.error, None
    if error is None and not isinstance(data, dict):
        data, error = None, "status.json is not a JSON object"
    if error is None:
        age = _ts_age(now, data.get("ts"))
        if age is None:
            age = now.timestamp() - r.mtime
        if age > OFFLINE_AFTER_S:
            error = f"status.json is {age:.1f} s old"
        elif age < -OFFLINE_AFTER_S:
            error = f"status.json is stamped {-age:.1f} s in the future"
    offline = error is not None
    return {"file": "status.json", "ok": not offline, "offline": offline, "banner": OFFLINE_BANNER if offline else None,
            "error": error, "age_s": None if age is None else round(age, 1), "data": data}


def _error(code: int, message: str) -> Response:
    resp = jsonify({"ok": False, "error": message})
    resp.status_code = code
    return resp


# ---------------------------------------------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------------------------------------------
def create_app(env: Mapping[str, str] | None = None, *, clock: Clock | None = None,
               var_dir: str | os.PathLike[str] | None = None, port: int = DASH_PORT, ip_hosts: bool = False) -> Flask:
    """Build the dashboard. Raises DashConfigError (so gunicorn refuses to boot) when credentials are missing or
    malformed, or commands_dash.db cannot be created or verified.

    port: the port the browser uses (the Host guard accepts 127.0.0.1:<port> and localhost:<port>). ip_hosts: also
    accept an IP-literal Host on that port (serve() with allow_public only)."""
    env = os.environ if env is None else env
    user, stored_hash, secret = load_credentials(env)
    var = Path(var_dir) if var_dir is not None else resolve_var_dir(env)
    hosts = allowed_hosts(port)
    origins = frozenset(f"http://{host}" for host in hosts)
    pub_dir, db_path = var / "pub", var / "dash" / "commands_dash.db"
    ensure_commands_db(db_path)
    try:
        page = TEMPLATE.read_text(encoding="utf-8")
    except OSError as exc:
        raise DashConfigError(f"cannot read {TEMPLATE}: {exc}") from exc
    clock = clock if clock is not None else RealClock()
    auth = BasicAuth(user, stored_hash)
    secret_key = secret.encode("utf-8")

    def csrf_token() -> str:
        return hmac.new(secret_key, f"{clock.today().isoformat()}|{user}".encode("utf-8"), hashlib.sha256).hexdigest()

    app = Flask(__name__, static_folder=None)
    app.config.update(MAX_CONTENT_LENGTH=MAX_BODY_BYTES, TITAN2_PUB_DIR=pub_dir, TITAN2_COMMANDS_DB=db_path)

    @app.before_request
    def guard() -> Response | None:
        host = (request.headers.get("Host") or "").strip().lower()
        if host not in hosts and not (ip_hosts and ip_literal_host(host, port)):
            return _error(403, f"host not allowed: open http://127.0.0.1:{port} or http://localhost:{port}")
        origin = request.headers.get("Origin")
        if origin is not None and origin.lower() not in origins and origin.lower() != f"http://{host}":
            return _error(403, "cross-origin request refused")
        header = request.headers.get("Authorization")
        if not auth.check(header):
            log.warning("dashboard: %s credentials for %s %s", "rejected" if header else "missing",
                        request.method, request.path)
            resp = _error(401, "authentication required")
            resp.headers["WWW-Authenticate"] = 'Basic realm="titan2", charset="UTF-8"'
            return resp
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            sent = (request.headers.get(CSRF_HEADER) or "").encode("utf-8", "replace")
            if not hmac.compare_digest(sent, csrf_token().encode("ascii")):
                return _error(403, f"missing or stale {CSRF_HEADER} token: fetch /api/csrf")
        return None

    @app.after_request
    def harden(resp: Response) -> Response:
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        return resp

    @app.errorhandler(HTTPException)
    def http_error(exc: HTTPException) -> Response:
        resp = _error(exc.code or 500, exc.description or exc.name)
        for key, value in exc.get_headers():        # keeps e.g. Allow on a 405
            if key.lower() != "content-type":
                resp.headers[key] = value
        return resp

    @app.get("/")
    def index() -> Response:
        nonce = secrets.token_urlsafe(16)
        resp = Response(page.replace(CSP_NONCE_SLOT, nonce), mimetype="text/html")
        resp.headers["Content-Security-Policy"] = (
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; "
            "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        return resp

    @app.get("/api/csrf")
    def api_csrf() -> Response:
        return jsonify({"csrf": csrf_token(), "header": CSRF_HEADER, "user": user, "date": clock.today().isoformat()})

    @app.get("/api/status")
    def api_status() -> Response:
        return jsonify(status_view(pub_dir, clock.now_wall()))

    @app.get("/api/trades")
    def api_trades() -> Response:
        scope = request.args.get("scope", "today")
        if scope not in TRADE_FILES:
            return _error(400, "scope must be today or 90d")
        return jsonify(pub_view(pub_dir, TRADE_FILES[scope], clock.now_wall()))

    @app.get("/api/trade/<int:structure_id>")
    def api_trade(structure_id: int) -> Response:
        body = pub_view(pub_dir, f"trade/{structure_id}.json", clock.now_wall())
        resp = jsonify(body)
        if body["missing"]:
            resp.status_code = 404
        return resp

    @app.get("/api/events")
    def api_events() -> Response:
        return jsonify(pub_view(pub_dir, "events_tail.json", clock.now_wall()))

    @app.get("/api/g1")
    def api_g1() -> Response:
        return jsonify(pub_view(pub_dir, "g1.json", clock.now_wall()))

    @app.get("/api/acks")
    def api_acks() -> Response:
        body = pub_view(pub_dir, "acks.json", clock.now_wall())
        try:
            con = open_commands_db(db_path)
            try:
                body["sent"] = [dict(zip(SENT_COLUMNS, row)) for row in con.execute(SENT_SQL, (SENT_LIMIT,))]
            finally:
                con.close()
        except sqlite3.Error as exc:
            body["sent"], body["sent_error"] = None, f"commands_dash.db unreadable: {exc}"
        return jsonify(body)

    @app.post("/api/command")
    def api_command() -> Response:
        body = request.get_json(silent=True)
        problem = command_problem(body)
        if problem is not None:
            return _error(400, problem)
        kind, created_ts = body["kind"], iso(clock.now_wall())
        try:
            con = open_commands_db(db_path)
            try:
                cmd_id = con.execute(INSERT_SQL, (created_ts, user, kind, body.get("confirm_text"),
                                                  body["nonce"])).lastrowid
            finally:
                con.close()
        except sqlite3.IntegrityError as exc:
            if "commands.nonce" in str(exc):
                return _error(409, "nonce already used: command NOT recorded again")
            return _error(400, f"command NOT recorded: {exc}")
        except sqlite3.Error as exc:
            log.error("dashboard: %s NOT recorded: %s", kind, exc)
            return _error(503, f"command NOT recorded: {exc}")
        log.info("dashboard: %s queued as cmd_id %s by %s", kind, cmd_id, user)
        resp = jsonify({"ok": True, "cmd_id": cmd_id, "kind": kind, "created_ts": created_ts})
        resp.status_code = 201
        return resp

    log.info("titan2 dashboard ready: pub=%s commands=%s", pub_dir, db_path)
    return app


# ---------------------------------------------------------------------------------------------------------------
# serve(): `titan2 dash` on the Windows laptop (EC2 keeps gunicorn and its unit)
# ---------------------------------------------------------------------------------------------------------------
def bind_problem(host: str, port: object, allow_public: bool = False) -> str | None:
    """Why serve() refuses to listen on host:port, or None. Without the explicit allow_public flag only 127.0.0.1 and
    localhost pass: 0.0.0.0, '::' and '' (every interface) and any other address are refused."""
    if isinstance(port, bool) or not isinstance(port, int) or not MIN_PORT <= port <= 65535:
        return f"port must be a whole number from {MIN_PORT} to 65535, not {port!r}"
    if host not in LOOPBACK_HOSTS and not allow_public:
        return (f"the dashboard listens on 127.0.0.1 only, not {host!r}: anyone who can reach it could try the "
                "password over plain HTTP. Pass the explicit allow-public flag to listen there anyway")
    return None


def listen_socket(host: str, port: int) -> socket.socket:
    """The listening socket, bound here so that a port in use is an OSError on every OS. On Windows a socket with
    SO_REUSEADDR (werkzeug sets it) may share a port another program listens on, splitting the connections without
    an error, so it is SO_EXCLUSIVEADDRUSE there; elsewhere SO_REUSEADDR, as the server does."""
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    try:
        option = getattr(socket, "SO_EXCLUSIVEADDRUSE", None) if WINDOWS else socket.SO_REUSEADDR
        if option is not None:
            sock.setsockopt(socket.SOL_SOCKET, option, 1)
        sock.bind((host, port))
        sock.listen(LISTEN_BACKLOG)
    except BaseException:
        sock.close()
        raise
    return sock


def run_server(application: Flask, sock: socket.socket) -> str:
    """Serve on sock until Ctrl+C: werkzeug's threaded server (Flask's built-in one, without the debugger or the
    reloader), on the socket bound here; it binds nothing itself. Returns the server's name."""
    from werkzeug.serving import make_server

    logging.getLogger("werkzeug").setLevel(logging.WARNING)     # not a line per 2 s poll; errors still show
    host, port = sock.getsockname()[:2]
    server = make_server(host, port, application, threaded=True, fd=sock.fileno())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return "werkzeug"


def serve(host: str = LOOPBACK, port: int = LAPTOP_PORT, laptop: bool = False, *, allow_public: bool = False,
          env: Mapping[str, str] | None = None, out: Callable[[str], Any] = print) -> int:
    """`titan2 dash`: the dashboard in the foreground until Ctrl+C. Returns 0 after Ctrl+C, 2 when refused (address,
    credentials, var folder, commands_dash.db, port in use); nothing is built or bound before the address passes.

    env defaults to os.environ, read under the dashboard env file (load_env). Never pass the bot's merged env: it
    holds ZERODHA_* keys, and the dashboard refuses to start with broker credentials. laptop (and any Windows run)
    creates <var>/dash when missing, as bootstrap.sh does on EC2."""
    host = LOOPBACK if host is None else str(host).strip().lower()
    problem = bind_problem(host, port, allow_public)
    if problem is not None:
        out(f"refused: {problem}")
        return EXIT_REFUSED
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    merged, note = load_env(env)
    try:
        load_credentials(merged)
    except DashConfigError as exc:
        out(f"refused: {exc}")
        out(f"  {note}. dash.env holds only DASH_USER, DASH_PASS_SCRYPT and DASH_SECRET; make the values with "
            "`python -m titan2.dash.app hash-password` and `python -m titan2.dash.app new-secret`")
        return EXIT_REFUSED
    public = host not in LOOPBACK_HOSTS
    try:
        var = resolve_var_dir(merged)
        if laptop or WINDOWS:
            (var / "dash").mkdir(parents=True, exist_ok=True)
        application = create_app(merged, var_dir=var, port=port, ip_hosts=public)
    except (DashConfigError, OSError) as exc:
        out(f"refused: {exc}")
        return EXIT_REFUSED
    try:
        sock = listen_socket(host, port)
    except OSError as exc:
        out(f"refused: cannot listen on {host or 'every interface'} port {port} (is a dashboard or another program "
            f"already on it?): {exc}")
        return EXIT_REFUSED
    if public:
        out(f"WARNING: listening on {host or 'every interface'} port {port}: anyone on this network can reach the "
            "dashboard and try its password over plain HTTP. Use it only on a network you trust")
    shown = "<this machine's IP>" if public else host
    out(f"titan2 dashboard{' (laptop profile)' if laptop else ''}: open http://{shown}:{port}  (Ctrl+C stops it; "
        f"{note}; files in {var})")
    try:
        name = run_server(application, sock)
    finally:
        sock.close()
    out(f"titan2 dashboard stopped ({name})")
    return EXIT_OK


# ---------------------------------------------------------------------------------------------------------------
# gunicorn entry point and the dash.env helper
# ---------------------------------------------------------------------------------------------------------------
_app: Flask | None = None
_app_lock = threading.Lock()


def __getattr__(name: str) -> Any:
    """PEP 562: `app` (gunicorn's titan2.dash.app:app) is built on first access. Importing this module therefore
    never needs secrets, while gunicorn still refuses to boot without DASH_USER, DASH_PASS_SCRYPT and DASH_SECRET."""
    global _app
    if name != "app":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    with _app_lock:
        if _app is None:
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
            _app = create_app()
        return _app


def main(argv: list[str]) -> int:
    """python -m titan2.dash.app hash-password | new-secret: prints lines for dash.env.
    python -m titan2.dash.app serve [--host H] [--port P] [--laptop] [--allow-public]: serve() with only the
    dashboard's own imports (a child process of the laptop session keeps the isolation of §12.1)."""
    if argv[:1] == ["serve"]:
        p = argparse.ArgumentParser(prog="python -m titan2.dash.app serve")
        p.add_argument("--host", default=LOOPBACK)
        p.add_argument("--port", type=int, default=LAPTOP_PORT)
        p.add_argument("--laptop", action="store_true")
        p.add_argument("--allow-public", dest="allow_public", action="store_true",
                       help="allow an address other than 127.0.0.1 / localhost (e.g. 0.0.0.0)")
        args = p.parse_args(argv[1:])
        apply_env_offset(os.environ)                     # a corrected laptop session's internet time, if it passed one
        return serve(args.host, args.port, args.laptop, allow_public=args.allow_public)
    if argv == ["hash-password"]:
        password = getpass.getpass("dashboard password (12+ characters): ")
        if len(password) < 12 or password != getpass.getpass("again: "):
            print("refused: the passwords differ or are shorter than 12 characters", file=sys.stderr)
            return 2
        print(f"DASH_PASS_SCRYPT='{hash_password(password)}'")    # single quotes: the value contains '$'
        return 0
    if argv == ["new-secret"]:
        print(f"DASH_SECRET={secrets.token_hex(32)}")
        return 0
    print("usage: python -m titan2.dash.app hash-password | new-secret | serve [--host H] [--port P] [--laptop] "
          "[--allow-public]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
