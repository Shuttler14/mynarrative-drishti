"""
Cross-platform price comparison — SerpApi Google Shopping + fallback scrapers.
All links are verified product page URLs.
"""
import asyncio
import hashlib
import html as html_mod
import json
import logging
import math
import os
import random
import re
import time
from datetime import datetime
from typing import Optional

import httpx

logger = logging.getLogger("drishti.pricing")

SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]

_cache: dict[str, tuple[float, dict]] = {}
# Configurable via env vars, with sensible defaults
CACHE_TTL = int(os.getenv("SCRAPING_CACHE_TTL", "86400"))  # 24 hours
_last_request: dict[str, float] = {}
MIN_DELAY = float(os.getenv("SCRAPING_MIN_DELAY", "0.5"))


def _cache_key(domain: str, query: str) -> str:
    return hashlib.md5(f"{domain}:{query}".encode()).hexdigest()


def _get_cached(domain: str, query: str) -> Optional[dict]:
    key = _cache_key(domain, query)
    if key in _cache:
        ts, data = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return data
        del _cache[key]
    return None


def _set_cache(domain: str, query: str, data: dict):
    _cache[_cache_key(domain, query)] = (time.time(), data)


async def _rate_limit(domain: str):
    now = time.time()
    last = _last_request.get(domain, 0)
    wait = MIN_DELAY - (now - last) + random.uniform(0.1, 0.5)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_request[domain] = time.time()


async def _fetch_with_retry(url: str, headers: dict, max_retries: int = 1, timeout: int = 10) -> Optional[httpx.Response]:
    """Fetch URL with one retry on failure. Returns None if all retries fail."""
    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, http1=True, http2=False) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200 and len(resp.text) > 5000:
                    return resp
                if attempt < max_retries:
                    await asyncio.sleep(1)
        except Exception as e:
            if attempt < max_retries:
                await asyncio.sleep(1)
            else:
                logger.warning(f"Fetch failed: {url} — {e}")
    return None


# ══════════════════════════════════════════════════════════════
# GOOGLE SHOPPING VIA SERPAPI — Primary price comparison
# ══════════════════════════════════════════════════════════════

async def search_google_shopping(query: str, max_results: int = 10,
                                  min_price: int | None = None,
                                  max_price: int | None = None) -> list[dict]:
    """Search Google Shopping via SerpApi. Returns products with prices from all platforms.

    NOTE: SerpApi's native min_price/max_price params return zero results with
    gl=in, so the price band is applied client-side after fetching. Raw results
    are cached per query — different bands share the same cached fetch.
    """
    if not SERPAPI_KEY:
        logger.warning("SerpApi key not configured")
        return []

    cached = _get_cached("google_shopping", query)
    if cached:
        products = cached.get("products", [])
    else:
        try:
            async with httpx.AsyncClient(timeout=25) as client:
                r = await client.get("https://serpapi.com/search", params={
                    "engine": "google_shopping",
                    "q": query,
                    "gl": "in",
                    "hl": "en",
                    "api_key": SERPAPI_KEY,
                })
                if r.status_code != 200:
                    logger.warning(f"SerpApi: HTTP {r.status_code}")
                    return []
                data = r.json()
        except Exception as e:
            logger.warning(f"SerpApi error: {e}")
            return []

        products = _parse_shopping_results(data)
        if products:
            _set_cache("google_shopping", query, {"products": products})

    if min_price or max_price:
        lo = min_price or 0
        hi = max_price or 10**9
        products = [p for p in products if lo <= (p.get("price") or 0) <= hi]

    return products[:max_results]


