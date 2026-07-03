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
    "casual": ["tshirt", "shirt", "jeans", "shorts", "sneakers"],
    "casual brunch": ["tshirt", "shirt", "casual shoes"],
    "work": ["formal shirt", "trousers", "formal shoes"],
    "office": ["formal shirt", "trousers", "blazer"],
    "wedding": ["kurta", "sherwani", "nehru jacket", "mojari"],
    "party": ["shirt", "blazer", "formal shoes"],
    "date": ["shirt", "jeans", "sneakers"],
    "gym": ["tank top", "track pants", "sneakers"],
    "beach": ["shorts", "flip flops", "sunglasses"],
    "festival": ["kurta", "ethnic jacket", "mojari"],
    "interview": ["formal shirt", "trousers", "formal shoes"],
    "travel": ["tshirt", "jeans", "sneakers"],
    "college": ["tshirt", "jeans", "sneakers"],
    "outing": ["tshirt", "jeans", "sneakers"],
    "brunch": ["shirt", "chinos", "loafers"],
    "dinner": ["shirt", "trousers", "formal shoes"],
    "concert": ["tshirt", "jeans", "sneakers"],
    "sport": ["tshirt", "shorts", "sneakers"],
    "festive": ["kurta", "ethnic jacket"],
    "puja": ["kurta", "dhoti"],
    "haldi": ["kurta", "yellow"],
    "sangeet": ["sherwani", "nehru jacket"],
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
    
    # Get occasion categories
    occ_lower = occasion.lower() if occasion else "casual"
    categories = _OCCASION_CATEGORIES.get(occ_lower, ["tshirt", "shirt"])
    
    # Get style keywords
    style_kw = _STYLE_KEYWORDS.get(style.lower(), [style]) if style else []
    
    # Get gender prefix
    gender_prefix = _GENDER_QUERY.get(gender.lower(), "") if gender else ""
    
    # Get fit recommendation
    fit = _BODY_FIT.get(body_shape.lower(), "") if body_shape else ""
    
    # Get weather fabric
    fabric_kws = _WEATHER_FABRIC.get(weather_condition.lower(), []) if weather_condition else []
    
    # Build queries for each category
    for cat in categories[:4]:  # Max 4 queries
        parts = []
        
        # Style keyword first
        if style_kw:
            parts.append(style_kw[0])
        
        # Category
        parts.append(cat)
        
        # Gender
        if gender_prefix:
            parts.append(gender_prefix)
        
        # Fabric (from weather)
        if fabric_kws:
            parts.append(fabric_kws[0])
        
        # Fit
        if fit and cat in ["tshirt", "shirt", "jeans", "trousers"]:
            parts.append(fit)
        
        query = " ".join(parts)
        queries.append(query)
    
    # Add one gender-specific general query
    if gender_prefix:
        general = f"{style or 'casual'} clothing {gender_prefix}"
        if weather_condition:
            general += f" {fabric_kws[0] if fabric_kws else ''}"
        queries.append(general)
    
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
    
    # Search all platforms in parallel for each query
    all_products = []
    seen_ids = set()
    
    for query in queries[:3]:  # Top 3 queries to avoid rate limiting
        tasks = [
            scrape_amazon(query, max_results=max_per_platform),
            scrape_myntra(query, max_results=max_per_platform),
            scrape_flipkart(query, max_results=max_per_platform),
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
    
    # Build the right URL based on source
    if source == "amazon":
        url = f"https://www.amazon.in/dp/{product_id}"
    elif source == "myntra":
        url = product.get("url", "")
    elif source == "flipkart":
        url = product.get("url", "")
    else:
        url = product.get("url", "")
    
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
