"""On-chain data adapter — optional, returns empty data without API keys."""
import os


class SchemaError(Exception):
    pass


class OnChainAdapter:
    SCHEMA_VERSION = "1.0"

    def __init__(self, mode: str = "paper"):
        self.mode = mode
        self.consecutive_failures = 0
        self.api_key = os.environ.get("GLASSNODE_API_KEY", "")

    async def fetch(self, asset_key: str) -> dict:
        """Fetch on-chain metrics if API key configured."""
        if not self.api_key:
            return {
                "schema_version": self.SCHEMA_VERSION,
                "asset": asset_key,
                "available": False,
                "message": "No GLASSNODE_API_KEY configured",
                "metrics": {},
            }

        try:
            import httpx
            symbol = asset_key.split("_")[0].lower()
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"https://api.glassnode.com/v1/metrics/transactions/count",
                    params={"a": symbol, "api_key": self.api_key},
                )
                if resp.status_code == 200:
                    data = resp.json()
                else:
                    data = {"error": resp.text}

            self.consecutive_failures = 0
            return {
                "schema_version": self.SCHEMA_VERSION,
                "asset": asset_key,
                "available": True,
                "metrics": data if isinstance(data, dict) else {"raw": data},
            }
        except Exception as e:
            self.consecutive_failures += 1
            return {
                "schema_version": self.SCHEMA_VERSION,
                "asset": asset_key,
                "available": False,
                "error": str(e),
                "metrics": {},
            }