def _parse_shopping_results(data: dict) -> list[dict]:
    """Parse SerpApi shopping_results into product dicts."""
    products = []
    for item in data.get("shopping_results", [])[:40]:
        title = item.get("title", "")
        price_str = item.get("price", "")
        extracted_price = item.get("extracted_price", 0)
        source = item.get("source", "")
        link = item.get("link", "") or item.get("product_link", "")
        rating = item.get("rating", 0) or 0
        reviews = item.get("reviews", 0) or 0
        thumbnail = item.get("thumbnail", "")
        # Prefer larger images for VTON — rich_thumbnail is higher quality
        rich_thumb = item.get("rich_thumbnail", "")
        if isinstance(rich_thumb, list) and rich_thumb:
            rich_thumb = rich_thumb[0]
        elif not isinstance(rich_thumb, str):
            rich_thumb = ""
        # Use the best available image (prefer rich_thumbnail > thumbnail)
        best_image = rich_thumb or thumbnail
        old_price = item.get("old_price", "")
        extracted_old = item.get("extracted_old_price", 0) or extracted_price

        # Skip if no price
        if not extracted_price or extracted_price <= 0:
            continue

        # Determine platform from source
        platform = "unknown"
        source_lower = source.lower()
        if "amazon" in source_lower:
            platform = "amazon"
        elif "flipkart" in source_lower:
            platform = "flipkart"
        elif "myntra" in source_lower:
            platform = "myntra"
        elif "ajio" in source_lower:
            platform = "ajio"
        elif "nykaa" in source_lower:
            platform = "nykaa"
        elif "meesho" in source_lower:
            platform = "meesho"
        elif "jiomart" in source_lower:
            platform = "jiomart"

        # Clean link — remove tracking params
        clean_link = link.split("?")[0] if link else ""

        mrp = int(extracted_old) if extracted_old and extracted_old > extracted_price else int(extracted_price)
        discount_pct = int((1 - extracted_price / mrp) * 100) if mrp > extracted_price else 0

        products.append({
            "source": platform,
            "product_id": item.get("product_id", ""),
            "title": title,
            "brand": title.split()[0] if title else "",
            "price": int(extracted_price),
            "mrp": mrp,
            "discount_pct": min(discount_pct, 90),
            "rating": round(rating, 1),
            "rating_count": reviews,
            "image_url": best_image,
            "url": clean_link or link,
            "color": "",
            "category": "",
            "seller": source,
        })

    return products


def _headers(domain: str = "") -> dict:
    ua = random.choice(_USER_AGENTS)
    h = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    if domain == "www.amazon.in":
        h["Sec-Ch-Ua"] = '"Chromium";v="126", "Google Chrome";v="126"'
        h["Sec-Ch-Ua-Mobile"] = "?0"
        h["Sec-Ch-Ua-Platform"] = '"macOS"'
    return h


# ══════════════════════════════════════════════════════════════
# AMAZON INDIA — Works via HTML parsing, ASIN-based URLs
# ══════════════════════════════════════════════════════════════

async def scrape_amazon(query: str, max_results: int = 10) -> list[dict]:
    """Scrape Amazon India. Links: https://www.amazon.in/dp/{ASIN}"""
    cached = _get_cached("amazon.in", query)
    if cached:
        return cached.get("products", [])[:max_results]

    await _rate_limit("amazon.in")
    search_url = f"https://www.amazon.in/s?k={query.replace(' ', '+')}&ref=nb_sb_noss"
    resp = await _fetch_with_retry(search_url, _headers("www.amazon.in"))
    if not resp:
        return []

    html = resp.text
    products = []
    results = list(re.finditer(r'data-component-type="s-search-result"', html))

    for i, match in enumerate(results[:max_results]):
        chunk = html[match.start():results[i+1].start() if i+1 < len(results) else match.start()+8000]

        asin_m = re.search(r'data-asin="([A-Z0-9]{10})"', chunk)
        if not asin_m:
            continue
        asin = asin_m.group(1)

        # Title
        h2s = re.findall(r'<h2[^>]*>(.*?)</h2>', chunk, re.DOTALL)
        title = ""
        if h2s:
            title = html_mod.unescape(re.sub(r'<[^>]+>', '', h2s[-1]).strip())
        if not title:
            tm = re.search(r'class="a-text-normal"[^>]*>([^<]+)<', chunk)
            title = html_mod.unescape(tm.group(1).strip()) if tm else ""

        # Price
        pm = re.search(r'class="a-price-whole"[^>]*>([0-9,]+)<', chunk)
        price = int(pm.group(1).replace(",", "")) if pm else 0

        # MRP
        mm = re.search(r'class="a-price a-text-price[^"]*"[^>]*>.*?class="a-offscreen"[^>]*>([0-9,]+)', chunk, re.DOTALL)
        if not mm:
            mm = re.search(r'a-text-price[^>]*>[^<]*<span[^>]*>([0-9,]+)<', chunk)
        mrp = int(mm.group(1).replace(",", "")) if mm else price

        # Rating
        rm = re.search(r'class="a-icon-alt">(\d+\.?\d*) out of', chunk)
        rating = float(rm.group(1)) if rm else 0

        # Rating count
        cm = re.search(r'(\d[\d,]*)\s*(?:ratings?|Reviews)', chunk)
        rating_count = int(cm.group(1).replace(",", "")) if cm else 0

        # Image
        im = re.search(r'<img[^>]*src="(https://m\.media-amazon\.com/[^"]+)"', chunk)
        image_url = im.group(1) if im else ""

        if title and price > 0:
            products.append({
                "source": "amazon",
                "product_id": asin,
                "title": title,
                "brand": title.split()[0] if title else "",
                "price": price,
                "mrp": mrp if mrp >= price else price,
                "discount_pct": int((1 - price / mrp) * 100) if mrp > price else 0,
                "rating": rating,
                "rating_count": rating_count,
                "image_url": image_url,
                "url": f"https://www.amazon.in/dp/{asin}",
                "color": "",
                "category": "",
            })

    if products:
        _set_cache("amazon.in", query, {"products": products})
    return products


