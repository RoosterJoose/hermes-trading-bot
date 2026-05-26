"""
reflect.py — Reflection engine with market context analysis.

THREE modes:
  --mode fallback:   Deterministic rules. Adjusts ONE variable based on score + market conditions.
  --mode summarize:  Outputs structured analysis of trades, scores, market patterns.
  --mode hermes:     [Placeholder] Full AI-driven reflection.

Always changes exactly ONE variable per cycle. Bumps version. Preserves history.
"""

import argparse
import json
import os
import shutil
import sys
import yaml
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__))).parent


def score_trades_for_asset(asset_key: str, trades: list, goal: dict):
    from hermes_trading.score import score_trades

    return score_trades(trades, goal)


def load_trades(state_dir: Path, asset_key: str, limit: int = 100) -> list:
    trades_file = state_dir / asset_key / "trades.jsonl"
    if not trades_file.exists():
        return []
    trades = []
    with open(trades_file) as f:
        for line in f:
            line = line.strip()
            if line:
                trades.append(json.loads(line))
    return trades[-limit:]


def load_strategy_snapshot(
    state_dir: Path, asset_key: str, version: str
) -> Optional[dict]:
    path = state_dir / asset_key / "history" / f"v{version}.yaml"
    if not path.exists():
        return None
    with open(path) as f:
        strategy = yaml.safe_load(f)
    return strategy


def load_all_hypotheses(state_dir: Path, asset_key: str) -> list:
    hyp_file = state_dir / asset_key / "hypotheses.jsonl"
    if not hyp_file.exists():
        return []
    hypotheses = []
    with open(hyp_file) as f:
        for line in f:
            line = line.strip()
            if line:
                hypotheses.append(json.loads(line))
    return hypotheses


def load_current_strategy(state_dir: Path, asset_key: str) -> Optional[dict]:
    strat_path = state_dir / asset_key / "strategy.yaml"
    if not strat_path.exists():
        return None
    with open(strat_path) as f:
        return yaml.safe_load(f)


def append_hypothesis(state_dir: Path, asset_key: str, hypothesis: dict):
    hyp_file = state_dir / asset_key / "hypotheses.jsonl"
    hyp_file.parent.mkdir(parents=True, exist_ok=True)
    hypothesis["timestamp"] = datetime.now(timezone.utc).isoformat()
    with open(hyp_file, "a") as f:
        f.write(json.dumps(hypothesis) + "\n")


def save_strategy_version(state_dir: Path, asset_key: str, strategy: dict):
    ver = strategy.get("version", "00")
    history_dir = state_dir / asset_key / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    dst = history_dir / f"v{ver}.yaml"
    with open(dst, "w") as f:
        yaml.dump(strategy, f, default_flow_style=False)


def write_strategy(state_dir: Path, asset_key: str, strategy: dict):
    """Write strategy to disk with hard absolute bounds enforced at write time.
    
    These bounds are the last-mile safety layer — they clamp any values that
    may have been set by LLM hermes mode, fallback reflect, or manual edits
    to prevent extreme/unreasonable parameters from reaching the trading loop.
    """
    # ── Exit param hard bounds (non-bypassable last-mile enforcement) ──
    bounds = {
        "scale_out_min_R": (0.1, 2.0),
        "tp2_target_R": (0.5, 5.0),
        "tp2_slice": (0.1, 0.9),
        "chandelier_mult_alts": (1.0, 10.0),
        "chandelier_mult_major": (1.0, 6.0),
        "stop_loss_pct": (0.3, 10.0),
    }
    for key, (lo, hi) in bounds.items():
        if key in strategy:
            strategy[key] = round(max(lo, min(hi, strategy[key])), 4)
    
    # Sub-dict bounds (btc_gate, entry thresholds, etc.)
    btc_gate = strategy.get("btc_gate", {})
    if "min_btc_1h_rsi" in btc_gate:
        btc_gate["min_btc_1h_rsi"] = max(5, min(60, btc_gate["min_btc_1h_rsi"]))
    
    entry = strategy.get("entry", {})
    if "threshold" in entry:
        entry["threshold"] = max(5, min(50, entry["threshold"]))
    
    fng_gate = strategy.get("fng_gate", {})
    if "min_value" in fng_gate:
        fng_gate["min_value"] = max(1, min(50, fng_gate["min_value"]))
    
    strat_path = state_dir / asset_key / "strategy.yaml"
    with open(strat_path, "w") as f:
        yaml.dump(strategy, f, default_flow_style=False)


def analyze_market_patterns(trades: list) -> dict:
    """Analyze trade outcomes grouped by market conditions to find patterns."""
    if not trades:
        return {"has_data": False, "findings": []}

    # Group trades by BTC RSI ranges
    btc_buckets = {
        "btc_4h_oversold": {"label": "BTC 4h RSI < 30", "trades": [], "pnls": []},
        "btc_4h_low": {"label": "BTC 4h RSI 30-40", "trades": [], "pnls": []},
        "btc_4h_mid": {"label": "BTC 4h RSI 40-60", "trades": [], "pnls": []},
        "btc_4h_high": {"label": "BTC 4h RSI > 60", "trades": [], "pnls": []},
        "btc_1h_oversold": {"label": "BTC 1h RSI < 25", "trades": [], "pnls": []},
        "btc_1h_normal": {"label": "BTC 1h RSI ≥ 25", "trades": [], "pnls": []},
    }

    fng_buckets = {
        "extreme_fear": {"label": "Fear & Greed < 15", "trades": [], "pnls": []},
        "fear": {"label": "Fear & Greed 15-30", "trades": [], "pnls": []},
        "neutral": {"label": "Fear & Greed > 30", "trades": [], "pnls": []},
    }

    for t in trades:
        pnl = t.get("pnl_pct", 0)

        btc_4h = t.get("btc_entry_4h_rsi")
        if btc_4h is not None:
            if btc_4h < 30:
                btc_buckets["btc_4h_oversold"]["trades"].append(t)
                btc_buckets["btc_4h_oversold"]["pnls"].append(pnl)
            elif btc_4h < 40:
                btc_buckets["btc_4h_low"]["trades"].append(t)
                btc_buckets["btc_4h_low"]["pnls"].append(pnl)
            elif btc_4h < 60:
                btc_buckets["btc_4h_mid"]["trades"].append(t)
                btc_buckets["btc_4h_mid"]["pnls"].append(pnl)
            else:
                btc_buckets["btc_4h_high"]["trades"].append(t)
                btc_buckets["btc_4h_high"]["pnls"].append(pnl)

        btc_1h = t.get("btc_entry_1h_rsi")
        if btc_1h is not None:
            if btc_1h < 25:
                btc_buckets["btc_1h_oversold"]["trades"].append(t)
                btc_buckets["btc_1h_oversold"]["pnls"].append(pnl)
            else:
                btc_buckets["btc_1h_normal"]["trades"].append(t)
                btc_buckets["btc_1h_normal"]["pnls"].append(pnl)

        fng = t.get("fear_greed_entry")
        if fng is not None:
            if fng < 15:
                fng_buckets["extreme_fear"]["trades"].append(t)
                fng_buckets["extreme_fear"]["pnls"].append(pnl)
            elif fng < 30:
                fng_buckets["fear"]["trades"].append(t)
                fng_buckets["fear"]["pnls"].append(pnl)
            else:
                fng_buckets["neutral"]["trades"].append(t)
                fng_buckets["neutral"]["pnls"].append(pnl)

    # Summarize findings
    findings = []
    for bucket_name, bucket in {**btc_buckets, **fng_buckets}.items():
        pnls = bucket["pnls"]
        if len(pnls) >= 2:
            avg = sum(pnls) / len(pnls)
            win_rate = len([p for p in pnls if p > 0]) / len(pnls) * 100
            findings.append(
                {
                    "condition": bucket["label"],
                    "count": len(pnls),
                    "avg_pnl": round(avg, 2),
                    "win_rate": round(win_rate, 0),
                    "total_pnl": round(sum(pnls), 2),
                }
            )

    findings.sort(key=lambda f: abs(f["avg_pnl"]), reverse=True)

    return {
        "has_data": len(trades) >= 2,
        "findings": findings,
        "total_trades": len(trades),
    }


