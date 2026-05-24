#!/usr/bin/env python3
"""Prepare structured trade data for LLM audit.

Outputs a JSON file per asset containing everything the four diagnostic
prompts need, plus a consolidated portfolio file. The LLM cron reads
these files, applies the prompts, and writes findings.

Usage:
  uv run python scripts/prepare_llm_audit.py

Output: state/*/llm_audit_data.json  +  state/portfolio_audit.json
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path("/opt/data/hermes-trading")
STATE_DIR = BASE_DIR / "state"


def load_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().strip().splitlines() if line.strip()]


def compute_r_multiple(trade, strategy):
    """Compute the R-multiple for a trade. 1R = stop loss distance."""
    entry = trade.get("entry_price", 0)
    exit_p = trade.get("exit_price", 0)
    direction = trade.get("direction", "long")
    sl_pct = strategy.get("stop_loss_pct", 2.0) if strategy else 2.0

    if entry == 0 or exit_p == 0:
        return 0

    pnl_pct = trade.get("pnl_pct", 0)
    if sl_pct == 0:
        return pnl_pct
    return round(pnl_pct / sl_pct, 2)


def prepare_asset_audit(asset_key):
    """Prepare all structured data for one asset."""
    trades = load_jsonl(STATE_DIR / asset_key / "trades.jsonl")
    setups = load_jsonl(STATE_DIR / asset_key / "setups_log.jsonl")
    strategy_path = STATE_DIR / asset_key / "strategy.yaml"
    strategy = {}
    if strategy_path.exists():
        import yaml
        strategy = yaml.safe_load(strategy_path.read_text()) or {}

    completed = [t for t in trades if t.get("exit_time") and t.get("entry_time")]

    # ── R-Multiple Distribution ──
    r_multis = [compute_r_multiple(t, strategy) for t in completed]

    # Bucket
    r_buckets = {
        "<-3R": 0, "-3R to -2R": 0, "-2R to -1R": 0, "-1R to 0": 0,
        "0 to 1R": 0, "1R to 2R": 0, "2R to 3R": 0, ">3R": 0,
    }
    for r in r_multis:
        if r < -3: r_buckets["<-3R"] += 1
        elif r < -2: r_buckets["-3R to -2R"] += 1
        elif r < -1: r_buckets["-2R to -1R"] += 1
        elif r < 0: r_buckets["-1R to 0"] += 1
        elif r < 1: r_buckets["0 to 1R"] += 1
        elif r < 2: r_buckets["1R to 2R"] += 1
        elif r < 3: r_buckets["2R to 3R"] += 1
        else: r_buckets[">3R"] += 1

    # ── Time-in-Trade vs Outcome ──
    durations = []
    for t in completed:
        try:
            entry = datetime.fromisoformat(t["entry_time"])
            exit_t = datetime.fromisoformat(t["exit_time"])
            mins = (exit_t - entry).total_seconds() / 60
            r = compute_r_multiple(t, strategy)
            durations.append({"duration_mins": round(mins, 1), "r_multiple": r, "pnl_pct": t.get("pnl_pct", 0)})
        except (ValueError, KeyError):
            pass

    # Bucket durations
    dur_buckets = {"0-5m": [], "5-15m": [], "15-30m": [], "30-60m": [], "60m+": []}
    for d in durations:
        mins = d["duration_mins"]
        if mins <= 5: dur_buckets["0-5m"].append(d)
        elif mins <= 15: dur_buckets["5-15m"].append(d)
        elif mins <= 30: dur_buckets["15-30m"].append(d)
        elif mins <= 60: dur_buckets["30-60m"].append(d)
        else: dur_buckets["60m+"].append(d)

    dur_analysis = {}
    for bucket, items in dur_buckets.items():
        if items:
            avg_r = sum(i["r_multiple"] for i in items) / len(items)
            avg_pnl = sum(i["pnl_pct"] for i in items) / len(items)
            wins = sum(1 for i in items if i["pnl_pct"] > 0)
            dur_analysis[bucket] = {
                "count": len(items),
                "avg_r": round(avg_r, 2),
                "avg_pnl_pct": round(avg_pnl, 3),
                "win_rate": round(wins / len(items) * 100, 1),
            }

    # ── Regime Labels vs Outcomes (last 50) ──
    recent = completed[-50:] if len(completed) >= 50 else completed
    regime_outcomes = defaultdict(list)
    for t in recent:
        regime = t.get("regime") or t.get("market_regime_entry") or "unknown"
        regime_outcomes[regime].append({
            "pnl_pct": t.get("pnl_pct", 0),
            "direction": t.get("direction", "long"),
            "exit_reason": t.get("exit_reason", "?"),
        })

    regime_analysis = {}
    for regime, outcomes in sorted(regime_outcomes.items()):
        pnls = [o["pnl_pct"] for o in outcomes]
        wins = sum(1 for p in pnls if p > 0)
        regime_analysis[str(regime)] = {
            "count": len(outcomes),
            "avg_pnl_pct": round(sum(pnls) / len(pnls), 4) if pnls else 0,
            "win_rate": round(wins / len(pnls) * 100, 1) if pnls else 0,
            "total_pnl_pct": round(sum(pnls), 2),
        }

    # ── Win/Loss Sequence (for autocorrelation) ──
    win_loss_seq = []
    for t in recent:
        pnl = t.get("pnl_pct", 0)
        if pnl > 0:
            win_loss_seq.append("W")
        elif pnl < 0:
            win_loss_seq.append("L")
        else:
            win_loss_seq.append("B/E")

    # ── Skip-Quality Analysis (evaluate if skip thresholds are correct) ──
    skips_by_reason = defaultdict(list)
    for s in setups[-200:]:  # last 200 setups
        reason = s.get("reason", "unknown")
        score = s.get("confidence_score")
        if reason.startswith("low_confidence"):
            skips_by_reason["low_confidence"].append({"score": score, "reason": reason})
        elif reason.startswith(("cooldown", "kill_switch", "mc_dd", "portfolio_dd", "correlation")):
            base = reason.split(":")[0]
            skips_by_reason[base].append({"score": score, "reason": reason})
        else:
            skips_by_reason["other"].append({"score": score, "reason": reason})

    skip_analysis = {}
    for reason, entries in sorted(skips_by_reason.items()):
        scores = [e["score"] for e in entries if e["score"] is not None]
        skip_analysis[reason] = {
            "count": len(entries),
            "avg_score": round(sum(scores) / len(scores), 3) if scores else None,
            "max_score": round(max(scores), 3) if scores else None,
            "min_score": round(min(scores), 3) if scores else None,
            "near_misses": sum(1 for s in scores if s is not None and 0.55 <= s < 0.60),
        }

    # ── Intended Policy vs Actual Execution ──
    intended_policy = {
        "entry_indicator": strategy.get("entry", {}).get("indicator", "rsi"),
        "entry_threshold": strategy.get("entry", {}).get("threshold", 30),
        "stop_loss_pct": strategy.get("stop_loss_pct", 2.0),
        "position_size_r": strategy.get("position_size_r", 0.5),
        "cooldown_cycles": strategy.get("cooldown_cycles", 30),
        "btc_4h_min_rsi": strategy.get("btc_gate", {}).get("min_btc_4h_rsi", 25),
        "btc_1h_min_rsi": strategy.get("btc_gate", {}).get("min_btc_1h_rsi", 20),
        "fng_min_value": strategy.get("fng_gate", {}).get("min_value", 10),
        "max_consecutive_losses": strategy.get("max_consecutive_losses", 5),
    }

    # Check recent trades for gate violations only (position sizing is dynamic)
    discipline_checks = []
    position_sizes = []
    for t in completed[-10:]:
        violations = []
        # Gate: BTC 4h RSI
        btc_4h = t.get("btc_entry_4h_rsi")
        if btc_4h is not None and btc_4h < intended_policy["btc_4h_min_rsi"]:
            violations.append(f"btc_4h_rsi={btc_4h:.0f} < gate={intended_policy['btc_4h_min_rsi']}")
        # Gate: BTC 1h RSI
        btc_1h = t.get("btc_entry_1h_rsi")
        if btc_1h is not None and btc_1h < intended_policy["btc_1h_min_rsi"]:
            violations.append(f"btc_1h_rsi={btc_1h:.0f} < gate={intended_policy['btc_1h_min_rsi']}")
        # Gate: Fear & Greed
        fng = t.get("fear_greed_entry")
        if fng is not None and fng < intended_policy["fng_min_value"]:
            violations.append(f"fng={fng} < gate={intended_policy['fng_min_value']}")
        # Track actual position sizes for LLM context (NOT a violation check — size is dynamic)
        pos_size = t.get("position_size_r", 0)
        signal = t.get("signal", "?")
        position_sizes.append({"entry_time": t.get("entry_time", "")[:19], "size": pos_size, "signal": signal})
        discipline_checks.append({
            "entry_time": t.get("entry_time", "")[:19],
            "exit_reason": t.get("exit_reason", "?"),
            "pnl_pct": t.get("pnl_pct", 0),
            "signal": signal,
            "violations": violations,
        })

    return {
        "asset": asset_key,
        "total_trades": len(trades),
        "completed_trades": len(completed),
        "period": {
            "first_trade": completed[0].get("entry_time", "")[:19] if completed else "N/A",
            "last_trade": completed[-1].get("exit_time", "")[:19] if completed else "N/A",
        },
        "prompt_1_discipline": {
            "intended_policy": intended_policy,
            "recent_checks": discipline_checks,
            "violation_count": sum(1 for c in discipline_checks if c["violations"]),
            "position_scaling_context": {
                "note": "Position sizes are dynamic (volatility scalar × streak scalar × confidence scale). Variations from base_r are INTENTIONAL, not discipline slips.",
                "base_r": strategy.get("position_size_r", 0.5),
                "recent_actual_sizes": position_sizes,
                "system": "full entries use base_r (0.5R) × vol_scalar × streak_scalar | half entries use 0.5× (confidence 0.55-0.60) | full entries use 1× (confidence ≥0.60)",
            },
        },
        "prompt_2_r_multiple": {
            "histogram": r_buckets,
            "total_r_multis": len(r_multis),
            "avg_r": round(sum(r_multis) / len(r_multis), 2) if r_multis else 0,
            "max_r": round(max(r_multis), 2) if r_multis else 0,
            "min_r": round(min(r_multis), 2) if r_multis else 0,
            "histogram_shape": "right-skewed" if sum(r_buckets.get(b, 0) for b in ["<-3R","-3R to -2R","-2R to -1R"]) > sum(r_buckets.get(b, 0) for b in ["1R to 2R","2R to 3R",">3R"]) else "left-skewed" if sum(r_buckets.get(b, 0) for b in [">3R","2R to 3R","1R to 2R"]) > sum(r_buckets.get(b, 0) for b in ["<-3R","-3R to -2R","-2R to -1R"]) else "symmetric",
        },
        "prompt_3_duration": {
            "durations_analyzed": len(durations),
            "duration_buckets": dur_analysis,
            "longest_trade_mins": max(d["duration_mins"] for d in durations) if durations else 0,
            "shortest_trade_mins": min(d["duration_mins"] for d in durations) if durations else 0,
        },
        "prompt_4_regime": {
            "win_loss_sequence": win_loss_seq[-20:],  # last 20 for readability
            "total_sequence_length": len(win_loss_seq),
            "regime_performance": regime_analysis,
            "clusters": [],
        },
        "skip_quality": {
            "analysis": skip_analysis,
            "total_skips": len(setups),
            "evaluation_note": "LLM: compare near_miss counts against threshold settings. If many near-misses (0.55-0.60) on winning moves, thresholds may be too strict.",
        },
        "recent_strategy_version": strategy.get("version", "?"),
        "current_strategy": {
            "entry_threshold": intended_policy["entry_threshold"],
            "stop_loss_pct": intended_policy["stop_loss_pct"],
            "btc_4h_gate": intended_policy["btc_4h_min_rsi"],
        },
    }


def main():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"[{ts}] 📊 Preparing LLM audit data")

    assets = sorted([
        d.name for d in STATE_DIR.iterdir()
        if d.is_dir() and (d / "trades.jsonl").exists()
    ])

    portfolio = []
    for asset in assets:
        try:
            data = prepare_asset_audit(asset)
            out_path = STATE_DIR / asset / "llm_audit_data.json"
            out_path.write_text(json.dumps(data, indent=2, default=str))
            print(f"  ✅ {asset:12s}  {data['completed_trades']:3d} trades → {out_path.name}")
            portfolio.append(data)
        except Exception as e:
            print(f"  ❌ {asset:12s}  ERROR: {e}")

    # Portfolio summary
    portfolio_path = STATE_DIR / "portfolio_audit.json"
    portfolio_data = {
        "generated_at": ts,
        "total_assets": len(portfolio),
        "total_trades_across_portfolio": sum(a["completed_trades"] for a in portfolio),
        "assets": [
            {
                "asset": a["asset"],
                "trades": a["completed_trades"],
                "avg_r": a["prompt_2_r_multiple"]["avg_r"],
                "histogram_shape": a["prompt_2_r_multiple"]["histogram_shape"],
                "violations": a["prompt_1_discipline"]["violation_count"],
                "durations": list(a["prompt_3_duration"]["duration_buckets"].keys()),
            }
            for a in portfolio
        ],
    }
    portfolio_path.write_text(json.dumps(portfolio_data, indent=2, default=str))
    print(f"\n  ✅ Portfolio summary → {portfolio_path.name}")
    print(f"  Total assets: {len(assets)} | Total completed trades: {portfolio_data['total_trades_across_portfolio']}")


if __name__ == "__main__":
    main()
