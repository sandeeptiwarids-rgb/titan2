# titan2 build report

2026-10-11 · branch `profit-first` · **nothing is committed**. Test, size and git numbers were measured today on this
laptop (Windows 11, Python 3.12.7, plus a Python 3.10.19 re-run). Review figures come from the round-1 records.

**Update, later on 2026-10-11 (the B1 batch).** The tree above is now commit `db5c5ba` (tag `v1-final` marks v1). You
decided B1 (strict, with a paper read delay) and B2 (keep 30 s); both are in RUNBOOK §10, Decisions. The fixes §6 step
2 asked for before paper data counts are done, uncommitted on top of `db5c5ba` for review as one diff: B1, F-18, F-19,
F-17, `base.py` in the version hash (§3G), F-7, F-15 and F-16, plus low-severity items 10–12. Each item below is
marked **Fixed (B1 batch)** where it applies; size and line counts were not re-measured. Tests after the batch: 1,026
pass, 0 fail, 2 skipped (unit + arch 663, scenarios 114, chaos + golden 221, ops 28); pyflakes is clean.

**Review of the B1 batch (same day, also uncommitted).** A review confirmed five gaps in the batch, now fixed: (1) a
retry after an unfilled IOC was capped by the read made for the first attempt, so a hand sale while the ladder ran
was sold again; every exit order now needs its own read, requested after the previous attempt ended. (2) Two failed
reads after the decision let an older read cap the exit at once; it now waits and re-asks for 10 s first, and that
fallback is a CRITICAL alert, `EXIT_CAP_BEFORE_DECISION`. (3) The strategy version did not hash the strategy module's
own code; it does now. (4) `EXIT_CAP_PRIOR_READ_S` sat in `recon`, outside `SEMANTIC_CONSTANTS`; it is in
`titan2/settings.py` and the semantic hash now. (5) No test checked that the placebos' SHADOW reads pay their own
read delay; one does. RUNBOOK P7 no longer advises selling by hand while the ladder runs. Both strategy versions
change; `REGISTRY.lock` was refreshed with every version kept at 1 (first release, no G1 data). Tests after the
review fixes: 1,030 pass, 0 fail, 2 skipped (4 new tests); pyflakes is clean.

## 0. The short version

| | |
|---|---|
| **Tests** | 1,005 tests. **1,003 pass, 0 fail, 2 skipped on purpose.** 10.3 min in 5 chunks. Under Python 3.10.19 all 977 non-ops tests: 975 pass, the same 2 skipped |
| **titan2 size** | 26,312 lines of Python in 45 files. 19,449 of them are code; the rest is 3,596 blank, 2,562 docstring and 918 comment lines. Plus 604 lines of SQL, HTML and JSON |
| **Against the target** | The brief's lean core was 4–6K new lines (plus about 5K reused from v1). The spec estimated 7.1K. **titan2 is 3.4× the spec estimate and 4.4–6.6× the brief's 4–6K** (3.2–4.9× on code lines alone; about 2.5× even if you add the 5K of reuse). §0.1 says where the bulk is and why |
| **Tests (code)** | 29,917 lines in 64 files, plus 3.5K lines of JSON fixtures. The spec estimated 3.5K |
| **Ops and docs** | 25 ops files, 1,860 lines: deploy, rollback, cutover and backup scripts, plus 10 systemd units and timers. `TITAN_INSTRUCTIONS.md` 839 lines, `docs/RUNBOOK.md` 581 |
| **v1 deleted** | 147 tracked files, −456,228 lines (working tree only, not committed): 108 Python files (88,081 lines), a 277K-line Dhan instrument CSV, 84K lines of JSON state dumps, HTML templates, the old deploy scripts and the CI deploy workflow |
| **v1 kept** | 245 v1-era Python files (34,107 lines: research scripts, ML model code, one-off tools), all untouched. The research data and model folders, and the v1 docs in `docs/v1/`. Every v1 file stays in git at `b4694f3` |
| **Review** | Round 1 of 3: 35 findings, 34 confirmed (1 critical, 7 high, 23 medium, 3 low). Now 26 fixed, 8 partly fixed, 0 not fixed (after the B1 batch). **Rounds 2–3, which re-review the round-1 fixes, are deferred to the paper period, before any LIVE decision** |
| **Still open** (the lists overlap) | After the B1 batch: 10 items the fix step left unresolved, 7 of the 10 items fixers marked "not fixed", 4 reviewer proof tests failing on real gaps, 9 low-severity findings (§3). Decisions B1 and B2 are made |
| **LIVE** | Impossible by construction until a strategy passes G1 and you turn two keys. The research found no strategy with an edge after costs, so the expected outcome is that titan2 never trades real money |

### 0.1 Where the size is, and why

| Area | Lines | Code lines | Spec estimate | × |
|---|---|---|---|---|
| Decision core (`core/`: engine, recon, risk, orders, positions, gateway, exit loop, exits, operator, state, events, invariants, sizing, single_leg) | 10,167 | 7,498 | 2,800 | 3.6 |
| Process, storage and ops glue (runtime, store, selfcheck, CLI, settings, alerts, token, clock, plugins) | 7,164 | 5,434 | 1,785 | 4.0 |
| Broker adapters and paper exchange (`broker/`) | 2,542 | 1,866 | 910 | 2.8 |
| Money, ledger and G1 statistics (charges, ledger, g1) | 2,140 | 1,582 | 610 | 3.5 |
| Model and instruments | 1,745 | 1,258 | 560 | 3.1 |
| Market data (`market/`) | 1,070 | 758 | 510 | 2.1 |
| Strategies (2) | 948 | 662 | 450 | 2.1 |
| Dashboard (`dash/`, plus 356 lines of HTML) | 536 | 391 | 200 | 2.7 |
| **Total** | **26,312** | **19,449** | **7,825** | **3.4** |

The spec's per-module estimates (DETAIL §2) add up to 7.8K; its headline figure is 7.1K. Every module is listed in
Appendix A. The largest are `core/engine.py` 2,630 lines (planned 340), `runtime.py` 1,604 (300), `store.py` 1,440
(360), `core/recon.py` 1,421 (300), `core/risk.py` 1,147 (300) and `core/orders.py` 1,125 (220).

Why it grew:
1. **Failure paths, not features.** The estimates sized the happy path: send, fill, exit. Most of the code handles
   what goes wrong between the bot and Zerodha. A place call times out: did Zerodha take the order? Updates arrive
   twice, out of order, or before the place answer. Positions lag the fills or contradict the order book. A read
   never comes back. The bot restarts mid-order, or the database is lost. Each case needs a way to spot it, a safe
   default (refuse entries, keep exits going), an alert and a test. Most of this is in `engine.py` and `recon.py`.
2. **Every input is checked, twice.** NaN, None, a wrong type, a stale or future timestamp: each refuses the entry,
   and the same bad input must never block an exit. So most paths are written twice.
3. **Operations that v1 left to you:** 18 morning self-checks, a 22-verb CLI, headless token login, alerts with a
   dead-man switch, backups and recovery-only restarts.
4. **G1 statistics** (bootstrap, multiple-testing correction, placebo, futility): 0.9K lines.
5. **Docstrings and comments:** 3.5K lines (13%).

titan2 was already about 25.4K lines before the review. Round 1 added roughly 1K lines of titan2 and 3.6K lines of
tests (106 tests). More code means more places for bugs. The weight sits on the order, position and recon path, the
part that lost money in v1, and about 1,000 tests cover that path. Still, this is not the small codebase you asked
for. If you trim later, these parts never touch an order: selfcheck (1.0K), the CLI (0.7K) and G1 reporting (0.9K).

## 1. What titan2 does, end to end

**One owner.** One process (`titan2-bot`, OS user `titan`) holds the only Kite session and places every order. Inside
it, one *core* thread decides everything, one event at a time, inside a database transaction, and orders go out only
after that transaction is saved. Network calls run on separate *lane* threads that report back as events. Think of a
single trader with one notebook: nothing happens unless it is written down first. The dashboard runs as another OS
user with no broker credentials. It reads published JSON and can only ask for PAUSE, RESUME or FLATTEN_ALL.

