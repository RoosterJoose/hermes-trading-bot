#!/usr/bin/env python3
"""Weekly reflection runner — multi-tier learning from trade-count milestones.

Implements the NotebookLM framework:
  Tier 1 (5 trades, micro-review)   — Rolling WR check, no param change.
  Tier 2 (20 trades, tactic)        — Full fallback reflection, one param change.
  Tier 3 (50 trades, structural)    — Autocorrelation + regime shift detection.

Schedule: weekly on Sunday 13:00 UTC.
The underlying reflect.py fallback engine handles the actual adjustments.
This script decides WHEN each asset needs its learning tiers triggered.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

UV_BIN = "/opt/data/home/.local/bin/uv"
BASE_DIR = Path("/opt/data/hermes-trading")
STATE_DIR = BASE_DIR / "state"

# ── helpers ──


def load_hypotheses(asset_key: str) -> list[dict]:
    path = STATE_DIR / asset_key / "hypotheses.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text().strip().splitlines()
        if line.strip()
    ]


def load_trades(asset_key: str) -> list[dict]:
    path = STATE_DIR / asset_key / "trades.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text().strip().splitlines()
        if line.strip()
    ]


def get_latest_hypothesis_time(hypotheses: list[dict]) -> str:
    return max((h.get("timestamp", "") for h in hypotheses), default="")


def trades_since_hypothesis(trades: list[dict], hyp_time: str) -> list[dict]:
    """Return trades that closed after the last hypothesis timestamp."""
    if not hyp_time:
        return trades  # no hypothesis yet → all trades are new
    return [t for t in trades if t.get("exit_time", "") > hyp_time]


def rolling_win_rate(pnls: list[float], window: int = 5) -> float:
    """Win rate over the last N trades. Returns 0.0 if insufficient data."""
    recent = pnls[-window:]
    if len(recent) < window:
        return 0.0
    wins = sum(1 for p in recent if p > 0)
    return wins / len(recent) * 100


def check_autocorrelation(trades: list[dict]) -> dict:
    """Run Wald-Wolfowitz runs test on the last 50 trades. Returns findings."""
    recent = trades[-50:] if len(trades) >= 50 else trades
    if len(recent) < 10:
        return {"action": "insufficient_data", "count": len(recent)}

    # Sort by exit_time
    sorted_t = sorted(recent, key=lambda t: t.get("exit_time", ""))
    outcomes = [1 if t.get("pnl_pct", 0) > 0 else -1 for t in sorted_t]

    # Count runs
    runs = 1
    for i in range(1, len(outcomes)):
        if outcomes[i] != outcomes[i - 1]:
            runs += 1

    n_wins = sum(1 for o in outcomes if o == 1)
    n_losses = len(outcomes) - n_wins
    n = len(outcomes)

    if n < 2 or n_wins == 0 or n_losses == 0:
        return {"action": "no_variance", "count": n, "runs": runs}

    # Expected runs under independence
    expected_runs = 1.0 + (2.0 * n_wins * n_losses) / n
    numerator = (2.0 * n_wins * n_losses) * (2.0 * n_wins * n_losses - n)
    denominator = (n * n * (n - 1)) if n > 1 else 1
    std_runs = max(1.0, (numerator / denominator) ** 0.5) if denominator > 0 else 1.0
    z_score = (runs - expected_runs) / std_runs if std_runs > 0 else 0.0

    regime_shift = z_score < -1.96  # p < 0.05 one-tailed
    return {
        "action": "regime_shift" if regime_shift else "normal",
        "count": n,
        "runs": runs,
        "expected_runs": round(expected_runs, 1),
        "z_score": round(z_score, 2),
        "regime_shift_detected": regime_shift,
    }


def grade_asset(asset_key: str) -> dict:
    """Grade an asset's learning needs across all three tiers."""
    trades = load_trades(asset_key)
    hypotheses = load_hypotheses(asset_key)

    if not trades:
        return {"asset": asset_key, "action": "skip", "reason": "no trades"}

    hyp_time = get_latest_hypothesis_time(hypotheses)
    new_trades = trades_since_hypothesis(trades, hyp_time)
    new_count = len(new_trades)

    pnls = [t.get("pnl_pct", 0) for t in trades]
    new_pnls = [t.get("pnl_pct", 0) for t in new_trades]

    # Tier 1: Micro-review (5 trades, rolling WR alert)
    tier1_wr = rolling_win_rate(pnls, 5)
    tier1_flag = tier1_wr > 0 and tier1_wr < 40  # 5-trade WR below 40% warning

    # Tier 2: Tactic (20 trades since last hypothesis)
    tier2_ready = new_count >= 20

    # Tier 3: Structural (50 trades total in system)
    total_count = len(trades)
    auto_corr = check_autocorrelation(trades)
    tier3_ready = total_count >= 50 and auto_corr["action"] in (
        "regime_shift",
        "normal",
    )
    tier3_flag = auto_corr["action"] == "regime_shift"

    # ── Decision ──
    # Tier 3 is highest priority — if regime shift, always fire
    # Tier 2 fires if enough data
    # Tier 1 is always reported as a watch flag

    if tier3_flag:
        return {
            "asset": asset_key,
            "action": "reflect_tier3",
            "reason": f"REGIME SHIFT (z={auto_corr['z_score']}) across {auto_corr['count']} trades",
            "tier": 3,
            "new_count": new_count,
            "total_count": total_count,
            "tier1_wr": tier1_wr,
            "tier1_flag": tier1_flag,
            "autocorrelation": auto_corr,
        }

    if tier2_ready:
        return {
            "asset": asset_key,
            "action": "reflect_tier2",
            "reason": f"{new_count} new trades since last hypothesis (tier 2:≥20)",
            "tier": 2,
            "new_count": new_count,
            "total_count": total_count,
            "tier1_wr": tier1_wr,
            "tier1_flag": tier1_flag,
        }

    if tier1_flag:
        return {
            "asset": asset_key,
            "action": "warn_tier1",
            "reason": f"rolling 5-trade WR = {tier1_wr:.0f}% — monitoring",
            "tier": 1,
            "new_count": new_count,
            "total_count": total_count,
            "tier1_wr": tier1_wr,
            "tier1_flag": tier1_flag,
        }

    return {
        "asset": asset_key,
        "action": "skip",
        "reason": f"{new_count} new trades (need 20), total {total_count} (need 50). WR={tier1_wr:.0f}% on last 5.",
        "tier": 0,
        "new_count": new_count,
        "total_count": total_count,
        "tier1_wr": tier1_wr,
        "tier1_flag": tier1_flag,
    }


