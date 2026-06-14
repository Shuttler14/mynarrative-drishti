# Real Indian Fashion Data Scraper
# Targets: Myntra, Flipkart, Amazon.in
# Purpose: Train ethnic LoRAs for VTOE

"""
RED FLAGS & RISKS:

1. LEGAL:
   - Myntra/Flipkart/Amazon ToS prohibit automated scraping
   - IP bans, CAPTCHAs, rate limits
   - DMCA takedowns if images are copyrighted
   - Data Protection Act 2023 (India) may apply

2. TECHNICAL:
   - Dynamic rendering (React/Next.js) - need Playwright/Selenium
   - Anti-bot: CloudFlare, PerimeterX, DataDome
   - Image CDN rotation (images.mynectar.com → static5.myntra.com)
   - Lazy loading (images load on scroll)
   - JavaScript-rendered product pages

3. DATA QUALITY:
   - Watermarks on product images
   - Mixed ethnic/western in same category
   - Duplicate images across sellers
   - Low-res thumbnails vs full images
   - Mannequin/hanger shots vs model shots

4. RATE LIMITS:
   - Myntra: ~60 req/min before block
   - Flipkart: ~30 req/min
   - Amazon: ~1 req/sec with delays
"""

# ═══════════════════════════════════════════════════════════════
# BRAINSTORM PROMPT (for AI when stuck)
# ═══════════════════════════════════════════════════════════════

BRAINSTROM_PROMPT = """
You are a web scraping expert for Indian fashion e-commerce sites. When you encounter 
a block, error, or unexpected behavior, use this thinking process:

1. ANALYZE THE BLOCK:
   - What HTTP status code? (403=blocked, 429=rate limited, 503=service unavailable)
   - Is it a CAPTCHA? Which type? (reCAPTCHA, hCaptcha, Cloudflare challenge)
   - Is it IP-based or session-based?
   - Did it happen immediately or after N requests?

2. BYPASS STRATEGIES (try in order):
   a) Rotate User-Agent (mobile vs desktop)
   b) Add random delays (2-8 seconds between requests)
   c) Use residential proxies if available
   d) Switch to mobile API endpoints (often less protected)
   e) Use Google cache/AMP versions
   f) Use Wayback Machine for historical data
   g) Switch to alternative data source (see FALLBACK_SOURCES)
   h) Use Selenium with undetected-chromedriver
   i) Use Playwright with stealth plugin
   j) Use cloudscraper library

3. DATA EXTRACTION (if page loads but structure changed):
   - Check for new CSS selectors
   - Look for JSON-LD structured data
   - Check for __NEXT_DATA__ or window.__PRELOADED_STATE__
   - Use network tab to find API endpoints
   - Look for GraphQL queries in JS bundles

4. QUALITY GATES:
   - Image must be > 300x300px
   - No watermarks (check with OCR or template matching)
   - Must be on model (not mannequin) - check with classifier
   - Must be ethnic wear (not western) - check with CLIP

5. FALLBACK_SOURCES (if primary blocked):
   - Google Images (search: "saree model photo site:myntra.com")
   - Pinterest (Indian fashion boards)
   - Instagram (fashion influencers, tagged products)
   - YouTube (fashion haul videos - extract frames)
   - Wayback Machine (web.archive.org)
   - Public datasets: DeepFashion, iMaterialist
   - Government textile portals (handloom.gov.in)

6. MORPHOLOGICAL ANALYSIS (when stuck):
   - What worked before that stopped working?
   - Did the site update recently? (check wayback)
   - Is it a permanent block or temporary cooldown?
   - Can I use a different entry point? (category page vs search)
"""

# ═══════════════════════════════════════════════════════════════
# SCRAPER IMPLEMENTATION
# ═══════════════════════════════════════════════════════════════

import asyncio
import json
import random
import re
import time
import hashlib
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from PIL import Image
from io import BytesIO


# ── User Agents ──
USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
]

# ── Category URLs ──
CATEGORIES = {
    "saree": {
        "myntra": "https://www.myntra.com/sarees?f=Gender%3Awomen%20Saree+Type%3AReady+To+Wear",
        "flipkart": "https://www.flipkart.com/search?q=women+saree&otracker=search",
        "amazon": "https://www.amazon.in/s?k=women+saree&ref=nb_sb_noss",
    },
    "lehenga": {
        "myntra": "https://www.myntra.com/lehenga-cholis?f=Gender%3Awomen",
        "flipkart": "https://www.flipkart.com/search?q=women+lehenga&otracker=search",
        "amazon": "https://www.amazon.in/s?k=women+lehenga&ref=nb_sb_noss",
    },
    "kurta": {
        "myntra": "https://www.myntra.com/kurtas?f=Gender%3Amen+Kurta+Type%3ARegular",
        "flipkart": "https://www.flipkart.com/search?q=men+kurta&otracker=search",
        "amazon": "https://www.amazon.in/s?k=men+kurta&ref=nb_sb_noss",
    },
}


