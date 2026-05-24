"""
adaptive.py — Phase 3 Adaptive Intelligence features

All features have data-availability gates. When insufficient data exists,
they gracefully fall back with a logged status and return inactive.
This allows coding all features now while they auto-activate as the
1m bar store accumulates.

Features:
  1. Dynamic RSI Percentiles — adaptive entry threshold from 1m RSI distribution
  2. Hurst Exponent (Phase 3b) — mean-reversion vs trending regime classification
  3. CUSUM Regime Detection (Phase 3c) — cumulative sum regime shift monitoring
"""

import math
import sqlite3
import numpy as np
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent.parent
BAR_DB_PATH = BASE_DIR / "data" / "bars.db"


def _read_1m_closes(asset_key: str) -> tuple[list[float], int]:
    """Read 1m close prices from SQLite bar store.

    Returns:
        (closes: list[float], bar_count: int)
    """
    if not BAR_DB_PATH.exists():
        return [], 0

    try:
        conn = sqlite3.connect(str(BAR_DB_PATH))
        rows = conn.execute(
            "SELECT close FROM bars WHERE asset = ? ORDER BY timestamp ASC",
            (asset_key,),
        ).fetchall()
        conn.close()
    except Exception:
        return [], 0

    closes = [r[0] for r in rows]
    return closes, len(closes)


def _compute_rsi_series(closes: list[float], period: int = 14) -> list[float]:
    """Compute full RSI series (SMA method, matching loop.py _calc_rsi).

    Returns list of RSI values, one per bar after the initial period.
    """
    rsi_values = []
    bar_count = len(closes)
    for i in range(period, bar_count):
        segment = closes[i - period : i + 1]
        deltas = [segment[j] - segment[j - 1] for j in range(1, len(segment))]
        gains = sum(d for d in deltas if d > 0)
        losses = sum(-d for d in deltas if d < 0)
        avg_gain = gains / period
        avg_loss = losses / period

        if avg_loss == 0:
            rsi_values.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi_values.append(100.0 - 100.0 / (1.0 + rs))

    return rsi_values


# ──────────────────────────────────────────────────────────────────────
# Feature 1: Dynamic RSI Percentile
# ──────────────────────────────────────────────────────────────────────


def compute_dynamic_rsi_threshold(
    asset_key: str,
    config: dict,
) -> dict:
    """Compute dynamic RSI entry threshold from 1m bar history.

    Reads 1m bars from the SQLite store, computes a series of RSI values
    across the entire history, then takes the Nth percentile as the entry
    threshold. Auto-adapts to changing volatility.

    Config keys (from strategy.yaml dynamic_rsi section):
      enabled (bool)           — master switch (default: True)
      min_bars (int)           — minimum bars before activating (default: 1440 = 1 day)
      entry_percentile (float) — RSI percentile to use as buy threshold (default: 5)
      exit_percentile (float)  — RSI percentile for take-profit signal (default: 95)
      rsi_period (int)         — RSI lookback period (default: 14)

    Returns dict:
      active         — bool, whether dynamic threshold is active
      threshold      — float or None, the entry threshold
      exit_threshold — float or None, the exit level
      percentile     — float, percentile used
      bars_used      — int, bars consumed
      rsi_values     — int, RSI values computed
      reason         — str, status message
    """
    result = {
        "active": False,
        "threshold": None,
        "exit_threshold": None,
        "percentile": None,
        "bars_used": 0,
        "rsi_values": 0,
        "reason": "not enabled",
    }

    if not config.get("enabled", True):
        return result

    min_bars = config.get("min_bars", 1440)
    entry_pct = config.get("entry_percentile", 5)
    exit_pct = config.get("exit_percentile", 95)
    rsi_period = config.get("rsi_period", 14)

    closes, bar_count = _read_1m_closes(asset_key)
    if not closes:
        result["reason"] = "bar DB empty or not found"
        return result

    if bar_count < min_bars:
        result["reason"] = f"need {min_bars} bars, have {bar_count}"
        result["bars_available"] = bar_count
        return result

    if bar_count < rsi_period + 2:
        result["reason"] = f"need {rsi_period + 2} bars for RSI, have {bar_count}"
        return result

    rsi_values = _compute_rsi_series(closes, rsi_period)

    if len(rsi_values) < 100:
        result["reason"] = f"only {len(rsi_values)} RSI values (need 100+ for stable percentile)"
        return result

    # Compute percentile thresholds
    threshold = float(np.percentile(rsi_values, entry_pct))
    exit_val = float(np.percentile(rsi_values, exit_pct))

    # Guard against degenerate values (can happen with flat markets)
    threshold = max(5.0, min(95.0, threshold))

    result["active"] = True
    result["threshold"] = round(threshold, 1)
    result["exit_threshold"] = round(exit_val, 1)
    result["percentile"] = entry_pct
    result["bars_used"] = bar_count
    result["rsi_values"] = len(rsi_values)
    result["reason"] = "active"

    return result


