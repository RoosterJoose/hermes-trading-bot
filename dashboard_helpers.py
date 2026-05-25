#!/usr/bin/env python3
"""
Hermes Trading Dashboard — Data helpers.
Reads from state files directly (more reliable) with optional HTTP fallback.
"""

import json
import os
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from collections import defaultdict

import pandas as pd
import urllib.request

# ── Config ──
API_BASE = os.environ.get("HERMES_API_URL", "http://127.0.0.1:8199")
STATE_DIR = Path(os.environ.get("HERMES_STATE_DIR", "/opt/data/hermes-trading/state"))


# ── File helpers ──

def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return None


def get_pnl(t: dict) -> float:
    """Normalize PnL access: net_pnl_pct with fallback to pnl_pct."""
    pnl = t.get("net_pnl_pct", 0)
    if pnl is None or pnl == 0:
        pnl = t.get("pnl_pct", 0) or 0
    return pnl


def _read_jsonl(path: Path, limit: int = 500) -> list:
    try:
        lines = path.read_text().strip().split("\n")
        records = []
        for line in lines[-limit:]:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return records
    except (FileNotFoundError, PermissionError):
        return []


def _try_api(endpoint: str) -> Optional[dict]:
    """Try HTTP API first, return None on failure."""
    try:
        with urllib.request.urlopen(f"{API_BASE}{endpoint}", timeout=3) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


# ── Primary data functions ──

def get_heartbeat() -> dict:
    """Read heartbeat from file or API."""
    data = _try_api("/status")
    if data and "error" not in data:
        return data
    hb = _read_json(STATE_DIR / "heartbeat.json")
    return hb or {"error": "no heartbeat data"}


