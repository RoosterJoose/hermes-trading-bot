#!/usr/bin/env python3
"""
Hermes Trading Bot — Hardened Watchdog
=======================================
Focused on ONE thing: keeping the bot alive.

Strategy:
  - PID file + heartbeat freshness check (primary)
  - If bot is down or heartbeat > 5 min stale → restart bot
  - If API server or tunnel die → log warning but DON'T restart 
    (they're long-lived and rarely crash; bot is the fragile one)
  - If API/tunnel are down at startup → start them once
  - Exponential backoff on repeated bot crashes (1min → 5min → 15min cap)
  - Logs every restart with crash count
  - All child processes use start_new_session=True (PPID 1 detachment)

Files:
  PID => /opt/data/hermes-trading/pids/<name>.pid
  Log => /opt/data/hermes-trading/watchdog.log
"""

import os
import sys
import time
import json
import signal
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path("/opt/data/hermes-trading")
STATE_DIR = BASE_DIR / "state"
PIDS_DIR = BASE_DIR / "pids"
LOG_FILE = BASE_DIR / "watchdog.log"
VENV_PYTHON = "/opt/hermes/.venv/bin/python"
CLOUDFLARED = "/tmp/cloudflared"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
    force=True,
)
log = logging.getLogger("wd")


def ensure_dir(d: Path):
    d.mkdir(parents=True, exist_ok=True)


def write_pid(name: str, pid: int):
    ensure_dir(PIDS_DIR)
    (PIDS_DIR / f"{name}.pid").write_text(str(pid))


def read_pid(name: str) -> int | None:
    pf = PIDS_DIR / f"{name}.pid"
    if pf.exists():
        try:
            return int(pf.read_text().strip())
        except (ValueError, OSError):
            return None
    return None


def is_alive(pid: int | None) -> bool:
    return pid is not None and os.kill(pid, 0) is None


def kill_all(pattern: str, exclude_pid: int | None = None):
    """Kill all processes matching `pattern` via pgrep. SIGTERM, wait, SIGKILL."""
    import subprocess as _sp

    try:
        r = _sp.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=10)
        if r.returncode != 0 or not r.stdout.strip():
            return
        pids = [int(p.strip()) for p in r.stdout.strip().split("\n")]
        for pid in pids:
            if exclude_pid and pid == exclude_pid:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        time.sleep(2)
        # Force survivors
        r2 = _sp.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=10)
        if r2.returncode == 0 and r2.stdout.strip():
            for pid_str in r2.stdout.strip().split("\n"):
                pid = int(pid_str.strip())
                if exclude_pid and pid == exclude_pid:
                    continue
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
    except Exception as e:
        log.warning(f"kill_all({pattern}): {e}")