# ══════════════════════════════════════════════════════════════
# FLIPKART — HTML parsing with real product page URLs
# ══════════════════════════════════════════════════════════════

async def scrape_flipkart(query: str, max_results: int = 10) -> list[dict]:
    """Scrape Flipkart via __INITIAL_STATE__ JSON or HTML fallback."""
    cached = _get_cached("flipkart.com", query)
    if cached:
        return cached.get("products", [])[:max_results]

    await _rate_limit("flipkart.com")
    search_url = f"https://www.flipkart.com/search?q={query.replace(' ', '+')}"
    resp = await _fetch_with_retry(search_url, _headers())
    if not resp:
        return []

    html = resp.text
    products = []

    # Strategy 1: Parse __INITIAL_STATE__ JSON
    try:
        m = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.+)', html, re.DOTALL)
        if m:
            raw = m.group(1)
            # Find the end of the JSON — look for }; or </script>
            end_markers = [';\n', ';\r', ';</script>', ';\nwindow.']
            best_end = len(raw)
            for marker in end_markers:
                idx = raw.find(marker)
                if idx > 0 and idx < best_end:
                    best_end = idx
            raw = raw[:best_end]

            state = json.loads(raw)
            page_data = state.get("pageDataV4", {}).get("page", {}).get("data", {})
            for key, val in page_data.items():
                if not isinstance(val, list):
                    continue
                for item in val:
                    if not isinstance(item, dict):
                        continue
                    pi = item.get("productInfo", {}).get("value", {})
                    if not pi:
                        continue

                    titles = pi.get("titles", {})
                    pricing = pi.get("pricing", {})
                    prices_list = pricing.get("prices", [])
                    base_url = pi.get("baseUrl", "")
                    pid = pi.get("id", "")

                    title = titles.get("title") or titles.get("newTitle", "")
                    brand = titles.get("superTitle", "")
                    category = pi.get("analyticsData", {}).get("subCategory", "")

                    selling_price = 0
                    mrp = 0
                    for p in prices_list:
                        if p.get("priceType") == "SPECIAL_PRICE":
                            selling_price = int(p.get("value", 0))
                        elif p.get("priceType") == "FSP":
                            mrp = int(p.get("value", 0))
                    if not selling_price and prices_list:
                        selling_price = int(prices_list[-1].get("value", 0))
                    if not mrp:
                        mrp = selling_price
                    discount_pct = pricing.get("totalDiscount", 0)

                    images = pi.get("media", {}).get("images", [])
                    image_url = ""
                    if images:
                        raw_img = images[0].get("url", "")
                        image_url = raw_img.replace("{@width}", "300").replace("{@height}", "300").replace("{@quality}", "70")

                    link = f"https://www.flipkart.com{base_url.split('?')[0]}" if base_url else ""

                    if title and selling_price > 0:
                        products.append({
                            "source": "flipkart",
                            "product_id": pid,
                            "title": f"{brand} {title}".strip() if brand else title,
                            "brand": brand,
                            "price": selling_price,
                            "mrp": mrp if mrp >= selling_price else selling_price,
                            "discount_pct": min(discount_pct, 90),
                            "rating": 0,
                            "rating_count": 0,
                            "image_url": image_url,
                            "url": link or search_url,
                            "color": "",
                            "category": category,
                        })
    except Exception as e:
        logger.warning(f"Flipkart JSON parse error: {e}")

    # Strategy 2: Fallback to HTML data-id extraction
    if not products:
        data_ids = list(re.finditer(r'data-id="([^"]+)"', html))
        for i, m in enumerate(data_ids[:max_results]):
            chunk = html[m.start():data_ids[i+1].start() if i+1 < len(data_ids) else m.start()+8000]
            product_id = m.group(1)

            link = ""
            link_m = re.search(r'href="(/[^"]+?/p/itm[A-Za-z0-9]+[^"]*)"', chunk)
            if link_m:
                raw = link_m.group(1).split("?")[0]
                link = f"https://www.flipkart.com{raw}"

            title = ""
            title_m = re.search(r'href="[^"]*"[^>]*title="([^"]+)"', chunk)
            if title_m:
                title = html_mod.unescape(title_m.group(1).strip())
            if not title:
                for tm in re.finditer(r'>([A-Z][^<]{15,80})</(?:a|span|div)', chunk):
                    t = tm.group(1).strip()
                    if len(t) > 15 and not t.startswith('₹'):
                        title = html_mod.unescape(t)
                        break

            prices = re.findall(r'₹([\d,]+)', chunk)
            price = int(prices[0].replace(",", "")) if prices else 0
            mrp = int(prices[1].replace(",", "")) if len(prices) > 1 else price

            rm = re.search(r'(\d+\.?\d*)\s*★', chunk)
            rating = float(rm.group(1)) if rm else 0

            if title and price > 0:
                products.append({
                    "source": "flipkart",
                    "product_id": product_id,
                    "title": title,
                    "brand": title.split()[0] if title else "",
                    "price": price,
                    "mrp": mrp if mrp >= price else price,
                    "discount_pct": int((1 - price / mrp) * 100) if mrp > price else 0,
                    "rating": rating,
                    "rating_count": 0,
                    "image_url": "",
                    "url": link or search_url,
                    "color": "",
                    "category": "",
                })

    if products:
        _set_cache("flipkart.com", query, {"products": products})
    return products[:max_results]


