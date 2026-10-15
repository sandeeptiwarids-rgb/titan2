"""Alerts (spec §12 "Alerts", DETAIL §12.6, §3.2-§3.3, §13.9): Telegram messages and the healthchecks.io dead-man.

Only the `alerts` thread does this network I/O. The core calls submit() / dispatch(), which never block on I/O: they
take a short lock, apply the delivery rules and append to a bounded queue (1,000 messages; when it is full the
oldest message is dropped, a non-CRITICAL one while any is queued, and counted in counters()["dropped"]). CRITICAL
messages are sent before the others, and Telegram sends are paced to one a second (a burst only earns HTTP 429s).

Delivery rules. The key is the alert's dedup_key, or its code when it has none (give per-object keys such as
"EXIT_DIFFICULTY:leg17" when distinct objects must each alert):
- the same (severity, key) is sent at most once per 5 min, and a WARN at most once per 30 min. A suppressed
  duplicate is not lost: it updates the text the next message for that key carries and is counted in it;
- a CRITICAL repeats every 5 min until it is acknowledged: ack(key), or an Alert command with code ACK_ALERT and
  dedup_key=<key> (`titan2 ack-alert <key>` -> commands_cli.db -> engine -> runtime dispatch). After an ack the same
  key stays quiet for the rest of its 5-min window, then a new occurrence alerts again;
- every accepted alert is also logged, so nothing depends on Telegram being up.

Undelivered messages (the outbox). A message that still fails after its tries because of the network (a lookup,
connect or timeout error, or HTTP 429/5xx: AlertError.retry) is kept, never dropped: up to OUTBOX_MAX (200) messages,
each for up to OUTBOX_MAX_AGE_S (2 h; past either limit the oldest go, CRITICAL last). While any is kept nothing
else is tried: the messages after it wait behind it, a CRITICAL's repeats pause (until the kept messages are all
out), and once every OUTBOX_RETRY_S (30 s) one ALERTS_DELAYED line ("network back at HH:MM:SS after N s; K alerts
delayed") is tried. Once it goes out, the kept messages follow, CRITICAL first, each with the time it was raised.
HTTP 429 is the service's rate limit, not an outage (AlertError.rate_limited): while the kept messages go out it
only makes them wait OUTBOX_RATE_LIMIT_S (10 s) and go on, with no new line, and a hold that began with one says
"rate limit (HTTP 429) over at ...". A message the service refused (another HTTP 4xx) is logged and dropped as
before. At stop() what is still undelivered, the message being sent at that moment included, goes to outbox_file
(<var>/run/alerts_outbox.json, OUTBOX_FILE), and the next start() sends it after one line saying so (2026-10-14
14:22: three alerts were lost in a 45 s DNS outage); the message being sent keeps its tries until the stop's
deadline.

Dead-man (HEALTHCHECK_URL; check schedule DEADMAN_SCHEDULE on the server, LAPTOP_DEADMAN_SCHEDULE on the laptop,
3-min grace): pinged at READY (ping_ready), then every 60 s from 08:45 IST for as long as the core runs, and only
while the core heartbeat is under 10 s old, so a stuck core stops the pings and healthchecks raises the alarm from
outside the host; one last ping as a graceful exit begins (ping_now). fail(text) pings /fail on a CRITICAL exit;
that ping is never gated. critical_now() pings /fail only for the bot's own failures: other units' failures are
Telegram only.

One-shot processes (the token unit, the self-check, `titan2 alert --critical`) use send_now() / critical_now().

Runtime use: heartbeat = Heartbeat(); alerter = Alerter.from_env(os.environ, heartbeat); alerter.start() (§9.5 step
5); every Alert command goes to alerter.dispatch(cmd); alerter.ping_ready() at READY; the core loop calls
heartbeat.core_progress(clock.mono()) after each Timer (the alerter's clock); status.json shows
alerter.unacked_critical(). A fatal exit: submit the CRITICAL, alerter.fail(reason), then alerter.stop(). Before the
thread exists (a §9.5 step 4 store failure) use critical_now(text).

Secrets: the Telegram bot token and the ping URL never appear in a log line or an error text (_redact), and a
transport error is raised without its original exception chained (a requests error's text carries the URL).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from titan2.clock import Clock, RealClock, iso, parse_wall

log = logging.getLogger(__name__)

INFO, WARN, CRITICAL = "INFO", "WARN", "CRITICAL"
SEVERITIES = (INFO, WARN, CRITICAL)            # == titan2.core.events.ALERT_SEVERITIES
ACK_CODE = "ACK_ALERT"                         # an Alert command with this code acknowledges its dedup_key

DEDUP_S = 300.0                                # INFO and CRITICAL: one message per (severity, key) per 5 min
WARN_DEDUP_S = 1800.0                          # WARN: one per key per 30 min
CRITICAL_REPEAT_S = 300.0                      # an unacknowledged CRITICAL repeats every 5 min
QUEUE_MAX = 1000
HEARTBEAT_MAX_AGE_S = 10.0                     # dead-man pings only while core progress is younger than this
PING_EVERY_S = 60.0
PING_RETRY_S = 10.0                            # after a failed ping (the check's grace is 3 min)
PING_FROM = dtime(8, 45)                       # IST: periodic pings from the schedule's first tick, for as long as
#                                                the core runs (its exit ends them; a ping after the last tick is
#                                                harmless: the next one expected is the next weekday's 08:45)
# The healthchecks.io check this bot can satisfy (Asia/Kolkata, DEADMAN_GRACE_MIN grace). The bot starts at 08:45
# (titan2-bot.timer) and pings until its EOD exit at BROKER_SQUAREOFF_AT + 14 min, i.e. 15:34 at the earliest
# allowed square-off (15:20) and 15:40 at the default (15:26), with one last ping as it exits; so the schedule ends
# at 15:33. On a weekday without a session the bot keeps pinging until then (runtime.BotProcess.keep_deadman):
# healthchecks cannot know NSE holidays. A check expecting pings later (the old `* 9-15`) goes DOWN every day.
DEADMAN_SCHEDULE = ("45-59 8 * * 1-5", "* 9-14 * * 1-5", "0-33 15 * * 1-5")
# The laptop's check (docs/LAPTOP.md section 2). It is started by hand between 08:40 and 09:10 and first pings at
# READY, a few minutes after the start, so the 08:45 tick above rang the phone for every start after about 08:46.
# It expects pings from 09:15 (a READY by 09:18, with the grace), and ends as the server's.
LAPTOP_DEADMAN_SCHEDULE = ("15-59 9 * * 1-5", "* 10-14 * * 1-5", "0-33 15 * * 1-5")
DEADMAN_LAST_TICK = dtime(15, 33)
DEADMAN_GRACE_MIN = 3
SEND_TRIES = 3
SEND_BACKOFF_S = (2.0, 5.0)
HTTP_TIMEOUT_S = 10.0
IDLE_WAKE_S = 1.0                              # the thread re-checks repeats and pings at least this often
TELEGRAM_MIN_INTERVAL_S = 1.0                  # Telegram's per-chat pace: a burst would only earn HTTP 429s
TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_MAX_CHARS = 4000                      # Telegram's limit is 4096
NTFY_SERVER = "https://ntfy.sh"                # ntfy: a free push-notification app; the topic name is the password
NTFY_MAX_CHARS = 3800                          # ntfy.sh's message limit is 4096 bytes
NTFY_TOPIC_RE = re.compile(r"[A-Za-z0-9_-]{20,64}")
NTFY_PRIORITY = {CRITICAL: ("5", "rotating_light"), WARN: ("4", "warning"), INFO: ("3", "chart_with_upwards_trend")}
PING_SIGNALS = ("", "fail", "log", "start")    # healthchecks: success, failure, log-only, start
PING_BODY_MAX = 10_000
OUTBOX_MAX = 200                               # undelivered messages kept for a resend (the oldest go past it)
OUTBOX_MAX_AGE_S = 7200.0                      # ... each for up to 2 hours
OUTBOX_RETRY_S = 30.0                          # while any is kept: one try of the ALERTS_DELAYED line this often
OUTBOX_RATE_LIMIT_S = 10.0                     # HTTP 429 while the kept ones go out: they wait this long, then go on
OUTBOX_FILE = "alerts_outbox.json"             # <var>/run/: undelivered at the stop, sent at the next start
DELAYED_CODE = "ALERTS_DELAYED"
_LEVEL = {INFO: logging.INFO, WARN: logging.WARNING, CRITICAL: logging.CRITICAL}


class AlertError(Exception):
    """A Telegram, ntfy or healthchecks call failed. The message never contains a secret. retry: a later try can
    deliver it (the network: a lookup, connect or timeout error; or HTTP 429 or 5xx); False when the service refused
    this message (another HTTP status), which no retry changes. rate_limited: HTTP 429, the service is up and asks
    for fewer messages (no network outage)."""

    def __init__(self, message: str, retry: bool = True, rate_limited: bool = False) -> None:
        super().__init__(message)
        self.retry = retry
        self.rate_limited = rate_limited


def _retryable(status: Any) -> bool:
    """An HTTP status a later try can get past: rate limited (429) or the service failing (5xx)."""
    return isinstance(status, int) and (status == 429 or status >= 500)


def _redact(text: str, *secrets: str | None) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def _default_post(url: str, **kwargs: Any) -> Any:
    import requests                            # lazy: only the alerts thread and one-shot senders post

    return requests.post(url, **kwargs)


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


# ---------------------------------------------------------------------------------------------------------------
# Core heartbeat (§3.3)
# ---------------------------------------------------------------------------------------------------------------
class Heartbeat:
    """Core progress. The core loop calls core_progress(mono) after every Timer; the alerts thread gates the dead-man
    ping on it and the runtime gates the systemd watchdog on it. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mono: float | None = None

    def core_progress(self, mono: float) -> None:
        with self._lock:
            self._mono = float(mono)

    def age(self, now_mono: float) -> float | None:
        """Seconds since the last core progress, or None if the core has not progressed yet."""
        with self._lock:
            last = self._mono
        return None if last is None else max(0.0, now_mono - last)

    def fresh(self, now_mono: float, max_age_s: float = HEARTBEAT_MAX_AGE_S) -> bool:
        age = self.age(now_mono)
        return age is not None and age < max_age_s


