# TITAN CHANGELOG

All notable changes to the Titan trading bot are documented here.  
Format: `[YYYY-MM-DD] Category: Description`

---

## 2026-10-11 — v2.0.0: titan2 replaces v1 (paper-first)

Titan v1 (76K lines) lost real money, and the research behind this release found no entry rule with a positive edge
after costs. v2.0.0 is a rewrite whose jobs are, in order: make engineering losses impossible, hard-cap every loss,
win more often with cost-aware exits judged on the bid, and keep every strategy on PAPER until gate G1 proves an edge.
The honest expected outcome is that no strategy reaches LIVE. Full description: [TITAN_INSTRUCTIONS.md](TITAN_INSTRUCTIONS.md).
Entries below this one describe v1.

### Reliability and visibility (2026-10-14 night)
No strategy, setting or version change: the strategies keep their versions (`opening_drive` 1.b230b83102,
`price_spike_up` 1.3a030f3e26, `price_spike_down` 1.9f039e44a7), and no strategy's decision sees anything new. From
the self-audit of the first three laptop sessions.
- **Kite's quote connection is kept warm.** A quote made after 5 or more quiet minutes took a median 0.98 s (16 of 33
  over 1 s, the 09:20 batch 2.0 s late), against 0.11 s on a warm connection: Kite, or a router on the way, closes an
  idle connection and the next quote waits for a new one. From 09:00 to 15:30 on a trading day the quote thread now
  makes one small request (the last price of NIFTY 50) whenever no quote went out for 60 s, through the same
  one-a-second limit as every quote (about 1% of it). Its answer is thrown away: no entry or exit decision sees it.
  Only a failure is logged. It never goes out within 3 s of a whole minute: a 60 s rhythm started at 09:00:00 would
  otherwise take the limit's token at 09:20:00, from opening_drive's batch, and at every bar close
  (`titan2/broker/lanes.py`, `ReadsLane.keep_warm`; the hours are `runtime.quotes_keep_warm`).
- **A DNS blip no longer breaks Kite calls.** On 10-14 from 14:22:08 to 14:22:53 every new connection failed with
  "getaddrinfo failed" (13 account reads lost, a STALE_RECON halt, 3 lost phone alerts) while the WebSocket stayed
  up. titan2 now remembers each server's last good IPv4 address and connects to it when the name lookup fails; the
  TLS certificate is still checked against the real name, so a wrong address fails instead of reaching another
  server (`titan2/net.py`, `remember_ipv4`, installed with the IPv4-only setting).
- **Phone alerts are no longer thrown away in a network drop.** A message that still fails after its tries because
  of the network (a timeout, no DNS, no connection, or HTTP 429/5xx) is kept, up to 200 messages for up to 2 hours,
  and the messages after it wait behind it. Every 30 s one line is tried; when it goes through ("network back at
  14:23:37 after 67 s; 3 alerts delayed") the kept messages follow, a CRITICAL first, each with the time it was
  raised. A CRITICAL's 5-minute repeats pause meanwhile, so no burst arrives afterwards. A message the service
  refuses (another HTTP 4xx) is logged and dropped, as before, and is no longer tried three times. What is still
  undelivered when titan2 stops is written to `var/run/alerts_outbox.json` and sent at the next start
  (`titan2/alerts.py`); so is a fatal start's `BOT_FATAL` (exit 78) that could not go out.
- **The phone says when titan2 is alive.** On a quiet day the phone stayed silent, which looked exactly like a dead
  laptop. Now, once the bot's own laptop self-check after the morning preparation is done (its signals start
  then), it sends one INFO `START_CARD`: PAPER only, every FAIL and WARN self-check row, the clock, network and power
  (AC or battery) rows, each strategy's mode and version, and the paper condor. No card by 09:15 means titan2 is not
  running. A crash restart reaches the phone as a WARN `BOT_RESTARTED` (sent by the restarted bot), and the stop
  after the fifth restart ("open positions are NOT managed now") as a CRITICAL `BOT_GAVE_UP`, which also pings the
  dead-man's `/fail` and is kept for the next start if the network is down; both were console lines only.
- **A `notify` row in the laptop self-check.** It checks that a phone channel (ntfy or Telegram) is set up and that
  `HEALTHCHECK_URL` is (without it there is no external dead-man: nothing rings when the laptop stops), WARN only and
  with no alert of its own (the start card lists it). It sends no test message: the start card proves delivery
  every morning. The env-file row no longer says alerts need Telegram when ntfy carries them, and the hint printed
  when there is no env file names `NTFY_TOPIC` and `HEALTHCHECK_URL`. `HEALTHCHECK_URL` is in
  `ops/windows/titan2.env.example`, `ops/windows/setup.ps1`'s list and docs/LAPTOP.md section 2, with the check's
  schedule.
- **A crossed quote can no longer hide the paper condor's 3x stop.** The 15:25 check values each leg at its mid, and
  a leg whose bid is above its ask (a crossed book) has none, so it fell to the "no bid" rule, half its ask: in the
  audit's probe a short put crossed by one tick turned a 3.70x stop into a 1.40x hold. Now, on a stop day, a crossed
  or locked leg (bid at or above the ask) fails that try and the job tries again 45 s later, as after a failed Kite
  call. At the last try (the third, one too late in the window for another, or a `titan2 condor tick` by hand) a
  short is valued at the higher of its bid and ask and a wing at the lower; a stop then buys a short back at no
  less than its best bid and sells a wing at no more than its best ask (the paper exchange's rule at its 15:26
  square-off), and a note says so (`titan2/condor.py`: `mark`, `leg_mark`, `worse_side`).
- **A missed condor check now leaves a trace.** When titan2 is not running at 15:25 (laptop off, stopped early,
  started late), that day's 3x stop check or entry is skipped and nothing makes it up, and until now nothing recorded
  it either. Each start now writes a MISSED note, once, for every trading day since the condor's first 15:25 tick
  with no completed tick while a condor was open (its stop check) or due (the entry day of an expiry the instruments
  listed at an earlier tick). `titan2 condor status` lists the latest three and the console prints each one as it is
  written (`missed_duties`, `record_missed`). How a missed check should be made up is still the owner's decision.
- **No 75-second blind spot after a restart.** A restart reuses the previous session's 5-minute bars stored in
  titan2.db only when they are complete, and complete meant a bar within 10 minutes of the 15:40 close. Since
  2026-08-03 F&O stocks close by auction and their last bar is the 15:10 one, so the test never passed: all 7 starts
  fetched 213 of 213 stocks again, about 75 s before signals start, the 10-13 restarts at 11:45 and 13:08 included.
  Stored bars are now complete when they reach the latest bar stored for that session; a stock whose bars stop
  earlier is still fetched. The bars are the same, so no decision changes (`titan2/runtime.py`, `_complete`).
