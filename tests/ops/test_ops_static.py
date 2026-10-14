"""Static checks of the ops files (DETAIL §13): systemd units, the test-only CI, the hash locks, the env examples,
the sudoers allow-list, logrotate/journald, and the scripts' hygiene. These need no bash, except the syntax check."""

from __future__ import annotations

import re
import subprocess

from opsharness import APP, BASH, OPS, bpath, needs_bash, sudo_allowed, sudo_rules
from titan2.token import parse_env

UNITS = OPS / "systemd"
CI = APP / ".github" / "workflows" / "ci.yml"
ALERT = "titan2-alert@%n.service"
EXPECTED_UNITS = {
    "titan2-bot.service", "titan2-bot.timer", "titan2-token.service", "titan2-token.timer",
    "titan2-selfcheck.service", "titan2-selfcheck.timer", "titan2-dash.service", "titan2-backup.service",
    "titan2-backup.timer", "titan2-alert@.service",
}


def parse_unit(name: str) -> dict[str, list[tuple[str, str]]]:
    """systemd unit file -> {section: [(key, value), ...]} (keys may repeat, e.g. OnCalendar)."""
    sections: dict[str, list[tuple[str, str]]] = {}
    cur: list[tuple[str, str]] | None = None
    for raw in (UNITS / name).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = sections.setdefault(line[1:-1], [])
            continue
        assert cur is not None, f"{name}: key outside a section: {line}"
        key, sep, value = line.partition("=")
        assert sep, f"{name}: not KEY=VALUE: {line}"
        cur.append((key.strip(), value.strip()))
    return sections


def values(unit: dict, section: str, key: str) -> list[str]:
    return [v for k, v in unit.get(section, []) if k == key]


def one(unit: dict, section: str, key: str) -> str:
    vs = values(unit, section, key)
    assert len(vs) == 1, f"[{section}] {key}: expected one value, got {vs}"
    return vs[0]


