# Titan v2 — Final Spec: DETAIL companion (long-form elaboration)

This companion elaborates FINAL_SPEC.md section by section (same numbering, same decisions) with rationale, worked examples and extended tables. FINAL_SPEC.md is normative and wins on any conflict.


Chief-architect synthesis, 2026-10-10.

The base is the **ops** design, which two of the three judges picked. Two other designs contributed:
- **correctness**: the functional core with write-ahead commits, invariants enforced by the database, the full order state machine, lag-safe reconciliation, and band-safe exit pricing.
- **minimal**: counters derived from rows, settings read once per process, an independent persisted paper ledger, `reactor.callFromThread`, the live paper twin, a daily live self-test, and one version hash.

Every fatal flaw any judge found is fixed (§0.3). Every "missing everywhere" item is handled.

Line references point to the production snapshot `b4694f3` under `agentic_trader/`. **[V]** marks a fact the implementer must re-verify against the live API or broker during the paper burn-in (§17.2).

## 0. Summary

### 0.1 What v2 is
- **One process owns every order.** `titan2-bot` is the only holder of the Kite session and the only writer of trading state.
  - The dashboard (`titan2-dash`) runs as a separate OS user with no broker credentials. It reads files that the bot publishes.
  - It can insert only three command kinds into its own SQLite file: PAUSE, RESUME, FLATTEN_ALL.
- **Paper and live share one code path.**
  - The engine, gateway, exit engine, sizing, risk, reconciliation and charges code are the same for both.
  - Only the broker adapter differs: `KiteBroker` for live, `PaperExchange` for paper.
  - `PaperExchange` fills by walking live depth after a simulated latency. It keeps its own persisted ledger and reconciles exactly like Kite.
- **P&L comes only from fills plus exact Zerodha charges, through one function.**
  - Exits are judged on the realizable bid.
  - Every loss is capped three ways: per trade, per day (realized plus bid MTM), and per worst case (total premium at risk).
- **No strategy trades real money until it proves an edge.** Strategies start in PAPER. LIVE needs all of the following:
  - a G1 certificate (multiple-testing-controlled, day-clustered, placebo-controlled, reconciliation-clean);
  - `ALLOW_LIVE=1` in the env file;
  - a signed-off CLI unlock bound to the G1 report hash;
  - a daily non-filling order-path self-test.
- **The expected honest outcome is no LIVE strategy.** The brief (§0, §1) shows no entry rule with a positive out-of-sample net expectancy. The system's job is to make engineering losses impossible and to measure honestly.

### 0.2 Guarantees
| # | Guarantee | Mechanism | § |
|---|---|---|---|
| G1 | No order is placed except by the bot's core, after every gate | The single core thread decides. Only `broker/kite.py` calls Kite order endpoints (asserted at runtime and by an AST test). The dashboard has no credentials | 3, 12 |
| G2 | A position exists only for broker-confirmed filled quantity | Leg qty is derived from fills. IOC orders reach a terminal state. An OPEN status is not a fill | 4, 5 |
| G3 | The bot never sells more than the broker holds, or more than its own fills explain | Exit qty = min(open_qty, post-fill broker net long). Verify-flat never sells a residual | 9.4 |
| G4 | P&L is booked exactly once, from fills and exact charges, by one function | `charges.net_pnl`. The `trades` PK and an immutability trigger. Booked only inside the CLOSED transition | 5.5, 8.1 |
| G5 | Every stop decision uses a bid at most 3 s old | Otherwise a REST refresh; otherwise a protective exit at 5 s | 7 |
| G6 | Phase-1 size is 1 lot, never more than both sides of the book can absorb | One `size()`. Caps are tighten-only. A zero or invalid cap refuses the trade | 8.5 |
| G7 | The day's loss is hard-capped, even in a no-bid or host-death worst case | Worst-case gate (Σ open premium + realized ≤ ₹50k), plus the sticky daily stop on realized + bid MTM | 8.8 |
| G8 | When in doubt, stop entering and keep exiting | Every gate fails closed. Halts never block exits | 8.6–8.7 |
| G9 | A restart never double-enters and always resumes exits | Write-ahead order rows. Tag lookup. Exits resume before READY | 9 |
| G10 | Paper is as realistic as live, and is checked against live | Fill-time depth walk at 0.5 participation; latency; independent ledger; paired live twin; X2 falsifier | 6, 10.9 |
| G11 | LIVE is impossible without G1 and two explicit human keys | `ALLOW_LIVE` + `SET_MODE LIVE` with the report hash + daily self-test; invariant I14 | 10.8 |
| G12 | A deploy cannot overwrite state, secrets or edits made on the server | Separate OS users; immutable releases; manifest drift abort; no CI deploy job | 13 |

Not guaranteed:
- positive expectancy;
- fills during exchange or broker outages;
- protection against a host failure in the minutes before Zerodha's MIS auto square-off at 15:26.

### 0.3 Fatal flaws found by the judges, and their fixes
| Flaw (design) | Fix in this spec | § |
|---|---|---|
| A blocking ExecStartPre prep/precheck prevents restarts while positions are open (correctness, minimal) | No blocking ExecStartPre. The self-check runs in-process; a failure raises entry halts, so the bot runs exits-only. `Type=notify`; READY after recovery; `TimeoutStartSec=300` | 13.3 |
| No external dead-man (correctness) | A healthchecks.io ping from the alerts thread, sent only while the core heartbeat is fresh | 13.9 |
| v1 is not masked, and the CI `deploy.yml` restarts `titan-bot` on every push (all three) | Stage 0: disable and mask the v1 units, clear the crontab, delete `deploy.yml` and the `EC2_SSH_KEY` secret, move the v1 tree aside. New unit names `titan2-*` | 13.11 |
| PaperBroker kept only in memory, so restarts cause mismatches (correctness); paper ledger rebuilt from engine fills, so reconciliation is circular (ops) | `PaperExchange` persists its own `paper_exchange.db`, written only by the paper-sim thread | 6 |
| The VERIFY_FLAT fallback sells the broker's residual quantity (correctness) | Verify-flat never sells. A residual becomes MISMATCH + FOREIGN | 9.4 |
| EXTERNAL is declared on a single positions read (minimal, ops) | Only post-fill snapshots count. EXTERNAL needs 2 post-fill snapshots ≥3 s apart, outside a 5 s fresh-fill grace | 9.2–9.4 |
| The exit ladder goes down to −50% and loops on LPP rejects (ops) | Sweep-based bands, capped at 12% below the fresh bid. A band reject re-prices near the bid instead of going deeper | 5.7 |
| A STUCK order freezes the trade as UNRESOLVED (ops) | The trade stays EXITING and the ladder resumes once the order resolves. The book is halted for entries only | 5.3 |
| Hot-reloaded params change open trades (ops) | Settings are read once per process. ExitParams are frozen at the entry fill | 7, 8.4 |
| The single loop thread does blocking I/O, including Telegram (minimal) | Network I/O happens only on the lane, paper-sim and alerts threads | 3 |
| After an exception the loop continues with half-applied state (minimal) | Roll back, reload the engine from the DB, raise a scoped halt. More than 20 exceptions a minute → exit 3 | 3.3 |
| The deploy updates the shared venv in place (minimal) | One venv per lock hash; immutable release; atomic `current` symlink | 13.6 |
| The live self-test runs before 09:15 and is always rejected (minimal) | Runs at 09:15:30, with a retry at 09:17:00 | 10.8 |
| Paper takes 100% of displayed depth (minimal) | Participation 0.5, measured at fill time | 6 |
| The live adapter is built only in LIVE mode (correctness, minimal) | The live order lane is always built, with `entries_allowed=ALLOW_LIVE` | 3.2 |
| Ceilings sit above the defaults; a `SET_SETTING` UI command exists; G1 is keyed to params only (correctness) | Ceilings = defaults (tighten-only). No UI can write settings. The version hash covers params, the exit profile, every versioned setting, and the engine and paper semantics versions | 8.4, 10.2 |
| No placebo floor (correctness); placebo judged at 1.5× stress (ops) | Placebo n ≥ 50 and coverage ≥ 80%, judged at 1.0× spreads | 10.7 |
| KiteTicker subscribe calls come from off the reactor thread (correctness, ops) | All subscribe/unsubscribe/set_mode calls go through `reactor.callFromThread`; the desired set is re-applied on connect | 3.2, 9.7 |
| REST quotes are not batched, and paper competes with live (ops) | The reads lane batches up to 500 symbols per call with priority EXIT > ENTRY_LIVE > RECON > PAPER > BACKGROUND. Paper-sim has its own thread | 3.2 |
| The emergency-flatten CLI is a second order-placing process (ops) | Removed. Flatten is a command to the bot and persists if the bot is down. Bot-down fallback is the Kite app. `bot --safe` is the same process | 13.12 |
| No single-instance lock (correctness) | `flock` on `/var/lib/titan2/run/titan2.lock` | 3.1 |
| ESTIMATED and EXTERNAL trades are dropped from G1 (minimal) | Any EXTERNAL or UNRESOLVED trade in a PAPER book fails G1. BROKER_SQUAREOFF trades are counted at their real cost | 10.7 |

The "missing everywhere" items are handled here:

| Item | § |
|---|---|
| STT 0.15% on sell and on exercise; NSE transaction charge 0.03553% | 8.1 |
| Positions-API lag | 9.2, 16 |
| Multiple testing | 10.7 |
| Operator-selection bias | 8.6, 10.7 |
| Live population vs G1 population | 10.5 |
| Paper-model validation and probation | 10.9 |
| Backtested exit profile | 10.3 |
| Power and futility | 10.7 |
| Phase-2 G1 | 14.8 |
| F&O ban list and contract blocks | 11.6 |
| Stock physical-settlement window | 11.4 |
| Live vs historical bar check | 10.3 |
| Hard worst-case loss cap | 8.8 |
| Stop debounce | 7.4 |
| MIS auto square-off hand-off and the ₹59 fee | 5.6, 8.1 |
| Session calendar | 11.7 |
| Token rotation and WS backfill | 9.7 |

### 0.4 Requirement trace
| Req | § | Req | § | Req | § |
|---|---|---|---|---|---|
| R1 | 3, 12 | R6 | 8 | R11 | 9 |
| R2 | 5 | R7 | 8.5 | R12 | 13 |
| R3 | 8.1 | R8 | 10 | R13 | 15 |
| R4 | 6 | R9 | 11 | R14 | 16 |
| R5 | 7 | R10 | 4.6, 12 | R15 | 14 |

### 0.5 Conventions
- **Time.**
  - Wall time is an IST-aware `datetime` (`zoneinfo("Asia/Kolkata")`), stored as ISO-8601 with offset.
  - Every age and timeout uses `time.monotonic()`. Monotonic values are never persisted; after a restart they are re-anchored to "now".
- **Money.** Rupees are `float`. Persisted money is rounded to 2 dp. Tick math uses integer ticks (§4.1).
- **"Fails closed"** means: if an input is missing, stale, unparseable, or raises, the entry is refused. It never blocks an exit.
- **Python 3.10+.**
  - Use `from __future__ import annotations`, `dataclasses(slots=True)` and `zoneinfo`.
  - Do not use `tomllib`, `typing.Self`, `datetime.UTC` or `ExceptionGroup`.
  - Every module except `runtime.py` and `market/feed.py` must import on Windows without twisted or fcntl, because developers run pytest on Windows.
- **Dependencies.**
  - Runtime: `kiteconnect==5.0.1` (pinned and hash-locked), `requests`, `pyotp`, `flask`, `gunicorn`.
  - Dev: `pytest`.
  - Not used: pandas, numpy, scipy, openai, schedule, python-dotenv.

## 1. Architecture choice

**Option B (lean core), with an operational strangler cutover.**
- `titan2` is a new package running in a new process, with a new DB and new systemd units.
- Proven v1 logic is *ported*: copied, corrected and given a provenance header. It is never imported.
- On the server, v1 is frozen, masked and moved aside. It never trades again.
- Here "strangler" means only that the server changes owner in reversible stages (§13.11). There are never two engines sharing tables or a broker account.

Why B, in terms of the goal and the brief:
- **The §4.1 live killers sit on the seams between modules.** Examples:
  - at least 12 exit callers, each doing its own LTP accounting;
  - P&L spread over 4 modules;
  - about 25 risk call sites;
  - 3 JSON files used for IPC;
  - a process-wide `KiteConnect.place_order` monkey-patch (zerodha_tools 23-99);
  - about 10 thread types.

  Option A (prune) keeps every seam. Option C (strangler) keeps two order owners. "Impossible" means bad states cannot be represented at all: no order path outside the gateway, no position without a fill, no second writer.
- **Nothing in v1 is worth keeping alive.** No rule has a positive out-of-sample Exp_net (brief §0), and the order and P&L path is unsafe (§4.1). EC2 is stopped, so there is no running service to protect. The ≥30-session paper period is the burn-in.
- **Testability.** Every component runs synchronously against fakes. Running every scenario through both brokers proves there is only one paper/live path (§16.2).

**Structural decisions (binding):**
1. **Functional core, imperative shell.**
   - `Engine.handle(event) -> list[Command]` runs on one core thread, inside a DB transaction.
   - Commands are dispatched only after the commit.
   - All network I/O happens on lane threads, which return results as events.
2. **One SQLite writer per file.**
   | File | Only writer |
   |---|---|
   | `titan2.db` | core thread |
   | `paper_exchange.db` | paper-sim thread |
   | `commands_dash.db` | dashboard |
   | `commands_cli.db` | CLI |
   | `pub/*.json` | core thread |
3. **Invariants are enforced by the database first** (CHECK constraints, partial unique indexes, triggers), then by runtime checks I1–I16, then by tests.
4. **Only LIMIT + IOC + MIS orders exist.** Phase 1 has no MARKET, no SL/SL-M and no GTT orders. An AST test forbids the identifiers.
5. **No v1 imports.**
   - `titan2` imports nothing from `agentic_trader/*.py` (AST test).
   - Startup asserts that `KiteConnect.place_order` is unpatched.

## 2. Module list

Repo layout. All new code lives under `agentic_trader/`; the server's WorkingDirectory stays `/home/ubuntu/titan/agentic_trader` (§13.1).
```
agentic_trader/
  titan2/                      # the package (python -m titan2 ...)
    __init__.py  __main__.py  clock.py  settings.py  model.py  charges.py  instruments.py
    store.py  ledger.py  g1.py  alerts.py  token.py  selfcheck.py  runtime.py  plugins.py
    migrations/0001_init.sql
    data/nse_sessions.json
    market/  __init__.py quotes.py feed.py bars.py
    broker/  __init__.py port.py kite.py lanes.py paper.py
    core/    __init__.py events.py state.py engine.py operator.py invariants.py
             gateway.py single_leg.py exit_loop.py exits.py sizing.py risk.py orders.py positions.py recon.py
    strategies/ __init__.py base.py opening_drive.py price_spike_up.py REGISTRY.lock
    dash/    __init__.py app.py templates/index.html
  tests/  conftest.py  fakes/  unit/  scenarios/  chaos/  arch/  golden/  ops/
  ops/    deploy.sh rollback.sh pull_drift.sh bootstrap.sh stage0_cutover.sh backup.sh titan2-cli
          systemd/*.service|*.timer  logrotate.titan2  sudoers.titan2  titan2.env.example  dash.env.example
  requirements.lock  requirements-dev.txt  pyproject.toml (pytest config only)
.github/workflows/ci.yml      # tests only, no deploy job
```

| File | Responsibility | ≈Lines | Ported from (b4694f3) |
|---|---|---|---|
| `__init__.py` | `__version__` | 5 | — |
| `__main__.py` | CLI (§13.2): bot, token, selfcheck, status, pause/resume/flatten-all/kill, ack-*, mode, live, recon, settings, trades, report, g1, is-trading-day, backup, selftest | 180 | argument-handling idea from autonomous_trader 13086-13138 |
| `clock.py` | `Clock`, `RealClock`, `FakeClock`, IST helpers, `SessionCalendar` (holidays and special sessions file) | 140 | — |
| `settings.py` | frozen `Settings` dataclass; `LIMITS` (bound, tighten direction, versioned flag); `load()`; `versioned_view()`; code constants | 230 | atomic JSON I/O from settings_manager 117-145 |
| `model.py` | enums, dataclasses (§4.1), `walk()`, tick math, tag make/parse | 300 | depth walk zerodha_tools 4632-4657 and dashboard 4255-4269; tick rounding zerodha_tools 49-57, changed to floor/ceil |
| `charges.py` | dated `RateSet`s, `order_charges`, `net_pnl`, `mtm_net`, `breakeven_px`, `exercise_stt` | 150 | replaces config.py 70-107, which used wrong rates |
| `instruments.py` | dump fetch, cache and sanity checks; indexes; DTE rule; `select_contract`; `opposite`; universe; ban list | 260 | expiry scan autonomous_trader 9429-9447; select_strike options_trader 3497-3550 with the clamp removed; index map config 1564-1569 |
| `store.py` + `migrations/0001_init.sql` | WAL connection, migrations (`PRAGMA user_version`), `tx()`, typed DAO, derived-counter queries, read-only opener | 360 + 230 SQL | pragmas from state_db 226-237, with `synchronous` changed to FULL |
| `ledger.py` | trade-row builder, daily report, `pub/` exports | 180 | append pattern from trade_ledger 457-481 |
| `g1.py` | trial registry, looks, bootstrap, placebo, futility, confirmation, certificate, live review | 280 | — |
| `alerts.py` | alert queue thread, Telegram, healthchecks ping, dedup, repeating CRITICAL alerts | 140 | — |
| `token.py` | session file, headless TOTP login, manual request-token exchange; never restarts services | 200 | kite_token_manager 52-307, without `restart_titan_bot` (312-330) |
| `selfcheck.py` | phases: morning, prestart, deploy, cutover | 190 | pre_market_check 47-85, 116-142, 169-186 |
| `runtime.py` | threads, queues, flock, sd_notify, startup sequence, core loop, SIGTERM | 300 | — |
| `plugins.py` | builds the `StructureHandler` map; loads the optional `titan2.multileg` | 40 | — |
| `market/quotes.py` | `QuoteCache`: thread-safe, newest wins, `fresh()` returns None when stale | 120 | cache fields kite_ticker 53-94; freshness 491-589, fixed so it never returns a stale quote |
| `market/feed.py` | KiteTicker wrapper: desired subscriptions via `reactor.callFromThread`; ticks → `Quote` / `UnderlyingTick`; order updates; health; dirty set | 230 | kite_ticker 274-414, 645-689, 733-796 |
| `market/bars.py` | 5-min bars from ticks, invalidation, previous-session bars, live-vs-historical check | 160 | volume delta from kite_ticker 1507-1515 |
| `broker/port.py` | `BrokerPort` and `DataPort` protocols, normalized broker types, error classes | 110 | — |
| `broker/kite.py` | `KiteBroker` + `KiteData`: LIMIT/IOC only, error mapping, normalization, thread assert, rate buckets | 260 | tick rounding zerodha_tools 49-57; reject read 75-92; history poll 1383-1405 |
| `broker/lanes.py` | `OrderLane` (LIVE), `ReadsLane` (batching + priority), `TokenBucket` | 200 | — |
| `broker/paper.py` | `PaperExchange` + `fill_from_depth()` (pure, shared with FakeKite), persistence, auto square-off, margins | 340 | — |
| `core/events.py` | Event and Command dataclasses (§4.2) | 140 | — |
| `core/state.py` | `EngineState`: in-memory indexes and loaders | 160 | — |
| `core/engine.py` | dispatch, timers, EOD timeline, status/pub, self-test, nightly G1 trigger | 340 | — |
| `core/operator.py` | command validation, expiry and execution (PAUSE … SET_MODE) | 140 | — |
| `core/invariants.py` | I1–I16 checks | 100 | — |
| `core/gateway.py` | intent routing, pre-reserve gates, reservation, decision-quote requests, plan → orders, twin, placebo | 260 | gate-order idea from zerodha_tools 5056-5234 |
| `core/single_leg.py` | `StructureHandler` for LONG_CE / LONG_PE | 120 | — |
| `core/exit_loop.py` | per-leg exit attempts, broker cap, verify-flat, backstop, STOP_SENDING, cover | 220 | — |
| `core/exits.py` | pure `decide()`, `compute_exit_params()`, `entry_limit()`, `exit_limit()`, `realizable_bid()` | 220 | hard SL exit_manager 445-485; ratchet 1332-1406, re-anchored to BE |
| `core/sizing.py` | the single `size()` | 80 | — |
| `core/risk.py` | pure gates, liquidity check, halt logic, MTM, daily stop, worst-case cap | 300 | liquidity zerodha_tools 4547-4669, made fail-closed, two-sided and mid-based |
| `core/orders.py` | order state machine, `apply_cum`, timers | 220 | — |
| `core/positions.py` | leg/structure state machine, `close()`, `close_external()` | 200 | — |
| `core/recon.py` | pure `reconcile()`, recovery planner, EOD truth check | 300 | broker fetch mapping position_reconciliation 248-281, made signed and per product; errors lead to no inference |
| `strategies/base.py` | `Strategy` ABC, `MarketView`, `make_intent`, registry, version hash | 140 | — |
| `strategies/opening_drive.py` | §10.3 | 120 | — |
| `strategies/price_spike_up.py` | §10.4 | 190 | kite_ticker 1407-1435, 1507-1533, 2049-2108; sustain 1701-1848 rewritten as signed |
| `dash/app.py` + `index.html` | read-only UI + 3 commands | 200 + 220 | meaning of the broker-truth fields from dashboard 2033-2066 |

Totals:

| Part | ≈Lines |
|---|---|
| Python | 7.1K (about 1.1K ported, about 6.0K new) |
| SQL | 0.23K |
| HTML | 0.22K |
| Shell and systemd units | 0.6K |
| Tests | 3.5K |

Phase 2 adds about 0.8K lines in `titan2/multileg/` (§14).

**Import rules** (enforced by `tests/arch`):
- `strategies/*` may import only `titan2.strategies.base`, `titan2.model` and the stdlib.
- `core/*` may not import `kiteconnect`, `broker.kite`, `broker.paper`, `market.feed` or `dash`.
- Only `broker/kite.py` references the Kite order methods (`place_order`, `cancel_order`, `modify_order`).
- No module references any of:
  - the GTT methods `place_gtt`, `modify_gtt`, `delete_gtt`;
  - the order-type constants `ORDER_TYPE_MARKET`, `ORDER_TYPE_SL`, `ORDER_TYPE_SLM`;
  - the strings `"MARKET"` and `"SL-M"`;
  - the identifier `multiplier`.
- `dash/*` may not import `kiteconnect`, `titan2.broker`, `titan2.core` or `titan2.store`.
- No `import config`, `import zerodha_tools` or any other v1 module name.


## 3. Concurrency and process model

### 3.1 Processes and OS users
| Process | OS user | Credentials | Writes |
|---|---|---|---|
| `titan2-bot` (exactly one, `flock(LOCK_EX\|LOCK_NB)` on `/var/lib/titan2/run/titan2.lock`; failure → exit 73) | `titan` | `/etc/titan2/titan2.env`, `session/kite_session.json` | `db/titan2.db`, `db/paper_exchange.db`, `pub/`, logs, reports, exports |
| `titan2-dash` (gunicorn `-w 1 --threads 4 -k gthread -b 127.0.0.1:8050`) | `titandash` | none: cannot read `titan2.env` or `session/` | `dash/commands_dash.db` only |
| `titan2-token` (oneshot, 08:30) | `titan` | env | `session/kite_session.json` |
| `titan2-selfcheck` (oneshot, 08:40) | `titan` | env | `reports/selfcheck-*.json` |
| `titan2-backup` (oneshot) | `titan` | none | `/var/backups/titan2` (uses SQLite `.backup`, a reader) |
| CLI `titan2 …` (`sudo -u titan`) | `titan` | env (read) | `db/commands_cli.db`; `config/settings.json` (via `settings set`) |

The CLI never writes `titan2.db`. Mutating CLI verbs insert a command and wait for the bot's ack (§13.2).

### 3.2 Threads inside `titan2-bot`
| Thread | Work | May write | Network |
|---|---|---|---|
| `core` (main) | `Engine.handle()` for every event, in a DB tx; dispatches commands; Timer every 250 ms; polls command DBs every 1 s; publishes `pub/status.json` every 1 s; sd_notify | `titan2.db`, `pub/`, all trading state | none |
| `kite-ws` (Twisted reactor, `KiteTicker.connect(threaded=True)`) | **on_ticks:** puts option and underlying quotes into `QuoteCache`; pushes `UnderlyingTick` batches to `signal_q`; adds held or pending option symbols to the dirty set and posts one coalesced `QuotesDirty`. **on_order_update:** posts `OrderSnapshot(account=LIVE, source=WS)`. **Connection callbacks:** post `FeedState`. Also applies subscription changes | `QuoteCache`, dirty set, queues | WS |
| `signals` | bars and strategies on underlying ticks plus `on_timer`; posts `IntentReceived` and `BarsClosed` | its own strategy objects | none |
| `lane-live-orders` | `KiteBroker` order calls (`place`, `cancel`, `order_history`, `order_trades`), one at a time | nothing (posts events) | Kite REST |
| `lane-reads` | REST `quote` (batched), `ltp`, `positions`, `orders`, `trades`, `margins`, `instruments`, `historical_data`, contract note, NSE ban-list HTTP | `QuoteCache` (REST quotes) | Kite REST, NSE |
| `paper-sim` | `PaperExchange` for accounts PAPER and SHADOW: request queue plus due-event heap | `paper_exchange.db` | none (depth comes from `QuoteCache`, or a REST request to `lane-reads`) |
| `alerts` | Telegram and healthchecks HTTP, with retry | none | HTTPS |

**Queues**
| Queue | Bound | On overflow |
|---|---|---|
| `core_q` | unbounded `queue.Queue` | never drops |
| `signal_q` | 20,000 tick batches | drop oldest; set `degraded`, which raises the STALE_FEED halt |
| `reads_q` | priority `heapq` + condition | — |
| `live_q`, `paper_q` | FIFO | — |
| `alert_q` | 1,000 | drop oldest; increment `alerts_dropped` |

**Dirty-set protocol**
1. The ws thread adds a symbol to `dirty` under a lock.
2. If `dirty_posted` is false, it sets it to true and posts `QuotesDirty()`.
3. The core calls `feed.take_dirty()`, which swaps the set out and clears the flag, then evaluates exits for those symbols.

**Subscriptions**
- The core emits `Subscribe`/`Unsubscribe` commands. These go to `Feed.request()`, which updates `desired: dict[token, mode]` under a lock and calls `reactor.callFromThread(self._apply)`.
- `_apply` runs on the reactor thread. If connected, it diffs `desired` against `applied` and calls `ticker.subscribe`, `set_mode` and `unsubscribe`.
- `on_connect` (reactor thread) re-applies the full desired set.
- `KiteTicker` methods are never called from any other thread. In kiteconnect 5.0.1, `subscribe`/`set_mode` call `ws.sendMessage` directly, which is not thread-safe.

**Rate buckets**
- Shared, thread-safe, blocking. They never fail open.
- Orders 8/s (burst 3); quote 1/s (≤500 instruments per call); historical 3/s; other 8/s (burst 3).

**`lane-reads` priority** (lowest number served first)
| Priority | Requests |
|---|---|
| 0 EXIT | exit quotes; positions/orders for the exit cap and verify-flat |
| 1 ENTRY_LIVE | any decision request that includes a LIVE book |
| 2 RECON | reconciliation reads |
| 3 PAPER | paper decision quotes; paper-fill depth |
| 4 SHADOW | shadow requests |
| 5 BACKGROUND | historical, instruments, contract note, ban list |

- Each `quote()` call collects symbols from pending requests in priority order, up to 500.
- A request completes when all its symbols have arrived.
- A request times out after 3 s and then yields `QuoteResult(ok=False, error="TIMEOUT")`.

