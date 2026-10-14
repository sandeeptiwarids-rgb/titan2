"""The Kite session (spec §13 "Token", DETAIL §13.4-§13.5, §9.7): the session file, the headless TOTP login and the
manual request-token exchange. `titan2 token [--request-token RT]` runs run(); the titan2-token oneshot runs it at
08:30.

- Only this unit logs in. It writes session/kite_session.json ({access_token, user_id, issued_at, valid_until})
  atomically with mode 0600, then verifies it with profile(). The bot and the dashboard never log in: the bot reads
  the file (read_session + kite_client) and, while HALT(TOKEN) is active, re-checks it every 30 s (token_recheck).
- It never edits an env file and never restarts or signals a service. v1's restart_titan_bot
  (kite_token_manager.py:312-330) is deliberately not ported: a token job restarting the trading process mid-session
  is how v1 lost control of open positions. A newer token reaches the bot only through §9.7 (controlled exit 75).
- TOKEN_MODE=auto: headless login (password + TOTP), up to 3 attempts 20 s apart; a rejected password is not
  retried (repeated bad logins can lock the account). TOKEN_MODE=manual: Telegram gets the Kite login URL and the
  operator runs `titan2 token --request-token <rt>` on the server.
- Secrets (password, TOTP secret and code, request and access tokens, API secret) are never logged, printed, put in
  an exception or an alert. A session is identified only by Session.fingerprint (12 hex digits of SHA-256).
- Env: /etc/titan2/titan2.env (root:titan 0640). systemd units get it as their environment; the CLI (sudo -u
  titan) reads the file itself (load_env). The file holds no access token and no trading mode. TITAN2_ENV_FILE
  names another file; on Windows (the laptop, titan2.laptop) the default is %APPDATA%\\titan2\\titan2.env and the
  session file lives under %LOCALAPPDATA%\\titan2\\var (TITAN2_VAR overrides both platforms' var).

Runtime use: the bot reads read_session(session_path()) (SessionError -> HALT(TOKEN)), builds its client with
kite_client(api_key, sess.access_token) and registers make_token_prep(path, api_key, sess.fingerprint) as the reads
lane's "TOKEN" prep. `titan2 token [--request-token RT]` is run(request_token=RT).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urljoin, urlparse

from titan2 import alerts, laptop
from titan2.clock import IST, RealClock, iso, parse_wall, to_ist

log = logging.getLogger(__name__)

DEFAULT_VAR_DIR = "/var/lib/titan2"
ENV_FILE = Path("/etc/titan2/titan2.env")
SESSION_FILENAME = "kite_session.json"
SESSION_KEYS = ("access_token", "user_id", "issued_at", "valid_until")
DAY_START = dtime(6, 0)                 # Kite access tokens expire at 06:00 IST the next day
TOKEN_MODES = ("auto", "manual")
LOGIN_ATTEMPTS = 3
LOGIN_RETRY_S = 20.0
HTTP_TIMEOUT_S = 30.0
KITE_TIMEOUT_S = 5                      # the bot's client (DETAIL §9.5 step 7)
MAX_REDIRECTS = 6
CREDENTIAL_KEYS = ("ZERODHA_API_KEY", "ZERODHA_API_SECRET", "ZERODHA_USER_ID", "ZERODHA_PASSWORD",
                   "ZERODHA_TOTP_SECRET")
SECRET_ENV_KEYS = ("ZERODHA_API_SECRET", "ZERODHA_PASSWORD", "ZERODHA_TOTP_SECRET", "TELEGRAM_BOT_TOKEN",
                   "HEALTHCHECK_URL")
# Ported from agentic_trader/kite_token_manager.py:40-43 @b4694f3 — changes: none (the endpoints)
KITE_LOGIN_URL = "https://kite.zerodha.com/api/login"
KITE_TWOFA_URL = "https://kite.zerodha.com/api/twofa"
KITE_CONNECT_LOGIN = "https://kite.trade/connect/login"     # redirects via kite.zerodha.com to the app's redirect URL
KITE_LOGIN_PAGE = "https://kite.zerodha.com/connect/login"  # KiteConnect.login_url(), for the manual flow
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/129.0.0.0 Safari/537.36")
_TOKEN_RE = re.compile(r"[A-Za-z0-9]{6,128}")
_REQUEST_TOKEN_IN_TEXT = re.compile(r"request_token=([A-Za-z0-9]{6,128})")
_QUERY_RE = re.compile(r"\?[^\s'\")]*")


class TokenError(Exception):
    """A login, exchange or verification failed. retry=False: retrying cannot help (wrong password, wrong user)."""

    def __init__(self, message: str, *, retry: bool = True) -> None:
        super().__init__(message)
        self.retry = retry


class SessionError(TokenError):
    """The session file is missing, unreadable or malformed."""


def _scrub(text: object, *secrets: str | None) -> str:
    """Error text safe to log: known secret values and every URL query string removed."""
    s = _QUERY_RE.sub("?...", str(text))
    for secret in secrets:
        if secret:
            s = s.replace(secret, "***")
    return s[:300]


# ---------------------------------------------------------------------------------------------------------------
# Paths and env
# ---------------------------------------------------------------------------------------------------------------
def var_dir() -> Path:
    """TITAN2_VAR, else %LOCALAPPDATA%\\titan2\\var on Windows, else /var/lib/titan2."""
    return laptop.default_var_dir(os.environ, DEFAULT_VAR_DIR)


def env_file(environ: Mapping[str, str] | None = None) -> Path:
    """The env file load_env() reads by default: TITAN2_ENV_FILE, else %APPDATA%\\titan2\\titan2.env on Windows,
    else ENV_FILE."""
    return laptop.default_env_file(os.environ if environ is None else environ, ENV_FILE)


def session_path(var: str | Path | None = None) -> Path:
    """<var>/session/kite_session.json (session/ is titan:titan 0700, DETAIL §13.1)."""
    return Path(var if var is not None else var_dir()) / "session" / SESSION_FILENAME


def read_env_file(path: str | Path) -> dict[str, str]:
    """parse_env() of a file (UTF-8, with or without a BOM). Raises OSError, or ValueError for other encodings."""
    return parse_env(Path(path).read_text(encoding="utf-8-sig"))


def parse_env(text: str) -> dict[str, str]:
    """KEY=VALUE lines (systemd EnvironmentFile style): '#' comments, optional matching quotes."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            out[key] = value
    return out


