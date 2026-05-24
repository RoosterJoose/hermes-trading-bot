# Hermes Trading Bot

A multi-asset, self-improving crypto trading bot for **Hyperliquid perpetual futures**. Combines mean-reversion and trend-following strategies with adaptive intelligence, safety gates, and an LLM-powered reflection system.

## Overview

A 60-second cadence trading loop that runs two strategy sleeves:

- **Mean-Reversion (MR) Sleeve** — Entry on RSI oversold conditions with a 7-component confidence score. 10-asset universe (BTC, ETH, SOL, BNB, XRP, DOGE, ADA, AVAX, LINK, DOT).
- **Trend-Following Sleeve** (Phase 3) — EMA 9/21 crossover + ADX > 25 on 1h candles for BTC/ETH/SOL, with Chandelier trailing exits.

Both sleeves share a portfolio-level exposure cap (max 3 concurrent positions, 3.0x leverage).

## Architecture

```
hermes_trading/
├── loop.py          — Main 60s async engine (signals, exits, safety gates)
├── adaptive.py      — Hurst exponent, CUSUM, dynamic RSI, percentile thresholds
├── universe.py      — Hyperliquid data fetcher + asset screening
├── risk.py          — Portfolio tracker, correlation gates, VaR
├── reflect.py       — LLM-powered reflection/learning pipeline
├── optimizer.py     — Parameter optimization (dormant until 200+ trades)
├── trust_state.py   — Trust-state scoring for position sizing
├── score.py         — Confidence score computation
├── backtest.py      — Historical backtesting harness
└── adapters/
    ├── price.py     — CCXT + yfinance price adapter (paper mode with Brownian noise)
    ├── onchain.py   — On-chain data adapter
    ├── news.py      — News sentiment adapter
    └── macro.py     — Macroeconomic data adapter
```

## Safety Features (17 Active Gates)

| Gate | What It Blocks |
|---|---|
| Hurst Regime | MR entries when H > 0.55 (trending) |
| Percentile RSI | Dynamic per-asset thresholds (q05 majors, q10 alts) from bars.db |
| Volume Filter | Assets with < $30M 24h volume |
| OI Velocity | >15% OI expansion in ~48h |
| BTC Vol Surge | Global MR halt when BTC 1m vol spikes >30% |
| Z-Score Cooling Chamber | Flash crash guard (30-cycle cooldown) |
| Bid-Ask Spread Proxy | Entry blocked when spread > 0.08% |
| ADX Danger Zone | Higher-TF trend extremes |
| Rolling Win Rate | Portfolio WR < 48% halts all entries |
| MC Drawdown | 95th percentile DD threshold |
| Trust State | Dynamic position sizing multiplier |
| Daily Loss Limit | Per-asset max daily loss % |
| Stale Price Detection | Blocks after N cycles with unchanged price |
| Consecutive Loss Pause | Auto-pauses asset after 5 consecutive losses |
| Correlation Gate | Caps correlated altcoin exposure |
| Time Exit | Forced close after 45-60 min |
| Portfolio DD | Global drawdown cap |
| Vol Sanity | Blocks entries when annualized 1m vol > 3.0 (detects broken data feeds) |

## Confidence Score (7 Components)

| Component | Weight | Source |
|---|---|---|
| RSI Score | 0.22 | 14-period RSI vs asset-specific threshold |
| Volume Score | 0.18 | Volume ratio vs trailing average |
| Lower-Low Score | 0.14 | Absence of lower-low cascades |
| Candle Position | 0.11 | Price position within candle range |
| Regime Score | 0.12 | Hurst/CUSUM regime alignment |
| ADX Score | 0.13 | Low ADX favors mean-reversion |
| Funding Score | 0.10 | Negative funding = squeeze potential. Formula: `(fr_1h × 8) / 0.001` (×8 amplification). |

Thresholds: ≥0.60 full entry, 0.55-0.60 half entry, <0.55 none.

## Position Sizing (VBPS)

Volatility-Based Position Sizing: `R_base = 1% of equity / ATR stop distance`. Halved in random-walk regimes. Further adjusted by streak scalar, heat scalar, trust-state, and VaR cap.

## Exit Logic