**Live order lane**
- Always constructed, because exits of any bot-owned live position need it.
- Built as `KiteBroker(entries_allowed = (env ALLOW_LIVE == "1"))`.
- The adapter refuses roles ENTRY and SELFTEST when entries are not allowed (`PlaceResult REJECTED "LOCAL: live entries disabled"`). This is defence in depth behind I14.

### 3.3 Core loop (`runtime.core_loop`): write-ahead, commit before dispatch
```python
def core_loop(rt):
    engine = Engine.load(rt.store, rt.ctx)
    next_timer = rt.clock.mono()
    while not rt.stopping:
        now = rt.clock.mono()
        if now >= next_timer:
            ev, next_timer = Timer(rt.clock.now_wall(), now), now + 0.25      # timers run even under load
        else:
            try: ev = rt.core_q.get(timeout=next_timer - now)
            except queue.Empty: continue
        try:
            with rt.store.tx():                         # BEGIN IMMEDIATE ... COMMIT  (synchronous=FULL)
                cmds = engine.handle(ev)                # mutates state + Store DAO writes inside the tx
                engine.check_invariants()               # raises InvariantViolation -> rollback
        except InvariantViolation as iv:
            engine = Engine.load(rt.store, rt.ctx)
            with rt.store.tx(): cmds = engine.on_invariant_violation(iv)   # halt or ANOMALY_EXIT + alert
        except Exception as exc:
            engine = Engine.load(rt.store, rt.ctx)
            with rt.store.tx():
                cmds = engine.raise_halt(kind="EXCEPTION", scope="GLOBAL", clears="ACK", reason=repr(exc)[:500])
            if rt.exc_window.hit_and_count(now, 60) > 20: rt.exit(3)       # systemd restarts -> recovery
        for c in cmds: rt.dispatch(c)                   # only after commit: order rows exist before HTTP
        if isinstance(ev, Timer): rt.heartbeat.core_progress(rt.clock.mono())
```
- **After a reload,** the engine re-polls every non-terminal order and runs an immediate recon. It ignores results for unknown `request_id`s and always applies `OrderSnapshot`s by tag or broker id. This is the restart path without process exit.
- **Watchdog.** `WATCHDOG=1` is sent from the core loop only when the last Timer finished less than 2 s ago (`WatchdogSec=30`).
- **Healthchecks.** The alerts thread pings only when `heartbeat.core_progress` is less than 10 s old.

### 3.4 Who may mutate what
| Rule | Enforcement |
|---|---|
| Every Engine method that writes, and every Store write method, runs only on the core thread | `@core_only` asserts `threading.get_ident() == CORE_TID`, else `InvariantViolation("I1")`. The test harness sets `CORE_TID` |
| Kite order methods run only on `lane-live-orders` | `KiteBroker` order methods assert `threading.current_thread().name == "lane-live-orders"` (I2); tests pass `thread_check=False`. The reads lane receives `KiteData`, which has no order methods |
| Shared containers are limited | Only `QuoteCache`, the dirty set, the queues and the token buckets are shared. Quotes are immutable values replaced under a short lock |
| Paper state has one owner | `PaperExchange` state is touched only by `paper-sim` (inline in tests) |
| Settings cannot change | `Settings` is immutable after `load()` |
| No monkey-patching | Startup asserts `KiteConnect.place_order.__module__ == "kiteconnect.connect"` and `__qualname__ == "KiteConnect.place_order"` |

### 3.5 How v1's races become impossible
| v1 race (map evidence) | v2 structure |
|---|---|
| About 10 thread types call ZerodhaTools; only `_trade_lock` serialises placement | Every decision is made on `core`; the order lane makes one call at a time |
| Locks held across about 6 s of broker I/O (zerodha_tools 1614-1712) | The core never does network I/O; results come back as events |
| `manual_exit_requests.json` has 2 consumers; 3 JSON IPC files | Commands are SQLite rows with one consumer, the core |
| Bot plus 2 gunicorn workers rewrite `active_trades` wholesale (state_db 269-300) | One writer per DB file; row-level writes; the dashboard never opens `titan2.db` |
| `exit_manager.trade_states` mutated from 3 threads without a lock | Exit state lives in core-owned `Leg` objects |
| Recon thread mutates positions and places orders (position_reconciliation 370-405) | `reconcile()` is pure and runs on the core; it never creates entries |
| `_on_ticks` holds the cache RLock through detection and file I/O (kite_ticker 645-731) | The ws thread only updates dicts and enqueues; detection runs on `signals` |
| Watcher built before settings apply (autonomous_trader 133 vs 340) | Fixed startup order (§9.5); settings are immutable and passed in |
| Process-wide monkey-patch (zerodha_tools 23-99) | No patch; asserted at startup; LIMIT orders need no `market_protection` |

### 3.6 Runtime invariants (checked after every event)
| ID | Invariant | Check |
|---|---|---|
| I1 | Trading state is mutated only on the core thread | `@core_only` |
| I2 | Kite order endpoints are called only from `lane-live-orders` | thread-name assert in `KiteBroker` |
| I3 | `0 ≤ open_qty(leg) = Σ entry fills − Σ exit/cover fills ≤ Σ entry fills` | after every event |
| I4 | At most 1 non-terminal bot order per leg | DB index + check |
| I5 | At most 1 non-terminal structure per (book, underlying), and per (LIVE account, underlying) | DB indexes |
| I6 | Exit/cover order qty ≤ min(open_qty, cap from §9.4) | at order creation |
| I7 | Phase 1: ENTRY ⇒ BUY, EXIT ⇒ SELL, COVER ⇒ BUY, SELFTEST ⇒ BUY | DB CHECK |
| I8 | Every OPEN leg has frozen ExitParams with `0 < hard_sl_px < entry_avg < tp_px` and `floor_px > be_px` | after every event |
| I9 | `filled_qty` only increases; terminal orders are immutable | DB triggers |
| I10 | P&L is booked once per structure | `trades` PK + immutability trigger |
| I11 | FULL books: open + reserved ≤ MAX_POSITIONS_PER_BOOK and entries ≤ MAX_ENTRIES_PER_DAY | after every event |
| I12 | Order qty is a multiple of lot and `lot ≤ qty ≤ min(caps)` | at order creation |
| I13 | Limits are tick multiples. ENTRY: `ask ≤ limit ≤ floor_tick(mid×1.01)` at decision. EXIT: `limit ≥ floor_tick(ref_bid×(1−MAX_EXIT_DISCOUNT))` | at order creation |
| I14 | No ENTRY/SELFTEST order on account LIVE unless ALLOW_LIVE=1, the strategy is LIVE with a valid unlock, and today's self-test has passed (SELFTEST orders are exempt from the self-test condition) | at order creation |
| I15 | Wall clock does not jump more than 2 s against monotonic | Timer (WARN alert only) |
| I16 | Settings stay within LIMITS | at load |

**On violation**
- Global or account scope: `HALT(INVARIANT, clears=ACK)` plus a CRITICAL alert. Exits continue.
- Leg scope: the leg goes to `EXITING(ANOMALY_EXIT)` through the normal capped exit loop, plus a CRITICAL alert.

## 4. Core data model

### 4.1 `model.py` (types used by every unit; field names are binding)
```python
class Account(str, Enum):  LIVE = "LIVE"; PAPER = "PAPER"; SHADOW = "SHADOW"
class Policy(str, Enum):   FULL = "FULL"; QUALITY = "QUALITY"
class Mode(str, Enum):     OFF = "OFF"; SHADOW = "SHADOW"; PAPER = "PAPER"; LIVE = "LIVE"
class Direction(str, Enum): UP = "UP"; DOWN = "DOWN"
class StructureType(str, Enum): LONG_CE = "LONG_CE"; LONG_PE = "LONG_PE"       # phase 2 extends (§14)
class StrikePref(str, Enum): ATM = "ATM"; ITM1 = "ITM1"
class Role(str, Enum):  ENTRY = "ENTRY"; EXIT = "EXIT"; COVER = "COVER"; SELFTEST = "SELFTEST"; EXTERNAL = "EXTERNAL"
class Side(str, Enum):  BUY = "BUY"; SELL = "SELL"
class OrderState(str, Enum): NEW, SUBMITTING, UNKNOWN, WORKING, CANCEL_REQUESTED, COMPLETE, CANCELLED, REJECTED, NOT_PLACED
TERMINAL_ORDER = frozenset({COMPLETE, CANCELLED, REJECTED, NOT_PLACED})
class LegState(str, Enum):    PLANNED, ENTERING, OPEN, EXITING, VERIFY_FLAT, CLOSED, CLOSED_EXTERNAL, VOID
class StructState(str, Enum): RESERVED, ENTERING, OPEN, EXITING, VERIFY_FLAT, CLOSED, CLOSED_EXTERNAL, VOID
TERMINAL_STRUCT = frozenset({CLOSED, CLOSED_EXTERNAL, VOID})
class Urgency(str, Enum):   NORMAL = "NORMAL"; PROTECTIVE = "PROTECTIVE"
class PnlStatus(str, Enum): FINAL = "FINAL"; ESTIMATED = "ESTIMATED"; UNRESOLVED = "UNRESOLVED"
class HaltClears(str, Enum): NEXT_SESSION = "NEXT_SESSION"; ACK = "ACK"; AUTO = "AUTO"; RESUME = "RESUME"
EXIT_REASONS = ("HARD_SL","LOCK_STOP","TAKE_PROFIT","TIME_STOP","EOD_FLATTEN","STALE_QUOTE_PROTECTIVE","NO_BID_PROTECTIVE",
                "KILL_SWITCH","OPERATOR_FLATTEN","DAILY_STOP","ANOMALY_EXIT","RECOVERY_FLATTEN","SELFTEST_FILLED",
                "CLOSED_EXTERNAL","BROKER_SQUAREOFF")

@dataclass(frozen=True, slots=True)
class Level: price: float; qty: int; orders: int = 0

@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str                         # "NFO:RELIANCE26OCT2900CE" | "NSE:RELIANCE" | "NSE:NIFTY 50"
    token: int
    bids: tuple[Level, ...]             # best first, ≤5 levels, only price>0 and qty>0
    asks: tuple[Level, ...]
    ltp: float | None; volume: int | None
    last_trade_wall: datetime | None; exch_wall: datetime | None
    recv_mono: float; recv_wall: datetime        # receipt time of THIS snapshot (WS packet or REST response)
    source: str                                  # "WS" | "REST" | "SIM"
    bid -> float | None ; ask -> float | None    # properties: best level price or None
    def mid(self) -> float | None: ...           # None unless bid and ask exist and ask >= bid
    def spread_pct(self) -> float | None: ...    # (ask-bid)/mid*100
    def age(self, now_mono: float) -> float: ...

@dataclass(frozen=True, slots=True)
class WalkResult: avg_px: float | None; fillable_qty: int; worst_px: float | None
def walk(levels: tuple[Level, ...], qty: int, limit: float | None, side: Side) -> WalkResult
    # BUY walks asks taking price <= limit; SELL walks bids taking price >= limit; limit None = unbounded.

@dataclass(frozen=True, slots=True)
class UnderlyingTick: symbol: str; token: int; ltp: float; cum_volume: int; day_open: float | None
                      prev_close: float | None; exch_wall: datetime | None; recv_mono: float; recv_wall: datetime
@dataclass(frozen=True, slots=True)
class Bar: symbol: str; start: datetime; open: float; high: float; low: float; close: float
           volume: int; valid: bool; source: str          # "LIVE" | "HIST"
@dataclass(frozen=True, slots=True)
class Underlying: symbol: str; token: int; name: str; kind: str   # kind "STOCK" | "INDEX"; name = NFO name
@dataclass(frozen=True, slots=True)
class Instrument: symbol: str; token: int; tradingsymbol: str; exchange: str; name: str; underlying: str
                  opt_type: str; strike: float; expiry: date; lot_size: int; tick: float   # all from the dump row
@dataclass(frozen=True, slots=True)
class Recheck: lo: float; hi: float                 # entry requires lo <= underlying LTP <= hi (±inf allowed)
@dataclass(frozen=True, slots=True)
class LegSpec: opt_type: str; strike_rule: str; side: Side; ratio: int = 1     # phase 2 only

@dataclass(frozen=True, slots=True)
class Intent:
    intent_id: str; strategy: str; strategy_version: str
    underlying: str; direction: Direction; structure: StructureType
    strike_pref: StrikePref; expiry_rule: str          # phase 1: "NEAREST_ELIGIBLE" (§11.4)
    trigger_px: float; recheck: Recheck; ttl_s: float  # 0 < ttl_s <= 10
    created_mono: float; created_wall: datetime
    reason: Mapping[str, float | str]                  # logged only; never drives size or direction
    is_placebo: bool = False; parent_intent_id: str | None = None
    legs: tuple[LegSpec, ...] = ()

@dataclass(frozen=True, slots=True)
class ExitProfile:                                     # per strategy (in PARAMS, versioned); bounds §7.2
    hard_sl_pct: float = 15.0; lock_arm_pct: float = 4.0; lock_floor_pct: float = 3.0; trail_retain: float = 0.70
    tp_pct: float = 15.0; time_stop_min: float = 45.0; time_stop_when_locked: bool = False
    stop_confirm_ms: int = 400; stop_confirm_max_ms: int = 1500; stop_confirm_bypass_pct: float = 3.0
    no_bid_exit_s: float = 10.0

@dataclass(frozen=True, slots=True)
class ExitParams:                                      # frozen at the entry fill; JSON in structures.exit_params
    profile: ExitProfile; entry_avg: float; qty: int; entry_charges: float; rates_version: str
    k_exit: float; be_px: float; hard_sl_px: float; arm_px: float; floor_px: float; tp_px: float
    entry_fill_wall: datetime; time_stop_at: datetime

@dataclass(frozen=True, slots=True)
class DecisionQuote: bid: float | None; ask: float | None; mid: float | None; age_ms: int; src: str
                     ltp: float | None; walk_px: float | None

@dataclass(frozen=True, slots=True)
class OrderRequest:                                    # implied constants: NFO, LIMIT, IOC, MIS, variety "regular"
    account: Account; tag: str; role: Role; symbol: str; tradingsymbol: str; exchange: str; token: int
    side: Side; qty: int; limit_px: float; lot_size: int; tick: float

@dataclass(slots=True)
class Order:
    order_pk: str; leg_id: int; structure_id: int; account: Account; book: str; role: Role; side: Side
    symbol: str; qty: int; limit_px: float | None; attempt: int; band_idx: int | None
    state: OrderState = OrderState.NEW
    broker_order_id: str | None = None; broker_status: str | None = None; status_msg: str | None = None
    external_kind: str | None = None                   # role EXTERNAL: "MANUAL" | "AUTO_SQUAREOFF"
    filled_qty: int = 0; avg_fill_px: float | None = None
    decision: DecisionQuote | None = None
    created_wall: datetime | None = None; submitted_mono: float | None = None; terminal_wall: datetime | None = None
    lookups: int = 0; stuck: bool = False

@dataclass(frozen=True, slots=True)
class Fill: order_pk: str; seq_key: str; source: str; qty: int; px: float; wall: datetime; exch_wall: datetime | None
    # seq_key: "cum:<cum_qty>" (source CUM) | "trade:<trade_id>" (source TRADE)

@dataclass(slots=True)
class Leg:
    leg_id: int; structure_id: int; leg_no: int; account: Account; book: str; session: date; seq_no: int
    inst: Instrument; side: str                        # "LONG" (phase 1)
    state: LegState; orders: list[str]                 # order_pks, creation order
    stop_px: float | None = None; lock_armed: bool = False
    peak_rb: float | None = None; peak_wall: datetime | None = None
    trough_rb: float | None = None; trough_wall: datetime | None = None
    breach_first_mono: float | None = None; breach_quote_mono: float | None = None; no_bid_since_mono: float | None = None
    entry_fill_wall: datetime | None = None; last_fill_mono: float | None = None; last_fill_wall: datetime | None = None
    exit_reason: str | None = None; exit_urgency: Urgency | None = None; exit_band_idx: int = 0
    exiting_since_mono: float | None = None; broker_residual_qty: int = 0
    # derived only (no setter, never stored): entry_filled, exit_filled, open_qty

@dataclass(slots=True)
class Structure:
    structure_id: int; session: date; book: str; account: Account; policy: Policy
    strategy: str; strategy_version: str; intent: Intent; type: StructureType; underlying: str
    state: StructState; legs: list[int]; is_placebo: bool; parent_structure_id: int | None
    blocked_by: str | None; decision: Mapping | None   # decision quotes, plan, placebo plan (JSON)
    placebo_status: str | None                         # "OPENED" | "SKIPPED:<gate>" | None
    exit_params: ExitParams | None; risk_basis_rs: float | None
    exit_reason: str | None; void_reason: str | None
    reserved_wall: datetime; opened_wall: datetime | None; closed_wall: datetime | None
    pnl_status: PnlStatus | None

@dataclass(frozen=True, slots=True)
class Charges: brokerage: float; stt: float; txn: float; sebi: float; ipft: float; gst: float; stamp: float
               sqoff_fee: float                        # total = sum of fields
@dataclass(frozen=True, slots=True)
class PnL: gross: float; charges: Charges; net: float; n_executed_orders: int; rates_version: str

@dataclass(frozen=True, slots=True)
class Halt: halt_id: int; scope: str; kind: str; reason: str; operator: bool; clears: HaltClears
            created_wall: datetime; session: date
    # scope: "GLOBAL" | "ACCOUNT:<A>" | "BOOK:<book>" | "STRATEGY:<s>" | "UNDERLYING:<sym>" | "SYMBOL:<sym>"

# broker-normalized types
@dataclass(frozen=True, slots=True)
class BrokerOrder: account: Account; broker_order_id: str; tag: str | None; symbol: str; tradingsymbol: str
    exchange: str; product: str; side: Side; order_type: str; validity: str; qty: int; filled_qty: int
    avg_px: float | None; status: str                  # "WORKING" | "COMPLETE" | "CANCELLED" | "REJECTED"
    raw_status: str; status_msg: str | None; order_wall: datetime | None; exch_wall: datetime | None; placed_by: str | None
@dataclass(frozen=True, slots=True)
class BrokerTrade: account: Account; trade_id: str; broker_order_id: str; symbol: str; side: Side; qty: int
                   px: float; fill_wall: datetime | None; exch_wall: datetime | None
@dataclass(frozen=True, slots=True)
class BrokerPosition: account: Account; symbol: str; tradingsymbol: str; exchange: str; product: str
                      net_qty: int; buy_qty: int; sell_qty: int; buy_value: float; sell_value: float
@dataclass(frozen=True, slots=True)
class Margins: account: Account; available_rs: float; raw: Mapping
@dataclass(frozen=True, slots=True)
class FeedHealth: connected: bool; connected_since_mono: float | None; last_underlying_tick_mono: float | None
                  reconnects: int; degraded: bool
@dataclass(frozen=True, slots=True)
class Refusal: gate: str; detail: str
@dataclass(frozen=True, slots=True)
class Sized: qty: int; lots: int; binding: str; per_lot_risk_rs: float
```

**Tick math**
```python
floor_tick(x, t) = round(math.floor(x / t + 1e-9) * t, 2)
ceil_tick(x, t)  = round(math.ceil(x / t - 1e-9) * t, 2)
is_tick_multiple(x, t) = abs(x/t - round(x/t)) < 1e-6
```

**Tags (Kite allows ≤20 alphanumeric characters)**
- `make_tag(role, account, session, seq_no, attempt)` returns `"T2" + R + A + yymmdd + b36(seq_no, 3) + b36(attempt, 2)`: 15 characters, `[0-9A-Z]`.
- R (role): ENTRY=E, EXIT=X, COVER=C, SELFTEST=T.
- A (account): LIVE=L, PAPER=P, SHADOW=S.
- Ranges: `seq_no` is per (account, session, leg), 1..46655. `attempt` is 1..1295.
- `parse_tag(tag)` returns `ParsedTag | None`; it returns None for anything that is not exactly this format.
- `is_bot_tag(tag, account)` is true when `parse_tag` succeeds and the account letter matches.
- Example: `T2EP26101200101`.

### 4.2 `core/events.py`: events into `core_q` and commands out of `Engine.handle`
```python
# events
Timer(now_wall, now_mono) ; QuotesDirty() ; FeedState(connected, reconnected, degraded, reason)
IntentReceived(intent) ; BarsClosed(bars: tuple[Bar, ...])
QuoteResult(request_id, ok, symbols, missing, error)
PlaceResult(account, order_pk, outcome: "OK"|"REJECTED"|"AMBIGUOUS"|"FATAL", broker_order_id, error_class, message, throttled: bool)
CancelResult(account, order_pk, outcome: "OK"|"BENIGN"|"REJECTED"|"AMBIGUOUS"|"FATAL", message)
OrderSnapshot(account, broker_order_id, tag, status: "WORKING"|"COMPLETE"|"CANCELLED"|"REJECTED", filled_qty, avg_px,
              status_msg, exch_wall, source: "WS"|"POLL"|"LOOKUP"|"SIM"|"RECOVERY", recv_mono)
LookupResult(account, order_pk, ok, found: BrokerOrder | None)          # ok=False = API error, retry later
TradesResult(account, order_pk, ok, trades: tuple[BrokerTrade, ...])
PositionsSnapshot(account, ok, rows: tuple[BrokerPosition, ...], req_mono, recv_mono, error)
OrdersSnapshot(account, ok, orders: tuple[BrokerOrder, ...], req_mono, recv_mono, error)
TradebookSnapshot(account, ok, trades: tuple[BrokerTrade, ...], req_mono)
MarginsResult(account, ok, margins: Margins | None, recv_mono)
HistoricalResult(request_id, symbol, ok, bars: tuple[Bar, ...])
PrepResult(kind: "INSTRUMENTS"|"BAN_LIST"|"PREV_BARS", ok, detail)
ContractNoteResult(account, ok, rows: tuple[ContractNoteRow, ...])      # ContractNoteRow(broker_order_id, total, components)
Shutdown(reason)
# commands
Place(account, order_pk, req: OrderRequest)          # LIVE -> lane-live-orders ; PAPER/SHADOW -> paper-sim
Cancel(account, order_pk, broker_order_id) ; PollOrder(account, order_pk, broker_order_id)
LookupTag(account, order_pk) ; FetchTrades(account, order_pk, broker_order_id)
FetchPositions(account, priority) ; FetchOrders(account, priority) ; FetchTradebook(account) ; FetchMargins(account)
FetchQuotes(request_id, symbols: tuple[str, ...], priority: int)
FetchHistorical(request_id, symbol, token, frm, to, interval="5minute") ; Prep(kind)
ContractNote(account, orders: tuple[ContractNoteOrder, ...])
Subscribe(tokens, mode: "quote"|"full") ; Unsubscribe(tokens)
Alert(severity: "INFO"|"WARN"|"CRITICAL", code, text, dedup_key)
StartSignals() ; Exit(code)
```
Paper and SHADOW accounts accept the same broker commands (`Place`, `Cancel`, `PollOrder`, `LookupTag`, `FetchTrades`, `FetchPositions`, `FetchOrders`, `FetchTradebook`, `FetchMargins`), served by `paper-sim`. The engine never branches on the broker type.

