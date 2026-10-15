"""`python -m titan2 VERB ...`: the bot entrypoint and the operator CLI (spec §13.2, DETAIL §13.2).

/usr/local/bin/titan2 (ops/titan2-cli) runs `cd /home/ubuntu/titan/agentic_trader && exec sudo -u titan
/home/ubuntu/titan/current/venv/bin/python -m titan2 "$@"`; the env comes from /etc/titan2/titan2.env (read here,
since sudo resets the environment) overridden by the process environment.

- Mutating verbs never write titan2.db. Each inserts one row into db/commands_cli.db (this CLI is that file's only
  writer: the dashboard's insert-only table, with the CLI's wider kind list) and waits up to 30 s for the bot's ack
  in titan2.db.command_acks, read-only. With the bot down, PAUSE, FLATTEN_ALL and KILL stay queued and run at its
  next start; any other command expires 300 s after it was written (core/operator.py decides both).
- Read-only verbs open titan2.db with mode=ro. `settings set|unset` rewrites config/settings.json (validated whole,
  atomic; effective at the next bot start). Nothing here places, cancels or modifies an order.
- `bot` is the systemd unit's command. Without systemd's INVOCATION_ID or NOTIFY_SOCKET it refuses (exit 2, nothing
  opened or locked): a hand-run bot has no watchdog, no restarts and no EnvironmentFile, and its instance lock would
  make the real titan2-bot exit 73 for good. --outside-systemd runs it anyway with /etc/titan2/titan2.env merged in.
- `laptop [check] [--date D] [--no-dash] [--skip-token]` is the Windows laptop's entry (titan2.laptop): today's PAPER
  session in the foreground, or only its self-check. It needs no systemd and no override. `dash [--host H] [--port
  P] [--allow-public]` serves the read-only dashboard (titan2.dash.app.serve; 127.0.0.1:5000 unless told otherwise).
- `condor [status|dry-run|tick|settle]` is the PAPER NIFTY weekly iron condor tracker (titan2.condor): it places no
  order; the laptop runs its tick at 15:25 itself.
- `whatif [--date D]` replays a session's refused entries under the exit rules (titan2.whatif): read-only, it
  writes only reports/whatif-<date>.json. Every verb first sends Kite REST over IPv4 only (titan2.net.force_ipv4).
- On Windows the state folder and the env file default to %LOCALAPPDATA%\\titan2\\var and %APPDATA%\\titan2\\titan2.env
  (TITAN2_VAR and TITAN2_ENV_FILE override them).

Exit codes: 0 ok; 2 refused or failed (argparse usage errors are 2 too). is-trading-day: 0 yes, 1 no, 2 unknown.
The bot's own codes (0, 3, 73, 75, 78) are in titan2.runtime; `laptop` returns them too (130: stopped before the
session started).
"""

from __future__ import annotations

import argparse
import getpass
import gzip
import json
import logging
import os
import shutil
import sqlite3
import sys
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from titan2 import __version__, alerts, g1, ledger, net, selfcheck
from titan2 import laptop as laptop_mod
from titan2 import settings as settings_mod
from titan2 import store as store_mod
from titan2 import token as token_mod
from titan2.clock import CalendarError, RealClock, SessionCalendar, iso
from titan2.core.operator import CONFIRM_TEXT, EXPIRE_AFTER_S, SURVIVES_DOWNTIME
from titan2.model import TERMINAL_ORDER, TERMINAL_STRUCT, Account

log = logging.getLogger("titan2.cli")

EXIT_OK, EXIT_NO, EXIT_FAILED = 0, 1, 2
ACK_WAIT_S = 30.0
ACK_POLL_S = 0.25
BOT_ALIVE_S = 10.0                             # status.json younger than this: the bot is running
STATUS_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_BACKUP_ROOT = "/var/backups/titan2"
CLI_DB = "commands_cli.db"
TERMINAL_ACKS = frozenset({"DONE", "REJECTED", "FAILED", "EXPIRED"})
OK_ACKS = frozenset({"DONE", "ACCEPTED"})
# DETAIL §4.4, in its order; must equal core/operator.py KINDS["CLI"] (a test checks it).
CLI_KINDS = ("PAUSE", "RESUME", "FLATTEN_ALL", "KILL", "ACK_HALT", "ACK_ALERT", "ACK_FOREIGN", "SET_MODE", "RUN_RECON")
COMMANDS_DDL = (
    "CREATE TABLE commands(cmd_id INTEGER PRIMARY KEY, created_ts TEXT NOT NULL, requested_by TEXT NOT NULL, "
    "kind TEXT NOT NULL CHECK (kind IN (" + ",".join(f"'{k}'" for k in CLI_KINDS) + ")), "
    "args TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(args)), confirm_text TEXT, nonce TEXT NOT NULL UNIQUE)",
    "CREATE TRIGGER c_no_upd BEFORE UPDATE ON commands BEGIN SELECT RAISE(ABORT,'insert-only'); END",
    "CREATE TRIGGER c_no_del BEFORE DELETE ON commands BEGIN SELECT RAISE(ABORT,'insert-only'); END",
)
_HELD_LEG_STATES = ("ENTERING", "OPEN", "EXITING", "VERIFY_FLAT")
# `titan2 bot` without systemd's INVOCATION_ID / NOTIFY_SOCKET (review F-15): the operator wrapper and its sudoers rule
#   expose every verb, bot included
BOT_OUTSIDE_SYSTEMD = (
    "refused: `titan2 bot` runs under systemd only. Start it with `sudo systemctl start titan2-bot` (after `sudo "
    "systemctl reset-failed titan2-bot` if it hit its start limit). A bot started by hand has no watchdog, no "
    "restarts and no EnvironmentFile (no Kite key, no Telegram), and it takes the instance lock, so the real "
    "titan2-bot exits 73 and systemd never restarts it. To run it by hand anyway (a drill, a laptop), add "
    "--outside-systemd: it then reads /etc/titan2/titan2.env itself.")
