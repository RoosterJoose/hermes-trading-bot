"""
score.py — Scores trades against goal.yaml goals.
Returns a composite score in [-1, +1].

Score formula (equal weighted):
  - Return score:   realised_return / target_return  (capped at 1.0, floored at -1.0)
  - Drawdown score: 1.0 - (max_drawdown / max_allowed_drawdown)  (floored at -1.0)
  - Sharpe score:   sharpe / min_sharpe  (capped at 1.0, floored at 0.0)

Final = weighted average of the three.
"""
from typing import List, Dict
import math


def score_trades(trades: List[Dict], goal: Dict) -> Dict:
    """Score a list of closed trades against the goal.

    Args:
        trades: list of trade dicts with keys:
            entry_price, exit_price, entry_time, exit_time,
            pnl_pct, sharpe (optional), max_drawdown (optional)
        goal: from goal.yaml

    Returns:
        dict with overall_score, component_scores, and trade_count
    """
    if not trades:
        return {
            "overall_score": 0.0,
            "component_scores": {},
            "trade_count": 0,
            "classification": "no_trades",
        }

    target_return = goal.get("target_return_30d", 0.05)
    max_drawdown = goal.get("max_drawdown", 0.08)
    min_sharpe = goal.get("min_sharpe", 1.2)

    # Calculate aggregate metrics from trades
    total_pnl_pct = sum(t.get("pnl_pct", 0.0) for t in trades)
    avg_pnl_pct = total_pnl_pct / len(trades) if trades else 0.0

    # Calculate Sharpe-like metric from trade returns
    returns = [t.get("pnl_pct", 0.0) for t in trades]
    mean_return = sum(returns) / len(returns) if returns else 0.0
    std_return = math.sqrt(sum((r - mean_return) ** 2 for r in returns) / len(returns)) if len(returns) > 1 else 0.0001
    sharpe = mean_return / std_return if std_return > 0 else 0.0

    # Track max drawdown from trades
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        cumulative += t.get("pnl_pct", 0.0)
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    # Component scores
    return_score = min(1.0, max(-1.0, total_pnl_pct / target_return))
    dd_score = max(-1.0, 1.0 - (max_dd / max_drawdown)) if max_drawdown > 0 else 0.0
    sharpe_score = min(1.0, max(0.0, sharpe / min_sharpe)) if min_sharpe > 0 else 0.0

    overall = (return_score + dd_score + sharpe_score) / 3.0

    # Classification
    if overall >= 0.5:
        classification = "good"
    elif overall >= 0.0:
        classification = "neutral"
    elif overall >= -0.3:
        classification = "poor"
    else:
        classification = "critical"

    return {
        "overall_score": round(overall, 4),
        "component_scores": {
            "return_score": round(return_score, 4),
            "dd_score": round(dd_score, 4),
            "sharpe_score": round(sharpe_score, 4),
        },
        "metrics": {
            "total_pnl_pct": round(total_pnl_pct, 4),
            "avg_pnl_pct": round(avg_pnl_pct, 4),
            "sharpe": round(sharpe, 4),
            "max_drawdown": round(max_dd, 4),
            "trade_count": len(trades),
        },
        "trade_count": len(trades),
        "classification": classification,
    }
