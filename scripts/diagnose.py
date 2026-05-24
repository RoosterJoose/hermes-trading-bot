#!/usr/bin/env python3
"""
diagnose.py — Trading Performance Diagnostics

Produces:
  1. R-multiple histogram (are winners being cut short?)
  2. Time-of-day PnL heat map (any hours the bot bleeds?)
  3. Exit reason analysis (which exit methods actually work?)
  4. Per-asset summary

Usage:
    uv run python scripts/diagnose.py [--publish]
    uv run python scripts/diagnose.py --asset SOL_USDT
"""

import json
import sys
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / "state"


def load_all_trades(asset_filter: str = None) -> list:
    """Load all trades from all assets (or a single asset)."""
    trades = []
    for asset_dir in STATE_DIR.iterdir():
        if not asset_dir.is_dir():
            continue
        if asset_filter and asset_dir.name != asset_filter:
            continue
        tf = asset_dir / "trades.jsonl"
        if not tf.exists():
            continue
        with open(tf) as f:
            for line in f:
                line = line.strip()
                if line:
                    t = json.loads(line)
                    t["_asset"] = asset_dir.name
                    trades.append(t)
    # Sort by exit_time
    trades.sort(key=lambda t: t.get("exit_time", ""))
    return trades


def compute_rmultiple(trades: list) -> list:
    """Compute R-multiple for each trade.

    1R reference is defined as the average |pnl_pct| of all stop_loss exits.
    R-multiple = trade_pnl / 1R_reference.
    Positive = win, negative = loss. ±1.0 = exactly 1R.
    """
    stop_losses = [t for t in trades if t.get("exit_reason") == "stop_loss"]
    if not stop_losses:
        return trades  # Can't compute R without reference

    ref_1r = abs(sum(t["pnl_pct"] for t in stop_losses)) / len(stop_losses)
    if ref_1r == 0:
        ref_1r = 1.0

    for t in trades:
        t["_rmultiple"] = round(t["pnl_pct"] / ref_1r, 4) if ref_1r else 0.0
        t["_1r_ref"] = round(ref_1r, 4)

    return trades


def build_rmultiple_histogram(trades: list) -> dict:
    """Bucket trades by R-multiple ranges."""
    buckets = defaultdict(lambda: {"count": 0, "total_pnl": 0.0, "pct": 0.0})
    ranges = [
        (-999, -5.0, "≤ -5.0R"),
        (-5.0, -3.0, "-5.0 to -3.0R"),
        (-3.0, -2.0, "-3.0 to -2.0R"),
        (-2.0, -1.0, "-2.0 to -1.0R"),
        (-1.0, -0.5, "-1.0 to -0.5R"),
        (-0.5, 0.0, "-0.5 to 0.0R"),
        (0.0, 0.5, "0.0 to 0.5R"),
        (0.5, 1.0, "0.5 to 1.0R"),
        (1.0, 2.0, "1.0 to 2.0R"),
        (2.0, 3.0, "2.0 to 3.0R"),
        (3.0, 5.0, "3.0 to 5.0R"),
        (5.0, 999, "≥ 5.0R"),
    ]

    for t in trades:
        r = t.get("_rmultiple", 0)
        for lo, hi, label in ranges:
            if lo <= r < hi:
                buckets[label]["count"] += 1
                buckets[label]["total_pnl"] += t["pnl_pct"]
                break

    total = len(trades)
    for b in buckets.values():
        b["pct"] = round(b["count"] / total * 100, 1) if total else 0

    return {
        "buckets": {label: buckets[label] for _, _, label in ranges},
        "total_trades": total,
        "ref_1r_pct": trades[0].get("_1r_ref", 0) if trades else 0,
    }


def build_timeofday_heatmap(trades: list) -> dict:
    """Bucket PnL by hour of day and quarter-hour.

    Heat map: rows=hour (0-23 UTC), cols=quarter (0-3), value=avg PnL %.
    """
    heat = defaultdict(
        lambda: {
            "count": 0,
            "total_pnl": 0.0,
            "avg_pnl": 0.0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
        }
    )

    for t in trades:
        exit_time = t.get("exit_time", "")
        if not exit_time:
            continue
        try:
            dt = datetime.fromisoformat(exit_time)
        except (ValueError, TypeError):
            continue
        hour = dt.hour
        quarter = dt.minute // 15
        key = f"h{hour:02d}:q{quarter}"

        heat[key]["count"] += 1
        heat[key]["total_pnl"] += t["pnl_pct"]
        if t["pnl_pct"] > 0:
            heat[key]["wins"] += 1
        else:
            heat[key]["losses"] += 1

    # Compute averages
    for k, v in heat.items():
        v["avg_pnl"] = round(v["total_pnl"] / v["count"], 4) if v["count"] else 0.0
        v["win_rate"] = round(v["wins"] / v["count"] * 100, 1) if v["count"] else 0.0

    return dict(heat)


