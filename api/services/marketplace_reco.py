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
import time
import uuid

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
from api.services import look_assembly

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
    blueprint_map: dict[str, str] | None = None,
    per_query: int = 20,
    stats: dict | None = None,
) -> list[dict]:
    """Run slot-labelled Google Shopping searches in parallel.

    Results are fetched unfiltered (raw pool cached per query) — the price
    intelligence layer resolves the band against the live distribution.
    RECO-v4.5: identical query texts fetch once (a duplicate in-flight call
    bypasses the cache and burns SerpApi quota) and all counters land in
    `stats` for request diagnostics.
    """
    from api.services.price_scraper import search_google_shopping

    stats = stats if stats is not None else {}
    unique: list[tuple[str, str]] = []
    seen_q: set[str] = set()
    for slot, query in slot_queries:
        if query in seen_q:
            continue
        seen_q.add(query)
        unique.append((slot, query))
    stats["queries_total"] = stats.get("queries_total", 0) + len(slot_queries)
    stats["queries_unique"] = stats.get("queries_unique", 0) + len(unique)

    tasks = [search_google_shopping(query, per_query, stats=stats)
             for _, query in unique]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    products: list[dict] = []
    for (slot, query), result in zip(unique, results):
        if isinstance(result, Exception):
            logger.warning(f"Google Shopping slot '{slot}' ('{query}') failed: {result}")
            continue
        bp_id = (blueprint_map or {}).get(query.lower(), "")
        for p in result:
            p["query_slot"] = slot
            if bp_id:
                p["blueprint_id"] = bp_id
            products.append(p)
    return products


async def _scraper_fallback(query: str, stats: dict | None = None) -> list[dict]:
    """Direct marketplace scrapers when Google Shopping under-delivers.

    Each marketplace gets its own deadline — one hung Flipkart retry must not
    cancel Amazon/Myntra results that already finished (that emptied the pool
    and sent office traffic to the random Shopify hoodie fallback).
    """
    per_scraper = float(os.getenv("SCRAPER_PER_TIMEOUT", "6"))
    if stats is not None:
        stats["fallback_calls"] = stats.get("fallback_calls", 0) + 1

    async def _one(coro) -> list[dict]:
        try:
            return await asyncio.wait_for(coro, timeout=per_scraper)
        except Exception as exc:
            if stats is not None:
                stats["fallback_errors"] = stats.get("fallback_errors", 0) + 1
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
    if stats is not None:
        stats["fallback_products"] = stats.get("fallback_products", 0) + len(products)
    return products


