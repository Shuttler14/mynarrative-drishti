"""
Cross-platform price comparison scraping engine.
Anti-blocking: rotating UAs, realistic headers, delays, retry, caching.
"""
import asyncio
import hashlib
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta
from typing import Optional

import httpx

logger = logging.getLogger("drishti.pricing.scraper")

# ── Anti-blocking: User-Agent pool ──
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

# ── In-memory cache (24h TTL) ──
_cache: dict[str, tuple[float, dict]] = {}
CACHE_TTL = 86400  # 24 hours

# ── Rate limiting ──
_last_request: dict[str, float] = {}
MIN_DELAY = 2.0  # Minimum seconds between requests per domain


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
    key = _cache_key(domain, query)
    _cache[key] = (time.time(), data)


def _random_headers(domain: str) -> dict:
    ua = random.choice(_USER_AGENTS)
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }
    if domain == "myntra.com":
        headers["X-Requested-With"] = "XMLHttpRequest"
    elif domain == "www.amazon.in":
        headers["Sec-Ch-Ua"] = '"Chromium";v="125", "Google Chrome";v="125", "Not.A/Brand";v="24"'
        headers["Sec-Ch-Ua-Mobile"] = "?0"
        headers["Sec-Ch-Ua-Platform"] = '"macOS"'
    return headers


async def _rate_limit(domain: str):
    now = time.time()
    last = _last_request.get(domain, 0)
    wait = MIN_DELAY - (now - last) + random.uniform(0.5, 1.5)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_request[domain] = time.time()


async def _fetch_with_retry(url: str, domain: str, max_retries: int = 3) -> Optional[str]:
    await _rate_limit(domain)
    headers = _random_headers(domain)

    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(
                timeout=15.0,
                follow_redirects=True,
                http2=True,
            ) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    return resp.text
                elif resp.status_code == 429:
                    wait = (2 ** attempt) * 5 + random.uniform(2, 5)
                    logger.warning(f"[{domain}] Rate limited, waiting {wait:.1f}s")
                    await asyncio.sleep(wait)
                elif resp.status_code == 403:
                    logger.warning(f"[{domain}] Blocked (403), attempt {attempt+1}")
                    await asyncio.sleep(random.uniform(3, 8))
                else:
                    logger.warning(f"[{domain}] HTTP {resp.status_code}")
                    return None
        except Exception as e:
            logger.warning(f"[{domain}] Request error: {e}")
            await asyncio.sleep(random.uniform(1, 3))

    return None


# ── Myntra Scraper ──

async def scrape_myntra(query: str, max_results: int = 10) -> list[dict]:
    """Scrape Myntra search results via internal search API (requires cookies from initial page load)."""
    cached = _get_cached("myntra.com", query)
    if cached:
        return cached.get("products", [])[:max_results]

    search_query = query.replace(" ", "+")
    products = []

    try:
        await _rate_limit("myntra.com")
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, http2=True) as client:
            # Step 1: Load main page to get cookies
            main_headers = _random_headers("myntra.com")
            main_resp = await client.get("https://www.myntra.com/", headers=main_headers)
            cookies = dict(main_resp.cookies)

            # Step 2: Search API call with cookies
            api_headers = _random_headers("myntra.com")
            api_headers["Accept"] = "application/json"
            api_headers["X-Requested-With"] = "XMLHttpRequest"
            api_headers["myntrawebsite"] = "desktop"
            api_headers["Referer"] = f"https://www.myntra.com/{search_query}"

            search_url = f"https://www.myntra.com/gateway/v2/search/query?q={search_query}&p=1&rows={max_results}&o=0"
            resp = await client.get(search_url, headers=api_headers)
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("products", [])
                for item in items[:max_results]:
                    products.append({
                        "source": "myntra",
                        "product_id": str(item.get("productId", "")),
                        "title": item.get("product", item.get("productName", "")),
                        "brand": item.get("brand", ""),
                        "price": item.get("price", 0),
                        "mrp": item.get("mrp", item.get("price", 0)),
                        "discount_pct": item.get("discount", 0),
                        "rating": item.get("rating", 0),
                        "rating_count": item.get("ratingCount", 0),
                        "image_url": item.get("searchImage", ""),
                        "url": f"https://www.myntra.com/{item.get('landingPageUrl', '')}",
                        "color": item.get("primaryColour", ""),
                        "category": item.get("category", ""),
                    })
            else:
                logger.warning(f"Myntra API returned {resp.status_code}")
    except Exception as e:
        logger.warning(f"Myntra API error: {e}")

    if products:
        _set_cache("myntra.com", query, {"products": products})
    return products


# ── AJIO Scraper ──

