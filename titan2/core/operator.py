"""Operator commands: PAUSE, RESUME, FLATTEN_ALL, KILL, the acks, SET_MODE, RUN_RECON (spec §4.4, §8.9, §10.5,
§10.8; DETAIL §4.4, §8.9, §13.2).

Two insert-only SQLite files feed the core: dash/commands_dash.db (the dashboard: PAUSE, RESUME, FLATTEN_ALL) and
db/commands_cli.db (the CLI: those plus KILL, ACK_HALT, ACK_ALERT, ACK_FOREIGN, SET_MODE, RUN_RECON). The core
reads both once a second, read-only, taking rows with cmd_id > the last one it picked per source, and records each
outcome in titan2.db.command_acks (ACCEPTED, DONE, REJECTED, FAILED, EXPIRED; pub/acks.json follows). It never
writes either command file.

- PAUSE, FLATTEN_ALL and KILL wait for a bot that is down and run at its next start (a stop's own FLATTEN_ALL
  excepted, below); every other command expires 300 s after it was created (EXPIRED, nothing done).
- FLATTEN_ALL needs confirm_text "FLATTEN" and KILL "KILL", from either source; the dashboard cannot send KILL.
- FLATTEN_ALL: HALT(FLATTEN); cancel every working ENTRY/SELFTEST order; every bot leg to EXITING(OPERATOR_FLATTEN)
  (an entering one exits as soon as it opens: the halt is its forced flag). Acked ACCEPTED, then DONE once every
  bot structure is closed and post-fill positions show every bot symbol flat; FAILED (CRITICAL, with per-leg detail)
  after 60 s, while the engine keeps trying, and DONE later if it gets there. Non-bot positions are listed, never
  touched.
- KILL = the same flatten with KILL_SWITCH under HALT(KILL, ACK), plus every LIVE strategy demoted (trial DEMOTED).
- RESUME clears PAUSE, and FLATTEN only once flat is verified. KILL clears only by ACK_HALT once flat is verified;
  MISMATCH / UNKNOWN_POSITION / SHORT_DETECTED only while recon is CLEAN; SAFE_FLAG only outside --safe mode.
- A stop's own FLATTEN_ALL (EngineContext.stop_flatten_by: the laptop runner's first Ctrl+C) belongs to that stop:
  it expires like a non-operator command (never run at a later start), and the FLATTEN halt it raised is cleared,
  once flat is verified, as the run ends (Shutdown, or the EOD timeline's DONE): entries stay off until the process
  stops, and the next start trades again (release_stop_flatten). Any other FLATTEN halt, and a PAUSE, stay.
- SET_MODE: OFF applies at once and marks the strategy's session IMPAIRED; SHADOW / PAPER / LIVE take effect at
  the next session (pending_mode). LIVE (key 2, §10.8) needs ALLOW_LIVE=1 in this process, the CERTIFIED trial's
  hash (g1.verify_unlock: certificate at most 7 days old) and no active GLOBAL ACK halt.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from titan2 import g1
from titan2.clock import parse_wall
from titan2.core.events import Alert, Command, Priority
from titan2.core.recon import net_qty, post_fill
from titan2.core.risk import HALT_RULES, can_clear
from titan2.core.state import ACCOUNTS, FlattenTrack
from titan2.model import MIS, Account, Halt, HaltClears, Role
from titan2.settings import POSITIONS_POST_FILL_LAG_S, VERIFY_FLAT_RECHECK_S
from titan2.store import StoreError, connect_readonly

if TYPE_CHECKING:
    from titan2.core.engine import Engine

__all__ = [
    "CONFIRM_TEXT", "CommandRow", "CommandsReset", "EXPIRE_AFTER_S", "FLATTEN_FAIL_AFTER_S", "KINDS",
    "SURVIVES_DOWNTIME", "flat_status", "flatten_tick", "is_stop_flatten", "poll", "process", "read_new",
    "release_stop_flatten", "start_flatten",
]

log = logging.getLogger(__name__)

KINDS = {
    "DASH": frozenset({"PAUSE", "RESUME", "FLATTEN_ALL"}),
    "CLI": frozenset({"PAUSE", "RESUME", "FLATTEN_ALL", "KILL", "ACK_HALT", "ACK_ALERT", "ACK_FOREIGN", "SET_MODE",
                      "RUN_RECON"}),
}
SURVIVES_DOWNTIME = frozenset({"PAUSE", "FLATTEN_ALL", "KILL"})
CONFIRM_TEXT = {"FLATTEN_ALL": "FLATTEN", "KILL": "KILL"}
EXPIRE_AFTER_S = 300.0
POLL_EVERY_S = 1.0
# The core thread reads the command files inside a Timer. dash/commands_dash.db belongs to titandash: a lock held
#   there (a wedged or hostile dashboard) must never stall fills, quotes and exits, so a busy file is skipped (WARN)
#   and read again at the next poll. A normal insert holds its lock for well under this (the wait itself is longer
#   on Windows, whose sleep granularity is about 16 ms).
READ_BUSY_TIMEOUT_MS = 10
FLATTEN_FAIL_AFTER_S = 60.0
MODES = ("OFF", "SHADOW", "PAPER", "LIVE")


@dataclass(frozen=True, slots=True)
class CommandRow:
    source: str                      # DASH | CLI
    cmd_id: int
    created_wall: datetime | None    # None: unparseable (refused)
    requested_by: str
    kind: str
    args: Any                        # the parsed args JSON (a dict when valid)
    confirm_text: str | None
    nonce: str


class CommandsReset(Exception):
    """The command file's highest cmd_id is below what the bot already picked: the file was recreated, and new
    commands would be ignored (cmd_id > last_seen). Never silent: the poll alerts CRITICAL."""


def read_new(path: str | Path, source: str, last_seen: int) -> list[CommandRow]:
    """Rows with cmd_id > last_seen, oldest first, through a read-only connection ([] while the file is absent).
    A file locked by another connection raises sqlite3.OperationalError after READ_BUSY_TIMEOUT_MS, never later."""
    p = Path(path)
    if not p.is_file():
        return []
    conn = connect_readonly(p)
    try:
        conn.execute(f"PRAGMA busy_timeout={int(READ_BUSY_TIMEOUT_MS)}")
        top = conn.execute("SELECT COALESCE(MAX(cmd_id), 0) FROM commands").fetchone()[0]
        if top < last_seen:
            raise CommandsReset(f"{source} commands file {p} tops out at cmd_id {top} but the bot already picked "
                                f"{last_seen}: it was recreated, so its commands are not read")
        rows = conn.execute("SELECT cmd_id, created_ts, requested_by, kind, args, confirm_text, nonce FROM commands "
                            "WHERE cmd_id > ? ORDER BY cmd_id", (int(last_seen),)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            created = parse_wall(r["created_ts"])
        except (TypeError, ValueError):
            created = None
        try:
            args = json.loads(r["args"]) if r["args"] else {}
        except ValueError:
            args = None
        out.append(CommandRow(source, int(r["cmd_id"]), created, str(r["requested_by"]), str(r["kind"]), args,
                              r["confirm_text"], str(r["nonce"])))
    return out


def poll(eng: Engine) -> list[Command]:
    """Once a second: read both command DBs and process every new row (Timer duty, §3.2)."""
    st = eng.state
    if st.last_poll_mono is not None and eng.now_mono - st.last_poll_mono < POLL_EVERY_S:
        return []
    st.last_poll_mono = eng.now_mono
    out: list[Command] = []
    for source in ("DASH", "CLI"):
        path = eng.ctx.command_dbs.get(source)
        if path is None:
            continue
        try:
            rows = read_new(path, source, st.cmd_last_seen.get(source, 0))
        except CommandsReset as exc:
            log.critical("%s", exc)
            out.append(Alert("CRITICAL", "COMMANDS_RESET", f"{exc}; use the CLI or the Kite app", f"commands:{source}"))
            continue
        except (sqlite3.Error, StoreError, OSError) as exc:
            log.warning("commands %s (%s) unreadable: %s", source, path, exc)
            out.append(Alert("WARN", "COMMANDS_UNREADABLE", f"{source} commands DB {path} unreadable: {exc}",
                             f"commands:{source}"))
            continue
        for row in rows:
            st.cmd_last_seen[source] = max(st.cmd_last_seen.get(source, 0), row.cmd_id)
            out += process(eng, row)
    return out


def _problem(row: CommandRow, now: datetime, stop_by: str | None = None) -> str | None:
    if row.kind not in KINDS.get(row.source, ()):
        return f"{row.kind} is not accepted from {row.source}"
    want = CONFIRM_TEXT.get(row.kind)
    if want is not None and (row.confirm_text or "").strip() != want:
        return f'{row.kind} needs confirm_text "{want}"'
    if row.created_wall is None:
        return "created_ts is not a timestamp"
    if not isinstance(row.args, dict):
        return "args must be a JSON object"
    survives = row.kind in SURVIVES_DOWNTIME and _who(row) != stop_by     # a stop's own FLATTEN_ALL dies with it
    if not survives and (now - row.created_wall).total_seconds() > EXPIRE_AFTER_S:
        return "EXPIRED"
    return None


def process(eng: Engine, row: CommandRow) -> list[Command]:
    """Validate and run one command; its command_acks row and events are written in the engine's transaction."""
    eng.event("COMMAND_RECEIVED", {"source": row.source, "cmd_id": row.cmd_id, "kind": row.kind,
                                   "requested_by": row.requested_by,
                                   "args": row.args if isinstance(row.args, dict) else None})
    problem = _problem(row, eng.now_wall, eng.ctx.stop_flatten_by)
    cmds: list[Command] = []
    if problem == "EXPIRED":
        status, result = "EXPIRED", f"created more than {EXPIRE_AFTER_S:.0f} s ago; nothing done"
    elif problem is not None:
        status, result = "REJECTED", problem
    else:
        status, result, cmds = _HANDLERS[row.kind](eng, row)
    _ack(eng, row.source, row.cmd_id, row.kind, status, result)
    return cmds


