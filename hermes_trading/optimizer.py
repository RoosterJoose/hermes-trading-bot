"""
optimizer.py — Walk-Forward Parameter Analyzer + Trigger Engine

What this does:
  We cannot replay trades with different params (no OHLCV stored),
  so instead we analyze realized trade sequences across time windows.

  1. Segment trades chronologically into rolling train/validate windows
  2. Compute multi-metric fitness on each window
  3. Compare windows to detect overfitting / regime shift / degradation
  4. Generate parameter recommendations grounded in actual performance
  5. Check against benchmark targets (Sharpe, win rate, max DD)
  6. Log everything to optimizer_log.jsonl per asset

Trigger conditions (wired in _check_optimizer_ready):
  - 200 trades available (automatic first activation)
  - Rolling 24-trade Sharpe < 0.5 (performance degradation)
  - 30 days since last optimization run (scheduled)

Multi-metric fitness:
  Sharpe (35%) + Win Rate (25%) + Avg PnL norm (20%) + Max DD score (20%)
"""

import json
import math
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple

# ── Constants ──────────────────────────────────────────────────────────
MIN_TRADES_FOR_OPTIMIZATION = 200
OPTIMIZER_COOLDOWN_DAYS = 30
PERFORMANCE_DROP_SHARPE = 0.5  # trigger optimizer if rolling Sharpe < this

# Benchmark targets (from You.com ARI May 2026 report)
BENCHMARKS = {
    "sharpe_target_mr": 1.2,  # minimum Sharpe for mean-reversion
    "sharpe_target_trend": 1.6,  # Sharpe for trend-following strategies
    "max_drawdown_pct": 12.0,  # max allowable drawdown
    "min_win_rate": 0.40,  # minimum win rate
    "min_trades_per_asset": 10,  # minimum trades for meaningful analysis
}


def trades_ready(trades: list) -> bool:
    return len(trades) >= MIN_TRADES_FOR_OPTIMIZATION


def _compute_sharpe(pnls: List[float]) -> float:
    """Annualized Sharpe from a list of trade PnL percentages."""
    if len(pnls) < 2:
        return 0.0
    mean = sum(pnls) / len(pnls)
    std = math.sqrt(sum((p - mean) ** 2 for p in pnls) / len(pnls))
    if std == 0:
        return 0.0
    # Rough annualization: ~365 trades/year for 1m strategy
    return (mean / std) * math.sqrt(365)


def _max_drawdown(pnls: List[float]) -> float:
    """Calculate max drawdown from a series of trade PnLs (compounding)."""
    eq = 1.0
    peak = 1.0
    mdd = 0.0
    for p in pnls:
        eq *= 1 + p / 100
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > mdd:
            mdd = dd
    return mdd


def multi_metric_fitness(trades: List[dict], window_label: str = "") -> dict:
    """Compute multi-metric fitness for a set of trades.

    Returns dict with individual metrics and composite score (0-100).
    """
    if not trades:
        return {
            "fitness": 0.0,
            "trades": 0,
            "sharpe": 0.0,
            "win_rate": 0.0,
            "avg_pnl": 0.0,
            "max_dd": 0.0,
        }

    pnls = [t.get("pnl_pct", 0) for t in trades]
    wins = [p for p in pnls if p > 0]
    loss_sum = sum(p for p in pnls if p < 0)

    sharpe = _compute_sharpe(pnls)
    win_rate = len(wins) / len(pnls) if pnls else 0
    avg_pnl = sum(pnls) / len(pnls) if pnls else 0
    max_dd = _max_drawdown(pnls)

    # Normalize components for composite score
    # Sharpe: target 2.0 for full score (>= 2.0 = 1.0)
    sharpe_norm = min(1.0, sharpe / 2.0)
    # Win rate: target 0.50 for full score
    wr_norm = min(1.0, win_rate / 0.50)
    # Avg PnL: target +2.0% for full score
    avg_norm = min(1.0, max(-1.0, avg_pnl / 2.0))
    # Max DD: inverse — lower is better. 0% DD = 1.0, 20% DD = 0.0
    dd_norm = max(0.0, 1.0 - max_dd / 20.0)

    # Composite: Sharpe has highest weight (most risk-adjusted)
    fitness = sharpe_norm * 0.35 + wr_norm * 0.25 + avg_norm * 0.20 + dd_norm * 0.20
    fitness = max(0.0, min(100.0, fitness * 100))

    return {
        "fitness": round(fitness, 1),
        "trades": len(pnls),
        "sharpe": round(sharpe, 3),
        "win_rate": round(win_rate, 3),
        "avg_pnl": round(avg_pnl, 3),
        "max_dd": round(max_dd, 2),
        "window": window_label,
    }


