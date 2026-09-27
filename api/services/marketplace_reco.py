"""
Marketplace-first recommendation engine.

Pipeline (see api/services/fashion_engine.py):
  INTENT → SLOT QUERIES → GOOGLE SHOPPING → ENRICH → HARD FILTER →
  MATCH SCORE → DIVERSITY SELECT → LABELLED PICKS

Google Shopping is the discovery layer; fashion intelligence lives in the engine.
"""
import asyncio
import logging
import os
import re

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
from api.services import fashion_rules as frules

logger = logging.getLogger("drishti.reco.marketplace")


def _slugify(title: str) -> str:
    """Lowercase dash slug for marketplace search paths (Myntra-style)."""
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s[:90]


def _resolve_product_url(product: dict) -> str:
    """Return a working shop URL for a product.

    Priority:
      1. Direct product URL from the scraper (never google search pages)
      2. Marketplace search URL in the platform's native format
         (Myntra needs dash slugs — plus-joined paths redirect into
         edge "Access Denied" pages)
      3. Myntra fashion search as a generic fallback (never google.com,
         which renders CAPTCHA walls on many networks)
    """
    from urllib.parse import quote_plus

    url = (product.get("url") or "").strip()
    if url and "google.com/search" not in url:
        return url

    title = product.get("title", "")
    source = (product.get("source") or "").lower()
    query = quote_plus(title)
    slug = _slugify(title)

    if source == "amazon":
        return f"https://www.amazon.in/s?k={query}"
    if source == "flipkart":
        return f"https://www.flipkart.com/search?q={query}"
    if source == "myntra":
        return f"https://www.myntra.com/{slug}" if slug else f"https://www.myntra.com/search?rawQuery={query}"
    if source == "ajio":
        return f"https://www.ajio.com/search/?text={query}"
    if source in ("nykaaman", "nykaa", "nykaafashion"):
        return f"https://www.nykaafashion.com/search?q={query}"
    if source == "meesho":
        return f"https://www.meesho.com/search?q={query}"
    if source == "jiomart":
        return f"https://www.jiomart.com/search/{query}"
    if source == "tatacliq":
        return f"https://www.tatacliq.com/search/?searchCategory=all&text={query}"
    if source == "snapdeal":
        return f"https://www.snapdeal.com/search?keyword={query}"

    # Unknown/brand source: fashion search on Myntra (works reliably)
    return f"https://www.myntra.com/{slug}" if slug else f"https://www.myntra.com/search?rawQuery={query}"