# ---------------------------------------------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------------------------------------------
class Telegram:
    """sendMessage to one chat (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID). post(url, data=, timeout=) is injectable."""

    def __init__(self, bot_token: str, chat_id: str, *, post: Callable[..., Any] | None = None,
                 timeout_s: float = HTTP_TIMEOUT_S) -> None:
        token = bot_token.strip() if isinstance(bot_token, str) else ""
        chat = chat_id.strip() if isinstance(chat_id, str) else ""
        if not token or not chat or any(c.isspace() or c == "/" for c in token):
            raise ValueError("Telegram needs a TELEGRAM_BOT_TOKEN without spaces or '/' and a TELEGRAM_CHAT_ID")
        self._token = token
        self._chat = chat
        self._post = post or _default_post
        self._timeout = timeout_s

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> Telegram | None:
        """None when either variable is unset; ValueError when one is set but malformed."""
        e = _env(env)
        token, chat = e.get("TELEGRAM_BOT_TOKEN", "").strip(), e.get("TELEGRAM_CHAT_ID", "").strip()
        if not token and not chat:
            return None
        return cls(token, chat, **kwargs)

    def send(self, text: str) -> None:
        url = f"{TELEGRAM_API}/bot{self._token}/sendMessage"
        try:
            resp = self._post(url, data={"chat_id": self._chat, "text": text[:TELEGRAM_MAX_CHARS],
                                         "disable_web_page_preview": "true"}, timeout=self._timeout)
            status = resp.status_code
            ok = status == 200 and resp.json().get("ok") is True
        except Exception as exc:               # requests errors carry the URL, which carries the bot token
            raise AlertError(_redact(f"telegram: {type(exc).__name__}: {exc}", self._token)) from None
        if not ok:
            raise AlertError(f"telegram: HTTP {status}", retry=_retryable(status), rate_limited=status == 429)

    def __repr__(self) -> str:
        return "Telegram(<configured>)"


