# Titan v2 design brief: max WR_net, hard-capped losses, positive out-of-sample Exp_net

Conventions: net = after bid-ask + Zerodha charges; % = of premium; 1 lot unless stated; TRAIN = entries before 2026-05-01, TEST = after; "x / y" = WR_net / Exp_net after a split or n, else TRAIN / TEST. Numbers survived adversarial verification (corrected values) unless tagged **[U]** (unproven).

## 0. Bottom line
- **No entry rule has a verified positive out-of-sample Exp_net.** Ledger, naked paper: TRAIN n=1,306, WR_net 38.8%, Exp -Rs562 (-0.90%); TEST n=916, 34.4%, -Rs911 (-2.17%, CI -1,114..-707). 98% of the TRAIN loss = 120 THP conversions (without them -Rs11, CI -441..+420: break-even). TEST stays negative after reverses stopped (05-16..07-14: n=299, 41.1%, -2.7%).
- **Costs are the wall:** Rs1,065/trade = 1.90% of premium, 85% of it bid-ask; TEST had no gross edge (per-trade gross -0.38%).
- **Current regime** (06-10..07-14, allow-list, 1 lot): n=32, WR_net 46.9%, Exp -3.77%; PRICE_SPIKE_UP 13 trades, 76.9% WR, Exp -Rs25; GMM_SNIPER 30.8% (n=13); PRICE_SPIKE_DOWN 16.7% (n=6). Anecdotal.
- **Both candle backtests:** all 7 trigger families lose after costs under every exit tested; pooled 30-min follow-through +0.017..+0.028% (right direction 47-50%) vs ≈0.15% underlying move needed (0.106% costs + 0.046% decay).
- Exits and day rules **trade Exp for WR; they create no edge.** The order/P&L path is unsafe for live money regardless (§4.1).

## 1. Realistic ceiling
- **WR_net 45-55%** with current-style entries: path-valid exit baseline TEST 51.7% (survivor-inflated; June-July 42.2%); best TRAIN-chosen exit (TP +8%) TEST 52.7% / -1.45%; max 2 entries/day 47-50%; cost-anchored lock 50-57% (path order unknown).
- **Any-exit ceiling** (sell at best observed bid): 73.2% TRAIN, 71.5% TEST, 64.4% June-July; 27-36% of entries never cover costs even at their peak (partly a speed-gate artefact).
- 77-93% WR appears only with TP 3-5% + wide stop (avg win +1.2% vs avg loss -15..-22%, 13-18x; TEST Exp -1.2..-2.9%) or in the constant-IV BS model.
- **Exp_net:** evidence supports ≈ -1.5% to 0% per trade. Most optimistic exits-only stack: TEST -0.08..+0.60%, June-July -0.09..-0.87%, 2x spreads -1.2%. Positive Exp is **[U]**.
- **Why 100% is impossible:** (a) costs are paid up front: break-even premium move +1.3-1.55% at ≥Rs20, +2.3-2.8% at Rs5-20, +4.3-5.3% below Rs5; (b) post-trigger direction is a coin flip; (c) stop fills slip median 0.63%, p95 4.4%, max 15.3% of premium (n=392, paper; live worse), so even a break-even lock can close red; (d) 1-tick break-even locks yield "wins" of Rs0.03-0.15; (e) the 13 live trades lost more than the cost model predicted at base costs.

