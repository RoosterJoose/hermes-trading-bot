"""
risk.py — Phase 6: Advanced Risk Management

Provides:
  - Rolling correlation tracking between tracked assets (and vs BTC)
  - Portfolio-level max drawdown kill switch
  - VaR-based position sizing cap

Integrated into loop.py as gating layers in the entry decision chain.
All functions are deterministic — no async, no network.
"""

import math
from datetime import datetime, timezone
from typing import Dict, List, Optional


# ── Rolling Correlation ──


def rolling_pearson(returns_a: List[float], returns_b: List[float]) -> Optional[float]:
    """Compute Pearson correlation between two return series.

    Args:
        returns_a: List of periodic returns (e.g. 90 x 1h returns)
        returns_b: Parallel list for the second asset

    Returns:
        Correlation coefficient r ∈ [-1, 1], or None if insufficient data.
    """
    if len(returns_a) < 30 or len(returns_b) < 30:
        return None
    if len(returns_a) != len(returns_b):
        return None

    n = len(returns_a)
    mean_a = sum(returns_a) / n
    mean_b = sum(returns_b) / n

    cov = sum((returns_a[i] - mean_a) * (returns_b[i] - mean_b) for i in range(n))
    var_a = sum((returns_a[i] - mean_a) ** 2 for i in range(n))
    var_b = sum((returns_b[i] - mean_b) ** 2 for i in range(n))

    if var_a <= 0 or var_b <= 0:
        return None

    r = cov / math.sqrt(var_a * var_b)
    return max(-1.0, min(1.0, r))


def compute_correlations(
    bar_data: Dict[str, List[float]],
    window: int = 90,
) -> Dict[str, float]:
    """Compute rolling correlations between all asset pairs.

    Args:
        bar_data: {asset_key: [list of close prices]} — must all be same length
        window: Lookback window for correlation (default 90 bars)

    Returns:
        { "SOL_USDT_XRP_USDT": 0.65, "SOL_USDT_BTC": 0.23, ... }
    """
    if len(bar_data) < 2:
        return {}

    # Compute log returns for each asset
    returns: Dict[str, List[float]] = {}
    for asset, closes in bar_data.items():
        if len(closes) < window + 1:
            return {}
        recent = closes[-(window + 1):]
        r = []
        for i in range(1, len(recent)):
            if recent[i - 1] > 0:
                r.append(math.log(recent[i] / recent[i - 1]))
        returns[asset] = r[-window:]  # exactly window returns

    keys = list(returns.keys())
    correlations = {}
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            pair = f"{keys[i]}_{keys[j]}"
            corr = rolling_pearson(returns[keys[i]], returns[keys[j]])
            if corr is not None:
                correlations[pair] = round(corr, 4)
    return correlations


def correlation_allows_entry(
    asset_key: str,
    correlations: Dict[str, float],
    positions: Dict[str, Optional[Dict]],
    max_same_side_alt: int = 2,
    correlation_threshold: float = 0.70,
) -> tuple[bool, str]:
    """Check if a new entry is allowed given current correlation state.

    If we already hold a position in a highly-correlated asset, cap concurrent
    entries to prevent over-concentration.

    Args:
        asset_key: The asset we want to enter
        correlations: Current correlation matrix (from compute_correlations)
        positions: Current open positions (asset_key -> dict or None)
        max_same_side_alt: Max concurrent positions in correlated alts
        correlation_threshold: Correlation above this is 'high'

    Returns:
        (allowed: bool, reason: str)
    """
    # Count current open positions
    open_positions = [k for k, v in positions.items() if v is not None]

    # If no other positions, always allow
    if not open_positions:
        return True, "no_correlated_positions"

    # Check correlations between asset_key and all open positions
    high_corr_count = 0
    for pos_key in open_positions:
        # Check both orderings
        pair1 = f"{asset_key}_{pos_key}"
        pair2 = f"{pos_key}_{asset_key}"
        corr = correlations.get(pair1, correlations.get(pair2))
        if corr is not None and abs(corr) >= correlation_threshold:
            high_corr_count += 1

    if high_corr_count >= max_same_side_alt:
        return False, f"correlation_gate: {high_corr_count} highly-correlated positions"

    return True, "correlation_ok"


# ── Portfolio Max Drawdown ──


