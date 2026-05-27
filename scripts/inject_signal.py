#!/usr/bin/env python3
"""inject_signal.py — Push a signal from Telegram into the running bot.

Usage:
  python inject_signal.py --state-dir /path/to/state "ONTUSDT LONG 0.0533 SL 0.0507 TP1 0.0536 TP2 0.0541 TP3 0.0545 TP4 0.0548 TP5 0.0552"
  python inject_signal.py --state-dir /path/to/state --file signal.txt

Writes state/pending_signal.json for the loop to pick up on next cycle.
Then reads state/signal_result.json for confirmation.
"""
import argparse
import json
import sys
import time
from pathlib import Path

# Add hermes-trading to path
sys.path.insert(0, str(Path(__file__).parent.parent))
from hermes_trading import signal_handler as sh


def main():
    parser = argparse.ArgumentParser(description="Inject a trading signal")
    parser.add_argument("--state-dir", default="/opt/data/hermes-trading/state",
                        help="Bot state directory")
    parser.add_argument("--file", help="Read signal from file")
    parser.add_argument("text", nargs="*", help="Signal text (e.g. /signal ONTUSDT LONG ...)")

    args = parser.parse_args()
    state_dir = Path(args.state_dir)

    # Get signal text
    if args.file:
        with open(args.file) as f:
            text = f.read()
    else:
        text = " ".join(args.text)

    if not text.strip():
        print("Error: no signal text provided")
        sys.exit(1)

    # Parse
    signal = sh.parse_signal(text)
    if signal is None:
        print(f"Error: could not parse signal from:\n{text}")
        sys.exit(1)

    print(f"Parsed signal: {signal['asset']} {signal['direction']} @ {signal['entry']}")
    print(f"  SL: {signal['stop_loss']}")
    print(f"  Targets: {signal['targets']}")

    # Validate with current market price (basic check — script doesn't have live data)
    # Bot does deeper validation on receive
    if not sh.validate_signal(signal, signal["entry"]):
        print("Warning: signal fails basic validation")

    # Write pending signal file
    pending_file = state_dir / "pending_signal.json"
    payload = {"signal": signal, "timestamp": time.time()}

    with open(pending_file, "w") as f:
        json.dump(payload, f)
    print(f"\nPending signal written to {pending_file}")
    print("Bot will pick it up on next cycle (~60s)")

    # Wait briefly for result
    result_file = state_dir / "signal_result.json"
    for _ in range(30):
        if result_file.exists():
            try:
                with open(result_file) as f:
                    result = json.load(f)
                result_file.unlink(missing_ok=True)
                status = result.get("status", "unknown")
                if status == "ok":
                    print(f"\n✅ Signal accepted by bot!")
                    print(f"   Asset: {result['asset']}")
                    print(f"   Entry: {result['entry_price']}")
                    print(f"   Size: {result['size_r']}")
                else:
                    print(f"\n❌ Signal rejected: {result.get('reason', 'unknown')}")
                sys.exit(0 if status == "ok" else 1)
            except (json.JSONDecodeError, OSError):
                pass
        time.sleep(0.5)

    print("\n⏳ Signal written, waiting for bot to process...")


if __name__ == "__main__":
    main()