def _ack(eng: Engine, source: str, cmd_id: int, kind: str, status: str, result: str) -> None:
    done = None if status == "ACCEPTED" else eng.now_wall
    eng.store.put_command_ack(source, cmd_id, kind, status, eng.now_wall, result=result[:500], done_wall=done)
    eng.event("COMMAND_RESULT", {"source": source, "cmd_id": cmd_id, "kind": kind, "status": status,
                                 "result": result[:500]},
              "INFO" if status in ("ACCEPTED", "DONE") else "WARN")
    eng.mark_pub("acks")


def _who(row: CommandRow) -> str:
    return f"{row.source}:{row.requested_by}"


# ---------------------------------------------------------------------------------------------------------------
# Handlers: (status, result text, commands)
# ---------------------------------------------------------------------------------------------------------------
def _pause(eng: Engine, row: CommandRow) -> tuple[str, str, list[Command]]:
    if eng.halted("PAUSE"):
        return "DONE", "already paused", []
    cmds = eng.add_halt("PAUSE", "GLOBAL", _halt_reason("PAUSE", _who(row)), operator=True)
    return "DONE", "paused: no new LIVE entries; PAPER intents divert to SHADOW; exits continue", cmds


def _resume(eng: Engine, row: CommandRow) -> tuple[str, str, list[Command]]:
    cleared, kept = [], []
    for h in eng.active_halts(scope="GLOBAL"):
        if h.kind not in ("PAUSE", "FLATTEN"):
            continue
        flat = flat_status(eng)[0] if h.kind == "FLATTEN" else False
        ok, why = can_clear(h, "RESUME", today=eng.session, flat=flat)
        if ok:
            eng.clear_halt(h, _who(row), "RESUME")
            cleared.append(h.kind)
        else:
            kept.append(f"{h.kind}: {why}")
    if kept:
        return "REJECTED", f"cleared {cleared or 'nothing'}; kept {'; '.join(kept)}", []
    return "DONE", f"cleared {', '.join(cleared)}" if cleared else "nothing to resume", []


