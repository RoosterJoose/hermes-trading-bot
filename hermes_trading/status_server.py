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
    """Compute go-live readiness from heartbeat + state data."""
    required_days = 30
    min_trades = 50
    max_drawdown_limit = 10.0

    paper_start = heartbeat.get("paper_start_date")
    paper_days = 0
    if paper_start:
        try:
            start = datetime.strptime(paper_start, "%Y-%m-%d").date()
            paper_days = (datetime.now(timezone.utc).date() - start).days
        except (ValueError, TypeError):
            pass

    # Count trades across all assets
    total_trades = 0
    assets_dir = state_dir
    if assets_dir.exists():
        for asset_dir in sorted(assets_dir.iterdir()):
            if not asset_dir.is_dir():
                continue
            trades_file = asset_dir / "trades.jsonl"
            if trades_file.exists():
                try:
                    total_trades += len(
                        [l for l in trades_file.read_text().strip().split("\n") if l.strip()]
                    )
                except Exception:
                    pass

    dd_pct = float(heartbeat.get("total_pnl_pct", 0))
    # For drawdown, we need max drawdown from portfolio tracker.
    # Use heartbeat's trust_state or monte_carlo as proxy
    mc_dd = heartbeat.get("monte_carlo", {}).get("dd_95_pct")
    dd_limit = abs(mc_dd) if mc_dd else max_drawdown_limit
    if dd_pct < 0:
        dd_ok = abs(dd_pct) <= dd_limit
    else:
        dd_ok = True

    days_met = paper_days >= required_days
    trades_met = total_trades >= min_trades

    blockers = []
    if not days_met:
        blockers.append(f"paper_days: {paper_days}/{required_days}")
    if not trades_met:
        blockers.append(f"trade_count: {total_trades}/{min_trades}")
    if not dd_ok:
        blockers.append(f"drawdown: {abs(dd_pct):.1f}% > {dd_limit:.0f}% limit")

    return {
        "paper_days_elapsed": paper_days,
        "required_paper_days": required_days,
        "paper_days_met": days_met,
        "total_trades": total_trades,
        "min_trade_count": min_trades,
        "min_trade_count_met": trades_met,
        "max_drawdown_pct": round(abs(dd_pct), 2) if dd_pct < 0 else 0.0,
        "max_drawdown_limit": dd_limit,
        "max_drawdown_ok": dd_ok,
        "data_integrity_ok": heartbeat is not None,
        "live_ready": days_met and trades_met and dd_ok,
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

    Args:
        state_dir: Path to state directory.
        port: HTTP port (default 8099).
        shutdown_event: Optional event to signal graceful shutdown.
    """
    app = create_app(state_dir)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)

    try:
        await site.start()
        print(f"🌐 Status server running on http://0.0.0.0:{port}")
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