def detect_loss_clustering(trades: list, min_cluster: int = 3) -> dict:
    """Detect clustered losing trades using a runs test.

    Two tests:
      1. Max consecutive losses — find the longest loss streak
      2. Runs test (Wald-Wolfowitz) — count runs of consecutive wins/losses.
         If trades were random, we expect roughly N*(2*w*l)/(w+l)+1 runs.
         Significantly fewer runs = clustering (losses stick together).

    Returns dict with:
      max_consecutive_losses (int)
      runs (int) — observed number of runs
      expected_runs (float) — expected runs under independence
      z_score (float) — how many std devs from expected
      regime_shift_detected (bool) — True if P < 0.05 for clustering
      message (str) — human-readable summary
    """
    result = {
        "max_consecutive_losses": 0,
        "runs": 0,
        "expected_runs": 0.0,
        "z_score": 0.0,
        "p_value": 1.0,
        "regime_shift_detected": False,
        "message": "",
    }

    if len(trades) < 10:
        result["message"] = "Need at least 10 trades for clustering analysis"
        return result

    # Sort by exit_time
    sorted_trades = sorted(trades, key=lambda t: t.get("exit_time", ""))
    outcomes = []  # +1 for win, -1 for loss
    for t in sorted_trades:
        pnl = t.get("pnl_pct", 0)
        outcomes.append(1 if pnl > 0 else -1)

    # 1. Run-length encoding — find max consecutive same-sign runs
    max_run = 0
    current_run = 1
    for i in range(1, len(outcomes)):
        if outcomes[i] == outcomes[i - 1]:
            current_run += 1
        else:
            max_run = max(max_run, current_run)
            current_run = 1
    max_run = max(max_run, current_run)

    # 2. Count runs (Wald-Wolfowitz)
    runs = 1
    for i in range(1, len(outcomes)):
        if outcomes[i] != outcomes[i - 1]:
            runs += 1

    n_wins = sum(1 for o in outcomes if o == 1)
    n_losses = len(outcomes) - n_wins

    # Expected runs under independence (null hypothesis)
    n = len(outcomes)
    expected_runs = 1.0 + (2.0 * n_wins * n_losses) / n if n > 0 else 1.0

    # Std dev of runs under null
    numerator = (2.0 * n_wins * n_losses) * (2.0 * n_wins * n_losses - n)
    denominator = (n * n * (n - 1)) if n > 1 else 1
    std_runs = max(1.0, (numerator / denominator) ** 0.5) if denominator > 0 else 1.0

    # Z-score: how many std devs from expected?
    z_score = (runs - expected_runs) / std_runs if std_runs > 0 else 0.0

    # P-value (one-sided: fewer runs than expected = clustering)
    # Normal approximation: P(Z < z_score)
    # Using Abramowitz & Stegun approximation for standard normal CDF
    abs_z = abs(z_score)
    t = 1.0 / (1.0 + 0.2316419 * abs_z)
    phi = 0.3989422804014327 * __exp(-abs_z * abs_z / 2.0)
    p = 1.0 - phi * (
        t
        * (
            0.319381530
            + t
            * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429)))
        )
    )

    # For fewer runs than expected (negative z), we want P(Z < z)
    p_value = 1.0 - p if z_score < 0 else p
    # One-tailed: we only care about fewer runs (clustering of losses)
    if z_score > 0:
        p_value = 1.0  # more runs than expected = anti-clustering, not a concern

    # Find the longest loss run specifically
    loss_run = 0
    current_loss = 0
    for o in outcomes:
        if o == -1:
            current_loss += 1
            loss_run = max(loss_run, current_loss)
        else:
            current_loss = 0

    result["max_consecutive_losses"] = loss_run
    result["runs"] = runs
    result["expected_runs"] = round(expected_runs, 2)
    result["z_score"] = round(z_score, 3)
    result["p_value"] = round(p_value, 4)
    result["regime_shift_detected"] = p_value < 0.05 and loss_run >= min_cluster

    # Build message
    if not n_losses:
        result["message"] = "No losing trades — no clustering to detect"
    elif result["regime_shift_detected"]:
        result["message"] = (
            f"REGIME SHIFT: {loss_run} max consecutive losses (P={p_value:.4f}, {runs} runs vs {expected_runs:.0f} expected)"
        )
    elif loss_run >= min_cluster:
        result["message"] = (
            f"{loss_run} max consecutive losses (P={p_value:.4f}) — watch for further clustering"
        )
    else:
        result["message"] = (
            f"No significant clustering: {loss_run} max consecutive losses, {runs} runs"
        )

    return result


def __exp(x: float) -> float:
    """Quick exp for the clustering test (avoids importing math inside reflect)."""
    # Use built-in if possible
    import math

    return math.exp(x)


