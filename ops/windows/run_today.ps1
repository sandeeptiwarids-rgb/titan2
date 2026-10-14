<#
.SYNOPSIS
    Today's titan2 PAPER session on this laptop: python -m titan2 laptop, in this window.

.DESCRIPTION
    Every trading morning, start it between 08:40 and 09:10 and leave the window open. It checks that today is a
    trading day, logs in to Kite, runs the laptop self-check, trades on PAPER, closes everything at the end of the
    day, writes the daily report, and then waits for Enter. On a holiday or a weekend it says so and stops.

    To stop early: press Ctrl+C ONCE and wait. titan2 stops new entries, closes every open paper position, waits until
    it is flat, then shuts down. A second Ctrl+C stops it at once (open positions are recovered at the next start).
    Do not close this window while positions are open. One-time setup: ops\windows\setup.ps1. Guide: docs\LAPTOP.md.

.PARAMETER Check
    Run only the laptop self-check (python -m titan2 laptop check) and print each row.

.PARAMETER NoDash
    Do not serve the read-only dashboard (--no-dash).

.PARAMETER SkipToken
    Use the Kite session already saved today instead of logging in again (--skip-token).

.PARAMETER Date
    YYYY-MM-DD: simulate another day (--date; testing only).

.PARAMETER NoPause
    Close the window at the end instead of waiting for Enter.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File ops\windows\run_today.ps1
#>
param(
    [switch]$Check,
    [switch]$NoDash,
    [switch]$SkipToken,
    [string]$Date = '',
    [switch]$NoPause
)

$ErrorActionPreference = 'Continue'      # titan2 reports through its exit code, read after it ends
$App = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)       # agentic_trader\ (this file: ops\windows\run_today.ps1)
$Base = Join-Path $env:LOCALAPPDATA 'titan2'
$Venv = Join-Path $Base 'venv'
$Py = Join-Path $Venv 'Scripts\python.exe'
$Lock = Join-Path $App 'requirements.lock'
$Stamp = Join-Path $Venv 'titan2-requirements.sha256'               # written by setup.ps1 after a good install
$Logs = if ($env:TITAN2_LOG_DIR) { $env:TITAN2_LOG_DIR } else { Join-Path $Base 'logs' }

function Wait-Close {
    if (-not $NoPause) { [void](Read-Host 'Press Enter to close this window') }
}

function Initialize-Console {
    # 1. Ctrl+C on: a window started with Ctrl+C ignored (some launchers do this, and children inherit it) would never
    #    pass Ctrl+C to titan2. 2. QuickEdit off: a click in a classic console window starts a text selection that
    #    pauses every program writing to it, titan2 included, until Esc.
    $win32 = @'
[DllImport("kernel32.dll")] public static extern bool SetConsoleCtrlHandler(System.IntPtr handler, bool add);
[DllImport("kernel32.dll")] public static extern System.IntPtr GetStdHandle(int handle);
[DllImport("kernel32.dll")] public static extern bool GetConsoleMode(System.IntPtr handle, out uint mode);
[DllImport("kernel32.dll")] public static extern bool SetConsoleMode(System.IntPtr handle, uint mode);
'@
    try {
        if (-not ('Titan2.Win32' -as [type])) { Add-Type -Namespace Titan2 -Name Win32 -MemberDefinition $win32 -ErrorAction Stop }
        [void][Titan2.Win32]::SetConsoleCtrlHandler([IntPtr]::Zero, $false)
        $stdin = [Titan2.Win32]::GetStdHandle(-10)
        $mode = [uint32]0
        if ([Titan2.Win32]::GetConsoleMode($stdin, [ref]$mode)) {
            [void][Titan2.Win32]::SetConsoleMode($stdin, [uint32](($mode -bor 0x80) -band (-bnot 0x40)))   # EXTENDED_FLAGS on, QUICK_EDIT off
        }
    } catch {
        Write-Warning "could not prepare this console window ($($_.Exception.Message)); Ctrl+C once is still the way to stop"
    }
    try { $Host.UI.RawUI.WindowTitle = 'titan2 PAPER session - Ctrl+C once to stop, then wait' } catch { }
}

if (-not (Test-Path -LiteralPath $Py)) {
    Write-Host "titan2 is not set up for this Windows user ($Py is missing): run ops\windows\setup.ps1 first." -ForegroundColor Red
    Wait-Close
    exit 2
}
try {
    $want = (Get-FileHash -LiteralPath $Lock -Algorithm SHA256 -ErrorAction Stop).Hash
    $have = (Get-Content -LiteralPath $Stamp -TotalCount 1 -ErrorAction Stop).Trim()
    if ($have -ne $want) { Write-Warning 'requirements.lock changed since setup.ps1 last ran: run ops\windows\setup.ps1 again before the next session.' }
} catch {
    Write-Warning 'cannot tell whether the venv matches requirements.lock: run ops\windows\setup.ps1 again if titan2 does not start.'
}

# Activate the venv (what Scripts\Activate.ps1 does, minus the prompt) in a clean Python environment, as on the server
$env:VIRTUAL_ENV = $Venv
$env:PATH = (Join-Path $Venv 'Scripts') + ';' + $env:PATH
foreach ($name in 'PYTHONHOME', 'PYTHONPATH', 'PYTHONSAFEPATH') { Remove-Item -LiteralPath "Env:\$name" -ErrorAction SilentlyContinue }
$env:PYTHONUTF8 = '1'                                              # UTF-8 output, even when redirected
$env:PYTHONUNBUFFERED = '1'                                        # every line shows at once

$cli = @('-m', 'titan2', 'laptop')
if ($Check) {
    $cli += 'check'
    if ($NoDash -or $SkipToken -or $Date) { Write-Warning '-Check runs only the self-check: -NoDash, -SkipToken and -Date are ignored' }
} else {
    if ($NoDash) { $cli += '--no-dash' }
    if ($SkipToken) { $cli += '--skip-token' }
    if ($Date) { $cli += @('--date', $Date) }
}

Initialize-Console
Write-Host "titan2 laptop (PAPER only) $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'): python $($cli -join ' ')"
if (-not $Check) {
    Write-Host 'To stop early: press Ctrl+C ONCE and wait until it says it is done. Do not close this window.' -ForegroundColor Yellow
}
$started = $false
$code = 2
try {
    Push-Location -LiteralPath $App -ErrorAction Stop
    $started = $true
    & $Py @cli          # in the foreground, no pipe and no redirection: Ctrl+C reaches titan2 and this window waits for it
} finally {
    # Runs after titan2 has exited, also after Ctrl+C (PowerShell waits for titan2 first); reads its exit code here
    if ($started) {
        $code = $LASTEXITCODE
        Pop-Location
    }
    Write-Host ''
    $when = Get-Date -Format 'HH:mm:ss'
    if ($code -eq 0) {
        Write-Host "titan2 finished at $when (exit 0)." -ForegroundColor Green
    } elseif ($code -eq 73) {
        Write-Host "Another titan2 is already running on this laptop (exit 73): use that window. This one did nothing." -ForegroundColor Yellow
    } else {
        Write-Host "titan2 stopped at $when (exit $code). Read the lines above and today's log in $Logs (laptop-<date>.log)." -ForegroundColor Yellow
        if (-not $Check) {
            Write-Host 'If paper positions were open, run run_today.ps1 again now: it recovers them and resumes their exits first.' -ForegroundColor Yellow
        }
    }
    Wait-Close
    exit $code          # here, not after the block: after a Ctrl+C PowerShell runs nothing past this finally
}
