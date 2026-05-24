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
            f"   BTC Gate: 4h min RSI {btc.get('min_btc_4h_rsi', 25)} | 1h min RSI {btc.get('min_btc_1h_rsi', 20)}"
        )
        fng = current.get("fng_gate", {})
        lines.append(f"   FnG Gate: min value {fng.get('min_value', 10)}")

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
                btc_gate_str = f" btc4h>{btc_g.get('min_btc_4h_rsi', '?')}"
            fng_str = ""
            fng_g = hist.get("fng_gate", {})
            if fng_g:
                fng_str = f" fng>{fng_g.get('min_value', '?')}"
            cool = hist.get("cooldown_cycles", "?")
            lines.append(
                f"   {hf.name}: stop={hist.get('stop_loss_pct', '?')}% | entry={hist.get('entry', {}).get('threshold', '?')} | cooldown={cool}{btc_gate_str}{fng_str}"
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
            f"   - btc_gate.min_btc_4h_rsi (current: {current.get('btc_gate', {}).get('min_btc_4h_rsi', 25)})"
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
            f"   - evaluator.min_candle_position (current: {ev.get('min_candle_position', 0.3)})"
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
        current_value = strategy.get("btc_gate", {}).get("min_btc_4h_rsi", 25)
        proposed_value = min(45, current_value + 5)

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
            # Losses correlate with BTC conditions — tighten BTC gate
            current_min = strategy.get("btc_gate", {}).get("min_btc_4h_rsi", 25)
            strategy.setdefault("btc_gate", {})
            strategy["btc_gate"]["min_btc_4h_rsi"] = min(45, current_min + 5)
            hypothesis["action"] = "tighten_btc_gate"
            hypothesis["reasoning"] = (
                f"Losses cluster in {worst_pattern['condition']} "
                f"(avg {worst_pattern['avg_pnl']:+.2f}%). "
                f"Raised BTC 4h min RSI from {current_min} to {strategy['btc_gate']['min_btc_4h_rsi']}"
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
        current_sl = strategy.get("stop_loss_pct", 2.0)
        strategy["stop_loss_pct"] = round(min(5.0, current_sl + 0.1), 2)
        hypothesis["action"] = "widen_stop_loss"
        hypothesis["reasoning"] = (
            f"Good performance. Widened stop_loss_pct from {current_sl} to {strategy['stop_loss_pct']} to capture more upside"
        )

    else:
        # Neutral performance — tighten threshold slightly
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
        "btc_min_4h_rsi": strategy.get("btc_gate", {}).get("min_btc_4h_rsi"),
        "fng_min_value": strategy.get("fng_gate", {}).get("min_value"),
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