def _cap_queries_interleaved(
    slot_queries: list[tuple[str, str]],
    blueprint_map: dict[str, str],
    cap: int = 4,
) -> list[tuple[str, str]]:
    """RECO-v4.5 — new-user query budget that preserves coverage.

    A blind prefix cut (`[:4]`) kept one blueprint's primary+bottom+shoes
    plus a second blueprint's BOTTOM while chopping every other archetype's
    primary — indian office drew a single-kurta pool and shipped 1 look.
    Keep the first blueprint's complete slot set (one full look needs it),
    then spend the remaining budget on a PRIMARY from each other blueprint
    before any non-primary query (round-robin afterwards). Garment families
    therefore span the queries instead of crowding into one.
    """
    if len(slot_queries) <= cap:
        return slot_queries
    if not blueprint_map:
        return slot_queries[:cap]

    groups: dict[str, list[tuple[str, str]]] = {}
    order: list[str] = []
    for sq in slot_queries:
        bp = blueprint_map.get(sq[1].lower()) or f"_ungrouped{len(order)}"
        if bp not in groups:
            groups[bp] = []
            order.append(bp)
        groups[bp].append(sq)

    picked: list[tuple[str, str]] = list(groups[order[0]][:cap])
    remaining = [groups[g] for g in order[1:] if groups[g]]

    # One primary per remaining blueprint (blueprint order) — a later
    # blueprint's top beats an earlier blueprint's second bottom.
    for g in remaining:
        if len(picked) >= cap:
            break
        prim = next(((s, q) for s, q in g if s == "primary"), None)
        if prim:
            picked.append(prim)
            g.remove(prim)
    # Round-robin whatever slots are left.
    while len(picked) < cap and any(remaining):
        for g in remaining:
            if g and len(picked) < cap:
                picked.append(g.pop(0))
    return picked[:cap]


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
    variation_parent: dict | None = None,
    exclude_product_ids: list | None = None,
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

    # ── Outfit blueprint → slot queries (occasions-aware, with exclusions)
    t0 = time.perf_counter()
    stages: dict[str, float] = {}
    search_stats: dict = {}
    blueprints: list[dict] = []
    try:
        if variation_parent:
            # See Variations: clone the parent archetype instead of exploring
            blueprints = look_assembly.build_variation_blueprints(
                intent, variation_parent.get("archetype_id", ""), n=4)
        else:
            blueprints = frules.build_outfit_blueprints(intent, n=4)
        intent["blueprints"] = [
            {"id": b["id"], "label": b["label"], "variant": b["variant"],
             "archetype": b["archetype"], "dress_code": b["dress_code"],
             "components": b.get("components", []),
             "is_variation": bool(b.get("is_variation")),
             "why": frules.explain_blueprint(b, intent)}
            for b in blueprints
        ]
    except Exception as exc:
        logger.warning(f"Blueprint generation failed: {exc}")

    if variation_parent:
        all_slot_queries = look_assembly.compile_variation_queries(
            blueprints, intent, max_queries=9)
        slot_queries = all_slot_queries
        if not user_id and len(slot_queries) > 5:
            slot_queries = _cap_queries_interleaved(
                slot_queries, intent.get("blueprint_query_map") or {}, cap=5)
    else:
        all_slot_queries = build_slot_queries(intent, count=count, max_queries=8)
        slot_queries = all_slot_queries
        # New users (no user_id) / thin count → smaller query budget so
        # first results land fast — but the cap KEEPS a primary per
        # blueprint family (RECO-v4.5): a prefix cut shipped a single-
        # garment-family pool and 1 look for indian office.
        if not user_id and count <= 6:
            slot_queries = _cap_queries_interleaved(
                slot_queries, intent.get("blueprint_query_map") or {}, cap=4)
    blueprint_map: dict[str, str] = intent.get("blueprint_query_map") or {}
    fetched_queries = {q for _, q in slot_queries}
    stages["intent_queries_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    logger.info(f"Slot queries: {slot_queries} (budget mode: {intent.get('budget_mode')})")

    t = time.perf_counter()
    products = await _search_google_shopping_slots(
        slot_queries, blueprint_map, stats=search_stats)
    stages["google_ms"] = round((time.perf_counter() - t) * 1000, 1)

    t_stage = time.perf_counter()
    if len(products) < max(6, count):
        logger.info(f"Google Shopping under-delivered ({len(products)}), using slot-aware scraper fallback")
        by_slot: dict[str, int] = {}
        for p in products:
            s = p.get("query_slot") or "primary"
            by_slot[s] = by_slot.get(s, 0) + 1
        seen = {(p.get("source"), p.get("product_id")) for p in products}
        # RECO-v4.5 — fallback budgets scale with look demand (was flat 2/2):
        # with google under-delivering, two bottoms in the whole pool meant
        # only two complete looks could ever assemble. Zero extra network —
        # the scrapers already return up to 5 items per query; this only
        # stops the merge from truncating scarce slots.
        looks_demand = min(4, max(count, 2))
        targets = {"primary": max(4, looks_demand),
                   "bottom": looks_demand, "shoes": looks_demand,
                   "accessory": 1}
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
        tasks = [asyncio.create_task(_scraper_fallback(q, search_stats))
                 for _, q in pending]
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

        # RECO-v4.5 — round-robin interleave (was: bottom→shoes→primary
        # batch order, which let ONE primary query's ethnic tops fill the
        # pool before a shirt query contributed a single item). Scarce slots
        # still claim first by priority, but within the merge each pending
        # query contributes one item per pass — every garment family lands
        # before any family gets seconds.
        slot_priority = {"bottom": 0, "shoes": 1, "primary": 2, "accessory": 3}
        merged: list[tuple[str, str, list[dict]]] = []
        for (slot, query), result in zip(pending, fetched):
            if isinstance(result, Exception):
                logger.warning(f"Scraper fallback '{slot}' ('{query}') failed: {result}")
                continue
            merged.append((slot, query, result))
        merged.sort(key=lambda item: slot_priority.get(item[0], 9))

        cursors = [0] * len(merged)
        per_query_added = [0] * len(merged)

        def _take_one(i: int) -> bool:
            slot, query, items = merged[i]
            target = targets.get(slot, 2)
            max_add = max(target, 3) if slot != "primary" else 6
            if per_query_added[i] >= max_add:
                return False
            if by_slot.get(slot, 0) >= target and slot != "primary":
                return False
            while cursors[i] < len(items):
                p = items[cursors[i]]
                cursors[i] += 1
                key = (p.get("source"), p.get("product_id"))
                if key in seen:
                    continue
                seen.add(key)
                bp_id = blueprint_map.get(query.lower(), "")
                p.setdefault("query_slot", slot)
                if bp_id:
                    p["blueprint_id"] = bp_id
                products.append(p)
                by_slot[slot] = by_slot.get(slot, 0) + 1
                per_query_added[i] += 1
                return True
            return False

        while True:
            pass_added = False
            for i in range(len(merged)):
                if _take_one(i):
                    pass_added = True
            if not pass_added:
                break
        logger.info(f"Slot-aware fallback done: by_slot={by_slot}, total={len(products)}")
    stages["fallback_ms"] = round((time.perf_counter() - t_stage) * 1000, 1)

    t_stage = time.perf_counter()
    for p in products:
        enrich_product(p)
    stages["enrich_ms"] = round((time.perf_counter() - t_stage) * 1000, 1)

    # Variation history: never re-serve products already shown this session.
    ex_keys = {
        (str(x.get("source", "")), str(x.get("product_id", "")))
        for x in (exclude_product_ids or []) if isinstance(x, dict)
    }
    ex_id_strs = {
        str(x) for x in (exclude_product_ids or []) if not isinstance(x, dict)
    }
    if ex_keys or ex_id_strs:
        products = [p for p in products
                    if (p.get("source", ""), p.get("product_id")) not in ex_keys
                    and str(p.get("product_id") or "") not in ex_id_strs]

    t_stage = time.perf_counter()
    result = recommend_products(products, intent, count)
    stages["filter_score_ms"] = round((time.perf_counter() - t_stage) * 1000, 1)

    # ── RECO-v4.5 gate-aware retrieval top-up ────────────────────────────
    # Google can clear the RAW threshold while gate attrition leaves too few
    # VALID candidates (live: indian office → 20 raw ethnic tops → 3 valid →
    # 1 look). Before accepting the thin pool, spend ONE bounded round on the
    # queries the new-user budget cut (shirt/blazer families never fetched).
    valid_n = len(result.get("candidates") or result.get("selected") or [])
    if valid_n < max(6, count):
        unused = [(s, q) for s, q in all_slot_queries
                  if q not in fetched_queries][:4]
        if unused:
            logger.info(
                f"Valid pool thin ({valid_n} < {max(6, count)}) — top-up "
                f"round over {len(unused)} unfetched blueprint queries")
            search_stats["topup_queries"] = (
                search_stats.get("topup_queries", 0) + len(unused))
            deadline = float(os.getenv("SCRAPER_TOPUP_TIMEOUT", "0")) or (
                8.0 if not user_id else 12.0
            )
            t_stage = time.perf_counter()
            tasks = [asyncio.create_task(_scraper_fallback(q, search_stats))
                     for _, q in unused]
            done, _still = await asyncio.wait(tasks, timeout=deadline)
            fetched = []
            for tk in tasks:
                if tk.done() and not tk.cancelled():
                    fetched.append(tk.exception() or tk.result())
                else:
                    tk.cancel()
                    fetched.append(asyncio.TimeoutError(
                        f"topup exceeded {deadline}s"))
            stages["topup_ms"] = round((time.perf_counter() - t_stage) * 1000, 1)

            existing = {(p.get("source", ""), p.get("product_id", ""))
                        for p in products} | ex_keys
            fresh: list[dict] = []
            for (slot, q), res in zip(unused, fetched):
                if isinstance(res, Exception):
                    logger.warning(f"Top-up '{slot}' failed: {res}")
                    continue
                bp_id = blueprint_map.get(q.lower(), "")
                added = 0
                for p in res:
                    key = (p.get("source"), p.get("product_id"))
                    if (key in existing or key in ex_keys
                            or str(p.get("product_id") or "") in ex_id_strs):
                        continue
                    existing.add(key)
                    p.setdefault("query_slot", slot)
                    if bp_id:
                        p["blueprint_id"] = bp_id
                    products.append(p)
                    fresh.append(p)
                    added += 1
                    if added >= 4:
                        break
            if fresh:
                search_stats["topup_products"] = (
                    search_stats.get("topup_products", 0) + len(fresh))
                for p in fresh:
                    enrich_product(p)
                t_stage = time.perf_counter()
                result = recommend_products(products, intent, count)
                stages["filter_score_ms"] = round(
                    stages.get("filter_score_ms", 0)
                    + (time.perf_counter() - t_stage) * 1000, 1)
                logger.info(f"Top-up added {len(fresh)} items → "
                            f"{len(result.get('candidates') or [])} valid")

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
        # RECO-v4.5 — flaky scrapers can deliver bodies but zero footwear;
        # a draw with NO shoes anywhere in the pool can only assemble
        # incomplete looks, so the shoes query gets its ONE bounded
        # re-scrape (pool-based: a low-scored shoe in `selected` is fine —
        # assembly scans all candidates).
        if not (slots & {"shoes"}) and not any(
                (p.get("fashion") or {}).get("slot") == "shoes"
                for p in products):
            missing.append("shoes")
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
            search_stats["coverage_retry_queries"] = (
                search_stats.get("coverage_retry_queries", 0) + len(retry))
            t_stage = time.perf_counter()
            done = await asyncio.gather(
                *[_scraper_fallback(q, search_stats) for _, q in retry],
                return_exceptions=True,
            )
            existing = {(p.get("source"), p.get("product_id")) for p in products}
            fresh: list[dict] = []
            for (slot, q), res in zip(retry, done):
                if isinstance(res, Exception):
                    logger.warning(f"Coverage retry '{slot}' failed: {res}")
                    continue
                bp_id = blueprint_map.get(q.lower(), "")
                added = 0
                for p in res:
                    key = (p.get("source"), p.get("product_id"))
                    if (key in existing or key in ex_keys
                            or str(p.get("product_id") or "") in ex_id_strs):
                        continue
                    existing.add(key)
                    p.setdefault("query_slot", slot)
                    if bp_id:
                        p["blueprint_id"] = bp_id
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
            stages["coverage_retry_ms"] = round(
                (time.perf_counter() - t_stage) * 1000, 1)

    stages["total_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    search_stats["pool_size"] = len(products)
    search_stats["valid_candidates"] = len(result.get("candidates") or [])
    result["intent"] = intent
    result["blueprints"] = intent.get("blueprints", [])
    result["search_stats"] = search_stats
    result["stage_ms"] = stages
    return result


def _format_look(look: dict) -> dict:
    """Assembled look → API contract (keeps legacy `id`/`archetype`/`why`
    keys used by the impression event, adds products for look-accurate UI)."""
    out = {
        "id": look.get("id", ""),
        "outfit_id": look.get("outfit_id", ""),
        "archetype_id": look.get("archetype_id", ""),
        "blueprint_id": look.get("blueprint_id", ""),
        "variant": look.get("variant", "safe"),
        "label": look.get("label", ""),
        "why": look.get("why", ""),
        "occasion": look.get("occasion", ""),
        "dress_code": look.get("dress_code", ""),
        "score": look.get("score", 0.0),
        "is_variation": look.get("is_variation", False),
        "variation_index": look.get("variation_index", -1),
        "signature": look.get("signature", {}),
        "is_complete": bool(look.get("is_complete", True)),
        "missing_slots": list(look.get("missing_slots") or []),
    }
    out["products"] = [_format_product_for_reco(p) for p in look.get("products", [])]
    out["total_price"] = sum(p.get("price") or 0 for p in out["products"])
    return out


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
    variation_parent: dict | None = None,
    exclude_product_ids: list | None = None,
    exclude_titles: list | None = None,
) -> dict:
    """Main entry point: recommendations + budget intelligence + ranked brands."""
    from api.services.price_intelligence import (
        band_label, distribution_by_slot, quick_picks, rank_brands,
    )
    from api.services.reco_version import RECO_VERSION

    t_entry = time.perf_counter()
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
        variation_parent=variation_parent,
        exclude_product_ids=exclude_product_ids,
    )

    selected = result.get("selected", [])
    band = result.get("band")
    dist = result.get("distribution", {})
    candidates = result.get("candidates") or selected
    intent = result.get("intent", {})
    validation = result.get("validation") or intent.get("outfit_validation") or {}
    blueprints = result.get("blueprints") or intent.get("blueprints") or []

    # ── Look assembly layer (RECO-v4.4) ────────────────────────────────
    # Four structurally distinct outfits from the scored pool; products are
    # unique across looks and duplicate skeletons are dropped (never forced).
    assembly_stats: dict = {}
    t_assemble = time.perf_counter()
    assembled = look_assembly.assemble_looks(
        candidates,
        blueprints,
        intent,
        count=4,
        exclude_product_ids=exclude_product_ids,
        exclude_titles=exclude_titles,
        enforce_diversity=not bool(variation_parent),
        stats_out=assembly_stats,
    )
    looks = [_format_look(lk) for lk in assembled]
    assemble_ms = round((time.perf_counter() - t_assemble) * 1000, 1)

    if looks:
        # Flat contract (backward compat) = union of look products in look
        # order: the legacy hero/round-robin UI then picks distinct heroes.
        seen_rp: set = set()
        recommendations = []
        for lk in looks:
            for fp in lk["products"]:
                key = (fp.get("source", ""), fp.get("product_id", ""))
                if key in seen_rp:
                    continue
                seen_rp.add(key)
                recommendations.append(fp)
        recommendations = recommendations[:max(count, 12)]
    else:
        recommendations = [_format_product_for_reco(p) for p in selected[:count]]

    brand_pool = candidates if len(candidates) >= 10 else selected
    ranked_brands = rank_brands(brand_pool, band=band, limit=8)

    # ── RECO-v4.5 request diagnostics ───────────────────────────────────
    # P50/P95/P99 aggregation happens client-side (bench/matrix) over these
    # per-stage timings — profile before touching any latency knob.
    stage_ms = dict(result.get("stage_ms") or {})
    for k in ("fallback_ms", "topup_ms", "coverage_retry_ms", "enrich_ms",
              "filter_score_ms", "google_ms", "intent_queries_ms"):
        stage_ms.setdefault(k, 0.0)
    stage_ms["assemble_ms"] = assemble_ms
    stage_ms["search_total_ms"] = stage_ms.get("total_ms", 0.0)
    stage_ms["total_ms"] = round((time.perf_counter() - t_entry) * 1000, 1)
    reco_diagnostics = {
        "request_id": uuid.uuid4().hex[:12],
        "reco_version": RECO_VERSION,
        "stage_ms": stage_ms,
        "search": dict(result.get("search_stats") or {}),
        "counts": {
            "pool_size": (result.get("search_stats") or {}).get("pool_size", 0),
            "candidates": len(candidates),
            "selected": len(selected),
            "looks": len(looks),
            "complete_looks": sum(1 for lk in looks if lk.get("is_complete")),
            "blueprints": len(blueprints),
            "duplicate_skeletons_removed": assembly_stats.get("skeleton_dropped", 0),
            "incomplete_dropped": assembly_stats.get("incomplete_dropped", 0),
        },
    }

    return {
        "recommendations": recommendations,
        "count": len(recommendations),
        "source": "fashion_engine",
        "platforms_searched": ["google_shopping", "amazon", "myntra", "flipkart"],
        "looks": looks or blueprints,
        "reco_diagnostics": reco_diagnostics,
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