## 2. backtest_bs vs backtest_dg
- **Agree** (both verified, trust high). Reference run: BS TRAIN n=3,255 45.4% / -1.53%, TEST n=2,651 42.6% / -1.78%; DG TRAIN n=3,290 43.8% / -1.71%, TEST n=2,698 41.5% / -1.99%. Unfiltered: BS 0 of 4,480 exits ≥+1% in both TRAIN halves; DG 0 of 1,078 positive in any family. Both: smaller TP → higher WR, lower Exp. DG with BS pricing: -1.39/-1.56, within ≈0.2 pp.
- **Disagree on the VSURGE opening drive.** BS (bar ≥2.5%, ITM-1, ≥Rs20, TP8/SL30, stop to BE+1% after +3%): TRAIN n=150 92.7% / +2.11%, TEST n=73 91.8% / +2.27%. DG analogue (VSURGE ≥2.06%, ATR14 ≤3.63%, TP5/SL30): TRAIN n=148 93.2% / +1.39%, TEST n=152 84.9% / -1.22% (-0.24% with Rs20 floor).
- **Trust DG for magnitudes; BS is an upper bound; opening drive = low-confidence paper hypothesis:**
  - BS bias vs 2,047 ledger trades: median +0.40 pp, mean +0.83; on PRICE_SPIKE trades +1.55 median, +2.96 mean, the size of the claimed edge.
  - Ledger premiums decay beyond BS, growing with hold (-0.5 pp ≤5 min, -4.7 pp >90 min); DG fits this (TRAIN-only fit: same conclusions), BS ignores it. TEST decay was 2x TRAIN (0.048 vs 0.021 pp/min), so DG's TEST numbers still lean optimistic.
  - BS's own stresses: exact TRAIN trades move +0.054% in 30 min (t 0.33); opposite-direction placebo still wins 83-86%; 10% IV crush → -1.39%; stop slippage + 2x spread → -0.49 / -0.19%; TP30/TS45 variant swings +0.54..+4.61% with intra-bar order; post-bar realised vol 1.23-1.29x model IV.

## 3. Rules (evidence · confidence · falsifier)
Default falsifier: the rule's metric flips sign over ≥100 paper-truth trades, with the counterfactual shadow-logged. Safety invariants (Z1, X1-X3, X8-X9, D2-D3) are verified by reconciliation, not P&L.
### 3.1 Entry
- **E1 No live entries until a candidate passes G1 (§3.8).** Delete GMM_SNIPER (TEST n=75, 33.3%, -Rs1,451), NEWS (TRAIN n=73, 19.2%), PRICE_SPIKE_DOWN, ARBTR/ORB/EARLYBIRD/VOLUME_SURGE (no producer or dead; EARLYBIRD -Rs601/trade at 2x spread), and all OI/GMM/LLM/reverse/dashboard-manual producers. High.
- **E2 Paper candidates, pre-registered, one at a time.** (a) Opening drive (new 5-min-bar trigger): bar ≥2.5% in direction (09:15 bar open-to-close; 84% fire there), volume ≥3x prior-20-bar mean, enter next open, ITM-1, ≥Rs20, §3.5 exits. BS TEST +2.27% (model), DG analogue -0.24%; model-free 30-min continuation TRAIN +0.19% (t 2.0), TEST +0.16% (t 1.0). (b) PRICE_SPIKE_UP, unflipped, signed sustain: n=13, Exp -Rs25. (c) NDHL break after a ≥1.55% 15-min run, TP30/SL15: TEST +0.08..+0.37%, CI -2.25..+2.47, WR ≈40% (clashes with WR goal). All low. Falsifier: fails G1, or opposite-direction shadow trades also profit.
- **E3 Direction = trigger direction; on scorer/OI disagreement skip, never flip.** Flips polluted PRICE_SPIKE stats. High (design).
- **E4 No score/ML/IV/delta/sector/weekday/DTE filters:** 588 TRAIN-picked single-feature rules, 0 positive on TEST; ml_move_prob ≥0.677: TRAIN +Rs1,374 → TEST -Rs361. High. Falsifier: a pre-registered filter passes G1.
- **E5 Keep no entries ≥14:55; don't adopt 14:30:** it lifts TEST WR (55.9% vs 26.5%) but late trades had better Exp (-0.95% vs -1.82%). Medium.
- **E6 Trigger TTL ≈10 s, price re-check at order time, no OI/news/scan work on the entry path** [U thresholds].

### 3.2 Strike / premium / liquidity
- **S1 Premium ≥Rs20** (current Rs3 setting is unenforced). <Rs20 bucket: WR_net 35.0% / 27.5% (n=555/360); remainder 41.7% / 38.8% (still -Rs118 / -Rs538); path-valid Exp +0.97 / +0.84 pp; ≈40% of the gain is gross, not cost-model. Medium-high. Falsifier: paper Rs20-30 bucket underperforms.
- **S2 DTE ≥2**, else next monthly: +0.11..+0.17 pp (DG). Medium.
- **S3 ATM or ITM-1** (within 0.15 pp in both backtests); delete the Dhan OI-heatmap picker. Medium.
- **S4 Fail-closed liquidity gate:** quote age ≤3 s, spread ≤1-1.5% of mid, ask- and bid-side top-3 depth ≥2-3x qty, slippage vs mid. Thresholds [U] (spreads never recorded); never looser than code defaults 4.0/2.5 (live json 5.0/5.0). Falsifier: median entry fill >1% from mid.

