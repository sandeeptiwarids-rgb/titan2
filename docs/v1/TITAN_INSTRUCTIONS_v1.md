# TITAN AUTONOMOUS TRADING BOT — INSTRUCTIONS & ARCHITECTURE

> **Last Updated:** 2026-03-09  
> **Version:** v5.2  
> **Mode:** PAPER TRADING | Zerodha Kite API  
> **EC2:** `titan-bot` SSH alias → `/home/ubuntu/titan/agentic_trader/`  
> **Service:** `systemctl status titan-bot`

---

## 1. WHAT IS TITAN?

Titan is an autonomous intraday options trading bot for NSE F&O stocks. It runs as a systemd service on AWS EC2, scanning ~200 stocks every 5 minutes, scoring opportunities via ML models, and executing trades through Zerodha Kite API. Currently in paper trading mode with ₹5,00,000 simulated capital.

---

## 2. FILE MAP — RUNTIME MODULES

| File | Purpose |
|------|---------|
| `autonomous_trader.py` | **Main orchestrator** — scan loop, scoring, strategy routing, P&L tracking (~600KB) |
| `zerodha_tools.py` | Broker API wrapper — order placement, position management, WebSocket, trade ledger hooks |
| `exit_manager.py` | Exit logic — SL, trailing, breakeven, time stops, thesis validation, Greeks-based exits |
| `config.py` | All constants — capital, thresholds, VIX regimes, scoring tiers, risk limits |
| `trade_ledger.py` | Append-only JSONL trade log + SQLite sync (ENTRY/EXIT/CONVERSION/SCAN events) |
| `state_db.py` | SQLite WAL-mode persistence — active trades, exit states, risk state, orders, trades table |
| `trade_query.py` | CLI for instant trade analysis — `python trade_query.py [date] [--week] [--source X]` |
| `execution_guard.py` | Order quality gates — IV crush, theta ratio, Greeks SL, position sizing |
| `greeks_engine.py` | Black-Scholes Greeks calculation for options |
| `correlation_guard.py` | Sector correlation filtering (prevents over-concentration) |
| `expiry_shield.py` | Gamma risk protection for 0DTE/1DTE trades |
| `kite_ticker.py` | WebSocket real-time price feed from Zerodha |
| `market_scanner.py` | Stock screening pipeline |
| `data_health_gate.py` | Data staleness/quality validation |
| `idempotent_order_engine.py` | Duplicate order prevention |
| `llm_agent.py` | GPT integration for trade picking (optional) |
| `options_trader.py` | Options-specific execution logic |
| `options_flow_analyzer.py` | OTM/premium flow analysis |
| `dhan_oi_fetcher.py` | DhanHQ futures OI data fetcher |
| `nse_oi_fetcher.py` | NSE OI data collection |
| `risk_governor.py` | Position/capital risk limits |
| `regime_score.py` | Market breadth/regime detection |
| `dashboard.py` | Flask SSE real-time monitoring + manual exit capability |
| `ml_models/predictor.py` | ML model loader/predictor — move probability, direction, DR scores |

---

## 3. ML MODELS

**Location:** `ml_models/saved_models/`

| Model | Type | Output | Used For |
|-------|------|--------|----------|
| Move Predictor | XGBoost binary | `ml_move_prob` (0-1) | P(move ≥0.5% in 30 min) — primary gate |
| Gate Model | XGBoost binary | P(MOVE) | Should-trade filter |
| Direction Model | Meta-ensemble | `prob_up`, `prob_down` | **Direction removed from gates as of 2026-03-09** — still predicted but not used for entry/exit decisions |
| Down-Risk GMM | VAE + GBM + GMM + IsolationForest | `dr_score`, `up_flag`, `down_flag` | Anomaly-based regime detection |

### XGB Direction Removal (2026-03-09)
Direction predictions (UP/DOWN) were **fully removed from all decision-making**:
- No more ALL_AGREE concept
- No SNIPER direction alignment gate
- No MODEL_TRACKER direction multiplier
- No herd filter
- No GPT direction conflict filter
- TEST_XGB strategy disabled entirely
- Debit spreads and Iron Condors use P(move)-based quality instead
- Direction is still predicted and logged but never gates or boosts trades

---

## 4. STRATEGY ENTRY POINTS

### 4.1 ELITE AUTO-FIRE
- **Trigger:** Score ≥ 76
- **Guards:** ML confidence ≥55%, P(move) ≥52%, max existing move ≤2.5%
- **Max:** 3 per cycle
- **Sizing:** Premium tier (5% risk, 80% target)

### 4.2 MODEL_TRACKER (Continuous Scoring)
- Runs every scan cycle on all F&O stocks
- Scores via IntradaySignaler → IntradayScorer
- Passes through ML gates (P(move), DR score)
- Routes to strategy: Credit Spread / Debit Spread / Naked Buy