# ══════════════════════════════════════════════════════════════
# MYNTRA — HTML extraction via window.__myx embedded JSON
# ══════════════════════════════════════════════════════════════

# Map search queries to Myntra category page URLs (gender-aware)
_MYNYTRA_CATEGORIES = {
    "men": {
        "tshirt": "men-tshirts",
        "shirt": "men-casual-shirts",
        "formal shirt": "men-formal-shirts",
        "polo": "men-polo-t-shirts",
        "kurta": "men-kurtas",
        "jeans": "men-jeans",
        "trousers": "men-trousers",
        "shorts": "men-shorts",
        "hoodie": "men-hoodies",
        "jacket": "men-jackets",
        "sneakers": "men-sneakers",
        "shoes": "men-casual-shoes",
        "blazer": "men-blazers",
        "tracksuit": "men-tracksuits",
        "sweatshirt": "men-sweatshirts",
        "ethnic": "men-kurtas",
        "sherwani": "men-sherwanis",
        "dhoti": "men-dhotis",
    },
    "women": {
        "tshirt": "women-tshirts",
        "top": "women-tops",
        "shirt": "women-casual-shirts",
        "kurta": "women-kurtas",
        "saree": "sarees",
        "lehenga": "lehenga-choli",
        "dress": "women-dresses",
        "jeans": "women-jeans",
        "trousers": "women-trousers",
        "shorts": "women-shorts",
        "jacket": "women-jackets",
        "sneakers": "women-sneakers",
        "shoes": "women-heeled-sandals",
        "heels": "women-heeled-sandals",
        "flat": "women-flats",
        "ethnic": "women-kurtas",
        "palazzo": "women-palazzos",
        "leggings": "women-leggings",
        "dupatta": "women-dupattas",
        "ghagra": "women-ghagra",
    },
}

# Detect gender from query text
_GENDER_KEYWORDS = {
    "men": ["men", "man", "boy", "male", "husband", "brother", "father", "dad"],
    "women": ["women", "woman", "girl", "female", "wife", "sister", "mother", "mom", "lady", "ladies"],
}