def build_exit_reason_analysis(trades: list) -> dict:
    """Analyze performance by exit reason."""
    by_reason = defaultdict(
        lambda: {
            "count": 0,
            "total_pnl": 0.0,
            "avg_pnl": 0.0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "avg_rmultiple": 0.0,
            "total_rmultiple": 0.0,
        }
    )

    for t in trades:
        reason = t.get("exit_reason", "unknown")
        by_reason[reason]["count"] += 1
        by_reason[reason]["total_pnl"] += t["pnl_pct"]
        by_reason[reason]["total_rmultiple"] += t.get("_rmultiple", 0)
        if t["pnl_pct"] > 0:
            by_reason[reason]["wins"] += 1
        else:
            by_reason[reason]["losses"] += 1

    for k, v in by_reason.items():
        v["avg_pnl"] = round(v["total_pnl"] / v["count"], 4) if v["count"] else 0.0
        v["win_rate"] = round(v["wins"] / v["count"] * 100, 1) if v["count"] else 0.0
        v["avg_rmultiple"] = (
            round(v["total_rmultiple"] / v["count"], 3) if v["count"] else 0.0
        )

    return dict(by_reason)


def build_per_asset_summary(trades: list) -> dict:
    """Per-asset performance breakdown."""
    by_asset = defaultdict(
        lambda: {
            "count": 0,
            "total_pnl": 0.0,
            "avg_pnl": 0.0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_r": 0.0,
            "avg_r": 0.0,
        }
    )

    for t in trades:
        asset = t.get("_asset", "?")
        by_asset[asset]["count"] += 1
        by_asset[asset]["total_pnl"] += t["pnl_pct"]
        by_asset[asset]["total_r"] += t.get("_rmultiple", 0)
        if t["pnl_pct"] > 0:
            by_asset[asset]["wins"] += 1
        else:
            by_asset[asset]["losses"] += 1

    for k, v in by_asset.items():
        v["avg_pnl"] = round(v["total_pnl"] / v["count"], 4) if v["count"] else 0.0
        v["win_rate"] = round(v["wins"] / v["count"] * 100, 1) if v["count"] else 0.0
        v["avg_r"] = round(v["total_r"] / v["count"], 3) if v["count"] else 0.0

    return dict(by_asset)


def format_histogram(hist: dict, trades: list = None) -> str:
    """Render R-multiple histogram as text."""
    lines = [f"\n📊 R-Multiple Histogram (1R = {hist['ref_1r_pct']:.2f}%)"]
    lines.append(f"{'=' * 60}")
    lines.append(f"{'Range':<22} {'Count':>6} {'%':>6} {'Total PnL':>10}")
    lines.append("-" * 60)

    buckets = hist["buckets"]
    total = hist["total_trades"]

    for label in buckets:
        b = buckets[label]
        bar = "█" * max(1, int(b["pct"] / 2)) if b["count"] > 0 else ""
        sign = "+" if b["total_pnl"] >= 0 else ""
        lines.append(
            f"  {label:<20} {b['count']:>6} {b['pct']:>5.1f}% {sign}{b['total_pnl']:>+8.2f}%  {bar}"
        )

    lines.append("-" * 60)
    lines.append(f"  {'TOTAL':<20} {total:>6} {'100.0%':>6}")

    # Summary stats — use PnL-based count, not bucket totals
    n_wins = sum(1 for t in (trades or []) if t["pnl_pct"] > 0)
    n_losses = sum(1 for t in (trades or []) if t["pnl_pct"] < 0)
    n_breakeven = len(trades or []) - n_wins - n_losses

    lines.append(f"\n  Symmetry check:")
    lines.append(
        f"    Wins (>0%): {n_wins} ({n_wins / total * 100:.0f}%) | Losses (<0%): {n_losses} ({n_losses / total * 100:.0f}%) | Breakeven: {n_breakeven}"
    )
    if n_wins > n_losses:
        lines.append(f"    ✅ Win-heavy — edge intact")
    elif n_losses > n_wins and n_wins > 0:
        lines.append(f"    ⚠️  Loss-heavy — review exits, tighten gates")
    elif n_losses > 0 and n_wins == 0:
        lines.append(f"    🔴 All losses — system may be broken, consider pause")

    # Left skew detection
    big_losses = sum(
        b["total_pnl"]
        for l, b in buckets.items()
        if (l.startswith("-3.") or l.startswith("-5.") or l.startswith("≤"))
        and b["total_pnl"] < 0
    )
    big_wins = sum(
        b["total_pnl"]
        for l, b in buckets.items()
        if (l.startswith("3.") or l.startswith("5.") or l.startswith("≥"))
        and b["total_pnl"] > 0
    )
    if abs(big_losses) > big_wins and big_losses < 0:
        lines.append(
            f"    ⚠️  Left-skewed: big losses (${abs(big_losses):.1f}) dwarf big wins (${big_wins:.1f})"
        )
        lines.append(
            f"    → Exit logic may be cutting winners too early, letting losers run"
        )
    elif big_wins > abs(big_losses) and big_wins > 0:
        lines.append(
            f"    ✅ Right-skewed: big wins (${big_wins:.1f}) > big losses (${abs(big_losses):.1f})"
        )

    return "\n".join(lines)


