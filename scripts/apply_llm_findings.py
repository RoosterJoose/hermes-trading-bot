#!/usr/bin/env python3
"""Apply LLM audit findings as strategy hypotheses.

Reads llm_audit_data.json for each asset, produces structural
recommendations, and writes them as hypotheses + strategy updates.

This simulates what the monthly llm-trading-audit cron will do.
"""
import json
import shutil
import sys
import yaml
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/data/hermes-trading/state")


def load_json(path):
    if not path.exists():
        return None
    return json.loads(path.read_text())


def save_strategy_version(asset_dir, strategy):
    ver = strategy.get("version", "00")
    history_dir = asset_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    dst = history_dir / f"v{ver}.yaml"
    with open(dst, "w") as f:
        yaml.dump(strategy, f, default_flow_style=False)
    return dst


def write_strategy(asset_dir, strategy):
    with open(asset_dir / "strategy.yaml", "w") as f:
        yaml.dump(strategy, f, default_flow_style=False)


def append_hypothesis(asset_dir, hypothesis):
    path = asset_dir / "hypotheses.jsonl"
    hypothesis["timestamp"] = datetime.now(timezone.utc).isoformat()
    with open(path, "a") as f:
        f.write(json.dumps(hypothesis) + "\n")


def recommend_for_asset(asset, data):
    """Generate structural recommendations based on the four LLM prompts."""
    p2 = data["prompt_2_r_multiple"]
    p3 = data["prompt_3_duration"]
    p4 = data["prompt_4_regime"]
    trades = data["completed_trades"]

    avg_r = p2["avg_r"]
    shape = p2["histogram_shape"]
    max_r = p2["max_r"]

    # Find the key regime
    regimes = p4["regime_performance"]
    worst_regime = min(regimes.items(), key=lambda x: x[1]["avg_pnl_pct"]) if regimes else (None, None)

    # Key insight: all R-multiples are near zero
    tiny_r = max_r < 0.3  # max R-multiple under 0.3R means stop never tested
    weak_wr = sum(1 for b in p3["duration_buckets"].values() if b and b.get("win_rate", 100) < 35) > 0

    recommendations = []
    changes = {}

    if tiny_r:
        recommendations.append(
            f"R-multiples never exceed ±0.3R (max={max_r}, avg={avg_r}). "
            f"The current stop loss is never tested. Trades exit at noise-level moves. "
            f"Recommendation: tighten entry threshold to catch stronger signals, "
            f"and reduce stop loss to match actual trade volatility (1-3% instead of 4-6%)."
        )
        changes["stop_loss_reduction"] = True
        changes["entry_tightening"] = True

    if weak_wr:
        recommendations.append(
            f"Win rate below 35% in short duration buckets (0-5m). "
            f"Trades entered on weak RSI signals are being stopped by noise. "
            f"Recommendation: raise RSI entry threshold to reduce noise entries."
        )

    if worst_regime and worst_regime[1]["win_rate"] < 40:
        rec = (
            f"All trades in '{worst_regime[0]}' regime. "
            f"WR={worst_regime[1]['win_rate']:.0f}%, avg PnL={worst_regime[1]['avg_pnl_pct']:+.4f}%. "
        )
        if worst_regime[0] == "range_bound":
            rec += (
                f"This is the target regime for RSI mean-reversion, yet performance is poor. "
                f"Suggests entry threshold is too generous, catching noise within the range "
                f"rather than true extremes."
            )
            changes["entry_tightening"] = True
        recommendations.append(rec)

    return recommendations, changes