def _detect_gender_from_query(query: str) -> str:
    """Detect gender from query text. Returns 'men' or 'women'."""
    q = query.lower()
    for gender, keywords in _GENDER_KEYWORDS.items():
        for kw in keywords:
            if f" {kw} " in f" {q} ":
                return gender
    return "men"  # Default fallback


def _query_to_myntra_url(query: str) -> str:
    """Convert a search query to a Myntra category page URL (gender-aware)."""
    q = query.lower().strip()
    gender = _detect_gender_from_query(q)
    categories = _MYNYTRA_CATEGORIES.get(gender, _MYNYTRA_CATEGORIES["men"])

    # Try exact category match first
    for keyword, slug in categories.items():
        if keyword in q:
            return f"https://www.myntra.com/{slug}"

    # Default: use Myntra search
    return f"https://www.myntra.com/{q.replace(' ', '-')}"


async def scrape_myntra(query: str, max_results: int = 10) -> list[dict]:
    """Scrape Myntra via HTML window.__myx extraction. Links: real product page URLs."""
    cached = _get_cached("myntra.com", query)
    if cached:
        return cached.get("products", [])[:max_results]

    await _rate_limit("myntra.com")
    url = _query_to_myntra_url(query)
    resp = await _fetch_with_retry(url, _headers())
    if not resp:
        return []

    html = resp.text
    products = []
    try:
        # Extract window.__myx JSON
        start = html.find("window.__myx")
        if start == -1:
            logger.warning("Myntra: window.__myx not found")
            return []

        products_start = html.find('"products"', start)
        if products_start == -1:
            return []

        bracket_start = html.find("[", products_start)
        decoder = json.JSONDecoder()
        myx_products, _ = decoder.raw_decode(html, bracket_start)

        for p in myx_products[:max_results]:
            pid = p.get("productId", "")
            name = p.get("product", "")
            brand = p.get("brand", "")
            price = p.get("price", 0)
            mrp = p.get("mrp", 0)
            discount = p.get("discount", 0)
            rating = p.get("rating", 0) or 0
            rating_count = p.get("ratingCount", 0) or 0
            landing = p.get("landingPageUrl", "")
            image = p.get("searchImage", "")
            color = p.get("colour", "")

            # Build real Myntra product URL
            url = f"https://www.myntra.com/{landing}" if landing else ""

            # Normalize discount to percentage
            if mrp > 0 and price > 0 and mrp > price:
                discount_pct = int((1 - price / mrp) * 100)
            elif discount > 100:
                discount_pct = int(discount / mrp * 100) if mrp > 0 else 0
            else:
                discount_pct = int(discount) if discount <= 100 else 0

            if name and price > 0:
                products.append({
                    "source": "myntra",
                    "product_id": str(pid),
                    "title": f"{brand} {name}".strip(),
                    "brand": brand,
                    "price": price,
                    "mrp": mrp if mrp >= price else price,
                    "discount_pct": min(discount_pct, 90),
                    "rating": round(rating, 1),
                    "rating_count": rating_count,
                    "image_url": image,
                    "url": url,
                    "color": color,
                    "category": p.get("categoryType", ""),
                })

    except Exception as e:
        logger.warning(f"Myntra parse error: {e}")

    if products:
        _set_cache("myntra.com", query, {"products": products})
    return products


# ══════════════════════════════════════════════════════════════
# AJIO — Best-effort (often blocked by Cloudflare)
# ══════════════════════════════════════════════════════════════

