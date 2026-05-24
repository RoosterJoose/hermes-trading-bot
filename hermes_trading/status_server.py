#!/usr/bin/env python3
"""
status_server.py — Lightweight HTTP status server for Hermes Trading Bot.

Serves GET endpoints by reading heartbeat.json and state files.
No shared state with the trading loop — reads from disk on each request.
Stale by at most one cycle (~60s).

Endpoints:
  GET /         → index with links
  GET /status   → full heartbeat JSON (mode, balance, positions, etc.)
  GET /health   → simple {"status": "ok", "uptime_seconds": N}
  GET /positions → open positions summary
  GET /trades   → recent trades across all assets (last 50)
  GET /readiness → go-live readiness assessment
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

from aiohttp import web

DEFAULT_PORT = 8099


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return None


def _get_trades(state_dir: Path, limit: int = 50) -> list:
    """Read last N trades across all assets."""
    trades = []
    assets_dir = state_dir
    if not assets_dir.exists():
        return []
    for asset_dir in sorted(assets_dir.iterdir()):
        if not asset_dir.is_dir():
            continue
        trades_file = asset_dir / "trades.jsonl"
        if not trades_file.exists():
            continue
        try:
            for line in trades_file.read_text().strip().split("\n"):
                line = line.strip()
                if line:
                    try:
                        trades.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass
    # Sort by exit_time descending, return most recent
    trades.sort(key=lambda t: t.get("exit_time", ""), reverse=True)
    return trades[:limit]


def _compute_readiness(state_dir: Path, heartbeat: dict) -> dict:
    """Compute go-live readiness from heartbeat + trade data.

    Uses daily equity-curve Sharpe, true peak-to-trough max drawdown,
    and multiple gate conditions for a strict readiness assessment.
    """
    required_days = 30
    min_trades = 50
    max_drawdown_limit = 10.0
    min_sharpe = 0.8
    min_uptime_hours = 24 * 7  # 7 days minimum uptime
    max_stop_loss_ratio = 0.40  # max 40% of closes via stop loss
    min_closed_trades_for_sharpe = 10

    # ── Paper days ──
    paper_start = heartbeat.get("paper_start_date")
    paper_days = 0
    if paper_start:
        try:
            start = datetime.strptime(paper_start, "%Y-%m-%d").date()
            paper_days = (datetime.now(timezone.utc).date() - start).days
        except (ValueError, TypeError):
            pass

    # ── Uptime from heartbeat age ──
    uptime_seconds = 0
    hb_ts = heartbeat.get("timestamp")
    stale_heartbeat = True
    if hb_ts:
        try:
            uptime_seconds = (
                datetime.now(timezone.utc) - datetime.fromisoformat(hb_ts)
            ).total_seconds()
            stale_heartbeat = uptime_seconds > 300  # >5min stale
        except (ValueError, TypeError):
            pass

    # ── Collect all trades ──
    all_trades = _get_trades(state_dir, limit=10000)
    closed_trades = [t for t in all_trades if t.get("exit_reason")]
    total_trades = len(closed_trades)

    # ── Daily equity-curve Sharpe ──
    realized_sharpe = 0.0
    sharpe_insufficient_data = True
    daily_returns = {}
    for t in closed_trades:
        exit_time = t.get("exit_time", "")
        if not exit_time:
            continue
        try:
            day = exit_time[:10]  # "2026-05-24"
        except Exception:
            continue
        net_pnl = t.get("net_pnl_pct", 0.0)
        if "net_pnl_pct" in t:
            daily_returns[day] = daily_returns.get(day, 0.0) + net_pnl

    daily_ret_values = list(daily_returns.values())
    if len(daily_ret_values) >= 5:
        mean_daily = np.mean(daily_ret_values)
        std_daily = np.std(daily_ret_values, ddof=1)
        if std_daily > 0 and total_trades >= min_closed_trades_for_sharpe:
            sharpe_insufficient_data = False
            # Annualize: daily Sharpe * sqrt(365)
            annualized_sharpe = (mean_daily / std_daily) * np.sqrt(365)
            realized_sharpe = round(annualized_sharpe, 2)

    # ── True max drawdown from portfolio tracker ──
    max_dd_pct = heartbeat.get("max_drawdown", {}).get("highest_dd_pct", 0.0)
    current_dd_pct = heartbeat.get("max_drawdown", {}).get("current_dd_pct", 0.0)
    mc_dd = heartbeat.get("monte_carlo", {}).get("dd_95_pct")
    dd_limit = abs(mc_dd) if mc_dd else max_drawdown_limit
    # Use max drawdown (not current total PnL) as the real risk metric
    dd_ok = max_dd_pct <= dd_limit

    # ── Stop-loss exits ──
    stop_loss_count = sum(
        1 for t in closed_trades if t.get("exit_reason") == "stop_loss"
    )
    stop_loss_ratio = stop_loss_count / total_trades if total_trades > 0 else 0

    time_exit_count = sum(
        1 for t in closed_trades if t.get("exit_reason") in ("time_exit",)
    )

    # Extreme loss: account-level net PnL worse than -3% (unusual for 0.5-1% sizing)
    extreme_losses = sum(
        1 for t in closed_trades if t.get("net_pnl_pct", 0) < -3.0
    )

    # ── Data integrity checks ──
    data_ok = heartbeat is not None
    all_assets_have_strategy = True  # checked at startup, assume ok if running
    no_data_gaps = not stale_heartbeat

    # ── Gate evaluation ──
    days_met = paper_days >= required_days
    trades_met = total_trades >= min_trades
    sharpe_met = not sharpe_insufficient_data and realized_sharpe >= min_sharpe
    uptime_met = uptime_seconds >= min_uptime_hours * 3600
    stop_loss_ok = stop_loss_ratio <= max_stop_loss_ratio
    extremes_ok = extreme_losses == 0
    data_ok_gate = data_ok and all_assets_have_strategy and no_data_gaps

    blockers = []
    if not days_met:
        blockers.append(f"paper_days: {paper_days}/{required_days}")
    if not trades_met:
        blockers.append(f"trade_count: {total_trades}/{min_trades}")
    if sharpe_insufficient_data:
        if total_trades < min_closed_trades_for_sharpe:
            blockers.append(
                f"sharpe: insufficient trades ({total_trades} closed, need {min_closed_trades_for_sharpe})"
            )
        else:
            blockers.append("sharpe: insufficient daily data (< 5 days with trades)")
    elif not sharpe_met:
        blockers.append(
            f"sharpe: {realized_sharpe:.2f} < {min_sharpe} (daily Sharpe)"
        )
    if not dd_ok:
        blockers.append(f"max_drawdown: {max_dd_pct:.1f}% > {dd_limit:.0f}% limit")
    if not uptime_met:
        uptime_hours = uptime_seconds / 3600
        blockers.append(
            f"uptime: {uptime_hours:.0f}h < {min_uptime_hours}h minimum"
        )
    if not stop_loss_ok:
        blockers.append(
            f"stop_loss_ratio: {stop_loss_ratio:.0%} > {max_stop_loss_ratio:.0%} max"
        )
    if not extremes_ok:
        blockers.append(f"extreme_losses: {extreme_losses} (> 0 not allowed)")
    if not data_ok_gate:
        if stale_heartbeat:
            blockers.append("stale_heartbeat: no recent data (>5min)")
        if not data_ok:
            blockers.append("no_heartbeat_data")
        if not all_assets_have_strategy:
            blockers.append("missing_strategy_config")

    live_ready = (
        days_met
        and trades_met
        and sharpe_met
        and dd_ok
        and uptime_met
        and stop_loss_ok
        and extremes_ok
        and data_ok_gate
    )

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
        "max_drawdown_pct": round(max_dd_pct, 2),
        "current_drawdown_pct": round(current_dd_pct, 2),
        "max_drawdown_limit": dd_limit,
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
        "data_integrity_ok": data_ok_gate,
        "live_ready": live_ready,
        "blockers": blockers,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _build_uptime(heartbeat: dict) -> Optional[float]:
    """Compute uptime in seconds from heartbeat timestamp."""
    ts = heartbeat.get("timestamp")
    if not ts:
        return None
    try:
        hb_time = datetime.fromisoformat(ts)
        return (datetime.now(timezone.utc) - hb_time).total_seconds()
    except (ValueError, TypeError):
        return None


def _find_state_dir() -> Path:
    """Find state dir relative to this file."""
    base = Path(os.path.dirname(os.path.abspath(__file__))).parent
    return base / "state"


async def handle_index(request: web.Request) -> web.Response:
    state_dir = request.app["state_dir"]
    hb = _read_json(state_dir / "heartbeat.json")
    live_ready = _compute_readiness(state_dir, hb) if hb else {}
    return web.json_response(
        {
            "service": "hermes-trading",
            "endpoints": ["/status", "/health", "/positions", "/trades", "/readiness"],
            "live_ready": live_ready.get("live_ready", False) if live_ready else False,
        }
    )


async def handle_status(request: web.Request) -> web.Response:
    state_dir = request.app["state_dir"]
    hb = _read_json(state_dir / "heartbeat.json")
    if hb is None:
        return web.json_response({"error": "no heartbeat data"}, status=503)
    return web.json_response(hb)


async def handle_health(request: web.Request) -> web.Response:
    state_dir = request.app["state_dir"]
    hb = _read_json(state_dir / "heartbeat.json")
    uptime = _build_uptime(hb) if hb else None
    mode = hb.get("mode", "unknown") if hb else "unknown"
    return web.json_response(
        {
            "status": "ok" if hb else "stale",
            "mode": mode,
            "uptime_seconds": uptime,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


async def handle_positions(request: web.Request) -> web.Response:
    state_dir = request.app["state_dir"]
    hb = _read_json(state_dir / "heartbeat.json")
    if hb is None:
        return web.json_response({"error": "no heartbeat data"}, status=503)
    return web.json_response(
        {
            "positions": hb.get("positions", {}),
            "trend_positions": hb.get("trend_positions", {}),
            "total_open": sum(
                1 for v in hb.get("positions", {}).values() if v is not None
            ),
        }
    )


async def handle_trades(request: web.Request) -> web.Response:
    state_dir = request.app["state_dir"]
    limit = int(request.query.get("limit", 50))
    trades = _get_trades(state_dir, limit=min(limit, 200))
    return web.json_response(
        {
            "count": len(trades),
            "trades": trades,
        }
    )


async def handle_readiness(request: web.Request) -> web.Response:
    state_dir = request.app["state_dir"]
    hb = _read_json(state_dir / "heartbeat.json")
    if hb is None:
        return web.json_response({"error": "no heartbeat data"}, status=503)
    readiness = _compute_readiness(state_dir, hb)
    return web.json_response(readiness)


def create_app(state_dir: Optional[Path] = None) -> web.Application:
    """Create the aiohttp web app.

    Args:
        state_dir: Path to state directory. If None, auto-detect.
    """
    if state_dir is None:
        state_dir = _find_state_dir()

    app = web.Application()
    app["state_dir"] = Path(state_dir)

    app.router.add_get("/", handle_index)
    app.router.add_get("/status", handle_status)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/positions", handle_positions)
    app.router.add_get("/trades", handle_trades)
    app.router.add_get("/readiness", handle_readiness)

    return app


async def run_status_server(
    state_dir: Path, port: int = DEFAULT_PORT, shutdown_event: Optional[asyncio.Event] = None
):
    """Run the status server as an asyncio task.

    Binds to 127.0.0.1 by default. Set HERMES_STATUS_BIND=0.0.0.0
    environment variable to expose to the network.

    Args:
        state_dir: Path to state directory.
        port: HTTP port (default 8099).
        shutdown_event: Optional event to signal graceful shutdown.
    """
    import os

    bind_host = os.environ.get("HERMES_STATUS_BIND", "127.0.0.1")
    app = create_app(state_dir)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, bind_host, port)

    try:
        await site.start()
        print(f"🌐 Status server running on http://{bind_host}:{port}")
        if shutdown_event:
            await shutdown_event.wait()
        else:
            # Wait forever
            while True:
                await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()