- **The paper square-off no longer sells a long at ₹0.05 when its only book came early in its wait.** Paper model 2
  says that when the only book the 15:26 square-off gets in its 1.5 s wait is crossed or locked, it fills on that
  book at its worse side, never at the no-book ₹0.05. That held only while the book was under 1 s old at the end of
  the wait. On a thin option's silent feed the only book is the REST answer the wait asks for, about 0.2 s in, so at
  the end it was 1.3 s old and counted as none (review round 4's probe sold 100 at ₹0.05). Any book received since
  the wait began now counts at its end; one from before the wait never does. This is what paper model 2 already
  described, so no version changes (`titan2/broker/paper.py`, `_maybe_squareoff`).
- **A settings file with several now-fixed paper keys can be repaired.** The remedy for a file that sets one of the
  paper keys fixed by paper model 2 is `titan2 settings unset KEY`. With two such keys neither `unset` nor
  `set KEY <default>` could write: each checked the whole resulting file, and removing one key left the other's error.
  A file that is already invalid is now repaired one change at a time: a change that removes some of its problems
  and adds none is written, and the message lists what is still wrong. Any other change still waits for a valid
  file. The bot stays on the code defaults (HALT SETTINGS_INVALID) until the file is valid and it restarts
  (`titan2/settings.py`, `_update`). A change no longer says "every strategy gets a new version" when what the bot
  runs on stays the same, as after the last repair of a file that leaves only defaults.
- **Every Kite quote call is measured.** The self-audit had to rebuild every latency by hand from receipt stamps.
  Each call of the quote client now records how long it took (what the entry or exit waited, and Kite's own share
  without titan2's one-a-second pacing), how many symbols it asked, the seconds since the previous call (60 s or
  more: a cold connection), whether a dropped connection was asked again, and the outcome; one `quote call:` line
  each goes to the log file (not to the laptop's console). Once a minute, with the heartbeat, the engine writes them
  into one DEBUG `LATENCY` event, and the daily report's latency table has `quote_call_ms` and `quote_kite_ms` rows
  (n, p50, p95 and a new max column) and a line with the cold share, failures, retries and keep-warm calls
  (`titan2/broker/lanes.py`, `QuoteCall`, `take_quote_calls`; `titan2/broker/kite.py`, `KiteData.last_quote`).
- **Every broker read's round trip is measured.** The exit's wait for a positions read (decision B1) failed Stage 2
  on paper's guessed delay, and nothing recorded what Kite's real reads take. The engine now keeps each positions,
  orders and margins read's round trip, from its send to its answer, per account and kind, in the same `LATENCY`
  event, and the latency table has a `read_ms:<ACCOUNT>:<KIND>` row for each (the LIVE rows are Kite's real numbers,
  the PAPER ones the paper exchange's seeded delay) and a line counting failed reads (`titan2/core/engine.py`,
  `_note_read`, `_flush_latency`; `titan2/ledger.py`).
- **A paper fill's log line names the book it filled on.** Paper kept no record of it, so paper and LIVE could not be
  compared after the fact. The COMPLETE (or IOC CANCELLED) line now says WS or REST, the book's receipt and exchange
  times, how old it was at the order's due time (or how long after it it came), how long past due the fill came, the
  walked side's levels down to the limit and the order's draw. The log line only: no fill and no table changes
  (`titan2/broker/paper.py`, `_book_note`).
- **The bar check keeps the signed volumes and the pre-open volume.** On 10-14 none of 23 opening bars matched Kite's
  volume within 0.1% (10 were more than 5% off) while their open and close, and later bars, matched: most likely
  Kite's 09:15 candle leaves out the pre-open auction, which titan2's first bar includes. The 15:36 check now stores
  in `bar_checks.detail` both volumes as they are, the signed gap (`volume_gap`, `volume_rel`), the pre-open volume
  (the cumulative volume at the stock's last tick before 09:15:00: the bar builder sends it with the first bars and
  the engine keeps it as a DEBUG `PREOPEN_VOLUME` event) and `volume_rel_ex_preopen`, so tomorrow's check shows the
  cause. Nothing judges on them: the check, G1 and `opening_drive`'s 3x volume filter are unchanged, and making the
  two volumes match would be a new version (the owner's decision). The "[V]" notes in `titan2/market/bars.py` and
  `titan2/strategies/opening_drive.py` now say the check failed them (docstrings only: no version moved).
- **Laptop settings that can end a session are checked.** On 10-14 the lid was set to Sleep when plugged in, a
  Windows restart was pending, Windows Update's active hours began at 09:00 (it had restarted this laptop at 06:08,
  06:55 and 07:34 on earlier days) and 8 of 9 saved Wi-Fi networks connected by themselves (a switch dropped the feed
  on 10-13), while the self-check said 12/12 PASS. Four new laptop rows, `lid`, `restart_pending`, `update_hours` and
  `wifi`, read them as a standard user (powercfg, the registry, netsh, never a Wi-Fi key) and WARN with what to
  change. They block nothing, send no message of their own (the start card lists them) and SKIP when a setting
  cannot be read (`titan2/selfcheck.py`; the probes in `titan2/laptop.py`).
- **A switch to battery reaches the phone.** It was a log line every 15 minutes. The power source is now looked at
  every 30 s: AC to battery is one WARN `POWER`, and back on AC after it one INFO. The alerter's 30-minute WARN dedup
  keeps a loose plug from ringing again and again, and starting on battery sends nothing (the start card says so)
  (`titan2/laptop.py`, `Supervisor._watch_power`).
- **Placebo alerts say what they are.** On 10-14 the phone showed "PLACEBO:price_spike_up ... net Rs -5,299.97" like
  any trade. A placebo's opened and closed alerts now begin "control trade (placebo), not a strategy result", and the
  daily report's phone text labels placebo books the same way (`titan2/core/positions.py`).
- **The 15:38 phone report says more.** Besides the books, the halts and the G1 lines it now gives the day's
  refusals and voids by reason, the what-if totals saved so far (today's what-if needs Kite's minute bars and runs
  after the session, so the line says it is not in yet), the paper condor (laptop), the bar check with its reasons,
  the alerts delayed or not delivered this run, and on each G1 line "today counts for G1: yes" or "no (why)": a mode
  G1 does not test, no running trial, a PAPER mismatch today, or an IMPAIRED mark. The markdown report has the same
  lines and a `today` column in its G1 table; display only (`titan2/ledger.py`).
- **A day G1 marks as disturbed reaches the phone.** SESSION_IMPAIRED was an event only, so 10-13's marks after its
  10:03 start never reached the phone. Each event's new marks are now one WARN `SESSION_IMPAIRED`, naming the
  sessions, the strategies and why; a mark is never sent twice (`titan2/core/engine.py`, `_impaired_alert`).
- **The laptop's Kite account warnings no longer read like real-money trouble.** No LIVE order leaves the laptop,
  yet "LIVE: 1 POSITIONS read(s) unanswered" looked like a live-trading problem. There the LIVE account's `READ_LOST`
  and `STALE_RECON` warnings now begin "Kite account check (no LIVE trading here)"; the events, and the server's
  wording, are unchanged.
- **The dashboard's events tail keeps the morning.** At the 10-14 close 227 of the last 500 events were heartbeats
  and the oldest left was from 11:53, so the 09:20 batch and the morning's refusals had dropped out. The tail now
  leaves out the DEBUG rows (the heartbeats and the new measurement events), which stay in titan2.db and the exports
  (`titan2/ledger.py`, `publish_events_tail`).
- Not done in this batch: the 09:21 message with `opening_drive`'s batch (`opening_drive` is moving to SHADOW).
- Tests: the new `tests/unit/test_reliability_visibility.py`; the laptop self-check table and its healthy laptop
  (`test_selfcheck_laptop.py`: 17 rows, `notify` without an alert, a healthy laptop has a dead-man URL); the start
  card through the real bot in `test_laptop.py`'s in-process session; the Windows env keys in
  `tests/ops/test_windows_scripts.py`; the condor's crossed legs, its last try and the MISSED notes in
  `test_condor.py`; the restart's stored bars in `test_runtime_cli.py`; the square-off on a silent feed in
  `test_paper.py`; the settings repair in `test_settings.py`; the measurements, the laptop-settings rows and the
  power watch, and the phone and dashboard changes in the new `tests/unit/test_measurement_and_housekeeping.py`, with
  the four new rows in `test_selfcheck_laptop.py` and the in-process session's offline probes in `test_laptop.py`.
  Docs: the RUNBOOK's alert codes (`BOT_RESTARTED`, `BOT_GAVE_UP`, `START_CARD`, `ALERTS_DELAYED`, the kept
  messages, `POWER`, `SESSION_IMPAIRED`, the laptop's wording of `READ_LOST` and `STALE_RECON`, what `DAILY_REPORT`
  gives), the laptop's `notify` row and its four settings rows, and P13's repair of an invalid settings file;
  LAPTOP.md sections 2, 3, 5, 8, 10 and 12; TITAN_INSTRUCTIONS §4, §5, §8, §12 and §14; one errata line in the spec.
- Follow-ups, from the review of this batch (2026-10-15; still no strategy, setting or version change):
  - **The phone hears of a missed condor check and of a condor stop.** The condor line of the start card and of the
    15:38 report kept only the open condor and the test, so an open condor whose 15:25 stop check was missed showed
    its old mark and nothing else, and a condor stopped at 3.70x showed only as "test: running". The line now also
    has the closed condors with the last close and its reason, and the latest MISSED note, shortened, with how many
    there are (`laptop.condor_brief`).
  - **A late condor try knows it is the last.** A try counts as the last when no other can start before 15:29:30,
    and that now allows for the supervisor's passes 0.2 s apart, with 2 s to spare: a try at 15:28:44.95 waited for
    a retry that the next pass, at 15:29:30.05, could no longer start. A try also asks again at its end: one that
    found a crossed leg at 15:28:40 and then waited past the window for a slow Kite day-bar call ended with the stop
    unchecked, and the condor was held to settlement. It now makes the check itself before it ends, the leg at its
    worse side (`condor.CondorJob.retry_fits`, `Ctx.can_retry`, `tick`).
  - **No false MISSED note when only the entry step failed.** A day whose stop check was made (its mark is stored)
    while the entry step failed on all three tries (Kite's instruments call, with no list saved that morning) got
    "skipped the 3x stop check" at the next start. Such a day is no missed check now (`condor.missed_duties` reads
    the marks).
  - **A message on its way out at a stop is kept.** stop() waits about 6 s for the alerts thread, then saved only
    the kept and the queued messages, so the one being sent (most likely `BOT_CRASHED`, sent right before the stop,
    into a connect that hangs) was in neither and was lost. It is saved too when its try still hangs at the stop's
    deadline; the thread makes no further try of it then and drops its late result (`alerts.Alerter._inflight`).
  - **One start card a session.** Each in-process restart sent its own `START_CARD`: a loop of 6 crashes put 6 cards
    on the phone besides the crash alerts. One run a session sends it now, the first that gets as far as its
    self-check result; the phone hears of a restart as `BOT_RESTARTED` (`runtime.BotProcess(start_card=...)` and
    its `card_sent`, which `laptop._bot_loop` uses).
  - **A rate limit is no network outage.** HTTP 429 counted as one: sending a backlog of 120 kept alerts into
    ntfy.sh's limit (60 at once, then one every 5 s) started a new outage at each 429, each with one more
    `ALERTS_DELAYED` line (9 in all, "network back ... after 30 s", though the network never dropped). A 429 while
    the backlog goes out now only makes it wait 10 s and go on (one line in all), and a hold that began with one
    says "rate limit (HTTP 429) over at ..." (`AlertError.rate_limited`). A CRITICAL's repeats pause until the kept
    messages are all out.
  - **The delayed-alerts line counts a start that is still offline.** A start that took the previous run's
    undelivered alerts while the network was still down never noted when the outage began, so after 3 minutes the
    line said only "2 alerts from the previous run ... were not delivered" and then 5 came. The first failed try of
    the line now marks it: "network back at ... after 180 s; 5 alerts delayed (2 of them from the previous run,
    saved ...)".
  - **A dead-man schedule that fits the laptop.** The laptop's docs gave the server's schedule, whose first tick is
    08:45: it rang the phone for every start after about 08:46 (and again when titan2 came up), and for every early
    stop. The laptop's own begins at 09:15 (`15-59 9 * * 1-5`, `* 10-14 * * 1-5` and `0-33 15 * * 1-5`, 3 minutes
    grace: `alerts.LAPTOP_DEADMAN_SCHEDULE`), so a start by 09:10 is in time, and docs/LAPTOP.md and the Windows env
    example say to pause the check when stopping before 15:33, after titan2 has stopped: within a minute of the
    window's `titan2 stopped cleanly` (its next ping resumes it). Change an existing laptop check to this schedule.
  - Tests: new cases in `test_condor.py` (the late and the slow try, a mark made by a try whose entry step failed),
    `test_reliability_visibility.py` (the condor line, one start card a session, the message in flight at the stop,
    the rate limit, a start still offline), `test_alerts_token_selfcheck.py` (a laptop day on the laptop's schedule)
    and `test_docs.py` (the laptop's dead-man docs); `test_reliability_visibility.py`'s first condor line test now
    expects the closed line too. Docs: LAPTOP.md sections 2, 4 and 8, the RUNBOOK's alerts and `START_CARD`,
    TITAN_INSTRUCTIONS §14.
  - From the review of these follow-ups (the same day; four fixes, still no strategy, setting or version change):
    - **The start card when the first run ends early.** Only a session's first run was told to send the card, also
      when that run ended before its self-check result (a crash in prep, or a controlled restart): the session then
      had no card at all, so "no card by 09:15" read as titan2 not running, and the card's FAIL and WARN rows never
      reached the phone. The first run that reaches its self-check result sends it now, once; a result that comes
      after its run's alerter began to stop sends nothing, and the next run sends the card. A controlled restart
      (exit 75) now reaches the phone too, as an INFO `BOT_RESTARTED`: with no second card, the phone's last word was
      the `FEED_DEAD` or `TOKEN` alert that announced the restart (`laptop._bot_loop`, `BotProcess.card_sent`).
    - **A quick retry still goes out during a stop.** The in-flight fix also ended a message's tries as soon as
      stop() was called: a `POSITION_CLOSED` raised seconds before a clean stop, whose first try met a connection
      reset, waited for the next start (on the laptop the next morning) though the stop's 5 s had room for the
      retry. The message keeps its tries until the stop's deadline; only a try still hanging then is cut and kept
      (`Alerter._past_stop_deadline`).
    - **No burst of repeats after a rate limit.** Once a 429 while the kept messages go out only made them wait, a
      CRITICAL's 5-minute repeats came back then: each got 429 and was kept as one more CRITICAL, so after an hour
      of 429 the 12 repeats raised in it reached the phone within 12 s, and a stop during it saved them all for the
      next start. The repeats now pause until the kept messages are all out (`Alerter._due_repeats`).
    - **Pause the dead-man after an early stop, not before.** The docs said to pause the check before stopping
      before 15:33, but titan2 pings while it stops (through the Ctrl+C flatten, and once more as it exits), and a
      ping resumes a paused check, which then rang 3 to 4 minutes after the exit. They now say to pause it after
      titan2 has stopped, within a minute of the window's `titan2 stopped cleanly` (with the check's page opened
      before the Ctrl+C).
    - Tests: `test_reliability_visibility.py` (the card after a first run that ended early, a card whose run had
      ended, the retry during a stop, the repeats during a rate limit), `test_alerts_token_selfcheck.py` (the
      laptop's day plays an early stop with the pause that docs/LAPTOP.md gives, read from it) and `test_docs.py`
      (the new advice, and the old one gone). Docs: LAPTOP.md sections 2, 3 and 4, the Windows env example, the
      RUNBOOK's alerts, `BOT_RESTARTED` and `START_CARD`, TITAN_INSTRUCTIONS §14.

### Review round 2 fixes (2026-10-14)
No paper fill, strategy or setting changes. The fixes that change how the engine trades, books or counts for G1 are
covered by this release's `ENGINE_SEMANTICS_VERSION` 2 (listed under "Signals wait for unfinished orders" below).
- **A paper position the paper exchange had already closed could block PAPER for good.** If the bot was down when
  the paper exchange squared a position off (say, back at 15:35 after dying at 15:10), or the paper exchange settled a
  leftover on a weekday holiday, nothing booked the close that day, and every later start called it a contradiction:
  a MISMATCH halt that could never be acked, so PAPER (and SHADOW) entries never resumed. Now: the paper exchange
  settles leftovers only on trading days; a start after the 15:26 square-off keeps reconciling every 5 s while it
  holds a leg (until 16:00, or a minute after it is ready), and the 15:33 truth check, the later EOD steps and the
  exit wait until the square-off is booked; and a leftover that was still closed on an earlier day is booked at the
  next start from the paper exchange's own record of that close, at its price (with no record: closed UNRESOLVED,
  with a MISMATCH halt that can be acked once recon is CLEAN). New paper-only read `FetchSquareoffs`.
- **The truth check failed on the day a leftover was settled.** A leftover's trade is booked to its own day, its
  settlement to today, so the 15:33 check failed and counted as a G1 mismatch. Both sides now go by the trade's own
  day; the leftover's settlement is checked against its own trade (`ledger.truth_inputs`).
- **One truth-check failure counted for every PAPER book, and twice for the bad one.** It now counts once, for the
  books it names (every book only when an order cannot be attributed).
- **A restart between 15:40 and 15:45 never ended.** A start after today's DONE step is recovery-only now (also on the
  laptop), so it reconciles, then stops.
