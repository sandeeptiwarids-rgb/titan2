# Verdict on the three options strategies

**Status:** All three studies and their independent re-checks are finished. None of the three strategies is ready for real money. One of them is just good enough to paper-trade.

All figures are per 1 current lot (NIFTY 65), after all real costs at today's charges.
- **TRAIN** = the older data used to pick a rule. **TEST** = later data the rule never saw.
- **Green** = net P&L above zero. **Exp_net** = average net P&L per trade.
- **"2x spreads"** = bid-ask spreads at double what we assumed.

| Approach | Verdict | Main reason |
|---|---|---|
| A. Buying NIFTY/BANKNIFTY options on intraday signals | **not viable** | Signals move the index −0.02% to +0.08%; costs plus decay need 0.03–0.3% |
| B. Defined-risk index selling | **marginal (paper only)** | One NIFTY weekly condor earns about Rs 300/lot/week, with little room for error |
| C. Defined-risk stock-option selling | **not viable** | Costs are about 2x a day's time decay; 0 of 12,800 rules made money |

## A. Index option buying: not viable

- **Break-even move needed:** about 0.03% in 30 min and 0.05% in 60 min. Held to 15:15 it is 0.13% with 4 days to expiry, rising to 0.29% on expiry day. The signals were right 45–60% of the time.
- **Real NSE prices, no model:** buying the ATM weekly at the open and selling at the close lost money in both periods.
  - TRAIN: calls −Rs 609 per trade (36% green), puts −Rs 495 (34% green).
  - TEST: calls −Rs 1,231 (34% green), puts −Rs 425 (38% green).
- **Rules don't carry over:** only 12–15% of rules made money in TEST, and the rules that won in TRAIN didn't win in TEST.
- **Gap fades** looked best only because option prices at 09:15 are stale: they price in only about 67% of the gap. Entering from 09:20 onward is breakeven at best.
- **Least-bad rule** (trend extension, 1–2 trades a month):
  - TRAIN: 57 trades, 54% green, +Rs 149 per trade. Worst trade −Rs 9,404, worst month −Rs 7,717.
  - TEST: 26 trades, 58% green, +Rs 628 per trade (95% range −Rs 246 to +Rs 1,502). Worst trade −Rs 4,554, worst month −Rs 4,170, max drawdown −Rs 7,018.
  - It lost money in 2025 and fails a 10% IV drop. It can't be told apart from luck.

## B. Defined-risk index selling: marginal

This was tested on 2,466 days of NSE's official daily option prices (the bhavcopy files). Results from our pricing model just followed whatever IV we assumed, so they were dropped.

- Only 24% of 741 weekly NIFTY setups made money in both periods. These failed:
  - the best put credit spread from TRAIN (+3.7% of max loss per trade in TRAIN, −3.3% in TEST);
  - iron flies (−8.7% TRAIN, −6.8% TEST);
  - entries 4–5 days before expiry;
  - BANKNIFTY;
  - expiry-day condors sold at the open (again because opening prices are stale).
- **The one survivor: a far-OTM NIFTY weekly iron condor.**
  - Enter 3 trading days before expiry.
  - Sell a put and a call about 1.5x the options-implied move away, and buy wings 2% further out.
  - If the condor's value reaches 3x the credit at a daily close, close it. Otherwise hold to expiry.

| 1 lot, 1 trade a week | TRAIN 2019–22 | TEST 2023–26 |
|---|---|---|
| Trades / green | 193 / 91.2% | 194 / 88.7% |
| Exp_net (% of max loss; 95% range) | +Rs 261 (+1.44%; −0.12 to +2.63) | +Rs 298 (+1.08%; −0.09 to +2.08) |
| Avg win / avg loss | +647 / −3,741 | +957 / −4,850 |
| Worst trade (= worst week) | −21,852 | −19,618 |
| Worst month / max drawdown | −18,201 / −24,118 | −15,465 / −19,618 |

