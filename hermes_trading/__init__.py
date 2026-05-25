#!/usr/bin/env python3
"""
Hermes Trading Worker — Multi-asset self-improving trading agent.
Entrypoint: parses config, starts the async trading loop.
"""

import argparse
import os
import sys
import yaml
import asyncio
from pathlib import Path
from typing import Optional

BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__))).parent
STATE_DIR = BASE_DIR / "state"


def load_goal() -> dict:
    goal_path = STATE_DIR / "goal.yaml"
    if not goal_path.exists():
        print("FATAL: state/goal.yaml not found. Run Phase 2 first.")
        sys.exit(1)
    with open(goal_path) as f:
        return yaml.safe_load(f)


def load_strategy(asset_key: str, mode: str = "paper") -> Optional[dict]:
    """Load strategy config. Returns None if missing (caller must handle fail-closed)."""
    strat_path = STATE_DIR / asset_key / "strategy.yaml"
    if not strat_path.exists():
        if mode == "live":
            print(f"FATAL: {asset_key}: strategy.yaml not found — refusing to trade live")
            sys.exit(1)
        print(f"  ⚠️  {asset_key}: strategy.yaml not found — skipping asset")
        return None
    with open(strat_path) as f:
        return yaml.safe_load(f)


CRASH_LOG = str(BASE_DIR / "crash.log")


def main():
    # ── Global exception handler: catch anything, log traceback, exit non-zero ──
    try:
        _main_impl()
    except KeyboardInterrupt:
        print("\n🛑 Shutdown requested.")
        sys.exit(0)
    except SystemExit:
        raise  # Let the sys.exit() calls in main propagate normally
    except BaseException as e:
        import traceback

        tb = traceback.format_exc()
        msg = (
            f"=== CRASH at {datetime.now(timezone.utc).isoformat()} ===\n"
            f"Exception: {e}\n"
            f"Traceback:\n{tb}\n"
        )
        print(msg, flush=True)
        try:
            with open(CRASH_LOG, "a") as f:
                f.write(msg)
        except OSError:
            pass
        sys.exit(99)


def _main_impl():
    parser = argparse.ArgumentParser(description="Hermes Trading Worker")
    parser.add_argument("--mode", choices=["paper", "live"], default="paper")
    parser.add_argument(
        "--asset", help="Override: trade a single asset key (e.g. SOL_USDT)"
    )
    args = parser.parse_args()

    goal = load_goal()

    # Determine which assets to run
    assets = goal.get("assets", [])
    if args.asset:
        assets = [a for a in assets if a["key"] == args.asset]
        if not assets:
            print(f"Unknown asset: {args.asset}")
            sys.exit(1)

    if not assets:
        print("FATAL: No assets defined in goal.yaml")
        sys.exit(1)

    print(f"🎯 Hermes Trading Worker — {args.mode.upper()} mode")
    valid_assets = []
    for a in assets:
        asset_key = a["key"]
        strat = load_strategy(asset_key, mode=args.mode)
        if strat is None:
            print(f"  ❌ {asset_key}: skipped (no config)")
            continue
        valid_assets.append(a)
        print(
            f"   {asset_key}: v{strat.get('version', '?')} | "
            f"target +{a['target_return_30d'] * 100}% | "
            f"max DD {a['max_drawdown'] * 100}%"
        )
    assets = valid_assets

    if not assets:
        print("FATAL: No assets with valid strategy config — refusing to start")
        sys.exit(1)

    # Import and run the loop
    sys.path.insert(0, str(BASE_DIR))
    from hermes_trading.loop import TradingLoop
    from hermes_trading.status_server import run_status_server

    async def run_all():
        """Run trading loop + status server together."""
        status_task = asyncio.create_task(
            run_status_server(STATE_DIR, port=8199)
        )
        try:
            await TradingLoop(
                assets,
                STATE_DIR,
                BASE_DIR,
                mode=args.mode,
                initial_balance=goal.get("initial_balance", 10000.0),
            ).run()
        finally:
            status_task.cancel()
            try:
                await status_task
            except asyncio.CancelledError:
                pass

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run_all())
    except KeyboardInterrupt:
        print("\n🛑 Shutdown requested.")
    finally:
        loop.close()


if __name__ == "__main__":
    main()