_TERMINAL_STRUCT_SQL = "(" + ",".join(f"'{s.value}'" for s in sorted(TERMINAL_STRUCT)) + ")"
_TERMINAL_ORDER_SQL = "(" + ",".join(f"'{s.value}'" for s in sorted(TERMINAL_ORDER)) + ")"

Out = Callable[[str], Any]


def var_dir(env: Mapping[str, str]) -> Path:
    """TITAN2_VAR, else the platform default (the laptop's folder on Windows, /var/lib/titan2 elsewhere)."""
    return settings_mod.default_var_dir(env)


def _squash(sql: str | None) -> str:
    return " ".join((sql or "").split())


# ---------------------------------------------------------------------------------------------------------------
# commands_cli.db: insert, then wait for the bot's ack
# ---------------------------------------------------------------------------------------------------------------
def open_cli_db(path: Path) -> sqlite3.Connection:
    """An existing CLI command file (journal_mode=DELETE, like the dashboard's), schema verified; never creates it
    (write_command does, with its first row already inside)."""
    con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=rw", uri=True, isolation_level=None, timeout=5.0)
    try:
        mode = con.execute("PRAGMA journal_mode").fetchone()[0]
        found = dict(con.execute("SELECT name, sql FROM sqlite_master WHERE type IN ('table','trigger')").fetchall())
        want = {"commands": COMMANDS_DDL[0], "c_no_upd": COMMANDS_DDL[1], "c_no_del": COMMANDS_DDL[2]}
        if str(mode).lower() != "delete" or {k: _squash(v) for k, v in found.items()} != {
                k: _squash(v) for k, v in want.items()}:
            raise sqlite3.DatabaseError(f"{path}: journal {mode}, schema {sorted(found)}: not a titan2 CLI "
                                        "command file")
        con.execute("PRAGMA recursive_triggers=ON")          # a REPLACE would fire c_no_del, never rewrite a row
    except BaseException:
        con.close()
        raise
    return con


def bot_last_seen(db_path: Path) -> int:
    """The highest CLI cmd_id the bot has acked (0 without a database). New ids start above it, so a recreated
    command file can never hide a command behind the bot's cmd_id > last_seen read."""
    if not db_path.is_file():
        return 0
    con = store_mod.connect_readonly(db_path)
    try:
        return int(con.execute("SELECT COALESCE(MAX(cmd_id), 0) FROM command_acks WHERE source = 'CLI'").fetchone()[0])
    finally:
        con.close()


def insert_command(con: sqlite3.Connection, kind: str, args: Mapping[str, Any], *, confirm_text: str | None,
                   requested_by: str, created: datetime, floor_id: int = 0) -> int:
    """One insert-only row; returns its cmd_id (above both the file's and the bot's highest)."""
    con.execute("BEGIN IMMEDIATE")
    try:
        top = int(con.execute("SELECT COALESCE(MAX(cmd_id), 0) FROM commands").fetchone()[0])
        cmd_id = max(top, int(floor_id)) + 1
        con.execute("INSERT INTO commands(cmd_id, created_ts, requested_by, kind, args, confirm_text, nonce) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (cmd_id, iso(created), requested_by, kind, json.dumps(dict(args), sort_keys=True), confirm_text,
                     uuid.uuid4().hex))
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    return cmd_id


def _create_with_command(path: Path, write: Callable[[sqlite3.Connection], int]) -> int | None:
    """First use, or a file an operator removed: the schema AND the command go into a temp file that is then
    hard-linked into place. The bot polls the file every second; it must never see it without the table, nor empty
    (an empty file below the bot's last cmd_id reads as recreated: CommandsReset, a CRITICAL alert). Returns the
    cmd_id, or None when another CLI created the file first (the caller then appends to that one)."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        con = sqlite3.connect(str(tmp), isolation_level=None)
        try:
            con.execute("PRAGMA journal_mode=DELETE")
            con.execute("BEGIN IMMEDIATE")
            for ddl in COMMANDS_DDL:
                con.execute(ddl)
            con.execute("COMMIT")
            cmd_id = write(con)
        finally:
            con.close()
        try:
            os.link(tmp, path)
        except FileExistsError:
            return None                             # another CLI won; our temp copy (and its row) is dropped
        except OSError:                             # a filesystem without hard links
            if path.exists():
                return None
            os.replace(tmp, path)
        return cmd_id
    finally:
        tmp.unlink(missing_ok=True)


def write_command(path: Path, kind: str, args: Mapping[str, Any], *, confirm_text: str | None, requested_by: str,
                  created: datetime, floor_id: int = 0) -> int:
    """Append one command to commands_cli.db (creating the file on first use); returns its cmd_id."""

    def write(con: sqlite3.Connection) -> int:
        return insert_command(con, kind, args, confirm_text=confirm_text, requested_by=requested_by,
                              created=created, floor_id=floor_id)

    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        cmd_id = _create_with_command(path, write)
        if cmd_id is not None:
            return cmd_id
    con = open_cli_db(path)
    try:
        return write(con)
    finally:
        con.close()


def read_ack(db_path: Path, cmd_id: int) -> dict[str, Any] | None:
    """The bot's command_acks row for a CLI command, or None (not picked yet, or no database yet)."""
    if not db_path.is_file():
        return None
    con = store_mod.connect_readonly(db_path)
    try:
        row = con.execute("SELECT kind, status, result, picked_ts, done_ts FROM command_acks "
                          "WHERE source = 'CLI' AND cmd_id = ?", (int(cmd_id),)).fetchone()
    finally:
        con.close()
    return dict(row) if row is not None else None