def load_env(path: str | Path | None = None, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The env file's values (path, else env_file(environ)), overridden by the process environment (systemd sets it
    from the same file). A missing or unreadable file just leaves the process environment; so does one that is not
    UTF-8 (Windows PowerShell 5's `>` writes UTF-16), with a warning."""
    p = Path(path) if path is not None else env_file(environ)
    merged: dict[str, str] = {}
    try:
        merged.update(read_env_file(p))
    except OSError as exc:
        log.debug("env file %s not read: %s", p, type(exc).__name__)
    except ValueError:
        log.warning("env file %s is not UTF-8 text: ignored (save it as UTF-8)", p)
    merged.update(os.environ if environ is None else environ)
    return merged


@dataclass(frozen=True, slots=True)
class Credentials:
    api_key: str
    user_id: str
    api_secret: str = field(repr=False)
    password: str = field(default="", repr=False)
    totp_secret: str = field(default="", repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Credentials:
        def get(k: str) -> str:
            return str(env.get(k, "")).strip()
        return cls(api_key=get("ZERODHA_API_KEY"), user_id=get("ZERODHA_USER_ID"),
                   api_secret=get("ZERODHA_API_SECRET"), password=get("ZERODHA_PASSWORD"),
                   totp_secret=get("ZERODHA_TOTP_SECRET"))

    def missing(self, *, login: bool) -> list[str]:
        """Names (never values) of the env keys this flow needs but lacks."""
        need = {"ZERODHA_API_KEY": self.api_key, "ZERODHA_API_SECRET": self.api_secret,
                "ZERODHA_USER_ID": self.user_id}
        if login:
            need.update(ZERODHA_PASSWORD=self.password, ZERODHA_TOTP_SECRET=self.totp_secret)
        return [k for k, v in need.items() if not v]

    def secrets(self) -> tuple[str, ...]:
        return (self.api_secret, self.password, self.totp_secret)


# ---------------------------------------------------------------------------------------------------------------
# The session file
# ---------------------------------------------------------------------------------------------------------------
def next_day_start(issued_at: datetime) -> datetime:
    """The first 06:00 IST strictly after issued_at: when Kite expires the token."""
    t = to_ist(issued_at)
    start = datetime.combine(t.date(), DAY_START, tzinfo=IST)
    return start if t < start else start + timedelta(days=1)


def _same_user(a: str, b: str) -> bool:
    return bool(a) and a.strip().upper() == b.strip().upper()


@dataclass(frozen=True, slots=True)
class Session:
    access_token: str = field(repr=False)
    user_id: str
    issued_at: datetime
    valid_until: datetime

    @property
    def fingerprint(self) -> str:
        """Safe to log and compare: identifies the token without revealing it."""
        return hashlib.sha256(self.access_token.encode("utf-8")).hexdigest()[:12]

    def is_fresh(self, now: datetime) -> bool:
        """Issued today (IST) at or after 06:00 and not yet expired: the 08:30 unit's "session is valid" test."""
        n, issued = to_ist(now), to_ist(self.issued_at)
        return issued.date() == n.date() and issued.time() >= DAY_START and issued <= n < self.valid_until

    def to_json(self) -> dict[str, str]:
        return {"access_token": self.access_token, "user_id": self.user_id, "issued_at": iso(self.issued_at),
                "valid_until": iso(self.valid_until)}


def new_session(access_token: str, user_id: str, issued_at: datetime) -> Session:
    if not (isinstance(access_token, str) and _TOKEN_RE.fullmatch(access_token)):
        raise TokenError("generate_session returned no usable access_token", retry=False)
    if not (isinstance(user_id, str) and user_id.strip()):
        raise TokenError("generate_session returned no user_id", retry=False)
    issued = to_ist(issued_at)
    return Session(access_token, user_id.strip(), issued, next_day_start(issued))


def read_session(path: str | Path | None = None) -> Session:
    """The session in the file. Raises SessionError; the message never contains the token."""
    p = Path(path) if path is not None else session_path()
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        raise SessionError(f"no session file at {p}") from None
    except OSError as exc:
        raise SessionError(f"cannot read {p}: {type(exc).__name__}") from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise SessionError(f"{p} is not valid JSON") from None
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) for k in SESSION_KEYS):
        raise SessionError(f"{p} must hold the string keys {', '.join(SESSION_KEYS)}")
    token, user = data["access_token"], data["user_id"].strip()
    if not _TOKEN_RE.fullmatch(token) or not user:
        raise SessionError(f"{p}: access_token or user_id is malformed")
    try:
        issued, until = parse_wall(data["issued_at"]), parse_wall(data["valid_until"])
    except ValueError:
        raise SessionError(f"{p}: issued_at / valid_until must be ISO times with an offset") from None
    if not until > issued:
        raise SessionError(f"{p}: valid_until is not after issued_at")
    return Session(token, user, issued, until)


