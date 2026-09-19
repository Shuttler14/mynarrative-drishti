"""
Marketplace-first recommendation engine.

Pipeline (see api/services/fashion_engine.py):
  INTENT → SLOT QUERIES → GOOGLE SHOPPING → ENRICH → HARD FILTER →
  MATCH SCORE → DIVERSITY SELECT → LABELLED PICKS

Google Shopping is the discovery layer; fashion intelligence lives in the engine.
"""
import asyncio
import logging

from api.services.price_scraper import (
    scrape_amazon,
    scrape_myntra,
    scrape_flipkart,
)
from api.services.fashion_engine import (
    build_intent,
    build_slot_queries,
    enrich_product,
    recommend_products,
)

logger = logging.getLogger("drishti.reco.marketplace")


def _format_product_for_reco(product: dict) -> dict:
    """Format an enriched marketplace product into the recommendation contract."""
    source = product.get("source", "")
    product_id = product.get("product_id", "")

    # Always prefer the actual URL from the scraper (direct product link)
    url = product.get("url", "")
    # Fallback: construct marketplace search URL so user can find the product
    if not url or "google.com/search" in url:
        from urllib.parse import quote_plus
        title = product.get("title", "")
        query = quote_plus(title)
        if source == "amazon":
            url = f"https://www.amazon.in/s?k={query}"
        elif source == "flipkart":
            url = f"https://www.flipkart.com/search?q={query}"
        elif source == "myntra":
            url = f"https://www.myntra.com/{query}"
        elif source == "ajio":
            url = f"https://www.ajio.com/search/?text={query}"
        elif source == "nykaaman" or source == "nykaa":
            url = f"https://www.nykaaman.com/search?q={query}"
        else:
            url = f"https://www.google.com/search?q={query}"

    fashion = product.get("fashion", {})

    return {
        "product_id": product_id,
        "title": product.get("title", ""),
        "category": fashion.get("category", product.get("category", "")),
        "slot": fashion.get("slot", "top"),
        "vton_friendly": fashion.get("vton_friendly", True),
        "price": product.get("price", 0),
        "mrp": product.get("mrp", 0),
        "discount_pct": product.get("discount_pct", 0),
        "currency": "INR",
        "image_url": product.get("image_url", ""),
        "url": url,
        "score": round(product.get("_final", product.get("relevance_score", 50) / 100), 3),
        "source": source,
        "brand": product.get("brand", ""),
        "rating": product.get("rating", 0),
        "rating_count": product.get("rating_count", 0),
        "color": fashion.get("color", ""),
        "fashion": {
            "category": fashion.get("category", ""),
            "color": fashion.get("color", ""),
            "fabrics": fashion.get("fabrics", []),
            "fit": fashion.get("fit", ""),
            "patterns": fashion.get("patterns", []),
        },
        "match_scores": product.get("match_scores", {}),
        "look_label": product.get("look_label", ""),
        "reason": product.get("_reason") or f"Found on {source.title()}",
    }


async def _search_google_shopping_slots(
    slot_queries: list[tuple[str, str]],
    per_query: int = 20,
) -> list[dict]:
    """Run slot-labelled Google Shopping searches in parallel.

    Results are fetched unfiltered (raw pool cached per query) — the price
    intelligence layer resolves the band against the live distribution.
    """
    from api.services.price_scraper import search_google_shopping

    tasks = [search_google_shopping(query, per_query) for _, query in slot_queries]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    products: list[dict] = []
    for (slot, query), result in zip(slot_queries, results):
        if isinstance(result, Exception):
            logger.warning(f"Google Shopping slot '{slot}' ('{query}') failed: {result}")
            continue
        for p in result:
            p["query_slot"] = slot
            products.append(p)
    return products


