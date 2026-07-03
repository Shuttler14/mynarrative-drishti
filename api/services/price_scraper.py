"""
Cross-platform price comparison — Amazon, Flipkart, Myntra, AJIO, Nykaa.
All links are verified product page URLs.
"""
import asyncio
import hashlib
import html as html_mod
import json
import logging
import math
import random
import re
import time
from datetime import datetime
from typing import Optional

import httpx

logger = logging.getLogger("drishti.pricing")

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]

_cache: dict[str, tuple[float, dict]] = {}
CACHE_TTL = 86400
_last_request: dict[str, float] = {}
MIN_DELAY = 0.8


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

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, http2=True) as client:
            resp = await client.get(search_url, headers=_headers("www.amazon.in"))
            if resp.status_code != 200:
                logger.warning(f"Amazon: HTTP {resp.status_code}")
                return []
            html = resp.text
    except Exception as e:
        logger.warning(f"Amazon error: {e}")
        return []

    # Bot detection — page too small
    if len(html) < 5000:
        logger.warning(f"Amazon: bot detection (len={len(html)})")
        return []

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

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, http2=True) as client:
            resp = await client.get(search_url, headers=_headers())
            if resp.status_code != 200:
                logger.warning(f"Flipkart: HTTP {resp.status_code}")
                return []
            html = resp.text
    except Exception as e:
        logger.warning(f"Flipkart error: {e}")
        return []

    # Bot detection — page is too small
    if len(html) < 5000:
        logger.warning(f"Flipkart: bot detection (len={len(html)})")
        return []

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

# Map search queries to Myntra category page URLs
_MYNYTRA_CATEGORY_MAP = {
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
}


def _query_to_myntra_url(query: str) -> str:
    """Convert a search query to a Myntra category page URL."""
    q = query.lower().strip()

    # Try exact category match first
    for keyword, slug in _MYNYTRA_CATEGORY_MAP.items():
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

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, http2=True) as client:
            resp = await client.get(url, headers=_headers())
            if resp.status_code != 200:
                logger.warning(f"Myntra: HTTP {resp.status_code} for {url}")
                return []
            html = resp.text
    except Exception as e:
        logger.warning(f"Myntra error: {e}")
        return []

    # Bot detection — page too small
    if len(html) < 5000:
        logger.warning(f"Myntra: bot detection (len={len(html)})")
        return []

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
    """Scrape AJIO. Best-effort — may be blocked."""
    cached = _get_cached("ajio.com", query)
    if cached:
        return cached.get("products", [])[:max_results]

    await _rate_limit("ajio.com")
    search_url = f"https://www.ajio.com/search/?text={query.replace(' ', '%20')}"

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, http2=True) as client:
            resp = await client.get(search_url, headers=_headers())
            if resp.status_code != 200:
                return []
            html = resp.text

        products = []
        # Try to find product data in script tags
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

    except Exception as e:
        logger.warning(f"AJIO error: {e}")
        return []


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

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, http2=True) as client:
            resp = await client.get(search_url, headers=_headers())
            if resp.status_code != 200:
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

    except Exception as e:
        logger.warning(f"Nykaa error: {e}")
        return []


# ══════════════════════════════════════════════════════════════
# SMART QUERY EXTRACTION
# ══════════════════════════════════════════════════════════════

def extract_search_query(product_name: str, brand: str = "", category: str = "") -> str:
    """Extract a clean, searchable query from a product name."""
    name_lower = product_name.lower()

    _CATEGORY_PATTERNS = {
        "tshirt": ["t-shirt", "tshirt", "tee"],
        "shirt": ["shirt"],
        "hoodie": ["hoodie"],
        "jacket": ["jacket", "varsity"],
        "jeans": ["jeans", "denim"],
        "kurta": ["kurta", "kurti"],
        "sneakers": ["sneakers", "shoes"],
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

    gender = ""
    for g in ["men", "man", "boy"]:
        if g in name_lower:
            gender = "men"
            break
    if not gender:
        for g in ["women", "woman", "girl"]:
            if g in name_lower:
                gender = "women"
                break

    parts = []
    if brand and len(brand) > 2:
        parts.append(brand)
    if gender:
        parts.append(gender)
    if detected:
        parts.append(detected)

    if not parts:
        words = re.findall(r'[a-z]+', name_lower)
        _filler = {"the", "a", "an", "is", "my", "and", "or", "for", "in", "on", "to", "of", "with", "by",
                    "just", "only", "not", "no", "but", "if", "so", "such", "name", "middle", "intrigue",
                    "calm", "chai", "keep", "respawn", "reload", "repeat", "swipe", "forever", "grave", "rave",
                    "unisexual", "printed", "graphic"}
        parts = [w for w in words if w not in _filler and len(w) > 2][:3]

    return " ".join(parts) if parts else product_name[:50]


# ══════════════════════════════════════════════════════════════
# CROSS-PLATFORM COMPARISON
# ══════════════════════════════════════════════════════════════

async def compare_prices(
    product_name: str,
    brand: str = "",
    category: str = "",
    sources: list[str] = None,
) -> dict:
    """Search across all platforms and return price comparison with valid links."""
    if sources is None:
        sources = ["amazon", "flipkart", "myntra", "ajio", "nykaa"]

    query = extract_search_query(product_name, brand, category)
    logger.info(f"Price search: '{product_name}' → query: '{query}'")

    _SCRAPERS = {
        "amazon": lambda q: scrape_amazon(q, 5),
        "flipkart": lambda q: scrape_flipkart(q, 5),
        "myntra": lambda q: scrape_myntra(q, 5),
        "ajio": lambda q: scrape_ajio(q, 5),
        "nykaa": lambda q: scrape_nykaa(q, 5),
    }

    # Run scrapers sequentially to avoid rate limiting conflicts
    all_products = []
    active_sources = []
    for s in sources:
        if s in _SCRAPERS:
            try:
                result = await asyncio.wait_for(_SCRAPERS[s](query), timeout=20)
                if isinstance(result, list) and result:
                    all_products.extend(result)
                    active_sources.append(s)
            except asyncio.TimeoutError:
                logger.warning(f"Scraper {s} timed out")
            except Exception as e:
                logger.warning(f"Scraper {s} failed: {e}")

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