def write_session(sess: Session, path: str | Path | None = None) -> Path:
    """Write atomically with mode 0600: the temp file is created 0600 (O_EXCL, never through a symlink), fsynced,
    then renamed over the old file, so no reader ever sees a partial file and no other user ever can read one."""
    p = Path(path) if path is not None else session_path()
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = (json.dumps(sess.to_json(), indent=2, sort_keys=True) + "\n").encode("utf-8")
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)                   # the umask can only remove bits; make the mode exact
        os.replace(tmp, p)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if os.name == "posix":
        dfd = os.open(str(p.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    return p


# ---------------------------------------------------------------------------------------------------------------
# Kite client and verification
# ---------------------------------------------------------------------------------------------------------------
KiteFactory = Callable[..., Any]               # (api_key, access_token=None) -> KiteConnect-like


def kite_client(api_key: str, access_token: str | None = None, *, timeout: float = KITE_TIMEOUT_S) -> Any:
    """A KiteConnect client. debug stays False: in debug mode kiteconnect prints requests and responses (tokens)."""
    from kiteconnect import KiteConnect        # lazy: the kiteconnect package imports the twisted ticker

    kite = KiteConnect(api_key=api_key, timeout=timeout)
    if access_token:
        kite.set_access_token(access_token)
    return kite


def verify_session(sess: Session, api_key: str, *, expected_user: str | None = None,
                   kite_factory: KiteFactory | None = None) -> Mapping[str, Any]:
    """profile() with the session's token; the account must be the session's user (and expected_user if given)."""
    kite = (kite_factory or kite_client)(api_key, sess.access_token)
    try:
        prof = kite.profile()
    except Exception as exc:
        raise TokenError(f"profile() failed: {type(exc).__name__}: {_scrub(exc, sess.access_token)}") from None
    if not isinstance(prof, Mapping):
        raise TokenError("profile() returned an unexpected response")
    got = str(prof.get("user_id") or "")
    if not _same_user(got, sess.user_id):
        raise TokenError(f"profile() user {got!r} is not the session's user {sess.user_id!r}", retry=False)
    if expected_user is not None and not _same_user(got, expected_user):
        raise TokenError(f"session belongs to {got!r}, not ZERODHA_USER_ID {expected_user!r}", retry=False)
    return prof


def token_recheck(path: str | Path | None, api_key: str, current_fingerprint: str | None, *,
                  kite_factory: KiteFactory | None = None) -> tuple[bool, str]:
    """The Prep("TOKEN") handler body on lane-reads while HALT(TOKEN) is active (§9.7, §8.7): profile() with the
    token in the session file. (True, "NEW fp=...") -> a newer token works: the engine exits 75 so systemd restarts
    the bot with it. (True, "SAME fp=...") -> the running token works again: the halt clears (AUTO).
    (False, reason) -> keep the halt and retry in 30 s. Never logs in."""
    try:
        sess = read_session(path)
        verify_session(sess, api_key, kite_factory=kite_factory)
    except TokenError as exc:
        return False, str(exc)
    tag = "SAME" if current_fingerprint is not None and sess.fingerprint == current_fingerprint else "NEW"
    return True, f"{tag} fp={sess.fingerprint}"


def make_token_prep(path: str | Path | None, api_key: str, current_fingerprint: str | None, *,
                    kite_factory: KiteFactory | None = None) -> Callable[[], tuple[bool, str]]:
    """ReadsLane(prep={"TOKEN": make_token_prep(...)})."""
    return lambda: token_recheck(path, api_key, current_fingerprint, kite_factory=kite_factory)


def login_url(api_key: str) -> str:
    """Kite Connect's login page for this app (KiteConnect.login_url()); the redirect carries the request_token."""
    return f"{KITE_LOGIN_PAGE}?api_key={api_key}&v=3"


# ---------------------------------------------------------------------------------------------------------------
# Headless login and exchange
# ---------------------------------------------------------------------------------------------------------------
def totp_now(secret: str) -> str:
    try:
        import pyotp                           # lazy: only the token unit needs it
    except ImportError:
        raise TokenError("pyotp is not installed (pip install -r requirements.lock)", retry=False) from None
    return pyotp.TOTP(secret).now()


def _new_http() -> Any:
    import requests                            # lazy

    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "X-Kite-Version": "3"})
    return s