class Ntfy:
    """One push notification per alert to an ntfy topic (NTFY_TOPIC; NTFY_SERVER, default https://ntfy.sh). The phone app
    subscribes to the topic. On ntfy.sh anyone who knows a topic can read it, so the topic must be a long random name
    (20-64 letters, digits, '-' or '_'), and it never appears in a log line or an error text (_redact). The alert's
    first line becomes the title; CRITICAL rings as urgent, WARN as high, INFO as normal. post(url, data=, headers=,
    timeout=) is injectable."""

    def __init__(self, topic: str, server: str = NTFY_SERVER, *, post: Callable[..., Any] | None = None,
                 timeout_s: float = HTTP_TIMEOUT_S) -> None:
        t = topic.strip() if isinstance(topic, str) else ""
        srv = (server or NTFY_SERVER).strip().rstrip("/")
        if not NTFY_TOPIC_RE.fullmatch(t):
            raise ValueError("ntfy needs an NTFY_TOPIC of 20-64 letters, digits, '-' or '_' (a long random name)")
        if not srv.startswith("https://"):
            raise ValueError("ntfy needs an https:// NTFY_SERVER")
        self._topic, self._server = t, srv
        self._post = post or _default_post
        self._timeout = timeout_s

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> Ntfy | None:
        """None when NTFY_TOPIC is unset; ValueError when it is malformed."""
        e = _env(env)
        topic = e.get("NTFY_TOPIC", "").strip()
        if not topic:
            return None
        return cls(topic, e.get("NTFY_SERVER", "").strip() or NTFY_SERVER, **kwargs)

    def send(self, text: str) -> None:
        first = text.split("\n", 1)[0]
        severity = next((s for s in (CRITICAL, WARN, INFO) if f" {s} " in f" {first} "), INFO)
        priority, tag = NTFY_PRIORITY[severity]
        title = first.removeprefix("[titan2] ").strip() or "titan2"
        headers = {"Title": ("titan2 " + title)[:200].encode("ascii", "replace").decode("ascii"),
                   "Priority": priority, "Tags": tag}
        try:
            resp = self._post(f"{self._server}/{self._topic}", data=text[:NTFY_MAX_CHARS].encode("utf-8"),
                              headers=headers, timeout=self._timeout)
            status = resp.status_code
        except Exception as exc:               # requests errors carry the URL, which carries the topic
            raise AlertError(_redact(f"ntfy: {type(exc).__name__}: {exc}", self._topic)) from None
        if status != 200:
            raise AlertError(f"ntfy: HTTP {status}", retry=_retryable(status), rate_limited=status == 429)

    def __repr__(self) -> str:
        return "Ntfy(<configured>)"


class Fanout:
    """Several message channels as one: send() delivers to each and fails only when all of them failed (worth a
    retry when any of them is; a rate limit only when every one worth a retry is rate limited)."""

    def __init__(self, channels: list[Any]) -> None:
        self.channels = [c for c in channels if c is not None]

    def send(self, text: str) -> None:
        errors: list[AlertError] = []
        for c in self.channels:
            try:
                c.send(text)
            except AlertError as exc:
                errors.append(exc)
        if errors and len(errors) == len(self.channels):
            again = [e for e in errors if e.retry]
            raise AlertError("; ".join(str(e) for e in errors), retry=bool(again),
                             rate_limited=bool(again) and all(e.rate_limited for e in again))
        for e in errors:
            log.warning("alert channel failed (another delivered it): %s", e)

    def __repr__(self) -> str:
        return f"Fanout({self.channels!r})"


def message_channel(env: Mapping[str, str] | None = None) -> Any:
    """The configured message channels as one sender: Telegram, ntfy, both (Fanout) or None. A malformed one is logged
    and left off, like a missing one."""
    found = []
    for transport in (Telegram, Ntfy):
        try:
            found.append(transport.from_env(env))
        except ValueError as exc:
            log.error("alerts: %s; that channel is disabled", exc)
    found = [c for c in found if c is not None]
    return None if not found else found[0] if len(found) == 1 else Fanout(found)


class Healthcheck:
    """healthchecks.io ping URL (HEALTHCHECK_URL). The URL is a credential: anyone holding it can ping the check."""

    def __init__(self, url: str, *, post: Callable[..., Any] | None = None, timeout_s: float = HTTP_TIMEOUT_S) -> None:
        u = url.strip().rstrip("/") if isinstance(url, str) else ""
        if not u.startswith("https://") or any(c.isspace() for c in u) or u.count("/") < 3:
            raise ValueError("HEALTHCHECK_URL must be an https:// ping URL")
        self._url = u
        path = u.split("://", 1)[1].partition("/")[2]                # "<uuid>" or "<ping-key>/<slug>"
        self._secrets = (u, path, u.rsplit("/", 1)[-1])               # longest first: a requests error shows the path
        self._post = post or _default_post
        self._timeout = timeout_s

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> Healthcheck | None:
        url = _env(env).get("HEALTHCHECK_URL", "").strip()
        return cls(url, **kwargs) if url else None

    def ping(self, signal: str = "", body: str = "") -> None:
        if signal not in PING_SIGNALS:
            raise ValueError(f"unknown healthchecks signal {signal!r}")
        target = f"{self._url}/{signal}" if signal else self._url
        what = f"healthchecks {signal or 'ping'}"
        try:
            resp = self._post(target, data=body.encode("utf-8")[:PING_BODY_MAX], timeout=self._timeout)
            status = resp.status_code
        except Exception as exc:
            raise AlertError(_redact(f"{what}: {type(exc).__name__}: {exc}", *self._secrets)) from None
        if not 200 <= status < 300:
            raise AlertError(f"{what}: HTTP {status}")

    def __repr__(self) -> str:
        return "Healthcheck(<configured>)"


def format_alert(severity: str, code: str, text: str, *, now_wall: datetime | None = None, repeat: int = 0,
                 more: int = 0, ack_key: str | None = None) -> str:
    """The Telegram text: plain text (no parse mode, so nothing in it is interpreted as markup)."""
    head = f"[titan2] {severity} {code}"
    if repeat:
        head += f" (repeat {repeat})"
    if now_wall is not None:
        head += f" {now_wall:%H:%M:%S}"
    lines = [head, str(text).strip()]
    if more:
        lines.append(f"(+{more} more like this since the last message)")
    if ack_key:
        lines.append(f"ack: titan2 ack-alert {ack_key}")
    return "\n".join(lines)


def _deliver(telegram: Any, msg: str, sleep: Callable[[float], Any], tries: int = SEND_TRIES,
             give_up: Callable[[], bool] | None = None) -> AlertError | None:
    """None when delivered, else the last error: `tries` tries, SEND_BACKOFF_S apart, while a retry can help and,
    with `give_up`, until it says no further try is wanted (the alerter's stop deadline has passed: stop() keeps the
    message for the next start)."""
    last = AlertError("not attempted")
    for attempt in range(1, tries + 1):
        try:
            telegram.send(msg)
            return None
        except AlertError as exc:
            last = exc
        except Exception as exc:               # an injected transport: still never let it kill the caller
            last = AlertError(f"telegram: {type(exc).__name__}")
        if not last.retry:                     # the service refused this message: another try changes nothing
            break
        if attempt < tries:
            sleep(SEND_BACKOFF_S[min(attempt - 1, len(SEND_BACKOFF_S) - 1)])
            if give_up is not None and give_up():
                break
    return last


def _send_with_retry(telegram: Telegram, msg: str, sleep: Callable[[float], Any],
                     tries: int = SEND_TRIES) -> str | None:
    """None when delivered, else the last error text."""
    err = _deliver(telegram, msg, sleep, tries)
    return None if err is None else str(err)


