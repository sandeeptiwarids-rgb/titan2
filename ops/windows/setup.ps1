<#
.SYNOPSIS
    One-time setup of titan2 laptop mode (PAPER only) for the current Windows user. Safe to run again.

.DESCRIPTION
    1. Finds a 64-bit Python 3.10 to 3.13 (3.12, the tested version, first).
    2. Creates the venv %LOCALAPPDATA%\titan2\venv and installs requirements.lock into it: hash-checked wheels from
       PyPI only (pip --require-hashes --only-binary :all:, no pip config files, no PIP_* variables).
    3. Creates %LOCALAPPDATA%\titan2\var and %LOCALAPPDATA%\titan2\logs (TITAN2_VAR and TITAN2_LOG_DIR override).
    4. Copies ops\windows\titan2.env.example to %APPDATA%\titan2\titan2.env if that file does not exist yet
       (TITAN2_ENV_FILE overrides the path; an existing file is never overwritten), names the values still empty
       (never a value) and makes the file readable by you only (icacls).
    5. Runs the laptop self-check: python -m titan2 laptop check.
    Guide: docs\LAPTOP.md. Every trading morning: ops\windows\run_today.ps1.

.PARAMETER Python
    The python.exe to build the venv with, instead of searching (64-bit, 3.10 or newer).

.PARAMETER Recreate
    Delete the venv and build it again.

.PARAMETER SkipCheck
    Do not run the laptop self-check at the end.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File ops\windows\setup.ps1
#>
param(
    [string]$Python = '',
    [switch]$Recreate,
    [switch]$SkipCheck
)

$ErrorActionPreference = 'Continue'      # native tools report through exit codes, checked after each call
if (-not $env:LOCALAPPDATA -or -not $env:APPDATA) {
    Write-Host 'FAILED: LOCALAPPDATA or APPDATA is not set: run setup.ps1 as your normal Windows user' -ForegroundColor Red
    exit 1
}
$App = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)       # agentic_trader\ (this file: ops\windows\setup.ps1)
$Lock = Join-Path $App 'requirements.lock'
$Example = Join-Path $PSScriptRoot 'titan2.env.example'
$Base = Join-Path $env:LOCALAPPDATA 'titan2'
$Venv = Join-Path $Base 'venv'
$VenvPy = Join-Path $Venv 'Scripts\python.exe'
$Stamp = Join-Path $Venv 'titan2-requirements.sha256'               # run_today.ps1 warns when the lock has changed
$PipCache = Join-Path $Base 'pip-cache'
$Var = if ($env:TITAN2_VAR) { $env:TITAN2_VAR } else { Join-Path $Base 'var' }
$Logs = if ($env:TITAN2_LOG_DIR) { $env:TITAN2_LOG_DIR } else { Join-Path $Base 'logs' }
$EnvFile = if ($env:TITAN2_ENV_FILE) { $env:TITAN2_ENV_FILE } else { Join-Path $env:APPDATA 'titan2\titan2.env' }
$PyPI = 'https://pypi.org/simple'
# 3.12 first (the suite runs on 3.10 and 3.12); 3.14 and newer: requirements.lock has no wheels for them (cffi 1.17.1)
$Preferred = @('3.12', '3.11', '3.10', '3.13')
$KeyHelp = [ordered]@{
    ZERODHA_API_KEY     = 'needed: your Kite Connect app''s API key (developers.kite.trade)'
    ZERODHA_API_SECRET  = 'needed: that app''s API secret'
    ZERODHA_USER_ID     = 'needed: your Zerodha client id, like AB1234'
    ZERODHA_PASSWORD    = 'automatic login: your Kite password'
    ZERODHA_TOTP_SECRET = 'automatic login: your TOTP secret (the text key behind the 2FA QR code)'
    TELEGRAM_BOT_TOKEN  = 'optional: Telegram alerts (a bot made with @BotFather)'
    TELEGRAM_CHAT_ID    = 'optional: your Telegram chat id'
    NTFY_TOPIC          = 'optional: a long random name for ntfy phone alerts (docs/LAPTOP.md)'
}
$NeedKeys = @('ZERODHA_API_KEY', 'ZERODHA_API_SECRET', 'ZERODHA_USER_ID')
$Probe = "import sys, struct, venv, ensurepip; print('%d.%d %d %s' % (sys.version_info[0], sys.version_info[1], " +
         "struct.calcsize('P') * 8, getattr(sys, '_base_executable', '') or sys.executable))"