def wait_for_ack(db_path: Path, cmd_id: int, *, wait_s: float = ACK_WAIT_S, poll_s: float = ACK_POLL_S,
                 sleep: Callable[[float], Any] = time.sleep,
                 mono: Callable[[], float] = time.monotonic) -> tuple[dict[str, Any] | None, str | None]:
    """(the newest ack seen, the last read error). Returns at once on a terminal ack; an ACCEPTED one (a flatten in
    progress) is waited on until DONE / FAILED or the deadline."""
    deadline = mono() + wait_s
    ack, error = None, None
    while True:
        try:
            row = read_ack(db_path, cmd_id)
            error = None
        except (sqlite3.Error, store_mod.StoreError, OSError) as exc:     # e.g. busy: retried until the deadline
            row, error = None, str(exc)
        if row is not None:
            ack = row
            if row["status"] in TERMINAL_ACKS:
                return ack, None
        if mono() >= deadline:
            return ack, error
        sleep(poll_s)


def requested_by(env: Mapping[str, str]) -> str:
    """The operator behind `sudo -u titan` (SUDO_USER), like the dashboard's DASH_USER; the bot records it as
    "CLI:<user>"."""
    who = env.get("SUDO_USER") or ""
    if not who:
        try:
            who = getpass.getuser()
        except Exception:                          # no login name (a bare container): still identify the source
            who = "unknown"
    return who


def send_command(kind: str, args: Mapping[str, Any], *, env: Mapping[str, str], out: Out,
                 now: Callable[[], datetime], confirm_text: str | None = None, wait_s: float = ACK_WAIT_S,
                 poll_s: float = ACK_POLL_S, sleep: Callable[[float], Any] = time.sleep,
                 mono: Callable[[], float] = time.monotonic) -> int:
    var = var_dir(env)
    db_path = var / "db" / store_mod.DB_FILENAME
    path = var / "db" / CLI_DB
    try:
        floor = bot_last_seen(db_path)
    except (sqlite3.Error, store_mod.StoreError, OSError) as exc:
        out(f"{kind}: refused: cannot read {db_path} to number the command safely ({exc})")
        return EXIT_FAILED
    try:
        cmd_id = write_command(path, kind, args, confirm_text=confirm_text, requested_by=requested_by(env),
                               created=now(), floor_id=floor)
    except (sqlite3.Error, OSError) as exc:
        out(f"{kind}: could not write {path}: {exc}")
        return EXIT_FAILED
    out(f"{kind} #{cmd_id} sent; waiting up to {wait_s:.0f} s for the bot")
    ack, error = wait_for_ack(db_path, cmd_id, wait_s=wait_s, poll_s=poll_s, sleep=sleep, mono=mono)
    if ack is None:
        later = ("it stays queued and runs when the bot next starts" if kind in SURVIVES_DOWNTIME
                 else f"it expires {EXPIRE_AFTER_S:.0f} s after it was written")
        out(f"{kind} #{cmd_id}: NOT ACKNOWLEDGED within {wait_s:.0f} s (is titan2-bot running?"
            f"{f' last read error: {error}' if error else ''}); {later}")
        return EXIT_FAILED
    out(f"{kind} #{cmd_id}: {ack['status']}: {ack['result'] or ''}".rstrip(": "))
    if ack["status"] == "ACCEPTED":
        out("  still in progress (DONE once verified flat, FAILED after 60 s while the bot keeps trying): "
            "follow it with `titan2 status`")
    return EXIT_OK if ack["status"] in OK_ACKS else EXIT_FAILED