# ---------------------------------------------------------------------------------------------------------------
# The outbox file: undelivered messages kept from one run to the next
# ---------------------------------------------------------------------------------------------------------------
def outbox_path(var_dir: str | Path) -> Path:
    """<var>/run/alerts_outbox.json: the messages a run could not deliver, sent by the next start's alerter."""
    return Path(var_dir) / "run" / OUTBOX_FILE


def _read_outbox(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("alerts") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("no 'alerts' list")
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("text"), str) and r["text"].strip()]


def save_undelivered(path: str | Path, messages: Iterable[tuple[str, bool]], *, saved: datetime) -> int:
    """Add undelivered messages, (text, critical) each, to the outbox file (written whole, then renamed: never half
    a file); what the file already holds is kept, oldest first, the newest OUTBOX_MAX in all. Returns how many it
    holds. An unreadable old file is set aside as <file>.bad, never thrown away unread."""
    p = Path(path)
    new = [{"text": str(t), "critical": bool(c), "saved": iso(saved)} for t, c in messages]
    if not new:
        return 0
    rows: list[dict[str, Any]] = []
    if p.is_file():
        try:
            rows = _read_outbox(p)
        except (OSError, ValueError) as exc:
            log.error("alerts: %s is not readable (%s); kept as %s.bad", p, exc, p.name)
            try:
                os.replace(p, p.with_name(p.name + ".bad"))
            except OSError:
                log.exception("alerts: cannot set %s aside", p)
    rows = (rows + new)[-OUTBOX_MAX:]
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"version": 1, "alerts": rows}, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return len(rows)


def take_undelivered(path: str | Path) -> tuple[list[tuple[str, bool]], datetime | None]:
    """The outbox file's messages, (text, critical) each, and when the newest was saved; the file is removed, so
    nothing is sent twice. ([], None) without a file; an unreadable one is set aside as <file>.bad."""
    p = Path(path)
    if not p.is_file():
        return [], None
    try:
        rows = _read_outbox(p)
    except (OSError, ValueError) as exc:
        log.error("alerts: %s is not readable (%s); kept as %s.bad", p, exc, p.name)
        try:
            os.replace(p, p.with_name(p.name + ".bad"))
        except OSError:
            log.exception("alerts: cannot set %s aside", p)
        return [], None
    saved = None
    for r in rows:
        try:
            when = parse_wall(str(r.get("saved")))
        except (ValueError, TypeError, IndexError):
            continue
        saved = when if saved is None or when > saved else saved
    try:
        p.unlink()
    except OSError as exc:                     # sent at every start would be worse than not sent: leave it, say so
        log.error("alerts: cannot remove %s (%s): its %d message(s) are not resent; they are in it", p, exc, len(rows))
        return [], None
    return [(r["text"], r.get("critical") is True) for r in rows], saved


# ---------------------------------------------------------------------------------------------------------------
# The alerts thread
# ---------------------------------------------------------------------------------------------------------------
@dataclass(slots=True)
class _Rec:
    severity: str
    key: str
    code: str
    text: str
    last_queued: float                         # mono of the last message queued for this (severity, key)
    active: bool                               # a CRITICAL not yet acknowledged: it repeats
    more: int = 0                              # duplicates suppressed since the last message
    repeats: int = 0


@dataclass(slots=True)
class _Held:
    """An undelivered message kept for a resend (the outbox)."""

    text: str
    critical: bool
    since: float | None                        # mono when it was kept; None: the previous run's (it never ages out)
    previous: bool = False


def _count(n: int, what: str) -> str:
    return f"{n} {what}" + ("" if n == 1 else "s")


def _late_log(text: str) -> None:
    """A message that went out after stop() had kept it for the next start: that start sends it once more."""
    log.warning("alerts: delivered after the stop had kept it for the next start, which sends it once more: %s",
                text.replace("\n", " | "))


