"""
Marketplace-first recommendation engine.
Searches Amazon, Myntra, Flipkart directly based on user preferences.
Returns products with live prices — no separate price lookup needed.
"""
import asyncio
import logging
import re
from typing import Optional

from api.services.price_scraper import (
    scrape_amazon,
    scrape_myntra,
    scrape_flipkart,
    extract_search_query,
)

logger = logging.getLogger("drishti.reco.marketplace")


# ── Occasion → clothing category mapping ──
_OCCASION_CATEGORIES = {
    "casual": ["tshirt", "casual shirt", "jeans"],
    "casual brunch": ["casual shirt", "polo tshirt"],
    "work": ["formal shirt", "formal trousers"],
    "office": ["formal shirt", "blazer"],
    "wedding": ["kurta", "nehru jacket"],
    "party": ["party shirt", "blazer"],
    "date": ["shirt", "jeans"],
    "gym": ["gym tshirt", "track pants"],
    "beach": ["beach shorts", "flip flops"],
    "festival": ["kurta", "ethnic jacket"],
    "interview": ["formal shirt", "formal shoes"],
    "travel": ["tshirt", "travel jeans"],
    "college": ["tshirt", "jeans casual"],
    "outing": ["tshirt", "casual shirt"],
    "brunch": ["casual shirt", "chinos"],
    "dinner": ["formal shirt", "trousers"],
    "concert": ["oversized tshirt", "streetwear"],
    "sport": ["sports tshirt", "shorts"],
    "festive": ["kurta", "ethnic wear"],
    "puja": ["kurta traditional"],
    "haldi": ["yellow kurta"],
    "sangeet": ["sherwani", "lehenga"],
}

# ── Style → search keyword mapping ──
_STYLE_KEYWORDS = {
    "minimalist": ["plain solid", "minimal"],
    "streetwear": ["oversized", "street style"],
    "indo-western": ["indo western", "fusion"],
    "corporate": ["formal", "office wear"],
    "athleisure": ["athleisure", "sporty"],
    "denimcore": ["denim", "jeans"],
    "resort": ["linen", "cotton", "breathable"],
    "techwear": ["tech wear", "utility"],
    "old money": ["classic", "polo", "chinos"],
    "y2k": ["retro", "vintage"],
    "cyberpunk": ["futuristic", "neon"],
    "boho": ["bohemian", "floral"],
    "power suit": ["blazer", "formal"],
    "casual": ["casual", "everyday"],
    "ethnic": ["kurta", "ethnic"],
    "western": ["shirt", "jeans"],
    "preppy": ["polo", "chinos", "loafers"],
    "grunge": ["oversized", "flannel"],
    "hypebeast": ["brand", "limited edition"],
}

# ── Gender mapping ──
_GENDER_QUERY = {
    "male": "men",
    "female": "women",
    "unisex": "",
}

# ── Body type → fit recommendations ──
_BODY_FIT = {
    "athletic": "slim fit",
    "rectangle": "regular fit",
    "triangle": "regular fit",
    "inverted_triangle": "regular fit",
    "oval": "relaxed fit",
    "hourglass": "slim fit",
}

# ── Weather → fabric recommendations ──
_WEATHER_FABRIC = {
    "hot": ["cotton", "linen", "breathable"],
    "cold": ["wool", "fleece", "warm"],
    "rainy": ["quick dry", "water resistant"],
    "humid": ["cotton", "linen", "lightweight"],
    "mild": ["cotton", "blend"],
}


def _build_search_queries(
    occasion: str = "",
    style: str = "",
    gender: str = "",
    body_shape: str = "",
    weather_condition: str = "",
    price_segment: str = "",
    city: str = "",
) -> list[str]:
    """
    Build 3-5 targeted search queries from user preferences.
    Each query targets a different clothing category for the occasion.
    """
    queries = []
    
    # Map gender to search suffix
    gender_suffix = _GENDER_QUERY.get(gender.lower(), "") if gender else ""
    
    # Get occasion categories (no longer gender-hardcoded)
    occ_lower = occasion.lower() if occasion else "casual"
    categories = _OCCASION_CATEGORIES.get(occ_lower, ["tshirt", "casual shirt"])
    
    # Get weather fabric
    fabric_kws = _WEATHER_FABRIC.get(weather_condition.lower(), []) if weather_condition else []
    
    # Build queries for each category
    for cat in categories[:3]:  # Max 3 queries
        parts = [cat, gender_suffix]
        
        # Add style modifier if not already in the category
        if style and style.lower() not in cat.lower():
            style_kw = _STYLE_KEYWORDS.get(style.lower(), [])
            if style_kw and style_kw[0] not in cat.lower():
                parts.insert(0, style_kw[0])
        
        # Add fabric for weather
        if fabric_kws and fabric_kws[0] not in cat.lower():
            parts.append(fabric_kws[0])
        
        query = " ".join(parts)
        queries.append(query)
    
    return queries


