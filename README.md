# Hermes Trading Bot

Multi-asset, self-improving crypto trading bot for **Hyperliquid perpetual futures** (paper mode). Combines mean-reversion and trend-following strategies with adaptive intelligence, safety gates, and an LLM-powered reflection system.

## Architecture

**60-second cadence** trading loop running dual-sleeve architecture:

### Mean-Reversion (MR) Sleeve — 10-asset universe

| Component | Detail |
|---|---|
| **Entry** | RSI oversold (< dynamic 5th percentile or fixed threshold ~24) + 7-component confidence score |
| **Exit** | **TP1** (50% at 20 EMA cross, requires `scale_out_min_R` profit — default 0.3R) |
| | **TP2** (25% at `tp2_target_R` — default 1.5R, `tp2_slice` 50% of remaining) |
| | **Chandelier** (trails last 25% via ATR multiplier, anchored from `runner_high` post-TP2) |
| **Stop loss** | Dynamic ATR-based (3.0× alts, 2.0× majors) with floor/ceiling clamping |
| **Universe** | BTC, ETH, SOL, BNB, XRP, DOGE, ADA, AVAX, LINK, DOT |

### Trend-Following Sleeve — BTC/ETH/SOL only

| Component | Detail |
|---|---|
| **Entry** | EMA 9/21 crossover + ADX > 25 on 1h candles + H > 0.55 (trending regime) |
| **Exit** | Pure Chandelier (2.5× ATR majors, 4.0× ATR SOL) — no TP1/TP2 |
| **Cooldown** | 60 cycles between trend entries |

## Safety Gates (18 active)

| Gate | Threshold | Scope |
|---|---|---|
| BTC 1h RSI | min 20 (configurable per asset) | Both sleeves |
| Fear & Greed | min 10 | Both sleeves |
| Portfolio daily loss hard halt | -4% | Stops ALL sleeves, auto-resets |
| Correlation sector cap | max 2 positions with BTC-β ≥ 0.7 | MR sleeve entries |
| Event calendar kill switch | Flatten 2h before FOMC/CPI/NFP, hold 1h after | Both sleeves |
| Consecutive loss pause | max 5 losses per asset | Per-asset |
| Stale price detection | 5 cycles unchanged price | Per-asset |
| OI velocity gate | >15% expansion in ~48h | Per-asset |
| Vol filter | Min $30M 24h volume | Per-asset |
| Spread proxy | < 0.08% avg (high-low)/close | Per-asset |
| Hurst regime filter | Block MR in trending regimes (H > 0.55) | Per-asset |
| CUSUM stability | Block on unstable break structure | Per-asset |
| Rolling win-rate | Portfolio WR < 48% (50-trade window) | Portfolio |

## Exit Stack (MR Sleeve)

```
Entry → [ATR-based stop always active]
      → TP1: 50% @ EMA cross + min 0.3R profit
      → TP2: 25% @ 1.5R target (resets chandelier anchor via runner_high)
      → Chandelier: trails last 25% from post-TP2 high
```

## Paper Fidelity

- Initial balance: $1,000
- Taker fees: 0.025% per leg (Hyperliquid standard)
- Funding accrual modeled in close PnL
- No slippage modeling (paper mode — limit/maker orders for live)

## LLM Reflection System

Runs weekly (Sunday 13:00 UTC) via cron. Three tiers based on trade count:

| Tier | Trigger | Action |
|---|---|---|
| 1 | 5 new trades | Rolling win-rate watch, no param changes |
| 2 | 20 new trades | Single-parameter adjustment |
| 3 | 50 total trades | Autocorrelation / regime shift analysis |

**Action space:** `entry.threshold`, `stop_loss_pct`, `btc_gate.min_btc_1h_rsi`, `fng_gate.min_value`, `scale_out_min_R`, `tp2_target_R`, `chandelier_mult_alts`, `chandelier_mult_major`, `cooldown_cycles`, `evaluator.*`

**Stale-data guard:** Skips reflection if no new trades since last hypothesis.
**R-distribution diagnostics:** Per-bucket R-multiple counts, avg R, win/loss R.

## Event Calendar

Auto-generates 2026 macro event dates:
- **FOMC**: 8 scheduled meetings
- **CPI**: Monthly (modeled as 2nd Wednesday)
- **NFP**: First Friday of each month

Configurable via `state/goal.yaml`:
```yaml
event_calendar:
  enabled: true
  flatten_minutes_before: 120
  hold_minutes_after: 60
  custom_events: []
```

## Dashboards

- **Bot status:** HTTP API at `:8199` (built into loop.py)
- **Full dashboard:** Python API server (`server.py`) on `:8502`, serves Next.js static export from `hermes-ui/out/`
- **Tunnel:** Cloudflare tunnel for HTTPS public access

## Key Files

```
hermes_trading/
├── loop.py              — Main async engine (60s, dual sleeve, all exits/gates)
├── reflect.py           — LLM reflection pipeline + fallback deterministic engine
├── event_calendar.py    — Macro event calendar kill switch
├── score.py             — Confidence score computation
├── trust_state.py       — Trust-state scoring for position sizing
├── backtest.py          — Historical backtesting harness
└── adapters/
    ├── price.py         — CEX/DEX price adapter
    ├── macro.py         — Fear & Greed adapter
    ├── news.py          — News sentiment adapter
    └── onchain.py       — On-chain metrics adapter
server.py                — Dashboard API server (port 8502)
dashboard_helpers.py     — State aggregation for live dashboard
```

## Key Decisions (May 25, 2026)

1. **No breakeven trail before TP1** — ATR-based stop gives trades room for MR edge
2. **No BTC 4h RSI gate** — Dead control surface (VM lacks 4h candle history), removed
3. **12-24h correlation lookback** — Faster than 60-period spec, better for crypto regime shifts
4. **Trend sleeve chandelier-only** — Preserves right-tail returns, no TP1/TP2 applied
5. **`runner_high` anchor reset** — Chandelier resets after TP2 to prevent spike-choking the runner
6. **Portfolio halt hard latch** — Once -4% breached, blocks entries until UTC day reset (no mid-day whipsaw)
7. **`write_strategy` hard bounds** — Last-mile clamping: `scale_out_min_R` [0.1, 2.0], `tp2_target_R` [0.5, 5.0], `stop_loss_pct` [0.3, 10.0], chandelier multipliers [1.0, 10.0]/[1.0, 6.0], plus all sub-dict params bounded
8. **Paper → Live path** — Requires limit/maker order execution layer for tight 0.3R targets