| Mechanism | Applies To |
|---|---|
| ATR Hard Stop | All positions (2-3x ATR) |
| Time Exit | MR only (45-60 min) |
| Scale-Out TP | MR only (50% at EMA20 reversion) |
| Chandelier Trail | MR remainder + Trend positions |
| Chandelier (Trend) | Pure trail, no time cap |

## LLM Reflection System

Weekly reflection (Sundays 13:00 UTC) with three tiers:

- **Tier 1** (5 trades): Rolling win-rate watch, no param changes
- **Tier 2** (20 trades): Parameter adjustment within bounded ranges
- **Tier 3** (50 trades): Autocorrelation/regime shift analysis

Monthly LLM audit generates strategy improvement proposals.

## Getting Started

```bash
# Clone (private repo — requires access)
git clone git@github.com:RoosterJoose/hermes-trading-bot.git
cd hermes-trading-bot

# Install dependencies
uv sync

# Run in paper mode
HERMES_TRADING_MODE=paper uv run python -m hermes_trading
```

## Configuration

- `state/goal.yaml` — Per-asset targets (return, drawdown, Sharpe)
- `state/<ASSET>/strategy.yaml` — Per-asset strategy parameters
- `data/bars.db` — 1m OHLC bar store (populated by bar ingester)

## Paper Fidelity

Since Hermes is intentionally paper-first, the paper execution model includes:

| Factor | Status |
|---|---|
| **Fees** | ✅ Taker fee 0.025% per leg deducted on entry and exit |
| **Funding accrual** | ✅ Per-hour funding rate × hours held applied at close |
| **Slippage** | ❌ Not modeled (fill at mid-price) |
| **Liquidation risk** | ❌ Not modeled (no leverage) |
| **Partial fills** | N/A (instant fill in paper) |
| **Stale data** | ✅ Detected and blocked by stale price gate |
| **Exit behavior** | ✅ All 4 exit paths execute identically to live |

Each trade record includes `fee_pct`, `funding_pct`, and `net_pnl_pct` for audit tracing.

## Go-Live Path

The system remains in paper mode until:

1. Backtest Sharpe ≥ 1.0 with max DD ≤ 20%
2. 30+ days paper trading with realized Sharpe ≥ 0.8, max DD ≤ 10%
3. No risk rule violations

## HTTP Status Server

A lightweight aiohttp server runs on `http://0.0.0.0:8099` alongside the trading loop:

| Endpoint | Response |
|---|---|
| `GET /` | Service info + endpoint list |
| `GET /status` | Full heartbeat JSON (balance, positions, context) |
| `GET /health` | Simple health check with uptime + mode |
| `GET /positions` | Open positions (MR + trend) |
| `GET /trades?limit=50` | Recent trades across all assets |
| `GET /readiness` | Go-live readiness assessment |
| | `live_ready: true/false` with detailed blockers |

Example readiness response:
```json
{
  "paper_days_elapsed": 18,      "required_paper_days": 30,
  "paper_days_met": false,
  "total_trades": 45,            "min_trade_count": 50,
  "min_trade_count_met": false,
  "realized_sharpe": 0.92,       "min_sharpe": 0.8,
  "sharpe_met": true,
  "max_drawdown_pct": 3.2,       "max_drawdown_limit": 10.0,
  "max_drawdown_ok": true,
  "uptime_hours": 432,           "min_uptime_hours": 168,
  "uptime_met": true,
  "stop_loss_ratio": 0.15,       "stop_loss_ratio_limit": 0.40,
  "stop_loss_ok": true,
  "stop_loss_exits": 7,
  "time_exits": 12,
  "extreme_losses": 0,
  "live_ready": false,
  "blockers": ["paper_days: 18/30", "trade_count: 45/50"]
}
```

## Configuration

### Strategy schema versioning

Strategy YAML files include a `version` field. On startup, `_validate_strategy()` checks:

- Version is present and numeric
- All required fields exist
- Version matches current schema (`v22`)

Missing or outdated versions produce startup warnings but don't block execution.

`load_strategy()` auto-creates a default v01 strategy for new assets.

## Key Dependencies

- `ccxt` — Exchange connectivity
- `yfinance` — 1m candle data
- `numpy` — Numerical computation (Hurst, CUSUM, RSI)
- `httpx` — Hyperliquid API client