**A trading day (IST)**

| When | What happens |
|---|---|
| 08:30 | The token job refreshes the Kite session (headless TOTP login). Nothing else ever logs in |
| 08:40 | Morning self-check, 18 rows: token, clock sync, calendar, instruments, ban list, previous bars, settings, DB, disk and RAM, v1 masked, working folder clean, env files, broker flat, egress IP, LIVE readiness, charges age, last session closed clean, Telegram and dead-man. A failure blocks entries and alerts you; it never blocks exits |
| 08:45 | systemd starts the bot. Settings are read once; they can only be tightened. The bot reconciles with the broker and resumes any exits *before* it reports READY. It then loads today's instrument dump (lots, ticks, strikes, expiries: never hardcoded), the F&O ban list and yesterday's 5-minute bars |
| 09:15 | The Kite websocket starts: full depth for held options, quotes for underlyings. Option quotes stamped before 09:15:30 count as stale carry-over |
| 09:15:30 | LIVE order-path self-test, only when a strategy is LIVE: a 1-lot IOC priced below the bid. It must come back cancelled and unfilled, with its tag echoed by the websocket and by `orders()`. Retry at 09:17 |
| 09:20–14:55 | Strategies may ask for entries. Two exist, both PAPER. **opening_drive**: the 09:15 bar moves ≥ 2.5% up or down on ≥ 3× the average of the previous session's last 20 five-minute bars → an ITM-1 call or put at 09:20. **price_spike_up**: a signed up-move of 0.6–1.0% that holds for 12 s → an ATM call. A strategy can only emit an *intent*, a request valid for at most 10 s. It cannot place orders |
| Per intent | 22 gates in a fixed order, each failing closed: mode, halts, hours, feed health, recon clean, ban list, at most 2 entries a day per book, one position per underlying, cooldowns (30 min to re-enter, 10 min after a reject), quote ≤ 1.5 s old, spread ≤ 1.5%, depth ≥ 3× the order on **both** sides, charges ≤ 1% of premium, size, worst-case cap, margin, LIVE keys. Then exactly 1 lot, LIMIT + IOC (fill now at my price or cancel), priced at the lower of ask + 2 ticks and mid + 1%. The asks within that price must cover the whole lot |
| On the fill | Exit levels are frozen from the real fill price and the real charges. Hard stop −15%. The lock arms at breakeven + 4%, floors at breakeven + 3%, then trails, keeping 70% of the peak gain. Take profit +15%. Time stop 45 min. *Breakeven* includes every charge |
| While held | Every exit decision uses the **realizable bid**: what selling the whole quantity into the shown bids would fetch, never LTP. A quote older than 3 s forces a REST refresh; past 5 s without a fresh price the bot exits protectively. A stop needs two different quotes about 400 ms apart, unless the bid is far through it |
| Exiting | IOC limit at the bid, stepping down in bands (0.5%, 1%, 2%, 4%, 8%) to at most 12% under the bid. It never sells more than the broker shows held, using a positions read made after the exit decision, and for each retry one made after the previous attempt ended (decision B1, strict; paper pays the same read round trip). A position closes only when the broker shows it flat |
| Every 10 s | Reconciliation (*recon*): the bot's books against Zerodha's positions and orders. It is lag-safe: reads taken under 2 s after a fill don't count, there is a 5 s grace after fills, and two agreeing reads ≥ 3 s apart are needed. Any unexplained difference halts entries and alerts you. Recon never sends a sell |
| 15:15 / 15:22 / 15:25 | Flatten everything / switch to aggressive pricing / stop sending orders |
| 15:26 [V] | Zerodha's MIS auto square-off is the last resort (₹50 + GST per order) |
| 15:27–15:40 | Two final recons; the truth check (bot ledger against the broker tradebook or the paper ledger, within ₹1); the bar check (live bars against Kite's historical bars); reports; the nightly G1 run; exit |

**Money.** P&L comes only from fills plus exact Zerodha charges (the rates in force since 2026-04-01), through one
function, booked once. Limits:
- per trade: 1 lot and at most ₹25K of premium, sized so that the hard stop plus 5% slippage loses at most ₹5K. In
  practice a trade passes only with ₹6.2K–₹24.5K of premium; for NIFTY (lot 65) that is an option at about ₹96–₹376;
- per day: no new entries once realized + bid-based MTM reaches −₹10K (sticky until the next session); the worst case
  (all open premium lost, plus realized) is capped at ₹40K;
- an unknown MTM blocks entries and never blocks exits.

**Paper = live.** One engine; only the broker differs: `KiteBroker` for LIVE, `PaperExchange` for PAPER and SHADOW.
The paper exchange keeps its own database. It fills against the book it sees at fill time (only books received after
the order count), after a random 80 ms–3 s latency, and takes at most 50% of the depth it walks. It also simulates
the 15:26 square-off. Each PAPER trade gets a **placebo**: the opposite trade, booked in SHADOW (simulation only), to
check that any "edge" is not just the market drifting.

**The proof gate (G1), then LIVE.** A strategy needs ≥ 100 paper trades over ≥ 30 sessions, with a mean net per trade
above 0. The day-clustered bootstrap lower bound must also be above 0 at 1.5× the measured spreads, corrected for how
many versions were tried. The placebo needs n ≥ 50 and must not also make money. At most 10% of sessions may be
impaired, and recon must find zero mismatches. 25 confirmation trades follow. LIVE then also needs `ALLOW_LIVE=1` in
the env file, a CLI unlock bound to the G1 report's hash, and that day's self-test. LIVE starts at 1 lot. It is
demoted automatically on any of these: a ₹6K loss within the first 20 LIVE trades, a ₹15K drawdown since unlock, the
last 50 trades averaging below 0, paper proving optimistic against live (X2), or any LIVE recon problem.

## 2. v1's live failures and how titan2 now prevents each

The normative scenario tests (k01–k12, pm_*, the restart cases) each run twice: once on PAPER and once on LIVE
against a fake Kite. Both runs must send the same broker commands and book the same P&L.

**The brief's §4.1 live killers**

| # | v1 failure | titan2 now | Tests |
|---|---|---|---|
| 1 | Recon read a dead JSON file and market-sold every live entry about 10 s after its fill | One owner, one DB. Recon never sells. Lag-safe rules (2 s post-fill, 5 s grace, two agreeing reads). A read that contradicts the order book halts instead of closing | `test_k01_recon_never_exits_fresh_entry`, `test_positions_lag_never_external`, `test_a_positions_lag_never_books_an_external_close[12/16/20 s]`, `tests/unit/test_recon.py` |
| 2 | A broker-flat position was booked as a stop hit, then sold with a MARKET order: a naked short | Exit qty = min(open qty, broker net long from a read made after the decision; see B1). Verify-flat never sells. A broker-side close is booked from the tradebook, with a MISMATCH halt | `test_k02_broker_flat_never_sells`, `test_verify_flat_residual_never_sold`, chaos invariant 8 (200 seeds) |
| 3 | P&L was booked at LTP and the position dropped even when the exit failed | Position = broker-confirmed fills. A failed exit stays EXITING with no trade row. P&L is booked once, from fills | `test_k03_failed_exit_keeps_position` |
| 4 | OPEN was counted as filled; position = planned qty at LTP | IOC only; quantity only from fills; OPEN with 0 filled → cancel → VOID | `test_k04_open_is_not_fill` |
| 5 | GTT legs front-ran the bot and sold flat positions | No GTT code anywhere; the identifiers are banned | `test_k05_no_gtt_anywhere`, `test_no_gtt_slm_market_identifiers` |
| 6 | An SL-M SELL was re-placed every 3 s | LIMIT + IOC + MIS only; the DB rejects anything else | `test_k06_only_limit_ioc`, chaos invariant 7 |
| 7 | A multiplier was applied after the caps; MAX_LOTS=0 disabled the cap | One `size()`: the smallest of 6 caps, applied last, no multiplier. A zero or invalid cap refuses. Caps can only be tightened | `test_k07_caps_and_cheap_bomb`, `test_pm_a_one_lot_cap_binds`, `test_no_multiplier_identifier` |
| 8 | Gates failed open (empty allow-list passed, missing data passed, `except: pass`) | Each of the 22 gates passes only while its good condition holds. An exception is a refusal; 5 gate errors in 5 min → halt | `test_k08_gates_fail_closed`, `tests/unit/test_risk.py::test_each_gate_fails_closed` (129 cases) |
| 9 | The spike sustain used `abs()`, so reversals fired as spikes, and direction was flipped | Signed sustain; UP → call only; nothing is ever flipped | `test_k09_signed_sustain_no_flip`, `tests/golden` (v1's `abs()` case) |
| 10 | Stale and unrealizable prices: LTP with no age check, REST quotes re-stamped fresh, exits on LTP, breakeven at gross entry | The quote cache never returns a stale quote (age from receipt time). Exits use the realizable bid. REST refresh at 3 s, protective exit at 5 s. Lock floor = breakeven with charges + 3%. Time stop on the wall clock | `test_k10_bid_not_ltp_and_cost_lock`, `test_pm_d_stale_quote` |
| 11 | Ungated order paths: a GPT agent with order tools, dashboard buttons that placed orders, no auth | Only the core decides. Only `broker/kite.py` calls Kite's order methods. The dashboard has no credentials and 3 commands. No LLM | `test_k11_single_order_path`, `test_only_kite_adapter_calls_order_methods`, `test_dashboard_import_isolation`, `tests/unit/test_dash.py` |
| 12 | Halts didn't stick, MTM came from a stale cache, the kill switch didn't flatten, paper and live had different caps | Halts are DB rows that survive restarts. Unknown MTM blocks entries. KILL and FLATTEN finish only when the broker shows flat. Paper and live have the same caps | `test_k12_sticky_halts_and_kill`, `tests/unit/test_engine.py::test_flatten_all_verified`, `::test_kill_demotes` |

**The May 13–14 postmortem and other incidents**

| v1 incident | titan2 now | Test |
|---|---|---|
| Oversize on cheap options (NMDC-like lot 6,750 at ₹3.5) | 1 lot, ₹20 premium floor, ₹25K premium cap | `test_pm_a_oversize`, `test_pm_a_one_lot_cap_binds` |
| Reverse churn (the −2.3% reverse safety net) | Reversals don't exist; cooldowns; at most 2 entries a day | `test_pm_b_no_reverse_churn` |
| LTP P&L and phantom profits | P&L from fills only; an AST test proves the P&L code never reads LTP | `test_pm_c_pnl_from_fills_only` |
| A CE and a PE held on one underlying | Gate G10 plus a DB unique index | `test_pm_c_both_legs_impossible` |
| ZYDUSLIFE: an internal SL_HIT with no real exit | Every exit is a real IOC; a leg closes only when broker net is 0 | `test_pm_phantom_sl_exit` |
| SONACOMS: a frozen websocket quote on a thin option | REST refresh at 3 s, protective exit at 5 s | `test_pm_d_stale_quote` |
| HINDPETRO: partial fills | The exit walks the bid levels; P&L = sum of fills; ₹20 per executed order | `test_pm_partial_fills_walk` |
| SAMMAANCAP: the thin-option spiral | Spread ≤ 1.5%, depth ≥ 3× on both sides, no reversals, worst-case cap | `test_pm_spread_spiral_blocked` |
| Paper and live behaved differently | One code path; every normative scenario runs on both | `test_pm_e_same_path`, plus the dual-run check |
| Paper filled at LTP or on 100% of the shown depth | Fill-time book, ≤ 50% participation, latency, never LTP | `test_pm_f_paper_fills_from_depth`, `test_entry_fills_on_the_book_that_moved_during_the_latency` |
| The ledger drifted from the broker | 15:33 truth check (≤ ₹1) with a drift alert | `test_pm_ledger_matches_broker` |
| The CI deploy restarted v1 on every push | `deploy.yml` deleted; CI only runs tests | `tests/ops/test_ops_static.py::test_ci_no_deploy_no_secrets`, `::test_only_the_ci_workflow_remains` |
| A process-wide monkey-patch of KiteConnect | Startup refuses to run (exit 78) if `place_order` is patched | `tests/unit/test_kite_lanes.py::test_monkeypatch_detected` |
| SEBI's 10-orders-per-second rule | Order bucket: 8/s, burst 3, never more than 8 in any second | `tests/unit/test_kite_lanes.py::test_orders_bucket_sebi_window` |

**How strong are these tests?** A reviewer broke the protections on purpose: 32 deliberate bugs. The named tests caught
28. Four got past them:
- removing recon's lag-safety rules (post-fill wait, grace), in 3 variants: `test_k01` and `test_positions_lag_never_external`
  still pass, and only `tests/unit/test_recon.py` catches them;
- removing the re-entry cooldown in `risk.cooldown_end`: that function turned out to be dead code. The live rule, in
  the DB query, is caught (low-severity item 12).

So every live protection is caught by some test, but `k01` should be sharpened (low-severity item 10, §3F).

*Update (B1 batch, 2026-10-11):* `k01` now catches the lag-safety bugs on PAPER and LIVE. The one variant that
changes nothing `k01` can see is still caught by `tests/unit/test_recon.py`. `k02` now also catches an exit capped by
a read older than the exit decision (a ₹500 naked short in the test). `risk.cooldown_end` is deleted; the live rule
is tested where it lives (`tests/unit/test_store.py::test_cooldowns_are_the_rule_the_gates_use`, plus
`tests/unit/test_risk.py::test_cooldown_gate_boundary`). Low-severity items 10–12 are fixed.

## 3. Review: what was found, fixed and open

**How it ran.** Round 1 used seven lenses: broker/API, exits/risk, incidents, money, ops/security, state/recovery and
strategy/G1. It produced 35 findings, 34 of them confirmed with proof tests (74 proof tests in all). Eight fixers
changed code in parallel. A post-fix pass then reconciled their work (docs, contract-note routing, one receipt-time
rule everywhere, the recon window), and the suite ended green. **Rounds 2–3 are deferred to the paper period, before
any LIVE decision.** Nobody has independently reviewed the round-1 fixes yet; the 106 tests added with them are the
only check on them.

### 3.1 The 34 confirmed findings

| # | Sev | What was wrong | Now |
|---|---|---|---|
| 1 | high | A LIVE place that timed out but was accepted late became NOT_PLACED: an unmanaged long, or an exit sold twice | **Fixed.** 30 s late-accept window; a late fill is adopted and sold (`tests/scenarios/test_late_accept.py`) |
| 2 | high | One bad broker report caused an endless rollback/reload loop, and every reload reset the post-fill clocks, so no leg could exit | **Partly.** Clocks survive reloads, other legs keep exiting, and a CRITICAL fires after 20 a minute. The root cause and the exit memory are open (§3A-1, 2) |
| 3 | critical | A websocket tick that arrived while an event was being handled read as infinitely stale, so healthy positions were closed protectively | **Fixed.** A receipt up to 2 s "in the future" counts as fresh, everywhere (`test_a_timer_racing_a_ws_tick_keeps_a_healthy_leg_open`) |
| 4 | high | An accepted order missed by two tag lookups became NOT_PLACED: an exit sold again (naked short), an entry left unmanaged | **Fixed.** Lookups at +1/+3/+7/+15 s; NOT_PLACED counts as a possible fill for recon (`tests/scenarios/test_review_incidents.py`) |
| 5 | high | A positions read that disagreed with the order book booked a phantom external close and abandoned a held position | **Fixed.** New CONTRADICTION class: nothing booked, the leg stays managed, HALT(MISMATCH) + CRITICAL. On LIVE it also demotes, and you must ACK |
| 6 | medium | Paper filled against the decision-time book: no latency slippage | **Fixed.** Only a book received after the order counts; else REST depth within 1.5 s; else cancel |
| 7 | high | A NOT_PLACED entry that really filled was never adopted | **Fixed** (same mechanism as 1) |
| 8 | high | EXTERNAL_CLOSE was declared from lagging positions alone (lags over 10–15 s) | **Fixed.** The sale must show in the orders or in the day's sell quantity |
| 9 | high | DB-lost recovery aborted when two bot legs shared an underlying | **Fixed.** `RECOVERED:<acct>:<seq>` books; recovery never aborts |
| 10 | medium | The running bot loaded the exit loop lazily, so after a deploy without restart it could run the new release's exit loop | **Partly.** titan2 modules load at start. Third-party modules can still load later from a new venv (§3A-11) |
| 11 | medium | Deploy checked "flat" once, then restarted the bot minutes later | **Fixed.** Re-checked before the switch and before the restart |
| 12 | medium | Paper could never fill a 1-lot order against 1-lot depth levels | **Fixed.** Participation applies to the depth walked. A book with only one lot inside the band still never filled (by design), which review round 2 found flatters paper stop-outs: since paper model 2 (2026-10-14, CHANGELOG) it fills half the time, by the order's seeded draw, so half of that bias is left (a LIVE IOC usually takes the lone lot) |
| 13 | medium | The PAPER contract-note sample went to the paper exchange, so charges were never checked against Zerodha | **Partly.** It now goes to Kite. Whether Kite prices paper ids is [V] V5, and no LIVE gate requires verified charges (§3A-10) |
| 14 | medium | The LIVE self-test passed without seeing the tag echo or a websocket update | **Fixed.** A VERIFY stage needs both |
| 15 | medium | The paper contract-note check was circular | **Fixed.** The paper exchange refuses; Kite prices it |
| 16 | medium | The exit-side depth check counted stub bids at any price | **Partly.** `size()` counts only levels within 1% of mid. Gate G19 still counts raw quantity (§3A-4) |
| 17 | low | DAY_BUDGET ignored the LIVE account's room and open positions' stop risk | **Fixed (review round 3, 2026-10-14).** `size()` supports it and `SingleLegHandler.plan_entry` passes the inputs (§3A-4) |
| 18 | medium | A leftover structure with a working order never settled, blocking LIVE entries | **Fixed.** Concluded at the next start (CANCELLED or NOT_PLACED, P&L UNRESOLVED, CRITICAL) |
| 19 | low | A broker square-off after 15:34 could not be configured or booked the same day | **Partly.** The range is now 15:20–15:39, and the recon window follows the setting. A real square-off later than the setting is still missed (§3A-3) |
| 20 | medium | The SIGTERM "stopping" flag lived only in RAM; a placebo entry was created during shutdown | **Fixed** |
| 21 | medium | The core loop starved its queue when a Timer took over 250 ms | **Fixed** |
| 22 | medium | A slow recon read starved the EXIT quote refresh | **Fixed.** Quotes have their own thread and Kite client |
| 23 | medium | Every exit's first order waited for a fresh positions read; paper answers instantly, so paper exits look better than live | **Fixed (B1 batch).** Strict: only a read made after the decision caps an exit, and paper's positions and orders reads now pay a seeded delay (median 250 ms, p95 about 1 s) |
| 24 | medium | The square-off setting and the recon window assumed the old 15:30 close | **Fixed** |
| 25 | medium | Paper exits never pay LIVE's positions round trip, and the X2 check can't see that | **Fixed (B1 batch).** Paper pays a modelled round trip, and each exit logs its decision-to-order time (`decision_to_order_ms`). Whether the model matches Zerodha's real `positions()` time is known only once something trades LIVE |
| 26 | medium | A lock on the dashboard's command DB froze the core for 5 s per Timer | **Fixed** (10 ms timeout) |
| 27 | medium | The dead-man schedule could not be met: DOWN at about 15:44 every day and all day on holidays | **Partly.** New schedule; the bot keeps pinging on weekday holidays. **You must reconfigure healthchecks.io** |
| 28 | medium | Stage 0 left v1's broker secrets readable once bootstrap opens `/home/ubuntu` | **Partly.** Stage 0 now makes v1's files owner-only; bootstrap doesn't double-check (§3A-11) |
| 29 | medium | Any stray module in the working folder made every start exit 78, and systemd never restarts that | **Fixed.** Only modules that shadow an import block a start. With positions open it starts exits-only and alerts |
| 30 | medium | The paper "fill-time" book could be the decision snapshot (same as 6) | **Fixed** |
| 31 | medium | G1's bar-mismatch test was diluted by the 20 random control symbols | **Fixed** |
| 32 | medium | A whole session with no heartbeat was never marked IMPAIRED | **Fixed** |
| 33 | medium | CONFIRM certified without the IMPAIRED and bar-check criteria | **Fixed** |
| 34 | low | The 09:15 bar closed before queued ticks were drained | **Fixed** |

### 3.2 What is open

**A. Left unresolved by the round-1 fix step (12)**
1. **Reload-storm root cause** (conflicts with the spec). A broker report that contradicts a *still-working* order
   (say, REJECTED but with fills) still raises an invariant violation and a rollback every time it is seen. Round 1
   kept everything else working through such a storm, but the poisoned order is never quarantined. Its stuck-order
   alarm can't fire either, because every reload restarts its cancel timer. Fix before LIVE. It needs a spec decision:
   quarantine the report instead of raising.
2. **Exit memory resets on every reload.** During a storm, the "exit uncapped after 10 s of failed reads" and
   "verify-flat not done in 30 s" escalations never fire. Fix together with 1.
3. **A real square-off later than the setting.** If Zerodha squares off later than `BROKER_SQUAREOFF_AT` (default
   15:26, [V] V2), the bot's last recon runs too early, and the close is booked only at the next start. Either set the
   real time once V2 is verified, or add a bounded EOD recon while legs are held.
4. **Day budget and stub bids.** The day budget is done (review round 3, 2026-10-14): `size()` gets the realized
   day, each open position's stop-out measured from its entry (the first version also charged an open gain as risk
   still to lose, which refused a safe second entry beside a winner; corrected the same day) and, on LIVE, the LIVE
   account's room. Gate G19 still counts bid quantity at any price (G18 refuses those books first). Fix that before
   LIVE.
5. **Decided 2026-10-11 and done:** B1 = strict, with a paper read delay (see B1).
6. **Decided 2026-10-11:** B2 = keep 30 s (see B2).
7. **No version bumps in the first release.** `PAPER_MODEL_VERSION` and `ENGINE_SEMANTICS_VERSION` stayed 1
   although round 1 changed paper fills and recon rules. That was harmless: no G1 data existed, and the spec pins 1
   for the first release. Once
   paper starts, every such change bumps a version, which restarts the G1 trials. The B1 batch kept them at 1 too:
   the first release's paper model now includes the read delay (CHANGELOG). Review round 2's paper fixes made
   `PAPER_MODEL_VERSION` 2 (2026-10-14, "Paper model v2" in the CHANGELOG), and the same release makes
   `ENGINE_SEMANTICS_VERSION` 2 for the engine changes of review rounds 2 and 3 and the wait for pending entries
   ("Signals wait for unfinished orders"): one new version for each strategy, three new G1 trials (m = 12).
8. **The deploy self-check looks in the wrong folder.** It checks for shadowing modules in the release folder, not in
   the bot's working folder. (`PYTHONSAFEPATH=1` was rejected: it would make titan2 unimportable.) The bot's own
   startup check runs in the right place.
9. **X2 is one-sided.** It flags only paper filling more often than live, and only twins where both traded. This
   matters only once something is LIVE.
10. **Contract-note follow-ups.** (a) Whether Kite prices paper (synthetic) order ids is unverified (V5). (b) No LIVE
    unlock requires verified charges. (c) The daily report's VCN line doesn't show the result. (d) The event doesn't
    record which rate set was checked. Do (b) before LIVE.
11. **Ops leftovers.** (a) Bootstrap doesn't refuse to open `/home/ubuntu` while v1 files there are still readable
    (Stage 0 locks them; this would be a second line of defence). (b) Deploy or rollback without `--restart` can
    switch `current` under a running bot, and any third-party module imported later would come from the new venv.
    Deploy after the close with `--restart`. (c) The websocket back-fill silently skips terminal EXIT/COVER rows that
    the broker shows filled (audit trail only). (d) On a weekday holiday with a recovery-only run, the dead-man pings
    stop early: a rare false alarm.
12. **Reviewer proof tests still failing:** see D.

**B. Two decisions for you** (both made on 2026-10-11; RUNBOOK §10, Decisions)

**B1. May an exit use a positions read made up to 1 s before the exit decision?** (`recon.EXIT_CAP_PRIOR_READ_S = 1.0`)

**Decided 2026-10-11: (b), and done.** `EXIT_CAP_PRIOR_READ_S` is 0 (since the review of the batch it lives in
`titan2/settings.py`, inside `SEMANTIC_CONSTANTS`, and every retry needs its own post-attempt read too). The paper
exchange answers positions and orders reads after a seeded delay (`PAPER_READ_LATENCY_MEDIAN_MS` 250 ms,
`PAPER_READ_LATENCY_SIGMA` 0.85: p95 about 1 s). Each exit's decision-to-order time is logged
(`EXIT_ATTEMPT.decision_to_order_ms`, and the daily report's `exit_decision_to_submit_ms` row) so the burn-in can
check p95 < 1.5 s. The text below is the question as it was put.

Before each exit order, titan2 asks Zerodha how many it holds and never sells more. To be strict, that question must
be asked *after* the exit decision. Otherwise a manual close (or a Zerodha RMS action) in the last moment is
invisible: the bot sells a position that is already gone, and you are naked short. Strict costs one broker round trip
per exit on LIVE: usually well under a second, but up to 5–10 s if the positions API hangs. The paper exchange
answers in about 10 ms, so paper exits look faster than live ones will be. A fixer allowed a read up to 1 s old. That
removes most of the wait, but it reopens a 1-second window for the naked-short race, and paper stays instant either
way.
- *Options:* (a) keep 1 s; (b) go strict (0 s) and make paper pay a realistic, seeded read delay, so paper = live;
  (c) go strict only.
- *Recommendation: (b), before paper data starts counting toward G1.* "Never sell what you don't hold" is guarantee
  G3. A naked short on a thin option has no loss cap, while a few hundred ms of delay on a 1-lot long is bounded by
  its premium. Paper should measure the exits you will really get, and changing this later restarts the trials. If
  you commit to a dedicated Zerodha account that you never trade by hand, (a) is defensible; then measure the
  decision-to-order latency in the burn-in.

**B2. After an exit order times out, titan2 waits up to 30 s before trying again.** (`orders.LATE_ACCEPT_S = 30`)

**Decided 2026-10-11: keep 30 s.** No code change. The reason, and how to measure it on LIVE before anyone shortens
it, are recorded above `orders.LATE_ACCEPT_S` and in RUNBOOK §10 (Decisions). The text below is the question as it
was put.

When an exit order's send times out, titan2 can't know whether Zerodha took it, and a second SELL could sell twice.
So it looks the order up by its tag at +1, +3, +7 and +15 s, and keeps watching until 30 s, before it concludes
"never placed" and tries again. Most cases resolve at the first lookups; 30 s is the worst case, and for that long a
losing position is not re-offered. A late fill found after 30 s is adopted and sold, not ignored.
- *Options:* keep 30 s; shorten it after measuring; or allow a second SELL sooner, which brings back the double-sell
  risk.
- *Recommendation: keep 30 s for paper and the first LIVE weeks.* The cost is bounded: a 1-lot long can't lose more
  than its premium, and the day is capped at ₹40K. What it protects against, a naked short, is not bounded. Paper never
  times out, so the burn-in won't show how often this happens. Log every ambiguous place on LIVE. Shorten the window
  (to about 15 s, say) only if Kite's `orders()` proves to list new orders within a few seconds.

**C. Items the fixers marked "not fixed" (10 entries; 6 still open)**

| From | Item | Status now |
|---|---|---|
| Fixer 1 | The PAPER contract-note sample is routed to the paper exchange | Resolved by the post-fix step: it now goes to Kite |
| Fixer 2 | Count reload-storm violations toward the "20 exceptions a minute → exit 3" rule | Open on purpose: a restart would re-read the same report and storm again. It alerts instead; the real fix is A-1 |
| Fixer 2 | A self-check row comparing `BROKER_SQUAREOFF_AT` with the observed square-off times | Open (it needs a RUNBOOK row with it) |
| Fixer 2 | Dead-man pings on a weekday-holiday recovery-only run | Open, rare (A-11d) |
| Fixer 4 | Paper exits never pay LIVE's positions round trip; X2 can't see it | Resolved by the B1 batch: paper reads pay a seeded delay |
| Fixer 4 | `size()`'s new day-budget inputs are not passed by its caller | Resolved by review round 3 (2026-10-14): `plan_entry` passes them |
| Fixer 4 | G19 still counts bid quantity at any price | Open (A-4) |
| Fixer 7 | X2's fill-rate check is one-directional | Open (A-9) |
| Fixer 8 | An independent check of the charges through Kite before LIVE | Partly: routing is done; Kite's acceptance (V5) and a LIVE-unlock gate are open (A-10) |
| Fixer 8 | Test failures caused by concurrent edits | Resolved: the suite is green |

**D. Reviewer proof tests: 74 in all; 54 pass and 20 fail**

7 fail on **real open gaps** (round-1 figures; the B1 batch fixed the gaps behind the last three rows and review
round 3 those behind `test_f6` and `test_m3`, so 1 is still open):

| Proof test (in scratchpad `wf4/review/`) | Gap |
|---|---|
| `exits_risk/test_f6…::test_realized_beyond_stop_trips_even_when_mtm_is_unknown` | The daily stop doesn't trip while any leg's MTM is unknown, even when the realized loss alone is past ₹10K. Entries are blocked anyway, but the other open legs run on to their own stops instead of being flattened. Arguable (an unknown long MTM may be a gain). **Fixed (review round 3):** a realized loss alone past the stop trips it. The proof test still fails: its ₹12K loss is inside today's ₹15K stop; `tests/unit/test_risk.py` has the case at ₹16K |
| `money/test_m2…::test_g19_liquidity_accepts_stub_bids` | G19 counts stub bids (A-4) |
| `money/test_m3…::test_live_account_room_is_not_budgeted` | The day budget is not wired (A-4). **Fixed (review round 3):** `plan_entry` passes the inputs. The proof test calls `size()` as the old caller did, so it still fails; `tests/unit/test_review_r3_fixes.py` goes through `plan_entry` |
| `money/test_m3…::test_open_positions_remaining_risk_is_not_budgeted` | The same, fixed the same way |
| `ops_security/test_manual_bot_verb.py` | Running `titan2 bot` by hand starts an unsupervised bot without the env file. It takes the instance lock, so systemd's bot exits 73 and is never restarted (low-severity item 15). **Fixed (B1 batch):** outside systemd the verb refuses (exit 2) unless `--outside-systemd`; this proof test now stops at that refusal |
| `state/test_recovery_vanished.py` | After a restart, an order sent after the recovery read is reported "vanished": a false CRITICAL and a global INVARIANT halt that blocks entries until you ACK (low-severity item 7). **Fixed (B1 batch):** the proof test passes, and a copy is in `tests/scenarios/test_recovery_vanished.py` |
| `strategy_g1/test_opening_drive_premarket_reset.py` | Any websocket reconnect before 09:20 makes opening_drive skip the whole day (low-severity item 17). **Fixed (B1 batch):** the proof test passes |

13 fail because the behaviour they pinned has **changed as intended**:
- 9 asserted the old defect, which is now fixed: the late-accepted entry and exit (2), a paper fill on the pre-order
  book, a self-test passing with no echo, the paper contract-note routing (3), 15:35 not being configurable, and the
  dead-man check against the *old* healthchecks schedule. That last one comes true if you don't reconfigure
  healthchecks.io;
- 4 check the fix in a way that no longer applies. `test_f4` reads the old `RECON_WINDOW` constant; the engine now
  extends the window from the setting, and `test_squareoff_at_1535_is_configurable_and_booked_same_day` covers it.
  `money/test_m1`'s wrong-rate-set test prices notes on the paper exchange, which now refuses: Kite does the pricing,
  which rests on V5. `test_shutdown_reload` loses its process because the bot now exits cleanly; unit tests cover the
  fix. `test_v1_secrets…stage0` is a text check; `tests/ops/test_stage0_lockdown.py` covers the behaviour.

3 tests **pass because the low-severity defect they demonstrate is still there**: `test_kite_error_types`,
`test_poll_pileup` and `test_squareoff_classifier` (low-severity items 1–3).

**E. Unverified backlog: none.** Every round-1 finding was either confirmed (34) or dismissed (1).

**F. Low-severity findings, not fixed in round 1 (20; the B1 batch fixed 9 of them)**

| # | Finding | When to fix |
|---|---|---|
| 1 | Kite's MarginException, HoldingException and UserException arrive as GeneralException and are treated as AMBIGUOUS, so the order is looked up for 30 s instead of being refused at once | Before LIVE |
| 2 | Order polls aren't tracked in flight. During a Kite brownout, polls pile up on the single live order lane ahead of exit orders | Before LIVE |
| 3 | The square-off classifier compares `placed_by` with "API", but Kite sends the user id. On LIVE, any external order after 15:26 counts as Zerodha's square-off: a manual close after 15:26 would book a ₹59 fee and raise no MISMATCH | Before LIVE (with V2) |
| 4 | `BROKER_SQUAREOFF_AT` capped at 15:29 | Addressed in round 1 (15:20–15:39) |
| 5, 6 | The LIVE contract-note check will flag a false CHARGES_DRIFT on every Zerodha square-off order (the ₹50 + GST fee model) [V] | Before LIVE |
| 7 | The false "vanished order" after a restart (proof test above) | **Fixed (B1 batch):** an order sent after the recovery read is skipped, not reported vanished |
| 8 | During the SIGTERM window (up to 20 s), no OPEN leg is evaluated and no new exit attempt is made | Before LIVE |
| 9 | The default 15:15 flatten starts exactly when F&O stocks enter the cash closing auction (15:15–15:35) | Check in the burn-in |
| 10 | `k01` still passes with recon's lag safety removed; unit tests catch it | **Fixed (B1 batch):** an 8 s positions lag, a post-fill read inside the grace, checked at every step |
| 11 | `k02` doesn't exercise the "read after the decision" cap rule; unit and chaos tests do | **Fixed (B1 batch):** a manual close and the hard stop at the same instant, 2.5 s after a read that shows the lot |
| 12 | `risk.cooldown_end` is dead code, and its unit test checks a copy of the rule the gates never use | **Fixed (B1 batch):** deleted; the live rule is tested in `Store.cooldowns` |
| 13 | The EOD hand-off can't follow a square-off later than the setting | Partly addressed (§3A-3) |
| 14 | The incident coverage map and mutation results | Information (§2) |
| 15 | `titan2 bot` from the operator wrapper (proof test above) | **Fixed (B1 batch):** outside systemd it refuses (exit 2) unless `--outside-systemd` |
| 16 | Watchdog, out-of-memory and SIGKILL restarts mid-session send no alert | **Fixed (B1 batch):** the next start sends CRITICAL `BOT_KILLED` |
| 17 | opening_drive skips the session after any websocket reconnect before StartSignals (proof test above) | **Fixed (B1 batch):** only a reconnect inside the opening bar skips the day |
| 18 | The placebo is priced at the parent's decision but sent after the parent's simulated fill, so the price path during that latency decides which placebos fill. That weakens the placebo check | **Fixed (B1 batch):** sent with its parent, from the same snapshot (a documented spec deviation: every PAPER entry sent, not every entry filled; FINAL_SPEC §10.6 still says filled) |
| 19 | Uncapped SHADOW books share the SHADOW account's paper capital with every PLACEBO book | **Fixed (B1 batch):** each SHADOW book has its own paper pool |
| 20 | opening_drive's volume baseline (the previous session's last 20 bars) now overlaps the closing auction, because of the 15:40 close | Check in the burn-in [V] |

**G. Older open items carried over from the build**
- **Fixed (B1 batch).** `strategies/base.py` sat outside the strategy version hash and `REGISTRY.lock`, so editing it
  changed both strategies without starting a new G1 trial. Its code hash is now part of both. The review of the batch
  found the strategy module's own code was still outside the version (only `REGISTRY.lock` guarded it, and a hand
  refresh of the lock hid an edit); the version now hashes it too.