class Alerter:
    """The `alerts` thread (§3.2). Build it, start() it, call submit()/dispatch() from any thread; step() is the unit
    of work (tests call it inline with a FakeClock and fake transports instead of starting the thread). outbox_file:
    where stop() keeps the messages still undelivered, and start() takes the previous run's (outbox_path)."""

    def __init__(self, telegram: Telegram | None, healthcheck: Healthcheck | None,
                 heartbeat: Heartbeat | None = None, *, clock: Clock | None = None, queue_max: int = QUEUE_MAX,
                 sleep: Callable[[float], Any] | None = None, outbox_file: str | Path | None = None) -> None:
        if not (isinstance(queue_max, int) and queue_max >= 1):
            raise ValueError(f"queue_max must be an int >= 1, got {queue_max!r}")
        self.telegram = telegram
        self.healthcheck = healthcheck
        self.heartbeat = heartbeat if heartbeat is not None else Heartbeat()
        self._clock = clock if clock is not None else RealClock()
        self._max = queue_max
        self._cv = threading.Condition()
        self._crit: deque[str] = deque()        # CRITICAL messages: sent first, dropped last
        self._rest: deque[str] = deque()
        self._last_send: float | None = None
        self._recs: dict[tuple[str, str], _Rec] = {}
        self._ready_ping = False
        self._fail_text: str | None = None
        self._last_ping: float | None = None
        self._last_ping_ok = True
        self._stopping = False
        self._stop_deadline = 0.0
        self._wake = threading.Event()          # set by stop(): interrupts retry back-off
        self._sleep = sleep if sleep is not None else self._wake.wait
        self._thread: threading.Thread | None = None
        self._warned_no_telegram = False
        # the outbox (module docstring): kept messages, oldest first, and where the outage stands
        self._outbox: deque[_Held] = deque()
        self._outbox_file = Path(outbox_file) if outbox_file is not None else None
        self._holding = False                  # the network is down: only the ALERTS_DELAYED line is tried
        self._draining = False                 # that line went out: the kept messages follow, one a step
        self._down_since: float | None = None  # mono of the failed send (or line) that began the outage
        self._limited = False                  # ... and so far only the service's rate limit (HTTP 429)
        self._next_retry: float | None = None  # the line's next try (None: at the next step); while the kept
        #                                        messages go out, the end of a rate limit's wait
        self._inflight: tuple[str, bool, bool] | None = None   # (text, critical, kept before) of the message the
        #                                        thread is sending now: stop() saves it too
        self._lost = 0                         # kept messages dropped (cap or age) since the last line
        self._previous_saved: datetime | None = None    # when the previous run saved what it could not send
        self._stop_tried = False               # stopping: the one last try was made
        self._n = {"accepted": 0, "suppressed": 0, "dropped": 0, "sent": 0, "send_failed": 0, "pings": 0,
                   "ping_failed": 0, "acks": 0, "held": 0, "resent": 0, "lost": 0}

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, heartbeat: Heartbeat | None = None,
                 **kwargs: Any) -> Alerter:
        """The bot's alerter. A malformed TELEGRAM_* / HEALTHCHECK_URL is logged and that channel is left off: the
        bot must still start, the morning self-check reports it, and a missing dead-man ping alarms by itself."""
        try:
            hc = Healthcheck.from_env(env)
        except ValueError as exc:
            log.error("alerts: %s; that channel is disabled", exc)
            hc = None
        return cls(message_channel(env), hc, heartbeat, **kwargs)

    # -- producers (any thread; never block on I/O) -----------------------------------------------------------
    def submit(self, severity: str, code: str, text: str, dedup_key: str | None = None) -> bool:
        """Accept an alert. False when it was suppressed as a duplicate (it still updates the next message)."""
        if severity not in SEVERITIES:
            text, severity = f"(unknown severity {severity!r}) {text}", CRITICAL     # fail loud, not silent
        code = str(code or "ALERT")
        text = str(text)
        key = str(dedup_key) if dedup_key else code
        now = self._clock.mono()
        with self._cv:
            rec = self._recs.get((severity, key))
            window = WARN_DEDUP_S if severity == WARN else DEDUP_S
            if rec is not None and (rec.active or now - rec.last_queued < window):
                rec.code, rec.text = code, text
                rec.more += 1
                self._n["suppressed"] += 1
                return False
            carried = rec.more if rec is not None else 0            # duplicates from the window that just ended
            rec = _Rec(severity, key, code, text, now, active=severity == CRITICAL, more=carried)
            self._recs[(severity, key)] = rec
            self._n["accepted"] += 1
            self._enqueue(rec)
        log.log(_LEVEL[severity], "alert %s %s [%s]: %s", severity, code, key, text)
        return True

    def dispatch(self, cmd: Any) -> bool:
        """Runtime dispatch of a titan2.core.events.Alert command. Code ACK_ALERT acknowledges cmd.dedup_key."""
        if cmd.code == ACK_CODE:
            return self.ack(cmd.dedup_key or cmd.text)
        return self.submit(cmd.severity, cmd.code, cmd.text, cmd.dedup_key)

    def ack(self, key: str) -> bool:
        """Stop the repeats of the CRITICAL alert with this key. False when no such alert was waiting."""
        with self._cv:
            hits = [r for r in self._recs.values() if r.key == key and r.active]
            for r in hits:
                r.active = False
            if hits:
                self._n["acks"] += 1
        if hits:
            log.warning("alert %s acknowledged", key)
        return bool(hits)

    def ping_ready(self) -> None:
        """Ping the dead-man as soon as the core heartbeat is fresh (READY, §9.5)."""
        self.ping_now()

    def ping_now(self) -> None:
        """Ping the dead-man as soon as the core heartbeat is fresh, outside the 60 s rhythm: at READY, and as a
        graceful exit begins (stop() then delivers it), so the day's last ping comes after the check's last tick."""
        with self._cv:
            self._ready_ping = True
            self._cv.notify()

    def fail(self, text: str) -> None:
        """Ping healthchecks /fail (a CRITICAL exit). Not gated on the heartbeat. Then call stop() to flush."""
        with self._cv:
            self._fail_text = str(text) or "titan2 critical exit"
            self._cv.notify()

    # -- reads (any thread) ------------------------------------------------------------------------------------
    def unacked_critical(self) -> int:
        """CRITICAL alerts still repeating (status.json "unacked_critical")."""
        with self._cv:
            return sum(1 for r in self._recs.values() if r.active)

    def active_keys(self) -> list[str]:
        with self._cv:
            return sorted({r.key for r in self._recs.values() if r.active})

    def queued(self) -> int:
        with self._cv:
            return len(self._crit) + len(self._rest)

    def held(self) -> int:
        """Undelivered messages kept for a resend (the outbox)."""
        with self._cv:
            return len(self._outbox)

    @property
    def running(self) -> bool:
        """The alerts thread runs and is not stopping: what submit() accepts now is sent, or kept for the next start.
        After stop() a submit would reach neither (the laptop's power watch waits for the next run's alerter)."""
        with self._cv:
            return self._thread is not None and self._thread.is_alive() and not self._stopping

    def counters(self) -> dict[str, int]:
        with self._cv:
            return dict(self._n)

    def restore_leftovers(self) -> int:
        """Take what the previous run could not deliver (outbox_file) into the outbox, ahead of everything this run
        sends; start() does it. Without a message channel the file is left for a run that has one. Returns how
        many were taken."""
        if self._outbox_file is None or self.telegram is None:
            return 0
        msgs, saved = take_undelivered(self._outbox_file)
        if not msgs:
            return 0
        with self._cv:
            for text, critical in msgs:
                self._outbox.append(_Held(text, critical, None, previous=True))
            self._holding, self._draining, self._next_retry = True, False, None
            self._previous_saved = saved
            self._n["held"] += len(msgs)
            self._cv.notify()
        log.warning("alerts: %s the previous run could not deliver go out first", _count(len(msgs), "alert"))
        return len(msgs)

    # -- the work ----------------------------------------------------------------------------------------------
    def _enqueue(self, rec: _Rec) -> None:
        """Caller holds the lock."""
        msg = format_alert(rec.severity, rec.code, rec.text, now_wall=self._clock.now_wall(), repeat=rec.repeats,
                           more=rec.more, ack_key=rec.key if rec.severity == CRITICAL else None)
        rec.more = 0
        if len(self._crit) + len(self._rest) >= self._max:
            (self._rest or self._crit).popleft()                     # the oldest, sparing CRITICAL while it can
            self._n["dropped"] += 1
        (self._crit if rec.severity == CRITICAL else self._rest).append(msg)
        self._cv.notify()

    def _due_repeats(self, now: float) -> None:
        """Caller holds the lock: queue CRITICAL repeats and forget quiet keys. While messages are kept, in an outage
        and until the kept ones are all out, a repeat is skipped (its first message is kept, or was delivered
        before), so the phone gets no burst of repeats when it is over. Until review round 6 the repeats came back
        as soon as the kept ones began to go out, where a rate limit (HTTP 429) can now keep them waiting for an hour
        (_back_off): each repeat was then kept as one more CRITICAL, and they all came at once when it ended."""
        for k, rec in list(self._recs.items()):
            if rec.active and now - rec.last_queued >= CRITICAL_REPEAT_S:
                rec.last_queued = now
                if self._holding or self._draining:
                    continue
                rec.repeats += 1
                self._enqueue(rec)
            elif not rec.active and now - rec.last_queued >= WARN_DEDUP_S:
                del self._recs[k]

    def _ping_due(self, now: float) -> bool:
        """Caller holds the lock. True claims the ping (so a failed one is retried after PING_RETRY_S)."""
        if self.healthcheck is None or not self.heartbeat.fresh(now):
            return False
        if self._ready_ping:
            self._ready_ping = False
        else:
            if self._clock.now_wall().time() < PING_FROM:
                return False
            every = PING_EVERY_S if self._last_ping_ok else PING_RETRY_S
            if self._last_ping is not None and now - self._last_ping < every:
                return False
        self._last_ping = now
        return True

    def step(self) -> list[str]:
        """Do the due work: the /fail ping, a dead-man ping, then one message: a queued one; or, while messages are
        kept (an outage), the ALERTS_DELAYED line or one kept message. Returns what was attempted: any of "fail",
        "ping", "telegram", "log_only", "outbox"."""
        now = self._clock.mono()
        with self._cv:
            self._due_repeats(now)
            fail_text, self._fail_text = self._fail_text, None
            ping = self._ping_due(now)
            job = self._next_job(now)
            if job is not None and job[0] == "send":         # taken from the queue: in flight until its result
                self._inflight = (job[1][0], job[1][1], False)
            elif job is not None and job[0] == "resend":
                self._inflight = (job[1].text, job[1].critical, True)
        done = []
        if fail_text is not None and self.healthcheck is not None:
            done.append("fail")
            self._ping("fail", fail_text, tries=1 if self._stopping else SEND_TRIES)
        if ping:
            done.append("ping")
            self._last_ping_ok = self._ping("", "", tries=1)
        if job is not None:
            kind, arg = job
            if kind == "send":
                done.append(self._send(*arg))
            elif kind == "probe":
                done.append(self._probe(now))
            else:
                done.append(self._resend(arg))
        return done

    def _next_job(self, now: float) -> tuple[str, Any] | None:
        """Caller holds the lock. This step's message: ("send", (text, critical)) a queued one; ("probe", None) the
        ALERTS_DELAYED line; ("resend", _Held) a kept one; None when nothing is due."""
        if self._draining:
            if self._waiting(now):                               # a rate limit (HTTP 429): wait, then go on
                return None
            if self._crit:                                       # a new CRITICAL still goes before the backlog
                return "send", (self._crit.popleft(), True)
            self._expire_held(now)
            if self._outbox:
                while self._rest:                                # newer than the backlog: after it
                    self._hold(_Held(self._rest.popleft(), False, now))
                return "resend", self._take_held()
            self._end_outage()
        if not self._holding:
            if self._crit:
                return "send", (self._crit.popleft(), True)
            if self._rest:
                return "send", (self._rest.popleft(), False)
            return None
        # an outage: everything waits behind what is kept, and only the ALERTS_DELAYED line is tried
        self._expire_held(now)
        for q, critical in ((self._crit, True), (self._rest, False)):
            while q:
                self._hold(_Held(q.popleft(), critical, now))
        if self._stopping:
            return None if self._stop_tried else ("probe", None)
        if self._next_retry is not None and now < self._next_retry:
            return None
        return "probe", None

    def _hold(self, held: _Held) -> None:
        """Caller holds the lock: keep an undelivered message; past OUTBOX_MAX the oldest goes, a CRITICAL last."""
        self._outbox.append(held)
        self._n["held"] += 1
        while len(self._outbox) > OUTBOX_MAX:
            victim = next((h for h in self._outbox if not h.critical), self._outbox[0])
            self._outbox.remove(victim)
            self._lose(victim, f"more than {OUTBOX_MAX} kept")

    def _expire_held(self, now: float) -> None:
        """Caller holds the lock: drop the kept messages older than OUTBOX_MAX_AGE_S (the previous run's stay)."""
        for h in [h for h in self._outbox if h.since is not None and now - h.since > OUTBOX_MAX_AGE_S]:
            self._outbox.remove(h)
            self._lose(h, f"older than {OUTBOX_MAX_AGE_S / 3600:.0f} h")

    def _lose(self, held: _Held, why: str) -> None:
        """Caller holds the lock: a kept message given up (the next ALERTS_DELAYED line counts it)."""
        self._lost += 1
        self._n["lost"] += 1
        log.error("alert dropped undelivered (%s); it is in this log: %s", why, held.text.replace("\n", " | "))

    def _take_held(self) -> _Held:
        """Caller holds the lock: the next kept message to resend, a CRITICAL first."""
        for h in self._outbox:
            if h.critical:
                self._outbox.remove(h)
                return h
        return self._outbox.popleft()

    def _note_down(self, at: float, rate_limited: bool) -> None:
        """Caller holds the lock: when the outage began, the first failed send or try of the line (a start that took
        the previous run's messages while still offline has no failed send of its own), and whether it is only the
        service's rate limit so far."""
        if self._down_since is None:
            self._down_since, self._limited = at, rate_limited
        elif not rate_limited:
            self._limited = False

    def _start_holding(self, at: float, rate_limited: bool = False) -> None:
        """Caller holds the lock: a send failed for the network (or its rate limit): hold everything, try the line in
        OUTBOX_RETRY_S."""
        self._note_down(at, rate_limited)
        self._holding, self._draining = True, False
        self._next_retry = self._clock.mono() + OUTBOX_RETRY_S
        if self._stopping:
            self._stop_tried = True

    def _back_off(self) -> None:
        """Caller holds the lock: HTTP 429 while the kept messages go out: the network is up, so no new outage and no
        new ALERTS_DELAYED line; they wait OUTBOX_RATE_LIMIT_S and go on."""
        self._next_retry = self._clock.mono() + OUTBOX_RATE_LIMIT_S

    def _waiting(self, now: float) -> bool:
        """Caller holds the lock: the kept messages wait for a rate limit (_back_off)."""
        return self._draining and self._next_retry is not None and now < self._next_retry

    def _end_outage(self) -> None:
        """Caller holds the lock: everything kept is delivered (or given up): back to sending as messages come."""
        self._holding = self._draining = self._limited = False
        self._down_since = self._next_retry = self._previous_saved = None
        self._lost = 0

    def _delayed_text(self, now: float, now_wall: datetime) -> str:
        """Caller holds the lock: the ALERTS_DELAYED line, sent before the kept messages."""
        n = len(self._outbox)
        prev = sum(1 for h in self._outbox if h.previous)
        if self._down_since is not None:
            what = "rate limit (HTTP 429) over" if self._limited else "network back"
            saved = f", saved {self._previous_saved:%Y-%m-%d %H:%M}" if self._previous_saved is not None else ""
            parts = [f"{what} at {now_wall:%H:%M:%S} after {max(0.0, now - self._down_since):.0f} s; "
                     + (f"{_count(n, 'alert')} delayed" if n else "no alert waiting")
                     + (f" ({prev} of them from the previous run{saved})" if prev else "")]
        elif prev:
            saved = f" (saved {self._previous_saved:%Y-%m-%d %H:%M})" if self._previous_saved is not None else ""
            parts = [f"{_count(prev, 'alert')} from the previous run{saved} {'was' if prev == 1 else 'were'} not "
                     "delivered before it stopped"]
        else:
            parts = [f"{_count(n, 'alert')} delayed"]
        if self._lost:
            parts.append(f"{self._lost} not delivered (more than {OUTBOX_MAX} kept, or older than "
                         f"{OUTBOX_MAX_AGE_S / 3600:.0f} h): see the log")
        return "; ".join(parts) + (". They follow, each with the time it was raised" if n else "")

    def _transmit(self, msg: str, tries: int) -> AlertError | None:
        """Send one message, paced to TELEGRAM_MIN_INTERVAL_S: None when delivered, else its error."""
        if self._last_send is not None and not self._stopping:
            wait = self._last_send + TELEGRAM_MIN_INTERVAL_S - self._clock.mono()
            if wait > 0:
                self._sleep(wait)
        err = _deliver(self.telegram, msg, self._sleep, tries=tries, give_up=self._past_stop_deadline)
        self._last_send = self._clock.mono()
        return err

    def _past_stop_deadline(self) -> bool:
        """No further try of the message in flight: stop()'s deadline has passed, and stop() keeps it for the next
        start. Until then a retry still goes, at once (stop() cuts the back-off short): review round 6 found that
        ending the tries as soon as stop() was called sent the quick retry of a message whose first try had just
        failed (a connection reset) to the next start, on the laptop the next morning."""
        with self._cv:
            return self._stopping and time.monotonic() > self._stop_deadline

    def _probe(self, now: float) -> str:
        """One try of the ALERTS_DELAYED line. Delivered: the network is back and the kept messages follow, one a
        step; else the next try is in OUTBOX_RETRY_S."""
        with self._cv:
            text = self._delayed_text(now, self._clock.now_wall())
            held = len(self._outbox)
        err = self._transmit(format_alert(INFO, DELAYED_CODE, text, now_wall=self._clock.now_wall()), tries=1)
        with self._cv:
            if err is None:
                self._n["sent"] += 1
                self._holding, self._draining, self._limited = False, True, False
                self._down_since, self._lost, self._previous_saved, self._next_retry = None, 0, None, None
            else:
                self._note_down(now, err.rate_limited)
                self._next_retry = self._clock.mono() + OUTBOX_RETRY_S
                if self._stopping:
                    self._stop_tried = True
        if err is None:
            log.warning("alerts: %s", text)
        else:
            log.info("alerts: still not delivered (%s); %s kept, the next try in %.0f s", err, _count(held, "alert"),
                     OUTBOX_RETRY_S)
        return "outbox"

    def _resend(self, held: _Held) -> str:
        """One try of a kept message, after the ALERTS_DELAYED line went out. A rate limit (HTTP 429) only makes the
        rest wait (_back_off); a network failure holds everything again."""
        err = self._transmit(held.text, tries=1)
        with self._cv:
            saved = self._inflight is None                       # stop() kept it for the next start meanwhile
            self._inflight = None
            if err is None:
                self._n["sent"] += 1
                self._n["resent"] += 1
            elif saved:
                pass                                             # its late result is dropped: the file has it
            elif err.retry:
                self._outbox.appendleft(held)                    # still the next one
                if err.rate_limited:
                    self._back_off()
                else:
                    self._start_holding(self._clock.mono())
            else:
                self._lose(held, f"refused: {err}")
            kept = len(self._outbox)
        if err is None and saved:
            _late_log(held.text)
        elif err is not None and err.retry and not saved and err.rate_limited:
            log.info("alerts: the alert service is limiting the rate (%s): the delayed alerts go on in %.0f s; %s kept",
                     err, OUTBOX_RATE_LIMIT_S, _count(kept, "alert"))
        elif err is not None and err.retry and not saved:
            log.warning("alerts: the network dropped again while sending the delayed alerts (%s); %s kept", err,
                        _count(kept, "alert"))
        return "outbox"

    def _ping(self, signal: str, body: str, tries: int) -> bool:
        last = ""
        for attempt in range(1, tries + 1):
            try:
                self.healthcheck.ping(signal, body)                  # type: ignore[union-attr]
                with self._cv:
                    self._n["pings"] += 1
                return True
            except Exception as exc:
                last = str(exc) if isinstance(exc, AlertError) else f"healthchecks: {type(exc).__name__}"
            if attempt < tries:
                self._sleep(SEND_BACKOFF_S[min(attempt - 1, len(SEND_BACKOFF_S) - 1)])
        with self._cv:
            self._n["ping_failed"] += 1
        log.warning("dead-man %s not delivered: %s", signal or "ping", last)
        return False

    def _send(self, msg: str, critical: bool = False) -> str:
        if self.telegram is None:
            with self._cv:
                self._inflight = None
            if not self._warned_no_telegram:
                self._warned_no_telegram = True
                log.warning("alerts: no message channel (Telegram is not configured, nor ntfy); alerts go to the log "
                            "only")
            return "log_only"
        started = self._clock.mono()
        err = self._transmit(msg, tries=1 if self._stopping else SEND_TRIES)
        kept = err is not None and err.retry
        with self._cv:
            saved = self._inflight is None                       # stop() kept it for the next start meanwhile
            self._inflight = None
            self._n["sent" if err is None else "send_failed"] += 1
            waits = kept and err.rate_limited and self._draining     # HTTP 429 while the kept messages go out
            if kept and not saved:
                self._hold(_Held(msg, critical, started))
                if waits:
                    self._back_off()                             # it joins them: they wait, then go on
                else:
                    self._start_holding(started, err.rate_limited)
        line = msg.replace("\n", " | ")
        if saved:
            if err is None:
                _late_log(msg)
        elif waits:
            log.info("alert not delivered yet (%s): the alert service is limiting the rate, so it waits %.0f s with "
                     "the delayed alerts; it is in this log: %s", err, OUTBOX_RATE_LIMIT_S, line)
        elif kept:
            log.error("alert not delivered yet (%s): kept, and sent once %s (a try every %.0f s); it is in this log: "
                      "%s", err, "the rate limit allows it" if err.rate_limited else "the network is back",
                      OUTBOX_RETRY_S, line)
        elif err is not None:
            log.error("alert not delivered (%s); it is in this log: %s", err, line)
        return "telegram"

    # -- thread ------------------------------------------------------------------------------------------------
    def run(self) -> None:
        while True:
            with self._cv:
                now = self._clock.mono()
                sendable = not self._waiting(now) and (bool(self._crit or self._rest)
                                                       or (self._draining and bool(self._outbox)))
                pending = (sendable or self._fail_text is not None
                           or (self._ready_ping and self.healthcheck is not None and self.heartbeat.fresh(now))
                           or (self._holding and self._stopping and not self._stop_tried))
                if self._stopping and (not pending or time.monotonic() > self._stop_deadline):
                    return
                if not pending and not self._stopping:
                    self._cv.wait(IDLE_WAKE_S)
            try:
                self.step()
            except Exception:
                log.exception("alerts: step failed")
                if self._wake.wait(IDLE_WAKE_S) and time.monotonic() > self._stop_deadline:
                    return

    def start(self) -> threading.Thread:
        """Start the alerts thread, first taking what the previous run could not deliver (restore_leftovers)."""
        if self._thread is not None:
            raise RuntimeError("alerts thread already started")
        try:
            self.restore_leftovers()
        except Exception:
            log.exception("alerts: the previous run's undelivered alerts could not be read")
        self._thread = threading.Thread(target=self.run, name="alerts", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, timeout_s: float = 5.0) -> None:
        """Deliver what is queued (one try each) for up to timeout_s, then end the thread; what is still undelivered
        is kept in outbox_file for the next start, also the message the thread may still be sending (a hanging
        connect: BOT_CRASHED, say, submitted just before the stop). The message the thread was sending when stop()
        came keeps its tries, at once, until timeout_s has passed (_past_stop_deadline); only a try still hanging
        then is cut: the thread makes no further try of that one and drops its late result."""
        with self._cv:
            self._stopping = True
            self._stop_deadline = time.monotonic() + timeout_s
            self._cv.notify_all()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout_s + 1.0)
        self._save_leftovers()

    def _save_leftovers(self) -> None:
        """At stop: the kept, the in-flight and the still queued messages go to outbox_file, oldest first. Without a
        file or a message channel they stay only in the log (no run could send them)."""
        if self._outbox_file is None or self.telegram is None:
            return
        with self._cv:
            left = [(h.text, h.critical) for h in self._outbox]
            if self._inflight is not None:                       # taken by the thread, its send not over
                text, critical, was_kept = self._inflight
                left.insert(0 if was_kept else len(left), (text, critical))
                self._inflight = None
            left += [(m, True) for m in self._crit] + [(m, False) for m in self._rest]
            self._outbox.clear()
            self._crit.clear()
            self._rest.clear()
        if not left:
            return
        try:
            save_undelivered(self._outbox_file, left, saved=self._clock.now_wall())
        except Exception:
            log.exception("alerts: %s could not be kept for the next start (they are in this log)",
                          _count(len(left), "undelivered alert"))
            return
        log.warning("alerts: %s kept in %s for the next start", _count(len(left), "undelivered alert"),
                    self._outbox_file)


