#!/usr/bin/env python3
"""Daily trading system readiness report — setups analysis + feature readiness.

Reads:
  - state/*/setups_log.jsonl   — skipped-setup data for threshold tuning
  - state/*/trades.jsonl       — trade history for count/performance tracking
  - state/heartbeat.json       — current bot state
  - /tmp/trading_loop.log      — recent errors/bot health

Reports:
  1. Bot health
  2. Setups analysis (score distribution, threshold tuning)
  3. Trade counts per asset (tracking toward 200 for optimizer)
  4. Performance snapshot
  5. Feature readiness summary
"""

import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

STATE_DIR = Path("/opt/data/hermes-trading/state")
LOG_FILE = Path("/tmp/trading_loop.log")

OPTIMIZER_THRESHOLD = 200  # trades per asset

# ── helpers ──


def find_files(dirpath: Path, pattern: str) -> list[Path]:
    return list(dirpath.rglob(pattern))


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    lines = path.read_text().strip().splitlines()
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return entries


def ago(ts_str: str) -> str:
    """Return human-friendly 'X ago' from ISO timestamp string."""
    try:
        dt = datetime.fromisoformat(ts_str)
        delta = datetime.now(timezone.utc) - dt
        mins = int(delta.total_seconds() / 60)
        if mins < 1:
            return "just now"
        if mins < 60:
            return f"{mins}m ago"
        hours = mins // 60
        if hours < 24:
            return f"{hours}h {mins % 60}m ago"
        days = hours // 24
        return f"{days}d {hours % 24}h ago"
    except Exception:
        return ts_str[:19]


# ── 1. Bot health ──