def format_heatmap(heat: dict) -> str:
    """Render time-of-day heat map as text table.

    24 rows (hour) × 4 columns (quarter).
    """
    lines = ["\n🕐 Time-of-Day PnL Heat Map (UTC)"]
    lines.append("=" * 80)
    lines.append(
        f"{'Hour':<8} {'Q1(0-15m)':<14} {'Q2(15-30m)':<14} {'Q3(30-45m)':<14} {'Q4(45-60m)':<14}"
    )
    lines.append("-" * 80)

    for hour in range(24):
        row = [f"  h{hour:02d}   "]
        for quarter in range(4):
            key = f"h{hour:02d}:q{quarter}"
            cell = heat.get(key, {})
            if cell.get("count", 0) > 0:
                avg = cell["avg_pnl"]
                wr = cell["win_rate"]
                marker = "🟢" if avg > 0 else "🔴" if avg < 0 else "⚪"
                row.append(f"{marker}{avg:>+6.2f}%/{wr:>4.0f}%  ")
            else:
                row.append(f"{'·':>14}")
        lines.append("".join(row))

    # Find best/worst hours
    enriched = []
    for key, cell in heat.items():
        if cell["count"] >= 2:
            enriched.append((key, cell["avg_pnl"], cell["count"]))
    enriched.sort(key=lambda x: x[1])

    if enriched:
        worst = enriched[0]
        best = enriched[-1]
        lines.append(
            f"\n  🔥 Best slot: {best[0]} avg {best[1]:+.2f}% ({best[2]} trades)"
        )
        lines.append(
            f"  🧊 Worst slot: {worst[0]} avg {worst[1]:+.2f}% ({worst[2]} trades)"
        )
        if worst[1] < -0.5 and worst[2] >= 3:
            lines.append(
                f"  ⚠️  Structural loss detected at {worst[0]} — consider time-based trading pause"
            )

    return "\n".join(lines)


def format_exit_analysis(analysis: dict) -> str:
    """Render exit reason analysis."""
    lines = ["\n🚪 Exit Reason Analysis"]
    lines.append("=" * 60)
    lines.append(f"{'Reason':<22} {'Count':>5} {'Avg PnL':>9} {'WR':>5} {'Avg R':>7}")
    lines.append("-" * 60)

    # Sort by count descending
    sorted_reasons = sorted(analysis.items(), key=lambda x: x[1]["count"], reverse=True)

    for reason, data in sorted_reasons:
        emoji = {"stop_loss": "🔴", "scale_out_tp1": "🟡", "chandelier_exit": "🔵"}.get(
            reason, "⚪"
        )
        sign = "+" if data["avg_pnl"] >= 0 else ""
        lines.append(
            f"  {emoji} {reason:<20} {data['count']:>5} {sign}{data['avg_pnl']:>+8.2f}% {data['win_rate']:>4.0f}% {data['avg_rmultiple']:>+6.2f}R"
        )

    return "\n".join(lines)