Every calendar year from 2019 to 2026 was positive, though 2024 made only +0.2%. No single week can lose more than one max loss.

**Why it is only marginal:**
1. **Spreads:** at 2x spreads, TEST falls to +Rs 161 per trade. The last 12 months made +Rs 211, or only +Rs 20 at 2x spreads.
2. **The stop carries the result:** without the 3x stop, TEST is +0.31% (range −1.6% to +1.9%). The stop's whole TEST benefit came from 2 of 194 trades.
3. **The expiry-day change hurts it:**
   - The old edge came from holds with no weekend inside (Monday entry, Thursday expiry): +1.7–1.8% per trade.
   - Holds that spanned a weekend made +0.1% (TRAIN) and −0.7% (TEST).
   - Since expiry moved to Tuesday (Sep-2025), every 3-day hold spans a weekend. In those 58 weeks it made +Rs 229 (+0.8%), and +Rs 48 at 2x spreads (my own re-check).
4. **Picked partly with hindsight:** it was the best of three rules chosen in advance on TRAIN. The other two made −3.3% and +0.7% in TEST.
5. **Small money even if real:** about Rs 15K a year per lot. That is 1.5% of Rs 10L, or about 10% on the roughly Rs 1.5L peak margin. At 2x spreads it is about Rs 8K a year.

## C. Stock-option selling: not viable

- **Costs beat decay:** a 0.30/0.15-delta credit spread collects about Rs 5,100–5,450 per lot. The round trip costs about Rs 375–390, but time decay from 11:00 to 15:15 earns only about Rs 190–200.
- **No rule works:** 0 of 12,800 rules made money in TRAIN or TEST. The rule picked on TRAIN lost Rs 189 per trade in TRAIN (46% green) and Rs 234 in TEST (45% green). No week in TEST was green. The worst week lost Rs 1.26L across all signals.
- **The signal adds nothing:** selling in the opposite direction, or at random times, loses the same amount.
- **Intraday condors** were green only 2–5% of the time after costs.
- **No IV drop after spikes:** the drop we hoped for came from a data-matching error. Corrected, IV actually rose 0.3%.

## Paper-trading spec: NIFTY 1.5-SD weekly iron condor

- **Goal:** prove the trading system works and check real credits and spreads against the backtest. Paper trading can't prove the edge itself. Weekly results swing about ±Rs 2,200 around a +Rs 300 average, so proving it would take about 200 trades (about 4 years).
- **Instrument:** the nearest NIFTY expiry. That is the Tuesday weekly (the previous trading day if Tuesday is a holiday), or the monthly in monthly-expiry weeks.
- **Entry:**
  - Enter on the 3rd trading day before expiry (normally Thursday) at about 15:25. That is inside the 15:10–15:40 window whose average price the backtest used.
  - No VIX or news filters.
  - Skip the week only if one of the four strikes has no bid or no ask.
- **Strikes:**
  - F = the forward price, worked out from the ATM call and put mid prices.
  - σ = ATM implied volatility, using trading days rather than calendar days.
  - d = 1.5 × σ × √(3/252). That puts each short strike about 1.9 ATM straddles away from F.
  - Short put = F·e^−d, rounded down to 50. Short call = F·e^+d, rounded up to 50.
  - Wing width W = 2% of F, rounded to 50.
  - Example (F 22,500, IV 13%): sell the 22,000 PE and 23,000 CE, buy the 21,550 PE and 23,450 CE.
  - Expected credit: about Rs 950 per lot at VIX 12–14, about Rs 1,250 at VIX 14–16.
- **Size:**
  - 1 lot, and only one condor open at a time.
  - Max loss = (W − credit) × 65 + costs, about Rs 28–32K. That is about 3% of Rs 10L, but about 6% of a Rs 5L account, which is too much.
  - Margin is about Rs 88K, rising to about Rs 1.5L on expiry day. Keep a 30% buffer on top.
