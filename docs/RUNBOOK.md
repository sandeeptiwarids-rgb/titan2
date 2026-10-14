# titan2 runbook

What to do, step by step. How the system works is in [TITAN_INSTRUCTIONS.md](../TITAN_INSTRUCTIONS.md); this file
is for acting. Commands run on the server (`ssh titan-bot`) unless marked **laptop**. `titan2 …` is the CLI wrapper
`/usr/local/bin/titan2`: it runs `python -m titan2` from the current release as user `titan`. Running titan2 itself on
the Windows laptop (laptop mode, PAPER only) has its own guide: [LAPTOP.md](LAPTOP.md).

## 1. Ground rules

1. **When unsure, pause.** `titan2 pause` stops every new LIVE and PAPER entry (PAPER intents are diverted to the
   simulated SHADOW book) and never stops an exit.
2. **Only two things ever place orders: the bot, and you in the Kite app.** There is deliberately no script or second
   tool that trades. If the bot is down, use the Kite app.
3. **A halt never stops exits.** Clearing a halt only lets entries resume, so check first, then ack.
4. **The bot never touches what it does not own.** A foreign position, a residual or an unexplained short is yours to
   handle in Kite.
5. **The broker's MIS auto square-off** (assumed 15:26, still [V]) is the last backstop for anything left open.
6. **Never edit code or state on the server, never loosen a setting, never copy an env file anywhere.**
7. **Write it down.** Every ack takes a note (`--note "…"`), stored with the halt. Use it to say what happened and
   what you did.

## 2. Quick reference

| Need | Command or path |
|---|---|
| Is it alive? | `titan2 status` · `systemctl status titan2-bot` |
| Follow the log | `journalctl -u titan2-bot -f` · `tail -f /var/log/titan2/bot.log` |
| Last 200 lines | `journalctl -u titan2-bot -n 200 --no-pager` |
| Restart (recovery resumes exits first) | `sudo systemctl restart titan2-bot` |
| Dashboard | **laptop**: `ssh -N -L 8050:127.0.0.1:8050 titan-bot`, then open `http://127.0.0.1:8050` |
| Today's trades, report, G1 | `titan2 trades` · `titan2 report` · `titan2 g1` |
| Settings | `titan2 settings show` · `titan2 settings diff` |
| State | `/var/lib/titan2/` (`db/`, `pub/`, `session/`, `config/`, `reports/`, `exports/`) |
| Secrets | `/etc/titan2/titan2.env`, `/etc/titan2/dash.env` (never copy, never print) |
| Backups | `/var/backups/titan2/<date>/`: gzipped copies of the four DBs plus `settings.json`; 30 daily and 12 monthly sets kept. By hand: `sudo -u titan /home/ubuntu/titan/current/ops/backup.sh` |

**Units**

| Unit | When | Job |
|---|---|---|
| `titan2-bot.service` (+ `.timer`) | enabled; timer Mon–Fri 08:45 | the bot; exits 0 outside the session |
| `titan2-dash.service` | always | the dashboard on 127.0.0.1:8050 |
| `titan2-token.timer` | Mon–Fri 08:30 | Kite login, writes the session file |
| `titan2-selfcheck.timer` | Mon–Fri 08:40 | morning self-check → Telegram |
| `titan2-backup.timer` | Mon–Fri 16:10, Sat 03:00 | DB backups |
| `titan2-alert@.service` | OnFailure of any titan2 unit | CRITICAL `UNIT_FAILED` alert |

**Exit codes**

| Code | Who | Meaning | systemd |
|---|---|---|---|
| 0 | bot / CLI | done / ok | — |
| 1 | CLI `is-trading-day` | not a trading day (0 = yes, 2 = unknown) | — |
| 2 | CLI | refused or failed (the message says why) | — |
| 3 | bot | more than 20 exceptions in a minute, or a crash | restarts → recovery |
| 73 | bot | another bot holds the lock | no restart |
| 75 | bot | controlled restart (new token, or the feed gave up) | restarts → recovery |
| 78 | bot | fatal config or DB problem (dirty working dir, failed migration or integrity check) | no restart |

systemd allows 5 starts in 15 minutes. After that: `sudo systemctl reset-failed titan2-bot`, fix the cause, start
again.

## 3. Daily routine

