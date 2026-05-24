"""Macro/fear-greed adapter — free API, no key needed. 3 retries, schema-compliant."""

SCHEMA_VERSION = "1.0"


class MacroAdapter:
    SCHEMA_VERSION = "1.0"

    def __init__(self, mode: str = "paper"):
        self.mode = mode
        self.consecutive_failures = 0

    async def fetch(self, asset_key: str) -> dict:
        """Fetch Crypto Fear & Greed Index with 3 retries.

        Returns:
            dict with schema_version, asset, available, indicators
        """
        max_retries = 3
        results = {}

        for attempt in range(1, max_retries + 1):
            try:
                import httpx
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        "https://api.alternative.me/fng/?limit=1"
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        raw = data.get("data", [{}])[0]
                        results["fear_greed"] = {
                            "value": raw.get("value"),
                            "classification": raw.get("value_classification"),
                            "timestamp": raw.get("timestamp"),
                        }

                self.consecutive_failures = 0
                return {
                    "schema_version": self.SCHEMA_VERSION,
                    "asset": asset_key,
                    "available": True,
                    "indicators": results,
                }
            except Exception as e:
                if attempt < max_retries:
                    import asyncio
                    await asyncio.sleep(2 ** attempt)

        self.consecutive_failures += 1
        return {
            "schema_version": self.SCHEMA_VERSION,
            "asset": asset_key,
            "available": False,
            "indicators": {},
            "error": f"All {max_retries} retries failed",
            "consecutive_failures": self.consecutive_failures,
        }
