#!/usr/bin/env python3
"""Export all trading data to CSV for external analysis (NotebookLM, spreadsheets, etc.)

Usage:
    python trade_export.py                     # export to stdout
    python trade_export.py --out-dir ./exports # save to directory
    python trade_export.py --day 10            # milestone label

Output: trades_export_day{N}.csv + dashboard_snapshot_day{N}.json
"""

import argparse
import csv
import glob
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

TRADING_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(TRADING_DIR, "state")
DATA_DIR = os.path.join(TRADING_DIR, "data")
OUT_DIR = os.path.join(TRADING_DIR, "exports")
PAPER_START_FILE = os.path.join(STATE_DIR, "paper_start_date.txt")
DASHBOARD_URL = "http://localhost:8502/api/dashboard"


def get_paper_start() -> str:
    if os.path.exists(PAPER_START_FILE):
        return open(PAPER_START_FILE).read().strip()
    return "unknown"


def load_all_trades() -> list[dict]:
    trades = []
    pattern = os.path.join(STATE_DIR, "*", "trades.jsonl")
    for fname in sorted(glob.glob(pattern)):
        asset = os.path.basename(os.path.dirname(fname))
        with open(fname) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        trade = json.loads(line)
                        trade["asset"] = asset
                        trades.append(trade)
                    except json.JSONDecodeError:
                        pass
    return trades


def trades_to_csv(trades: list[dict]) -> str:
    if not trades:
        return ""
    all_keys: list[str] = []
    seen = set()
    for t in trades:
        for k in t:
            if k not in seen:
                all_keys.append(k)
                seen.add(k)
    priority = ["asset", "signal", "direction", "entry_time", "exit_time",
                "entry_price", "exit_price", "pnl_pct", "net_pnl_pct",
                "exit_reason", "enter_confidence", "regime",
                "position_size_r", "hours_held", "fee_pct", "funding_pct",
                "btc_entry_1h_rsi", "btc_exit_1h_rsi",
                "entry_source", "funding_rate_1h"]
    ordered = [k for k in priority if k in seen]
    ordered += [k for k in all_keys if k not in ordered]
    lines = [",".join(ordered)]
    for t in trades:
        row = []
        for k in ordered:
            v = t.get(k, "")
            if v is None:
                v = ""
            sv = str(v)
            if "," in sv or '"' in sv or "\n" in sv:
                sv = '"' + sv.replace('"', '""') + '"'
            row.append(sv)
        lines.append(",".join(row))
    return "\n".join(lines)


def fetch_dashboard() -> dict:
    try:
        resp = urllib.request.urlopen(DASHBOARD_URL, timeout=10)
        return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def compute_advanced_stats(trades: list[dict]) -> dict:
    stats = {"total_trades": len(trades)}
    by_signal: dict[str, list[float]] = {}
    for t in trades:
        sig = t.get("signal", "unknown")
        pnl = t.get("pnl_pct")
        if pnl is not None:
            by_signal.setdefault(sig, []).append(pnl)
    stats["signals"] = {}
    for sig, pnls in sorted(by_signal.items()):
        wins = sum(1 for p in pnls if p > 0)
        stats["signals"][sig] = {
            "count": len(pnls), "wins": wins, "losses": len(pnls) - wins,
            "win_rate": round(wins / len(pnls) * 100, 1) if pnls else 0,
            "avg_pnl_pct": round(sum(pnls) / len(pnls), 4) if pnls else 0,
            "total_pnl_pct": round(sum(pnls), 4),
        }
    by_exit: dict[str, list[float]] = {}
    for t in trades:
        reason = t.get("exit_reason", "unknown")
        pnl = t.get("pnl_pct")
        if pnl is not None:
            by_exit.setdefault(reason, []).append(pnl)
    stats["exit_reasons"] = {}
    for reason, pnls in sorted(by_exit.items()):
        wins = sum(1 for p in pnls if p > 0)
        stats["exit_reasons"][reason] = {
            "count": len(pnls), "wins": wins, "losses": len(pnls) - wins,
            "win_rate": round(wins / len(pnls) * 100, 1) if pnls else 0,
            "avg_pnl_pct": round(sum(pnls) / len(pnls), 4) if pnls else 0,
        }
    by_asset: dict[str, list[float]] = {}
    for t in trades:
        a = t.get("asset", "unknown")
        pnl = t.get("pnl_pct")
        if pnl is not None:
            by_asset.setdefault(a, []).append(pnl)
    stats["assets"] = {}
    for a, pnls in sorted(by_asset.items()):
        wins = sum(1 for p in pnls if p > 0)
        stats["assets"][a] = {
            "count": len(pnls), "wins": wins, "losses": len(pnls) - wins,
            "win_rate": round(wins / len(pnls) * 100, 1) if pnls else 0,
            "avg_pnl_pct": round(sum(pnls) / len(pnls), 4) if pnls else 0,
        }
    return stats