- A restart inside 09:20:00–09:20:10 re-emits opening_drive's batch. SHADOW books skip the cooldown gate, so a
  duplicate SHADOW entry is possible.
- Recon's REPAIRED class can never occur, because terminal orders are immutable. The spec decision is open.
- When both broker reads after an exit decision fail, the newest earlier read caps the exit. In that window a position
  closed outside the bot can still be sold (SHORT_DETECTED alert, no automatic cover). **Narrowed (review of the B1
  batch):** an earlier read now caps nothing until 10 s have passed without an answer made after the decision (the
  exit waits and re-asks every 2 s), and that fallback sends a CRITICAL `EXIT_CAP_BEFORE_DECISION`. Before it fired
  as soon as one positions and one orders read had failed. It still sells less than `EXIT_UNCAPPED` would, but a close
  made after that older read is sold again.
- `tests/unit/test_alerts_token_selfcheck.py::test_alert_dedup_nonblocking` keeps a 50 ms wall-clock bound. It is the
  suite's only timing-sensitive check.

**H. Left open by review round 2 (2026-10-14; its fixes are in the CHANGELOG)**
- **A COVER on an already CLOSED leg is never booked** (low, LIVE only; `exit_loop.py:436`). The extra sale and its
  cover move real money, but the leg's trades row is immutable and stays FINAL. It needs append-only correction rows.
  The laptop cannot trade LIVE. Fix before LIVE.