function Step([string]$Text) { Write-Host ''; Write-Host "== $Text" -ForegroundColor Cyan }

function Fail([string]$Text) {
    Write-Host "FAILED: $Text" -ForegroundColor Red
    exit 1
}

function Get-PythonInfo([string]$Exe, [string[]]$Pre = @()) {
    # Version, bits and the real python.exe behind $Exe (a venv's base, a py.exe pick), or $null.
    if ($Exe -like '*\WindowsApps\*') { return $null }     # Store aliases: open the Store; Store Python redirects AppData
    try { $out = & $Exe @Pre -c $Probe 2>$null } catch { return $null }
    if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
    $parts = "$out".Trim() -split ' ', 3
    if ($parts.Count -ne 3 -or $parts[2] -like '*\WindowsApps\*') { return $null }
    [pscustomobject]@{ Version = $parts[0]; Bits = $parts[1]; Exe = $parts[2] }
}

function Find-Python {
    # Every 64-bit Python 3.10-3.13 here: py.exe, the registry (PEP 514), the python.org folders, then PATH.
    $commands = New-Object System.Collections.ArrayList
    $launcher = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($launcher) {
        foreach ($v in $Preferred) { [void]$commands.Add(@($launcher.Source, "-$v")) }
    }
    foreach ($hive in 'HKCU:\Software\Python', 'HKLM:\Software\Python') {
        foreach ($tag in (Get-ChildItem $hive -ErrorAction SilentlyContinue | Get-ChildItem -ErrorAction SilentlyContinue)) {
            $ip = Get-ItemProperty -LiteralPath (Join-Path $tag.PSPath 'InstallPath') -ErrorAction SilentlyContinue
            if ($ip -and $ip.ExecutablePath) { [void]$commands.Add(@($ip.ExecutablePath)) }
            elseif ($ip -and $ip.'(default)') { [void]$commands.Add(@(Join-Path $ip.'(default)' 'python.exe')) }
        }
    }
    foreach ($pattern in "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:ProgramFiles\Python3*\python.exe") {
        foreach ($f in Get-ChildItem $pattern -ErrorAction SilentlyContinue) { [void]$commands.Add(@($f.FullName)) }
    }
    foreach ($c in Get-Command python.exe, python3.exe -CommandType Application -All -ErrorAction SilentlyContinue) {
        [void]$commands.Add(@($c.Source))
    }
    $found = @()
    $seen = @{}
    foreach ($cmd in $commands) {
        $info = Get-PythonInfo $cmd[0] @($cmd | Select-Object -Skip 1)
        if ($info -and -not $seen.ContainsKey($info.Exe.ToLower())) {
            $seen[$info.Exe.ToLower()] = $true
            $found += $info
        }
    }
    foreach ($v in $Preferred) {
        $best = $found | Where-Object { $_.Version -eq $v -and $_.Bits -eq '64' } | Select-Object -First 1
        if ($best) { return $best }
    }
    foreach ($f in $found) { Write-Host "  found Python $($f.Version) ($($f.Bits)-bit): $($f.Exe)" }
    Fail ("no 64-bit Python 3.10 to 3.13 found. Install Python 3.12 (64-bit) from " +
          "https://www.python.org/downloads/windows/ and run setup.ps1 again, or pass -Python C:\path\to\python.exe")
}