async def _scraper_fallback(query: str) -> list[dict]:
    """Direct marketplace scrapers when Google Shopping under-delivers."""
    tasks = [
        scrape_amazon(query, max_results=5),
        scrape_flipkart(query, max_results=5),
        scrape_myntra(query, max_results=5),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    products: list[dict] = []
    for result in results:
        if isinstance(result, list):
            products.extend(result)
    return products


async def search_marketplace_products(
    occasion: str = "",
    style: str = "",
    gender: str = "",
    body_profile: dict | None = None,
    weather: dict | None = None,
    price_segment: str = "",
    price_range: str = "",
    city: str = "",
    brands: list[str] | None = None,
    count: int = 12,
    budget_level: str = "",
    budget_min: float | None = None,
    budget_max: float | None = None,
    budget_point: float | None = None,
    budget_tolerance_pct: float = 20.0,
) -> dict:
    """Search marketplaces → enrich → price intelligence → scored selection."""
    intent = build_intent(
        occasion=occasion,
        style=style,
        gender=gender,
        price_segment=price_segment,
        price_range=price_range,
        weather=weather,
        city=city,
        brands=brands,
        budget_level=budget_level,
        budget_min=budget_min,
        budget_max=budget_max,
        budget_point=budget_point,
        budget_tolerance_pct=budget_tolerance_pct,
    )
    intent["body_shape"] = (body_profile or {}).get("body_shape", "")

    slot_queries = build_slot_queries(intent, count=count)
    logger.info(f"Slot queries: {slot_queries} (budget mode: {intent.get('budget_mode')})")

    products = await _search_google_shopping_slots(slot_queries)

    if len(products) < max(6, count):
        logger.info(f"Google Shopping under-delivered ({len(products)}), using scraper fallback")
        primary_query = slot_queries[0][1] if slot_queries else "fashion"
        fallback = await _scraper_fallback(primary_query)
        seen = {(p.get("source"), p.get("product_id")) for p in products}
        for p in fallback:
            key = (p.get("source"), p.get("product_id"))
            if key not in seen:
                seen.add(key)
                p.setdefault("query_slot", "primary")
                products.append(p)

    for p in products:
        enrich_product(p)

    result = recommend_products(products, intent, count)
    result["intent"] = intent
    return result


async def get_marketplace_recommendations(
    occasion: str = "",
    style: str = "",
    gender: str = "",
    body_profile: dict | None = None,
    weather: dict | None = None,
    price_segment: str = "",
    price_range: str = "",
    city: str = "",
    brands: list[str] | None = None,
    count: int = 12,
    budget_level: str = "",
    budget_min: float | None = None,
    budget_max: float | None = None,
    budget_point: float | None = None,
    budget_tolerance_pct: float = 20.0,
) -> dict:
    """Main entry point: recommendations + budget intelligence + ranked brands."""
    from api.services.price_intelligence import (
        band_label, distribution_by_slot, quick_picks, rank_brands,
    )

    result = await search_marketplace_products(
        occasion=occasion,
        style=style,
        gender=gender,
        body_profile=body_profile,
        weather=weather,
        price_segment=price_segment,
        price_range=price_range,
        city=city,
        brands=brands,
        count=count,
        budget_level=budget_level,
        budget_min=budget_min,
        budget_max=budget_max,
        budget_point=budget_point,
        budget_tolerance_pct=budget_tolerance_pct,
    )

    selected = result.get("selected", [])
    band = result.get("band")
    dist = result.get("distribution", {})
    candidates = result.get("candidates") or selected
    intent = result.get("intent", {})

    recommendations = [_format_product_for_reco(p) for p in selected[:count]]

    brand_pool = candidates if len(candidates) >= 10 else selected
    ranked_brands = rank_brands(brand_pool, band=band, limit=8)

    return {
        "recommendations": recommendations,
        "count": len(recommendations),
        "source": "fashion_engine",
        "platforms_searched": ["google_shopping", "amazon", "myntra", "flipkart"],
        "budget_intelligence": {
            "mode": intent.get("budget_mode", "none"),
            "level": intent.get("budget_level", ""),
            "band": [int(band[0]), int(band[1])] if band else None,
            "label": band_label(band, intent.get("budget_level", "") if intent.get("budget_mode") == "level" else ""),
            "distribution": dist,
            "by_slot": distribution_by_slot(candidates),
            "quick_picks": quick_picks(dist),
        },
        "brands": ranked_brands,
    }