### 4.3 `titan2.db` DDL (`migrations/0001_init.sql`). WAL, `synchronous=FULL`, `foreign_keys=ON`, `busy_timeout=5000`
```sql
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);               -- schema_version, install_id, last_session
CREATE TABLE seq(name TEXT PRIMARY KEY, value INTEGER NOT NULL);            -- "leg:<ACCOUNT>:<yymmdd>"

CREATE TABLE events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, mono REAL NOT NULL, session TEXT NOT NULL, kind TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'INFO' CHECK (severity IN ('DEBUG','INFO','WARN','CRITICAL')),
  account TEXT, book TEXT, strategy TEXT, structure_id INTEGER, leg_id INTEGER, order_pk TEXT, intent_id TEXT,
  payload TEXT NOT NULL CHECK (json_valid(payload)));
CREATE INDEX ev_session_kind ON events(session, kind);
CREATE INDEX ev_structure ON events(structure_id);
CREATE TRIGGER ev_no_upd BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'events append-only'); END;
CREATE TRIGGER ev_no_del BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'events append-only'); END;

CREATE TABLE intents(
  intent_id TEXT PRIMARY KEY, session TEXT NOT NULL, strategy TEXT NOT NULL, strategy_version TEXT NOT NULL,
  underlying TEXT NOT NULL, direction TEXT NOT NULL CHECK (direction IN ('UP','DOWN')), structure TEXT NOT NULL,
  strike_pref TEXT NOT NULL CHECK (strike_pref IN ('ATM','ITM1')), expiry_rule TEXT NOT NULL,
  trigger_px REAL NOT NULL CHECK (trigger_px > 0), recheck_lo REAL NOT NULL, recheck_hi REAL NOT NULL,
  created_ts TEXT NOT NULL, ttl_s REAL NOT NULL CHECK (ttl_s > 0 AND ttl_s <= 10),
  reason TEXT NOT NULL CHECK (json_valid(reason)), is_placebo INTEGER NOT NULL DEFAULT 0 CHECK (is_placebo IN (0,1)),
  parent_intent_id TEXT,
  CHECK ((direction='UP' AND structure='LONG_CE') OR (direction='DOWN' AND structure='LONG_PE')));   -- widened in phase 2

CREATE TABLE intent_routes(
  intent_id TEXT NOT NULL REFERENCES intents(intent_id), book TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK (outcome IN ('RESERVED','REFUSED','DIVERTED','EXPIRED')),
  gate TEXT, detail TEXT, structure_id INTEGER, decided_ts TEXT NOT NULL, PRIMARY KEY (intent_id, book));

CREATE TABLE structures(
  structure_id INTEGER PRIMARY KEY, session TEXT NOT NULL, book TEXT NOT NULL,
  account TEXT NOT NULL CHECK (account IN ('LIVE','PAPER','SHADOW')), policy TEXT NOT NULL CHECK (policy IN ('FULL','QUALITY')),
  strategy TEXT NOT NULL, strategy_version TEXT NOT NULL, intent_id TEXT NOT NULL REFERENCES intents(intent_id),
  type TEXT NOT NULL CHECK (type IN ('LONG_CE','LONG_PE')), underlying TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('RESERVED','ENTERING','OPEN','EXITING','VERIFY_FLAT','CLOSED','CLOSED_EXTERNAL','VOID')),
  is_placebo INTEGER NOT NULL DEFAULT 0, parent_structure_id INTEGER REFERENCES structures(structure_id), blocked_by TEXT,
  decision_json TEXT CHECK (decision_json IS NULL OR json_valid(decision_json)), placebo_status TEXT,
  exit_params TEXT CHECK (exit_params IS NULL OR json_valid(exit_params)), risk_basis_rs REAL,
  exit_reason TEXT, void_reason TEXT, reserved_ts TEXT NOT NULL, opened_ts TEXT, closed_ts TEXT,
  pnl_status TEXT CHECK (pnl_status IS NULL OR pnl_status IN ('FINAL','ESTIMATED','UNRESOLVED')));
CREATE UNIQUE INDEX one_active_per_book_underlying ON structures(book, underlying)
  WHERE state NOT IN ('CLOSED','CLOSED_EXTERNAL','VOID');                               -- I5 (RESERVED counts)
CREATE UNIQUE INDEX one_active_live_underlying ON structures(account, underlying)
  WHERE account='LIVE' AND state NOT IN ('CLOSED','CLOSED_EXTERNAL','VOID');
CREATE INDEX st_session_book ON structures(session, book);
CREATE TRIGGER st_terminal BEFORE UPDATE OF state ON structures WHEN OLD.state IN ('CLOSED','CLOSED_EXTERNAL','VOID')
  BEGIN SELECT RAISE(ABORT,'terminal structure immutable'); END;

CREATE TABLE legs(
  leg_id INTEGER PRIMARY KEY, structure_id INTEGER NOT NULL REFERENCES structures(structure_id), leg_no INTEGER NOT NULL,
  session TEXT NOT NULL, account TEXT NOT NULL, book TEXT NOT NULL, seq_no INTEGER NOT NULL,
  symbol TEXT NOT NULL, tradingsymbol TEXT NOT NULL, exchange TEXT NOT NULL, token INTEGER NOT NULL, underlying TEXT NOT NULL,
  side TEXT NOT NULL CHECK (side IN ('LONG')), opt_type TEXT NOT NULL CHECK (opt_type IN ('CE','PE')),
  strike REAL NOT NULL CHECK (strike > 0), expiry TEXT NOT NULL, lot_size INTEGER NOT NULL CHECK (lot_size > 0),
  tick REAL NOT NULL CHECK (tick > 0), product TEXT NOT NULL DEFAULT 'MIS' CHECK (product='MIS'),
  state TEXT NOT NULL CHECK (state IN ('PLANNED','ENTERING','OPEN','EXITING','VERIFY_FLAT','CLOSED','CLOSED_EXTERNAL','VOID')),
  stop_px REAL, lock_armed INTEGER NOT NULL DEFAULT 0, peak_rb REAL, peak_ts TEXT, trough_rb REAL, trough_ts TEXT,
  entry_fill_ts TEXT, last_fill_ts TEXT, exit_reason TEXT, exit_urgency TEXT, exit_band_idx INTEGER NOT NULL DEFAULT 0,
  exiting_since_ts TEXT, broker_residual_qty INTEGER NOT NULL DEFAULT 0,
  UNIQUE (structure_id, leg_no), UNIQUE (account, session, seq_no));

CREATE TABLE orders(
  order_pk TEXT PRIMARY KEY, leg_id INTEGER NOT NULL REFERENCES legs(leg_id), structure_id INTEGER NOT NULL,
  account TEXT NOT NULL, book TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('ENTRY','EXIT','COVER','SELFTEST','EXTERNAL')),
  side TEXT NOT NULL CHECK (side IN ('BUY','SELL')), symbol TEXT NOT NULL,
  qty INTEGER NOT NULL CHECK (qty > 0), limit_px REAL CHECK (limit_px IS NULL OR limit_px > 0),
  order_type TEXT NOT NULL, validity TEXT NOT NULL, product TEXT NOT NULL, variety TEXT NOT NULL,
  attempt INTEGER NOT NULL, band_idx INTEGER,
  state TEXT NOT NULL CHECK (state IN ('NEW','SUBMITTING','UNKNOWN','WORKING','CANCEL_REQUESTED','COMPLETE','CANCELLED','REJECTED','NOT_PLACED')),
  broker_order_id TEXT UNIQUE, broker_status TEXT, status_msg TEXT, external_kind TEXT,
  filled_qty INTEGER NOT NULL DEFAULT 0, avg_fill_px REAL,
  decision_bid REAL, decision_ask REAL, decision_mid REAL, decision_quote_age_ms INTEGER, decision_quote_src TEXT,
  decision_ltp REAL, decision_walk_px REAL,
  created_ts TEXT NOT NULL, submitted_ts TEXT, acked_ts TEXT, terminal_ts TEXT, stuck INTEGER NOT NULL DEFAULT 0,
  CHECK (filled_qty >= 0 AND filled_qty <= qty),
  CHECK (role='EXTERNAL' OR (order_type='LIMIT' AND validity='IOC' AND product='MIS' AND variety='regular'
         AND limit_px IS NOT NULL AND length(order_pk)=15 AND substr(order_pk,1,2)='T2')),
  CHECK ((role='ENTRY' AND side='BUY') OR (role='EXIT' AND side='SELL') OR (role='COVER' AND side='BUY')
         OR (role='SELFTEST' AND side='BUY') OR role='EXTERNAL'));                        -- I7, widened in phase 2
CREATE UNIQUE INDEX one_working_order_per_leg ON orders(leg_id)
  WHERE role <> 'EXTERNAL' AND state NOT IN ('COMPLETE','CANCELLED','REJECTED','NOT_PLACED');   -- I4
CREATE TRIGGER ord_fill_mono BEFORE UPDATE OF filled_qty ON orders WHEN NEW.filled_qty < OLD.filled_qty
  BEGIN SELECT RAISE(ABORT,'filled_qty decreased'); END;
CREATE TRIGGER ord_terminal BEFORE UPDATE ON orders
  WHEN OLD.state IN ('COMPLETE','CANCELLED','REJECTED','NOT_PLACED')
   AND (NEW.state IS NOT OLD.state OR NEW.filled_qty IS NOT OLD.filled_qty OR NEW.avg_fill_px IS NOT OLD.avg_fill_px)
  BEGIN SELECT RAISE(ABORT,'terminal order immutable'); END;
-- EXTERNAL orders (closing trades not placed by the bot) use order_pk = 'EX' || broker_order_id.

CREATE TABLE fills(
  fill_id INTEGER PRIMARY KEY, order_pk TEXT NOT NULL REFERENCES orders(order_pk), seq_key TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('CUM','TRADE')), qty INTEGER NOT NULL CHECK (qty > 0),
  px REAL NOT NULL CHECK (px > 0), ts TEXT NOT NULL, exch_ts TEXT, UNIQUE (order_pk, seq_key));
CREATE TRIGGER fills_no_upd BEFORE UPDATE ON fills BEGIN SELECT RAISE(ABORT,'fills append-only'); END;
CREATE TRIGGER fills_no_del BEFORE DELETE ON fills BEGIN SELECT RAISE(ABORT,'fills append-only'); END;
CREATE VIEW effective_fills AS                          -- TRADE rows replace CUM rows once they explain filled_qty
  SELECT f.* FROM fills f JOIN orders o ON o.order_pk = f.order_pk
  WHERE f.source = CASE WHEN o.filled_qty > 0 AND
        (SELECT COALESCE(SUM(t.qty),0) FROM fills t WHERE t.order_pk=f.order_pk AND t.source='TRADE') = o.filled_qty
        THEN 'TRADE' ELSE 'CUM' END;

CREATE TABLE trades(                                    -- the ledger: one immutable row per closed structure
  structure_id INTEGER PRIMARY KEY REFERENCES structures(structure_id),
  session TEXT NOT NULL, book TEXT NOT NULL, account TEXT NOT NULL, mode TEXT NOT NULL, strategy TEXT NOT NULL,
  strategy_version TEXT NOT NULL, intent_id TEXT NOT NULL, is_placebo INTEGER NOT NULL, parent_structure_id INTEGER, blocked_by TEXT,
  underlying TEXT NOT NULL, symbol TEXT NOT NULL, structure_type TEXT NOT NULL, direction TEXT NOT NULL,
  opt_type TEXT, strike REAL, expiry TEXT, lot_size INTEGER, qty INTEGER NOT NULL,
  entry_ts TEXT NOT NULL, exit_ts TEXT NOT NULL, hold_s REAL NOT NULL, entry_avg REAL NOT NULL, exit_avg REAL,
  risk_basis_rs REAL NOT NULL CHECK (risk_basis_rs > 0),
  gross REAL NOT NULL, brokerage REAL NOT NULL, stt REAL NOT NULL, txn REAL NOT NULL, sebi REAL NOT NULL, ipft REAL NOT NULL,
  gst REAL NOT NULL, stamp REAL NOT NULL, sqoff_fee REAL NOT NULL, charges REAL NOT NULL, net REAL NOT NULL, net_pct REAL NOT NULL,
  rates_version TEXT NOT NULL, n_entry_orders INTEGER NOT NULL, n_exit_orders INTEGER NOT NULL, n_executed_orders INTEGER NOT NULL,
  entry_bid REAL, entry_ask REAL, entry_mid REAL, entry_spread_pct REAL, entry_slip_vs_mid_pct REAL, entry_walk_px REAL,
  exit_mid_w REAL, exit_spread_pct_w REAL, exit_slip_vs_mid_pct REAL,
  hs_cost_rs REAL NOT NULL, crossing_cost_rs REAL NOT NULL,
  mfe_pct REAL, mfe_ts TEXT, mae_pct REAL, mae_ts TEXT, lock_armed INTEGER NOT NULL,
  exit_reason TEXT NOT NULL, pnl_status TEXT NOT NULL CHECK (pnl_status IN ('FINAL','ESTIMATED','UNRESOLVED')),
  ltp_entry REAL, ltp_exit REAL, thin_book INTEGER NOT NULL DEFAULT 0, exit_profile TEXT NOT NULL CHECK (json_valid(exit_profile)));
CREATE INDEX tr_book_session ON trades(book, session);
CREATE INDEX tr_strategy_version ON trades(strategy, strategy_version);
CREATE TRIGGER tr_no_upd BEFORE UPDATE ON trades BEGIN SELECT RAISE(ABORT,'ledger immutable'); END;
CREATE TRIGGER tr_no_del BEFORE DELETE ON trades BEGIN SELECT RAISE(ABORT,'ledger immutable'); END;

CREATE TABLE quote_checks(id INTEGER PRIMARY KEY, leg_id INTEGER NOT NULL, ts TEXT NOT NULL,
  phase TEXT NOT NULL CHECK (phase IN ('HOLD','POST')), bid REAL, ask REAL, bid_qty INTEGER, ask_qty INTEGER, ltp REAL,
  rb REAL, age_ms INTEGER, src TEXT, stop_px REAL, lock_armed INTEGER, decision TEXT);
CREATE INDEX qc_leg_ts ON quote_checks(leg_id, ts);

CREATE TABLE halts(halt_id INTEGER PRIMARY KEY, scope TEXT NOT NULL, kind TEXT NOT NULL, reason TEXT NOT NULL,
  operator INTEGER NOT NULL DEFAULT 0, clears TEXT NOT NULL CHECK (clears IN ('NEXT_SESSION','ACK','AUTO','RESUME')),
  created_ts TEXT NOT NULL, session TEXT NOT NULL, cleared_ts TEXT, cleared_by TEXT, clear_note TEXT);
CREATE UNIQUE INDEX one_active_halt ON halts(scope, kind) WHERE cleared_ts IS NULL;

CREATE TABLE strategy_modes(strategy TEXT PRIMARY KEY, version TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('OFF','SHADOW','PAPER','LIVE')),
  pending_mode TEXT CHECK (pending_mode IS NULL OR pending_mode IN ('OFF','SHADOW','PAPER','LIVE')),
  live_unlock_report_id INTEGER, live_since_session TEXT, demoted_reason TEXT, changed_ts TEXT NOT NULL, changed_by TEXT NOT NULL);
CREATE TABLE g1_trials(strategy TEXT NOT NULL, version TEXT NOT NULL, family_seq INTEGER NOT NULL UNIQUE, registered_ts TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('COLLECTING','CONFIRMING','CERTIFIED','FAILED','FUTILE','RETIRED','DEMOTED')),
  version_inputs TEXT NOT NULL CHECK (json_valid(version_inputs)), looks_done TEXT NOT NULL DEFAULT '[]',
  pass_report_id INTEGER, cert_report_id INTEGER, state_ts TEXT NOT NULL, PRIMARY KEY (strategy, version));
CREATE TRIGGER trials_no_del BEFORE DELETE ON g1_trials BEGIN SELECT RAISE(ABORT,'trial registry append-only'); END;
CREATE TABLE g1_reports(report_id INTEGER PRIMARY KEY, strategy TEXT NOT NULL, version TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('PROGRESS','LOOK','CONFIRM','FUTILITY','LIVE_REVIEW')), look_n INTEGER, computed_ts TEXT NOT NULL,
  n INTEGER, sessions INTEGER, exp_pct REAL, exp_pct_stressed REAL, lb_pct REAL, ub_pct REAL, alpha REAL, family_m INTEGER,
  placebo_n INTEGER, placebo_coverage REAL, placebo_exp_pct REAL, mismatches INTEGER, unresolved INTEGER,
  impaired_frac REAL, bar_mismatch_frac REAL, passed INTEGER NOT NULL, fail_reasons TEXT, inputs_hash TEXT NOT NULL,
  detail TEXT NOT NULL CHECK (json_valid(detail)));
CREATE TABLE bars_5m(symbol TEXT NOT NULL, start_ts TEXT NOT NULL, source TEXT NOT NULL CHECK (source IN ('LIVE','HIST')),
  open REAL, high REAL, low REAL, close REAL, volume INTEGER, valid INTEGER NOT NULL, PRIMARY KEY (symbol, start_ts, source));
CREATE TABLE bar_checks(session TEXT NOT NULL, symbol TEXT NOT NULL, start_ts TEXT NOT NULL, ok INTEGER NOT NULL,
  detail TEXT NOT NULL, PRIMARY KEY (session, symbol, start_ts));
CREATE TABLE broker_snapshots(account TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('POSITIONS','ORDERS','TRADES','MARGINS')),
  req_ts TEXT NOT NULL, ok INTEGER NOT NULL, payload TEXT CHECK (payload IS NULL OR json_valid(payload)),
  PRIMARY KEY (account, kind, req_ts));        -- ≤1/min per (account, kind) + every non-OK + every recon finding
CREATE TABLE foreign_acks(session TEXT NOT NULL, account TEXT NOT NULL, symbol TEXT NOT NULL, product TEXT NOT NULL,
  qty INTEGER NOT NULL, acked_by TEXT NOT NULL, acked_ts TEXT NOT NULL, PRIMARY KEY (session, account, symbol, product));
CREATE TABLE command_acks(source TEXT NOT NULL CHECK (source IN ('DASH','CLI')), cmd_id INTEGER NOT NULL, kind TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ACCEPTED','DONE','REJECTED','FAILED','EXPIRED')), result TEXT,
  picked_ts TEXT NOT NULL, done_ts TEXT, PRIMARY KEY (source, cmd_id));
CREATE TABLE session_flags(session TEXT NOT NULL, strategy TEXT NOT NULL, flag TEXT NOT NULL CHECK (flag IN ('IMPAIRED')),
  detail TEXT NOT NULL, PRIMARY KEY (session, strategy, flag));
```

**Pruning**
- During EOD tasks on the last session of each week, the core:
  1. exports `quote_checks` rows older than 180 days to gzipped CSV, then deletes them;
  2. deletes `broker_snapshots` rows older than 30 days.
- Nothing else is ever deleted.
- The v1 `titan_state.db` is never opened by v2.

### 4.4 Other database files
```sql
-- db/paper_exchange.db : writer = paper-sim thread only (WAL)
CREATE TABLE p_orders(broker_order_id TEXT PRIMARY KEY, account TEXT NOT NULL CHECK (account IN ('PAPER','SHADOW')), tag TEXT,
  symbol TEXT NOT NULL, token INTEGER NOT NULL, side TEXT NOT NULL, product TEXT NOT NULL, order_type TEXT NOT NULL,
  validity TEXT NOT NULL, qty INTEGER NOT NULL, lot_size INTEGER NOT NULL, tick REAL NOT NULL, limit_px REAL,
  placed_ts TEXT NOT NULL, due_ts TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('OPEN','COMPLETE','CANCELLED','REJECTED')),
  filled_qty INTEGER NOT NULL DEFAULT 0, avg_px REAL, status_msg TEXT, terminal_ts TEXT,
  placed_by TEXT NOT NULL CHECK (placed_by IN ('API','EXCHANGE')), UNIQUE (account, tag));
CREATE TABLE p_fills(trade_id TEXT PRIMARY KEY, broker_order_id TEXT NOT NULL REFERENCES p_orders(broker_order_id),
  account TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL CHECK (qty > 0),
  px REAL NOT NULL CHECK (px > 0), fill_ts TEXT NOT NULL, level_idx INTEGER NOT NULL);
CREATE TABLE p_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);    -- id counters, rng seed, last_squareoff_session:<acct>
-- positions = signed Σ p_fills per (account, symbol, 'MIS'); never stored separately.

-- dash/commands_dash.db : writer = titan2-dash only (journal_mode=DELETE so the bot can read it as another user)
CREATE TABLE commands(cmd_id INTEGER PRIMARY KEY, created_ts TEXT NOT NULL, requested_by TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('PAUSE','RESUME','FLATTEN_ALL')),
  args TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(args)), confirm_text TEXT, nonce TEXT NOT NULL UNIQUE);
CREATE TRIGGER c_no_upd BEFORE UPDATE ON commands BEGIN SELECT RAISE(ABORT,'insert-only'); END;
CREATE TRIGGER c_no_del BEFORE DELETE ON commands BEGIN SELECT RAISE(ABORT,'insert-only'); END;

-- db/commands_cli.db : writer = CLI (user titan). Same table and triggers; kind CHECK IN
-- ('PAUSE','RESUME','FLATTEN_ALL','KILL','ACK_HALT','ACK_ALERT','ACK_FOREIGN','SET_MODE','RUN_RECON')
```
The bot reads both command DBs every 1 s (`cmd_id > last_seen` per source, where `last_seen` comes from `command_acks`). It records processing in `titan2.db.command_acks` and publishes `pub/acks.json`. The bot never writes the command DBs.

### 4.5 Derived counters
All counters are SQL over rows. `EngineState` caches them, and they are recomputed on every `Engine.load`. No counter is stored anywhere else.

| Counter | Query (session = today) |
|---|---|
| entries(book) | `COUNT(*) FROM structures WHERE book=? AND session=? AND opened_ts IS NOT NULL` |
| attempts(book) | `COUNT(DISTINCT structure_id) FROM orders WHERE book=? AND role='ENTRY' AND substr(created_ts,1,10)=?` |
| orders_today(book) | `COUNT(*) FROM orders WHERE book=? AND role<>'EXTERNAL' AND substr(created_ts,1,10)=?` |
| entry_orders_last_60s(book) | `COUNT(*) FROM orders WHERE book=? AND role='ENTRY' AND created_ts >= now-60s` |
| realized_net(book) | `COALESCE(SUM(net),0) FROM trades WHERE book=? AND session=?` |
| first_loss_today(book) | `EXISTS(SELECT 1 FROM trades WHERE book=? AND session=? AND net<0)` |
| cooldown_until(book, underlying) | Maximum, over today's structures on that (book, underlying), of `closed_ts` plus: REENTRY_COOLDOWN_MIN for CLOSED/CLOSED_EXTERNAL; REJECT_COOLDOWN_MIN for VOID with `void_reason LIKE 'REJECTED%'`; VOID_COOLDOWN_MIN for any other VOID |
| live_sessions(strategy) | `COUNT(DISTINCT session) FROM structures WHERE book='LIVE:'\|\|? AND session >= live_since_session` |

### 4.6 Event log
- Every state transition writes one `events` row, inside the same transaction as the change.
- **Kinds:**
  - **Lifecycle and health:** BOOT, STARTUP_STEP, READY, SHUTDOWN, SETTINGS_LOADED, SETTINGS_INVALID, SELFCHECK, PREP, HEARTBEAT (every 60 s while running), PHASE, FEED_STATE.
  - **Quotes:** QUOTE_STALE, QUOTE_REFRESHED, QUOTE_TIMEOUT.
  - **Intents and structures:** INTENT_RECEIVED, INTENT_WAITING{book, gate, detail, pending, ttl_s} (2026-10-14, §5.4 step 1), INTENT_ROUTED{book, outcome, gate, detail; waited_s, waited_for after a wait}, INTENT_EXPIRED, STRUCTURE_RESERVED, STRUCTURE_VOID, POSITION_OPENED{exit_params}, PLACEBO_OPENED, PLACEBO_SKIPPED.
  - **Orders:** ORDER_CREATED, ORDER_SUBMITTED, ORDER_PLACE_RESULT, ORDER_UPDATE, FILL, ORDER_TERMINAL, ORDER_LOOKUP, ORDER_STUCK.
  - **Exits:** EXIT_DECISION{reason, urgency, quote}, LOCK_ARMED, STOP_MOVED (≤1/s), STOP_BREACH_PENDING, EXIT_ATTEMPT, EXIT_STUCK, EXIT_UNCAPPED, NO_BID, VERIFY_FLAT.
  - **Closes and reconciliation:** POSITION_CLOSED{pnl}, CLOSED_EXTERNAL, BROKER_SQUAREOFF, RECON{account, status}, MISMATCH, UNKNOWN_POSITION, SHORT_DETECTED, FILLS_REPAIRED.
  - **Halts and commands:** HALT, HALT_CLEARED, COMMAND_RECEIVED, COMMAND_RESULT.
  - **Faults:** INVARIANT_VIOLATION, EXCEPTION, GATE_ERROR.
  - **Modes, G1 and checks:** MODE_CHANGED, SELFTEST, G1_REPORT, TRIAL_STATE, LIVE_DEMOTED, PAPER_MODEL_OPTIMISTIC, CHARGES_CHECK, CHARGES_DRIFT, BAR_CHECK, SESSION_IMPAIRED, EOD_TRUTH_CHECK, ALERT.
- **Export.** At EOD the core writes `exports/events/YYYY-MM-DD.jsonl.gz` and `exports/trades/YYYY-MM-DD.jsonl.gz` from the DB. The DB is authoritative.
- **Example JSONL line:**
```json
{"seq":1042,"ts":"2026-10-12T09:20:01.412+05:30","session":"2026-10-12","kind":"ORDER_SUBMITTED","severity":"INFO",
 "account":"PAPER","book":"PAPER:opening_drive","structure_id":17,"leg_id":17,"order_pk":"T2EP26101200101","intent_id":"a9c31f04e2b7",
 "payload":{"symbol":"NFO:RELIANCE26OCT2900CE","side":"BUY","qty":500,"limit":42.35,"bid":41.90,"ask":42.15,"mid":42.025,
            "age_ms":180,"src":"REST","ltp":42.10,"walk_px":42.15,"attempt":1}}
```

### 4.7 `pub/`: the only data the dashboard reads
- **Location and permissions:** `/var/lib/titan2/pub`, files 0640 `titan:titandash`.
- **Writes are atomic:** write `tmp`, `fsync`, then `os.replace`.

| File | Written | Content |
|---|---|---|
| `status.json` | every 1 s | see schema below |
| `positions` | — | carried inside `status.json` |
| `trades_today.json` | on each close | ledger rows for today |
| `trades_90d.json` | at EOD | ledger rows for the last 90 days |
| `trade/<structure_id>.json` | on close | orders, fills, 1-min quote path |
| `events_tail.json` | every 2 s if changed | last 500 events |
| `g1.json` | on every G1 report | per-strategy scorecard |
| `acks.json` | on change | last 50 command acks |

`status.json` schema:
```json
{"ts":"…","session":"2026-10-12","phase":"PREOPEN|RECOVERING|TRADING|EOD|DONE","heartbeat_age_s":0.3,
 "allow_live":false,"entry_halts":[{"halt_id":3,"scope":"GLOBAL","kind":"PAUSE","reason":"…","since":"…","clears":"RESUME"}],
 "feed":{"connected":true,"last_tick_age_s":0.4,"reconnects":0,"warm":true,"degraded":false},
 "accounts":{"LIVE":{"recon":"CLEAN|MISMATCH|UNKNOWN|N/A","recon_age_s":4.1,"broker_positions":[…],"available_rs":null},
             "PAPER":{…},"SHADOW":{…}},
 "books":{"PAPER:opening_drive":{"policy":"FULL","realized_net":0.0,"mtm_net":-120.5,"entries":1,"max_entries":2,
          "open":1,"daily_stop_room":9879.5,"worst_case_room":21200.0,"cooldowns":{"NSE:RELIANCE":"10:05:00"}}},
 "positions":[{"structure_id":17,"book":"…","symbol":"…","qty":500,"entry_avg":42.15,"bid":43.0,"ask":43.2,"age_ms":300,
   "src":"WS","rb":42.98,"mtm_net":331.2,"stop":35.83,"lock_armed":false,"tp":48.47,"time_stop_in_s":2340,
   "mfe_pct":2.4,"mae_pct":-1.1,"state":"OPEN","exit_reason":null,"ltp_info":43.1}],
 "strategies":[{"name":"opening_drive","version":"1.3f2a9c01de","mode":"PAPER","pending_mode":null,
   "trial":{"state":"COLLECTING","n":37,"sessions":19,"exp_pct":-0.8,"lb_pct":null,"next_look":100}}],
 "pending_commands":0,"unacked_critical":0}
```


## 5. Order lifecycle

### 5.1 LIMIT + IOC only
- An IOC order is filled or cancelled by the exchange within about a second. So the bot never leaves a working order outside its own process. The sources of v1's surprise fills (resting market-protection orders, GTT legs, SL-M orders) cannot exist.
- `OrderRequest` implies LIMIT/IOC/MIS/regular. `KiteBroker.place` asserts this before sending. The DB CHECK in §4.3 rejects anything else.
- Orders are inserted directly in state **SUBMITTING**, in the same transaction that emits `Place`. That transaction commits before the HTTP call is made. `NEW` is reserved for phase-2 basket legs that are planned but not yet dispatched.

### 5.2 Order state machine (`core/orders.py`)
Any (state, event) pair not listed raises `IllegalTransition` (an InvariantViolation). The test enumerates every pair.

| From | Event | Guard / action | To |
|---|---|---|---|
| — | create (same tx as the leg/structure change) | row inserted; `Place` returned | SUBMITTING |
| SUBMITTING | `PlaceResult OK(id)` | store `broker_order_id`, `acked_ts`; confirm timer at submit+3 s | WORKING |
| SUBMITTING | `PlaceResult REJECTED` | store msg; classify (§5.9) | REJECTED (filled 0) |
| SUBMITTING | `PlaceResult AMBIGUOUS` | `LookupTag` at +1 s and +3 s. If throttled, first back off 1/2/4 s | UNKNOWN |
| SUBMITTING | `PlaceResult FATAL` | `HALT(TOKEN, GLOBAL, AUTO)` + CRITICAL; `LookupTag` once the token works | UNKNOWN |
| SUBMITTING, UNKNOWN | `OrderSnapshot` whose tag == order_pk, or whose broker id matches | adopt id; apply snapshot. A WS update may arrive before `PlaceResult` | WORKING or terminal |
| UNKNOWN | `LookupResult(found)` | adopt; apply | WORKING or terminal |
| UNKNOWN | `LookupResult(not found)`, ≥2 lookups done, ≥3 s since submit | — | NOT_PLACED (filled 0) |
| UNKNOWN | `LookupResult(ok=False)` | retry every 2 s. After 30 s: `ORDER_STUCK` CRITICAL + `HALT(ORDER_STUCK, BOOK, ACK)`. No new attempt on the leg | UNKNOWN |
| WORKING | snapshot, filled↑, non-terminal | `apply_cum` | WORKING |
| WORKING | snapshot COMPLETE | `apply_cum`; if filled ≠ qty, WARN and trust `filled_qty`; `FetchTrades` | COMPLETE |
| WORKING | snapshot CANCELLED | `apply_cum` (IOC remainder); `FetchTrades` if filled > 0 | CANCELLED |
| WORKING | snapshot REJECTED | filled must be 0, else InvariantViolation | REJECTED |
| WORKING | submit+3 s, no terminal | `PollOrder`; re-arm every 1 s | WORKING |
| WORKING | submit+6 s, no terminal | `Cancel` | CANCEL_REQUESTED |
| CANCEL_REQUESTED | `CancelResult OK/BENIGN` or terminal snapshot | `PollOrder` until terminal | terminal |
| CANCEL_REQUESTED | cancel+10 s, no terminal | `stuck=1`; `ORDER_STUCK` CRITICAL; `HALT(ORDER_STUCK, BOOK, ACK)`; poll every 2 s. **The leg stays EXITING and the ladder resumes when the order resolves** | CANCEL_REQUESTED |
| terminal | snapshot with a different state or filled qty | `INVARIANT_VIOLATION` event; `PollOrder`. If P&L is affected, the structure becomes `UNRESOLVED` | unchanged |