def _halt_reason(kind: str, who: str) -> str:
    """The reason of the operator halt a command raised: who sent it is in it (release_stop_flatten reads it)."""
    return f"operator {kind} ({who})"


def start_flatten(eng: Engine, row: CommandRow, *, halt: str, reason: str) -> tuple[str, str, list[Command]]:
    """FLATTEN_ALL steps 1-3 (§8.9); step 4 (DONE / FAILED) is flatten_tick()."""
    cmds = eng.add_halt(halt, "GLOBAL", _halt_reason(row.kind, _who(row)), operator=True)
    for o in list(eng.state.orders.values()):
        if o.role in (Role.ENTRY, Role.SELFTEST) and not o.is_terminal:
            cmds += eng.request_cancel(o, row.kind)
    for leg in sorted(eng.state.legs.values(), key=lambda lg: lg.leg_id):
        cmds += eng.force_exit(leg, reason)
    flat, why = flat_status(eng)
    if flat:
        return "DONE", f"already flat ({why})", cmds
    eng.state.flatten[(row.source, row.cmd_id)] = FlattenTrack(row.kind, eng.now_mono)
    return "ACCEPTED", f"flattening every bot leg; {why}", cmds


def _flatten_all(eng: Engine, row: CommandRow) -> tuple[str, str, list[Command]]:
    return start_flatten(eng, row, halt="FLATTEN", reason="OPERATOR_FLATTEN")


