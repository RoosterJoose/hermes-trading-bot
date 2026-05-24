"""
universe.py — Phase 4: Hyperliquid data fetcher + Universe scan

Fetches all perp metadata + asset contexts from HL's public info endpoint.
No wallet/signer needed for read-only data.

Hard eligibility screen + watch score ranking → top 5-7 tradable perps.
"""

import httpx
from typing import Optional

HL_INFO_URL = "https://api.hyperliquid.xyz/info"

# Eligibility thresholds (from spec.md Section 2.2)
MIN_VOLUME_24H = 50_000_000  # $50M
MIN_OPEN_INTEREST = 10_000_000  # $10M
MIN_RV_RATIO = 0.8
MAX_RV_RATIO = 3.0
MIN_MOVE = 0.75

# Spread thresholds per asset tier (from spec)
MAX_SPREAD_BPS = {
    "core": 5,  # BTC, ETH
    "major": 8,  # SOL, XRP, LINK, AVAX, ADA
    "beta": 12,  # DOGE and others
}

# Asset tier classification
TIERS = {
    "core": {"BTC", "ETH"},
    "major": {"SOL", "XRP", "LINK", "AVAX", "ADA"},
    "beta": {
        "DOGE",
        "MATIC",
        "DOT",
        "ARB",
        "OP",
        "ATOM",
        "APT",
        "SUI",
        "NEAR",
        "HYPE",
        "PEPE",
        "AAVE",
        "UNI",
        "LDO",
        "INJ",
        "TIA",
    },
}


def _get_tier(symbol: str) -> str:
    """Classify an asset into its tier based on symbol name."""
    for tier, symbols in TIERS.items():
        if symbol in symbols:
            return tier
    return "beta"  # default


async def fetch_hl_meta_and_ctx() -> Optional[list]:
    """Fetch all perp metadata + asset contexts from Hyperliquid.

    Returns:
        Raw [meta, ctxs] list or None on failure.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(HL_INFO_URL, json={"type": "metaAndAssetCtxs"})
            if r.status_code == 429:
                return None  # rate-limited, skip this cycle
            r.raise_for_status()
            return r.json()
    except Exception:
        return None


def parse_universe(raw_data: list) -> list[dict]:
    """Parse HL metaAndAssetCtxs response into per-asset dicts.

    Returns list of dicts with keys:
      symbol, mark_px, prev_px, funding_rate, funding_annualized_pct,
      open_interest_usd, volume_24h, change_24h_pct, tier
    """
    meta = raw_data[0]
    ctxs = raw_data[1]
    assets = []

    for i, asset_meta in enumerate(meta.get("universe", [])):
        name = asset_meta.get("name", "")
        ctx = ctxs[i] if i < len(ctxs) else {}

        mark_px = float(ctx.get("markPx", 0))
        oi = float(ctx.get("openInterest", 0))
        funding = float(ctx.get("funding", 0))
        prev_px = float(ctx.get("prevPx", 0))
        volume_24h = float(ctx.get("dayNtlVlm", 0))
        oi_usd = oi * mark_px if mark_px > 0 else 0
        change_24h = ((mark_px - prev_px) / prev_px * 100) if prev_px > 0 else 0

        assets.append(
            {
                "symbol": name,
                "mark_px": mark_px,
                "prev_px": prev_px,
                "funding_rate": funding,
                "funding_annualized_pct": funding * 365 * 24 * 100,
                "open_interest_usd": oi_usd,
                "volume_24h": volume_24h,
                "change_24h_pct": round(change_24h, 2),
                "tier": _get_tier(name),
            }
        )

    return assets


def screen_and_rank(assets: list[dict], max_results: int = 7) -> list[dict]:
    """Apply hard eligibility screen → watch score → top N.

    Eligibility (ALL must pass):
      - 24h volume >= $50M
      - Open interest >= $10M

    Watch score:
      0.30 × rank(volume) + 0.20 × rank(OI) + 0.15 × rank(|change|)

    Returns:
        List of eligible assets sorted by watch_score descending.
        Empty list if fewer than 3 pass eligibility.
    """
    # Hard screen
    eligible = []
    for a in assets:
        if a["volume_24h"] < MIN_VOLUME_24H:
            continue
        if a["open_interest_usd"] < MIN_OPEN_INTEREST:
            continue
        # Must have valid price
        if a["mark_px"] <= 0:
            continue
        eligible.append(a)

    if len(eligible) < 3:
        return []  # No-trade state if fewer than 3 eligible

    # Compute max values for normalization
    max_vol = max(a["volume_24h"] for a in eligible)
    max_oi = max(a["open_interest_usd"] for a in eligible)
    max_abs_change = max(abs(a["change_24h_pct"]) for a in eligible)

    # Watch score
    for a in eligible:
        vol_score = a["volume_24h"] / max_vol if max_vol > 0 else 0
        oi_score = a["open_interest_usd"] / max_oi if max_oi > 0 else 0
        move_score = (
            abs(a["change_24h_pct"]) / max_abs_change if max_abs_change > 0 else 0
        )

        a["watch_score"] = round(
            0.30 * vol_score + 0.20 * oi_score + 0.15 * move_score,
            4,
        )

    eligible.sort(key=lambda a: a["watch_score"], reverse=True)
    return eligible[:max_results]


def format_universe_report(ranked: list[dict]) -> str:
    """Format a ranked universe list for log output."""
    if not ranked:
        return "  ⚠ Universe scan: < 3 assets eligible — no-trade state"

    lines = [f"  🌌 Universe scan: top {len(ranked)}/{len(ranked)} eligible"]
    lines.append(
        f"     {'Symbol':8s} {'Vol($M)':8s} {'OI($M)':8s} {'Fund%APY':9s} {'Chg%':6s} {'Tier':6s} {'Score':6s}"
    )
    for a in ranked:
        lines.append(
            f"     {a['symbol']:8s} {a['volume_24h'] / 1e6:8.1f} {a['open_interest_usd'] / 1e6:8.1f} "
            f"{a['funding_annualized_pct']:>+8.3f}% {a['change_24h_pct']:>+5.1f}% "
            f"{a['tier']:6s} {a['watch_score']:.4f}"
        )
    return "\n".join(lines)