**`apply_cum(order, cum_qty, avg_px, wall)`** is idempotent:
- `cum < filled`: InvariantViolation (I9).
- `cum == filled`: no-op on quantity. The status may still change.
- `cum > filled`: insert `Fill(seq_key=f"cum:{cum}", source="CUM", qty=cum-prev, px=(avg*cum - prev_avg*prev)/(cum-prev))`. If that px is ≤ 0 or more than 20% from `avg`, use `avg` instead and WARN.
- Then set `leg.last_fill_mono` and `leg.last_fill_wall`.
- `TradesResult` inserts `trade:<id>` rows; the UNIQUE constraint drops duplicates. The `effective_fills` view picks TRADE rows once they explain `filled_qty`.

### 5.3 Structure / leg state machine (`core/positions.py`, phase 1 = one leg)
| From | Event | To | Effects (same transaction) |
|---|---|---|---|
| — | pre-reserve gates pass (§8.6) | structure RESERVED | `structures` row. The unique index holds the (book, underlying) slot, which counts toward MAX_POSITIONS |
| RESERVED | post-quote refusal, TTL expiry or quote timeout | VOID | `void_reason=<gate>`; VOID cooldown |
| RESERVED | plan accepted | ENTERING (structure + leg) | `legs` row; ENTRY order (SUBMITTING); `Subscribe(token, "full")`; `decision_json` |
| ENTERING | entry order terminal, filled 0 | VOID | `void_reason = "ENTRY_NO_FILL"` or `"REJECTED:<msg>"` |
| ENTERING | entry order terminal, filled > 0 | OPEN | `compute_exit_params` from fills; `stop_px = hard_sl_px`; `risk_basis = entry_avg × filled`; `POSITION_OPENED`; placebo trigger (PAPER books). No top-up order |
| OPEN | `Exit(reason)` or forced flag | EXITING | first reason wins; `exit_urgency`; `exiting_since` |
| EXITING | bot `open_qty` reaches 0 | VERIFY_FLAT | request a post-fill positions snapshot (§9.4) |
| VERIFY_FLAT | post-fill broker net 0 | CLOSED | `close()`: trades row, realized, cooldown, `Unsubscribe` after the post-exit tail |
| VERIFY_FLAT | residual confirmed (§9.4) | CLOSED | `broker_residual_qty>0`; `HALT(MISMATCH)`; residual goes to FOREIGN; **never sold** |
| OPEN, EXITING | recon `EXTERNAL_CLOSE` confirmed | CLOSED_EXTERNAL | `close_external()`; `HALT(MISMATCH, ACCOUNT, ACK)` unless the kind is BROKER_SQUAREOFF |
| OPEN | recon `PARTIAL_EXTERNAL` | EXITING (ANOMALY_EXIT) | capped exit loop |

- `close()` and `close_external()` are the **only** callers of `ledger.build_trade_row`, which uses `charges.net_pnl`. They are the only writers of `trades`.
- Both run inside the terminal transition, so P&L is booked once (I10).

### 5.4 Entry flow (`core/gateway.py`)
1. **`IntentReceived`.**
   - Validate it again with `make_intent` rules and insert the `intents` row.
   - Route it to books (§10.5).
   - For each book, run the pre-reserve gates G01–G13 (§8.6). The outcome is RESERVED, REFUSED or DIVERTED; one `intent_routes` row per book.
   - A first book refused at G09 only because of pending structures waits (`risk.waits_for_pending`; FINAL_SPEC errata 2026-10-14): no book is routed, its row reads REFUSED until the wait ends, and every Timer and every newer intent route it again, oldest first, until it is reserved, refused or expired. A woken intent that G09 refuses as RUNAWAY raises no halt. G09 counts a RESERVED structure's entry order before it is sent (ENTRY_ORDERS, a pending cap that waits), so no mix of fresh and woken intents sends more than `MAX_ENTRY_ORDERS_PER_MIN` entry orders in 60 s.
2. **Select the contract** with `handler.select()` (§11.5). It needs the underlying LTP from `QuoteCache`, no more than 2 s old. If none is available, add the underlying to the quote fetch and select after the result arrives.
   - PAPER books also select the opposite contract for the placebo plan.
3. **Fetch quotes:** `FetchQuotes(request_id=intent_id, symbols={option, opposite (PAPER), underlying}, priority=1 if any LIVE book else 3 (4 for SHADOW-only))`.
4. **`QuoteResult`.** Process each RESERVED structure of this intent in the order LIVE, PAPER, SHADOW:
   - run post-quote gates G14–G22;
   - `handler.plan_entry()` (liquidity, size, limit);
   - write the leg, the ENTRY order and `decision_json`;
   - emit `Place`.

   A refusal sets the structure to VOID. A failed or timed-out quote sets all of the intent's structures to VOID (gate QUOTE).
5. **One attempt per intent per book.** No chasing and no retry. Intents are never persisted for replay. Waiting for a pending slot (step 1) defers the attempt; it is not a retry.

### 5.5 Exit loop (`core/exit_loop.py`)
It runs per EXITING leg on every Timer and every relevant event:
```
step(leg, now):
  if leg has a non-terminal bot order: return                                  # I4
  if now_wall >= STOP_SENDING: alert once CRITICAL "not flat at STOP_SENDING; broker square-off expected"; return
  q = open_qty(leg); if q == 0: verify_flat(leg); return                       # §9.4
  cap = broker_cap(leg, now)                                                   # §9.4; may return WAIT
  if cap is WAIT: return
  qty = min(q, cap); if qty == 0: external_wait(leg); return                   # never sells; recon confirms (§9.4)
  fq = quotes.fresh(sym, 1.0)
  if fq is None: request FetchQuotes(P0) if not in flight
                 if now - max(leg.exiting_since, last_attempt) < STALE_PROTECTIVE_S: return
                 pricing = PROTECTIVE from last known bid (band >= 3)
  elif not fq.bids: emit NO_BID (WARN after 10 s, CRITICAL from 15:20); re-request quotes every 1 s; return   # no order into an empty book
  if now_wall >= EOD_BACKSTOP: leg.exit_band_idx = max(leg.exit_band_idx, 3)
  limit = exit_limit(...)                                                      # §5.7
  create EXIT order(attempt+1, band_idx) -> Place
on EXIT order terminal:
  if band-type reject (§5.7): keep band, next limit = max(prev_limit, floor_tick(bid - 2 ticks)); 3 in a row -> WARN
  elif filled < qty: band = min(band + 1, 6)
  next step after max(250 ms, next fresh quote)
  >=5 attempts or >=15 s exiting: EXIT_STUCK WARN once; >=10 attempts: HALT(EXIT_DIFFICULTY, BOOK, AUTO) + CRITICAL
  keep trying until flat or STOP_SENDING
```

### 5.6 Hand-off to the broker's MIS auto square-off
- **15:25:00 (`STOP_SENDING`):** the bot sends no further orders of any kind.
- **15:26 (`BROKER_SQUAREOFF_AT`):** Zerodha's F&O MIS auto square-off **[V]** closes anything left. The fee is ₹50 + 18% GST per order.
- **Recon at 15:27 and 15:31:** non-bot closing orders found then are classified as `BROKER_SQUAREOFF`. This raises a CRITICAL alert, but no MISMATCH halt.
- `charges.net_pnl` adds the ₹59 fee per square-off order. Paper simulates the same square-off (§6.6).

### 5.7 Exact pricing (`core/exits.py`)
- **Entry BUY.**
  - `limit = min(ask + 2·tick, floor_tick(mid × 1.01))`.
  - Refuse if `limit < ask` or `walk(asks, qty, limit, BUY).fillable < qty`.
  - With spread ≤ 1.5% of mid, `ask ≤ mid × 1.0075`, so the limit normally clears the ask.
- **Exit SELL at band index b.**
  - `BANDS = (0.0, 0.005, 0.01, 0.02, 0.04, 0.08, 0.12)`; `MAX_EXIT_DISCOUNT = 0.12`.
  - `ref_bid` is the fresh best bid. For protective pricing it is the last known bid.
  - `sweep` is the worst bid level needed to fill qty on the fresh quote, or the lowest shown bid if depth is short. For protective pricing, `sweep = ref_bid`.
  - `raw = min(sweep - 2·tick, ref_bid·(1 - BANDS[b]))`.
  - `limit = max(tick, floor_tick(raw), ceil_tick(ref_bid·(1 - MAX_EXIT_DISCOUNT)))`. This never goes more than 12% under the fresh bid (I13).
  - Starting band: 0 when pricing from a fresh quote; 3 for protective pricing; at least 3 after EOD_BACKSTOP.
  - An IOC sell fills at resting bids ≥ limit. A lower limit only matters if the book moves.
- **Band-type reject.** `status_msg` matches `(?i)(price|lpp|band|range|circuit|protection|outside)`. The next attempt keeps `b` and uses `max(prev_limit, floor_tick(fresh_bid - 2·tick))`.
- **COVER BUY** (bot-explained short only, §9.3). Same loop, mirrored:
  - `limit = min(ceil_tick(max(sweep_ask + 2·tick, ref_ask·(1 + BANDS[b]))), floor_tick(ref_ask·(1 + MAX_EXIT_DISCOUNT)))`.
- **SELFTEST BUY.** `limit = max(tick, floor_tick(best_bid × 0.97))`. It sits below the best bid, so it can never match.

### 5.8 Broker error taxonomy (`broker/kite.py`)
| Kite outcome | Class | Engine handling |
|---|---|---|
| `place_order` returns `order_id` | OK | WORKING |
| `InputException`, `OrderException` | REJECTED | see §5.9 |
| `requests.Timeout`, `requests.ConnectionError`, `NetworkException` (code 429 ⇒ `throttled=True`), `GeneralException`, `DataException`, any other exception | AMBIGUOUS | tag lookup (§5.2) |
| `TokenException`, `PermissionException` (e.g., static IP) | FATAL | `HALT(TOKEN)`; §9.7 |
| cancel message matches `(?i)(complete|cancel|reject|not found|already)` | BENIGN | poll |
| read call raises anything | `ok=False` | no inference; retry with backoff |

One exception (2026-10-14, FINAL_SPEC errata "One REST quote retry"): a `quote()` call whose connection the other end closed before Kite answered at all (`requests.ConnectionError` around `ProtocolError('Connection aborted.', …)` wrapping a `ConnectionResetError`, `RemoteDisconnected` included, a `ConnectionAbortedError` or a `BrokenPipeError`) is made once more at once, paced by the quote bucket; a second failure fails the read as above. Nothing else is retried: no timeout (not even one while the request is being sent, which urllib3 wraps in the same `ProtocolError`), malformed status line or headers, failed connect, SSL error or Kite answer, and no other read or order call.

### 5.9 Rejection handling
- **Entry reject:**
  - The structure goes to VOID with `REJECTED:<msg>`, and a reject cooldown of 10 minutes starts.
  - 3 entry rejects in one session for a book → `HALT(REJECTS, BOOK, ACK)`.
  - If the message matches `(?i)(block|not allowed|restricted|ban)` → `HALT(CONTRACT_BLOCKED, SYMBOL:<sym>, NEXT_SESSION)`. This halt applies to every book, so paper mirrors live restrictions.
  - A margin or RMS message → WARN.
- **Exit reject:** the next attempt follows §5.7.

### 5.10 Restart in the middle of an order
| Crash point | Recovery outcome |
|---|---|
| After the SUBMITTING row committed, before the HTTP call | `LookupTag` finds nothing twice → NOT_PLACED. Entry → VOID. Exit → the loop retries with a new tag |
| After the HTTP call, before `PlaceResult` committed | tag found in `orders()` → adopt and apply |
| WORKING | `order_history` → apply (IOC orders resolve within seconds) |
| Fills applied, state transition not yet made | state rebuilt from fills (leg rows commit before their orders) |
| Partial exit done | `open_qty` from fills; the loop resumes for the remainder |
| DB lost or rolled back | bot-tagged broker orders are imported (§9.6). Structures are rebuilt with `ExitParams` from the current strategy profile and marked `RECOVERED`, plus an alert |

## 6. Paper exchange (`broker/paper.py`)

### 6.1 Same code path
- `PaperExchange` implements `BrokerPort` for accounts PAPER and SHADOW: `place`, `cancel`, `order_history`, `orders_today`, `order_trades`, `trades_today`, `positions`, `margins`, `contract_note`.
- It emits the same `OrderSnapshot` / `*Result` events as Kite, with `source="SIM"`.
- A book is bound to an account at routing time. That binding is the only paper/live distinction. `tests/arch` greps `core/` for `paper`/`Paper` and allows only the account enum values and policy names.

### 6.2 `place(req)`
1. **Validate as the exchange would:**
   - `qty > 0`, `qty % lot == 0`;
   - `is_tick_multiple(limit)`;
   - LIMIT/IOC/MIS;
   - the symbol is known.

   Failure → REJECTED (`"SIM: …"`).
2. **Band check.** With LTP known, reject if `|limit/ltp − 1| > PAPER_PRICE_BAND_PCT/100` (`"SIM: price outside band (LPP)"`).
3. **Funds check for BUY.** Require `available(account) ≥ qty·limit + est. charges`, else REJECTED `"SIM: insufficient funds"`.
4. **Accept.** Assign `broker_order_id = f"P{yymmdd}{n:09d}"`. Persist OPEN with `due = now + L`. Emit `OrderSnapshot(WORKING)` at once.

### 6.3 Latency and fill at the due time
- `L ~ lognormal(median=PAPER_LATENCY_MEDIAN_MS, sigma=PAPER_LATENCY_SIGMA)`, clipped to [80, 3000] ms. The seeded RNG uses `seed = int(sha256(f"{session}|{account}"))`.
- At the due time, take depth `D = QuoteCache.fresh(symbol, PAPER_BOOK_MAX_AGE_S=1.0)`. This is the book **at fill time, not at decision time**, so slippage from latency is included.
  - If `D` is missing, ask `lane-reads` for REST depth (priority PAPER/SHADOW) and wait up to 1.5 s.
  - If it is still missing → CANCELLED, filled 0, `"SIM: no fresh depth"`.
- **`fill_from_depth(levels, side, qty, limit, lot, participation, adverse_ticks, tick) -> list[tuple[px, qty]]`** is pure and shared with FakeKite.
  1. Walk levels best first.
  2. Stop when `BUY and px > limit` or `SELL and px < limit`. If the book has moved through the limit during the latency, nothing fills.
  3. At each level take `min(remaining, floor(level.qty × participation / lot) × lot)`.
  4. The fill price is `px ± adverse_ticks·tick`, against us.
- **Result:**
  - filled == qty → COMPLETE;
  - otherwise CANCELLED with a partial fill (IOC remainder).
  - One `p_fills` row per level. Persist, then emit the terminal `OrderSnapshot`.
- **`cancel()`.** An OPEN order becomes CANCELLED with filled 0. Otherwise the result is BENIGN.

### 6.4 Positions, margins, persistence
- **Positions** are derived from `p_fills`: signed per (account, symbol, MIS), with day buy/sell values. They are independent of the engine's fills, so paper recon compares two separately kept ledgers.
- **Margins.** `available = PAPER_CAPITAL_RS + Σ sell value − Σ buy value − Σ charges` for today's fills. It resets each session.
- **On startup:**
  - OPEN orders with `due + 1 s < now` → CANCELLED, filled 0, `"SIM: expired during downtime"`.
  - Positions left over from a previous session → settled by an EXCHANGE square-off at ₹0.05 (`"SIM: missed square-off"`). This is a conservative near-total loss.

### 6.5 Determinism
- `PaperExchange.step(now_mono)` processes everything due. The thread wrapper calls it in a loop with a 10 ms condition wait. Tests call it inline with `FakeClock`.

### 6.6 Auto square-off simulation
At `BROKER_SQUAREOFF_AT`, for each account and each symbol with non-zero MIS net:
1. Create a `p_order` with `placed_by=EXCHANGE`, `tag="SQOFF"` and market semantics.
2. Fill by walking all shown bids for a sell (asks for a buy) at participation 1.0. Fill the remainder at the deepest level × 0.95 for a sell, or the best level × 1.05 for a buy.
3. Mark it COMPLETE.

Recon then classifies it as `BROKER_SQUAREOFF` (§9.3).

### 6.7 Comparability (X2) and calibration
- Every order stores `decision_walk_px`: the walk of the decision quote for that qty.
- Both modes report `slip_vs_walk = (fill − decision_walk_px)/decision_walk_px` (sign against us) and the IOC fill rate.
- The LIVE twin compares these per trade (§10.9).
- Any recalibration of `PAPER_*` values bumps `PAPER_MODEL_VERSION`, which bumps every strategy version (§10.2).

**Not modelled, with mitigations:**
| Not modelled | Mitigation |
|---|---|
| Queue position, hidden liquidity, adverse selection beyond the latency | Participation 0.5; G1 1.5× crossing-cost stress; live twin + X2 falsifier; live probation |

## 7. Exit engine (`core/exits.py`, pure)

### 7.1 Frozen levels (computed once, from the entry fills)
Notation:
- `E` = effective entry average; `Q` = entry filled qty.
- `C_in` = actual entry charges: Σ `order_charges` over the entry orders.
- `R` = the RateSet in force.

Formulas:
```
k_exit      = R.stt_sell + (R.txn + R.sebi + R.ipft)·(1 + R.gst)          # 2026-04-01 rates: 0.00192043
be_px       = (E·Q + C_in + R.brokerage·(1 + R.gst)) / (Q·(1 − k_exit))   # exact net-zero price with one exit order
hard_sl_px  = E·(1 − hard_sl_pct/100)                                      # X4
arm_px      = be_px + (lock_arm_pct/100)·E                                 # X5
floor_px    = be_px + (lock_floor_pct/100)·E
tp_px       = E·(1 + tp_pct/100)                                           # X6; must be > arm_px
time_stop_at = entry_fill_wall + time_stop_min                             # X7, wall clock
```
- **Charges-hurdle entry gate (G18):** refuse unless `(be_planned/limit − 1)·100 ≤ MAX_CHARGES_HURDLE_PCT`, using the planned limit and qty.
- No level changes after entry. v1 replaced the SL after entry (exit_manager 1219); v2 does not.

### 7.2 `ExitProfile` bounds (code constant `EXIT_BOUNDS`; a strategy whose profile violates them fails registry load and is disabled)
| Field | Default | Bound |
|---|---|---|
| hard_sl_pct | 15 | 5 – 15 |
| lock_arm_pct | 4 | 2 – 10 |
| lock_floor_pct | 3 | 2 – (arm − 0.5) |
| trail_retain | 0.70 (exit_manager 150 run-zone value) | 0.5 – 0.9 |
| tp_pct | 15 | 8 – 50, and tp_px > arm_px |
| time_stop_min | 45 [U] | 10 – 120 |
| time_stop_when_locked | false | bool |
| stop_confirm_ms / stop_confirm_max_ms | 400 / 1500 | 0–1000 / 500–3000 |
| stop_confirm_bypass_pct | 3.0 | 1 – 10 |
| no_bid_exit_s | 10 | 3 – 30 |

### 7.3 Realizable bid
`realizable_bid(q, Q) -> (rb, thin)`:
1. `w = walk(q.bids, Q, None, SELL)`.
2. If `w.fillable == Q`, then `rb = w.avg_px`.
3. Otherwise `rb = (w.avg_px·w.fillable + (Q − w.fillable)·deepest_bid·0.98)/Q` and `thin = True`.
4. If there are no bids, `rb = None`.

LTP is never used for a decision. It is logged only.

### 7.4 `decide(leg, q, refresh, now_mono, now_wall, p: ExitParams, eod: EodTimes, forced: str | None) -> Decision`
`Decision = NoAction(marks) | RefreshQuote(marks) | Exit(reason, urgency, marks)`

`marks` carries `peak_rb`/`trough_rb` with timestamps, `stop_px`, `lock_armed`, `breach_*` and `no_bid_since`.

The rules are evaluated in this order. The first match wins, and the reason is frozen.
```
0  forced in {KILL_SWITCH, OPERATOR_FLATTEN, DAILY_STOP, ANOMALY_EXIT, RECOVERY_FLATTEN, SELFTEST_FILLED} -> Exit(forced, PROTECTIVE)
1  now_wall >= EOD_FLATTEN -> Exit(EOD_FLATTEN, PROTECTIVE)
2  age = q.age(now) if q else inf                       # receipt time of the snapshot, never last-trade time
   if age > STALE_REFRESH_S (3):
       if refresh.failed_since_stale or age > STALE_PROTECTIVE_S (5): Exit(STALE_QUOTE_PROTECTIVE, PROTECTIVE)
       else: RefreshQuote (NoAction if one is in flight)
   # a quote older than 3 s is never used by any rule below
3  if not q.bids:
       no_bid_since = no_bid_since or now
       if no REST refresh since no_bid_since: RefreshQuote
       elif now - no_bid_since >= no_bid_exit_s: Exit(NO_BID_PROTECTIVE, PROTECTIVE)
       else NoAction
   else no_bid_since = None
4  rb, thin = realizable_bid(q, open_qty); update peak/trough (+ wall timestamps)     # bid-based MFE/MAE
5  if not lock_armed and rb >= arm_px: lock_armed = True; stop = max(stop, floor_px)  # LOCK_ARMED
   if lock_armed: stop = max(stop, floor_px, be_px + trail_retain·(peak_rb - be_px))  # ratchet only; never lowers
6  if rb <= stop:                                        # debounce (stops are irreversible)
       reason = LOCK_STOP if lock_armed else HARD_SL
       if rb <= stop - (bypass_pct/100)·E: Exit(reason, PROTECTIVE)
       elif breach_first is None: breach_first, breach_quote = now, q.recv_mono; NoAction  # STOP_BREACH_PENDING
       elif q.recv_mono > breach_quote and now - breach_first >= stop_confirm_ms: Exit(reason, PROTECTIVE)
       elif now - breach_first >= stop_confirm_max_ms: RefreshQuote     # a REST snapshot is a distinct snapshot
       else NoAction
   else breach_first = breach_quote = None
7  if rb >= tp_px: Exit(TAKE_PROFIT, NORMAL)
8  if now_wall >= time_stop_at and (not lock_armed or time_stop_when_locked): Exit(TIME_STOP, NORMAL)
9  NoAction
```

What `decide` never does: reverse, add, average down, exit partially, or use greeks/TIE/velocity/conviction/OI rules (brief X7, X9).

### 7.5 Cadence and logging
- **Evaluation:** on `QuotesDirty` for held symbols, plus every 250 ms Timer (freshness, time stop, EOD).
- **`quote_checks` rows (phase HOLD):** at most 1/s per leg, plus every decision change.
- **Persistence:** peak, trough and stop at most once a second (dirty marks flushed on the Timer). LOCK_ARMED and EXIT decisions are persisted immediately.
- **Post-exit tail.** After CLOSED, the contract stays subscribed (full mode) for 45 minutes, with `quote_checks` phase POST every 5 s. This records the bid path that the entry would have followed, so other exit profiles can be tested offline on identical entries (§10.3). Then the contract is unsubscribed.

### 7.6 Worked example (2026-04-01 rates, one entry order, E = 32.00, Q = 500, premium ₹16,000)
- **Entry charges.** `C_in = 20 + 5.6848 (txn) + 0.016 (SEBI) + 4.6261 (GST) + 0.48 (stamp) = 30.81`.
- **Levels.**
  | Level | Value |
  |---|---|
  | be_px | (16000 + 30.81 + 23.60)/(500 × 0.99807957) = **32.17** |
  | hard_sl_px | 27.20 |
  | arm_px | 33.45 |
  | floor_px | 33.13 |
  | tp_px | 36.80 |
- **Ratchet.** At peak rb 35.00 the stop is `max(33.13, 32.17 + 0.70 × 2.83) = 34.15`.
- **Lock-floor exit at 33.13.** Net ≈ ₹479 ≈ 3% of premium.


## 8. Risk and sizing

### 8.1 Charges and the single P&L function (`charges.py`, R3)
```python
@dataclass(frozen=True, slots=True)
class RateSet:
    version: str; effective_from: date
    brokerage_per_order: float   # ₹ per EXECUTED order (options, flat); an unfilled IOC costs 0
    stt_sell: float              # on sell-side premium
    stt_exercise: float          # on intrinsic value of exercised ITM longs
    txn: float                   # NSE options, on premium, both sides
    sebi: float                  # ₹10/crore, both sides
    ipft: float                  # NSE IPFT on premium [V]
    stamp_buy: float             # buy side only
    gst: float                   # on brokerage + txn + sebi + ipft + sqoff_fee
    sqoff_fee: float             # ₹ per broker auto-square-off order (before GST)
RATESETS = (
  RateSet("2024-10-01", date(2024,10,1), 20.0, 0.001,  0.00125, 0.0003503, 1e-6, 0.0, 0.00003, 0.18, 50.0),
  RateSet("2026-04-01", date(2026,4,1),  20.0, 0.0015, 0.0015,  0.0003553, 1e-6, 0.0, 0.00003, 0.18, 50.0),  # [V] fixture
)
def rates_for(d: date) -> RateSet                                   # latest effective_from <= d
@dataclass(frozen=True, slots=True)
class OrderFills: side: Side; filled_qty: int; value: float; external_kind: str | None = None  # value = Σ px·qty (effective fills)
def order_charges(o: OrderFills, r: RateSet) -> Charges
    # filled_qty == 0 -> all zero. brokerage = r.brokerage_per_order (once per executed order, any number of fills)
    # stt = r.stt_sell·value if SELL; stamp = r.stamp_buy·value if BUY; txn/sebi/ipft = rate·value
    # sqoff_fee = r.sqoff_fee if external_kind == "AUTO_SQUAREOFF"; gst = r.gst·(brokerage+txn+sebi+ipft+sqoff_fee)
    # each component rounded to 2 dp; total = Σ components
def net_pnl(orders: Sequence[OrderFills], r: RateSet) -> PnL        # gross = Σ SELL value − Σ BUY value; net = gross − Σ charges
def mtm_net(done: Sequence[OrderFills], open_qty: int, rb: float, r: RateSet) -> float
    # net_pnl(done + [OrderFills(SELL, open_qty, open_qty*rb)], r).net — "net if closed now at the realizable bid"
def breakeven_px(entry_value: float, qty: int, c_in: float, r: RateSet) -> float   # §7.1 closed form
def round_trip_charges(px: float, lot: int, r: RateSet) -> float   # entry + exit charges for 1 lot at px
def exercise_stt(intrinsic_per_unit: float, qty: int, r: RateSet) -> float        # never expected (MIS + DTE rules)
```
- **Sources for the 2026-04-01 rates:**
  - Zerodha charges page as of 2026-10: STT on options sell premium is 0.15%, and on exercise 0.15%. These rose from 0.10% and 0.125% in Budget 2026-27, effective 2026-04-01.
  - NSE transaction charge is 0.03553% of premium, SEBI fee ₹10/crore, stamp duty 0.003% on the buy side, GST 18%.
  - The auto square-off fee is ₹50 + GST per order.
  - IPFT is defaulted to 0. Pin it from a real contract-note fixture (Q5).
- **Where `net_pnl` is used.** It is the only realized-P&L function, and these all call it:
  - `positions.close`/`close_external` (the ledger);
  - risk MTM (through `mtm_net`);
  - `status.json`;
  - reports;
  - the EOD truth check;
  - G1, which recomputes every sample trade's net with the *current* RateSet from its fills.
- **Virtual contract note check (§9.8).** `kite.get_virtual_contract_note(list of {order_id, exchange, tradingsymbol, transaction_type, variety, product, order_type, quantity, average_price})` **[V]** is called daily:
  - for every LIVE order;
  - for up to 50 PAPER orders using synthetic ids. If the API rejects synthetic ids, log `VCN_PAPER_UNSUPPORTED` once and skip paper.
  - Any order differing from the model by more than ₹1 raises `CHARGES_DRIFT` (WARN): a new RateSet is needed.

### 8.2 MTM
- **Leg MTM** = `mtm_net(...)`, computed for each OPEN or EXITING leg from its realizable bid on a fresh quote (≤3 s old).
- **Book MTM** = the sum over its legs. It is **`None` (unknown) if any open leg lacks a fresh rb**, whether because the quote is stale or there is no bid.
- **Unknown MTM blocks entries** in that book (G12) and blocks the daily-stop decision. Exits are unaffected.
- No cached unrealized value is ever reused. v1 did this (risk_governor 335/775); v2 does not.