# ---------------------------------------------------------------------------------------------------------------
# systemd units
# ---------------------------------------------------------------------------------------------------------------
def test_units_static_no_execstartpre():
    names = {p.name for p in UNITS.iterdir()}
    assert names == EXPECTED_UNITS
    units = {n: parse_unit(n) for n in names}

    # No unit can block a start: no ExecStartPre / ExecCondition anywhere (the self-check runs in the bot).
    for n, u in units.items():
        for section in u.values():
            assert not [k for k, _ in section if k in ("ExecStartPre", "ExecCondition")], n

    bot = units["titan2-bot.service"]
    expected_service = {
        "Type": "notify", "NotifyAccess": "main", "User": "titan", "Group": "titan", "UMask": "0027",
        "WorkingDirectory": "/home/ubuntu/titan/agentic_trader", "EnvironmentFile": "/etc/titan2/titan2.env",
        "ExecStart": "/home/ubuntu/titan/current/venv/bin/python -m titan2 bot", "Restart": "on-failure",
        "RestartSec": "5", "TimeoutStartSec": "300", "TimeoutStopSec": "45", "WatchdogSec": "30",
        "MemoryMax": "1G", "NoNewPrivileges": "true", "ProtectSystem": "strict", "ProtectHome": "read-only",
        "PrivateTmp": "true",
    }
    for key, want in expected_service.items():
        assert one(bot, "Service", key) == want, key
    assert set(one(bot, "Service", "RestartPreventExitStatus").split()) == {"73", "78"}
    assert set(one(bot, "Service", "ReadWritePaths").split()) == {"/var/lib/titan2", "/var/log/titan2"}
    env = one(bot, "Service", "Environment").split()
    assert {"PYTHONUNBUFFERED=1", "PYTHONDONTWRITEBYTECODE=1", "TZ=Asia/Kolkata", "TITAN2_VAR=/var/lib/titan2"} <= set(env)
    assert one(bot, "Unit", "OnFailure") == ALERT
    assert one(bot, "Unit", "Wants") == "network-online.target"
    assert set(one(bot, "Unit", "After").split()) == {"network-online.target", "time-sync.target"}
    assert one(bot, "Unit", "StartLimitIntervalSec") == "900" and one(bot, "Unit", "StartLimitBurst") == "5"
    assert one(bot, "Install", "WantedBy") == "multi-user.target"           # enabled: any boot starts it

    timers = {
        "titan2-bot.timer": (["Mon..Fri 08:45 Asia/Kolkata"], "true"),
        "titan2-token.timer": (["Mon..Fri 08:30 Asia/Kolkata"], "true"),
        "titan2-selfcheck.timer": (["Mon..Fri 08:40 Asia/Kolkata"], None),
        "titan2-backup.timer": (["Mon..Fri 16:10 Asia/Kolkata", "Sat 03:00 Asia/Kolkata"], None),
    }
    for n, (calendar, persistent) in timers.items():
        t = units[n]
        assert values(t, "Timer", "OnCalendar") == calendar, n
        assert values(t, "Timer", "Persistent") == ([persistent] if persistent else []), n
        assert one(t, "Timer", "Unit") == n.replace(".timer", ".service")
        assert one(t, "Install", "WantedBy") == "timers.target"

    oneshots = {
        "titan2-token.service": ("-m titan2 token", {"/var/lib/titan2/session"}),
        "titan2-selfcheck.service": ("-m titan2 selfcheck --phase morning", {"/var/lib/titan2/reports", "/var/lib/titan2/db"}),
        "titan2-backup.service": ("/home/ubuntu/titan/current/ops/backup.sh",
                                  {"/var/backups/titan2", "/var/lib/titan2/db", "/var/lib/titan2/backups"}),
    }
    for n, (cmd, rw) in oneshots.items():
        s = units[n]
        assert one(s, "Service", "Type") == "oneshot" and one(s, "Service", "User") == "titan", n
        assert one(s, "Service", "ExecStart").endswith(cmd), n
        assert set(one(s, "Service", "ReadWritePaths").split()) == rw, n
        assert not values(s, "Service", "Restart"), n
    assert not values(units["titan2-backup.service"], "Service", "EnvironmentFile")   # no credentials

    dash = units["titan2-dash.service"]
    assert one(dash, "Service", "User") == "titandash" and one(dash, "Service", "Restart") == "always"
    assert one(dash, "Service", "EnvironmentFile") == "/etc/titan2/dash.env"
    assert one(dash, "Service", "ExecStart") == ("/home/ubuntu/titan/current/venv/bin/gunicorn -w 1 --threads 4 "
                                                 "-k gthread -b 127.0.0.1:8050 titan2.dash.app:app")
    assert one(dash, "Service", "ReadWritePaths") == "/var/lib/titan2/dash"

    alert = units["titan2-alert@.service"]
    assert one(alert, "Service", "ExecStart") == ('/home/ubuntu/titan/current/venv/bin/python -m titan2 alert '
                                                  '--critical "unit %i failed"')
    assert not values(alert, "Unit", "OnFailure")                          # no alert loops

    for n, u in units.items():
        if n.endswith(".service"):
            assert one(u, "Service", "WorkingDirectory") == "/home/ubuntu/titan/agentic_trader", n
            assert one(u, "Service", "ProtectHome") == "read-only", n
            assert one(u, "Service", "ProtectSystem") == "strict", n
            assert one(u, "Service", "NoNewPrivileges") == "true", n
            for cmd in values(u, "Service", "ExecStart"):
                assert "/home/ubuntu/titan/current/" in cmd, n          # always the current release
            if n != "titan2-alert@.service":
                assert one(u, "Unit", "OnFailure") == ALERT, n
        directives = " ".join(f"{k}={v}" for section in u.values() for k, v in section)
        for v1 in ("titan-bot", "titan-dashboard", "autonomous_trader", "/home/titan/app", "0.0.0.0", ":5000",
                   "agentic_trader/.env", "/home/ubuntu/titan/venv/"):
            assert v1 not in directives, (n, v1)