def _kill(eng: Engine, row: CommandRow) -> tuple[str, str, list[Command]]:
    status, result, cmds = start_flatten(eng, row, halt="KILL", reason="KILL_SWITCH")
    demoted = g1.demote_all_live(eng.store, "KILL", eng.now_wall, eng.now_mono, changed_by=_who(row))
    eng.reload_modes()
    cmds += [d.alert() for d in demoted]
    names = ", ".join(d.strategy for d in demoted) or "none"
    cmds.append(Alert("CRITICAL", "KILL", f"KILL by {_who(row)}: every bot leg exits (KILL_SWITCH); LIVE strategies "
                      f"demoted: {names}; HALT(KILL) clears by ACK once flat", "kill"))
    return status, f"{result}; demoted: {names}", cmds


def _ack_halt(eng: Engine, row: CommandRow) -> tuple[str, str, list[Command]]:
    try:
        hid = int(row.args.get("halt_id"))
    except (TypeError, ValueError):
        return "REJECTED", f"halt_id {row.args.get('halt_id')!r} is not a number", []
    h = next((x for x in eng.state.halts if x.halt_id == hid), None)
    if h is None:
        return "REJECTED", f"no active halt {hid}", []
    note = row.args.get("note")
    rule = HALT_RULES.get(h.kind)
    flat = flat_status(eng)[0] if rule is not None and rule.needs == "FLAT" else False
    ok, why = can_clear(h, "ACK", today=eng.session, flat=flat, recon_clean=eng.recon_clean(h.scope),
                        safe_mode=eng.ctx.safe_mode)
    if not ok:
        return "REJECTED", why, []
    eng.clear_halt(h, _who(row), str(note)[:300] if note is not None else None)
    return "DONE", why, []


def _ack_alert(eng: Engine, row: CommandRow) -> tuple[str, str, list[Command]]:
    key = row.args.get("key")
    if not isinstance(key, str) or not key:
        return "REJECTED", "ACK_ALERT needs args.key", []
    return "DONE", f"acknowledged {key}", [Alert("INFO", "ACK_ALERT", f"acknowledged by {_who(row)}", key)]


def _ack_foreign(eng: Engine, row: CommandRow) -> tuple[str, str, list[Command]]:
    a = row.args
    symbol, product = str(a.get("symbol", "")).strip().upper(), str(a.get("product", "")).strip().upper()
    if symbol and ":" not in symbol:
        symbol = f"NFO:{symbol}"
    try:
        qty = int(a.get("qty"))
        acct = Account(str(a.get("account", "LIVE")).upper())
    except (TypeError, ValueError):
        return "REJECTED", "ACK_FOREIGN needs symbol, product, an integer qty and a valid account", []
    if not symbol or not product:
        return "REJECTED", "ACK_FOREIGN needs symbol and product", []
    eng.store.ack_foreign(eng.session, acct, symbol, product, qty, _who(row), eng.now_wall)
    eng.event("UNKNOWN_POSITION", {"account": str(acct), "symbol": symbol, "product": product, "qty": qty,
                                   "acked_by": _who(row)}, "INFO", account=acct)
    exact, why = _foreign_all_acked(eng, acct)
    note = f"acked {acct} {symbol} {product} {qty}"
    for h in eng.active_halts("UNKNOWN_POSITION", f"ACCOUNT:{acct}"):
        ok, reason = can_clear(h, "ACK_FOREIGN", today=eng.session, foreign_exact=exact)
        if ok:
            eng.clear_halt(h, _who(row), note)
            return "DONE", f"{note}; UNKNOWN_POSITION cleared", []
        return "DONE", f"{note}; UNKNOWN_POSITION stays: {why or reason}", []
    return "DONE", note, []