async def scrape_ajio(query: str, max_results: int = 10) -> list[dict]:
    """Scrape AJIO search results."""
    cached = _get_cached("ajio.com", query)
    if cached:
        return cached.get("products", [])[:max_results]

    # AJIO uses an internal API
    search_query = query.replace(" ", "%20")
    url = f"https://www.ajio.com/search/?text={search_query}"
    html = await _fetch_with_retry(url, "ajio.com")
    if not html:
        return []

    products = []
    try:
        # AJIO embeds product data in script tags
        match = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});\s*</script>', html, re.DOTALL)
        if not match:
            match = re.search(r'"items"\s*:\s*\[(.*?)\]\s*[,}]', html, re.DOTALL)

        if match:
            try:
                data = json.loads(match.group(1))
                items = data if isinstance(data, list) else data.get("items", data.get("products", []))
            except json.JSONDecodeError:
                items = []

            for item in items[:max_results]:
                if isinstance(item, dict):
                    products.append({
                        "source": "ajio",
                        "product_id": str(item.get("id", item.get("productId", ""))),
                        "title": item.get("name", item.get("productName", "")),
                        "brand": item.get("brand", {}).get("name", "") if isinstance(item.get("brand"), dict) else item.get("brand", ""),
                        "price": item.get("price", {}).get("value", 0) if isinstance(item.get("price"), dict) else item.get("price", 0),
                        "mrp": item.get("mrp", {}).get("value", 0) if isinstance(item.get("mrp"), dict) else item.get("mrp", 0),
                        "discount_pct": item.get("discountPercent", 0),
                        "rating": item.get("rating", 0),
                        "rating_count": item.get("ratingCount", 0),
                        "image_url": item.get("image", item.get("imageUrl", "")),
                        "url": f"https://www.ajio.com{item.get('url', '')}",
                        "color": item.get("color", ""),
                        "category": item.get("category", ""),
                    })
    except Exception as e:
        logger.warning(f"AJIO parse error: {e}")

    if products:
        _set_cache("ajio.com", query, {"products": products})
    return products


# ── Amazon India Scraper ──

async def scrape_amazon(query: str, max_results: int = 10) -> list[dict]:
    """Scrape Amazon India search results via HTML parsing."""
    cached = _get_cached("amazon.in", query)
    if cached:
        return cached.get("products", [])[:max_results]

    search_query = query.replace(" ", "+")
    url = f"https://www.amazon.in/s?k={search_query}&ref=nb_sb_noss"
    html = await _fetch_with_retry(url, "www.amazon.in")
    if not html:
        return []

    products = []
    try:
        # Find all s-search-result containers
        results = list(re.finditer(r'data-component-type="s-search-result"', html))

        for i, match in enumerate(results[:max_results]):
            start = match.start()
            end = results[i + 1].start() if i + 1 < len(results) else start + 8000
            chunk = html[start:end]

            # ASIN
            asin_match = re.search(r'data-asin="([A-Z0-9]{10})"', chunk)
            if not asin_match:
                continue
            asin = asin_match.group(1)

            # Title (second h2 in the chunk contains the full product name)
            h2s = re.findall(r'<h2[^>]*>(.*?)</h2>', chunk, re.DOTALL)
            title = ""
            if len(h2s) >= 2:
                title = re.sub(r'<[^>]+>', '', h2s[1]).strip()
            elif len(h2s) == 1:
                title = re.sub(r'<[^>]+>', '', h2s[0]).strip()
            if not title:
                title_match = re.search(r'class="a-text-normal"[^>]*>([^<]+)<', chunk)
                title = title_match.group(1).strip() if title_match else ""

            # Price
            price_match = re.search(r'class="a-price-whole"[^>]*>([0-9,]+)<', chunk)
            price = int(price_match.group(1).replace(",", "")) if price_match else 0

            # MRP
            mrp_match = re.search(r'class="a-price a-text-price[^"]*"[^>]*>.*?class="a-offscreen"[^>]*>([0-9,]+)', chunk, re.DOTALL)
            if not mrp_match:
                mrp_match = re.search(r'a-text-price[^>]*>[^<]*<span[^>]*>([0-9,]+)<', chunk)
            mrp = int(mrp_match.group(1).replace(",", "")) if mrp_match else price

            # Rating
            rating_match = re.search(r'class="a-icon-alt">(\d+\.?\d*) out of', chunk)
            rating = float(rating_match.group(1)) if rating_match else 0

            # Rating count
            count_match = re.search(r'(\d[\d,]*)\s*(?:ratings?| Reviews)', chunk)
            rating_count = int(count_match.group(1).replace(",", "")) if count_match else 0

            # Image
            img_match = re.search(r'<img[^>]*src="(https://m\.media-amazon\.com/[^"]+)"', chunk)
            image_url = img_match.group(1) if img_match else ""

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

    except Exception as e:
        logger.warning(f"Amazon parse error: {e}")

    if products:
        _set_cache("amazon.in", query, {"products": products})
    return products


# ── Cross-platform price comparison ──

async def compare_prices(
    product_name: str,
    brand: str = "",
    category: str = "",
    sources: list[str] = None,
) -> dict:
    """Search across platforms and return price comparison."""
    if sources is None:
        sources = ["myntra", "ajio", "amazon"]

    query = f"{brand} {product_name}".strip() if brand else product_name

    tasks = []
    if "myntra" in sources:
        tasks.append(scrape_myntra(query, max_results=5))
    if "ajio" in sources:
        tasks.append(scrape_ajio(query, max_results=5))
    if "amazon" in sources:
        tasks.append(scrape_amazon(query, max_results=5))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_products = []
    for result in results:
        if isinstance(result, list):
            all_products.extend(result)

    # Sort by price
    all_products.sort(key=lambda x: x.get("price", float("inf")))

    # Find best price
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
        "best_price": best_price,
        "savings": savings,
        "compared_at": datetime.utcnow().isoformat(),
    }


# ── Standalone test ──

async def _test():
    logging.basicConfig(level=logging.INFO)
    result = await compare_prices("narrative tshirt", sources=["myntra"])
    print(json.dumps(result, indent=2)[:1000])


if __name__ == "__main__":
    asyncio.run(_test())
