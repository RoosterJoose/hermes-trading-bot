"""News/sentiment adapter — optional, returns empty without API key."""
import os


class SchemaError(Exception):
    pass


class NewsAdapter:
    SCHEMA_VERSION = "1.0"

    def __init__(self, mode: str = "paper"):
        self.mode = mode
        self.consecutive_failures = 0
        self.api_key = os.environ.get("NEWS_API_KEY", "")

    async def fetch(self, asset_key: str) -> dict:
        if not self.api_key:
            return {
                "schema_version": self.SCHEMA_VERSION,
                "asset": asset_key,
                "available": False,
                "message": "No NEWS_API_KEY configured",
                "articles": [],
            }

        try:
            import httpx
            symbol = asset_key.split("_")[0]
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    "https://newsapi.org/v2/everything",
                    params={
                        "q": f"crypto {symbol}",
                        "sortBy": "publishedAt",
                        "pageSize": 5,
                        "apiKey": self.api_key,
                    },
                )
                data = resp.json()

            self.consecutive_failures = 0
            articles = [
                {"title": a["title"], "url": a["url"], "published": a["publishedAt"]}
                for a in data.get("articles", [])
            ]
            return {
                "schema_version": self.SCHEMA_VERSION,
                "asset": asset_key,
                "available": True,
                "articles": articles,
            }
        except Exception as e:
            self.consecutive_failures += 1
            return {
                "schema_version": self.SCHEMA_VERSION,
                "asset": asset_key,
                "available": False,
                "error": str(e),
                "articles": [],
            }