### 3.3 Sizing
- **Z1 Exactly 1 lot; delete all multipliers.** One size() = min(risk_qty, depth_qty, MAX_LOTS, MAX_UNITS), applied last; a 0/invalid cap refuses; skip if 1 lot breaches any cap. Basis: postmortem (a); >6,960-unit bucket = <Rs20 bucket (354/356). High.

### 3.4 Execution
- **X1 One gateway, one lock: check → place → confirm → record.** Entry: marketable LIMIT-IOC ≤ min(ask+2 ticks, mid×1.01). Exit: bid-2 ticks, re-priced until broker net qty is 0. Positions/P&L only from terminal WS order updates (filled_qty, average_price). High. Falsifier: any bot/broker mismatch.
- **X2 Paper = same code + broker stub** filling at ask/bid (depth-walk) from fresh quotes, same charges. High. Falsifier: paper-vs-live slippage gap >0.5 pp.
- **X3 No broker SL-M;** GTT only as SL-only crash net below the hard SL, cancelled and verified before any bot exit. High.

### 3.5 Exits: one engine, bid-based, wall-clock
- **X4 Hard SL -15% of fill premium on bid; a quote >3 s old forces a REST depth refresh, else exit.** The current -32% SL never bound (0/803). Evidence mixed: upper-bound gains (TP8 base, no MAE) +0.14/+0.26 pp at -10%, +0.42/+0.88 pp at -5%; SL10 hit ≈29% of reference trades; invalidation-only worst trades -57..-71%. Low. Falsifier: MAE logs show >25% of stopped trades reaching cost-BE+5% within 30 min.
- **X5 Cost-anchored lock:** once bid ≥ entry + costs + 4%, stop ≥ entry + costs + 3%, ratchet only; replaces gross-entry breakeven and partials. Failed winners: 19.9-22.5% of path-valid trades. A 1% buffer is refuted (37% of stop fills slip >1%; TEST pessimistic WR 43.2% vs 51.7% baseline); 2-3% buffers keep it at 49.6-52.3%. Low-medium. Falsifier: >15% of lock exits net ≤0.
- **X6 TP +15% (never below +8%):** +0.44 pp TRAIN, +0.20 pp TEST, better in all 5 months; TEST rests on 26 hits; TP ≤6% made TRAIN Exp ≤0. Medium; small.
- **X7 One time stop (45 min [U]) replaces** the speed gate, GREEKS_EXIT (59/60 net losers), TIE, velocity kill, GCR, WME (a no-op), conviction reversal, static targets and mirror exits. Speed gate unvalidated: 25% of trades never reaching cost-BE (half speed-gated) later rose ≥10% above it. Low. Falsifier: the same pattern after time-stop exits.
- **X8** Flatten from 15:15 and verify flat; 15:22 backstop. **X9 Never reverse:** reverse legs TRAIN n=530, 27.5%, -Rs1,043; TEST n=1,125, 33.5%, -Rs1,179. High.

### 3.6 Day level
- **D1 Max 2 entries/day:** TRAIN n=60 50.0%, TEST n=55 47.3% (baseline 38.9% / 34.2%); a far smaller worst day than the baseline's, matching the 3-rule set (first loss ends day, 2 positions, 5 entries: TEST 49.2% / -3.1%). Current regime (n=23) 47.8% / -2.61% vs 46.9% / -3.77%. Partly regime mix; no day rule makes TEST Exp positive (-1.8..-3.4%). Medium.
- **D2** MAX_POSITIONS 2, ≤1 per underlying, same in paper and live, enforced in the gateway.
- **D3 Daily stop on realised + bid MTM, sticky to the next session.** Realised-only caps overshoot (with a Rs5K cap the worst days still lost many times the cap). High.
- **D4** Stop-after-first-loss [U]: +14.6 pts TEST WR, mostly regime mix (n.s. within day/time buckets); cuts current green days 57% → 43%. **D5** Keep the 30-min re-entry cooldown, persisted.

