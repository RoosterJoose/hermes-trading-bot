# Hyperliquid Multi-Asset Mean-Reversion Bot — System Spec

**Converged specification from 6 source documents (May 2026)**
*You.com ARI Report · Designing a Multi-Asset MR System · Implementation-Ready PDF Guide · Financial Risk Mind Map · Systematic MR Framework · spec.md + SKILL.md*

---

## 1. Objectives

Build and maintain an automated crypto **mean-reversion** system running on Hyperliquid perps,
with an optional slow trend-following overlay.

- Target account size: **$10k–$50k** retail
- Execution loop: **60-second cadence**
- Primary goals: capital preservation, smooth equity curve, parameter simplicity
- Secondary: reasonable trade frequency, modular Python (CCXT + Hyperliquid SDK)

---

## 2. Markets & Universe

### 2.1 Base Universe

Top 10–15 Hyperliquid perps by **30-day median `dayNtlVlm`** and **median OI in USD**.
Candidates: BTC, ETH, SOL, XRP, DOGE, ADA, AVAX, LINK, MATIC, DOT, plus any
other perp consistently in the top 15.

### 2.2 Universe Scan (Every 60s)

Per symbol `s`, compute over last 24h:

| Metric | Definition |
|---|---|
| `V_s` | 24h notional volume |
| `OI_s` | Open interest in USD = `openInterest * markPx` |
| `spread_bps_s` | `10,000 × (ask1 − bid1) / mid` |
| `depth10_s` | `min(bid_depth_within_10bps, ask_depth_within_10bps)` |
| `rv30_s` | `std(log returns of last 30 × 1m bars)` |
| `rv_ratio_s` | `rv30 / median(rv30 over last 20 trading days)` |
| `move_s` | `abs(return over last 15m) / ATR14_15m` |

**Hard eligibility screen** — all must pass:

| Metric | Threshold | Rationale |
|---|---|---|
| `V_s` | `>= $50M` | Consensus across #2, #3, #6. Filters illiquid long-tail perps. |
| `OI_s` | `>= $10M` | Consensus across #2, #3, #6. |
| `spread_bps` | `<= 5 bps` BTC/ETH/SOL; `<= 8 bps` SOL/XRP/LINK/AVAX/ADA; `<= 12 bps` DOGE/beta alts | From #1 — granularity reflects real venue differences. |
| `depth10_s` | `>= max(20 × planned entry notional, $50k core / $30k liquid alts / $20k beta alts)` | From #1 — prevents trading through the book. |
| `rv_ratio` | `0.8 <= rv_ratio <= 3.0` | From #1 — filters dead (no vol) and exploding (broken) markets. |
| `move_s` | `>= 0.75` | From #1 — ensures enough displacement for a fade. |

**If fewer than 3 assets pass, accept no-trade state.** *(All 6 docs agree on this.)*

From the eligible set, take **top 5–7** by watch score:

```
watch_score_s = 0.30 × rank(V_s) + 0.20 × rank(OI_s)
              + 0.20 × rank(depth10 / spread_bps)
              + 0.15 × rank(move_s)
              + 0.15 × rank(volume_ratio_5m_s)
```

Watch score is from #1. It's the most complete ranking formula across all docs.

### 2.3 ⚠️ Design Decision Flag: Liquidity Floors