# ──────────────────────────────────────────────────────────────────────
# Feature 2: Hurst Exponent — Regime Classification (Phase 3b)
# ──────────────────────────────────────────────────────────────────────


def _r_s_hurst_exponent(
    prices: list[float],
    max_lags: int = 20,
) -> Optional[float]:
    """Compute Hurst exponent via R/S (Rescaled Range) analysis.

    Parameters:
        prices   — list of close prices (will be converted to log returns)
        max_lags — max number of R/S lags (default: 20, power-of-2 spacing)

    Returns:
        H value or None if insufficient data

    Interpretation:
        H < 0.40  → strongly mean-reverting
        H < 0.45  → mean-reverting
        H ≈ 0.50  → random walk (efficient market)
        H > 0.55  → trending
        H > 0.60  → strongly trending
    """
    if len(prices) < 100:
        return None

    log_returns = np.diff(np.log(prices))

    if len(log_returns) < 50:
        return None

    max_power = min(int(np.floor(np.log2(len(log_returns)))), max_lags)
    if max_power < 2:
        return None

    tau_list = []
    rs_list = []

    for power in range(2, max_power + 1):
        tau = int(2**power)
        if tau < 2 or tau > len(log_returns):
            continue

        n_chunks = len(log_returns) // tau
        if n_chunks < 2:
            continue

        chunks = log_returns[: n_chunks * tau].reshape(n_chunks, tau)

        chunk_rs = []
        for chunk in chunks:
            adjusted = chunk - np.mean(chunk)
            cumsum = np.cumsum(adjusted)
            r = float(np.max(cumsum) - np.min(cumsum))
            s = float(np.std(chunk, ddof=1))
            if s > 0 and r > 0:
                chunk_rs.append(r / s)

        if chunk_rs:
            tau_list.append(tau)
            rs_list.append(np.mean(chunk_rs))

    if len(tau_list) < 4 or len(rs_list) < 4:
        return None

    H, _ = np.polyfit(np.log(tau_list), np.log(rs_list), 1)
    return float(max(0.0, min(1.0, H)))


