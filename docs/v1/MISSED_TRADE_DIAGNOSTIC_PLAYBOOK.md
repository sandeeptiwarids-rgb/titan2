# Missed Trade Diagnostic Playbook
**Version**: 1.0 — Created 2026-03-05

## Purpose
Step-by-step procedure for diagnosing why the Titan bot missed a trade opportunity that was visible on the chart. Use this when the user reports "should have caught this trade at HH:MM" for a specific stock.

---

## Step 1: Verify Stock is in Instrument Universe

**What**: Confirm the stock is in `config.py → F_AND_O_UNIVERSE` and subscribed on WebSocket.

```bash
# Local search
grep -i 'STOCKNAME' config.py
# EC2 check — is it in subscribed tokens?
ssh titan-bot "grep -i STOCKNAME /home/ubuntu/titan/logs/titan.log | head 5"
```

**Why**: If the stock isn't subscribed, no ticks flow → no triggers possible.

---

## Step 2: Check Bot Operational Status at That Time

**What**: Was the bot running, restarting, or crashed during the event window?

```bash
ssh titan-bot "journalctl -u titan-bot --since 'YYYY-MM-DD HH:MM' --until 'YYYY-MM-DD HH:MM' --no-pager"
ssh titan-bot "cat /etc/systemd/system/titan-bot.service"  # Check where logs go
```

**Why**: Deployments restart the bot. The ONGC case (2026-03-05) had a restart at 14:36:43 — just 2 minutes before the drop. During restart, WebSocket needs time to reconnect + resubscribe + build baselines.

---

## Step 3: Check Watcher Triggers for the Stock

**What**: Did the BreakoutWatcher detect any trigger (spike, day extreme, volume surge, slow grind)?

```bash
# All watcher events for the stock
ssh titan-bot "grep -i 'STOCKNAME' /home/ubuntu/titan/logs/titan.log | grep -iE 'Watcher|sustain|GATE|BLOCKED|PASSED|QUEUED' | tail 40"

# Specifically around the event time
ssh titan-bot "grep -E 'HH:M[M-M]' /home/ubuntu/titan/logs/titan.log | grep -i STOCKNAME"
```

**Key things to look for**:
- `✅ Watcher: ... SUSTAINED` — trigger was detected and survived sustain check
- `❌ Watcher: ... sustain FAILED` — trigger detected but price retraced
- `⏳ Watcher: ... blocked by cooldown` — another trigger already set cooldown
- `⏳ Watcher: ... rate-limited` — global rate limit hit
- `📤 Watcher: ... QUEUED` — made it to the main thread queue
- `🐢 Watcher: ... SLOW GRIND` — 5-min baseline detected persistent move

---

## Step 4: Check Primary Scanner (scan_and_trade) Activity

**What**: Did the periodic scanner (runs every ~90s) evaluate the stock?

```bash
ssh titan-bot "grep -E 'HH:M[M-M]' /home/ubuntu/titan/logs/titan.log | grep -iE 'SCAN CYCLE|SUMMARY|STOCKNAME|Top 5'"
```

**Why**: The primary scanner scores ALL F&O stocks but has a higher bar (score≥52). Even if the watcher misses, the primary scanner might catch it — or its score can explain why the stock wasn't "elite".

---

## Step 5: Analyze Gate Block Reasons

**What**: If the stock was triggered but blocked, which gate stopped it?

Gates in order:
| Gate | Name | Block Reason | Fix Lever |
|------|------|-------------|-----------|
| A | SCORE | Score < min_score (35) | Lower threshold or check scorer weights |
| B | CHOP | In chop zone | Review chop detection |
| C | SETUP | No ORB/VWAP+VOL/EMA/RSI setup | ⚠️ FIXED: Watcher-implicit setups now pass |
| D | FT | FT=0 + stale ORB hold | Check if this is for real breakouts |
| E | ADX | ADX < 25 | Check if ADX too lagging for this stock |
| F | OI | Direction vs OI conflict | Check OI data availability |
| G | ML_FLAT | ML says FLAT high prob | Check XGB model freshness |
| G2 | XGB_DIR | XGB opposes direction | Check model prediction |
| G3 | XGB_PROB | P(move) too low | Check threshold |
| G4 | GMM_DR | GMM down-risk opposes | Check GMM scores |
| H | POSITION | Position limit reached | Check max_positions |