def _kite_ok(resp: Any, step: str, creds: Credentials, *, retry_on_4xx: bool) -> Mapping[str, Any]:
    """Kite's internal login API answers {"status": "success", "data": {...}}; anything else fails the step."""
    status = getattr(resp, "status_code", None)
    try:
        body = resp.json()
    except Exception:
        body = None
    if status == 200 and isinstance(body, Mapping) and body.get("status") == "success":
        data = body.get("data")
        return data if isinstance(data, Mapping) else {}
    message = body.get("message") if isinstance(body, Mapping) else None
    retry = retry_on_4xx or status == 429 or not (isinstance(status, int) and 400 <= status < 500)
    raise TokenError(f"{step} failed: HTTP {status}: {_scrub(message or 'no message', *creds.secrets())}",
                     retry=retry)


def _request_token_from(location: str) -> str | None:
    if "request_token" not in location:
        return None
    values = parse_qs(urlparse(location).query).get("request_token") or []
    value = values[0] if values else None
    return value if value and _TOKEN_RE.fullmatch(value) else None


# Ported from agentic_trader/kite_token_manager.py:148-292 @b4694f3 — changes: returns the request_token (the exchange
#   is exchange()); raises TokenError instead of returning {}; a rejected password is marked not retryable; nothing
#   secret is logged (v1 logged part of the request_token); relative redirects are resolved; the request_token is
#   read from a Location header before that URL is ever fetched
def headless_login(creds: Credentials, *, http: Any = None, totp: Callable[[str], str] | None = None) -> str:
    """Password + TOTP login, then the Kite Connect redirect chain. Returns the request_token."""
    s = http if http is not None else _new_http()
    secrets = creds.secrets()
    try:
        log.info("token: step 1/3 login as %s", creds.user_id)
        resp = s.post(KITE_LOGIN_URL, data={"user_id": creds.user_id, "password": creds.password},
                      timeout=HTTP_TIMEOUT_S)
        request_id = _kite_ok(resp, "login", creds, retry_on_4xx=False).get("request_id")
        if not (isinstance(request_id, str) and request_id):
            raise TokenError("login returned no request_id")
        log.info("token: step 2/3 TOTP")
        code = (totp or totp_now)(creds.totp_secret)
        resp = s.post(KITE_TWOFA_URL, data={"user_id": creds.user_id, "request_id": request_id,
                                            "twofa_value": code, "twofa_type": "totp"},
                      timeout=HTTP_TIMEOUT_S, allow_redirects=False)
        _kite_ok(resp, "twofa", creds, retry_on_4xx=True)      # a TOTP at the edge of its window: retry helps
        log.info("token: step 3/3 Kite Connect redirect")
        url = f"{KITE_CONNECT_LOGIN}?api_key={creds.api_key}&v=3"
        resp = s.get(url, timeout=HTTP_TIMEOUT_S, allow_redirects=False)
        for _ in range(MAX_REDIRECTS):
            location = (getattr(resp, "headers", None) or {}).get("Location", "") or ""
            found = _request_token_from(location)
            if found:
                return found
            if resp.status_code not in (301, 302, 303, 307, 308) or not location:
                break
            url = urljoin(url, location)
            resp = s.get(url, timeout=HTTP_TIMEOUT_S, allow_redirects=False)
        match = _REQUEST_TOKEN_IN_TEXT.search(str(getattr(resp, "text", "") or ""))
        if match:
            return match.group(1)
        raise TokenError(f"no request_token in the Kite Connect redirect chain (last HTTP {resp.status_code})")
    except TokenError:
        raise
    except Exception as exc:                   # network errors: retryable; the text is scrubbed of URLs and secrets
        raise TokenError(f"login: {type(exc).__name__}: {_scrub(exc, *secrets)}") from None