async def search_marketplace_products(
    occasion: str = "",
    style: str = "",
    gender: str = "",
    body_profile: dict = None,
    weather: dict = None,
    price_segment: str = "",
    city: str = "",
    max_per_platform: int = 10,
) -> list[dict]:
    """
    Search marketplaces directly for products matching user preferences.
    Returns products with live prices from all platforms.
    """
    body_shape = (body_profile or {}).get("body_shape", "")
    weather_condition = (weather or {}).get("condition", "")
    
    # Build search queries
    queries = _build_search_queries(
        occasion=occasion,
        style=style,
        gender=gender,
        body_shape=body_shape,
        weather_condition=weather_condition,
        price_segment=price_segment,
        city=city,
    )
    
    logger.info(f"Marketplace search queries: {queries}")
    
    # PRIMARY: Use Google Shopping for fast, multi-platform results
    all_products = []
    seen_ids = set()
    
    from api.services.price_scraper import search_google_shopping
    for query in queries[:2]:  # Top 2 queries
        gs_products = await search_google_shopping(query, 10)
        for product in gs_products:
            pid = f"{product['source']}:{product['product_id']}"
            if pid not in seen_ids:
                seen_ids.add(pid)
                all_products.append(product)
    
    # FALLBACK: If Google Shopping returned nothing, use individual scrapers
    if not all_products:
        for query in queries[:2]:
            tasks = [
                scrape_amazon(query, max_results=5),
                scrape_flipkart(query, max_results=5),
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, list):
                    for product in result:
                        pid = f"{product['source']}:{product['product_id']}"
                        if pid not in seen_ids:
                            seen_ids.add(pid)
                            all_products.append(product)
    
    # Sort by relevance score (rating + review count as proxy)
    for p in all_products:
        rating = p.get("rating", 0) or 0
        count = p.get("rating_count", 0) or 0
        # Logarithmic scale for review count
        import math
        log_count = math.log10(count + 1) * 10
        p["relevance_score"] = rating * 10 + log_count
    
    all_products.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
    
    logger.info(f"Total marketplace products found: {len(all_products)}")
    return all_products


def _format_product_for_reco(product: dict) -> dict:
    """Format a marketplace product into the recommendation format."""
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
    
    return {
        "product_id": product_id,
        "title": product.get("title", ""),
        "category": product.get("category", ""),
        "price": product.get("price", 0),
        "mrp": product.get("mrp", 0),
        "discount_pct": product.get("discount_pct", 0),
        "currency": "INR",
        "image_url": product.get("image_url", ""),
        "url": url,
        "score": product.get("relevance_score", 0) / 100,
        "source": source,
        "brand": product.get("brand", ""),
        "rating": product.get("rating", 0),
        "rating_count": product.get("rating_count", 0),
        "color": product.get("color", ""),
        "reason": f"Found on {source.title()} — ₹{product.get('price', 0)}",
    }


async def get_marketplace_recommendations(
    occasion: str = "",
    style: str = "",
    gender: str = "",
    body_profile: dict = None,
    weather: dict = None,
    price_segment: str = "",
    city: str = "",
    count: int = 12,
) -> dict:
    """
    Main entry point: Get outfit recommendations from ALL marketplaces.
    Returns products with live prices, ready for VTON and purchase.
    """
    products = await search_marketplace_products(
        occasion=occasion,
        style=style,
        gender=gender,
        body_profile=body_profile,
        weather=weather,
        price_segment=price_segment,
        city=city,
        max_per_platform=count // 3 + 2,
    )
    
    # Format for recommendation output
    recommendations = [_format_product_for_reco(p) for p in products[:count]]
    
    return {
        "recommendations": recommendations,
        "count": len(recommendations),
        "source": "marketplace_search",
        "platforms_searched": ["amazon", "myntra", "flipkart"],
    }
