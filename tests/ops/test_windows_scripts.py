"""Static checks of the Windows laptop kit (ops/windows/, docs/LAPTOP.md): laptop mode is PAPER only.

The PowerShell scripts are read as text, never run: ASCII (Windows PowerShell 5.1 reads a script without a BOM in the
ANSI code page), LF endings (ops/.gitattributes), no secret, the commands each script must run, and installs only of
hash-checked wheels from PyPI. When Windows PowerShell or pwsh is on PATH, its parser also checks their syntax.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from opsharness import APP, OPS
from titan2 import selfcheck
from titan2 import token as token_mod
from titan2.token import parse_env

WIN = OPS / "windows"
SETUP, RUN_TODAY, REGISTER = WIN / "setup.ps1", WIN / "run_today.ps1", WIN / "register_task.ps1"
ENV_EXAMPLE = WIN / "titan2.env.example"
SCRIPTS = (SETUP, RUN_TODAY, REGISTER)
LAPTOP_DOC = APP / "docs" / "LAPTOP.md"
ENV_KEYS = token_mod.CREDENTIAL_KEYS + ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NTFY_TOPIC")
NEEDED_KEYS = ("ZERODHA_API_KEY", "ZERODHA_API_SECRET", "ZERODHA_USER_ID")
PYPI = "https://pypi.org/simple"
ALLOWED_URLS = {PYPI, "https://www.python.org/downloads/windows/"}
DOWNLOADERS = ("invoke-webrequest", "invoke-restmethod", "iwr ", "irm ", "curl", "wget", "start-bitstransfer",
               "net.webclient", "downloadfile", "downloadstring", "invoke-expression", "iex ")
INDEX_OPTIONS = ("--extra-index-url", "--trusted-host", "--find-links", "--index-url http:", "git+", "http://")
# A secret's shape: a Telegram bot token, a base32 TOTP secret, a Kite-style key (letters and digits), a hex secret
SECRET_SHAPES = (r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b", r"\b[A-Z2-7]{32}\b",
                 r"\b(?=[a-z0-9]*\d)(?=[a-z0-9]*[a-z])[a-z0-9]{16,32}\b", r"\b[0-9a-fA-F]{64}\b")


def text(path) -> str:
    return path.read_text(encoding="utf-8")


def code(path) -> str:
    """A script without its comments: the <# #> help block and whole-line # comments."""
    body = re.sub(r"(?s)<#.*?#>", "", text(path))
    return "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))


def block(src: str, start: str) -> str:
    """The brace-balanced block that follows the first `start` (a line such as 'finally {')."""
    i = src.index(start) + len(start)
    depth = 1
    for j in range(i, len(src)):
        depth += {"{": 1, "}": -1}.get(src[j], 0)
        if depth == 0:
            return src[i:j]
    raise AssertionError(f"unbalanced block after {start!r}")


def params(path) -> set[str]:
    """The names a script's param() block declares."""
    m = re.search(r"(?s)^param\((.*?)\n\)", code(path), re.M)
    assert m, f"{path.name}: no param() block"
    return set(re.findall(r"\$([A-Za-z]+)", m.group(1)))


# ---------------------------------------------------------------------------------------------------------------
# Every file: encoding, line endings, no secret
# ---------------------------------------------------------------------------------------------------------------
def test_kit_files_are_ascii_lf_without_bom():
    for p in SCRIPTS + (ENV_EXAMPLE,):
        raw = p.read_bytes()
        assert raw.isascii(), f"{p.name}: non-ASCII bytes (PowerShell 5.1 would misread them)"
        assert b"\r" not in raw, f"{p.name}: CR line endings (ops/.gitattributes keeps the ops tree LF)"
        assert raw.endswith(b"\n")
    assert not LAPTOP_DOC.read_bytes().startswith(b"\xef\xbb\xbf")
    assert "* text eol=lf" in text(OPS / ".gitattributes")


def test_kit_holds_no_secret():
    for p in SCRIPTS + (ENV_EXAMPLE, LAPTOP_DOC):
        body = text(p)
        for shape in SECRET_SHAPES:
            assert not re.search(shape, body), f"{p.name}: something shaped like a secret: {re.search(shape, body)}"
        assert not re.search(r"(?im)^\s*#?\s*(?:export\s+)?(ZERODHA|TELEGRAM|HEALTHCHECK|DASH)_\w+=\S", body), p.name
        assert not re.search(r"(?i)\$env:(ZERODHA|TELEGRAM|HEALTHCHECK|DASH)_\w+\s*=", body), p.name
        assert not re.search(r"(?i)access_token\s*[=:]\s*['\"]?\w{8,}", body), p.name
    for p in SCRIPTS:                           # the scripts never read the env file's values into their output
        src = code(p)
        assert "ZERODHA_ACCESS_TOKEN" not in src and "kite_session.json" not in src, p.name