| IST | What you do |
|---|---|
| 08:40 | Read the morning self-check on Telegram. PASS: nothing to do. FAIL: [P11](#p11-morning-self-check-failed) |
| 09:15–15:15 | Nothing. CRITICAL alerts repeat every 5 minutes until acked: find the code in [§5](#5-alert-codes) |
| 15:22 | Only if a `NOT_FLAT` CRITICAL arrives: [P7](#p7-not-flat-at-1522) |
| 15:38–15:40 | Read the daily report (2 minutes): trades, net after charges, exit reasons, refusals by gate, mismatches |
| Weekly | `titan2 report --date <d>` and `titan2 g1`: is any trial near a look, futile or failed? (10 minutes) |
| Yearly / on change | Holidays, hours, charges: [P15](#p15-calendar-hours-and-charges-updates) |

A normal paper day on Telegram: the morning self-check, a few `POSITION_OPENED` / `POSITION_CLOSED` lines, the daily
report. A quiet day with no trades is normal: the gates are strict on purpose.

## 4. Playbooks

Each playbook says when it applies, what to do, how to know it worked, and what not to do.

### P1. Bot down with positions open

**When**: a `UNIT_FAILED` alert, the healthchecks.io dead-man alert, the dashboard showing "bot offline", or
`titan2 status` showing an old heartbeat.

1. `ssh titan-bot`, then `titan2 status`, `systemctl status titan2-bot`, `journalctl -u titan2-bot -n 200 --no-pager`.
2. `sudo systemctl restart titan2-bot`. Recovery rebuilds every position from the broker and resumes exits before
   READY (target under 30 s). Watch `journalctl -u titan2-bot -f` for READY. After the 15:26 square-off a restart
   that still holds a leg keeps reconciling every 5 s (until 16:00, or a minute after READY when later) and books
   the broker's square-off before the 15:33 truth check and its own exit. If the day's last steps are all due by
   then, it runs them in order (the truth check, the bar check, the 15:38 files and report) and exits after them.
   After the day's 15:40 end a restart only recovers and stops.
3. If it will not start: exit 73 means another bot holds the lock (`pgrep -af "titan2 bot"`); exit 78 means config or
   DB (read the last log lines; DB → [P10](#p10-db-corruption)). Meanwhile, flatten the bot's positions yourself in
   the Kite app.
4. Last resort: the broker's MIS auto square-off at 15:26 [V].

**Worked when**: `titan2 status` shows a fresh heartbeat, recon CLEAN, and the same positions as Kite.

**Don't**: write a script to place orders, delete the lock file, or delete the DB. Don't start `titan2 bot` by hand:
outside systemd it refuses (exit 2), and `--outside-systemd` is for drills and laptops only. If systemd gave up after
its start limit, run `sudo systemctl reset-failed titan2-bot`, then step 2.

### P2. MISMATCH or EXTERNAL_CLOSE

**When**: CRITICAL `MISMATCH` or `HALT_MISMATCH`: the broker holds less than the bot expected (someone closed it
outside the bot), or a confirmed leftover after the bot's exit (a residual, never sold).

1. `titan2 status`: note the halt id, account and symbol.
2. Kite app → Orders and Positions for that symbol: who closed it, at what price, is anything left?
3. Anything left that the bot does not own → [P3](#p3-foreign-position-a-manual-trade). A bot leg still exiting →
   let it finish.
4. When Kite and `titan2 status` agree and recon is CLEAN: `titan2 ack-halt <id> --note "what happened"`. The ack is
   refused while recon is not CLEAN.

**Worked when**: `titan2 trades` shows the closed row (`CLOSED_EXTERNAL`, with pnl_status FINAL, ESTIMATED or
UNRESOLVED) and the halt is gone.

**Don't**: ack before you know who traded. On PAPER this should never happen: treat it as a bug (it also counts as a
G1 mismatch).

### P3. FOREIGN position (a manual trade)

**When**: `UNKNOWN_POSITION` / `HALT_UNKNOWN_POSITION`: the broker shows a position or open order the bot cannot
explain (a manual trade, another app, a leftover).

1. Either close it yourself in the Kite app; recon clears; then `titan2 ack-halt <id> --note "…"`.
2. Or keep it on purpose: `titan2 ack-foreign SYMBOL PRODUCT QTY` with the exact signed quantity (add
   `--account PAPER` or `--account SHADOW` for a simulated account; LIVE is the default). Entries resume; any change in
   that quantity halts again.

**Don't**: expect the bot to touch it. It never trades a position it does not own. Manual trading belongs in a
separate Zerodha account.

### P4. SHORT_DETECTED not explained by the bot

**When**: CRITICAL `SHORT_DETECTED`. The bot only buys back a short its own fills explain exactly (two snapshots
agree, bot sells − bot buys = the short); then it covers automatically. Anything else is left to you.

1. Kite app: find the short (symbol, quantity, which order created it).
2. If the bot does not explain it, buy it back in Kite after checking the quantity: a short option has unlimited risk.
3. When flat and recon is CLEAN: `titan2 ack-halt <id> --note "…"`.

**Don't**: wait for the bot to fix a short it cannot explain.

### P5. Token failure

**When**: `TOKEN_REFRESH_FAILED` (the 08:30 unit), CRITICAL `TOKEN` or `HALT_TOKEN` in the bot, self-check row
`token` failing, or `TOKEN_LOGIN_NEEDED` (manual mode).

1. Auto mode: `titan2 token`. If it fails again, check that the `ZERODHA_*` keys exist in `/etc/titan2/titan2.env`
   (look at key names only; never print values).
2. Manual mode: open the Kite login link from Telegram, log in, copy `request_token` from the redirect URL, then
   `titan2 token --request-token <rt>`.
3. Nothing else: the running bot re-reads the session file every 30 s and, once the new token works, restarts itself
   cleanly (exit 75).

**Worked when**: `titan2 status` shows no TOKEN halt and the log shows the controlled restart and READY.

**Note**: while the token is bad the broker refuses exits too. With positions open, act in the Kite app now.

**Don't**: log in to Kite Connect with the same API key from anywhere else; a new session can invalidate the bot's
token (v1's dashboard did exactly this).

### P6. Feed dead

**When**: `FEED_RECONNECT` WARNs, CRITICAL `FEED_DEAD`, or `HALT_STALE_FEED`.

What the bot already does: entries stop (gate G06); exits switch to REST quotes (stale quote → REST refresh →
protective exit at 5 s); if KiteTicker gives up reconnecting, the bot restarts itself after 60 s (exit 75).

1. Check the host's network and Kite's status. `journalctl -u titan2-bot -n 200 --no-pager` shows the reconnects.
2. If the outage lasts more than about 5 minutes with positions open, consider
   `titan2 flatten-all --confirm FLATTEN`.

**Don't**: restart the bot in a loop. Every restart is a full recovery.

### P7. Not flat at 15:22

**When**: CRITICAL `NOT_FLAT` (15:22 backstop or 15:25), `NO_BID` (CRITICAL from 15:20), `EXIT_DIFFICULTY`,
`EXIT_ATTEMPTS_EXHAUSTED` or `NOT_FLAT_STOP_SENDING`.

1. `titan2 status`: which legs, and why (no bids, price-band rejects, a stuck order).
2. Kite app → market depth of that contract. If there is a bid the bot is not hitting, sell it yourself in Kite, but
   only where the bot's exit ladder has stopped: after `EXIT_ATTEMPTS_EXHAUSTED` for that leg, or from 15:25. Before
   that, stop the bot first: `sudo systemctl stop titan2-bot` (a working exit order finishes, then it exits; nothing
   manages any position until it starts again). In Kite, check that no bot order is open, sell, wait until Positions
   shows the sale, then `sudo systemctl start titan2-bot`. The bot books your sale as an external close and raises
   MISMATCH; ack it afterwards ([P2](#p2-mismatch-or-external_close)).
3. From 15:25 the bot sends nothing. The broker's MIS square-off at 15:26 [V] closes what is left (₹50 + GST per
   order, booked in the trade's net).

**Worked when**: the 15:27/15:31 recons show the close (`BROKER_SQUAREOFF` if the broker did it) and the 15:33 truth
check passes.

**Don't**: sell by hand while the ladder still runs. Each retry first asks Zerodha for positions, with a read made after
its previous attempt ended, so it sells nothing once positions show your sale. But a retry already sent, or one whose
read went out before your sale reached Zerodha's positions (they can lag), sells the lot again: a naked short that the
bot cannot buy back (`SHORT_DETECTED` → [P4](#p4-short_detected-not-explained-by-the-bot)).

### P8. Bot misbehaving but positions need managing

**When**: the bot is doing something wrong (odd entries, a loop) but its exits must keep running.

```
sudo systemctl set-environment TITAN2_SAFE=1
sudo systemctl restart titan2-bot
```

This raises HALT SAFE_FLAG: no entries; exits, reconciliation and the EOD timeline continue. To undo:

```
sudo systemctl unset-environment TITAN2_SAFE
sudo systemctl restart titan2-bot
titan2 ack-halt <SAFE_FLAG id> --note "…"
```

The ack is refused while the process still runs in safe mode. Lighter alternative with no restart: `titan2 pause`.

### P9. Deploy failed

**When**: `ops/deploy.sh titan-bot …` (run on the **laptop**) exits non-zero.

- **Exit 2, refused**: nothing changed on the server (dirty tree, failing tests, open positions or working orders,
  market hours without `--allow-market-hours`, drift, disk, venv or deploy self-check). Fix the cause and deploy
  again.
- **Drift**: files on the server differ from their release manifest (someone edited the server).
  `ops/pull_drift.sh titan-bot` copies them to `./drift/<release>/` on the laptop and never overwrites anything.
  Compare with `git diff --no-index`, move real fixes into git, then deploy. Never deploy over drift.
- **Exit 1, rolled back**: the switch happened but READY did not arrive within 120 s, so deploy flipped `current`
  back, restarted and sent a CRITICAL by itself. Read `journalctl -u titan2-bot -n 200 --no-pager` for why. It can
  also mean titan2-bot opened a leg or a working order just before its restart: the bot was not touched,
  `current` was flipped back and a CRITICAL was sent; deploy again once flat. The market-hours rule is checked
  again just before the switch and just before the restart.
- **Roll back by hand** (same market-hours rule as deploy): **laptop** `ops/rollback.sh titan-bot [--restart]`, or
  any earlier release with `--to <release>`. Without the laptop, on the server:
  `bash /home/ubuntu/titan/current/ops/release.sh rollback`.

**Worked when**: `titan2 status` shows a heartbeat under 10 s and `readlink /home/ubuntu/titan/current` names the
release you expect.

### P10. DB corruption

**When**: the bot exits 78 with an integrity or migration error, or the self-check row `db` fails.

1. `sudo systemctl stop titan2-bot`. Manage any open positions in the Kite app meanwhile.
2. Move the damaged files aside (never delete them): `titan2.db` with its `-wal` and `-shm` files, and
   `paper_exchange.db`, in `/var/lib/titan2/db/`, keeping owner `titan`.
3. Restore the newest backup **set** from `/var/backups/titan2/<date>/`: `titan2.db.gz` and `paper_exchange.db.gz`
   together, from the same set, gunzipped (each copy was integrity-checked when taken). If the damage came right
   after a deploy, `/var/lib/titan2/backups/` also holds that deploy's copy (`deploy-<release>-<time>`) and the
   bot's own pre-migration copy (`premigrate-<ts>.db`).
4. `sudo systemctl start titan2-bot`. Recovery imports today's bot orders from the broker by tag and reconciles twice.

**Worked when**: `titan2 status` shows recon CLEAN. Read any `RECOVERED_ORDERS` or `RECOVERY_IMPORT` alert: those are
the orders the backup did not know.

**Note**: a paper trial restored from an older backup may record G1 mismatches. That is honest; leave it.

### P11. Morning self-check failed

Find the failing row in [§7](#7-morning-self-check) and follow its fix. `titan2 selfcheck --phase morning` runs it
again on demand. A failing row only adds an entry halt in the bot; exits are never affected.

### P12. Stop everything now

| Want | Command | Effect |
|---|---|---|
| No new entries, keep positions | `titan2 pause` | operator halt: no LIVE or PAPER entries (SHADOW keeps simulating); `titan2 resume` undoes it |
| Everything flat, then carry on later | `titan2 flatten-all --confirm FLATTEN` | every bot leg exits (verified flat); `titan2 resume` once flat |
| Everything flat, LIVE off | `titan2 kill --confirm KILL` | flatten + HALT KILL + every LIVE strategy demoted; `titan2 ack-halt <KILL id> --note "…"` once flat |

If the bot is down, these three wait in the command queue and run at its next start. Act in the Kite app now. The
dashboard can PAUSE, RESUME and FLATTEN_ALL (type `FLATTEN`), never KILL.

### P13. Change a setting

Settings only tighten. Bounds and defaults are the table in
[TITAN_INSTRUCTIONS §8](../TITAN_INSTRUCTIONS.md#8-risk-sizing-and-settings).

```
titan2 settings show
titan2 settings set MAX_ENTRIES_PER_DAY 1
titan2 settings diff
```

`titan2 settings unset KEY` returns a key to its default (and removes an unknown key left in the file). The change
takes effect at the next restart: restart after the close (`sudo systemctl restart titan2-bot`). A versioned key
gives every strategy a new version, so new G1 trials start. A looser value is refused: that needs a code change,
review, tests and a deploy. A file broken by hand makes the bot run on the defaults under HALT SETTINGS_INVALID until
a valid file and a restart.

### P14. LIVE unlock and rollback to PAPER

Preconditions: a CERTIFIED trial (`G1_CERTIFIED` on Telegram, with the hash; `titan2 g1 <strategy>`); the Elastic IP
registered in the Kite developer console (SEBI requires a static IP for API orders); margin ≥ ₹60,000; a clean
morning self-check; the [V] checklist done ([§10](#10-v-verification-checklist-and-results)).

1. **Key 1** (root, after the close, while flat): in `/etc/titan2/titan2.env` set `ALLOW_LIVE=1` and
   `REGISTERED_EGRESS_IP=<elastic ip>`, then `sudo systemctl restart titan2-bot`.
2. **Key 2**: `titan2 live <strategy> --confirm <hash> --i-accept-live-risk`. LIVE starts next session, with a
   PAPER twin of every trade.
3. Next session at 09:15:30 the self-test must pass (INFO `SELFTEST`). Two failures → LIVE_DISABLED_TODAY.
4. First 5 sessions: at most 1 LIVE entry per session. Check the first 10 LIVE trades by hand against Zerodha's
   contract notes.

**Back to PAPER**: `titan2 mode <strategy> PAPER` (from the next session) or `titan2 kill --confirm KILL` (now).
Remove key 1 by setting `ALLOW_LIVE=0` and restarting while flat. The bot also demotes by itself (CRITICAL
`LIVE_DEMOTED`) on probation loss, drawdown, the rolling-50 rule, the X2 falsifier or any LIVE mismatch; LIVE again
needs a new version and a fresh G1.

### P15. Calendar, hours and charges updates

- **Holidays** (NSE publishes next year's list in mid-December): add the year to `titan2/data/nse_sessions.json` with
  the circular numbers in `source`, run the tests, deploy. Do it before 1 December: the self-check row `calendar`
  warns when coverage is under 30 days, and dates beyond the file are never trading days. 2027 is not in the file yet.
- **Market hours** (as on 2026-08-03, when the F&O close moved to 15:40): add a dated `regular_hours` entry. Never put
  a time in code.
- **Charges** (Budget, NSE circular, Zerodha change, or a `CHARGES_DRIFT` alert): add a new dated RateSet to
  `titan2/charges.py` (never edit an old one), update `RATES_VERIFIED_ON`, pin it with a real contract note in
  `tests/unit/fixtures/contract_note.json`, run the tests, deploy. G1 re-prices every sample with the current rates.
  Review yearly even without a change: the self-check row `rates` warns after 180 days.
- **Lot sizes, strikes, expiries**: nothing to do; they come from the instruments dump every morning.
- **Zerodha's MIS square-off time**: set `BROKER_SQUAREOFF_AT` (allowed 15:20–15:39, any minute before the 15:40
  close). It must stay after `STOP_SENDING`, so if the broker's time moved earlier, first move `EOD_FLATTEN`,
  `EOD_BACKSTOP` and `STOP_SENDING` earlier (all tighten-only, so allowed). The EOD recons, the truth check and the
  bot's exit move with it (+1, +5, +7 … +14 minutes). A time outside 15:20–15:39 needs a code change to the bound
  and a review of the EOD timeline.

## 5. Alert codes

Telegram messages read `[titan2] <SEVERITY> <CODE>`. A CRITICAL repeats every 5 minutes until acknowledged; the
message ends with the exact command, `titan2 ack-alert <key>`. A WARN is sent at most once per 30 minutes per key.
Every halt also sends `HALT_<KIND>` (CRITICAL for the dangerous kinds): see [§6](#6-halts). Self-check rows send
`SELFCHECK_<ROW>`: see [§7](#7-morning-self-check). Acknowledging an alert only stops the repeats; it clears nothing.

**CRITICAL**

| Code | Meaning | Do |
|---|---|---|
| `BOT_CRASHED` | The bot process crashed; systemd restarts it (exit 3) and recovery resumes the exits | Watch for READY; if it repeats → [P1](#p1-bot-down-with-positions-open); report the traceback |
| `BOT_KILLED` | At this start, the previous run had died during the session without a clean exit (a watchdog SIGABRT, an out-of-memory kill, SIGKILL or a host failure; its BOOT event has no SHUTDOWN). The text says when it was last alive; this start's recovery resumes the exits | Watch for READY and check the open positions in `titan2 status`; read `journalctl -u titan2-bot` and the kernel log for an out-of-memory kill (`journalctl -k`); if it repeats → [P1](#p1-bot-down-with-positions-open) |
| `BOT_FATAL` | The bot refuses to start (exit 78: a module in the working directory that shadows an import while no structure is open, patched Kite, or a DB that fails migration or its integrity check); systemd does not restart it | [P1](#p1-bot-down-with-positions-open); a DB problem → [P10](#p10-db-corruption) |
| `BROKER_SQUAREOFF` | The broker's MIS square-off closed a bot position (₹50 + GST); the bot was not flat by 15:25. At a start it can also be a paper leftover that the paper exchange closed on an earlier day while titan2 was down, booked now from the paper exchange's own record of that close | Find why in the report (no bids? stuck order? the bot down at 15:26?). No halt |
| `CALENDAR` | The session calendar file is unusable, so titan2 treats every day as closed | Fix `titan2/data/nse_sessions.json` in git and deploy ([P15](#p15-calendar-hours-and-charges-updates)) |
| `COMMANDS_RESET` | A command DB was replaced or its ids went backwards; that source's commands are ignored | Use the CLI or the Kite app; investigate the file, never edit it |
| `COVER_REFUSED` | A planned short buy-back does not match its leg | [P4](#p4-short_detected-not-explained-by-the-bot) |
| `DISPATCH_FAILED` | A command could not be handed to its lane; the engine got a refusal instead | Read the log; report. If it repeats, `titan2 pause` and restart after checking positions |
| `EOD_TRUTH_MISMATCH` | The LIVE ledger differs from the broker tradebook by more than ₹1 | Compare `titan2 trades` with Kite's tradebook and contract note; charges changed → [P15](#p15-calendar-hours-and-charges-updates) |
| `EXIT_ATTEMPTS_EXHAUSTED` | A leg used all 1,295 order attempts its tags allow (retries had slowed to one every 20 s); the bot sends no more orders for it | [P7](#p7-not-flat-at-1522): close it in the Kite app; else the MIS square-off does |
| `EXIT_CAP_BEFORE_DECISION` | For 10 s no positions or orders read asked after an exit decision came back (Kite errors or hangs), so the exit was capped by the newest earlier read. A close made after that read (by hand, or by Zerodha's RMS) would be sold a second time: a naked short | Kite app → Positions for that symbol now: short → [P4](#p4-short_detected-not-explained-by-the-bot). Check Kite's API status; while reads fail, every exit waits 10 s |
| `EXIT_DIFFICULTY` | 10 exit attempts without getting flat; the book is halted for entries | [P7](#p7-not-flat-at-1522) |
| `EXIT_PARAMS_INVALID` | A new position's frozen exit levels failed a sanity check; the bot exits it at once | A bug: report it with the structure id |
| `FEED_DEAD` | KiteTicker gave up reconnecting; exits use REST; controlled restart after 60 s | [P6](#p6-feed-dead) |
| `FEED_START_FAILED` | The WS feed did not start; exits use REST quotes and entries are refused | [P6](#p6-feed-dead); restart after the cause is fixed |
| `FLATTEN_FAILED` | FLATTEN_ALL or KILL not verified flat after 60 s; the bot keeps trying | [P7](#p7-not-flat-at-1522) |
| `INVARIANT` | A safety invariant failed: entries halted, or that leg exits. Key `invariant:reload_storm`: more than 20 violations in 60 s (a broker report that keeps contradicting the DB); the bot keeps running and exiting the other legs. Key `invariant:unsafe_cwd`: a shadowing module in the working directory while structures may be open; the bot runs in SAFE mode (exits only) | Read the event in the log; report; ack after review. `unsafe_cwd`: remove the module once flat, then restart |
| `KILL` | KILL executed | Confirm flat in Kite; ack the KILL halt when ready |
| `LATE_FILL` | A bot BUY given up as NOT_PLACED (its place timed out and no tag lookup found it within 30 s) was later taken and filled by Kite: titan2 adopted it into a `RECOVERED:<account>` structure and sells it at once, or says it could not because another structure holds the underlying | Check Kite positions: the long must be gone; if the alert says it could not be adopted, close it in Kite; report |
| `LIVE_DEMOTED` | A LIVE strategy went back to PAPER (review rule, new version or KILL) | Read the reason; LIVE again needs a new version and a fresh G1 |
| `MISMATCH` | Broker and bot disagree: an external close or a residual. A contradiction (positions show less than the bot holds, but today's orders show no sale that explains it) books nothing: the leg stays managed and its exit is capped by positions. At a start: an earlier session's leftover closed UNRESOLVED (LIVE; or paper with no square-off record left in `paper_exchange.db`) | [P2](#p2-mismatch-or-external_close); for a contradiction compare Kite positions with the order book and ack once recon is CLEAN |
| `NO_BID` | From 15:20: still exiting into an empty bid book | [P7](#p7-not-flat-at-1522) |
| `NOT_FLAT` | Positions still open at the 15:22 backstop or at 15:25 | [P7](#p7-not-flat-at-1522) |
| `NOT_FLAT_STOP_SENDING` | A leg (or an uncovered short) still open at 15:25; the broker square-off is expected | [P7](#p7-not-flat-at-1522) |
| `ORDER_CONFLICT` | A broker report contradicts a finished order; that trade's P&L may be UNRESOLVED | Compare with the Kite order history; report |
| `ORDER_STUCK` | An order did not resolve 10 s after its cancel; the book is halted | Kite order book: cancel it there if still open; then ack |
| `PAPER_MODEL_OPTIMISTIC` | X2 falsifier: live fills are worse than paper predicted; every LIVE strategy is demoted | Recalibrate the `PAPER_*` settings (a code review; every version changes) |
| `PLUGIN_FAILED` | The phase-2 plugin failed to load; the bot runs the phase-1 handlers only | Report it; phase-2 intents are refused meanwhile |
| `RECOVERED_ORDERS` | A restart found bot orders missing from the DB and imported them | Expected after [P10](#p10-db-corruption); otherwise report |
| `RECOVERY_ABORTED` | Recovery did not finish in 30 s; exits run on broker caps | Check the log and Kite; restart once broker reads work |
| `RECOVERY_IMPORT` | Bot orders that cannot be rebuilt (no contract row, or no RECOVERED book can take the underlying because another structure holds it, I5) | Settle them in Kite; ack |
| `SELFTEST_FILLED` | The LIVE self-test filled (it is priced not to); the bot exits it at once | Check Kite; report |
| `SHORT_DETECTED` | A net short on NFO | [P4](#p4-short_detected-not-explained-by-the-bot) |
| `SIGNALS_START_FAILED` | The signals thread did not start: no strategy runs today (exits are unaffected) | Read the log; report; restart after the close |
| `STALE_STRUCTURE` | An order of an earlier session was still open at startup and the broker no longer reports it: the bot recorded it CANCELLED (it had reached the broker) or NOT_PLACED, with P&L UNRESOLVED. Key `squareoffs:...`: the paper exchange's record of an earlier day's square-off could not be read three times, so a paper leftover it closed then stays unbooked | Check that order's outcome in the Kite order history; settle any position left in Kite. `squareoffs:...`: read the log, check `paper_exchange.db`, restart |
| `STRATEGY_DISABLED` | A strategy hook raised; that strategy is off for the rest of the session (its open positions are still managed) | Report the error; it runs again next session |
| `STRATEGY_REFUSED` | A strategy failed its registry checks (bad class, or an exit profile outside the bounds) and is disabled | Report it; the fix is a code change with a version bump |
| `TOKEN` | A token or permission error on an order or a broker read: exits are impossible until fixed | [P5](#p5-token-failure), now |
| `TOKEN_REFRESH_FAILED` | The 08:30 token unit failed | [P5](#p5-token-failure) |
| `UNIT_FAILED` | A titan2 systemd unit failed (OnFailure) | The bot → [P1](#p1-bot-down-with-positions-open); another unit → `journalctl -u <unit>` |
| `UNKNOWN_POSITION` | A position or order the bot does not own | [P3](#p3-foreign-position-a-manual-trade) |
| `VERIFY_FLAT_STUCK` | A leg not confirmed flat 30 s after its last exit fill | Check Kite positions; if they differ → [P2](#p2-mismatch-or-external_close) |

**Severity decided at run time**

| Code | Meaning | Do |
|---|---|---|
| `DAILY_STOP` | CRITICAL on LIVE, WARN on paper: the day's loss reached the daily stop; the book halts until the next session and its open legs exit | Nothing now; review in the report |
| `SELFCHECK_MORNING` | The morning self-check summary (WARN or CRITICAL when rows fail) | [§7](#7-morning-self-check) |

**WARN**

| Code | Meaning | Do |
|---|---|---|
| `BAN_LIST` | The F&O ban list is unavailable: no stock is eligible today | Check the NSE file; a manual override goes in `/var/lib/titan2/config/ban_override/<date>.csv` |
| `CHARGES_CHECK` | The LIVE virtual contract note was unavailable today. (For the PAPER sample the same failure is only an event, `CHARGES_CHECK` with `VCN_PAPER_UNSUPPORTED`: Kite refused the synthetic ids, so the rates were not checked) | Nothing; it retries tomorrow. The RateSet stays [V] until V1 and V5 ([§10](#10-v-verification-checklist-and-results)) |
| `CHARGES_DRIFT` | Zerodha's contract note differs from titan2's charges by more than ₹1 per order | [P15](#p15-calendar-hours-and-charges-updates) |
| `CLOCK_JUMP` | The wall clock jumped more than 2 s | Check time sync (`timedatectl`) |
| `COMMANDS_UNREADABLE` | A command DB could not be read | Check permissions on `/var/lib/titan2/dash/` and `/var/lib/titan2/db/commands_cli.db` |
| `COMPLETE_PARTIAL` | The broker reported COMPLETE with filled ≠ qty; titan2 trusts the filled qty | Nothing; noted |
| `EXIT_BAND_REJECTS` | Three exchange price-band rejects in a row on an exit | Watch; near the close → [P7](#p7-not-flat-at-1522) |
| `EXIT_STUCK` | 5 exit attempts or 15 s without getting flat | Watch; → [P7](#p7-not-flat-at-1522) |
| `EXIT_UNCAPPED` | No broker view for 10 s; the exit used the bot's own qty | Check Kite positions |
| `FEED_RECONNECT` | The WS feed reconnected | Nothing unless repeated → [P6](#p6-feed-dead) |
| `FILL_PX_FALLBACK` | A derived fill price looked wrong; the broker's average was used | Nothing; noted |
| `G1_FAILED` · `G1_FUTILE` | A G1 trial ended without an edge; the strategy moved to SHADOW | The expected outcome; only a new version starts a new trial |
| `GATE_ERROR` | A gate raised; that entry was refused | Report if repeated (5 in 5 minutes → HALT GATE_ERRORS) |
| `INTENT_INVALID` | A strategy emitted a malformed intent | A strategy bug: report |
| `LIMIT_BOUNDED` | An order's limit was clamped to the allowed band | Nothing; noted |
| `MARGIN_REJECT` | An order was rejected for margin or RMS | Check funds in Kite |
| `NO_BID` | Exiting into an empty bid book for 10 s (CRITICAL from 15:20) | Watch; → [P7](#p7-not-flat-at-1522) |
| `ORDER_REJECTED` | An order was rejected | Entry: a cooldown starts (3 per book → HALT REJECTS). Exit: retried by the ladder |
| `PUB_WRITE` | Writing a dashboard file failed; the dashboard may be stale | Check disk space and permissions |
| `READ_LOST` | A broker read went unanswered for 15 s; asked again | Nothing unless repeated |
| `RESERVE_REFUSED` | The DB refused to reserve a position slot | Report |
| `SELFCHECK` | The prestart self-check did not run | Check the log; entries may stay halted |
| `SELFTEST` | A LIVE self-test attempt failed: not CANCELLED unfilled within 5 s, or its tag did not come back in a WS order update and in `orders()` within 5 s more (retry at 09:17; two failures → LIVE_DISABLED_TODAY) | Check the Kite order history |
| `STALE_RECON` | Broker reads are failing (3 in a row → HALT STALE_RECON) | Check Kite API status |
| `TOKEN` | A new session token works: controlled restart (exit 75) | Nothing |
| `TOKEN_LOGIN_NEEDED` | Manual token mode: a login is needed | [P5](#p5-token-failure) |

**INFO** (no action): `POSITION_OPENED`, `POSITION_CLOSED`, `DAILY_REPORT`, `G1_REPORT`, `G1_CERTIFIED`, `SELFTEST`
(passed), `ACK_ALERT`, `NO_SESSION` (a weekday without an NSE session: the bot only keeps the dead-man check fed).

## 6. Halts

A halt blocks new entries in its scope until it clears. It never blocks an exit. `titan2 status` lists active halts
with their ids. NEXT_SESSION halts clear at the first timer of the next trading session; AUTO halts clear when the
condition is gone; ACK halts need `titan2 ack-halt <id> --note "…"`; RESUME halts need `titan2 resume`.

| Halt | Scope | Raised when | Clears | Before clearing |
|---|---|---|---|---|
| `PAUSE` | GLOBAL | `titan2 pause` or the dashboard | RESUME | — |
| `FLATTEN` | GLOBAL | FLATTEN_ALL | RESUME, once verified flat | Check Kite is flat |
| `KILL` | GLOBAL | `titan2 kill --confirm KILL` | ACK, once verified flat | Check Kite is flat; LIVE strategies stay demoted |
| `SAFE_FLAG` | GLOBAL | `bot --safe` or `TITAN2_SAFE=1` | ACK, after leaving safe mode | [P8](#p8-bot-misbehaving-but-positions-need-managing) |
| `DAILY_STOP` | BOOK, ACCOUNT:LIVE | realized + MTM at the bid reached the daily stop, or the realized loss alone did while a leg's MTM was unknown | NEXT_SESSION | — |
| `LOSS_STREAK` | BOOK | `STOP_AFTER_FIRST_LOSS` is on and the book lost today | NEXT_SESSION | — |
| `CONTRACT_BLOCKED` | SYMBOL | the broker rejected an entry as banned or blocked | NEXT_SESSION | — |
| `LIVE_DISABLED_TODAY` | ACCOUNT:LIVE | the self-test failed twice, or the egress IP is not the registered one | NEXT_SESSION | Check the IP and the Kite order history |
| `MISMATCH` | ACCOUNT | an external close or a residual after an exit | ACK, only while recon is CLEAN | [P2](#p2-mismatch-or-external_close) |
| `UNKNOWN_POSITION` | ACCOUNT | a foreign or unexplained position or order | ACK when clean, or ACK_FOREIGN with the exact qty | [P3](#p3-foreign-position-a-manual-trade) |
| `SHORT_DETECTED` | GLOBAL | a net short on NFO | ACK, only while recon is CLEAN | [P4](#p4-short_detected-not-explained-by-the-bot) |
| `INVARIANT` | GLOBAL, ACCOUNT | a runtime invariant failed | ACK | Read the INVARIANT_VIOLATION event; report |
| `EXCEPTION` | GLOBAL | the engine raised; state was rolled back and reloaded | ACK | Read the traceback in the log; report |
| `GATE_ERRORS` | GLOBAL | 5 gate errors within 5 minutes | ACK | Report the gate and the error |
| `ORDER_STUCK` | BOOK | an order unresolved 10 s after its cancel | ACK | Kite order book: nothing left working |
| `REJECTS` | BOOK | 3 entry rejects in one session | ACK | Read the reject messages |
| `RUNAWAY` | BOOK | entry orders per minute at the cap | ACK | Find what fired so often. A batch can also do it: when the first IOCs of a batch of 3+ signals fill nothing, its waiting signals take the freed slots and their orders count too, so a signal arriving within that minute raises it (a woken signal is refused RUNAWAY without the halt; the woken signals' INTENT_ROUTED carry waited_s). The minute never gets a fourth entry order: a reserved signal's order counts before it is sent, and a signal refused `ENTRY_ORDERS: … reserved/3` waits like one refused POSITIONS, with no halt |
| `EXIT_DIFFICULTY` | BOOK | 10 unfilled exit attempts on a leg | AUTO, when that leg is flat | [P7](#p7-not-flat-at-1522) |
| `TOKEN` | GLOBAL | a token or permission error | AUTO, when `profile()` works | [P5](#p5-token-failure) |
| `STALE_FEED` | GLOBAL | the feed is down, stale or dropping ticks | AUTO, after 60 s healthy | [P6](#p6-feed-dead) |
| `STALE_RECON` | ACCOUNT | 3 broker reads failed in a row | AUTO, after 2 good reads | Check Kite API status |
| `SETTINGS_INVALID` | GLOBAL | `settings.json` is invalid; running on the defaults | AUTO, at the next start with a valid file | [P13](#p13-change-a-setting) |
| `CLOCK_UNSYNCED` | GLOBAL | NTP is not synchronised | AUTO | `timedatectl`; check chrony |
| `NO_INSTRUMENTS` | GLOBAL | today's instruments dump failed its sanity checks | AUTO | Check the log; Kite API status |
| `V1_ACTIVE` | GLOBAL | a v1 unit is not masked, or a crontab line mentions `agentic_trader` | AUTO | Stage 0, [§8](#8-cutover-stages) |

## 7. Morning self-check

`titan2-selfcheck` runs at 08:40 and sends one Telegram summary; `titan2 selfcheck --phase morning` reruns it. The
same rows run inside the bot at startup (prestart), where a failure adds an entry halt. The report is also saved as
`/var/lib/titan2/reports/selfcheck-*.json`.

| Row | Checks | If it fails | Fix |
|---|---|---|---|
| `token` | the Kite session works and is the right user | HALT TOKEN | [P5](#p5-token-failure) |
| `ntp` | the clock is NTP-synchronised | HALT CLOCK_UNSYNCED | `timedatectl`; restart chrony |
| `calendar` | `nse_sessions.json` covers today plus 30 days | WARN; dates beyond it do not trade | [P15](#p15-calendar-hours-and-charges-updates) |
| `instruments` | today's instruments dump is sane | HALT NO_INSTRUMENTS | Kite API status; the bot retries prep every 60 s |
| `ban_list` | today's F&O ban list loaded | stocks ineligible today | NSE file reachable? Override in `config/ban_override/<date>.csv` |
| `prev_bars` | previous-session bars for ≥ 90% of the universe | WARN; missing stocks ineligible | Kite historical API status |
| `settings` | `settings.json` within its limits | HALT SETTINGS_INVALID | [P13](#p13-change-a-setting) |
| `db` | DB integrity and schema version | exit 78 | [P10](#p10-db-corruption) |
| `resources` | ≥ 2 GB disk and ≥ 300 MB memory free | WARN | Clean `/var/log`, old exports; check memory |
| `v1_disabled` | v1 units masked and inactive; no v1 crontab line | HALT V1_ACTIVE + CRITICAL | Stage 0, [§8](#8-cutover-stages) |
| `code_clean` | no module or package in the working directory whose name shadows an import (the stdlib, the venv, `titan2`, `sitecustomize`); Kite unpatched. Also runs in the morning phase | exit 78 when no structure is open; otherwise SAFE mode (exits only) and a CRITICAL `INVARIANT` | Remove the shadowing file from `/home/ubuntu/titan/agentic_trader/` (the `_*.py` research scripts are fine) |
| `env_files` | `dash.env` has no `ZERODHA_*` key; env file permissions | CRITICAL | Fix owner and mode ([TITAN_INSTRUCTIONS §12](../TITAN_INSTRUCTIONS.md#12-server-layout-data-and-files)); move the key out |
| `broker_flat` | no foreign NFO positions and no open orders at the broker | HALT UNKNOWN_POSITION | [P3](#p3-foreign-position-a-manual-trade) |
| `egress_ip` | the egress IP is `REGISTERED_EGRESS_IP` (LIVE) | LIVE_DISABLED_TODAY | Re-associate the Elastic IP |
| `live_ready` | LIVE strategies hold CERTIFIED trials; margin ≥ ₹60,000 | LIVE_DISABLED_TODAY or demotion | Add funds, or run PAPER |
| `rates` | charges confirmed within 180 days | WARN | [P15](#p15-calendar-hours-and-charges-updates) |
| `prev_session` | the previous session ended flat with its EOD timeline done | CRITICAL | Check yesterday's report and Kite; ack what recovery raises |
| `notify` | Telegram test message and healthchecks ping | WARN | Check `TELEGRAM_*` and `HEALTHCHECK_URL` in the env |

On the Windows laptop, `titan2 laptop check` and the bot run the phase `laptop` instead, with its own rows and
`SELFCHECK_<ROW>` alerts: `python`, `env_file`, `token`, `clock`, `network`, `calendar`, `instruments`, `disk`,
`db`, `settings`, `power`, `live_disabled`. Their effects are in
[TITAN_INSTRUCTIONS §14](../TITAN_INSTRUCTIONS.md#laptop-mode-windows-paper-only), the fixes in
[LAPTOP.md §12](LAPTOP.md#12-if-something-goes-wrong).

## 8. Cutover stages

Each stage is reversible. Do them outside market hours.

**Stage 0: secure and freeze v1** (no v2 yet)

1. **Laptop, before anything is pushed to `main`**: `gh workflow disable "Deploy Titan Bot"`; delete the
   `EC2_SSH_KEY` repository secret; merge the commit that deletes `.github/workflows/deploy.yml` and adds
   `.github/workflows/ci.yml`. (v1's `deploy.yml` restarted `titan-bot` on every push to `main` that touched
   `agentic_trader/**`.)
2. **Server, stop v1**: disable, stop and **mask** `titan-bot` and `titan-dashboard` (masking matters: v1's token job
   restarts a merely stopped bot); back up and clear the ubuntu crontab; remove the nginx titan site. Then remove the
   security-group rules for ports 80 and 5000 in the AWS console.
3. **Archive v1**: online-backup `titan_state.db` to `~/titan-v1-state.db`; `chmod 600` the v1 `.env`; move
   `/home/ubuntu/titan/agentic_trader` to `/home/ubuntu/titan/v1_frozen_<date>` (read-only), with a tarball that holds
   no secrets or databases. Then make the rest of v1's files under `/home/ubuntu` owner-only (the frozen tree,
   `~/titan/logs`, `~/backups`, stray token files, `/tmp/.env.backup`) before Stage 1 opens `/home/ubuntu` to 0751.

   `ops/stage0_cutover.sh` does steps 2–3. **Laptop**: `ops/stage0_cutover.sh titan-bot` is a dry run that prints
   every action; `ops/stage0_cutover.sh titan-bot --execute` performs them and copies the v1 DB, the tarball and the
   crontab backup to `./v1_archive/titan-bot-<date>/`. It refuses `--execute` on a weekday between 08:30 and 16:00
   IST, keeps every backup, and each action is reversible (`systemctl unmask`, `crontab FILE`, `mv`).
4. **Rotate secrets**: `.kite_access_token` and `dhan_config.json` were committed and pushed. Regenerate the Kite API
   secret, change the Zerodha password, re-enrol TOTP, revoke the Dhan token. Purging git history is optional and
   needs a force-push (your call); the file cleanup is deferred.
5. **Static IP**: allocate and associate an Elastic IP, update `titan-bot` in `~/.ssh/config`, and register the IP in
   the Kite developer console [V]. LIVE is impossible without it.

**Stage 1: install v2** (no trading)

1. `ops/bootstrap.sh` (idempotent; a dry run that prints a diff of every change unless given `--execute`): users
   `titan` and `titandash`, the directories and permissions, Python ≥ 3.10, chrony, logrotate, the journald cap, the
   CLI wrapper and its sudoers allow-list, the timezone, `chmod 0751 /home/ubuntu`, and the `agentic_trader/titan2`
   symlink (refused while the v1 tree is still there: Stage 0 first). No release exists yet, so run it from a copy:

   ```
   scp -r agentic_trader/ops titan-bot:/tmp/titan2-ops
   ssh titan-bot 'sudo bash /tmp/titan2-ops/bootstrap.sh'
   ssh titan-bot 'sudo bash /tmp/titan2-ops/bootstrap.sh --execute'
   ```
2. Write `/etc/titan2/titan2.env` (from `ops/titan2.env.example`, `ALLOW_LIVE=0`) and `/etc/titan2/dash.env` (from
   `ops/dash.env.example`) by hand; each example starts with the exact `sudo install …` line that sets its owner and
   mode. Generate the dashboard values with `python -m titan2.dash.app hash-password` and
   `python -m titan2.dash.app new-secret`: on the **laptop** from `agentic_trader/` before the first deploy (the
   dashboard refuses to start without them), or later on the server with the release's venv as the example shows.
3. **Laptop**: `ops/deploy.sh titan-bot` (the first release).
4. `titan2 token`.
5. `titan2 selfcheck --phase cutover`: the broker must be flat with no open orders, the token valid, the dump sane.
6. Enable the units and timers (deploy installs them; nothing enables them for you):

   ```
   sudo systemctl enable titan2-bot.service
   sudo systemctl enable --now titan2-bot.timer titan2-token.timer titan2-selfcheck.timer titan2-backup.timer titan2-dash.service
   ```
7. healthchecks.io: a check with schedule `45-59 8 * * 1-5`, `* 9-14 * * 1-5` and `0-33 15 * * 1-5`
   (`titan2.alerts.DEADMAN_SCHEDULE`; the bot's EOD exit comes at 15:34 at the earliest), time zone Asia/Kolkata,
   grace 3 minutes; its URL goes in `HEALTHCHECK_URL`. A check still on the older `* 9-15` goes DOWN every day:
   reconfigure it.

**Stage 2: PAPER burn-in** (at least 10 sessions). Exit criteria: zero CRITICAL alerts caused by software faults; zero
paper reconciliation mismatches; exit decision → order dispatch p95 under 1.5 s, read from the daily report's latency
row `exit_decision_to_submit_ms` (each exit's own figure is the `decision_to_order_ms` field of its first
`EXIT_ATTEMPT` event; it includes the post-decision positions read of decision B1, [§10](#decisions-2026-10-11));
daily reports produced; the drills in [§9](#9-drills) passed; the [V] checklist in
[§10](#10-v-verification-checklist-and-results) done. Any fix bumps versions, so G1 restarts: that is intended.

**Stage 3: G1 accumulation**: at least 34 sessions at ≤ 3 entries per day per book (G1's first look needs 100
trades), so 2–5 months. Weekly review. No parameter change without a version bump.

**Stage 4: LIVE at 1 lot**: only a CERTIFIED strategy, through [P14](#p14-live-unlock-and-rollback-to-paper).

**Stage 5: multi-leg**: only if the research says it is viable, with its own paper period and its own G1.

## 9. Drills

Run each during Stage 2 and record the result here (date, what happened, follow-ups).

| Drill | How | Passes when | Result |
|---|---|---|---|
| D1. Crash mid-session | With paper positions open: `sudo systemctl kill -s KILL titan2-bot` | systemd restarts it; a CRITICAL `BOT_KILLED` arrives naming when it was last alive; READY in under 30 s; exits resume; no duplicate entry; recon CLEAN | PENDING |
| D2. Deploy and roll back | After the close: **laptop** `ops/deploy.sh titan-bot --restart`, then `ops/rollback.sh titan-bot --restart` | READY after each; `current` flips both ways; state untouched | PENDING |
| D3. Token failure | Before the session: move `/var/lib/titan2/session/kite_session.json` aside (as `titan`), start the bot, then `titan2 token` | HALT TOKEN + CRITICAL while missing; controlled restart (75) and READY after the new token | PENDING |
| D4. Flatten from the dashboard | With paper positions open: FLATTEN_ALL (type `FLATTEN`) | ack DONE; verified flat; `titan2 resume` clears FLATTEN | PENDING |
| D5. Dead-man | In the ping window: `sudo systemctl stop titan2-bot` | healthchecks.io alerts within about 4 minutes | PENDING |
| D6. Restore a backup | `titan2 backup --to /tmp/drill`, then integrity-check the copies | both DBs restore and pass `integrity_check` | PENDING |

## 10. [V] verification checklist and results

[V] marks a fact titan2 assumes but that only the live broker or exchange can confirm. Status: **PENDING** (not
checked), **PARTIAL** (checked offline only: library source, published rules, the research cost model), **VERIFIED**
(confirmed, with evidence), **FAILED** (contradicted: fix code or settings, bump versions). To record a result, edit the
row with the date and the evidence (contract note number, log line, document link). A result that changes a semantic
constant needs an `ENGINE_SEMANTICS_VERSION` bump.

| # | Item | How to check | Status | Result and evidence |
|---|---|---|---|---|
| V1 | Charges (STT, transaction, IPFT) match a real contract note | Put one real F&O contract note dated on or after 2026-04-01 into `tests/unit/fixtures/contract_note.json` (now a placeholder) and run `tests/unit/test_charges.py` | PARTIAL | 2026-10-11: RateSet `2026-04-01` cross-checked against the research cost model (`leg_charges`, zerodha.com/charges as of 2026-10-10): 7 sample orders (NIFTY, BANKNIFTY and stock lots) agree within ₹0.02 per order, the gap being 2-dp rounding. Contract-note pin pending (question Q5) |
| V2 | Zerodha's MIS auto square-off time for NFO, and how those orders appear in `orders()` (`tag`, `placed_by`) | Zerodha's published square-off schedule since the 2026-08-03 close change; then watch one square-off in burn-in (paper simulates, it cannot prove) | PENDING | The F&O close moved to 15:40 on 2026-08-03; the square-off time since then is unverified. titan2 keeps `BROKER_SQUAREOFF_AT` 15:26 with its own flatten at 15:15 and stop-sending at 15:25. A real time outside 15:20–15:39 needs a code change ([P15](#p15-calendar-hours-and-charges-updates)) |
| V3 | NFO accepts IOC validity on option LIMIT orders | The first LIVE self-test (an IOC that is CANCELLED with 0 filled) | PARTIAL | 2026-10-11: `kiteconnect` 5.0.1 defines `VALIDITY_IOC = "IOC"` and `place_order(..., validity=...)`. Exchange acceptance pending |
| V4 | `tag` comes back in WS order updates and in `orders()` | The LIVE self-test; `titan2 status` shows the order matched by tag | PARTIAL | 2026-10-11: 5.0.1 `place_order` accepts `tag`, and `on_order_update` passes the broker's dict through unchanged. Echo pending |
| V5 | The virtual contract note prices synthetic (paper) order ids | The 15:33 check: a PAPER `CHARGES_CHECK` event with `orders` > 0 and no drift. `result: VCN_PAPER_UNSUPPORTED` means Kite refused the synthetic ids, so V5 is not verified | PARTIAL | 2026-10-11: 5.0.1 has `get_virtual_contract_note(params)` (POST `/charges/orders`). Synthetic ids pending; if refused, the bot logs it once and skips paper |
| V6 | `positions()` lag after fills, p95 | Needs real fills (the self-test never fills): measure on the first LIVE trades or one manual 1-lot test trade in a separate session | PENDING | titan2 waits 2 s after the last fill and needs two agreeing snapshots 3 s apart. Changing the 2 s needs an `ENGINE_SEMANTICS_VERSION` bump |
| V7 | Static IP whitelisting and the egress-IP check | Register the Elastic IP in the Kite developer console; the `egress_ip` self-check row | PARTIAL | 2026-10-10: SEBI's retail-algo rules require a registered static IP for API orders. The `egress_ip` self-check disables LIVE for the day when `ALLOW_LIVE=1` comes without `REGISTERED_EGRESS_IP` or the egress IP differs. Elastic IP not allocated yet (Stage 0 step 5) |
| V8 | NSE ban-list URL and format; NSE holiday calendars | The `ban_list` self-check row daily; NSE holiday circulars | PARTIAL | 2026-10-10: 2025 and 2026 holidays checked against NSE/FAOP/65588, 71777, 72262 and 72352, and the 15:40 close recorded in `titan2/data/nse_sessions.json`. 2027 list pending (December 2026). Ban-list URL `https://nsearchives.nseindia.com/content/fo/fo_secban.csv` unconfirmed |
| V9 | Zerodha's physical-settlement policy for stock options | Zerodha support article on expiry-week stock options | PENDING | titan2 never holds into expiry: stock options need ≥ 5 trading sessions to expiry (`MIN_TD_TO_EXPIRY_STOCK`) and roll from E−4 |
| V10 | Kite's 09:15 candle includes pre-open volume | The 15:36 bar check (`bar_checks` rows, the daily report) | PENDING | `opening_drive` assumes it; G1 tolerates at most 10% bar mismatches |
| V11 | Which `margins()` field is available cash | One live `margins()` response | PARTIAL | 2026-10-11: titan2 uses the smaller of `equity.net` and `equity.available.live_balance` (conservative) |
| V12 | EC2 Python ≥ 3.10; Ubuntu home-directory permissions as assumed | `ops/bootstrap.sh` output; `titan2 selfcheck --phase cutover` | PENDING | — |
| V13 | `basket_order_margins` usable read-only (phase 2 only) | One call with a 2-leg basket | PARTIAL | 2026-10-11: 5.0.1 has `basket_order_margins(params, consider_positions=True, mode=None)`. Not used in phase 1 |
| L1 | NSE F&O hours 09:15–15:40 since 2026-08-03 | NSE circular | VERIFIED | 2026-10-10 (NSE, SEBI, Zerodha sources); `regular_hours` in `titan2/data/nse_sessions.json`. Derivatives close = VWAP 15:10–15:40 |
| L2 | Lot sizes: NIFTY 65, BANKNIFTY 30, revised twice a year | NSE lot-size circulars, `fo_mktlots.csv` | VERIFIED | 2026-10-10. titan2 reads every lot from today's instruments dump; nothing hardcoded |
| L3 | Expiries: NIFTY weekly Tuesday; monthly and BANKNIFTY (monthly only) last Tuesday; holiday → previous trading day; no NIFTY weekly in a monthly week | NSE circulars | VERIFIED | 2026-10-10. titan2 takes expiries from the dump, never from weekday arithmetic |
| L4 | Charges in force since 2026-04-01 (STT 0.15% on sell and exercise) | zerodha.com/charges; Budget 2026-27 | VERIFIED | 2026-10-10; RateSet `2026-04-01`. Contract-note pin is V1 |
| L5 | SEBI retail-algo rules: at most 10 orders per second; static IP for LIVE | SEBI circular; Zerodha notice | VERIFIED | 2026-10-10. titan2's order bucket is 8/s (burst 3, never more than 8 in any second); static IP → V7 |
| L6 | Options have no pre-open; opening option quotes are stale | NSE market structure | VERIFIED | 2026-10-10. No entry before 09:20 (`ENTRY_START` bound); the paper exchange never fills against a quote stamped before 09:15:30 |
| L7 | Freeze quantity: NIFTY 1,800, BANKNIFTY 600 units per order (from 2026-09-01) | NSE circular | VERIFIED | 2026-10-10 (research cost model sources). Irrelevant at 1 lot; phase 2 must split |

### Decisions (2026-10-11)

- **B1: an exit is capped only by a broker read made after the exit decision.** `settings.EXIT_CAP_PRIOR_READ_S` = 0
  (a `SEMANTIC_CONSTANTS` entry, so changing it gives every strategy a new version): before every exit order titan2
  asks Zerodha (`positions()`) how much it holds, and only an answer to a request made at or after the decision may
  cap the sale. Each retry counts as a new decision: its read must be asked after the previous attempt ended. A
  manual close (or an RMS square-off) a moment before the decision or between two attempts is then never sold a
  second time as a naked short (guarantee G3), once Zerodha's positions show it. The cost is one positions round
  trip per exit order, on LIVE and, through the seeded paper read delay (`PAPER_READ_LATENCY_MEDIAN_MS` 250 ms,
  `PAPER_READ_LATENCY_SIGMA` 0.85: p95 about 1 s), on paper too, so paper exit timing matches LIVE. Measure it in
  the burn-in: the daily report's `exit_decision_to_submit_ms` row (p95 under 1.5 s, the Stage 2 criterion) and each
  first exit's `EXIT_ATTEMPT.decision_to_order_ms`. A positions API that fails or hangs makes exits wait: an older
  read caps nothing until 10 s have passed without an answer made after the decision; only then does the newest
  earlier read cap the exit (CRITICAL `EXIT_CAP_BEFORE_DECISION`). What is left: a close made after that older read,
  during a broker-read outage, is sold again; so is a hand sale that Zerodha's positions do not show yet, or one made
  while a retry is already on its way ([P7](#p7-not-flat-at-1522) says how to sell by hand safely). Never shorten the
  rule to work around a slow API.
- **B2: after an ambiguous exit order titan2 waits 30 s before another SELL.** `orders.LATE_ACCEPT_S` = 30 s, kept for
  paper and the first LIVE weeks. A shorter window lets a second SELL go out while the first may still be accepted:
  a double sell, i.e. an uncapped naked short. Waiting costs at most a 1-lot long's premium (up to about ₹49.85K since
  2026-10-13), and the day's worst case is capped at ₹50K (₹40K before 2026-10-13). Paper never times out, so the burn-in cannot measure it. How to measure it on LIVE: every ambiguous place is
  an `ORDER_PLACE_RESULT` WARN event with outcome `AMBIGUOUS`; the gap to that order's first adopting broker report
  (an `ORDER_UPDATE` from LOOKUP, POLL or WS) is how late `orders()` listed it. **Do not shorten it without that
  evidence:** only if `orders()` proves to list new orders within a few seconds, shorten it (to about 15 s) as a
  reviewed code change, never a setting.