def summarize(state_dir: Path, asset_key: str, goal: dict, trades: list) -> str:
    """Generate a structured analysis report with market context patterns."""
    lines = []
    lines.append(f"⚡ REFLECTION REPORT — {asset_key}")
    lines.append("=" * 60)

    # Current strategy
    current = load_current_strategy(state_dir, asset_key)
    if current:
        lines.append(f"\n📋 Current Strategy (v{current.get('version', '??')}):")
        lines.append(
            f"   Entry: {current.get('entry', {}).get('indicator', 'rsi')} < {current.get('entry', {}).get('threshold', 30)}"
        )
        lines.append(f"   Stop Loss: {current.get('stop_loss_pct', 2.0)}%")
        lines.append(
            f"   Take Profit: {current.get('stop_loss_pct', 2.0) * 2}% (2x stop)"
        )
        lines.append(f"   Position Size: {current.get('position_size_r', 0.5)}R")
        lines.append(f"   Cooldown: {current.get('cooldown_cycles', 30)} cycles")
        btc = current.get("btc_gate", {})
        lines.append(
            f"   BTC Gate: 1h min RSI {btc.get('min_btc_1h_rsi', 20)}"
        )
        fng = current.get("fng_gate", {})
        lines.append(f"   FnG Gate: min value {fng.get('min_value', 10)}")

        # ── Stop-Loss ATR Suitability (NB recommendation) ──
        sl_pct = current.get("stop_loss_pct", 2.0)
        atr_pct = current.get("atr_sl_floor_pct", 1.0)
        # Rough ATR check: compare stop to ATR
        atr_multiple = sl_pct / atr_pct if atr_pct > 0 else 0
        lines.append(f"\n🛑 Stop-Loss ATR Assessment:")
        lines.append(f"   Stop: {sl_pct}% | ATR floor: {atr_pct}% | Ratio: {atr_multiple:.1f}x ATR")
        if atr_multiple < 1.5:
            lines.append(f"   ⚠️  Stop ({atr_multiple:.1f}x ATR) may be too tight for MR per NB guidance (1.5-3.0x ATR recommended)")
            lines.append(f"   → Consider widening stop_loss_pct or lowering atr_sl_mult")
        elif atr_multiple > 4.0:
            lines.append(f"   ⚠️  Stop ({atr_multiple:.1f}x ATR) may be too wide — consider tightening")

    # Trade statistics
    lines.append(f"\n📊 Trade Statistics ({len(trades)} trades):")
    if trades:
        pnls = [t.get("pnl_pct", 0) for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        total_pnl = sum(pnls)
        avg_pnl = sum(pnls) / len(pnls) if pnls else 0
        win_rate = len(wins) / len(trades) * 100 if trades else 0
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        max_loss = min(losses) if losses else 0
        max_win = max(wins) if wins else 0

        lines.append(f"   Total PnL: {total_pnl:+.2f}%")
        lines.append(f"   Avg PnL: {avg_pnl:+.2f}%")
        lines.append(f"   Win Rate: {win_rate:.0f}%")
        lines.append(f"   Avg Win: {avg_win:+.2f}% | Avg Loss: {avg_loss:+.2f}%")
        lines.append(f"   Best Trade: {max_win:+.2f}% | Worst Trade: {max_loss:+.2f}%")

        # R-multiple distribution
        sl_pct = current.get("stop_loss_pct", 2.0) if current else 2.0
        r_multiples = [p / sl_pct if sl_pct else 0 for p in pnls] if sl_pct else []
        zero_r = sum(1 for r in r_multiples if abs(r) < 0.1)
        small_r = sum(1 for r in r_multiples if 0.1 <= abs(r) < 0.5)
        mid_r = sum(1 for r in r_multiples if 0.5 <= abs(r) < 1.5)
        big_r = sum(1 for r in r_multiples if abs(r) >= 1.5)
        avg_r = sum(r_multiples) / len(r_multiples) if r_multiples else 0
        win_r = [r for r in r_multiples if r > 0]
        loss_r = [r for r in r_multiples if r < 0]
        lines.append(f"\n📐 R-Multiple Distribution (stop={sl_pct}%):")
        lines.append(f"   Avg R: {avg_r:.2f} | Win R: {sum(win_r) / len(win_r):.2f} ({len(win_r)} wins)" if win_r else f"   Avg R: {avg_r:.2f} | Win R: N/A")
        lines.append(f"   Loss R: {sum(loss_r) / len(loss_r):.2f} ({len(loss_r)} losses)" if loss_r else f"   Loss R: N/A")
        lines.append(f"   ~0R: {zero_r} | ±0.1-0.5R: {small_r} | ±0.5-1.5R: {mid_r} | ±1.5R+: {big_r}")
        lines.append(f"   ⚠️  If most trades cluster at ~0R, exit is the problem — add/modify exit params")

        # ── Rolling 20-trade win-rate (win-rate velocity) ──
        recent_20 = trades[-20:] if len(trades) >= 20 else trades
        r20_wins = sum(1 for t in recent_20 if t.get("pnl_pct", 0) > 0)
        r20_total = len(recent_20)
        r20_wr = r20_wins / r20_total * 100 if r20_total > 0 else 0
        wr_delta = r20_wr - win_rate if r20_total > 0 else 0
        wr_emoji = "🚀" if wr_delta > 5 else ("⚠️" if wr_delta < -5 else "➡️")
        lines.append(f"\n📈 Win-Rate Velocity:")
        lines.append(f"   Cumulative WR ({len(trades)}t): {win_rate:.0f}% | "
                      f"Recent WR ({r20_total}t): {r20_wr:.0f}% ({wr_emoji} Δ{wr_delta:+.0f}pp)")
        if wr_delta < -5:
            lines.append(f"   ⚠️  Recent WR {r20_wr:.0f}% significantly below cumulative — possible regime shift or strategic drift")

        # ── Expectancy momentum: recent avg R vs all-time baseline ──
        r20_r = [t.get("pnl_pct", 0) / sl_pct if sl_pct else 0 for t in recent_20]
        r20_avg_r = sum(r20_r) / len(r20_r) if r20_r else 0
        r_delta = r20_avg_r - avg_r
        expect_emoji = "📈" if r_delta > 0.1 else ("📉" if r_delta < -0.1 else "➡️")
        lines.append(f"\n🎲 Expectancy Momentum:")
        lines.append(f"   Baseline avg R: {avg_r:.2f} | Recent avg R: {r20_avg_r:.2f} ({expect_emoji} Δ{r_delta:+.2f}R)")
        if r_delta < -0.1:
            lines.append(f"   ⚠️  Recent R-multiples decaying vs baseline — edge may be contracting")
        elif wr_delta < -5 and r_delta > 0.1:
            lines.append(f"   💡 WR dropping but R increasing — entry problem (wrong setups win rarely but big)")
        elif wr_delta > 5 and r_delta < -0.1:
            lines.append(f"   💡 WR rising but R compressing — exit problem (winning small, need to optimize exits)")

        # ── Duration vs R-Multiple correlation ──
        duration_data = []
        for t in trades:
            pnl = t.get("pnl_pct", 0)
            hrs = t.get("hours_held")
            if hrs is None:
                # Derive from timestamps if hours_held not in old trades
                et = t.get("entry_time", "")
                xt = t.get("exit_time", "")
                if et and xt:
                    try:
                        from datetime import datetime, timezone
                        hrs = (datetime.fromisoformat(xt) - datetime.fromisoformat(et)).total_seconds() / 3600
                    except (ValueError, TypeError):
                        continue
            if hrs is not None and hrs >= 0:
                duration_data.append((hrs, pnl))
        if duration_data:
            win_durations = [d for d in duration_data if d[1] > 0]
            loss_durations = [d for d in duration_data if d[1] <= 0]
            avg_win_hrs = sum(d[0] for d in win_durations) / len(win_durations) if win_durations else 0
            avg_loss_hrs = sum(d[0] for d in loss_durations) / len(loss_durations) if loss_durations else 0
            avg_hrs_all = sum(d[0] for d in duration_data) / len(duration_data)
            # Correlation: quick Pearson r between hours and PnL
            if len(duration_data) >= 8:
                n = len(duration_data)
                h_mean = sum(d[0] for d in duration_data) / n
                p_mean = sum(d[1] for d in duration_data) / n
                num = sum((d[0] - h_mean) * (d[1] - p_mean) for d in duration_data)
                den = (sum((d[0] - h_mean)**2 for d in duration_data) ** 0.5 *
                       sum((d[1] - p_mean)**2 for d in duration_data) ** 0.5)
                dur_r = num / den if den != 0 else 0
            else:
                dur_r = 0
            lines.append(f"\n⏱️ Duration vs R-Multiple ({len(duration_data)} trades):")
            lines.append(f"   Avg hold: {avg_hrs_all:.1f}h | Winners: {avg_win_hrs:.1f}h | Losers: {avg_loss_hrs:.1f}h")
            lines.append(f"   Duration-PnL correlation: r={dur_r:.3f}")
            if dur_r < -0.3:
                lines.append(f"   📋 NB Diagnosis: Short holds profit, long holds lose → Standard MR profile")
                lines.append(f"   → Tighten time_exit_cycles to cut dead trades before they drift to stop")
            elif dur_r > 0.3:
                lines.append(f"   📋 NB Diagnosis: Long holds profit more → Runner capture working correctly")
                lines.append(f"   → Do NOT tighten time stop — would choke 1.5R+ winners")
            else:
                lines.append(f"   📋 Duration-PnL: No clear correlation — time exit may not be the limiting factor")
            if avg_win_hrs < avg_loss_hrs and avg_loss_hrs > 0:
                lines.append(f"   💡 Losers held {avg_loss_hrs / max(avg_win_hrs, 0.1):.1f}x longer than winners — time stop could improve expectancy")

        # ── Half-signal analysis (NB recommendation) ──
        half_trades = [t for t in trades if t.get("signal") == "rsi_oversold_half"]
        if half_trades:
            half_pnls = [t.get("pnl_pct", 0) for t in half_trades]
            half_wins = [p for p in half_pnls if p > 0]
            half_losses = [p for p in half_pnls if p < 0]
            half_wr = len(half_wins) / len(half_trades) * 100
            half_total = sum(half_pnls)
            half_net_negative = half_total < 0
            lines.append(f"\n🔍 Half-Signal Analysis ({len(half_trades)} trades):")
            lines.append(f"   Win Rate: {half_wr:.0f}% | Total PnL: {half_total:+.2f}% | Avg R: {sum(half_pnls)/len(half_pnls):+.2f}%")
            if half_wr < 40 and half_net_negative:
                lines.append(f"   ⚠️  Half-signal bleeding (WR={half_wr:.0f}%, PnL={half_total:+.2f}%)")
                lines.append(f"   → NB recommends requiring volume spike (≥4x avg) or LL cascade as confluence for half entries")
                lines.append(f"   → Consider disabling half-signal or tightening the decision threshold")

        # ── Regime-conditioned performance splits ──
        regimes = {}
        for t in trades:
            regime = t.get("market_regime_entry", "Unknown")
            pnl = t.get("pnl_pct", 0)
            if regime not in regimes:
                regimes[regime] = {"count": 0, "pnls": []}
            regimes[regime]["count"] += 1
            regimes[regime]["pnls"].append(pnl)
        if len(regimes) > 1:
            lines.append(f"\n🌍 Regime-Conditioned Performance:")
            for regime, data in sorted(regimes.items(), key=lambda x: -sum(x[1]["pnls"])):
                r_pnls = data["pnls"]
                r_wins = sum(1 for p in r_pnls if p > 0)
                r_wr = r_wins / data["count"] * 100
                lines.append(
                    f"   {regime}: {data['count']}t | WR {r_wr:.0f}% | "
                    f"avg {sum(r_pnls)/len(r_pnls):+.2f}% | total {sum(r_pnls):+.2f}%"
                )
            # Flag if best and worst regimes are far apart
            sorted_regimes = sorted(regimes.items(), key=lambda x: sum(x[1]["pnls"])/len(x[1]["pnls"]))
            if len(sorted_regimes) >= 2:
                best_r = sorted_regimes[-1]
                worst_r = sorted_regimes[0]
                spread = sum(best_r[1]["pnls"])/len(best_r[1]["pnls"]) - sum(worst_r[1]["pnls"])/len(worst_r[1]["pnls"])
                if spread > 0.5:
                    lines.append(f"   💡 Best regime: {best_r[0]} ({sum(best_r[1]['pnls'])/len(best_r[1]['pnls']):+.2f}% avg) vs "
                                  f"Worst: {worst_r[0]} ({sum(worst_r[1]['pnls'])/len(worst_r[1]['pnls']):+.2f}% avg) — "
                                  f"optimize params for the regime that actually produced trades")

        # ── ADX Regime-Conditioned Performance (NB recommendation) ──
        if state_dir and asset_key:
            setups_path = state_dir / asset_key / "setups_log.jsonl"
            if setups_path.exists():
                adx_trades = []  # trades with ADX data
                try:
                    with open(setups_path) as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            entry = json.loads(line)
                            # Match trades by finding setups that led to entries near the trade's entry time
                            # Simpler: just analyze setups_log entries that have adx in components
                            comps = entry.get("confidence_components", {})
                            adx_val = comps.get("adx")
                            if adx_val is not None and entry.get("confidence_decision") in ("full", "half"):
                                # Invert: adx_score = max(0.0, 1.0 - adx/30)
                                # So adx ≈ (1 - adx_score) * 30
                                raw_adx = (1 - adx_val) * 30 if adx_val > 0 else 0
                                adx_trades.append({"adx": raw_adx, "score": entry.get("confidence_score", 0)})
                except Exception:
                    pass

                if len(adx_trades) >= 5:
                    bands = {"<20": [], "20-25": [], "25-30": [], ">30": []}
                    for t in adx_trades:
                        a = t["adx"]
                        if a < 20: bands["<20"].append(t)
                        elif a < 25: bands["20-25"].append(t)
                        elif a < 30: bands["25-30"].append(t)
                        else: bands[">30"].append(t)

                    lines.append(f"\n📊 ADX Regime-Conditioned Setups ({len(adx_trades)} entries):")
                    for band, entries in bands.items():
                        if not entries:
                            continue
                        avg_score = sum(e["score"] for e in entries) / len(entries)
                        lines.append(f"   ADX {band}: {len(entries)} setups | avg score {avg_score:.2f}")
                        if band == ">30" and len(entries) >= 3:
                            lines.append(f"   ⚠️  {len(entries)} setups entered with ADX > 30 — these should be blocked per NB recommendation")

        # Recent trades
        lines.append(f"\n📈 Recent Trades (last 5):")
        for t in trades[-5:]:
            btc_ctx = ""
            btc_4h = t.get("btc_entry_4h_rsi")
            fng_val = t.get("fear_greed_entry")
            if btc_4h is not None:
                btc_ctx = f" [BTC4h={btc_4h:.0f}"
                if fng_val is not None:
                    btc_ctx += f" FnG={fng_val}"
                btc_ctx += "]"
            lines.append(
                f"   {t.get('exit_reason', '?')}: {t.get('pnl_pct', 0):+.2f}%{btc_ctx}"
            )
    else:
        lines.append("   (no trades yet)")

    # Score
    score = score_trades_for_asset(asset_key, trades, goal) if trades else {}
    if score:
        lines.append(
            f"\n🎯 Score: {score.get('overall_score', 0):+.4f} ({score.get('classification', 'n/a')})"
        )
        comp = score.get("component_scores", {})
        lines.append(f"   Return: {comp.get('return_score', 0):+.4f}")
        lines.append(f"   Drawdown: {comp.get('dd_score', 0):+.4f}")
        lines.append(f"   Sharpe: {comp.get('sharpe_score', 0):+.4f}")
        metrics = score.get("metrics", {})
        lines.append(
            f"   Metrics: PnL={metrics.get('total_pnl_pct', 0):+.2f}% | DD={metrics.get('max_drawdown', 0) * 100:.2f}% | Sharpe={metrics.get('sharpe', 0):.3f}"
        )

    # Cluster analysis (autocorrelation of trade outcomes)
    clustering = detect_loss_clustering(trades)
    if clustering.get("max_consecutive_losses", 0) > 0:
        emoji = "🚨" if clustering["regime_shift_detected"] else "📊"
        lines.append(f"\n{emoji} Trade Autocorrelation:")
        lines.append(
            f"   Max consecutive losses: {clustering['max_consecutive_losses']}"
        )
        lines.append(
            f"   Runs (observed/expected): {clustering['runs']}/{clustering['expected_runs']}"
        )
        lines.append(
            f"   Z-score: {clustering['z_score']:.3f} | P-value: {clustering['p_value']:.4f}"
        )
        if clustering["regime_shift_detected"]:
            lines.append(
                f"   ⚠️  REGIME SHIFT detected — losses clustering beyond random chance"
            )
            lines.append(f"   → Consider tightening gates or running optimizer early")

    # Market pattern analysis
    patterns = analyze_market_patterns(trades)
    if patterns.get("findings"):
        lines.append(f"\n🔍 Market Pattern Analysis:")
        for f in patterns["findings"]:
            emoji = "✅" if f["avg_pnl"] > 0 else "❌"
            lines.append(
                f"   {emoji} {f['condition']}: {f['count']} trades | avg {f['avg_pnl']:+.2f}% | {f['win_rate']:.0f}% WR | total {f['total_pnl']:+.2f}%"
            )

        # Highlight strongest signal
        if len(patterns["findings"]) >= 2:
            best = max(patterns["findings"], key=lambda f: f["avg_pnl"])
            worst = min(patterns["findings"], key=lambda f: f["avg_pnl"])
            if best["avg_pnl"] > 0 and worst["avg_pnl"] < 0:
                lines.append(f"\n💡 Key Insight:")
                lines.append(
                    f"   Best conditions: {best['condition']} (avg {best['avg_pnl']:+.2f}%)"
                )
                lines.append(
                    f"   Worst conditions: {worst['condition']} (avg {worst['avg_pnl']:+.2f}%)"
                )

    # Strategy evolution
    history_files = (
        sorted((state_dir / asset_key / "history").glob("v*.yaml"))
        if (state_dir / asset_key / "history").exists()
        else []
    )
    if history_files:
        lines.append(f"\n🗺️ Strategy Evolution ({len(history_files)} versions):")
        for hf in history_files:
            with open(hf) as f:
                hist = yaml.safe_load(f)
            btc_gate_str = ""
            btc_g = hist.get("btc_gate", {})
            if btc_g:
                btc_gate_str = f" btc1h>{btc_g.get('min_btc_1h_rsi', '?')}"
            fng_str = ""
            fng_g = hist.get("fng_gate", {})
            if fng_g:
                fng_str = f" fng>{fng_g.get('min_value', '?')}"
            cool = hist.get("cooldown_cycles", "?")
            te = hist.get("time_exit_cycles", "")
            te_str = f" timeExit={te}" if te else ""
            lines.append(
                f"   {hf.name}: stop={hist.get('stop_loss_pct', '?')}% | entry={hist.get('entry', {}).get('threshold', '?')} | cooldown={cool}{btc_gate_str}{fng_str}{te_str}"
            )

    # Hypotheses
    hypotheses = load_all_hypotheses(state_dir, asset_key)
    if hypotheses:
        lines.append(f"\n💡 Previous Hypotheses ({len(hypotheses)}):")
        for h in hypotheses[-3:]:
            lines.append(f"   Cycle {h.get('cycle', '?')}: {h.get('action', '?')}")
            lines.append(f"     → {h.get('reasoning', '?')[:120]}")

    # Variables available to change
    lines.append(f"\n⚙️ Changeable Variables:")
    if current:
        lines.append(
            f"   - entry.threshold (current: {current.get('entry', {}).get('threshold', 30)})"
        )
        lines.append(
            f"   - stop_loss_pct (current: {current.get('stop_loss_pct', 2.0)}%)"
        )
        lines.append(
            f"   - position_size_r (current: {current.get('position_size_r', 0.5)}R)"
        )
        lines.append(
            f"   - cooldown_cycles (current: {current.get('cooldown_cycles', 30)})"
        )
        lines.append(
            f"   - btc_gate.min_btc_1h_rsi (current: {current.get('btc_gate', {}).get('min_btc_1h_rsi', 20)})"
        )
        lines.append(
            f"   - fng_gate.min_value (current: {current.get('fng_gate', {}).get('min_value', 10)})"
        )
        ev = current.get("evaluator", {})
        lines.append(
            f"   - evaluator.lower_low_cascade (current: {ev.get('lower_low_cascade', 3)})"
        )
        lines.append(
            f"   - evaluator.volume_spike_mult (current: {ev.get('volume_spike_mult', 1.5)})"
        )
        lines.append(
            f"   - evaluator.min_candle_position (current: {ev.get('min_candle_position', 0.3)})\n"
        )
        time_exit = current.get("time_exit_cycles", 60 if ("BTC" in asset_key or "ETH" in asset_key) else 45)
        lines.append(
            f"   - time_exit_cycles (current: {time_exit}) — max hours before time-based exit (MR sleeve)"
        )
        exit_min_r = current.get("scale_out_min_R", 0.3)
        lines.append(
            f"   - scale_out_min_R (current: {exit_min_r}) — minimum R before TP1 fires"
        )
        lines.append(
            f"   - tp2_target_R (current: {current.get('tp2_target_R', 1.5)}) — R target for TP2 slice"
        )
        lines.append(
            f"   - tp2_slice (current: {current.get('tp2_slice', 0.5)}) — fraction of remaining at TP2"
        )
        lines.append(
            f"   - chandelier_mult_alts (current: {current.get('chandelier_mult_alts', 4.0)}) — alt trailing tightness"
        )
        lines.append(
            f"   - chandelier_mult_major (current: {current.get('chandelier_mult_major', 2.5)}) — BTC/ETH trailing tightness"
        )
    lines.append(f"\n⚠️ RULE: Change EXACTLY ONE variable per cycle. No exceptions.")

    return "\n".join(lines)


def summarize_mode(state_dir: Path, assets: list):
    goal = load_goal(state_dir)
    for asset_cfg in assets:
        key = asset_cfg["key"]
        trades = load_trades(state_dir, key)
        report = summarize(state_dir, key, goal, trades)
        print(report)
        print()


def has_new_trades_since_last_hypothesis(trades: list, hypotheses: list) -> tuple:
    """Check if any trades closed since the last reflection hypothesis was written.

    Returns (has_new: bool, reason: str). Skips scoring if no new data.
    """
    if not trades:
        return (False, "no trades exist")
    if not hypotheses:
        return (True, "no prior hypotheses — first reflection run")

    # Newest trade exit timestamp vs newest hypothesis timestamp
    latest_trade = max(t.get("exit_time", "") for t in trades)
    latest_hyp = max(h.get("timestamp", "") for h in hypotheses)

    if not latest_trade:
        return (False, "trades have no exit_time field")

    if latest_trade > latest_hyp:
        return (
            True,
            f"new trade at {latest_trade} since last hypothesis at {latest_hyp}",
        )
    else:
        return (
            False,
            f"no new trades since last hypothesis at {latest_hyp} (latest trade: {latest_trade})",
        )


def filter_recent_trades(trades: list, max_age_hours: float = 24) -> list:
    """Keep only trades closed within max_age_hours of now.

    Filters out stale trades from obsolete strategy versions so they
    don't pollute scoring. Only trades with valid exit_time within the
    window are returned. If all trades are filtered, returns [] which
    triggers the stale-data guard naturally.
    """
    now = datetime.now(timezone.utc)
    recent = []
    for t in trades:
        exit_time = t.get("exit_time", "")
        if not exit_time:
            continue
        try:
            t_ts = datetime.fromisoformat(exit_time)
            if (now - t_ts).total_seconds() < max_age_hours * 3600:
                recent.append(t)
        except (ValueError, TypeError):
            continue
    return recent


def _load_setups_count(state_dir: Path, asset_key: str) -> int:
    """Count qualifying setups (skipped due to low confidence) for gatekeeper."""
    path = state_dir / asset_key / "setups_log.jsonl"
    if not path.exists():
        return 0
    count = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("reason", "").startswith("low_confidence"):
                    count += 1
            except json.JSONDecodeError:
                continue
    return count


def _gatekeeper(
    asset_key: str,
    action: str,
    strategy: dict,
    completed_trades: int,
    total_trades: int,
) -> dict:
    """Validate a proposed parameter change against conservative criteria.

    Returns {'approved': bool, 'reason': str}.
    Rules from the document's Parameter Change Gatekeeper prompt:
      1. 30+ completed trades for asset-level risk changes (stop_loss, position_size)
      2. 100+ qualifying setups for entry-threshold changes
      3. Bounded per-cycle adjustment limits
      4. Reject dangerous widenings of stop loss
    """
    # ── Determine change type from action ──
    change_type = None
    proposed_value = None
    current_value = None

    if "tighten_sl" in action or "widen_stop" in action or "stop_loss" in action:
        change_type = "stop_loss"
        current_value = strategy.get("stop_loss_pct", 2.0)
        proposed_value = current_value  # Approximate — will be set by decision tree
        # The actual value is set in the decision tree above, but actions are
        # named before the value is computed. We approximate from the action:
        if "tighten" in action:
            proposed_value = round(
                max(0.5, current_value - 0.5 if "regime" in action else 0.2), 2
            )
        elif "widen" in action:
            proposed_value = round(min(5.0, current_value + 0.1), 2)

    elif "entry" in action or "threshold" in action:
        change_type = "entry_threshold"
        current_value = strategy.get("entry", {}).get("threshold", 30)
        if "loosen" in action:
            proposed_value = min(45, current_value + 2)
        elif "tighten" in action:
            proposed_value = max(20, current_value - 1)
        else:
            proposed_value = current_value

    elif "btc_gate" in action:
        change_type = "btc_gate"
        current_value = strategy.get("btc_gate", {}).get("min_btc_1h_rsi", 20)
        proposed_value = min(45, current_value + 5)

    elif "scale_out" in action or "tp2_target" in action or "chandelier_mult" in action or "time_exit" in action:
        change_type = "exit_params"
        if completed_trades < 10:
            return {
                "approved": False,
                "reason": f"Need 10+ completed trades for exit-param changes (have {completed_trades})",
            }
        # Bounded per-cycle limits
        if "scale_out" in action:
            key = "scale_out_min_R"
            cur = strategy.get(key, 0.3)
            prop = strategy.get(key, 0.3)
            limit = 0.3
        elif "tp2" in action:
            key = "tp2_target_R"
            cur = strategy.get(key, 1.5)
            prop = strategy.get(key, 1.5)
            limit = 0.5
        elif "time_exit" in action:
            key = "time_exit_cycles"
            is_major = "BTC" in asset_key or "ETH" in asset_key
            cur = strategy.get(key, 60 if is_major else 45)
            prop = strategy.get(key, 60 if is_major else 45)
            limit = 20  # max 20-hour change per cycle
        else:
            key = "chandelier_mult_alts" if "alts" in action else "chandelier_mult_major"
            cur = strategy.get(key, 4.0 if "alts" in action else 2.5)
            prop = strategy.get(key, 4.0 if "alts" in action else 2.5)
            limit = 1.0
        delta = abs(prop - cur)
        if delta > limit:
            return {
                "approved": False,
                "reason": f"{key} change of {delta:.2f} exceeds {limit} per-cycle limit",
            }
        # Don't lower exit thresholds if recent trades include large winners
        if "lower" in action or "reduce" in action:
            state_dir = BASE_DIR / "state"
            tc = load_trades(state_dir, asset_key)
            recent_tc = filter_recent_trades(tc)
            if recent_tc:
                max_win = max((t.get("pnl_pct", 0) for t in recent_tc), default=0)
                if max_win > 1.0:
                    return {
                        "approved": False,
                        "reason": f"Recent max win {max_win:+.2f}% — lowering exits would cap winners",
                    }

    # ── Rule 1: Asset-level risk changes need 30+ completed trades ──
    if change_type == "stop_loss" and completed_trades < 30:
        return {
            "approved": False,
            "reason": f"Need 30+ completed trades for stop-loss changes (have {completed_trades})",
        }

    # ── Rule 2: Entry-threshold changes need 100+ qualifying setups ──
    if change_type == "entry_threshold":
        state_dir = BASE_DIR / "state"
        setups = _load_setups_count(state_dir, asset_key)
        if setups < 100:
            return {
                "approved": False,
                "reason": f"Need 100+ qualifying setups for entry-threshold changes (have {setups})",
            }

    # ── Rule 3: Bounded adjustment limits ──
    if change_type == "entry_threshold" and proposed_value is not None:
        delta = proposed_value - current_value
        if abs(delta) > 3:
            return {
                "approved": False,
                "reason": f"Entry-threshold change of {delta:+d} exceeds ±3 limit per cycle",
            }

    if change_type == "stop_loss" and proposed_value is not None:
        reduction = current_value - proposed_value
        if reduction > 1.0:
            return {
                "approved": False,
                "reason": f"Stop-loss reduction of {reduction:.1f}% exceeds 1% per cycle limit",
            }
        if proposed_value > current_value and proposed_value - current_value > 0.5:
            return {
                "approved": False,
                "reason": f"Stop-loss increase of {proposed_value - current_value:.1f}% exceeds 0.5% per cycle limit",
            }

    # ── Rule 4: Don't widen stop when performance is already bad ──
    if "widen" in action and completed_trades > 0:
        # Check recent win rate
        state_dir = BASE_DIR / "state"
        trades = load_trades(state_dir, asset_key)
        recent = filter_recent_trades(trades)
        if recent:
            losses = sum(1 for t in recent if t.get("pnl_pct", 0) < 0)
            total_recent = len(recent)
            if total_recent >= 5 and losses > total_recent * 0.6:
                return {
                    "approved": False,
                    "reason": f"Widening stop when recent WR is {(total_recent - losses) / total_recent * 100:.0f}% would increase risk",
                }

    # ── All checks passed ──
    return {"approved": True, "reason": "ok"}


def fallback_reflect(state_dir: Path, asset_key: str, goal: dict, trades: list):
    """Deterministic fallback reflection — one variable change, now with market-aware options."""
    strat_path = state_dir / asset_key / "strategy.yaml"
    if not strat_path.exists():
        print(f"No strategy for {asset_key}, skipping")
        return

    # ── Stale-data guard: skip scoring if no new trades since last hypothesis ──
    hypotheses = load_all_hypotheses(state_dir, asset_key)
    has_new, reason = has_new_trades_since_last_hypothesis(trades, hypotheses)
    if not has_new and hypotheses:
        print(f"   {asset_key}: STALE DATA — {reason}")
        print(f"   Skipping reflection until new trades appear.")
        return

    # ── Recency filter: only score trades from the last 24h ──
    # Prevents old-version trades (e.g., v01-v03 era) from polluting
    # the score when the strategy has since evolved significantly.
    recent = filter_recent_trades(trades)
    if len(recent) < len(trades):
        print(
            f"   Filtered {len(trades) - len(recent)} old trade(s) — scoring {len(recent)} recent trade(s)"
        )
    if not recent:
        print(f"   {asset_key}: No recent trades to score — skipping")
        return

    score = score_trades_for_asset(asset_key, recent, goal)

    with open(strat_path) as f:
        strategy = yaml.safe_load(f) or {}

    save_strategy_version(state_dir, asset_key, strategy)

    raw_ver = str(strategy.get("version", "00"))
    old_ver_num = int(raw_ver.lstrip("vV").lstrip("'").strip())
    new_ver = f"{old_ver_num + 1:02d}"

    overall = score.get("overall_score", 0.0)
    metrics = score.get("metrics", {})
    classification = score.get("classification", "neutral")

    hypothesis = {
        "cycle": new_ver,
        "asset": asset_key,
        "mode": "fallback",
        "score": overall,
        "classification": classification,
    }

    # Analyze market patterns to inform decision
    patterns = analyze_market_patterns(trades)

    # Check if we have enough data to make market-aware adjustments
    worst_pattern = None
    best_pattern = None
    if patterns.get("findings"):
        sorted_findings = sorted(patterns["findings"], key=lambda f: f["avg_pnl"])
        worst_pattern = (
            sorted_findings[0] if sorted_findings[0]["avg_pnl"] < 0 else None
        )
        best_pattern = (
            sorted_findings[-1] if sorted_findings[-1]["avg_pnl"] > 0 else None
        )

    # P1: Clustered loss detection — regime shift check
    clustering = detect_loss_clustering(trades)
    regime_shift = clustering.get("regime_shift_detected", False)
    if regime_shift:
        print(f"   ⚠️  REGIME SHIFT: {clustering['message']}")

    # P2: Exit quality check — trades clustering at ~0R means exit is broken
    pnls = [t.get("pnl_pct", 0) for t in trades if t.get("pnl_pct") is not None]
    sl_pct = strategy.get("stop_loss_pct", 2.0)
    r_multiples = [p / sl_pct for p in pnls if sl_pct]
    zero_r_pct = sum(1 for r in r_multiples if abs(r) < 0.15) / len(r_multiples) * 100 if r_multiples else 0
    avg_r = sum(r_multiples) / len(r_multiples) if r_multiples else 0
    exit_problem = zero_r_pct > 55 and len(r_multiples) >= 8 and overall < 0.1
    if exit_problem:
        print(f"   ⚠️  EXIT PROBLEM: {zero_r_pct:.0f}% of trades at ~0R (avg R={avg_r:.2f})")

    # Decision tree — start with the most impactful adjustment
    if overall < 0:
        # Underperforming — diagnose
        if regime_shift:
            # Regime shift detected — aggressive response
            current_sl = strategy.get("stop_loss_pct", 2.0)
            strategy["stop_loss_pct"] = round(max(0.5, current_sl - 0.5), 2)
            hypothesis["action"] = "regime_shift_tighten_sl"
            hypothesis["reasoning"] = (
                f"REGIME SHIFT: losses clustering (P={clustering['p_value']:.4f}, "
                f"{clustering['max_consecutive_losses']} consec losses). "
                f"Tightened stop_loss from {current_sl}% to {strategy['stop_loss_pct']}%"
            )
        elif metrics.get("max_drawdown", 0) > goal.get("max_drawdown", 0.08):
            # Drawdown too high — tighten stop loss
            current_sl = strategy.get("stop_loss_pct", 2.0)
            strategy["stop_loss_pct"] = round(max(0.5, current_sl - 0.2), 2)
            hypothesis["action"] = "tighten_stop_loss"
            hypothesis["reasoning"] = (
                f"Drawdown exceeded limit. Tightened stop_loss_pct from {current_sl} to {strategy['stop_loss_pct']}"
            )
        elif worst_pattern and "BTC" in worst_pattern.get("condition", ""):
            # Losses correlate with BTC conditions — tighten BTC 1h gate
            current_min = strategy.get("btc_gate", {}).get("min_btc_1h_rsi", 20)
            strategy.setdefault("btc_gate", {})
            strategy["btc_gate"]["min_btc_1h_rsi"] = min(40, current_min + 5)
            hypothesis["action"] = "tighten_btc_gate"
            hypothesis["reasoning"] = (
                f"Losses cluster in {worst_pattern['condition']} "
                f"(avg {worst_pattern['avg_pnl']:+.2f}%). "
                f"Raised BTC 1h min RSI from {current_min} to {strategy['btc_gate']['min_btc_1h_rsi']}"
            )
        elif exit_problem:
            # Exit quality is the root cause — fix exit params
            current_min_r = strategy.get("scale_out_min_R", 0.3)
            new_min_r = round(min(1.0, current_min_r + 0.15), 2)
            strategy["scale_out_min_R"] = new_min_r
            hypothesis["action"] = "raise_scale_out_min_R"
            hypothesis["reasoning"] = (
                f"EXIT PROBLEM: {zero_r_pct:.0f}% of trades exit near ~0R. "
                f"TP1 fires at EMA cross without profit. Raised scale_out_min_R from "
                f"{current_min_r} to {new_min_r} to let runners develop."
            )
        else:
            # Check duration-PnL correlation
            dur_data = []
            for t in trades:
                hrs = t.get("hours_held")
                if hrs is None:
                    et, xt = t.get("entry_time", ""), t.get("exit_time", "")
                    if et and xt:
                        try:
                            from datetime import datetime, timezone
                            hrs = (datetime.fromisoformat(xt) - datetime.fromisoformat(et)).total_seconds() / 3600
                        except (ValueError, TypeError):
                            continue
                if hrs is not None and hrs >= 0:
                    dur_data.append((hrs, t.get("pnl_pct", 0)))
            if dur_data and len(dur_data) >= 8:
                win_d = [d for d in dur_data if d[1] > 0]
                loss_d = [d for d in dur_data if d[1] <= 0]
                avg_win_hrs = sum(d[0] for d in win_d) / len(win_d) if win_d else 0
                avg_loss_hrs = sum(d[0] for d in loss_d) / len(loss_d) if loss_d else 0
                if avg_loss_hrs > avg_win_hrs * 2 and avg_win_hrs > 0:
                    # Losers held much longer — tighten time stop
                    is_major = "BTC" in asset_key or "ETH" in asset_key
                    cur = strategy.get("time_exit_cycles", 60 if is_major else 45)
                    new_val = max(12, cur - 15 if cur > 30 else cur - 8)
                    strategy["time_exit_cycles"] = new_val
                    hypothesis["action"] = "tighten_time_exit"
                    hypothesis["reasoning"] = (
                        f"Underperforming: losers held {avg_loss_hrs/avg_win_hrs:.1f}x longer than winners "
                        f"({avg_loss_hrs:.0f}h vs {avg_win_hrs:.0f}h). "
                        f"Tightened time_exit_cycles from {cur} to {new_val}h."
                    )
                else:
                    # Generic: loosen entry threshold to catch more signals
                    current_threshold = strategy.get("entry", {}).get("threshold", 30)
                    strategy["entry"]["threshold"] = min(45, current_threshold + 2)
                    hypothesis["action"] = "loosen_entry_threshold"
                    hypothesis["reasoning"] = (
                        f"Return under target. Loosened entry.threshold from {current_threshold} to {strategy['entry']['threshold']}"
                    )
            else:
                # Generic: loosen entry threshold to catch more signals
                current_threshold = strategy.get("entry", {}).get("threshold", 30)
                strategy["entry"]["threshold"] = min(45, current_threshold + 2)
                hypothesis["action"] = "loosen_entry_threshold"
                hypothesis["reasoning"] = (
                    f"Return under target. Loosened entry.threshold from {current_threshold} to {strategy['entry']['threshold']}"
                )

    elif overall > 0.3:
        # Overperforming — take a bit more risk
        if exit_problem and zero_r_pct > 40:
            # Exit still contains ~0R trades — reduce min_R guard
            current_min_r = strategy.get("scale_out_min_R", 0.3)
            new_min_r = round(max(0.1, current_min_r - 0.1), 2)
            strategy["scale_out_min_R"] = new_min_r
            hypothesis["action"] = "lower_scale_out_min_R"
            hypothesis["reasoning"] = (
                f"Performance good but {zero_r_pct:.0f}% of trades at ~0R. "
                f"Lowered scale_out_min_R from {current_min_r} to {new_min_r} "
                f"to avoid over-constraining TP1."
            )
        else:
            # Check duration to see if widening time exit could help winners grow
            dur_data = []
            for t in trades:
                hrs = t.get("hours_held")
                if hrs is None:
                    et, xt = t.get("entry_time", ""), t.get("exit_time", "")
                    if et and xt:
                        try:
                            from datetime import datetime, timezone
                            hrs = (datetime.fromisoformat(xt) - datetime.fromisoformat(et)).total_seconds() / 3600
                        except (ValueError, TypeError):
                            continue
                if hrs is not None and hrs >= 0:
                    dur_data.append((hrs, t.get("pnl_pct", 0)))
            if dur_data and len(dur_data) >= 8:
                win_d = [d for d in dur_data if d[1] > 0]
                loss_d = [d for d in dur_data if d[1] <= 0]
                avg_win_hrs = sum(d[0] for d in win_d) / len(win_d) if win_d else 0
                avg_loss_hrs = sum(d[0] for d in loss_d) / len(loss_d) if loss_d else 0
                if avg_win_hrs > 0 and avg_win_hrs > avg_loss_hrs * 1.5:
                    # Winners benefit from longer holds — widen time exit
                    is_major = "BTC" in asset_key or "ETH" in asset_key
                    cur = strategy.get("time_exit_cycles", 60 if is_major else 45)
                    new_val = min(120, cur + 20)
                    strategy["time_exit_cycles"] = new_val
                    hypothesis["action"] = "widen_time_exit"
                    hypothesis["reasoning"] = (
                        f"Good performance and winners held {avg_win_hrs/avg_loss_hrs:.1f}x longer than losers "
                        f"({avg_win_hrs:.0f}h vs {avg_loss_hrs:.0f}h). "
                        f"Widened time_exit_cycles from {cur} to {new_val}h to let runners develop further."
                    )
                else:
                    current_sl = strategy.get("stop_loss_pct", 2.0)
                    strategy["stop_loss_pct"] = round(min(5.0, current_sl + 0.1), 2)
                    hypothesis["action"] = "widen_stop_loss"
                    hypothesis["reasoning"] = (
                        f"Good performance. Widened stop_loss_pct from {current_sl} to {strategy['stop_loss_pct']} to capture more upside"
                    )
            else:
                current_sl = strategy.get("stop_loss_pct", 2.0)
                strategy["stop_loss_pct"] = round(min(5.0, current_sl + 0.1), 2)
                hypothesis["action"] = "widen_stop_loss"
                hypothesis["reasoning"] = (
                    f"Good performance. Widened stop_loss_pct from {current_sl} to {strategy['stop_loss_pct']} to capture more upside"
                )

    else:
        # Neutral performance — diagnose
        if exit_problem:
            # Still have exit problem — raise scale_out_min_R
            current_min_r = strategy.get("scale_out_min_R", 0.3)
            new_min_r = round(min(1.2, current_min_r + 0.1), 2)
            strategy["scale_out_min_R"] = new_min_r
            hypothesis["action"] = "raise_scale_out_min_R"
            hypothesis["reasoning"] = (
                f"Neutral score but exit quality poor ({zero_r_pct:.0f}% of trades ~0R). "
                f"Raised scale_out_min_R from {current_min_r} to {new_min_r}."
            )
        elif avg_r < 0.5:
            # Trades don't run far enough — check tp2_target_R
            current_tp2 = strategy.get("tp2_target_R", 1.5)
            new_tp2 = round(max(0.8, current_tp2 - 0.3), 2)
            strategy["tp2_target_R"] = new_tp2
            hypothesis["action"] = "lower_tp2_target_R"
            hypothesis["reasoning"] = (
                f"Avg R={avg_r:.2f} — trades not reaching targets. "
                f"Lowered tp2_target_R from {current_tp2} to {new_tp2} "
                f"to make TP2 more achievable."
            )
        else:
            # Neutral: check if time-based exit is cutting winners short
            dur_data = []
            for t in trades:
                hrs = t.get("hours_held")
                if hrs is None:
                    et, xt = t.get("entry_time", ""), t.get("exit_time", "")
                    if et and xt:
                        try:
                            from datetime import datetime, timezone
                            hrs = (datetime.fromisoformat(xt) - datetime.fromisoformat(et)).total_seconds() / 3600
                        except (ValueError, TypeError):
                            continue
                if hrs is not None and hrs >= 0:
                    dur_data.append((hrs, t.get("pnl_pct", 0)))
            if dur_data and len(dur_data) >= 8:
                win_d = [d for d in dur_data if d[1] > 0]
                loss_d = [d for d in dur_data if d[1] <= 0]
                avg_win_hrs = sum(d[0] for d in win_d) / len(win_d) if win_d else 0
                avg_loss_hrs = sum(d[0] for d in loss_d) / len(loss_d) if loss_d else 0
                if avg_loss_hrs > avg_win_hrs * 1.5 and avg_win_hrs > 0:
                    # Losers held longer — tighten time exit
                    is_major = "BTC" in asset_key or "ETH" in asset_key
                    cur = strategy.get("time_exit_cycles", 60 if is_major else 45)
                    new_val = max(12, cur - 10)
                    strategy["time_exit_cycles"] = new_val
                    hypothesis["action"] = "tighten_time_exit"
                    hypothesis["reasoning"] = (
                        f"Neutral with duration imbalance: losers held {avg_loss_hrs/avg_win_hrs:.1f}x longer "
                        f"({avg_loss_hrs:.0f}h vs {avg_win_hrs:.0f}h). "
                        f"Tightened time_exit_cycles from {cur} to {new_val}h."
                    )
                elif avg_win_hrs > avg_loss_hrs * 2 and avg_loss_hrs > 0:
                    # Winners clearly need time — widen
                    is_major = "BTC" in asset_key or "ETH" in asset_key
                    cur = strategy.get("time_exit_cycles", 60 if is_major else 45)
                    new_val = min(120, cur + 15)
                    strategy["time_exit_cycles"] = new_val
                    hypothesis["action"] = "widen_time_exit"
                    hypothesis["reasoning"] = (
                        f"Neutral with winners needing time: held {avg_win_hrs/avg_loss_hrs:.1f}x longer "
                        f"({avg_win_hrs:.0f}h vs {avg_loss_hrs:.0f}h). "
                        f"Widened time_exit_cycles from {cur} to {new_val}h."
                    )
                else:
                    # Tighten threshold slightly
                    current_threshold = strategy.get("entry", {}).get("threshold", 30)
                    strategy["entry"]["threshold"] = max(20, current_threshold - 1)
                    hypothesis["action"] = "tighten_entry_slightly"
                    hypothesis["reasoning"] = (
                        f"Neutral performance. Tightened entry.threshold from {current_threshold} to {strategy['entry']['threshold']}"
                    )
            else:
                # Tighten threshold slightly
                current_threshold = strategy.get("entry", {}).get("threshold", 30)
                strategy["entry"]["threshold"] = max(20, current_threshold - 1)
                hypothesis["action"] = "tighten_entry_slightly"
                hypothesis["reasoning"] = (
                    f"Neutral performance. Tightened entry.threshold from {current_threshold} to {strategy['entry']['threshold']}"
                )

    strategy["version"] = new_ver

    # ── Parameter Change Gatekeeper ──
    # Before applying changes, validate against conservative criteria.
    # Document's rules:
    #   • 50+ trades for system-level changes
    #   • 30+ trades for asset-level risk adjustments
    #   • 100+ qualifying setups for entry-threshold changes
    #   • Bounded adjustment limits
    #   • Reject if it increases drawdown without improving expectancy
    completed_trades = [t for t in trades if t.get("exit_time")]
    quality = _gatekeeper(
        asset_key, hypothesis["action"], strategy, len(completed_trades), len(trades)
    )
    if not quality["approved"]:
        print(f"   🚫 GATEKEEPER BLOCKED: {hypothesis['action']} — {quality['reason']}")
        # Write a rejection hypothesis so we don't keep re-attempting
        hypothesis["gatekeeper_rejected"] = True
        hypothesis["rejection_reason"] = quality["reason"]
        append_hypothesis(state_dir, asset_key, hypothesis)
        print(f"   Logged hypothesis with rejection — waiting for more data.")
        return  # Don't update strategy

    hypothesis["new_strategy"] = {
        "version": new_ver,
        "entry_threshold": strategy.get("entry", {}).get("threshold"),
        "stop_loss_pct": strategy.get("stop_loss_pct"),
        "cooldown_cycles": strategy.get("cooldown_cycles"),
        "btc_min_1h_rsi": strategy.get("btc_gate", {}).get("min_btc_1h_rsi"),
        "fng_min_value": strategy.get("fng_gate", {}).get("min_value"),
        "scale_out_min_R": strategy.get("scale_out_min_R"),
        "tp2_target_R": strategy.get("tp2_target_R"),
        "chandelier_mult_alts": strategy.get("chandelier_mult_alts"),
        "chandelier_mult_major": strategy.get("chandelier_mult_major"),
        "time_exit_cycles": strategy.get("time_exit_cycles"),
    }

    write_strategy(state_dir, asset_key, strategy)
    append_hypothesis(state_dir, asset_key, hypothesis)

    print(
        f"   {asset_key}: v{old_ver_num} → v{new_ver} | {hypothesis['action']} (score: {overall:+.3f})"
    )


def load_goal(state_dir: Path) -> dict:
    goal_path = state_dir / "goal.yaml"
    with open(goal_path) as f:
        return yaml.safe_load(f)


def main():
    parser = argparse.ArgumentParser(description="Hermes Trading Reflection Engine")
    parser.add_argument(
        "--mode", choices=["fallback", "summarize", "hermes"], default="fallback"
    )
    parser.add_argument("--asset", help="Single asset to reflect on")
    args = parser.parse_args()

    state_dir = BASE_DIR / "state"
    goal = load_goal(state_dir)

    assets = goal.get("assets", [])
    if args.asset:
        assets = [a for a in assets if a["key"] == args.asset]

    for asset_cfg in assets:
        key = asset_cfg["key"]
        trades = load_trades(state_dir, key)
        print(f"📊 {key}: {len(trades)} trades loaded")

        if len(trades) < 1:
            print(f"   Skipping {key}: not enough trades")
            continue

        if args.mode == "fallback":
            fallback_reflect(state_dir, key, goal, trades)
        elif args.mode == "summarize":
            print(summarize(state_dir, key, goal, trades))
        elif args.mode == "hermes":
            print(
                f"   {key}: Hermes mode runs via the cron job. Use --mode summarize to view data."
            )
            print(
                "   The cron agent will read the summary, analyze it, and apply changes."
            )


if __name__ == "__main__":
    main()
