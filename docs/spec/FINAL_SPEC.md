# Titan v2 — Final Buildable Spec (normative)
Base and grafts. The base is the ops design (judges' winner, 2/3). Grafts from correctness: functional core, write-ahead, DB-enforced invariants, order state machine, band-safe exits, lag-safe recon. Grafts from minimal: derived counters, settings read once, independent paper ledger, `callFromThread`, live twin, self-test, version hash.

Conventions. Line refs point to snapshot `b4694f3` (`agentic_trader/`). [V] = verify during burn-in (§17).

Companion document. `FINAL_SPEC_DETAIL.md` (in this folder) uses the same numbering and adds rationale, examples and full tables. This file wins any conflict, except where it explicitly says "DETAIL is normative".

## Errata: decisions made after the spec (2026-10-11); these override the text below
- **Market facts:** the F&O close is 15:40 since 2026-08-03 (closing auction). The NIFTY lot is 65 and lots are read from the instruments dump. Weekly expiries are on Tuesday. Option STT has been 0.15% since 2026-04-01 (`charges.py`). `BROKER_SQUAREOFF_AT` may be set anywhere from 15:20 to 15:39.
- **B1, strict exit cap:** an exit may be capped only by a positions read requested at or after the exit decision (`recon.EXIT_CAP_PRIOR_READ_S = 0`), and each retry needs a fresh read. Paper and shadow broker reads pay a seeded delay (`PAPER_READ_LATENCY_MEDIAN_MS` 250, `PAPER_READ_LATENCY_SIGMA` 0.85). Every exit logs `decision_to_order_ms`.
- **B2:** `orders.LATE_ACCEPT_S` stays at 30 s. Shortening it brings back the double-sell risk, so it is a reviewed code change only.
- **Placebo timing (§10, replaces "opens when the entry fills"):** the placebo opens in its parent's own transaction, priced from the same decision snapshot.
- **Shadow capital:** each SHADOW-account book has its own paper capital (paper DB schema v2).
- **Version hash:** `strategies/base.py` and the B1 setting are part of every strategy's version hash.
- **Bot start guard:** `titan2 bot` refuses to start outside systemd unless an explicit override is given. The Windows entry is laptop mode (next item), which needs no override.
- **Laptop mode (Windows, PAPER only):** `titan2 laptop` runs one trading day in the foreground on a Windows laptop (`docs/LAPTOP.md`; profile `TITAN2_PROFILE=laptop`): the trading-day check, the token, the self-check phase `laptop`, the bot session (recovery first, exits resume before READY), the EOD timeline, the daily report. LIVE is refused in this profile whatever `ALLOW_LIVE` or a strategy's mode says (no registered static IP; no backstop if the laptop sleeps, loses Wi-Fi or reboots); a LIVE-mode strategy trades only its PAPER twin. No LIVE order of any role leaves the laptop (its KiteBroker refuses every LIVE place and cancel), and its recovery never imports today's titan2 LIVE orders that its own titan2.db lacks (another titan2's, e.g. EC2's, in the shared Kite account): HALT(UNKNOWN_POSITION, ACCOUNT:LIVE) and a CRITICAL alert instead. The phase `laptop` replaces the systemd, users, v1-masked and egress-IP rows; each FAIL halts entries and never exits. Its clock row (NTP, else the HTTP Date of api.kite.trade) stops entries (CLOCK_UNSYNCED) when this clock can be more than 1 s off internet time, the reading's own error counted (the whole-second HTTP Date adds 0.5 s), or when no time source answers. The first Ctrl+C sends a FLATTEN_ALL (also with nothing open), whose FLATTEN halt keeps entries off until the process stops; it waits until verified flat, then stops cleanly without a RESUME; the engine clears that halt once the run ends flat (Shutdown, or the EOD exit), and that command expires like a non-operator one. The second stops at once. Windows paths: `%LOCALAPPDATA%\titan2\var` and `\logs`, `%APPDATA%\titan2\titan2.env` (overridden by `TITAN2_VAR`, `TITAN2_LOG_DIR`, `TITAN2_ENV_FILE`). EC2, systemd and the Linux paths are unchanged.
- **price_spike_down (§10.4):** price_spike_down added 2026-10-12 at the user's request as a PAPER experiment, against brief E1: the exact signed mirror of `price_spike_up` v1 (same parameters; the moves measured downward; `Intent(DOWN, ATM, Recheck(ltp·0.995, baseline·0.9965))`, so its placebo is a CE), with its own version, `REGISTRY.lock` entry and G1 trial; it starts in PAPER like every strategy, and `price_spike_up` stays UP only.
- **Risk caps raised (2026-10-13, the user's decision after a 1-lot POLYCAB 8200CE entry was refused):** its planned stop-out (the 15% stop, the 5% slippage allowance and charges: ₹5,668 on a ₹27,775 premium) was over the ₹5,000 per-trade cap, and its premium over the ₹25,000 premium cap. `MAX_LOSS_PER_TRADE_RS` 15000, `MAX_PREMIUM_PER_TRADE_RS` 50000, `DAILY_STOP_RS` 15000 and `MAX_WORST_CASE_DAY_LOSS_RS` 50000, as defaults and as bounds, replace these four keys' old values (5000, 25000, 10000 and 40000; `MAX_UNITS`'s own 5000 is unchanged) wherever this file quotes them (§0, §8.3–8.4, §8.8); DETAIL (§0.2 G7, §8.4, §8.5, §8.8, §17.1) already shows the new values. The LIVE-only caps are unchanged: `LIVE_ACCOUNT_DAILY_STOP_RS` 10000, `LIVE_DD_STOP_RS` 15000 and `LIVE_PROBATION_LOSS_RS` 6000. At these caps G20 (premium + entry charges + ₹100 within ₹50k) is what limits one position's premium, at about ₹49.85k. All four keys are versioned, so at the next start every strategy gets a new version and its G1 trial starts again (the running trials are retired).
- **Three entries a day (2026-10-13, the user's decision after `price_spike_up` refused 23 signals with `ENTRIES: 2 + 0 pending/2`, PRESTIGE's at 14:31:47 among them):** `MAX_ENTRIES_PER_DAY` 3, as default and as bound, replaces 2 wherever this file quotes it (§8.3–8.4); DETAIL (§8.4, §13.11 Stage 3, §16.3 `test_pm_b_no_reverse_churn` and `test_pm_b_storm_within_a_minute_runaway`, §17.1) already shows it. It is per FULL book, so every strategy's PAPER book gets 3 (a LIVE book after its first-week cap of 1). Unchanged: `MAX_POSITIONS_PER_BOOK` 2, `MAX_ENTRY_ATTEMPTS_PER_DAY` 6, `MAX_ORDERS_PER_DAY_PER_BOOK` 40, `MAX_ENTRY_ORDERS_PER_MIN` 3 and `LIVE_FIRST_WEEK_MAX_ENTRIES` 1; so the day's three entries, if their orders go out within 60 s, now make G09 refuse a fourth intent in that minute as RUNAWAY (`HALT(RUNAWAY, BOOK, ACK)`) where the 2-a-day cap gave an ENTRIES refusal. The key is versioned, so at the next start every strategy gets a new version and its G1 trial starts again (the running trials are retired).
- **Late starts and paper leftovers (review round 2, 2026-10-14; §6 startup, §9.3 runs, §9.5, §9.6 step 7, §12.5):** the paper exchange's startup square-off of an earlier session's position runs on a trading day only (a weekend or holiday start settles nothing; the next trading day's start does). A start at or after `BROKER_SQUAREOFF_AT` on a trading day keeps the 5 s recon while an account holds a bot leg and no recon requested more than 5 s (`FRESH_FILL_GRACE_S`) after READY has shown the broker agreeing, until 16:00 (or 60 s after READY when later); meanwhile the EOD steps from TRUTH on (TRUTH, BAR_CHECK, EOD_TASKS, DONE) and a recovery-only run's exit wait. A trading-day start after today's recorded DONE step is recovery-only, like one after 15:45. An EOD step row (a `PHASE` event with a step) stamped later than now, or than a start (`BOOT`) written after it, was not written by a run on that day (a `titan2 laptop --date D` rehearsal from before the rehearsals had their own folders left D's whole timeline in the real titan2.db, stamped on its shifted clock): it is ignored with a WARN, for that decision and for the engine's EOD steps done (`state.eod_phase_rows`), so the real D runs its session and every EOD step (review round 3 follow-up). A PAPER/SHADOW leftover the paper exchange had already closed on an earlier day (its net at the start of today is 0, which no order of today shows) is booked from the paper exchange's own record of that close (the new paper-only read `FetchSquareoffs` → `SquareoffsResult`), as `close_external` books a square-off (`BROKER_SQUAREOFF`, FINAL at the record's price; UNRESOLVED and never above the record's lowest price when the record does not explain the qty); with no record at all it is closed like a LIVE leftover (CLOSED_EXTERNAL, UNRESOLVED, HALT(MISMATCH) + CRITICAL, acked once recon is CLEAN).
- **EOD truth check attribution (review round 2, 2026-10-14; §9.8, §10.7):** both sides are taken by structure session: today's ledger is today's structures' trades and today's attribution their orders. An earlier session's structure with a broker order executed today (a leftover settled at today's start) leaves today's check (its broker orders are listed as `carried`) and is checked against its own trade: today's tradebook rows of its orders plus its earlier fills from titan2.db, with its session's charges; its rows join the event's `books` with their `session`. A close with no broker order (CLOSE_UNRESOLVED, or the paper backstop with no record) is in no tradebook and is never compared: CLOSED_EXTERNAL, UNRESOLVED and its MISMATCH halt report it. A failed check counts as one G1 mismatch for each book it names (every book when it cannot attribute); the EOD_TRUTH_MISMATCH event beside it is not counted again.
- **RECOVERED books (review round 2, 2026-10-14; §8.2 books, §8.6, §8.8):** `RECOVERED:<account>[:<seq>]` is a FULL book of the account in its name (the account guard on LIVE): the per-book and LIVE-account daily stops and G20's LIVE account state count it. No intent routes to it and I11's caps leave it out (recovery fills it, never an entry).
- **Restart mid-order (review round 2, 2026-10-14; §5.10, decision B2):** after a process restart every UNKNOWN order, and every SUBMITTING one (its Place may have been sent before the crash), is looked up at +1/+3/+7/+15 s and through a whole `LATE_ACCEPT_S` window counted from the restart: NOT_PLACED no earlier than 30 s after it (it was 15 s, inside the window in which a sent SELL may still be accepted).
- **EOD steps due together (review round 2 follow-up, 2026-10-14; §12.5, §9.8):** when a start after 15:40, or a late start's wait that ends after their times, makes TRUTH, BAR_CHECK, EOD_TASKS and DONE due at once, they still run in order, each after what the steps before it sent: EOD_TASKS (nightly G1, exports, daily report) once TRUTH's tradebook reads and contract notes and BAR_CHECK's bars are answered, DONE (the exit) once EOD_TASKS' files are written. Each wait lasts at most the normal gap between the two steps' times, counted from when the earlier one ran; a recovery-only run's exit waits for the engine's DONE. A step whose earlier steps ran before its own time does not wait, so a normal day's timeline is unchanged.
- **Paper model v2 (review round 2, 2026-10-14; §6 at due time, auto square-off and X2, §8.4, §10.2; DETAIL §6.3, §6.6, §8.4):** `PAPER_MODEL_VERSION` 2. Of the three ways review round 2 found the paper exchange looking better than LIVE, it removes two and halves the third (item 1). (1) `fill_from_depth(levels, side, qty, limit, lot, participation, adverse_ticks, tick, *, draw)` keeps the level-by-level walk (participation applies to the depth walked so far), but the leftover fraction of a lot in walked qty·participation/lot fills one more lot with that probability, by the order's own uniform draw in [0, 1) (`paper.fill_draw`: the first `random()` of a `random.Random` seeded with `int(sha256(f"{session}|{account}|fill|{tag}"))`, so a replay gives the same fills), on both sides. The expected fill is participation × the walked depth: a lone one-lot level inside the limit fills with probability = participation (about half the time at 0.5), where it never filled. That is not "always, as LIVE": a live IOC usually takes that lot, so on a stop into a lone one-lot gap bid paper still misses, about half the time, the loss LIVE books (NSE shows whole lots, so at 0.5 the draw only ever decides such a half lot). FakeKite gives a LIVE order its PAPER twin's draw. `decide()` and the deep-breach bypass are unchanged. (2) The fill-time book (step 1) also counts as missing when it is crossed or locked (best bid ≥ best ask), or when its exchange time is in an earlier second than that of the quote the order was decided on. The engine sends that time with the order (`DecisionQuote.exch_wall` → `OrderRequest.decision_exch_wall`, never sent to Kite); the quote cached when `place()` runs is not used, since a lagged packet received between the decision and `place()` would pass for the decision. Kite stamps whole seconds; an order re-armed after a paper restart, or sent after an engine reload (titan2.db does not keep the time), has none and is checked by receipt only. The order waits for the next packet or REST as for a missing book. A wait that ends without a usable book is CANCELLED `SIM: crossed or locked book` or `SIM: book older than the decision` instead of `SIM: no fresh depth`, and the daily report counts the session's unfilled paper orders by SIM reason. The 15:26 square-off waits out a crossed or locked book the same way; if only such a book comes within the 1.5 s, it fills on that book at its worse side (a sell takes no bid above the best ask, a buy no ask under the best bid: min / max of bid and ask at the touch), never at the no-book ₹0.05. (3) The keys that only calibrate the paper exchange are fixed at their defaults: `PAPER_PARTICIPATION` 0.5, `PAPER_LATENCY_MEDIAN_MS` 250, `PAPER_LATENCY_SIGMA` 0.6, `PAPER_READ_LATENCY_MEDIAN_MS` 250, `PAPER_READ_LATENCY_SIGMA` 0.85, `PAPER_ADVERSE_TICKS` 0, `PAPER_PRICE_BAND_PCT` 20 and `PAPER_BOOK_MAX_AGE_S` 1.0 (lo = hi). A calibration is no setting to tighten: no side of the participation or the order latency is safe (a lower participation misses the lone gap bid more often, a wider sigma makes every order whose seeded draw is under the median faster, and a slower median can land a stop's exit after a gap), and a slower read delay, a younger book age or a narrower price band can delay a stop's exit the same way. A settings file may set them to nothing else (any other value, on either side, makes it invalid); changing one is a code change with a `PAPER_MODEL_VERSION` bump. This replaces the ranges of DETAIL §8.4 (0.1–0.5, ≥150 / ≥0.3, ≥0 / 5–20 / 0.2–1.0; DETAIL now shows it) and decision B1's ≥250 / ≥0.85; `PAPER_CAPITAL_RS` (the account's size, not a calibration) stays 100000–500000, tighten-only. Every strategy gets a new version at the next start, so G1 registers 3 new trials: m = 12, α per look 0.05/48 ≈ 0.104%. The trades made with paper model 1 count only for the old versions' trials.
- **Late-accept window across reloads, and the lookups' request time (review round 3, 2026-10-14; §5.2, §5.10, decision B2):** an engine reload after an error ends each SUBMITTING or UNKNOWN order's late-accept window where the order had it (30 s after its AMBIGUOUS answer, after the lookups a lost answer started, or after the restart; the engine keeps it for the process), not at a time rebuilt from the submit; a SUBMITTING order whose Place is still waiting for its answer gets a window too, ending `LATE_ACCEPT_S` after `PLACE_TIMEOUT_S` past its submit. A tag lookup's "not found" counts toward NOT_PLACED only when its orders() read was requested at or after the window's end (`LookupResult.req_mono`, taken before the call as for positions reads); a miss asked earlier but answered after the end asks for one more lookup, unless one sent after the end is still out. The NOT_PLACED moment, which counts like a fill for the post-fill rule (§9.2), also survives a reload.
- **Receipt times in G06 and G16 (review round 3, 2026-10-14; §8.6):** the feed's last underlying tick and the underlying's LTP are aged like quotes (G17) and exits: a receipt up to `RECEIPT_SKEW_S` (2 s) after the event began, written by the WS thread while the event runs, is age 0; one further ahead, or NaN, is stale.
- **DAY_BUDGET's inputs (review round 3, 2026-10-14, corrected by its review the same day; §8.5):** `DAY_BUDGET = floor((DAILY_STOP_RS + min(realized net, 0) − open stop-outs) / per_lot_risk)`: the realized day (a gain gives no room), every open and entering structure's stop-out measured from its entry, and this trade's planned stop-out must together stay within D3. A structure's stop-out (`sizing.remaining_stop_risk`) is its sizing plan's per-lot risk × lots, less what a lock or trail has raised its stop (open qty × (stop − hard stop)), never below 0, or its loss now (−MTM) when that is larger (a position gapped past its stop); an unrealized gain is neither credited nor counted as risk still to lose. With nothing open it is the formula above (the day net is the realized net). `size()`'s day term is therefore the book's realized net (`realized_net`, None while the MTM is unknown: `SIZE:DAY_PNL_UNKNOWN`), and an open structure without a sizing plan makes the stop-outs unknown (`SIZE:OPEN_RISK_UNKNOWN`). For a LIVE book the LIVE account's room caps it too, `LIVE_ACCOUNT_DAILY_STOP_RS + min(account realized net, 0) − account open stop-outs` over every LIVE book, RECOVERED:LIVE included (unknown: `SIZE:LIVE_DAY_PNL_UNKNOWN`). So the planned stop-outs of every open trade and the new one together stay within D3, and on LIVE within the account's stop: at the current caps a 1-lot LIVE premium above about ₹49,180 is refused even with the account flat. (The first version subtracted Σ max(0, planned stop-out + MTM now) from `DAILY_STOP_RS + min(day net, 0)`: an open winner's gain got no room from the min and was charged again as risk still to lose, so a safe second entry was refused once the first position was up more than `DAILY_STOP_RS` less both planned stop-outs, about ₹2,740 for two ₹30k 1-lot entries.)
- **Daily stop with an unknown MTM (review round 3, 2026-10-14; §8.2, §8.8):** an unknown MTM still blocks entries and the daily-stop decision, except that a realized loss alone at or past −`DAILY_STOP_RS` (a book), or −`LIVE_ACCOUNT_DAILY_STOP_RS` (the LIVE books' sum), trips the stop, with the same halt and forced exits.
- **G1's sample order (review round 3, 2026-10-14; §10.7):** the sample is in booking order (the structure's close time, then structure_id): a LOOK judges the first n trades booked and CONFIRM the next 25 booked after them, so a trade booked late (a missed square-off settled the next day) comes after the trades already judged and is assessed once.
- **Waiting for pending entries (2026-10-14, the user's decision after `opening_drive`'s TCS was refused `POSITIONS: 2/2` at 09:20:00.212 while two reservations, refused at their decisions 2 s later, held both slots; §5.4, §8.6 G09; DETAIL §4.6, §5.4):** when G09 refuses an intent's first routed book (`PAPER:<s>` in PAPER mode, `LIVE:<s>` in LIVE mode) only because of pending structures, the intent waits instead of being refused. Pending means today's RESERVED structures and today's ENTERING ones not yet opened, of the book and, for a LIVE book, of the LIVE account. "Only because" means a BOOK_CAPS refusal with no halt and no gate error for which G01–G13 all pass on the same snapshot with the pending structures treated as voided (`risk.waits_for_pending` over `risk.settled`, which frees their position, entry and reservation slots and the entry orders the reserved ones would still send, and keeps what they already did: their orders in attempts, orders, RUNAWAY and ENTRY_ORDERS, their underlyings in G10; a malformed count never waits). So positions, entries (the LIVE first week's 1 included), attempts, the entry orders still to come (ENTRY_ORDERS, below) and the LIVE account's positions can wait; RUNAWAY, ORDERS, BAD_COUNTERS, GATE_ERROR and every other gate refuse at once, QUALITY books never wait (they skip G09), and the PAPER twin of a LIVE book never waits on its own. A waiting intent holds nothing (no structure, slot or fetch) and none of its books is routed: its first book's `intent_routes` row reads REFUSED with today's gate and detail, and `INTENT_WAITING{book, gate, detail, pending, ttl_s}` names the structures it waits for. At the end of every Timer (after the TTL and lost-fetch voids) and before any newer intent is routed, the waiting intents are routed again, oldest first, as if they had just arrived: every G01–G13 gate on fresh data, so G03 ends the wait at the intent's TTL, G04 diverts it under an operator halt and G05 refuses it from NO_NEW_AFTER. It ends reserved (then one fetch and G14–G22 on its own decision snapshot, as for any intent), refused with a fresh intent's gate and detail, EXPIRED or DIVERTED; while only pending structures still block it, it keeps its place and nothing is written. The rows written after a wait (the first book, the twin, a divert target) carry `waited_s` and `waited_for` in INTENT_ROUTED. In LIVE mode the PAPER twin has no row while `LIVE:<s>` waits, and both are routed in one pass (one fetch, one snapshot); a twin whose LIVE book was reserved or refused at arrival is routed at once and never waits. The wait lives in RAM only: a reload, restart or SIGTERM drops it, the REFUSED row stands and the intent is never replayed. §5.4 step 5 still holds: deferring the one attempt is not a retry. A woken intent meets RUNAWAY like a new one and is refused, but raises no `HALT(RUNAWAY)` (the owner's decision: the entry orders it counts are those of the intents it waited behind); an intent arriving in that minute still halts the book. No limit changes, and the entry-order cap holds for any mix of fresh and woken intents (the review of this change, the same day): G09 counts each RESERVED structure of the book as an entry order still to come (it sends its one order at its decision, after G09), refusing `ENTRY_ORDERS: <n> in 60 s + <r> reserved/<MAX_ENTRY_ORDERS_PER_MIN>` when the orders made in the last 60 s plus the reserved structures reach the cap. It is checked after ORDERS and is a pending cap: no halt, and the intent waits (it is reserved if a reserved one voids without sending; once their orders are out it meets RUNAWAY as a woken intent, refused without the halt). RUNAWAY, checked first, still counts only the orders made, so it halts a fresh intent exactly as before. Without it one batch sent 4 entry orders in 0.33 s (4 real orders on LIVE): two IOCs that filled nothing freed both slots before a Timer that woke two waiting intents, and each counted only the 2 orders made. `ENGINE_SEMANTICS_VERSION` 2 (bullet "Engine semantics 2" below).
- **One REST quote retry (2026-10-14; supplements §5.8 and DETAIL §5.8):** `KiteData.quote()` makes a call whose connection the other end closed before Kite answered at all (requests' `ConnectionError` around urllib3's `ProtocolError('Connection aborted.', <reason>)` whose reason is a `ConnectionResetError` (also `http.client.RemoteDisconnected`, closed before a status line), a `ConnectionAbortedError` (Windows) or a `BrokenPipeError`: an idle keep-alive connection Kite had closed, which voided an entry `QUOTE:FETCH_FAILED` on 2026-10-13) once more at once, after a fresh token of the quote bucket (1/s, never skipped), and logs a WARNING with the process's running count. Never retried: timeouts (urllib3 wraps a timeout while the request is being sent in that same `ProtocolError`; the first version retried it, and a malformed status line or headers, review of the retry), failed connects (`Max retries exceeded`), SSL errors, any answer from Kite (429, 5xx, token errors), `ltp()`, the account reads, place and cancel. A second failure fails the read as before, its text ending `[retried once: the first try's connection dropped]`; the error class stays first, so FATAL still halts TOKEN. The same quote path serves exit refreshes, the self-test and the paper exchange's REST depth reads, which get the retry too.
- **Engine semantics 2 (2026-10-14; §8.4 code constants, §10.2; DETAIL §8.4):** `ENGINE_SEMANTICS_VERSION` 2, one bump for the release that also makes `PAPER_MODEL_VERSION` 2, so each strategy gets one new version, not two (`opening_drive` 1.54ba54e096 → 1.b230b83102, `price_spike_up` 1.1882405ca0 → 1.3a030f3e26, `price_spike_down` 1.6d19c4137a → 1.9f039e44a7), and G1 registers 3 trials: m = 12, α per look 0.05/48 ≈ 0.104%, bootstrap B 48,000. `REGISTRY.lock`'s `_engine.engine_semantics_version` is 2; `SEMANTIC_CONSTANTS` and its hash are unchanged. It covers the two bullets above and the engine changes of review rounds 2 and 3 (the bullets above them), which change which entries pass, how orders resolve, how trades are booked or what G1 counts: RECOVERED books in the daily stops; the late-accept window after a restart and across reloads, and NOT_PLACED by the lookups' request time; a paper leftover booked from the paper exchange's record of its close (and settled on trading days only); a late start's recon hold (its grace counted from READY), and the EOD steps due together run in order, so the truth check is in before the nightly G1; the truth check by structure session, counted once per named book, without the closes no tradebook shows; receipt times in G06 and G16; DAY_BUDGET's inputs (the realized day and each open trade's stop-out from its entry); the daily stop with an unknown MTM; and G1's sample in booking order. The rest of those rounds (a start after DONE is recovery-only, and an old rehearsal's EOD rows are ignored; an adopted late fill on the WebSocket; `titan2 laptop --date` in its own folders) changes neither a strategy's trades nor G1's counts.
- **Alerts in a network drop (2026-10-14 night; supplements §12 Alerts and DETAIL §12.6):** a message that still fails after its tries because of the network (no DNS answer, no connection, a timeout, or HTTP 429/5xx) is kept, up to 200 messages for up to 2 h each, and the messages after it wait behind it. Every 30 s one `ALERTS_DELAYED` line ("network back at HH:MM:SS after N s; K alerts delayed") is tried; once it goes out the kept messages follow, CRITICAL first, each with the time it was raised. Meanwhile a CRITICAL's 5-minute repeats pause, so no burst of repeats follows an outage; they resume after it. A message the service refused (any other HTTP status) is logged and not tried again. What is still undelivered when the bot stops goes to `<var>/run/alerts_outbox.json` and is sent at the next start, after one line saying so. Nothing else changes: no strategy, setting or version.
- **Measurement, laptop checks and the phone report (2026-10-14 night; supplements §4.6, §4.7, §12 Alerts and the daily report, DETAIL §4.6, §12.6, §13.7):** information and display only: no strategy, setting, version or decision changes, and nothing measured is an input. Two DEBUG event kinds: `LATENCY` (once a minute with the heartbeat, and before the daily report: the quote lane's measured calls, each with its whole time, Kite's own share, the symbols, the seconds since the previous call, a retry and the outcome, and every broker read's round trip per account and kind) and `PREOPEN_VOLUME` (the first bars' pre-open cumulative volume, the last tick before the open). `bar_checks.detail` adds `live_volume`, `kite_volume`, `preopen_volume`, `volume_gap`, `volume_rel` and `volume_rel_ex_preopen`; the check still judges `volume_dev` alone. `events_tail.json` leaves out DEBUG rows. The daily report's latency table adds `quote_call_ms`, `quote_kite_ms` and `read_ms:<ACCOUNT>:<KIND>` rows and a max column; its Telegram text adds the day's refusals and voids by reason, the saved what-if totals, the runtime's notes (the laptop's condor), the bar check, the alerter's delayed and undelivered counts and, on each G1 line, whether today counts for G1. The WARN list adds `SESSION_IMPAIRED` (one alert per event's new marks) and, on the laptop, `POWER` (AC to battery; back on AC an INFO). On the laptop the LIVE account's `READ_LOST` and `STALE_RECON` texts begin "Kite account check (no LIVE trading here)", and the phase `laptop` adds the WARN-only rows `lid`, `restart_pending`, `update_hours` and `wifi` (no alert of their own; SKIP when unreadable). A placebo's position alerts begin "control trade (placebo), not a strategy result".

## 0. Summary
- One owner. `titan2-bot` (user `titan`) owns every order and all trading state. `titan2-dash` (user `titandash`) has no credentials, reads only published JSON, and can only insert PAUSE/RESUME/FLATTEN_ALL commands (R1).
- Paper = live. Both use the same engine, gateway, exits, sizing, risk, recon and charges. Only the broker differs: `KiteBroker` vs `PaperExchange`. The paper broker fills from fill-time depth at 0.5 participation after lognormal latency, keeps its own persisted ledger, and simulates the 15:26 square-off.
- P&L and loss caps. P&L comes only from fills plus exact charges, through one function. Exits use the realizable bid. Loss caps:
  - ₹5k per trade;
  - ₹10k per day on realized + bid MTM (sticky);
  - ₹40k worst case.
- PAPER until proven. A strategy stays in PAPER until all of the following hold:
  1. G1 passes: multiple-testing controlled, day-clustered, placebo-controlled, recon-clean.
  2. `ALLOW_LIVE=1` is set.
  3. A CLI unlock bound to the G1 report hash is issued.
  4. The daily non-filling order-path self-test passes.

  The expected honest outcome is no LIVE strategy (brief §0–1).

Guarantees
| # | Guarantee | § |
|---|---|---|
| G1 | Only the core decides orders | 3 |
| G2 | Position = broker-confirmed fills | 5 |
| G3 | Never sell more than the broker holds or than own fills explain | 9.4 |
| G4 | P&L booked once, from fills | 5.3, 8.1 |
| G5 | Stops use a bid ≤3 s old; otherwise REST, otherwise protective exit at 5 s | 7 |
| G6 | 1 lot; two-sided depth check; an invalid cap refuses | 8.5 |
| G7 | Hard worst-case cap | 8.8 |
| G8 | Gates fail closed; halts never block exits | 8.6 |
| G9 | Restart never double-enters; exits resume before READY | 9 |
| G10 | Paper is realistic and checked against live | 6, 10.9 |
| G11 | LIVE needs G1 plus two human keys | 10.8 |
| G12 | Deploy cannot clobber state or server edits | 13 |

Not guaranteed: positive expectancy; fills during outages; host death just before 15:26.

Judges' findings. Every fatal flaw the judges found is fixed, and every gap they marked "missing everywhere" is closed. DETAIL §0.3 has the full flaw-to-fix table.

Fixes to the fatal flaws:
- no blocking ExecStartPre;
- an external dead-man check;
- v1 and the CI deploy are masked or removed;
- the paper ledger is persisted and independent of the engine;
- verify-flat never sells;
- EXTERNAL needs post-fill confirmation;
- the exit band is capped at 12%, with LPP re-pricing;
- settings are read once and exits are frozen;
- I/O runs only on the lanes, with rollback and reload on failure;
- `callFromThread`;
- immutable releases;
- the self-test runs at 09:15:30;
- settings can only be tightened, and the version hash covers all of them;
- the placebo needs n ≥ 50 and is scored at 1.0×;
- flock;
- no second order tool.

Gaps now covered: STT and txn rates, positions lag, multiple testing, operator bias, live/G1 population, paper probation, the bar check, the worst-case cap, stop debounce, the square-off hand-off, the calendar, the ban list, the settlement window, and token rotation. They are handled in §5.6, §7.4, §8, §9, §10 and §11.

Requirement trace
| Req | § | Req | § | Req | § |
|---|---|---|---|---|---|
| R1 | 3, 12 | R6 | 8 | R11 | 9 |
| R2 | 5 | R7 | 8.5 | R12 | 13 |
| R3 | 8.1 | R8 | 10 | R13 | 15 |
| R4 | 6 | R9 | 11 | R14 | 16 |
| R5 | 7 | R10 | 4.6, 12 | R15 | 14 |

Coding conventions
- Time. Wall time is IST-aware (`zoneinfo("Asia/Kolkata")`). Ages use `time.monotonic()`, which is never persisted.
- Money. ₹ as float, rounded to 2 dp when persisted.
- Fails closed. A bad input refuses an ENTRY and never blocks an exit.
- Python ≥3.10. No `tomllib`, `typing.Self`, `datetime.UTC` or `ExceptionGroup`. All modules except `runtime` and `market/feed` must import on Windows.
- Dependencies. `kiteconnect==5.0.1` (hash-locked), requests, pyotp, flask, gunicorn; pytest for dev.

## 1. Architecture
B: a lean core with an operational strangler cutover.
- New code, ported not imported. New package, process, DB and units. Proven v1 logic is ported with the header `# Ported from agentic_trader/<file>:<lines> @b4694f3 — changes: …` and is never imported.
- v1 retired on the server. It is masked and moved aside, and never trades again. "Strangler" means reversible server stages only (§13).