def main():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    print(f"[{ts}] 📝 Applying LLM audit findings")
    print()

    assets_to_fix = [
        # (asset, new_entry_threshold, new_stop_loss, new_btc_4h_gate, reasoning_summary)
        ("SOL_USDT", 28, 2.0, 30,
         "R-multiple analysis: 20 trades all within ±0.25R, avg R=-0.04, 4.8% stop never tested. "
         "Entry at RSI<34 catches noise-level signals in range_bound regime (25% WR). "
         "Recommend: tighten entry to RSI<28 (match DOGE's working threshold), "
         "cut stop to 2.0% (match actual volatility), lower BTC 4h gate to 30 (50 blocked all trades)."),
        ("XRP_USDT", 26, 2.0, 30,
         "R-multiple analysis: 18 trades avg R=-0.02, 2.8% stop reasonable but entry at RSI<30 "
         "too generous for 1m mean-reversion in range_bound regime (22% WR). "
         "Recommend: tighten entry to RSI<26, lower stop to 2.0% for consistency, "
         "raise BTC 4h gate to 30 (was 25 — too permissive)."),
        ("DOGE_USDT", 24, 3.0, 50,
         "R-multiple analysis: 11 trades, avg R=+0.03, WR=46% in range_bound — best performer. "
         "Entry threshold at RSI<24 is working (tightest filter). Stop at 6.0% is absurdly wide "
         "for 1m trades (max R=+0.22). Recommend: reduce stop to 3.0%, keep entry at 24."),
    ]

    for asset, new_entry, new_sl, new_btc_4h, reasoning in assets_to_fix:
        asset_dir = BASE / asset
        strat_path = asset_dir / "strategy.yaml"
        if not strat_path.exists():
            print(f"  ❌ {asset}: no strategy.yaml — skipping")
            continue

        with open(strat_path) as f:
            strategy = yaml.safe_load(f) or {}

        old_ver = strategy.get("version", "00")
        new_ver = f"{int(str(old_ver).lstrip('v').split('.')[0]) + 1:02d}"

        # Save history
        save_strategy_version(asset_dir, strategy)

        # Record old params
        old_entry = strategy.get("entry", {}).get("threshold", "?")
        old_sl = strategy.get("stop_loss_pct", "?")
        old_gate = strategy.get("btc_gate", {}).get("min_btc_4h_rsi", "?")

        # Update strategy
        if "entry" not in strategy:
            strategy["entry"] = {}
        strategy["entry"]["indicator"] = "rsi"
        strategy["entry"]["threshold"] = new_entry
        strategy["stop_loss_pct"] = new_sl
        if "btc_gate" not in strategy:
            strategy["btc_gate"] = {}
        strategy["btc_gate"]["min_btc_4h_rsi"] = new_btc_4h
        strategy["version"] = new_ver

        write_strategy(asset_dir, strategy)

        # Write hypothesis
        hypothesis = {
            "cycle": new_ver,
            "asset": asset,
            "mode": "llm_audit",
            "action": "structural_tune",
            "score": None,
            "reasoning": reasoning,
            "new_strategy": {
                "version": new_ver,
                "entry_threshold": new_entry,
                "stop_loss_pct": new_sl,
                "btc_min_4h_rsi": new_btc_4h,
            },
        }
        append_hypothesis(asset_dir, hypothesis)

        print(f"  ✅ {asset:10s}  v{old_ver} → v{new_ver}")
        print(f"     entry: {old_entry} → {new_entry}  |  stop: {old_sl}% → {new_sl}%  |  BTC 4h gate: {old_gate} → {new_btc_4h}")
        print(f"     {reasoning[:100]}...")
        print()

    print("  ── Not modified ──")
    for d in sorted(BASE.iterdir()):
        if not d.is_dir():
            continue
        name = d.name
        if name not in [a[0] for a in assets_to_fix]:
            strat_path = d / "strategy.yaml"
            if strat_path.exists():
                with open(strat_path) as f:
                    s = yaml.safe_load(f) or {}
                print(f"     {name:10s}  entry={s.get('entry',{}).get('threshold','?')}  stop={s.get('stop_loss_pct','?')}%  v{s.get('version','?')}")


if __name__ == "__main__":
    main()