def exchange(request_token: str, creds: Credentials, *, kite_factory: KiteFactory | None = None,
             now: datetime | None = None) -> Session:
    """generate_session(request_token): the new Session (not yet written). The account must be ZERODHA_USER_ID."""
    rt = (request_token or "").strip()
    if not _TOKEN_RE.fullmatch(rt):
        raise TokenError("the request_token is malformed (expected letters and digits)", retry=False)
    kite = (kite_factory or kite_client)(creds.api_key)
    try:
        data = kite.generate_session(rt, api_secret=creds.api_secret)
    except Exception as exc:
        raise TokenError(f"generate_session failed: {type(exc).__name__}: "
                         f"{_scrub(exc, rt, *creds.secrets())}") from None
    if not isinstance(data, Mapping):
        raise TokenError("generate_session returned an unexpected response")
    user = str(data.get("user_id") or "")
    if not _same_user(user, creds.user_id):
        raise TokenError(f"logged in as {user!r}, not ZERODHA_USER_ID {creds.user_id!r}: session not saved",
                         retry=False)
    return new_session(data.get("access_token"), user, now or RealClock().now_wall())


# ---------------------------------------------------------------------------------------------------------------
# `titan2 token`
# ---------------------------------------------------------------------------------------------------------------
Notify = Callable[[str, str, str], bool]       # (severity, code, text) -> delivered