def compute_hurst_exponent(
    asset_key: str,
    config: dict,
) -> dict:
    """Compute Hurst exponent from 1m bar data for regime classification.

    The Hurst exponent measures long-term memory in the price series:
      H < 0.40  → strongly mean-reverting
      H 0.40-0.45 → mean-reverting
      H 0.45-0.55 → random walk
      H > 0.55  → trending
      H > 0.60  → strongly trending

    Config keys (from strategy.yaml hurst section):
      enabled (bool)            — master switch (default: True)
      min_bars (int)            — minimum bars before activating (default: 500)
      mr_threshold (float)      — H below this = mean-reverting (default: 0.45)
      trend_threshold (float)   — H above this = trending (default: 0.55)
      block_on_trending (bool)  — whether to block entries when trending (default: True)

    Returns dict:
      active       — bool, whether the feature is active (sufficient data)
      hurst        — float or None, the H value
      regime       — str
      bars_used    — int, bars consumed
      block_entry  — bool, whether to block mean-reversion entry
      reason       — str, status message
    """
    result = {
        "active": False,
        "hurst": None,
        "regime": "insufficient_data",
        "bars_used": 0,
        "block_entry": False,
        "reason": "not enabled",
    }

    if not config.get("enabled", True):
        return result

    min_bars = config.get("min_bars", 500)
    mr_threshold = config.get("mr_threshold", 0.45)
    trend_threshold = config.get("trend_threshold", 0.55)
    block_on_trending = config.get("block_on_trending", True)

    closes, bar_count = _read_1m_closes(asset_key)
    if not closes:
        result["reason"] = "bar DB empty or not found"
        return result

    if bar_count < min_bars:
        result["reason"] = f"need {min_bars} bars, have {bar_count}"
        result["bars_available"] = bar_count
        return result

    H = _r_s_hurst_exponent(closes)
    if H is None:
        result["reason"] = f"Hurst: computation failed on {bar_count} bars"
        return result

    result["active"] = True
    result["hurst"] = round(H, 4)
    result["bars_used"] = bar_count

    if H < 0.40:
        result["regime"] = "strongly_mean_reverting"
        result["block_entry"] = False
        result["reason"] = f"H={H:.4f} — strongly mean-reverting, favourable for MR"
    elif H < mr_threshold:
        result["regime"] = "mean_reverting"
        result["block_entry"] = False
        result["reason"] = f"H={H:.4f} — mean-reverting, MR favourable"
    elif H <= trend_threshold:
        result["regime"] = "random_walk"
        result["block_entry"] = False
        result["reason"] = f"H={H:.4f} — random walk, neutral"
    elif H < 0.60:
        result["regime"] = "trending"
        result["block_entry"] = block_on_trending
        result["reason"] = f"H={H:.4f} — trending, block MR entries"
    else:
        result["regime"] = "strongly_trending"
        result["block_entry"] = block_on_trending
        result["reason"] = f"H={H:.4f} — strongly trending, block MR entries"

    return result


# ──────────────────────────────────────────────────────────────────────
# Feature 3: CUSUM Regime Detection (Phase 3c)
# ──────────────────────────────────────────────────────────────────────


def _cusum_detection(
    log_returns: np.ndarray,
    baseline_mean: float,
    baseline_std: float,
    k: float = 0.5,
    h: float = 4.0,
) -> tuple[int, int]:
    """Run two-sided CUSUM on log returns.

    Standard cumulative-sum control chart:
      S_high = max(0, S_high + (x - mu - k*sigma))
      S_low  = min(0, S_low  + (x - mu + k*sigma))

    Returns:
        (up_breaks, down_breaks) — count of CUSUM breaks in each direction
    """
    up_breaks = 0
    down_breaks = 0
    S_high = 0.0
    S_low = 0.0
    k_sigma = k * baseline_std
    h_sigma = h * baseline_std

    for x in log_returns:
        S_high = max(0.0, S_high + (x - baseline_mean - k_sigma))
        if S_high > h_sigma:
            up_breaks += 1
            S_high = 0.0

        S_low = min(0.0, S_low + (x - baseline_mean + k_sigma))
        if S_low < -h_sigma:
            down_breaks += 1
            S_low = 0.0

    return up_breaks, down_breaks