## 4. Known gaps and the [V] items to verify in the paper burn-in

[V] marks a fact that only the live broker or exchange can confirm. Record each result in `docs/RUNBOOK.md` §10.

| # | Item | Status | How it gets checked |
|---|---|---|---|
| V1 | Charges match a real contract note | PARTIAL: matches the research cost model within ₹0.02 an order | Put a real F&O contract note dated 2026-04-01 or later into `tests/unit/fixtures/contract_note.json` (Q5) |
| V2 | Zerodha's MIS square-off time since the 15:40 close, and how those orders look (`tag`, `placed_by`) | PENDING | Zerodha's schedule, then watch one square-off |
| V3 | NFO accepts IOC on option LIMIT orders | PARTIAL (library source) | The first LIVE self-test |
| V4 | `tag` comes back in websocket updates and in `orders()` | PARTIAL | The LIVE self-test |
| V5 | The virtual contract note prices paper (synthetic) order ids | PARTIAL | The 15:33 PAPER CHARGES_CHECK must show orders > 0 and no drift |
| V6 | `positions()` lag after fills (p95) | PENDING | Needs real fills: the first LIVE trades, or one manual 1-lot test |
| V7 | Static-IP whitelisting (SEBI) and the egress-IP check | PARTIAL | Allocate and register the Elastic IP |
| V8 | The NSE ban-list URL and format; the holiday calendars | PARTIAL: 2025–26 holidays verified, 2027 not yet published | The daily ban-list row; add 2027 in December |
| V9 | Zerodha's physical-settlement policy for stock options | PENDING | Zerodha's support article |
| V10 | Kite's 09:15 candle includes pre-open volume | PENDING | The 15:36 bar check |
| V11 | Which `margins()` field is available cash | PARTIAL (titan2 uses the smaller of two) | One live `margins()` response |
| V12 | EC2 has Python ≥ 3.10; home-folder permissions are as assumed | PENDING | `ops/bootstrap.sh`, `titan2 selfcheck --phase cutover` |
| V13 | `basket_order_margins` works read-only (phase 2 only) | PARTIAL | Not used in phase 1 |
| L1–L7 | The 15:40 close; lots (NIFTY 65, BANKNIFTY 30) taken from the dump; Tuesday expiries taken from the dump; the 2026-04-01 charges; SEBI's 10 orders/s and static IP; no entries before 09:20 and no paper fills before 09:15:30; freeze quantities | VERIFIED 2026-10-10 | — |

