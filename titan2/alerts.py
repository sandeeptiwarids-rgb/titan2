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

Dead-man (HEALTHCHECK_URL; check schedule DEADMAN_SCHEDULE, 3-min grace): pinged at READY (ping_ready), then every
60 s from 08:45 IST for as long as the core runs, and only while the core heartbeat is under 10 s old, so a stuck
core stops the pings and healthchecks raises the alarm from outside the host; one last ping as a graceful exit
begins (ping_now). fail(text) pings /fail on a CRITICAL exit; that ping is never gated. critical_now() pings /fail
only for the bot's own failures: other units' failures are Telegram only.

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

import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, time as dtime
from typing import Any, Callable, Mapping

from titan2.clock import Clock, RealClock

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
_LEVEL = {INFO: logging.INFO, WARN: logging.WARNING, CRITICAL: logging.CRITICAL}


class AlertError(Exception):
    """A Telegram or healthchecks call failed. The message never contains a secret."""


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
            raise AlertError(f"telegram: HTTP {status}")

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
            raise AlertError(f"ntfy: HTTP {status}")

    def __repr__(self) -> str:
        return "Ntfy(<configured>)"


class Fanout:
    """Several message channels as one: send() delivers to each and fails only when all of them failed."""

    def __init__(self, channels: list[Any]) -> None:
        self.channels = [c for c in channels if c is not None]

    def send(self, text: str) -> None:
        errors = []
        for c in self.channels:
            try:
                c.send(text)
            except AlertError as exc:
                errors.append(str(exc))
        if errors and len(errors) == len(self.channels):
            raise AlertError("; ".join(errors))
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


def _send_with_retry(telegram: Telegram, msg: str, sleep: Callable[[float], Any],
                     tries: int = SEND_TRIES) -> str | None:
    """None when delivered, else the last error text."""
    last = "not attempted"
    for attempt in range(1, tries + 1):
        try:
            telegram.send(msg)
            return None
        except AlertError as exc:
            last = str(exc)
        except Exception as exc:               # an injected transport: still never let it kill the caller
            last = f"telegram: {type(exc).__name__}"
        if attempt < tries:
            sleep(SEND_BACKOFF_S[min(attempt - 1, len(SEND_BACKOFF_S) - 1)])
    return last


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


class Alerter:
    """The `alerts` thread (§3.2). Build it, start() it, call submit()/dispatch() from any thread; step() is the unit
    of work (tests call it inline with a FakeClock and fake transports instead of starting the thread)."""

    def __init__(self, telegram: Telegram | None, healthcheck: Healthcheck | None,
                 heartbeat: Heartbeat | None = None, *, clock: Clock | None = None, queue_max: int = QUEUE_MAX,
                 sleep: Callable[[float], Any] | None = None) -> None:
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
        self._n = {"accepted": 0, "suppressed": 0, "dropped": 0, "sent": 0, "send_failed": 0, "pings": 0,
                   "ping_failed": 0, "acks": 0}

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

    def counters(self) -> dict[str, int]:
        with self._cv:
            return dict(self._n)

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
        """Caller holds the lock: queue CRITICAL repeats and forget quiet keys."""
        for k, rec in list(self._recs.items()):
            if rec.active and now - rec.last_queued >= CRITICAL_REPEAT_S:
                rec.repeats += 1
                rec.last_queued = now
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
        """Do the due work: the /fail ping, a dead-man ping, then one queued message. Returns what was attempted:
        any of "fail", "ping", "telegram", "log_only"."""
        now = self._clock.mono()
        with self._cv:
            self._due_repeats(now)
            fail_text, self._fail_text = self._fail_text, None
            ping = self._ping_due(now)
            msg = self._crit.popleft() if self._crit else self._rest.popleft() if self._rest else None
        done = []
        if fail_text is not None and self.healthcheck is not None:
            done.append("fail")
            self._ping("fail", fail_text, tries=1 if self._stopping else SEND_TRIES)
        if ping:
            done.append("ping")
            self._last_ping_ok = self._ping("", "", tries=1)
        if msg is not None:
            done.append(self._send(msg))
        return done

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

    def _send(self, msg: str) -> str:
        if self.telegram is None:
            if not self._warned_no_telegram:
                self._warned_no_telegram = True
                log.warning("alerts: no message channel (Telegram is not configured, nor ntfy); alerts go to the log "
                            "only")
            return "log_only"
        if self._last_send is not None and not self._stopping:
            wait = self._last_send + TELEGRAM_MIN_INTERVAL_S - self._clock.mono()
            if wait > 0:
                self._sleep(wait)
        error = _send_with_retry(self.telegram, msg, self._sleep, tries=1 if self._stopping else SEND_TRIES)
        self._last_send = self._clock.mono()
        with self._cv:
            self._n["sent" if error is None else "send_failed"] += 1
        if error is not None:
            log.error("alert not delivered (%s); it is in this log: %s", error, msg.replace("\n", " | "))
        return "telegram"

    # -- thread ------------------------------------------------------------------------------------------------
    def run(self) -> None:
        while True:
            with self._cv:
                pending = (bool(self._crit or self._rest) or self._fail_text is not None
                           or (self._ready_ping and self.healthcheck is not None
                               and self.heartbeat.fresh(self._clock.mono())))
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
        if self._thread is not None:
            raise RuntimeError("alerts thread already started")
        self._thread = threading.Thread(target=self.run, name="alerts", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, timeout_s: float = 5.0) -> None:
        """Deliver what is queued (one try each) for up to timeout_s, then end the thread."""
        with self._cv:
            self._stopping = True
            self._stop_deadline = time.monotonic() + timeout_s
            self._cv.notify_all()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout_s + 1.0)


