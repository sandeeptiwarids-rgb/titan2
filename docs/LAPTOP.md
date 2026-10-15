# titan2 on a Windows laptop (PAPER only)

titan2 can run a whole trading day in one window on this laptop. It reads live Kite market data and trades on
PAPER: every order is simulated, nothing is ever sent to Zerodha, and no money is at risk. The laptop never trades
LIVE (section 9 says why). How titan2 works is in [TITAN_INSTRUCTIONS.md](../TITAN_INSTRUCTIONS.md); the server
procedures are in the [RUNBOOK](RUNBOOK.md).

| When | What you do | Section |
|---|---|---|
| Once | Install, then fill in the env file | 1, 2 |
| Every trading morning, 08:40 to 09:10 | Start `ops/windows/run_today.ps1` and leave its window open | 3 |
| To stop early | Ctrl+C once, then wait | 4 |
| After the close (about 15:40) | Read the daily report | 6, 10 |

## 1. Install once

You need Windows 11, an internet connection and a 64-bit Python 3.10 to 3.13 (3.12 is the tested one). This laptop
already has Anaconda's Python 3.12, which setup finds by itself. On another laptop, install Python 3.12 (64-bit) from
python.org first; Python 3.14 does not work yet.

Open PowerShell (Start, type `PowerShell`, Enter) and run:

```powershell
cd $env:USERPROFILE\MCX\agentic_trader
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\windows\setup.ps1
```

What `ops/windows/setup.ps1` does (2 to 5 minutes the first time, seconds after that):

1. Finds Python and builds titan2's own Python environment in `%LOCALAPPDATA%\titan2\venv`. It installs exactly
   the packages in `requirements.lock`, checked against their fingerprints (hashes), from PyPI only.
