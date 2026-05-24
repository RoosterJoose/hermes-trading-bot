#!/usr/bin/env python3
"""Drawdown Autopsy — post-mortem analysis when loss triggers fire.

Document's triggers:
  • Daily loss > 1.5%
  • Weekly drawdown > 4%  
  • 5 consecutive losses

Runs as part of the daily readiness check. Writes structured autopsy
reports to state/*/drawdown_autopsy.json for LLM review.
"""
import json
import os
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR = Path("/opt/data/hermes-trading")
STATE_DIR = BASE_DIR / "state"


def load_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().strip().splitlines() if line.strip()]


def check_triggers() -> dict:
    """Check all three drawdown triggers. Returns list of triggered assets."""
    result = {
        "daily_loss_triggered": False,
        "weekly_dd_triggered": False,
        "consecutive_losses_triggered": False,
        "triggered_assets": [],
        "daily_pnl_pct": 0.0,
        "weekly_dd_pct": 0.0,
        "max_consecutive_losses": 0,
    }

    hb_path = STATE_DIR / "heartbeat.json"
    if not hb_path.exists():
        return result

    try:
        hb = json.loads(hb_path.read_text())
    except Exception:
        return result

    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)
    week_ago = now - timedelta(days=7)

    # Daily PnL from trades in last 24h
    daily_pnls = []
    weekly_pnls = []

    for asset_dir in sorted(STATE_DIR.iterdir()):
        if not asset_dir.is_dir():
            continue
        trades = load_jsonl(asset_dir / "trades.jsonl")
        if not trades:
            continue

        # Track consecutive losses
        completed = [t for t in trades if t.get("exit_time")]
        sorted_t = sorted(completed, key=lambda x: x.get("exit_time", ""))
        loss_streak = 0
        max_streak = 0
        for t in sorted_t:
            pnl = t.get("pnl_pct", 0)
            if pnl < 0:
                loss_streak += 1
                max_streak = max(max_streak, loss_streak)
            else:
                loss_streak = 0

        if max_streak >= 5:
            result["triggered_assets"].append({
                "asset": asset_dir.name,
                "reason": f"{max_streak} consecutive losses",
                "trades_in_streak": max_streak,
            })

        for t in trades:
            exit_time = t.get("exit_time", "")
            try:
                dt = datetime.fromisoformat(exit_time)
                if dt > day_ago:
                    daily_pnls.append(t.get("pnl_pct", 0))
                if dt > week_ago:
                    weekly_pnls.append(t.get("pnl_pct", 0))
            except (ValueError, TypeError):
                continue

    result["max_consecutive_losses"] = max(
        (a["trades_in_streak"] for a in result["triggered_assets"]), default=0
    )

    # Daily loss > 1.5%
    if daily_pnls:
        daily_total = sum(daily_pnls)
        result["daily_pnl_pct"] = round(daily_total, 2)
        if daily_total < -1.5:
            result["daily_loss_triggered"] = True

    # Weekly drawdown > 4%
    if weekly_pnls:
        weekly_total = sum(weekly_pnls)
        result["weekly_dd_pct"] = round(weekly_total, 2)
        if weekly_total < -4.0:
            result["weekly_dd_triggered"] = True

    # 5 consecutive losses
    if result["triggered_assets"]:
        result["consecutive_losses_triggered"] = True

    return result


def write_autopsy(asset_key: str, trades: list, trigger_info: str):
    """Write a structured autopsy for one asset to its state directory."""
    completed = [t for t in trades if t.get("exit_time") and t.get("entry_time")]
    recent = sorted(completed, key=lambda x: x.get("exit_time", ""))[-20:]

    # Compute key metrics for the drawdown period
    pnls = [t.get("pnl_pct", 0) for t in recent]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)

    # R-multiples
    strategy_path = STATE_DIR / asset_key / "strategy.yaml"
    sl_pct = 2.0
    if strategy_path.exists():
        import yaml
        try:
            with open(strategy_path) as f:
                s = yaml.safe_load(f) or {}
                sl_pct = s.get("stop_loss_pct", 2.0)
        except Exception:
            pass

    r_multis = []
    for t in recent:
        pnl = t.get("pnl_pct", 0)
        r = round(pnl / sl_pct, 2) if sl_pct else pnl
        r_multis.append({"r": r, "exit_reason": t.get("exit_reason", "?"), "pnl_pct": pnl})

    autopsy = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "asset": asset_key,
        "trigger": trigger_info,
        "total_trades_in_period": len(recent),
        "win_rate": round(wins / max(len(pnls), 1) * 100, 1),
        "total_pnl_pct": round(sum(pnls), 2),
        "avg_r": round(sum(r["r"] for r in r_multis) / max(len(r_multis), 1), 2),
        "exit_reasons": dict((r["exit_reason"], sum(1 for x in r_multis if x["exit_reason"] == r["exit_reason"])) for r in r_multis),
        "recent_trades": [
            {
                "entry": t.get("entry_time", "")[:19],
                "exit": t.get("exit_time", "")[:19],
                "pnl_pct": t.get("pnl_pct", 0),
                "exit_reason": t.get("exit_reason", "?"),
                "regime": t.get("regime") or t.get("market_regime_entry", "unknown"),
            }
            for t in recent[-10:]
        ],
        "current_strategy": {
            "stop_loss_pct": sl_pct,
        },
    }

    out_path = STATE_DIR / asset_key / "drawdown_autopsy.json"
    out_path.write_text(json.dumps(autopsy, indent=2, default=str))
    return autopsy


def run():
    """Main entry point. Checks triggers and writes autopsies if needed."""
    triggers = check_triggers()
    any_triggered = triggers["daily_loss_triggered"] or triggers["weekly_dd_triggered"] or triggers["consecutive_losses_triggered"]

    if any_triggered:
        print(f"⚠️  DRAWDOWN TRIGGER DETECTED")
        if triggers["daily_loss_triggered"]:
            print(f"   Daily loss: {triggers['daily_pnl_pct']:.2f}% (trigger: < -1.5%)")
        if triggers["weekly_dd_triggered"]:
            print(f"   Weekly drawdown: {triggers['weekly_dd_pct']:.2f}% (trigger: < -4%)")
        if triggers["consecutive_losses_triggered"]:
            print(f"   Consecutive losses: {triggers['max_consecutive_losses']} (trigger: >= 5)")

        # Write autopsies
        for asset_dir in sorted(STATE_DIR.iterdir()):
            if not asset_dir.is_dir():
                continue
            trades = load_jsonl(asset_dir / "trades.jsonl")
            if not trades:
                continue
            completed = [t for t in trades if t.get("exit_time")]
            if len(completed) < 5:
                continue

            trigger_info = []
            pnls = [t.get("pnl_pct", 0) for t in completed[-5:]]
            if sum(pnls) < -1.5:
                trigger_info.append(f"daily_loss_{sum(pnls):+.2f}%")
            loss_streak = 0
            for t in reversed(completed):
                if t.get("pnl_pct", 0) < 0:
                    loss_streak += 1
                else:
                    break
            if loss_streak >= 5:
                trigger_info.append(f"loss_streak_{loss_streak}")

            if trigger_info:
                autopsy = write_autopsy(asset_dir.name, completed, "; ".join(trigger_info))
                print(f"   📄 Autopsy: {asset_dir.name} ({'; '.join(trigger_info)})")
    else:
        print(f"   No drawdown triggers — system healthy (daily: {triggers['daily_pnl_pct']:.2f}%, weekly: {triggers['weekly_dd_pct']:.2f}%)")

    return int(any_triggered)


if __name__ == "__main__":
    exit(run())