### 8.3 Books and policies
| Book | Account | Policy | Purpose |
|---|---|---|---|
| `PAPER:<s>` | PAPER | FULL | G1 population; caps identical to live |
| `LIVE:<s>` | LIVE | FULL + account guard | real money (after unlock) |
| `SHADOW:<s>` | SHADOW | QUALITY | SHADOW-mode strategies; operator-diverted PAPER intents (`blocked_by`) |
| `PLACEBO:<s>` | SHADOW | QUALITY | opposite-direction twin of every `PAPER:<s>` entry |
| `LIVE:_selftest` | LIVE | special | daily order-path self-test only |

QUALITY skips gates G09, G11, G12, G20 and G21. It keeps every other gate, including liquidity, size, eligibility and one structure per underlying.

### 8.4 Settings (`settings.py`)
- **Loading.**
  - File: `/var/lib/titan2/config/settings.json`. It is optional; absent keys take their defaults.
  - **Read once at process start; immutable for the process.**
- **The file is invalid if any of these is true:**
  - a key is unknown;
  - a value has the wrong type, or is NaN/inf;
  - a value is looser than its bound.
- **When the file is invalid:**
  - the process starts with code defaults (the strictest values);
  - it raises `HALT(SETTINGS_INVALID, GLOBAL, AUTO)`, which clears at the next start with a valid file;
  - exits keep their frozen ExitParams.
- **Versioned keys.** Keys marked ✓ feed the strategy version hash (§10.2). Changing any of them starts a new G1 trial.
- **CLI.** `titan2 settings set KEY VALUE` validates, writes atomically, and prints "takes effect at next restart; new versions for: …". No UI writes settings.

| Key | Default | Allowed (cannot loosen) | Versioned |
|---|---|---|---|
| MAX_LOTS | 1 | {0, 1} (phase-1 code ceiling) | ✓ |
| MAX_UNITS | 5000 | 0–5000 | ✓ |
| MAX_LOSS_PER_TRADE_RS | 15000 | 0–15000 | ✓ |
| MAX_PREMIUM_PER_TRADE_RS | 50000 | 0–50000 | ✓ |
| MIN_PREMIUM_RS | 20 | 20–500 | ✓ |
| STOP_SLIP_ALLOW_PCT | 5.0 (stop-fill p95 4.4%) | 5–15 | ✓ |
| DAILY_STOP_RS (per FULL book) | 15000 | 0–15000 | ✓ |
| MAX_WORST_CASE_DAY_LOSS_RS (per FULL book) | 50000 | 0–50000 | ✓ |
| MAX_POSITIONS_PER_BOOK | 2 | 0–2 | ✓ |
| MAX_ENTRIES_PER_DAY | 3 (D1 chose 2; raised 2026-10-13, the user's decision) | 0–3 | ✓ |
| MAX_ENTRY_ATTEMPTS_PER_DAY | 6 | 0–6 | ✓ |
| MAX_ORDERS_PER_DAY_PER_BOOK | 40 | 0–40 | ✓ |
| MAX_ENTRY_ORDERS_PER_MIN | 3 | 0–3 | ✓ |
| REENTRY_COOLDOWN_MIN | 30 (D5) | 30–390 | ✓ |
| VOID_COOLDOWN_MIN / REJECT_COOLDOWN_MIN | 5 / 10 | 5–390 / 10–390 | ✓ |
| STOP_AFTER_FIRST_LOSS | false (D4 [U]) | bool (true is tighter) | ✓ |
| LIQ_MAX_SPREAD_PCT | 1.5 | 0.1–1.5 | ✓ |
| LIQ_DEPTH_MULT | 3.0 | 3–20 | ✓ |
| LIQ_MAX_SLIP_VS_MID_PCT | 1.0 | 0.1–1.0 | ✓ |
| LIQ_MAX_LAST_TRADE_AGE_S | 60 [U] | 5–60 | ✓ |
| MAX_CHARGES_HURDLE_PCT | 1.0 | 0.1–1.0 | ✓ |
| QUOTE_MAX_AGE_ENTRY_S | 1.5 | 0.2–1.5 | ✓ |
| UNDERLYING_MAX_AGE_S | 2.0 | 0.5–2.0 | ✓ |
| STALE_REFRESH_S / STALE_PROTECTIVE_S | 3 / 5 | ≤3 / ≤5 (protective > refresh) | ✓ |
| ENTRY_START / NO_NEW_AFTER | 09:20:00 / 14:55:00 (E5) | ≥09:20 / ≤14:55 | ✓ |
| EOD_FLATTEN / EOD_BACKSTOP / STOP_SENDING | 15:15 / 15:22 / 15:25 (X8) | ≤ each; strictly increasing | ✓ |
| BROKER_SQUAREOFF_AT | 15:26 [V] | 15:20–15:29, > STOP_SENDING | ✓ |
| MIN_TD_TO_EXPIRY_INDEX / _STOCK | 2 / 5 (S2, §11.4) | 2–10 / 5–15 | ✓ |
| STOCK_ALLOWLIST / STOCK_DENYLIST | [] / [] | lists | ✓ |
| PAPER_PARTICIPATION | 0.5 | the default only: fixed in code (paper model 2, 2026-10-14; was 0.1–0.5) | ✓ |
| PAPER_LATENCY_MEDIAN_MS / _SIGMA | 250 / 0.6 | the defaults only: fixed in code (paper model 2; was ≥150 / ≥0.3) | ✓ |
| PAPER_READ_LATENCY_MEDIAN_MS / _SIGMA (decision B1) | 250 / 0.85 | the defaults only: fixed in code (paper model 2; was ≥250 / ≥0.85) | ✓ |
| PAPER_ADVERSE_TICKS / PAPER_PRICE_BAND_PCT / PAPER_BOOK_MAX_AGE_S | 0 / 20 / 1.0 | the defaults only: fixed in code (paper model 2; was ≥0 / 5–20 / 0.2–1.0) | ✓ |
| PAPER_CAPITAL_RS | 500000 | 100000–500000 | ✓ |
| FEED_MAX_TICK_GAP_S / FEED_WARM_S | 5 / 60 | ≤5 / ≥60 | — |
| RECON_INTERVAL_S / RECON_MAX_AGE_ENTRY_S | 10 / 30 | ≤10 / ≤30 | — |
| LIVE_ACCOUNT_MAX_POSITIONS / LIVE_ACCOUNT_DAILY_STOP_RS | 2 / 10000 | 0–2 / 0–10000 | — |
| LIVE_FIRST_WEEK_MAX_ENTRIES / LIVE_FIRST_WEEK_SESSIONS | 1 / 5 | 0–1 / ≥5 | — |
| LIVE_DD_STOP_RS / LIVE_PROBATION_TRADES / LIVE_PROBATION_LOSS_RS | 15000 / 20 / 6000 | 0–15000 / ≥20 / 0–6000 | — |
| LIVE_MIN_MARGIN_RS | 60000 | ≥60000 | — |

**Code constants (not settings).**
- `G1_*` (§10.7), `ENGINE_SEMANTICS_VERSION=2` (engine semantics 2, 2026-10-14; FINAL_SPEC errata), `PAPER_MODEL_VERSION=2` (paper model 2, 2026-10-14; FINAL_SPEC errata), `SCHEMA_VERSION=1`, `EXIT_BOUNDS`, `BANDS`, `MAX_EXIT_DISCOUNT=0.12`, `THIN_BOOK_PENALTY=0.02`.
- Order timers: poll at 3 s, cancel at 6 s, stuck at 10 s; lookups at +1 s and +3 s.
- Reconciliation and verify-flat: `FRESH_FILL_GRACE_S=5`, `POSITIONS_POST_FILL_LAG_S=2`, `RECON_CONFIRM_GAP_S=3`, `POSITIONS_WAIT_MAX_S=5`, `BROKER_READS_FAIL_UNCAP_S=10`, `VERIFY_FLAT_RECHECK_S=2`, `VERIFY_FLAT_TIMEOUT_S=10`.
- Quotes and loop: `QUOTE_REQUEST_TIMEOUT_S=3`, `REST_BATCH=500`, `INTENT_MAX_PER_MIN=30`, `TIMER_DT=0.25`, `HEARTBEAT_EVERY_S=60`.
- Limits: `EXC_LIMIT=20/min`, `GATE_ERROR_LIMIT=5 per 5 min`, `POST_EXIT_TAIL_MIN=45`.
- `settings.SEMANTIC_CONSTANTS` is a dict of every constant that affects trade selection, pricing or fills. Its sha256 is recorded in `strategies/REGISTRY.lock` next to `ENGINE_SEMANTICS_VERSION`. A test fails if the hash changes without a version bump.

### 8.5 The single `size()` (`core/sizing.py`, R7)
```python
def size(inst: Instrument, q: Quote, limit_px: float, profile: ExitProfile, s: Settings,
         day_net: float | None, rates: RateSet, policy: Policy) -> Sized | Refusal:
    caps = (s.MAX_LOTS, s.MAX_UNITS, s.MAX_LOSS_PER_TRADE_RS, s.MAX_PREMIUM_PER_TRADE_RS)
    if any(isinstance(c, bool) or not isinstance(c, (int, float)) or not math.isfinite(c) or c <= 0 for c in caps):
        return Refusal("SIZE", "INVALID_OR_ZERO_CAP")            # 0 / negative / NaN / non-numeric never means "disabled"
    lot = inst.lot_size
    if not isinstance(lot, int) or lot <= 0:            return Refusal("SIZE", "LOT_INVALID")
    if q.ask is None or min(q.ask, limit_px) < s.MIN_PREMIUM_RS: return Refusal("SIZE", "PREMIUM_FLOOR")   # S1
    per_lot_risk = lot * limit_px * (profile.hard_sl_pct + s.STOP_SLIP_ALLOW_PCT) / 100 + round_trip_charges(limit_px, lot, rates)
    terms = {"RISK":    floor(s.MAX_LOSS_PER_TRADE_RS / per_lot_risk),
             "DEPTH":   floor(min(top_qty(q.asks, 3), top_qty(q.bids, 3)) / (s.LIQ_DEPTH_MULT * lot)),  # entry AND exit side
             "LOTS":    int(s.MAX_LOTS),
             "UNITS":   floor(s.MAX_UNITS / lot),
             "PREMIUM": floor(s.MAX_PREMIUM_PER_TRADE_RS / (lot * limit_px))}
    if policy is Policy.FULL:
        if day_net is None: return Refusal("SIZE", "DAY_PNL_UNKNOWN")
        terms["DAY_BUDGET"] = floor((s.DAILY_STOP_RS + min(day_net, 0.0)) / per_lot_risk)   # a stop-out cannot breach D3
    binding = min(terms, key=terms.get); lots = terms[binding]
    if lots < 1: return Refusal("SIZE", binding)               # skip if 1 lot breaches any cap
    return Sized(qty=lots * lot, lots=lots, binding=binding, per_lot_risk_rs=per_lot_risk)
```
- `size()` is applied last: the order is built from `Sized.qty` and nothing modifies it afterwards.
- `Intent` has no qty field.
- No `multiplier` exists anywhere (AST test).
- With the defaults, a 1-lot trade passes the RISK term only if `premium × 20% + charges ≤ ₹15,000`, i.e. premium ≲ ₹73.9k, so the ₹50k PREMIUM term binds first; G20 (§8.8) then holds one position's premium to about ₹49.85k.
- DAY_BUDGET's inputs (FINAL_SPEC errata, review round 3 and its review): the day term is the book's realized net (`realized_net`; a gain gives no room), and `open_risk_rs` subtracts each open or entering structure's stop-out measured from its entry (`sizing.remaining_stop_risk`: its plan's per-lot risk × lots, less what a lock or trail raised its stop, never below 0, or its loss now when larger; an open gain is neither credited nor charged). A LIVE book is also capped by the LIVE account's room, the same sums over every LIVE book against `LIVE_ACCOUNT_DAILY_STOP_RS`. With nothing open this is the listing above.

### 8.6 Gates (`core/risk.py`)
- Gates are pure: `gate_x(ctx: GateContext, …) -> None | Refusal | Divert`.
- Any exception becomes `Refusal(<gate>, "GATE_ERROR")` plus a `GATE_ERROR` event. Five gate errors in 5 minutes raise `HALT(GATE_ERRORS, GLOBAL, ACK)`.
- `GateContext` is a frozen snapshot that the engine builds per check. It holds:
  - time: `now_mono`, `now_wall`, `session`;
  - configuration: `settings`, `calendar`, `clock_synced`, `instruments_loaded`;
  - eligibility: `ban_list` (`frozenset[str] | None`), `blocked_symbols`;
  - market data: `feed: FeedHealth`, `signals_degraded`;
  - reconciliation: `recon: Mapping[Account, ReconStatus]`;
  - halts and modes: `halts`, `strategy_modes`, `running_versions`;
  - `book: BookState`: policy, open/reserved counts, entries, attempts, orders_today, entry_orders_60s, realized_net, mtm_net (or None), first_loss_today, cooldown_until, active_underlyings;
  - `live_account: LiveAccountState | None`;
  - `live: LivePreconditions | None`: allow_live, unlocked, selftest_passed, demoted, first_week.

**Pre-reserve gates** (on `IntentReceived`, per routed book; the first refusal is recorded)
| # | Gate | Refuses when (missing or stale data ⇒ refuse) | FULL | QUALITY |
|---|---|---|---|---|
| G01 | STRATEGY | name not exactly registered; version ≠ running version; mode does not route here; strategy disabled | ✓ | ✓ |
| G02 | STRUCTURE | type has no handler (phase 1: LONG_CE/LONG_PE only) | ✓ | ✓ |
| G03 | TTL | `now_mono − created_mono > ttl_s` | ✓ | ✓ |
| G04 | HALTS | any active halt in scope GLOBAL / ACCOUNT / BOOK / STRATEGY / UNDERLYING. If **all** blocking halts are operator halts and the book is `PAPER:<s>`, **DIVERT** to `SHADOW:<s>` (`blocked_by=kind`) and set `session_flags IMPAIRED` | ✓ | GLOBAL/ACCOUNT only |
| G05 | SESSION | not a trading session; outside [ENTRY_START, NO_NEW_AFTER); clock unsynced; today's instruments not loaded | ✓ | ✓ |
| G06 | FEED | not connected; last underlying tick older than 5 s; less than 60 s since (re)connect; signals degraded | ✓ | ✓ |
| G07 | RECON | no CLEAN recon for the account within 30 s | ✓ | ✓ |
| G08 | ELIGIBILITY | stock: ban list for today missing, or underlying banned; underlying not in the universe, or deny-listed | ✓ | ✓ |
| G09 | BOOK_CAPS | any of: open+reserved ≥ MAX_POSITIONS_PER_BOOK; entries ≥ MAX_ENTRIES_PER_DAY (LIVE in the first 5 sessions: 1); attempts ≥ MAX_ENTRY_ATTEMPTS_PER_DAY; orders_today ≥ 40, or entry orders in the last 60 s ≥ 3 (also `HALT(RUNAWAY, BOOK, ACK)`, except for an intent woken from a wait); entry orders in the last 60 s + RESERVED structures (each sends one order after G09) ≥ 3 (`ENTRY_ORDERS`, no halt); LIVE open+reserved across LIVE books ≥ LIVE_ACCOUNT_MAX_POSITIONS. A refusal due only to pending structures makes the first book's intent wait (§5.4 step 1) | ✓ | — |
| G10 | UNDERLYING | a non-terminal structure exists for (book, underlying), or for (LIVE account, underlying) | ✓ | ✓ |
| G11 | COOLDOWN | `cooldown_until(book, underlying) > now` | ✓ | — |
| G12 | DAY_RISK | book MTM unknown; realized + MTM ≤ −DAILY_STOP_RS; STOP_AFTER_FIRST_LOSS set and a loss already today; LIVE account-level MTM unknown or ≤ −LIVE_ACCOUNT_DAILY_STOP_RS | ✓ | — |
| G13 | LIVE | LIVE books only: ALLOW_LIVE ≠ 1; not unlocked (mode LIVE + trial CERTIFIED + unlock report); self-test not passed today; demoted | ✓ | n/a |

**Post-quote gates** (on `QuoteResult`)
| # | Gate | Refuses when |
|---|---|---|
| G14 | TTL | again |
| G15 | CONTRACT | no expiry satisfies the DTE rule; requested strike missing (**no clamping**); lot/tick invalid; `SYMBOL:` CONTRACT_BLOCKED |
| G16 | RECHECK | underlying LTP ≤2 s old is missing, or outside `[recheck.lo, recheck.hi]` (E6) |
| G17 | QUOTE | option quote missing or older than 1.5 s; bid or ask missing; `ask ≤ bid` (crossed or locked) |
| G18 | SIZE | `size()` refused (`limit = entry_limit(q)`; refuse if None) |
| G19 | LIQUIDITY | spread > max(2·tick, 1.5%·mid); top-3 ask or bid qty < 3× qty; walk(asks, qty, limit) fillable < qty, or avg > mid×1.01; last trade older than 60 s, or unknown; charges hurdle > 1.0% |
| G20 | WORST_CASE (FULL) | `min(realized,0) − Σ_open(premium + entry charges + 100) − (new premium + est. entry charges + 100) < −MAX_WORST_CASE_DAY_LOSS_RS`; LIVE also checked at account level |
| G21 | FUNDS (FULL) | margins cache older than 60 s or failed; `available < premium × 1.1 + est. charges` (paper: PaperExchange margins) |
| G22 | FINAL | TTL and halts re-checked immediately before the order row is created |

### 8.7 Halts (sticky by kind; **halts never block exits**)
| Kind | Scope | Set by | Clears | Operator |
|---|---|---|---|---|
| PAUSE | GLOBAL | PAUSE command | RESUME | yes |
| FLATTEN | GLOBAL | FLATTEN_ALL | RESUME (accepted only when flat is verified) | yes |
| KILL | GLOBAL | KILL (CLI) | ACK (only when flat is verified) | yes |
| DAILY_STOP | BOOK, ACCOUNT:LIVE | daily-stop trip | NEXT_SESSION (D3) | no |
| LOSS_STREAK | BOOK | STOP_AFTER_FIRST_LOSS | NEXT_SESSION | no |
| MISMATCH | ACCOUNT | recon / verify-flat | ACK (accepted only if recon is CLEAN at ack time) | no |
| UNKNOWN_POSITION | ACCOUNT | recon FOREIGN/EXCESS | ACK_FOREIGN exact, or ACK when clean | no |
| SHORT_DETECTED | GLOBAL | recon | ACK (clean) | no |
| INVARIANT, EXCEPTION, GATE_ERRORS | GLOBAL | engine | ACK | no |
| ORDER_STUCK, REJECTS, RUNAWAY | BOOK | order SM / gates | ACK | no |
| EXIT_DIFFICULTY | BOOK | ≥10 unfilled exit attempts | AUTO (when that leg is flat) | no |
| TOKEN | GLOBAL | Token/Permission exception | AUTO (when `profile()` OK) | no |
| STALE_FEED | GLOBAL | feed health | AUTO (60 s healthy) | no |
| STALE_RECON | ACCOUNT | 3 consecutive snapshot errors | AUTO (2 OK) | no |
| SETTINGS_INVALID | GLOBAL | settings load | AUTO (next start with a valid file) | no |
| CLOCK_UNSYNCED, NO_INSTRUMENTS | GLOBAL | prestart self-check | AUTO | no |
| SAFE_FLAG | GLOBAL | `bot --safe` or `TITAN2_SAFE=1` | ACK | yes |
| V1_ACTIVE | GLOBAL | self-check: v1 units not masked, or a v1 cron line present | AUTO (once masked/removed) | no |
| CONTRACT_BLOCKED | SYMBOL | RMS block reject | NEXT_SESSION | no |
| LIVE_DISABLED_TODAY | ACCOUNT:LIVE | self-test fail / egress-IP mismatch | NEXT_SESSION | no |

- **Session rollover.** The first Timer of a new session clears every NEXT_SESSION halt.
- **Survive restarts.** All halts are DB rows.
- **Exits.** No halt ever stops the exit loop. KILL and FLATTEN only add exits.

### 8.8 Daily stop and the hard worst-case cap
- **Daily stop (D3).**
  - Checked on every MTM update: if the book's realized + MTM ≤ −DAILY_STOP_RS:
    1. `HALT(DAILY_STOP, BOOK:<b>, NEXT_SESSION)`;
    2. every open leg in the book → `EXITING(DAILY_STOP)`;
    3. CRITICAL alert if LIVE, WARN if paper.
  - LIVE account level: if the Σ over LIVE books ≤ −LIVE_ACCOUNT_DAILY_STOP_RS, raise `HALT(DAILY_STOP, ACCOUNT:LIVE)` and flatten every LIVE book.
  - The halt survives restarts and MTM recovery.
- **Worst-case cap (G20).** This is the truly hard bound. Even if every open position goes to zero with no bids, the day's loss stays ≤ MAX_WORST_CASE_DAY_LOSS_RS (₹50k), plus broker square-off fees. With the ₹50k per-trade premium cap, not even one max-premium position passes: premium + entry charges + the ₹100 pad must stay within ₹50k, so one position's premium tops out at about ₹49.85k.

### 8.9 Operator commands: kill, flatten, pause (`core/operator.py`)
- **PAUSE.**
  - Adds an operator halt.
  - **RESUME** clears PAUSE, and clears FLATTEN once flat is verified.
- **FLATTEN_ALL** (dashboard with the typed word `FLATTEN`, or CLI):
  1. Raise `HALT(FLATTEN)`.
  2. Cancel every non-terminal ENTRY order and verify each reaches a terminal state.
  3. Move every bot leg in every book to `EXITING(OPERATOR_FLATTEN)`.
  4. Ack **DONE** when all legs are CLOSED and post-fill snapshots show flat on every bot symbol. Ack **FAILED** after 60 s with per-leg detail and a CRITICAL alert, and keep trying.

  Non-bot positions are listed and never touched (R11).
- **KILL** (CLI only, typed `KILL`):
  - Runs FLATTEN_ALL with reason `KILL_SWITCH`.
  - Raises `HALT(KILL, ACK)`.
  - Demotes every LIVE strategy to PAPER; its trial becomes DEMOTED.
- **Side effects of operator halts.** They mark the affected strategies' sessions IMPAIRED and divert PAPER intents to SHADOW (§10.7).

## 9. Reconciliation and restart recovery (`core/recon.py`)

### 9.1 Ownership
- **Bot-owned order:** `parse_tag(tag)` succeeds, the account letter matches, and the `order_pk` is in the DB. Today's `T2` tags missing from the DB are imported during recovery.
- **Bot-owned position:** an (account, symbol, MIS) whose net quantity is fully explained by bot-owned fills.
- **FOREIGN:** anything else.
- **EXTERNAL closing order:** a non-bot order on a bot-owned symbol.

### 9.2 Broker views and the post-fill rule (`EngineState.broker[account]`)
- **Latest snapshots.** The engine keeps the latest `PositionsSnapshot` and `OrdersSnapshot` (each with `req_mono`) and the `ReconMemory`.
- **`post_fill(snapshot, leg)`** holds when `snapshot.ok and snapshot.req_mono ≥ leg.last_fill_mono + POSITIONS_POST_FILL_LAG_S (2 s)`.
  - After a restart, every leg's `last_fill_mono := process start`.
  - A snapshot that does not satisfy this is **never** used to cap an exit, verify flat, or detect an external close. This fixes positions-API lag, which no design handled.
- **Grace.** A symbol is in grace while any bot order on it is non-terminal, or while any fill on it is younger than 5 s.
- **Confirmation.** A difference counts only when seen in two post-fill snapshots whose `req_mono` values differ by ≥3 s, both outside grace.

### 9.3 Periodic reconciliation
`reconcile(bot: BotView, pos, ords, mem, now) -> ReconResult` is pure.

**Cadence:**
- every 10 s per account between 09:00 and 15:31;
- every 5 s while the account has open legs;
- immediately on verify-flat, external-wait or RUN_RECON;
- at startup, and at 15:27 and 15:31.

Quantities are compared **signed**, per (symbol, product). This fixes v1's `abs()` at position_reconciliation 344.

| Class | Condition | Action (never an entry; never an unexplained sell) |
|---|---|---|
| BROKER_ERROR | snapshot not ok (any error, including 429) | No inference. 3 in a row → `HALT(STALE_RECON, ACCOUNT, AUTO)` |
| CLEAN | every bot symbol's broker net == expected; no foreign rows except exact acks | `recon.last_clean_mono = now` |
| PENDING | difference seen once, or symbol in grace | wait for confirmation |
| REPAIRED | broker > expected, and `filled_qty` of bot-tagged orders in `orders()` explains the gap | apply those snapshots + `FetchTrades`; `FILLS_REPAIRED` INFO; no halt |
| EXTERNAL_CLOSE | broker net 0 while expected > 0, confirmed | `close_external()`. Its EXTERNAL orders come from non-bot SELL orders on the symbol with `filled_qty>0` and `order_wall ≥ entry_fill_wall`. Fills come from `FetchTrades`, else from the CUM average. P&L status: **FINAL** if those orders explain the missing qty; **ESTIMATED** if derived from the positions row (`(sell_value − bot sell value)/(sell_qty − bot sells)`); **UNRESOLVED** otherwise (estimated at last rb). Order time ≥ BROKER_SQUAREOFF_AT with a non-API `placed_by`/no tag **[V]** ⇒ `BROKER_SQUAREOFF` (`external_kind=AUTO_SQUAREOFF`, CRITICAL, no MISMATCH halt). Otherwise MANUAL ⇒ `HALT(MISMATCH, ACCOUNT, ACK)` + CRITICAL |
| PARTIAL_EXTERNAL | 0 < broker < expected, confirmed | leg → `EXITING(ANOMALY_EXIT)` capped at the broker qty; `HALT(MISMATCH)`; CRITICAL |
| EXCESS | broker > expected and not explained by bot orders | `HALT(UNKNOWN_POSITION, ACCOUNT, ACK)`; CRITICAL; never traded |
| FOREIGN | non-bot symbol with net ≠ 0 (any product), or a non-MIS row | `HALT(UNKNOWN_POSITION)` unless an exact `foreign_acks` row (symbol, product, qty) exists for this session. A changed qty re-halts |
| SHORT | any NFO net < 0 | `HALT(SHORT_DETECTED, GLOBAL, ACK)` + CRITICAL. **Auto-COVER** only if all of: (a) two snapshots ≥2 s apart agree; (b) bot-tagged SELL fills − bot-tagged BUY fills for the symbol today == \|short\| exactly; (c) the symbol is bot-owned today. Then COVER BUY IOC for exactly that qty through the cover loop. Otherwise alert only |

- Paper accounts reconcile against `PaperExchange` through the same code.
- On a PAPER account, any class other than CLEAN, PENDING or BROKER_SQUAREOFF counts as a G1 mismatch.

### 9.4 Exit cap and verify-flat (lag-safe)
```
broker_cap(leg, now):
  s = latest positions snapshot(account)
  if post_fill(s, leg): return max(0, net_long(s, symbol, "MIS"))
  if no positions request in flight: FetchPositions(account, P0)
  if now - cap_wait_start < 5: return WAIT
  o = latest orders snapshot(account)            # FetchOrders(P0) if needed
  if post_fill(o, leg): return max(0, Σ BUY filled − Σ SELL filled over ALL orders for symbol/MIS today)
  if now - cap_wait_start < 10: return WAIT
  emit EXIT_UNCAPPED (WARN); return open_qty(leg)  # both broker reads failing: exit on own fills
external_wait(leg):   # cap == 0 while open_qty > 0: place nothing; request recon; recon either confirms
                      # EXTERNAL_CLOSE or a later post-fill snapshot shows qty again (lag) and the loop resumes
verify_flat(leg):     # after bot open_qty == 0; request positions at last_fill + 2 s
  post-fill net == 0                       -> CLOSED
  net > 0, confirmed (2 post-fill snapshots ≥3 s apart, ≥5 s after last fill)
                                           -> CLOSED with broker_residual_qty=net; HALT(MISMATCH)+CRITICAL;
                                              residual handled as FOREIGN; NEVER sold
  net < 0                                  -> SHORT path (§9.3)
  positions failing > 10 s                 -> orders view (post-fill) == 0 -> CLOSED (VERIFY_FLAT_VIA_ORDERS);
                                              else keep waiting, CRITICAL after 30 s
```
`PaperExchange` positions never lag, but the identical code runs for paper. This keeps paper and live delays comparable.