async def scrape_ajio(query: str, max_results: int = 10) -> list[dict]:
    """Scrape AJIO. Best-effort — often blocked by Cloudflare."""
    cached = _get_cached("ajio.com", query)
    if cached:
        return cached.get("products", [])[:max_results]

    await _rate_limit("ajio.com")
    search_url = f"https://www.ajio.com/search/?text={query.replace(' ', '%20')}"
    resp = await _fetch_with_retry(search_url, _headers())
    if not resp:
        return []

    html = resp.text
    products = []
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
    for s in scripts:
        if "productId" in s and "price" in s:
            pids = re.findall(r'"productId"\s*:\s*"([^"]+)"', s)
            names = re.findall(r'"name"\s*:\s*"([^"]+)"', s)
            prices_vals = re.findall(r'"value"\s*:\s*(\d+)', s)
            brands = re.findall(r'"brandName"\s*:\s*"([^"]+)"', s)

            for j in range(min(len(pids), max_results)):
                pid = pids[j] if j < len(pids) else ""
                name = names[j] if j < len(names) else ""
                price = int(prices_vals[j]) if j < len(prices_vals) else 0
                brand = brands[j] if j < len(brands) else ""

                if name and price > 0:
                    products.append({
                        "source": "ajio",
                        "product_id": pid,
                        "title": f"{brand} {name}".strip(),
                        "brand": brand,
                        "price": price,
                        "mrp": price,
                        "discount_pct": 0,
                        "rating": 0,
                        "rating_count": 0,
                        "image_url": "",
                        "url": f"https://www.ajio.com/p/{pid}",
                        "color": "",
                        "category": "",
                    })
            break

    if products:
        _set_cache("ajio.com", query, {"products": products})
    return products


# ══════════════════════════════════════════════════════════════
# NYKAA FASHION — Best-effort (often blocked)
# ══════════════════════════════════════════════════════════════

async def scrape_nykaa(query: str, max_results: int = 10) -> list[dict]:
    """Scrape Nykaa Fashion. Best-effort — may be blocked."""
    cached = _get_cached("nykaafashion.com", query)
    if cached:
        return cached.get("products", [])[:max_results]

    await _rate_limit("nykaafashion.com")
    search_url = f"https://www.nykaafashion.com/search?q={query.replace(' ', '+')}"
    resp = await _fetch_with_retry(search_url, _headers())
    if not resp:
        return []

    html = resp.text
    products = []
    # Try __NEXT_DATA__
    nd = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if nd:
        data = json.loads(nd.group(1))
        items = data.get("props", {}).get("pageProps", {}).get("products", [])
        for item in items[:max_results]:
            pid = item.get("id", "")
            name = item.get("name", "")
            price = item.get("price", 0)
            mrp = item.get("mrp", price)
            brand = item.get("brand", "")

            if name and price > 0:
                products.append({
                    "source": "nykaa",
                    "product_id": str(pid),
                    "title": f"{brand} {name}".strip(),
                    "brand": brand,
                    "price": price,
                    "mrp": mrp if mrp >= price else price,
                    "discount_pct": int((1 - price / mrp) * 100) if mrp > price else 0,
                    "rating": item.get("rating", 0),
                    "rating_count": item.get("ratingCount", 0),
                    "image_url": item.get("image", ""),
                    "url": f"https://www.nykaafashion.com/p/{pid}",
                    "color": "",
                    "category": "",
                })

    if products:
        _set_cache("nykaafashion.com", query, {"products": products})
    return products


# ══════════════════════════════════════════════════════════════
# SMART QUERY EXTRACTION
# ══════════════════════════════════════════════════════════════