Also check during the burn-in:
- **exit latency:** decision → order dispatch must be under 1.5 s at p95 (a Stage 2 exit criterion; the daily
  report's `exit_decision_to_submit_ms` row); also note how often the post-decision positions read is slow (B1);
- **closing auction:** the 15:15 flatten meets the stock closing auction (15:15–15:35), so watch exit spreads and
  fills from 15:15 to 15:25. opening_drive's baseline window also differs from the backtest's;
- **server sandbox:** the timezone-suffixed timers need systemd ≥ 235, and chrony should use Amazon Time Sync. Also,
  `systemctl is-enabled` and `timedatectl` must work inside the bot's sandbox, or the v1-masked and clock rows fail
  closed every morning;
- **Kite behaviour:** `placed_by` on API, app and RMS orders (low item 3), and Kite's error types (low item 1);
- **paper realism:** X2 (paper against live twins) can't run until something trades LIVE, so until then check paper
  fills against the spreads logged in `quote_checks`;
- **calendar:** the file covers only 2025–2026. The self-check warns from 2026-12-01, and from 2027-01-01 every day is
  a non-trading day (fail closed) until NSE's 2027 list is added.

Gaps by design: multi-leg trading (phase 2) is designed but not built. There is no broker-side stop: if the host dies,
the 15:26 square-off is the backstop.

## 5. Commands

From `%USERPROFILE%\MCX\agentic_trader`:

```
python -m pip install --require-hashes --only-binary :all: -r requirements-dev.txt   # once per environment

python -m pytest tests -q -p no:cacheprovider                  # all 1,005 tests, about 10 min here
python -m pytest tests/unit tests/arch -q -p no:cacheprovider  # 646 tests, 1.6 min
python -m pytest tests/golden -q -p no:cacheprovider           # 11 tests, under a second
python -m pytest tests/scenarios -q -p no:cacheprovider        # 109 runs (normative scenarios on PAPER and LIVE), 1.8 min
python -m pytest tests/chaos -q -p no:cacheprovider            # 200 seeds + 11 checker self-tests, 2.5 min
python -m pytest tests/ops -q -p no:cacheprovider              # 28 tests, 4.4 min; needs Git for Windows bash
python -m pyflakes titan2 tests                                 # clean today (pip install pyflakes)
```

- The 2 skips: the contract-note fixture is waiting for a real note (Q5), and the v1 golden recapture is opt-in
  (`TITAN2_GOLDEN_RECAPTURE=1`).
- Nightly-strength chaos: set `TITAN2_CHAOS_SEEDS=500` first.
- Python 3.10: run the same commands with a 3.10 interpreter. Today's run used `scratchpad\venv310\Scripts\python.exe`.
- A Windows trap: don't give the ops tests a long `--basetemp`. My first ops run used one, and 10 tests failed on the
  260-character path limit. With pytest's default temp folder all 28 pass.

**A local offline paper session:** the spec doesn't support one for the real bot. Paper trading fills against
Zerodha's live order book, so the bot needs a Kite login and the live feed. What does run offline:
- a seeded simulated session: fake market and fake Kite, with the real engine, the real paper exchange and injected
  faults. It prints a timeline:
  `python -c "import sys; sys.path[:0]=['.', 'tests']; from fakes.chaos import replay; replay(4)"`
- the scenario tests, each a scripted mini-session on both accounts, for example
  `python -m pytest tests/scenarios/test_postmortem.py -q -p no:cacheprovider`;
- the read-only CLI against a scratch state folder. In PowerShell: `$env:TITAN2_VAR = "C:\temp\titan2var"`, then
  `python -m titan2 is-trading-day --date 2026-10-12`, `python -m titan2 settings show` and `python -m titan2 status`.

## 6. Needed before EC2 paper deployment

**On the laptop**
1. **Review the uncommitted tree** and make decisions B1 and B2. *Decisions made 2026-10-11; the tree is committed as
   `db5c5ba`, the review checkpoint.*
2. **Fix what must be in place before paper data counts.** My recommended set: B1 (a paper read delay), the placebo
   timing (F-18), SHADOW capital sharing (F-19), the opening_drive reconnect skip (F-17), `base.py` in the version hash
   (§3G), the false "vanished order" halt (F-7), a guard on a hand-run `titan2 bot` (F-15) and a restart alert (F-16).
   After paper starts, any behaviour change bumps a version and restarts the G1 trials. *Done in the B1 batch
   (uncommitted on top of `db5c5ba`, for review as one diff), with the five fixes from its review (top of this
   report). The operator rule that came out of that review: never sell by hand while the exit ladder runs (RUNBOOK
   P7).*
3. **Commit.** *Done: tag `v1-final`, commit `db5c5ba`, specs in `docs/spec/`.* Run `git tag v1-final b4694f3`.
   Then make one commit with the 147 deletions
   (`git ls-files --deleted -z | xargs -0 git rm --cached -q --`) and the new `titan2/`, `tests/`, `ops/`, `docs/`,
   `pyproject.toml`, `requirements*.txt`, `.github/workflows/ci.yml`, `.gitignore`, `CHANGELOG.md` and
   `TITAN_INSTRUCTIONS.md`. Also commit `FINAL_SPEC.md` and `FINAL_SPEC_DETAIL.md` (as `docs/spec/`, say): the code
   cites them, and today they exist only in the scratchpad. `_titan_dashboard.html` also shows as modified; that
   change is your own from May, not this build.
4. **Before anything reaches `main`,** run `gh workflow disable "Deploy Titan Bot"` and delete the `EC2_SSH_KEY`
   secret. v1's `deploy.yml` restarts the old bot on every push.
5. **Rotate the leaked secrets:** the Kite API secret, the Zerodha password, TOTP and the Dhan token.
   `.kite_access_token` and `dhan_config.json` are still tracked (cleanup deferred, as you asked); `.gitignore` now
   lists them.
6. **Generate the dashboard credentials:** `python -m titan2.dash.app hash-password` and `… new-secret`.

**On EC2** (RUNBOOK §8)

7. **Stage 0:** run `ops/stage0_cutover.sh titan-bot` (a dry run), then `--execute`, outside 08:30–16:00 IST. It masks
   v1, clears the crontab, archives the v1 DB, and freezes and locks down the v1 tree. Then remove the AWS rules for
   ports 80 and 5000.
8. **Elastic IP:** allocate it now so the IP is stable. LIVE needs it (V7).
9. **Stage 1:** run `bootstrap.sh` (a dry run, then `--execute`). Write `/etc/titan2/titan2.env` with `ALLOW_LIVE=0`,
   and `dash.env`. Then run `ops/deploy.sh titan-bot`, `titan2 token` and `titan2 selfcheck --phase cutover`; enable
   the units and timers; set up Telegram; and create a healthchecks.io check with the schedule `45-59 8 * * 1-5`,
   `* 9-14 * * 1-5`, `0-33 15 * * 1-5` (Asia/Kolkata, 3 min grace).
10. **Stage 2 burn-in,** at least 10 sessions. It passes with: zero software-caused CRITICALs, zero paper recon
    mismatches, exit p95 under 1.5 s, daily reports produced, drills D1–D6 passed (crash, deploy and rollback, token
    failure, dashboard flatten, dead-man, backup restore), and the [V] list worked through.

**Before any LIVE decision,** add: review rounds 2–3; §3A items 1, 2, 4 and 10b; low-severity items 1, 2, 3, 5 and
8; the daily-stop gap (§3D); V1–V7 verified; and at least 50 G1 sessions (3–5 months).

## 7. Honest limits: what titan2 cannot guarantee

- **Profitable trades.** "Every trade profitable" is not reachable. Costs are paid up front (about 1.9% of premium per
  round trip, 85% of it bid-ask), the direction after a trigger is close to a coin flip, and stop fills slip. The
  research found no entry rule with a positive out-of-sample expectancy. titan2 makes engineering losses hard and
  measures honestly; it cannot create an edge. The likely outcome is that no strategy passes G1 and titan2 never
  trades real money. That is the system working.
- **A per-trade loss of at most ₹5K.** ₹5K is a sizing budget: the hard stop plus a 5% slippage allowance. A gap
  through the stop, an empty book or a stuck exit can lose more on one trade. The true ceiling for one long option is
  its premium (at most ₹25K), and for the day the ₹40K worst-case cap.
- **Fills in outages.** When Kite, the exchange or the EC2 host is down, exits may not happen. The backstops are the
  15:26 MIS square-off, the dead-man alert and the worst-case cap. A host death just before 15:26 is not covered.
- **Paper realism.** Paper models latency, 50% participation and the fill-time book, and since B1 a seeded delay on
  its positions and orders reads. It can't model queue position, hidden liquidity or other traders reacting, and its
  read delay is a model, not Zerodha's measured time. Nothing checks paper against live until a strategy trades LIVE.
- **The broker as tested.** Every test runs against a fake Kite built from the spec's understanding of Kite. Real
  behaviour (IOC acceptance, tag echo, positions lag, square-off orders, error types) is still [V].
- **Review depth.** One review round of three. Eight agents wrote the round-1 fixes in parallel, and they have not
  been re-reviewed; only the tests added with them check them.
- **Manual trading in the same Zerodha account.** By design it halts entries (FOREIGN), and a hand close at the wrong
  moment is the one way left to get a naked short. Strict B1, with its own read for every exit order, narrows that to
  three cases: a close that the post-attempt positions read does not show yet (positions can lag), a close made while
  a bot SELL is already on its way, and, when Zerodha's reads have failed for 10 s, a close made after the older read
  that then caps the exit (CRITICAL `EXIT_CAP_BEFORE_DECISION`). So never sell by hand while the bot's exit ladder
  runs: RUNBOOK P7 says to stop the bot first. Use a dedicated account (Q3).
- **Size.** At 26K lines titan2 is more than four times the planned size. The tests are broad (1,005 of them, plus
  74 proof tests and 32 deliberate-bug checks), but they show the absence of the bugs someone looked for, not of all
  bugs.

## Appendix A. titan2 by module

| Module | Lines | Code lines | Planned (DETAIL §2) |
|---|---|---|---|
| `core/engine.py` | 2,630 | 2,187 | 340 |
| `runtime.py` | 1,604 | 1,273 | 300 |
| `store.py` | 1,440 | 1,080 | 360 |
| `core/recon.py` | 1,421 | 1,020 | 300 |
| `core/risk.py` | 1,147 | 806 | 300 |
| `core/orders.py` | 1,125 | 822 | 220 |
| `selfcheck.py` | 1,011 | 773 | 190 |
| `broker/paper.py` | 1,008 | 772 | 340 |
| `g1.py` | 940 | 708 | 280 |
| `model.py` | 917 | 667 | 300 |
| `ledger.py` | 886 | 678 | 180 |
| `instruments.py` | 828 | 591 | 260 |
| `settings.py` | 814 | 594 | 230 |
| `__main__.py` (CLI) | 735 | 616 | 180 |
| `broker/lanes.py` | 701 | 530 | 200 |
| `broker/kite.py` | 699 | 502 | 260 |
| `market/feed.py` | 574 | 417 | 230 |
| `alerts.py` | 566 | 406 | 140 |
| `core/exits.py` | 534 | 316 | 220 |
| `dash/app.py` | 531 | 391 | 200 |
| `core/positions.py` | 526 | 380 | 200 |
| `token.py` | 516 | 367 | 200 |
| `strategies/base.py` | 509 | 343 | 140 |
| `core/gateway.py` | 501 | 377 | 260 |
| `core/exit_loop.py` | 461 | 343 | 220 |
| `core/operator.py` | 442 | 343 | 140 |
| `clock.py` | 410 | 284 | 140 |
| `market/bars.py` | 392 | 285 | 160 |
| `core/events.py` | 379 | 242 | 140 |
| `core/state.py` | 339 | 229 | 160 |
| `core/single_leg.py` | 335 | 221 | 120 |
| `charges.py` | 314 | 196 | 150 |
| `core/invariants.py` | 202 | 143 | 100 |
| `strategies/price_spike_up.py` | 181 | 134 | 190 |
| `strategies/opening_drive.py` | 144 | 107 | 120 |
| `broker/port.py` | 129 | 62 | 110 |
| `core/sizing.py` | 124 | 69 | 80 |
| `strategies/__init__.py` | 114 | 78 | — |
| `market/quotes.py` | 95 | 56 | 120 |
| `plugins.py` | 65 | 40 | 40 |
| 5 package `__init__.py` files | 23 | 1 | 5 |
| **Python total** | **26,312** | **19,449** | **7,825** |
| `dash/templates/index.html` | 356 | 338 | 220 |
| `migrations/0001_init.sql` | 168 | 156 | 230 |
| `data/nse_sessions.json` | 66 | 66 | — |
| `strategies/REGISTRY.lock` | 14 | 14 | — |

## Appendix B. Tests by folder

| Folder | Files | Lines | Tests (collected) | Time today |
|---|---|---|---|---|
| `tests/unit` | 31 Python + 1 fixture | 20,436 + 26 | 627 | with arch: 95 s |
| `tests/arch` | 2 Python + 1 manifest | 763 + 1,345 | 19 | (above) |
| `tests/scenarios` | 10 | 2,037 | 109 | 105 s |
| `tests/chaos` | 1 | 184 | 211 | 147 s |
| `tests/golden` | 2 Python + 4 fixtures | 1,221 + 2,157 | 11 | under 1 s |
| `tests/ops` | 6 | 1,874 | 28 | 261 s |
| `tests/fakes` (the shared harness: fake Kite, scripted market, faults, scenario runner) | 11 | 3,383 | — | — |
| `tests/conftest.py` | 1 | 19 | — | — |
| **Total** | **64 Python + 6 data** | **29,917 + 3,528** | **1,005** | **10.3 min** |

Under Python 3.10.19, unit + arch + golden + scenarios + chaos: 975 passed, 2 skipped, in 5.7 min.

## Appendix C. Against baseline `b4694f3`

`git diff --stat b4694f3` (tracked files): **153 files changed, 1,600 insertions(+), 456,228 deletions(−)**.
- **147 deleted.** 108 Python files (88,081 lines), led by `autonomous_trader.py` 13,142, `zerodha_tools.py` 7,799,
  `options_trader.py` 6,881, `dashboard.py` 4,903 and `kite_ticker.py` 2,467. Also data files (`dhan_instruments.csv`
  277,439 lines, `scan_decisions.json` 70,769, `trade_history_today.json` 13,199), 2 HTML templates (4,342), 10 shell
  scripts, 8 systemd unit files, the Docker and nginx files, `.env.example` and `.github/workflows/deploy.yml`.
- **4 modified:** `TITAN_INSTRUCTIONS.md` (rewritten, +827/−327), `CHANGELOG.md` (+112), `.gitignore` (+11) and
  `_titan_dashboard.html` (+650/−38, your change from May, not this build).
- **2 renamed** (staged): the v1 technical doc and playbook moved to `docs/v1/`.
- **151 new files**, not yet tracked: 63,837 lines in `titan2/`, `tests/`, `ops/`, `docs/`, `pyproject.toml`,
  `requirements.lock`, `requirements-dev.txt` and `.github/workflows/ci.yml`. The 184 other untracked files in the repo
  (your research scripts and local files) predate the build and were not touched.

**What remains of v1 (tracked, kept):** 202 `agentic_trader/_*` research files (20,556 lines of Python) and 17
repo-root `_*.py` (10,065 lines), all byte-for-byte unchanged. About 55 other repo-root v1-era files (one-off scripts,
sector CSVs, pyright outputs, the old `README.md`, which still describes the GPT-4o agent). Research data:
`trade_ledger/`, `daily_summaries/`, `all_trades_dump.json`, `sniper_trades_dump.json`, `speed_gate_log.jsonl`,
`ml_models/data` (593 files) and `ml_models/oi_snapshots`. Model folders: `ml_models/saved_models`, `reports` and
`direction` (6 Python files that import a deleted module and run only from a `v1-final` worktree). The two leaked
token files. On the server, v1 is still installed until Stage 0 freezes it. titan2 imports none of it; an arch test
enforces that.
