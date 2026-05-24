#!/usr/bin/env python3
"""Backfill 1h historical bar data from Kraken into SQLite.

Creates a `bars_1h` table alongside the existing `bars` (1m) table.
Fetches up to 720 1h candles (~30 days) per asset.

Usage:
    uv run python scripts/backfill_1h.py
"""

import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import asyncio

import ccxt.async_support as ccxt

ASSETS = ["SOL/USDT", "XRP/USDT"]
DB_PATH = Path(__file__).parent.parent / "data" / "bars.db"


def init_db():
    """Create bars_1h table if it doesn't exist."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bars_1h (
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
        CREATE INDEX IF NOT EXISTS idx_bars1h_asset_ts
        ON bars_1h (asset, timestamp)
    """)
    conn.commit()
    return conn


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


async def main():
    log("📊 1h Bar Backfill starting")
    conn = init_db()

    exchange = ccxt.kraken(
        {
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
    )

    try:
        for symbol in ASSETS:
            asset = symbol.replace("/", "_")
            log(f"\nFetching {symbol} 1h bars...")

            # Start from 35 days ago (buffer beyond 30d Kraken cap)
            since = int(
                (datetime.now(timezone.utc) - timedelta(days=35)).timestamp() * 1000
            )
            total_inserted = 0
            pages = 0

            while True:
                ohlcv = await exchange.fetch_ohlcv(symbol, "1h", since=since, limit=720)
                if not ohlcv:
                    break

                pages += 1
                inserted = 0
                for c in ohlcv:
                    ts = int(c[0] / 1000)
                    try:
                        conn.execute(
                            "INSERT OR IGNORE INTO bars_1h (asset, timestamp, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (asset, ts, c[1], c[2], c[3], c[4], c[5]),
                        )
                        if conn.total_changes > 0:
                            inserted += 1
                    except sqlite3.IntegrityError:
                        pass
                conn.commit()
                total_inserted += inserted

                log(f"  Page {pages}: {len(ohlcv)} bars fetched, {inserted} new")

                # Advance to next page
                last_ts = ohlcv[-1][0]
                since = last_ts + 3600000  # 1h later

                # If we got fewer than 720 bars, we've hit the end
                if len(ohlcv) < 720:
                    break

                # Rate limit courtesy
                await asyncio.sleep(1)

            log(f"  ✅ {symbol}: {total_inserted} bars inserted (new)")

            # Show date range
            row = conn.execute(
                "SELECT MIN(timestamp), MAX(timestamp) FROM bars_1h WHERE asset = ?",
                (asset,),
            ).fetchone()
            if row and row[0]:
                min_dt = datetime.fromtimestamp(row[0]).strftime("%Y-%m-%d %H:%M")
                max_dt = datetime.fromtimestamp(row[1]).strftime("%Y-%m-%d %H:%M")
                log(f"  Range: {min_dt} → {max_dt}")

    finally:
        await exchange.close()
        conn.close()

    # Summary
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute("SELECT asset, COUNT(*) FROM bars_1h GROUP BY asset").fetchall()
    conn.close()
    log(f"\n📊 Backfill complete:")
    for asset, count in rows:
        log(f"  {asset}: {count} bars")


if __name__ == "__main__":
    asyncio.run(main())