def format_asset_summary(summary: dict) -> str:
    """Render per-asset summary."""
    lines = ["\n💼 Per-Asset Breakdown"]
    lines.append("=" * 60)
    lines.append(
        f"{'Asset':<12} {'Trades':>6} {'Total PnL':>10} {'Avg PnL':>9} {'WR':>5} {'Avg R':>7}"
    )
    lines.append("-" * 60)

    sorted_assets = sorted(
        summary.items(), key=lambda x: x[1]["total_pnl"], reverse=True
    )

    for asset, data in sorted_assets:
        sign = "+" if data["total_pnl"] >= 0 else ""
        lines.append(
            f"  {asset:<10} {data['count']:>6} {sign}{data['total_pnl']:>+9.2f}% {sign}{data['avg_pnl']:>+8.2f}% {data['win_rate']:>4.0f}% {data['avg_r']:>+6.2f}R"
        )

    return "\n".join(lines)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Trading performance diagnostics")
    parser.add_argument("--asset", help="Single asset to analyze")
    parser.add_argument(
        "--publish", action="store_true", help="Publish report to here.now"
    )
    args = parser.parse_args()

    # Load
    trades = load_all_trades(args.asset)
    if not trades:
        print("No trades found.")
        return

    print(
        f"📈 Loaded {len(trades)} trades ({len(set(t['_asset'] for t in trades))} assets)"
    )
    print(
        f"   Time range: {trades[0].get('exit_time', '?')[:19]} → {trades[-1].get('exit_time', '?')[:19]}"
    )

    # Compute R-multiple
    trades = compute_rmultiple(trades)
    ref_1r = trades[0].get("_1r_ref", 0)
    print(f"   Reference 1R: {ref_1r:.2f}% (from stop_loss exits)")

    # Build reports
    hist = build_rmultiple_histogram(trades)
    heat = build_timeofday_heatmap(trades)
    exit_analysis = build_exit_reason_analysis(trades)
    asset_summary = build_per_asset_summary(trades)

    # Print
    print(format_histogram(hist, trades))
    print(format_heatmap(heat))
    print(format_exit_analysis(exit_analysis))
    print(format_asset_summary(asset_summary))

    # Overall
    total_pnl = sum(t["pnl_pct"] for t in trades)
    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] < 0]
    wr = len(wins) / len(trades) * 100 if trades else 0
    avg_r = sum(t.get("_rmultiple", 0) for t in trades) / len(trades) if trades else 0

    print(f"\n{'=' * 60}")
    print(f"📋 OVERALL")
    print(f"{'=' * 60}")
    print(f"  Total PnL:        {total_pnl:>+9.2f}%")
    print(f"  Win Rate:         {wr:>9.1f}%")
    print(f"  Avg R-multiple:   {avg_r:>+9.3f}R")
    print(f"  Total Trades:     {len(trades):>9}")
    print(
        f"  Avg Win:          {sum(t['pnl_pct'] for t in wins) / len(wins):>+9.2f}%"
        if wins
        else ""
    )
    print(
        f"  Avg Loss:         {sum(t['pnl_pct'] for t in losses) / len(losses):>+9.2f}%"
        if losses
        else ""
    )
    print(
        f"  Profit Factor:    {abs(sum(t['pnl_pct'] for t in wins) / sum(t['pnl_pct'] for t in losses)):.2f}"
        if losses and sum(t["pnl_pct"] for t in losses) != 0
        else "  Profit Factor:    ∞ (no losses)"
        if not losses
        else ""
    )

    # Key insight
    print(f"\n💡 KEY INSIGHT:")
    if avg_r < 0:
        print(f"  The bot is losing {abs(avg_r):.3f}R per trade on average.")
        print(
            f"  At this rate, it will be at -2R in ~{abs(2 / avg_r):.0f} more trades."
        )
        print(f"  Recommendation: Tighten stops, raise entry threshold, or pause.")
    elif avg_r < 0.2:
        print(f"  The bot is winning but marginally ({avg_r:.3f}R/trade).")
        print(
            f"  Edge exists but is thin — ensure position sizing isn't over-concentrated."
        )
    else:
        print(f"  The bot is winning a healthy {avg_r:.3f}R per trade.")
        print(f"  Edge is intact. Focus on not over-optimizing a working system.")


if __name__ == "__main__":
    main()