# ---------------------------------------------------------------------------------------------------------------
# The env example
# ---------------------------------------------------------------------------------------------------------------
def test_env_example_holds_key_names_only():
    body = text(ENV_EXAMPLE)
    env = parse_env(body)                       # the parser the bot, the token step and the self-check use
    assert tuple(env) == ENV_KEYS
    assert all(v == "" for v in env.values())
    for absent in ("ACCESS_TOKEN", "REQUEST_TOKEN", "TRADING_MODE", "ALLOW_LIVE", "REGISTERED_EGRESS_IP", "TOKEN_MODE"):
        assert not [k for k in env if absent in k], absent
    assert not re.findall(r"(?m)^#\s*[A-Z][A-Z0-9_]*=", body), "no commented-out keys: names only"
    # the five Kite names are the Linux example's (and v1's), so old lines can be copied as they are
    linux = parse_env(text(OPS / "titan2.env.example"))
    assert set(token_mod.CREDENTIAL_KEYS) <= set(linux) and set(ENV_KEYS) <= set(linux)
    # where the old v1 values live, and the rules a Windows editor needs
    for needed in ("_titan_ec2_backup", "titan_backup_2026-06-10.tgz", "agentic_trader/.env", "ZERODHA_ACCESS_TOKEN",
                   r"%APPDATA%\titan2\titan2.env", "UTF-8", "setup.ps1"):
        assert needed in body, needed
    # the laptop self-check's env_file row asks for exactly these
    wanted = (getattr(selfcheck, "LAPTOP_ENV_KEYS", NEEDED_KEYS) + getattr(selfcheck, "LOGIN_ENV_KEYS", ())
              + getattr(selfcheck, "TELEGRAM_ENV_KEYS", ()))
    assert set(wanted) <= set(env)


# ---------------------------------------------------------------------------------------------------------------
# setup.ps1
# ---------------------------------------------------------------------------------------------------------------
def test_setup_installs_only_hash_checked_wheels_from_pypi():
    src = code(SETUP)
    installs = re.findall(r"'-m', 'pip', 'install'", src)
    assert len(installs) == 1, "exactly one pip install"
    for opt in ("'--require-hashes'", "'--only-binary', ':all:'", "'--index-url', $PyPI", "'--isolated'",
                "'--no-input'", "'-r', $Lock"):
        assert opt in src, opt
    assert f"$PyPI = '{PYPI}'" in src
    assert "$Lock = Join-Path $App 'requirements.lock'" in src
    assert "$env:PIP_CONFIG_FILE = 'nul'" in src                 # no pip.ini of any scope adds an index
    assert "& $VenvPy @pipArgs" in src and "-m venv $Venv" in src
    for p in SCRIPTS:
        low = code(p).lower()
        for bad in INDEX_OPTIONS + DOWNLOADERS:
            assert bad not in low, (p.name, bad)
        assert len(re.findall(r"pip\W+install", low)) == (1 if p == SETUP else 0), p.name
        urls = set(re.findall(r"https?://[^\s'\"`)]+", text(p)))
        assert urls <= ALLOWED_URLS, (p.name, urls - ALLOWED_URLS)


def test_setup_python_choice():
    src = code(SETUP)
    m = re.search(r"\$Preferred = @\(([^)]*)\)", src)
    versions = re.findall(r"'(\d+)\.(\d+)'", m.group(1))
    assert versions[0] == ("3", "12"), "3.12, the tested version, first"
    assert all((3, 10) <= (int(a), int(b)) < (3, 14) for a, b in versions), "requirements.lock has no 3.14 wheels"
    assert "[version]'3.10'" in src and "'64'" in src                       # -Python: 64-bit 3.10 or newer
    assert r"'*\WindowsApps\*'" in src                                       # never a Store alias or Store Python
    for source in ("py.exe", "HKCU:\\Software\\Python", "HKLM:\\Software\\Python", "Programs\\Python",
                   "python.exe, python3.exe"):
        assert source in src, source


