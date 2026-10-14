<#
.SYNOPSIS
    Optional: start ops\windows\run_today.ps1 by itself every weekday at 08:40 (Windows Task Scheduler).

.DESCRIPTION
    Registers the task "titan2 laptop session" for you: no admin rights, no stored password.
      - Monday to Friday at 08:40 India time (register it while Windows is on India Standard Time; Task Scheduler
        then keeps that moment even if the laptop's time zone changes).
      - Wakes the laptop from sleep to run it (Windows must allow wake timers).
      - Starts on battery too, and keeps running if the laptop switches to battery.
      - One copy at a time: while one is still running, a new start is skipped.
      - Starts as soon as it can after a missed start (the laptop was off at 08:40).
      - Normal priority. A 12-hour cap, so a window left open overnight never blocks the next morning.
      - Runs only while you are signed in (a locked screen is fine), in a normal window where Ctrl+C works.
    On a holiday or a weekend day run_today.ps1 says so and stops. Do not also start run_today.ps1 by hand on the same
    morning: the second copy refuses to start. Guide: docs\LAPTOP.md.

.PARAMETER Remove
    Delete the task.

.PARAMETER DryRun
    Show the task that would be registered, and change nothing.

.PARAMETER At
    Start time, HH:mm, in the laptop's time zone now (India Standard Time). Default 08:40.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File ops\windows\register_task.ps1
#>
param(
    [switch]$Remove,
    [switch]$DryRun,
    [string]$At = '08:40'
)

$ErrorActionPreference = 'Continue'      # the cmdlets that matter use -ErrorAction Stop inside try/catch
$TaskName = 'titan2 laptop session'
$App = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)       # agentic_trader\ (this file: ops\windows\register_task.ps1)
$RunToday = Join-Path $PSScriptRoot 'run_today.ps1'
$PowerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

function Fail([string]$Text) {
    Write-Host "FAILED: $Text" -ForegroundColor Red
    exit 1
}

if ($Remove) {
    if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
        Write-Host "No task named '$TaskName': nothing to remove."
        exit 0
    }
    if ($DryRun) {
        Write-Host "Would remove the task '$TaskName' (-DryRun: nothing changed)."
        exit 0
    }
    try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop } catch { Fail $_.Exception.Message }
    Write-Host "Removed the task '$TaskName'."
    exit 0
}

if (-not (Test-Path -LiteralPath $RunToday)) { Fail "$RunToday is missing: run this script from the repo" }
try {
    $time = [datetime]::ParseExact($At, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture)
} catch {
    Fail "-At must be HH:mm (24-hour laptop time), not '$At'"
}
if ((Get-TimeZone).BaseUtcOffset -ne [TimeSpan]::FromMinutes(330)) {
    Write-Warning "this laptop's time zone is $((Get-TimeZone).Id), not India Standard Time: $At here is not $At in India. Set the time zone to (UTC+05:30) Chennai, Kolkata, Mumbai, New Delhi, then register again."
}

$actionArgs = @{
    Execute          = $PowerShellExe
    Argument         = ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $RunToday)
    WorkingDirectory = $App
}
$action = New-ScheduledTaskAction @actionArgs
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $time
$settingsArgs = @{
    WakeToRun                  = $true                       # wake the laptop from sleep
    AllowStartIfOnBatteries    = $true                       # AC power is not required to start
    DontStopIfGoingOnBatteries = $true                       # never stopped for switching to battery
    MultipleInstances          = 'IgnoreNew'                 # one copy at a time
    StartWhenAvailable         = $true                       # run after a missed start
    ExecutionTimeLimit         = (New-TimeSpan -Hours 12)    # ends a window left open overnight
    Priority                   = 4                           # normal; the Task Scheduler default (7) is below normal
}
$settings = New-ScheduledTaskSettingsSet @settingsArgs
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$description = 'titan2 PAPER session on this laptop (ops\windows\run_today.ps1). Made by ops\windows\register_task.ps1; ' +
               'remove it with register_task.ps1 -Remove.'
$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description $description

$s = $task.Settings
$t = $task.Triggers[0]
$p = $task.Principal
try {        # Task Scheduler keeps the start in UTC, so it stays at the same India time whatever the laptop's zone later
    $india = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([datetime]::Parse($t.StartBoundary), 'India Standard Time')
    $indiaTime = $india.ToString('HH:mm')
} catch {
    $indiaTime = '?'
}
Write-Host "Task '$TaskName' for $User (logon $($p.LogonType), run level $($p.RunLevel): only while you are signed in):"
Write-Host "  when     days mask $($t.DaysOfWeek) (62 = Monday to Friday) at $indiaTime India time ($($t.StartBoundary)); a missed start runs later: $($s.StartWhenAvailable)"
Write-Host "  power    wakes the laptop: $($s.WakeToRun); starts on battery: $(-not $s.DisallowStartIfOnBatteries); stops on battery: $($s.StopIfGoingOnBatteries)"
Write-Host "  copies   $($s.MultipleInstances) (one at a time); time cap $($s.ExecutionTimeLimit); priority $($s.Priority)"
Write-Host "  runs     $PowerShellExe $($task.Actions[0].Arguments)"
Write-Host "  in       $App"
if ($DryRun) {
    Write-Host '-DryRun: nothing registered.'
    exit 0
}
try {
    Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force -ErrorAction Stop | Out-Null
} catch {
    Fail "Task Scheduler refused the task: $($_.Exception.Message)"
}
$next = (Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue).NextRunTime
Write-Host "Registered. Next start: $next. It opens the run_today.ps1 window; stop a session with Ctrl+C ONCE, then wait."
Write-Host 'Wake from sleep needs Power Options > Change advanced power settings > Sleep > Allow wake timers: Enable.'
Write-Host "Remove it with: powershell -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Remove"
exit 0
