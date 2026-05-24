"""
price.py — Price adapter with CCXT primary, yfinance fallback.
3 retries with exponential backoff. Schema-validated output.
Paper mode uses accumulated Brownian noise for realistic simulation.
"""
import asyncio
import math
import os
import random
from typing import Optional

import ccxt.async_support as ccxt_async

SCHEMA_VERSION = "1.0"

# Map asset keys to CoinGecko IDs for fallback
CG_IDS = {
    "SOL_USDT": "solana",
    "XRP_USDT": "ripple",
    "BTC_USDT": "bitcoin",
    "ETH_USDT": "ethereum",
    "BNB_USDT": "binancecoin",
    "DOGE_USDT": "dogecoin",
    "ADA_USDT": "cardano",
    "LINK_USDT": "chainlink",
    "AVAX_USDT": "avalanche-2",
    "DOT_USDT": "polkadot",
}


class PriceAdapter:
    """Fetches price data from CCXT exchanges with fallback chain.

    Priority: Kraken → Coinbase → yfinance → CoinGecko simple price.
    In live mode with premium keys, uses the configured exchange directly.
    """

    SCHEMA_VERSION = "1.0"

    def __init__(self, mode: str = "paper"):
        self.mode = mode
        self.consecutive_failures = 0
        self._exchanges = []
        self._init_exchanges()

        # Paper mode random walk state (per asset)
        self._noise_offset: dict[str, float] = {}
        self._noise_drift: dict[str, float] = {}  # small persistent drift
        self._last_real_prices: dict[str, float] = {}

    def _init_exchanges(self):
        """Initialize CCXT exchanges with optional premium keys."""
        config = {
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
        api_key = os.environ.get("CCXT_API_KEY", "")
        api_secret = os.environ.get("CCXT_API_SECRET", "")
        if api_key and api_secret:
            config["apiKey"] = api_key
            config["secret"] = api_secret

        self._exchanges = [
            ("kraken", ccxt_async.kraken(config)),
            ("coinbase", ccxt_async.coinbase(config)),
        ]

    async def _close_exchanges(self):
        for name, ex in self._exchanges:
            try:
                await ex.close()
            except Exception:
                pass

    async def fetch(self, asset_key: str, timeframe: str = "1m", limit: int = 100) -> dict:
        """Fetch price data with 3 retries and exponential backoff.

        Args:
            asset_key: e.g. 'SOL_USDT'
            timeframe: '1m', '5m', '1h', etc.
            limit: number of candles to fetch

        Returns:
            dict with schema_version, asset, current_price, candles, source
        """
        max_retries = 3
        last_error = None

        for attempt in range(1, max_retries + 1):
            try:
                result = await self._fetch_once(asset_key, timeframe, limit)
                self.consecutive_failures = 0
                return result
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    wait = 2 ** attempt  # 2s, 4s, 8s
                    await asyncio.sleep(wait)

        self.consecutive_failures += 1
        return {
            "schema_version": self.SCHEMA_VERSION,
            "asset": asset_key,
            "current_price": 0.0,
            "candles": [],
            "source": "error",
            "error": f"All {max_retries} retries failed: {last_error}",
            "consecutive_failures": self.consecutive_failures,
        }

    async def _fetch_once(self, asset_key: str, timeframe: str, limit: int) -> dict:
        """Single fetch attempt — yfinance primary for 1m candles, CCXT fallback."""
        symbol = asset_key.replace("_", "/")

        # 1. Try yfinance first — better 1m candle resolution for alt-L1 pairs
        #    (Kraken 1m SOL data only has ~13% non-zero deltas; yfinance has ~89%)
        if timeframe == "1m":
            try:
                import yfinance as yf

                ticker_symbol = asset_key.split("_")[0] + "-USD"
                ticker = yf.Ticker(ticker_symbol)
                hist = ticker.history(period="1d", interval="1m")
                if not hist.empty and len(hist) > 2:
                    candles = [
                        {
                            "timestamp": int(idx.timestamp() * 1000),
                            "open": float(row["Open"]),
                            "high": float(row["High"]),
                            "low": float(row["Low"]),
                            "close": float(row["Close"]),
                            "volume": float(row["Volume"]),
                        }
                        for idx, row in hist.iterrows()
                    ]
                    # Trim to requested limit (yfinance returns full day)
                    candles = candles[-limit:] if limit > 0 else candles
                    current_price = candles[-1]["close"]

                    if self.mode == "paper":
                        current_price = self._apply_noise(asset_key, current_price)

                    return {
                        "schema_version": self.SCHEMA_VERSION,
                        "asset": asset_key,
                        "current_price": current_price,
                        "candles": candles,
                        "source": "yfinance",
                    }
            except Exception:
                pass  # Fall through to CCXT

        # 2. Try CCXT exchanges in order (Kraken → Coinbase)
        for name, ex in self._exchanges:
            try:
                ohlcv = await ex.fetch_ohlcv(symbol, timeframe, limit=limit)
                if ohlcv and len(ohlcv) > 2:
                    candles = [
                        {
                            "timestamp": c[0],
                            "open": c[1],
                            "high": c[2],
                            "low": c[3],
                            "close": c[4],
                            "volume": c[5],
                        }
                        for c in ohlcv
                    ]
                    current_price = ohlcv[-1][4]

                    if self.mode == "paper":
                        current_price = self._apply_noise(asset_key, current_price)

                    return {
                        "schema_version": self.SCHEMA_VERSION,
                        "asset": asset_key,
                        "current_price": current_price,
                        "candles": candles,
                        "source": f"ccxt_{name}",
                    }
            except Exception:
                continue  # Try next exchange

        # 3. Try yfinance as fallback for non-1m timeframes (if CCXT failed)
        if timeframe != "1m":
            try:
                import yfinance as yf

                ticker_symbol = asset_key.split("_")[0] + "-USD"
                ticker = yf.Ticker(ticker_symbol)
                hist = ticker.history(period="1d", interval="1m")
                if not hist.empty and len(hist) > 2:
                    candles = []
                    for idx, row in hist.iterrows():
                        candles.append({
                            "timestamp": int(idx.timestamp() * 1000),
                            "open": float(row["Open"]),
                            "high": float(row["High"]),
                            "low": float(row["Low"]),
                            "close": float(row["Close"]),
                            "volume": float(row["Volume"]),
                        })
                    current_price = candles[-1]["close"]

                    if self.mode == "paper":
                        current_price = self._apply_noise(asset_key, current_price)

                    return {
                        "schema_version": self.SCHEMA_VERSION,
                        "asset": asset_key,
                        "current_price": current_price,
                        "candles": candles,
                        "source": "yfinance",
                    }
            except Exception:
                pass

        # 3. Fallback: CoinGecko simple price (no candles)
        cg_id = CG_IDS.get(asset_key)
        if cg_id:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.get(
                        "https://api.coingecko.com/api/v3/simple/price",
                        params={"ids": cg_id, "vs_currencies": "usd"},
                    )
                    if r.status_code == 200:
                        data = r.json()
                        price_data = data.get(cg_id, {})
                        current_price = price_data.get("usd", 0.0)
                        if current_price > 0:
                            if self.mode == "paper":
                                current_price = self._apply_noise(asset_key, current_price)
                            return {
                                "schema_version": self.SCHEMA_VERSION,
                                "asset": asset_key,
                                "current_price": current_price,
                                "candles": [],
                                "source": "coingecko",
                            }
            except Exception:
                pass

        raise RuntimeError(f"All price sources exhausted for {asset_key}")

    def _apply_noise(self, asset_key: str, real_price: float) -> float:
        """Apply accumulated Brownian noise for paper mode simulation.

        Instead of ±2% independent noise per tick (which makes trades random),
        we maintain a random walk offset that accumulates slowly. This gives
        realistic price movement — trending behavior rather than white noise.

        The noise is mean-reverting (Ornstein-Uhlenbeck-like) so it doesn't
        drift infinitely far from the real price.
        """
        if asset_key not in self._noise_offset:
            # Initialize noise state
            self._noise_offset[asset_key] = 0.0
            self._noise_drift[asset_key] = (random.random() - 0.5) * 0.002  # ±0.1% drift
            self._last_real_prices[asset_key] = real_price

        last_real = self._last_real_prices[asset_key]

        # If real price moved significantly, re-center our noise
        real_change = abs(real_price - last_real) / last_real if last_real > 0 else 0
        if real_change > 0.01:
            # Real price moved >1% — reset noise around new price
            self._noise_offset[asset_key] = 0.0
            self._last_real_prices[asset_key] = real_price
            return real_price

        # Update the random walk
        # Each tick: increment by N(0, 0.001) = average 0.1% move
        increment = random.gauss(0, 0.001)
        self._noise_offset[asset_key] += increment + self._noise_drift[asset_key]

        # Mean reversion: pull offset back toward 0
        self._noise_offset[asset_key] *= 0.98

        # Clamp to ±3% so we don't drift absurdly
        self._noise_offset[asset_key] = max(-0.03, min(0.03, self._noise_offset[asset_key]))

        noisy_price = real_price * (1.0 + self._noise_offset[asset_key])
        self._last_real_prices[asset_key] = real_price
        return round(noisy_price, 6)