def test_setup_paths_env_file_and_check():
    src = code(SETUP)
    for line in ("$Base = Join-Path $env:LOCALAPPDATA 'titan2'", "$Venv = Join-Path $Base 'venv'",
                 "$Var = if ($env:TITAN2_VAR) { $env:TITAN2_VAR } else { Join-Path $Base 'var' }",
                 "$Logs = if ($env:TITAN2_LOG_DIR) { $env:TITAN2_LOG_DIR } else { Join-Path $Base 'logs' }",
                 "$EnvFile = if ($env:TITAN2_ENV_FILE) { $env:TITAN2_ENV_FILE } else { Join-Path $env:APPDATA "
                 "'titan2\\titan2.env' }",
                 "$Example = Join-Path $PSScriptRoot 'titan2.env.example'"):
        assert line in src, line
    # the env file is copied only when missing, never with -Force
    copy = re.findall(r"Copy-Item [^}\n]*", src)
    assert len(copy) == 1 and copy[0].startswith("Copy-Item -LiteralPath $Example -Destination $EnvFile ")
    assert "-Force" not in copy[0]
    assert src.index("if (Test-Path -LiteralPath $EnvFile)") < src.index("Copy-Item")
    # readable by the current user only
    protect = block(src, "function Protect-EnvFile([string]$Path) {")
    assert "WindowsIdentity]::GetCurrent().User.Value" in protect
    assert "& icacls.exe $Path /reset" in protect
    assert "& icacls.exe $Path /inheritance:r /grant:r ('*{0}:(F)' -f $sid)" in protect
    assert "AreAccessRulesProtected" in protect
    # the values still empty are named, never shown
    keys = re.findall(r"(?m)^\s+([A-Z][A-Z0-9_]+)\s+=", block(src, "$KeyHelp = [ordered]@{"))
    assert tuple(keys) == ENV_KEYS
    assert re.search(r"\$NeedKeys = @\('ZERODHA_API_KEY', 'ZERODHA_API_SECRET', 'ZERODHA_USER_ID'\)", src)
    assert set(NEEDED_KEYS) == set(getattr(selfcheck, "LAPTOP_ENV_KEYS", NEEDED_KEYS))
    filled = block(src, "function Get-FilledKeys([string]$Path) {")
    assert "$filled[$Matches[1]] = [bool]$v.Trim()" in filled
    assert not re.search(r"(?i)write-(host|output)|\bout-", filled), "Get-FilledKeys must never print"
    assert "_titan_ec2_backup\\titan_backup_2026-06-10.tgz" in src
    # then the laptop self-check, from the repo folder
    assert "Push-Location -LiteralPath $App" in src and "& $VenvPy -m titan2 laptop check" in src
    for d in ("$Var", "$Logs", "(Split-Path -Parent $EnvFile)"):
        assert d in block(src, "Step 'folders'\ntry {"), d
    assert "$Stamp = Join-Path $Venv 'titan2-requirements.sha256'" in src
    assert params(SETUP) == {"Python", "Recreate", "SkipCheck"}


# ---------------------------------------------------------------------------------------------------------------
# run_today.ps1
# ---------------------------------------------------------------------------------------------------------------
def test_run_today_runs_the_laptop_session_in_the_foreground():
    src = code(RUN_TODAY)
    assert "$Venv = Join-Path $Base 'venv'" in src and "$Py = Join-Path $Venv 'Scripts\\python.exe'" in src
    # the venv is activated, in a clean Python environment
    assert "$env:VIRTUAL_ENV = $Venv" in src and "$env:PATH = (Join-Path $Venv 'Scripts') + ';' + $env:PATH" in src
    for name in ("'PYTHONHOME'", "'PYTHONPATH'", "'PYTHONSAFEPATH'"):
        assert name in src, name
    assert "$env:PYTHONUTF8 = '1'" in src
    # python -m titan2 laptop [check | --no-dash --skip-token --date D], from the repo folder
    assert "$cli = @('-m', 'titan2', 'laptop')" in src
    for opt in ("$cli += 'check'", "$cli += '--no-dash'", "$cli += '--skip-token'", "$cli += @('--date', $Date)"):
        assert opt in src, opt
    assert params(RUN_TODAY) == {"Check", "NoDash", "SkipToken", "Date", "NoPause"}
    assert "Push-Location -LiteralPath $App -ErrorAction Stop" in src
    # no pipe and no redirection on the titan2 call: Ctrl+C must reach it and the window must wait for it
    calls = [line.split("#", 1)[0] for line in src.splitlines() if "& $Py" in line]
    assert calls and all(not re.search(r"[|>]", c) for c in calls), calls
    # after titan2 exits (also after a Ctrl+C) the window stays open and passes titan2's exit code on
    fin = block(src, "} finally {")
    leave = re.search(r"(?m)^\s*exit \$code\b", fin)
    assert "$code = $LASTEXITCODE" in fin and leave and re.search(r"(?m)^\s*Wait-Close$", fin[:leave.start()])
    assert not re.search(r"(?m)^exit ", src[src.index("} finally {"):][len(fin):]), "exit inside the finally block"
    assert "Read-Host 'Press Enter to close this window'" in block(src, "function Wait-Close {")
    # Ctrl+C handling on for this window and its children, QuickEdit off
    console = block(src, "function Initialize-Console {")
    assert "SetConsoleCtrlHandler([IntPtr]::Zero, $false)" in console
    assert "-band (-bnot 0x40)" in console
    assert src.index("Initialize-Console\n") < src.index("& $Py @cli")
    assert "ALLOW_LIVE" not in src and "TITAN2_PROFILE" not in src     # the laptop verb sets the profile itself