def detect_performance_drop(trades: List[dict]) -> Optional[dict]:
    """Check if recent performance warrants optimizer activation.

    Returns trigger info dict if conditions met, None otherwise.
    """
    if len(trades) < 30:
        return None

    # Use last 24 trades as the recent window
    recent = trades[-24:]
    pnls = [t.get("pnl_pct", 0) for t in recent]
    sharpe = _compute_sharpe(pnls)

    if sharpe < PERFORMANCE_DROP_SHARPE:
        return {
            "trigger": "performance_drop",
            "recent_sharpe": round(sharpe, 3),
            "threshold": PERFORMANCE_DROP_SHARPE,
            "trades_in_window": len(recent),
        }
    return None


def check_benchmarks(trades: List[dict]) -> dict:
    """Check trade performance against benchmark targets.

    Returns dict with per-metric pass/fail and recommendation.
    """
    if len(trades) < BENCHMARKS["min_trades_per_asset"]:
        return {"status": "insufficient_data", "trades": len(trades)}

    pnls = [t.get("pnl_pct", 0) for t in trades]
    sharpe = _compute_sharpe(pnls)
    wins = [p for p in pnls if p > 0]
    win_rate = len(wins) / len(pnls) if pnls else 0
    max_dd = _max_drawdown(pnls)

    results = {
        "status": "analyzed",
        "trades": len(trades),
        "sharpe": round(sharpe, 3),
        "sharpe_target": BENCHMARKS["sharpe_target_mr"],
        "sharpe_met": sharpe >= BENCHMARKS["sharpe_target_mr"],
        "win_rate": round(win_rate, 3),
        "win_rate_target": BENCHMARKS["min_win_rate"],
        "win_rate_met": win_rate >= BENCHMARKS["min_win_rate"],
        "max_drawdown": round(max_dd, 2),
        "max_dd_target": BENCHMARKS["max_drawdown_pct"],
        "max_dd_met": max_dd <= BENCHMARKS["max_drawdown_pct"],
    }

    # Generate recommendations
    recs = []
    if not results["sharpe_met"] and results["sharpe"] < 0.8:
        recs.append(
            "Sharpe well below MR benchmark (1.2) — consider: "
            "tightening stop loss, raising RSI entry threshold to reduce noise"
        )
    if not results["win_rate_met"]:
        recs.append(
            f"Win rate {results['win_rate']:.0%} below {BENCHMARKS['min_win_rate']:.0%} target — "
            "raise RSI threshold to be more selective"
        )
    if not results["max_dd_met"]:
        recs.append(
            f"Max drawdown {results['max_drawdown']:.1f}% exceeds {BENCHMARKS['max_drawdown_pct']:.0f}% — "
            "tighten stop loss, reduce position sizing"
        )

    results["recommendations"] = recs
    return results