- **Fills:** buy the wings first, then sell the shorts. Sell at the bid and buy at the ask. Also record a 2x-spread version. Apply all charges.
- **Exits:**
  - On each day between entry and expiry, at about 15:25: if the condor's mid value is 3x the entry credit or more, buy back the shorts first, then sell the wings.
  - No stop on expiry day. Hold to cash settlement at the official auction close, and book exercise STT plus Rs 23.60 per in-the-money leg.
  - No take-profit, and no re-entry that week.
- **Log:**
  - Market: spot, futures, VIX, F, ATM IV.
  - For each leg: bid, ask, LTP, depth and quote age.
  - Trade: fills (real and 2x), charges, margin, daily marks, exit reason, settlement.
  - That evening's bhavcopy close for each leg.
  - Also log T-2, T-1 and no-stop versions on the side. They are for information only and are not used to judge the strategy.
- **Kill the test if any of these happen:**
  1. After 12 entries, the median real credit is below 85% of the bhavcopy credit for the same strikes on the same day, or the median real spread is more than 2x what the cost model assumes.
  2. 19 or fewer wins in the first 26 trades, or 41 or fewer in 52. If the backtest is right, each of these happens by bad luck only about 3–4% of the time.
  3. Cumulative P&L at 2x-spread fills falls below −Rs 42K (1.5 max losses). If the backtest is right, that happens by bad luck only 1–4% of the time.
  4. Any loss bigger than 1.05x the computed max loss, any short leg left without its wing, or any P&L that doesn't match the fills. In that case stop and fix the trading system.
- **At 52 weeks:** consider trading 1 lot live only if nothing above was triggered and the average P&L at real fills is above zero.

## What the trading system must support

- **Calendar and contracts:** NSE holidays, Tuesday expiry with the holiday shift, and counting "n trading days before expiry". Read lot size, strikes and order-size limits from the exchange instrument file, since lot sizes change twice a year.
- **Market data:** live option-chain bid/ask/depth with checks that reject stale quotes, forward and IV calculation, and a VIX feed.
- **Multi-leg orders:**
  - Buy wings first on entry and close shorts first on exit.
  - Limit orders only (stop-loss market orders aren't allowed on options).
  - If a leg fails to fill in time, undo the legs that did fill. A short leg must never be open without its wing.
  - At most 10 orders a second.
  - A static IP and an exchange algo ID before going live.
- **Margin check before entry:** include the extra 2% per short leg on expiry day and a 30% buffer.
- **Scheduling and settlement:** the 15:25 daily mark and stop check, and settlement at the auction close with exercise charges.
- **P&L:**
  - One P&L number, built only from real fills and settlement minus charges.
  - Sign checks for every leg (PE/CE, long/short).
  - A nightly check against the bhavcopy.
- **Paper fills and hard caps:** fills at the real bid/ask plus a 2x-spread version; 1 lot, one position, and an automatic shutdown at −Rs 42K. Only one process may place orders.
- **Optional quote logging (no orders):** index options from 09:15 to 09:25, to measure the stale opening prices, and stock options at spikes.

## Best case for "every trade profitable"

- **Index buying:** 35–58% of trades green. Tight profit targets lift that to 66–88%, but losses then run about 1.7x the wins and the total goes negative.
- **Index condor:**
  - 89–91% of weeks green, which still means 5–6 losing weeks a year.
  - Each average loss wipes out 5–6 average wins.
  - The worst week is about −Rs 20–22K.
  - One full max loss (about Rs 28K) equals about 95 weeks of average profit.
- **Stock selling:** green 2–5% of the time (intraday condors) up to about 60% (best spread), and always a net loss.

## Recommendation

Build the lean trading system in paper mode and run this condor through it for 52 weeks. The main point is to prove that multi-leg orders, real-fill P&L and our price assumptions all hold up. Don't plan any income around it (see point 5 above).

**Your decision:** go ahead on that basis, or shelve options automation.

The scripts for my re-checks stayed in a local working folder (wf2/verdict/); they are not part of this repository.