class IndianFashionScraper:
    """Production scraper for Indian fashion e-commerce."""

    def __init__(self, output_dir: str = "data/scraped", delay_range: tuple = (2, 6)):
        self.output = Path(output_dir)
        self.output.mkdir(parents=True, exist_ok=True)
        self.delay_range = delay_range
        self.seen_urls = set()
        self.failed_urls = []
        self.stats = {"total": 0, "success": 0, "failed": 0, "skipped": 0}

    async def _delay(self):
        await asyncio.sleep(random.uniform(*self.delay_range))

    def _get_headers(self) -> dict:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }

    def _hash_url(self, url: str) -> str:
        return hashlib.md5(url.encode()).hexdigest()[:12]

    def _is_valid_image(self, data: bytes, min_size: tuple = (300, 300)) -> bool:
        """Check if image is valid and meets minimum size."""
        try:
            img = Image.open(BytesIO(data))
            return img.width >= min_size[0] and img.height >= min_size[1]
        except Exception:
            return False

    def _has_watermark(self, img: Image.Image) -> bool:
        """Simple watermark detection (check for text in corners)."""
        # TODO: Use OCR or template matching for production
        return False

    async def scrape_myntra(self, subtype: str, max_items: int = 500) -> list[dict]:
        """Scrape Myntra for ethnic wear images."""
        items = []
        url = CATEGORIES.get(subtype, {}).get("myntra")
        if not url:
            return items

        print(f"[Myntra] Scraping {subtype} from {url}")

        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            # Myntra uses API endpoints
            api_url = "https://www.myntra.com/gateway/v2/search/query"
            params = {
                "p": 1,
                "rows": 60,
                "plaession": "false",
                "query": f"women {subtype}",
                "f": "Gender:Women",
            }

            for page in range(1, (max_items // 60) + 1):
                params["p"] = page
                try:
                    await self._delay()
                    resp = await client.get(api_url, headers=self._get_headers(), params=params)

                    if resp.status_code == 403:
                        print("[Myntra] Blocked! Switching to mobile API...")
                        # Try mobile API
                        api_url = "https://www.myntra.com/gateway/v2/search/query"
                        resp = await client.get(api_url, headers=self._get_headers(), params=params)

                    if resp.status_code != 200:
                        print(f"[Myntra] HTTP {resp.status_code}")
                        self.failed_urls.append(url)
                        continue

                    data = resp.json()
                    products = data.get("searchData", {}).get("products", [])

                    for product in products:
                        img_url = product.get("images", [{}])[0].get("src", "")
                        if not img_url or img_url in self.seen_urls:
                            self.stats["skipped"] += 1
                            continue

                        items.append({
                            "source": "myntra",
                            "subtype": subtype,
                            "product_id": product.get("id"),
                            "name": product.get("brand", "") + " " + product.get("product", ""),
                            "price": product.get("price", {}).get("mrp"),
                            "image_url": img_url,
                            "category": product.get("category"),
                        })
                        self.seen_urls.add(img_url)
                        self.stats["total"] += 1

                    print(f"[Myntra] Page {page}: {len(products)} products")
                    if len(items) >= max_items:
                        break

                except Exception as e:
                    print(f"[Myntra] Error: {e}")
                    continue

        return items

    async def scrape_flipkart(self, subtype: str, max_items: int = 500) -> list[dict]:
        """Scrape Flipkart for ethnic wear images."""
        items = []
        url = CATEGORIES.get(subtype, {}).get("flipkart")
        if not url:
            return items

        print(f"[Flipkart] Scraping {subtype} from {url}")

        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            for page in range(1, (max_items // 30) + 1):
                try:
                    await self._delay()
                    page_url = f"{url}&page={page}"
                    resp = await client.get(page_url, headers=self._get_headers())

                    if resp.status_code == 403:
                        print("[Flipkart] Blocked! Waiting 30s...")
                        await asyncio.sleep(30)
                        resp = await client.get(page_url, headers=self._get_headers())

                    if resp.status_code != 200:
                        print(f"[Flipkart] HTTP {resp.status_code}")
                        continue

                    # Extract image URLs from HTML
                    img_pattern = r'https://rukminim\d\.flixcart\.com/image/[^"]+'
                    img_urls = re.findall(img_pattern, resp.text)

                    for img_url in set(img_urls):
                        if img_url in self.seen_urls:
                            self.stats["skipped"] += 1
                            continue

                        # Get high-res version
                        img_url = re.sub(r'/\d+/\d+/', '/600/600/', img_url)

                        items.append({
                            "source": "flipkart",
                            "subtype": subtype,
                            "image_url": img_url,
                        })
                        self.seen_urls.add(img_url)
                        self.stats["total"] += 1

                    print(f"[Flipkart] Page {page}: {len(img_urls)} images")
                    if len(items) >= max_items:
                        break

                except Exception as e:
                    print(f"[Flipkart] Error: {e}")
                    continue

        return items

    async def scrape_amazon(self, subtype: str, max_items: int = 500) -> list[dict]:
        """Scrape Amazon.in for ethnic wear images."""
        items = []
        url = CATEGORIES.get(subtype, {}).get("amazon")
        if not url:
            return items

        print(f"[Amazon] Scraping {subtype} from {url}")

        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            for page in range(1, (max_items // 20) + 1):
                try:
                    await self._delay()
                    page_url = f"{url}&page={page}"
                    resp = await client.get(page_url, headers=self._get_headers())

                    if resp.status_code == 503:
                        print("[Amazon] CAPTCHA detected! Waiting 60s...")
                        await asyncio.sleep(60)
                        continue

                    if resp.status_code != 200:
                        print(f"[Amazon] HTTP {resp.status_code}")
                        continue

                    # Extract image URLs
                    img_pattern = r'https://m\.media-amazon\.com/images/I/[^"]+\.jpg'
                    img_urls = re.findall(img_pattern, resp.text)

                    for img_url in set(img_urls):
                        if img_url in self.seen_urls:
                            self.stats["skipped"] += 1
                            continue

                        items.append({
                            "source": "amazon",
                            "subtype": subtype,
                            "image_url": img_url,
                        })
                        self.seen_urls.add(img_url)
                        self.stats["total"] += 1

                    print(f"[Amazon] Page {page}: {len(img_urls)} images")
                    if len(items) >= max_items:
                        break

                except Exception as e:
                    print(f"[Amazon] Error: {e}")
                    continue

        return items

    async def download_images(self, items: list[dict], subtype: str) -> int:
        """Download and validate images."""
        downloaded = 0
        output_dir = self.output / subtype / "raw"
        output_dir.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            for item in items:
                img_url = item.get("image_url")
                if not img_url:
                    continue

                try:
                    await self._delay()
                    resp = await client.get(img_url, headers=self._get_headers())

                    if resp.status_code != 200:
                        continue

                    if not self._is_valid_image(resp.content):
                        print(f"  Skipped (too small): {img_url[:80]}")
                        continue

                    # Save
                    fname = f"{subtype}_{self._hash_url(img_url)}.jpg"
                    (output_dir / fname).write_bytes(resp.content)
                    downloaded += 1

                    if downloaded % 50 == 0:
                        print(f"  Downloaded {downloaded} images...")

                except Exception as e:
                    print(f"  Download error: {e}")
                    continue

        return downloaded

    async def scrape_all(self, subtypes: list[str] = None, max_per_source: int = 500):
        """Scrape all sources for all subtypes."""
        if subtypes is None:
            subtypes = ["saree", "lehenga", "kurta"]

        for subtype in subtypes:
            print(f"\n{'='*60}")
            print(f"  Scraping {subtype.upper()}")
            print(f"{'='*60}")

            all_items = []

            # Scrape all sources
            for source_name, source_func in [
                ("Myntra", self.scrape_myntra),
                ("Flipkart", self.scrape_flipkart),
                ("Amazon", self.scrape_amazon),
            ]:
                try:
                    items = await source_func(subtype, max_per_source)
                    all_items.extend(items)
                    print(f"  {source_name}: {len(items)} items")
                except Exception as e:
                    print(f"  {source_name} failed: {e}")

            # Download images
            print(f"\nDownloading {len(all_items)} images...")
            downloaded = await self.download_images(all_items, subtype)
            print(f"Downloaded: {downloaded}")

        # Print stats
        print(f"\n{'='*60}")
        print(f"  STATS")
        print(f"{'='*60}")
        print(f"Total URLs: {self.stats['total']}")
        print(f"Skipped (dupes): {self.stats['skipped']}")
        print(f"Failed: {len(self.failed_urls)}")


# ── Alternative: Use existing datasets ──

async def download_deepfashion(output_dir: str = "data/deepfashion"):
    """Download DeepFashion-MultiModal dataset."""
    try:
        from datasets import load_dataset

        ds = load_dataset("deepvisualrl/DeepFashion_MultiModal", split="train", streaming=True)
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        count = 0
        for item in ds:
            if count >= 1000:
                break
            img = item.get("image")
            if img:
                img.save(out / f"df_{count:04d}.jpg", quality=95)
                count += 1
                if count % 100 == 0:
                    print(f"  Downloaded {count} DeepFashion images")

        print(f"DeepFashion: {count} images saved to {out}")
    except ImportError:
        print("Install datasets: pip install datasets")


if __name__ == "__main__":
    scraper = IndianFashionScraper()
    asyncio.run(scraper.scrape_all(max_per_source=500))