2. Creates the folders `%LOCALAPPDATA%\titan2\var` (titan2's state) and `%LOCALAPPDATA%\titan2\logs`.
3. Creates your env file `%APPDATA%\titan2\titan2.env` from `ops/windows/titan2.env.example`, if it does not exist
   yet. It never overwrites an existing one. It makes the file readable by you only, and lists the values that are
   still empty (names only, never values).
4. Runs the laptop self-check and prints each row.

Run it again any time: after `requirements.lock` changes (`run_today.ps1` warns you), or with `-Recreate` to
rebuild the Python environment from scratch. Do not run it while a session is running.

Scripts are allowed on this laptop, so `.\ops\windows\setup.ps1` also works on its own. If Windows ever says
"running scripts is disabled on this system", use the long `powershell -NoProfile -ExecutionPolicy Bypass -File`
form shown above.

## 2. Fill in the env file (your secrets)

Open it with Notepad:

```powershell
notepad "$env:APPDATA\titan2\titan2.env"
```

| Key | What it is | Needed for |
|---|---|---|
| `ZERODHA_API_KEY` | Your Kite Connect app's API key (developers.kite.trade, My apps) | every day |
| `ZERODHA_API_SECRET` | That app's API secret | every day |
| `ZERODHA_USER_ID` | Your Zerodha client id, like AB1234 | every day |
| `ZERODHA_PASSWORD` | Your Kite password | the automatic morning login |
| `ZERODHA_TOTP_SECRET` | Your TOTP secret: the text key behind the 2FA QR code, not a 6-digit code | the automatic morning login |
| `TELEGRAM_BOT_TOKEN` | A Telegram bot's token, from @BotFather | optional alerts and the daily report |
| `TELEGRAM_CHAT_ID` | Your chat id: send your bot a message, then open `https://api.telegram.org/bot<token>/getUpdates` and copy the number after `"chat":{"id":` | optional alerts and the daily report |
| `NTFY_TOPIC` | A long random name (20-64 letters, digits, `-`, `_`) for the free ntfy phone app: install ntfy, tap +, subscribe to this name on ntfy.sh. Anyone who knows the name can read the alerts, so keep it private | phone alerts: the morning start card, trades, problems and the daily report (instead of, or as well as, Telegram) |
| `HEALTHCHECK_URL` | The ping URL of a free healthchecks.io check, the dead-man: schedule `15-59 9 * * 1-5`, `* 10-14 * * 1-5` and `0-33 15 * * 1-5`, time zone Asia/Kolkata, grace 3 minutes, and its ntfy integration (your topic) so the same phone rings. titan2 pings it every minute from the moment it is ready, a few minutes after you start it, until it stops at about 15:40; when the pings stop (the laptop slept, lost power, or titan2 stopped) healthchecks rings. The schedule begins at 09:15 so that a start by 09:10 is in time (the server's check begins at 08:45: its bot starts by itself then). Pause the check on NSE holidays (it cannot know them), and when you stop titan2 before 15:33 pause it after titan2 has stopped, within a minute (not before: titan2 pings it while it stops; section 4); its next ping resumes it. It is a password too: keep it private | an alarm when titan2 stops (recommended: without it nothing outside the laptop notices) |

**Your old v1 values** are in the file `agentic_trader/.env` inside
`%USERPROFILE%\MCX\_titan_ec2_backup\titan_backup_2026-06-10.tgz`. Open the `.tgz` in File Explorer or
7-Zip (do not extract it into the repo). v1 used the same five `ZERODHA_` names, so copy those five lines as they are.
Never copy `ZERODHA_ACCESS_TOKEN`, `PAPER_MODE` or any other v1 line. If you rotated a secret since then (RUNBOOK
section 8, Stage 0 step 4: a new API secret, a new password, a re-enrolled TOTP), use the new value. v1 had no
Telegram keys.

Rules for the file:

- One `KEY=value` per line, the value right after `=`, nothing after it (no comment on a value line). No quotes are
  needed, even when a value contains `$`, `#` or spaces.
- Save with Ctrl+S in Notepad, which saves UTF-8 as titan2 needs. Do not make the file with `>` or `Out-File` in
  PowerShell (they write UTF-16), and do not use Save As (Notepad would add `.txt` to the name).
- Without the password and TOTP secret, titan2 shows a Kite login link every morning and asks you to paste the
  request token (section 3).
- Keep it private: never copy it into the repo, a chat, an email or a cloud drive.

Then check:

```powershell
.\ops\windows\run_today.ps1 -Check
```

Every row should say PASS (WARN is only a warning). Rows that need today's Kite login or today's data, such as
`token` and `instruments`, can fail until the first session of the day has run: that is expected. To test your Kite
values the evening before, log in once with `& $t2 -m titan2 token` (section 7): it says whether the login worked.

## 3. Every trading morning

Before you start: the laptop is plugged in, the lid is open, the internet is stable, and the clock is synced
(section 5).

Between 08:40 and 09:10, run:

```powershell
cd $env:USERPROFILE\MCX\agentic_trader
.\ops\windows\run_today.ps1
```

or right-click `run_today.ps1` in File Explorer and choose **Run with PowerShell**. titan2 needs a few minutes to log
in, check itself, load the day's instrument list and recover anything left from an earlier run before the market
opens at 09:15; it looks for entries from 09:20. Leave the window open all day. On a weekend or an NSE holiday it
says "no NSE trading session" and stops.

What you will see, in order:

1. `titan2 laptop: PAPER only`, with the state, logs and env file folders, then today's session hours.
2. The Kite login: `token: today's Kite session works`, or `token: logging in to Kite (password + TOTP)` then
   `token: OK`. If the automatic login cannot run, it prints a Kite login link: open it in a browser, log in, copy
   the whole address the browser lands on (it contains `request_token=`), paste it into the window and press Enter.
3. The self-check, one row per check: PASS, WARN or FAIL. A FAIL keeps new entries off; the session still runs and
   still manages exits. Fix the cause, stop (Ctrl+C once) and start again.
4. `keep-awake on`, the dashboard address (section 7), and `PAPER session starting`.
5. On your phone, one **start card** (`START_CARD`) a minute or two later, once titan2 has prepared the day and
   checked itself again: PAPER only, any FAIL or WARN self-check row, the clock, the network, AC or battery, each
   strategy's mode and version, and the condor. **No card by 09:15 means titan2 is not running**: look at the window.
   A restart that titan2 makes by itself comes as `BOT_RESTARTED` (a WARN after a crash, an INFO after a controlled
   restart: a new Kite session, or the market feed gave up), and the card still comes once, from the first run that
   gets that far; after the fifth crash restart it stops and says so (`BOT_GAVE_UP`: open positions are not managed;
   start `run_today.ps1` again at once).
6. Log lines all day. A quiet day with no trades is normal: the entry rules are strict on purpose, and the phone
   stays quiet too, which is why the start card (and the dead-man, `HEALTHCHECK_URL`) matter. The phone also hears
   of: each trade opened and closed (a placebo's begin "control trade (placebo), not a strategy result": it is the
   G1 honesty check of section 8, not a strategy's trade, so ignore its profit or loss); a switch from the charger
   to battery (`POWER`, then once more when it is back on AC); a day G1 marks as disturbed (`SESSION_IMPAIRED`, with
   why). A warning about "Kite account check (no LIVE trading here)" is titan2 reading the Kite account it shares
   with EC2: no real-money trade is involved.
7. From 15:15 it closes every position. At 15:38 the phone gets the day's report: the trades, the halts, the
   refusals by reason, the what-if totals saved so far (today's runs after the session), the condor, the bar check,
   the alerts that were delayed or never delivered, and for each strategy "today counts for G1: yes" or "no" with
   why. At about 15:40 the bot stops: the window shows `titan2: the trading day is done`, the daily report, and then
   `Press Enter to close this window`.

## 4. How to stop safely

- **Ctrl+C once, then wait.** No new entries from that second on; every open paper position is closed through the
  normal exit engine. The window shows the progress (`flatten #N ...: k open leg(s)`) until `verified flat`, then
  titan2 stops cleanly. Wait for `Press Enter to close this window`. If nothing is open it stops within a few
  seconds. Entries stay off until titan2 has stopped; the next start trades normally, with nothing to resume.
- **Ctrl+C a second time** stops now, cleanly: entries stop, entry orders cancel, exits already sent finish within
  about 20 seconds. Open positions stay open in the paper books, and the next start recovers them and resumes
  their exits before anything else. If the second press came while positions were being closed, the next start
  keeps closing them and keeps entries off until you run `titan2 resume` (section 7).
- After an early stop with positions open during market hours, start `run_today.ps1` again soon.
- **The dead-man check.** When you stop titan2 before 15:33, pause its check on healthchecks.io (the check's
  Pause button) after titan2 has stopped: once the window says `titan2 stopped cleanly`, within a minute (open the
  check's page before you press Ctrl+C). Not before: titan2 pings the check while it stops (every minute while it
  closes positions, and once more as it exits), and a ping resumes a paused check. Unpaused, it rings your phone 3
  to 4 minutes after that last ping. Its next ping, at your next start, resumes it. Leave it running otherwise: a
  crash or a sleeping laptop is what it is there to catch.
- **Stopping early and the G1 test.** G1 marks the day as disturbed (IMPAIRED) for every strategy, not only for the
  one holding a position, when you press Ctrl+C while any position is open (before the 15:15 close), and when titan2
  stays stopped for more than 10 minutes of entry time (09:20 to 14:55). To leave early without the mark, wait until
  the dashboard's Open positions panel is empty, then press Ctrl+C at 14:46 or later, or start again within a few
  minutes. After 15:15 a stop never marks the day. Too many marked days keep a strategy from passing G1 (section 8).
- Do not close the window with its X, sign out, shut down or let the laptop sleep while positions are open. Windows
  gives a closing window about 5 seconds: titan2 tries to close positions for 3 of them, then stops; whatever is
  still open waits for the next start.
- titan2 is never left half-stopped: the window either shows the closing progress or has finished.
- To pause new entries without stopping, use `titan2 pause` from a second window (section 7); `titan2 resume` undoes
  it. Exits always continue. A pause before 15:15 marks that day IMPAIRED for every strategy, open positions or not.

## 5. Keep the laptop awake and online

The self-check reads the four settings below that can end a session (as a standard user, no admin rights) and
WARNs on each one that is wrong: `lid`, `restart_pending`, `update_hours` and `wifi`. They never block entries and
send no message of their own: the start card on your phone lists them. One it cannot read is SKIP.

- **Plugged in.** titan2 logs the power source at the start and every 15 minutes, with a warning on battery, and
  looks at it every 30 seconds: a switch from the charger to battery sends `POWER` to your phone (and one more when
  it is back on AC).
- **Lid open**, or Control Panel, Power Options, "Choose what closing the lid does": When plugged in, Do nothing.
  The `lid` row WARNs otherwise (on 2026-10-14 it was set to Sleep). That setting can be hidden from Control Panel;
  then run `powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0` and
  `powercfg /setactive SCHEME_CURRENT` (ask IT if it needs admin rights), or never close the lid in market hours.
- **No sleep.** While it runs, titan2 asks Windows not to sleep on its idle timer. Closing the lid, the power
  button, a flat battery or a Windows Update restart still stop the laptop. As a backup, set Settings, System,
  Power, Screen and sleep, "When plugged in, put my device to sleep after": Never.
- **Windows Update.** Settings, Windows Update, Advanced options, Active hours: 07:00 to 01:00, so it never restarts
  the laptop between 08:00 and 16:00, when titan2 runs (the `update_hours` row WARNs on hours that leave any of it
  open: on 2026-10-14 they began at 09:00, and Windows had restarted this laptop at 06:08, 06:55 and 07:34 on earlier
  days). Or pause updates for the week. When the `restart_pending` row WARNs, Windows Update is waiting for a
  restart: restart the laptop yourself after the session, never during one.
- **A stable connection.** Use one network all day (Wi-Fi with a good signal, or a cable); do not switch hotspots
  or VPNs in the middle of a session. The `wifi` row WARNs when more than one saved Wi-Fi network connects
  automatically: Windows can then switch between them mid-session (on 2026-10-13 a switch dropped the Kite feed for
  3 seconds). Untick "Connect automatically" on all but the one you trade on (Settings, Network & internet, Wi-Fi,
  Manage known networks). If the data stops, new entries stop until it is fresh again; exits are never
  blocked. A short drop costs less than before: a phone alert that cannot go out is kept and sent when the
  internet is back (after one `ALERTS_DELAYED` line, "network back at ... after N s; K alerts delayed"; what is
  still unsent when titan2 stops goes out at the next start); a failed name lookup (DNS, as on 2026-10-14 at 14:22)
  reuses the last address that worked for that server, the certificate still checked; and from 09:00 to 15:30
  titan2 keeps its Kite connection open with one small request a minute, so a quote after a quiet spell does not
  wait about a second for a new connection.
- **The clock: titan2 keeps its own.** At the start and every 5 minutes titan2 reads internet time (NTP, as the
  self-check does) and adds the difference to the Windows clock, so a Windows clock that drifts, or that you cannot
  fix because you have no admin rights, does not matter: the start of the window says, for example, "clock:
  Windows is 1.433 s behind internet time (+-0.043 s, NTP time.windows.com); titan2 keeps internet time". Its clock
  never moves backwards, and the log file shows the same corrected times as the database and the dashboard. The
  self-check's `clock` row judges this corrected clock and still shows how far off Windows is. If no internet
  time source answers at the start, titan2 uses the Windows clock as it is (and the row below applies to it) and
  tries again every 20 seconds. When Windows sets its own clock mid-session (on this laptop it does, by 1 to 2.5
  seconds, around midday on most days), titan2 notices at once and keeps its time unbroken.
  `TITAN2_CLOCK_CORRECT=0` in the env file turns the correction off.
  Without the correction, or to fix Windows itself: Settings, Time & language, Date & time: "Set time
  automatically" on, time zone (UTC+05:30) Chennai, Kolkata, Mumbai, New Delhi, and click **Sync now** before the
  session. The self-check's `clock` row keeps
  entries off when the clock can be more than **1 second** off internet time. It counts the reading's own error:
  a few milliseconds over NTP; half a second more over the fallback (the whole-second Date header of
  api.kite.trade, used when NTP is blocked), which then passes only a clock within about half a second. On
  2026-10-11 this laptop was not syncing at all, although "Set time automatically" was on (`w32tm /query /status`
  said "Source: Local CMOS Clock"), and it was 1.25 seconds behind: enough to keep entries off. To make Windows
  really sync (only with admin rights; titan2's own correction makes it optional), do this once in a PowerShell
  window opened as administrator (right-click PowerShell, Run as administrator):

  ```powershell
  w32tm /config /manualpeerlist:"time.windows.com,0x9" /syncfromflags:manual /update
  Set-Service w32time -StartupType Automatic   # else the service stops again (found 2026-10-13: 1.43 s behind)
  Restart-Service w32time
  w32tm /resync
  w32tm /query /status     # now "Source: time.windows.com" and a recent "Last Successful Sync Time"
  ```

  To check the clock without admin rights: `w32tm /stripchart /computer:time.windows.com /samples:3 /dataonly`
  (each line shows how far this clock is off; under 0.5 s is comfortable).
- **One copy only.** Run titan2 on this laptop or on the EC2 server, never both on the same day: they share the
  Kite app, and a new login by one replaces the other's session. If you start a second copy on the laptop it
  refuses (exit 73) and leaves the first one alone.

## 6. Where things are

| What | Where |
|---|---|
| Your secrets | `%APPDATA%\titan2\titan2.env` (readable by you only) |
| The dashboard login (optional) | `%APPDATA%\titan2\dash.env` |
| titan2's state: databases, settings, today's Kite session | `%LOCALAPPDATA%\titan2\var\` (`db\`, `config\`, `session\`, `pub\`) |
| The daily report | `%LOCALAPPDATA%\titan2\var\reports\<date>.md` (also printed at the end of the session, and sent on Telegram or ntfy when set up) |
| Self-check results | `%LOCALAPPDATA%\titan2\var\reports\selfcheck-*.json` |
| Logs | `%LOCALAPPDATA%\titan2\logs\laptop-<date>.log` and `dash-<date>.log`, kept 30 days |
| Exports for research | `%LOCALAPPDATA%\titan2\var\exports\` |
| titan2's Python environment | `%LOCALAPPDATA%\titan2\venv\` (built by setup.ps1) |
| A rehearsal of another date (`-Date`, testing only) | `%LOCALAPPDATA%\titan2\sim\<date>\var\` and `\logs\`, apart from the real state |

Open the folder with `explorer "$env:LOCALAPPDATA\titan2"`. Do not edit anything under `var\` by hand, and never
share `session\kite_session.json`: it holds the day's Kite access.

**Rehearsing another date.** `.\ops\windows\run_today.ps1 -Date 2026-10-19` (`titan2 laptop --date 2026-10-19`)
runs as if it were that day, for tests and rehearsals only. It keeps its own state and logs in
`%LOCALAPPDATA%\titan2\sim\<date>\`, starting from a copy of today's Kite session and your settings file, and never
writes to `var\`. Run there, a rehearsal counted as that day's real paper session: its trades became G1 evidence,
its start made the real day IMPAIRED, its 15:26 square-off was marked done, so the real day's was skipped, and its
end made that day's morning start stop at once with `titan2: the trading day is done`. It refuses to start while a
real session runs (exit 73): if it had to log in to Kite again, the running session's login would end. A
`sim\<date>` folder can be deleted at any time. A rehearsal run before this release may have left a future date's
rows in `var\`: titan2 ignores its end-of-day steps (a WARNING in the log, "ignoring the EOD DONE row stamped …" or
"ignoring N EOD step row(s)"), so that day trades and ends as usual, but its trades, IMPAIRED marks and square-off
marks stay: if you rehearsed a date before this release, have those rows removed before that date's first session.

## 7. Commands from a second window (optional)

While a session runs, open another PowerShell window:

```powershell
cd $env:USERPROFILE\MCX\agentic_trader
$t2 = "$env:LOCALAPPDATA\titan2\venv\Scripts\python.exe"
& $t2 -m titan2 status                       # is it running? positions, halts
& $t2 -m titan2 trades                       # today's trades
& $t2 -m titan2 report                       # today's report (complete after the close)
& $t2 -m titan2 report --date 2026-10-12     # another day's report
& $t2 -m titan2 pause                        # no new entries; exits continue
& $t2 -m titan2 resume                       # entries allowed again
& $t2 -m titan2 token                        # log in to Kite now (tests the env file)
& $t2 -m titan2 whatif                       # what today's refused entries would have made (after the close)
& $t2 -m titan2 condor                       # the paper iron condor: open position, results, notes
& $t2 -m titan2 condor dry-run               # the strikes it would pick right now (nothing saved)
```

**The dashboard** is a status page in your browser at http://127.0.0.1:5000 while a session runs (`-NoDash` turns it
off). It shows the bot's status, positions and trades from the files titan2 publishes; it has no Kite credentials and
cannot place an order. It needs a login file once:

```powershell
& $t2 -m titan2.dash.app hash-password    # choose a password of 12+ characters; prints DASH_PASS_SCRYPT='...'
& $t2 -m titan2.dash.app new-secret       # prints DASH_SECRET=...
notepad "$env:APPDATA\titan2\dash.env"    # write DASH_USER=<a name>, paste the two printed lines, save
```

`dash.env` holds those three lines only: never a `ZERODHA_` key. Without it the session runs normally; it just says
the dashboard stopped and that trading is unaffected.

## 8. What PAPER means

- Simulated trading on live data. titan2 reads real Kite prices and market depth, and its own paper exchange fills
  orders the way the market would: only against the bids and offers really there at that moment, after a realistic
  delay, taking on average half of what is shown, with every charge (brokerage, STT, exchange fees, GST, stamp duty)
  deducted. One known gap: a lone one-lot bid fills on paper only about half the time, where a real order would
  usually get it. So when a price drops suddenly for a second, paper can still miss a loss the real market would
  have taken. The paper exchange's settings (its delays, the half, its price band) are fixed in the code: a settings
  file cannot change them.
- No order ever reaches Zerodha. Your funds, holdings and positions in Kite are never touched: titan2 only reads
  market data, your profile and the account's orders and positions (to spot what it does not own).
- The point is to measure honestly, with no money at risk, whether a strategy has a real edge (the G1 test). Expect
  many days with few or no trades; G1 needs months of sessions before it can say anything.
- **Three strategies run**, each with its own paper book and its own G1 test: `opening_drive` (follows a strong
  first 5 minutes, up or down), `price_spike_up` (buys an ATM call when a stock jumps and the jump still holds 12
  seconds later) and, since 2026-10-12, `price_spike_down`, the put version you asked for. It is the exact mirror
  image of `price_spike_up`: it buys an ATM put when a stock falls 0.6% or more (1.0% before 09:45) and is still at
  least 0.35% down (1.0% before 09:55) 12 seconds later. A fall on an unusually quiet tick (less than half the
  recent average volume per tick) is ignored, and so is a stock that bounces back above where the fall started. In
  v1 the down-spike trades won only 1 in 6, so it is an experiment: PAPER only, like the others, until its own G1
  test passes. The other two kept their rules and versions, so their G1 tests carry on, but the three still affect
  each other in the three ways below.
- **The two spike strategies do not wait for each other.** v1 paused a stock for 90 seconds after any spike, up or
  down; here each spike strategy keeps its own 90-second pause. So a stock that jumps and then drops back can get
  `price_spike_up`'s call and, seconds later (rarely on the very same tick), `price_spike_down`'s put, each with its
  own placebo: up to four positions on one stock. It also means the puts are not exactly the trades v1's
  down-spikes took (the 1 in 6 above).
- **One paper account, so some G1 checks are shared.** A Ctrl+C while any position is open (a put, its call, or any
  other) marks the day as disturbed (IMPAIRED) in all three strategies' G1 tests, not only the one holding it, even
  after 14:55 when nothing new can be bought. The puts are bought until 14:55 and held until the 15:15 close, so a
  late Ctrl+C now finds something open more often; section 4 says how to stop without the mark. A bookkeeping
  problem on one of the puts (a MISMATCH or UNKNOWN_POSITION halt, rare on paper) stops new entries for all three
  until you check it and clear it with `titan2 ack-halt` (the RUNBOOK's playbooks P2 and P3); left more than 10
  minutes of entry time, it marks the day IMPAIRED for all three. It also counts as a mismatch, which G1 does not
  allow, for `opening_drive` if that strategy traded the same put during its test. G1 cannot pass a strategy while
  its IMPAIRED days are more than 10% of the days it traded, and a marked day counts even if the strategy did not
  trade that day.
- **A cost to the other two's G1 tests.** G1 gives a strategy without a real edge only a small chance of passing by
  luck at each look, and it shrinks that chance as more versions are tried (each version run on PAPER is a "trial":
  the more tries, the more likely one gets lucky). The three strategies share one count, so any change that gives
  every strategy a new version, such as a changed setting, now adds three trials instead of two, and a new
  `price_spike_down` version alone adds one. After the two setting changes of 2026-10-13 (the risk caps, then three
  entries a day) and the release of 2026-10-14 (the paper exchange's recalibration, paper model 2, and the engine's
  changes, engine semantics 2, which ship together and so give every strategy one new version) there are 12 trials
  and the chance is about 0.10% per look, down from about 0.31% with 3: passing is harder for all three. So change
  the experiment only for a real reason.
- **What you will see of it.** At the start of each session the window and the log say
  `signals: strategies enabled (opening_drive, price_spike_down, price_spike_up)`. Its trades are in the book
  `PAPER:price_spike_down`, and each one comes with a call bought at the same moment in `PLACEBO:price_spike_down`:
  that call is the honesty check, and if the calls make money too, the puts had no real edge. Both books are on the
  dashboard's Books panel (and in the daily report's Books table on days they traded), the strategy has its row in
  the dashboard's Strategies panel and in the report's G1 table, and `& $t2 -m titan2 g1 price_spike_down` shows
  its scorecard (section 7). To stop it, run
  `& $t2 -m titan2 mode price_spike_down OFF` from a second window: it stops at once, and G1 marks that session as
  disturbed (IMPAIRED). It stays off until `& $t2 -m titan2 mode price_spike_down PAPER`, which applies from the
  next session.

- **The iron condor (paper, from 2026-10-14).** Separate from the three strategies above and from G1: titan2
  tracks the one setup the research found with a thin edge, a NIFTY weekly iron condor (sell a put and a call about
  1.5 times the expected 3-day move away, buy protection 2% further out), on paper only. It places no order at all,
  not even a paper one through the bot: at 15:25 on the third trading day before each NIFTY expiry it reads the
  live option prices and books the four legs at the prices really on offer (and a version at twice the spreads); on
  each later day but expiry it closes them at 15:25 if they are worth 3 times what was collected; otherwise it holds
  to expiry and settles at NIFTY's official close. It keeps its own records in `db\condor.db`, prints where it
  stands after the daily report, and stops entering if one of the research's kill rules fires (for example, real
  credits clearly below the backtest's). Expect a small gain most weeks and an occasional loss of up to about
  Rs 28,000 on paper: one bad week can wipe out months of gains. The window must be open at 15:25 on those days
  (it already is in a normal session); a missed 15:25 skips that day's entry or check, and the next start writes a
  MISSED note for it, which `& $t2 -m titan2 condor` shows (the start card and the 15:38 report show the latest one,
  and a 3x stop). If a leg's quote is crossed at 15:25 (its bid above its ask), the check is tried again 45 s later,
  and at the last try (also one too late for another, or one that ran late) the leg is valued at its worse side, so
  such a quote cannot hide the 3x stop. `TITAN2_CONDOR=0` in the env file turns it off.

## 9. Why LIVE is refused on the laptop

1. **SEBI's rule for API orders:** they must come from a static IP address registered with Zerodha. A laptop's
   address changes with every network; only the server has a fixed one.
2. **No backstop:** if a laptop sleeps, loses Wi-Fi or reboots mid-trade, nothing manages the open position until you
   notice. The server restarts the bot by itself, has a watchdog and sends alerts.

So the laptop never sends a real order, whatever `ALLOW_LIVE` or a strategy's mode says: a strategy in LIVE mode
runs as PAPER for that session, with a warning. Not even an exit: if EC2 traded LIVE earlier the same day, its
titan2 orders and positions are in the Kite account you share, but not in the laptop's own database. The laptop
leaves them alone and says so at its start (a CRITICAL `UNKNOWN_POSITION` alert, "titan2 LIVE orders exist in this
Kite account today", section 12); manage them in the Kite app or on EC2. Better: let EC2 close everything before
you switch to the laptop (section 5, one copy only). LIVE is only possible on the EC2 server,
after G1 and two separate human keys ([TITAN_INSTRUCTIONS.md](../TITAN_INSTRUCTIONS.md) section 11).

## 10. After your first session: what to check

1. The window ended with `titan2: the trading day is done` (or `titan2 stopped cleanly` after a Ctrl+C), exit 0,
   and the daily report was printed.
2. The self-check at the start: every row PASS. Power may WARN if you were on battery; plug in next time.
   `network` WARNs when the line to Kite loses packets: expect some signals refused as QUOTE:FETCH_FAILED that day.
   `notify` WARNs when no phone alert channel is set up, or `HEALTHCHECK_URL` is not (no dead-man); it sends no
   test message, the start card does that job. `lid`, `restart_pending`, `update_hours` and `wifi` WARN on a laptop
   setting that can end a session: fix each as section 5 says.
3. Read the report in `%LOCALAPPDATA%\titan2\var\reports\`: the trades, the net after charges, the exit reasons, the
   refusals by gate, the paper orders that did not fill and why (their `SIM:` reasons), and the mismatches (there
   should be none). Its latency table also shows how long Kite's quote calls took (`quote_call_ms`, and
   `quote_kite_ms` without titan2's own one-a-second pacing), how many were cold (no call in the minute before), and
   each account read's round trip (`read_ms:LIVE:POSITIONS` is the real Kite one); after a week of these the numbers
   say whether the laptop needs a cable. After it comes the what-if table: what each refused entry would have made
   under titan2's own exit rules after charges, plus running totals per refusal reason (saved in
   `reports\whatif-<date>.json`;
   `& $t2 -m titan2 whatif --date YYYY-MM-DD` reruns it while that day's login is valid). It flatters the refused
   trades (no depth, no slippage), so judge a filter only on weeks of totals.
4. Search the log for trouble. Expect no lines, or only ones you can explain:

   ```powershell
   Select-String -Path "$env:LOCALAPPDATA\titan2\logs\laptop-*.log" -Pattern 'CRITICAL|ERROR|Traceback|battery'
   ```

   A WARNING `REST quote of ... the connection dropped before Kite answered ...; retrying once` is harmless: Kite
   had closed an idle connection and titan2 asked once more at once (it says how many times so far this run).

5. The `power:` lines every 15 minutes: no battery warnings and no long gaps between their times (a gap means the
   laptop slept). The `quote call:` lines (one per Kite quote call, in the log file only) show each call's time and
   whether it was cold.
6. After the close, `& $t2 -m titan2 status` shows no open legs, no working orders and `halts: none` (or ones you
   understand).
7. If anything looks wrong, keep the log and the report and write down what you saw. The RUNBOOK playbooks
   (section 4) are written for the server, but the ideas carry over.

## 11. Optional: start it by itself every weekday

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\windows\register_task.ps1 -DryRun   # shows the task
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\windows\register_task.ps1           # registers it
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\windows\register_task.ps1 -Remove   # deletes it
```

`ops/windows/register_task.ps1` makes a Windows Task Scheduler task, "titan2 laptop session", for you (no admin
rights, no stored password): Monday to Friday at 08:40 India time, it wakes the laptop, it starts on battery too and
keeps running if the laptop switches to battery, one copy at a time, and a missed start (laptop off at 08:40) runs as
soon as possible. It opens the same `run_today.ps1` window, so Ctrl+C works as usual.

- You must be signed in; a locked screen is fine. After a restart, sign in before 08:40.
- Waking from sleep needs Power Options, Change advanced power settings, Sleep, "Allow wake timers": Enable. Some
  laptops still do not wake reliably: plugged in with sleep set to Never is the safe setup.
- Do not also start `run_today.ps1` by hand on those mornings: the second copy refuses to start (exit 73).
- The window stays open after the session for you to read; the task closes it 12 hours after it started.

## 12. If something goes wrong

| You see | Do this |
|---|---|
| "running scripts is disabled on this system" | Use the `powershell -NoProfile -ExecutionPolicy Bypass -File ...` form |
| setup: no 64-bit Python 3.10 to 3.13 found | Install Python 3.12 (64-bit) from python.org, then run setup again |
| setup: pip could not install requirements.lock | Check the internet connection and run setup again |
| check: `env_file` FAIL | Fill in the three needed Kite values and save the file in Notepad |
| check: `token` or `instruments` FAIL before today's first session | Expected: the session logs in first. To test now: `& $t2 -m titan2 token` |
| check: `notify` WARN | Set `NTFY_TOPIC` (phone alerts) and `HEALTHCHECK_URL` (the dead-man) in the env file, section 2. No test message is sent: the start card on your phone each morning proves the alerts work |
| check: `lid`, `restart_pending`, `update_hours` or `wifi` WARN | A laptop setting that can end a session: the lid set to sleep when plugged in, a Windows restart waiting, update hours that let Windows restart between 08:00 and 16:00, or more than one Wi-Fi network that connects by itself. Section 5 says how to fix each; they never block entries. SKIP means titan2 could not read that setting |
| Phone: `POWER` "switched to battery power" | The charger came out or the power failed: plug it in. A second `POWER` says when it is back on AC |
| Phone: `SESSION_IMPAIRED` | G1 marked the day (or days) as disturbed, with why: titan2 was stopped or started late, a pause, a Ctrl+C with a position open. Nothing to do now; a strategy with too many such days cannot pass G1 (section 8) |
| Phone: "Kite account check (no LIVE trading here)" | titan2 could not read the Kite account (usually the internet). The laptop never trades LIVE: no real money is at risk; it asks again by itself |
| check: `clock` FAIL | More than 1 s off (or possibly so: the reading's own error counts), so entries stay off: Sync now and the one-time fix in section 5, then stop (Ctrl+C once) and start again. The bot's own check after its morning preparation also lets entries start if the clock is right by then |
| CRITICAL `UNKNOWN_POSITION`: "titan2 LIVE orders exist in this Kite account today" | EC2 traded LIVE today on the account you share. The laptop never touches those orders or positions: close or manage them in the Kite app (or on EC2). Its halt (`ACCOUNT:LIVE`) only blocks LIVE, which the laptop never trades. While such a position is still open, reconciliation also halts paper entries (`halt #N INVARIANT`): close it in Kite, then clear both with `& $t2 -m titan2 ack-halt N --note closed in Kite`. The alert repeats every 5 minutes until `& $t2 -m titan2 ack-alert recovery:LIVE:not_imported` |
| check: `python`, `disk` or `db` FAIL | Fix the cause first (`python`: run setup again and start with `run_today.ps1`). Its halt stays after the fix: while a session runs, `& $t2 -m titan2 status` shows it as `halt #N INVARIANT`; clear it with `& $t2 -m titan2 ack-halt N --note fixed` |
| `Another titan2 is already running` (exit 73) | Another titan2 window is open: use that one |
| The window title starts with "Select" and the output has stopped | Press Esc: a click started a text selection, which pauses the window. `run_today.ps1` turns this off for its own window |
| titan2 stopped with an error during market hours | Start `run_today.ps1` again: it recovers the open paper positions first |
| You start `run_today.ps1` again after 15:26 (the laptop slept, or the window was closed, with a position open) | Expected: the paper exchange squares the position off at once, and titan2 keeps checking every 5 s until that close is booked (up to 16:00, or a minute after it is ready), then runs the day's last steps and stops. After `titan2: the trading day is done` (15:40) a new start only recovers and stops at once |
| `requirements.lock changed since setup.ps1 last ran` | Run setup again after the session |
