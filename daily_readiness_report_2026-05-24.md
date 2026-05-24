warning: `VIRTUAL_ENV=/opt/hermes/.venv` does not match the project environment path `.venv` and will be ignored; use `--active` to target the active environment instead
╔══════════════════════════════════════════╗
║   Trading System Daily Readiness Report  ║
╚══════════════════════════════════════════╝
Generated: 2026-05-24 13:02 UTC

Bot Status: ✅ Running  (PID: 38842, uptime: just now)
  ✅ No errors in recent log

── Setups Analysis ──
  Total cycles: 3368
  Entries: 48
  Skipped (low confidence): 3368
  Safety blocks: {'kill_switch': 1743, 'cooldown 1/30': 25, 'cooldown 2/30': 24, 'cooldown 3/30': 24, 'cooldown 4/30': 24, 'cooldown 5/30': 24, 'cooldown 6/30': 22, 'cooldown 7/30': 22, 'cooldown 8/30': 22, 'cooldown 9/30': 22, 'cooldown 10/30': 22, 'cooldown 11/30': 22, 'cooldown 12/30': 22, 'cooldown 13/30': 22, 'cooldown 14/30': 22, 'cooldown 15/30': 22, 'cooldown 16/30': 21, 'cooldown 17/30': 21, 'cooldown 18/30': 21, 'cooldown 19/30': 21, 'cooldown 20/30': 21, 'cooldown 21/30': 21, 'cooldown 22/30': 21, 'cooldown 23/30': 21, 'cooldown 24/30': 21, 'cooldown 25/30': 21, 'cooldown 26/30': 21, 'cooldown 27/30': 21, 'cooldown 28/30': 21, 'cooldown 29/30': 21}
  Score distribution:
    0–0.2:    0  (very low)
    0.2–0.4:  283  (low)
    0.4–0.62: 707  (near threshold)
    0.62–0.70: 0  (half-size zone)
    0.70+:    0  (full-size zone)
  Avg skipped score: 0.433
  Avg half-entry score: 0.601
  Avg full-entry score: 0.675
  ✅ No near-miss tuning pressure

── Trade Progress ──
  Total closed trades: 69
  ADA_USDT    :   1 trades  ░░░░░░░░░░░░░░░░░░░  (199 to optimizer)
  AVAX_USDT   :   8 trades  ░░░░░░░░░░░░░░░░░░░  (192 to optimizer)
  BNB_USDT    :  10 trades  █░░░░░░░░░░░░░░░░░░░  (190 to optimizer)
  BTC_USDT    :   1 trades  ░░░░░░░░░░░░░░░░░░░  (199 to optimizer)
  DOGE_USDT   :   7 trades  ░░░░░░░░░░░░░░░░░░░  (193 to optimizer)
  DOT_USDT    :   7 trades  ░░░░░░░░░░░░░░░░░░░  (193 to optimizer)
  ETH_USDT    :   6 trades  ░░░░░░░░░░░░░░░░░░░  (194 to optimizer)
  LINK_USDT   :   1 trades  ░░░░░░░░░░░░░░░░░░░  (199 to optimizer)
  SOL_USDT    :  14 trades  █░░░░░░░░░░░░░░░░░░  (186 to optimizer)
  XRP_USDT    :  14 trades  █░░░░░░░░░░░░░░░░░░  (186 to optimizer)

── Performance ──
  Paper balance: $998.70
  Total PnL: -0.13%
  Open positions: 3
  Overall win rate: 30.4%
  Per-asset performance:
    ADA_USDT    :   1 trades, WR 0.0%, avg PnL -1.14%
    AVAX_USDT   :   8 trades, WR 37.5%, avg PnL +0.03%
    BNB_USDT    :  10 trades, WR 40.0%, avg PnL +0.07%
    BTC_USDT    :   1 trades, WR 0.0%, avg PnL -1.00%
    DOGE_USDT   :   7 trades, WR 71.4%, avg PnL +0.46%
    DOT_USDT    :   7 trades, WR 28.6%, avg PnL -0.15%
    ETH_USDT    :   6 trades, WR 16.7%, avg PnL +0.30%
    LINK_USDT   :   1 trades, WR 0.0%, avg PnL -1.04%
    SOL_USDT    :  14 trades, WR 14.3%, avg PnL -0.28%
    XRP_USDT    :  14 trades, WR 28.6%, avg PnL -0.03%

── Feature Readiness ──
  Optimizer (BayesianTPE + DE)              ⏳ 186 trades away          SOL_USDT has 14/200 trades
  Confidence Score Threshold Tuning         📊 Data available           3368 cycles recorded. Score distribution: {'0-0.2': 0, '0.2-0.4': 283, '0.4-0.62': 707, '0.62-0.70': 0, '0.70-1.0': 0}
  Trend Sleeve (EMA20/50 4h)                📊 Baseline available       69 total trades provide performance baseline for design decisions

────────────────────────────────────────────
💤 No action items — system running normally.
