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

BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__))).parent
STATE_DIR = BASE_DIR / "state"


def load_goal() -> dict:
    goal_path = STATE_DIR / "goal.yaml"
    if not goal_path.exists():
        print("FATAL: state/goal.yaml not found. Run Phase 2 first.")
        sys.exit(1)
    with open(goal_path) as f:
        return yaml.safe_load(f)


def load_strategy(asset_key: str) -> dict:
    strat_path = STATE_DIR / asset_key / "strategy.yaml"
    if not strat_path.exists():
        # Return default v01 strategy
        return {
            "version": "01",
            "entry": {"indicator": "rsi", "threshold": 30, "direction": "long"},
            "stop_loss_pct": 2.0,
            "position_size_r": 0.5,
        }
    with open(strat_path) as f:
        return yaml.safe_load(f)


def main():
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
    for a in assets:
        asset_key = a["key"]
        strat = load_strategy(asset_key)
        print(
            f"   {asset_key}: v{strat['version']} | target +{a['target_return_30d'] * 100}% | max DD {a['max_drawdown'] * 100}%"
        )

    # Import and run the loop
    sys.path.insert(0, str(BASE_DIR))
    from hermes_trading.loop import TradingLoop

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(
            TradingLoop(
                assets,
                STATE_DIR,
                BASE_DIR,
                mode=args.mode,
                initial_balance=goal.get("initial_balance", 10000.0),
            ).run()
        )
    except KeyboardInterrupt:
        print("\n🛑 Shutdown requested.")
    finally:
        loop.close()


if __name__ == "__main__":
    main()