- **An adopted late fill was not on the live feed.** It is subscribed again (full mode), so it exits on WebSocket
  quotes, not REST only.
- **Recovered positions were outside the loss guards.** `RECOVERED:<account>` books (a lost DB's imports, an adopted
  late fill) now count for the per-book daily stop and, on LIVE, the account's daily stop and guard.
- **After a restart, a timed-out order lost its 30 s late-accept window** (NOT_PLACED 15 s after the restart, so a
  second SELL could go out while the first was still being accepted). Every UNKNOWN or SUBMITTING order now gets the
  full 30 s again after a restart (decision B2).
- Follow-ups, from the review of the fixes above:
  - **A leftover closed UNRESOLVED failed that day's truth check.** Any earlier day's position with an order row made
    today became a check of its own, also a close with no broker order (a LIVE leftover, or a paper one with no
    square-off record). Its exit value was on the bot's side and nothing on the broker's, so the whole 15:33 check
    failed (on LIVE a false CRITICAL `EOD_TRUTH_MISMATCH`). Only a broker order executed today brings a leftover into
    the check now; a close no tradebook can show is left out (its MISMATCH alert already reports it).
  - **A start after 15:40 stopped before its truth check and its daily files.** With the truth check, the bar check,
    the 15:38 tasks and the exit all due at once (a start after 15:40, or a late start whose wait ended after 15:40),
    they ran in one step, and the exit stopped the bot before the tradebook answers were read and before the exports,
    the daily report and its alert, `g1.json` and `trades_90d.json` were written. Each step now waits for what the
    steps before it asked for, at most the normal gap between them: the nightly G1 after the truth check and the bar
    check, the exit after the files. A recovery-only run waits for them too. A normal day's times do not change.
  - **A slow start could end the late start's wait too soon.** Its 5 s grace counted from the process start, which
    comes before the database open, the backup and the token check, so a reconciliation read made before the paper
    square-off filled could let the truth check run, and it then found that square-off unexplained (a G1 mismatch
    for every PAPER book). The grace counts from READY now.
  - **A failed read of the paper square-off record was forgotten after an engine reload** (no retry and no
    STALE_STRUCTURE alert; the leftover stayed unbooked until a restart). The pending read now outlives a reload.
  - RUNBOOK P1: the late start's reconciling runs until 16:00, or a minute after READY when later (it said 16:00 at
    most).