# ---------------------------------------------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------------------------------------------
def db_summary(path: Path) -> dict[str, Any]:
    """Counts the deploy preflight needs (open legs, working orders) plus the active halts, read-only."""
    out: dict[str, Any] = {"path": str(path), "exists": path.is_file(), "ok": True, "error": None,
                           "nonterminal_structures": 0, "open_legs": 0, "nonterminal_orders": 0,
                           "active_halts": [], "last_heartbeat": None, "cli_unacked": 0}
    if not out["exists"]:
        return out
    try:
        st = store_mod.open_readonly(path)
        try:
            out["nonterminal_structures"] = int(st.scalar(
                f"SELECT COUNT(*) FROM structures WHERE state NOT IN {_TERMINAL_STRUCT_SQL}"))
            out["open_legs"] = int(st.scalar(
                "SELECT COUNT(*) FROM legs l JOIN structures s ON s.structure_id = l.structure_id WHERE l.state IN "
                f"({','.join(repr(x) for x in _HELD_LEG_STATES)}) AND s.state NOT IN {_TERMINAL_STRUCT_SQL}"))
            out["nonterminal_orders"] = int(st.scalar(
                f"SELECT COUNT(*) FROM orders WHERE role <> 'EXTERNAL' AND state NOT IN {_TERMINAL_ORDER_SQL}"))
            out["active_halts"] = [{"halt_id": h.halt_id, "scope": h.scope, "kind": h.kind, "reason": h.reason,
                                    "clears": str(h.clears), "since": iso(h.created_wall)} for h in st.active_halts()]
            out["last_heartbeat"] = st.scalar("SELECT ts FROM events WHERE kind = 'HEARTBEAT' "
                                              "ORDER BY seq DESC LIMIT 1")
            acked = st.last_command_id("CLI")
        finally:
            st.close()
        cli = path.parent / CLI_DB
        if cli.is_file():
            con = store_mod.connect_readonly(cli)
            try:
                out["cli_unacked"] = int(con.execute("SELECT COUNT(*) FROM commands WHERE cmd_id > ?",
                                                     (acked,)).fetchone()[0])
            finally:
                con.close()
    except Exception as exc:                       # reported, and the CLI exits 2: callers must not assume "flat"
        out["ok"], out["error"] = False, f"{type(exc).__name__}: {exc}"
    return out


def status_info(var: Path, now_wall: datetime, calendar: SessionCalendar | None = None) -> dict[str, Any]:
    pub = var / "pub" / "status.json"
    status = age = error = None
    try:
        with open(pub, "rb") as fh:
            mtime = os.fstat(fh.fileno()).st_mtime
            raw = fh.read(STATUS_MAX_BYTES + 1)
        if len(raw) > STATUS_MAX_BYTES:
            raise ValueError("larger than 8 MB")
        status = json.loads(raw)
        age = max(0.0, time.time() - mtime)
    except FileNotFoundError:
        error = "not published yet"
    except (OSError, ValueError) as exc:
        error = f"unreadable: {exc}"
    try:
        trading = (calendar or SessionCalendar.load()).is_trading_day(now_wall.date())
    except CalendarError:
        trading = None
    return {"now": iso(now_wall), "session": now_wall.date().isoformat(), "trading_day": trading,
            "status_file": str(pub), "status_age_s": None if age is None else round(age, 1), "status_error": error,
            "bot_active": age is not None and age < BOT_ALIVE_S, "status": status,
            "db": db_summary(var / "db" / store_mod.DB_FILENAME)}


def render_status(info: Mapping[str, Any]) -> str:
    db, st = info["db"], info.get("status") or {}
    bot = (f"running (status.json {info['status_age_s']} s old)" if info["bot_active"] else
           f"NOT RUNNING or stuck (status.json {info['status_age_s'] if info['status_age_s'] is not None else '-'} "
           f"s old{'; ' + info['status_error'] if info['status_error'] else ''})")
    lines = [f"titan2 {__version__} status at {info['now']}",
             f"bot: {bot}; phase {st.get('phase', '-')}; trading day {info['trading_day']}; "
             f"allow_live {st.get('allow_live', '-')}"]
    feed = st.get("feed") or {}
    if feed:
        lines.append(f"feed: connected {feed.get('connected')}, last tick {feed.get('last_tick_age_s')} s ago, "
                     f"warm {feed.get('warm')}, degraded {feed.get('degraded')}")
    for name, acct in sorted((st.get("accounts") or {}).items()):
        lines.append(f"account {name}: recon {acct.get('recon')} ({acct.get('recon_age_s')} s); "
                     f"{len(acct.get('broker_positions') or [])} broker position row(s)")
    for book, b in sorted((st.get("books") or {}).items()):
        lines.append(f"book {book}: realized {b.get('realized_net')} mtm {b.get('mtm_net')} entries "
                     f"{b.get('entries')}/{b.get('max_entries')} open {b.get('open')} daily-stop room "
                     f"{b.get('daily_stop_room')}")
    for p in st.get("positions") or []:
        lines.append(f"position {p.get('book')} {p.get('symbol')} qty {p.get('qty')} entry {p.get('entry_avg')} "
                     f"bid {p.get('bid')} ({p.get('src')}, {p.get('age_ms')} ms) mtm {p.get('mtm_net')} "
                     f"stop {p.get('stop')} {p.get('state')}")
    if not db["ok"]:
        lines.append(f"titan2.db: UNREADABLE: {db['error']}")
    elif not db["exists"]:
        lines.append("titan2.db: not created yet")
    else:
        lines.append(f"titan2.db: {db['open_legs']} open leg(s), {db['nonterminal_orders']} working order(s), "
                     f"{db['nonterminal_structures']} live structure(s), {db['cli_unacked']} CLI command(s) not "
                     f"acked yet")
        for h in db["active_halts"]:
            lines.append(f"halt #{h['halt_id']} {h['kind']} [{h['scope']}] since {h['since']} (clears {h['clears']}): "
                         f"{h['reason']}")
        if not db["active_halts"]:
            lines.append("halts: none")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------------------