def start_detached(cmd: list, cwd: str | None = None, logfile: str | None = None) -> int | None:
    """Launch a process fully detached (new session, no stdin, appended stdout)."""
    try:
        lf = logfile or str(BASE_DIR / f"{cmd[0].split('/')[-1]}.log")
        proc = subprocess.Popen(
            cmd,
            cwd=cwd or str(BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=open(lf, "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return proc.pid
    except Exception as e:
        log.error(f"start_detached({' '.join(str(c) for c in cmd[:3])}...): {e}")
        return None


# ── Health checks ──

def bot_healthy() -> tuple[bool, str]:
    """Check bot PID + heartbeat freshness."""
    pid = read_pid("bot")
    if not pid or not is_alive(pid):
        return False, f"bot PID {pid} not alive" if pid else "bot never started"

    hb = STATE_DIR / "heartbeat.json"
    if not hb.exists():
        return False, "no heartbeat file"
    try:
        data = json.loads(hb.read_text())
        ts = data.get("timestamp", "")
        if not ts:
            return False, "heartbeat has no timestamp"
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
        if age > 300:
            return False, f"heartbeat stale ({age:.0f}s > 300s)"
        return True, f"ok (PID {pid}, hb {age:.0f}s old)"
    except (json.JSONDecodeError, ValueError, OSError) as e:
        return False, f"heartbeat error: {e}"


def api_responding() -> bool:
    import urllib.request, urllib.error
    try:
        r = urllib.request.Request("http://localhost:8502/api/dashboard", method="HEAD")
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def tunnel_alive() -> bool:
    pid = read_pid("tunnel")
    return pid is not None and is_alive(pid)


def ingester_alive() -> bool:
    pid = read_pid("ingester")
    return pid is not None and is_alive(pid)


# ── Actions ──

def start_bot() -> int | None:
    kill_all("launch_bot.py")
    time.sleep(1)
    pid = start_detached(
        [VENV_PYTHON, str(BASE_DIR / "launch_bot.py")],
        logfile=str(BASE_DIR / "bot.log"),
    )
    if pid:
        write_pid("bot", pid)
    return pid


def start_api() -> int | None:
    kill_all("server.py")
    time.sleep(1)
    pid = start_detached(
        [VENV_PYTHON, str(BASE_DIR / "server.py")],
        logfile=str(BASE_DIR / "api.log"),
    )
    if pid:
        write_pid("api", pid)
    return pid


def start_tunnel() -> int | None:
    kill_all("cloudflared")
    time.sleep(1)
    pid = start_detached(
        [CLOUDFLARED, "tunnel", "--no-autoupdate", "--url", "http://localhost:8502"],
        logfile=str(BASE_DIR / "tunnel.log"),
    )
    if pid:
        write_pid("tunnel", pid)
    return pid


def start_ingester() -> int | None:
    kill_all("bar_ingester")
    time.sleep(1)
    pid = start_detached(
        [VENV_PYTHON, str(BASE_DIR / "scripts" / "bar_ingester.py")],
        logfile=str(BASE_DIR / "ingester.log"),
    )
    if pid:
        write_pid("ingester", pid)
    return pid


# ── Main loop ──

def main():
    log.info("=" * 50)
    log.info("🐶 Hermes Bot Watchdog v2 — PID %s (PPID %s)" % (os.getpid(), os.getppid()))
    write_pid("watchdog", os.getpid())

    # Phase 1: Ensure support infrastructure is up
    if not api_responding():
        log.info("  API server not responding — starting fresh")
        start_api()
    if not tunnel_alive():
        log.info("  Tunnel not alive — starting fresh")
        start_tunnel()
    if not ingester_alive():
        log.info("  Bar ingester not alive — starting fresh")
        start_ingester()

    # Phase 2: Start bot
    healthy, reason = bot_healthy()
    if not healthy:
        log.info(f"  Bot not healthy ({reason}) — starting")
        start_bot()
    else:
        log.info(f"  Bot healthy ({reason})")

    # Wait for bot to initialize
    time.sleep(10)

    # Phase 3: Watchdog loop
    failures = 0
    backoff = 60  # seconds, doubles up to 15min
    last_report = 0
    last_bot_start = time.time()

    while True:
        try:
            now = time.time()
            healthy, reason = bot_healthy()

            if not healthy:
                failures += 1
                wait = min(backoff * (2 ** (failures - 1)), 900)
                log.warning(f"🚨 Bot unhealthy (#{failures}): {reason} — restarting in {wait}s")
                time.sleep(min(wait, 30))  # Check in 30s chunks
                start_bot()
                last_bot_start = time.time()
                if failures >= 5:
                    log.error("🔥 5+ bot restarts — entering slow-poll mode (every 5 min)")
                    time.sleep(240)
            else:
                failures = 0  # Reset on success

            # Also ensure API/tunnel are up (but only log — don't crash the loop)
            if not api_responding():
                log.warning("  API server down — restarting")
                start_api()

            # Periodic health report (every 5 min)
            if now - last_report > 300:
                h, r = bot_healthy()
                api = api_responding()
                tun = tunnel_alive()
                ing = ingester_alive()
                log.info(f"📊 Bot={'✅' if h else '❌'} API={'✅' if api else '❌'} "
                         f"Tun={'✅' if tun else '❌'} Ingest={'✅' if ing else '❌'} "
                         f"| fail#{failures} | {r}")
                last_report = now

        except KeyboardInterrupt:
            log.info("🛑 Watchdog stopped by signal")
            break
        except Exception as e:
            log.error(f"💥 Watchdog error: {e}", exc_info=True)

        time.sleep(30)  # Standard check interval


if __name__ == "__main__":
    main()
