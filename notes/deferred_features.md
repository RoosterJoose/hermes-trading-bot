# Deferred Trading Features — Phase Reference

Features from You.com ARI report (May 2026) and earlier discussions
that we've deliberately deferred. NOT to be built now — captured so
they don't get lost when their trigger conditions are met.

## Phase 3 — Trend Sleeve
Trigger: confidence score system stabilizes (2-3 days setups_log data),

Trigger: confidence score system stabilizes (~3 days setups_log data) + MR baseline established (~20 trades)

- **Kalman filter** (not simple EMA20/50) — report Sec 2.2.1 says Kalman "outperforms static EMAs in noisy regime-switching markets." Sharpe target: 1.6-2.4, max DD ≤12%.
- **Capital allocator** between MR and trend — each strategy gets a % of the $1,000 based on recent performance.
- **4h timeframe** for BTC/ETH/SOL — independent data fetcher.

## Phase 4 — Optimizer Activation
Trigger: any asset reaches 200 trades or rolling 24-trade Sharpe < 0.5

The walk-forward analyzer already exists and runs every cycle. When 200 trades fire:
- **Param replay** — need to cross-reference trades with bars.db OHLCV data to simulate RSI threshold changes. This requires wiring the SQLite bar store into the optimizer.
- **Multi-metric promotion gate** — only promote a parameter set if it beats current baseline on ALL of: Sharpe, win rate, max drawdown (not just one metric).

## Phase 5 — Advanced ML / Regime Detection
Trigger: Phase 3+4 stabilized, paper account positive for 30+ days

- **HMM/GMM** regime detection (report Sec 2.2.2) — alternative to current Hurst-based approach. Detects bull/bear/sideways/high-vol regimes at 85-92% accuracy.
- **Temporal Fusion Transformer** (TFT) — multi-horizon volatility forecasting. Report says Sharpe 1.5-2.4. High compute cost.
- **Contrastive pre-training** (report Sec 2.3.4) — for representation learning from unlabeled time series. "Sharpe improvement of 10-20%." Requires major infra.
- **LLM-augmented signals** (report Sec 2.3.5) — sentiment from news/social. "Sharpe 1.3-2.0, max DD ≤7%." Needs API costs and infrastructure.

## Phase 6 — Exchange Connectivity / Live
Trigger: paper proving out — consistent Sharpe > 1.0 over 60 days

- **L4 order books** — not relevant until we're on a real exchange.
- **Chaos engineering** — premature until live deployment.
- **MEV-aware execution** (report Sec 2.4.2) — for DEX trading on CEX pairs.

## Not Planned
- Market-making (GLFT/AS) — different asset class, needs exchange connectivity.
- DeFi liquidity (Uniswap v3/v4 RL) — not our focus.
- Cross-chain yield optimization — not our focus.
- Multi-agent RL — research-grade overkill for a paper bot.