def _format_product_for_reco(product: dict) -> dict:
    """Format an enriched marketplace product into the recommendation contract."""
    source = product.get("source", "")
    product_id = product.get("product_id", "")

    url = _resolve_product_url(product)

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
    """Direct marketplace scrapers when Google Shopping under-delivers.

    Each marketplace gets its own deadline — one hung Flipkart retry must not
    cancel Amazon/Myntra results that already finished (that emptied the pool
    and sent office traffic to the random Shopify hoodie fallback).
    """
    per_scraper = float(os.getenv("SCRAPER_PER_TIMEOUT", "6"))

    async def _one(coro) -> list[dict]:
        try:
            return await asyncio.wait_for(coro, timeout=per_scraper)
        except Exception as exc:
            logger.debug(f"Scraper partial timeout/err for '{query[:50]}': {exc}")
            return []

    results = await asyncio.gather(
        _one(scrape_amazon(query, max_results=5)),
        _one(scrape_flipkart(query, max_results=5)),
        _one(scrape_myntra(query, max_results=5)),
    )
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
    prefs: dict | None = None,
    user_id: str | None = None,
) -> dict:
    """Search marketplaces → enrich → price intelligence → scored selection."""
    # Soft preference budget fallback when request didn't send one
    if prefs and budget_min is None and budget_max is None:
        if prefs.get("budget_min") is not None:
            budget_min = prefs["budget_min"]
        if prefs.get("budget_max") is not None:
            budget_max = prefs["budget_max"]

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
        user_prefs=prefs,
    )
    intent["body_shape"] = (body_profile or {}).get("body_shape", "")
    if user_id:
        intent["user_id"] = user_id
    if prefs:
        intent["user_prefs"] = prefs
        # Extend palette with DNA colours when present
        dna_colors = (prefs.get("dna") or {}).get("colours") or prefs.get("colors") or []
        if isinstance(dna_colors, list):
            intent["palette"] = set(intent.get("palette") or set()) | {str(c).lower() for c in dna_colors}
        # Request-level style multi-select: prefer first non-empty as style terms source
        if not intent.get("style_query_terms") and prefs.get("styles"):
            first_style = prefs["styles"][0] if isinstance(prefs.get("styles"), list) else ""
            if first_style:
                intent["style_query_terms"] = frules.style_query_terms(first_style)

    # Outfit blueprint → slot queries (occasions-aware, with exclusions)
    blueprints: list[dict] = []
    try:
        blueprints = frules.build_outfit_blueprints(intent, n=3)
        intent["blueprints"] = [
            {"id": b["id"], "label": b["label"], "variant": b["variant"],
             "archetype": b["archetype"], "dress_code": b["dress_code"],
             "why": frules.explain_blueprint(b, intent)}
            for b in blueprints
        ]
    except Exception as exc:
        logger.warning(f"Blueprint generation failed: {exc}")

    slot_queries = build_slot_queries(intent, count=count)
    # New users (no user_id) / thin count → simple query set so first results
    # land fast: one blueprint's primary+bottom+shoes is enough to show a look.
    if not user_id and count <= 6:
        slot_queries = slot_queries[:3]
    logger.info(f"Slot queries: {slot_queries} (budget mode: {intent.get('budget_mode')})")

    products = await _search_google_shopping_slots(slot_queries)

    if len(products) < max(6, count):
        logger.info(f"Google Shopping under-delivered ({len(products)}), using slot-aware scraper fallback")
        by_slot: dict[str, int] = {}
        for p in products:
            s = p.get("query_slot") or "primary"
            by_slot[s] = by_slot.get(s, 0) + 1
        seen = {(p.get("source"), p.get("product_id")) for p in products}
        targets = {"primary": 4, "bottom": 2, "shoes": 2, "accessory": 1}
        slots_in_queries = {sq[0] for sq in slot_queries}

        # Parallel fill: all under-target slot scrapers at once (was sequential —
        # 4 queries × ~4–15s each made first paint take ~60s for new users).
        pending = [
            (slot, query)
            for slot, query in slot_queries
            if by_slot.get(slot, 0) < targets.get(slot, 2)
        ]
        # Hard deadline so a hung marketplace can't block first paint.
        # New users get a tighter budget — show something good, fast.
        # Per-scraper timeout is 6s inside _scraper_fallback, so 10/15s here
        # only guards the outer merge (not the first thing that hangs).
        deadline = float(os.getenv("SCRAPER_TOTAL_TIMEOUT", "0")) or (
            10.0 if not user_id else 15.0
        )
        tasks = [asyncio.create_task(_scraper_fallback(q)) for _, q in pending]
        if tasks:
            done, _still = await asyncio.wait(tasks, timeout=deadline)
            fetched = []
            for t in tasks:
                if t.done() and not t.cancelled():
                    fetched.append(t.exception() or t.result())
                else:
                    t.cancel()
                    fetched.append(asyncio.TimeoutError(f"scraper exceeded {deadline}s"))
        else:
            fetched = []

        # Merge bottom → shoes → primary so scarce slots claim inventory first
        slot_priority = {"bottom": 0, "shoes": 1, "primary": 2, "accessory": 3}
        merged: list[tuple[str, list[dict]]] = []
        for (slot, query), result in zip(pending, fetched):
            if isinstance(result, Exception):
                logger.warning(f"Scraper fallback '{slot}' ('{query}') failed: {result}")
                continue
            merged.append((slot, result))
        merged.sort(key=lambda item: slot_priority.get(item[0], 9))

        for slot, fallback in merged:
            target = targets.get(slot, 2)
            max_add = max(target, 3) if slot != "primary" else 6
            added = 0
            for p in fallback:
                if by_slot.get(slot, 0) >= target and slot != "primary":
                    break
                key = (p.get("source"), p.get("product_id"))
                if key in seen:
                    continue
                seen.add(key)
                p.setdefault("query_slot", slot)
                products.append(p)
                by_slot[slot] = by_slot.get(slot, 0) + 1
                added += 1
                if added >= max_add:
                    break
        logger.info(f"Slot-aware fallback done: by_slot={by_slot}, total={len(products)}")

    for p in products:
        enrich_product(p)

    result = recommend_products(products, intent, count)

    # Coverage guarantee: one flaky scrape draw can deliver zero usable body
    # garments (slot fill has nothing to pull from) → outfit ships without a
    # top or bottom. One targeted re-scrape of the missing body slot(s) +
    # thin-count top-up, then re-run the pipeline. A `full` outfit (dress,
    # saree, kurta set) covers BOTH top and bottom (RECO-v4.1 contract) —
    # never re-scrape bottoms for a full-outfit selection.
    def _missing_body_slots(sel: list[dict]) -> list[str]:
        slots = {(p.get("fashion") or {}).get("slot", "") for p in sel}
        missing = []
        if not (slots & {"top", "full"}):
            missing.append("primary")
        if not (slots & {"bottom", "full"}):
            missing.append("bottom")
        return missing

    missing = _missing_body_slots(result.get("selected") or [])
    # Slots complete but the list is thin (e.g. full+shoes = 2 of 6): one
    # extra primary draw for variety. Bounded to a single retry either way.
    if not missing and len(result.get("selected") or []) < min(count, 4):
        missing.append("primary")
    if missing:
        want = set(missing)
        # ALL queries for the missing slots — a dict keyed by slot would keep
        # only the last primary query, starving the re-scrape of the others.
        retry = [(s, q) for s, q in slot_queries if s in want]
        if retry:
            logger.info(f"Slot coverage retry: re-scraping {sorted(missing)}")
            done = await asyncio.gather(
                *[_scraper_fallback(q) for _, q in retry],
                return_exceptions=True,
            )
            existing = {(p.get("source"), p.get("product_id")) for p in products}
            fresh: list[dict] = []
            for (slot, _q), res in zip(retry, done):
                if isinstance(res, Exception):
                    logger.warning(f"Coverage retry '{slot}' failed: {res}")
                    continue
                added = 0
                for p in res:
                    key = (p.get("source"), p.get("product_id"))
                    if key in existing:
                        continue
                    existing.add(key)
                    p.setdefault("query_slot", slot)
                    products.append(p)
                    fresh.append(p)
                    added += 1
                    if added >= 3:
                        break
            if fresh:
                logger.info(f"Slot coverage retry added {len(fresh)} items")
                for p in fresh:
                    enrich_product(p)
                result = recommend_products(products, intent, count)
                result["intent"] = intent
                result["blueprints"] = intent.get("blueprints", [])
                still = _missing_body_slots(result.get("selected") or [])
                if still:
                    logger.warning(f"Slot coverage retry still missing {sorted(still)}")

    result["intent"] = intent
    result["blueprints"] = intent.get("blueprints", [])
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
    prefs: dict | None = None,
    user_id: str | None = None,
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
        prefs=prefs,
        user_id=user_id,
    )

    selected = result.get("selected", [])
    band = result.get("band")
    dist = result.get("distribution", {})
    candidates = result.get("candidates") or selected
    intent = result.get("intent", {})
    validation = result.get("validation") or intent.get("outfit_validation") or {}
    blueprints = result.get("blueprints") or intent.get("blueprints") or []

    recommendations = [_format_product_for_reco(p) for p in selected[:count]]

    brand_pool = candidates if len(candidates) >= 10 else selected
    ranked_brands = rank_brands(brand_pool, band=band, limit=8)

    return {
        "recommendations": recommendations,
        "count": len(recommendations),
        "source": "fashion_engine",
        "platforms_searched": ["google_shopping", "amazon", "myntra", "flipkart"],
        "looks": blueprints,
        "outfit_validation": {
            "passed": validation.get("passed", True),
            "confidence": validation.get("confidence", 0.5),
            "level": validation.get("level", "medium"),
            "dress_code": validation.get("dress_code") or intent.get("dress_code", ""),
            "violations": validation.get("violations", []),
            "scores": validation.get("scores", {}),
        },
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