def compute_cusum_regime(
    asset_key: str,
    config: dict,
) -> dict:
    """Run CUSUM regime detection on 1m log returns.

    Detects structural shifts in the return distribution that aren't
    captured by Hurst or ADX. Multiple CUSUM breaks indicate regime
    instability where mean-reversion is unreliable.

    Config keys (from strategy.yaml cusum section):
      enabled (bool)             — master switch (default: True)
      min_bars (int)             — minimum bars before activating (default: 300)
      baseline_window (int)      — rolling window for baseline mean/std (default: 100)
      k (float)                  — CUSUM slack in std units (default: 0.5)
      h (float)                  — CUSUM decision interval in std units (default: 4.0)
      break_threshold (int)      — max breaks before flagging unstable (default: 3)
      block_on_unstable (bool)   — block entries when regime is unstable (default: True)
      block_on_up_drift (bool)   — block entries when upward drift detected (default: True)

    Returns dict:
      active         — bool
      regime         — str: "normal", "shifting_down", "shifting_up", "unstable"
      up_breaks      — int, upward CUSUM break count
      down_breaks    — int, downward CUSUM break count
      total_breaks   — int, combined break count
      baseline_mean  — float
      baseline_std   — float
      block_entry    — bool
      reason         — str
    """
    result = {
        "active": False,
        "regime": "insufficient_data",
        "up_breaks": 0,
        "down_breaks": 0,
        "total_breaks": 0,
        "baseline_mean": None,
        "baseline_std": None,
        "block_entry": False,
        "reason": "not enabled",
    }

    if not config.get("enabled", True):
        return result

    min_bars = config.get("min_bars", 300)
    baseline_window = config.get("baseline_window", 100)
    k = config.get("k", 0.5)
    h = config.get("h", 4.0)
    break_threshold = config.get("break_threshold", 3)
    block_on_unstable = config.get("block_on_unstable", True)
    block_on_up_drift = config.get("block_on_up_drift", True)

    closes, bar_count = _read_1m_closes(asset_key)
    if not closes:
        result["reason"] = "bar DB empty or not found"
        return result

    if bar_count < min_bars:
        result["reason"] = f"need {min_bars} bars, have {bar_count}"
        result["bars_available"] = bar_count
        return result

    prices = np.array(closes, dtype=float)
    log_returns = np.diff(np.log(prices))

    if len(log_returns) < baseline_window + 50:
        result["reason"] = f"need {baseline_window + 50} log returns, have {len(log_returns)}"
        return result

    # Split: first baseline_window values establish baseline
    baseline_returns = log_returns[:baseline_window]
    test_returns = log_returns[baseline_window:]

    baseline_mean = float(np.mean(baseline_returns))
    baseline_std = float(np.std(baseline_returns, ddof=1))

    if baseline_std < 1e-10:
        result["reason"] = "baseline std near zero (flat market)"
        result["baseline_mean"] = baseline_mean
        result["baseline_std"] = baseline_std
        return result

    up_breaks, down_breaks = _cusum_detection(
        test_returns, baseline_mean, baseline_std, k, h
    )
    total_breaks = up_breaks + down_breaks

    result["active"] = True
    result["baseline_mean"] = round(baseline_mean, 8)
    result["baseline_std"] = round(baseline_std, 8)
    result["up_breaks"] = up_breaks
    result["down_breaks"] = down_breaks
    result["total_breaks"] = total_breaks
    result["bars_used"] = bar_count

    if total_breaks >= break_threshold:
        result["regime"] = "unstable"
        result["block_entry"] = block_on_unstable
        result["reason"] = (
            f"unstable: {total_breaks} CUSUM breaks ({up_breaks}↑/{down_breaks}↓) "
            f"exceed threshold {break_threshold}"
        )
    elif up_breaks > down_breaks:
        result["regime"] = "shifting_up"
        result["block_entry"] = block_on_up_drift
        result["reason"] = (
            f"shifting up: {up_breaks}↑ vs {down_breaks}↓ CUSUM breaks"
        )
    elif down_breaks > 0:
        result["regime"] = "shifting_down"
        result["block_entry"] = False
        result["reason"] = (
            f"shifting down: {down_breaks}↓ vs {up_breaks}↑ CUSUM breaks"
        )
    else:
        result["regime"] = "normal"
        result["block_entry"] = False
        result["reason"] = "normal: no CUSUM breaks detected"

    return result


# ──────────────────────────────────────────────────────────────────────
# Feature 4: Per-Asset Percentile RSI Threshold (spec-based)
# ──────────────────────────────────────────────────────────────────────