# ---------------------------------------------------------------------------------------------------------------
# One-shot senders (token unit, self-check, `titan2 alert --critical`)
# ---------------------------------------------------------------------------------------------------------------
def send_now(severity: str, code: str, text: str, *, env: Mapping[str, str] | None = None,
             telegram: Telegram | None = None, sleep: Callable[[float], Any] = time.sleep,
             now_wall: datetime | None = None, outbox: str | Path | None = None) -> bool:
    """Log the alert and send it to Telegram or ntfy now, with retries. True when delivered. outbox: where one the
    network kept from going out is saved for the next start's alerter (outbox_path(var))."""
    if severity not in SEVERITIES:
        severity = CRITICAL
    log.log(_LEVEL[severity], "alert %s %s: %s", severity, code, text)
    tg = telegram if telegram is not None else message_channel(env)
    if tg is None:
        log.warning("alerts: no message channel (Telegram is not configured, nor ntfy); alert logged only")
        return False
    when = now_wall or RealClock().now_wall()
    msg = format_alert(severity, code, text, now_wall=when)
    err = _deliver(tg, msg, sleep)
    if err is not None:
        log.error("alert %s not delivered: %s", code, err)
        if outbox is not None and err.retry:
            try:
                save_undelivered(outbox, [(msg, severity == CRITICAL)], saved=when)
                log.warning("alert %s kept in %s: the next start sends it", code, outbox)
            except Exception:
                log.exception("alert %s could not be kept for the next start", code)
    return err is None


