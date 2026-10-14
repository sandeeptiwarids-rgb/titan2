# titan2 (Titan v2): instructions and architecture

> **Version** 2.0.0 (`titan2.__version__`) · **Updated** 2026-10-12 · **Mode** PAPER only: no strategy has proven an edge yet
> **Trades** NSE F&O options, intraday (MIS), long CE or long PE, 1 lot · **Broker** Zerodha Kite Connect, `kiteconnect==5.0.1`
> **Server** EC2, ssh alias `titan-bot`, WorkingDirectory `/home/ubuntu/titan/agentic_trader` · **Units** `titan2-bot`, `titan2-dash`, `titan2-token`, `titan2-selfcheck`, `titan2-backup`
> **Also read** [RUNBOOK](docs/RUNBOOK.md) (what to do, when) · [Laptop mode](docs/LAPTOP.md) (Windows, PAPER only) · [CHANGELOG](CHANGELOG.md) · [v1 archive](docs/v1/README.md)

This file is the single source of truth for how titan2 works and how to change it safely. Change it in the same commit
as the code it describes; `tests/unit/test_docs.py` fails when the settings table, the gates, the exit reasons or the
module map drift from the code.

If you are an AI assistant working in this repo: read [§2](#2-rules-for-changing-titan2) before touching anything.

## 1. What titan2 is

titan2 replaces Titan v1, a 76K-line options bot that lost real money. The research behind it found no entry rule
with a positive edge after costs. So titan2 is built to do four things, in this order:

1. **Make engineering losses impossible.** No bad fills, no fake P&L, no state that disagrees with the broker, no
   oversize, no runaway loops, no order path outside the bot's core.
2. **Hard-cap every loss.** ₹15,000 per trade; ₹15,000 per day (realized plus open positions valued at the bid);
   ₹50,000 worst case, even if every open option went to zero.
3. **Win more often with cost-aware exits.** Every exit decision uses the price you could actually sell at, after
   charges.
4. **Prove before paying.** Strategies trade on PAPER, with fills as realistic as live, until the statistical gate
   **G1** shows a real edge. Then two separate human keys are still needed for LIVE.

The honest expected outcome is that no strategy ever reaches LIVE. That is a success: the system measured honestly
and lost nothing.

**Plain-English terms used everywhere**

| Term | Meaning |
|---|---|
| realizable bid (rb) | What selling the whole position into the current bids would fetch right now (walking down the bid levels). LTP is logged, never used for money decisions |
| breakeven (BE) | The exit price at which net P&L after every charge is exactly zero |
| intent | A strategy's proposal ("buy a CE on RELIANCE"). It carries no quantity: only the engine sizes |
| book | A ledger per account and strategy: `PAPER:<s>`, `LIVE:<s>`, `SHADOW:<s>`, `PLACEBO:<s>` |
| halt | A sticky database row that blocks new entries in some scope. A halt never blocks an exit |
| IOC | Immediate-or-cancel: the exchange fills what it can within about a second and cancels the rest, so nothing is ever left resting |
| fail closed | Missing, stale or strange input refuses the entry. It never blocks an exit |
| G1 | The paper-to-live statistical gate (§11). G01–G22 are different things: the entry checks (§6) |
| [V] | A fact still to verify against the live broker during burn-in ([RUNBOOK §10](docs/RUNBOOK.md#10-v-verification-checklist-and-results)) |

**The twelve guarantees**

| # | Guarantee | How |
|---|---|---|
| 1 | Only the core decides orders | One core thread decides; only `titan2/broker/kite.py` calls Kite order endpoints; the dashboard has no credentials |
| 2 | A position exists only for broker-confirmed fills | Quantity is derived from fills; an OPEN status is not a fill |
| 3 | Never sell more than the broker holds or than the bot's own fills explain | Exit qty = min(own open qty, post-fill broker net); verify-flat never sells a residual |
| 4 | P&L is booked once, from fills | `charges.net_pnl` is the only P&L function; the `trades` row is immutable |
| 5 | Stops use a bid at most 3 s old | Else a REST refresh; else a protective exit at 5 s |
| 6 | 1 lot, never more than both sides of the book can absorb | One `size()`; an invalid or zero cap refuses |
| 7 | The day's loss is hard-capped | Worst-case gate (₹50k) plus the sticky daily stop (₹15k) |
| 8 | When in doubt, stop entering and keep exiting | Gates fail closed; halts never block exits |
| 9 | A restart never double-enters and always resumes exits | Write-ahead order rows, tag lookup, exits resume before READY |
| 10 | Paper is as realistic as live, and checked against it | Fill-time depth at 50% participation, latency, own ledger, live twin, X2 falsifier |
| 11 | LIVE is impossible without G1 and two human keys | `ALLOW_LIVE=1` + a CLI unlock bound to the G1 report hash + a daily self-test |
| 12 | A deploy cannot clobber state, secrets or server edits | Separate OS users, immutable releases, drift check, no CI deploy |

Not guaranteed: positive expectancy; fills during exchange or broker outages; a host dying in the minutes just before
the broker's MIS auto square-off.

## 2. Rules for changing titan2

Non-negotiable. Most are enforced by tests (`tests/arch/`, `tests/unit/`).

1. **One order path.** Only `titan2/broker/kite.py` references `place_order`/`cancel_order`/`modify_order`, and only
   the core thread decides orders. No script, UI, CLI verb or second process may place, modify or cancel an order.
   If the bot is down, the human uses the Kite app.
2. **Only LIMIT + IOC + MIS.** No MARKET, SL, SL-M, GTT or order modification, anywhere.
3. **P&L only from fills plus exact charges**, through `charges.net_pnl`. Never from LTP.
4. **Exits judge the realizable bid** of a quote at most 3 s old (receipt time), never the LTP.
5. **Fail closed for entries; never block an exit.** No silent `except: pass` on an order, money or state path.
6. **Settings are read once and tighten-only.** Loosening a bound is a code change with review. Nothing hot-reloads.
7. **Every behaviour change is a new strategy version.** Changing strategy code, its exit profile, a versioned setting
   or a semantic constant gives a new version and so a new G1 trial. Bump `LOGIC_VERSION` (strategy code) or
   `ENGINE_SEMANTICS_VERSION` (anything in `SEMANTIC_CONSTANTS`), then refresh the lock (§15). No silent tuning.
8. **Never relax a threshold to manufacture trades.** Quality over quantity; a quiet day is a correct day.
9. **Paper = live.** No `if paper` branch in core code: only the broker adapter differs.
10. **Port, never import v1.** Ported code carries `# Ported from agentic_trader/<file>:<lines> @b4694f3 — changes: …`.
11. **Strategies are pure.** They import only `titan2.strategies.base`, `titan2.model` and the stdlib, never read the
    clock and never size.
12. **Python ≥ 3.10**, without `tomllib`, `typing.Self`, `datetime.UTC` or `ExceptionGroup`. Every module except
    `titan2/runtime.py` and `titan2/market/feed.py` must import on Windows.
13. **Never hardcode a market fact.** Session hours come from `titan2/data/nse_sessions.json`; lot, tick, strikes and
    expiries from today's instruments dump; charges from the dated RateSets in `titan2/charges.py`.
14. **Secrets stay out of git, logs, alerts and the dashboard.** Only the `titan2-token` unit logs in to Kite.
15. **User rules:** never modify a `_*.py` research script; leave `.kite_access_token` and `dhan_config.json` alone
    (their cleanup is deferred; rotating the secrets is what made them harmless).
16. **Deploy only with `ops/deploy.sh`.** Never copy files into the server tree or edit code on the server.
17. **Docs move with code.** Update this file, `docs/RUNBOOK.md` and `CHANGELOG.md` in the same change.

## 3. Market facts

Verified 2026-10-10 from NSE, SEBI and Zerodha sources. titan2 reads each of them from data, never from a constant.

| Fact | Value | Where titan2 gets it |
|---|---|---|
| F&O trading hours | 09:15–15:40 IST since 2026-08-03 (was 15:30). Derivatives close = VWAP of 15:10–15:40; cash closing auction for F&O stocks 15:15–15:35 | `regular_hours` in `titan2/data/nse_sessions.json` → `SessionCalendar` |
| Opening option quotes | Options have no pre-open; quotes in the first seconds after 09:15 are stale carry-overs | No entry before 09:20 (`ENTRY_START` cannot be earlier); the paper exchange never fills against a quote stamped before 09:15:30 (`OPENING_QUOTE_GRACE_S`) |
| Zerodha MIS auto square-off | 15:26 assumed **[V]**: unverified since the 15:40 close | Setting `BROKER_SQUAREOFF_AT` (15:20–15:39, any minute before the 15:40 close). titan2 flattens at 15:15 and stops sending orders at 15:25, always before it |
| Lot sizes | NIFTY 65, BANKNIFTY 30; revised twice a year | Each contract's own row in today's instruments dump |
| Expiries | NIFTY weekly on Tuesday; NIFTY monthly and BANKNIFTY (monthly only) on the last Tuesday; a holiday moves an expiry to the previous trading day; no NIFTY weekly in a monthly-expiry week | The dump's expiry list, never weekday arithmetic |
| Freeze quantity | NIFTY 1,800 and BANKNIFTY 600 units per order (from 2026-09-01) | Irrelevant at 1 lot; phase 2 must split larger orders |
| Charges (from 2026-04-01) | Brokerage ₹20 per executed order; STT 0.15% on sell premium and on exercise; NSE transaction 0.03553%; SEBI ₹10/crore; stamp 0.003% on buys; GST 18% on brokerage + transaction + SEBI (+ IPFT + square-off fee); MIS auto square-off ₹50 + GST per order | RateSet `2026-04-01` in `titan2/charges.py` **[V]**: pin from a real contract note |
| SEBI retail-algo rules | At most 10 orders per second per client; LIVE needs a registered static IP | Order bucket 8/s (burst 3, never more than 8 in any second); the `egress_ip` self-check disables LIVE for the day when `ALLOW_LIVE=1` comes without `REGISTERED_EGRESS_IP`, or when the egress IP differs from it |
| Trading holidays | NSE F&O holiday circulars | `titan2/data/nse_sessions.json` covers 2025–2026. Dates beyond it count as non-trading, so add 2027 before December 2026 |

## 4. Architecture

Picture one "brain" thread that makes every decision, surrounded by workers that only talk to the network and report
back. The brain never waits on the network; the workers never decide anything.

**Processes**

| Process | OS user | What it may do |
|---|---|---|
| `titan2-bot` | `titan` | The only Kite session holder and the only writer of trading state. Single instance: `flock` on `/var/lib/titan2/run/titan2.lock`, else exit 73 |
| `titan2-dash` | `titandash` | Reads `pub/*.json` only; inserts PAUSE, RESUME and FLATTEN_ALL into its own `commands_dash.db`. No credentials |
| `titan2-token`, `titan2-selfcheck`, `titan2-backup` | `titan` | Oneshot timers: log in at 08:30, morning check at 08:40, backups |
| CLI `titan2 …` | `titan` (via `sudo -u titan`) | Inserts commands into `commands_cli.db` and edits `settings.json`. Never writes `titan2.db` |

**Threads inside `titan2-bot`**

| Thread | Job |
|---|---|
| `core` | Runs `Engine.handle(event)` for each event inside one SQLite transaction, then dispatches the resulting commands. Timer every 250 ms; reads the command DBs every 1 s; writes `pub/status.json` every 1 s; systemd watchdog |
| `kite-ws` | KiteTicker (Twisted). Puts quotes in the `QuoteCache`, feeds underlying ticks to `signals`, marks held options dirty, turns order updates into events. Never blocks, never decides |
| `signals` | Builds bars and runs the strategies; posts intents |
| `lane-live-orders` | LIVE place, cancel, order history and trades: one call at a time |
| `lane-reads` | Positions, orders, trades, margins, historical bars, the contract note (LIVE orders and the PAPER sample) |
| `lane-reads-quotes` | REST quotes (batched, up to 500 symbols per call, EXIT first), on their own thread so a slow positions or orders call never holds back an exit refresh |
| `prep` | The daily prep: instruments dump, F&O ban list, previous-session bars (about 200 historical calls), kept off `lane-reads` so exit reads never wait behind it |
| `selfcheck` | The prestart self-check, run once after prep |
| `paper-sim` | The paper exchange for the PAPER and SHADOW accounts |
| `alerts` | Telegram and the healthchecks.io dead-man ping |

**The core loop** (`titan2/runtime.py`): an event comes in, `Engine.handle` changes state inside one transaction
(`BEGIN IMMEDIATE` … `COMMIT`, `synchronous=FULL`), and only after the commit are its commands sent. So an order row
(state SUBMITTING) is on disk before its HTTP request leaves: a crash can never forget an order it sent. On an
exception the transaction rolls back, the engine reloads from the DB and raises a halt; more than 20 exceptions in a
minute exits with code 3 and systemd restarts it into recovery.

**One writer per file**

| File | Only writer |
|---|---|
| `db/titan2.db` | core thread |
| `db/paper_exchange.db` | paper-sim thread |
| `dash/commands_dash.db` | dashboard |
| `db/commands_cli.db` | CLI |
| `pub/*.json` | core thread |

**Paper = live.** The engine never knows which broker answers it. LIVE commands go to `KiteBroker`; PAPER and SHADOW
commands go to `PaperExchange`, which answers with the same events (source `SIM`). Every scenario test runs on both
and must produce the same commands and the same P&L.

**How paper fills work** (`titan2/broker/paper.py`). An accepted order waits a random latency (lognormal, median
250 ms, clipped to 80–3000 ms, seeded per session), then fills against the order book *at that moment*, never the
decision book and never the LTP: only a book received after the order was sent counts (else one REST depth
request), and never a crossed or locked one, or one the exchange stamped in an earlier second than the quote the
order was decided on (a late WebSocket packet; the engine sends that quote's time with the order). It walks the
levels best first and earns 50% of the quantity walked so far: the whole lots for sure, and the leftover fraction of
a lot with that probability, by a draw seeded per order (paper model 2: on average it fills exactly 50% of the
walked depth, and a replay fills the same), each lot priced at the level that completes it. So a lone 1-lot bid or
offer inside the limit fills with probability 0.5 (`PAPER_PARTICIPATION`), about half the time, where a LIVE IOC
usually takes it: a stop into a lone bid in a gap can still cost paper less than LIVE. It stops at the first level
beyond the limit and cancels the rest (IOC).
No usable book within 1.5 s means CANCELLED (`SIM: no fresh depth`, `SIM: crossed or locked book` or
`SIM: book older than the decision`; the daily report counts them), and a quote stamped before 09:15:30 is never
filled against. Broker reads pay a round trip too (decision B1): a positions or orders read is answered only after
its own seeded delay (lognormal, median 250 ms, p95 about 1 s, clipped to 80–3000 ms; `PAPER_READ_LATENCY_*`), one
queue per account, and shows the ledger at that moment, so an exit that waits for its post-decision positions read
costs paper what it costs LIVE. Orders and fills live in the paper exchange's own `paper_exchange.db`. The PAPER
account has one pool of `PAPER_CAPITAL_RS`, like the one LIVE account; on SHADOW every book (`SHADOW:<s>`,
`PLACEBO:<s>`) has its own, so one book can never starve another. The broker's MIS square-off is simulated at
`BROKER_SQUAREOFF_AT` (it takes the whole book, and any remainder fills 5% worse than the deepest level; a book that
stays crossed or locked for 1.5 s is taken at its worse side, a sell never above the best ask), and a position left
from an earlier session is squared off at ₹0.05 at startup: the worst case, on purpose.

**Rate limits** (shared, blocking, never fail open): orders 8/s burst 3; quotes 1 call/s; historical 3/s; others 8/s.
Reads are served by priority: EXIT > ENTRY_LIVE > RECON > PAPER > SHADOW > BACKGROUND. A quote request that waits
3 s fails with TIMEOUT.

**Runtime invariants**, checked after every event (a violation halts entries; exits continue)

| ID | Invariant |
|---|---|
| I1 | Trading state changes only on the core thread |
| I2 | Kite order endpoints are called only from `lane-live-orders` |
| I3 | 0 ≤ open qty = entry fills − exit fills ≤ entry fills |
| I4 | At most one working bot order per leg (DB index) |
| I5 | At most one live structure per (book, underlying) and per (LIVE account, underlying) (DB index) |
| I6 | An exit never asks for more than the open qty and the broker cap |
| I7 | ENTRY, COVER and SELFTEST buy; EXIT sells (DB check) |
| I8 | Every OPEN leg has frozen exit levels with 0 < stop < entry < target and floor > breakeven |
| I9 | Filled qty only grows; a finished order never changes (DB triggers) |
| I10 | P&L is booked once per structure (DB key and trigger) |
| I11 | FULL books stay within their position and entry caps (RECOVERED books excepted: recovery fills them) |
| I12 | Order qty is a whole number of lots and within every cap |
| I13 | Prices sit on the tick grid; entry ≤ 1% over mid; exit never more than 12% under the bid |
| I14 | No LIVE ENTRY or SELFTEST order without `ALLOW_LIVE=1` (and, for ENTRY, the unlock and today's self-test) |
| I15 | The wall clock does not jump more than 2 s against the monotonic clock (WARN only) |
| I16 | Settings stay within their limits |

## 5. Module map

All code lives in `titan2/` under `agentic_trader/`: about 23K lines of heavily commented Python. Each module's
docstring is its detailed reference.

| Path | Job |
|---|---|
| `titan2/__init__.py` | `__version__` |
| `titan2/__main__.py` | The CLI: `python -m titan2 <verb>` (§14) |
| `titan2/runtime.py` | The bot process: threads (including the signals host and the prep worker), queues, flock, sd_notify, startup, the core loop, command routing, SIGTERM |
| `titan2/plugins.py` | Builds the structure-handler map; loads `titan2.multileg` if it exists (phase 2) |
| `titan2/clock.py` | Real and fake clocks, IST helpers, `SessionCalendar` |
| `titan2/settings.py` | The frozen `Settings`, `LIMITS` (bounds, tighten direction, versioned flag), code constants, `SEMANTIC_CONSTANTS` |
| `titan2/model.py` | Shared types: enums, quotes, orders, legs, structures, the depth `walk()`, tick math, order tags |
| `titan2/charges.py` | Dated Zerodha RateSets and the single P&L function `net_pnl` (plus `mtm_net`, `breakeven_px`) |
| `titan2/instruments.py` | Today's instruments dump, expiry (DTE) rule, strike selection, eligibility, F&O ban list |
| `titan2/store.py` | `titan2.db`: connection, migrations, transactions, typed queries, derived counters, pruning |
| `titan2/migrations/0001_init.sql` | The schema, including the constraints that enforce invariants |
| `titan2/data/nse_sessions.json` | Trading hours, holidays and special sessions |
| `titan2/ledger.py` | The immutable trade row, the daily report, the `pub/` and export files |
| `titan2/g1.py` | G1 trials, looks, bootstrap, placebo, futility, certificate, live review and demotion |
| `titan2/alerts.py` | Telegram, healthchecks dead-man, dedup and repeating CRITICAL alerts |
| `titan2/token.py` | The Kite session file, headless TOTP login, manual request-token exchange |
| `titan2/selfcheck.py` | Self-check phases: morning, prestart, deploy, cutover, and laptop (its own rows, `LAPTOP_CHECKS`) |
| `titan2/laptop.py` | Laptop mode (Windows, PAPER only, §14): the profile, the Windows paths, keep-awake and power, the clock and network probes, the two-stage Ctrl+C and the day runner behind `titan2 laptop` |
| `titan2/net.py` | `force_ipv4()`: every process's Kite REST calls go over IPv4 only (`TITAN2_IPV6=1` turns it off); `tcp_probe()` for the laptop's `network` self-check row |
| `titan2/condor.py` | `titan2 condor`: the PAPER NIFTY weekly 1.5-SD iron condor tracker (edge_research_verdict.md's paper spec). No order path: reads Kite quotes, day bars and basket margins, books paper fills, 3x-credit stop, expiry settlement and the kill rules in its own `db/condor.db`; the laptop runs it at 15:25 (`laptop.Supervisor`) |
| `titan2/whatif.py` | `titan2 whatif`: replays a session's refused entries (ENTRY_REFUSED events) on Kite minute bars under the strategy's exit rules, after charges; read-only, writes `reports/whatif-<date>.json` (research aid; the laptop runs it after each session) |
| `titan2/market/__init__.py` | Market-data package (imports nothing) |
| `titan2/market/quotes.py` | `QuoteCache`: newest snapshot per symbol; `fresh()` never returns a stale quote |
| `titan2/market/feed.py` | KiteTicker wrapper: subscriptions through `reactor.callFromThread`, ticks, order updates, feed health |
| `titan2/market/bars.py` | 5-minute bars from ticks, previous-session bars, the live-vs-historical bar check |
| `titan2/broker/__init__.py` | Broker package (imports nothing heavy) |
| `titan2/broker/port.py` | The `BrokerPort` / `DataPort` contracts and the result vocabulary |
| `titan2/broker/kite.py` | `KiteBroker` (LIVE orders, LIMIT/IOC/MIS only) and `KiteData` (reads only; one retry of a quote read whose connection dropped); error classes |
| `titan2/broker/lanes.py` | The order lane, the reads lane (batching, priority) and the token buckets |
| `titan2/broker/paper.py` | `PaperExchange` and `fill_from_depth()`: fills, own ledger, square-off simulation |
| `titan2/core/__init__.py` | Core package |
| `titan2/core/events.py` | The events into the core and the commands out of it |
| `titan2/core/state.py` | `EngineState` (RAM cache of the DB) and `EngineContext` |
| `titan2/core/engine.py` | `Engine.handle()`: dispatch, timers, EOD timeline, recovery, status, self-test |
| `titan2/core/operator.py` | Operator commands: PAUSE, RESUME, FLATTEN_ALL, KILL, acks, modes, recon |
| `titan2/core/invariants.py` | I1–I16 |
| `titan2/core/gateway.py` | Entry flow: routing, gates, the wait for pending entries, reservation, decision quote, order, live twin, placebo |
| `titan2/core/single_leg.py` | The `StructureHandler` protocol and the phase-1 single-leg handler |
| `titan2/core/exit_loop.py` | Exit attempts, broker cap, verify-flat, EOD backstop, STOP_SENDING, short cover |
| `titan2/core/exits.py` | Pure exit decisions and exact order prices |
| `titan2/core/sizing.py` | The single `size()` |
| `titan2/core/risk.py` | The 22 entry gates, MTM, daily stop, worst-case cap, halt rules |
| `titan2/core/orders.py` | Order state machine, fills from cumulative quantity, order timers |
| `titan2/core/positions.py` | Structure and leg state machine, `close()`, `close_external()` |
| `titan2/core/recon.py` | Pure reconciliation, the recovery planner, the EOD truth check |
| `titan2/strategies/__init__.py` | The strategy registry and the `REGISTRY.lock` helper |
| `titan2/strategies/base.py` | Strategy plug-in API, `MarketView`, `make_intent`, version hash |
| `titan2/strategies/opening_drive.py` | Strategy `opening_drive` v1 (§11) |
| `titan2/strategies/price_spike_up.py` | Strategy `price_spike_up` v1 (§11) |
| `titan2/strategies/price_spike_down.py` | Strategy `price_spike_down` v1 (§11): the PAPER put experiment, `price_spike_up`'s signed mirror |
| `titan2/strategies/REGISTRY.lock` | Pinned code hashes of each strategy (its module plus `strategies/base.py`) and of the semantic constants |
| `titan2/dash/__init__.py` | Dashboard package (imports nothing) |
| `titan2/dash/app.py` | The read-only dashboard plus three commands (Flask, served by gunicorn on EC2, by `serve()` on the laptop) |
| `titan2/dash/templates/index.html` | The dashboard page |

Import rules (enforced by `tests/arch/`): strategies import only `strategies.base`, `model` and the stdlib; core
imports no `kiteconnect`, `broker.kite`, `broker.paper`, `market.feed` or `dash`; the dashboard imports no
`kiteconnect`, `broker`, `core` or `store`; nothing imports a v1 module.

**"spec §N" in the code.** Docstrings and comments cite the build specification of 2026-10-10 (the normative
FINAL_SPEC and its DETAIL companion, which share one numbering). Those spec sections map to this file as follows:

| Spec § | Topic | Here |
|---|---|---|
| 0–2 | summary, architecture, modules | §1, §4, §5 |
| 3 | processes, threads, core loop, invariants | §4 |
| 4 | data model, events, DB, `pub/` | §4, §12 |
| 5 | order lifecycle and pricing | §6, §7 |
| 6 | paper exchange | §4 |
| 7 | exit engine | §7 |
| 8 | charges, MTM, settings, sizing, gates, halts, operator commands | §8, §9 |
| 9 | reconciliation, startup, recovery, token rotation, EOD truth check | §10 |
| 10 | strategies, placebo, G1, LIVE unlock, live review | §11 |
| 11 | instruments, expiries, strikes, eligibility, calendar | §3, §6 |
| 12 | dashboard, observability, EOD timeline, alerts | §12, §13, §14; RUNBOOK §5 |
| 13 | ops: layout, CLI, units, deploy, self-check, cutover, playbooks | §12, §14; RUNBOOK |
| 14 | phase 2 | §16 |
| 15 | v1 deletion | §17 |
| 16 | tests | §15 |
| 17 | risks and the [V] checklist | RUNBOOK §10 |

## 6. How a trade happens

1. **Signal.** A strategy on the `signals` thread emits an intent: underlying, direction (UP → long CE, DOWN →
   long PE), strike rule (ATM or ITM1), trigger price, a recheck band for the underlying, and a time-to-live of at most
   10 s.
2. **Routing.** The strategy's mode picks the books: PAPER → `PAPER:<s>`; SHADOW → `SHADOW:<s>`; LIVE →
   `LIVE:<s>` plus a `PAPER:<s>` twin from the same snapshot.
3. **Pre-reserve gates G01–G13**, per book. The first refusal is recorded in `intent_routes`. If all pass, the
   structure is RESERVED, which holds its database slot. If G09 refuses only because of entries still pending
   (reserved, or sent and not yet filled), the intent waits instead, holding nothing, for at most its TTL: on every
   Timer, and before any newer intent, it goes through every gate again, oldest first, and ends reserved, refused as
   before, or expired (INTENT_WAITING, then INTENT_ROUTED with waited_s). A woken intent that meets RUNAWAY is
   refused without the halt; a new one still raises it. A reserved entry's order counts toward the three entry
   orders a minute before it is sent (`ENTRY_ORDERS`, which waits like the other pending caps and never halts), so
   no batch, woken or new, sends a fourth inside the minute.
4. **Contract and quote.** Pick the contract from today's dump around the underlying LTP (at most 2 s old). One
   quote fetch per intent covers the option, the placebo's option and the underlying.
5. **Post-quote gates G14–G21**, then `size()`, then **G22** just before the order row is written. Any refusal
   voids the structure and starts a cooldown.
6. **Order.** One ENTRY BUY, LIMIT + IOC + MIS, inserted as SUBMITTING in the same transaction that sends it. One
   attempt per intent per book: no chase, no retry, no replay (waiting for a slot in step 3 defers that one attempt;
   it is not a retry). A `PAPER:<s>` entry opens its placebo in the same
   transaction, so both orders go out together, priced on the same snapshot (§11).
7. **Fill.** Whatever filled becomes the position. The exit levels are computed from the actual fills and frozen
   (§7). Nothing is topped up.
8. **Exit.** `decide()` watches every OPEN leg on each quote and every 250 ms. An exit moves the leg to EXITING and
   the exit loop sells, one IOC at a time, until the bot's open qty is 0.
9. **Verify flat.** Two broker position reads taken after the last fill must agree that the position is gone.
10. **Close.** `close()` books the one immutable `trades` row from fills and exact charges, inside the same
    transaction. The dashboard, the daily report and G1 all read that number.

**Entry gates** (pure functions in `titan2/core/risk.py`; any exception inside a gate refuses with `GATE_ERROR`)

| Gate | Name | Refuses the entry when | QUALITY books |
|---|---|---|---|
| G01 | `STRATEGY` | not exactly registered, version mismatch, mode does not route here, or disabled | checked |
| G02 | `STRUCTURE` | no handler for the structure type | checked |
| G03 | `TTL` | the intent expired | checked |
| G04 | `HALTS` | a halt blocks the scope. Operator halts divert a PAPER intent to SHADOW and mark the session IMPAIRED | GLOBAL/ACCOUNT only |
| G05 | `SESSION` | not a trading day; outside 09:20–14:55; clock not synced; no instruments | checked |
| G06 | `FEED` | feed disconnected, last tick older than 5 s (one received while the event runs is fresh), connected less than 60 s, or degraded | checked |
| G07 | `RECON` | no CLEAN reconciliation in the last 30 s | checked |
| G08 | `ELIGIBILITY` | stock without today's ban list, banned, deny-listed or outside the universe | checked |
| G09 | `BOOK_CAPS` | positions, entries, attempts, orders per day or entry orders per minute at cap (also HALT RUNAWAY, except for a woken intent); entry orders per minute at cap counting the reserved entries' orders still to come (`ENTRY_ORDERS`, no halt); LIVE account at cap. Only pending entries in the way: the intent waits (step 3) | skipped |
| G10 | `UNDERLYING` | a structure is already live on this (book, underlying) or (LIVE, underlying) | checked |
| G11 | `COOLDOWN` | cooldown active (30 min after a close, 10 after a reject, 5 after a void) | skipped |
| G12 | `DAY_RISK` | MTM unknown; realized + MTM at the daily stop; first-loss stop; same at LIVE account level | skipped |
| G13 | `LIVE` | LIVE book without `ALLOW_LIVE=1`, unlock, today's self-test, or after a demotion | n/a |
| G14 | `TTL` | expired again | checked |
| G15 | `CONTRACT` | no expiry meets the DTE rule; strike missing (never clamped); bad lot or tick; contract blocked | checked |
| G16 | `RECHECK` | underlying LTP older than 2 s (a tick received while the event runs is fresh) or outside the intent's recheck band | checked |
| G17 | `QUOTE` | option quote missing, older than 1.5 s, one-sided, or crossed | checked |
| G18 | `SIZE` | `size()` refuses or no valid entry limit | checked |
| G19 | `LIQUIDITY` | spread over max(2 ticks, 1.5% of mid); top-3 depth on either side under 3 × qty; the ask walk cannot fill within mid + 1%; last trade older than 60 s; charges hurdle over 1% | checked |
| G20 | `WORST_CASE` | if every open position and this one went to zero, the day's loss would pass ₹50,000 | skipped |
| G21 | `FUNDS` | margins older than 60 s, or below 1.1 × premium + charges | skipped |
| G22 | `FINAL` | TTL or halts fail when re-checked just before the order row is written | checked |

**Orders.** States: SUBMITTING → WORKING → COMPLETE / CANCELLED / REJECTED, with UNKNOWN when the broker's answer is
ambiguous: looked up by tag at +1, +3, +7 and +15 s, then every 5 s until 30 s after the answer; only a miss whose
orders() read was asked after that is NOT_PLACED (a read asked earlier but answered late, on a slow order lane, does
not count), and that moment counts as a possible fill for reconciliation. An engine reload after an error keeps
both: the window ends where the answer put it, and a Place still waiting for its answer gets a window too (30 s after
the latest normal answer). After a restart an UNKNOWN or SUBMITTING order gets the whole 30 s again, from the
restart (its answer may have died with the process). A bot BUY
that Kite takes even later is adopted into a `RECOVERED:<account>` structure and sold at once (CRITICAL `LATE_FILL`).
A working order is polled from +3 s and cancelled at +6 s; 10 s after a cancel it is ORDER_STUCK (book halted,
CRITICAL). Any transition not in the table raises. Every order carries a 15-character tag, `T2` + role (E/X/C/T) + account (L/P/S) + yymmdd + leg sequence (3) +
attempt (2), for example `T2EP26101200101`; every retry is a new attempt and so a new tag.

Kite errors are classified once: input or order exceptions → REJECTED; timeouts, network errors, HTTP 429 and
anything unknown → AMBIGUOUS (tag lookup); token or permission errors → FATAL (HALT TOKEN). Entry rejects void the
structure; three per book per session → HALT REJECTS; a message about a ban or block → CONTRACT_BLOCKED for the
rest of the session. A REST quote read whose connection dropped before any answer (an idle connection Kite closed:
reset, aborted or closed with no status line) is made once more at once, paced by the quote limit; nothing else is
retried there, not even a timeout while the request is being sent or a garbled answer.

## 7. Exits

**Levels are frozen at the entry fill** and never move afterwards except the ratcheting stop. With E = average entry
price, Q = filled qty, C_in = actual entry charges:

```
k        = STT on sell + (txn + SEBI + IPFT) × (1 + GST)              # 0.00192043 at 2026-04-01 rates
be       = (E·Q + C_in + brokerage × (1 + GST)) / (Q × (1 − k))      # exact net-zero exit price
hard_sl  = E × (1 − 15%)          arm   = be + 4% of E               floor = be + 3% of E
tp       = E × (1 + 15%)          time stop = entry fill time + 45 min (not once the lock is armed)
```

Worked example (2026-04-01 rates, E = ₹32.00, Q = 500, premium ₹16,000): entry charges ₹30.81, breakeven 32.17,
hard stop 27.20, lock arms at 33.45 with floor 33.13, target 36.80. At a peak rb of 35.00 the stop ratchets to
max(33.13, 32.17 + 0.70 × (35.00 − 32.17)) = 34.15. Exiting at the floor 33.13 nets about ₹479.

**`decide()` rules** (pure, in `titan2/core/exits.py`; the first match wins and the reason is frozen)

| # | Rule | Result |
|---|---|---|
| 0 | A forced flag: `KILL_SWITCH`, `OPERATOR_FLATTEN`, `DAILY_STOP`, `ANOMALY_EXIT`, `RECOVERY_FLATTEN`, `SELFTEST_FILLED` | exit, protective |
| 1 | Time ≥ EOD_FLATTEN (15:15) | `EOD_FLATTEN`, protective |
| 2 | Quote older than 3 s by receipt time: refresh over REST; failed or older than 5 s | `STALE_QUOTE_PROTECTIVE`; a stale quote feeds nothing below |
| 3 | No bids: refresh first; still none for 10 s | `NO_BID_PROTECTIVE` |
| 4 | Compute rb; update bid-based peak and trough (MFE/MAE) | — |
| 5 | rb ≥ arm → lock armed, stop = max(stop, floor). While armed: stop = max(stop, floor, be + 0.70 × (peak − be)) | the stop only rises |
| 6 | rb ≤ stop: deep breach (3% of E below) exits at once; otherwise confirm on a later, distinct snapshot after 400 ms (REST refresh at 1.5 s) | `LOCK_STOP` if armed, else `HARD_SL` |
| 7 | rb ≥ tp | `TAKE_PROFIT` |
| 8 | Time stop reached and lock not armed | `TIME_STOP` |

`decide()` never reverses, adds, averages down or exits partially; there are no greeks, OI, velocity or conviction
rules. Two more reasons come from reconciliation, not from `decide()`: `CLOSED_EXTERNAL` (someone else closed it) and
`BROKER_SQUAREOFF` (the broker's MIS square-off closed it).

**Exit pricing** (`exit_limit`): SELL at band b of `BANDS = 0, 0.5, 1, 2, 4, 8, 12%`:
`limit = max(tick, floor_tick(min(sweep − 2 ticks, bid × (1 − band))), ceil_tick(bid × 0.88))`, where sweep is the
deepest bid level needed to fill the qty. A fresh quote starts at band 0; protective pricing and anything after the
15:22 backstop start at band 3. An under-fill moves one band deeper (at most 12% under the bid); an exchange
price-band reject keeps the band and re-prices 2 ticks under the fresh bid, never below the previous limit. Entry BUY:
`min(ask + 2 ticks, floor_tick(mid × 1.01))`, refused if that is below the ask or the asks within it cannot fill the
qty.

**The exit loop** (`titan2/core/exit_loop.py`) sends one order at a time per leg, for min(own open qty, post-fill
broker net long). It never sends into an empty book, retries no sooner than 250 ms, a fresh quote and a positions
read asked after the previous try ended (decision B1, §10), and never gives up before STOP_SENDING (15:25): 5 tries
or 15 s → `EXIT_STUCK` WARN; 10 tries → HALT EXIT_DIFFICULTY + CRITICAL. After STOP_SENDING it sends nothing and the
broker's square-off is the backstop.

## 8. Risk, sizing and settings

**Three loss caps**

| Cap | Default | What happens |
|---|---|---|
| Per trade | ₹15,000 | `size()` refuses unless 1 lot × premium × (stop 15% + slippage allowance 5%) + round-trip charges fits |
| Per day, per book | ₹15,000 | When realized + MTM at the bid reaches −₹15,000: HALT DAILY_STOP until the next session and every open leg exits. With an open leg's MTM unknown it trips once the realized loss alone reaches −₹15,000. The LIVE account has its own ₹10,000 stop (`LIVE_ACCOUNT_DAILY_STOP_RS`), on the same rules |
| Worst case, per book | ₹50,000 | Gate G20: even if every open option went to zero with no bids, the day's loss stays within ₹50,000 (plus square-off fees) |

MTM uses only a realizable bid at most 3 s old. If any open leg lacks one, the book's MTM is *unknown*, which blocks
entries and the daily-stop decision but never an exit, unless the realized loss alone is already at the stop (then
the stop trips: an unknown MTM cannot undo a booked loss). A cached value is never reused.

**`size()`** (`titan2/core/sizing.py`) is the only sizing function and runs last. It takes the smallest of: the
per-trade risk budget; the depth on *both* sides of the book (the smaller of the top-3 bid and ask quantities priced
within `LIQ_MAX_SLIP_VS_MID_PCT` of the mid, divided by 3 lots), so the exit side can absorb the position too and
stub bids far under the touch never count; `MAX_LOTS` (1); `MAX_UNITS`; the premium cap; and the day's
remaining budget: what the daily stop leaves after the book's realized loss (a profit adds nothing) and after the
stop-outs of its open and entering positions, each measured from its entry: its planned stop-out from its sizing
plan, less what a locked or trailed stop has moved up, or what it has lost already when that is more. A position's
open profit neither adds room nor counts as more to lose. For a LIVE book the LIVE account's ₹10,000 stop caps it
too, after the same sums over every LIVE book. A position without a sizing plan (a recovered one) makes that unknown,
and `size()` refuses. Below 1 lot it refuses; it never rounds up. A zero, negative, NaN or non-numeric cap refuses:
zero never means "no limit".

**What the caps mean in practice.** A 1-lot trade passes only between about ₹6,200 and ₹49,850 of premium. Below
₹6,200 the flat ₹20-per-order brokerage makes the charges hurdle exceed 1% (gate G19); above ₹49,850 the premium,
its entry charges and the ₹100 pad pass the ₹50,000 worst-case cap (gate G20), just under the ₹50,000 premium cap.
The ₹15,000 per-trade cap does not bind at 1 lot: the 20% stop budget plus charges on ₹49,850 is about ₹10,140. For
NIFTY (lot 65) that is an option priced about ₹96–₹766; for BANKNIFTY (lot 30) about ₹207–₹1,661. An earlier loss
or another open position shrinks the G20 room: the day's realized loss plus every open premium stays within ₹50,000.
Past about ₹6,100 of loss in a day the daily-stop budget binds first: after a ₹10,000 loss the biggest 1-lot premium
is about ₹24,500 (its 20% stop budget plus charges must fit the ₹5,000 left of the ₹15,000 daily stop). A second open
position must fit what the first one's stop-out leaves of that budget, whether the first is up or down: its planned
stop-out (less once its stop is locked or trailed above the entry), or its loss when it is already past its stop. On
LIVE the account's ₹10,000 stop caps one trade's planned stop-out too, so a 1-lot premium above about ₹49,180 is
refused there even with the account flat (its PAPER twin still trades).

**Books**

| Book | Account | Policy | Purpose |
|---|---|---|---|
| `PAPER:<s>` | PAPER | FULL | The G1 population; caps identical to LIVE |
| `LIVE:<s>` | LIVE | FULL + account guard | Real money, after the unlock |
| `SHADOW:<s>` | SHADOW | QUALITY | SHADOW mode, and PAPER intents diverted by operator halts |
| `PLACEBO:<s>` | SHADOW | QUALITY | The opposite-option twin of every PAPER entry |
| `RECOVERED:<account>[:<seq>]` | that account | FULL (+ account guard on LIVE) | Recovery's own: a lost DB's imports, an adopted late fill. Counted by the daily stops; never entered, so no entry caps |
| `LIVE:_selftest` | LIVE | — | The daily order-path self-test only |

QUALITY books skip G09, G11, G12, G20 and G21 and keep every other gate.

**Settings.** The file `/var/lib/titan2/config/settings.json` is optional; absent keys take the defaults. The bot reads
it once at start and never again. An unknown key, a wrong type, NaN, or any value looser than its bound makes the
*whole* file invalid: the bot then runs on the defaults and raises HALT SETTINGS_INVALID (open trades keep their
frozen exits). Change settings only with `titan2 settings set KEY VALUE` (validated, atomic, effective at the next
restart). Defaults are the loosest allowed values: you can only tighten. The keys that only calibrate the paper
exchange (every `PAPER_*` key but `PAPER_CAPITAL_RS`) are fixed at their defaults (paper model 2): calibration is a
code decision, and for the delays and the participation no side is safe (a slower or a faster delay, or a lower
participation, each makes paper look better than LIVE in some case). A file that sets one to anything but its
default is invalid; changing one is a code change with a `PAPER_MODEL_VERSION` bump. A versioned key (`yes` below) is
part of every strategy's version, so changing it starts new G1 trials.

<!-- settings-table:start (generated from titan2.settings.LIMITS; tests/unit/test_docs.py) -->
| Key | Default | Allowed | Safer | Versioned |
|---|---|---|---|---|
| `MAX_LOTS` | 1 | 0 – 1 | lower | yes |
| `MAX_UNITS` | 5,000 | 0 – 5,000 | lower | yes |
| `MAX_LOSS_PER_TRADE_RS` | 15,000 | 0 – 15,000 | lower | yes |
| `MAX_PREMIUM_PER_TRADE_RS` | 50,000 | 0 – 50,000 | lower | yes |
| `MIN_PREMIUM_RS` | 20 | 20 – 500 | higher | yes |
| `STOP_SLIP_ALLOW_PCT` | 5 | 5 – 15 | higher | yes |
| `DAILY_STOP_RS` | 15,000 | 0 – 15,000 | lower | yes |
| `MAX_WORST_CASE_DAY_LOSS_RS` | 50,000 | 0 – 50,000 | lower | yes |
| `MAX_POSITIONS_PER_BOOK` | 2 | 0 – 2 | lower | yes |
| `MAX_ENTRIES_PER_DAY` | 3 | 0 – 3 | lower | yes |
| `MAX_ENTRY_ATTEMPTS_PER_DAY` | 6 | 0 – 6 | lower | yes |
| `MAX_ORDERS_PER_DAY_PER_BOOK` | 40 | 0 – 40 | lower | yes |
| `MAX_ENTRY_ORDERS_PER_MIN` | 3 | 0 – 3 | lower | yes |
| `REENTRY_COOLDOWN_MIN` | 30 | 30 – 390 | higher | yes |
| `VOID_COOLDOWN_MIN` | 5 | 5 – 390 | higher | yes |
| `REJECT_COOLDOWN_MIN` | 10 | 10 – 390 | higher | yes |
| `STOP_AFTER_FIRST_LOSS` | false | false, true | true | yes |
| `LIQ_MAX_SPREAD_PCT` | 1.5 | 0.1 – 1.5 | lower | yes |
| `LIQ_DEPTH_MULT` | 3 | 3 – 20 | higher | yes |
| `LIQ_MAX_SLIP_VS_MID_PCT` | 1 | 0.1 – 1 | lower | yes |
| `LIQ_MAX_LAST_TRADE_AGE_S` | 60 | 5 – 60 | lower | yes |
| `MAX_CHARGES_HURDLE_PCT` | 1 | 0.1 – 1 | lower | yes |
| `QUOTE_MAX_AGE_ENTRY_S` | 1.5 | 0.2 – 1.5 | lower | yes |
| `UNDERLYING_MAX_AGE_S` | 2 | 0.5 – 2 | lower | yes |
| `STALE_REFRESH_S` | 3 | 1 – 3 | lower | yes |
| `STALE_PROTECTIVE_S` | 5 | 2 – 5 | lower | yes |
| `ENTRY_START` | 09:20 | ≥ 09:20 | higher | yes |
| `NO_NEW_AFTER` | 14:55 | ≤ 14:55 | lower | yes |
| `EOD_FLATTEN` | 15:15 | ≤ 15:15 | lower | yes |
| `EOD_BACKSTOP` | 15:22 | ≤ 15:22 | lower | yes |
| `STOP_SENDING` | 15:25 | ≤ 15:25 | lower | yes |
| `BROKER_SQUAREOFF_AT` | 15:26 | 15:20 – 15:39 | fixed window | yes |
| `MIN_TD_TO_EXPIRY_INDEX` | 2 | 2 – 10 | higher | yes |
| `MIN_TD_TO_EXPIRY_STOCK` | 5 | 5 – 15 | higher | yes |
| `STOCK_ALLOWLIST` | [] | NSE symbol names | any list only restricts | yes |
| `STOCK_DENYLIST` | [] | NSE symbol names | any list only restricts | yes |
| `PAPER_PARTICIPATION` | 0.5 | 0.5 only | neither: fixed in code | yes |
| `PAPER_LATENCY_MEDIAN_MS` | 250 | 250 only | neither: fixed in code | yes |
| `PAPER_LATENCY_SIGMA` | 0.6 | 0.6 only | neither: fixed in code | yes |
| `PAPER_READ_LATENCY_MEDIAN_MS` | 250 | 250 only | neither: fixed in code | yes |
| `PAPER_READ_LATENCY_SIGMA` | 0.85 | 0.85 only | neither: fixed in code | yes |
| `PAPER_ADVERSE_TICKS` | 0 | 0 only | neither: fixed in code | yes |
| `PAPER_PRICE_BAND_PCT` | 20 | 20 only | neither: fixed in code | yes |
| `PAPER_BOOK_MAX_AGE_S` | 1 | 1 only | neither: fixed in code | yes |
| `PAPER_CAPITAL_RS` | 500,000 | 100,000 – 500,000 | lower | yes |
| `FEED_MAX_TICK_GAP_S` | 5 | 1 – 5 | lower | no |
| `FEED_WARM_S` | 60 | ≥ 60 | higher | no |
| `RECON_INTERVAL_S` | 10 | 2 – 10 | lower | no |
| `RECON_MAX_AGE_ENTRY_S` | 30 | 5 – 30 | lower | no |
| `LIVE_ACCOUNT_MAX_POSITIONS` | 2 | 0 – 2 | lower | no |
| `LIVE_ACCOUNT_DAILY_STOP_RS` | 10,000 | 0 – 10,000 | lower | no |
| `LIVE_FIRST_WEEK_MAX_ENTRIES` | 1 | 0 – 1 | lower | no |
| `LIVE_FIRST_WEEK_SESSIONS` | 5 | ≥ 5 | higher | no |
| `LIVE_DD_STOP_RS` | 15,000 | 0 – 15,000 | lower | no |
| `LIVE_PROBATION_TRADES` | 20 | ≥ 20 | higher | no |
| `LIVE_PROBATION_LOSS_RS` | 6,000 | 0 – 6,000 | lower | no |
| `LIVE_MIN_MARGIN_RS` | 60,000 | ≥ 60,000 | higher | no |
<!-- settings-table:end -->

The EOD times must also stay in order: ENTRY_START < NO_NEW_AFTER ≤ EOD_FLATTEN < EOD_BACKSTOP < STOP_SENDING <
BROKER_SQUAREOFF_AT, and STALE_PROTECTIVE_S > STALE_REFRESH_S.

**Code constants** (not settings; in `titan2/settings.py`): the G1 constants, `ENGINE_SEMANTICS_VERSION`,
`PAPER_MODEL_VERSION`, `SCHEMA_VERSION`, the exit-profile bounds `EXIT_BOUNDS`, the price `BANDS`, order timers
(poll 3 s, cancel 6 s, stuck 10 s), reconciliation timers (post-fill lag 2 s, grace 5 s, confirm gap 3 s), the exit
cap's read rule (`EXIT_CAP_PRIOR_READ_S` 0, decision B1) and the rest of `SEMANTIC_CONSTANTS`. Its hash is pinned in
`titan2/strategies/REGISTRY.lock`.

## 9. Halts and operator commands

A **halt** is a sticky row in `titan2.db`, scoped GLOBAL, to an account, a book, a strategy, an underlying or a
symbol. It blocks new entries in that scope, survives restarts, and **never stops an exit**. Each kind clears one way:

- **NEXT_SESSION**: cleared by the first timer of the next trading session (DAILY_STOP, LOSS_STREAK,
  CONTRACT_BLOCKED, LIVE_DISABLED_TODAY).
- **AUTO**: cleared when the condition is gone (TOKEN, STALE_FEED, STALE_RECON, EXIT_DIFFICULTY, SETTINGS_INVALID,
  CLOCK_UNSYNCED, NO_INSTRUMENTS, V1_ACTIVE).
- **ACK**: a human runs `titan2 ack-halt <id> --note "…"` after checking (MISMATCH, UNKNOWN_POSITION,
  SHORT_DETECTED, INVARIANT, EXCEPTION, GATE_ERRORS, ORDER_STUCK, REJECTS, RUNAWAY, KILL, SAFE_FLAG). Some acks are
  refused until it is safe: MISMATCH, UNKNOWN_POSITION and SHORT_DETECTED only while reconciliation is CLEAN; KILL
  only once flat; SAFE_FLAG only after the bot left safe mode.
- **RESUME**: PAUSE, and FLATTEN once flat is verified.

The full table, with what to check before each ack, is [RUNBOOK §6](docs/RUNBOOK.md#6-halts).

**Operator commands**

| Command | From | Effect |
|---|---|---|
| PAUSE / RESUME | dashboard or CLI | PAUSE adds an operator halt: no new LIVE or PAPER entries (PAPER intents are diverted to the simulated SHADOW book); exits continue. RESUME clears it |
| FLATTEN_ALL | dashboard (type `FLATTEN`) or CLI | HALT FLATTEN; cancel working entry orders; every bot leg exits (`OPERATOR_FLATTEN`); ack DONE when verified flat, FAILED (CRITICAL) after 60 s while it keeps trying |
| KILL | CLI only (type `KILL`) | FLATTEN_ALL with `KILL_SWITCH` + HALT KILL + every LIVE strategy demoted |
| ACK_HALT, ACK_ALERT, ACK_FOREIGN | CLI | acknowledge a halt, silence a repeating CRITICAL alert, accept an exact foreign position |
| SET_MODE, RUN_RECON | CLI | change a strategy's mode (§11); reconcile now |

Commands are rows in insert-only SQLite files that only the bot consumes. If the bot is down, PAUSE, FLATTEN_ALL and
KILL wait and run at the next start; other commands expire after 300 s. Positions the bot does not own are listed but
never touched. Operator halts (PAUSE, FLATTEN, KILL, SAFE_FLAG) divert PAPER intents to SHADOW and mark the session
IMPAIRED, so operator timing cannot bias the G1 sample. SHADOW keeps simulating under PAUSE only; FLATTEN, KILL and
SAFE_FLAG stop every book.

## 10. Reconciliation and restarts

**Ownership.** A bot order has a valid `T2…` tag for its account *and* a row in the DB. A bot position is one whose
net qty is fully explained by bot fills. Everything else is foreign and is never traded.

**The post-fill rule** (the positions API lags fills). A broker positions or orders snapshot counts for a symbol only
if it was requested at least 2 s after the bot's last fill on that symbol. A difference counts only when two such
snapshots, at least 3 s apart and outside the 5 s fresh-fill grace, agree. Nothing else may cap an exit, confirm flat
or declare an external close.

**Periodic reconciliation** runs every 10 s per account from 09:00 until the last EOD recon (`BROKER_SQUAREOFF_AT`
+ 5 minutes: 15:31 at the default), every 5 s with open legs, at startup, on demand (`titan2 recon`), and at the
two EOD recons. Quantities are signed, per symbol and product.

| Class | Meaning | Action |
|---|---|---|
| BROKER_ERROR | a broker read failed | infer nothing; 3 in a row → HALT STALE_RECON |
| CLEAN | broker = bot | — |
| PENDING | seen once or in grace | wait for confirmation |
| REPAIRED | missing fills found on bot-tagged orders | apply them |
| EXTERNAL_CLOSE | the broker shows less than the bot holds, confirmed, and a sale shows for it (today's orders, or the positions row's day sells) | `close_external`; manual → HALT MISMATCH + CRITICAL; after `BROKER_SQUAREOFF_AT` by a non-API order → `BROKER_SQUAREOFF` (CRITICAL, no halt) |
| PARTIAL_EXTERNAL | 0 < broker < bot | exit the rest (`ANOMALY_EXIT`), capped at the broker qty; HALT MISMATCH |
| CONTRADICTION | positions show less than the bot holds, but no sale explains it (today's orders and positions disagree) | book nothing; HALT MISMATCH + CRITICAL; the leg stays managed and its exit is capped by positions |
| EXCESS / FOREIGN | the broker shows a position the bot cannot explain | HALT UNKNOWN_POSITION; never traded |
| SHORT | net short on NFO | HALT SHORT_DETECTED + CRITICAL; buy back automatically only if the bot's own fills explain it exactly |

**Exit cap and verify-flat.** The exit qty is capped by the post-fill broker net long, read **after** the exit was
decided: only a read requested at or after the decision counts (decision B1, 2026-10-11:
`EXIT_CAP_PRIOR_READ_S` = 0, a semantic constant), and every retry needs its own read, requested after the previous
attempt ended. So a position closed by hand (or by the broker) a moment before the decision, or between two
attempts, is never sold a second time once Zerodha's positions show it. The exit loop runs the moment that read
lands, so every exit order pays one positions round trip; paper pays the same round trip (the paper read delay, §4),
so paper exits are as fast as LIVE ones, no faster. The first EXIT order's `EXIT_ATTEMPT` event records
`decision_to_order_ms`, and the daily report's latency table has `exit_decision_to_submit_ms`. If the positions read
made after the decision fails, the orders view is asked at once; while neither answers, the exit waits and re-asks
both every 2 s. After 10 s without a usable answer, the newest post-fill read from before the decision caps it (a
CRITICAL `EXIT_CAP_BEFORE_DECISION`); with none, it uses the bot's own qty with an `EXIT_UNCAPPED` WARN. If the
broker shows 0 while the bot thinks it is open, the bot sends nothing and lets reconciliation decide. When the bot's
own open qty reaches 0, two post-fill reads must show flat; a confirmed leftover is recorded as a residual with HALT
MISMATCH and is **never sold**.

**Startup** (`titan2/runtime.py`): take the lock (exit 73); refuse a dirty working directory (exit 78); load settings;
back up, migrate and integrity-check the DB (exit 78 on failure); start alerts; check the token (HALT TOKEN if bad);
build the lanes, paper exchange and feed; **recover**; send READY (target under 30 s); start the WS feed; prep
(instruments, ban list, previous bars) and the prestart self-check; start the strategies (ignored before 09:15); run
the LIVE self-test at 09:15:30 if a LIVE strategy exists; run the EOD timeline; exit 0. Not a trading day, after
15:45, or after today's 15:40 end (its DONE step is recorded): recover if needed, then exit 0. An EOD step row stamped
later than now, or than a start recorded after it, was not written on that day (an old `titan2 laptop --date`
rehearsal left it in the real state folder): it is ignored with a WARNING, here and for the EOD steps the engine
counts as done, so that day still trades and runs every EOD step. A start after the
15:26 square-off (`BROKER_SQUAREOFF_AT`) that still holds a leg may find it squared off while the bot was down (the
paper exchange does it at its first step): it keeps reconciling every 5 s until that square-off is booked or the
broker shows the leg still held in a read made more than 5 s after READY (until 16:00 at most, or a minute after
READY when later), and the 15:33 truth check, the later EOD steps and the exit wait for it, so the day is booked on
the day. EOD steps that are due together (a start after 15:40, or that wait ending after their times) run in order,
each after the answers and files of the steps before it: the nightly G1 after the truth check and the bar check, the
exit after the 15:38 files and report.

**Recovery.** Match every unfinished DB order with the broker by id or tag; import today's bot tags missing from the
DB; fetch trades; rebuild open qty from fills; reconcile twice 3 s apart; restore OPEN and EXITING legs with their
frozen levels. A restart after 15:15 flattens (`RECOVERY_FLATTEN`). Leftovers from an earlier session: LIVE →
CLOSED_EXTERNAL, UNRESOLVED, HALT MISMATCH; paper → the paper exchange squares them off at the next trading day's
start (never on a weekend or holiday). A paper leftover the paper exchange had already closed on an earlier day
(nothing of today shows that) is booked from the paper exchange's own record of that close, at its price
(`BROKER_SQUAREOFF`); with no record it closes like a LIVE leftover. Intents are never replayed.
Entries resume only after two CLEAN recons per account, a warm feed, prep done and no entry halts.

**SIGTERM** stops entries, cancels working entry orders (≤ 10 s), lets in-flight exits finish (≤ 10 s), commits and
exits 0. It never flattens. **Token rotation:** on a token error the bot halts entries, re-reads the session file
every 30 s and, when a newer token works, exits 75 so systemd restarts it cleanly. It never logs in itself. **WS
reconnect** back-fills order updates over REST and invalidates bars across the gap; still down 60 s after the feed gives
up → exit 75.

**EOD truth check** (15:33): LIVE — the sum of today's `trades.net` must equal the broker tradebook recomputed with the
same charges, within ₹1, else `EOD_TRUTH_MISMATCH` (CRITICAL). Paper — the ledger must exactly equal the paper
exchange's own records, per book, else one G1 mismatch for each book that differs (every book only when an order
cannot be attributed). Both sides are taken by the trade's own session: a leftover of an earlier session settled at
today's start is checked against its own trade, not against today's ledger. A leftover closed UNRESOLVED at the start
(no broker order: no tradebook can show that close) is not compared; its MISMATCH alert already reports it.

**One broker order, several books.** Two paper books may hold the same contract, and one broker order (the
square-off, or a manual close of the symbol) can close both legs. Each leg books its share of that order
(`charges.OrderPart`): the order's value and every charge component are split by quantity so the shares add up
exactly to the order, and its ₹20 brokerage and ₹50 square-off fee are paid once in total, not once per leg. The
truth check compares each book on its share (`ledger.truth_attribution`).

## 11. Strategies, G1 and LIVE

**Plug-in API** (`titan2/strategies/base.py`). A strategy class declares `NAME`, `LOGIC_VERSION`, `PARAMS`,
`EXIT_PROFILE`, `UNIVERSE` and `WINDOW`, and implements `on_tick`, `on_bar_close`, `on_timer` (each returns intents)
and `on_feed_reset`. It sees the market only through a read-only `MarketView`. A hook that raises disables the
strategy for the session. At most 30 intents per minute.

**Version.** A strategy's version is `<LOGIC_VERSION>.<hash>`: the first 10 hex digits of a sha256 over its name,
`PARAMS`, `EXIT_PROFILE`, every versioned setting, `ENGINE_SEMANTICS_VERSION`, `PAPER_MODEL_VERSION`, the
semantic-constants hash, and the code: its own module's code hash combined with that of `titan2/strategies/base.py`
(the plug-in API every strategy runs on). So any code change gives a new version, even if someone refreshed the lock
by hand. `titan2/strategies/REGISTRY.lock` pins each strategy's code hash too: a code change of either file without
a version bump fails the tests (an edit of `base.py` needs every strategy bumped). A new version opens a new G1
trial; the old one is RETIRED.

**The phase-1 strategies** (all stock options, all start in PAPER, all low-confidence hypotheses)

| Strategy | Idea | Entry |
|---|---|---|
| `opening_drive` v1 | Follow a strong, high-volume first 5-minute bar | 09:15 bar return ≥ +2.5% (UP) or ≤ −2.5% (DOWN); volume ≥ 3 × the previous session's last-20-bar average; retrace from the extreme ≤ 65%. One batch at 09:20, strongest first, ITM1, at most one per symbol per day |
| `price_spike_up` v1 | A sharp upward move that holds | Move ≥ 1.0% (0.6% from 09:45) against a 60 s baseline with volume confirmation, still holding after 12 s (≥ 0.35%, or 1.0% before 09:55; retrace ≤ 65%). ATM CE only, never DOWN, 90 s cooldown, 09:20–14:55 |
| `price_spike_down` v1 | A sharp downward move that holds (a PAPER experiment, below) | `price_spike_up`'s exact signed mirror: a fall ≥ 1.0% (0.6% from 09:45) against a 60 s baseline with volume confirmation, still holding after 12 s (≥ 0.35%, or 1.0% before 09:55; retrace from the low ≤ 65%; a move back up through the baseline fails). ATM PE only, never UP, recheck band [LTP × 0.995, baseline × 0.9965], 90 s cooldown, 09:20–14:55 |

**`price_spike_down` is an experiment the user asked for** (2026-10-12), against brief E1, which deleted v1's
PRICE_SPIKE_DOWN (it won 1 of 6 trades in the 2026-06-10 to 07-14 regime, an anecdotal sample). It trades PAPER only,
like every strategy until G1, on its own trial and scorecard, and its placebo is a CE. Adding it changed neither the
code, the versions nor the `REGISTRY.lock` entries of the other two, so their G1 trials go on; it is registered last,
so on a tick that reaches all three the other two still run first. `titan2 mode price_spike_down OFF` stops it. It
still reaches the other two's trials in three ways (review, 2026-10-12):

- **Shared PAPER-account controls.** An operator halt while any structure is open, its PE or CE placebo included,
  marks the session IMPAIRED for every PAPER/LIVE strategy (`Engine._operator_impaired`) until EOD_FLATTEN, also
  after NO_NEW_AFTER when no entry can be censored any more: the laptop's first Ctrl+C with a put open impairs
  `opening_drive` and `price_spike_up` too (with nothing open, or from 15:15, it marks nobody). A MISMATCH or
  UNKNOWN_POSITION halt on its PE legs is scoped `ACCOUNT:PAPER` with ACK: it blocks every PAPER book and, left for
  more than G1_IMPAIRED_GAP_S (600 s) of the entry window, impairs every G1 strategy. PAPER mismatches count by book
  or by traded symbol (`g1.count_mismatches`), so one on a PE contract also counts against `opening_drive` if its
  DOWN (ITM1 PE) legs traded that contract in its trial window. A look or the confirm fails when IMPAIRED sessions
  exceed 10% of the sample's sessions, and the count includes sessions the strategy did not trade.
- **No shared cooldown.** Each spike strategy keeps its own 90 s per-stock cooldown, where v1's `_fire_trigger` had
  one per stock (PRICE_SPIKE_UP and PRICE_SPIKE_DOWN at equal priority, so neither bypassed the other's). A stock
  that spikes up and pulls back can get `price_spike_up`'s CE and, seconds later, `price_spike_down`'s PE, each
  with its placebo: up to four structures on one underlying (I5 allows one per book). Both can even fire on the
  same tick, when the price is up against baseline A and down against baseline B. So the experiment does not
  sample v1's PRICE_SPIKE_DOWN trades, the population behind E1's 1 of 6. A shared cooldown would change
  `price_spike_up`'s behaviour, and so its running trial.
- **G1's family size.** m = max(trials ever registered, 4) is shared. With one version of each strategy (3 trials)
  m stays 4: α per look 0.3125%, bootstrap B 16,000. But every change that re-versions all strategies (a versioned
  `titan2 settings set`, an edit of `titan2/strategies/base.py`, an `ENGINE_SEMANTICS_VERSION` bump, the `PAPER_*`
  recalibration X2 asks for) now registers 3 trials instead of 2: the first gives m = 6 (α 0.2083%, B 24,000) where
  it would have stayed 4. And from its third version on, each `price_spike_down` version alone raises m for every
  strategy (two bumps: 5 trials, m = 5, α 0.25%). Bump it only for a real reason; a version registers its trial
  only once it runs in PAPER or LIVE.

**Modes**: OFF (applies at once; session IMPAIRED) · SHADOW (QUALITY book only) · PAPER (the G1 population) · LIVE
(`LIVE:<s>` plus a `PAPER:<s>` twin from the same snapshot). Changes other than OFF take effect at the next session.

**Placebo.** Every PAPER entry also opens the opposite option type on the same underlying, strike rule, expiry and
snapshot in `PLACEBO:<s>`, in the same transaction: both orders go out together and each pays its own paper latency,
so the price path while the parent fills never decides which placebos fill. One refused on the snapshot is recorded
on the parent as `SKIPPED:<gate>`. (The spec opens it for every entry that *fills*; a placebo whose parent never fills
stays an ordinary SHADOW trade, which G1 never pairs, so it can only make G1 harder to pass.) If the "edge" also
shows up in the placebo, it was not an edge.

**G1** (`titan2/g1.py`) judges one strategy version on its own PAPER trades. In plain words: enough trades, a positive
average after a harsh cost stress, a statistical lower bound above zero after paying for every version ever tried, a
placebo that does *not* make money, and clean books.

| Requirement | Value |
|---|---|
| Sample | ≥ 100 trades over ≥ 30 sessions, taken in the order they were booked (a trade booked late, say a missed square-off settled next day, comes after the trades already judged); looks at 100, 150, 200, 300 trades |
| Return per trade | r = (net − 0.5 × C) / premium paid, where net is re-priced with today's charges and C is the measured cost of crossing the spread: each trade must survive paying that cost 1.5 times |
| Edge | mean r > 0 and the day-clustered bootstrap lower bound > 0 at α = 0.05 / (m × 4), m = max(trials ever registered, 4) |
| Placebo | n ≥ 50, coverage ≥ 80%, mean unstressed return ≤ 0 |
| Clean books | zero PAPER mismatches; zero UNRESOLVED or ESTIMATED trades; ≤ 10% IMPAIRED sessions; ≤ 10% bar-check mismatches (`opening_drive`) |
| Confirmation | the next 25 trades booked after the passing look's sample also have mean r > 0, then CERTIFIED (certificate valid 7 days) |
| Futility | from 50 trades, every 25: if even the optimistic bound is below 0 → FUTILE, mode SHADOW |

Trial states: COLLECTING → CONFIRMING → CERTIFIED, or FAILED / FUTILE / RETIRED / DEMOTED. Passing needs roughly +3% net
per trade after the stress; the evidence suggests −1.5% to 0%. FUTILE or FAILED is the honest expected result.

**LIVE unlock: two human keys** (procedure in [RUNBOOK P14](docs/RUNBOOK.md#p14-live-unlock-and-rollback-to-paper))

1. The trial is CERTIFIED and Telegram shows `G1_CERTIFIED` with the report hash.
2. Key 1: root sets `ALLOW_LIVE=1` in `/etc/titan2/titan2.env`, with `REGISTERED_EGRESS_IP` set to the registered
   static IP, and restarts the bot while flat.
3. Key 2: `titan2 live <strategy> --confirm <hash> --i-accept-live-risk`, accepted only for a CERTIFIED trial with a
   certificate under 7 days old, no GLOBAL ACK halt and no demotion. LIVE starts next session.
4. Every session with a LIVE strategy runs the **self-test** at 09:15:30 (retry 09:17): a 1-lot NIFTY ATM CE BUY IOC
   at 97% of the bid, which must come back CANCELLED with 0 filled within 5 s. Failure → LIVE_DISABLED_TODAY. A fill →
   immediate exit.
5. First LIVE week: at most 1 entry per session for 5 sessions.

**Live review** (after every LIVE close and nightly) demotes a strategy to PAPER on: cumulative net ≤ −₹6,000 within
its first 20 trades; drawdown ≤ −₹15,000 since unlock; mean of the last 50 trades below 0; the X2 falsifier (after 20
twin pairs, live slippage worse than paper by more than 0.5 pp, or live IOC fill rate more than 10 pp below paper →
`PAPER_MODEL_OPTIMISTIC`, every strategy demoted); or any LIVE mismatch, unknown position or short. LIVE again needs a
new version and a fresh G1.

## 12. Server layout, data and files

```
/home/ubuntu/titan/
  agentic_trader/                 WorkingDirectory: contains ONLY the symlink titan2 -> ../current/titan2
  current -> releases/<sha>       the single atomic switch
  releases/<sha>/                 immutable release: titan2/ ops/ requirements.lock MANIFEST.sha256 VERSION, venv -> ../../venvs/<lockhash>
  venvs/<lockhash>/  incoming/
  v1_frozen_YYYYMMDD/             the archived v1 tree (read-only)
/etc/titan2/titan2.env            root:titan 0640       secrets for the bot, token and self-check
/etc/titan2/dash.env              root:titandash 0640   dashboard login and CSRF secret only
/var/lib/titan2/                  state (titan:titandash 0710)
  db/        titan2.db, paper_exchange.db, commands_cli.db
  dash/      commands_dash.db (dashboard writes, bot reads)
  pub/       *.json for the dashboard (bot writes)
  session/   kite_session.json 0600
  config/    settings.json, ban_override/
  cache/  run/  reports/  exports/  backups/
/var/log/titan2/bot.log           also in journald
/var/backups/titan2/              daily and monthly DB backups
```

**`titan2.db`** (WAL, `synchronous=FULL`): `meta`, `seq`; audit `events` (append-only); `intents`,
`intent_routes`; `structures`, `legs`, `orders`, `fills`, `trades` (immutable ledger); `quote_checks`, `halts`;
`strategy_modes`, `g1_trials`, `g1_reports`; `bars_5m`, `bar_checks`; `broker_snapshots`, `foreign_acks`,
`command_acks`, `session_flags`. The constraints in `titan2/migrations/0001_init.sql` enforce the invariants first.
Nothing is deleted except the weekly prune (`quote_checks` over 180 days, after a CSV.gz export; `broker_snapshots`
over 30 days). v1's `titan_state.db` is never opened.

**`paper_exchange.db`** holds the paper exchange's own orders, per-level fills and counters. Its positions are always
the sum of its fills, kept independently of the engine so reconciliation is a real check.

**`pub/`**, the only thing the dashboard reads: `status.json` (every second), `trades_today.json`, `trades_90d.json`,
`trade/<id>.json` (orders, fills, 1-minute bid path), `events_tail.json`, `g1.json`, `acks.json`.

**Outputs**: `reports/<date>.md` (daily report, also on Telegram), `reports/selfcheck-*.json`,
`exports/{events,trades}/<date>.jsonl.gz`, `/var/log/titan2/bot.log`, `quote_checks` HOLD rows (each check) and POST
rows (45 minutes of bid path after every exit, for offline exit research).

**Env keys** (`/etc/titan2/titan2.env`, written by hand from `ops/titan2.env.example`, never in git): `ZERODHA_API_KEY`,
`ZERODHA_API_SECRET`, `ZERODHA_USER_ID`, `ZERODHA_PASSWORD`, `ZERODHA_TOTP_SECRET`, `TOKEN_MODE` (auto or manual),
`ALLOW_LIVE` (0), `REGISTERED_EGRESS_IP`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `HEALTHCHECK_URL`, optional
`BAN_LIST_URL` and `BACKUP_S3_URI`. Deliberately absent: the access token (it lives in the session file) and any
trading mode (modes are per strategy, in the DB). `dash.env` holds only `DASH_USER`, `DASH_PASS_SCRYPT` and
`DASH_SECRET`, and must never contain a `ZERODHA_*` key.

## 13. Daily timeline

| IST | What happens | You |
|---|---|---|
| 08:30 | `titan2-token` refreshes the Kite session | nothing (manual mode: one login) |
| 08:40 | `titan2-selfcheck` morning check → Telegram | read PASS/FAIL |
| 08:45 | `titan2-bot` starts, recovers, preps, READY | nothing |
| 09:15:30 | LIVE self-test (only with a LIVE strategy; retry 09:17) | nothing |
| 09:20–14:55 | entries (PAPER) | nothing; the dashboard is optional |
| 15:15 | EOD_FLATTEN: every open leg exits | — |
| 15:22 | EOD_BACKSTOP: exits price at band ≥ 3; CRITICAL for anything still open | act only on a CRITICAL |
| 15:25 | STOP_SENDING: no more orders of any kind | — |
| 15:26 | Broker MIS auto square-off **[V]** (paper simulates it) | — |
| 15:27, 15:31 | final reconciliations (classify broker square-offs) | — |
| 15:33 | EOD truth check and virtual contract note check | — |
| 15:36 | bar check (live 09:15 bars against Kite's history) | — |
| 15:38 | exports, daily report, nightly G1, live review, weekly prune | read the report (2 min) |
| 15:40 | the bot exits 0 (the market also closes at 15:40) | — |
| 16:10 | backup | — |

The steps from 15:27 on are offsets from `BROKER_SQUAREOFF_AT` (+1, +5, +7, +10, +12, +14 minutes), so they move with
that setting. A start after 15:40 runs the steps it missed in order, each after what the one before it asked for, and
exits after the report. Weekly: `titan2 report --date <d>` and `titan2 g1` (10 minutes). Yearly: add the next year's NSE
holidays and review the charges (10 minutes).

## 14. Operating titan2

Everything operational is in the [RUNBOOK](docs/RUNBOOK.md). The short version:

**CLI** (`/usr/local/bin/titan2`, installed from `ops/titan2-cli`; runs `python -m titan2` as user `titan`)

| Command | Purpose |
|---|---|
| `titan2 status [--json]` | heartbeat, phase, feed, halts, books, positions (read-only) |
| `titan2 pause` · `titan2 resume` | stop or allow new LIVE and PAPER entries; exits always continue |
| `titan2 flatten-all --confirm FLATTEN` | exit everything the bot owns, verified flat |
| `titan2 kill --confirm KILL` | flatten + HALT KILL + demote every LIVE strategy |
| `titan2 ack-halt <id> --note TEXT` · `titan2 ack-alert <key>` · `titan2 ack-foreign SYMBOL PRODUCT QTY [--account A]` | acknowledgements (account defaults to LIVE) |
| `titan2 mode <strategy> OFF\|SHADOW\|PAPER` · `titan2 live <strategy> --confirm <hash> --i-accept-live-risk` | modes and the LIVE key |
| `titan2 recon` | reconcile now |
| `titan2 settings show\|diff` · `titan2 settings set KEY VALUE` · `titan2 settings unset KEY` | settings (tighten-only, effective at next restart) |
| `titan2 trades [--date D] [--book B]` · `titan2 report [--date D]` · `titan2 g1 [strategy] [--json]` | read-only reports |
| `titan2 token [--request-token RT]` | refresh the Kite session (auto TOTP, or manual exchange) |
| `titan2 selfcheck --phase morning\|prestart\|deploy\|cutover` | run a self-check phase |
| `titan2 is-trading-day [--date D]` · `titan2 backup [--to DIR] [--gzip]` · `titan2 selftest` · `titan2 alert --critical TEXT` | helpers: trading-day test for scripts, online DB backup, today's LIVE self-test result (deploy check), the OnFailure alert |
| `titan2 bot [--safe] [--outside-systemd]` | the bot itself (systemd only: without systemd it refuses, exit 2, unless `--outside-systemd`, which reads `/etc/titan2/titan2.env` itself); `--safe` or `TITAN2_SAFE=1` = exits only |
| `titan2 laptop [check] [--date D] [--no-dash] [--skip-token]` | the Windows laptop's PAPER day in the foreground, or only its self-check (below; no systemd, no override) |
| `titan2 dash [--host H] [--port P] [--allow-public]` | the read-only dashboard in the foreground (the laptop: 127.0.0.1:5000; any other address needs `--allow-public`) |

Mutating verbs insert a command and wait up to 30 s for the bot's ack, then print it.

**Exit codes.** CLI: 0 ok, 2 refused or failed (`is-trading-day`: 0 yes, 1 no, 2 unknown). Bot: 0 done; 3 exception
storm or crash; 73 lock held (another bot running); 75 controlled restart (token or feed); 78 fatal config or DB
(systemd does not restart 73 or 78).

**Dashboard.** `ssh -N -L 8050:127.0.0.1:8050 titan-bot`, then open `http://127.0.0.1:8050`. Basic auth; it shows MTM
at the bid (LTP greyed as "info only") and offers PAUSE, RESUME and FLATTEN_ALL. It computes nothing and shows "bot
offline — use the Kite app" when `status.json` is older than 10 s.

**Deploy** (from the laptop, Git Bash): `ops/deploy.sh <host> [--allow-market-hours] [--restart]`, where `<host>` is
the `titan-bot` ssh alias. It refuses a dirty tree or failing tests; refuses while the running bot has open legs or
working orders, between 08:40 and 15:45 without `--allow-market-hours`, on server drift, or with under 1 GB free;
builds an immutable release and a hash-locked venv; runs `titan2 selfcheck --phase deploy` against a fresh DB backup;
switches the `current` symlink atomically; restarts the bot only with `--restart` (or when it is not running),
rolling back if READY does not arrive within 120 s. Exit 0 deployed, 1 rolled back after a failed switch, 2 refused
with nothing changed. The server side is `ops/release.sh`, sent over ssh; it never touches state, secrets or v1.
`ops/rollback.sh <host> [--to <release>]` flips back to the previous release; `ops/pull_drift.sh <host>` copies files
edited on the server back to `./drift/<release>/`. CI (`.github/workflows/ci.yml`) runs tests only: no deploy job, no
secrets.

**If the bot is down with positions open:** use the Kite app. There is deliberately no second order tool. The broker's
MIS auto square-off is the last backstop.

### Laptop mode (Windows, PAPER only)

titan2 runs on a Windows laptop first, not on EC2. The operator guide is [docs/LAPTOP.md](docs/LAPTOP.md): install once
with `ops/windows/setup.ps1`, then every trading morning start `ops/windows/run_today.ps1`, which runs
`python -m titan2 laptop` from titan2's venv. EC2 and systemd are unchanged.

- **PAPER only.** `titan2 laptop` sets `TITAN2_PROFILE=laptop`, which refuses LIVE whatever `ALLOW_LIVE` or a
  strategy's mode says: SEBI requires a registered static IP for API orders, and a laptop has no backstop if it
  sleeps, loses Wi-Fi or reboots. A LIVE-mode strategy trades only its PAPER twin that session (a WARNING and a
  `LIVE_REFUSED` event); its mode in `titan2.db` is kept. No LIVE order of any role leaves the laptop: its
  `KiteBroker` refuses every LIVE place and cancel ("LOCAL: laptop profile: no LIVE orders"), and recovery never
  imports today's titan2 LIVE orders that its `titan2.db` lacks (EC2's, in the shared Kite account): it raises
  HALT(UNKNOWN_POSITION, ACCOUNT:LIVE) with a CRITICAL alert instead, and those positions are managed in Kite or on
  EC2.
- **The day.** Not a trading day: it says so and exits 0. Otherwise: another titan2 on this laptop (exit 73, before
  any login), the Kite session (today's file, else the TOTP login, else a pasted request token), the laptop
  self-check, the bot session (recovery first, exits resume before READY; restarted in-process on exit 75, and up to
  5 times on exit 3), the bot's own EOD timeline, the daily report, exit.
- **Self-check phase `laptop`** (`titan2 laptop check` runs it alone; in the bot it replaces `prestart`): rows
  `python`, `env_file`, `token`, `clock`, `network`, `calendar`, `instruments`, `disk`, `db`, `settings`, `power`,
  `live_disabled`, instead of the systemd, users, v1 and egress-IP rows. A FAIL halts entries and never exits.
  `network` (WARN only) times 8 IPv4 TCP connects to api.kite.trade: a lost or slow connect means Kite timeouts,
  i.e. entries refused as QUOTE:FETCH_FAILED. Every titan2 process sends Kite REST over IPv4 (titan2/net.py).
  `clock` blocks entries (CLOCK_UNSYNCED) when titan2's clock can be more than 1 s off internet time, the reading's
  own error counted (half the NTP round trip; the whole-second HTTP Date fallback of api.kite.trade adds 0.5 s), or
  when no source answers. On the laptop titan2's clock is the Windows clock corrected to internet time
  (`titan2.clock.CORRECTION`: measured by `laptop.correct_clock` at the start of a real run and every 5 minutes,
  every 20 s while it has none; a step of the Windows clock, seen as system minus monotonic time moving by more
  than 0.25 s, is absorbed at once; never backwards; also applied to log records and handed to the dashboard
  child; `TITAN2_CLOCK_CORRECT=0` turns it off), so the row judges what the correction leaves; `power` and `calendar` only warn. A `python`, `disk` or `db` FAIL raises HALT(INVARIANT), which
  stays after the fix until `titan2 ack-halt <id> --note TEXT`.
- **Ctrl+C.** The first press always sends a FLATTEN_ALL (also when nothing is open): its FLATTEN halt stops
  entries within a second, every open paper leg is flattened through the exit engine, the progress shows until
  verified flat, then titan2 stops cleanly with the halt still up. No RESUME is sent: the engine clears that halt
  itself once the run ends flat (Shutdown, or the EOD exit), so the next start trades normally, and the command
  expires like a non-operator one, so it never runs at a later start. With nothing open the halt does not mark the
  session IMPAIRED unless it outlives the stop by G1_IMPAIRED_GAP_S of the entry window; with anything open (any
  book's, placebos included) it marks every PAPER/LIVE strategy's session IMPAIRED at once, until EOD_FLATTEN
  (§11). A second press stops at once; open positions are recovered at the next start, which keeps exiting them
  under the FLATTEN halt until `titan2 resume`. Closing the window gets about 3 s of the first press.
- **Paths** (`TITAN2_VAR`, `TITAN2_LOG_DIR` and `TITAN2_ENV_FILE` override them): state in
  `%LOCALAPPDATA%\titan2\var`, logs in `%LOCALAPPDATA%\titan2\logs` (`laptop-<date>.log`, 30 days), secrets in
  `%APPDATA%\titan2\titan2.env`, outside the repo. `titan2 status`, `titan2 pause` and the other verbs find them
  from any window. The dashboard runs as a child process on http://127.0.0.1:5000 with its own
  `%APPDATA%\titan2\dash.env`. While a session runs, Windows is kept from sleeping on its idle timer, and the power
  source is logged every 15 minutes (a WARNING on battery).
- **`--date D`** (tests and rehearsals only) moves the wall clock to D and runs in its own state and logs,
  `%LOCALAPPDATA%\titan2\sim\<D>\var` and `\logs` (next to the state dir in use; `TITAN2_SIM_DIR` replaces the `sim`
  folder, never with the real state dir), seeded with today's Kite session and the settings file. The real state
  dir is only read: a rehearsal written there became session D's G1 evidence, flagged the real D IMPAIRED and used
  up the real D's paper square-off. A rehearsal session is refused (exit 73) while a titan2 runs on the real state
  dir.

## 15. Tests

Run on Windows or Linux from `agentic_trader/`, with Python ≥ 3.10:

```
python -m pip install --require-hashes --only-binary :all: -r requirements-dev.txt
python -m pytest tests -q -p no:cacheprovider
python -m pyflakes titan2 tests
```

The first line (once per environment) installs pytest plus the hash-locked runtime pins of `requirements.lock`; the
last is an optional lint (`pip install pyflakes`).

| Suite | What it proves |
|---|---|
| `tests/unit/` | each module against its spec, including `tests/unit/test_docs.py` (these docs against the code) |
| `tests/scenarios/` | every brief killer (`k01`–`k12`), the May 13–14 postmortem cases, restart/lag cases and `price_spike_down`'s PE entry with its CE placebo, each run on PAPER **and** LIVE (fake Kite) with identical commands and P&L |
| `tests/chaos/` | seeded random faults and restarts (200 seeds in CI, 500 nightly): no unexplained short, open = fills = broker, ≤ 1 working order per leg, CLOSED net = `net_pnl`, LIMIT/IOC/MIS only |
| `tests/arch/` | no v1 imports, one order adapter, no GTT/SL-M/MARKET identifiers, import isolation, no paper branches in core, registry locks current, Python 3.10 syntax, Windows-importable; `tests/arch/test_v1_retired.py`: the deleted v1 files stay gone and unimported, the `_*` scripts and research data stay untouched |
| `tests/golden/` | ported logic matches fixtures captured from v1 at `v1-final` |
| `tests/ops/` | deploy, rollback and cutover scripts against temp directories |

**Changing behaviour, step by step**

1. Write or adjust the test first; keep every suite green.
2. Strategy code changed → bump its `LOGIC_VERSION`. Anything in `SEMANTIC_CONSTANTS` changed → bump
   `ENGINE_SEMANTICS_VERSION`. Paper-fill model changed → bump `PAPER_MODEL_VERSION`.
3. Refresh the lock from `agentic_trader/`:
   `python -c "import sys, titan2.settings as t, titan2.strategies as s; sys.exit(s.main(t.DEFAULTS))" --update-lock`
   (it refuses while a change still lacks its bump).
4. Update this file (the settings table is checked by `tests/unit/test_docs.py`, which prints the block to paste),
   `docs/RUNBOOK.md` and `CHANGELOG.md`.
5. Deploy after the close; a new version restarts its G1 trial by design.

## 16. Phase 2: multi-leg (designed, not built)

Defined-risk index structures (credit and debit spreads, iron condors) are designed but built only if research says
they are viable. The code would live only in new files (`titan2/multileg/`, migration `0002_multileg.sql`,
`tests/multileg/`), plugged in through the `StructureHandler` protocol in `titan2/core/single_leg.py` and
`titan2/plugins.py`. Rules already fixed: size by maximum loss; buy the wings first and never sell more than the
filled hedge; close the shorts first; indices only, MIS only, no position into expiry day; basket margin ≤ 80% of
available; its own paper period and its own G1. Freeze limits (NIFTY 1,800, BANKNIFTY 600 units) would then apply.

## 17. v1 history

v1 ("Titan", last production snapshot 2026-07-14) was a 76K-line autonomous options bot: GPT-4o and ML scoring,
about 10 thread types, GTT and SL-M safety nets, reversals, auto-pilot, OI signals. It ran as `titan-bot` on the same
EC2 host and lost real money. The failures that shaped v2:

| v1 failure | v2 answer |
|---|---|
| Oversize on cheap options (NMDC-like lots of 6,750 at ₹3.5) | 1 lot, ₹20 premium floor, one `size()` taking the smallest of six caps, no multiplier |
| P&L from LTP; phantom profits on frozen quotes (SONACOMS) | P&L only from fills; exits on the realizable bid of a quote ≤ 3 s old |
| Internal stop "hit" with no real exit (ZYDUSLIFE) | every exit is a real IOC; a leg closes only when the broker shows flat |
| Reverse churn and a thin-option spread spiral (SAMMAANCAP) | no reversals; liquidity gate on both sides of the book; cooldowns |
| Recon selling a fresh entry; `abs()` hiding shorts | lag-safe, signed, post-fill reconciliation that never sells |
| Many order paths, monkey-patched Kite, a dashboard that re-logged in | one order adapter, unpatched Kite asserted at startup, only the token unit logs in |
| CI deploy restarting the bot on every push | no CI deploy; immutable releases; v1 units masked in Stage 0 |

Where v1 lives now: the git tag `v1-final` (commit `b4694f3`) holds every v1 file; the v1 docs are in
[docs/v1/](docs/v1/README.md); the research data (`trade_ledger/`, `daily_summaries/`, the `*_dump.json` files,
`speed_gate_log.jsonl`, `ml_models/data/`, `ml_models/oi_snapshots/`) stays in the tree, and so do the model folders
`ml_models/saved_models/`, `ml_models/reports/` and `ml_models/direction/` (user rule). `.kite_access_token` and
`dhan_config.json` stay tracked until their deferred cleanup. The `_*.py` research scripts are kept unchanged; the
ones that import v1 modules run from a v1 worktree: `git worktree add ../titan_v1 v1-final`.
`tests/arch/v1_retired.json` records what the deletion removed and kept, and `tests/arch/test_v1_retired.py` holds
the tree to it.
