# Hermes Trading Bot — Full System Map

## Core Principle
Every component has a defined failure mode, and every failure mode has an automated detection + recovery path. There is no gap between "the bot should work" and "how we know the bot is working."

---

## Layer 1: Process Infrastructure

| Component | Launch Method | Parent | Recovery |
|-----------|--------------|--------|----------|
| **Watchdog** | `bot_watchdog.py` | Container init (PID 1) via nohup | Watchdog is the root — if it dies, heartbeats stop → cron alerts |
| **Bot** | `launch_bot.py` | Watchdog | Watchdog detects zombie/death → kills all → restarts |
| **API Server** | `server.py` | Watchdog | Watchdog detects non-response → restarts |
| **Ingester** | `bar_ingester.py` | Watchdog | Watchdog detects stale data → restarts |
| **Tunnel** | `cloudflared` | Watchdog | Watchdog detects unreachable → restarts |

**Failure modes covered:**
- [x] Bot crash → watchdog restarts within 10s
- [x] Bot zombie → `/proc/PID/stat` check → watchdog kills and restarts
- [x] Duplicate bot → `bot.lock` file prevents second instance
- [x] Duplicate watchdog → `kill_all("bot_watchdog.py")` on startup
- [x] Orphan processes → reparented to PID 1, no resource leak

---

## Layer 2: Self-Diagnostics (Runs Inside Bot)

| Check | Data Source | Frequency | Action on Failure |
|-------|-----------|-----------|-------------------|
| **Heartbeat** | In-memory → `heartbeat.json` | Every cycle (~2 min) | External cron detects staleness |
| **Cycle counter** | `cycles_since_last_trade` | Every cycle | Logs warning if > 200 with 0 positions |
| **Gate log** | `setups_log.jsonl` | Every skip | Dashboard reads for attribution |
| **Trade log** | `trades.jsonl` | Every close | Used by reflection + daily audit |
| **Duplicate check** | `bot.lock` | Startup | Refuses to start if PID alive |

---

## Layer 3: External Monitoring (Cron Jobs)

| Job | Schedule | Type | What It Checks | Alert On |
|-----|----------|------|---------------|----------|
| **Heartbeat check** | Every 5 min | no-agent script | Heartbeat age < 300s, zombie check, stuck detection | Stale heartbeat OR stuck > 150 cycles |
| **Daily audit** | 13:00 UTC daily | no-agent script | Balance, positions, cycles idle, blockers, bot/watchdog alive | Stuck state, missing processes, balance decline pattern |
| **Weekly reflection** | Sunday 13:00 UTC | agent-driven | Strategy performance, win rate, expectancy, parameter tuning | Degrading metrics |
| **Monthly LLM audit** | 1st of month | agent-driven | Full code review, parameter calibration, edge case analysis | Systematic issues |

---

## Layer 4: Pre-Deployment Validation (Every Code Change)

**Gate: `bash scripts/pre_launch_checklist.sh`** — must exit 0 before reporting done.

| Check | What It Validates |
|-------|------------------|
| **Git clean** | No uncommitted changes leaking into deploy |
| **Python syntax** | All modified files compile |
| **Validation suite** | `python3 scripts/validate_system.py` — all 7 checks pass |
| **Heartbeat check** | No false positive alert from monitoring script |
| **Remote sync** | Changes pushed to origin/main |

**Failure to pass = deployment blocked. No exceptions.**

---

## Layer 5: Recovery Tests (Manual, After Infrastructure Changes)

| Test | Procedure | Expected Result |
|------|-----------|----------------|
| **Bot crash recovery** | `kill -9 $(cat pids/bot.pid)` | Watchdog restarts within 10s, new heartbeat within 20s |
| **Zombie recovery** | Kill bot parent so child becomes zombie | Watchdog detects Z state, kills zombie, restarts |
| **Duplicate prevention** | Run `launch_bot.py` manually while bot running | Second instance exits with "already running" error |
| **Watchdog crash** | Kill watchdog | Heartbeat goes stale → cron alerts within 5 min |
| **Full restart** | Kill everything, run bootstrap.sh | All 5 processes up within 60s |
| **API server crash** | Kill API server | Watchdog detects non-response → restarts within 30s |

---

## Layer 6: Trading Reliability

| Parameter | Current Value | Why |
|-----------|--------------|-----|
| **Volume kill** | $1M | For $1k paper, anything under $1M is illiquid |
| **Spread gate** | 0.15% (raised from 0.08%) | NEAR had 0.13% spread during pump |
| **Max consecutive losses** | 5 per asset | Reset by decay after 50 idle cycles |
| **ADX threshold** | 25 (strong) | Trend entries require confirmed momentum |
| **Hurst regime** | 0.55 threshold | MR disabled in trending markets (correct behavior) |
| **Stop loss** | 3.0% ATR mult (alts) | Gives breathing room in volatile moves |
| **TP1** | 0.3R min | Captures partial profit early |
| **TP2** | 1.5R, 25% slice | Rides runners |

---

## What's Still Missing (To Be Built)

### Missing Layer 5
Recovery tests have never been run. Every failure mode is theoretical until tested.

### Missing graceful shutdown
The bot has no SIGTERM handler. If the container restarts, open positions are abandoned with no flatten logic.

### Missing live trading preparation
Slippage modeling, limit order stacking for tight 0.3R targets, withdrawal key management.

---

## The Deployment Flow (Going Forward)

```
1. Identify issue or feature
2. Implement change
3. Run `bash scripts/pre_launch_checklist.sh`
   ↓ FAIL → fix and repeat
   ↓ PASS → proceed
4. Run recovery tests if infrastructure changed
5. Push to origin
6. Report to user with audit result
```

This is the process that should have existed from the first deployment. It exists now. Every change from here goes through this gate.
