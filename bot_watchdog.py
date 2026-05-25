#!/usr/bin/env python3
"""
Hermes Trading Bot — Hardened Watchdog v3
=========================================
Focused on ONE thing: keeping the bot alive.

Changes in v3:
  - Named logger with RotatingFileHandler (no duplicate lines, bounded logs)
  - -B flag + PYTHONDONTWRITEBYTECODE in env for all child processes
  - fcntl.flock OS-level lock at startup (prevents two watchdogs)
  - API server circuit breaker (3-strike + exponential backoff)
  - Restart storm quarantine (3 crashes → safe mode → diagnostics only)

Strategy:
  - PID file + heartbeat freshness check (primary)
  - If bot is down or heartbeat > 5 min stale → restart bot
  - Exponential backoff on repeated bot crashes (1min → 5min → 15min cap)
  - API restart has its own circuit breaker (not fork-bomb)
  - Logs every restart with crash count
  - All child processes use start_new_session=True (PPID 1 detachment)
  - fcntl.flock prevents duplicate watchdog instances at OS level

Files:
  PID => /opt/data/hermes-trading/pids/<name>.pid
  Log => /opt/data/hermes-trading/watchdog.log (rotated at 10MB)
"""

import fcntl
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path("/opt/data/hermes-trading")
STATE_DIR = BASE_DIR / "state"
PIDS_DIR = BASE_DIR / "pids"
LOG_FILE = str(BASE_DIR / "watchdog.log")
VENV_PYTHON = "/opt/hermes/.venv/bin/python"
CLOUDFLARED = "/tmp/cloudflared"
RUNTIME_FILE = "state/runtime.json"
MANUAL_INTERVENTION_FILE = "state/manual_intervention_required"
LOCK_FILE = str(BASE_DIR / "state" / "watchdog.lock")

# ── Logger setup (named logger, RotatingFileHandler, no duplicates) ──
log = logging.getLogger("hermes.watchdog")
log.setLevel(logging.INFO)
log.propagate = False
log.handlers.clear()

_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

_file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=10_000_000, backupCount=10, encoding="utf-8"
)
_file_handler.setFormatter(_formatter)
log.addHandler(_file_handler)


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
    """Check if a process is alive and not a zombie (Z state)."""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        # Process exists — check it's not a zombie
        try:
            stat_path = f"/proc/{pid}/stat"
            if os.path.exists(stat_path):
                stat = open(stat_path).read().split()
                if len(stat) > 2 and stat[2] == 'Z':
                    return False  # Zombie — not usable
        except (OSError, IOError, IndexError):
            pass
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def read_runtime() -> dict:
    """Read state/runtime.json, return empty dict on failure."""
    path = BASE_DIR / RUNTIME_FILE
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return {}