def save_export(trades: list[dict], day_label: str, out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    csv_path = os.path.join(out_dir, f"trades_export_day{day_label}.csv")
    csv_content = trades_to_csv(trades)
    with open(csv_path, "w") as f:
        f.write(csv_content)
    dash = fetch_dashboard()
    stats = compute_advanced_stats(trades)
    snapshot = {
        "export_time_utc": now,
        "paper_start_date": get_paper_start(),
        "day": int(day_label) if day_label.isdigit() else day_label,
        "dashboard": dash,
        "advanced_stats": stats,
    }
    snapshot_path = os.path.join(out_dir, f"dashboard_snapshot_day{day_label}.json")
    with open(snapshot_path, "w") as f:
        json.dump(snapshot, f, indent=2, default=str)
    summary_lines = [
        f"# Hermes Trading Bot - Day {day_label} Export",
        f"Export time: {now}",
        f"Paper start: {get_paper_start()}",
        "",
        f"Total trades: {len(trades)}",
    ]
    if "dashboard" in snapshot and "error" not in snapshot.get("dashboard", {}):
        d = snapshot["dashboard"]
        summary_lines.extend([
            f"Balance: ${d.get('paperBalance', '?')}",
            f"Total PnL: {d.get('totalPnlPct', '?')}%",
            f"Win rate: {d.get('winRate', '?')}%",
            f"Trades: {d.get('totalTrades', '?')} ({d.get('wins', '?')}W / {d.get('losses', '?')}L)",
            f"Profit factor: {d.get('profitFactor', '?')}",
            f"Max drawdown: {d.get('maxDrawdown', '?')}%",
            f"BTC price: ${d.get('btcPrice', '?')}",
            f"Fear and Greed: {d.get('fearGreedValue', '?')} ({d.get('fearGreedLabel', '?')})",
        ])
    summary_lines.extend(["", "## Per-Signal Breakdown"])
    for sig, s in stats.get("signals", {}).items():
        summary_lines.append(
            f"  {sig}: {s['count']} trades, {s['win_rate']}% WR, "
            f"avg {s['avg_pnl_pct']:+.4f}%, total {s['total_pnl_pct']:+.4f}%"
        )
    summary_lines.extend(["", "## Per-Asset Breakdown"])
    for a, s in stats.get("assets", {}).items():
        summary_lines.append(
            f"  {a}: {s['count']} trades, {s['win_rate']}% WR, "
            f"avg {s['avg_pnl_pct']:+.4f}%"
        )
    summary_path = os.path.join(out_dir, f"summary_day{day_label}.md")
    with open(summary_path, "w") as f:
        f.write("\n".join(summary_lines))
    return {
        "csv": csv_path,
        "snapshot": snapshot_path,
        "summary": summary_path,
        "trade_count": len(trades),
        "csv_size_kb": round(os.path.getsize(csv_path) / 1024, 1),
    }


def main():
    parser = argparse.ArgumentParser(description="Export trading data")
    parser.add_argument("--out-dir", default=OUT_DIR, help="Output directory")
    parser.add_argument("--day", default=None, help="Milestone day label")
    args = parser.parse_args()
    trades = load_all_trades()
    day_label = args.day or datetime.now(timezone.utc).strftime("%Y%m%d")
    result = save_export(trades, day_label, args.out_dir)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