# Read-only reports
# ---------------------------------------------------------------------------------------------------------------
def _open_db(var: Path, out: Out) -> store_mod.Store | None:
    path = var / "db" / store_mod.DB_FILENAME
    try:
        return store_mod.open_readonly(path)
    except (store_mod.StoreError, sqlite3.Error, OSError) as exc:
        out(f"cannot open {path} read-only: {exc}")
        return None


def _day(text: str | None, today: date) -> date:
    return date.fromisoformat(text) if text else today


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def cmd_trades(var: Path, day: date, book: str | None, out: Out) -> int:
    st = _open_db(var, out)
    if st is None:
        return EXIT_FAILED
    try:
        rows = st.trades(session=day, book=book)
    finally:
        st.close()
    if not rows:
        out(f"no trades on {day}{f' in {book}' if book else ''}")
        return EXIT_OK
    cols = ("structure_id", "book", "symbol", "qty", "entry_avg", "exit_avg", "charges", "net", "exit_reason",
            "pnl_status")
    table = [cols] + [tuple(_fmt(r.get(c)) for c in cols) for r in rows]
    widths = [max(len(row[i]) for row in table) for i in range(len(cols))]
    for row in table:
        out("  ".join(cell.ljust(w) for cell, w in zip(row, widths)))
    by_book: dict[str, float] = {}
    for r in rows:
        by_book[r["book"]] = by_book.get(r["book"], 0.0) + float(r["net"])
    for b, net in sorted(by_book.items()):
        out(f"net {b}: {net:.2f}")
    return EXIT_OK


def cmd_report(var: Path, day: date, now_wall: datetime, out: Out) -> int:
    st = _open_db(var, out)
    if st is None:
        return EXIT_FAILED
    try:
        out(ledger.daily_report(st, day, now_wall, reports_dir=var / "reports").markdown)
    finally:
        st.close()
    return EXIT_OK


def cmd_g1(var: Path, strategy: str | None, as_json: bool, out: Out) -> int:
    st = _open_db(var, out)
    if st is None:
        return EXIT_FAILED
    try:
        cards = [c for c in g1.scorecards(st) if strategy is None or c["strategy"] == strategy]
    finally:
        st.close()
    if strategy is not None and not cards:
        out(f"no strategy {strategy!r} in titan2.db")
        return EXIT_FAILED
    if as_json:
        out(json.dumps(cards, indent=2, sort_keys=True, default=str))
        return EXIT_OK
    for c in cards:
        pending = f" (next session: {c['pending_mode']})" if c["pending_mode"] else ""
        out(f"{c['strategy']} {c['version']}: mode {c['mode']}{pending}; "
            f"trial {c['state']}; n {c['n']} over {c['sessions']} session(s); Exp_net {c['exp_pct']}% "
            f"(LB {c['lb_pct']}%); placebo n {c['placebo_n']} coverage {c['placebo_coverage']}; next look "
            f"{c['next_look']}")
        if c["certificate"]:
            cert = c["certificate"]
            out(f"  CERTIFIED {cert['computed_ts']}: unlock with `titan2 live {c['strategy']} --confirm "
                f"{cert['inputs_hash']} --i-accept-live-risk` (key 1, ALLOW_LIVE=1, must be set too)")
    if not cards:
        out("no strategies registered yet")
    return EXIT_OK


def cmd_selftest(var: Path, today: date, out: Out) -> int:
    """Today's LIVE order-path self-test (§10.8), read-only: 0 = passed or not required; 2 = needed, not passed."""
    st = _open_db(var, out)
    if st is None:
        return EXIT_FAILED
    try:
        live = sorted(r["strategy"] for r in st.strategy_modes() if r["mode"] == "LIVE")
        events = st.events(session=today, kind="SELFTEST")
    finally:
        st.close()
    passed = [e for e in events if e["payload"].get("outcome") == "PASSED"]
    if passed:
        p = passed[-1]["payload"]
        out(f"LIVE self-test {today}: PASSED ({p.get('latency_ms')} ms, order {p.get('order_pk')})")
        return EXIT_OK
    if not live:
        out(f"LIVE self-test {today}: not required (no LIVE-mode strategy)")
        return EXIT_OK
    bad = [e["payload"] for e in events if e["payload"].get("outcome") in ("FAILED", "FILLED")]
    if bad:
        out(f"LIVE self-test {today}: {bad[-1].get('outcome')}: {bad[-1].get('detail', '')} "
            f"(LIVE entries for {', '.join(live)} stay off today)")
    else:
        out(f"LIVE self-test {today}: not passed yet (runs at 09:15:30, retry 09:17:00) for {', '.join(live)}")
    return EXIT_FAILED


def cmd_is_trading_day(day: date, out: Out) -> int:
    try:
        cal = SessionCalendar.load()
    except CalendarError as exc:
        out(f"unknown: {exc}")
        return EXIT_FAILED
    if cal.is_trading_day(day):
        hours = cal.session_hours(day)
        out(f"{day}: trading session {hours[0]:%H:%M}-{hours[1]:%H:%M}")
        return EXIT_OK
    out(f"{day}: no trading session{'' if cal.covers(day) else ' (outside the calendar: fails closed)'}")
    return EXIT_NO