class PortfolioTracker:
    """Tracks simulated equity and computes max drawdown gate.

    Usage:
        tracker = PortfolioTracker(initial_equity=10000.0)
        tracker.update(trades)  # after each trade
        if not tracker.allow_entry():
            log("Portfolio max DD hit")
    """

    def __init__(self, initial_equity: float = 10000.0, max_drawdown_pct: float = 15.0):
        self.initial_equity = initial_equity
        self.peak_equity = initial_equity
        self.current_equity = initial_equity
        self.max_drawdown_pct = max_drawdown_pct
        self.highest_dd_pct = 0.0
        self.last_update = datetime.now(timezone.utc)

    def update(self, trades: List[Dict]) -> float:
        """Update equity curve from a list of executed trades.

        Args:
            trades: List of trade dicts with 'pnl_pct' key

        Returns:
            Current drawdown percentage from peak
        """
        for t in trades:
            pnl = t.get("pnl_pct", 0)
            self.current_equity *= 1 + pnl / 100
            if self.current_equity > self.peak_equity:
                self.peak_equity = self.current_equity
            self.last_update = datetime.now(timezone.utc)

        dd = (self.peak_equity - self.current_equity) / self.peak_equity * 100
        if dd > self.highest_dd_pct:
            self.highest_dd_pct = dd
        return dd

    def allow_entry(self) -> tuple[bool, str]:
        """Check if portfolio drawdown allows new entries."""
        dd = (self.peak_equity - self.current_equity) / self.peak_equity * 100
        if dd >= self.max_drawdown_pct:
            return False, f"portfolio_dd_{dd:.1f}%"
        return True, f"portfolio_dd_{dd:.1f}%_ok"

    def reset(self, new_equity: float):
        """Reset tracker if portfolio is rebalanced."""
        self.initial_equity = new_equity
        self.peak_equity = new_equity
        self.current_equity = new_equity
        self.highest_dd_pct = 0.0

    def status(self) -> dict:
        """Return current status dict."""
        dd = (self.peak_equity - self.current_equity) / self.peak_equity * 100
        return {
            "current_equity": round(self.current_equity, 2),
            "peak_equity": round(self.peak_equity, 2),
            "drawdown_pct": round(dd, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "highest_dd_pct": round(self.highest_dd_pct, 2),
            "allow_entry": dd < self.max_drawdown_pct,
        }


# ── VaR Position Sizing ──


def compute_var(
    closes: List[float],
    confidence: float = 0.95,
    horizon_bars: int = 1,
) -> Optional[float]:
    """Compute VaR as percentage of position (parametric, log-normal).

    Uses historical log returns to estimate volatility, then computes
    VaR assuming normally distributed returns.

    Args:
        closes: Historical close prices
        confidence: Confidence level (default 0.95 = 95%)
        horizon_bars: VaR horizon in bars (default 1)

    Returns:
        VaR as decimal (e.g. 0.023 = 2.3%), or None if insufficient data.
    """
    if len(closes) < 30:
        return None

    # Log returns
    returns = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0 and closes[i] > 0:
            returns.append(math.log(closes[i] / closes[i - 1]))

    if len(returns) < 30:
        return None

    # Sample standard deviation
    n = len(returns)
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    sigma = math.sqrt(var)

    # Scale to horizon
    sigma_horizon = sigma * math.sqrt(horizon_bars)

    # Z-score for confidence level
    # 95% -> 1.645, 99% -> 2.326, 90% -> 1.282
    z_map = {0.90: 1.282, 0.95: 1.645, 0.99: 2.326}
    z = z_map.get(confidence, 1.645)

    var_decimal = z * sigma_horizon
    return var_decimal


def var_position_cap(
    equity: float,
    var_pct: float,
    max_var_exposure: float = 0.10,
    min_cap: float = 0.01,
    max_cap: float = 0.50,
) -> float:
    """Compute position size cap based on VaR.

    Position size is capped so that the VaR (potential loss) doesn't exceed
    max_var_exposure of equity.

    Example:
        equity=$10k, var_pct=2.3%, max_var_exposure=10%
        -> cap = (10000 * 0.10) / 0.023 = $43,478
        -> but capped at 0.50 * equity = $5,000
        -> final cap = $5,000

    Args:
        equity: Current portfolio equity
        var_pct: VaR as decimal (e.g. 0.023)
        max_var_exposure: Max fraction of equity at risk per trade (default 10%)
        min_cap: Minimum position cap as fraction of equity (default 1%)
        max_cap: Maximum position cap as fraction of equity (default 50%)

    Returns:
        Position cap as fraction of equity (0.0 to 1.0)
    """
    if var_pct <= 0:
        return max_cap

    # How much equity can we risk?
    risk_budget = equity * max_var_exposure
    # What position size would risk that much given VaR?
    computed = risk_budget / var_pct
    # Convert to fraction of equity
    cap_pct = computed / equity if equity > 0 else max_cap

    # Clamp
    return max(min_cap, min(cap_pct, max_cap))