- From the re-review of the order path and the money (round 3, the same day):
  - **A crash while an exit's Place was in flight sold twice** (LIVE). The order is SUBMITTING at the restart and
    its answer is gone; it had no late-accept window, so it was NOT_PLACED 15 s after the boot and a second SELL went
    out while the first could still be taken. Already fixed by the restart window above; its own test is ported.
  - **An engine reload after an error shortened the late-accept window** (LIVE). It was rebuilt from the order's
    submit plus 5 s, so an AMBIGUOUS answer that came late (after a wait on the order lane) lost part of its 30 s,
    and a Place still waiting for its answer got no window at all. The engine now remembers each window's end for the
    process, and a reload keeps it; a Place still in flight gets one counted from its submit. The NOT_PLACED moment,
    which counts like a fill for reconciliation's post-fill wait, is kept through a reload too.
  - **NOT_PLACED rested on when a "not found" answer arrived, not when its orders() read was asked** (LIVE). In a
    Kite brownout a read asked inside the window but answered after it ended the order while the lookup asked at the
    window's end was still queued. Lookups now carry their request time (`LookupResult.req_mono`); only a read asked
    at or after the window's end decides, and one more lookup goes out when none is pending.
  - **A tick that arrived during the event read as stale in gates G06 and G16.** The WS thread stamps ticks while the
    core handles an event, so a tick from any underlying could refuse an intent `FEED:STALE_TICK`, skip a placebo
    while its parent traded (G1's placebo pairs lost exactly the fast-market ones), or void an entry
    `RECHECK:STALE: underlying LTP -0.00 s old`. They age a receipt like quotes and exits do: up to 2 s ahead is age 0.
  - **G1 judged its sample in structure_id order.** A trade booked late (a missed square-off settled the next day)
    landed inside a passed look's sample, never assessed, and pushed one of the look's trades into the 25-trade
    confirmation. The sample is in booking order now, so it lands in the confirmation, which judges it.
  - **`titan2 laptop --date D` used the real state folder.** A rehearsal was written there as day D: its trades were
    D's G1 evidence, its start made the real D IMPAIRED, and its 15:26 marks skipped the real D's paper square-off. A
    `--date` run now has its own state and logs (`%LOCALAPPDATA%\titan2\sim\<D>`, seeded with today's Kite session and
    the settings file; `TITAN2_SIM_DIR` names another folder, never the real one) and is refused while a titan2 runs
    on the real one.
  - **The day budget did not see open positions or the LIVE account.** `size()` could already subtract the open
    positions' remaining stop risk and use the LIVE account's room, but nothing passed them: after a ₹10,069 loss a
    second position opened beside a first although both stopping out would put the day at -₹16,626, past the
    ₹15,000 stop; and a 1-lot LIVE trade could plan a ₹10,131 stop-out against the account's ₹10,000. Both are passed
    now (`Engine.book_state`, `SingleLegHandler.plan_entry`); a position without a sizing plan refuses new entries.
    (As first passed, an open gain counted twice: see the follow-up below.)
  - **The daily stop decided nothing while any leg's MTM was unknown,** even with the realized loss alone past the
    stop (a leg with no bids kept its book's stop suspended). It trips now when the realized loss alone reaches the
    stop, for a book and for the LIVE account.
  - Engine semantics: the G06/G16 receipt rule, the day budget's inputs and the daily stop with an unknown MTM
    (re-review items N3, N6 and N7) change which entries pass and when the stop trips. `ENGINE_SEMANTICS_VERSION` is
    bumped once for this release, in "Signals wait for unfinished orders" below.
- Follow-ups, from the review of round 3 (the same day):
  - **The day budget counted an open winner's gain twice.** The day term was `min(day net, 0)`, which gives an open
    gain no room, and each open position counted its planned stop-out plus its MTM now, which charges that gain again
    as risk still to lose. So a safe second entry was refused `SIZE:DAY_BUDGET` once the first position was up more
    than the ₹15,000 stop less both planned stop-outs: with two ₹30k 1-lot entries (₹6,128 of stop-out each) at
    ₹3,000 up, although both stopping out puts the day at -₹12,257 however far up the first one is. Now the day term
    is the realized net (a profit gives no room, as before) and each open position counts its stop-out measured from
    its entry: its planned stop-out, less what a locked or trailed stop has moved up, or its loss now when that is
    larger. An open gain is neither credited nor charged. Flat or losing first positions get the same budget as
    before; with a profit already booked and an open position under water the budget is now a little smaller (the
    booked profit no longer offsets the open loss, as in G20). Part of engine semantics 2: no further version.
  - **An old rehearsal's end of day ended the real day at its morning start.** Before `titan2 laptop --date D` had
    its own folders (above), a rehearsal wrote D's EOD timeline into the real state folder, stamped with D's times. On
    the real D the 08:50 start found D's DONE row and only recovered and stopped ("today's EOD timeline is done", no
    session), and the engine counted every EOD step of D as done (no 15:10 flatten, no truth check, no daily files). A
    step row stamped later than now, or than a start recorded after it, was not written on that day: it is ignored
    with a WARNING, for the start and for the engine's EOD steps. The rehearsal's other rows for D (its trades, the
    IMPAIRED mark, the paper exchange's square-off marks) are not removed (docs/LAPTOP.md §6).
- Open (not in this batch): a COVER run on a leg that had already CLOSED is never booked (LIVE only; build report
  §3.2 H).
- Tests: `tests/unit/test_review_r2_fixes.py`, `tests/scenarios/test_review_r2_recovery.py`, new cases in
  `test_paper.py`, `test_recon_review.py`, `test_runtime_cli.py` and `test_docs.py`; `test_late_fill_and_selftest.py`,
  `test_orders_positions.py` and `test_restart.py` now expect the late-accept window after a restart. Round 3:
  `tests/unit/test_review_r3_fixes.py`, `tests/scenarios/test_review_r3_entries.py`, new cases in `test_risk.py`,
  `test_kite_lanes.py` and `test_laptop.py`; `test_risk.py` (a tick or LTP up to 2 s ahead is fresh; a realized loss
  past the stop trips it), `test_kite_lanes.py` (lookups carry their request time) and `test_laptop.py` (a `--date`
  run has its own folders) now expect the new behaviour, and `test_ledger_g1.py`'s trade builder can stamp a
  booking time. Its review: new cases in `test_review_r3_fixes.py` and `test_review_r3_entries.py` (the second entry
  beside a winner, today's caps, the engine's stop-outs, end to end on PAPER), `test_review_r2_fixes.py` and
  `test_runtime_cli.py` (a rehearsal's EOD rows); `test_sizing.py` (size()'s day term is `realized_net`),
  `test_sizing_review.py` and `test_review_r3_fixes.py` (a position's stop-out from its entry: they pinned ₹4,880 and
  ₹6,980 for a leg ₹100 down and ₹2,000 up, now ₹4,980 both) now expect the corrected budget. Docs:
  `TITAN_INSTRUCTIONS.md`, `docs/LAPTOP.md`, `docs/RUNBOOK.md`, the build report, `docs/spec/FINAL_SPEC_DETAIL.md`
  §8.5 (its review) and errata lines in the spec.

### Paper model v2 (2026-10-14)
G1 judges every strategy on paper P&L, so the paper exchange must never look better than LIVE. Review round 2 found
three ways it could. They are fixed together, as one recalibration: `PAPER_MODEL_VERSION` goes from 1 to 2 (one bump
for all three, decided by the owner's delegate: today it costs the least). Two are gone; the first is only halved, so
on a stop into a lone one-lot bid paper can still look better than LIVE (below).
- **A thin bid or offer now fills part of the time.** Paper takes half of the depth it walks (`PAPER_PARTICIPATION`
  0.5) and rounded that half down to whole lots, so a lone 1-lot bid inside an exit's limit never filled. In the
  review's test, a SONACOMS stop-out during a one-second gap: one lot (600) bid at ₹21.90, 19% under the ₹27.05
  entry. The exchange sells into that bid on the first IOC: ₹3,090 lost (₹3,170 after charges). Paper's IOC was
  unfilled, the exit tried again once the market came back and sold at ₹26.95: ₹146 lost. Now the leftover part of a
  lot fills with that chance, decided by a random draw that belongs to the order (seeded by its tag, so replaying a
  day gives exactly the same fills). On average paper fills half of the depth it walks, on entries and exits alike.
  So a lone 1-lot bid or offer inside the limit fills with probability `PAPER_PARTICIPATION`, about half the time,
  not always as on LIVE, where the IOC usually takes it. In the review's test the stop's draw still misses the bid
  and paper still books the ₹146 scratch; with another draw it books the ₹3,170 loss. (An earlier version of this
  entry said "as on the exchange" and "all three are fixed": wrong for this one.) NSE shows whole lots, so at 0.5
  the draw only ever decides such a half lot.
- **A book that cannot be real is never filled against.** A crossed or locked book (best bid at or above the best
  ask), or one stamped by the exchange in an earlier second than the quote the order was decided on (a late WebSocket
  packet that arrives after the order left, showing a market that was already gone; the engine sends that quote's
  time with the order), now counts as no book: the order waits for the next one or asks REST, up to 1.5 s as before.
  If nothing usable comes it is cancelled `SIM: crossed or locked book` or `SIM: book older than the decision`. The
  daily report has a new table, paper orders not filled by SIM reason, which counts these next to
  `SIM: no fresh depth`, `SIM: IOC unfilled` and the rest. The 15:26 square-off waits out a crossed book the same
  way, and if only a crossed or locked book comes it sells on it at its worse side (follow-ups below).
- **The paper exchange's calibration can no longer be changed by a settings file.** `PAPER_LATENCY_MEDIAN_MS` and
  `PAPER_LATENCY_SIGMA` accepted 150 ms and 0.3, so a settings file could make paper orders meet a book closer to
  their decision quote. Raising their bounds to the defaults was not enough: no direction of these keys is safe
  (follow-ups below). So every key that only calibrates the paper exchange is fixed at its default:
  `PAPER_PARTICIPATION` 0.5, `PAPER_LATENCY_MEDIAN_MS` 250, `PAPER_LATENCY_SIGMA` 0.6, `PAPER_READ_LATENCY_MEDIAN_MS`
  250, `PAPER_READ_LATENCY_SIGMA` 0.85, `PAPER_ADVERSE_TICKS` 0, `PAPER_PRICE_BAND_PCT` 20 and `PAPER_BOOK_MAX_AGE_S`
  1. A settings file may restate a default but set nothing else: another value, higher or lower, makes the whole
  file invalid (HALT SETTINGS_INVALID at the next start, which then runs on the code defaults; remove the key with
  `titan2 settings unset KEY`). Changing one is a code change with a `PAPER_MODEL_VERSION` bump. `PAPER_CAPITAL_RS`
  is the paper account's size, not a calibration, and stays tighten-only (100,000 to 500,000).
- Follow-ups, from the review of paper model v2 (the same day; all part of paper model 2, so no further version):
  - **A late packet that arrived just before the order reached the paper exchange became "the decision".** The
    older-book rule took the decision's exchange time from the newest quote in the cache when the order arrived.
    The cache keeps the newest by arrival, so a late WebSocket packet received between the engine's decision and
    that moment passed for the decision, and a second late packet, still older than the quote the engine priced
    from, filled the order: in the review's test a paper BUY of 350 HCLTECH 1500CE at ₹25.80, under the decision ask
    of ₹26.05. The engine now sends the decision quote's exchange time with the order
    (`OrderRequest.decision_exch_wall`, never sent to Kite), and the rule uses that.
  - **"Higher is tighter" was false for the paper calibration.** Each order draws a fixed seeded number for its
    delay, so a wider `PAPER_LATENCY_SIGMA` (2.0 was accepted) made 90 of 200 orders faster than at the default. A
    slower `PAPER_LATENCY_MEDIAN_MS` (3,000 ms was accepted) landed the review's stop exit after its one-second gap
    (with a 2-lot bid, so no draw involved) and turned the ₹3,170 loss into a ₹146 scratch. A lower
    `PAPER_PARTICIPATION` (0.1 was accepted as "lower is tighter") filled the lone gap bid for 11% of orders instead
    of 52%. Hence the fixed calibration above; this entry had said the delay could "no longer be set faster than its
    default", which the sigma still allowed.
  - **A crossed book at the 15:26 square-off sold a long at ₹0.05.** A book that stayed crossed for the whole 1.5 s
    wait (REST included) counted as no book, so the position got the no-book price, a near-total loss: 100 @ ₹0.05
    in the review's test, where the book showed ₹10.00 to ₹10.50 (before paper model 2 it sold at the ₹10.50 bid;
    LIVE would sell near ₹10). It now fills on that book at its worse side: a sell never above the best ask (₹10.00
    there), a buy never under the best bid.
  - **The condor tracker and `titan2 whatif` traded on books that cannot be real.** The condor booked a locked leg
    (bid equal to ask) at a zero spread, in its real fill, its 2x-spreads fill and the kill rule's median spread; a
    locked leg now skips the week, as a crossed one or a missing side already did (`NO_TWO_SIDED_QUOTE`). The
    what-if bought a crossed refused quote at its stale ask, under the bid; a crossed or locked refused quote now
    counts as no quote (the next minute's open plus the assumed half-spread). Neither uses the paper exchange's fills
    or changes a strategy version.
- Every strategy gets a new version at the next start, one for this and the same release's engine change together
  (`ENGINE_SEMANTICS_VERSION` 2, "Signals wait for unfinished orders" below): `opening_drive` 1.54ba54e096 →
  1.b230b83102, `price_spike_up` 1.1882405ca0 → 1.3a030f3e26 and `price_spike_down` 1.6d19c4137a → 1.9f039e44a7.
  Three new G1 trials are registered and the running ones retired, so G1's family size m goes from 9 to 12: α per
  look 0.1042% instead of 0.1389%, bootstrap B 48,000 instead of 36,000. Today's paper trades (made with paper
  model 1) stay in the ledger and count only for the old versions' trials; a session already running keeps paper
  model 1 until its next start. Unchanged: the strategies, every setting's default, the exit rules (`decide()`, the
  stop's deep-breach bypass). The condor tracker and `titan2 whatif` change only as in the last follow-up (neither
  uses the paper exchange's fills; an earlier version of this entry listed them as unchanged).
- Tests: new cases in `tests/unit/test_paper_realism.py` (the draw, the expected fill, the lone bid, a replay; and
  the review's three calibration probes: a wider sigma, a slower median, a lower participation),
  `test_paper_fill_time_book.py` (lagged, crossed and locked books, the new SIM reasons, the one-second rule, a late
  packet just before the order reaches the exchange), `test_paper.py` (the square-off, and a book that stays
  crossed or locked), `test_settings.py` (every bound is its default; the paper calibration is refused on both
  sides), `test_seams.py` (the engine's request carries its decision time), `test_condor.py` and `test_whatif.py`
  (locked and crossed quotes), `test_ledger_g1.py` (the report's SIM table) and the new
  `tests/scenarios/test_paper_gap_exit.py`, whose one-lot case is the bias left (this order's draw misses the bid).
  `fill_from_depth` takes the order's draw, which `tests/fakes/fake_kite.py` gives a LIVE order from its PAPER
  twin's tag so the dual runs still match; the scenarios with thin books (`test_paper_one_lot_ladder.py`,
  `test_postmortem.py`, `test_restart.py`) state their orders' draws. `test_docs.py` pins the new versions, m = 12,
  the fixed calibration and the honest lone-lot wording. Docs: `TITAN_INSTRUCTIONS.md` §4 and §8, `docs/LAPTOP.md`
  §8, the build report, `docs/spec/FINAL_SPEC_DETAIL.md` §8.4 and an errata line in the spec.

### Signals wait for unfinished orders (2026-10-14)
The user's decision after the 09:20 batch of 2026-10-14. `opening_drive` sent three signals within 22 ms: TATAELXSI
(up 5.06%) at 09:20:00.190, POLYCAB (down 2.72%) at 09:20:00.206 and TCS (up 2.51%, on 17.9 times its usual volume)
at 09:20:00.212. TATAELXSI and POLYCAB took the book's two position slots while their quotes were being fetched, so
TCS was refused `POSITIONS: 2/2` with nothing open. Two seconds later neither of them traded: at 09:20:02.192
TATAELXSI's option had one lot of buyers near the mid where the rule asks for three, and at 09:20:02.248 POLYCAB's
had a 2.1% spread. So the day's `opening_drive` batch made no trade, although both slots were free 2 s later.
- **The rule.** A signal refused only because of entries that are not finished yet (reserved, or sent and not yet
  filled) now waits instead, holding nothing, for at most its time to live (10 s for `opening_drive`). On every
  250 ms tick, and before any newer signal is looked at, it goes through every entry check again, oldest first, on
  fresh data, as if it had just arrived. It ends entered on its own fresh quotes, refused exactly as before (two
  positions really open: `POSITIONS: 2/2`), or expired. Replayed, TCS waits 2.038 s, is reserved by the 09:20:02.250
  tick and its 3080CE order (175) goes out with its placebo, decided on its own quotes; the book never holds more
  than two.
- **What can wait:** the positions cap, the entries cap (a LIVE book's 1 in its first week too), the attempts cap,
  the entry orders a minute still to come from reserved entries (follow-up below) and the LIVE account's positions
  cap, when only unfinished entries are in the way. Everything else is refused at
  once, as before: the order caps, a halt, a bad counter, a gate error, a stock the book already holds or is cooling
  down on, the day's loss limits. SHADOW and placebo books never wait. In LIVE mode the PAPER twin waits with its
  LIVE book (both then decide on one quote fetch) and never on its own.
- **No limit changes:** two positions per book, three entries and six attempts a day, 40 orders a day and three
  entry orders a minute. Nothing is reserved while a signal waits, so the positions and entries caps hold at every
  moment (invariant I11 checks them after every event), and since the follow-up below so does the three entry
  orders a minute. (This entry first said all the caps held: one batch could send a fourth entry order inside the
  minute.) The waiting signals are checked against the counters as they are after that tick's own fills and closes
  (the engine refreshes them first).
- **What you will see:** an `INTENT_WAITING` event when a signal starts waiting (the entries it waits for, its time
  to live). Its routing row says REFUSED meanwhile, and that is what stays if the bot stops or restarts during the
  wait: the wait lives in memory only and a signal is never replayed. When the wait ends, its `INTENT_ROUTED` event
  has `waited_s` and `waited_for`.
- **The order-rate limit (RUNAWAY).** A woken signal is checked like a new one, also against three entry orders in
  60 s, but it never raises the RUNAWAY halt (the owner's decision: the orders it counts are its own batch's). A new
  signal in that minute still halts the book, and a batch can now lead to that: when the first orders of a batch of
  three or more fill nothing, its waiting signals take the freed slots and send orders too, so the next signal
  within the minute meets three entry orders. The book then stays halted until `titan2 ack-halt`, and left unacked
  for more than 10 minutes of the entry window the day counts as IMPAIRED for that strategy's G1.
- **One quote retry.** On 2026-10-13 an entry was voided `QUOTE:FETCH_FAILED` because Kite had closed an idle
  connection and the quote request died before any answer (`Connection aborted.`). Such a quote read is now made
  once more at once, after the next slot of the one-a-second quote limit (so up to 1 s later). Only that case: a
  timeout, a failed connect, an SSL error or any answer from Kite still fails at once, and nothing else is retried
  (no other read, no order); as first built, a timeout while the request was being sent and a garbled answer were
  retried too (follow-up below). Each retry logs a WARNING with the run's count so far; if the second try fails too, the
  read fails as before and its error text ends `[retried once: the first try's connection dropped]`. The same quote
  reads serve exit refreshes, the self-test and the paper exchange's own depth reads, so those get the retry too.
- **Versions.** `ENGINE_SEMANTICS_VERSION` goes from 1 to 2, once for this release. It covers this change and the
  engine fixes of review rounds 2 and 3 above that change how titan2 trades, books or counts for G1: RECOVERED books
  in the daily stops; the late-accept window after a restart and across reloads, and NOT_PLACED by when the lookup
  was asked; paper leftovers booked from the paper exchange's record of their close (and settled on trading days
  only); the late start's wait (its grace counted from READY) and the EOD steps run in order when due together; the
  truth check by the trade's own day, counted once for the books it names and without the closes no tradebook
  shows; receipt times in G06 and G16; the day budget's inputs; the daily stop with an unknown MTM; and G1's sample
  in booking order (the rest of those rounds changes neither trades nor G1's counts). It ships with paper model 2,
  so every strategy gets one new version for both: `opening_drive` 1.54ba54e096 → 1.b230b83102, `price_spike_up`
  1.1882405ca0 → 1.3a030f3e26 and `price_spike_down` 1.6d19c4137a → 1.9f039e44a7. That is three new G1 trials, so
  m = 12 (α per look 0.1042%, bootstrap B 48,000), as in "Paper model v2". `REGISTRY.lock` records engine version 2;
  `SEMANTIC_CONSTANTS` and its hash are unchanged.
- **Known edges.** A signal woken with little of its 10 s left can be reserved and still run out of time before its
  decision (a VOID starts that stock's 5-minute cooldown). An operator halt during the wait sends it to SHADOW and
  marks the day IMPAIRED, as for a new signal (the laptop's Ctrl+C FLATTEN counts as one). In LIVE mode the PAPER
  twin can now expire together with its waiting LIVE book. The retry relies on requests' and urllib3's class names
  and on the text `Connection aborted.` (both libraries are pinned): if an upgrade changed them, quote reads would
  fail at once again, as before, so a test builds the chain with the pinned libraries and fails first.
- Follow-ups, from the review of this change (the same day; part of engine semantics 2, so no further version):
  - **One batch could send four entry orders inside a minute.** The three-a-minute check counted only entry orders
    already made, never a reserved entry about to send one. In the review's batch of four (TATAELXSI and POLYCAB
    reserved, TCS and INFY waiting), both first IOCs filled nothing and freed both slots before the next 250 ms tick,
    which woke TCS and INFY together: each counted the 2 orders made, both were reserved, and four entry orders went
    out in 0.33 s with no halt (on LIVE, four real orders). Two new signals right after two unfilled IOCs did the same
    before the waits existed. Now every reserved entry counts as an order still to come, so no mix of new and woken
    signals sends a fourth entry order inside 60 s. A signal refused only by those orders still to come
    (`ENTRY_ORDERS: 2 in 60 s + 1 reserved/3`) waits like one refused `POSITIONS: 2/2`, with no halt: if the reserved
    entry voids without an order it goes ahead, and once that order is out it is refused RUNAWAY without the halt (a
    woken signal). The RUNAWAY halt itself still counts only orders made, so a new signal is halted exactly as before.
  - **The quote retry also retried what the docs said it never retries.** urllib3 reports a timeout while the
    request is being sent, and a garbled status line or headers, as the same `Connection aborted.` error as a
    dropped connection, and the retry took them all. A send timeout so held the quote lane (and the exit refreshes
    queued behind it) up to about 11 s instead of 5. Now only a connection the other end closed before any answer is
    retried: closed with no status line, reset, or aborted (Windows' error for an idle connection the server closed).
- Tests: the new `tests/unit/test_wait_pending.py` (the gateway on its test engine, a daily stop raised during the
  wait, `risk.settled` and `risk.waits_for_pending`, the 09:20 batch on the real engine, a reload and a SIGTERM
  during the wait, the counters' refresh, the quote retry and the libraries' own exception chain) and
  `tests/scenarios/test_wait_for_pending.py` (the 09:20 batch end to end on both accounts, a restart while TCS
  waits, and the RUNAWAY decision: a woken signal refused without the halt, a new one halting the book). The test
  engine of `test_gateway_exitloop.py` now counts pending structures like the real one, `test_settings.py` expects
  engine version 2 and `test_docs.py` pins the release's versions, m = 12 and these docs. The follow-ups: the batch
  of four, two new signals after two unfilled IOCs and a reserved entry that voids unsent (`test_wait_pending.py`,
  the batch also end to end on both accounts in `test_wait_for_pending.py`), and what urllib3 raises for a send
  timeout, a garbled answer, an SSL EOF, a reset and an abort (`test_wait_pending.py`); `test_wait_pending.py`'s
  settled-view test also accepts `ENTRY_ORDERS` among the caps that count pending structures. Docs:
  `TITAN_INSTRUCTIONS.md` §5 and §6, `docs/RUNBOOK.md` (the RUNAWAY halt), `docs/LAPTOP.md` §8 and §10, the build
  report, `docs/spec/FINAL_SPEC_DETAIL.md` §4.6, §5.4, §5.8, §8.4 and §8.6, and three errata lines in the spec.

### Added
- **`titan2/` package** with its own process, database and systemd units. A functional core: one thread runs
  `Engine.handle(event)` inside one SQLite transaction and sends commands only after the commit, so every order row
  exists before its HTTP request. All network I/O runs on lanes; one writer per database file.
- **Order path**: LIMIT + IOC + MIS only; a full order state machine with tag lookup for ambiguous answers; 15-character
  `T2…` tags; one working order per leg; the order bucket stays under SEBI's 10 orders/s.
- **Paper exchange** (`titan2/broker/paper.py`): fills from the order book at fill time (a book received after the
  order was sent, else REST depth within 1.5 s, else cancelled), 50% participation of the depth walked, lognormal
  latency, its own persisted ledger, the broker square-off simulated. Every scenario test runs on PAPER and LIVE (fake
  Kite) and must give identical commands and P&L.
- **Exits** (`titan2/core/exits.py`): levels frozen from the entry fills (hard stop 15%, lock armed at breakeven + 4%,
  floor breakeven + 3%, 70% trail, target 15%, 45-minute time stop), decisions on the realizable bid of a quote at
  most 3 s old, debounced stops, protective exits on stale quotes or no bids, an exit ladder never more than 12% under
  the bid, EOD flatten at 15:15.
- **Risk**: one `size()` (1 lot), 22 fail-closed entry gates (liquidity on both sides of the book, charges hurdle,
  worst case, funds), caps of ₹5,000 per trade, ₹10,000 per day on realized + bid MTM, ₹40,000 worst case; sticky
  halts that never block exits; tighten-only settings read once per process.
- **Reconciliation and recovery** (`titan2/core/recon.py`): signed, per product, lag-safe (only snapshots taken after
  the last fill count, confirmed twice), never sells; verify-flat before every close; restart recovery by tag;
  controlled restarts on token rotation and feed loss.
- **Charges** (`titan2/charges.py`): dated Zerodha RateSets (`2026-04-01`: STT 0.15% on sell and exercise) and the
  single P&L function `net_pnl`, used by the ledger, MTM, status, reports, the EOD truth check and G1.
- **Strategies**: `opening_drive` v1 and `price_spike_up` v1, both PAPER; a version hash over code, parameters, exit
  profile, settings and engine semantics; `REGISTRY.lock` makes silent tuning fail the tests.
- **G1 and LIVE** (`titan2/g1.py`): ≥ 100 trades over ≥ 30 sessions, 1.5× cost stress, day-clustered bootstrap with
  multiple-testing control, an opposite-option placebo, futility and confirmation; LIVE needs `ALLOW_LIVE=1`, a CLI
  unlock bound to the G1 report hash and a daily non-filling self-test; live review demotes on probation, drawdown,
  rolling losses or the X2 paper-model falsifier.
- **Dashboard** (`titan2/dash/`): read-only, no credentials, reached over an SSH tunnel; PAUSE, RESUME, FLATTEN_ALL.
- **Tests**: unit, scenarios (the 12 live killers, the May 13–14 postmortem, restart and lag cases), seeded chaos,
  architecture rules, golden fixtures from v1, ops scripts, and `tests/unit/test_docs.py`.

### Market facts (verified 2026-10-10; read from data, never hardcoded)
- NSE F&O trades 09:15–15:40 since 2026-08-03; Zerodha's MIS square-off time after that change is unverified [V], so
  it stays a setting (15:26) and titan2 flattens at 15:15 and stops sending at 15:25.
- NIFTY lot 65, BANKNIFTY 30 (from the instruments dump); NIFTY weeklies expire Tuesday, monthlies and BANKNIFTY on the
  last Tuesday (from the dump).
- Charges in force since 2026-04-01; SEBI's retail-algo limits (≤ 10 orders/s, a registered static IP for LIVE).
- No entry before 09:20; the paper exchange never fills against an option quote stamped before 09:15:30.

### Removed
- The v1 runtime: the scan loop, GPT agent, ML models, OI fetchers, thesis-hedge conversions, reversals and auto-pilot,
  GTT and SL-M safety nets, the `place_order` monkey-patch, hot-reloaded settings, JSON IPC files, the v1 dashboard,
  the CI deploy workflow, the v1 deploy kit and cron jobs. Retired whole and deleted by the v2.0.0 deletion commit;
  everything stays recoverable from the tag `v1-final` (commit `b4694f3`).
- Kept: the research data, the `_*.py` research scripts (unchanged), the model folders `ml_models/saved_models/`,
  `ml_models/reports/` and `ml_models/direction/`, and this changelog. `.kite_access_token` and `dhan_config.json`
  stay tracked until their deferred cleanup. `tests/arch/v1_retired.json` lists every deleted and kept file.

### Safety
- Never sells more than the broker holds or the bot's own fills explain; never sells a residual; never trades a
  position it does not own.
- P&L only from fills plus exact charges; LTP is never used for a money decision.
- No order outside the core: one Kite order adapter, no second order tool, a dashboard without credentials.
- A restart never double-enters and resumes exits before READY; a deploy cannot clobber state, secrets or server
  edits.

### Review fixes before release (round 1)
- Orders: an ambiguous place is looked up by tag at +1, +3, +7 and +15 s and then until 30 s after the answer;
  only then is it NOT_PLACED. A bot BUY Kite takes even later is adopted and sold at once (CRITICAL `LATE_FILL`).
  The LIVE self-test also needs its tag back in a WS order update and in `orders()`.
- Reconciliation: positions that show less than the bot holds book nothing unless a sale shows for it (a
  contradiction halts MISMATCH and keeps the leg managed); a lost bot SELL that filled is priced from its own
  average; an earlier session's open order is concluded at startup (CRITICAL `STALE_STRUCTURE`); the periodic
  recon runs until the last EOD recon, which moves with `BROKER_SQUAREOFF_AT`.
- Quotes: a tick received while an event is handled counts as fresh (age 0) everywhere, so the exit decision, the
  exit pricing and the entry gates agree; a slow positions or orders call no longer delays exit quote refreshes
  (new thread `lane-reads-quotes`).
- Paper realism: the decision snapshot never fills its own order; one-lot levels can fill one lot; the PAPER
  contract-note sample is priced by Zerodha's calculator on lane-reads, never by the paper exchange.
- Sizing: the depth term counts only levels near the mid, so stub bids far under the touch never count.
- Robustness: a rollback-and-reload keeps this process's timers, so other legs keep exiting during a reload storm
  (CRITICAL `INVARIANT` after 20 violations in 60 s); a slow timer no longer starves the event queue; a stray
  research script in the working directory no longer blocks a restart (only a module that shadows an import
  does, and with positions open the bot starts exits-only); a locked dashboard command file is skipped at once.
- Settings and schedules: `BROKER_SQUAREOFF_AT` may be 15:20–15:39 (the close is 15:40); the healthchecks.io
  dead-man schedule is now `45-59 8`, `* 9-14`, `0-33 15` (Mon–Fri): reconfigure the check.
- G1: the bar-mismatch rule counts only the sample's own signal bars, and CONFIRM applies the IMPAIRED and bar
  criteria too; a session the bot never ran in, or a halt blocking PAPER entries for over 10 minutes, marks the
  session IMPAIRED.
- Ops: deploy and rollback re-check the market-hours rule just before the switch and just before the restart;
  Stage 0 makes v1's leftovers under `/home/ubuntu` owner-only.

### Decisions B1 and B2, and the fixes before paper data counts (2026-10-11)
- Decision B1, strict: an exit is capped only by a positions read requested at or after the exit decision
  (`EXIT_CAP_PRIOR_READ_S` = 0; the 1 s window is gone), so a close just before the decision, once Zerodha's
  positions show it, is never sold again as a naked short. Paper pays that round trip too: the paper exchange answers
  positions and orders reads after a seeded delay (new versioned, tighten-only settings `PAPER_READ_LATENCY_MEDIAN_MS`
  250 ms and `PAPER_READ_LATENCY_SIGMA` 0.85: p95 about 1 s, clipped like the order latency), one queue per account.
  Each first exit order logs `decision_to_order_ms` in its `EXIT_ATTEMPT` event, and the daily report has the
  `exit_decision_to_submit_ms` row (the Stage 2 criterion, p95 under 1.5 s). The first release's paper model now
  includes this read delay; `PAPER_MODEL_VERSION` and `ENGINE_SEMANTICS_VERSION` stay 1 (the spec pins 1 for the first
  release, and no G1 data exists yet).
- Decision B2: `orders.LATE_ACCEPT_S` stays 30 s (no double sell); RUNBOOK §10 records why and how to measure it on
  LIVE before anyone shortens it.
- Placebo (F-18): sent together with its `PAPER:<s>` parent, from the same decision snapshot and transaction, so the
  price path during the parent's latency no longer decides which placebos fill (spec deviation: every entry sent,
  not every entry filled). SHADOW capital (F-19): each SHADOW-account book has its own paper pool (paper exchange
  schema v2: `p_orders.book`, a v1 file is upgraded on open). opening_drive (F-17): a WS reconnect before the first
  timer no longer skips the day; the opening bar's own validity decides. `strategies/base.py` (§3G) is part of every
  strategy's version and `REGISTRY.lock` entry. Recovery (F-7): an order sent after the recovery orders read is no
  longer reported vanished (no false INVARIANT halt). `titan2 bot` (F-15) refuses to start outside systemd unless
  `--outside-systemd`. A restart after an unclean exit during a session (F-16) sends CRITICAL `BOT_KILLED`. Strategy
  LOGIC_VERSIONs stay 1 for the same first-release reason.
- Tests: `k01` and `k02` sharpened (k01: an 8 s positions lag with a post-fill read inside the fresh-fill grace,
  checked every step; k02: a manual close and the hard stop at the same instant, so only a read made after the exit
  decision may cap the exit). The dead `risk.cooldown_end` is removed; the cooldown rule is tested where it lives,
  `Store.cooldowns`.
- Review of this batch: B1 now holds for every exit order. A retry after an unfilled or rejected IOC needs its own
  positions read, requested after that attempt ended, so a hand sale while the ladder runs is not sold again once
  Zerodha's positions show it (RUNBOOK P7 now says to stop the bot before selling by hand). When the reads made
  after the decision fail or hang, an older read no longer caps the exit at once: the exit waits and re-asks for
  10 s, and only then caps by the newest earlier read (new CRITICAL alert `EXIT_CAP_BEFORE_DECISION`; it never sells
  more than `EXIT_UNCAPPED` would). `EXIT_CAP_PRIOR_READ_S` moved from `recon` to `titan2/settings.py` and into
  `SEMANTIC_CONSTANTS`, and a strategy's version now hashes its own module's code as well as `base.py`'s, so neither
  can change without a new version even if `REGISTRY.lock` is refreshed by hand. A test now checks that SHADOW reads
  (the placebos') pay their own read delay. Both strategy versions change; `REGISTRY.lock` was refreshed with
  `ENGINE_SEMANTICS_VERSION` and both `LOGIC_VERSION`s kept at 1 (first release, no G1 data yet).

### Laptop mode: Windows, PAPER only (2026-10-11)
- titan2 runs on a Windows laptop first. `titan2 laptop` runs one trading day in the foreground: the trading-day
  check, the Kite session (auto TOTP, else a pasted request token), the new self-check phase `laptop`, the bot session
  in PAPER (recovery first; restarted in-process on exit 75, and up to 5 times on exit 3), the EOD timeline and the
  daily report. `titan2 laptop check` runs only the self-check; `titan2 dash` serves the read-only dashboard on
  127.0.0.1:5000 (werkzeug's server; any other address needs `--allow-public`). New `titan2/laptop.py`, the
  `ops/windows/` kit (setup, run-today and scheduled-task scripts, an env-file template) and `docs/LAPTOP.md`.
- Safety: the laptop profile refuses LIVE whatever `ALLOW_LIVE` or a strategy's mode says (a `LIVE_REFUSED` event).
  No LIVE order of any role leaves the laptop: its `KiteBroker` refuses every LIVE place and cancel, and recovery
  never imports today's titan2 LIVE orders that its own `titan2.db` lacks (EC2's, in the shared Kite account):
  HALT(UNKNOWN_POSITION, ACCOUNT:LIVE) and a CRITICAL alert instead. Every laptop self-check FAIL halts entries and
  never exits; the clock row blocks entries when the clock can be more than 1 s off internet time, the reading's
  own error counted. The first Ctrl+C sends a FLATTEN_ALL (also with nothing open), whose halt keeps entries off
  until the stop; it waits until verified flat, then stops cleanly without a RESUME (the engine clears that halt as
  the run ends flat, and the command expires instead of running at a later start); the second stops at once.
  Windows is kept awake while a session runs, and the power source is logged every 15 minutes.
- Windows defaults: state and logs under `%LOCALAPPDATA%\titan2`, secrets in `%APPDATA%\titan2\titan2.env` (the new
  `TITAN2_ENV_FILE` overrides it). The Linux paths, EC2 and the systemd units are unchanged.

### price_spike_down: a PAPER put experiment (2026-10-12)
- Added at the user's request, against brief E1 (which deleted v1's PRICE_SPIKE_DOWN: it won 1 of 6 trades in the
  2026-06-10 to 07-14 regime, an anecdotal sample): the new strategy `price_spike_down` v1
  (`titan2/strategies/price_spike_down.py`), the exact signed mirror of `price_spike_up` v1. On a stock that falls
  1.0% (0.6% from 09:45) against its 60 s baselines, on a trigger tick with at least half the recent average volume,
  and 12 s later is still at least 0.35% down (1.0% before 09:55), having given back at most 65% of its deepest
  fall, it emits a DOWN intent: an ATM PE, recheck band [LTP × 0.995, baseline × 0.9965], TTL 10 s; 90 s cooldown,
  09:20–14:55, the runner's 30 intents per minute. A move back up through its baseline fails; it never emits UP.
  Same PARAMS as `price_spike_up`, but its own version, `REGISTRY.lock` entry, G1 trial and scorecard; PAPER from
  its first session like every strategy, and the laptop refuses LIVE for it as for the others. Its placebo is a CE.
- The other two strategies' code is untouched: `strategies/base.py`, `price_spike_up.py` and `opening_drive.py` are
  unchanged, so are their versions and `REGISTRY.lock` entries (the lock was refreshed with the module's own
  helper; it only gained the new entry), and their G1 trials go on. The new strategy is registered last, so the
  other two still run first on a shared tick.
- What it does change for the other two (review of this entry; documented, no code change, since changing the G1
  rules would change the running trials): the three share the PAPER account's G1 controls. An operator halt with
  any structure open, its PE or CE placebo included, marks every G1 strategy's session IMPAIRED until 15:15, so the
  laptop's first Ctrl+C with a put open impairs `opening_drive` and `price_spike_up` too, even after 14:55; a
  MISMATCH or UNKNOWN_POSITION halt on its PE legs (`ACCOUNT:PAPER`, ACK) blocks every PAPER book and, after 600 s of
  the entry window, impairs every G1 strategy; a PAPER mismatch on a PE contract counts against `opening_drive` too
  if it traded that contract. The two spike strategies keep separate 90 s cooldowns where v1 had one per stock, so
  one stock can get a CE and, seconds later (rarely on the same tick), a PE. G1's family size m = max(trials ever
  registered, 4) is shared: a change that re-versions every strategy now registers 3 trials instead of 2 (the first
  gives m = 6, α per look 0.2083% instead of 0.3125%), and from its third version on each `price_spike_down` version
  raises m for every strategy.
- Tests: the mirror of every `price_spike_up` unit test, an AST check that the code is `price_spike_up`'s with only
  the mirrored lines changed, its registration and lock (the two running entries pinned), v1's own down-spike tape
  from the golden fixtures (it fires where v1 fired PRICE_SPIKE_DOWN, and nowhere else), the scenario
  `tests/scenarios/test_price_spike_down.py` (a PE paper entry whose placebo is a CE, on PAPER and LIVE, and on the
  laptop with no LIVE order) and the laptop's LIVE refusal for it. The scenario harness's LIVE fixture now names its
  earlier-session intents by the whole strategy name (`price_spike_up` and `price_spike_down` share six letters).
  The review's facts are pinned too: a laptop Ctrl+C with a put open impairs all three (with nothing open, or after
  15:15, nobody); a MISMATCH on a put halts and, after 600 s, impairs all three; one stock can hold
  `price_spike_up`'s CE and `price_spike_down`'s PE together, and both strategies can fire on one tick; and
  `tests/unit/test_docs.py` checks these docs' volume rule, cooldowns, IMPAIRED rules and m figures against the code.
- Docs: `TITAN_INSTRUCTIONS.md` §5, §11 and §14, `docs/LAPTOP.md` sections 4 and 8, and an errata line in the spec.

### titan2 keeps internet time on the laptop (2026-10-13)
- Found 2026-10-13: the laptop's Windows Time service was stopped (Manual start), the clock 1.43 s behind internet
  time, so the `clock` row would keep entries off all day, and the user has no admin rights to fix Windows.
- `titan2/clock.py`: `CORRECTION` (a `WallCorrection`): once it holds an offset, `RealClock.now_wall()` is the
  system clock plus that offset, never moving backwards (a lower re-measurement holds the clock still until the
  system clock catches up). Unset, `RealClock` is exactly as before, so EC2, the operator CLI and the tests are
  unchanged. Log records get the corrected time too (`install_log_correction`), and the dashboard child takes the
  session's offset from `TITAN2_CLOCK_OFFSET_S`.
- `titan2/laptop.py`: `correct_clock()` measures internet time (the self-check's own NTP / HTTP Date reading) at the
  start of a real `titan2 laptop` run, before anything is stamped, and the supervisor re-measures every 5 minutes
  on its own thread; no reading keeps the last offset, and a coarse reading (the HTTP Date fallback) never replaces
  an exact one. `TITAN2_CLOCK_CORRECT=0` turns it off.
- The laptop `clock` row judges the corrected clock (still 1 s, the reading's error counted) and reports the
  Windows offset beside it. The 1 s rule itself is unchanged: it now holds for the clock titan2 actually uses.
- Review of this entry (3 reviewers + a skeptic; 2 major, 2 minor confirmed, all fixed before merging): Windows
  Time on this laptop wakes up and steps the clock +1.1 to +2.5 s around midday on most days (System log, Time-Service
  id 35, 5-9 Oct), which the first version carried as an error of up to 5 minutes (all day if only the HTTP Date
  answered). Now a step is absorbed at the next timestamp: the system clock minus the monotonic clock moving by
  more than 0.25 s moves the offset by minus that, so titan2's time goes on unbroken, and a fresh reading follows
  within 20 s; a coarse reading that contradicts the offset in force beyond both errors is applied. A failed start
  reading is retried before the self-check and then every 20 s (it waited 5 minutes, after the one-time checks
  had kept entries off for the day). The start reading is measured after the log is set up, so the log file
  records it.
- No engine, strategy, paper-model or G1 version changes: the engine sees the same events, stamped with internet
  time instead of a clock that was up to 1.4 s off (the accuracy the spec already assumed).
- Tests: `tests/unit/test_clock_correction.py`.

### Risk caps raised (2026-10-13)
- The user's decision, after a 1-lot POLYCAB 8200CE entry was refused that morning: its planned stop-out came to
  ₹5,668 (the 15% stop is ₹4,166 on a ₹27,775 premium; the 5% slippage allowance adds ₹1,389 and charges ₹113),
  over the ₹5,000 per-trade cap, and its premium was over the ₹25,000 premium cap. `titan2/settings.py` raises
  four defaults together with their ceilings: `MAX_LOSS_PER_TRADE_RS` 5,000 → 15,000, `MAX_PREMIUM_PER_TRADE_RS`
  25,000 → 50,000, `DAILY_STOP_RS` 10,000 → 15,000 and `MAX_WORST_CASE_DAY_LOSS_RS` 40,000 → 50,000. Unchanged: 1
  lot, 2 positions per book, the 5% slippage allowance and the ₹20 premium floor.
- The LIVE-only caps do not change: the LIVE account's ₹10,000 daily stop (`LIVE_ACCOUNT_DAILY_STOP_RS`), the
  ₹15,000 LIVE drawdown stop (`LIVE_DD_STOP_RS`) and the ₹6,000 probation loss (`LIVE_PROBATION_LOSS_RS`). They
  matter only for LIVE, which the laptop refuses; one full-size stop-out (about ₹10,140) is already more than the
  probation loss, so look at them again before any LIVE.
- In practice a 1-lot trade now passes with about ₹6,200 to ₹49,850 of premium (NIFTY about ₹96–₹766, BANKNIFTY
  about ₹207–₹1,661). The worst-case gate G20 sets that ceiling (premium, entry charges and the ₹100 pad within
  ₹50,000), so the largest planned stop-out is about ₹10,140 and the ₹15,000 per-trade cap does not bind at 1 lot.
  A book halts for the day at a ₹15,000 loss; G20 keeps its realized loss plus every open premium within ₹50,000.
- All four keys are versioned, so at the next start every strategy gets a new version and its G1 trial starts
  again: `opening_drive` 1.03a618af25 → 1.8acb18e334, `price_spike_up` 1.a2a5683926 → 1.ffdfbbebc8 and
  `price_spike_down` 1.1199b091c0 → 1.ef56e0791a. The three running trials (begun 2026-10-12 and 2026-10-13) are
  retired and three new ones registered, so G1's family size m goes from 4 to 6: α per look 0.2083% instead of
  0.3125%, bootstrap B 24,000 instead of 16,000. The new caps apply from the next `titan2 laptop` start; a session
  already running keeps the old ones.
- Tests: `tests/unit/test_settings.py`, `tests/unit/test_sizing.py`, `tests/unit/test_sizing_review.py`,
  `tests/unit/test_risk.py`, `tests/unit/test_seams.py` and `tests/scenarios/test_killers.py` (k12) use the new
  values. Docs: `TITAN_INSTRUCTIONS.md` §1, §6 and §8, `docs/spec/FINAL_SPEC_DETAIL.md` (§0.2, §8.4, §8.5, §8.8,
  §17.1) and an errata line in `docs/spec/FINAL_SPEC.md`.

### Phone alerts through ntfy; no uncertain clock readings (2026-10-14)
- `titan2/alerts.py`: ntfy (https://ntfy.sh, a free push-notification app) is a message channel next to Telegram, which
  is not an option for the user: `NTFY_TOPIC` (a long random topic, the only secret; never in a log line) and an
  optional `NTFY_SERVER`. The alert's first line is the notification title; CRITICAL rings as urgent, WARN as high,
  INFO (trades opened and closed, the daily report) as normal. With both channels set, each alert goes to both.
- The 2026-10-14 08:04 start read internet time once, got a +-3.6 s answer and moved titan2's clock 3.5 s ahead (the
  self-check caught it and kept entries off): each NTP host is now asked 3 times and the most exact answer kept, and
  a reading less exact than 0.75 s is never applied (retried every 20 s instead).
- Tests: `tests/unit/test_alerts_ntfy.py`, and two clock-reading cases in `tests/unit/test_clock_correction.py`.

### Three entries a day (2026-10-13)
- The user's decision, after `price_spike_up` used its 2 entries of the day on paper (HCLTECH at 11:38, PAYTM at
  13:34) and then refused 23 more signals from 13:36 on (`BOOK_CAPS`, `ENTRIES: 2 + 0 pending/2`), PRESTIGE's rally
  at 14:31:47 among them. `titan2/settings.py` raises `MAX_ENTRIES_PER_DAY` from 2 to 3, as the default and as its
  ceiling. The limit counts per strategy book, so `price_spike_up` (calls), `price_spike_down` (puts) and
  `opening_drive` may each open 3 positions a day. On a day like this one the third entry goes to the first later
  signal that passes every other check, not necessarily PRESTIGE's.
- Unchanged: at most 2 positions open at a time per book (a third entry needs one of the first two closed), 6 entry
  attempts and 40 orders a day per book, 3 entry orders a minute, and every loss cap: a third trade must still fit
  what is left of the book's ₹15,000 daily stop, and G20 keeps the day's worst case within ₹50,000. LIVE keeps 1
  entry a day for its first 5 sessions (the laptop never trades LIVE). The research rule D1 measured 2 a day
  (`docs/research/stock_options_evidence_brief.md`); 3 was not tested there.
- One side effect: the runaway alarm (3 entry orders from one book within a minute) can now be set off by 3 trades
  alone. If all three of a book's trades go out within one minute and another signal comes before that minute is
  up, the book halts (`RUNAWAY`) until `titan2 ack-halt`; before, that signal got a quiet daily-limit refusal. It is
  rare, since the third trade needs one of the first two closed by then. Left un-acked for more than 10 minutes of
  the entry window, the halt also marks that day IMPAIRED for the strategy's G1 test.
- The key is versioned, so at the next start every strategy gets a new version and its G1 trial starts again:
  `opening_drive` 1.8acb18e334 → 1.54ba54e096, `price_spike_up` 1.ffdfbbebc8 → 1.1882405ca0 and `price_spike_down`
  1.ef56e0791a → 1.6d19c4137a. The three running trials are retired and three new ones registered, so G1's family
  size m goes from 6 to 9: α per look 0.1389% instead of 0.2083%, bootstrap B 36,000 instead of 24,000. The new
  limit applies from the next `titan2 laptop` start; a session already running keeps 2.
- Tests: `tests/unit/test_settings.py` and `tests/unit/test_runtime_cli.py` use the new default and bound;
  `tests/unit/test_risk.py` moves the G09 cases to the new edge and adds `test_entries_cap_edge` (the third entry
  passes, a fourth is refused, a LIVE book's first week stays at 1, and RUNAWAY is checked before ENTRIES).
  `test_pm_b_no_reverse_churn` (`tests/scenarios/test_postmortem.py`) now opens and closes an earlier entry over a
  minute before its stop-out, so its storm of triggers still ends at the entries cap (the third entry passes, every
  later trigger is refused) without a RUNAWAY halt. The new `test_pm_b_storm_within_a_minute_runaway` keeps the old
  staging (the stop-out is the day's first entry, the storm comes seconds later): three entries, then the next
  trigger is refused as RUNAWAY and halts the book, and every later one is refused by that halt.
  `tests/unit/test_docs.py` checks the G1 figure in `docs/LAPTOP.md`. Docs: `TITAN_INSTRUCTIONS.md` §8,
  `docs/RUNBOOK.md` §8 (Stage 3: at least 34 sessions instead of 50), `docs/LAPTOP.md` §8 (G1's chance per look
  with 9 trials), `docs/spec/FINAL_SPEC_DETAIL.md` (§8.4, §13.11, §16.3, §17.1) and an errata line in
  `docs/spec/FINAL_SPEC.md`.

### PAPER iron condor tracker (2026-10-12)
- New `titan2/condor.py` and `titan2 condor [status|dry-run|tick|settle]`: the research's one surviving setup, a
  NIFTY weekly far-OTM iron condor, run on paper exactly as `docs/research/edge_research_verdict.md` specifies:
  entry at 15:25 on the 3rd trading day before expiry (the instruments dump decides the expiry, so holiday shifts
  are followed: the 2026-10-19 Monday expiry enters on Wednesday 10-14); F from the ATM call and put mids, sigma
  from that straddle (Black-76, T = 3/252), shorts at F e^(-/+1.5 sigma sqrt(3/252)) rounded out to 50, wings 2% of
  F further out; wings bought at the ask and shorts sold at the bid (depth walked for the lot), plus a 2x-spread
  version; the 3x-credit stop at 15:25 on each later day but expiry; cash settlement at NIFTY 50's close with Rs 20
  + GST per in-the-money leg and exercise STT on in-the-money longs; a no-stop result for every condor; the spec's
  kill rules (credit vs day close, spread vs cost model, wins in 26/52, -Rs 42K at 2x, loss beyond max, P&L
  reproducibility) stop new entries.
- It has no order path and shares nothing with the trading engine: its own database (`db/condor.db`), its own
  thread in the laptop supervisor on real-time sessions only (never under `--date`; settle once at start when that
  database exists, tick once a trading day in 15:25-15:29:30, tried up to 3 times 45 s apart while a step fails: a
  tick counts as done only when its mark and entry did not fail), read-only Kite calls 1.1 s apart (quote,
  historical day bars, basket margins). `titan2 condor tick` and `settle` refuse while a session runs, and every
  write is conditional on the state it read. No strategy, engine or G1 version changes. `TITAN2_CONDOR=0` turns it
  off, and a condor failure never stops the trading session.
- Review of this entry (no blocker; fixed before the first entry): the 3x stop no longer waits for a wing with no
  bid (marked at half its ask, abandoned on the exit: no order, nothing back; only a short with no ask postpones
  the stop); the four legs get their own snapshot, so a high-volatility week's far wings are always quoted; the
  2x-spread fill is the real fill plus one more half-spread (never better than the real one on a thin book);
  settlement runs before the entry, so the kill rules see it; a condor expiring today does not block the next
  expiry's entry; no entry for an expiry beyond the session calendar (2026-12-31); stale or unstamped quotes skip
  the week; the NIFTY future and each leg's day close are logged.
- Not built (documented): the T-2/T-1 entry shadows and the NSE bhavcopy download (each leg's Kite day close
  stands in for it); a 15:25 the laptop misses is a missed entry or stop check.
- Tests: `tests/unit/test_condor.py` (the research's own strike example, parity and IV, the holiday-shifted entry
  day, fills, marks and settlement arithmetic, a held week and a stopped week end to end on a fake Kite, the stop
  with a bidless wing, a short with no ask, skips on a one-sided, stale or unstamped book, a high-volatility week,
  a failed tick retried, an entry on the last expiry's day, Kite pacing, every kill rule, the supervisor job's
  timing and retries, the laptop attaching it only to real-time sessions, the CLI and the laptop lock).

### What-if report, Kite over IPv4, the `network` row (2026-10-12)
- Every entry refused after its contract was chosen now first writes an `ENTRY_REFUSED` event: the gate and detail,
  the contract (symbol, token, lot, strike, expiry) and the decision snapshot the bot saw (bid, ask, depth; none
  when the fetch itself failed). Information only: no decision reads it, so no strategy or engine version changes.
- New `titan2 whatif [--date D]` (`titan2/whatif.py`), run by the laptop after each session's daily report: each
  refused entry is replayed as one lot bought at the ask the bot saw, on Kite's 1-minute bars read as bids (less the
  half-spread seen), under the strategy's own exit levels (hard stop, lock and trail, take profit, time stop, the
  15:15 flatten), each minute read worst first, net of Zerodha's charges. Saved to `reports/whatif-<date>.json`,
  printed with running totals per refusal reason. Read-only; it flatters the refused trades (no depth, no slippage),
  so a filter is judged only on weeks of totals.
- Kite REST goes over IPv4 only in every titan2 process (`titan2/net.py`, called first in `__main__.main()`;
  `TITAN2_IPV6=1` turns it off): on 2026-10-12 the laptop's IPv6 route to api.kite.trade lost about half its packets,
  and two entries were refused on 5 s read timeouts. KiteTicker already resolved IPv4 only.
- Laptop self-check row `network` (WARN only): 8 timed IPv4 TCP connects to api.kite.trade:443; a failed connect,
  one slower than 900 ms (a resent packet) or a median above 250 ms warns that the day will see Kite timeouts.
- Tests: `tests/unit/test_net.py`, `tests/unit/test_whatif.py`, the `network` row in
  `tests/unit/test_selfcheck_laptop.py`, and the cheap-option scenario (k07) now checks its `ENTRY_REFUSED` event.

### Operations
- New units `titan2-bot`, `titan2-dash`, `titan2-token`, `titan2-selfcheck`, `titan2-backup`, `titan2-alert@`; no
  blocking `ExecStartPre`; flock, systemd watchdog, healthchecks.io dead-man.
- Immutable releases with hash-locked venvs; `ops/deploy.sh` aborts on drift or open positions; CI runs tests only.
- Cutover in reversible stages: Stage 0 masks v1 and rotates the leaked secrets, Stage 1 installs v2, Stage 2 is a
  ≥ 10-session PAPER burn-in with drills.

### Docs
- `TITAN_INSTRUCTIONS.md` rewritten for v2.
- New `docs/RUNBOOK.md`: playbooks, alert codes, halts, the morning self-check, cutover stages, drills and the [V]
  checklist with its results so far.
- v1 docs moved to `docs/v1/`: `TECHNICAL_DOCUMENTATION.md`, `MISSED_TRADE_DIAGNOSTIC_PLAYBOOK.md`, and a copy of the
  v1 instructions (`TITAN_INSTRUCTIONS_v1.md`).

### Open items
- The [V] checklist (`docs/RUNBOOK.md` §10), including a real contract note for the charges fixture and Zerodha's MIS
  square-off time after the 15:40 close.
- The 2027 NSE holiday list (before December 2026); the Elastic IP (Stage 0).

---

## 2026-03-09

### Architecture
- **Added SQLite `trades` table** to `state_db.py` — proper queryable trade ledger with indexed columns (date, symbol, source, underlying, status, order_id)
- **Hooked `trade_ledger.py` → SQLite** — every ENTRY/EXIT/CONVERSION event now writes to both JSONL and SQLite `trades` table
- **Created `trade_query.py`** — CLI tool for instant trade analysis (`--week`, `--month`, `--source`, `--underlying`, `--backfill`, `--exit-types`)
- **Created `TITAN_INSTRUCTIONS.md`** — comprehensive architecture doc (strategies, thresholds, exit types, ML models, deployment)
- **Created `CHANGELOG.md`** — this file

### XGB Direction Removal (Major)
- **Removed XGB direction (UP/DOWN) from ALL decision-making:**
  - `_ml_override_allowed`: Gate 4 (direction alignment) removed, max DR score used instead
  - Herd filter: disabled entirely
  - MODEL_TRACKER: ALL_AGREE concept removed — no score bonus, gate floor, or lot multiplier for direction agreement
  - SNIPER: XGB direction alignment + flip + ML_DIRECTION_CONFLICT filter removed
  - TEST_GMM: `require_xgb` direction gate removed
  - ARBTR: XGB direction opposition gate removed
  - TEST_XGB: strategy disabled (returns empty list)
  - Debit spreads: ML UP/DOWN boost replaced with P(move)-based quality
  - Iron Condors: ML FLAT/UP/DOWN quality replaced with P(move)-based quality
  - GPT direct-place: ML direction conflict filter removed entirely
- **Rationale:** Direction predictions were unreliable and caused false rejections. P(move) is more reliable for gating.

### Fixes
- Direction is still predicted and logged (for analysis) but never used to gate, boost, or reject trades

---

## 2026-03-08

### THP Improvements
- THP now skips interception when trailing SL is active (trade already in profit, let trailing SL manage it)
- THP skips when breakeven is already applied

---

## 2026-03-06

### State Persistence
- `state_db.py` created — SQLite WAL-mode database replacing all JSON state files
- Tables: active_trades, daily_state, exit_states, risk_state, order_idempotency, data_health, slippage_log, scan_decisions, live_pnl
- Migration from JSON → SQLite via `db.migrate_from_json()`

---

## 2026-03-02

### Trade Ledger
- `trade_ledger.py` created — append-only JSONL trade event logger
- Events: ENTRY, EXIT, SCAN, CONVERSION
- Daily files: `trade_ledger/trade_ledger_YYYY-MM-DD.jsonl`
- CLI viewer: `python -m trade_ledger [date]`

---

## Pre-March 2026

### Core System
- Autonomous scan loop (5-min cycles + real-time watcher)
- IntradaySignaler + IntradayScorer (0-100 scoring)
- ML pipeline: XGBoost move predictor, direction model, GMM/VAE DR anomaly
- Exit manager with 10+ exit types
- THP (Thesis Hedge Protocol) — SL/time stop → debit spread conversion
- VIX regime scaling (LOW/NORMAL/HIGH/EXTREME/PANIC)
- Correlation guard (sector concentration limits)
- Expiry shield (0DTE/1DTE gamma protection)
- Position sizing tiers (BASE/STANDARD/PREMIUM/ELITE)
- Dashboard (Flask SSE real-time monitoring)
- Zerodha Kite API integration (paper + live mode ready)
- GTT safety nets (server-side SL + target)
- Idempotent order engine

---

*Update this file EVERY TIME you make changes. Include the date, category, and a clear description.*