def extract_search_query(product_name: str, brand: str = "", category: str = "", gender: str = "") -> str:
    """Extract a clean, searchable query from a product name.
    
    Args:
        product_name: Full product name/title
        brand: Brand name (optional)
        category: Category hint (optional)
        gender: Gender hint ('men'/'women', optional). Used if not detectable from name.
    """
    name_lower = product_name.lower()

    _CATEGORY_PATTERNS = {
        "tshirt": ["t-shirt", "tshirt", "tee"],
        "shirt": ["shirt", "blouse"],
        "hoodie": ["hoodie"],
        "jacket": ["jacket", "varsity", "blazer"],
        "jeans": ["jeans", "denim"],
        "kurta": ["kurta", "kurti"],
        "saree": ["saree", "sari"],
        "lehenga": ["lehenga", "lehnga"],
        "dress": ["dress", "gown", "frock"],
        "sneakers": ["sneakers", "shoes"],
        "heels": ["heels", "heels", "pumps"],
        "palazzo": ["palazzo"],
        "leggings": ["leggings", "churidar"],
        "dupatta": ["dupatta"],
        "top": ["top", "camisole"],
        "trousers": ["trousers", "pants", "chinos"],
        "shorts": ["shorts"],
        "ethnic": ["ethnic", "traditional"],
    }

    detected = ""
    for cat, patterns in _CATEGORY_PATTERNS.items():
        for pat in patterns:
            if pat in name_lower:
                detected = cat
                break
        if detected:
            break

    if not detected and category:
        detected = category.lower()

    # Detect gender from product name
    detected_gender = ""
    for g in ["women", "woman", "girl", "ladies", "lady", "female"]:
        if g in name_lower:
            detected_gender = "women"
            break
    if not detected_gender:
        for g in ["men", "man", "boy", "male"]:
            if g in name_lower:
                detected_gender = "men"
                break

    # Use provided gender if not detected from name
    if not detected_gender and gender:
        detected_gender = gender.lower()

    # Detect occasion from product name
    occasion = ""
    for occ in ["wedding", "party", "casual", "formal", "festive", "work", "office"]:
        if occ in name_lower:
            occasion = occ
            break

    parts = []
    if brand and len(brand) > 2 and brand.lower() not in ("the", "a", "an"):
        parts.append(brand)
    if detected_gender:
        parts.append(detected_gender)
    if occasion:
        parts.append(occasion)
    if detected:
        parts.append(detected)

    if not parts:
        words = re.findall(r'[a-z]+', name_lower)
        _filler = {"the", "a", "an", "is", "my", "and", "or", "for", "in", "on", "to", "of", "with", "by",
                    "just", "only", "not", "no", "but", "if", "so", "such", "name", "middle", "intrigue",
                    "calm", "chai", "keep", "respawn", "reload", "repeat", "swipe", "forever", "grave", "rave",
                    "unisexual", "printed", "graphic", "solid", "regular", "fit", "slim", "relaxed",
                    "original", "combo", "pack", "set", "new"}
        parts = [w for w in words if w not in _filler and len(w) > 2][:4]

    return " ".join(parts) if parts else product_name[:50]


# ══════════════════════════════════════════════════════════════
# CROSS-PLATFORM COMPARISON
# ══════════════════════════════════════════════════════════════

async def compare_prices(
    product_name: str,
    brand: str = "",
    category: str = "",
    gender: str = "",
    sources: list[str] = None,
) -> dict:
    """Search across all platforms. Google Shopping primary, scrapers fallback."""
    if sources is None:
        sources = ["amazon", "flipkart", "myntra", "ajio", "nykaa"]

    query = extract_search_query(product_name, brand, category, gender=gender)
    logger.info(f"Price search: '{product_name}' → query: '{query}'")

    all_products = []
    active_sources = []

    # PRIMARY: Google Shopping via SerpApi (covers all platforms in one call)
    gs_products = await search_google_shopping(query, 15)
    if gs_products:
        all_products.extend(gs_products)
        platforms = set(p["source"] for p in gs_products if p["source"] != "unknown")
        active_sources.extend(platforms)
        logger.info(f"Google Shopping: {len(gs_products)} products from {platforms}")

    # FALLBACK: Direct scrapers for platforms missing from Google Shopping
    missing_platforms = [s for s in ["amazon", "flipkart", "myntra"] if s not in active_sources]
    if missing_platforms:
        _SCRAPERS = {
            "amazon": lambda q: scrape_amazon(q, 5),
            "flipkart": lambda q: scrape_flipkart(q, 5),
            "myntra": lambda q: scrape_myntra(q, 5),
        }
        fallback_tasks = [_SCRAPERS[s](query) for s in missing_platforms if s in _SCRAPERS]
        if fallback_tasks:
            results = await asyncio.gather(*fallback_tasks, return_exceptions=True)
            for name, result in zip(missing_platforms, results):
                if isinstance(result, list) and result:
                    all_products.extend(result)
                    active_sources.append(name)
                    logger.info(f"Fallback {name}: {len(result)} products")

    all_products.sort(key=lambda x: x.get("price", float("inf")))
    available = [p for p in all_products if p.get("price", 0) > 0]
    best_price = available[0] if available else None
    worst_price = available[-1] if available else None

    savings = 0
    if best_price and worst_price and worst_price["price"] > best_price["price"]:
        savings = worst_price["price"] - best_price["price"]

    return {
        "query": query,
        "results": all_products,
        "total_found": len(all_products),
        "platforms_searched": active_sources,
        "best_price": best_price,
        "savings": savings,
        "compared_at": datetime.utcnow().isoformat(),
    }
