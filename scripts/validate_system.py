#!/usr/bin/env python3
"""
Hermes Trading Bot — Full-System Health Validation
Run after every code change or when suspecting issues.
Exit 0 = all checks pass. Exit 1 = something is wrong.

python3 scripts/validate_system.py
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/data/hermes-trading")
STATE = BASE / "state"
PIDS = BASE / "pids"

RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

failures = []


def ok(msg):
    print(f"  {GREEN}✅{RESET} {msg}")


def warn(msg):
    print(f"  {YELLOW}⚠️ {RESET} {msg}")


def fail(msg):
    print(f"  {RED}❌{RESET} {msg}")
    failures.append(msg)


def bold(msg):
    print(f"\n{CYAN}{BOLD}{msg}{RESET}")


# ── 1. Process tree check ──
bold("1. Process Tree Integrity")

# Get all python processes related to the bot
proc_data = subprocess.run(
    ["ps", "-eo", "pid,ppid,stat,cmd"],
    capture_output=True, text=True, timeout=10
).stdout

bot_procs = []
watchdog_procs = []
api_procs = []
ingester_procs = []

for line in proc_data.split("\n"):
    stripped = line.strip()
    if "launch_bot.py" in stripped:
        bot_procs.append(stripped)
    elif "bot_watchdog.py" in stripped:
        # Filter out bash wrappers (contain 'bash -lic' or 'set +m')
        # Actual watchdog is a Python process
        if "python" in stripped and "bash" not in stripped:
            watchdog_procs.append(stripped)
    elif "server.py" in stripped:
        api_procs.append(stripped)
    elif "bar_ingester.py" in stripped:
        ingester_procs.append(stripped)

# Exactly 1 bot
if len(bot_procs) == 0:
    fail("No bot process found (launch_bot.py)")
elif len(bot_procs) > 1:
    fail(f"Multiple bot processes ({len(bot_procs)}) — duplicate!")
    for p in bot_procs:
        print(f"       {p}")
else:
    ok(f"1 bot process")

# Exactly 1 watchdog
if len(watchdog_procs) == 0:
    fail("No watchdog process found (bot_watchdog.py)")
elif len(watchdog_procs) > 1:
    fail(f"Multiple watchdog processes ({len(watchdog_procs)}) — duplicate!")
    for p in watchdog_procs:
        print(f"       {p}")
else:
    ok(f"1 watchdog process")

# Verify bot is child of watchdog
if bot_procs and watchdog_procs:
    bot_pid = int(bot_procs[0].split()[0])
    bot_ppid = int(bot_procs[0].split()[1])
    wd_pid = int(watchdog_procs[0].split()[0])
    if bot_ppid == wd_pid:
        ok("Bot is child of watchdog")
    else:
        warn(f"Bot PPID {bot_ppid} ≠ watchdog PID {wd_pid}")

# API server
if len(api_procs) == 0:
    fail("No API server (server.py)")
elif len(api_procs) > 1:
    warn(f"Multiple API servers ({len(api_procs)})")
else:
    ok(f"1 API server (PID {api_procs[0].split()[0]})")

# Ingester
if len(ingester_procs) == 0:
    fail("No bar ingester (bar_ingester.py)")
else:
    ok(f"1 bar ingester")


# ── 2. Zombie check ──
bold("2. Zombie Detection")

zombies = []
for line in proc_data.split("\n"):
    parts = line.split()
    if len(parts) > 2 and parts[2] == 'Zs':
        zombies.append(line.strip())

if zombies:
    for z in zombies:
        fail(f"Zombie process: {z}")
else:
    ok("No zombie processes")


# ── 3. PID file integrity ──
bold("3. PID File Integrity")

pid_files = ["bot.pid", "watchdog.pid", "api.pid", "ingester.pid", "tunnel.pid"]
for pf in pid_files:
    path = PIDS / pf
    if not path.exists():
        fail(f"Missing PID file: {pf}")
        continue
    try:
        pid = int(path.read_text().strip())
        # Check if process exists and state
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat=", "--no-headers"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            fail(f"PID file {pf}={pid}: process not found (stale PID)")
        elif result.stdout.strip() == 'Z':
            fail(f"PID file {pf}={pid}: process is ZOMBIE")
        else:
            ok(f"PID file {pf}={pid} [{result.stdout.strip()}]")
    except (ValueError, OSError) as e:
        fail(f"PID file {pf}: read error — {e}")


# ── 4. Heartbeat freshness ──
bold("4. Heartbeat Freshness")

hb_path = STATE / "heartbeat.json"
if not hb_path.exists():
    fail("No heartbeat file")
else:
    try:
        hb = json.loads(hb_path.read_text())
        ts = hb.get("timestamp", "")
        if not ts:
            fail("Heartbeat has no timestamp")
        else:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
            if age > 300:
                fail(f"Heartbeat stale: {int(age)}s old (> 300s)")
            else:
                ok(f"Heartbeat fresh: {int(age)}s old")

        mode = hb.get("mode", "?")
        balance = hb.get("paper_balance", 0)
        assets = len(hb.get("trade_count_since_reflection", {}))
        ok(f"Mode: {mode}, Balance: ${balance:.2f}, Assets: {assets}")

        # Check cycles_since_last_trade
        cycles = hb.get("cycles_since_last_trade", {})
        if cycles:
            min_c = min(cycles.values())
            max_c = max(cycles.values())
            # 999 sentinel = restart (not stuck)
            if min_c >= 990 and min_c <= 1010:
                warn(f"Recent restart detected (cycles={min_c}) — bot may need warm-up time")
            elif min_c > 150:
                warn(f"All assets idle for {min_c} cycles (~{min_c*2//60}h) — check gates")

            # Check for stalled range (should have variation if trading)
            if max_c - min_c > 100:
                ok(f"Some assets have traded recently (cycle range: {min_c}-{max_c})")
            elif max_c <= 10:
                ok(f"Recently started (cycles: {min_c}-{max_c})")
    except (json.JSONDecodeError, ValueError, Exception) as e:
        fail(f"Corrupt heartbeat: {e}")


# ── 5. Recent setups_log activity ──
bold("5. Trading Activity (setups_log)")

base_dir = str(STATE)
recent_entries = 0
recent_skips = 0
recent_entries_by_asset = 0
now = time.time()

for asset_dir in sorted(os.listdir(base_dir)):
    ap = os.path.join(base_dir, asset_dir)
    if not os.path.isdir(ap):
        continue
    sf = os.path.join(ap, "setups_log.jsonl")
    if not os.path.exists(sf):
        continue
    try:
        lines = open(sf).readlines()
        if lines:
            recent_entries_by_asset += 1
        # Check last 10 entries for timing
        for line in lines[-10:]:
            try:
                rec = json.loads(line)
                ts = rec.get("timestamp", "")
                if ts:
                    try:
                        entry_age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
                        if entry_age < 300:  # Last 5 min
                            recent_entries += 1
                            if rec.get("decision") == "skipped" or "skip" in rec.get("reason", "").lower():
                                recent_skips += 1
                    except:
                        pass
            except:
                pass
    except:
        pass

if recent_entries_by_asset == 0:
    fail("No assets have any setups_log data — bot may not be cycling")
else:
    ok(f"{recent_entries_by_asset} assets with setups_log data")
    if recent_entries > 0:
        ok(f"{recent_entries} recent entries in last 5 min ({recent_skips} skips)")
        if recent_entries == recent_skips:
            warn("ALL recent entries are skips — bot is cycling but fully blocked by gates")
    else:
        warn("No entries in last 5 min — bot may not be cycling")


# ── 6. Watchdog health ──
bold("6. Watchdog Status")

wd_path = BASE / "watchdog.log"
if wd_path.exists():
    lines = subprocess.run(
        ["tail", "-10", str(wd_path)],
        capture_output=True, text=True, timeout=5
    ).stdout
    for line in lines.split("\n"):
        if "Bot=" in line and "API=" in line:
            # e.g., "Bot=✅ API=✅ Tun=✅ Ingest=✅"
            if "❌" not in line:
                ok(f"Watchdog reports all green: {line.split('INFO')[-1].strip()}")
            else:
                parts = line
                if "Bot=❌" in parts:
                    fail("Watchdog reports BOT UNHEALTHY")
                if "API=❌" in parts:
                    fail("Watchdog reports API UNHEALTHY")
                if "Tun=❌" in parts:
                    fail("Watchdog reports TUNNEL UNHEALTHY")
                if "Ingest=❌" in parts:
                    fail("Watchdog reports INGESTER UNHEALTHY")
            break
    else:
        warn("No health report line in watchdog log")
else:
    fail("No watchdog.log")


# ── 7. Crash check ──
bold("7. Recent Crashes")

crash_path = BASE / "crash.log"
if crash_path.exists():
    try:
        crash_age = time.time() - crash_path.stat().st_mtime
        if crash_age < 3600:  # Last hour
            crash_content = crash_path.read_text().strip()
            if crash_content:
                last_crash = crash_content.split("=== CRASH")[-1][:200] if "=== CRASH" in crash_content else crash_content[:200]
                fail(f"Crash log modified {int(crash_age//60)} min ago:\n       {last_crash[:120].strip()}")
            else:
                ok("Crash log exists but is empty")
        else:
            ok(f"No crashes in last hour (last: {int(crash_age//3600)}h ago)")
    except OSError:
        ok("Can't read crash.log")
else:
    ok("No crash.log")


# ── Summary ──
bold("=" * 40)
total = len(failures)
if total == 0:
    print(f"  {GREEN}{BOLD}✅ ALL CHECKS PASSED{RESET}")
    sys.exit(0)
else:
    print(f"  {RED}{BOLD}{total} FAILURE(S) DETECTED:{RESET}")
    for f in failures:
        print(f"    {RED}•{RESET} {f}")
    sys.exit(1)