### 3.7 P&L truth (precondition)
Ledger pnl is fiction: plain gross on 904 rows, gross - 0.6% turnover on 1,093; 36/38 EOD-orphan PE signs flipped; naked trades booked a small loss but netted a large one, and reverse legs booked a large gain but netted a larger loss; MFE missing on 62%. Require one net_pnl(fills, exact per-order charges: flat Rs20/order, current STT/exchange rates) feeding ledger, risk governor and dashboard; log mode, decision LTP, bid/ask at entry/exit/each check, fills, slippage, bid-based MFE/MAE with timestamps, 1-min premium path and distinct exit codes; signed-qty broker reconciliation.

### 3.8 Gate G1 (paper → 1-lot live)
≥100 paper-truth trades over ≥30 sessions; Exp_net >0 with day-clustered 90% CI lower bound >0 at 1.5x measured spreads; opposite-direction shadow Exp ≤0; zero reconciliation mismatches. Live kill: rolling-50 Exp_net <0 or any mismatch.

## 4. Code plan (code facts from the maps, not statistics)
### 4.1 Fix regardless of design (live killers first)
1. position_reconciliation 283-301 + autonomous_trader 9197-9238: reads dead active_trades.json → every live entry market-exited ≈10 s after fill (offline sim; 5/14 legs logged BROKER_ONLY). Fix: read state_db, check ownership, alert/halt, no orders.
2. autonomous_trader 12653-12706: broker-flat position booked STOPLOSS_HIT → MARKET SELL on flat (naked short); use BROKER_CLOSED + real closing fill.
3. zerodha_tools 1614-1712, 1438-1537: books LTP P&L and pops the position even when the exit fails.
4. zerodha_tools 17-99, 5953-6063: OPEN counted as success; position = plan qty at LTP after 0.5 s.
5. zerodha_tools 1006-1135: GTT target leg front-runs the bot; orphan GTT can sell a flat position; startup cleanup deletes user GTTs.
6. autonomous_trader 8805-8826: re-places an SL-M SELL every 3 s for positions without sl_order_id.
7. zerodha_tools 5633-5643, 5790-5824: multiplier applied after MAX_UNITS; MAX_LOTS=0 disables the cap.
8. Fail-open gates: liquidity 4587-4593/5873-5875; allow-list 5003-5014 (empty setup passes, substring match, except:pass); missing P(move) passes 3409/3475; unknown tick age = fresh (data_health_gate 272).
9. kite_ticker 1701-1848: sustain uses abs() (reversals fire as spikes); autonomous_trader 2435-2443, 2513-2542, 3072-3114 flip direction, 3385-3392 relabel.
10. Stale/unrealisable prices: get_ltp no age check (kite_ticker 420-448), 60 s batch default, REST LTP re-stamped fresh (autonomous_trader 9310); exits on LTP not bid (8380-8390); breakeven at gross entry (exit_manager 1321-1330); loop-count candles (7090).
11. Ungated order paths: GPT agent with order tools (llm_agent 304-578; autonomous_trader 132, 11996-12322); dashboard place_option_1lot 2873-3089, place_news_trade 3736-4047, ghost add_lot 1323-1508; no auth on 0.0.0.0:5000.
12. Risk/state: non-sticky halt (risk_governor 150-257); stale unrealised cache (335, 775); consecutive-loss limit 999; paper cap 80 vs live 2 (autonomous_trader 1572-1580, zerodha_tools 5130-5148); kill switch doesn't flatten (dashboard 4798); watcher built before settings apply; bot + 2 dashboard workers rewrite active_trades wholesale.