def _foreign_all_acked(eng: Engine, acct: Account) -> tuple[bool, str]:
    """True when every position the bot does not explain, in the newest positions read, is acked exactly."""
    pos = eng.state.broker[acct].positions
    if pos is None:
        return False, "no positions read yet"
    acks = {(r["symbol"], r["product"]): int(r["qty"]) for r in eng.store.foreign_acks(eng.session, acct)}
    bot = {lg.inst.symbol for lg in eng.state.legs.values()
           if Account(lg.account) is acct and (not lg.is_terminal or lg.session == eng.session)}
    nets: dict[tuple[str, str], int] = {}
    for r in pos.rows:
        nets[(r.symbol, r.product)] = nets.get((r.symbol, r.product), 0) + int(r.net_qty)
    for key, net in sorted(nets.items()):
        if net == 0 or (key[0] in bot and key[1] == MIS):
            continue
        if acks.get(key) != net:
            return False, f"{key[0]} {key[1]} net {net} is not acked exactly"
    return True, "every foreign position acked exactly"


def _set_mode(eng: Engine, row: CommandRow) -> tuple[str, str, list[Command]]:
    a = row.args
    name, mode = a.get("strategy"), str(a.get("mode", "")).upper()
    if name not in eng.ctx.versions:
        return "REJECTED", f"unknown strategy {name!r}", []
    if mode not in MODES:
        return "REJECTED", f"mode must be one of {', '.join(MODES)}", []
    version = eng.ctx.versions[name]
    if a.get("version") not in (None, version):
        return "REJECTED", f"{name} runs version {version}, not {a.get('version')}", []
    cur = eng.state.modes.get(name)
    if cur is None:
        return "REJECTED", f"{name} has no mode row yet", []
    if mode == "OFF":
        eng.store.update_row("strategy_modes", {"strategy": name},
                             {"mode": "OFF", "pending_mode": None, "changed_ts": eng.now_wall,
                              "changed_by": _who(row)})
        eng.event("MODE_CHANGED", {"version": version, "from": cur["mode"], "to": "OFF", "by": _who(row)},
                  strategy=name)
        eng.flag_impaired(name, "mode OFF during the session")
        eng.reload_modes()
        return "DONE", f"{name} is OFF now (session marked IMPAIRED)", []
    values: dict[str, Any] = {"pending_mode": mode, "changed_ts": eng.now_wall, "changed_by": _who(row)}
    if mode == "LIVE":
        if eng.ctx.allow_live is not True:
            return "REJECTED", "ALLOW_LIVE=1 is not set for this process (key 1)", []
        acks = [h for h in eng.state.halts if h.scope == "GLOBAL" and h.clears is HaltClears.ACK]
        if acks:
            return "REJECTED", f"a GLOBAL ACK halt is active: {', '.join(h.kind for h in acks)}", []
        check = g1.verify_unlock(eng.store, name, version, a.get("confirm"), eng.now_wall)
        if not check.ok:
            return "REJECTED", f"unlock refused: {check.reason}", []
        values["live_unlock_report_id"] = check.cert_report_id
    eng.store.update_row("strategy_modes", {"strategy": name}, values)
    eng.event("MODE_CHANGED", {"version": version, "from": cur["mode"], "pending": mode, "by": _who(row),
                               "unlock_report_id": values.get("live_unlock_report_id")},
              "WARN" if mode == "LIVE" else "INFO", strategy=name)
    eng.reload_modes()
    return "DONE", f"{name}: {mode} takes effect at the next session", []


def _run_recon(eng: Engine, row: CommandRow) -> tuple[str, str, list[Command]]:
    cmds: list[Command] = []
    for acct in ACCOUNTS:
        cmds += eng.start_recon(acct, Priority.RECON)
    return "DONE", "recon requested for every account", cmds


_HANDLERS = {
    "PAUSE": _pause, "RESUME": _resume, "FLATTEN_ALL": _flatten_all, "KILL": _kill, "ACK_HALT": _ack_halt,
    "ACK_ALERT": _ack_alert, "ACK_FOREIGN": _ack_foreign, "SET_MODE": _set_mode, "RUN_RECON": _run_recon,
}