def run_fallback_reflection(asset_key: str) -> dict:
    """Run the reflect.py fallback engine for one asset."""
    hyp_path = STATE_DIR / asset_key / "hypotheses.jsonl"
    before = sum(1 for _ in open(hyp_path)) if hyp_path.exists() else 0

    try:
        result = subprocess.run(
            [
                UV_BIN,
                "run",
                "python",
                "-m",
                "hermes_trading.reflect",
                "--mode",
                "fallback",
                "--asset",
                asset_key,
            ],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=60,
        )
        after = sum(1 for _ in open(hyp_path)) if hyp_path.exists() else 0
        return {
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "new_hypothesis": after > before,
            "hypotheses_after": after,
        }
    except subprocess.TimeoutExpired:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": "TIMEOUT",
            "new_hypothesis": False,
            "hypotheses_after": before,
        }
    except Exception as e:
        return {
            "returncode": -2,
            "stdout": "",
            "stderr": str(e),
            "new_hypothesis": False,
            "hypotheses_after": before,
        }


def main():
    now = datetime.now(timezone.utc)
    print(f"[{now.strftime('%Y-%m-%d %H:%M UTC')}] 📊 Weekly Reflection")
    print(
        f"    Tier 1 (5 trades) — rolling WR watch | Tier 2 (20) — param adjust | Tier 3 (50) — regime check"
    )
    print()

    assets = sorted(
        [
            d.name
            for d in STATE_DIR.iterdir()
            if d.is_dir() and (d / "trades.jsonl").exists()
        ]
    )

    if not assets:
        print("No assets found — exiting")
        return 0

    # Phase 1: Grade all assets
    grades = [grade_asset(a) for a in assets]

    # Phase 2: Display grades
    emoji = {
        "reflect_tier3": "🔴",
        "reflect_tier2": "⚡",
        "warn_tier1": "⚠️",
        "skip": "⏸️",
    }
    for g in grades:
        print(
            f"  {emoji.get(g['action'], '❓')} {g['asset']:12s}  [{g['action']}] {g['reason']}"
        )
        if g.get("tier1_flag") and g["action"] not in (
            "reflect_tier3",
            "reflect_tier2",
        ):
            print(f"               ⇢ 5-trade WR={g['tier1_wr']:.0f}% — watching")
        if (
            g.get("autocorrelation")
            and g["autocorrelation"]["action"] == "regime_shift"
        ):
            print(
                f"               ⇢ z-score={g['autocorrelation']['z_score']}, runs={g['autocorrelation']['runs']} vs {g['autocorrelation']['expected_runs']} expected"
            )

    # Phase 3: Run Tier 2 & Tier 3 reflections
    reflect_assets = [
        g for g in grades if g["action"] in ("reflect_tier2", "reflect_tier3")
    ]

    if reflect_assets:
        print()
        print(f"  ── Running {len(reflect_assets)} reflection(s) ──")
        for g in reflect_assets:
            r = run_fallback_reflection(g["asset"])
            if r["new_hypothesis"]:
                print(
                    f"    ✅ {g['asset']}: v{r['hypotheses_after']} hypothesis written (tier {g['tier']})"
                )
            elif r["returncode"] != 0:
                print(f"    ❌ {g['asset']}: CRASHED (exit {r['returncode']})")
                for line in r["stderr"].split("\n")[-3:]:
                    if line.strip():
                        print(f"       {line.strip()}")
            else:
                print(f"    ⏸️ {g['asset']}: skipped (guard blocked)")

    # Summary
    tier3_count = sum(1 for g in grades if g["action"] == "reflect_tier3")
    tier2_count = sum(1 for g in grades if g["action"] == "reflect_tier2")
    tier1_count = sum(1 for g in grades if g["action"] == "warn_tier1")
    skip_count = sum(1 for g in grades if g["action"] == "skip")
    errors = sum(
        1
        for g in reflect_assets
        for r in [run_fallback_reflection(g["asset"])]
        if r["returncode"] != 0
    )

    print()
    print(f"  Portfolio: {len(assets)} assets")
    print(
        f"    Tier 3 (regime): {tier3_count}  |  Tier 2 (adjust): {tier2_count}  |  Tier 1 (watch): {tier1_count}  |  Skip: {skip_count}"
    )
    print(f"    Errors: {errors}")

    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