def run(*, request_token: str | None = None, env: Mapping[str, str] | None = None,
        path: str | Path | None = None, now: Callable[[], datetime] | None = None, http: Any = None,
        totp: Callable[[str], str] | None = None, kite_factory: KiteFactory | None = None,
        sleep: Callable[[float], Any] = time.sleep, notify: Notify | None = None) -> int:
    """The token unit. Exit code 0 = a valid session is on disk (or, in manual mode, the login link was sent);
    2 = failed (and a CRITICAL alert was attempted). Never restarts anything and never writes an env file."""
    e = load_env() if env is None else dict(env)
    clock = now or RealClock().now_wall
    p = Path(path) if path is not None else session_path(e.get("TITAN2_VAR") or None)
    creds = Credentials.from_env(e)
    send: Notify = notify or (lambda sev, code, text: alerts.send_now(sev, code, text, env=e))

    def failed(reason: str) -> int:
        text = f"Kite token refresh failed: {reason}. The bot halts entries (HALT TOKEN) until a valid session " \
               f"exists; fix with `titan2 token` or `titan2 token --request-token <rt>`."
        log.error("token: %s", reason)
        send(alerts.CRITICAL, "TOKEN_REFRESH_FAILED", text)
        return 2

    def save(sess: Session) -> int:
        try:
            write_session(sess, p)
        except OSError as exc:
            return failed(f"cannot write {p}: {type(exc).__name__}: {exc}")
        try:
            verify_session(read_session(p), creds.api_key, expected_user=creds.user_id, kite_factory=kite_factory)
        except TokenError as exc:
            return failed(f"session {sess.fingerprint} written but profile() check failed: {exc}")
        log.info("token: session %s for %s saved to %s (valid until %s)", sess.fingerprint, sess.user_id, p,
                 iso(sess.valid_until))
        return 0

    mode = str(e.get("TOKEN_MODE", "auto")).strip().lower() or "auto"
    if mode not in TOKEN_MODES:
        return failed(f"TOKEN_MODE must be auto or manual, not {mode!r}")

    if request_token is not None:                       # manual exchange (works in either mode)
        missing = creds.missing(login=False)
        if missing:
            return failed(f"missing in the env: {', '.join(missing)}")
        try:
            return save(exchange(request_token, creds, kite_factory=kite_factory, now=clock()))
        except TokenError as exc:
            return failed(str(exc))

    missing = creds.missing(login=mode == "auto")
    if missing:
        return failed(f"missing in the env: {', '.join(missing)}")
    try:
        current = read_session(p)
        if current.is_fresh(clock()):
            verify_session(current, creds.api_key, expected_user=creds.user_id, kite_factory=kite_factory)
            log.info("token: session %s is valid; nothing to do", current.fingerprint)
            return 0
        log.info("token: session %s is not today's; refreshing", current.fingerprint)
    except TokenError as exc:
        log.info("token: no usable session (%s); refreshing", exc)

    if mode == "manual":
        text = (f"Kite login needed (TOKEN_MODE=manual). Open {login_url(creds.api_key)} , log in, copy the "
                f"request_token from the redirect URL, then on the server run: titan2 token --request-token <rt>")
        return 0 if send(alerts.WARN, "TOKEN_LOGIN_NEEDED", text) else 2

    last = "not attempted"
    for attempt in range(1, LOGIN_ATTEMPTS + 1):
        try:
            rt = headless_login(creds, http=http, totp=totp)
            return save(exchange(rt, creds, kite_factory=kite_factory, now=clock()))
        except TokenError as exc:
            last = str(exc)
            log.warning("token: attempt %d/%d failed: %s", attempt, LOGIN_ATTEMPTS, last)
            if not exc.retry:
                break
        if attempt < LOGIN_ATTEMPTS:
            sleep(LOGIN_RETRY_S)
    return failed(last)