def walk_forward_analysis(trades: List[dict], current_config: dict) -> dict:
    """Run walk-forward analysis across chronological windows.

    1. Sort trades by time
    2. Split into train (first 70%) and validate (last 30%)
    3. Compute multi-metric fitness on each
    4. Compare — detect overfitting vs regime shift
    5. Return analysis + recommendations
    """
    # Sort chronological
    sorted_trades = sorted(trades, key=lambda t: t.get("entry_time", ""))
    total = len(sorted_trades)

    if total < 20:
        return {
            "status": "insufficient_data",
            "trades": total,
            "message": f"Need at least 20 trades for walk-forward (have {total})",
        }

    split_idx = int(total * 0.70)
    train = sorted_trades[:split_idx]
    validate = sorted_trades[split_idx:]

    train_fit = multi_metric_fitness(train, window_label="train")
    val_fit = multi_metric_fitness(validate, window_label="validate")
    overall = multi_metric_fitness(sorted_trades, window_label="overall")

    # Detect degradation
    degradation = train_fit["fitness"] - val_fit["fitness"] > 15
    overfitting = degradation and train_fit["sharpe"] > 1.5 and val_fit["sharpe"] < 0.5
    regime_shift = degradation and val_fit["sharpe"] < 0.5 and train_fit["sharpe"] > 1.0

    recommendations = []
    if overfitting:
        recommendations.append(
            "Potential overfitting detected (train >> validate). "
            "Reduce parameter complexity before next optimization."
        )
    if regime_shift:
        recommendations.append(
            "Possible regime shift detected (validate performance diverging from train). "
            "Consider adjusting RSI threshold or enabling stricter market gates."
        )
    if val_fit["sharpe"] < 0.8:
        recommendations.append(
            "Recent (validation) Sharpe below 0.8 — strategy may be losing edge. "
            "Review market conditions vs when the best trades occurred."
        )
    if val_fit["win_rate"] < 0.30:
        recommendations.append(
            "Recent win rate below 30% — raise RSI entry threshold to increase selectivity."
        )

    return {
        "status": "analyzed",
        "trades": total,
        "train_fitness": train_fit,
        "validate_fitness": val_fit,
        "overall_fitness": overall,
        "overfitting_detected": overfitting,
        "regime_shift_detected": regime_shift,
        "degradation_detected": degradation,
        "recommendations": recommendations,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def needs_optimization(
    asset_key: str, trades: List[dict], last_optimization: Optional[dict] = None
) -> dict:
    """Check if optimizer should activate based on trigger conditions.

    Returns trigger info dict with reason + urgency.
    """
    reasons = []
    urgency = "low"

    # Trigger 1: Trade count threshold
    if len(trades) >= MIN_TRADES_FOR_OPTIMIZATION:
        reasons.append(
            f"trade_threshold ({len(trades)} >= {MIN_TRADES_FOR_OPTIMIZATION})"
        )
        urgency = "medium"

    # Trigger 2: Performance drop
    drop = detect_performance_drop(trades)
    if drop:
        reasons.append(
            f"performance_drop (Sharpe={drop['recent_sharpe']:.2f} < {PERFORMANCE_DROP_SHARPE})"
        )
        urgency = "high" if drop["recent_sharpe"] < 0.3 else "medium"

    # Trigger 3: Scheduled (30 days since last run)
    if last_optimization:
        last_ts = last_optimization.get("timestamp", "")
        try:
            last_dt = datetime.fromisoformat(last_ts)
            days_since = (datetime.now(timezone.utc) - last_dt).days
            if days_since >= OPTIMIZER_COOLDOWN_DAYS:
                reasons.append(f"scheduled ({days_since} days since last)")
                urgency = "low"
        except Exception:
            pass

    return {
        "asset": asset_key,
        "triggered": len(reasons) > 0,
        "reasons": reasons,
        "urgency": urgency,
        "trades_available": len(trades),
        "threshold": MIN_TRADES_FOR_OPTIMIZATION,
    }


def check_and_optimize(
    asset_key: str,
    trades: List[dict],
    current_config: dict,
    optimization_log: Optional[list] = None,
) -> dict:
    """Full optimizer check: triggers → analysis → recommendations.

    This is called by _check_optimizer_ready() in loop.py.
    """
    # Get last optimization record
    last_opt = None
    if optimization_log:
        last_opt = (
            optimization_log[-1]
            if isinstance(optimization_log, list)
            else optimization_log
        )

    # Check triggers
    trigger = needs_optimization(asset_key, trades, last_opt)

    result = {
        "asset": asset_key,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trigger": trigger,
        "trades_available": len(trades),
        "threshold": MIN_TRADES_FOR_OPTIMIZATION,
    }

    if not trigger["triggered"]:
        result["status"] = "dormant"
        result["message"] = (
            f"No trigger active. "
            f"Trades: {len(trades)}/{MIN_TRADES_FOR_OPTIMIZATION}. "
            f"Next scheduled: last run + {OPTIMIZER_COOLDOWN_DAYS}d"
        )
        return result

    # Run benchmarks
    benchmarks = check_benchmarks(trades)
    result["benchmarks"] = benchmarks

    # Run walk-forward analysis
    wf = walk_forward_analysis(trades, current_config)
    result["walk_forward"] = wf
    result["status"] = "analyzed"
    result["message"] = (
        f"Optimizer ran. "
        f"Fitness: train={wf.get('train_fitness', {}).get('fitness', '?')} "
        f"validate={wf.get('validate_fitness', {}).get('fitness', '?')}. "
        f"Trigger: {trigger['reasons'][0] if trigger['reasons'] else 'unknown'}. "
        f"Recs: {len(wf.get('recommendations', []))}"
    )

    return result