Why: the §4.1 killers live on seams that A keeps and C duplicates, and v1 has no positive Exp_net. The seams:
- ≥12 exit callers;
- P&L spread over 4 modules;
- ~25 risk call sites;
- 3 JSON IPC files;
- a process-wide monkey-patch (zerodha_tools 23-99);
- ~10 thread types.

Binding decisions
1. Functional core. `Engine.handle(event) -> list[Command]` runs on one core thread inside a DB transaction. Commands are dispatched only after commit. Network I/O happens only on lanes.
2. One SQLite writer per file.
   | File | Writer |
   |---|---|
   | titan2.db | core |
   | paper_exchange.db | paper-sim |
   | commands_dash.db | dashboard |
   | commands_cli.db | CLI |
   | pub/*.json | core |
3. Invariants are enforced in this order: DB constraints, then runtime checks I1–I16, then tests.
4. Only LIMIT+IOC+MIS orders. No MARKET, SL, SL-M or GTT.
5. No v1 imports. Startup asserts `KiteConnect.place_order` is unpatched.

## 2. Modules (`agentic_trader/`; the server WorkingDirectory stays `/home/ubuntu/titan/agentic_trader`)
```
titan2/ __init__ __main__ clock settings model charges instruments store ledger g1 alerts token selfcheck runtime plugins
        migrations/0001_init.sql  data/nse_sessions.json  market/{quotes,feed,bars}  broker/{port,kite,lanes,paper}
        core/{events,state,engine,operator,invariants,gateway,single_leg,exit_loop,exits,sizing,risk,orders,positions,recon}
        strategies/{__init__,base,opening_drive,price_spike_up} + REGISTRY.lock   dash/{__init__,app} + templates/index.html
tests/{conftest.py,fakes,unit,scenarios,chaos,arch,golden,ops}  ops/  requirements.lock requirements-dev.txt pyproject.toml
<repo>/.github/workflows/ci.yml (tests only)
```
Ported code
| Module | Ported from | Change |
|---|---|---|
| `settings` | settings_manager 117-145 | — |
| `model.walk` | zerodha_tools 4632-4657 | — |
| `charges` | replaces config 70-107 | — |
| `instruments` | options_trader 3497-3550; autonomous_trader 9429-9447; config 1564-1569 | no clamp |
| `store` | state_db 226-237 | `synchronous=FULL` |
| `token` | kite_token_manager 52-307 | 312-330 dropped |
| `selfcheck` | pre_market_check 47-185 | — |
| `market/quotes` | kite_ticker 53-94, 491-589 | never returns stale |
| `market/feed` | kite_ticker 274-414, 645-689, 733-796 | — |
| `broker/kite` | zerodha_tools 49-57, 75-92, 1383-1405 | — |
| `core/exits` | exit_manager 445-485, 1332-1406 | re-anchored to BE |
| `core/risk` liquidity | zerodha_tools 4547-4669 | fail-closed |
| `core/recon` | position_reconciliation 248-281 | signed |
| `strategies/price_spike_up` | kite_ticker 1407-1435, 1507-1533, 2049-2108; sustain 1701-1848 | sustain made signed |

Everything else is new. Module roles and sizes are in DETAIL §2: about 7.1K lines of Python, 3.5K of tests, and 0.8K more for phase 2.

Import rules (`tests/arch`)
- Strategies import only `strategies.base`, `model` and the stdlib.
- core does not import kiteconnect, broker.kite, broker.paper, market.feed or dash.
- Only `broker/kite.py` references place_order, cancel_order or modify_order.
- No code references any GTT method, `ORDER_TYPE_MARKET`/`SL`/`SLM`, `"MARKET"`, `"SL-M"` or `multiplier`.
- dash does not import kiteconnect, broker, core or store.
- No v1 module names anywhere.

## 3. Concurrency and process model
### 3.1 Processes
| Process | User | Notes |
|---|---|---|
| `titan2-bot` | titan | Single instance: `flock(LOCK_EX\|LOCK_NB)` on `/var/lib/titan2/run/titan2.lock`, else exit 73. Writes db/, pub/, logs, reports, exports |
| `titan2-dash` | titandash | `gunicorn -w1 --threads 4 -k gthread -b 127.0.0.1:8050`. Cannot read secrets or `session/`. Writes only `dash/commands_dash.db` |
| `titan2-token`, `titan2-selfcheck`, `titan2-backup` | titan | Oneshot timers |
| CLI `titan2` | titan (via `sudo -u titan`) | Writes `commands_cli.db` and `settings.json`. Never writes `titan2.db` |

### 3.2 Threads
| Thread | Job |
|---|---|
| `core` | `Engine.handle` per event in a transaction, then dispatch. Timer every 250 ms. Reads the command DBs every 1 s. Writes `pub/status.json` every 1 s. Sends sd_notify |
| `kite-ws` | Twisted reactor. `on_ticks` updates QuoteCache, pushes UnderlyingTick batches to `signal_q`, and adds held/pending option symbols to the dirty set, posting at most one `QuotesDirty`. `on_order_update` produces `OrderSnapshot(LIVE, WS)`. Connection callbacks produce `FeedState` |
| `signals` | Bars and strategies → `IntentReceived`, `BarsClosed` |
| `lane-live-orders` | KiteBroker place / cancel / order_history / order_trades, one call at a time |
| `lane-reads` | Batched quotes, ltp, positions, orders, trades, margins, instruments, historical, contract note, ban list |
| `paper-sim` | PaperExchange for the PAPER and SHADOW accounts |
| `alerts` | Telegram and healthchecks |

Queues
- `core_q` is unbounded and never drops.
- `signal_q` holds 20k batches. On overflow it drops the oldest batch and marks the feed degraded (STALE_FEED).
- `alert_q` holds 1k and drops the oldest.

Feed handling
- The core reads the dirty set with `feed.take_dirty()`.
- `Feed.request()` updates `desired: dict[token, mode]` and calls `reactor.callFromThread(_apply)`.
- `on_connect` re-applies the desired set. KiteTicker methods are never called off the reactor thread, because in 5.0.1 `subscribe`/`set_mode` call `ws.sendMessage` directly.

Rate limits and priority
- Blocking buckets that never fail open: orders 8/s (burst 3), quote 1/s (≤500 instruments per call), historical 3/s, other 8/s.
- Read priority: EXIT (exit quotes, plus positions/orders for the cap and verify-flat) > ENTRY_LIVE > RECON > PAPER (decisions and fill depth) > SHADOW > BACKGROUND.
- Quote requests are batched by priority. A request that waits 3 s returns `QuoteResult(ok=False, "TIMEOUT")`.

Live lane. Always built, as `KiteBroker(entries_allowed=(ALLOW_LIVE=="1"))`. When entries are not allowed it refuses ENTRY and SELFTEST.

### 3.3 Core loop (`runtime.core_loop`)
```python
while not rt.stopping:
    now = clock.mono()
    if now >= next_timer: ev, next_timer = Timer(clock.now_wall(), now), now + 0.25
    else:
        try: ev = core_q.get(timeout=next_timer - now)
        except queue.Empty: continue
    try:
        with store.tx():                                   # BEGIN IMMEDIATE ... COMMIT (synchronous=FULL)
            cmds = engine.handle(ev); engine.check_invariants()
    except InvariantViolation as iv:
        engine = Engine.load(store, ctx)
        with store.tx(): cmds = engine.on_invariant_violation(iv)
    except Exception as exc:
        engine = Engine.load(store, ctx)
        with store.tx(): cmds = engine.raise_halt("EXCEPTION", "GLOBAL", "ACK", repr(exc)[:500])
        if exc_window.hit_and_count(now, 60) > 20: rt.exit(3)
    for c in cmds: rt.dispatch(c)                          # only after commit
    if isinstance(ev, Timer): heartbeat.core_progress(clock.mono())
```
- After a reload: re-poll every non-terminal order, run recon immediately, ignore unknown `request_id`s, and always apply `OrderSnapshot`s (matched by tag or id).
- Watchdog: `WATCHDOG=1` only if the last Timer was under 2 s ago (`WatchdogSec=30`).
- Healthchecks: ping only if core progress is under 10 s old.

### 3.4 Mutation rules and invariants
Mutation rules
- `@core_only` guards every Engine/Store writer by asserting `get_ident()==CORE_TID` (the harness sets it).
- KiteBroker order methods assert the `lane-live-orders` thread; tests pass `thread_check=False`.
- The reads lane gets `KiteData`, which has no order methods.
- Shared state is limited to QuoteCache (immutable values), the dirty set, the queues and the buckets.

Invariants (checked after every event)
| ID | Invariant |
|---|---|
| I1 | core-only mutation |
| I2 | order endpoints only on lane-live-orders |
| I3 | 0 ≤ open_qty = Σentry − Σexit/cover fills ≤ Σentry |
| I4 | ≤1 non-terminal bot order per leg (DB) |
| I5 | ≤1 non-terminal structure per (book, underlying) and per (LIVE, underlying) (DB) |
| I6 | exit/cover qty ≤ min(open_qty, §9.4 cap) |
| I7 | ENTRY⇒BUY, EXIT⇒SELL, COVER⇒BUY, SELFTEST⇒BUY (DB) |
| I8 | OPEN leg has frozen ExitParams with 0 < sl < entry < tp and floor > be |
| I9 | filled_qty monotonic; terminal orders immutable (DB) |
| I10 | P&L booked once (DB) |
| I11 | FULL books: open+reserved ≤ MAX_POSITIONS and entries ≤ MAX_ENTRIES |
| I12 | qty % lot == 0, and lot ≤ qty ≤ caps |
| I13 | tick-multiple limits; entry ask ≤ limit ≤ floor_tick(mid·1.01); exit limit ≥ floor_tick(ref_bid·0.88) |
| I14 | no ENTRY/SELFTEST on LIVE unless ALLOW_LIVE=1; ENTRY also needs mode LIVE, unlock, and today's self-test |
| I15 | clock jump > 2 s → WARN |
| I16 | settings within LIMITS |

On violation
- Global or account scope: HALT(INVARIANT, ACK) + CRITICAL. Exits continue.
- Leg scope: EXITING(ANOMALY_EXIT) + CRITICAL.

## 4. Data model
### 4.1 `model.py` (names are binding)
```python
# str Enums
Account LIVE PAPER SHADOW | Policy FULL QUALITY | Mode OFF SHADOW PAPER LIVE | Direction UP DOWN | Side BUY SELL
StructureType LONG_CE LONG_PE (+phase 2) | StrikePref ATM ITM1 | Role ENTRY EXIT COVER SELFTEST EXTERNAL
OrderState NEW SUBMITTING UNKNOWN WORKING CANCEL_REQUESTED + terminal COMPLETE CANCELLED REJECTED NOT_PLACED
LegState PLANNED ENTERING OPEN EXITING VERIFY_FLAT + terminal CLOSED CLOSED_EXTERNAL VOID
StructState RESERVED ENTERING OPEN EXITING VERIFY_FLAT + terminal CLOSED CLOSED_EXTERNAL VOID
Urgency NORMAL PROTECTIVE | PnlStatus FINAL ESTIMATED UNRESOLVED | HaltClears NEXT_SESSION ACK AUTO RESUME
EXIT_REASONS: HARD_SL LOCK_STOP TAKE_PROFIT TIME_STOP EOD_FLATTEN STALE_QUOTE_PROTECTIVE NO_BID_PROTECTIVE KILL_SWITCH
  OPERATOR_FLATTEN DAILY_STOP ANOMALY_EXIT RECOVERY_FLATTEN SELFTEST_FILLED CLOSED_EXTERNAL BROKER_SQUAREOFF

# frozen dataclasses (slots=True)
Level(price, qty, orders=0)
Quote(symbol, token, bids, asks, ltp, volume, last_trade_wall, exch_wall, recv_mono, recv_wall, source)
    # source: WS | REST | SIM
    # bid/ask properties; mid() is None unless both sides exist and ask >= bid; spread_pct(); age(now_mono)
WalkResult(avg_px, fillable_qty, worst_px)
walk(levels, qty, limit, side)        # BUY takes asks <= limit; SELL takes bids >= limit
UnderlyingTick(symbol, token, ltp, cum_volume, day_open, prev_close, exch_wall, recv_mono, recv_wall)
Bar(symbol, start, open, high, low, close, volume, valid, source)                  # source: LIVE | HIST
Underlying(symbol, token, name, kind)                                             # kind: STOCK | INDEX
Instrument(symbol, token, tradingsymbol, exchange, name, underlying, opt_type, strike, expiry, lot_size, tick)
Recheck(lo, hi)
LegSpec(opt_type, strike_rule, side, ratio=1)                                     # phase 2
Intent(intent_id, strategy, strategy_version, underlying, direction, structure, strike_pref,
       expiry_rule="NEAREST_ELIGIBLE", trigger_px, recheck, ttl_s (0 < ttl_s <= 10), created_mono, created_wall,
       reason, is_placebo=False, parent_intent_id=None, legs=())                  # Intent has no qty field
ExitProfile(hard_sl_pct=15, lock_arm_pct=4, lock_floor_pct=3, trail_retain=0.70, tp_pct=15, time_stop_min=45,
            time_stop_when_locked=False, stop_confirm_ms=400, stop_confirm_max_ms=1500,
            stop_confirm_bypass_pct=3.0, no_bid_exit_s=10)
ExitParams(profile, entry_avg, qty, entry_charges, rates_version, k_exit, be_px, hard_sl_px, arm_px, floor_px,
           tp_px, entry_fill_wall, time_stop_at)
DecisionQuote(bid, ask, mid, age_ms, src, ltp, walk_px)
OrderRequest(account, tag, role, symbol, tradingsymbol, exchange, token, side, qty, limit_px, lot_size, tick)
    # LIMIT / IOC / MIS / regular are implied
Fill(order_pk, seq_key, source, qty, px, wall, exch_wall)       # seq_key "cum:<n>" | "trade:<id>"; source CUM | TRADE
Charges(brokerage, stt, txn, sebi, ipft, gst, stamp, sqoff_fee)  # .total
PnL(gross, charges, net, n_executed_orders, rates_version)
Halt(halt_id, scope, kind, reason, operator, clears, created_wall, session)
    # scope: GLOBAL | ACCOUNT:<A> | BOOK:<b> | STRATEGY:<s> | UNDERLYING:<u> | SYMBOL:<sym>
BrokerOrder(account, broker_order_id, tag, symbol, tradingsymbol, exchange, product, side, order_type, validity,
            qty, filled_qty, avg_px, status, raw_status, status_msg, order_wall, exch_wall, placed_by)
    # status normalised to WORKING | COMPLETE | CANCELLED | REJECTED
BrokerTrade(account, trade_id, broker_order_id, symbol, side, qty, px, fill_wall, exch_wall)
BrokerPosition(account, symbol, tradingsymbol, exchange, product, net_qty, buy_qty, sell_qty, buy_value, sell_value)
Margins(account, available_rs, raw)
FeedHealth(connected, connected_since_mono, last_underlying_tick_mono, reconnects, degraded)
Refusal(gate, detail)
Sized(qty, lots, binding, per_lot_risk_rs)

# mutable dataclasses (slots=True), owned by the core thread
Order(order_pk, leg_id, structure_id, account, book, role, side, symbol, qty, limit_px, attempt, band_idx,
      state=NEW, broker_order_id, broker_status, status_msg,
      external_kind,                     # MANUAL | AUTO_SQUAREOFF
      filled_qty=0, avg_fill_px, decision: DecisionQuote, created_wall, submitted_mono, terminal_wall,
      lookups=0, stuck=False)
Leg(leg_id, structure_id, leg_no, account, book, session, seq_no, inst, side="LONG", state, orders: list[str],
    stop_px, lock_armed, peak_rb, peak_wall, trough_rb, trough_wall, breach_first_mono, breach_quote_mono,
    no_bid_since_mono, entry_fill_wall, last_fill_mono, last_fill_wall, exit_reason, exit_urgency,
    exit_band_idx=0, exiting_since_mono, broker_residual_qty=0)
    # entry_filled, exit_filled, open_qty: derived properties only
Structure(structure_id, session, book, account, policy, strategy, strategy_version, intent, type, underlying,
          state, legs, is_placebo, parent_structure_id, blocked_by, decision, placebo_status, exit_params,
          risk_basis_rs, exit_reason, void_reason, reserved_wall, opened_wall, closed_wall, pnl_status)
```

Tick math
- `floor_tick(x, t) = round(floor(x/t + 1e-9)·t, 2)`
- `ceil_tick(x, t) = round(ceil(x/t − 1e-9)·t, 2)`
- `is_tick_multiple` tolerance: 1e-6

Tags
- Format: `"T2" + role{E,X,C,T} + account{L,P,S} + yymmdd + b36(seq_no,3) + b36(attempt,2)`.
- Always 15 characters from `[0-9A-Z]`; Kite allows up to 20. Example: `T2EP26101200101`.
- `seq_no` counts per (account, session, leg). Every retry is a new attempt, so it gets a new tag.
- `parse_tag()` returns None for anything that is not exactly this format. `is_bot_tag(tag, account)`.

### 4.2 `core/events.py`
Events (into `core_q`)

| Event | Fields |
|---|---|
| `Timer` | now_wall, now_mono |
| `QuotesDirty` | — |
| `FeedState` | connected, reconnected, degraded, reason |
| `IntentReceived` | intent |
| `BarsClosed` | bars |
| `QuoteResult` | request_id, ok, symbols, missing, error |
| `PlaceResult` | account, order_pk, outcome (OK \| REJECTED \| AMBIGUOUS \| FATAL), broker_order_id, error_class, message, throttled |
| `CancelResult` | account, order_pk, outcome (OK \| BENIGN \| REJECTED \| AMBIGUOUS \| FATAL), message |
| `OrderSnapshot` | account, broker_order_id, tag, status, filled_qty, avg_px, status_msg, exch_wall, source (WS \| POLL \| LOOKUP \| SIM \| RECOVERY), recv_mono |
| `LookupResult` | account, order_pk, ok, found |
| `TradesResult` | account, order_pk, ok, trades |
| `PositionsSnapshot` / `OrdersSnapshot` | account, ok, rows/orders, req_mono, recv_mono, error |
| `TradebookSnapshot` | account, ok, trades, req_mono |
| `MarginsResult` | account, ok, margins, recv_mono |
| `HistoricalResult` | request_id, symbol, ok, bars |
| `PrepResult` | kind (INSTRUMENTS \| BAN_LIST \| PREV_BARS), ok, detail |
| `ContractNoteResult` | account, ok, rows |
| `Shutdown` | reason |

Commands (returned by `Engine.handle`)
- Orders: `Place(account, order_pk, req)`, `Cancel(account, order_pk, broker_order_id)`, `PollOrder`, `LookupTag(account, order_pk)`, `FetchTrades`.
- Broker state: `FetchPositions(account, priority)`, `FetchOrders(account, priority)`, `FetchTradebook`, `FetchMargins`.
- Market data: `FetchQuotes(request_id, symbols, priority)`, `FetchHistorical(...)`, `Prep(kind)`, `ContractNote(account, orders)`.
- Feed: `Subscribe(tokens, quote|full)`, `Unsubscribe`.
- Control: `Alert(severity, code, text, dedup_key)`, `StartSignals()`, `Exit(code)`.

Routing: LIVE commands go to `lane-live-orders`; PAPER and SHADOW commands go to `paper-sim`, which replies with the same events (source SIM). The engine never branches on the broker type.

### 4.3 `titan2.db` (`migrations/0001_init.sql`)
- Connection settings: WAL, `synchronous=FULL`, `foreign_keys=ON`, `busy_timeout=5000`.
- The full DDL in FINAL_SPEC_DETAIL.md §4.3 is normative and must be copied verbatim.

| Group | Tables |
|---|---|
| Base | meta, seq |
| Audit | events (append-only) |
| Entry decisions | intents, intent_routes |
| Positions and orders | structures, legs, orders, fills, trades (immutable ledger) |
| Monitoring | quote_checks, halts |
| Strategy and G1 | strategy_modes, g1_trials (no delete), g1_reports |
| Market data | bars_5m, bar_checks |
| Broker and operator | broker_snapshots, foreign_acks, command_acks, session_flags |

Constraints that must exist (each is tested):
- UNIQUE partial index `structures(book, underlying)` over non-terminal rows. RESERVED counts as non-terminal.
- UNIQUE partial index `structures(account, underlying)` where `account='LIVE'`, also over non-terminal rows.
- Trigger: a terminal structure's state cannot change.
- `orders` CHECK constraints:
  - role=EXTERNAL, or all of: LIMIT, IOC, MIS, regular, a limit price present, and a 15-character `T2…` primary key;
  - role↔side pairing as defined in I7;
  - `0 ≤ filled_qty ≤ qty`.
- UNIQUE partial index on `orders(leg_id)` over non-terminal, non-EXTERNAL rows.
- Triggers: `filled_qty` can never decrease, and a terminal order's state, `filled_qty` and `avg_fill_px` are immutable.
- `fills`: UNIQUE `(order_pk, seq_key)`; append-only.
  - View `effective_fills` returns an order's TRADE rows when they sum to its `filled_qty`, otherwise its CUM rows.
- `trades`: PK `structure_id`; no UPDATE or DELETE.
- `halts`: UNIQUE `(scope, kind)` while not yet cleared.
- `intents`: CHECK `0 < ttl ≤ 10`, plus UP⇔LONG_CE and DOWN⇔LONG_PE.

Pruning: at EOD on the last session of each week, export `quote_checks` rows older than 180 days to CSV.gz and then delete them, and delete `broker_snapshots` older than 30 days. Nothing else is ever deleted. v1's `titan_state.db` is never opened.

### 4.4 Other database files
`db/paper_exchange.db` (only the paper-sim thread writes it)
- `p_orders`: account, tag, qty, lot, tick, limit, due_ts, status, filled, avg, placed_by (API or EXCHANGE). UNIQUE `(account, tag)`.
- `p_fills`: one row per filled level.
- `p_meta`: counters, RNG seed, last square-off.
- Positions are never stored. They are the signed sum of `p_fills`.

`dash/commands_dash.db` (only the dashboard writes it; journal_mode=DELETE)
- `commands(cmd_id, created_ts, requested_by, kind ∈ {PAUSE, RESUME, FLATTEN_ALL}, args, confirm_text, nonce UNIQUE)`.
- Insert-only.

`db/commands_cli.db` (only the CLI writes it)
- Same table. Additional kinds: KILL, ACK_HALT, ACK_ALERT, ACK_FOREIGN, SET_MODE, RUN_RECON.

How the bot reads commands:
- Polls both files every second, taking rows with `cmd_id > last_seen` for each source.
- Records the outcome in `command_acks` and publishes `pub/acks.json`.
- Never writes to either command file.

### 4.5 Derived counters
- Source of truth: SQL over existing rows. EngineState caches the results and recomputes them on every `Engine.load`. Nothing is stored separately.
- Per book, for today:
  - `entries`: structures with `opened_ts` set.
  - `attempts`: structures that have an ENTRY order.
  - `orders_today`.
  - `entry_orders_60s`.
  - `realized_net`: Σ `trades.net`.
  - `first_loss_today`.
- `cooldown_until(book, underlying)`: the last `closed_ts` plus a duration that depends on how the structure ended:
  - REENTRY_COOLDOWN_MIN if it CLOSED;
  - REJECT_COOLDOWN_MIN if it went VOID with a `REJECTED%` reason;
  - VOID_COOLDOWN_MIN for any other VOID.
- `live_sessions(strategy)`.

### 4.6 Event log and exports (R10)
- Every state transition writes one `events` row, inside the same transaction as the transition. Kinds are listed in DETAIL §4.6.
- At EOD the bot exports `exports/{events,trades}/YYYY-MM-DD.jsonl.gz`.

### 4.7 `pub/` — the only data the dashboard reads
- Writing: atomic (tmp file, then `os.replace`), mode 0640, owner `titan:titandash`.

| File | When written |
|---|---|
| `status.json` | every second |
| `trades_today.json` | on each close |
| `trades_90d.json` | at EOD |
| `trade/<id>.json` | on close (orders, fills, 1-min quote path) |
| `events_tail.json` | every 2 s (last 500 events) |
| `g1.json` | on change |
| `acks.json` | on change |

- `status.json` contents:
  - session, phase, heartbeat age, allow_live, entry halts;
  - feed: connected, age, warm;
  - per account: recon status and broker positions;
  - per book: realized, mtm (null when unknown), entries, open, daily-stop room, worst-case room, cooldowns;
  - positions: entry, bid/ask/age/src, rb, mtm, stop, lock, tp, time stop, MFE/MAE, state;
  - strategies: version, mode, pending_mode, and trial n / sessions / exp / lb / next_look.
- The full schema is in DETAIL §4.7.

## 5. Order lifecycle
### 5.1–5.2 LIMIT+IOC only; order state machine (`core/orders.py`)
- An order is inserted as SUBMITTING in the same transaction that returns `Place`. NEW is reserved for phase-2 baskets.
- An IOC order completes in about 1 s, so no working order is ever left behind.
- Any transition not listed below raises `IllegalTransition`. A test enumerates every pair.

SUBMITTING
| Event | Result |
|---|---|
| OK | WORKING; start the confirm timer at +3 s |
| REJECTED | REJECTED (see §5.9) |
| AMBIGUOUS | UNKNOWN; LookupTag at +1 s and +3 s (back off 1/2/4 s first if throttled) |
| FATAL | UNKNOWN; HALT(TOKEN, AUTO) + CRITICAL; look up again once the token works |

SUBMITTING or UNKNOWN + OrderSnapshot. If its tag or broker id matches (a WS update can arrive before PlaceResult), adopt it and apply it.

UNKNOWN
- Lookup found → adopt.
- Not found twice, and ≥ 3 s since submit → NOT_PLACED.
- API error → retry every 2 s. After 30 s: ORDER_STUCK + HALT(ORDER_STUCK, BOOK, ACK). Make no new attempt.

WORKING
- Snapshot with more filled → `apply_cum`.
- COMPLETE or CANCELLED → `apply_cum`, plus FetchTrades if filled > 0. A COMPLETE with filled ≠ qty raises a WARN; trust `filled_qty`.
- REJECTED requires filled = 0.
- At +3 s, poll every 1 s. At +6 s, Cancel → CANCEL_REQUESTED.

CANCEL_REQUESTED
- OK, BENIGN, or a terminal snapshot → poll until terminal.
- 10 s after the cancel → stuck: CRITICAL + HALT(ORDER_STUCK, BOOK, ACK); poll every 2 s. The leg stays EXITING, and the ladder resumes once the order resolves.

Terminal. A snapshot that disagrees → INVARIANT_VIOLATION event + poll. If P&L is affected, set it to UNRESOLVED.

`apply_cum(cum, avg)`
- cum < filled → violates I9.
- cum = filled → no-op.
- Otherwise insert `Fill("cum:<cum>", cum−prev, (avg·cum − prev_avg·prev)/(cum−prev))`. If that price is ≤ 0 or more than 20% from avg, use avg and WARN.
- Then set `leg.last_fill_*`.
- `TradesResult` adds `trade:<id>` rows, deduplicated by UNIQUE.

### 5.3 Structure and leg states (`core/positions.py`; phase 1 has one leg)
| From | Event | To / effect |
|---|---|---|
| — | pre-gates pass | RESERVED (holds the DB slot) |
| RESERVED | refused, TTL, or quote timeout | VOID with void_reason, then cooldown |
| RESERVED | plan accepted | ENTERING: create leg + ENTRY order, Subscribe(full), store decision_json |
| ENTERING | entry terminal, 0 filled | VOID |
| ENTERING | entry terminal, filled > 0 | OPEN: ExitParams from fills; stop = hard_sl; risk_basis = avg·filled; placebo trigger (PAPER); no top-up |
| OPEN | exit decision or forced flag | EXITING (first reason frozen) |
| EXITING | open_qty = 0 | VERIFY_FLAT |
| VERIFY_FLAT | post-fill broker net = 0 | CLOSED via `close()` |
| VERIFY_FLAT | residual confirmed | CLOSED + residual recorded + HALT(MISMATCH); residual is FOREIGN and never sold |
| OPEN / EXITING | recon EXTERNAL_CLOSE | CLOSED_EXTERNAL via `close_external()` |
| OPEN | PARTIAL_EXTERNAL | EXITING(ANOMALY_EXIT), capped |

Only `close()` and `close_external()` call `ledger.build_trade_row` (which uses `net_pnl`) and write `trades`. Both run inside the same transaction as the transition.

### 5.4 Entry flow (`core/gateway.py`)
1. IntentReceived. Validate the intent, insert it, route it (§10.5), and run gates G01–G13 per book. Write `intent_routes`.
2. Select the contract via `handler.select` (underlying LTP ≤ 2 s old, else fetched with the quotes). PAPER also selects the opposite contract for the placebo.
3. Fetch quotes: `FetchQuotes(intent_id, {option, opposite, underlying})`. Priority: 1 if any LIVE book, else 3, or 4 if SHADOW only.
4. On QuoteResult, for each RESERVED structure in the order LIVE, PAPER, SHADOW: run G14–G22, then `plan_entry`, then write leg + order + decision, then `Place`. Any failure → VOID.
5. One attempt per intent per book. No chase, no retry, no replay.

### 5.5 Exit loop (`core/exit_loop.py`; runs for each EXITING leg on every Timer and every relevant event)
```
working bot order -> return (I4); now >= STOP_SENDING -> CRITICAL once, return
open_qty == 0 -> verify_flat (§9.4); cap = broker_cap (§9.4) -> WAIT returns; qty = min(open, cap); 0 -> external_wait
fresh quote (<=1 s) else FetchQuotes(P0); none until STALE_PROTECTIVE_S -> protective pricing from last bid (band >= 3)
fresh but no bids -> NO_BID (WARN 10 s, CRITICAL from 15:20), re-quote 1/s, no order
now >= EOD_BACKSTOP -> band >= 3; place EXIT SELL IOC at exit_limit (§5.7)
terminal: band reject -> same band; under-filled -> band+1 (max 6); next try after max(250 ms, next fresh quote)
>=5 tries or 15 s -> EXIT_STUCK WARN; >=10 -> HALT(EXIT_DIFFICULTY, BOOK, AUTO) + CRITICAL; never give up before STOP_SENDING
```

### 5.6 MIS square-off hand-off
- At 15:25:00 (`STOP_SENDING`) the bot sends no more orders.
- At 15:26, Zerodha auto square-off runs [V]. It costs ₹50 + GST = ₹59 per order, which `net_pnl` includes.
- The 15:27 and 15:31 recons classify non-bot closing orders as BROKER_SQUAREOFF: CRITICAL alert, no MISMATCH.
- Paper simulates the same square-off.

### 5.7 Pricing (`core/exits.py`)
Entry BUY
- `limit = min(ask + 2·tick, floor_tick(mid·1.01))`.
- Refuse if limit < ask, or if the ask walk within the limit can't fill qty.

Exit SELL at band b
- `BANDS = (0, .005, .01, .02, .04, .08, .12)`.
- `ref_bid` = the fresh best bid. In protective mode it is the last bid, and `sweep = ref_bid`.
- `sweep` = the worst level needed to fill qty, or the lowest level shown.
- `limit = max(tick, floor_tick(min(sweep − 2·tick, ref_bid·(1−BANDS[b]))), ceil_tick(ref_bid·0.88))`.
- Starting band: 0 when fresh, 3 when protective or after the backstop.

Band rejects. A reject whose message matches `(?i)(price|lpp|band|range|circuit|protection|outside)` keeps the same band and re-prices at `max(prev_limit, floor_tick(fresh_bid − 2·tick))`. Three in a row → WARN.

COVER BUY: `min(ceil_tick(max(sweep_ask + 2·tick, ref_ask·(1+BANDS[b]))), floor_tick(ref_ask·1.12))`.

SELFTEST: `floor_tick(bid·0.97)`. It can never fill.

### 5.8 Error taxonomy (`broker/kite.py`)
| Kite outcome | Class |
|---|---|
| order_id returned | OK |
| InputException, OrderException | REJECTED |
| Timeout, ConnectionError, NetworkException (429 = throttled), General, Data, anything else | AMBIGUOUS → tag lookup |
| TokenException, PermissionException | FATAL |

- A cancel message matching `(?i)(complete|cancel|reject|not found|already)` is BENIGN.
- A failed read returns `ok=False`. Nothing is inferred from it.

### 5.9 Rejections
Entry rejects
- The structure goes VOID with `REJECTED:<msg>`, and a 10-minute cooldown starts.
- 3 rejects per book per session → HALT(REJECTS, ACK).
- A message matching `(?i)(block|not allowed|restricted|ban)` → HALT(CONTRACT_BLOCKED, SYMBOL, NEXT_SESSION), applied to all books.
- Margin or RMS messages → WARN.

Exit rejects follow §5.7.

### 5.10 Restart mid-order
| State at restart | Outcome |
|---|---|
| SUBMITTING, not sent | NOT_PLACED after 2 lookups |
| sent but unacknowledged | adopted by tag |
| WORKING | order_history |
| partial exit | resumes from fills |
| DB lost | tags imported (§9.6) |

## 6. Paper exchange (`broker/paper.py`)
Interface. Implements `BrokerPort` for PAPER and SHADOW:
- place, cancel, order_history, orders_today, order_trades, trades_today, positions, margins, contract_note.

It emits the same events as Kite, with `source=SIM`. Core contains no paper branches (arch test).

`place`
- Rejected (`SIM: …`) unless all hold:
  - qty > 0 and a multiple of lot;
  - limit is on the tick grid;
  - the order is LIMIT/IOC/MIS;
  - the symbol is known;
  - `|limit/ltp − 1| ≤ PAPER_PRICE_BAND_PCT`;
  - for a BUY, funds ≥ qty·limit + charges.
- On acceptance, assign id `P<yymmdd><n:09d>`, persist the order as OPEN with `due = now + L`, and emit WORKING.
- Latency `L ~ lognormal(median, σ)`, clipped to 80–3000 ms, from an RNG seeded with `sha256(session|account)`.

At due time
1. Take depth from `QuoteCache.fresh(sym, 1.0)` — the fill-time book. If it is missing, fetch REST and wait up to 1.5 s; still missing → CANCELLED with 0 filled.
2. `fill_from_depth(levels, side, qty, limit, lot, participation, adverse_ticks, tick)` — pure, and shared with FakeKite:
   - walk levels best first, stopping at the first level beyond the limit (a trade-through means no fill);
   - take `min(remaining, floor(level.qty·participation/lot)·lot)` per level;
   - fill at the level price ± adverse ticks.
3. All filled → COMPLETE. Otherwise CANCELLED with the partial fill. Write one `p_fills` row per level, persist, then emit.

Positions and margins
- Positions = signed Σ `p_fills`.
- Margins = capital + sells − buys − charges, for the day.

Startup
- An OPEN order whose due time has passed → CANCELLED, 0 filled.
- A leftover previous-session position → EXCHANGE square-off at ₹0.05 (conservative).

Auto square-off at `BROKER_SQUAREOFF_AT`. Each non-zero MIS position gets an EXCHANGE order tagged `SQOFF`. It walks all levels at participation 1.0; any remainder fills at the deepest level ×0.95 for a sell, ×1.05 for a buy.

Testing. `step(now)` runs inline.

X2 comparability
- Each order stores `decision_walk_px`.
- `slip_vs_walk` and the IOC fill rate are reported per mode and per twin pair (§10.9).
- Changing any `PAPER_*` value bumps `PAPER_MODEL_VERSION`, and therefore every strategy version.

## 7. Exit engine (`core/exits.py`, pure)
### 7.1 Levels frozen at the entry fill
With E = average entry, Q = qty, C_in = actual entry charges, R = RateSet:
- `k = R.stt_sell + (R.txn+R.sebi+R.ipft)(1+R.gst)` (0.00192043 at 2026-04 rates)
- `be = (E·Q + C_in + R.brokerage(1+R.gst)) / (Q(1−k))` — the exact net-zero price
- `hard_sl = E(1−sl%)`
- `arm = be + arm%·E`
- `floor = be + floor%·E`
- `tp = E(1+tp%)` (must be > arm)
- `time_stop_at = fill time + time_stop_min`

Nothing changes after entry. Gate G19 also refuses entry if `be_planned/limit − 1 > MAX_CHARGES_HURDLE_PCT`. Worked example: DETAIL §7.6.

### 7.2 Profile bounds (`EXIT_BOUNDS`; a strategy whose profile violates them is disabled)
| Parameter | Range |
|---|---|
| sl | 5–15 |
| arm | 2–10 |
| floor | 2 to arm − 0.5 |
| retain | 0.5–0.9 |
| tp | 8–50 |
| time stop | 10–120 min |
| confirm_ms | 0–1000 |
| confirm_max_ms | 500–3000 |
| bypass | 1–10% |
| no_bid_exit | 3–30 s |

Defaults (X4–X7): 15 / 4 / 3 / 0.70 / 15 / 45 min [U]; no time stop once locked.

### 7.3 Realizable bid (rb)
- rb = walk of the bids for `open_qty`.
- Any shortfall is valued at the deepest bid ×0.98 and flagged `thin`.
- No bids → None.
- LTP is used for logging only.

### 7.4 `decide(leg, q, refresh, now_mono, now_wall, p, eod, forced) -> NoAction | RefreshQuote | Exit(reason, urgency)` (plus marks)
The first matching rule wins:
```
0 forced (KILL_SWITCH|OPERATOR_FLATTEN|DAILY_STOP|ANOMALY_EXIT|RECOVERY_FLATTEN|SELFTEST_FILLED) -> Exit(PROTECTIVE)
1 now_wall >= EOD_FLATTEN -> Exit(EOD_FLATTEN, PROTECTIVE)
2 snapshot receipt age > 3 s: refresh failed or age > 5 s -> Exit(STALE_QUOTE_PROTECTIVE); else RefreshQuote. Stale quotes feed nothing below.
3 no bids: RefreshQuote first; no bids for no_bid_exit_s -> Exit(NO_BID_PROTECTIVE)
4 rb; update peak/trough (bid MFE/MAE + timestamps)
5 rb >= arm -> armed, stop = max(stop, floor); armed -> stop = max(stop, floor, be + retain·(peak − be))   # never lowers
6 rb <= stop (LOCK_STOP if armed else HARD_SL): rb <= stop − bypass%·E -> Exit(PROTECTIVE); else debounce: exit when a DISTINCT
  later snapshot (WS or REST) still breaches after confirm_ms; none by confirm_max_ms -> RefreshQuote; recovery clears marks
7 rb >= tp -> Exit(TAKE_PROFIT, NORMAL)
8 now >= time_stop_at and (not armed or time_stop_when_locked) -> Exit(TIME_STOP, NORMAL)
```
The engine never reverses, adds, averages down or exits partially. There are no greeks, TIE, velocity, conviction or OI rules.

### 7.5 Cadence
- Evaluates on QuotesDirty for held symbols and on the 250 ms Timer.
- `quote_checks` HOLD rows: at most 1/s per leg, plus one on every decision change.
- peak/trough/stop are flushed at most 1/s. Lock and exit events are persisted immediately.
- Post-exit tail: stay subscribed to full depth for 45 min, writing POST rows every 5 s, for offline exit-profile research.

## 8. Risk and sizing
### 8.1 Charges (`charges.py`, R3)
RateSet fields: version, effective_from, brokerage_per_order, stt_sell, stt_exercise, txn, sebi, ipft, stamp_buy, gst, sqoff_fee.

| RateSet | brokerage | stt_sell / stt_exercise | txn | sebi | ipft | stamp_buy | gst | sqoff_fee |
|---|---|---|---|---|---|---|---|---|
| "2024-10-01" | 20 | .001 / .00125 | .0003503 | 1e-6 | 0 | .00003 | .18 | 50 |
| "2026-04-01" [V] fixture | 20 | .0015 / .0015 | .0003553 | 1e-6 | 0 | .00003 | .18 | 50 |

`rates_for(d)` returns the RateSet in effect on date d.

`order_charges(OrderFills(side, filled_qty, value, external_kind), r)`
- An unfilled order costs 0.
- Brokerage is charged once per *executed* order.
- STT applies to SELL value; stamp to BUY value.
- `sqoff_fee` applies only to AUTO_SQUAREOFF.
- `gst = r.gst·(brokerage + txn + sebi + ipft + sqoff_fee)`.
- Each component is rounded to 2 dp.

`net_pnl(orders, r) -> PnL`: gross = sells − buys; net = gross − charges. Related: `mtm_net(done, open_qty, rb, r)`, `breakeven_px`, `round_trip_charges`, `exercise_stt`.

One P&L function for close, MTM, status, reports, the truth check and G1. G1 recomputes every sample with the current RateSet.

VCN. `get_virtual_contract_note` [V] runs daily on all LIVE orders and on up to 50 PAPER orders (synthetic ids; log once if unsupported). A difference above ₹1 per order → CHARGES_DRIFT WARN.

### 8.2 MTM and books
MTM
- Leg MTM = `mtm_net` with rb ≤ 3 s old.
- Book MTM is None (unknown) if any open leg lacks a fresh rb.
- Unknown MTM blocks entries and the daily-stop decision, never exits. Cached values are never reused.

Books
| Book | Account | Policy | Notes |
|---|---|---|---|
| `PAPER:<s>` | PAPER | FULL | G1 population |
| `LIVE:<s>` | LIVE | FULL + account guard | — |
| `SHADOW:<s>` | SHADOW | QUALITY | SHADOW mode and diverted intents |
| `PLACEBO:<s>` | SHADOW | QUALITY | placebos |
| `LIVE:_selftest` | LIVE | — | self-test |

QUALITY skips G09, G11, G12, G20 and G21.

### 8.3–8.4 Settings (`/var/lib/titan2/config/settings.json`; the full key table in DETAIL §8.4 is normative)
Loading
- The file is optional, read once per process, and immutable afterwards.
- Unknown keys, wrong types, NaN or inf, or a looser-than-bound value make the file invalid. The bot then runs on defaults, raises HALT(SETTINGS_INVALID, AUTO), and frozen exits are unaffected.
- `titan2 settings set` validates the value and writes it atomically. No UI can write settings.
- ✓ marks versioned keys. Changing one starts a new G1 trial (§10.1).

Defaults (tighten-only: a value may move only in the safer direction)

| Group | Keys and defaults |
|---|---|
| Risk ✓ | MAX_LOTS 1; MAX_UNITS 5000; MAX_LOSS_PER_TRADE_RS 5000; MAX_PREMIUM_PER_TRADE_RS 25000; MIN_PREMIUM_RS 20; STOP_SLIP_ALLOW_PCT 5; DAILY_STOP_RS 10000; MAX_WORST_CASE_DAY_LOSS_RS 40000; MAX_POSITIONS_PER_BOOK 2; MAX_ENTRIES_PER_DAY 2; MAX_ENTRY_ATTEMPTS_PER_DAY 6; MAX_ORDERS_PER_DAY_PER_BOOK 40; MAX_ENTRY_ORDERS_PER_MIN 3; cooldowns 30/5/10 min; STOP_AFTER_FIRST_LOSS false |
| Liquidity and freshness ✓ | LIQ_MAX_SPREAD_PCT 1.5; LIQ_DEPTH_MULT 3; LIQ_MAX_SLIP_VS_MID_PCT 1; LIQ_MAX_LAST_TRADE_AGE_S 60; MAX_CHARGES_HURDLE_PCT 1; QUOTE_MAX_AGE_ENTRY_S 1.5; UNDERLYING_MAX_AGE_S 2; STALE_REFRESH_S 3; STALE_PROTECTIVE_S 5 |
| Times ✓ | ENTRY_START 09:20; NO_NEW_AFTER 14:55; EOD_FLATTEN 15:15; EOD_BACKSTOP 15:22; STOP_SENDING 15:25; BROKER_SQUAREOFF_AT 15:26; MIN_TD_TO_EXPIRY index 2 / stock 5; stock allow/deny lists |
| Paper ✓ | PAPER_PARTICIPATION 0.5; latency 250 ms median, sigma 0.6; adverse 0 ticks; band 20%; book age 1 s; capital 500000 |
| Unversioned | feed gap 5 s / warm 60 s; recon 10 s / 30 s; LIVE_ACCOUNT_MAX_POSITIONS 2; LIVE_ACCOUNT_DAILY_STOP_RS 10000; first week 1 entry over 5 sessions; LIVE_DD_STOP_RS 15000; probation 20 trades / 6000; LIVE_MIN_MARGIN_RS 60000 |

Code constants
- G1_*; ENGINE_SEMANTICS_VERSION; PAPER_MODEL_VERSION; SCHEMA_VERSION; EXIT_BOUNDS; BANDS
- MAX_EXIT_DISCOUNT .12; THIN_BOOK_PENALTY .02
- Order timers: 3 / 6 / 10 s
- Grace 5 s; post-fill lag 2 s; confirm gap 3 s; positions wait 5 s; uncap 10 s; verify timeout 10 s
- Quote timeout 3 s; 30 intents per minute; post-exit tail 45 min
- sha256(SEMANTIC_CONSTANTS) is pinned in REGISTRY.lock. A change without an ENGINE version bump fails a test.

### 8.5 `size()` (`core/sizing.py`; the only sizing function; applied last; no multiplier)
```python
def size(inst, q, limit_px, profile, s, day_net, rates, policy) -> Sized | Refusal:
    if any(isinstance(c, bool) or not isinstance(c, (int, float)) or not math.isfinite(c) or c <= 0
           for c in (s.MAX_LOTS, s.MAX_UNITS, s.MAX_LOSS_PER_TRADE_RS, s.MAX_PREMIUM_PER_TRADE_RS)):
        return Refusal("SIZE", "INVALID_OR_ZERO_CAP")
    lot = inst.lot_size
    if not isinstance(lot, int) or lot <= 0: return Refusal("SIZE", "LOT_INVALID")
    if q.ask is None or min(q.ask, limit_px) < s.MIN_PREMIUM_RS: return Refusal("SIZE", "PREMIUM_FLOOR")
    per_lot_risk = lot*limit_px*(profile.hard_sl_pct + s.STOP_SLIP_ALLOW_PCT)/100 + round_trip_charges(limit_px, lot, rates)
    terms = {"RISK": floor(s.MAX_LOSS_PER_TRADE_RS/per_lot_risk),
             "DEPTH": floor(min(top_qty(q.asks, 3), top_qty(q.bids, 3))/(s.LIQ_DEPTH_MULT*lot)),   # entry AND exit side
             "LOTS": int(s.MAX_LOTS), "UNITS": floor(s.MAX_UNITS/lot), "PREMIUM": floor(s.MAX_PREMIUM_PER_TRADE_RS/(lot*limit_px))}
    if policy is Policy.FULL:
        if day_net is None: return Refusal("SIZE", "DAY_PNL_UNKNOWN")
        terms["DAY_BUDGET"] = floor((s.DAILY_STOP_RS + min(day_net, 0.0))/per_lot_risk)
    b = min(terms, key=terms.get)
    return Refusal("SIZE", b) if terms[b] < 1 else Sized(terms[b]*lot, terms[b], b, per_lot_risk)
```

### 8.6 Gates (`core/risk.py`)
- Each gate is pure: `gate(ctx, …) -> None | Refusal | Divert`.
- An exception becomes `Refusal(gate, "GATE_ERROR")`. Five within 5 minutes → HALT(GATE_ERRORS, ACK).
- `GateContext` is a frozen snapshot of:
  - now, session, settings, calendar, clock_synced, instruments_loaded;
  - ban_list (None means missing), blocked_symbols, feed, signals_degraded;
  - recon per account, halts, modes, running versions;
  - book: policy, open, reserved, entries, attempts, orders_today, entry_orders_60s, realized, mtm (or None), first_loss, cooldowns, active_underlyings;
  - live_account;
  - live flags: allow_live, unlocked, selftest_passed, demoted, first_week.

Pre-reserve gates, per routed book (QUALITY books skip G09, G11, G12)
| Gate | Refuses when |
|---|---|
| G01 STRATEGY | not exactly registered; version mismatch; mode doesn't route here; disabled |
| G02 STRUCTURE | no handler for the structure |
| G03 TTL | intent expired |
| G04 HALTS | any GLOBAL, ACCOUNT, BOOK, STRATEGY or UNDERLYING halt. If all blocking halts are operator halts and the book is `PAPER:<s>`, DIVERT to `SHADOW:<s>` instead (blocked_by set; session marked IMPAIRED) |
| G05 SESSION | not a session day; outside [ENTRY_START, NO_NEW_AFTER); clock unsynced; no instruments |
| G06 FEED | disconnected, tick older than 5 s, connected less than 60 s, or degraded |
| G07 RECON | no CLEAN recon within 30 s |
| G08 ELIGIBILITY | stock with no ban list loaded or on the ban list; outside the universe; on the deny list |
| G09 BOOK_CAPS | positions, entries (LIVE first 5 sessions: 1), attempts, orders/day, entry orders/min (also HALT RUNAWAY), or LIVE account positions at cap |
| G10 UNDERLYING | a non-terminal structure already exists on (book, underlying) or (LIVE, underlying) |
| G11 COOLDOWN | cooldown active |
| G12 DAY_RISK | MTM unknown; realized + MTM ≤ −DAILY_STOP_RS; first-loss stop triggered; or the same at the LIVE account level |
| G13 LIVE | LIVE book and any of: ALLOW_LIVE≠1, not unlocked, no self-test today, demoted |

Post-quote gates
| Gate | Refuses when |
|---|---|
| G14 TTL | intent expired |
| G15 CONTRACT | no expiry meets DTE; strike missing (never clamped); bad lot/tick; contract blocked |
| G16 RECHECK | underlying LTP older than 2 s or outside [lo, hi] |
| G17 QUOTE | quote missing or older than 1.5 s; a side missing; ask ≤ bid |
| G18 SIZE | `size()` refuses, or `entry_limit` returns None |
| G19 LIQUIDITY | spread > max(2 ticks, 1.5%·mid); top-3 depth on either side < 3·qty; walk short or average > mid·1.01; last trade older than 60 s or unknown; charges hurdle > 1% |
| G20 WORST_CASE (FULL) | `min(realized,0) − Σ_open(premium+charges+100) − (new premium+charges+100) < −MAX_WORST_CASE_DAY_LOSS_RS`, checked for the book and the LIVE account |
| G21 FUNDS (FULL) | margins older than 60 s, or `< 1.1·premium + charges` |
| G22 FINAL | TTL or halts fail when re-checked just before the order row is written |

### 8.7 Halts
Halts are sticky DB rows and never block exits. The first Timer of a new session clears all NEXT_SESSION halts.

| Halt | Scope | Cleared by |
|---|---|---|
| PAUSE (operator) | GLOBAL | RESUME |
| FLATTEN (operator) | GLOBAL | RESUME once flat |
| KILL (operator) | GLOBAL | ACK once flat |
| SAFE_FLAG (`--safe` or TITAN2_SAFE=1) | GLOBAL | ACK |
| DAILY_STOP | BOOK or ACCOUNT:LIVE | next session |
| LOSS_STREAK | BOOK | next session |
| CONTRACT_BLOCKED | SYMBOL | next session |
| LIVE_DISABLED_TODAY | ACCOUNT:LIVE | next session |
| MISMATCH | ACCOUNT | ACK, only while recon is CLEAN |
| UNKNOWN_POSITION | ACCOUNT | ACK_FOREIGN (exact) or ACK |
| SHORT_DETECTED, INVARIANT, EXCEPTION, GATE_ERRORS | GLOBAL | ACK |
| ORDER_STUCK, REJECTS, RUNAWAY | BOOK | ACK |
| EXIT_DIFFICULTY | BOOK | AUTO when the leg is flat |
| TOKEN | — | AUTO when `profile()` succeeds |
| STALE_FEED | — | AUTO after 60 s healthy |
| STALE_RECON | ACCOUNT | AUTO after 2 OK |
| SETTINGS_INVALID | — | next valid start |
| CLOCK_UNSYNCED, NO_INSTRUMENTS, V1_ACTIVE | — | AUTO |

### 8.8 Daily stop and worst-case cap
- Daily stop (D3) is checked on every MTM update. When realized + MTM ≤ −DAILY_STOP_RS:
  - HALT(DAILY_STOP, BOOK, NEXT_SESSION);
  - every open leg → EXITING(DAILY_STOP);
  - CRITICAL for LIVE, WARN for paper.
  - The same check runs at the LIVE account level against LIVE_ACCOUNT_DAILY_STOP_RS and flattens every LIVE book.
- Worst-case cap (G20) is the hard bound: even if every open position goes to zero with no bids, the day's loss stays ≤ ₹40k plus square-off fees.

### 8.9 Operator commands (`core/operator.py`)
| Command | Source | Effect |
|---|---|---|
| PAUSE / RESUME | dashboard or CLI | PAUSE is an operator halt; RESUME clears it (and clears FLATTEN once flat) |
| FLATTEN_ALL | dashboard (typed FLATTEN) or CLI | see below |
| KILL | CLI only (typed KILL) | FLATTEN_ALL with KILL_SWITCH + HALT(KILL, ACK) + demote every LIVE strategy (trial DEMOTED) |

FLATTEN_ALL:
1. HALT(FLATTEN).
2. Cancel and verify every ENTRY order.
3. Move every bot leg to EXITING(OPERATOR_FLATTEN).
4. Ack DONE when everything is CLOSED and post-fill snapshots confirm flat. Ack FAILED after 60 s (with CRITICAL), but keep trying.

Non-bot positions are listed and never touched. Operator halts also divert PAPER intents to SHADOW and mark the session IMPAIRED.

## 9. Reconciliation and recovery (`core/recon.py`)
### 9.1–9.2 Ownership and the post-fill rule
- Bot order: `parse_tag` succeeds and the order is in the DB. Today's T2 tags that are missing from the DB are imported during recovery.
- Bot position: its net is fully explained by bot fills.
- Foreign: everything else.
- External close: a non-bot order on a bot symbol.

Post-fill rule (guards against positions-API lag)
- A snapshot counts for a leg only if it is ok and `req_mono ≥ leg.last_fill_mono + 2 s`. After a restart, `last_fill_mono` is the process start time.
- Nothing else may cap an exit, verify flat, or declare EXTERNAL.
- Grace: a symbol is in grace while any of its bot orders is non-terminal, or for 5 s after a fill.
- Confirmation: two post-fill snapshots, at least 3 s apart and both outside grace, must agree.

### 9.3 Periodic recon
`reconcile(bot_view, positions, orders, memory, now) -> ReconResult` is pure.

Runs: every 10 s per account (09:00–15:31); every 5 s with open legs; on demand; at startup; at 15:27 and 15:31. Quantities are signed, per (symbol, product). This fixes v1's `abs()` at line 344.

| Class | Condition | Action |
|---|---|---|
| BROKER_ERROR | any API error, including 429 | infer nothing; 3 in a row → HALT(STALE_RECON) |
| CLEAN | broker = expected; no foreign position except exact acks | — |
| PENDING | seen once, or in grace | wait |
| REPAIRED | excess explained by bot-tagged orders' fills | apply them + FetchTrades |
| EXTERNAL_CLOSE | broker 0 < expected, confirmed | `close_external` (below) |
| PARTIAL_EXTERNAL | 0 < broker < expected | EXITING(ANOMALY_EXIT) capped at broker qty; HALT(MISMATCH) |
| EXCESS | unexplained broker > expected | HALT(UNKNOWN_POSITION); never traded |
| FOREIGN | non-bot net ≠ 0, or a non-MIS row | HALT(UNKNOWN_POSITION) unless an exact ack exists; a qty change re-halts |
| SHORT | NFO net < 0 | HALT(SHORT_DETECTED) + CRITICAL; auto-COVER only if 2 snapshots ≥2 s apart agree, bot sells − bot buys = \|short\| exactly, and the symbol is bot-owned today (then BUY exactly that qty via the cover loop); otherwise alert only |

`close_external`
- Fills come from non-bot SELL orders filled after the entry (via FetchTrades, or the cumulative average).
- pnl_status:
  - FINAL if those fills explain the missing qty;
  - ESTIMATED if inferred from the positions row;
  - UNRESOLVED otherwise (valued at the last rb).
- Classification:
  - BROKER_SQUAREOFF if the time is ≥ BROKER_SQUAREOFF_AT and the order is non-API or untagged [V] — CRITICAL, no halt;
  - otherwise MANUAL — HALT(MISMATCH, ACK) + CRITICAL.
- Paper uses the same code. In paper, any class other than CLEAN, PENDING or BROKER_SQUAREOFF counts as a G1 mismatch.

### 9.4 Exit cap, external wait, verify-flat
`broker_cap` uses the first available source:
1. post-fill positions net long (floored at 0);
2. otherwise FetchPositions(P0) and wait up to 5 s;
3. then a post-fill orders view: Σ BUY filled − Σ SELL filled for the symbol (MIS);
4. then wait up to 10 s;
5. then EXIT_UNCAPPED WARN and use the bot's own open_qty.

`external_wait` (cap 0 while open > 0): send no order and ask recon. Recon either confirms EXTERNAL_CLOSE, or a later snapshot shows the position again and the exit loop resumes.

`verify_flat` (open 0; ask for positions at last fill + 2 s)
- Post-fill net 0 → CLOSED.
- Net > 0, confirmed twice (≥3 s apart, ≥5 s after the fill) → CLOSED with a residual, HALT(MISMATCH) + CRITICAL. The residual is never sold.
- Net < 0 → SHORT path.
- Positions failing for more than 10 s → use the orders view: 0 → CLOSED; otherwise CRITICAL at 30 s.

Paper runs identical code.

### 9.5 Startup (`runtime.py`)
1. Logging. Take the flock (exit 73 if held). Assert a clean cwd (exit 78 if any `*.py` file is present), Python ≥ 3.10, and unpatched Kite.
2. Load settings. If invalid, use the defaults and raise a HALT.
3. Store: pre-migration backup, migrate, `integrity_check`. On failure, exit 78 with a direct CRITICAL alert.
4. Start the alerts thread.
5. Not a session day, or after 15:45: READY + STOPPING, exit 0. If non-terminal structures exist, run recovery and recon first.
6. Token: call `profile()`. On failure, HALT(TOKEN), re-check every 30 s, CRITICAL.
7. Build QuoteCache, InstrumentStore (from today's cache), the lanes (including the live lane), PaperExchange, the Feed, and `Engine.load`. Start the threads.
8. Recovery (§9.6) runs on REST quotes, then `READY=1` (target < 30 s).
9. Start the WS: underlyings in quote mode, held options in full mode.
10. Prep, once per day and cached: instruments, ban list, previous-session bars. Then the prestart self-check (§13); each failure only adds a halt.
11. `StartSignals`. Signals are ignored before 09:15.
12. Self-test at 09:15:30 (retry 09:17), only if a LIVE strategy exists.
13. EOD timeline (§12), then exit 0.

SIGTERM: stop entries; cancel ENTRY orders (≤ 10 s); let in-flight EXITs finish (≤ 10 s); commit; exit 0. It never flattens.

### 9.6 Recovery (phase RECOVERING)
For each account:
1. FetchOrders, FetchPositions and FetchTradebook (3 tries). If they still fail: HALT(STALE_RECON), and caps fall back to §9.4.
2. Match each non-terminal DB order by broker id or tag:
   - found → apply; if not yet terminal, follow the §5.1 timers;
   - SUBMITTING/UNKNOWN not found → look up twice, then NOT_PLACED;
   - anything else → INVARIANT + poll.
3. Import bot tags missing from the DB. Attach each to its (account, session, seq_no) leg; if there is none, create a `RECOVERED:<account>` structure (`_recovered`, default profile) + CRITICAL.
4. FetchTrades for LIVE terminal orders that lack TRADE fills.
5. Rebuild `open_qty` from fills. ENTERING → OPEN or VOID. RESERVED with no order → VOID. Intents are never replayed.
6. Run `reconcile` twice, at least 3 s apart, and apply the results.
7. Restore OPEN/EXITING legs: params, stop, lock, peak, reason.
   - Restart at or after EOD_FLATTEN → EXITING(RECOVERY_FLATTEN).
   - Leftovers from earlier sessions: LIVE → CLOSED_EXTERNAL UNRESOLVED + HALT(MISMATCH); paper → PaperExchange startup square-off.

Entries resume only when all hold: 2 consecutive CLEAN recons per account, feed warm, prep done, no entry halts.

### 9.7 Token rotation and WS backfill
- Token or permission error → HALT(TOKEN, AUTO) + CRITICAL (exits impossible).
  - Re-read `session/kite_session.json` every 30 s.
  - A newer token that passes `profile()` → `Exit(75)`; systemd restarts and recovery rebuilds Kite and the ticker.
  - The bot never logs in.
- WS reconnect:
  - FetchOrders(LIVE, P0) and apply it to every non-terminal bot order (backfill);
  - call `on_feed_reset()` on every strategy;
  - mark bars spanning the gap invalid.
- `on_noreconnect` → CRITICAL. Still down after 60 s → `Exit(75)`.

### 9.8 EOD truth check (15:33)
- LIVE: Σ `trades.net` must equal the broker tradebook recomputed with the same charges, within ₹1. Otherwise EOD_TRUTH_MISMATCH (CRITICAL). The VCN check also runs.
- Paper: the ledger must exactly equal the PaperExchange recompute. Otherwise it counts as a G1 mismatch.

## 10. Strategies, placebo, G1, LIVE
### 10.1–10.2 API, registry, version (`strategies/base.py`)
MarketView (read-only)
- `ltp(u) -> (px, age) | None`
- `last_tick(u)`
- `bars(u, n)` — today's live bars
- `prev_session_bars(u, n)` — historical bars
- `universe()`
- `feed_ok()`

Strategy (abstract base class)
- Class attributes: `NAME`, `LOGIC_VERSION`, `PARAMS`, `EXIT_PROFILE`, `UNIVERSE` (STOCKS | INDICES), `WINDOW`.
- Hooks: `on_tick`, `on_bar_close` and `on_timer(now, mv)` each return `Sequence[Intent]`; plus `on_feed_reset()`.
- Never reads the clock.
- If a hook raises, the strategy is disabled for the session and an alert is sent.
- At most 30 intents per minute.

`make_intent`
- Maps UP→LONG_CE and DOWN→LONG_PE.
- Validates `0 < ttl ≤ 10`, `trigger_px > 0`, and `lo ≤ hi`.
- Generates the id as `uuid4().hex[:12]`.

Version string
- `f"{LOGIC_VERSION}." + sha256(canonical_json({name, PARAMS, EXIT_PROFILE, settings.versioned_view(), ENGINE_SEMANTICS_VERSION, PAPER_MODEL_VERSION, SEMANTIC_HASH}))[:10]`.
- `REGISTRY.lock` pins each strategy's AST hash (docstrings stripped) and the semantic hash. A test fails if either changes without a version bump.
- A new version registers a new `g1_trials` row the first time it runs in PAPER. The previous version's trial becomes RETIRED.

### 10.3 `opening_drive` v1 (stocks, PAPER, brief E2a)
The 09:15 bar
- Built from WS ticks between 09:15:00 and 09:19:59.
- open = `ohlc.open`. high/low = LTP extremes, including the open. close = the last LTP before 09:20. volume = cumulative `volume_traded`, which includes the pre-open [V].
- Invalid if the feed dropped during the bar or no ticks arrived.

Signal
- `ret = close/open − 1`. UP if ret ≥ +2.5%, DOWN if ret ≤ −2.5%.
- Volume ≥ 3 × the mean of the previous session's last 20 historical bars. The window crosses the day boundary; with fewer than 20 bars there is no signal.
- Retrace limit (VSURGE): UP requires `(high − close)/(high − open) ≤ 0.65`; DOWN requires `(close − low)/(open − low) ≤ 0.65`.

Emission
- One batch at the first timer ≥ 09:20:00 (window closes 09:20:10), sorted by |ret| descending, at most one per symbol per day.
- Intent parameters: ITM1, `trigger_px = close`, recheck bounds UP [0.995, 1.010]·close and DOWN [0.990, 1.005]·close, ttl 10 s, default exits.

Notes
- Evidence is low-confidence: the BS model shows +2.27%, the DG analogue −0.24%.
- The backtested exits (TP8 / SL30 / BE+1%) violate X4 and X5, so they are not run. They can be evaluated offline from the bid-path tails (§7.5). Adopting them later means a new registered version, which counts toward the family size.

Bar check (15:36)
- Compares each live 09:15 bar against Kite's historical bar, for every symbol that fired plus 20 seeded samples.
- Tolerances: open and close within 0.1%, volume within 5%.
- G1 requires mismatched signal bars ≤ 10%.

### 10.4 `price_spike_up` v1 (stocks, PAPER)
Parameters
- Spike threshold: 1.0% until 09:45, then 0.6%.
- Baseline A: 60 s window. Baseline B: offset 30 s, usable once it is at least 5 s old.
- Volume confirmation: tick delta ≥ 0.5 × average over the last 50 deltas (needs at least 3).
- Sustain: 12 s. Hold ≥ 0.35%, or ≥ 1.0% before 09:55. Retrace ≤ 65%.
- Cooldown 90 s. Window 09:20–14:55. Strike ATM. TTL 10 s.

The 12 s sustain and 90 s cooldown are the values that actually ran in v1 (kite_ticker 1061/1077), not the 15/120 in the JSON.

Algorithm (per symbol)
1. `d = max(0, cum − prev)`. Push d to the history if d > 0.
2. Reset baseline A once it is older than 60 s. Create B 30 s after A, and reset B once it is older than 60 s.
3. If a sustain is pending, go to step 5. Skip the symbol if it is in cooldown or outside the window; nothing is queued.
4. `m = max(mA, mB)`, signed, so only UP counts. Trigger when m ≥ θ and volume confirms (fewer than 3 deltas, or avg = 0, or d ≥ 0.5·avg). Record {baseline, t0, trigger_px, peak}.
5. Track `held = (ltp − baseline)/baseline·100` (signed) and the peak. At t0 + 12 s the sustain passes only if all hold:
   - held ≥ max(0.35, 1.0 before 09:55);
   - held > 0;
   - 1 − held/peak ≤ 0.65.

   Pass or fail, the cooldown starts. On pass, emit `Intent(UP, ATM, Recheck(baseline·1.0035, ltp·1.005))`.

Never DOWN (E1), never flipped (E3), and no scorer, OI or ML (E4).

### 10.5–10.6 Modes, twin, placebo
Modes
| Mode | Effect |
|---|---|
| OFF | Applies immediately; the session is marked IMPAIRED |
| SHADOW | Intents go to `SHADOW:<s>` |
| PAPER | Intents go to `PAPER:<s>`; this is the G1 population |
| LIVE | Intents go to `LIVE:<s>` and to a `PAPER:<s>` twin, from one snapshot |

- Other mode changes take effect at the next session (`pending_mode`). Both strategies start in PAPER.
- `LIVE:<s>` caps mirror `PAPER:<s>`. If the account guard blocks a live entry, the refusal is recorded and the twin still trades. The report shows live coverage.

Placebo (R8)
- Every filled `PAPER:<s>` entry opens a placebo in `PLACEBO:<s>`.
- Same underlying LTP, strike rule, expiry and decision snapshot, but the opposite option type.
- QUALITY policy, same `decide()` and `net_pnl`.
- If refused, record `placebo_status = SKIPPED:<gate>`.

### 10.7 G1 (`g1.py`)
Constants
- MIN_TRADES 100; MIN_SESSIONS 30
- LOOKS (100, 150, 200, 300)
- ALPHA 0.05, one-sided (the brief's 90% CI); FAMILY_FLOOR 4
- SPREAD_STRESS 1.5
- Placebo: n ≥ 50, coverage ≥ 0.8
- IMPAIRED ≤ 0.10; BAR_MISMATCH ≤ 0.10
- CONFIRM_TRADES 25
- Futility: from n = 50, checked every 25 trades, α 0.10
- Certificate max age 7 days; bootstrap minimum 10k

Sample. PAPER trades of this version, non-placebo, entered since the trial was registered.

Per trade
- N = net, recomputed with the current RateSet.
- P = `risk_basis_rs`.
- C = max(hs_cost, crossing_cost, 0):
  - hs_cost = qty·(ask − bid)/2, at the entry decision and at each exit decision;
  - crossing_cost = Σ_entry (fill − mid)·q + Σ_exit (mid − fill)·q.
- Stressed return r = (N − 0.5·C)/P. Unstressed return u = N/P.

Multiple-testing control. m = max(#trials ever registered, 4); m never decreases. α_look = 0.05/(m·4).

Bootstrap
- Day-clustered: resample sessions with replacement and take Σr/count.
- B = max(10k, ⌈50/α⌉). Seed = `sha256(s|v|max_id|kind)`.
- LB = the ⌊α·B⌋-th order statistic.

Reports
- PROGRESS (nightly): informational only.
- LOOK (when n first reaches a look). PASS requires all of:
  - n ≥ 100, D ≥ 30, mean r > 0, LB > 0;
  - placebo n ≥ 50, coverage ≥ 0.8, mean u ≤ 0 at 1.0×;
  - zero PAPER mismatches;
  - zero UNRESOLVED or ESTIMATED trades;
  - IMPAIRED ≤ 10%;
  - bar mismatches ≤ 10% (opening_drive).

  PASS moves the trial to CONFIRMING. Failing the last look sets FAILED, mode SHADOW, and sends an alert.
- CONFIRM: the next 25 trades must have mean r > 0 with no mismatch. Then CERTIFIED, with `cert_report_id` and `inputs_hash`. Otherwise back to COLLECTING, or FAILED if no looks remain.
- FUTILITY: if the 90% upper bound of u is below 0, set FUTILE, mode SHADOW, and send an alert.

`inputs_hash` = sha256(strategy, version, rates_version, sorted (id, N, C), sorted placebo (id, N)).

Selection bias. A diverted operator-halt intent, or a heartbeat gap longer than 10 min during 09:20–14:55, marks the session IMPAIRED.

Power. Passing needs roughly +3% net per trade after stress. Evidence suggests −1.5% to 0%, so FUTILE or FAILED is the honest expected outcome (DETAIL §10.7).

### 10.8 LIVE unlock (two keys) and daily self-test
1. The trial reaches CERTIFIED and sends a `G1_CERTIFIED` alert with the hash.
2. Key 1: root sets `ALLOW_LIVE=1` in `/etc/titan2/titan2.env` and restarts the bot while flat.
3. Key 2: run `titan2 live <s> --confirm <hash> --i-accept-live-risk`. It is accepted only if:
   - the trial is CERTIFIED with that hash, and the certificate is ≤ 7 days old;
   - the process has ALLOW_LIVE;
   - there is no GLOBAL ACK halt;
   - the strategy has not been demoted.

   On acceptance: `pending_mode=LIVE` and `live_unlock_report_id` is recorded.
4. Self-test, in any session with a LIVE strategy, at 09:15:30 (retry 09:17):
   - `LIVE:_selftest` sends BUY IOC for 1 lot of the NIFTY ATM CE at `floor_tick(bid·0.97)`.
   - Expected: CANCELLED with 0 filled within 5 s.
   - If it fails: HALT(LIVE_DISABLED_TODAY) + CRITICAL.
   - If it fills: exit at once (SELFTEST_FILLED).
   - An egress IP mismatch also triggers LIVE_DISABLED_TODAY.

### 10.9 Live review (`g1.live_review`)
Runs after every LIVE close and nightly. A strategy is demoted when any of these occurs:
- Probation: cumulative net ≤ −₹6k within the first 20 trades.
- Drawdown: cumulative net ≤ −₹15k since unlock.
- Rolling-50: with ≥ 50 trades, the mean of the last 50 is below 0.
- X2 falsifier: after ≥ 20 twin pairs (same `intent_id`), the median (live − paper) `slip_vs_walk` exceeds 0.5 pp, or the live IOC fill rate is more than 10 pp below paper. This raises PAPER_MODEL_OPTIMISTIC and demotes all strategies; recalibrating bumps every version.
- Any LIVE mismatch, unknown position or short: halt and demote all.

On demotion the mode becomes PAPER immediately and the trial is set to DEMOTED. Open legs are still managed. Promotion again requires a new version and a fresh G1.

## 11. Instruments (`instruments.py`)
Dump
- At prep, fetch `instruments("NFO")` and `instruments("NSE")` and cache them under `cache/instruments/<date>/`. Only today's dump is ever used.
- Sanity checks:
  - NIFTY and BANKNIFTY CE/PE exist for ≥ 2 expiries;
  - lot, tick and strike are all > 0;
  - tokens exist for the stocks, NIFTY 50 and NIFTY BANK;
  - there are ≥ 100 stocks.
- On failure: HALT(NO_INSTRUMENTS) + CRITICAL.
- Leg rows store everything they need, so open legs never depend on the dump.

Indexes and names
- Indexes: symbol, token, (name, expiry, type) → strikes, and name → expiries.
- Stocks: `NSE:X ↔ X`. Indices: only `{NIFTY: "NSE:NIFTY 50", BANKNIFTY: "NSE:NIFTY BANK"}`.
- Lot size and tick always come from the contract row.

DTE
- `td(a, b)` counts the trading sessions in (a, b]. Use the first listed expiry with td ≥ MIN_TD (INDEX 2, STOCK 5).
- For indices, NIFTY weeklies roll on the Monday before a Tuesday expiry.
- For stocks, roll from E−4 to the next month, staying out of the physical-settlement window [V].
- No weekday is hardcoded, and positions are never held into expiry day.

Strike
- Spot is the underlying LTP, at most 2 s old.
- ATM = `argmin |k − spot|`; ties go to the more-ITM strike. ITM1 = the next lower strike for CE, the next higher for PE.
- A missing strike is a Refusal; there is no clamping (v1 clamped at options_trader 3529-3531).
- `opposite()` applies the same rule.

Eligibility
- Ban list: fetch `BAN_LIST_URL` (default `https://nsearchives.nseindia.com/content/fo/fo_secban.csv` [V]) with browser headers, up to 3 tries.
  - The file date must be today. A manual override can be placed at `config/ban_override/<date>.csv`.
  - If the list is missing, no stocks are eligible (WARN). Indices are unaffected. Paper and live apply the list identically.
- RMS block on a symbol → HALT(CONTRACT_BLOCKED, SYMBOL) for all books.

Sessions: `data/nse_sessions.json` = `{years: {YYYY: {holidays: [...], special_sessions: [{date, open, close, trade: false}]}}}` (from the NSE circular [V]).
- Weekends, holidays and special sessions (`trade: false`) are not traded.
- Coverage of less than 30 days → WARN. Dates beyond the coverage count as non-trading.

## 12. Dashboard and observability
Dashboard.
- Runs as `titandash`, bound to `127.0.0.1:8050`, reached through an SSH tunnel.
- Imports no kiteconnect, broker, core or store code.
- Reads only `pub/*.json`.
- Writes only `commands_dash.db`, through an authorizer that permits INSERT and SELECT only.
- Basic auth against a scrypt hash in `dash.env`; it refuses to start without one.
- Host must be `127.0.0.1:8050` or `localhost:8050`.
- POST needs `X-Titan-CSRF` (HMAC of date|user) and a unique nonce. FLATTEN_ALL also needs `confirm_text` set to `FLATTEN`.
- Routes:
  - `/`, which polls every 2 s;
  - `/api/status`, which shows "bot offline" when status is older than 10 s;
  - `/api/trades`, `/api/trade/<id>`, `/api/events`, `/api/g1`, `/api/acks`;
  - `POST /api/command` (PAUSE, RESUME or FLATTEN_ALL).
- It computes nothing. MTM is shown at the bid; LTP is labelled "info only".

Observability (R10): events; the fill ledger; `quote_checks` HOLD and POST rows; bid MFE/MAE; the 1-min path; `intent_routes`; G1 reports; logs.

EOD timeline
| Time | Action |
|---|---|
| 15:15 | flatten |
| 15:22 | backstop |
| 15:25 | STOP_SENDING |
| 15:26 | square-off |
| 15:27, 15:31 | recon |
| 15:33 | truth check + VCN |
| 15:36 | bar check |
| 15:38 | exports, daily report, nightly G1, live review, weekly prune |
| 15:40 | exit 0 |

Daily report (`reports/<date>.md` plus Telegram), per book and strategy: n, WR_net, Exp_net, worst trade, charges, exit mix, slippage, refusals by gate, halts, twin gaps, G1 progress, checks and latencies.

Alerts
- Telegram, deduplicated by key for 5 min. CRITICAL repeats every 5 min until ACK_ALERT.
- healthchecks.io is pinged at READY and every 60 s from 08:45 to 15:45, only while core progress is under 10 s old.
- The CRITICAL and WARN lists are in DETAIL §12.6.

## 13. Ops (DETAIL §13 is normative for layout, scripts, units, env, deploy, self-check, cutover and playbooks)
Server layout
- WorkingDirectory `/home/ubuntu/titan/agentic_trader/` contains only `titan2 -> ../current/titan2`.
- `current -> releases/<sha>`. Each release is immutable and has `venv -> ../../venvs/<lockhash>`.
- v1 is moved to `v1_frozen_<date>`.
- Secrets live in `/etc/titan2/{titan2.env, dash.env}`; state in `/var/lib/titan2/{db,dash,pub,session,config,...}`.

Users and permissions: `titan` runs the bot; `titandash` runs the dashboard and cannot read secrets or the session; `ubuntu` deploys and cannot write state or secrets.

`titan2-bot.service`
- `Type=notify`, no ExecStartPre.
- `Restart=on-failure`, `RestartPreventExitStatus=73 78`.
- `TimeoutStartSec=300`, `WatchdogSec=30`, an `OnFailure` alert.
- `ProtectHome=read-only`.
- Enabled, plus a Mon–Fri 08:45 timer. The bot exits 0 outside the session.
- Other units: token (08:30), selfcheck (08:40), dash, backup, alert@.

Token: only `titan2-token` logs in. It writes the session file atomically with mode 0600 and never restarts anything. `TOKEN_MODE=manual` is supported. The env file holds no access token and no trading mode.

Deploy (`ops/deploy.sh`)
1. Check the local tree is clean and the tests pass.
2. Abort if the manifest has drifted, if it is market hours and the bot is not flat, or if there is less than 1 GB free.
3. Create an immutable release.
4. Build the venv with `--require-hashes`.
5. Run the selfcheck deploy phase.
6. Switch the symlink atomically.
7. Restart only with `--restart`; roll back if READY does not arrive within 120 s.

`assert_owned()` restricts writes to `releases/`, `venvs/`, `incoming/`, `current*` and `titan2-*` units. Deploy never touches state, secrets or v1. CI runs tests only.

Cutover
- Stage 0, before any push to main:
  - disable the deploy workflow, delete `EC2_SSH_KEY`, and add `ci.yml`;
  - disable and mask `titan-bot` and `titan-dashboard`. Masking matters because kite_token_manager 312-330 restarts the bot;
  - clear the crontab;
  - remove nginx and close ports 80 and 5000;
  - archive v1;
  - rotate the leaked secrets;
  - set up an Elastic IP [V].
- Stage 1: bootstrap, install the env files, deploy, then cutover selfcheck.
- Stage 2: at least 10 PAPER sessions, with drills.
- Stage 3: G1.
- Stage 4: LIVE at 1 lot.

If the bot is down: use the Kite app. The 15:26 square-off is the final backstop.

## 14. Phase 2: multi-leg defined risk (designed now; built only if the research says viable; elaboration in DETAIL §14)
Where the code lives. Only new files: `titan2/multileg/`, `migrations/0002_multileg.sql` and `tests/multileg/`. `plugins.load_handlers()` calls `titan2.multileg.register(handlers, deps)` when the package exists.

The protocol phase 1 already uses. The gateway, exit evaluation and exit loop call only this interface (`core/single_leg.py`):
```python
class StructureHandler(Protocol):
    types: frozenset[StructureType]
    def select(self, intent, ctx) -> list[Instrument] | Refusal
    def quote_symbols(self, intent, legs, placebo: bool) -> tuple[str, ...]
    def plan_entry(self, intent, legs, quotes, ctx) -> EntryPlan | Refusal  # EntryPlan(legs=[LegPlan(inst, side, qty, limit_px, role)], risk_basis_rs, decision)
    def next_entry_orders(self, st, ctx) -> list[OrderSpec]                  # OrderSpec(leg_id, role, side, qty, limit_px, band_idx)
    def on_entry_progress(self, st, ctx) -> str                             # OPEN | VOID | UNWIND | WAIT
    def exit_params(self, st, ctx) -> ExitParams
    def evaluate(self, st, quotes, now_mono, now_wall, forced, ctx) -> Decision  # phase 1 delegates to exits.decide
    def exit_sequence(self, st) -> list[int]                                 # phase 2: shorts first
    def placebo_intent(self, intent, ctx) -> Intent | None
```
- PaperExchange funds checks go through `MarginModel`.
- Migration 0002 widens the CHECKs: SHORT legs, ENTRY SELL, EXIT BUY, UNWIND.

Structures. Credit put/call spreads, iron condor, debit spreads; indices only. After every fill, a short's qty must not exceed its hedging wing's filled qty, and the wing must be further OTM.

Entry
1. Size by max loss:
   - credit spread: W − credit;
   - debit spread: the debit paid;
   - iron condor: max(W) − credit.

   lots ≤ MAX_STRUCT_LOSS_RS / (lot·max_loss + charges), and also within the depth cap, the day budget and MAX_LOTS.
2. `basket_order_margins` must be ≤ 0.8 × available margin. If the call fails, refuse [V].
3. Buy the wings first, then sell the shorts, never more than the filled hedge.
4. Leg timeout: 5 s or 3 tries, then unwind.

Exit
- Close the shorts first; sell the wings only after the broker confirms the shorts are flat.
- Structure value: DTC = Σ ask-walk(shorts) − Σ bid-walk(wings).
- Exit on SL at SL_FRAC·max_loss, TP at TP_FRAC·credit, the time stop, or EOD. These parameters come from the research [U].

Expiry and product
- No DTE below 2.
- Force-close by 14:30 on the day before expiry; never hold a short into expiry day.
- No stock structures; MIS only (NRML would need a review).
- An unexplained short triggers UNWIND.

G1. Returns are measured against max-loss risk. The placebo is a seeded random entry time for condors, and the opposite spread for verticals. Paper margin comes from the basket snapshot.

## 15. Deletion (R13)
- Whole v1 files are retired after porting; the brief's region-level deletes are superseded.
- Tag `v1-final` at b4694f3 before deleting.
- Only `git rm` tracked paths. Never touch untracked or ignored files (the local `.env`, `auto_pilot.py.bak_pre_simplify`, `gmm_calibration_data.jsonl`, `titan_state.db`) or `_*.py`.
- Keep the research data (`trade_ledger/`, `daily_summaries/`, `*_dump.json`, `speed_gate_log.jsonl`, `ml_models/{data,oi_snapshots}/`), `CHANGELOG.md` (appended) and `TITAN_INSTRUCTIONS.md` (rewritten). The v1 technical doc and playbook move to `docs/v1/`.
- The exact list is in the `deletion_list` field. Corrections to the brief are in DETAIL §15.
- 46 underscore scripts import deleted modules; run them from `git worktree add ../titan_v1 v1-final`.
- Order: green build → tag → golden capture → one deletion commit → Stage 0 → Stage 1 → PAPER.

## 16. Tests (R14)
Harness
- FakeClock.
- ScriptedMarket, which feeds a real QuoteCache.
- FakeKite: implements every KiteConnect method we use, plus WS updates; fills come from `fill_from_depth`.
- FaultInjector modes:
  - reject, band_reject;
  - timeout before or after accept, http_429, token_expired;
  - duplicate, out-of-order, dropped, or early WS updates;
  - cancel_race, stuck_open;
  - positions_lag(s), positions_contradict, positions or orders errors;
  - external_close, external_buy, broker_squareoff;
  - stale_depth.
- ScenarioRunner: the real Engine, Store, inline PaperExchange, `KiteBroker(FakeKite)` and inline lanes, stepped every 50 ms.

Dual run. Every scenario runs once on PAPER and once on LIVE. Both runs must issue identical commands (only the account letter differs) and reach identical P&L (R4).

Scenarios. The names in DETAIL §16.2–16.4 are normative:
- `k01`–`k12`: one per brief §4.1 killer.
- `pm_*`: postmortem cases: oversize, reverse churn, LTP P&L, both legs, phantom SL, stale quote, partial fills, wide spread, same path, paper fills.
- restart and lag cases, including `positions_lag_never_external` (≤ 8 s), `verify_flat_residual_never_sold`, `ws_reconnect_backfills_orders`, `token_rotation_controlled_restart` and `mis_squareoff_handoff_1525_1526`.

Chaos. 200 seeds in CI, 500 nightly. After every step:
- no unexplained short;
- open = Σ fills = broker;
- at most one working order per leg;
- every OPEN leg evaluated within 2 s;
- no entry while halted;
- CLOSED net = `net_pnl`;
- LIMIT/IOC/MIS only;
- never sell more than is open;
- paper ledger = engine ledger.

Architecture tests
- no v1 imports;
- only the Kite adapter places orders;
- no GTT, SL-M, MARKET or `multiplier` identifiers;
- dashboard and strategy imports isolated;
- no paper branches in core;
- store writes from core only;
- registry and semantic locks current;
- Python 3.10 syntax;
- importable on Windows.

Also
- unit tests per unit;
- golden fixtures from v1-final;
- ops scripts tested in temp dirs.

## 17. Risks, [V] checklist, questions
Risks (detail in DETAIL §17.1)
| Risk | Mitigation |
|---|---|
| Slow G1 | futility checks; SHADOW mode |
| Optimistic paper fills | 0.5 participation, 1.5× stress, twin, X2, probation |
| Liquidity gate blocks most 09:20 entries | Accepted as the honest outcome |
| LPP stalls | Re-price at the bid, with square-off as the backstop |
| Host death | Watchdog, dead-man, worst-case cap |
| API drift | Single adapter; self-test |
| Manual trades | Dedicated account (Q3) |

[V] checklist (record results in RUNBOOK)
1. Rates against a contract note (Q5).
2. 15:26 square-off timing and how those orders appear.
3. IOC on NFO.
4. `tag` in WS updates and in `orders()`.
5. VCN with synthetic ids.
6. Positions lag p95 (changing the 2 s rule bumps ENGINE).
7. Static IP.
8. Ban-list URL and the calendar.
9. Physical-settlement policy.
10. Pre-open volume in the 09:15 candle.
11. The margins `available` field.
12. EC2 Python version and permissions.
13. `basket_order_margins`.

Questions: see `open_questions`. Defaults apply until they are answered.