### 9.5 Startup sequence (`runtime.py`)
1. Parse args. Set up logging (`/var/log/titan2/bot.log` + stdout). systemd `EnvironmentFile` provides the env.
2. Take the flock, or exit 73. Run `assert_clean_cwd()`: refuse (exit 78) if the cwd contains any `*.py` file, which would be v1 shadowing. Assert Python ≥3.10. Assert KiteConnect is unpatched.
3. Load `Settings`. If invalid, use code defaults and stage `SETTINGS_INVALID`.
4. Open the Store. Before any migration, take a backup to `backups/premigrate-<ts>.db`. Then migrate and run `integrity_check`. On failure: exit 78 (`RestartPreventExitStatus`) with a CRITICAL alert through a direct Telegram call.
5. Start the alerts thread.
6. Load `SessionCalendar`. If today is not a trading session, or now > 15:45:
   - with no non-terminal structures in the DB: `READY=1`, `STOPPING=1`, exit 0;
   - with non-terminal structures: run recovery and recon only, then exit 0.
7. Read the session token, build `KiteConnect(timeout=5)` and `KiteData`, and call `profile()`. On failure: `HALT(TOKEN)` (exits impossible), re-check every 30 s, CRITICAL alert.
8. Build `QuoteCache`. Build `InstrumentStore` from today's cached dump if present; otherwise it is loaded during prep. Build the lanes (live-orders lane always), `PaperExchange` (PAPER and SHADOW), `Feed` (not started) and `Engine.load()`.
9. Start the core loop, lanes and paper-sim.
10. Post `RecoveryStart`. The engine runs §9.6 (phase RECOVERING), using REST quotes for exits.
11. When recovery is done, send `sd_notify READY=1` (target < 30 s from start).
12. Start the WS feed. Subscribe underlyings in quote mode and held or pending options in full mode.
13. Prep (only once per day, cached): the instruments dump (§11.1), the NSE ban list (§11.6), previous-session 5-min bars (§10.3). Then the prestart self-check (§13.7). Each failure only adds the matching entry halt.
14. `StartSignals` once prep is done. Strategies ignore ticks before 09:15:00.
15. If any strategy is LIVE-enabled, run the live self-test at 09:15:30, with a retry at 09:17:00 (§10.8).
16. Run the core loop until the EOD timeline ends (§12.5), then `STOPPING=1` and exit 0.
17. **SIGTERM:**
    1. stop entries;
    2. cancel non-terminal ENTRY orders (wait ≤10 s);
    3. let in-flight EXIT orders reach a terminal state (≤10 s);
    4. commit and exit 0.

    Open positions are deliberately left for recovery. Stopping never flattens.

### 9.6 Recovery algorithm (phase RECOVERING; exits resume as soon as a leg is consistent)
```
for account in (LIVE, PAPER, SHADOW):
  FetchOrders, FetchPositions, FetchTradebook   (≤3 tries, 2 s apart; failure -> HALT(STALE_RECON, ACCOUNT); exits use §9.4 caps)
  for o in DB non-terminal orders(account):
      b = match(o.broker_order_id) or match_tag(o.order_pk)
      if b: apply snapshot (source RECOVERY); if b non-terminal: follow §5.2 timers (poll, then cancel)
      elif o.state in (SUBMITTING, UNKNOWN): LookupTag twice (+1 s, +3 s) -> NOT_PLACED
      else: INVARIANT_VIOLATION("working order vanished") + HALT(INVARIANT) + PollOrder
  for b in orders snapshot with is_bot_tag(b.tag, account) and b not in DB:      # lost writes
      attach to the leg (account, session, seq_no) if it exists, else create a structure in book "RECOVERED:<account>"
      (strategy "_recovered", default ExitProfile from fills), CRITICAL alert
  FetchTrades for terminal LIVE orders lacking TRADE fills
  rebuild open_qty of every non-terminal leg from fills
  ENTERING legs whose entry order is terminal -> OPEN or VOID now
  RESERVED structures with no order -> VOID("RESTART")                        # intents are never replayed
  reconcile() twice ≥3 s apart (post-fill rule with last_fill_mono = start) -> apply findings
  OPEN/EXITING legs: restore ExitParams, stop, lock, peak/trough from DB; EXITING resumes with its persisted reason
  if now >= EOD_FLATTEN: OPEN legs -> EXITING(RECOVERY_FLATTEN)
  structures from an earlier session still non-terminal:
     LIVE  -> if broker shows nothing: CLOSED_EXTERNAL, pnl UNRESOLVED (earlier days' orders are not in the API), HALT(MISMATCH), CRITICAL
     PAPER/SHADOW -> PaperExchange's startup square-off (§6.4) produces BROKER_SQUAREOFF
entries become possible only after: 2 consecutive CLEAN recons per account, feed warm, prep done, no entry halts
```

### 9.7 Token rotation, WS reconnect, back-fill
- **Token or permission failure.** Any `TokenException`/`PermissionException` raises `HALT(TOKEN, GLOBAL, AUTO)` plus a CRITICAL alert ("exits impossible until the token is restored").
  - The engine re-reads `session/kite_session.json` every 30 s.
  - If the token there is newer and `profile()` succeeds (reads lane), the engine does a **controlled restart**: `Exit(75)`. systemd restarts the bot; recovery builds a new KiteConnect and KiteTicker. A running reactor cannot rebind its token, so a restart is the tested path.
  - The bot never logs in itself.
- **WS reconnect** (`FeedState.reconnected`):
  - `FetchOrders(LIVE, P0)`, then apply snapshots to every non-terminal bot order. This back-fills order updates missed while the WS was down.
  - `on_feed_reset()` for every strategy.
  - Bars spanning the gap are marked invalid.
  - `on_connect` re-applies the desired subscriptions.
- **`on_noreconnect`.** The bot sends `FeedState(connected=False)` with a CRITICAL alert. If the WS is still down 60 s later, it exits 75; the restart rebuilds the ticker.

