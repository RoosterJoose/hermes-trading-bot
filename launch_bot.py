#!/opt/hermes/.venv/bin/python3
import datetime
#!/usr/bin/env python
"""Launch the trading bot with proper asyncio event loop."""
import sys
import os
from pathlib import Path

# Ensure we're in the right directory
os.chdir("/opt/data/hermes-trading")
sys.path.insert(0, "/opt/data/hermes-trading")

# Write version marker before starting the bot
try:
    import json as _json
    import subprocess as _sp
    from hermes_trading import state as _hm_state
    version = {"commit": _sp.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=5
    ).stdout.strip(), "pid": os.getpid(), "started_at": __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ).isoformat()}
    _hm_state.atomic_write_json(Path("state") / "version.json", version)
    print(f"  📝 Bot version: {version['commit'][:12]}")
except Exception as e:
    print(f"  ⚠️  Could not write version.json: {e}")

from hermes_trading import main

# ── Top-level crash handler: write traceback to disk, then exit ──
import traceback
import sys as _sys

def _run():
    try:
        main()
    except KeyboardInterrupt:
        _sys.exit(0)
    except SystemExit:
        raise  # Let sys.exit() calls propagate normally
    except BaseException:
        tb = traceback.format_exc()
        print(f"💥 Bot crashed — writing traceback to state/crash.log", flush=True)
        print(tb, flush=True)
        try:
            crash_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "crash.log")
            os.makedirs(os.path.dirname(crash_path), exist_ok=True)
            with open(crash_path, "a") as f:
                f.write(f"\n=== CRASH at {__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()} ===\n")
                f.write(tb)
                f.write("\n")
        except OSError:
            pass
        _sys.exit(99)

_run()