def _sqlite_ok(path: Path) -> list[str]:
    """PRAGMA integrity_check on a backup copy (our own file; the source is never opened for writing)."""
    con = sqlite3.connect(str(path))
    try:
        rows = [r[0] for r in con.execute("PRAGMA integrity_check").fetchall()]
    finally:
        con.close()
    return [] if rows == ["ok"] else rows


def cmd_backup(var: Path, to: str | None, compress: bool, now_wall: datetime, env: Mapping[str, str],
               out: Out) -> int:
    """DETAIL §13.8 steps 1, 2 and 4: SQLite online backups of the four databases (each checked), and
    settings.json. Secrets are never copied. Retention and the optional S3 copy are ops/backup.sh's job."""
    if to:
        dest = Path(to)
    else:
        root = Path(env.get("TITAN2_BACKUP_DIR") or DEFAULT_BACKUP_ROOT)
        dest = root / now_wall.strftime("%Y-%m-%d")
        if dest.exists() and any(dest.iterdir()):
            dest = root / now_wall.strftime("%Y-%m-%d-%H%M%S")
    sources = {"titan2.db": var / "db" / store_mod.DB_FILENAME, "paper_exchange.db": var / "db" / "paper_exchange.db",
               "commands_cli.db": var / "db" / CLI_DB, "commands_dash.db": var / "dash" / "commands_dash.db"}
    failed = False
    dest.mkdir(parents=True, exist_ok=True)
    for name, src in sources.items():
        target = dest / name
        if not src.is_file():
            out(f"{name}: absent, skipped")
            continue
        if target.exists() or Path(f"{target}.gz").exists():
            out(f"{name}: {target} already exists; refusing to overwrite a backup")
            failed = True
            continue
        try:
            store_mod.backup_db(src, target)
            problems = _sqlite_ok(target)
        except (sqlite3.Error, store_mod.StoreError, OSError) as exc:
            out(f"{name}: backup FAILED: {exc}")
            failed = True
            continue
        if problems:
            out(f"{name}: copy FAILED integrity_check: {'; '.join(problems[:3])}")
            failed = True
            continue
        if compress:
            with open(target, "rb") as fin, gzip.open(f"{target}.gz", "wb") as fout:
                shutil.copyfileobj(fin, fout)
            target.unlink()
            target = Path(f"{target}.gz")
        out(f"{name}: ok -> {target}")
    settings_file = var / "config" / "settings.json"
    if settings_file.is_file():
        shutil.copy2(settings_file, dest / "settings.json")
        out(f"settings.json: copied -> {dest / 'settings.json'}")
    return EXIT_FAILED if failed else EXIT_OK


# ---------------------------------------------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------------------------------------------
def cmd_settings(var: Path, action: str, key: str | None, value: str | None, out: Out) -> int:
    path = var / "config" / "settings.json"
    if action in ("set", "unset"):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)     # bootstrap creates config/; a fresh dev tree may not
        except OSError as exc:
            out(f"refused: cannot create {path.parent}: {exc}")
            return EXIT_FAILED
        try:
            res = (settings_mod.set_setting(key, value, path) if action == "set"
                   else settings_mod.unset_setting(key, path))
        except settings_mod.SettingsError as exc:
            out(f"refused: {'; '.join(exc.errors)}")
            return EXIT_FAILED
        out(res.message())
        return EXIT_OK
    loaded = settings_mod.load(path)
    out(loaded.summary())
    if action == "show":
        out(json.dumps(loaded.settings.to_json(), indent=2))
    else:
        changes = settings_mod.diff(settings_mod.DEFAULTS, loaded.settings)
        for k, (default, current) in changes.items():
            out(f"{k}: default {json.dumps(default)} -> {json.dumps(current)}")
        if not changes:
            out("no differences from the code defaults")
    return EXIT_OK if loaded.valid else EXIT_FAILED


def cmd_dash(host: str, port: int, allow_public: bool, out: Out) -> int:
    """`titan2 dash`: the read-only dashboard in the foreground until Ctrl+C (the laptop; EC2 runs it under
    gunicorn). titan2.dash.app.serve() reads the process environment under the dashboard's own env file, never the
    bot's: no broker credential reaches it. Any address but 127.0.0.1 / localhost needs --allow-public."""
    try:
        from titan2.dash import app as dash_app             # lazy: flask, and the dashboard's own imports only
    except ImportError as exc:
        out(f"refused: the dashboard cannot load ({exc}): python -m pip install -r requirements.lock")
        return EXIT_FAILED
    return dash_app.serve(host, port, laptop_mod.is_laptop(), allow_public=allow_public, out=out)