def test_scripts_share_one_layout():
    for p in SCRIPTS:
        assert "$App = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)" in code(p), p.name
    for p in (SETUP, RUN_TODAY):
        src = code(p)
        assert "$Base = Join-Path $env:LOCALAPPDATA 'titan2'" in src, p.name
        assert "$Stamp = Join-Path $Venv 'titan2-requirements.sha256'" in src, p.name
        assert "$Logs = if ($env:TITAN2_LOG_DIR) { $env:TITAN2_LOG_DIR } else { Join-Path $Base 'logs' }" in src
    assert "Join-Path $PSScriptRoot 'run_today.ps1'" in code(REGISTER)


def test_requirements_lock_still_installs_on_windows():
    """setup.ps1 installs requirements.lock as it is: it must keep its Windows-only pins (verified 2026-10-11 in
    scratch venvs: CPython 3.10, 3.12 and 3.13 install it with --require-hashes --only-binary :all:; 3.14 does not)."""
    lock = text(APP / "requirements.lock")
    assert re.search(r"(?m)^pywin32==\S+ ; sys_platform == 'win32' \\$", lock)
    assert re.search(r"(?m)^colorama==\S+ ; sys_platform == 'win32' \\$", lock)
    assert "Windows" in lock[:2000], "the lock's header must still say it is universal (Linux and Windows)"


# ---------------------------------------------------------------------------------------------------------------
# register_task.ps1
# ---------------------------------------------------------------------------------------------------------------
def test_register_task_settings():
    src = code(REGISTER)
    assert "$At = '08:40'" in src
    assert "-Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $time" in src
    settings = " ".join(block(src, "$settingsArgs = @{").split())
    for line in ("WakeToRun = $true", "AllowStartIfOnBatteries = $true", "DontStopIfGoingOnBatteries = $true",
                 "MultipleInstances = 'IgnoreNew'", "StartWhenAvailable = $true",
                 "ExecutionTimeLimit = (New-TimeSpan -Hours 12)", "Priority = 4"):
        assert line in settings, line
    assert "New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited" in src
    action = " ".join(block(src, "$actionArgs = @{").split())
    assert "Execute = $PowerShellExe" in action and "WorkingDirectory = $App" in action
    assert "Argument = ('-NoProfile -ExecutionPolicy Bypass -File \"{0}\"' -f $RunToday)" in action
    assert "$PowerShellExe = Join-Path $env:SystemRoot 'System32\\WindowsPowerShell\\v1.0\\powershell.exe'" in src
    for bad in ("Highest", "-Password", "S4U", "ServiceAccount", "Saturday", "Sunday", "-Hidden", "RunOnlyIfIdle"):
        assert bad not in src, bad
    assert "Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false" in src
    assert src.index("if ($DryRun) {\n    Write-Host '-DryRun: nothing registered.'") < src.index("Register-ScheduledTask -TaskName")
    assert params(REGISTER) == {"Remove", "DryRun", "At"}


# ---------------------------------------------------------------------------------------------------------------
# The PowerShell parser (only where PowerShell exists; nothing is executed)
# ---------------------------------------------------------------------------------------------------------------
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


@pytest.mark.skipif(POWERSHELL is None, reason="needs Windows PowerShell or pwsh")
def test_scripts_parse_in_powershell():
    lines = []
    for p in SCRIPTS:
        path = str(p).replace("'", "''")
        lines.append(f"$t = $null; $e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                     f"'{path}', [ref]$t, [ref]$e); 'PARSED {p.name} ' + $e.Count; "
                     f"$e | ForEach-Object {{ '  ' + $_.Extent.StartLineNumber + ': ' + $_.Message }}")
    r = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", "; ".join(lines)],
                       capture_output=True, text=True, timeout=180)
    for p in SCRIPTS:
        assert f"PARSED {p.name} 0" in r.stdout, r.stdout + r.stderr