# ---------------------------------------------------------------------------------------------------------------
# CI: tests only
# ---------------------------------------------------------------------------------------------------------------
def _code(text: str) -> str:
    """The workflow without its comments (no value in it contains '#')."""
    return "\n".join(line.split("#", 1)[0].rstrip() for line in text.splitlines())


def test_ci_no_deploy_no_secrets():
    text = CI.read_text(encoding="utf-8")
    code = _code(text)
    low = code.lower()
    for word in ("secrets", "ssh", "scp", "rsync", "deploy", "ec2", "aws", "sudo", "systemctl", "environment:",
                 "github_token", "id-token", "workflow_run", "pull_request_target"):
        assert word not in low, word
    assert re.search(r"(?m)^permissions:\n  contents: read$", code)
    assert "write" not in low
    on = re.search(r"(?ms)^on:\n(.*?)^\S", code).group(1)
    assert set(re.findall(r"(?m)^  ([a-z_]+):", on)) == {"push", "pull_request", "workflow_dispatch"}
    jobs = re.search(r"(?ms)^jobs:\n(.*)", code).group(1)
    assert set(re.findall(r"(?m)^  ([a-z][\w-]*):$", jobs)) == {"linux", "windows"}
    assert sorted(re.findall(r"runs-on: (\S+)", code)) == ["ubuntu-latest", "windows-latest"]
    assert re.search(r'python: \["3\.10", "3\.12"\]', code)
    assert 'python-version: "3.12"' in code
    uses = re.findall(r"uses: (\S+)", code)
    assert sorted(set(uses)) == ["actions/checkout@v4", "actions/setup-python@v5"]
    assert code.count("actions/checkout@v4") == code.count("persist-credentials: false") == 2
    installs = re.findall(r"run: (.*pip install.*)", code)
    assert installs and all("--require-hashes" in i and "-r requirements-dev.txt" in i for i in installs)
    runs = [r for r in re.findall(r"run: (.*)", code) if "pip install" not in r]
    assert runs == ["python -m pytest tests -q -p no:cacheprovider",
                    "python -m pytest tests/unit tests/arch -q -p no:cacheprovider"]


def test_only_the_ci_workflow_remains():
    """v1's deploy.yml restarted titan-bot on every push to main; U23 deleted it (Stage 0 step 1)."""
    names = sorted(p.name for p in CI.parent.iterdir())
    assert names == ["ci.yml"]


# ---------------------------------------------------------------------------------------------------------------
# Hash-locked requirements
# ---------------------------------------------------------------------------------------------------------------
def parse_requirements(path) -> list[tuple[str, str, list[str], str]]:
    """[(name, version, hashes, marker)]; '-r FILE' lines come back as ('-r', FILE, [], '')."""
    out, buf = [], ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not buf and (not line or line.startswith("#")):
            continue
        buf += " " + line.rstrip("\\").strip()
        if line.endswith("\\"):
            continue
        logical, buf = buf.strip(), ""
        if logical.startswith("-r "):
            out.append(("-r", logical[3:].strip(), [], ""))
            continue
        m = re.match(r"^([A-Za-z0-9._-]+)==([^\s;]+)\s*(?:;\s*([^-]*?))?\s*((?:--hash=sha256:[0-9a-f]{64}\s*)*)$", logical)
        assert m, f"{path.name}: not a pinned, hashed requirement: {logical[:120]}"
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", m.group(4))
        out.append((m.group(1).lower(), m.group(2), hashes, (m.group(3) or "").strip()))
    return out


def test_requirements_hash_locked():
    lock = parse_requirements(APP / "requirements.lock")
    dev = parse_requirements(APP / "requirements-dev.txt")
    names = [r[0] for r in lock]
    assert len(names) == len(set(names))
    for name, version, hashes, _ in lock:
        assert hashes, f"{name}=={version} has no --hash"
    pins = {r[0]: r[1] for r in lock}
    assert pins["kiteconnect"] == "5.0.1"
    assert {"requests", "pyotp", "flask", "gunicorn", "autobahn", "twisted"} <= set(pins)
    for banned in ("pandas", "numpy", "scipy", "openai", "schedule", "python-dotenv"):
        assert banned not in pins
    assert dev[0] == ("-r", "requirements.lock", [], "")
    dev_pins = {r[0]: r for r in dev[1:]}
    assert dev_pins["pytest"][1] and dev_pins["pytest"][2]
    assert not set(dev_pins) & set(pins), "a package is pinned twice"
    for name, version, hashes, _ in dev[1:]:
        assert hashes, f"{name}=={version} has no --hash"
    # Python 3.10 servers: txaio must not be one of the >= 3.11-only releases
    assert tuple(int(x) for x in pins["txaio"].split(".")[:2]) < (25, 12)


