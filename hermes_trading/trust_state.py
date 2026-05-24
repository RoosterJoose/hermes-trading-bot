"""
trust_state.py — Unified Trust-State Scaling.

Combines multiple risk signals into a single 0-1 multiplier that
cascades across all pairs to reduce position sizes when risk rises.

Factors:
  - Consecutive losses (streak)
  - Stale price cycles
  - Daily PnL drawdown
  - Open position concentration
  - Portfolio DD (from PortfolioTracker)

Usage in _calc_position_size:
    trust = trust_state_scale(asset_key, loop_state)
    final_size *= trust
"""

from datetime import datetime, timezone
from typing import Dict, Optional


def compute_trust_state(
    asset_key: str,
    consecutive_losses: Dict[str, int],
    stale_price_cycles: Dict[str, int],
    daily_pnl_pct: Optional[float],
    open_positions_count: int,
    max_open_positions: int,
    max_stale_cycles: int = 5,
    max_daily_loss: float = 2.5,
    streak_penalty: float = 0.3,
    stale_penalty: float = 0.2,
    daily_loss_penalty: float = 0.4,
) -> float:
    """Compute a trust multiplier ∈ [0, 1] for position sizing.

    Each factor independently penalizes; the final trust is the product
    of all penalties, so any single bad signal reduces size.
    """
    trust = 1.0

    # 1. Consecutive losses penalty
    ls = consecutive_losses.get(asset_key, 0)
    if ls >= 2:
        trust *= max(1.0 - streak_penalty * (ls - 1), 0.1)

    # 2. Stale price penalty
    stale = stale_price_cycles.get(asset_key, 0)
    if stale >= max_stale_cycles:
        # Scale penalty by how stale — full penalty at 3x threshold
        ratio = min(stale / (max_stale_cycles * 3), 1.0)
        trust *= max(1.0 - stale_penalty * ratio, 0.1)

    # 3. Daily loss penalty
    if daily_pnl_pct is not None and daily_pnl_pct < 0:
        loss_ratio = min(abs(daily_pnl_pct) / max_daily_loss, 1.0)
        trust *= max(1.0 - daily_loss_penalty * loss_ratio, 0.1)

    # 4. Position concentration penalty
    # If we're near the limit, reduce size
    if max_open_positions > 0:
        conc_ratio = open_positions_count / max_open_positions
        if conc_ratio > 0.5:
            trust *= max(1.0 - 0.3 * (conc_ratio - 0.5) / 0.5, 0.3)

    return max(trust, 0.05)  # floor at 5%


def status(trust: float) -> str:
    """Return a human label for trust level."""
    if trust >= 0.9:
        return "high"
    elif trust >= 0.7:
        return "moderate"
    elif trust >= 0.4:
        return "low"
    else:
        return "critical"