_UNIT_FAILED_RE = re.compile(r"unit (\S+) failed")      # the text titan2-alert@.service sends ("unit %i failed")


def is_bot_failure(text: str) -> bool:
    """Whether a CRITICAL text is about the bot itself (the dead-man check is the bot's): False only for the OnFailure
    text of another unit ("unit titan2-selfcheck.service failed"); anything else counts as the bot's."""
    m = _UNIT_FAILED_RE.fullmatch(str(text).strip())
    return m is None or m.group(1).startswith("titan2-bot.")


def critical_now(text: str, *, code: str = "UNIT_FAILED", env: Mapping[str, str] | None = None,
                 telegram: Telegram | None = None, healthcheck: Healthcheck | None = None,
                 sleep: Callable[[float], Any] = time.sleep, deadman: bool | None = None,
                 outbox: str | Path | None = None) -> int:
    """`titan2 alert --critical TEXT` (OnFailure units): Telegram CRITICAL, plus healthchecks /fail when the failure
    is the bot's (deadman None: decided by is_bot_failure(text)). Another unit's failure (the self-check, backup,
    token, dashboard) is Telegram only: /fail on the bot's dead-man check would mark a healthy bot DOWN. outbox: as
    send_now's (the laptop's give-up keeps its message there for the next start).
    Exit code 0 when at least one channel delivered it, else 2."""
    sent = send_now(CRITICAL, code, text, env=env, telegram=telegram, sleep=sleep, outbox=outbox)
    pinged = False
    if not (is_bot_failure(text) if deadman is None else deadman):
        return 0 if sent else 2
    try:
        hc = healthcheck if healthcheck is not None else Healthcheck.from_env(env)
    except ValueError as exc:
        log.error("alerts: %s", exc)
        hc = None
    if hc is not None:
        try:
            hc.ping("fail", text)
            pinged = True
        except AlertError as exc:
            log.error("healthchecks /fail not delivered: %s", exc)
    return 0 if sent or pinged else 2