def check_bot_health() -> dict:
    result = {"status": "❓ Unknown", "errors_last_100": 0, "pid": None, "uptime": None}

    # Find PID
    try:
        r = subprocess.run(
            ["ps", "aux"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in r.stdout.splitlines():
            if "python -m hermes_trading" in line and "grep" not in line:
                parts = line.split()
                result["pid"] = int(parts[1])
                break
    except Exception:
        pass

    if result["pid"]:
        result["status"] = "✅ Running"
        # Uptime from heartbeat timestamp
        if STATE_DIR.joinpath("heartbeat.json").exists():
            try:
                hb = json.loads(STATE_DIR.joinpath("heartbeat.json").read_text())
                ts = hb.get("timestamp", "")
                if ts:
                    result["uptime"] = ago(ts)
            except Exception:
                pass
    else:
        result["status"] = "❌ NOT RUNNING"

    # Errors in log
    if LOG_FILE.exists():
        try:
            log_text = LOG_FILE.read_text()
            lines = log_text.splitlines()
            recent = lines[-100:]
            result["errors_last_100"] = sum(
                1
                for l in recent
                if any(w in l.lower() for w in ["traceback", "error", "exception"])
            )
        except Exception:
            pass

    return result


# ── 2. Setups analysis ──


def analyze_setups() -> dict:
    """Analyze all setups_log.jsonl files."""
    result = {
        "total_cycles": 0,
        "skipped_setups": 0,
        "entries": 0,
        "score_buckets": Counter(),
        "safety_blocks": Counter(),
        "score_distribution": {
            "0-0.2": 0,
            "0.2-0.4": 0,
            "0.4-0.62": 0,
            "0.62-0.70": 0,
            "0.70-1.0": 0,
        },
        "per_asset": {},
        "avg_score_full": 0.0,
        "avg_score_half": 0.0,
        "avg_score_skip": 0.0,
    }

    total_full_score = 0
    total_half_score = 0
    total_skip_score = 0
    count_full = 0
    count_half = 0
    count_skip = 0

    for f in find_files(STATE_DIR, "setups_log.jsonl"):
        asset = f.parent.name
        entries = read_jsonl(f)
        if not entries:
            continue

        per_asset = {"total": len(entries), "safety": 0, "low_conf": 0, "entries": 0}
        for e in entries:
            result["total_cycles"] += 1
            reason = e.get("reason", "")
            score = e.get("confidence_score")

            if reason.startswith("low_confidence"):
                result["skipped_setups"] += 1
                per_asset["low_conf"] += 1
                if score is not None:
                    total_skip_score += score
                    count_skip += 1
                    if score < 0.2:
                        result["score_distribution"]["0-0.2"] += 1
                    elif score < 0.4:
                        result["score_distribution"]["0.2-0.4"] += 1
                    elif score < 0.62:
                        result["score_distribution"]["0.4-0.62"] += 1
                    elif score < 0.70:
                        result["score_distribution"]["0.62-0.70"] += 1
                    else:
                        result["score_distribution"]["0.70-1.0"] += 1
            elif reason.startswith(
                ("cooldown", "kill_switch", "mc_dd", "portfolio_dd", "correlation")
            ):
                result["skipped_setups"] += 1
                per_asset["safety"] += 1
                # Bucket safety reasons
                base_reason = reason.split(":")[0] if ":" in reason else reason
                result["safety_blocks"][base_reason] += 1
            else:
                result["skipped_setups"] += 1
                per_asset["safety"] += 1

        result["per_asset"][asset] = per_asset

    # Also check for entries from trades log — only count ones with confidence data
    total_trades = 0
    for f in find_files(STATE_DIR, "trades.jsonl"):
        trades = read_jsonl(f)
        total_trades += len(trades)
        for t in trades:
            # Only count entries from the confidence score era (have enter_confidence field)
            entry_score = t.get("enter_confidence")
            if entry_score is None:
                continue
            signal = t.get("signal", "")
            if signal in ("rsi_oversold", "rsi_oversold_half"):
                result["entries"] += 1
                if "half" in signal:
                    total_half_score += entry_score
                    count_half += 1
                else:
                    total_full_score += entry_score
                    count_full += 1

    if count_full:
        result["avg_score_full"] = round(total_full_score / count_full, 3)
    if count_half:
        result["avg_score_half"] = round(total_half_score / count_half, 3)
    if count_skip:
        result["avg_score_skip"] = round(total_skip_score / count_skip, 3)

    # Threshold tuning recommendation
    near_miss_entries = sum(
        1
        for f in find_files(STATE_DIR, "setups_log.jsonl")
        for e in read_jsonl(f)
        if isinstance(e.get("confidence_score"), (int, float))
        and 0.62 <= e["confidence_score"] < 0.65
        and e.get("reason", "").startswith("low_confidence")
    )
    result["near_misses_062_065"] = near_miss_entries

    return result


# ── 3. Trade counts + optimizer readiness ──


def check_trade_progress() -> dict:
    result = {
        "per_asset": {},
        "total_trades": 0,
        "optimizer_ready": [],
        "closest_to_200": None,
    }

    for f in find_files(STATE_DIR, "trades.jsonl"):
        asset = f.parent.name
        trades = read_jsonl(f)
        count = len(trades)
        result["per_asset"][asset] = count
        result["total_trades"] += count
        if count >= OPTIMIZER_THRESHOLD:
            result["optimizer_ready"].append(asset)

    # Find closest
    max_count = 0
    closest_asset = None
    for asset, count in result["per_asset"].items():
        if count > max_count:
            max_count = count
            closest_asset = asset
    if closest_asset:
        result["closest_to_200"] = {
            "asset": closest_asset,
            "count": max_count,
            "remaining": max(0, OPTIMIZER_THRESHOLD - max_count),
        }

    return result


# ── 4. Performance snapshot ──


def performance_snapshot() -> dict:
    result = {
        "paper_balance": 0,
        "total_pnl_pct": 0.0,
        "open_positions": 0,
        "win_rate": 0.0,
        "sharpe": 0.0,
        "per_asset_perf": {},
    }

    # Heartbeat
    hb_path = STATE_DIR / "heartbeat.json"
    if hb_path.exists():
        try:
            hb = json.loads(hb_path.read_text())
            result["paper_balance"] = hb.get("paper_balance", 0)
            result["total_pnl_pct"] = hb.get("total_pnl_pct", 0.0)
            result["open_positions"] = sum(
                1 for v in hb.get("positions", {}).values() if v is not None
            )
        except Exception:
            pass

    # Trade performance from trades.jsonl
    all_trades = []
    for f in find_files(STATE_DIR, "trades.jsonl"):
        asset = f.parent.name
        trades = read_jsonl(f)
        for t in trades:
            t["_asset"] = asset
        all_trades.extend(trades)

    # Check benchmark targets from optimizer_log.jsonl
    benchmark_status = {}
    for f in find_files(STATE_DIR, "optimizer_log.jsonl"):
        asset = f.parent.name
        entries = read_jsonl(f)
        if entries:
            latest = entries[-1]
            bm = latest.get("benchmarks", {})
            if bm.get("status") == "analyzed":
                benchmark_status[asset] = {
                    "sharpe": bm.get("sharpe"),
                    "sharpe_met": bm.get("sharpe_met"),
                    "sharpe_target": bm.get("sharpe_target"),
                    "win_rate": bm.get("win_rate"),
                    "win_rate_met": bm.get("win_rate_met"),
                    "max_dd": bm.get("max_drawdown"),
                    "max_dd_met": bm.get("max_dd_met"),
                }
    result["benchmarks"] = benchmark_status

    if all_trades:
        completed = [t for t in all_trades if t.get("exit_time")]
        closed_pnls = [t.get("pnl_pct", 0) for t in completed]
        wins = [p for p in closed_pnls if p > 0]

        if completed:
            result["win_rate"] = round(len(wins) / len(completed) * 100, 1)

            # Per-asset
            per_asset = defaultdict(list)
            for t in completed:
                per_asset[t["_asset"]].append(t.get("pnl_pct", 0))
            for asset, pnls in per_asset.items():
                wins_a = [p for p in pnls if p > 0]
                result["per_asset_perf"][asset] = {
                    "trades": len(pnls),
                    "win_rate": round(len(wins_a) / len(pnls) * 100, 1),
                    "avg_pnl": round(sum(pnls) / len(pnls), 2),
                }

    return result


# ── 5. Reflection Health ──


def check_reflection_health() -> dict:
    """Check if the reflection engine is producing hypotheses consistently."""
    result = {
        "total_assets_with_hypotheses": 0,
        "stale_assets": [],
        "healthy_assets": [],
        "latest_hypothesis_overall": None,
        "status": "✅ Healthy",
    }

    now = datetime.now(timezone.utc)
    for asset_dir in sorted(STATE_DIR.iterdir()):
        if not asset_dir.is_dir():
            continue
        hyp_file = asset_dir / "hypotheses.jsonl"
        if not hyp_file.exists():
            continue
        hypotheses = read_jsonl(hyp_file)
        if not hypotheses:
            continue

        result["total_assets_with_hypotheses"] += 1
        latest = hypotheses[-1]
        latest_ts = latest.get("timestamp", "")
        try:
            dt = datetime.fromisoformat(latest_ts)
            age_hours = (now - dt).total_seconds() / 3600
            entry = {
                "asset": asset_dir.name,
                "total_hypotheses": len(hypotheses),
                "latest_timestamp": latest_ts[:19],
                "age_hours": round(age_hours, 1),
                "latest_action": latest.get("action", "?"),
            }
            if age_hours < 3:
                result["healthy_assets"].append(entry)
            else:
                result["stale_assets"].append(entry)

            if (
                result["latest_hypothesis_overall"] is None
                or latest_ts > result["latest_hypothesis_overall"]
            ):
                result["latest_hypothesis_overall"] = latest_ts
        except (ValueError, TypeError):
            pass

    if result["healthy_assets"]:
        result["status"] = f"✅ {len(result['healthy_assets'])} asset(s) reflecting"
    elif result["stale_assets"]:
        result["status"] = "⚠️ All hypotheses stale — reflection may be stuck"
    else:
        result["status"] = "❌ No hypotheses found — reflection never ran"

    return result


# ── 6. Feature readiness ──


def feature_readiness(trade_progress: dict, setups: dict, perf: dict) -> list[dict]:
    """Generate feature readiness items."""
    items = []

    # Optimizer
    ready_count = len(trade_progress["optimizer_ready"])
    if ready_count > 0:
        items.append(
            {
                "feature": "Optimizer (BayesianTPE + DE)",
                "status": "✅ READY",
                "detail": f"{ready_count} asset(s) have 200+ trades",
            }
        )
    elif trade_progress["closest_to_200"]:
        c = trade_progress["closest_to_200"]
        items.append(
            {
                "feature": "Optimizer (BayesianTPE + DE)",
                "status": f"⏳ {c['remaining']} trades away",
                "detail": f"{c['asset']} has {c['count']}/{OPTIMIZER_THRESHOLD} trades",
            }
        )
    else:
        items.append(
            {
                "feature": "Optimizer (BayesianTPE + DE)",
                "status": "⏳ Collecting data",
                "detail": "No assets with trades yet",
            }
        )

    # Confidence score tuning
    if setups["total_cycles"] >= 50:
        items.append(
            {
                "feature": "Confidence Score Threshold Tuning",
                "status": "📊 Data available",
                "detail": f"{setups['total_cycles']} cycles recorded. Score distribution: {setups['score_distribution']}",
            }
        )
    else:
        items.append(
            {
                "feature": "Confidence Score Threshold Tuning",
                "status": f"⏳ {setups['total_cycles']}/50 cycles",
                "detail": "Need more skipped-setup data for meaningful tuning",
            }
        )

    # Trend sleeve readiness
    total_trades = trade_progress.get("total_trades", 0)
    if total_trades >= 20:
        items.append(
            {
                "feature": "Trend Sleeve (EMA20/50 4h)",
                "status": "📊 Baseline available",
                "detail": f"{total_trades} total trades provide performance baseline for design decisions",
            }
        )
    elif total_trades >= 5:
        items.append(
            {
                "feature": "Trend Sleeve (EMA20/50 4h)",
                "status": f"⏳ {total_trades}/20 trades",
                "detail": "Need ~20 trades for MR performance baseline before adding second strategy",
            }
        )
    else:
        items.append(
            {
                "feature": "Trend Sleeve (EMA20/50 4h)",
                "status": "⏳ Waiting for MR data",
                "detail": f"Only {total_trades} trades so far",
            }
        )

    return items


# ── MAIN ──


def main():
    print("╔══════════════════════════════════════════╗")
    print("║   Trading System Daily Readiness Report  ║")
    print("╚══════════════════════════════════════════╝")
    print(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print()

    # ── 1. Bot health ──
    health = check_bot_health()
    print(
        f"Bot Status: {health['status']}  (PID: {health['pid'] or 'N/A'}, uptime: {health['uptime'] or 'N/A'})"
    )
    if health["errors_last_100"] > 0:
        print(f"  ⚠️  {health['errors_last_100']} errors in last 100 log lines")
    else:
        print("  ✅ No errors in recent log")
    print()

    # ── 2. Setups analysis ──
    setups = analyze_setups()
    print(f"── Setups Analysis ──")
    print(f"  Total cycles: {setups['total_cycles']}")
    print(f"  Entries: {setups['entries']}")
    print(f"  Skipped (low confidence): {setups['skipped_setups']}")
    print(
        f"  Safety blocks: {dict(setups['safety_blocks']) if setups['safety_blocks'] else '—'}"
    )

    sd = setups["score_distribution"]
    print(f"  Score distribution:")
    print(f"    0–0.2:    {sd['0-0.2']}  (very low)")
    print(f"    0.2–0.4:  {sd['0.2-0.4']}  (low)")
    print(f"    0.4–0.62: {sd['0.4-0.62']}  (near threshold)")
    print(f"    0.62–0.70: {sd['0.62-0.70']}  (half-size zone)")
    print(f"    0.70+:    {sd['0.70-1.0']}  (full-size zone)")

    if setups["avg_score_skip"] > 0:
        print(f"  Avg skipped score: {setups['avg_score_skip']:.3f}")
    if setups["avg_score_half"] > 0:
        print(f"  Avg half-entry score: {setups['avg_score_half']:.3f}")
    if setups["avg_score_full"] > 0:
        print(f"  Avg full-entry score: {setups['avg_score_full']:.3f}")

    near_miss = setups["near_misses_062_065"]
    if near_miss > 2:
        print(
            f"  ⚠️  Recommendation: Consider lowering half-size threshold to 0.60 — {near_miss} near-misses at 0.62-0.65"
        )
    elif near_miss > 0:
        print(f"  ℹ️  {near_miss} near-miss entries at 0.62-0.65 — monitoring")
    else:
        print(f"  ✅ No near-miss tuning pressure")
    print()

    # ── 3. Trade progress ──
    trades = check_trade_progress()
    print(f"── Trade Progress ──")
    print(f"  Total closed trades: {trades['total_trades']}")
    for asset, count in sorted(trades["per_asset"].items()):
        remaining = max(0, OPTIMIZER_THRESHOLD - count)
        bar = (
            "█" * min(count // 10, 20) + "░" * min(remaining // 10, 20)
            if remaining > 0
            else "█" * 20
        )
        print(f"  {asset:12s}: {count:3d} trades  {bar}  ({remaining} to optimizer)")
    print()

    # ── 4. Performance ──
    perf = performance_snapshot()
    print(f"── Performance ──")
    print(f"  Paper balance: ${perf['paper_balance']:.2f}")
    print(f"  Total PnL: {perf['total_pnl_pct']:+.2f}%")
    print(f"  Open positions: {perf['open_positions']}")
    if perf["win_rate"]:
        print(f"  Overall win rate: {perf['win_rate']}%")
    if perf["per_asset_perf"]:
        print(f"  Per-asset performance:")
        for asset, ap in sorted(perf["per_asset_perf"].items()):
            print(
                f"    {asset:12s}: {ap['trades']:3d} trades, WR {ap['win_rate']}%, avg PnL {ap['avg_pnl']:+.2f}%"
            )

    # Benchmark targets
    if perf.get("benchmarks"):
        print(
            f"  Benchmark targets (vs Sharpe={perf['benchmarks'].get(list(perf['benchmarks'])[0], {}).get('sharpe_target', '?')}, DD≤12%, WR≥40%):"
        )
        for asset, bm in sorted(perf["benchmarks"].items()):
            s_ok = "✅" if bm.get("sharpe_met") else "❌"
            w_ok = "✅" if bm.get("win_rate_met") else "❌"
            d_ok = "✅" if bm.get("max_dd_met") else "❌"
            print(
                f"    {asset:12s}: Sharpe {bm.get('sharpe', '?'):<6} {s_ok} "
                f"WR {bm.get('win_rate', '?'):.0%} {w_ok} "
                f"DD {bm.get('max_dd', '?'):<5}% {d_ok}"
            )
    print()

    # ── 5. Reflection health ──
    reflection = check_reflection_health()
    print(f"── Reflection Health ──")
    print(f"  Status: {reflection['status']}")
    print(f"  Assets with hypotheses: {reflection['total_assets_with_hypotheses']}")
    for entry in reflection["healthy_assets"]:
        age_str = ago(entry["latest_timestamp"])
        print(
            f"  ✅ {entry['asset']:12s}: v{entry['total_hypotheses']} | {entry['latest_action']} | {age_str}"
        )
    for entry in reflection["stale_assets"]:
        print(
            f"  ⚠️  {entry['asset']:12s}: v{entry['total_hypotheses']} | {entry['latest_action']} | {entry['age_hours']}h ago — STALE"
        )

    # ── 5b. Drawdown Autopsy ──
    print()
    print(f"── Drawdown Check ──")
    import sys, importlib

    scripts_path = str(Path("/opt/data/hermes-trading/scripts"))
    if scripts_path not in sys.path:
        sys.path.insert(0, scripts_path)
    from drawdown_autopsy import check_triggers, write_autopsy

    triggers = check_triggers()
    if (
        triggers["daily_loss_triggered"]
        or triggers["weekly_dd_triggered"]
        or triggers["consecutive_losses_triggered"]
    ):
        print(f"  ⚠️  DRAWDOWN TRIGGERED")
        if triggers["daily_loss_triggered"]:
            print(
                f"     Daily loss: {triggers['daily_pnl_pct']:.2f}% (< -1.5% trigger)"
            )
        if triggers["weekly_dd_triggered"]:
            print(
                f"     Weekly drawdown: {triggers['weekly_dd_pct']:.2f}% (< -4% trigger)"
            )
        if triggers["consecutive_losses_triggered"]:
            for a in triggers["triggered_assets"]:
                print(f"     {a['asset']}: {a['trades_in_streak']} consecutive losses")
    else:
        print(
            f"  ✅ No drawdown triggers (daily: {triggers['daily_pnl_pct']:.2f}%, weekly: {triggers['weekly_dd_pct']:.2f}%)"
        )
    print()

    # ── 6. Feature readiness ──
    features = feature_readiness(trades, setups, perf)
    print(f"── Feature Readiness ──")
    for f in features:
        print(f"  {f['feature']:40s}  {f['status']:25s}  {f['detail']}")
    print()

    # ── Summary ──
    print("────────────────────────────────────────────")
    if health["pid"] is None:
        print(
            "⚠️  ACTION REQUIRED: Bot is not running — start with HERMES_TRADING_MODE=paper"
        )
    elif trades["optimizer_ready"]:
        print(f"✅ Optimizer ready for {len(trades['optimizer_ready'])} asset(s)")
    else:
        print("💤 No action items — system running normally.")


if __name__ == "__main__":
    main()