def get_health() -> dict:
    """Get health status."""
    data = _try_api("/health")
    if data:
        return data
    hb = get_heartbeat()
    if "error" in hb:
        return {"status": "stale", "error": "no data"}
    ts = hb.get("timestamp", "")
    try:
        dt = datetime.fromisoformat(ts)
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        return {
            "status": "ok" if age < 300 else "stale",
            "mode": hb.get("mode", "unknown"),
            "uptime_seconds": age,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except (ValueError, TypeError):
        return {"status": "stale", "error": "bad timestamp"}


def get_readiness() -> dict:
    """Get readiness assessment from API or file."""
    data = _try_api("/readiness")
    if data and "error" not in data:
        return data
    # Compute from heartbeat + trades
    hb = get_heartbeat()
    if "error" in hb:
        return {"error": "no data"}
    return _compute_readiness(hb)


def _compute_readiness(hb: dict) -> dict:
    """Compute readiness locally from heartbeat + trade data."""
    required_days = 30
    min_trades = 50
    max_drawdown_limit = 10.0
    min_sharpe = 0.8
    min_uptime_hours = 24 * 7
    max_stop_loss_ratio = 0.40
    min_closed_trades_for_sharpe = 10

    paper_start = hb.get("paper_start_date", "")
    paper_days = 0
    if paper_start:
        try:
            start = datetime.strptime(paper_start, "%Y-%m-%d").date()
            paper_days = (datetime.now(timezone.utc).date() - start).days
        except (ValueError, TypeError):
            pass

    uptime_seconds = 0
    stale_heartbeat = True
    ts = hb.get("timestamp", "")
    if ts:
        try:
            uptime_seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
            stale_heartbeat = uptime_seconds > 300
        except (ValueError, TypeError):
            pass

    all_trades = get_all_trades()
    closed_trades = [t for t in all_trades if t.get("exit_reason")]
    total_trades = len(closed_trades)

    realized_sharpe = 0.0
    sharpe_insufficient_data = True
    daily_returns = {}
    for t in closed_trades:
        exit_time = t.get("exit_time", "")
        if not exit_time:
            continue
        day = exit_time[:10]
        net_pnl = t.get("net_pnl_pct", 0.0) or 0.0
        if net_pnl == 0:
            net_pnl = t.get("pnl_pct", 0.0) or 0.0
        daily_returns[day] = daily_returns.get(day, 0.0) + net_pnl

    daily_ret_values = list(daily_returns.values())
    if len(daily_ret_values) >= 5:
        import numpy as np
        mean_daily = np.mean(daily_ret_values)
        std_daily = np.std(daily_ret_values, ddof=1)
        if std_daily > 0 and total_trades >= min_closed_trades_for_sharpe:
            sharpe_insufficient_data = False
            realized_sharpe = round((mean_daily / std_daily) * np.sqrt(365), 2)

    # Compute drawdown from trades (heartbeat doesn't provide it)
    dd = _compute_drawdown_from_trades()
    max_dd_pct = dd.get("highest_dd_pct", 0.0) or 0.0
    current_dd_pct = dd.get("current_dd_pct", 0.0) or 0.0
    dd_ok = abs(max_dd_pct) <= max_drawdown_limit

    stop_loss_count = sum(1 for t in closed_trades if t.get("exit_reason") == "stop_loss")
    stop_loss_ratio = stop_loss_count / total_trades if total_trades > 0 else 0
    time_exit_count = sum(1 for t in closed_trades if t.get("exit_reason") in ("time_exit",))
    extreme_losses = sum(1 for t in closed_trades if (t.get("net_pnl_pct", 0) or 0) < -3.0)

    data_ok = bool(hb) and not stale_heartbeat

    days_met = paper_days >= required_days
    trades_met = total_trades >= min_trades
    sharpe_met = not sharpe_insufficient_data and realized_sharpe >= min_sharpe
    uptime_met = uptime_seconds >= min_uptime_hours * 3600
    stop_loss_ok = stop_loss_ratio <= max_stop_loss_ratio
    extremes_ok = extreme_losses == 0

    blockers = []
    if not days_met:
        blockers.append(f"Paper validation period incomplete ({paper_days}/{required_days} days)")
    if not trades_met:
        blockers.append(f"Trade sample too small ({total_trades}/{min_trades} trades)")
    if sharpe_insufficient_data:
        blocker = f"sharpe: insufficient data" if total_trades < min_closed_trades_for_sharpe else "sharpe: insufficient daily data"
        blockers.append(f"Realized Sharpe insufficient ({blocker})")
    elif not sharpe_met:
        blockers.append(f"Realized Sharpe below threshold ({realized_sharpe:.2f} < {min_sharpe})")
    if not dd_ok:
        blockers.append(f"Max drawdown ({max_dd_pct:.1f}%) exceeds limit ({max_drawdown_limit:.0f}%)")
    if not uptime_met:
        blockers.append(f"Uptime insufficient ({uptime_seconds/3600:.0f}h < {min_uptime_hours}h)")
    if not stop_loss_ok:
        blockers.append(f"Stop-loss ratio too high ({stop_loss_ratio:.0%} > {max_stop_loss_ratio:.0%})")
    if not extremes_ok:
        blockers.append(f"Extreme losses detected ({extreme_losses})")
    if stale_heartbeat:
        blockers.append("Stale heartbeat — no recent data (>5 min)")

    live_ready = all([days_met, trades_met, sharpe_met, dd_ok, uptime_met, stop_loss_ok, extremes_ok, not stale_heartbeat])

    return {
        "paper_days_elapsed": paper_days,
        "required_paper_days": required_days,
        "paper_days_met": days_met,
        "total_trades": total_trades,
        "min_trade_count": min_trades,
        "min_trade_count_met": trades_met,
        "realized_sharpe": realized_sharpe if not sharpe_insufficient_data else None,
        "daily_return_days": len(daily_ret_values),
        "min_sharpe": min_sharpe,
        "sharpe_met": sharpe_met,
        "sharpe_insufficient_data": sharpe_insufficient_data,
        "max_drawdown_pct": round(abs(max_dd_pct), 2),
        "current_drawdown_pct": round(abs(current_dd_pct), 2),
        "max_drawdown_limit": max_drawdown_limit,
        "max_drawdown_ok": dd_ok,
        "uptime_seconds": int(uptime_seconds),
        "uptime_hours": round(uptime_seconds / 3600, 1),
        "min_uptime_hours": min_uptime_hours,
        "uptime_met": uptime_met,
        "stale_heartbeat": stale_heartbeat,
        "stop_loss_ratio": round(stop_loss_ratio, 3),
        "stop_loss_ratio_limit": max_stop_loss_ratio,
        "stop_loss_ok": stop_loss_ok,
        "stop_loss_exits": stop_loss_count,
        "time_exits": time_exit_count,
        "extreme_losses": extreme_losses,
        "extremes_ok": extremes_ok,
        "data_integrity_ok": data_ok,
        "live_ready": live_ready,
        "blockers": blockers,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def get_all_trades() -> list:
    """Read trades from all asset dirs, sorted by exit_time desc."""
    if not STATE_DIR.exists():
        return []
    trades = []
    for asset_dir in sorted(STATE_DIR.iterdir()):
        if not asset_dir.is_dir():
            continue
        tf = asset_dir / "trades.jsonl"
        if tf.exists():
            trades.extend(_read_jsonl(tf, limit=200))
    trades.sort(key=lambda t: t.get("exit_time", ""), reverse=True)
    return trades


def get_positions() -> dict:
    """Get open positions with enriched data."""
    hb = get_heartbeat()
    if "error" in hb:
        return {"positions": {}, "trend_positions": {}}
    return {
        "positions": hb.get("positions", {}),
        "trend_positions": hb.get("trend_positions", {}),
        "total_open": sum(1 for v in hb.get("positions", {}).values() if v is not None)
                       + len(hb.get("trend_positions", {})),
    }


def get_activity_events(limit: int = 100) -> list:
    """Build chronological event stream from setups_log + trades."""
    events = []

    # From setups_log
    if STATE_DIR.exists():
        for asset_dir in sorted(STATE_DIR.iterdir()):
            if not asset_dir.is_dir():
                continue
            sf = asset_dir / "setups_log.jsonl"
            if sf.exists():
                for rec in _read_jsonl(sf, limit=100):
                    ts = rec.get("timestamp", "")
                    decision = rec.get("decision", "")
                    reason = rec.get("reason", "")
                    rsi = rec.get("rsi")
                    price = rec.get("price")
                    asset = rec.get("asset", asset_dir.name)

                    if decision == "entered":
                        etype = "ENTRY"
                    elif "kill" in (reason or "").lower():
                        etype = "RISK_BLOCK"
                    elif "skip" in (reason or "").lower():
                        etype = "SKIP"
                    else:
                        etype = "SIGNAL"

                    events.append({
                        "timestamp": ts,
                        "type": etype,
                        "asset": asset.replace("_USDT", ""),
                        "message": reason or "",
                        "details": {"rsi": rsi, "price": price,
                                    "confidence": rec.get("confidence_score"),
                                    "components": rec.get("confidence_components")},
                    })

    # From trades (closed)
    trades = get_all_trades()
    for t in trades[:50]:
        ts = t.get("exit_time", t.get("entry_time", ""))
        asset = t.get("asset", "?").replace("_USDT", "")
        exit_reason = t.get("exit_reason", "exit")
        pnl = t.get("net_pnl_pct", t.get("pnl_pct", 0))

        if exit_reason == "stop_loss":
            etype = "EXIT"
            icon = "🛑"
        elif "tp" in (exit_reason or "").lower():
            etype = "EXIT"
        elif "chandelier" in (exit_reason or ""):
            etype = "EXIT"
        elif "time" in (exit_reason or ""):
            etype = "EXIT"
        else:
            etype = "EXIT"

        msg = f"{exit_reason}: {pnl:+.2f}%"
        if t.get("partial"):
            msg = f"SCALE_OUT: {pnl:+.2f}%"

        events.append({
            "timestamp": ts,
            "type": etype,
            "asset": asset,
            "message": msg,
            "details": {"pnl": pnl, "exit_reason": exit_reason,
                        "hours_held": t.get("hours_held"),
                        "entry_price": t.get("entry_price"),
                        "exit_price": t.get("exit_price")},
        })

    # System events from heartbeat
    hb = get_heartbeat()
    if "error" not in hb:
        events.append({
            "timestamp": hb.get("timestamp", ""),
            "type": "SYSTEM",
            "asset": "",
            "message": f"Heartbeat: mode={hb.get('mode','?')}, balance=${hb.get('paper_balance',0):.2f}",
            "details": {},
        })

    # Sort by timestamp descending, deduplicate by type+timestamp+asset
    events.sort(key=lambda e: e.get("timestamp", ""), reverse=True)

    # Deduplicate
    seen = set()
    unique = []
    for e in events:
        key = (e["type"], e["timestamp"][:19] if e["timestamp"] else "", e["asset"], e["message"][:30])
        if key not in seen:
            seen.add(key)
            unique.append(e)
            if len(unique) >= limit:
                break

    return unique


def get_equity_curve() -> pd.DataFrame:
    """Build equity curve from all trades."""
    all_trades = get_all_trades()
    closed = [t for t in all_trades if t.get("exit_time")]
    closed.sort(key=lambda t: t.get("exit_time", ""))

    if not closed:
        return pd.DataFrame()

    hb = get_heartbeat()
    initial = 1000.0  # default
    if "error" not in hb:
        # Try to get initial from total_pnl_pct + current balance
        balance = hb.get("paper_balance", 0)
        total_pnl = hb.get("total_pnl_pct", 0) or 0
        if balance > 0 and total_pnl != 0:
            initial = balance / (1 + total_pnl / 100)
        else:
            initial = balance

    points = [{"time": closed[0]["exit_time"] if closed else "", "equity": initial, "return": 0}]
    running = initial
    for t in closed:
        net = t.get("net_pnl_pct", 0) or 0
        if net == 0:
            net = t.get("pnl_pct", 0) or 0
        running *= (1 + net / 100)
        points.append({
            "time": t.get("exit_time", ""),
            "equity": round(running, 2),
            "return": net,
        })

    if not points:
        return pd.DataFrame()
    df = pd.DataFrame(points)
    df["time"] = pd.to_datetime(df["time"])
    return df


def get_gate_attribution() -> list:
    """Analyze setups_log to determine which gates block trades most often."""
    gate_counts = defaultdict(int)
    total_skips = 0

    if STATE_DIR.exists():
        for asset_dir in sorted(STATE_DIR.iterdir()):
            if not asset_dir.is_dir():
                continue
            sf = asset_dir / "setups_log.jsonl"
            if sf.exists():
                for rec in _read_jsonl(sf, limit=200):
                    decision = rec.get("decision", "")
                    reason = rec.get("reason", "")
                    if decision == "skipped" or (reason and "skip" in reason.lower()):
                        total_skips += 1
                        # Categorize the block reason
                        r = (reason or "").lower()
                        if "volume" in r or "vol <" in r:
                            gate_counts["Volume Filter"] += 1
                        elif "consecutive" in r or "max_consecutive" in r:
                            gate_counts["Consecutive Losses"] += 1
                        elif "hurst" in r:
                            gate_counts["Hurst Regime"] += 1
                        elif "btc" in r and "vol" in r:
                            gate_counts["BTC Vol Surge"] += 1
                        elif "adx" in r:
                            gate_counts["ADX Danger Zone"] += 1
                        elif "correlation" in r:
                            gate_counts["Correlation Cap"] += 1
                        elif "confidence" in r or "low_confidence" in r:
                            gate_counts["Low Confidence"] += 1
                        elif "open" in r and "max" in r:
                            gate_counts["Max Positions"] += 1
                        elif "btc" in r:
                            gate_counts["BTC Gate"] += 1
                        elif "fear" in r or "greed" in r or "fng" in r:
                            gate_counts["Fear & Greed"] += 1
                        elif "cooldown" in r:
                            gate_counts["Cooldown"] += 1
                        elif "eval" in r or "cascade" in r or "lower_low" in r:
                            gate_counts["Entry Evaluator"] += 1
                        elif "regime" in r:
                            gate_counts["Regime Block"] += 1
                        elif "zscore" in r or "z-score" in r:
                            gate_counts["Z-Score Cooling"] += 1
                        elif "skip" in reason:
                            # Generic skip — count as "Other Gate"
                            # But skip "low_confidence" which we already handle
                            if "low_confidence" not in r:
                                gate_counts["Other Gate"] += 1

    total = sum(gate_counts.values())
    if total == 0:
        return [{"gate": "No block data yet", "count": 0, "pct": 0}]

    result = []
    for gate, count in sorted(gate_counts.items(), key=lambda x: -x[1]):
        result.append({
            "gate": gate,
            "count": count,
            "pct": round(count / total * 100),
        })
    return result


def get_leaderboard() -> pd.DataFrame:
    """Per-asset performance summary."""
    trades = get_all_trades()
    closed = [t for t in trades if t.get("exit_time")]

    if not closed:
        return pd.DataFrame()

    hb = get_heartbeat()
    open_positions = hb.get("positions", {}) if "error" not in hb else {}

    rows = []
    assets = sorted(set(t.get("asset", "?") for t in closed))
    for asset in assets:
        at = [t for t in closed if t.get("asset") == asset]
        if not at:
            continue

        total_pnl = sum(get_pnl(t) for t in at)
        wins = [t for t in at if get_pnl(t) > 0]
        losses = [t for t in at if get_pnl(t) <= 0]
        win_rate = len(wins) / len(at) * 100 if at else 0

        gw = sum(get_pnl(t) for t in wins)
        gl = abs(sum(get_pnl(t) for t in losses))
        pf = round(gw / gl, 2) if gl > 0 else (999 if gw > 0 else 0)

        # Max drawdown per asset from trade pnl
        cum = 0
        peak = 0
        max_dd = 0
        for t in at:
            cum += get_pnl(t)
            peak = max(peak, cum)
            dd = peak - cum
            max_dd = max(max_dd, dd)

        is_active = asset in open_positions and open_positions[asset] is not None
        avg_hold = sum(t.get("hours_held", 0) or 0 for t in at) / len(at) if at else 0

        rows.append({
            "Asset": asset.replace("_USDT", ""),
            "Trades": len(at),
            "Net PnL": f"{total_pnl:+.2f}%",
            "Win Rate": f"{win_rate:.1f}%",
            "PF": pf,
            "Max DD": f"{max_dd:.1f}%",
            "Avg Hold": f"{avg_hold:.1f}h",
            "Status": "ACTIVE" if is_active else "STANDBY",
        })

    return pd.DataFrame(rows)


def get_performance_metrics() -> dict:
    """Aggregate performance stats across all trades."""
    trades = get_all_trades()
    hb = get_heartbeat() if "error" not in get_heartbeat() else {}

    closed = [t for t in trades if t.get("exit_time") and t.get("exit_reason")]
    if not closed:
        return {}

    wins = [t for t in closed if get_pnl(t) > 0]
    losses = [t for t in closed if get_pnl(t) <= 0]
    total_pnl = sum(get_pnl(t) for t in closed)
    win_rate = len(wins) / len(closed) * 100 if closed else 0
    gw = sum(get_pnl(t) for t in wins)
    gl = abs(sum(get_pnl(t) for t in losses))
    pf = round(gw / gl, 2) if gl > 0 else (999 if gw > 0 else 0)

    # Expectancy (average return per trade)
    expectancy = round(total_pnl / len(closed), 2) if closed else 0

    # Fees & funding
    total_fees = sum(t.get("fee_pct", 0) or 0 for t in closed)
    total_funding = sum(t.get("funding_pct", 0) or 0 for t in closed)
    gross_pnl = sum(t.get("pnl_pct", 0) or 0 for t in closed)

    # Avg hold
    avg_hold = sum(t.get("hours_held", 0) or 0 for t in closed) / len(closed) if closed else 0

    balance = hb.get("paper_balance", 0) if hb else 0
    total_pnl_pct = hb.get("total_pnl_pct", 0) if hb else 0

    return {
        "balance": balance,
        "total_pnl_pct": total_pnl_pct,
        "gross_pnl": round(gross_pnl, 2),
        "net_pnl": round(total_pnl, 2),
        "fees": round(total_fees, 2),
        "funding": round(total_funding, 2),
        "win_rate": round(win_rate, 1),
        "profit_factor": pf,
        "expectancy": expectancy,
        "total_trades": len(closed),
        "avg_hold_hours": round(avg_hold, 1),
        "wins": len(wins),
        "losses": len(losses),
    }


def get_skip_analysis() -> pd.DataFrame:
    """Analyze skipped setups — how often each skip reason occurs."""
    skip_reasons = defaultdict(int)
    skip_by_asset = defaultdict(lambda: defaultdict(int))

    if STATE_DIR.exists():
        for asset_dir in sorted(STATE_DIR.iterdir()):
            if not asset_dir.is_dir():
                continue
            sf = asset_dir / "setups_log.jsonl"
            if sf.exists():
                for rec in _read_jsonl(sf, limit=300):
                    if rec.get("decision") == "skipped" or "skip" in (rec.get("reason", "") or "").lower():
                        reason = rec.get("reason", "unknown")
                        # Normalize reason
                        r = reason.lower()
                        if "low_confidence" in r:
                            cat = "Low Confidence"
                        elif "kill" in r:
                            cat = "Kill Switch"
                        elif "btc" in r or "fear" in r or "fng" in r:
                            cat = "Market Gate"
                        elif "adx" in r:
                            cat = "ADX Danger Zone"
                        elif "correlation" in r:
                            cat = "Correlation Cap"
                        elif "cooldown" in r:
                            cat = "Cooldown"
                        elif "open" in r and "max" in r:
                            cat = "Max Positions"
                        elif "eval" in r or "cascade" in r or "lower_low" in r:
                            cat = "Entry Evaluator"
                        elif "volume" in r:
                            cat = "Volume Filter"
                        elif "hurst" in r:
                            cat = "Hurst Regime"
                        elif "regime" in r:
                            cat = "Regime Block"
                        elif "zscore" in r:
                            cat = "Z-Score Cooling"
                        elif "not below" in r or "threshold" in r:
                            cat = "RSI Not Below Threshold"
                        else:
                            cat = "Other"

                        asset = asset_dir.name.replace("_USDT", "")
                        skip_reasons[cat] += 1
                        skip_by_asset[asset][cat] += 1

    if not skip_reasons:
        return pd.DataFrame()

    rows = []
    for reason, count in sorted(skip_reasons.items(), key=lambda x: -x[1]):
        rows.append({"Reason": reason, "Count": count, "%": round(count / sum(skip_reasons.values()) * 100, 1)})
    return pd.DataFrame(rows)


def get_score_distribution() -> dict:
    """Get confidence score distribution from setups_log."""
    scores = []
    if STATE_DIR.exists():
        for asset_dir in sorted(STATE_DIR.iterdir()):
            if not asset_dir.is_dir():
                continue
            sf = asset_dir / "setups_log.jsonl"
            if sf.exists():
                for rec in _read_jsonl(sf, limit=300):
                    cs = rec.get("confidence_score")
                    if cs is not None:
                        scores.append(cs)
    return {"scores": scores, "count": len(scores)}


def get_market_context() -> dict:
    """Extract market context from heartbeat."""
    hb = get_heartbeat()
    if "error" in hb:
        return {}

    # Compute drawdown from trades since heartbeat doesn't provide it
    dd = _compute_drawdown_from_trades()

    return {
        "btc": hb.get("btc_context", {}),
        "fear_greed": hb.get("fear_greed", {}),
        "trust_state": hb.get("trust_state", {}),
        "optimizer": hb.get("optimizer", {}),
        "monte_carlo": hb.get("monte_carlo", {}),
        "max_drawdown": dd,
    }


def _compute_drawdown_from_trades() -> dict:
    """Compute max drawdown and current drawdown from trade data."""
    all_trades = get_all_trades()
    closed = [t for t in all_trades if t.get("exit_time")]
    closed.sort(key=lambda t: t.get("exit_time", ""))

    if not closed:
        return {"highest_dd_pct": 0.0, "current_dd_pct": 0.0}

    initial = 1000.0
    peak = initial
    running = initial
    max_dd = 0.0
    max_dd_peak = initial

    for t in closed:
        net = get_pnl(t)
        running *= (1 + net / 100)
        if running > peak:
            peak = running
        dd = (peak - running) / peak * 100
        if dd > max_dd:
            max_dd = dd
            max_dd_peak = peak

    current_dd = (peak - running) / peak * 100 if peak > 0 else 0.0

    return {
        "highest_dd_pct": round(max_dd, 2),
        "current_dd_pct": round(current_dd, 2),
        "peak_equity": round(peak, 2),
        "current_equity": round(running, 2),
        "max_dd_peak": round(max_dd_peak, 2),
    }