def test_every_third_party_import_is_locked():
    """A titan2 import missing from requirements.lock would fail only on the server (the deploy self-check's
    fresh-interpreter import); this catches it in CI."""
    import ast
    import sys

    dist = {"yaml": "pyyaml", "dateutil": "python-dateutil", "OpenSSL": "pyopenssl", "zope": "zope-interface"}
    pins = {r[0].replace("_", "-") for r in parse_requirements(APP / "requirements.lock")}
    found: dict[str, str] = {}
    for p in sorted((APP / "titan2").rglob("*.py")):
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import) else
                     [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module else [])
            for name in names:
                top = name.split(".")[0]
                if top != "titan2" and top not in sys.stdlib_module_names:
                    found.setdefault(top, p.relative_to(APP).as_posix())
    assert {"kiteconnect", "requests"} <= set(found)
    missing = {m: f for m, f in found.items() if dist.get(m, m).lower().replace("_", "-") not in pins}
    assert missing == {}, f"imported but not in requirements.lock: {missing}"


# ---------------------------------------------------------------------------------------------------------------
# Env examples
# ---------------------------------------------------------------------------------------------------------------
def test_env_examples_hold_the_spec_keys_and_no_secret():
    titan2 = (OPS / "titan2.env.example").read_text(encoding="utf-8")
    env = parse_env(titan2)                      # the CLI's own parser (titan2/token.py) reads it
    assert list(env) == ["ZERODHA_API_KEY", "ZERODHA_API_SECRET", "ZERODHA_USER_ID", "ZERODHA_PASSWORD",
                         "ZERODHA_TOTP_SECRET", "TOKEN_MODE", "ALLOW_LIVE", "REGISTERED_EGRESS_IP",
                         "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NTFY_TOPIC", "HEALTHCHECK_URL"]
    assert {k: v for k, v in env.items() if v} == {"TOKEN_MODE": "auto", "ALLOW_LIVE": "0"}
    assert set(re.findall(r"(?m)^#([A-Z0-9_]+)=$", titan2)) == {"BAN_LIST_URL", "BACKUP_S3_URI"}
    for absent in ("ACCESS_TOKEN", "TRADING_MODE", "REQUEST_TOKEN"):
        assert not [k for k in env if absent in k]
    dash = (OPS / "dash.env.example").read_text(encoding="utf-8")
    denv = parse_env(dash)
    assert denv == {"DASH_USER": "", "DASH_PASS_SCRYPT": "", "DASH_SECRET": ""}
    assert not re.search(r"(?m)^\s*ZERODHA_", dash)