### 4.3 WATCHER (Real-time Breakout)
- WebSocket-driven, not scan-cycle dependent
- **Triggers:** Price spike (≥0.7%/60s), volume surge (≥3x), new day high/low
- **Gates:** Score ≥30, P(move) ≥0.40, ADX ≥20
- **Cooldown:** 180s between same stock
- **VIX block:** >32 blocks all watcher entries

### 4.4 GMM SNIPER
- High-conviction trades from GMM/VAE anomaly clusters
- 3× lot multiplier
- Max 1 per cycle

### 4.5 ARBTR (Sector Arbitrage)
- Laggard convergence plays within sectors
- Requires sector divergence signal

### 4.6 TEST_GMM (Pure DR Model)
- Bypasses standard scoring gates
- Uses DR model anomaly signals directly

### 4.7 TEST_XGB — **DISABLED** (2026-03-09)

---

## 5. EXIT MANAGEMENT

### Exit Types (in priority order)
| Exit | Trigger | Hedgeable? |
|------|---------|------------|
| SL_HIT | Premium hits stop loss | → THP intercept |
| TARGET_HIT | Premium hits target | N/A (win) |
| TRAILING_SL | After +1R, locks 50%+ | N/A (win) |
| TIME_STOP | No follow-through in 10-20 candles | → THP intercept |
| IV_CRUSH | Implied volatility collapses | **No** — immediate exit |
| GREEKS_EXIT | Delta collapse (<0.08) or theta bleed | Yes |
| VELOCITY_KILL | Premium bleeding >1.5%/candle | Yes |
| DEBIT_SPREAD_TIME_EXIT | THP-hedged spread hits 15:05 cutoff | N/A |
| EOD_EXIT | Force-close at 15:25 | N/A |

### THP (Thesis Hedge Protocol)
When a trade hits SL or time stop, instead of closing:
1. **Check if hedgeable** (spread width available, cost-aware)
2. **Convert naked option → debit spread** (buy the option + sell a further OTM strike)
3. **New SL/target** based on spread dynamics
4. **Time gate:** 15:05 hard cutoff for all debit spreads
5. **Skip THP when:** trailing SL is active, or trade already in profit (breakeven applied)

THP Intercept Types:
- `TIME_STOP_HEDGE` — dead trade, hedge instead of exit
- `SL_HIT_HEDGE` — SL hit, convert to spread
- `PROACTIVE_LOSS_HEDGE` — large unrealized loss, pre-emptive hedge
- `NEVER_SHOWED_LIFE` — option never moved in favorable direction

**Non-hedgeable exits:** IV_CRUSH exits bypass THP entirely (immediate exit).

---

## 6. SCORING SYSTEM

### Score Tiers
| Score Range | Tier | Risk % | Target % |
|-------------|------|--------|----------|
| 0-39 | BLOCK | — | — |
| 40-59 | BASE | 2% | 50% |
| 60-72 | STANDARD | 3% | 65% |
| 73+ | PREMIUM | 5% | 80% |
| 76+ | ELITE | 5% | 80% (auto-fire) |

### ML Score Adjustments
- P(move) ≥ 0.65: +6 score boost
- P(move) 0.55-0.64: +4 boost
- P(move) 0.45-0.54: +2 boost
- P(move) < 0.35: -3 penalty

### OI Quality Gate (predictor.py:630-700)
When DhanHQ futures OI data has garbage values (basis=0%, vol_ratio=0) due to spot merge failure:
- Zeroes score_boost
- Sets signal to FLAT
- Prevents bad-data predictions from inflating scores

---

## 7. VIX REGIME SCALING

| VIX Range | Regime | Score Mult | Lot Size | SL Width |
|-----------|--------|-----------|----------|----------|
| <13 | LOW | 1.0× | 100% | 1.0× |
| 13-18 | NORMAL | 1.0× | 100% | 1.0× |
| 18-25 | HIGH | 1.10× | 75% | 1.15× |
| >25 | EXTREME | 1.25× | 50% | 1.30× |
| >32 | PANIC | Watcher blocked entirely | — | — |

---

## 8. RISK LIMITS (HARD)

| Parameter | Value |
|-----------|-------|
| Capital | ₹5,00,000 |
| Max Positions | 80 |
| Max Daily Loss | 20% of capital |
| Max Units Per Trade | 30,000 |
| Min Option Premium | ₹3 |
| Portfolio Profit Target | 15% unrealized → close all |
| No New Trades After | 15:10 IST |
| Force Exit All | 15:25 IST |
| Credit Spread Min Score | 65 |
| Debit Spread Min Score | 65 |
| Naked Buy Min Score | 66 |

---

## 9. DATA PERSISTENCE