The docs range from $10M (doc #5) to $500M (doc #3) volume floors. I've chosen $50M as the median,
which is conservative enough for safety but not so restrictive that only BTC/ETH qualify.
**Revisit after 30 live days** — if slippage on fills is tolerable at lower volumes, tighten.
If fills are consistently bad, loosen.

---

## 3. Signals & Features

### 3.1 Base Timeframe

**1-minute bars** for mean-reversion engine. 5m and 15m bars for trend filter layer.

### 3.2 Per-Asset Dynamic RSI Thresholds

This is the most impactful parameter in the system. The docs agree on method (percentile-based)
but disagree on percentile values.

**Chosen approach:**

| Bucket | Symbols | Percentile `p_i` | Clip bounds |
|---|---|---|---|
| Core | BTC, ETH | **5th** percentile | `(15, 32)` |
| Major alts | SOL, XRP | **8th** percentile | `(16, 33)` |
| Liquid alts | LINK, AVAX, ADA, MATIC, DOT | **10th** percentile | `(18, 35)` |
| Beta alts | DOGE, high-beta majors | **12th** percentile | `(18, 35)` |

```
theta_s(t) = clip(Q_{p_s}(RSI14_s[t-W, t-1]), lower_s, upper_s)
```

where `W = 90 days` minimum, `60 days` if insufficient data, and **outright refuse to calibrate
on fewer than 30 days**.

**Entry condition:** `RSI_t^s <= theta_s(t)` — *then* run signal stacking.

**Calibration cadence:** Daily refresh, rolling window.

### 3.3 ⚠️ Design Decision Flag: RSI Percentile Choice

The docs disagree sharply here:
- **Doc #1** uses 10th pctile for BTC/ETH and 12th–15th for alts (looser for majors).
- **Docs #2, #5, #6** use 5th pctile for BTC/ETH (tighter for majors).

I've chosen a **graduated approach** (5th → 8th → 10th → 12th) to match the most defensible
reasoning: BTC/ETH have deeper liquidity and institutional flow, so they can sustain more
false reversals — but a small account benefits from the higher win-rate of rarer entries.
The graduation avoids sharp cutoffs between buckets.
**Revisit after 100 trades per bucket and adjust using the shrinkage formula from doc #1.**

### 3.4 Feature Vector

Per asset `s` at every tick, compute 7 normalized features:

| # | Feature | Raw metric | Normalization | Rationale |
|---|---|---|---|---|
| 1 | **RSI deviation** | `RSI_t^s` | `x_rsi = clip((theta_s - RSI) / 8, 0, 1.5)` | Depth past oversold threshold |
| 2 | **Volume ratio** | `VR_5m = vol_last5m / avg_vol_5m` | `x_vol = clip(log(VR)/log(2.0), 0, 1.2)` | Participation spike / exhaustion |
| 3 | **Lower-low count** | Consecutive lower closes | `x_ll = clip((k - 2) / 3, 0, 1.0)` | Capitulation depth |
| 4 | **Candle position** | `CP = (close - low) / (high - low)` | `x_candle = clip((0.35 - CP) / 0.35, 0, 1.0)` | Close near low = momentum still down |
| 5 | **OI change** | `dOI_15m = OI_t / OI_{t-15} - 1` | `x_oi = clip((-dOI) / 0.04, -1, 1)` | Falling OI = liquidation, rising = fresh shorts (squeeze setup) |
| 6 | **Funding rate** | `FR_h` (1h annualized) | `x_funding = clip((-FR) / 0.0005, -1, 1)` | Negative funding = shorts pay you |
| 7 | **BTC correlation** | `corr90_s` (90-bar to BTC) | Multiplier (see below) | Penalize correlated alts during BTC bear |

### 3.5 Confidence Score

**Hand-built score** (before logistic regression is trained on enough data):

```
raw_s = 0.28×x_rsi + 0.18×x_vol + 0.14×x_ll
       + 0.10×x_candle + 0.10×x_oi + 0.05×x_funding

btc_multiplier = 1.0 if not BTC_bear_veto
                 else max(0.0, 1 - corr90_s / 0.70)

confidence = sigmoid(4 × (raw_s - 0.55)) × btc_multiplier
```

Weights from doc #1. Sigmoid gating from doc #1 (k=4, theta=0.55) vs doc #5 (k=10, theta=0.65).

**Entry tiers:**

| Confidence | Action |
|---|---|
| `>= 0.70` | Full size |
| `0.62 – 0.70` | Half size |
| `< 0.62` | No trade |

Once 500+ labeled outcomes exist, replace hand-built score with:
```
P(win)_s = sigmoid(beta_0 + sum(beta_j × z_{s,j}))
```
One pooled model across assets with asset bucket feature (`core`, `liquid_alt`, `beta_alt`).
Label: `1` if +TP hit before -SL within `H=30m`, else `0`.
Triple-barrier framing from doc #1, supported by Gradzki et al. (2025).

### 3.6 ⚠️ Design Decision Flag: Sigmoid Parameters

Doc #1 uses k=4, theta=0.55. Doc #5 uses k=10, theta=0.65.
The difference: k=10 produces a harder gate (more binary on/off). k=4 produces smoother
size scaling across the confidence range. I've chosen k=4 for smoother size transitions
and theta=0.55 for a higher baseline. **Backtest both** once you have trade history.

---

## 4. Trend & Regime Filters

### 4.1 Layer 1: Per-Asset Trend Filter (15m bars)

Compute on 15-minute bars:

- `EMA20_15m`, `EMA96_15m`
- `slope20 = 100 × (EMA20_t / EMA20_{t-4} − 1)` — approximate 1h slope in percent
- `ADX14_15m`
- `ext = (close − EMA20_15m) / ATR14_15m` — how stretched
- `dOI_1h = OI_t / OI_{t-4} − 1`

| State | Condition | Action |
|---|---|---|
| **Range / soft trend** | `ADX14 < 18` | Full MR allowed |
| **Caution** | `18 <= ADX14 < 25` AND `ext > -1.25` AND `dOI_1h <= +2%` | Half-size only |
| **Alt self-veto** | `close < EMA96`, `ADX14 > 25`, `slope20 < -0.35%`, `ext < -1.5`, AND `dOI_1h > +4%` | No alt MR long |
| **BTC self-veto** | `close < EMA96`, `ADX14 > 30`, `slope20 < -0.30%`, `ext < -2.0`, OR `ret15m < -2.5×ATR15m_pct AND dOI_1h > +2%` | No BTC MR long |

### 4.2 Layer 2: BTC Market Gate (4h bars)

Slow background veto, not a trigger.

| Condition | Action |
|---|---|
| `BTC close_4h < EMA50_4h` AND `BTC ADX14_4h > 20` | Cut **all** alt long MR size by 50% |
| Above condition **AND** 15-minute BTC market veto active | No new alt fades |

15-minute BTC market veto (all must hold):
- `BTC close < EMA96`
- `BTC ADX14 > 28`
- `BTC slope20 < -0.25%`
- `BTC ext < -1.5`

### 4.3 ⚠️ Design Decision Flag: ADX Approach

Doc #1 uses 15m ADX with tiered thresholds (18/25). Docs #2/#6 use 1m+1h ADX with simpler
cutoffs (20/25). I've chosen doc #1's approach because it's more nuanced and directly
addresses the "falling knife" problem with four distinct regime states instead of a binary
on/off. This adds one parameter but is still far from "overfit" territory.

---

## 5. Position Sizing

### 5.1 Risk-Budget Formula (not Kelly)

All 6 docs agree: Kelly is too aggressive for crypto. Use a risk-budget formula.

```
equity_alloc = total_equity × strategy_budget
strategy_budget = 0.70         (leaving 30% room for trend overlay)

r0_s = base risk fraction
  Core (BTC, ETH):          0.35% of allocated equity
  Major alts (SOL, XRP):    0.30%
  Liquid alts:               0.30%
  Beta alts:                 0.25%
  First live month × 0.50    (i.e., 0.175% / 0.15% / 0.15% / 0.125%)

stop_pct_s = max(stop_floor_s, k_s × ATR14_15m_pct)
  stop_floor: 1.8% BTC/ETH, 2.5% liquid alts, 3.2% beta alts
  k: 1.2 BTC/ETH, 1.5 alts

vol_scalar = clip( target_ATR_s / ATR14_15m_pct_s, 0.6, 1.2 )
  target_ATR: 1.6% BTC/ETH, 2.2% liquid alts, 2.8% beta alts

streak_scalar = clip(1 − 0.10×L + 0.04×W, 0.5, 1.1)
  L = consecutive losing trades, W = consecutive winning trades on this sleeve

heat_scalar = clip(1 − portfolio_heat / 0.015, 0.25, 1.0)
  portfolio_heat = sum(open_trade_risk) / total_equity

risk_dollars = equity_alloc × r0_s × vol_scalar × streak_scalar × heat_scalar
notional_s = min(cap_s, risk_dollars / stop_pct_s)

cap_s: 20% BTC/ETH, 15% liquid alts, 12% beta alts
```

### 5.2 Position Sizing — Summary Table

| Parameter | BTC/ETH | SOL/XRP/LINK/AVAX/ADA | DOGE/beta |
|---|---|---|---|
| Base risk `r0` | 0.35% | 0.30% | 0.25% |
| Stop floor | 1.8% | 2.5% | 3.2% |
| ATR multiplier `k` | 1.2 | 1.5 | 1.5 |
| Target ATR | 1.6% | 2.2% | 2.8% |
| Cap | 20% | 15% | 12% |

### 5.3 ⚠️ Design Decision Flag: Per-Trade Risk

Doc #5 uses 1% base risk. Everyone else uses 0.25%–0.5%.
I've chosen 0.25%–0.35% from doc #1 because:
- At $10k equity, 0.35% = $35 risk per trade → 4 concurrent trades = $140 at risk
- At $10k equity, 1% (doc #5) = $100 per trade → 4 concurrent = $400 = 4% portfolio heat
- Daily loss limit of -2.5% means doc #5 allows just 2.5 losing trades before system halt
- The lower figure gives the strategy room to breathe through normal variance

**Rule:** The sizing formula should let you survive 10 consecutive losses at the daily limit
without hitting the -2.5% daily hard stop. 0.35% × 4 positions × ~2 loops = 2.8% daily loss
potential — within tolerance.

---

## 6. Exit Logic

From doc #1 (most complete exit framework across all docs).

### 6.1 Two-Stage TP + Trailing + Time Stop

For **long trades**:

```
mean_target_price = min(EMA20_5m, session_VWAP)  // both above entry
ATR5m = ATR14 on 5m bars
stop_dist = entry − (entry × stop_pct_s)          // from sizing
```

| Asset | TP1 | TP2 | Trailing | Time stop |
|---|---|---|---|---|
| BTC/ETH | `entry + min(0.6×stop_dist, 1.0×ATR5m)` | `min(mean_target, entry + 1.6×ATR5m)` | After TP1: breakeven → `highest_close - 1.0×ATR5m` | 90 min if MFE < 0.5×ATR5m |
| Liquid alts | `entry + min(0.75×stop_dist, 1.2×ATR5m)` | `min(mean_target + 0.25×ATR5m, entry + 2.0×ATR5m)` | After TP1: `highest_close - 1.25×ATR5m` | 60 min |
| Beta alts | Same as liquid alts | Same as liquid alts | `1.4×ATR5m` trail | 45 min |

**Bot-managed exits** (not exchange TP/SL): Use reduce-only limit/stop orders. Hyperliquid's
TP/SL has 10% slippage tolerance and uses mark price — too loose for mean-reversion on majors.

---

## 7. Kill Switches & Risk Controls

From doc #1 (most comprehensive, Hyperliquid-specific). All values specifically for $10k–$50k account.

### 7.1 PnL Kill Switches

| Control | Threshold | Action |
|---|---|---|
| Daily soft stop | `-1.5%` of starting equity | No new entries |
| Daily hard stop | `-2.5%` of starting equity | Flatten all, disable for 24h |
| Rolling weekly stop | `-6%` from 7-day peak | Flatten and pause strategy |
| Max portfolio heat | `1.5%` of equity | Reject new positions |
| Max drawdown | `-15%` from peak | Hard shutdown, manual review |

### 7.2 Position & Concentration Limits

| Control | Threshold |
|---|---|
| Max open positions | `4` if equity < $20k; `5` if >= $20k |
| Max gross leverage | `1.5×` first month, `2.0×` after 30 stable days, **never above 3.0×** |
| Max same-direction correlated alt exposure | If 90-bar correlations to BTC > 0.70, max **2** concurrent alt longs |
| Max cluster concentration | No more than `35%` gross notional in one beta cluster |
| Max single-asset exposure | `cap_s` from sizing section (20%/15%/12%) |

### 7.3 Infrastructure Kill Switches

| Control | Threshold | Action |
|---|---|---|
| Stale market data | Last update > **2s** | Freeze new entries |
| Entry-order protection | Data stale > **5s** | Cancel resting entries |
| Disconnect — public | Public + private streams impaired > **15s** | Disable new entries |
| Disconnect — positions | Position state uncertain > **30s** | Flatten via reduce-only IOC |
| Order ack latency | Median > **700ms** | Alert |
| Order ack latency — critical | P95 > **1500ms** | No new entries |
| Order ack latency — emergency | > **3000ms** | Critical mode |
| Engine heartbeat | Channel quiet > **60s** (server idle close) | Send WebSocket ping |
| Dead-man's switch | `scheduleCancel(now + 8s)` | Refresh every **2s** |
| Order expiry | Entries: `expiresAfter = now + 3000ms` | Cancels: `2000ms` |

### 7.4 Daily Loss Limit Rationale

Following doc #1's 2.5% hard stop (vs doc #5's 4%). At $10k:
- -2.5% = -$250 max daily loss
- With 0.35% per-trade risk, that's ~7 losing trade "units" before shutdown
- With 4 positions concurrent, roughly 2 full loops of all-positions-losing
- This is tight but survivable — and prevents the catastrophic day that kills the account

---

## 8. Asset Halt & Regime Change Detection

### 8.1 Bayesian Win-Rate Test (Primary)

From doc #1. More rigorous than Wald-Wolfowitz (docs #3, #5) and reacts faster.

```
R_k = realized trade outcome in R-multiples
Wbar = average winner in R over baseline
Lbar = average loser magnitude in R over baseline
p_be = Lbar / (Wbar + Lbar)   // break-even win rate

p_i ~ Beta(1 + wins_i, 1 + losses_i)
Pr(p_i > p_be) = 1 − Beta.cdf(p_be)
```

**Pause one asset** if (over last 30 closed trades on that asset):
- `Pr(p_i > p_be) < 0.10` AND `mean(R_last30) < 0`, OR
- Loss streak `>= 5`

**Pause whole MR sleeve** if (over last 50 trades):
- `Pr(p_sys > p_be_sys) < 0.05` AND `mean(R_last50) < -0.10`, OR
- Daily hard stop hit twice in 5 trading days

### 8.2 Regime Shift Detector (Secondary)

From doc #1. Compares forward 30m outcome distribution of last 100 setups vs prior 500.
Pause if one-sided KS or Mann-Whitney gives `p < 0.01` AND recent median < 0.

### 8.3 ⚠️ Design Decision Flag: Bayesian vs Wald-Wolfowitz

Docs #3 and #5 use Wald-Wolfowitz runs test (detects non-random loss clustering).
Doc #1 uses Bayesian Beta posterior (directly estimates probability win rate > breakeven).

I've chosen Bayesian as primary because:
- It answers the direct question: "is the win rate still good enough?"
- It reacts faster (updates every trade, not every N trades)
- It naturally handles uncertainty (posterior distribution, not point estimate)

Wald-Wolfowitz is still useful as a secondary check — it detects *regime change* (losses are
clustered, suggesting the asset stopped reverting) even if the win rate is still above breakeven.

**Implement both:** Bayesian for pause decisions, Wald-Wolfowitz for diagnostic logging.

---

## 9. Mean-Reversion vs Trend Diversification

### 9.1 Allocation

From doc #1 (70/30) over docs #2/#3/#5 (50/50 Shannon's Demon). Rationale:

| Sleeve | Risk budget | Assets | Timeframe | Style |
|---|---|---|---|---|
| **Mean-reversion** | **70%** | Universe scan top 5–7 | 60s loop | This spec (entirety above) |
| **Trend-following** | **30%** | BTC, ETH, SOL only | 4h / 1d | Simple EMA/breakout, few parameters |

### 9.2 ⚠️ Design Decision Flag: 70/30 vs 50/50

Docs #2, #3, #5 push 50/50 with Shannon's Demon (rebalancing bonus between negatively
correlated strategies). Doc #1 recommends 70/30.

I've chosen **70/30** because:
- We already have a working MR engine — the trend sleeve is additive, not a rewrite
- At $10k–$50k, splitting capital 50/50 means the MR sleeve only gets $5k–$25k —
  at 0.35% per trade, that's $17.50–$87.50 risk per trade. The trend sleeve on BTC at
  50% capital would be even smaller — not enough for meaningful entries
- Shannon's Demon requires regular rebalancing between uncorrelated strategies, which
  adds operational complexity and trading costs
- Start at 70/30, **migrate toward 60/40 as account grows above $30k**

---

## 10. Architecture & Implementation

### 10.1 Module Structure

```
hermes_trading/
├── spec.md              ← This file (single source of truth)
├── config.yaml          ← Runtime config (symbols, risk, timeframes, paths)
├── universe.py          ← Universe scan + liquidity filters
├── rsi_profile.py       ← RSI percentile calibration (daily refresh)
├── engine.py            ← Main 60s loop (signals, trend filter, sizing, orders)
├── backtest.py          ← Backtesting harness (same logic as engine)
├── state.py             ← PnL, Sharpe, win-rate, streaks, halt logic
├── killswitches.py      ← Infrastructure safety (stale data, latency, disconnect)
├── reflect.py           ← Renamed from current — self-analysis (market context, rejection stats)
├── adapters/            ← Exchange-specific wrappers
├── tests/               ← Unit + integration tests
└── .env                 ← Secrets (never committed)
```

### 10.2 Six-Layer Architecture

From doc #1:

1. **Ingestion** — WebSocket for trades/book/mids, `metaAndAssetCtxs` snapshot every 30–60s,
   local roll-up into 1m/5m/15m bars, local persistence for RSI distributions

2. **Eligibility** — Hard tradability screen → watch score → top 5–7

3. **Signal** — Dynamic RSI threshold → confidence score → BTC market veto → per-asset trend veto

4. **Sizing** — Risk-budget formula (r0 × vol_scalar × streak_scalar × heat_scalar)

5. **Execution** — Reduce-only exit logic, `expiresAfter` on entries, dead-man's switch

6. **Monitoring** — Bayesian efficacy tracker, kill switches, latency watchdogs

### 10.3 Hyperliquid-Specific

- **Own 1m bar store required.** Hyperliquid's `candleSnapshot` only returns most recent 5,000
  candles. For 30–60 day RSI distributions (1,296,000–2,592,000 1m bars), you need local persistence.
- **One signer per process.** The Python MR engine and Go multi-strategy trader must NOT share
  an API wallet. Separate signers or subaccounts.
- **Dead-man's switch:** `scheduleCancel(now + 8s)`, refresh every 2s.
- **TP/SL:** Use mark price in backtests. Exchange TP/SL uses mark with 10% slippage tolerance.
  Bot-managed reduce-only exits are safer.

### 10.4 Data Architecture

```
┌─────────────────────────────────────────────────────┐
│                    Ingestion                          │
│  WebSocket (trades/book/mids)  ⟵   Hyperliquid       │
│  metaAndAssetCtxs snapshot (30-60s)                   │
│  Local OHLCV rollup (1m/5m/15m)                      │
│  ↓                                                    │
│  Local SQLite/Parquet store (1m bars)                 │
│  ├── Bar data for RSI distribution (90d+ retained)   │
│  └── Bar data for backtesting                         │
└─────────────────────────────────────────────────────┘
```

---

## 11. Go-Live Criteria

From doc #6 (most detailed go-live spec across all docs).

**System remains in paper mode** until ALL of the following are met:

### 11.1 Backtest Requirements
- 1–2 years of historical data
- Sharpe ratio `>= 1.0`
- Max drawdown `<= 20%`
- No look-ahead bias in backtest framework

### 11.2 Paper Trading Requirements
- 30+ consecutive calendar days of paper trading
- Realized Sharpe `>= 0.8`
- Max drawdown `<= 10%`
- Zero risk rule violations (daily loss, portfolio heat, max leverage)
- At least 50 qualifying setups triggered (to have sufficient R-multiple data for Bayesian halt)

### 11.3 Live Ramp
```
Week 1–2:    r0 × 0.50    (base risk halved)
Week 3–4:    r0 × 0.75
Week 5+:     r0 × 1.00    (full spec risk)
Condition:   No daily hard stop hit. No max leverage breach.
```

---

## 12. Overfitting Risk Map

From doc #1 (most complete overfitting table across all docs).

| Component | Fragility | Notes |
|---|---|---|
| Liquidity/spread/depth floors | **Low** | Structural market features |
| Stale-data / kill switches | **Low** | Execution safety, not alpha |
| Per-asset RSI percentile (fixed bucket) | **Medium** | Sensitive but 4 buckets coarse |
| Per-asset RSI percentile (optimized per-symbol) | **High** | Don't do this |
| Hand-weighted score (7 features, fixed weights) | **Medium** | Doc #1 weights preferred |
| Logistic regression (pooled, asset bucket feature) | **Medium** | Better once labeled data exists |
| ADX/slope/ext cutoffs | **Medium–High** | Keep coarse (18/25) |
| Streak-based sizing scalars | **Medium** | Intuitive, self-correcting |
| TP multipliers / trailing width / time-stop | **High** | Most fragile — backtest with walk-forward |
| Multi-timeframe MR cloning | **High** | Don't do this |
| Trend overlay (BTC/ETH/SOL, few params) | **Medium** | Safer than MR expansion |

### 12.1 Practical Safeguards
- Walk-forward testing for key thresholds
- Coarse grids (no fine-tuned optimization)
- Same parameters across assets within bucket
- Monitor live vs backtest divergence — be ready to revert to simpler logic

---

## 13. References (Selected)

| Source | Citation | Used For |
|---|---|---|
| Hyperliquid Docs (Perpetuals, Info, WebSocket, Fees, Rate Limits) | primary | Venue-state fields, risk controls, latency, fees, order behavior |
| Baur, Flatz et al. (2025) — Order Book Liquidity on Crypto Exchanges | JRFM | Spread/depth/variation risk importance |
| Grądzki, Wójcik, Lessmann (2025) — Algorithmic crypto trading | Financial Innovation | Triple Barrier labeling, event-driven sampling |
| Cakici et al. (2024) — ML and cross-section of crypto returns | IRFA | Simple models retain most economic value |
| Rajendran, Kayal, Maiti (2024) — ML for crypto returns | Global Business Review | Logistic over complex classifiers |
| Grobys et al. (2025) — Crypto momentum | FMPM | MR + trend diversification need |
| Lee (2025) — Temporal Fusion Transformer | Systems | Chain-fundamental as slow covariates only |
| Lengyel, Pancsira, Füzesi (2026) — ML in crypto trading | Discover AI | Regime-shift degradation, operational governance |
| Bieganowski, Ślepaczuk (2026 preprint) — Crypto microstructure | — | Cross-asset feature library stability |

---

## Appendix: Source Document Map

| # | Doc | Key Unique Contribution |
|---|---|---|
| 1 | You.com ARI "Hyperliquid MR System Design" | **Used as primary source** — most complete kill switches, Bayesian halt, signer management, 6-layer architecture, tiered trend filter, shrinkage formula |
| 2 | "Designing a Multi-Asset MR System" | **Used as secondary** — liquidity thresholds, RSI percentile method, Shannon's Demon rationale |
| 3 | You.com ARI "Implementation-Ready Guide" (PDF) | **Used as secondary** — Hurst exponent, Wald-Wolfowitz, Hyperliquid rate limit handling |
| 4 | "Financial Risk Indicators" mind map | **Conceptual reference only** — no implementation content, confirms signal weighting taxonomy |
| 5 | "Systematic MR Framework" (richtext) | **Used with caution** — some values (1% risk, -4% daily limit) are too aggressive; good on RSI mechanics and Python implementation |
| 6 | "yes provide both" spec + SKILL | **Used for format and go-live criteria** — spec structure, Hermes skill workflow, deployment gates |