# ---------------------------------------------------------------------------------------------------------------
# sudoers, logrotate, journald, the CLI wrapper
# ---------------------------------------------------------------------------------------------------------------
def test_sudoers_allow_list_is_narrow():
    text = (OPS / "sudoers.titan2").read_text(encoding="ascii")          # ASCII only: visudo-safe
    rules = sudo_rules()
    assert {r for r, _, _ in rules} == {"titan", "root"}
    for runas, path, args in rules:
        assert path.startswith("/") and "ALL" not in path + " " + args
        assert path.rsplit("/", 1)[-1] not in ("bash", "sh", "su", "sudoedit", "tee", "cp", "mv", "rm", "chmod",
                                               "chown", "ln", "dd", "env")
        assert "/etc/titan2" not in args
    assert not re.search(r"(?m)^[^#]*NOPASSWD:\s*ALL", text)
    # the CLI wrapper's command is exactly what TITAN2_CLI allows
    wrapper = (OPS / "titan2-cli").read_text(encoding="utf-8")
    m = re.search(r"exec sudo -n -u titan (\S+) -m titan2 \"\$@\"", wrapper)
    assert m and "cd /home/ubuntu/titan/agentic_trader && exec sudo" in wrapper
    assert sudo_allowed("titan", [m.group(1), "-m", "titan2", "status", "--json"], rules)
    assert sudo_allowed("titan", [m.group(1), "-m", "titan2", "backup", "--to", "/var/lib/titan2/backups/deploy-x"], rules)
    # what the deploy needs, and nothing broader
    py = "/home/ubuntu/titan/releases/abcdef123456/venv/bin/python"
    assert sudo_allowed("titan", [py, "-m", "titan2", "selfcheck", "--phase", "deploy"], rules)
    assert sudo_allowed("titan", [py, "-m", "titan2", "selfcheck", "--phase", "deploy", "--db-copy",
                                  "/var/lib/titan2/backups/deploy-abcdef123456-20261011T103000Z/titan2.db"], rules)
    assert not sudo_allowed("titan", [py, "-m", "titan2", "bot"], rules)
    assert not sudo_allowed("titan", ["/home/ubuntu/titan/releases/a/b/venv/bin/python", "-m", "titan2", "selfcheck",
                                      "--phase", "deploy"], rules)
    assert sudo_allowed("root", ["install", "-m", "0644", "/home/ubuntu/titan/releases/abc/ops/systemd/titan2-bot.service",
                                 "/etc/systemd/system/titan2-bot.service"], rules)
    assert not sudo_allowed("root", ["install", "-m", "0644", "/tmp/x", "/etc/systemd/system/titan2-bot.service"], rules)
    assert not sudo_allowed("root", ["install", "-m", "0644", "/home/ubuntu/titan/releases/abc/ops/systemd/titan2-bot.service",
                                     "/etc/systemd/system/ssh.service"], rules)
    for verb in ("daemon-reload", "restart titan2-bot", "restart titan2-dash", "reset-failed titan2-bot",
                 "stop titan2-bot", "set-environment TITAN2_SAFE=1", "unset-environment TITAN2_SAFE"):
        assert sudo_allowed("root", ["systemctl", *verb.split()], rules), verb
    for verb in ("restart ssh", "stop titan2-dash", "mask titan2-bot", "set-environment ALLOW_LIVE=1", "edit titan2-bot"):
        assert not sudo_allowed("root", ["systemctl", *verb.split()], rules), verb


def test_logrotate_and_journald():
    lr = (OPS / "logrotate.titan2").read_text(encoding="utf-8")
    body = re.search(r"(?ms)^/var/log/titan2/\*\.log \{\n(.*?)^\}", lr).group(1)
    directives = {line.strip() for line in body.splitlines() if line.strip()}
    assert directives == {"su titan titan", "daily", "rotate 60", "compress", "delaycompress", "missingok",
                          "notifempty", "create 0640 titan titan"}
    assert "SystemMaxUse=200M" in (OPS / "journald-titan2.conf").read_text(encoding="utf-8").splitlines()


@needs_bash
def test_shell_scripts_parse_and_use_lf():
    scripts = sorted(OPS.glob("*.sh")) + [OPS / "titan2-cli"]
    assert {p.name for p in scripts} >= {"deploy.sh", "rollback.sh", "pull_drift.sh", "bootstrap.sh",
                                         "stage0_cutover.sh", "backup.sh", "release.sh", "titan2-cli"}
    for p in sorted(OPS.rglob("*")):
        if p.is_file():
            assert b"\r" not in p.read_bytes(), f"{p.name} has CR line endings"
    for p in scripts:
        assert p.read_text(encoding="utf-8").startswith("#!"), p.name
        r = subprocess.run([BASH, "-n", bpath(p)], capture_output=True, text=True)
        assert r.returncode == 0, f"{p.name}: {r.stderr}"
    assert "* text eol=lf" in (OPS / ".gitattributes").read_text(encoding="utf-8")