# ---------------------------------------------------------------------------------------------------------------
# docs/LAPTOP.md
# ---------------------------------------------------------------------------------------------------------------
_SPAN = re.compile(r"(`+)(.+?)\1")
_REPO_PATH = re.compile(r"(?:titan2|tests|ops|docs)/[\w.-][\w./-]*")
DOC_SECTIONS = ("1. Install once", "2. Fill in the env file (your secrets)", "3. Every trading morning",
                "4. How to stop safely", "5. Keep the laptop awake and online", "6. Where things are",
                "7. Commands from a second window (optional)", "8. What PAPER means",
                "9. Why LIVE is refused on the laptop", "10. After your first session: what to check",
                "11. Optional: start it by itself every weekday", "12. If something goes wrong")


def _doc_parts() -> tuple[list[str], list[str]]:
    """(prose lines, fenced code lines) of docs/LAPTOP.md."""
    prose, fenced, inside = [], [], False
    for line in text(LAPTOP_DOC).splitlines():
        if line.strip().startswith("```"):
            inside = not inside
            continue
        (fenced if inside else prose).append(line)
    return prose, fenced


def test_laptop_doc_covers_the_quickstart():
    doc = text(LAPTOP_DOC)
    heads = re.findall(r"(?m)^## (.+)$", doc)
    assert tuple(heads) == DOC_SECTIONS
    for needed in ("ops/windows/setup.ps1", "ops/windows/run_today.ps1", "ops/windows/register_task.ps1",
                   r"%APPDATA%\titan2\titan2.env", r"%LOCALAPPDATA%\titan2\var\reports\<date>.md",
                   r"%LOCALAPPDATA%\titan2\logs\laptop-<date>.log", "Ctrl+C once", "Ctrl+C a second time",
                   "09:10", "plugged in", "Lid open", "Sync now", "static IP", "backstop", "PAPER",
                   "titan_backup_2026-06-10.tgz", "http://127.0.0.1:5000", "exit 73"):
        assert needed in doc, needed
    keys = [m.group(1) for m in re.finditer(r"(?m)^\| `([A-Z][A-Z0-9_]+)` \|", doc)]
    assert tuple(keys) == ENV_KEYS


def test_laptop_doc_spans_paths_and_commands_are_real():
    prose, fenced = _doc_parts()
    broken = [line for line in prose if "`" in _SPAN.sub("", line)]
    assert not broken, f"inline code broken across lines: {broken}"
    snippets = [m.group(2) for line in prose for m in _SPAN.finditer(line)] + fenced
    for tok in (t.strip("`'\",;:()") for s in snippets for t in s.split()):
        if _REPO_PATH.fullmatch(tok) or tok in ("requirements.lock", "TITAN_INSTRUCTIONS.md"):
            assert (APP / tok).exists(), f"docs/LAPTOP.md quotes a path that does not exist: {tok}"
    # `python -m titan2 VERB` anywhere, and two-word spans such as `titan2 resume` (not quoted output lines)
    verbs = set(re.findall(r'add_parser\(\s*"([a-z][a-z0-9-]*)"', text(APP / "titan2" / "__main__.py")))
    verbs |= {"laptop", "dash"}                         # the laptop interface's verbs
    used = {m.group(1) for s in snippets for m in re.finditer(r"-m titan2 ([a-z][a-z0-9-]*)", s)}
    used |= {m.group(1) for s in snippets for m in [re.fullmatch(r"titan2 ([a-z][a-z0-9-]*)", s.strip())] if m}
    assert {"status", "pause", "resume", "token", "report"} <= used, used
    assert used <= verbs, f"unknown titan2 verbs in docs/LAPTOP.md: {sorted(used - verbs)}"
    dash_helpers = set(re.findall(r"-m titan2\.dash\.app ([a-z-]+)", "\n".join(snippets)))
    assert dash_helpers == {"hash-password", "new-secret"}
    app_src = text(APP / "titan2" / "dash" / "app.py")
    assert all(f'argv == ["{h}"]' in app_src for h in dash_helpers)
    # the script switches the guide names exist
    doc = text(LAPTOP_DOC)
    for script, switch in ((RUN_TODAY, "Check"), (RUN_TODAY, "NoDash"), (SETUP, "Recreate"), (REGISTER, "DryRun"),
                           (REGISTER, "Remove")):
        assert f"-{switch}" in doc and switch in params(script), (script.name, switch)