### 4.2 Keep / modify / delete
- **KEEP:** kite_ticker WS/cache core (53-94, 274-414, 645-689, 733-770), PriorityTriggerQueue 973-1036 (+TTL), spike baselines/detection 1507-1533, 2049-2108; state_db 213-263 + exit/risk/recon persistence; settings_manager 106-183 (+ceilings: may only tighten); kite_token_manager; zerodha_tools kite_global_throttle 101-153, _check_option_liquidity 4547-4669; options_trader BlackScholes 3023-3161, dataclasses 2695-2794, select_strike 3497-3550; trade_ledger; exit_manager hard SL/cutoff 445-485, trailing ratchet 1332-1406; dashboard broker_state 2019-2072, pause flag 674-717.
- **MODIFY:** place_option_order 4960-6067 into gates → select → size → execute → record; exit_manager.update_trade 277-438; risk_governor (sticky, fill-fed); position_reconciliation 303-457 (signed qty, fail-closed); market_scanner 132-168 (nearest-expiry lots); config.calc_brokerage 70-107; dashboard exits → bot command queue.
- **DELETE modules (19.1K lines):** watcher_pipeline, watcher_exit_engine, capital_swap_engine, dashboard_ec2, llm_agent, oi_watcher_engine, options_flow_analyzer, dhan_oi_fetcher, nse_oi_fetcher, dhan_risk_tools, dhan_futures_oi, dhan_token_manager, download_oi_history, sniper_strategies, regime_score, smart_money_signals, quant_analytics, gmm_data_collector, idempotent_order_engine, execution_guard, correlation_guard, data_health_gate (→ option-quote check), greeks_engine, thesis_validator, expiry_shield (keep get_dte); ~49 research scripts (≈9.5K). Unless one passes G1: ml_models (8.4K), news_scanner (2.2K), IntradayOptionScorer (options_trader 105-2577), trend_following (1.1K).
- **DELETE regions:** autonomous_trader 859-1137, 1153-1518, 1632-1738, 3744-4523, 4552-6354, 6415-6657, 7608-7766, 8191-8253, 8260-8379, 8402-8609, 8620-8679, 8805-8951, 9606-9617, 9920-10287, 10523-10735, 10744-12465 (≈6.7K of 13,142); zerodha_tools 644-936, 1539-1612, 1714-1881, 3699-4536, 4671-5054, 5274-5352, 5411-5519, 6069-7736 (≈3.7K of 7,799); options_trader 4037-4403, 4509-6511, 6605-6757 (≈2.5K); kite_ticker 121-272, 810-958, 1194-1272, 1535-1695, 1850-2047, 2128-2228; exit_manager 570-718, 800-1025, 1064-1319, 1492-1649; dashboard 724-1508, 1636-2008, 2707-4047; config 112-131, 364-458, 535-764, 778-848, 866-988, 1097-1150, 1173-1293, 1349-1377, 1452-1697.

## 5. Architecture options (facts, no decision)
Size: 76.1K lines in top-level non-underscore .py (incl. ≈10.6K scripts) + 8.4K ml_models + 15.9K underscore scripts; autonomous_trader 13,142, zerodha_tools 7,799, options_trader 6,881, dashboard 4,903. ≈60K is dead/off-goal per the maps (est.).
Couplings: ≥12 exit callers do their own LTP accounting; risk_governor ~25 call sites in 4 modules; watcher gates read caches built by scan_and_trade (breadth, VIX, sector, futures OI); BreakoutWatcher private state mutated from 3 modules; OIWatcherEngine back-reference plus startup assert; importing zerodha_tools monkey-patches KiteConnect process-wide, dashboard included; 3 JSON IPC files, one with 2 consumers; ~10 thread types, only _trade_lock serialises orders; no real test suite.

| Option | Removed (est.) | Testability | Regression risk |
|---|---|---|---|
| A prune: §4.2 deletions + §4.1 fixes in place | ≈60K; ≈24K remain, paper/live branches interleaved | low: monolith, live paths hard to stub | high: every cut crosses the couplings above; P&L fix spans 4 modules |
| B lean core: new gateway, exit engine, reconciler, risk gate, orchestrator on KEEP infra | ≈79K retired; ≈4-6K new + ≈5K reused | high: broker-stub unit tests, one paper/live path | medium: must rebuild restart recovery, token/ops flows, ledger/DB compatibility |
| C strangler: keep ticker/state_db/ledger/settings/dashboard reads; replace orders, exits, recon, risk first; new core shadows on the same tick cache | ≈60-70K in phases | medium-high | medium: two engines coexist; every shared table needs one writer |

## 6. Unverified, do not cite
Map-only figures (trailing exits 17/17 green; the speed gate/TIE loss total; latency percentiles; exit-type win rates); the 'unflipped SPIKE_UP' profit (verified: n=13, Exp -Rs25); EARLYBIRD's TRAIN profit (far smaller with orphans); DG trend-extension pocket (found post-TEST).

Evidence scripts: a local working folder, wf1/ (scorecard, exits, backtest_bs, backtest_dg, day_rules, verify_*); not part of this repository.