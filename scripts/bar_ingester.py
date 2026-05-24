#!/usr/bin/env python3
"""1-minute bar ingester — runs alongside the trading loop.

Fetches 1m candles from Kraken for all tracked assets and stores them
in a local SQLite database for RSI percentile / Hurst / CUSUM computation.

Idempotent: skips bars already stored. Runs until Ctrl+C.
"""
import asyncio
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import ccxt.async_support as ccxt

# ── Config ──────────────────────────────────────────────────────────────────
ASSETS = ["ADA/USDT", "AVAX/USDT", "BNB/USDT", "BTC/USDT", "DOGE/USDT",
         "DOT/USDT", "ETH/USDT", "LINK/USDT", "SOL/USDT", "XRP/USDT"]
TIMEFRAME = "1m"
LIMIT = 100  # fetch last 100 one-minute bars each cycle
CYCLE_SECONDS = 60
MAX_AGE_DAYS = 30

STATE_DIR = Path("/opt/data/hermes-trading/state")
DATA_DIR = Path("/opt/data/hermes-trading/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "bars.db"
LOG_PATH = Path("/tmp/bar_ingester.log")


def init_db():
    """Create the bars table if it doesn't exist."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bars (
            asset TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL,
            PRIMARY KEY (asset, timestamp)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_bars_asset_ts
        ON bars (asset, timestamp)
    """)
    conn.commit()
    return conn


def insert_bars(conn, asset: str, candles: list) -> int:
    """Insert candle list into DB. Returns count of new bars inserted."""
    rows = []
    for c in candles:
        ts = int(c[0] / 1000)  # CCXT timestamps are ms
        rows.append((asset, ts, c[1], c[2], c[3], c[4], c[5]))

    inserted = 0
    for row in rows:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO bars (asset, timestamp, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            if conn.total_changes > 0:
                inserted += 1
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    return inserted


def prune_old(conn):
    """Remove bars older than MAX_AGE_DAYS."""
    cutoff = int(time.time()) - MAX_AGE_DAYS * 86400
    conn.execute("DELETE FROM bars WHERE timestamp < ?", (cutoff,))
    conn.commit()


def get_stats(conn) -> dict:
    """Return bar count per asset and earliest/latest timestamps."""
    rows = conn.execute(
        "SELECT asset, COUNT(*), MIN(timestamp), MAX(timestamp) FROM bars GROUP BY asset"
    ).fetchall()
    stats = {}
    for asset, count, min_ts, max_ts in rows:
        min_dt = datetime.fromtimestamp(min_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        max_dt = datetime.fromtimestamp(max_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        stats[asset] = {"count": count, "from": min_dt, "to": max_dt}
    return stats


async def run_cycle(exchange, conn):
    """Fetch bars for all assets and store them."""
    for symbol in ASSETS:
        try:
            ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=LIMIT)
            if not ohlcv:
                log(f"  {symbol}: no data returned")
                continue
            new = insert_bars(conn, symbol.replace("/", "_"), ohlcv)
            log(f"  {symbol}: {len(ohlcv)} candles fetched, {new} new")
        except Exception as e:
            log(f"  {symbol}: ERROR — {e}")

    prune_old(conn)
    stats = get_stats(conn)
    for asset, s in stats.items():
        log(f"  DB {asset}: {s['count']} bars from {s['from']} to {s['to']}")


def log(msg: str):
    """Timestampped log line to both stdout and log file."""
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


async def main():
    log("📊 Bar Ingestion started")
    log(f"   Assets: {', '.join(ASSETS)}")
    log(f"   DB: {DB_PATH}")
    log(f"   Cycle: every {CYCLE_SECONDS}s | Max age: {MAX_AGE_DAYS}d")

    conn = init_db()
    exchange = ccxt.kraken({
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })

    # Check connectivity and existing data
    try:
        stats = get_stats(conn)
        for asset, s in stats.items():
            log(f"   Existing: {asset} — {s['count']} bars ({s['from']} → {s['to']})")
    except Exception as e:
        log(f"   DB init: {e}")

    try:
        while True:
            t0 = time.time()
            await run_cycle(exchange, conn)
            elapsed = time.time() - t0
            sleep_time = max(1, CYCLE_SECONDS - elapsed)
            await asyncio.sleep(sleep_time)
    except asyncio.CancelledError:
        log("⏹ Ingestion stopped")
    finally:
        await exchange.close()
        conn.close()


if __name__ == "__main__":
    asyncio.run(main())