# ---------------------------------------------------------------------------------------------------------------
# argument parsing and dispatch
# ---------------------------------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="titan2", description=f"titan2 {__version__}: bot entrypoint and operator CLI")
    sub = p.add_subparsers(dest="verb", required=True, metavar="VERB")
    b = sub.add_parser("bot", help="the trading process (systemd only)")
    b.add_argument("--safe", action="store_true", help="exits only (same as TITAN2_SAFE=1)")
    b.add_argument("--outside-systemd", dest="outside_systemd", action="store_true",
                   help="run without systemd anyway (no watchdog, no restarts; reads /etc/titan2/titan2.env)")
    lp = sub.add_parser("laptop", help="the Windows laptop: today's PAPER session in the foreground ('laptop check': "
                                       "only its self-check)")
    lp.add_argument("action", nargs="?", choices=("check",), help="check: run only the laptop self-check")
    lp.add_argument("--date", help="YYYY-MM-DD: run as if it were that day (simulation and tests only)")
    lp.add_argument("--no-dash", dest="no_dash", action="store_true", help="do not start the dashboard")
    lp.add_argument("--skip-token", dest="skip_token", action="store_true",
                    help="no Kite login: the bot uses the session already saved")
    d = sub.add_parser("dash", help="serve the read-only dashboard here (the laptop; EC2 runs gunicorn)")
    d.add_argument("--host", default="127.0.0.1", help="the address to listen on (default 127.0.0.1)")
    d.add_argument("--port", type=int, default=laptop_mod.DASH_PORT, help=f"default {laptop_mod.DASH_PORT}")
    d.add_argument("--allow-public", dest="allow_public", action="store_true",
                   help="listen on an address other than 127.0.0.1 / localhost (e.g. 0.0.0.0)")
    t = sub.add_parser("token", help="refresh the Kite session (auto TOTP, or a manual request token)")
    t.add_argument("--request-token", dest="request_token")
    s = sub.add_parser("selfcheck", help="run a self-check phase")
    s.add_argument("--phase", required=True, choices=selfcheck.PHASES)
    s.add_argument("--db-copy", dest="db_copy")
    st = sub.add_parser("status", help="the bot's published status plus titan2.db counts")
    st.add_argument("--json", action="store_true")
    sub.add_parser("pause", help="no new LIVE entries; PAPER intents go to SHADOW; exits continue")
    sub.add_parser("resume", help="clear PAUSE (and FLATTEN once verified flat)")
    f = sub.add_parser("flatten-all", help="exit every bot leg (needs --confirm FLATTEN)")
    f.add_argument("--confirm", required=True)
    k = sub.add_parser("kill", help="flatten with KILL_SWITCH and demote every LIVE strategy (needs --confirm KILL)")
    k.add_argument("--confirm", required=True)
    ah = sub.add_parser("ack-halt", help="acknowledge a halt by id")
    ah.add_argument("halt_id", type=int)
    ah.add_argument("--note", required=True)
    aa = sub.add_parser("ack-alert", help="stop a CRITICAL alert repeating")
    aa.add_argument("key")
    af = sub.add_parser("ack-foreign", help="acknowledge a non-bot position exactly")
    af.add_argument("symbol")
    af.add_argument("product")
    af.add_argument("qty", type=int)
    af.add_argument("--account", default="LIVE", choices=[a.value for a in Account])
    m = sub.add_parser("mode", help="set a strategy's mode (OFF now; SHADOW / PAPER at the next session)")
    m.add_argument("strategy")
    m.add_argument("mode", choices=("OFF", "SHADOW", "PAPER"))
    lv = sub.add_parser("live", help="key 2: LIVE from the next session for a CERTIFIED strategy")
    lv.add_argument("strategy")
    lv.add_argument("--confirm", required=True, metavar="HASH", help="the G1 certificate's inputs_hash")
    lv.add_argument("--i-accept-live-risk", dest="accept", action="store_true", required=True)
    sub.add_parser("recon", help="reconcile every account now")
    se = sub.add_parser("settings", help="show, diff, set or unset settings.json (effective at the next start)")
    se_sub = se.add_subparsers(dest="action", required=True, metavar="ACTION")
    se_sub.add_parser("show")
    se_sub.add_parser("diff")
    ss = se_sub.add_parser("set")
    ss.add_argument("key")
    ss.add_argument("value")
    su = se_sub.add_parser("unset")
    su.add_argument("key")
    tr = sub.add_parser("trades", help="ledger rows of a session")
    tr.add_argument("--date")
    tr.add_argument("--book")
    rp = sub.add_parser("report", help="the daily report of a session")
    rp.add_argument("--date")
    wi = sub.add_parser("whatif", help="what a session's refused entries would have made (research aid, read-only)")
    wi.add_argument("--date")
    co = sub.add_parser("condor", help="the PAPER NIFTY weekly iron condor tracker (no orders): status, dry-run, "
                                       "tick (15:25-15:29), settle")
    co.add_argument("action", nargs="?", default="status", choices=["status", "dry-run", "tick", "settle"])
    g = sub.add_parser("g1", help="G1 scorecards")
    g.add_argument("strategy", nargs="?")
    g.add_argument("--json", action="store_true")
    it = sub.add_parser("is-trading-day", help="exit 0 on a trading session, 1 otherwise")
    it.add_argument("--date")
    bk = sub.add_parser("backup", help="online backup of the databases and settings.json")
    bk.add_argument("--to")
    bk.add_argument("--gzip", action="store_true")
    sub.add_parser("selftest", help="today's LIVE order-path self-test result (deploy check)")
    al = sub.add_parser("alert", help="send a CRITICAL alert (OnFailure units)")
    al.add_argument("--critical", required=True, metavar="TEXT")
    return p


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None, out: Out | None = None,
         now: Callable[[], datetime] | None = None, wait_s: float = ACK_WAIT_S, poll_s: float = ACK_POLL_S,
         sleep: Callable[[float], Any] = time.sleep, mono: Callable[[], float] = time.monotonic) -> int:
    args = build_parser().parse_args(argv)
    net.force_ipv4(os.environ if env is None else env)   # before any connection: Kite REST over IPv4 only
    if args.verb == "bot":
        bot_env = dict(os.environ if env is None else env)
        if not (bot_env.get("INVOCATION_ID") or bot_env.get("NOTIFY_SOCKET")):
            # not started by systemd (titan2-bot.service is Type=notify): review F-15
            if not args.outside_systemd:
                (out or print)(BOT_OUTSIDE_SYSTEMD)
                return EXIT_FAILED
            # what the unit's EnvironmentFile would have given it
            env = token_mod.load_env(token_mod.ENV_FILE, environ=bot_env)
            log.warning("titan2 bot started outside systemd (--outside-systemd): no watchdog, no restarts; it holds "
                        "the instance lock while it runs")
        from titan2 import runtime              # imported only here: the operator verbs stay light

        return runtime.main(safe=args.safe, env=env)
    if args.verb == "laptop":                   # the env file and the logs are the laptop's own (laptop.cli)
        return laptop_mod.cli(args.action, date_text=args.date, dash=not args.no_dash, skip_token=args.skip_token,
                              env=env, out=out, cli_mod=sys.modules[__name__])
    if args.verb == "dash":                     # never the bot's env: serve() reads the dashboard's own
        return cmd_dash(args.host, args.port, args.allow_public, out or print)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    env = token_mod.load_env() if env is None else dict(env)
    out = out or print
    now = now or RealClock().now_wall
    var = var_dir(env)
    verb = args.verb

    def send(kind: str, cmd_args: Mapping[str, Any] | None = None, confirm: str | None = None) -> int:
        return send_command(kind, cmd_args or {}, env=env, out=out, now=now, confirm_text=confirm, wait_s=wait_s,
                            poll_s=poll_s, sleep=sleep, mono=mono)

    if verb in ("flatten-all", "kill"):
        kind = "FLATTEN_ALL" if verb == "flatten-all" else "KILL"
        if args.confirm != CONFIRM_TEXT[kind]:
            out(f"refused: {verb} needs --confirm {CONFIRM_TEXT[kind]}")
            return EXIT_FAILED
        return send(kind, {}, args.confirm)
    if verb == "pause":
        return send("PAUSE")
    if verb == "resume":
        return send("RESUME")
    if verb == "recon":
        return send("RUN_RECON")
    if verb == "ack-halt":
        return send("ACK_HALT", {"halt_id": args.halt_id, "note": args.note})
    if verb == "ack-alert":
        return send("ACK_ALERT", {"key": args.key})
    if verb == "ack-foreign":
        symbol = args.symbol.strip().upper()
        return send("ACK_FOREIGN", {"symbol": symbol if ":" in symbol else f"NFO:{symbol}",
                                    "product": args.product.strip().upper(), "qty": args.qty,
                                    "account": args.account})
    if verb == "mode":
        return send("SET_MODE", {"strategy": args.strategy, "mode": args.mode})
    if verb == "live":
        confirm = args.confirm.strip().lower()
        if not confirm:
            out("refused: live needs the certificate hash in --confirm")
            return EXIT_FAILED
        return send("SET_MODE", {"strategy": args.strategy, "mode": "LIVE", "confirm": confirm})
    if verb == "token":
        return token_mod.run(request_token=args.request_token, env=env)
    if verb == "selfcheck":
        return selfcheck.cli(args.phase, db_copy=args.db_copy, out=out, env=env)
    if verb == "alert":
        return alerts.critical_now(args.critical, env=env)
    if verb == "status":
        info = status_info(var, now())
        out(json.dumps(info, indent=2, sort_keys=True, default=str) if args.json else render_status(info))
        return EXIT_OK if info["db"]["ok"] else EXIT_FAILED
    if verb == "settings":
        return cmd_settings(var, args.action, getattr(args, "key", None), getattr(args, "value", None), out)
    if verb == "condor":
        from titan2 import condor                   # lazy: kiteconnect

        return condor.cli(args.action, var, env, out)
    today = now().date()
    try:
        if verb == "trades":
            return cmd_trades(var, _day(args.date, today), args.book, out)
        if verb == "report":
            return cmd_report(var, _day(args.date, today), now(), out)
        if verb == "whatif":
            from titan2 import whatif               # lazy: kiteconnect

            return whatif.run(var, _day(args.date, today), env, out=out)
        if verb == "is-trading-day":
            return cmd_is_trading_day(_day(args.date, today), out)
    except ValueError as exc:                         # a malformed --date
        out(f"refused: {exc}")
        return EXIT_FAILED
    if verb == "g1":
        return cmd_g1(var, args.strategy, args.json, out)
    if verb == "backup":
        return cmd_backup(var, args.to, args.gzip, now(), env, out)
    if verb == "selftest":
        return cmd_selftest(var, today, out)
    out(f"unknown verb {verb!r}")
    return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