### SQLite Database (`titan_state.db`)
- **WAL mode** — crash-safe, concurrent reads
- Tables: `active_trades`, `daily_state`, `exit_states`, `risk_state`, `order_idempotency`, `data_health`, `slippage_log`, `scan_decisions`, `live_pnl`, `trades`
- The `trades` table is the **queryable trade ledger** — one row per trade lifecycle

### Trade Ledger (JSONL)
- `trade_ledger/trade_ledger_YYYY-MM-DD.jsonl` — append-only, one JSON per line
- Events: ENTRY, EXIT, CONVERSION, SCAN
- Each ENTRY/EXIT is also synced to SQLite `trades` table

### Query CLI
```bash
python trade_query.py                    # Today's summary
python trade_query.py 2026-03-09         # Specific date
python trade_query.py --week             # Last 7 days
python trade_query.py --month            # Last 30 days
python trade_query.py --source SNIPER    # Filter by source
python trade_query.py --underlying ONGC  # Filter by stock
python trade_query.py --backfill         # Import JSONL → SQLite
```

### Logs
- `logs/titan.log` — rotating log (can grow to 160K+ lines)
- Contains all print statements, trade decisions, P&L updates

---

## 10. EXECUTION PIPELINE (Signal → Trade → Exit)

```
1. SCAN (every 5 min + real-time watcher)
   ├─ ~200 F&O stocks on WebSocket
   └─ Watcher: continuous breakout detection

2. SCORING
   ├─ ORB strength, VWAP, momentum, volume, ADX, RSI
   └─ Score: 0-100

3. ML GATES (parallel)
   ├─ P(move) ≥ floor (0.40-0.65 by setup)
   ├─ DR anomaly check (dr_score threshold)
   └─ Score boost/penalty applied

4. STRATEGY ROUTING
   ├─ Score ≥76 → ELITE auto-fire
   ├─ Credit Spread (theta-positive, score ≥65)
   ├─ Debit Spread (momentum, score ≥65)  
   ├─ Naked Buy (fallback, score ≥66)
   └─ Sniper (high-conviction GMM, 3× lots)

5. EXECUTION GUARD
   ├─ IV crush check (IV/RV ratio)
   ├─ Theta entry gate (theta/premium ratio)
   ├─ Position sizing (tier × VIX adjustment)
   └─ Idempotency check (no duplicate orders)

6. ORDER PLACEMENT
   ├─ Kite API (paper or live)
   ├─ GTT safety net (server-side SL+target)
   └─ Trade ledger ENTRY logged

7. MONITORING
   ├─ Real-time LTP via WebSocket
   ├─ Exit checks every 5-min candle
   ├─ THP intercept on SL/time stops
   └─ Thesis re-validation (GCR)

8. EXIT
   ├─ Trade ledger EXIT logged
   ├─ P&L accumulated
   └─ State persisted to SQLite
```

---

## 11. DEPLOYMENT

### EC2 Commands
```bash
ssh titan-bot                                      # Connect
sudo systemctl restart titan-bot                   # Restart
sudo systemctl status titan-bot                    # Status
journalctl -u titan-bot -f                         # Live logs
tail -f /home/ubuntu/titan/logs/titan.log          # App logs
```

### File Sync (Local → EC2)
```bash
scp local_file.py titan-bot:/home/ubuntu/titan/agentic_trader/
```

### Dashboard
```bash
python dashboard.py   # Flask SSE on port 5000
```

---

## 12. KNOWN ISSUES & GOTCHAS

1. **Trade state is in-memory** — `self.tools.paper_positions` list. Persisted to SQLite on every change, but between restarts only SQLite state is restored. If crash happens mid-trade, the last saved state is used.

2. **Log file grows unbounded** — `titan.log` can reach 164K+ lines spanning multiple days. Use `trade_query.py` instead of grepping logs.

3. **THP can amplify losses** — Converting to debit spread doesn't guarantee recovery. If spread expires at time exit (15:05), the full spread loss is realized. This was the #1 loss source on 2026-03-09.

4. **IV_CRUSH is non-hedgeable** — THP explicitly skips IV_CRUSH exits. The trade is closed immediately.

5. **Restarts reset counters** — Win/loss counters in the running log reset on each restart. Use trade ledger for accurate daily counts.

6. **OI data quality** — DhanHQ spot merge can fail for some stocks, producing garbage features. The OI quality gate (predictor.py) catches this and zeros the score boost.

---

## 13. COMMON ANALYSIS TASKS

### "What happened today?"
```bash
python trade_query.py
```

### "Show me SNIPER trades this week"
```bash
python trade_query.py --week --source GMM_SNIPER
```

### "How many THP conversions today?"
Check `thp_count` in daily summary, or:
```bash
python trade_query.py --exit-types
```

### "Backfill historical data into SQLite"
```bash
python trade_query.py --backfill
```

---

*This document is the single source of truth for Titan's architecture. Update it when making changes.*