function Get-FilledKeys([string]$Path) {
    # Names of the keys that have a value. Values are only tested for being empty: never kept, never shown.
    $filled = @{}
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8 -ErrorAction Stop) {
        if ($line -match '^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') {
            $v = $Matches[2].Trim()
            if ($v.Length -ge 2 -and ($v[0] -eq '"' -or $v[0] -eq "'") -and $v[-1] -eq $v[0]) {
                $v = $v.Substring(1, $v.Length - 2)
            }
            $filled[$Matches[1]] = [bool]$v.Trim()
        }
    }
    $filled
}

function Protect-EnvFile([string]$Path) {
    # Only the current user may read or change the file: inherited and other explicit entries removed (icacls).
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & icacls.exe $Path /reset /Q | Out-Null
    $reset = $LASTEXITCODE
    & icacls.exe $Path /inheritance:r /grant:r ('*{0}:(F)' -f $sid) /Q | Out-Null
    $grant = $LASTEXITCODE
    $acl = Get-Acl -LiteralPath $Path
    $rules = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
    $others = @($rules | Where-Object { $_.IdentityReference.Value -ne $sid })
    if ($reset -ne 0 -or $grant -ne 0 -or -not $acl.AreAccessRulesProtected -or $others.Count -gt 0 -or -not $rules.Count) {
        Write-Host "  WARNING: could not make $Path private to you (icacls exit $reset/$grant): check it with: icacls `"$Path`"" -ForegroundColor Yellow
    } else {
        Write-Host "  readable by you only ($([System.Security.Principal.WindowsIdentity]::GetCurrent().Name))"
    }
}

# A clean Python environment, as the server's units have; UTF-8 output even when redirected
foreach ($name in 'PYTHONHOME', 'PYTHONPATH', 'PYTHONSAFEPATH') { Remove-Item -LiteralPath "Env:\$name" -ErrorAction SilentlyContinue }
$env:PYTHONUTF8 = '1'

Write-Host "titan2 laptop setup (PAPER only) for $([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)"
Write-Host "repo: $App"
if (-not (Test-Path -LiteralPath $Lock) -or -not (Test-Path -LiteralPath $Example)) {
    Fail "run this copy of setup.ps1 from the repo (agentic_trader\ops\windows): $Lock or $Example is missing"
}
$running = @(Get-Process -Name python -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $VenvPy })
if ($running.Count -gt 0) {
    Fail "titan2 is running from $Venv (process $($running[0].Id)): stop it first (Ctrl+C once in its window, then wait)"
}

Step 'Python'
if ($Python) {
    $base = Get-PythonInfo $Python
    if (-not $base) { Fail "$Python does not run, is a Microsoft Store alias, or lacks the venv and ensurepip modules" }
    if ($base.Bits -ne '64' -or [version]$base.Version -lt [version]'3.10') {
        Fail "$Python is Python $($base.Version) ($($base.Bits)-bit): titan2 needs 64-bit Python 3.10 or newer"
    }
    if ($Preferred -notcontains $base.Version) {
        Write-Host "  WARNING: Python $($base.Version): requirements.lock may have no wheels for it (tested: 3.10, 3.12)" -ForegroundColor Yellow
    }
} else {
    $base = Find-Python
}
Write-Host "  Python $($base.Version): $($base.Exe)"

Step "venv: $Venv"
try {
    if ($Recreate -and (Test-Path -LiteralPath $Venv)) {
        Write-Host '  removing the old venv (-Recreate)'
        Remove-Item -LiteralPath $Venv -Recurse -Force -ErrorAction Stop
    }
    if ((Test-Path -LiteralPath $Venv) -and -not (Get-PythonInfo $VenvPy)) {
        Write-Host '  the old venv does not run (was its Python removed?): building it again'
        Remove-Item -LiteralPath $Venv -Recurse -Force -ErrorAction Stop
    }
} catch {
    Fail "cannot remove $Venv ($($_.Exception.Message)): close every titan2 window and run setup.ps1 again"
}
if (Test-Path -LiteralPath $VenvPy) {
    Write-Host '  reusing it (-Recreate builds it again)'
} else {
    & $base.Exe -m venv $Venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPy)) { Fail "python -m venv $Venv failed (see above)" }
    Write-Host '  created'
}

Step 'packages: requirements.lock (hash-checked wheels from PyPI)'
if (-not (Test-Path -LiteralPath $Stamp)) { Write-Host '  first install: about 15 MB from PyPI, a few minutes' }
$pipArgs = @('-m', 'pip', 'install', '--isolated', '--disable-pip-version-check', '--no-input', '--cache-dir', $PipCache,
             '--index-url', $PyPI, '--only-binary', ':all:', '--require-hashes', '-r', $Lock)
$oldConfig = $env:PIP_CONFIG_FILE
$env:PIP_CONFIG_FILE = 'nul'                        # no pip.ini of any scope can add an index or change options
try {
    & $VenvPy @pipArgs
    $pipExit = $LASTEXITCODE
} finally {
    $env:PIP_CONFIG_FILE = $oldConfig
}
if ($pipExit -ne 0) {
    Fail "pip could not install $Lock (exit $pipExit, see above): check the internet connection and run setup.ps1 again"
}
Set-Content -LiteralPath $Stamp -Value (Get-FileHash -LiteralPath $Lock -Algorithm SHA256).Hash -Encoding Ascii

Step 'folders'
try {
    foreach ($dir in @($Var, $Logs, (Split-Path -Parent $EnvFile))) {
        New-Item -ItemType Directory -Force -Path $dir -ErrorAction Stop | Out-Null
        Write-Host "  $dir"
    }
} catch {
    Fail "cannot create a folder: $($_.Exception.Message)"
}

Step "env file: $EnvFile"
if (Test-Path -LiteralPath $EnvFile) {
    Write-Host '  kept as it is (setup.ps1 never overwrites it)'
} else {
    try { Copy-Item -LiteralPath $Example -Destination $EnvFile -ErrorAction Stop } catch { Fail "cannot create $EnvFile`: $($_.Exception.Message)" }
    Write-Host '  created from ops\windows\titan2.env.example'
}
Protect-EnvFile $EnvFile
try { $filled = Get-FilledKeys $EnvFile } catch { Fail "cannot read $EnvFile`: $($_.Exception.Message)" }
$empty = @($KeyHelp.Keys | Where-Object { -not $filled[$_] })
if ($empty.Count -gt 0) {
    Write-Host "  Still empty (fill them in with: notepad `"$EnvFile`"):"
    foreach ($k in $empty) { Write-Host ('    {0,-20} {1}' -f $k, $KeyHelp[$k]) }
    Write-Host '  Your old v1 values: agentic_trader/.env inside ..\_titan_ec2_backup\titan_backup_2026-06-10.tgz'
    Write-Host '  (the five ZERODHA_ names are the same; copy only those lines). Save, then: .\ops\windows\run_today.ps1 -Check'
} else {
    Write-Host '  every value is filled in'
}

$checkExit = 0
Step 'laptop self-check: python -m titan2 laptop check'
if ($SkipCheck) {
    Write-Host '  skipped (-SkipCheck)'
} else {
    if (@($empty | Where-Object { $NeedKeys -contains $_ }).Count -gt 0) {
        Write-Host '  The env_file and token rows fail until the Kite values are filled in: that is expected now.' -ForegroundColor Yellow
    }
    Push-Location -LiteralPath $App
    try {
        & $VenvPy -m titan2 laptop check
        $checkExit = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($checkExit -eq 0) {
        Write-Host '  passed (WARN rows are only warnings)' -ForegroundColor Green
    } else {
        Write-Host "  not passed (exit $checkExit): a FAIL row blocks new entries (never exits). Fix it, then run: .\ops\windows\run_today.ps1 -Check" -ForegroundColor Yellow
    }
}

Write-Host ''
Write-Host 'Setup done. Every trading morning between 08:40 and 09:10, from the repo folder, run:'
Write-Host '  .\ops\windows\run_today.ps1          (stop it with Ctrl+C ONCE, then wait; guide: docs\LAPTOP.md)'
exit $checkExit