**Log format**: `GATE CHECK: STOCKNAME | score=XX dir=DIR trigger=TYPE | XGB=... | GMM...`

---

## Step 6: Check Cooldown / Rate-Limit Impact

**What**: Was the stock blocked by cooldown from an earlier (weaker) trigger?

```bash
ssh titan-bot "grep -i 'STOCKNAME.*(cooldown|rate-limit|blocked|ESCALAT)' /home/ubuntu/titan/logs/titan.log"
```

**Why** (fixed 2026-03-05): Previously, a VOLUME_SURGE trigger would set 180s cooldown, blocking a subsequent NEW_DAY_LOW or PRICE_SPIKE. Now triggers have priority levels:
- Priority 3: PRICE_SPIKE_UP/DOWN, SLOW_GRIND_UP/DOWN
- Priority 2: NEW_DAY_HIGH/LOW
- Priority 1: VOLUME_SURGE

Higher-priority triggers bypass cooldown set by lower-priority ones.

---

## Step 7: Check the Move Characteristics

**What**: Was it a fast spike or a slow grind?

**Indicators from chart**:
- **Fast spike**: Price drops/rises sharply in 1-2 minutes → should trigger PRICE_SPIKE
- **Slow grind**: Price drifts persistently over 5-15 minutes → only caught by SLOW_GRIND (5-min baseline) or NEW_DAY_LOW/HIGH
- **Volume-driven**: Big volume without huge price move → VOLUME_SURGE

**Check the 60s baseline problem**:
The BreakoutWatcher resets its baseline every 60s. A stock dropping 3% over 15 minutes may show only 0.2% per baseline window — never hitting the 0.7% spike threshold. The SLOW_GRIND detector (5-min baseline, ≥1% move) was added to catch this.

---

## Step 8: Verify Data Feed Health

**What**: Were ticks actually flowing for this stock?

```bash
# Check overall watcher stats
ssh titan-bot "grep 'Watcher Stats\|ticks_processed' /home/ubuntu/titan/logs/titan.log | tail 5"
# Check if the stock had stale data
ssh titan-bot "grep -i 'stale.*STOCKNAME\|STOCKNAME.*stale' /home/ubuntu/titan/logs/titan.log"
```

---

## Common Root Causes (Ranked by Frequency)

1. **Gate C Setup too rigid** — Watcher's own trigger (volume surge, day low) wasn't counted as a setup → FIXED
2. **Cooldown suppressing stronger signals** — Volume surge sets cooldown before day low fires → FIXED
3. **Slow grind below 60s spike threshold** — 3% drop over 15min = 0.2% per 60s window → FIXED (5-min baseline)
4. **Bot restart during the move** — Deployment restarts kill baselines, need 60s to rebuild
5. **Score threshold** — Stock has low scorer weights despite clear move (check sector, OI, ML)
6. **ADX too lagging** — ADX reads 20 during the start of a new trend (takes candles to catch up)
7. **Volume regime classification lag** — Ticker detects volume surge in real-time, but bar-based volume_regime still says NORMAL

---

## Quick One-Liner Diagnosis

```bash
# Replace STOCK and TIME with the actual values
ssh titan-bot "grep -E 'TIME_RANGE' /home/ubuntu/titan/logs/titan.log | grep -i STOCK | head 30"
```

Example for ONGC at 14:38:
```bash
ssh titan-bot "grep -E '14:3[5-9]|14:4[0-5]' /home/ubuntu/titan/logs/titan.log | grep -i ONGC | head 30"
```