def compute_rsi_percentile_threshold(
    asset_key: str,
    min_bars: int = 500,
) -> dict:
    """Compute per-asset percentile RSI entry threshold from bars.db.

    Majors (BTC/ETH/SOL) use the 5th percentile (q05).
    Alts use the 10th percentile (q10).

    This adapts the threshold to each asset's own RSI distribution,
    avoiding one-size-fits-all fixed thresholds.

    Args:
        asset_key: e.g. 'BTC_USDT'
        min_bars: minimum 1m bars required before computing (default: 500)

    Returns dict:
      active     — bool, whether threshold was computed
      threshold  — float or None, the RSI entry threshold
      percentile — int, which percentile was used (5 or 10)
      bars_used  — int, bars consumed
      reason     — str, status message
    """
    symbol = asset_key.split("_")[0]
    if symbol in ("BTC", "ETH", "SOL"):
        percentile = 5
    else:
        percentile = 10

    closes, bar_count = _read_1m_closes(asset_key)
    if not closes:
        return {"active": False, "threshold": None, "percentile": percentile,
                "bars_used": 0, "reason": "bar DB empty or not found"}

    if bar_count < min_bars:
        return {"active": False, "threshold": None, "percentile": percentile,
                "bars_used": bar_count, "reason": f"need {min_bars} bars, have {bar_count}"}

    if bar_count < 16:
        return {"active": False, "threshold": None, "percentile": percentile,
                "bars_used": bar_count, "reason": "need 16+ bars for RSI(14)"}

    rsi_values = _compute_rsi_series(closes, period=14)
    if len(rsi_values) < 100:
        return {"active": False, "threshold": None, "percentile": percentile,
                "bars_used": bar_count, "reason": f"only {len(rsi_values)} RSI values (need 100+)"}

    # Data quality check: if >30% of RSI values are at extremes (≥99 or ≤1),
    # the underlying bar data is too flat for meaningful percentiles.
    extreme_ratio = sum(1 for r in rsi_values if r >= 99 or r <= 1) / len(rsi_values)
    if extreme_ratio > 0.30:
        return {"active": False, "threshold": None, "percentile": percentile,
                "bars_used": bar_count,
                "reason": f"flat data: {extreme_ratio:.0%} RSI at extremes (need ≤30%)"}

    threshold = float(np.percentile(rsi_values, percentile))
    threshold = max(5.0, min(95.0, threshold))

    return {
        "active": True,
        "threshold": round(threshold, 1),
        "percentile": percentile,
        "bars_used": bar_count,
        "reason": f"active ({percentile}th pctile={threshold:.1f}, {bar_count} bars)",
    }


def check_vol_sanity(asset_key: str, n_bars: int = 60) -> dict:
    """Check if annualized 1m realized volatility exceeds sane limits.

    Reads the last n_bars 1m candles from bars.db, computes log-return
    volatility, annualizes by sqrt(525600), and blocks if > 3.0.
    This filters broken/corrupted data feeds without blocking genuine
    high volatility.

    Args:
        asset_key: e.g. 'BTC_USDT'
        n_bars: number of recent 1m bars to check (default: 60 = 1 hour)

    Returns dict:
      active     — bool, True if vol was computed
      sane       — bool, True if vol ≤ 3.0 (OK to trade), False if broken
      vol_ann    — float or None, annualized vol value
      reason     — str, status message
    """
    result = {"active": False, "sane": True, "vol_ann": None, "reason": "insufficient_data"}

    closes, bar_count = _read_1m_closes(asset_key)
    if not closes or len(closes) < 3:
        result["reason"] = "bar DB empty or too few bars"
        return result

    recent = closes[-n_bars:] if len(closes) >= n_bars else closes
    if len(recent) < 3:
        result["reason"] = f"only {len(recent)} bars (need 3+)"
        return result

    log_returns = np.diff(np.log(recent))
    if len(log_returns) < 2:
        result["reason"] = "too few log returns"
        return result

    sigma_1m = float(np.std(log_returns))
    if sigma_1m <= 0:
        result["reason"] = "zero vol (flat prices)"
        result["active"] = True
        result["sane"] = True
        result["vol_ann"] = 0.0
        return result

    # Annualize: 525600 minutes per year (365 * 24 * 60)
    vol_ann = sigma_1m * math.sqrt(525600)
    result["active"] = True
    result["vol_ann"] = round(vol_ann, 4)
    result["sane"] = vol_ann <= 3.0

    if vol_ann > 3.0:
        result["reason"] = f"annualized 1m vol {vol_ann:.2f} > 3.0 (broken feed)"
    else:
        result["reason"] = f"vol OK ({vol_ann:.2f} annualized)"

    return result