# ---------------------------------------------------------------------------------------------------------------
# Flat verification and the FLATTEN_ALL / KILL ack (§8.9 step 4)
# ---------------------------------------------------------------------------------------------------------------
def flat_status(eng: Engine) -> tuple[bool, str]:
    """(True, why) when every bot structure is closed and, per account, a post-fill positions read shows every
    symbol the bot traded today at zero (acked foreign quantity aside); else (False, what is missing)."""
    open_ = [s for s in eng.state.live_structures()]
    if open_:
        legs = [f"{s.book} {lg.inst.symbol} {lg.state}" for s in open_ for lg in eng.state.legs_of(s)]
        return False, f"{len(open_)} structure(s) not closed: {', '.join(legs) or 'reserved'}"
    for acct in ACCOUNTS:
        legs = [lg for lg in eng.state.legs.values() if Account(lg.account) is acct and lg.session == eng.session
                and lg.entry_filled > 0]
        if not legs:
            continue
        pos = eng.state.broker[acct].positions
        last = max((lg.last_fill_mono for lg in legs if lg.last_fill_mono is not None), default=None)
        if not post_fill(pos, last):
            return False, f"waiting for a post-fill {acct} positions read"
        acks = {(r["symbol"], r["product"]): int(r["qty"]) for r in eng.store.foreign_acks(eng.session, acct)}
        for sym in sorted({lg.inst.symbol for lg in legs}):
            net = net_qty(pos, sym) - acks.get((sym, MIS), 0)
            if net != 0:
                return False, f"{acct} {sym}: broker net {net}"
    return True, "verified flat"


def _ask_positions(eng: Engine) -> list[Command]:
    """A post-fill positions read for each account whose flat check waits for one."""
    out: list[Command] = []
    for acct in ACCOUNTS:
        legs = [lg for lg in eng.state.legs.values() if Account(lg.account) is acct and lg.session == eng.session
                and lg.entry_filled > 0]
        last = max((lg.last_fill_mono for lg in legs if lg.last_fill_mono is not None), default=None)
        if not legs or (last is not None and eng.now_mono < last + POSITIONS_POST_FILL_LAG_S):
            continue
        pos = eng.state.broker[acct].positions
        if post_fill(pos, last):
            continue
        if pos is not None and eng.now_mono - pos.req_mono < VERIFY_FLAT_RECHECK_S:
            continue
        out += eng.request_positions(acct, Priority.EXIT)
    return out


def is_stop_flatten(eng: Engine, h: Halt) -> bool:
    """h is the FLATTEN halt that a stop's own FLATTEN_ALL raised (ctx.stop_flatten_by: the laptop's Ctrl+C)."""
    who = eng.ctx.stop_flatten_by
    return bool(who) and h.kind == "FLATTEN" and h.scope == "GLOBAL" and h.reason == _halt_reason("FLATTEN_ALL", who)


def release_stop_flatten(eng: Engine, why: str) -> None:
    """The run ends (Shutdown, or the EOD timeline's DONE), so no entry can follow: the FLATTEN halt raised by the
    stop's own FLATTEN_ALL (the laptop runner's first Ctrl+C) is cleared once flat is verified, and the next start
    trades again. Any other FLATTEN halt, a PAUSE, or that halt while not flat, stays (RESUME clears it; the next
    start keeps exiting what is still open)."""
    for h in eng.active_halts("FLATTEN", "GLOBAL"):
        if is_stop_flatten(eng, h) and flat_status(eng)[0]:
            eng.clear_halt(h, str(eng.ctx.stop_flatten_by), f"{why}: flat verified, so the next start trades again")


def flatten_tick(eng: Engine) -> list[Command]:
    """Every Timer while a FLATTEN_ALL / KILL is pending: DONE once flat is verified, FAILED + CRITICAL at 60 s."""
    if not eng.state.flatten:
        return []
    flat, why = flat_status(eng)
    out: list[Command] = []
    for (source, cmd_id), tr in sorted(eng.state.flatten.items()):
        if flat:
            del eng.state.flatten[(source, cmd_id)]
            _ack(eng, source, cmd_id, tr.kind, "DONE", why)
        elif not tr.failed and eng.now_mono - tr.started_mono >= FLATTEN_FAIL_AFTER_S:
            tr.failed = True
            _ack(eng, source, cmd_id, tr.kind, "FAILED", f"not flat after {FLATTEN_FAIL_AFTER_S:.0f} s: {why}; "
                                                         "still trying")
            out.append(Alert("CRITICAL", "FLATTEN_FAILED", f"{tr.kind} ({source} #{cmd_id}) not flat after "
                             f"{FLATTEN_FAIL_AFTER_S:.0f} s: {why}. The bot keeps trying; check Kite.",
                             f"flatten_failed:{source}:{cmd_id}"))
    if not flat:
        out += _ask_positions(eng)
    return out
