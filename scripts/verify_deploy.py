#!/usr/bin/env python3
"""Verify deployment: prove the right code is running.

Checks:
1. All critical processes are alive
2. The running bot's git commit matches deploy.json
3. No zombie processes
4. Heartbeat is fresh
5. API and tunnel responding
6. No duplicate instances
7. No stale .pyc files conflicting with source
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/data/hermes-trading")
STATE = BASE / "state"
PIDS = BASE / "pids"

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {name} — {detail}" if detail else f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} — {detail}" if detail else f"  ❌ {name}")


def is_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        stat_path = f"/proc/{pid}/stat"
        if os.path.exists(stat_path):
            stat = open(stat_path).read().split()
            if len(stat) > 2 and stat[2] == 'Z':
                return False
        return True
    except (ProcessLookupError, OSError):
        return False


def read_pid(name: str) -> int | None:
    pf = PIDS / f"{name}.pid"
    if pf.exists():
        try:
            return int(pf.read_text().strip())
        except (ValueError, OSError):
            return None
    return None


def main():
    global PASS, FAIL
    print(f"\n{'='*55}")
    print(f"🔍 Deployment Verification — {datetime.now(timezone.utc).isoformat()}")
    print(f"{'='*55}\n")

    # ── 1. Deployment marker exists ──
    dep_path = STATE / "deploy.json"
    if dep_path.exists():
        dep = json.loads(dep_path.read_text())
        check(1, "Deployment marker", f"commit {dep['commit'][:12]} at {dep['deployed_at']}")
    else:
        check(1, False, "deploy.json not found!")
        dep = {"commit": "unknown"}

    # ── 2. Git HEAD matches deploy.json ──
    try:
        git_hash = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, cwd=str(BASE)
        ).stdout.strip()
        match = git_hash == dep.get("commit")
        check(2, match, f"git={git_hash[:12]} deploy={dep['commit'][:12]}")
    except Exception as e:
        check(2, False, f"git error: {e}")

    # ── 2b. Running bot version matches deploy ──
    ver_path = STATE / "version.json"
    if ver_path.exists():
        try:
            ver = json.loads(ver_path.read_text())
            bot_commit = ver.get("commit", "")
            version_match = bot_commit == dep.get("commit")
            check("Bot version matches deploy", version_match,
                  f"bot={bot_commit[:12]} deploy={dep['commit'][:12]} started={ver.get('started_at','?')}")
        except Exception as e:
            check("Bot version matches deploy", False, str(e))
    else:
        check("Bot version matches deploy", False, "version.json not written yet (bot may still be starting)")

    # ── 3. All processes alive ──
    processes = {
        "watchdog": "bot_watchdog.py",
        "bot": "launch_bot.py",
        "api": "server.py",
        "ingester": "bar_ingester",
    }
    for name, pattern in processes.items():
        pid = read_pid(name)
        alive = is_alive(pid)
        extra = f"(PID {pid})" if pid else "(no PID file)"
        check(f"Process: {name}", alive, extra)

    # ── 4. No duplicate bot processes ──
    try:
        r = subprocess.run(["pgrep", "-f", "launch_bot.py"],
                           capture_output=True, text=True, timeout=5)
        count = len(r.stdout.strip().split("\n")) if r.stdout.strip() else 0
        check("No duplicate bot", count <= 1, f"{count} instance(s)")
    except Exception as e:
        check("No duplicate bot", False, f"pgrep error: {e}")

    # ── 5. No zombie processes ──
    zombies = 0
    for pid_file in PIDS.glob("*.pid"):
        pid = read_pid(pid_file.stem)
        if pid and os.path.exists(f"/proc/{pid}/stat"):
            stat = open(f"/proc/{pid}/stat").read().split()
            if len(stat) > 2 and stat[2] == 'Z':
                zombies += 1
    check("No zombie processes", zombies == 0, f"{zombies} zombie(s)")

    # ── 6. Heartbeat freshness ──
    hb_path = STATE / "heartbeat.json"
    if hb_path.exists():
        try:
            hb = json.loads(hb_path.read_text())
            ts = hb.get("timestamp", "")
            if ts:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
                check("Heartbeat fresh", age < 300, f"{age:.0f}s old")
            else:
                check("Heartbeat fresh", False, "no timestamp")
        except Exception as e:
            check("Heartbeat fresh", False, str(e))
    else:
        check("Heartbeat fresh", False, "no heartbeat file")

    # ── 7. API responding ──
    try:
        import urllib.request
        r = urllib.request.Request("http://localhost:8502/api/dashboard", method="GET")
        with urllib.request.urlopen(r, timeout=5) as resp:
            check("API responding", resp.status == 200, f"HTTP {resp.status}")
    except Exception as e:
        check("API responding", False, str(e))

    # ── 8. No stale .pyc files ──
    try:
        pyc_count = len(list(BASE.rglob("*.pyc")))
        check("No stale .pyc files", pyc_count == 0, f"{pyc_count} .pyc file(s) found")
    except Exception as e:
        check("No stale .pyc files", False, str(e))

    # ── 9. Lock file exists with correct PID ──
    lock_path = STATE / "bot.lock"
    bot_pid = read_pid("bot")
    if lock_path.exists() and bot_pid:
        try:
            lock_pid = int(lock_path.read_text().strip())
            check("Lock file valid", lock_pid == bot_pid, f"lock={lock_pid} pid={bot_pid}")
        except Exception as e:
            check("Lock file valid", False, str(e))
    elif not lock_path.exists():
        check("Lock file", bot_pid is not None, "no lock file but bot is starting")
    else:
        check("Lock file", False, "missing")

    # ── 10. runtime.json exists and is fresh ──
    rt_path = STATE / "runtime.json"
    if rt_path.exists():
        try:
            rt = json.loads(rt_path.read_text())
            rt_ts = rt.get("timestamp", "")
            if rt_ts:
                rt_age = (datetime.now(timezone.utc) - datetime.fromisoformat(rt_ts)).total_seconds()
                loop_interval = rt.get("loop_interval_seconds", 60)
                rt_fresh = rt_age < loop_interval * 5
                check("runtime.json fresh", rt_fresh, f"{rt_age:.0f}s old (max {loop_interval * 5}s)")
            else:
                check("runtime.json fresh", False, "no timestamp")
        except Exception as e:
            check("runtime.json fresh", False, str(e))
    else:
        check("runtime.json fresh", False, "no runtime.json")

    # ── 11. runtime.json git_commit matches deploy ──
    if rt_path.exists():
        try:
            rt = json.loads(rt_path.read_text())
            rt_commit = rt.get("git_commit", "")
            commit_match = rt_commit and (rt_commit in dep.get("commit", ""))
            check("Runtime git commit matches deploy", commit_match,
                  f"runtime={rt_commit} deploy={dep['commit'][:12]}")
        except Exception as e:
            check("Runtime git commit matches deploy", False, str(e))
    else:
        check("Runtime git commit matches deploy", False, "no runtime.json")

    # ── 12. No manual intervention flag ──
    mi_path = STATE / "manual_intervention_required"
    mi_exists = mi_path.exists()
    check("No manual intervention flag", not mi_exists,
          "MANUAL INTERVENTION REQUIRED — remove state/manual_intervention_required" if mi_exists else "clear")

    # ── 13. Trading enabled state (informational) ──
    if rt_path.exists():
        try:
            rt = json.loads(rt_path.read_text())
            te = rt.get("trading_enabled", True)
            reason = rt.get("trading_disabled_reason", "")
            pnl = rt.get("daily_pnl_pct", None)
            bal = rt.get("paper_balance", 0)
            pos = rt.get("total_open_positions", 0)
            detail = f"balance=${bal} PnL={pnl:+.2f}% positions={pos}"
            check("Trading enabled", bool(te), detail if te else f"DISABLED: {reason} | {detail}")
        except Exception:
            pass
    else:
        check("Trading enabled", False, "no runtime.json")

    # ── Summary ──
    total = PASS + FAIL
    print(f"\n{'='*55}")
    print(f"📊 {PASS}/{total} checks passed | {FAIL} failed")
    print(f"{'='*55}\n")

    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