### 9.8 EOD truth check (15:33)
- **LIVE.** Σ `trades.net` over LIVE books today must equal the net recomputed from the broker tradebook (bot-tagged orders' trades) with the same charges function, within ₹1. Otherwise `EOD_TRUTH_MISMATCH` (CRITICAL). The virtual contract note check (§8.1) runs per order.
- **PAPER/SHADOW.** The ledger must equal the `PaperExchange` recompute exactly. Any difference is a G1 mismatch for the affected books.


## 10. Strategies, shadow/placebo, G1, LIVE

### 10.1 Plug-in API (`strategies/base.py`)
```python
class MarketView(Protocol):                       # read-only, built by the signals thread; no engine/broker/risk access
    def ltp(self, underlying: str) -> tuple[float, float] | None: ...          # (price, age_s)
    def last_tick(self, underlying: str) -> UnderlyingTick | None: ...
    def bars(self, underlying: str, n: int) -> tuple[Bar, ...]: ...            # completed LIVE bars today, oldest first
    def prev_session_bars(self, underlying: str, n: int) -> tuple[Bar, ...]: ...  # HIST bars of the previous session
    def universe(self) -> frozenset[str]: ...                                   # eligible stock underlyings today
    def feed_ok(self) -> bool: ...
class Strategy(ABC):
    NAME: ClassVar[str]; LOGIC_VERSION: ClassVar[int]; PARAMS: ClassVar[Mapping[str, object]]
    EXIT_PROFILE: ClassVar[ExitProfile] = ExitProfile()
    UNIVERSE: ClassVar[str] = "STOCKS"            # "STOCKS" | "INDICES"
    WINDOW: ClassVar[tuple[str, str]]             # emission window, inside [ENTRY_START, NO_NEW_AFTER)
    def on_tick(self, t: UnderlyingTick, mv: MarketView) -> Sequence[Intent]: return ()
    def on_bar_close(self, b: Bar, mv: MarketView) -> Sequence[Intent]: return ()
    def on_timer(self, now: datetime, mv: MarketView) -> Sequence[Intent]: return ()
    def on_feed_reset(self) -> None: ...          # WS reconnect: drop baselines, pending sustains, bars in progress
def make_intent(cls, version, *, underlying, direction, strike_pref, trigger_px, recheck, ttl_s,
                now_mono, now_wall, reason) -> Intent
    # derives structure (UP -> LONG_CE, DOWN -> LONG_PE); validates 0 < ttl_s <= 10, trigger_px > 0, lo <= hi,
    # reason values numeric/str; intent_id = uuid4().hex[:12]. Raises ValueError otherwise.
```
- **Strategies never read the clock.** Time comes from ticks, bars and `on_timer(now)`.
- **A strategy that raises is disabled for the rest of the session** (fails closed), with an alert.
- **At most 30 intents per minute per strategy.** Any excess is dropped and an event is logged.
- **Purity is tested by AST** (§2 import rules).

### 10.2 Registry and version hash
- `REGISTRY = {"opening_drive": OpeningDrive, "price_spike_up": PriceSpikeUp}`
- The version is computed as:
```python
version(cls, settings) = f"{cls.LOGIC_VERSION}." + sha256(canonical_json({
    "name": cls.NAME, "params": cls.PARAMS, "exit_profile": asdict(cls.EXIT_PROFILE),
    "settings": settings.versioned_view(), "engine": ENGINE_SEMANTICS_VERSION,
    "paper_model": PAPER_MODEL_VERSION, "semantic_constants": SEMANTIC_HASH}))[:10]
# canonical_json = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=repr)
```
- **`REGISTRY.lock`** holds `{name: {logic_version, ast_hash}, "_engine": {engine_semantics_version, semantic_constants_hash}}`.
  - `ast_hash = sha256(ast.dump(ast.parse(src), include_attributes=False))`, computed with docstrings stripped.
  - A test fails if any hash changes without a version bump.
- **Any version change starts a new G1 trial.**
  - The trigger is a change to logic, params, exit profile, any versioned setting, engine semantics or the paper model.
  - The new (strategy, version) is registered in `g1_trials` the first time it runs in PAPER. The previous trial becomes RETIRED.
  - This blocks silent tuning.

### 10.3 `opening_drive` v1 (stock options, PAPER), brief E2(a)
```python
PARAMS = {"bar_start": "09:15:00", "min_ret_pct": 2.5, "min_vol_mult": 3.0, "prev_bars": 20, "retrace_max": 0.65,
          "strike": "ITM1", "ttl_s": 10, "recheck_lo_pct": 0.5, "recheck_hi_pct": 1.0,
          "emit_at": "09:20:00", "emit_until": "09:20:10"}
```
- **The opening bar (09:15:00–09:19:59.999)** is built from WS quote-mode ticks:
  - `open` = the exchange day open (`tick.ohlc.open`);
  - `high`/`low` = extremes of the LTPs, including `open`;
  - `close` = the last LTP before 09:20:00;
  - `volume` = cumulative `volume_traded` at that tick. It includes pre-open volume, matching Kite's 09:15 candle **[V by bar check]**.
- **The bar is invalid** if the WS was disconnected at any moment between 09:15:00 and 09:20:00, or if there were no ticks.
- **Signal conditions** (all must hold):
  - `ret = close/open − 1`. UP needs `ret ≥ +2.5%`; DOWN needs `ret ≤ −2.5%`.
  - `volume ≥ 3 × mean(volume of the previous session's last 20 HIST bars)`. The window crosses the day boundary, as the backtest's `avgV` did.
  - VSURGE retrace rule from backtest_bs: UP needs `(high − close)/(high − open) ≤ 0.65`; DOWN needs `(close − low)/(open − low) ≤ 0.65`.
  - If fewer than 20 previous-session bars exist, the symbol is ineligible (`SKIP:NO_HISTORY`).
- **Emission:**
  - Happens at the first `on_timer` with `09:20:00 ≤ now < 09:20:10`, as one batch sorted by |ret| descending.
  - At most one intent per symbol per day.
  - The intent is ITM1 with `trigger_px = close`, TTL 10 s, and a recheck range on the underlying LTP:
    - UP: `[close×0.995, close×1.010]`
    - DOWN: `[close×0.990, close×1.005]`
- **Exits:** the default `ExitProfile` (brief E2a points to §3.5).
- **Evidence status: low-confidence paper hypothesis.** BS TEST shows +2.27% (model only); the DG analogue shows −0.24%.
- **The backtested exit profile is not run.** That profile is TP8 / SL30 / lock to BE+1% after +3%. It violates X4 and X5: an SL wider than 15%, and the 1% buffer was refuted.
  - Instead, each trade records its 1-s bid path while held, plus a 45-min post-exit tail (§7.5). This lets alternative exit profiles be compared offline on identical entries.
  - Any alternative that is adopted becomes a new pre-registered version and counts in the G1 family (§10.7).
- **Bar check** (no design had this). At 15:36 the core fetches today's historical 5-min bars for every symbol that fired, plus a seeded random sample of 20 universe symbols. It compares the live 09:15 bar against the historical one:
  - `|open| ≤ 0.1%`, `|close| ≤ 0.1%`, `|vol_live/vol_hist − 1| ≤ 5%`;
  - results go to `bar_checks`.
  - G1 requires that at most 10% of the sample's signal bars mismatch. Otherwise the paper population is not the backtested rule.

### 10.4 `price_spike_up` v1 (stock options, PAPER)
```python
PARAMS = {"spike_pct_open": 1.0, "open_until": "09:45:00", "spike_pct": 0.6, "base_window_s": 60, "base_b_offset_s": 30,
          "base_b_min_age_s": 5, "vol_confirm_mult": 0.5, "vol_hist": 50, "vol_min_hist": 3, "sustain_s": 12,
          "hold_pct": 0.35, "early_end": "09:55:00", "early_min_hold_pct": 1.0, "retrace_max_pct": 65,
          "cooldown_s": 90, "window": ("09:20:00", "14:55:00"), "strike": "ATM", "ttl_s": 10, "recheck_chase_pct": 0.5}
```
These are the values that were actually in effect during the evidence window:
- sustain 12 s and cooldown 90 s, copied at construction from config defaults (kite_ticker 1061/1077), not the JSON's 15/120;
- retrace 65, read from the JSON at runtime.

The rule below is ported from kite_ticker 1507-1533 and 2049-2108, with the sustain logic (1701-1848) made **signed**. Per stock symbol, using `recv_mono` for intervals and `recv_wall` for clock-of-day:
1. **Volume delta.** `d = max(0, cum − prev_cum) if prev_cum > 0 else 0`. If `d > 0`, append it to `deque(maxlen=50)`.
2. **Baselines.**
   - A: if A is unset or older than 60 s, set A = (ltp, t).
   - B: if B is unset, A exists, and A is at least 30 s old, set B = (ltp, t). If B is older than 60 s, reset it.
3. **Pending sustain.** If one exists, go to step 6. If the symbol is in cooldown or outside the window, skip detection. Nothing is queued outside the window (v1 queued pre-09:20 triggers at kite_ticker 2284).
4. **Move.**
   - `mA = (ltp − A)/A·100`.
   - `mB = (ltp − B)/B·100` if B is at least 5 s old, else −∞.
   - `m = max(mA, mB)`: signed, so only upward moves count. The baseline is whichever gave `m`.
5. **Detection.**
   - Threshold θ = 1.0 before 09:45, else 0.6.
   - Volume confirmation passes if fewer than 3 deltas exist, or the average delta is 0, or `d ≥ 0.5 × avg`.
   - If `m ≥ θ` and volume confirms, set `pending = {baseline, t0: t, trigger_px: ltp, peak: m}`.
6. **Sustain.**
   - `held = (ltp − baseline)/baseline·100` (signed); `peak = max(peak, held)`.
   - At `t − t0 ≥ 12 s`, it passes only if all hold:
     - `held ≥ max(0.35, 1.0 if before 09:55 else 0)`;
     - `held > 0`;
     - `peak ≤ 0` or `(1 − held/peak)·100 ≤ 65`.
   - Pass or fail, the 90 s cooldown starts and `pending` is cleared.
   - On pass, emit `Intent(UP, ATM, trigger_px=ltp, Recheck(lo=baseline×1.0035, hi=ltp×1.005), ttl_s=10, reason={m, held, peak, retrace, vol_ratio})`.
   - A move that reverses through its baseline fails. v1's `abs()` passed it (brief §4.1 #9).

What this strategy never does:
- emit DOWN (E1);
- flip direction (E3);
- use a scorer, OI, ML or a lot multiplier (E4).

### 10.5 Modes, routing, the live twin
| Mode | Intent goes to | Counts for G1 |
|---|---|---|
| OFF | not run | — |
| SHADOW | `SHADOW:<s>` (QUALITY) | never |
| PAPER | `PAPER:<s>` (FULL); a `PLACEBO:<s>` entry is created when it fills | yes |
| LIVE | `LIVE:<s>` (FULL + account guard) **and** the `PAPER:<s>` twin, from one decision snapshot | the paper twin continues the G1 and X2 record; LIVE trades feed the live review |

- **Live and paper populations match.**
  - `LIVE:<s>` has per-strategy caps that mirror `PAPER:<s>`, so live trades come from the same population as the G1 sample.
  - The exception is when the account guard binds (LIVE_ACCOUNT_MAX_POSITIONS, or the account daily stop). Those LIVE refusals are recorded, the twin still trades, and the report shows live coverage %.
- **Mode changes** are made with the CLI `SET_MODE`.
  - OFF takes effect immediately and marks that strategy's session IMPAIRED.
  - SHADOW, PAPER and LIVE take effect at the next session start (`pending_mode`).
  - Both strategies start in PAPER.

### 10.6 Placebo (opposite-direction shadow trade, R8)
- **When:** every `PAPER:<s>` entry that fills creates a placebo intent (`is_placebo=1`, `parent_intent_id`) in `PLACEBO:<s>`.
- **What:** the opposite option type (CE↔PE), the same strike rule relative to the same underlying LTP, and the same expiry rule.
  - It is planned at the parent's decision time, from the same quote snapshot (the decision fetch included the opposite contract).
  - It uses the QUALITY policy. The post-quote gates use the parent's decision snapshot. `size()` runs on the placebo's own premium.
- **How it is managed:** the same `decide()` and profile, booked by `net_pnl`.
- **If refused:** the parent's `placebo_status` becomes `SKIPPED:<gate>`, which counts against coverage.

### 10.7 G1 (`g1.py`): honest, multiple-testing-controlled
Code constants:

| Constant | Value |
|---|---|
| G1_MIN_TRADES | 100 |
| G1_MIN_SESSIONS | 30 |
| G1_LOOKS | (100, 150, 200, 300) |
| G1_ALPHA | 0.05 (one-sided, matching the brief's "90% CI lower bound") |
| G1_FAMILY_FLOOR | 4 |
| G1_SPREAD_STRESS | 1.5 |
| G1_PLACEBO_MIN_N | 50 |
| G1_PLACEBO_MIN_COVERAGE | 0.8 |
| G1_MAX_IMPAIRED_FRAC | 0.10 |
| G1_MAX_BAR_MISMATCH_FRAC | 0.10 |
| G1_CONFIRM_TRADES | 25 |
| G1_FUTILITY_MIN_N / _EVERY / _ALPHA | 50 / 25 / 0.10 |
| G1_REPORT_MAX_AGE_DAYS | 7 |
| G1_BOOT_MIN | 10,000 |

- **Population T:** trades in `PAPER:<s>` with `strategy_version = v` and `is_placebo = 0`, entered on or after the trial's registration.
- **Per-trade quantities:**
  - `N_i`: net recomputed with the **current** RateSet from the effective fills (square-off fees included).
  - `P_i`: `risk_basis_rs`.
  - `C_i = max(hs_cost, crossing_cost, 0)`, where:
    - `hs_cost = qty·(ask−bid)/2` at the entry decision, plus `Σ q_j·(ask−bid)_j/2` at the exit decisions;
    - `crossing_cost = Σ_entry (fill − decision_mid)·q + Σ_exit (decision_mid − fill)·q`.
  - Stressed return `r_i = (N_i − 0.5·C_i)/P_i` (1.5× the measured crossing cost). Unstressed return `u_i = N_i/P_i`.
- **Multiple-testing control** (no design had this):
  - Family size: `m = max(#rows ever in g1_trials, G1_FAMILY_FLOOR)`. It never decreases.
  - `α_look = G1_ALPHA/(m·len(G1_LOOKS))`: a Bonferroni correction across all trials and all pre-registered looks. With m = 4 this is 0.3125%.
- **Day-clustered bootstrap:**
  - D = number of sessions; `B = max(10,000, ⌈50/α⌉)`.
  - `seed = int(sha256(f"{s}|{v}|{max_structure_id}|{kind}").hexdigest()[:16], 16)`.
  - Each replicate resamples D sessions with replacement and takes `Σr/count` over the sampled sessions' trades.
  - `LB` = the ⌊α·B⌋-th order statistic. `UB` is defined symmetrically.

**Report kinds:**
- **PROGRESS** (nightly): informational only. The dashboard labels it "progress, not a decision".
- **LOOK**: evaluated only when n first reaches a G1_LOOKS value. **PASS** requires all of:
  - n ≥ 100 and D ≥ 30;
  - mean r > 0 and `LB(α_look) > 0`;
  - placebo: n ≥ 50, coverage ≥ 0.8, and mean placebo `u` ≤ 0 (**1.0× spreads**);
  - zero mismatches on account PAPER for this book's symbols in the window (MISMATCH, UNKNOWN_POSITION, SHORT_DETECTED, CLOSED_EXTERNAL of kind MANUAL, EOD_TRUTH_MISMATCH);
  - zero UNRESOLVED or ESTIMATED trades;
  - IMPAIRED sessions / D ≤ 0.10;
  - opening_drive only: bar-mismatch fraction ≤ 0.10.

  On PASS the trial goes to CONFIRMING. A failure at the last look sets it to FAILED and the mode to SHADOW, with an alert.
- **CONFIRM**: over the next 25 PAPER trades, mean r > 0 and zero mismatches. Success sets CERTIFIED and stores `cert_report_id` and `inputs_hash`. Failure returns the trial to COLLECTING; if no look remains, it becomes FAILED.
- **FUTILITY**: from n ≥ 50, every 25 trades, compute the 90% UB of the unstressed bootstrap. If UB < 0, the trial is FUTILE and the mode goes to SHADOW, with an alert. This saves months of hopeless paper time.

**`inputs_hash`** = `sha256(canonical_json({strategy, version, rates_version, trades: sorted (structure_id, round(N,2), round(C,2)), placebos: sorted (structure_id, round(N,2))}))`.

**Operator-selection control:**
- Intents blocked only by operator halts are diverted to `SHADOW:<s>` (G04), and that session is marked IMPAIRED.
- A heartbeat gap of more than 10 minutes inside 09:20–14:55 marks the session IMPAIRED for every active strategy.
- The report prints the Exp of the diverted trades.

**Power, stated plainly:**
- Per-trade σ is about 8–10% of premium. With day clustering, SE(mean) at n = 100 is about 1.0–1.2%.
- At α_look = 0.3125% (z ≈ 2.73), a version needs a stressed mean of roughly **+3% net per trade** to pass its first look.
- The evidence supports −1.5% to 0% (brief §1). The expected result is FUTILE or FAILED, and that is the system working.

### 10.8 LIVE unlock (two keys) and the daily self-test
1. The trial becomes CERTIFIED. An alert `G1_CERTIFIED` carries the `inputs_hash`.
2. **Key 1, the process.** root sets `ALLOW_LIVE=1` in `/etc/titan2/titan2.env`, then restarts the bot while flat, or before 08:45.
3. **Key 2, the strategy.** Run `titan2 live <s> --confirm <inputs_hash> --i-accept-live-risk`, which sends `SET_MODE{strategy, version, mode: LIVE, confirm}`.
   - It is accepted only if all hold:
     - the trial for (s, current version) is CERTIFIED with that hash;
     - the certificate is no more than 7 days old;
     - `ALLOW_LIVE=1` in the running process;
     - no GLOBAL ACK-halt is active;
     - the strategy has not been demoted.
   - On acceptance it sets `pending_mode=LIVE` (effective at the next session) and `live_unlock_report_id`.
4. **Daily self-test.** It runs on any session with at least one LIVE-mode strategy, at 09:15:30, with a retry at 09:17:00.
   - Book `LIVE:_selftest` places a BUY IOC for 1 lot of NIFTY ATM CE (nearest expiry with ≥2 TD) at `max(tick, floor_tick(bid×0.97))`.
   - It expects CANCELLED with filled 0 within 5 s, and records ack latency. This proves token, static IP, IOC on NFO, the tag round trip and WS order updates, at zero cost.
   - On failure: `HALT(LIVE_DISABLED_TODAY, ACCOUNT:LIVE, NEXT_SESSION)` plus CRITICAL. The paper twins continue.
   - If the order ever fills, it is exited at once (`SELFTEST_FILLED`) with a CRITICAL alert.
   - The self-check also requires egress IP == `REGISTERED_EGRESS_IP`. Otherwise LIVE_DISABLED_TODAY.

### 10.9 Live review, probation, the X2 falsifier (`g1.live_review`, after every LIVE close and nightly)
**Demotion triggers:**
- **Probation:** within the first 20 live trades, cumulative live net ≤ −₹6,000.
- **Drawdown:** cumulative live net since unlock ≤ −₹15,000.
- **Rolling-50:** once there are at least 50 live trades, the mean net of the last 50 is below 0 (the brief's live kill).
- **X2 falsifier**, which validates the paper model:
  - For paired (`LIVE:<s>`, `PAPER:<s>`) trades with the same `intent_id`, `gap = live slip_vs_walk − paper slip_vs_walk`, entry plus exit, as % of premium.
  - It fires after at least 20 pairs if the median gap is above 0.5 pp, **or** the live IOC fill rate is more than 10 pp below paper.
  - Result: `PAPER_MODEL_OPTIMISTIC` (CRITICAL) and **all** LIVE strategies are demoted. Recalibrating `PAPER_*` bumps the versions, so G1 restarts.
- **Reconciliation:** any LIVE MISMATCH, UNKNOWN_POSITION or SHORT triggers a global halt and demotes all LIVE strategies.

**What demotion does:**
- The mode becomes PAPER at once. No new LIVE entries are made; open LIVE legs are still managed. The trial becomes DEMOTED.
- Re-promotion needs a new version and a fresh G1, which by construction uses only paper trades made after the change.

**First live week:** `LIVE:<s>` may enter at most 1 trade per day for its first 5 live sessions.

## 11. Instruments, expiry, lot sizes, strikes, eligibility (`instruments.py`)

### 11.1 Daily dump
- **Prep:** `kite.instruments("NFO")` and `("NSE")` through the reads lane at BACKGROUND priority.
- **Cache:** `/var/lib/titan2/cache/instruments/YYYY-MM-DD/{NFO,NSE}.json.gz`. Today's cache is reused on restart. A previous day's dump is **never** used, because lots and expiries change.
- **Sanity checks.** A failure raises `HALT(NO_INSTRUMENTS)` plus CRITICAL. The checks:
  - NIFTY and BANKNIFTY CE/PE rows for at least 2 expiries each;
  - every kept option row has `lot_size > 0`, `tick_size > 0` and `strike > 0`;
  - an NSE row for every stock underlying, plus index tokens for "NIFTY 50" and "NIFTY BANK";
  - at least 100 stock underlyings.
- Open legs never depend on the dump: leg rows carry every instrument field.

### 11.2 Indexes
- `by_symbol`, `by_token`;
- `(name, expiry, opt_type) → sorted strikes`;
- `name → sorted expiries`;
- `underlying symbol ↔ name`.

### 11.3 Names, lots, ticks
- **Stocks:** `NSE:X ↔ X`, for NFO-OPT names that have an NSE EQ row.
- **Indices:** only `{"NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK"}`. This naming map comes from config 1564-1569 and contains no lot sizes.
- **Lot size and tick always come from the chosen contract's own row.** `FNO_LOT_SIZES` (options_trader 2927) is deleted.

### 11.4 Expiry and DTE (S2, R9)
- `td(a, b)` = the number of trading sessions d with `a < d ≤ b` (per `SessionCalendar`).
- `choose_expiry(name, kind, today)` = the first listed expiry `e ≥ today` with `td(today, e) ≥ MIN_TD_TO_EXPIRY_<kind>`.
- **Indices (MIN_TD = 2).** NIFTY weeklies roll forward on the Monday before a Tuesday expiry. BANKNIFTY monthly expiries come from the dump.
- **Stocks (MIN_TD = 5).** Stock monthlies roll to the next month from E−4. That avoids the physical-settlement window, where delivery margins and broker restrictions on ITM stock options begin **[V Zerodha policy]**.
- No weekday is hardcoded; expiries come only from the dump.
- Together with the EOD flatten, a position is never held into expiry day: no exercise STT and no physical delivery.

### 11.5 Strike selection (`select_contract(underlying, opt_type, pref, spot, today) -> Instrument | Refusal`)
- `spot` is the underlying LTP, no more than 2 s old.
- **ATM:** `argmin |k − spot|`. A tie picks the lower strike for CE and the higher for PE (more in the money).
- **ITM1:** for CE, the next lower listed strike below ATM; for PE, the next higher.
- **A missing neighbour is a Refusal.** There is no clamping (v1 clamped silently at options_trader 3529-3531).
- `opposite(inst, spot, pref)` returns the same name and expiry, the other option type, chosen by the same rule.

### 11.6 Eligibility
- **NSE F&O ban list.**
  - Fetched at prep: GET `BAN_LIST_URL`, default `https://nsearchives.nseindia.com/content/fo/fo_secban.csv` **[V]**, with browser headers, through the reads lane, up to 3 tries.
  - The file's trade date must equal today; the list is then cached.
  - A manual override file is allowed at `/var/lib/titan2/config/ban_override/YYYY-MM-DD.csv`.
  - If the list is missing or unparseable, every stock underlying is ineligible today (G08) and a WARN is raised. Indices are unaffected.
  - Paper and live apply the list identically.
- **Broker blocks.** An RMS block reject → `HALT(CONTRACT_BLOCKED, SYMBOL:<sym>, NEXT_SESSION)`, applied to every book.

### 11.7 `SessionCalendar` (`titan2/data/nse_sessions.json`)
```json
{"years": {"2026": {"holidays": ["2026-01-26", "…"],
                     "special_sessions": [{"date": "2026-02-01", "open": "09:15", "close": "15:30", "trade": false}]}}}
```
- **Source:** NSE's trading-holiday circular **[V]**.
- **Not trading days:** weekends, holidays, and special sessions with `trade: false` (Muhurat, budget Sundays).
- **Coverage:** if coverage runs less than 30 days ahead, the self-check raises a WARN. A date beyond coverage is treated as not a trading day (fails closed).
- **Session hours:** 09:15–15:30.

## 12. Dashboard and observability

### 12.1 Isolation
- **Process.** `titan2-dash` runs as `titandash`, bound to `127.0.0.1:8050` and reached over an SSH tunnel (`ssh -N -L 8050:127.0.0.1:8050 titan-bot`).
  - No nginx.
  - The security group allows no inbound 80/5000/8050.
- **What it can touch.**
  - It imports no kiteconnect, broker, core or store code (arch test).
  - It reads only `pub/*.json`.
  - It writes only `dash/commands_dash.db`, through a SQLite authorizer that allows INSERT into `commands` and SELECT. Everything else returns `SQLITE_DENY`.

### 12.2 Authentication
- **Credentials.** HTTP Basic, checked in-app against a scrypt hash: `DASH_USER` and `DASH_PASS_SCRYPT` in `/etc/titan2/dash.env`. The app refuses to start if either is missing.
- **Host header** must be `127.0.0.1:8050` or `localhost:8050`, which blocks DNS rebinding.
- **POST requirements:**
  - an `X-Titan-CSRF` header, equal to `HMAC(DASH_SECRET, date|user)` as returned by `GET /api/csrf`;
  - a unique `nonce`;
  - for `FLATTEN_ALL`, `confirm_text == "FLATTEN"`.

### 12.3 Routes
| Route | Returns / does |
|---|---|
| `GET /` | `index.html`, which polls every 2 s |
| `GET /api/status` | `pub/status.json` plus its age; shows "bot offline — use the Kite app" if older than 10 s |
| `GET /api/trades?scope=today\|90d` | ledger rows: net, charges breakdown, slippage, MFE/MAE, exit reason |
| `GET /api/trade/<id>` | orders, fills, 1-min quote path |
| `GET /api/events` | `events_tail.json` |
| `GET /api/g1` | per-strategy scorecard (n, sessions, Exp, LB, placebo, next look, trial state) |
| `GET /api/acks` | last 50 command acks |
| `POST /api/command` | `{kind ∈ {PAUSE, RESUME, FLATTEN_ALL}, confirm_text, nonce}` is inserted into `commands_dash.db` and the `cmd_id` returned; the UI then follows `acks.json` |

The dashboard computes nothing. Every number comes from bot-written files produced by `net_pnl`. It shows realizable-bid MTM, with LTP greyed out as "info only".

### 12.4 Observability artifacts (R10)
| Artifact | Content |
|---|---|
| `events` | append-only |
| `trades` | fill-based, immutable, with a charges breakdown |
| `quote_checks` | per check: bid/ask/qty/age/src/rb/stop/decision; HOLD at ≤1/s and on decision changes; POST tail every 5 s for 45 min |
| MFE/MAE | bid-based, with timestamps |
| 1-min premium path | derived from `quote_checks` |
| `intent_routes` | every intent's outcome and refusal gate |
| `g1_reports` / `pub/g1.json` | G1 scorecards |
| Logs | Python logging to `/var/log/titan2/bot.log` (WatchedFileHandler) and stdout (journald) |

### 12.5 EOD timeline (core)
| Time | Action |
|---|---|
| 15:15 | EOD_FLATTEN: every open leg goes to EXITING |
| 15:22 | EOD_BACKSTOP: band ≥3; CRITICAL for any open leg; immediate recon |
| 15:25 | STOP_SENDING |
| 15:26 | Broker auto square-off (paper simulates it) |
| 15:27, 15:31 | Final recons (BROKER_SQUAREOFF classification) |
| 15:33 | EOD truth check + virtual-contract-note check |
| 15:36 | Bar check (historical vs live) |
| 15:38 | Exports (events/trades JSONL.gz); daily report; `pub/trades_90d.json`; nightly G1 (PROGRESS, LOOK, CONFIRM, FUTILITY); live review; weekly prune on the last session of the week |
| 15:40 | `STOPPING=1`, exit 0 |

**Daily report** (`/var/lib/titan2/reports/YYYY-MM-DD.md` plus a Telegram summary), per book and strategy:
- n, WR_net, Exp_net (₹ and %), worst trade, average win/loss, Σ charges;
- exit-reason mix;
- entry/exit slippage versus mid and versus the decision walk; spread distribution;
- refusals by gate;
- halts, mismatches, alerts;
- paper–live twin gaps and live coverage %;
- G1 progress: n, sessions, next look, futility UB;
- VCN check and bar check;
- latencies (intent→submit, submit→ack, ack→terminal; p50/p95), p95 quote age at decision, loop stalls.

### 12.6 Alerts (`alerts.py`)
- **Telegram** uses `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
- **healthchecks.io dead-man.** `HEALTHCHECK_URL` is pinged:
  - at READY;
  - every 60 s during 08:45–15:45, only while the core heartbeat is less than 10 s old;
  - at `/fail` on a CRITICAL exit.

  The check's schedule is `45-59 8 * * 1-5` / `* 9-15 * * 1-5` with a 3-min grace.
- **Delivery rules:**
  - dedup by key for 5 min;
  - CRITICAL repeats every 5 min until `ACK_ALERT`;
  - WARN at most once per key per 30 min;
  - the queue is bounded and never blocks the core.

| Severity | Events |
|---|---|
| CRITICAL | unit failed / restart limit hit; TOKEN with positions open; MISMATCH, EXTERNAL_CLOSE (manual), UNKNOWN_POSITION, SHORT_DETECTED; EXIT_DIFFICULTY; not flat at 15:22 or STOP_SENDING; BROKER_SQUAREOFF; ORDER_STUCK; DAILY_STOP on LIVE; KILL executed; EOD_TRUTH_MISMATCH; PAPER_MODEL_OPTIMISTIC; live demotion; self-test failure; INVARIANT/EXCEPTION halts |
| WARN | entry or exit reject; NO_BID; CHARGES_DRIFT; EXIT_UNCAPPED; feed reconnects; STALE_RECON; ban list unavailable; self-check WARNs; daily stop on paper |
| INFO | morning check; trade opened/closed (net); daily report; G1 reports |


## 13. Ops

### 13.1 Server layout (the systemd WorkingDirectory stays `/home/ubuntu/titan/agentic_trader`)
```
/home/ubuntu                               chmod 0751 (user titan may traverse; nothing becomes readable)
/home/ubuntu/titan/                        ubuntu:ubuntu 0755
  agentic_trader/                          WorkingDirectory — contains ONLY the symlink below (v1 tree moved aside, §13.11)
    titan2 -> ../current/titan2            static, created once by bootstrap
  current -> releases/<sha>                the single atomic switch (ln -sfn current.new + mv -T)
  releases/<sha>/                          immutable: titan2/ ops/ requirements.lock MANIFEST.sha256 VERSION
    venv -> ../../venvs/<lockhash>         one venv per requirements.lock hash
  venvs/<lockhash>/  incoming/
  v1_frozen_YYYYMMDD/                      the v1 tree (read-only archive); /home/ubuntu/titan/{venv,logs} left untouched
/etc/titan2/titan2.env                     root:titan 0640       /etc/titan2/dash.env   root:titandash 0640
/var/lib/titan2/                           titan:titandash 0710  (dashboard may traverse to pub/ and dash/ only)
  db/        titan:titan 0700            titan2.db(-wal,-shm), paper_exchange.db, commands_cli.db
  dash/      titandash:titan 2750        commands_dash.db (dashboard writes; bot reads via group)
  pub/       titan:titandash 2750        *.json 0640 (bot writes; dashboard reads)
  session/   titan:titan 0700            kite_session.json 0600
  config/    titan:titan 0750            settings.json, ban_override/
  cache/ run/ reports/ exports/ backups/ (premigrate copies)
/var/log/titan2/   titan:titan 0750        /var/backups/titan2/   titan:titan 0700
```
- **How Python finds the code.** `python -m titan2` resolves `titan2` from the cwd, through the symlink, into the current release.
- **When a switch takes effect.** A running process keeps its loaded modules; a release switch takes effect only on restart.
- **Bytecode.** Bytecode is precompiled at deploy, and the bot runs with `PYTHONDONTWRITEBYTECODE=1`.

### 13.2 CLI and entrypoints
`/usr/local/bin/titan2` (installed from `ops/titan2-cli`) runs:
```
cd /home/ubuntu/titan/agentic_trader && exec sudo -u titan /home/ubuntu/titan/current/venv/bin/python -m titan2 "$@"
```
A sudoers allow-list permits only this wrapper.

| Subcommand | Purpose |
|---|---|
| `bot [--safe]` | the bot (systemd only). Setting `TITAN2_SAFE=1` in the environment equals `--safe` and raises `SAFE_FLAG` |
| `token [--request-token RT]` | refresh the session (auto TOTP, or manual exchange) |
| `selfcheck --phase morning\|prestart\|deploy\|cutover [--db-copy PATH]` | §13.7 |
| `status [--json]` | reads `pub/status.json` + `titan2.db` (read-only) |
| `pause`, `resume`, `flatten-all --confirm FLATTEN`, `kill --confirm KILL` | operator commands (§8.9) |
| `ack-halt <id> --note TEXT`, `ack-alert <key>`, `ack-foreign SYMBOL PRODUCT QTY` | acknowledgements |
| `mode <strategy> OFF\|SHADOW\|PAPER`, `live <strategy> --confirm <hash> --i-accept-live-risk` | modes (§10.5, §10.8) |
| `recon` | RUN_RECON |
| `settings show\|diff\|set KEY VALUE` | settings (§8.4) |
| `trades [--date D] [--book B]`, `report [--date D]`, `g1 [strategy]` | read-only reports |
| `is-trading-day`, `backup [--to DIR]`, `selftest`, `alert --critical TEXT` | scripts, backup, deploy check, OnFailure |

- **Mutating verbs** insert into `commands_cli.db` and wait up to 30 s for the ack, then print it. If the bot is down:
  - PAUSE, FLATTEN_ALL and KILL stay pending and run at the next start;
  - every other command expires after 300 s.
- **Exit codes:**
  - CLI: 0 = ok; 2 = refused or failed.
  - Bot: 0 = done; 3 = exception storm; 73 = lock held; 75 = controlled restart (token/WS); 78 = fatal config/DB.

### 13.3 systemd units (`ops/systemd/`)
```ini
# titan2-bot.service — NO ExecStartPre: a restart with open positions is never blocked (self-check runs in-process)
[Unit]
Description=Titan v2 trading core (sole order owner)
Wants=network-online.target
After=network-online.target time-sync.target
OnFailure=titan2-alert@%n.service
StartLimitIntervalSec=900
StartLimitBurst=5
[Service]
Type=notify
NotifyAccess=main
User=titan
Group=titan
UMask=0027
WorkingDirectory=/home/ubuntu/titan/agentic_trader
EnvironmentFile=/etc/titan2/titan2.env
Environment=PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Kolkata TITAN2_VAR=/var/lib/titan2
ExecStart=/home/ubuntu/titan/current/venv/bin/python -m titan2 bot
Restart=on-failure
RestartSec=5
RestartPreventExitStatus=73 78
TimeoutStartSec=300
TimeoutStopSec=45
WatchdogSec=30
MemoryMax=1G
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
ReadWritePaths=/var/lib/titan2 /var/log/titan2
[Install]
WantedBy=multi-user.target
```
- The service is **enabled**, so any boot starts it; outside sessions it exits 0.
- `titan2-bot.timer` (Mon..Fri 08:45 Asia/Kolkata, `Persistent=true`) starts the daily run.
- `ProtectHome=read-only` (not `true`) is required because the WorkingDirectory is under `/home`.

| Unit | Type / user | Schedule | Command |
|---|---|---|---|
| `titan2-token.service/.timer` | oneshot, titan | Mon..Fri 08:30, Persistent | `python -m titan2 token` |
| `titan2-selfcheck.service/.timer` | oneshot, titan | Mon..Fri 08:40 | `python -m titan2 selfcheck --phase morning` (Telegram summary) |
| `titan2-dash.service` | simple, titandash, `Restart=always` | — | `/home/ubuntu/titan/current/venv/bin/gunicorn -w 1 --threads 4 -k gthread -b 127.0.0.1:8050 titan2.dash.app:app`; same WorkingDirectory; `EnvironmentFile=/etc/titan2/dash.env`; `ProtectSystem=strict`; `ReadWritePaths=/var/lib/titan2/dash` |
| `titan2-backup.service/.timer` | oneshot, titan | Mon..Fri 16:10, Sat 03:00 | `ops/backup.sh` |
| `titan2-alert@.service` | oneshot, titan | OnFailure of any titan2 unit | `python -m titan2 alert --critical "unit %i failed"` (Telegram + healthchecks `/fail`) |

- `logrotate.titan2`: `/var/log/titan2/*.log { daily rotate 60 compress delaycompress missingok notifempty create 0640 titan titan }`.
- journald: `SystemMaxUse=200M`.

### 13.4 Env and settings files
- **`/etc/titan2/titan2.env`** is written by hand from `ops/titan2.env.example`. It is never in git and never touched by deploy. Keys:
  - `ZERODHA_API_KEY`, `ZERODHA_API_SECRET`, `ZERODHA_USER_ID`, `ZERODHA_PASSWORD`, `ZERODHA_TOTP_SECRET`
  - `TOKEN_MODE=auto|manual`, `ALLOW_LIVE=0`, `REGISTERED_EGRESS_IP=`
  - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `HEALTHCHECK_URL`
  - optional `BAN_LIST_URL`, `BACKUP_S3_URI`
- **What is deliberately absent from the env:** the access token (it lives in the session file) and any `TRADING_MODE` (modes are per strategy in the DB; process-level live permission is `ALLOW_LIVE`).
- **`/etc/titan2/dash.env`** holds `DASH_USER`, `DASH_PASS_SCRYPT` and `DASH_SECRET`. It must not contain any `ZERODHA_*` key (self-check).
- **Settings** live in `/var/lib/titan2/config/settings.json`, edited only through `titan2 settings set`.

### 13.5 Token flow
- **`titan2-token`, 08:30, `TOKEN_MODE=auto`:**
  1. If the session file is valid (issued today after 06:00 IST and `profile()` succeeds), exit 0.
  2. Otherwise run the headless login: TOTP, login, twofa, request_token, `generate_session`. This is ported from kite_token_manager 137-307. Up to 3 attempts, 20 s apart.
  3. Atomically write `session/kite_session.json` (`{access_token, user_id, issued_at, valid_until: next 06:00 IST}`, mode 0600) and verify `profile()`.
  - It never edits an env file and never restarts a service (kite_token_manager 312-330 is not ported).
- **`TOKEN_MODE=manual`:** at 08:30, Telegram sends the Kite login URL. The user runs `titan2 token --request-token <rt>`, and the exchange happens on the server.
- **Only the token unit creates sessions.** The bot and the dashboard never log in. v1's dashboard re-login (dashboard 40-86) invalidated the bot's token.
- **On failure:** CRITICAL alert. The bot runs with `HALT(TOKEN)` and re-reads the file every 30 s (§9.7).

### 13.6 Deploy (`ops/deploy.sh <host> [--allow-market-hours] [--restart]`, run from the operator's machine)
1. **Local checks and packaging.**
   - The git tree under `agentic_trader/{titan2,ops}` and `requirements.lock` must be clean, and `pytest -q` must pass.
   - `REL=$(git rev-parse --short=12 HEAD)`.
   - `git archive` those paths into `release-$REL.tgz`, together with `MANIFEST.sha256` (every file) and `VERSION`.
2. **Remote preflight** (ssh as ubuntu, no sudo).
   - (a) Run `titan2 status --json`. On a trading day between 08:40 and 15:45, with the bot active and any open legs or non-terminal orders, **ABORT** unless `--allow-market-hours` is given *and* there are zero open legs and zero non-terminal orders.
   - (b) **Drift check:** `cd current && sha256sum -c --quiet MANIFEST.sha256`, plus no files beyond the manifest. On failure, **ABORT** and list the drifted files. `ops/pull_drift.sh` copies them to local `./drift/<sha>/` and never overwrites.
   - (c) At least 1 GB of disk free.
3. **Upload.**
   - `scp` to `incoming/` and verify the sha256.
   - Unpack to `releases/$REL.tmp`, then `mv` to `releases/$REL`.
   - If that release already exists: an identical MANIFEST means reuse it; a different one means **ABORT**.
4. **venv.**
   - `H = sha256(requirements.lock)[:16]`.
   - If `venvs/$H` does not exist, create it with `python3 -m venv` and `pip install --require-hashes -r requirements.lock`.
   - Link `releases/$REL/venv` to `../../venvs/$H`.
5. **Freeze.** Run `python -m compileall -q titan2`, then `chmod -R a-w releases/$REL`.
6. **Verify.** Run `sudo -u titan releases/$REL/venv/bin/python -m titan2 selfcheck --phase deploy`. It checks imports and settings against the new LIMITS, and dry-runs the migration on a DB copy made by `titan2 backup --to /var/lib/titan2/backups/deploy-$REL`.
7. **Switch atomically.** `ln -sfn releases/$REL current.new && mv -T current.new current`.
8. **Units.** Changed unit files only: `sudo install -m 0644` into `/etc/systemd/system/titan2-*`, then `daemon-reload` (sudoers allow-list).
9. **Restart.**
   - Always: `sudo systemctl restart titan2-dash`.
   - Bot: `restart titan2-bot` only with `--restart`, or when the bot is not running. Then wait up to 120 s for READY and a heartbeat under 10 s old.
10. **If step 9 fails:** flip `current` back to the previous release, restart, send a CRITICAL alert and exit 1.
11. **Prune.** Keep the 5 newest releases; `current` and the previous release are never pruned. Remove venvs that no kept release references.

**Guarantees:**
- `assert_owned()` (a realpath prefix check) allows writes only to:
  - `/home/ubuntu/titan/{incoming,releases,venvs}`;
  - the `current` and `current.new` symlinks;
  - `/etc/systemd/system/titan2-*`, through sudo.
- There is no `rsync --delete`. `.env` is never copied.
- The script never touches `/var/lib/titan2`, `/etc/titan2`, `v1_frozen_*` or `agentic_trader/`.
- OS permissions back this up: `ubuntu` cannot write `/var/lib/titan2` or `/etc/titan2`.

**Related scripts:**
- `ops/rollback.sh` flips `current` to the previous release. It follows the same market-hours rule.
- CI (`.github/workflows/ci.yml`) runs pytest on ubuntu-latest with Python 3.10 and 3.12, plus windows-latest with 3.12 for unit and arch tests. It has **no deploy job and no secrets**.

### 13.7 Self-check phases (`selfcheck.py`)
| Check | morning | prestart (in-bot) | deploy | cutover | Effect of a prestart FAIL |
|---|---|---|---|---|---|
| token valid (`profile()`), user_id matches | ✓ | ✓ | – | ✓ | HALT(TOKEN) |
| NTP synced (`timedatectl show -p NTPSynchronized --value` = yes) | ✓ | ✓ | – | ✓ | HALT(CLOCK_UNSYNCED) |
| calendar covers today and the next 30 days | ✓ | ✓ | ✓ | ✓ | WARN (beyond coverage = no trading) |
| today's instruments dump is sane | ✓ | ✓ | – | ✓ | HALT(NO_INSTRUMENTS) |
| today's ban list loaded | ✓ | ✓ | – | – | stocks ineligible (G08) |
| previous-session bars for ≥90% of the universe | ✓ | ✓ | – | – | WARN (missing symbols ineligible) |
| settings within LIMITS | ✓ | ✓ | ✓ | ✓ | HALT(SETTINGS_INVALID) |
| DB `integrity_check`; schema version | ✓ | ✓ | ✓ (copy) | ✓ | exit 78 |
| disk ≥2 GB; memory ≥300 MB | ✓ | ✓ | ✓ | ✓ | WARN |
| v1 units masked and inactive; no crontab line mentioning `agentic_trader` | ✓ | ✓ | ✓ | ✓ | HALT(V1_ACTIVE, GLOBAL, AUTO) + CRITICAL |
| cwd clean (no `*.py` in `agentic_trader/`); KiteConnect unpatched in a fresh interpreter | – | ✓ | ✓ | ✓ | exit 78 |
| `dash.env` has no `ZERODHA_*`; env file permissions correct | ✓ | ✓ | ✓ | ✓ | CRITICAL |
| broker: no non-bot NFO positions; no open orders | ✓ | (recon) | – | ✓ | HALT(UNKNOWN_POSITION) |
| egress IP == REGISTERED_EGRESS_IP (if set) | ✓ | ✓ | – | ✓ | LIVE_DISABLED_TODAY |
| LIVE: margins ≥ LIVE_MIN_MARGIN_RS; LIVE strategies hold CERTIFIED trials | ✓ | ✓ | – | – | LIVE_DISABLED_TODAY / demote |
| newest RateSet younger than 180 days | ✓ | – | ✓ | – | WARN |
| previous session closed clean (flat, EOD done) | ✓ | ✓ | – | – | CRITICAL |
| Telegram test and healthchecks ping | ✓ | – | – | ✓ | WARN |


### 13.8 Backups
`titan2-backup` (`ops/backup.sh`, calling `titan2 backup`) does the following:
1. SQLite `.backup` of `titan2.db`, `paper_exchange.db`, `commands_cli.db` and `commands_dash.db` into `/var/backups/titan2/YYYY-MM-DD/`.
2. `integrity_check` on the copies, then gzip.
3. Keep 30 daily and 12 monthly backups.
4. Copy `settings.json`. Secrets are never copied.
5. Optionally `aws s3 cp` when `BACKUP_S3_URI` is set.

This fixes daily_maintenance.sh 37-47, which copied a plaintext `.env` and never backed up the DB.

### 13.9 Monitoring
- the `alerts` thread (§12.6);
- the healthchecks dead-man;
- `OnFailure` alert units;
- optional EC2 auto-recovery (CloudWatch `StatusCheckFailed_System` → recover).

### 13.10 Daily routine
| IST | System | User |
|---|---|---|
| 08:30 | token refresh | nothing (manual mode: one login) |
| 08:40 | morning self-check → Telegram | read PASS/FAIL |
| 08:45 | bot starts, recovers, preps, READY | nothing |
| 09:15:30 | live self-test (LIVE-enabled strategies only) | nothing |
| 09:20–14:55 | entries (PAPER) | nothing; the dashboard over the SSH tunnel is optional |
| 15:15–15:25 | flatten, backstop, stop-sending | nothing (CRITICAL alert if not flat) |
| 15:40 | daily report → Telegram | read (2 min) |
| 16:10 | backup | nothing |
| weekly | G1 progress (`titan2 report --date`, `titan2 g1`) | 10 min |
| yearly | holiday calendar and RateSet review | 10 min |

### 13.11 Cutover stages (each is reversible)

**Stage 0 — secure and freeze v1** (outside market hours; no v2 yet)
1. **Before anything is pushed to `main`:**
   - `gh workflow disable "Deploy Titan Bot"`;
   - delete the `EC2_SSH_KEY` repo secret;
   - merge the commit that deletes `.github/workflows/deploy.yml` and adds `ci.yml`.

   Verified: `deploy.yml` restarts `titan-bot` on every push to `main` touching `agentic_trader/**`.
2. **Start EC2 and stop v1:**
   - `sudo systemctl disable --now titan-bot titan-dashboard && sudo systemctl mask titan-bot titan-dashboard`. Masking matters: kite_token_manager 312-330 runs `systemctl restart`, which would start a merely stopped unit.
   - `crontab -l > ~/v1-crontab.bak && crontab -r`. This removes the 08:30 token job that restarted the bot, and the Dhan, OI-backfill, maintenance and log-cleanup jobs.
   - Remove the nginx titan site, and remove the security-group inbound rules for 80 and 5000.
3. **Archive the v1 tree:**
   - `sqlite3 …/titan_state.db ".backup ~/titan-v1-state.db"`;
   - `mv /home/ubuntu/titan/agentic_trader /home/ubuntu/titan/v1_frozen_$(date +%Y%m%d)`;
   - `chmod 600` its `.env`;
   - copy the DB and a tarball to the laptop.
4. **Rotate secrets.** `.kite_access_token` and `dhan_config.json` were committed and pushed. Regenerate the Kite API secret, change the Zerodha password, re-enrol TOTP, and revoke the Dhan token. Purging git history afterwards is optional and needs a force-push (user's call).
5. **Elastic IP.** Allocate and associate an Elastic IP, update `~/.ssh/config` (`titan-bot`), and register the IP in the Kite developer console (static-IP rule **[V]**).

`ops/stage0_cutover.sh` automates steps 2–3. It is dry-run by default; `--execute` performs the changes.

**Stage 1 — install v2 (no trading)**
1. Run `ops/bootstrap.sh`. It is idempotent and prints a diff before making changes. It:
   - creates users `titan` and `titandash`;
   - creates the directories and permissions in §13.1;
   - ensures Python ≥3.10 (deadsnakes if needed **[V]**);
   - sets up chrony, logrotate, the journald cap, the sudoers allow-list and the timezone;
   - runs `chmod 0751 /home/ubuntu` and creates the `agentic_trader/titan2` symlink.
2. Write `/etc/titan2/*.env` by hand.
3. Run `deploy.sh` for the first release.
4. Run `titan2 token`.
5. Run `titan2 selfcheck --phase cutover`. It requires the broker to be flat with no orders, a valid token and a valid dump.
6. Enable the units and timers.

**Stage 2 — PAPER burn-in (≥10 sessions)**
- Exit criteria:
  - zero CRITICAL alerts caused by software faults;
  - zero paper reconciliation mismatches;
  - exit-decision → order-dispatch p95 < 1.5 s;
  - daily reports produced;
  - the §17.2 [V] checklist done;
  - three drills done:
    - `kill -9` mid-session with open paper positions (READY < 30 s, exits resume);
    - deploy and rollback after the close;
    - token failure.
- Any fix bumps the version, so G1 restarts. That is intended.

**Stage 3 — G1 accumulation**
- At least 34 sessions at ≤3 entries/day/book (the first look needs 100 trades), so expect 2–5 months.
- Weekly review. No parameter change without a version bump.

**Stage 4 — LIVE, 1 lot**
- Only for a CERTIFIED strategy, with both keys, the daily self-test, the first-week cap and probation.
- Check the first 10 live trades by hand against Zerodha contract notes.
- Rollback: `titan2 mode <s> PAPER`, or `titan2 kill --confirm KILL`.

**Stage 5 — multi-leg**
- Only if the research verdict says it is viable (§14). It gets its own paper period and its own G1.

### 13.12 Playbooks (`docs/RUNBOOK.md`)
1. **Bot down with positions open** (OnFailure or dead-man alert):
   1. `ssh titan-bot`, `titan2 status`, `journalctl -u titan2-bot -n 200`.
   2. `sudo systemctl restart titan2-bot`. Recovery resumes exits.
   3. If it will not start, flatten in the Kite app. The MIS auto square-off at 15:26 is the last resort.

   There is deliberately no second order-placing tool (R1).
2. **MISMATCH or EXTERNAL_CLOSE:** check the Kite order book, then `titan2 ack-halt <id> --note "…"`.
3. **FOREIGN position (a manual trade):** close it, or `titan2 ack-foreign SYMBOL PRODUCT QTY`.
4. **SHORT_DETECTED not explained by the bot:** buy it back in Kite after checking, then ack.
5. **Token failure:** `titan2 token`, or `titan2 token --request-token <rt>`.
6. **Feed dead:** exits switch to REST quotes automatically. If it lasts more than 5 minutes, consider `titan2 flatten-all --confirm FLATTEN`.
7. **Not flat at 15:22:** check for NO_BID and act in Kite. The broker square-off at 15:26 is the backstop.
8. **Bot misbehaving but positions need managing:** `sudo systemctl set-environment TITAN2_SAFE=1 && sudo systemctl restart titan2-bot`. This runs exits only. To undo: `sudo systemctl unset-environment TITAN2_SAFE`, restart, then `titan2 ack-halt <SAFE_FLAG id>`.
9. **Deploy failed:** rollback is automatic; otherwise `ops/rollback.sh`.
10. **DB corruption:** stop the bot, restore the latest backup set (`titan2.db` and `paper_exchange.db` together), then start. Recovery rebuilds from the broker.

## 14. Multi-leg defined-risk positions — PHASE 2 (designed now; built only if the research verdict says viable)

### 14.1 Extension points already present in phase 1
- **Code location.** Phase-2 code lives only in new files:
  - `titan2/multileg/` (`__init__.py`, `handler.py`, `structures.py`, `exits.py`, `margin.py`, optional `greeks.py`);
  - `titan2/migrations/0002_multileg.sql`;
  - `tests/multileg/`.
- **`plugins.load_handlers()`** returns `{LONG_CE: SingleLegHandler, LONG_PE: SingleLegHandler}`. It then calls `titan2.multileg.register(handlers, deps)` if `importlib.util.find_spec("titan2.multileg")` finds the package.
- **Shared protocol.** `core/single_leg.py` defines the `StructureHandler` protocol. The gateway, the engine's exit evaluation and the exit loop call only this protocol:
```python
class StructureHandler(Protocol):
    types: frozenset[StructureType]
    def select(self, intent: Intent, ctx: HandlerCtx) -> list[Instrument] | Refusal: ...       # legs (phase 1: one)
    def quote_symbols(self, intent: Intent, legs: list[Instrument], placebo: bool) -> tuple[str, ...]: ...
    def plan_entry(self, intent, legs, quotes: Mapping[str, Quote], ctx) -> EntryPlan | Refusal: ...  # liquidity, size, limits, leg order
    def next_entry_orders(self, st: Structure, ctx) -> list[OrderSpec]: ...                    # phase 1: the single ENTRY
    def on_entry_progress(self, st: Structure, ctx) -> str: ...                                # "OPEN"|"VOID"|"UNWIND"|"WAIT"
    def exit_params(self, st: Structure, ctx) -> ExitParams: ...
    def evaluate(self, st: Structure, quotes, now_mono, now_wall, forced: str | None, ctx) -> Decision: ...  # phase 1 -> exits.decide
    def exit_sequence(self, st: Structure) -> list[int]: ...                                   # leg_ids; phase 2: shorts first
    def placebo_intent(self, intent: Intent, ctx) -> Intent | None: ...                        # phase 1: opposite direction
```
- **`EntryPlan`** = `(legs: list[LegPlan(inst, side, qty, limit_px, role)], risk_basis_rs, decision: Mapping)`.
- **`OrderSpec`** = `(leg_id, role, side, qty, limit_px, band_idx)`.
- **Other hooks:**
  - The `PaperExchange` margin check goes through a `MarginModel` protocol; phase 1 uses `PremiumMargin`.
  - Recon is already signed per leg.
  - G1 is already generic over `risk_basis_rs` and parent-linked placebos.
- **Migration `0002_multileg.sql`** rebuilds the tables to widen these CHECKs:
  - `structures.type`;
  - `legs.side IN ('LONG','SHORT')`;
  - `orders` role/side (ENTRY SELL for shorts, EXIT BUY to buy back, UNWIND);
  - `intents` direction/structure.

### 14.2 Structures and the defined-risk invariant
- **Types:**
  - CREDIT_PUT: sell put K1, buy put K2 < K1.
  - CREDIT_CALL: sell call K1, buy call K2 > K1.
  - IRON_CONDOR: both of the above.
  - DEBIT_CALL / DEBIT_PUT: buy K1, sell K2 further OTM.
- **Underlyings:** index first (NIFTY/BANKNIFTY are cash-settled). No stock structures.
- **Invariant**, checked after every fill (a violation triggers UNWIND): for each side, `short_filled ≤ filled qty of its hedging wing`, and the wing strike is further OTM than the short.

### 14.3 Entry protocol (`multileg/structures.py`, `BasketCoordinator`)
1. **Select legs** by listed-strike distance. Use delta only if the research requires it (`greeks.py`, a port of options_trader 3023-3161 with `math.erf`).
2. **Check liquidity per leg:** both sides of the book, DEPTH_MULT × leg qty, and the spread cap.
3. **Size by maximum loss.**
   - `W` = width.
   - Credit `Cr = Σ bid-walk(shorts) − Σ ask-walk(wings)`. Debit `Dr = Σ ask-walk(longs) − Σ bid-walk(shorts)`.
   - `max_loss_per_unit` is `W − Cr` for credit spreads, `Dr` for debit spreads, and `max(W_put, W_call) − Cr` for iron condors.
   - `lots = min(MAX_LOTS, floor(MAX_STRUCT_LOSS_RS/(lot·max_loss_per_unit + charges_per_lot)), per-leg depth terms, day budget)`.
   - Refuse if `Cr ≤ MIN_CREDIT_FRAC·W` or `lots < 1`.
   - `risk_basis_rs = lots·lot·max_loss_per_unit`.
4. **Margin pre-check.** `kite.basket_order_margins(orders, consider_positions=True, mode="compact")` must report required margin ≤ 0.8 × available. An API failure refuses the entry. Paper calls the same read-only API **[V]**.
5. **Leg ordering.**
   - BUY the wings first (IOC, `min(ask+2 ticks, mid×1.01)`).
   - Once the wings are terminal, SELL the shorts (IOC, bid − 2 ticks) for `min(target, filled hedging-wing qty)`. The gateway asserts that a short qty never exceeds its hedging wing's fill.
6. **Leg-risk timeout.**
   - Each short gets `LEG_TIMEOUT_S = 5` and at most 3 attempts.
   - Unmatched wing qty is UNWOUND: SELL through the exit loop with reason `LEG_UNWIND`.
   - The structure becomes OPEN with the matched qty, or VOID. Costs are booked either way.

### 14.4 Exits
- **Order of closing.** Buy back the shorts first (mirrored BUY ladder). Sell the wings only after broker qty confirms the shorts are flat.
- **Valuation.** `DTC = Σ ask-walk(shorts) − Σ bid-walk(wings)`; `pnl = Cr − DTC − charges`.
- **Rules:**
  - hard stop at `SL_FRAC·max_loss`;
  - take profit at `TP_FRAC·Cr`;
  - time stop and EOD;
  - one stale leg → protective exit of the whole structure.
- Parameters come from the research **[U]**.

### 14.5 Expiry-day rules
- No new index structure with DTE < 2.
- Force-close by `EXPIRY_EXIT_AT = 14:30` on the session before expiry.
- No short option is ever held into expiry day.

### 14.6 Recon
- Expected qty is per leg and signed: short legs are expected negative.
- SHORT_DETECTED now means a short that no open phase-2 short leg explains.
- Any leg mismatch → HALT, then UNWIND the structure: shorts first, capped at broker qty.

### 14.7 Product
- MIS for intraday structures, which also gets the hedge margin benefit.
- NRML only if the research picks multi-day holds. That needs a separate review: EOD flatten disabled for that strategy, overnight caps, and recovery across sessions.

### 14.8 Phase-2 paper model and G1
- **Paper model:**
  - `MarginModel` = the `basket_order_margins` snapshot at entry;
  - simulated auto square-off of shorts at 15:26.
- **G1:**
  - returns are normalized by the max-loss `risk_basis_rs`;
  - for iron condors the placebo is the same structure entered at a seeded random time inside the strategy window on the same session (there is no "opposite" condor);
  - for verticals the placebo is the opposite spread (bull put ↔ bear call);
  - the same family accounting as §10.7 applies.

### 14.9 Phase-2 tests (`tests/multileg/`)
- `test_wings_before_shorts`
- `test_short_qty_never_exceeds_hedge_property`
- `test_leg_timeout_unwinds_unmatched_wing`
- `test_partial_wing_sizes_shorts`
- `test_basket_margin_failclosed`
- `test_close_shorts_first_then_wings`
- `test_no_short_into_expiry_day`
- `test_max_loss_sizing_credit_debit_condor`
- `test_struct_value_uses_ask_for_shorts_bid_for_longs`
- `test_recon_signed_short_legs`
- `test_time_placebo_seeded`
- `test_migration_0002_preserves_rows`

## 15. Deletion plan (R13)

**Principles**
- **Whole files are retired.** Under option B, whole v1 files are retired once their KEEP regions have been ported (§2). The brief's region-level deletes are superseded.
- **Nothing is lost.** Baseline `b4694f3`, tagged `v1-final`, keeps everything recoverable.
- **Only git-tracked paths are deleted, with `git rm`.** Untracked and ignored files are never touched: the local `.env`, `auto_pilot.py.bak_pre_simplify`, `gmm_calibration_data.jsonl`, any local `titan_state.db`, `.pytest_cache`.
- **`_*.py` files are untouched.** That covers the 202 tracked under `agentic_trader/` and the root ones (user rule).
- **Research data stays.**

**Delete groups.** The exact list is in `deletion_list`.
- A. v1 runtime replaced by `titan2`.
- B. The brief's DELETE modules.
- C. Corrections to the brief: `commodities_trader.py` (MCX), `oi_watcher_engine_ec2.py`.
- D. The 47 non-underscore research/diagnostic scripts (9,513 lines).
- E. `ml_models/` code, saved models, reports and `direction/`. Keep `ml_models/data/` and `ml_models/oi_snapshots/`.
- F. UI, ops scripts and the v1 deploy kit: `templates/`, `deploy/`, `cron_*.sh`, `daily_maintenance.sh`, `log_cleanup.sh`, `run_bot.bat`, `requirements.txt`.
- G. v1 runtime state and config files.
- H. Tracked secrets (rotate them first, Stage 0).
- I. Repo-root v1 deploy paths that can start or overwrite v1: `deploy/`, `titan-bot.service`, `docker-compose.yml`, `deploy_check.sh`, `.github/workflows/deploy.yml`, `.env.example`.

**Keep**
- `agentic_trader/_*.py`.
- `agentic_trader/trade_ledger/` (2 underscore scripts plus data), `daily_summaries/`, `all_trades_dump.json`, `sniper_trades_dump.json`, `speed_gate_log.jsonl`, `ml_models/data/`, `ml_models/oi_snapshots/`.
- `CHANGELOG.md` (appended).
- `TITAN_INSTRUCTIONS.md` (rewritten).
- `TECHNICAL_DOCUMENTATION.md` and `MISSED_TRADE_DIAGNOSTIC_PLAYBOOK.md`, moved to `agentic_trader/docs/v1/` by the docs unit.
- Root research files that are not production code (CSV, xlsx, `fix_aggr_*.py`, …) are out of scope.

**Corrections to the brief's KEEP list (each verified)**
| KEEP item | Problem | Replacement |
|---|---|---|
| `kite_global_throttle` | fails open (zerodha_tools 148-153) | in-process blocking buckets |
| `PriorityTriggerQueue` | — | per-intent TTL flow |
| BlackScholes | no phase-1 rule uses it | phase 2 only |
| `trade_ledger` / `state_db` persistence functions | encode defects: full-list rewrite (269-300), symbol-fallback exit match (884-888), CLOSED on the first exit (893) | new ledger and store |
| `settings_manager` hot reload | lets settings change mid-session | read once per process |
| `get_quote` | returns the stale cache on REST failure (547-551) | `QuoteCache.fresh()` returns None |
| `_check_hard_sl` | skips the stop when price ≤ 0 (448-450) | stale/no-bid rules in `decide()` |
| `OptionContract` cache | compares `.seconds` (3377) | per-quote `recv_mono` ages |
| `expiry_shield.get_dte` | — | trading-day DTE |
| `market_scanner` lot sizes | lot taken from the wrong row | lot from each contract's own row |

Other corrections:
- `ml_models/` is 10.0K lines, not the 8.4K the brief stated.
- 46 underscore scripts import deleted modules. Run them from `git worktree add ../titan_v1 v1-final`.

**Order of operations**
1. Build `titan2` and the tests on `profit-first`.
2. Get every test green.
3. `git tag v1-final b4694f3`.
4. Capture the golden fixtures from a worktree at `v1-final`.
5. Make one deletion commit, with the list in the message.
6. Stage 0 on EC2.
7. Stage 1.
8. PAPER.

## 16. Test plan (R14)

### 16.1 Harness (`tests/fakes/`, `tests/conftest.py`)
- **`FakeClock`** (from `titan2.clock`).
- **`ScriptedMarket`** feeds a real `QuoteCache`:
  - `depth(sym, bids=[(px, qty)…], asks=…, at=…)`;
  - `tick(underlying, ltp, cum_vol, at=…)`;
  - `freeze(sym)` (no WS updates);
  - `rest(sym, …)` / `rest_fail(sym)`;
  - `disconnect()` / `reconnect()`.
- **`FakeKite`** implements every KiteConnect method `broker/kite.py` uses: `place_order`, `cancel_order`, `order_history`, `orders`, `trades`, `order_trades`, `positions`, `margins`, `quote`, `ltp`, `instruments`, `historical_data`, `profile`, `get_virtual_contract_note`, `basket_order_margins`. It also emits scripted WS order updates.
  - By default it fills with `paper.fill_from_depth` on the script's depth at place time + latency.
  - **`FaultInjector` modes:**
    - **Rejects:** `reject(msg)`, `band_reject`.
    - **Submit ambiguity:** `timeout_after_accept` (the order exists, then `NetworkException` is raised), `timeout_before_accept`, `http_429`, `token_expired`.
    - **Order-update anomalies:** `duplicate_updates`, `out_of_order_updates`, `drop_ws_updates`, `ws_update_before_place_returns`, `cancel_race_already_complete`, `order_stuck_open`.
    - **Positions/orders API:** `positions_lag(s)` (returns pre-fill positions for s seconds after a fill), `positions_contradict`, `positions_error`, `orders_error`.
    - **Account events:** `external_close(sym, at, px)`, `external_buy(sym, qty)`, `broker_squareoff(at)`.
    - **Data:** `stale_depth`.
- **`ScenarioRunner`** wires real components: `Engine` + `Store(tmp)` + `PaperExchange` (inline `step`) + `KiteBroker(FakeKite, thread_check=False)` + inline lanes.
  - `run_until("09:21:00")` advances the `FakeClock` in 50 ms steps, delivering due events and Timers synchronously and deterministically.
  - Assertion helpers cover orders, legs, structures, trades, halts, alerts, events and dispatched commands.
- **Dual run** (proves R4).
  - Every test in `tests/scenarios/` is parametrized over `acct ∈ {PAPER, LIVE}`: `PaperExchange` versus `KiteBroker(FakeKite)`, the latter with an ALLOW_LIVE plus unlocked-strategy fixture.
  - It asserts identical engine command streams, modulo the account and tag letter, and identical net P&L.

### 16.2 Scenarios: one per brief §4.1 live killer (`tests/scenarios/test_killers.py`)
| Test | Proves |
|---|---|
| `test_k01_recon_never_exits_fresh_entry` | 120 s of recon cycles after a fill, with `positions_lag(4)`: no SELL, no halt, the leg stays OPEN |
| `test_k02_broker_flat_never_sells` | `external_close`, then an SL condition: zero SELL orders; CLOSED_EXTERNAL with the tradebook price; MISMATCH halt |
| `test_k03_failed_exit_keeps_position` | exit IOC rejected, times out, is rejected: the leg stays EXITING with no trade row; the final fill books P&L from fills |
| `test_k04_open_is_not_fill` | OPEN/0 for 6 s → cancel → CANCELLED 0 → VOID; a partial fill gives qty = filled |
| `test_k05_no_gtt_anywhere` | over a full day, the FakeKite GTT methods are never called |
| `test_k06_only_limit_ioc` | an hour of holding: only LIMIT/IOC/MIS orders; the DB CHECK rejects SL-M |
| `test_k07_caps_and_cheap_bomb` | MAX_LOTS=0 refuses; ₹3.5 × 4,300 is refused (premium floor); a 6,000-lot contract is refused (UNITS); `size` has no multiplier parameter |
| `test_k08_gates_fail_closed` | quote error, empty strategy name, unknown tick age, stale feed, or a gate exception → REFUSED with an event |
| `test_k09_signed_sustain_no_flip` | +0.7% then −0.4% through the baseline: no intent; a DOWN tape never yields a CE |
| `test_k10_bid_not_ltp_and_cost_lock` | LTP +6% with rb −1%: no TP/lock; the lock floor is BE + 3%; REST age = receipt time; time stop on the wall clock |
| `test_k11_single_order_path` | a dashboard FLATTEN goes command → engine → exits; plus the arch tests |
| `test_k12_sticky_halts_and_kill` | daily stop trips; after a restart the halt remains until the next session; unknown MTM blocks entries; FLATTEN_ALL ends verified flat; paper and live caps are equal |

### 16.3 Scenarios: May 13–14 postmortem and the memory incidents (`tests/scenarios/test_postmortem.py`)
| Test | Proves |
|---|---|
| `test_pm_a_oversize` | NMDC-like lot 6,750 @ ₹3.5 and lot 4,300 @ ₹25: at most 1 lot, or refused |
| `test_pm_b_no_reverse_churn` | −2.3% then SL on a wide spread (the day's second entry; the first closed over a minute earlier): only exit SELLs; an opposite intent hits COOLDOWN; a storm of 20 triggers gives ≤3 entries, the cap refusing every trigger after the third, with no RUNAWAY |
| `test_pm_b_storm_within_a_minute_runaway` | the same stop-out as the day's first entry, the storm seconds later: ≤3 entries (SBIN, RELIANCE, INFY); with three entry orders sent within 60 s G09 refuses the next trigger as RUNAWAY and raises HALT(RUNAWAY, BOOK, ACK), and G04 refuses every later one; never a PE on SBIN |
| `test_pm_c_pnl_from_fills_only` | LTP path ≠ fills: ledger = risk = status.json = `net_pnl(fills)`; AST test: no LTP read in P&L code |
| `test_pm_c_both_legs_impossible` | a CE leg EXITING plus a PE intent on the same underlying: refused by the gate and the DB index |
| `test_pm_phantom_sl_exit` | ZYDUSLIFE: the internal stop places a real SELL; the leg is not closed until broker net is 0 |
| `test_pm_d_stale_quote` | SONACOMS: WS frozen at 14.0, REST bid 11.5 → HARD_SL (debounced through a REST snapshot); REST down → protective exit by 5 s |
| `test_pm_partial_fills_walk` | HINDPETRO: the exit sweeps 4 bid levels over 3 IOC partials; P&L = Σ fills; brokerage = executed orders × ₹20 |
| `test_pm_spread_spiral_blocked` | SAMMAANCAP 8% spread → LIQUIDITY refusal |
| `test_pm_e_same_path` | the dual-run parity assertion itself |
| `test_pm_f_paper_fills_from_depth` | paper BUY = ask walk at 0.5 participation on the fill-time book; SELL = bid walk; never LTP |
| `test_pm_ledger_matches_broker` | EOD truth check within ₹1; injected drift raises the alert |

### 16.4 Restart, concurrency, lag (`tests/scenarios/test_restart.py`)
- `test_restart_submitting_not_sent_becomes_not_placed`
- `test_restart_after_send_before_ack_adopts_by_tag`
- `test_restart_mid_exit_partial_resumes`
- `test_restart_unknown_broker_position_halts_no_orders`
- `test_restart_db_lost_imports_bot_tags`
- `test_restart_after_1515_recovery_flatten`
- `test_restart_previous_session_live_unresolved`
- `test_paper_restart_settles_expired_and_missed_squareoff`
- `test_exception_reload_equals_db_state`
- `test_duplicate_and_out_of_order_updates`
- `test_ws_update_before_place_result`
- `test_positions_lag_never_external` (lag up to 8 s)
- `test_verify_flat_residual_never_sold`
- `test_ws_reconnect_backfills_orders`
- `test_token_rotation_controlled_restart`
- `test_mis_squareoff_handoff_1525_1526`
- `test_watchdog_only_with_core_progress`

### 16.5 Chaos invariants (`tests/chaos/test_invariants.py`)
Seeded random sequences combine FaultInjector modes, ScriptedMarket faults and restarts at random points: 200 seeds in CI, 500 nightly.

After each step, assert:
1. No broker net < 0 that isn't bot-explained and covered.
2. At quiescence, each leg's open_qty = Σ fills = broker net for bot symbols.
3. At most one non-terminal order per leg.
4. Every OPEN leg was evaluated within 2 s.
5. No ENTRY while an entry-blocking halt applies.
6. A CLOSED structure's net = `net_pnl(fills)`.
7. Only LIMIT/IOC/MIS orders were submitted.
8. No SELL qty exceeded open_qty.
9. At quiescence, the paper exchange ledger equals the engine ledger.

### 16.6 Architecture tests (`tests/arch/test_arch.py`)
- `test_no_v1_imports`
- `test_only_kite_adapter_calls_order_methods`
- `test_no_gtt_slm_market_identifiers`
- `test_no_multiplier_identifier`
- `test_dashboard_import_isolation`
- `test_strategies_import_purity`
- `test_core_has_no_paper_branches`
- `test_store_writers_core_only`
- `test_registry_lock_current`
- `test_semantic_constants_hash_current`
- `test_python310_syntax` (`ast.parse(..., feature_version=(3,10))`)
- `test_windows_importable` (every module except `runtime`/`feed` imports without twisted/fcntl)

### 16.7 Unit tests
Listed per unit in the work breakdown (`tests/unit/test_<module>.py`).

Golden tests (`tests/golden/`) use fixtures captured from v1 at `v1-final`. Ops tests (`tests/ops/`) run the deploy scripts against temp directories.

## 17. Risks, verification checklist, open questions

### 17.1 Risks and mitigations
| Risk | Mitigation |
|---|---|
| G1 is slow: ≥34 sessions per strategy (100 trades at 3 a day) | Per-strategy paper books; futility retirement at about 50 trades; SHADOW for cheap early evidence. G1 is never relaxed |
| Paper is still optimistic (queue position, hidden liquidity) | 0.5 participation on the fill-time book; 1.5× crossing-cost stress; live twin + X2 falsifier; probation |
| The liquidity gate blocks most stock-option entries at 09:20 | An honest result. Refusals are logged with spreads, so the cost of loosening is measurable. Bounds cannot be loosened without a code change |
| LPP/band rejects stall an exit | Re-price at the bid without deepening; EXIT_STUCK escalation; broker square-off as the last resort |
| Single host fails mid-session | systemd watchdog + restart in 5 s + recovery in <30 s; dead-man alert; MIS square-off; worst-case cap ₹50k |
| Kite API drift (fields, statuses, IOC behaviour, static IP) | Adapter normalizes in one file; FakeKite contract tests; daily live self-test; [V] checklist |
| SQLite fsync latency or corruption | WAL + `synchronous=FULL` (few writes/s); daily integrity checks; nightly verified backups; the broker is the source of truth for recovery |
| Manual trades in the same Zerodha account | They halt entries by design (FOREIGN). A dedicated account is better (Q3) |
| A big-bang rewrite adds new bugs | ≈150 tests, including dual-run, chaos and golden; ≥10-session burn-in with drills; LIVE only after G1 |

### 17.2 [V] verification checklist (implementers confirm during burn-in; record results in `docs/RUNBOOK.md`)
1. **Rates.** STT 0.15% on sell and on exercise; NSE transaction charge 0.03553%; the IPFT rate. Pin them from a real contract note (Q5) as `tests/unit/fixtures/contract_note.json`.
2. **MIS auto square-off.** Confirm 15:26, and how those orders appear in `orders()` (`tag`/`placed_by`). This feeds the BROKER_SQUAREOFF classifier.
3. **IOC on NFO.** Kite accepts IOC validity for NFO option LIMIT orders. The daily self-test proves this.
4. **Tags.** WS order updates and `orders()` carry `tag` for API orders.
5. **Contract note.** `get_virtual_contract_note` prices synthetic order params (needed for the paper VCN check).
6. **Positions lag.** Measure `positions()` lag after fills (p95). Any change to `POSITIONS_POST_FILL_LAG_S` needs an ENGINE_SEMANTICS_VERSION bump.
7. **Static IP.** Static-IP whitelisting and the egress-IP check.
8. **Exchange data.** The NSE ban-list URL and file format; the NSE 2026/2027 holiday circulars.
9. **Physical settlement.** Zerodha's settlement-window policy for stock options. It determines MIN_TD_TO_EXPIRY_STOCK.
10. **Opening candle.** Kite's 09:15 historical candle includes pre-open volume. The bar check confirms this.
11. **Margins field.** Whether `margins()` exposes available cash as `equity.available.live_balance` or `equity.net`.
12. **EC2.** The instance has Python ≥3.10, and the Ubuntu home-directory permissions are as assumed.
13. **Phase 2.** `basket_order_margins` can be used read-only.

### 17.3 Open questions for the user
See `open_questions` (Q1–Q5). Every default above is chosen so that the build can proceed without the answers.
