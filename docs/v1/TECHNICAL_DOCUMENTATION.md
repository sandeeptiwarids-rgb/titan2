# TITAN — Autonomous Options Trading System
## Technical Documentation v2.0

> **Goal:** ₹2L → ₹20L (10x returns) via fully autonomous intraday options trading on NSE F&O
> **Capital:** ₹2,00,000 | **Mode:** Paper Trading | **Entry Point:** `python autonomous_trader.py --capital 200000`
> **LLM:** OpenAI GPT-4o | **Broker:** Zerodha Kite Connect

---

## Table of Contents
1. [Quick Start](#1-quick-start)
2. [System Overview](#2-system-overview)
3. [Architecture](#3-architecture)
4. [Data Pipeline — Indicator Calculation to Scoring](#4-data-pipeline)
5. [Core Modules](#5-core-modules)
6. [Scoring Systems — Summary](#6-scoring-systems--summary)
7. [Risk Management](#7-risk-management)
8. [Order Execution](#8-order-execution)
9. [Safety Gates — Summary](#9-safety-gates--summary)
10. [LLM Agent](#10-llm-agent)
11. [Market Scanner](#11-market-scanner)
12. [Configuration Reference](#12-configuration-reference)
13. [Thread Safety & Persistence](#13-thread-safety--persistence)
14. [File Structure](#14-file-structure)
15. [Known Issues (Open)](#15-known-issues-open)
16. [Version History & Changelog](#16-version-history--changelog)
17. [Debugging & Troubleshooting](#17-debugging--troubleshooting)

---

## 1. Quick Start

### Prerequisites
- Python 3.10+
- Zerodha Kite Connect API credentials
- OpenAI API key (GPT-4o access)
- Dependencies: `pip install -r requirements.txt`

### Environment Setup
Create `.env` in `agentic_trader/`:
```env
OPENAI_API_KEY=sk-...
ZERODHA_API_KEY=your_api_key
ZERODHA_API_SECRET=your_api_secret
```

### Running the Bot
```bash
cd agentic_trader
python autonomous_trader.py --capital 200000
```

### Pre-Market Checklist (Every Day Before 9:15 AM)
1. **Refresh Kite auth token** — run `python quick_auth.py` or login to Kite web
2. Ensure `.env` has valid `ZERODHA_API_KEY` and `ZERODHA_API_SECRET`
3. Check `risk_state.json` — `daily_pnl` auto-resets on new date via `_check_new_day()`
4. Check `active_trades.json` — should show `"active_trades": []` (no overnight positions)
5. Start the bot before 9:20 AM (first scan at `TRADING_HOURS["start"] = "09:20"`)

---

## 2. System Overview

TITAN is an **AI-powered autonomous trading agent** that:
- Uses **OpenAI GPT-4o** for reasoning and strategy decisions
- Executes trades via **Zerodha Kite Connect API**
- Specializes in **intraday options** on NSE F&O stocks (18-stock tiered universe + 7 wild-cards)
- Implements a **multi-gate safety pipeline** to prevent "risk of ruin"
- Runs two concurrent loops: **5-min scan cycle** (GPT agent) + **3-sec monitor daemon** (exits/SL/targets)

### Core Philosophy
```
"Price escapes balance and does not come back."
- Don't predict, FOLLOW the trend
- Trend is your friend until it bends
- Add to winners, cut losers fast
- Let profits run with trailing stops
```

### Paper Trading Results (as of 10 Feb 2026)

| Day | Date | Trades | W/L | Day P&L | Cumulative |
|-----|------|--------|-----|---------|------------|
| Day 1 | 6 Feb 2026 | 8 | 4W/4L | +₹1,37,872 | +₹1,37,872 |
| Day 2 | 9 Feb 2026 | 8 | 7W/0L | +₹4,441 | +₹1,42,313 |
| Day 3 | 10 Feb 2026 | 27 | 12W/14L | +₹3,438 | +₹1,45,751 |

---

## 3. Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│              AUTONOMOUS TRADER (Main Thread)                     │
│                autonomous_trader.py                              │
│    schedule.every(5).minutes → scan_and_trade() → GPT agent     │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐         │
│  │ Market       │──▶│ LLM Agent    │──▶│ Zerodha      │         │
│  │ Scanner      │   │ (GPT-4o)     │   │ Tools        │         │
│  │ ~200 stocks  │   │ 10 tools     │   │ 2 order paths│         │
│  └──────────────┘   └──────────────┘   └──────────────┘         │
│                            │                  │                  │
│                            ▼                  ▼                  │
│  ┌─────────────────────────────────────────────────────┐        │
│  │            MULTI-GATE SAFETY PIPELINE               │        │
│  │  Risk Governor → Data Health → Correlation Guard    │        │
│  │  → Idempotent Engine → Regime Score → Execution     │        │
│  │  → Intraday Scorer → Microstructure → Premium Cap   │        │
│  └─────────────────────────────────────────────────────┘        │
│                            │                                     │
│                            ▼                                     │
│  ┌─────────────────────────────────────────────────────┐        │
│  │          ZERODHA KITE CONNECT API                   │        │
│  │  (Orders, Positions, Quotes, Option Chains)         │        │
│  └─────────────────────────────────────────────────────┘        │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│              MONITOR DAEMON (Background Thread)                  │
│         threading.Thread(daemon=True) — every 3 seconds          │
├──────────────────────────────────────────────────────────────────┤
│  check_and_update_trades() → SL/Target hits                      │
│  exit_manager.check_exit() → Time stops, trailing, speed gate    │
│  Thread-safe via threading.RLock() on paper_positions             │
└──────────────────────────────────────────────────────────────────┘
```

### Two Order Paths

| Path | Function | Used For | Key Gates |
|------|----------|----------|-----------|
| **Options** | `place_option_order()` | All F&O stocks (primary) | 10 gates: trading hours → risk governor → recon → data health → correlation → duplicate underlying → intraday scorer → microstructure → premium cap |
| **Equity** | `place_order()` | Non-F&O stocks only | 9 gates: recon → data health → idempotent → duplicate → correlation → regime score → validate → execution guard → adaptive sizing |

---

## 4. Data Pipeline

This is the **most critical section** for understanding how market data flows from raw candles to trade decisions. Bugs here have historically been the #1 source of scoring failures.

### 4.1 Pipeline Overview
```
Zerodha Kite API (OHLCV candles)
        │
        ▼
┌────────────────────────────┐
│ _calculate_indicators()    │  zerodha_tools.py
│ Returns: dict of 50 keys   │  (raw indicator computation)
└────────────┬───────────────┘
             │
             ▼
┌────────────────────────────┐
│ MarketData dataclass       │  zerodha_tools.py
│ 60 typed fields            │  (boundary — drops anything not mapped!)
│ Constructor maps each key   │
└────────────┬───────────────┘
             │
             ▼ asdict()
┌────────────────────────────┐
│ market_data dict           │  (Python dict — passed everywhere)
│ All 60 fields as keys      │
└────┬───────┬───────┬───────┘
     │       │       │
     ▼       ▼       ▼
   GPT    Scorer   Trend
  Prompt  (100pt)  Engine
```

### 4.2 Indicator Keys — Complete List (50 keys)

Every key computed by `_calculate_indicators()` and what consumes it:

| # | Key | Type | Description | Consumed By |
|---|-----|------|-------------|-------------|
| 1 | `sma_20` | float | 20-day SMA | MarketData only |
| 2 | `sma_50` | float | 50-day SMA | MarketData only |
| 3 | `rsi_14` | float | 14-period RSI | GPT prompt, Options scorer |
| 4 | `atr_14` | float | 14-period ATR | IntradaySignal.atr, exit manager |
| 5 | `trend` | str | BULLISH/BEARISH/SIDEWAYS | GPT prompt |
| 6–8 | `high_20d`, `low_20d`, `high_5d`, `low_5d` | float | Range bounds | MarketData |
| 9–11 | `prev_high`, `prev_low`, `prev_close` | float | Previous day levels | MarketData |
| 12–15 | `resistance_1/2`, `support_1/2` | float | S/R levels | GPT prompt |
| 16–17 | `atr_target`, `atr_stoploss` | float | ATR-based exit levels | MarketData |
| 18 | `volume_ratio` | float | Today vs 5d avg volume | Trend engine |
| 19 | `volume_signal` | str | HIGH/NORMAL/LOW | MarketData |
| 20 | `vwap` | float | VWAP value | GPT, regime scorer, trend engine |
| 21 | `vwap_slope` | str | RISING/FALLING/FLAT | GPT, options scorer |
| 22 | `price_vs_vwap` | str | ABOVE/BELOW/AT_VWAP | GPT, options scorer |
| 23–24 | `ema_9`, `ema_21` | float | EMAs | GPT, trend engine |
| 25 | `ema_spread` | float | abs(EMA9-EMA21)/EMA21 % | Regime scorer |
| 26 | `ema_regime` | str | **EXPANDING / COMPRESSED / NORMAL** | All scorers, GPT, setup detection |
| 27–28 | `orb_high`, `orb_low` | float | ORB range (first 15 min) | GPT, trend engine |
| 29 | `orb_signal` | str | BREAKOUT_UP/DOWN/INSIDE_ORB | GPT, all scorers |
| 30 | `orb_strength` | float | % move from ORB | GPT, regime scorer |
| 31–33 | `volume_5d_avg`, `volume_vs_avg`, `volume_regime` | mixed | Time-normalized volume | All scorers |
| 34–35 | `chop_zone`, `chop_reason` | bool/str | Chop detection | GPT (skip stock) |
| 36 | `atr_range_ratio` | float | Range compression signal | MarketData |
| 37 | `orb_reentries` | int | ORB whipsaw count | Regime scorer |
| 38–40 | `htf_trend`, `htf_ema_slope`, `htf_alignment` | str | Higher TF signals | GPT, trend engine, regime scorer |
| 41 | `vwap_change_pct` | float | VWAP change % (numeric) | Trend engine VWAP slope |
| 42 | `orb_hold_candles` | int | Post-ORB hold count | Trend engine, GPT |
| 43 | `adx` | float | ADX-14 trend strength | Trend engine, GPT |
| 44 | `follow_through_candles` | int | Confirming candles post-breakout | Options scorer, GPT |
| 45 | `range_expansion_ratio` | float | Candle body / 5-min ATR | Options scorer, GPT |
| 46 | `vwap_slope_steepening` | bool | Acceleration signal | Options scorer, GPT |
| 47 | `vwap_distance_pct` | float | ((LTP-VWAP)/VWAP)*100 signed | Trend engine |
| 48 | `pullback_depth_pct` | float | Pullback depth from swing | Trend engine pullback scorer |
| 49 | `pullback_candles` | int | Candles in last pullback | Trend engine pullback scorer |

### 4.3 MarketData Dataclass — The Boundary

**CRITICAL CONCEPT:** `MarketData` is the typed boundary between raw indicators and the rest of the system. If an indicator key is NOT mapped in the constructor, it is **silently dropped** and every downstream consumer gets a default value.

The MarketData dataclass has **60 fields** (includes base OHLCV + all indicator fields above).

**Historical Bug Pattern:** Multiple critical bugs were caused by indicators being computed in `_calculate_indicators()` but not mapped in the MarketData constructor. This caused scoring components to always receive defaults (usually 0), making them non-discriminating. See [Version History](#16-version-history--changelog) for the full list of fixes.

### 4.4 EMA Regime Values

**Current valid values (as of v2.0):**
| Value | Meaning | Condition |
|-------|---------|-----------|
| `EXPANDING` | EMAs diverging — active trend | ema_spread > 0.04% on 5-min |
| `COMPRESSED` | EMAs converging — breakout imminent | ema_spread < 0.02% on 5-min |
| `NORMAL` | Neither expanding nor compressed | Everything else |

**Default fallback across all modules:** `"NORMAL"` (not "NEUTRAL" — that was a historical bug).

**Historical Bug:** Older code used `EXPANDING_BULL` / `EXPANDING_BEAR` / `CONTRACTING`. These strings were updated to `EXPANDING` / `COMPRESSED` / `NORMAL` in the indicator code but not updated in the GPT prompt, setup detection, and scoring code. This meant ~30 points of scoring capacity was permanently stuck at zero. Fixed in v2.0.

### 4.5 Volume Time-Normalization

**Problem:** At 9:30 AM, today's volume is naturally ~15% of full-day volume. Comparing to yesterday's full-day average always shows "LOW" until 11+ AM.

**Solution (v2.0):** Project today's volume to full-day equivalent:
```python
elapsed_minutes = (now - 9:15 AM) in minutes
projected_volume = today_volume * (375 / elapsed_minutes)  # 375 = full session
projected_volume = min(projected_volume, today_volume * 12.5)  # cap at 12.5x
```
Also excludes today from the 5-day average: `vol.iloc[-21:-1].mean()` (not `vol.tail(5).mean()`).

### 4.6 Intraday ATR Calculation

For 5-min intraday ATR used by acceleration fields:
- **≥14 candles:** Standard ATR-14 on intraday data
- **6–13 candles:** Mean of available True Ranges
- **<6 candles:** Fallback `daily_atr / 8.66` (sqrt rule: daily ATR / √75 five-min periods)

### 4.7 Pullback Detection

Computed from intraday 5-min candles after ORB breakout:
1. Find **swing high** (for BREAKOUT_UP) or **swing low** (for BREAKOUT_DOWN) in post-ORB candles
2. Measure pullback depth from swing to lowest/highest point after it
3. Count consecutive pullback candles (close moving against breakout direction)
4. Returns `pullback_depth_pct` (% depth) and `pullback_candles` (count)

If no breakout or < 6 candles: both default to 0, and pullback scorer gives neutral 50% score.

---

## 5. Core Modules

### 5.1 Trend Following Engine (`trend_following.py`)

**Purpose:** Score trend strength on a 100-point scale and determine entry/exit signals.

#### Scoring Weights (100 points — exact)
| Factor | Points | Sub-Scorer | What It Measures |
|--------|--------|------------|------------------|
| VWAP Slope | 25 | `_score_vwap_slope()` | Institutional direction (from `vwap_change_pct`) |
| EMA Expansion | 20 | `_score_ema_expansion()` | Trend acceleration (from `ema_expanding` + `ema_spread_pct`) |
| Volume Regime | 20 | `_score_volume()` | Participation validation (EXPLOSIVE/HIGH/NORMAL/LOW) |
| ORB Break Hold | 15 | `_score_orb_hold()` | Momentum confirmation (candles holding breakout) |
| Pullback Quality | 10 | `_score_pullback()` | Continuation strength (depth + duration of pullback) |
| ADX Strength | 10 | `_score_adx()` | Trend conviction (ADX-14 value) |
| **Total** | **100** | | |

#### Trend States
| State | Score Range | Trading Action |
|-------|------------|----------------|
| `STRONG_BULLISH` | ≥ 80 | Aggressive BUY entry |
| `BULLISH` | 60–79 | Standard BUY entry |
| `NEUTRAL` | 40–59 | **No trade** (penalty applied in options scorer) |
| `BEARISH` | 20–39 | Standard SELL entry |
| `STRONG_BEARISH` | 0–19 | Aggressive SELL entry |

#### Hysteresis System (Prevents Whipsaw)
| Transition | Threshold | Candles Required |
|------------|-----------|------------------|
| Upgrade to STRONG | ≥ 82 | 2 consecutive |
| Stay STRONG | ≥ 70 | — |
| Downgrade from STRONG | < 70 | Immediate |
| Upgrade to TREND | ≥ 60 | — |
| Downgrade to NEUTRAL | < 50 | Immediate |

#### Directional Point Tracking
Not all sub-scorers contribute to directional tallies:
| Sub-Scorer | Added to bullish/bearish? | Notes |
|------------|--------------------------|-------|
| VWAP Slope | YES | Based on slope direction |
| EMA | YES (when expanding) | Sign of ema_spread_pct determines direction |
| ORB Hold | YES (if hold ≥ 2) | ORB direction determines bullish/bearish |
| Volume | NO | Non-directional (inflates raw score only) |
| Pullback | NO | Non-directional |
| ADX | NO | Non-directional |

**Implication:** 40/100 points are non-directional. A high total score doesn't guarantee directional conviction — `_determine_entry()` checks both the total score AND directional balance.

#### Unused TrendSignal Fields
These are declared in the TrendSignal dataclass but **never scored** by any sub-scorer:
- `rsi_14` — declared but not consumed (RSI scoring was removed in favor of ADX)
- `htf_trend` — passed through to TrendDecision but not scored
- `orb_high`, `orb_low` — ORB scoring uses `orb_broken` and `orb_hold_candles` instead

---

### 5.2 Intraday Option Scorer (`options_trader.py :: IntradayOptionScorer`)

**Purpose:** Score intraday signals for options trading decisions on a ~115-point scale (BLOCK threshold at 30).

#### Scoring Components
| # | Component | Max Points | Source |
|---|-----------|-----------|--------|
| 0 | Trend Following | 30 | Normalized from TrendFollowing Engine (0–100 → 0–30) |
| 1 | Microstructure | 15 | Spread (6), Depth (3), OI (3), Volume (2), Penalties (-3) |
| 2 | ORB Breakout | 20 | Capped to 10 if same window as trend engine |
| 3 | Volume Regime | 15 | EXPLOSIVE=15, HIGH=12, NORMAL=6, LOW=2 |
| 4 | VWAP Position | 10 | Aligned=10, Partial=7, At VWAP=3, Misaligned=-8 |
| 5 | EMA Regime | 10 | EXPANDING=10, COMPRESSED=8, NORMAL=3 |
| 5b | HTF Alignment | 5 | BULLISH/BEARISH=5, NEUTRAL=0 |
| 7 | Acceleration | 10 | Follow-through (4), Range expansion (3), VWAP steepening (3) |
| | RSI Penalty | -2 | RSI < 20 or > 80 |
| | NEUTRAL Trend | -5 | When trend state is NEUTRAL |
| | Chop Zone | -12 | When chop_zone = True |
| 8 | Exhaustion Filter | -51 to +10 | Gap detection, chase detection, volume confirmation |
| | Caller Direction | +5 | When GPT direction aligns with computed direction |

#### Trade Tiers
| Score | Decision | Strike | Size |
|-------|----------|--------|------|
| < 30 | **BLOCK** — no trade | — | — |
| 30–49 | Standard | ATM/ITM | 1.0x |
| 50–54 | Standard+ | ATM/ITM | 1.0x |
| ≥ 55 | Premium | ATM/ITM | Up to 1.2x (if accel ≥ 8 AND micro ≥ 12) |

#### Anti-Double-Counting
TrendFollowing and OptionScorer may score the same signal twice. Solution: **window overlap detection** — if the ORB/Volume scoring window overlaps with TrendFollowing's window (within 5 min), the duplicate component is capped at half points.

#### IntradaySignal Fields — Used vs Unused
| Field | Used by Scorer? |
|-------|----------------|
| `orb_signal`, `vwap_position`, `vwap_trend`, `ema_regime` | YES |
| `volume_regime`, `rsi`, `htf_alignment`, `chop_zone` | YES |
| `follow_through_candles`, `range_expansion_ratio`, `vwap_slope_steepening` | YES |
| **`price_momentum`** | **NEVER** — always 0, dead field |
| **`atr`** | **NEVER** — populated from `atr_14` but not consumed by scorer |

---

### 5.3 Regime Scorer (`regime_score.py`)

**Purpose:** Confidence gate for trade quality. Calculates a 0–100 score (clamped) with strategy-specific minimum thresholds.

#### Minimum Score by Strategy
| Trade Type | Min Score |
|------------|-----------|
| ORB Breakout | 70 |
| EMA Squeeze | 65 |
| Mean Reversion | 65 |
| Momentum | 60 |
| VWAP Trend | 60 |
| EOD Play | 55 |

---

### 5.4 Exit Manager (`exit_manager.py`)

**Purpose:** First-class exit logic for consistent edge preservation.

#### Exit Types (Priority Order)
| Type | Trigger | Action |
|------|---------|--------|
| Hard Stop Loss | LTP ≤ SL (BUY) or LTP ≥ SL (SELL) | Immediate exit |
| Session Cutoff | 15:15 IST | Exit all positions |
| Speed Gate | After 4 candles: premium < +12% AND R < 0.3 | Kill slow option |
| Time Stop | No +0.5R in 7 candles (~35 min) | Exit position |
| Break-even | +0.8R achieved | Move SL to entry |
| Trailing Stop | After +1R, trail at 50% of max profit | Lock in profits |
| Target Hit | 2R or 3R target | Take profit |

#### R-Multiple Tracking
```
R = abs(Entry - Stop Loss)
+0.5R = Time stop threshold (must show progress)
+0.8R = Move stop to break-even
+1.0R = Activate trailing stop
+2.0R = First profit target
+3.0R = Second profit target
```

#### Speed Gate (Options-Specific)
After **4 candles (20 min)**, if option premium hasn't gained **+12%** AND R-multiple < **0.3**, the trade is killed as a slow mover. Both conditions must be true (dual check prevents exiting profitable but slow trades). Logged to `speed_gate_log.jsonl`.

---

### 5.5 Microstructure Gate (`options_trader.py`)

**Purpose:** Hard gate for options tradability — prevents losses from poor execution.

#### Tick-Based Spread Evaluation
Old method (% spread) was broken — ₹0.10 on a ₹10 option = 1% ("wide") but it's just 2 ticks (excellent).

New method measures in **ticks** (market quality) + **impact cost** (P&L drag):
```
Tick size = ₹0.05
Excellent: 1–2 ticks (₹0.10 max) → +6 pts
Good: 3–4 ticks (₹0.20 max) → +4 pts
BLOCK: depends on premium bucket (6 ticks for <₹20, 4 ticks for >₹75)
```

#### Depth Normalization
Kite API returns depth in **shares**. TITAN normalizes to **lots**:
```python
bid_lots = bid_qty / lot_size  # e.g., 5500 shares / 5500 lot = 1 lot
# MIN_DEPTH_LOTS = 2 → need ≥ 2 lots at best bid/ask
```

---

## 6. Scoring Systems — Summary

TITAN has **three independent scorers** that evaluate different aspects of a trade:

| Scorer | File | Total Points | Purpose | When Used |
|--------|------|-------------|---------|-----------|
| TrendFollowing | `trend_following.py` | 100 (exact) | Trend strength + direction | Fed INTO options scorer as 30pts |
| IntradayOptionScorer | `options_trader.py` | ~115 positive max (BLOCK at 30) | Trade quality for options | Gate for every option order |
| RegimeScorer | `regime_score.py` | 130 positive max (clamped 0–100) | Confidence gate by strategy | Gate for equity orders |

### How They Interact
```
TrendFollowingEngine.analyze_trend()              # 100-pt scale
        │
        │ normalized to 0–30
        ▼
IntradayOptionScorer.score_intraday_signal()       # ~115-pt scale
        │
        │ score >= 30 required
        ▼
   TRADE or BLOCK
```

The TrendFollowing score is the **foundation** — it provides 30 of the ~115 available points in the options scorer. A `NEUTRAL` trend (40–59 trend score) gets normalized to 12–18 out of 30, plus a **-5 penalty** — making it hard (but not impossible) to pass the BLOCK threshold of 30 from intraday signals alone.

---

## 7. Risk Management

### 7.1 Risk Governor (`risk_governor.py`)

**Purpose:** Account-level risk with kill-switch capability.

#### System States
| State | Description | Recovery |
|-------|-------------|----------|
| `ACTIVE` | Normal trading | — |
| `COOLDOWN` | 10-min pause after loss | Auto-clears after cooldown expires |
| `HALT_TRADING` | Stopped for the day | `_check_new_day()` resets next morning |
| `CIRCUIT_BREAK` | Emergency stop | Manual intervention |

#### Risk Limits
| Limit | Value | On ₹2L Capital |
|-------|-------|-----------------|
| Max daily loss | 3.0% | ₹6,000 |
| Max consecutive losses | 3 | Halt |
| Max trades per day | 8 | — |
| Cooldown after loss | 10 min | — |
| Max position size | 25% of capital | ₹50,000 |
| Max total exposure | 80% of capital | ₹1,60,000 |

#### New Day Auto-Reset
`_check_new_day()` runs at the start of every `is_trading_allowed()` call. If the date has changed:
- `daily_pnl` → 0
- `trades_today` → 0
- `consecutive_losses` → 0 (fresh start)
- `system_state` → `ACTIVE`

**Persistence:** Saved to `risk_state.json` using absolute pathing (`os.path.dirname(__file__)`).

### 7.2 Correlation Guard (`correlation_guard.py`)

**Purpose:** Prevent hidden overexposure from correlated positions.

#### Beta Categories
| Category | Example Stocks |
|----------|----------------|
| HIGH_BETA (>1.2) | BAJFINANCE, TATASTEEL, HINDALCO, JSWSTEEL, AXISBANK |
| MEDIUM_BETA (0.8–1.2) | RELIANCE, HDFCBANK, LT, TITAN |
| LOW_BETA (<0.8) | INFY, TCS, SUNPHARMA, ITC |

**Rules:**
- Max **2 HIGH_BETA** positions simultaneously
- If holding index options → max **1 additional HIGH_BETA** stock

---

## 8. Order Execution

### 8.1 Option Order Gate Sequence (10 gates)
```
1. F&O Eligibility           → Is underlying in FNO_LOT_SIZES?
2. Trading Hours Check       → Between 09:20 and 15:05?
3. Risk Governor             → Daily loss, consecutive losses, cooldown OK?
4. Position Reconciliation   → Local state synced with broker?
5. Data Health Gate          → Market data fresh and valid?
6. Correlation Guard         → Not too many correlated positions?
7. Duplicate Underlying      → No existing option on same underlying?
8. Intraday Option Scorer    → Score ≥ 30 (BLOCK_THRESHOLD)?
9. Premium Cap Check         → Single trade ≤ ₹1,00,000? Total ≤ ₹2,00,000?
10. Microstructure Gate      → Spread, depth, OI all acceptable?
```

### 8.2 Equity Order Gate Sequence (9 gates)
```
1. Position Reconciliation   → Is local state synced with broker?
2. Data Health Gate          → Is market data fresh and valid?
3. Idempotent Order Engine   → Has this exact order been placed already?
4. Duplicate Symbol Check    → Already holding this symbol?
5. Correlation Guard         → Too many correlated positions?
6. Regime Scorer             → Is trade quality above threshold?
7. Validate Trade            → Do SL/target/risk meet hard rules?
8. Execution Guard           → Is spread acceptable? What order type?
9. Adaptive Position Sizing  → Adjust quantity for slippage/volume
```

### 8.3 Execution Guard (`execution_guard.py`)
| Spread | Action |
|--------|--------|
| < 0.3% | MARKET order |
| 0.3–0.5% | LIMIT with 30s timeout |
| > 0.5% | **BLOCK** |
| > ₹5 absolute | **BLOCK** |

### 8.4 Idempotent Order Engine
```
Client Order ID format: SYMBOL_YYYYMMDD_STRATEGY_DIRECTION_HASH
Example: RELIANC_20260210_ORB_B_a1b2c3d4
```
Prevents duplicate orders on reconnect/retry. Persisted to `order_idempotency.json`.

### 8.5 Position Sizing (Options)
| Parameter | Value |
|-----------|-------|
| Max premium per trade | 50% of capital (₹1,00,000) |
| Max premium per day | 100% of capital (₹2,00,000) |
| Max lots per trade | 10 |
| Risk per trade | 2.5% of capital (₹5,000) |

**Capital Guard:** If single lot costs > 50% of capital → **BLOCK** (no trade). Prevents catastrophic single-position exposure.

---

## 9. Safety Gates — Summary

| Gate | Block Condition | Module |
|------|-----------------|--------|
| Trading Hours | Before 09:20 or after 15:05 | config.py |
| Risk Governor | Daily loss > 3%, 3 consecutive losses, cooldown | risk_governor.py |
| Position Recon | Local/broker mismatch | position_reconciliation.py |
| Data Health | Stale > 60s, zero volume, NaN indicators | data_health_gate.py |
| Correlation | > 2 HIGH_BETA positions | correlation_guard.py |
| Execution Guard | Spread > 0.5%, LOW volume | execution_guard.py |
| Intraday Scorer | Score < 30 | options_trader.py |
| Microstructure | Spread > wide threshold, depth < 2 lots, OI < 500 | options_trader.py |
| Duplicate | Already holding option on same underlying | zerodha_tools.py |
| Premium Cap | Single trade > ₹1,00,000 or total > ₹2,00,000 | zerodha_tools.py |
| Capital Guard | Single lot > 50% of capital | options_trader.py |
| Idempotent | Same order ID already placed | idempotent_order_engine.py |

---

## 10. LLM Agent

### Configuration
| Parameter | Value |
|-----------|-------|
| Model | `gpt-4o` (set in `llm_agent.py:388`) |
| Max tokens per response | 4096 |
| Max iterations per scan | 10 |
| Auto-execute | `True` (autonomous mode) |

### Available Tools (10)
| Tool | Purpose |
|------|---------|
| `get_account_state` | Account margins, positions, P&L, trading permission |
| `get_market_data` | OHLCV + all 50 indicators for specified symbols |
| `calculate_position_size` | Safe sizing based on entry/SL/capital |
| `validate_trade` | Check against all hard rules |
| `get_portfolio_risk` | Current portfolio risk exposure |
| `place_order` | Equity order (9-gate pipeline) |
| `get_options_chain` | Option chain with ATM strikes + recommendation |
| `place_option_order` | F&O order (10-gate pipeline) |
| `get_volume_analysis` | Futures OI analysis for EOD momentum plays |
| `get_oi_analysis` | Detailed OI breakdown for single stock |

### GPT Prompt — What GPT Sees Per Stock
The detailed technicals view (built in `autonomous_trader.py` scan loop) shows GPT:
```
SYMBOL: LTP=₹X (+Y%) Trend=BULLISH
  RSI=65 ATR=25 ADX=35
  VWAP: Above | Slope: RISING | EMA: EXPANDING
  ORB: BREAKOUT_UP (strength=45%) | Hold: 4 candles
  Vol: HIGH (2.1x avg) | HTF: BULLISH
  Chop: No
  Quality: FollowThru=3 | RangeExp=1.5 | VWAPSteep=Y
  S1=₹X S2=₹X | R1=₹X R2=₹X
```

### Argument Validation
Before executing any order tool, the agent validates:
- `symbol`: Required, auto-prepends `NSE:` if missing
- `quantity`: Integer 1–50,000
- `side`/`direction`: Must be `"BUY"` or `"SELL"`
- `strike_selection`: One of `{ATM, OTM1, OTM2, ITM1, ITM2}`

---

## 11. Market Scanner

### Purpose
Scans the **entire NSE F&O universe (~200 stocks)** every cycle and surfaces up to **7 wild-card** opportunities beyond the fixed 18-stock universe.

### Scanner Config
| Parameter | Value | Description |
|-----------|-------|-------------|
| `max_wildcards` | 7 | Max wild-card stocks per scan |
| `min_change_pct` | 1.5% | Min absolute % change to flag |
| `min_volume_ratio` | 2.0x | Min volume vs session average |
| `breakout_proximity_pct` | 0.3% | Within 0.3% of day-high |
| `max_lot_value_lakh` | 15 | Skip if lot value > ₹15 lakh |

### Metal Momentum Boost
- 2 wild-card slots reserved for metals when metal sector is hot (avg change > 1.5%)
- +20 score boost for metal stocks on hot metal days
- Metal symbols: TATASTEEL, JSWSTEEL, JINDALSTEL, HINDALCO, HINDZINC, VEDL, NMDC, NATIONALUM, COALINDIA, etc.

---

## 12. Configuration Reference

### Hard Rules (`config.py :: HARD_RULES`)
```python
RISK_PER_TRADE = 0.025     # 2.5% max risk per trade (₹5,000 on ₹2L)
MAX_DAILY_LOSS = 0.03      # 3% max daily loss (₹6,000 on ₹2L)
MAX_POSITIONS  = 5         # Max 5 simultaneous (prompt guides to 3)
STALE_DATA_SECONDS = 60    # Data freshness threshold
API_RATE_LIMIT_MS  = 500   # Min ms between API calls
CAPITAL = 200000           # Starting capital
```

### Trading Hours
```python
start       = "09:20"     # 5 min after open
end         = "15:20"     # 10 min before close
no_new_after = "15:05"    # No new trades (10 min buffer)
```

### Tiered Universe (18 fixed stocks)

**Tier 1 — Always Eligible (10 stocks):**
SBIN, HDFCBANK, ICICIBANK, AXISBANK, KOTAKBANK, BAJFINANCE, RELIANCE, BHARTIARTL, INFY, TCS

**Tier 2 — Conditional, need trend_score ≥ 60 (8 stocks):**
TATASTEEL, JSWSTEEL, JINDALSTEL, HINDALCO, LT, MARUTI, TITAN, SUNPHARMA

### F&O Lot Sizes
Complete mapping in `options_trader.py :: FNO_LOT_SIZES` — 50+ stocks with lot sizes.
Key ones: NIFTY=65, BANKNIFTY=30, SBIN=750, RELIANCE=500, INFY=400, HDFCBANK=550.

---

## 13. Thread Safety & Persistence

### Two Threads
| Thread | Function | Interval |
|--------|----------|----------|
| Main | GPT scan loop → `scan_and_trade()` | Every 5 min |
| Monitor Daemon | `check_and_update_trades()` → SL/target/exit manager | Every 3 sec |

### RLock Protection
Both threads access `paper_positions`. Protected by `threading.RLock()` (reentrant, because `check_and_update_trades()` calls `update_trade_status()` which also acquires the lock).

### Persistence Files
| File | Purpose | Writer |
|------|---------|--------|
| `active_trades.json` | Open positions (date-partitioned) | RLock-protected |
| `trade_history.json` | All closed trades (append-only) | via `update_trade_status` |
| `risk_state.json` | Risk governor state | Single-writer |
| `exit_manager_state.json` | Exit manager trade states | Single-writer |
| `order_idempotency.json` | Dedup engine state | Single-writer |
| `slippage_log.json` | Execution quality records | Single-writer |
| `speed_gate_log.jsonl` | Speed gate decisions | Append-only |
| `data_health_state.json` | Data health state | Single-writer |
| `reconciliation_state.json` | Position recon state | Single-writer |
| `trade_decisions.log` | Indicator snapshots per trade | Append-only |

---

## 14. File Structure

```
agentic_trader/
├── autonomous_trader.py         # MAIN: scan loop + monitor daemon + GPT prompt construction
├── config.py                    # Hard rules, universe, trading hours, GPT system prompt
├── llm_agent.py                 # GPT-4o agent wrapper + 10 tool definitions
├── zerodha_tools.py             # Kite API tools + MarketData dataclass + indicator calculation
│                                #   _calculate_indicators() — 50 indicator keys
│                                #   MarketData — 60 fields (typed boundary)
│                                #   place_order() — 9-gate equity pipeline
│                                #   place_option_order() — 10-gate options pipeline
├── trend_following.py           # TrendFollowingEngine — 100-pt trend scorer (6 sub-scorers)
├── options_trader.py            # IntradayOptionScorer — ~115-pt options scorer
│                                #   IntradaySignal dataclass
│                                #   OptionMicrostructure dataclass
│                                #   OptionPositionSizer
│                                #   FNO_LOT_SIZES
├── market_scanner.py            # F&O universe scanner (~200 stocks → 7 wild-cards)
├── regime_score.py              # Strategy-specific confidence scorer (0–100 clamped)
├── exit_manager.py              # Exit logic: SL/trailing/time stop/speed gate/session cutoff
├── execution_guard.py           # Order quality: spread/slippage/order type selection
├── risk_governor.py             # Account-level risk: daily loss, cooldown, halt, circuit break
├── correlation_guard.py         # Beta/sector correlation guard
├── position_reconciliation.py   # Broker ↔ local state sync
├── data_health_gate.py          # Data quality gate (staleness, NaN, zero volume)
├── idempotent_order_engine.py   # Duplicate order prevention
├── dashboard.py                 # Web dashboard
├── run.py                       # Alternative entry point
├── quick_auth.py                # Kite auth token refresh helper
├── candle_replay.py             # EMA regime backtest tool
├── check_pnl.py                 # P&L verification utility
├── .env                         # API keys (not committed)
├── TECHNICAL_DOCUMENTATION.md   # THIS FILE
├── active_trades.json           # Current positions (auto-generated)
├── trade_history.json           # All closed trades (auto-generated)
├── risk_state.json              # Risk governor state (auto-generated)
├── trade_decisions.log          # Per-trade indicator snapshots (auto-generated)
└── templates/                   # HTML templates for dashboard
```

---

## 15. Known Issues (Open)

### Priority 2 (Moderate Impact)
| # | Issue | Module |
|---|-------|--------|
| P2-1 | Paper mode hard-codes 1% SL / 1.5% target for equity, ignoring GPT values | zerodha_tools.py |
| P2-2 | Auto-retry regex can match symbols in GPT's natural language | autonomous_trader.py |
| P2-3 | 3x redundant `get_market_data()` calls per order (~4.5s latency) | zerodha_tools.py |
| P2-4 | `candles_since_entry` requires external increment — can drift | exit_manager.py |
| P2-5 | NFO symbols bypass beta mapping in correlation guard | correlation_guard.py |
| P2-6 | Conversation grows unbounded (no trimming) | llm_agent.py |
| P2-7 | Options scorer WEIGHTS dict sums to 115 not 100 (works because threshold is 30) | options_trader.py |
| P2-8 | Exhaustion filter (-51 to +10) is undocumented in WEIGHTS and can dominate scoring | options_trader.py |
| P2-9 | RegimeScorer base positive sum is 130, clamped to 100 — compresses discrimination | regime_score.py |

### Priority 3 (Low Impact)
| # | Issue | Module |
|---|-------|--------|
| P3-1 | Dead code: `monitor_positions()`, `_exit_position()`, `self.positions` | autonomous_trader.py |
| P3-2 | `starting_capital` never resets daily in risk governor | risk_governor.py |
| P3-3 | Sector names differ: `BANKING` vs `BANK` between modules | correlation_guard.py |
| P3-4 | `IntradaySignal.price_momentum` never populated (always 0) — dead field | options_trader.py |
| P3-5 | `IntradaySignal.atr` populated but never consumed by scorer | options_trader.py |
| P3-6 | TrendSignal `rsi_14` and `htf_trend` declared but never scored by any sub-scorer | trend_following.py |
| P3-7 | `RegimeScorer._score_ema_expansion()` direction param has zero effect | regime_score.py |
| P3-8 | Paper option exits (speed gate, time stop) may not trigger through monitor | zerodha_tools.py |

---

## 16. Version History & Changelog

### v2.0 — Deep Audit & Data Flow Fixes (10 Feb 2026)

**Root Cause Analysis:** Day 3 showed 27 trades but only 12 wins (+₹3,438). Deep analysis revealed the scoring pipeline had 55/100 trend points permanently broken — indicators were computed but silently dropped at the MarketData boundary, causing scorers to always receive defaults.

#### Scoring Pipeline Fixes (9 original + 6 data flow + 8 string/consistency)

**Data Flow Fixes (indicators computed but never reaching scorers):**
1. **6 dropped MarketData fields** — `vwap_change_pct`, `orb_hold_candles`, `adx`, `follow_through_candles`, `range_expansion_ratio`, `vwap_slope_steepening` were computed by `_calculate_indicators()` but NOT mapped in the MarketData constructor. All downstream consumers got default values.
2. **3 new indicators added** — `vwap_distance_pct`, `pullback_depth_pct`, `pullback_candles` were consumed by TrendFollowing but never computed. Pullback scorer always returned "NO_DATA" (half marks). Now computed from intraday 5-min candles.
3. **IntradaySignal.atr** — now populated from `atr_14` (was always 0).

**EMA Regime String Mismatch (8 fixes across 6 files):**
4. EMA regime values changed from `EXPANDING_BULL`/`EXPANDING_BEAR`/`CONTRACTING` to `EXPANDING`/`COMPRESSED`/`NORMAL` — but the GPT prompt, setup detection, and scoring code still referenced old strings. ~30 points of scoring capacity permanently stuck at zero.
5. 5× `"NEUTRAL"` → `"NORMAL"` default fallbacks (NEUTRAL was never a valid EMA regime value).
6. Dead `CONTRACTING` branch removed from options scorer (never matched, lost 1 point).
7. Fragile `'EXPANDING' in ema_regime` → exact `== 'EXPANDING'` match.

**Indicator Computation Fixes:**
8. **Volume time-normalization** — projects today's partial volume to full-day equivalent (capped at 12.5x). Excludes today from 5-day average.
9. **Intraday ATR fallback** — uses mean of available TRs for 6-13 candles, `daily_atr / 8.66` as ultimate fallback (sqrt rule).
10. **ADX computation** — was returning 0 (not computed). Now uses standard ADX-14 formula.
11. **Pullback detection** — new computation from intraday 5-min candles post-ORB breakout.

**Scoring Logic Fixes:**
12. VWAP section max: 8 → **10** points (matching WEIGHTS declaration).
13. WEIGHTS dict `rsi_penalty`: -3 → **-2** (matching actual code).
14. Pullback display: added `NO_DATA` and `ACCEPTABLE` quality messages (were silent).

**GPT Prompt Updates:**
15. Added ADX, ORB hold candles, follow-through, range expansion, VWAP steepening to per-stock view.
16. Added quality filter guidance (ADX>25, ORB Hold>2, FollowThru>2, RangeExp>1.0, VWAPSteep).
17. Updated EMA regime references to EXPANDING/COMPRESSED/NORMAL.

**Integration Test Results (synthetic):**
| Signal | Before v2.0 | After v2.0 |
|--------|-------------|------------|
| Strong bullish | 79/100 | **90/100** |
| Weak neutral | 11/100 | 11/100 |
| Discrimination gap | 68 pts | **79 pts** |

---

### v1.5 — Full Code Audit + P0/P1 Fixes (9 Feb 2026)

**38 issues found across 10 modules.** Key P0 fixes:
1. Option order safety gates — `place_option_order()` had **zero gates**, bypassing ALL safety checks
2. Lot sizing capital guard — blocked if single lot > 50% of capital
3. NIFTY lot size corrected to 65
4. Execution guard failure now blocks (was `except: proceed`)
5. Thread lock on paper_positions (RLock)
6. LLM tool argument validation

### v1.4 — Scoring Fix + Critical Bugs (9 Feb 2026)

**Problem:** Bot was not taking any trades — thresholds mathematically unreachable.
- BLOCK_THRESHOLD: 45 → **30**
- Cooldown expiry bug fix (bot permanently halted after any single loss)
- Option side='SELL' for PE buys fix
- Duplicate option position prevention

### v1.3 — Risk of Ruin Fixes (Feb 2026)

7 changes including OTM restrictions, tighter spread gates, anti-double-counting.

### Paper Trading Results Summary

| Day | Date | Version | Trades | Record | Day P&L | Notes |
|-----|------|---------|--------|--------|---------|-------|
| 1 | 6 Feb | v1.4 | 8 | 4W/4L | +₹1,37,872 | First day, equity-heavy |
| 2 | 9 Feb | v1.5 | 8 | 7W/0L | +₹4,441 | Post-audit, clean run |
| 3 | 10 Feb | v1.5 | 27 | 12W/14L | +₹3,438 (+1.72%) | High trades, scoring pipeline broken |
| 5 | TBD | v2.0 | — | — | — | First day with full scoring pipeline |

---

## 17. Debugging & Troubleshooting

### Key Log Files
| File | What to Look For |
|------|-----------------|
| `trade_decisions.log` | Per-trade indicator snapshots (20 keys) — verify indicators are non-zero |
| `speed_gate_log.jsonl` | Speed gate exits — verify not killing good trades |
| `risk_state.json` | Check `system_state`, `daily_pnl`, `consecutive_losses` |
| `active_trades.json` | Should be empty at end of day, check for stale positions |

### Common Issues

**Bot not trading:**
1. Check `risk_state.json` → is `system_state` = `"ACTIVE"`?
2. Check Kite auth → token may have expired (run `quick_auth.py`)
3. Check time → must be between 09:20 and 15:05
4. Check `trade_decisions.log` → are scores too low? What's the BLOCK reason?

**Scoring all zero / defaults:**
1. This was the **#1 historical bug**. Check `trade_decisions.log` for indicator values.
2. If ADX=0, follow_through=0, pullback_depth=0 → indicators are dropping at MarketData boundary.
3. Verify MarketData dataclass has all fields mapped in constructor (~line 1050–1110 in zerodha_tools.py).

**EMA regime always NORMAL:**
1. Check intraday data availability — need ≥ 14 five-min candles for EMA computation.
2. Before 10:30 AM, fewer candles available → EMA spread near zero → NORMAL is expected.

**Risk governor stuck in COOLDOWN:**
1. Fixed in v1.4 — `is_trading_allowed()` now clears expired cooldowns inline.
2. If stuck, check `risk_state.json` → `cooldown_until` timestamp. Delete file to reset.

**Position mismatch:**
1. Check `reconciliation_state.json` for state.
2. Compare `active_trades.json` with actual broker positions.
3. If paper mode: delete `active_trades.json` to reset (positions will be lost).

### Adding a New Indicator — Step-by-Step
1. Compute it in `_calculate_indicators()` (zerodha_tools.py ~line 1200+)
2. Add the key to the return dict (~line 1580)
3. Add a field to `MarketData` dataclass (~line 48)
4. Map it in the MarketData constructor (~line 1050)
5. **WARNING:** If you skip step 3 or 4, the indicator will be **silently dropped**!
6. If needed by GPT: add to the detailed technicals view in `autonomous_trader.py` (~line 698)
7. If needed by options scorer: add to `IntradaySignal` dataclass and populate in constructor (~line 1964)
8. If needed by trend engine: add to `TrendSignal` dataclass and `build_trend_signal_from_market_data()`

### Verifying Data Flow
Run a flow audit to check all indicators are reaching their consumers:
1. Start the bot and let it run one scan cycle
2. Check `trade_decisions.log` — every trade should log 20+ indicator keys with non-zero values
3. If any key is 0/empty when it shouldn't be, trace it through the pipeline above

---

*TITAN Autonomous Trading System — Technical Documentation v2.0*
*Updated: 10 February 2026*
*Next: Day 5 paper trading with full scoring pipeline (v2.0)*