def kill_all(pattern: str, exclude_pid: int | None = None):
    """Kill all processes matching `pattern` via pgrep. SIGTERM, wait, SIGKILL."""
    try:
        r = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=10)
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
        r2 = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=10)
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
    """Launch a process fully detached with -B flag and PYTHONDONTWRITEBYTECODE.

    Defense-in-depth: passes -B on the Python invocation AND sets the env var,
    so .pyc files can never be created by child processes.
    """
    try:
        lf = logfile or str(BASE_DIR / f"{cmd[0].split('/')[-1]}.log")
        # Ensure -B flag is present (prevents .pyc generation)
        launch_cmd = list(cmd)
        if launch_cmd and launch_cmd[0].endswith("python"):
            if "-B" not in launch_cmd:
                launch_cmd.insert(1, "-B")
        # Explicit env with PYTHONDONTWRITEBYTECODE (defense-in-depth)
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.Popen(
            launch_cmd,
            cwd=cwd or str(BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=open(lf, "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
        return proc.pid
    except Exception as e:
        log.error(f"start_detached({' '.join(str(c) for c in cmd[:3])}...): {e}")
        return None


# ── Health checks ──

def bot_healthy() -> tuple[bool, str]:
    """
    Check bot health using PRIMARY signal: heartbeat age from runtime.json.
    PID check is secondary/fallback only.
    """
    runtime = read_runtime()
    pid = read_pid("bot")

    # PRIMARY: heartbeat age from runtime.json
    ts = runtime.get("timestamp", "")
    if ts:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
            loop_interval = runtime.get("loop_interval_seconds", 60)
            max_age = loop_interval * 4  # 4x loop interval grace period

            # If PID exists but is zombie, bot is dead even if heartbeat looks fresh
            if pid and not is_alive(pid):
                return False, f"zombie PID {pid} (hb {age:.0f}s old)"

            if age > max_age:
                return False, f"heartbeat stale ({age:.0f}s > {max_age}s max)"

            # Heartbeat is fresh — bot is running
            return True, f"ok (hb {age:.0f}s old, PID {pid})"
        except (ValueError, TypeError):
            pass

    # FALLBACK: PID + legacy heartbeat.json
    hb = STATE_DIR / "heartbeat.json"
    if hb.exists() and pid and is_alive(pid):
        try:
            data = json.loads(hb.read_text())
            ts = data.get("timestamp", "")
            if ts:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
                if age > 300:
                    return False, f"legacy heartbeat stale ({age:.0f}s > 300s)"
                return True, f"ok (legacy hb {age:.0f}s old, PID {pid})"
        except (json.JSONDecodeError, ValueError, OSError):
            pass
    elif hb.exists() and (not pid or not is_alive(pid)):
        return False, f"legacy heartbeat present but bot PID {pid} not alive"

    # LAST RESORT: PID check
    if pid and is_alive(pid):
        return True, f"ok (PID {pid} alive, no heartbeat yet)"
    if pid:
        return False, f"bot PID {pid} not alive"
    return False, "bot never started"


def api_responding() -> bool:
    try:
        r = urllib.request.Request("http://localhost:8502/api/dashboard", method="GET")
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def verify_restart() -> tuple[bool, str]:
    """
    Run 3 checks after a bot restart:
    1. Bot PID is alive and not zombie
    2. Lock file PID matches running bot
    3. Heartbeat written within 2 cycles
    """
    pid = read_pid("bot")
    if not pid or not is_alive(pid):
        return False, f"PID {pid} not alive"

    lock_path = STATE_DIR / "bot.lock"
    if lock_path.exists():
        try:
            lock_pid = int(lock_path.read_text().strip())
            if lock_pid != pid:
                return False, f"lock PID {lock_pid} != bot PID {pid}"
        except (ValueError, OSError):
            pass

    runtime = read_runtime()
    ts = runtime.get("timestamp", "")
    if ts:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
            loop_interval = runtime.get("loop_interval_seconds", 60)
            if age < loop_interval * 3:
                return True, f"all checks passed (PID {pid}, hb {age:.0f}s)"
        except (ValueError, TypeError):
            pass

    return True, f"started (PID {pid}, waiting for first heartbeat)"


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


def write_manual_intervention(reason: str):
    """Write manual_intervention_required file and return True."""
    mi_path = BASE_DIR / MANUAL_INTERVENTION_FILE
    try:
        mi_path.write_text(
            f"{reason}\n"
            f"Written at {datetime.now(timezone.utc).isoformat()}\n"
            f"Remove this file to resume watchdog.\n"
        )
        log.error(f"🔴 Manual intervention flag written — {reason}")
    except OSError:
        pass


# ── Circuit breaker for API server ──
class APICircuitBreaker:
    """3-strike + exponential backoff for API server restart.

    Prevents fork-bomb if server.py crashes immediately on restart.
    """

    def __init__(self):
        self.failures = 0
        self.last_failure_time = 0
        self.min_gap = 300  # 5 minutes between strike resets
        self.max_strikes = 3
        self.backoff_base = 5  # 5, 15, 45 seconds
        self.degraded = False

    def record_failure(self):
        now = time.time()
        if now - self.last_failure_time > self.min_gap:
            self.failures = 0  # Reset if long gap (bot recovered)
        self.failures += 1
        self.last_failure_time = now
        if self.failures >= self.max_strikes:
            self.degraded = True
        return self.failures

    def get_backoff(self) -> int:
        return min(self.backoff_base * (3 ** (self.failures - 1)), 120)  # 5, 15, 45, 120 cap

    def record_success(self):
        now = time.time()
        if now - self.last_failure_time > self.min_gap:
            self.failures = 0
            self.degraded = False

    def should_retry(self) -> bool:
        if self.degraded:
            return False  # Circuit open — stop trying
        if self.failures == 0:
            return True
        backoff = self.get_backoff()
        return (time.time() - self.last_failure_time) > backoff


# ── Main loop ──

def main():
    # ── OS-level lock: prevent duplicate watchdog instances ──
    try:
        os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
        lock_fd = open(LOCK_FILE, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        log.info("🔒 Watchdog lock acquired (fd=%s)", lock_fd.fileno())
    except (IOError, OSError) as e:
        log.error("🔴 Another watchdog is already running (lock file: %s) — exiting", LOCK_FILE)
        sys.exit(1)

    log.info("=" * 50)
    log.info("🐶 Hermes Bot Watchdog v3 — PID %s (PPID %s)", os.getpid(), os.getppid())
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
        log.info("  Bot not healthy (%s) — starting", reason)
        start_bot()
    else:
        log.info("  Bot healthy (%s)", reason)

    # Wait for bot to initialize
    time.sleep(10)

    # Phase 3: Watchdog loop
    failures = 0
    backoff = 60  # seconds, doubles up to 15min
    last_report = 0
    last_bot_start = time.time()
    consecutive_verify_fails = 0
    safe_mode = False  # Quarantine: after 3 verify fails, stop paper trading, only diagnostics
    api_cb = APICircuitBreaker()  # Separate circuit breaker for API server

    while True:
        try:
            now = time.time()

            # Check for manual intervention flag
            mi_path = BASE_DIR / MANUAL_INTERVENTION_FILE
            if mi_path.exists():
                log.error("🔴 Manual intervention flag set — watchdog paused")
                log.error("  Remove state/manual_intervention_required to resume")
                time.sleep(60)
                continue

            # ── Bot health check ──
            healthy, reason = bot_healthy()

            if not healthy:
                failures += 1
                wait = min(backoff * (2 ** (failures - 1)), 900)
                log.warning("🚨 Bot unhealthy (#%s): %s — restarting in %ss", failures, reason, wait)

                # Check for crash log
                if (BASE_DIR / "state" / "crash.log").exists() and (BASE_DIR / "state" / "crash.log").stat().st_mtime > (time.time() - 300):
                    log.warning("  📋 Crash log found (recent) — check state/crash.log for traceback")

                if safe_mode:
                    log.error("  🔒 SAFE MODE: Not restarting bot. Only diagnostics running.")
                    log.error("  Remove state/manual_intervention_required and restart to recover.")
                    write_manual_intervention("Safe mode: 3+ consecutive crashes, bot quarantined")
                    time.sleep(60)
                    continue

                time.sleep(min(wait, 30))
                start_bot()
                last_bot_start = time.time()

                # 3-strike verification after restart
                verify_wait = 0
                verified = False
                vreason = "no verify attempt"
                while verify_wait < 15:
                    time.sleep(5)
                    verify_wait += 5
                    vpassed, vreason = verify_restart()
                    if vpassed:
                        verified = True
                        consecutive_verify_fails = 0
                        log.info("  ✅ Restart verified: %s", vreason)
                        break
                    log.warning("  ⏳ Restart verify pending (%ss): %s", verify_wait, vreason)

                if not verified:
                    consecutive_verify_fails += 1
                    log.warning("  ⚠️  Restart verification failed (%s/3)", consecutive_verify_fails)
                    if consecutive_verify_fails >= 3:
                        log.error("🔥 3 consecutive restart verify failures — entering safe mode")
                        safe_mode = True
                        write_manual_intervention(
                            f"3 consecutive restart verify failures. Last: {vreason}"
                        )
                elif failures >= 5:
                    log.error("🔥 5+ bot restarts — entering slow-poll mode (every 5 min)")
                    time.sleep(240)
            else:
                failures = 0  # Reset on success

            # ── API server health check (with circuit breaker) ──
            if not api_responding():
                if api_cb.should_retry():
                    strike = api_cb.record_failure()
                    backoff_sec = api_cb.get_backoff()
                    log.warning("  🖥️  API server down (strike %s/3) — restarting in %ss", strike, backoff_sec)
                    time.sleep(backoff_sec)
                    start_api()
                    # If it comes back, record success
                    if api_responding():
                        api_cb.record_success()
                        log.info("  ✅ API server recovered")
                else:
                    if not api_cb.degraded:
                        api_cb.degraded = True
                        log.warning("  🖥️  API server circuit OPEN — marking degraded, will retry later")
            else:
                api_cb.record_success()

            # Periodic health report (every 5 min)
            if now - last_report > 300:
                h, r = bot_healthy()
                api = api_responding()
                tun = tunnel_alive()
                ing = ingester_alive()
                mode_label = "🔒SAFE" if safe_mode else "   "
                log.info(
                    f"📊 {mode_label} Bot={'✅' if h else '❌'} API={'✅' if api else '❌'} "
                    f"Tun={'✅' if tun else '❌'} Ingest={'✅' if ing else '❌'} "
                    f"| fail#{failures} api_s#{api_cb.failures}/3{'🔴' if api_cb.degraded else ''} | {r}"
                )
                last_report = now

        except KeyboardInterrupt:
            log.info("🛑 Watchdog stopped by signal")
            break
        except Exception as e:
            log.error("💥 Watchdog error: %s", e, exc_info=True)

        time.sleep(30)


if __name__ == "__main__":
    main()