# ---------------------------------------------------------------------------------------------------------------
# One-shot senders (token unit, self-check, `titan2 alert --critical`)
# ---------------------------------------------------------------------------------------------------------------
def send_now(severity: str, code: str, text: str, *, env: Mapping[str, str] | None = None,
             telegram: Telegram | None = None, sleep: Callable[[float], Any] = time.sleep,
             now_wall: datetime | None = None) -> bool:
    """Log the alert and send it to Telegram now, with retries. True when delivered."""
    if severity not in SEVERITIES:
        severity = CRITICAL
    log.log(_LEVEL[severity], "alert %s %s: %s", severity, code, text)
    tg = telegram if telegram is not None else message_channel(env)
    if tg is None:
        log.warning("alerts: no message channel (Telegram is not configured, nor ntfy); alert logged only")
        return False
    msg = format_alert(severity, code, text, now_wall=now_wall or RealClock().now_wall())
    error = _send_with_retry(tg, msg, sleep)
    if error is not None:
        log.error("alert %s not delivered: %s", code, error)
    return error is None


_UNIT_FAILED_RE = re.compile(r"unit (\S+) failed")      # the text titan2-alert@.service sends ("unit %i failed")


def is_bot_failure(text: str) -> bool:
    """Whether a CRITICAL text is about the bot itself (the dead-man check is the bot's): False only for the OnFailure
    text of another unit ("unit titan2-selfcheck.service failed"); anything else counts as the bot's."""
    m = _UNIT_FAILED_RE.fullmatch(str(text).strip())
    return m is None or m.group(1).startswith("titan2-bot.")


def critical_now(text: str, *, code: str = "UNIT_FAILED", env: Mapping[str, str] | None = None,
                 telegram: Telegram | None = None, healthcheck: Healthcheck | None = None,
                 sleep: Callable[[float], Any] = time.sleep, deadman: bool | None = None) -> int:
    """`titan2 alert --critical TEXT` (OnFailure units): Telegram CRITICAL, plus healthchecks /fail when the failure
    is the bot's (deadman None: decided by is_bot_failure(text)). Another unit's failure (the self-check, backup,
    token, dashboard) is Telegram only: /fail on the bot's dead-man check would mark a healthy bot DOWN.
    Exit code 0 when at least one channel delivered it, else 2."""
    sent = send_now(CRITICAL, code, text, env=env, telegram=telegram, sleep=sleep)
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
