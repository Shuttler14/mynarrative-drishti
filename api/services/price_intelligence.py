"""
Price Intelligence Layer — dynamic, category-aware budget resolution.

Replaces static price slabs with:
  - Percentile-based price distributions computed from live results
  - Shopping levels (value / contemporary / premium / luxury) mapped onto the
    live distribution for whatever category the user is browsing
  - Continuous budget ranges (slider) and "around my budget" point budgets
  - Dynamically generated quick picks
  - Algorithmic brand ranking (price affinity × style relevance × quality ×
    popularity × coverage) — brands are never hardcoded to price slabs
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict

logger = logging.getLogger("drishti.reco.pricing")

# Shopping levels → percentile windows of the live category distribution
LEVEL_WINDOWS: dict[str, tuple[float, float]] = {
    "value": (0.03, 0.40),
    "contemporary": (0.30, 0.70),
    "premium": (0.60, 0.90),
    "luxury": (0.85, 1.0),
}

# Used only when there is not enough live data to build a distribution
LEVEL_FALLBACK_BANDS: dict[str, tuple[float, float]] = {
    "value": (0, 1200),
    "contemporary": (1000, 2500),
    "premium": (2200, 4000),
    "luxury": (3500, 10**9),
}

_DEFAULT_QUICK_PICKS: list[tuple[int, int]] = [
    (0, 1000), (1000, 2000), (2000, 3000), (3000, 5000), (5000, 10**9),
]


def fmt_inr(n: float) -> str:
    if n >= 100000:
        v = n / 100000
        return f"₹{v:.0f}L" if v == int(v) else f"₹{v:.1f}L"
    if n >= 1000:
        v = n / 1000
        return f"₹{v:.0f}K" if v == int(v) else f"₹{v:.1f}K"
    return f"₹{int(n)}"


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0
    idx = min(len(sorted_vals) - 1, max(0, round(p * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def price_distribution(products: list[dict]) -> dict:
    """Live price distribution for the candidate pool (the 'category picture')."""
    prices = sorted(float(p.get("price") or 0) for p in products if (p.get("price") or 0) > 0)
    if not prices:
        return {"count": 0}
    return {
        "count": len(prices),
        "min": prices[0],
        "p25": _percentile(prices, 0.25),
        "median": _percentile(prices, 0.50),
        "p75": _percentile(prices, 0.75),
        "p90": _percentile(prices, 0.90),
        "max": prices[-1],
    }


def distribution_by_slot(products: list[dict]) -> dict:
    """Per-slot distributions (tops vs bottoms price differently)."""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for p in products:
        slot = (p.get("fashion") or {}).get("slot", "top")
        buckets[slot].append(p)
    out = {}
    for slot, items in buckets.items():
        if len(items) >= 5:
            out[slot] = price_distribution(items)
    return out


def resolve_band(intent: dict, dist: dict) -> tuple[float, float] | None:
    """Resolve the effective price band for this request."""
    mode = intent.get("budget_mode", "none")
    if mode in ("range", "around", "legacy"):
        return intent.get("band")
    if mode == "level":
        level = intent.get("budget_level", "contemporary")
        if dist.get("count", 0) >= 8:
            lo_p, hi_p = LEVEL_WINDOWS.get(level, LEVEL_WINDOWS["contemporary"])
            lo = _percentile(
                [dist["min"], dist["p25"], dist["median"], dist["p75"], dist["p90"], dist["max"]], lo_p
            )
            hi = _percentile(
                [dist["min"], dist["p25"], dist["median"], dist["p75"], dist["p90"], dist["max"]], hi_p
            )
            if hi > lo:
                return (lo, hi)
        return LEVEL_FALLBACK_BANDS.get(level)
    return None


def quick_picks(dist: dict) -> list[dict]:
    """Dynamic budget chips derived from the live distribution."""
    if dist.get("count", 0) < 8:
        picks = _DEFAULT_QUICK_PICKS
    else:
        lo = dist["min"]
        bounds = [
            (lo, dist["p25"]),
            (dist["p25"], dist["median"]),
            (dist["median"], dist["p75"]),
            (dist["p75"], dist["p90"]),
            (dist["p90"], dist["max"]),
        ]
        picks = []
        seen = set()
        for a, b in bounds:
            a, b = round(a / 100) * 100, round(b / 100) * 100
            if b <= a or (a, b) in seen:
                continue
            if a > 0 and (b - a) < 300:
                continue
            seen.add((a, b))
            picks.append((a, b))
        if len(picks) < 3:
            picks = _DEFAULT_QUICK_PICKS

    out = []
    for a, b in picks:
        label = f"Under {fmt_inr(b)}" if a <= 0 else f"{fmt_inr(a)} – {fmt_inr(b)}" if b < 10**9 else f"{fmt_inr(a)}+"
        out.append({"label": label, "min": int(a), "max": int(b)})
    return out[:5]


def band_label(band: tuple[float, float] | None, level: str = "") -> str:
    if level in LEVEL_WINDOWS:
        return level.title()
    if not band:
        return "Any budget"
    lo, hi = band
    if lo <= 0:
        return f"Under {fmt_inr(hi)}"
    if hi >= 10**9:
        return f"{fmt_inr(lo)}+"
    return f"{fmt_inr(lo)} – {fmt_inr(hi)}"


# ══════════════════════════════════════════════════════════════
# BRAND RANKING (algorithmic — never hardcoded to price slabs)
# ══════════════════════════════════════════════════════════════

_BRAND_BLOCKLIST = {"women", "womens", "women's", "men", "mens", "men's", "the", "new", "buy",
                    "pack", "combo", "puma", "unisex"}


def rank_brands(products: list[dict],
                band: tuple[float, float] | None = None,
                limit: int = 8) -> list[dict]:
    """Score brands from their actual live products in this context."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for p in products:
        brand = (p.get("brand") or "").strip().lower()
        if not brand or brand in _BRAND_BLOCKLIST or len(brand) < 3:
            continue
        groups[brand].append(p)

    total = max(len(products), 1)
    results: list[dict] = []

    for brand, items in groups.items():
        prices = [float(p.get("price") or 0) for p in items if (p.get("price") or 0) > 0]
        if not prices:
            continue
        avg_price = sum(prices) / len(prices)
        rated = [p.get("rating") for p in items if p.get("rating")]
        avg_rating = sum(rated) / len(rated) if rated else 0
        reviews = sum(int(p.get("rating_count") or 0) for p in items)
        relevance = sum(p.get("_final", 0.5) for p in items) / len(items)

        quality = min(avg_rating / 5.0, 1.0) if avg_rating else 0.5
        popularity = min(math.log10(reviews + 1) / 4.0, 1.0)
        coverage = min(len(items) / max(total * 0.10, 1), 1.0)

        if band:
            lo, hi = band
            center = (lo + hi) / 2
            span = max(hi - lo, 1)
            price_aff = max(0.0, 1.0 - abs(avg_price - center) / span)
        else:
            price_aff = 0.6

        score = (0.30 * relevance + 0.25 * price_aff + 0.15 * quality +
                 0.15 * popularity + 0.15 * coverage)

        results.append({
            "name": brand.title(),
            "score": round(score, 3),
            "avg_price": int(avg_price),
            "product_count": len(items),
            "rating": round(avg_rating, 1) if avg_rating else None,
            "_quality": quality,
            "_popularity": popularity,
        })

    results.sort(key=lambda b: b["score"], reverse=True)
    top = results[:limit]

    # Contextual tags (computed, not hardcoded)
    if top:
        trending = max(top, key=lambda b: b["_popularity"])
        trending["tag"] = "Trending"
        prices = sorted(b["avg_price"] for b in top)
        median_price = prices[len(prices) // 2]
        affordables = [b for b in top if b["avg_price"] <= median_price and b["_quality"] >= 0.6]
        if affordables:
            max(affordables, key=lambda b: b["_quality"])["tag"] = "Best Value"
        premium_candidates = [b for b in top if b["avg_price"] >= median_price and b["_quality"] >= 0.7]
        if premium_candidates:
            max(premium_candidates, key=lambda b: b["score"])["tag"] = "Premium"
        rising = [b for b in top if not b.get("tag") and b["_quality"] >= 0.7]
        if rising:
            max(rising, key=lambda b: b["score"])["tag"] = "Rising"

    for b in top:
        b.pop("_quality", None)
        b.pop("_popularity", None)
    return top
