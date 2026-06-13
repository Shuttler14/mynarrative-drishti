"""
Drishti Marketplace Scraper Base
================================
Shared functionality for Myntra, Amazon, Flipkart, Ajio scrapers
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any
from urllib.parse import quote_plus

import httpx

logger = logging.getLogger("drishti.scrapers")


class RateLimiter:
    def __init__(self, rate: int = 2, per: float = 1.0):
        self.rate = rate
        self.per = per
        self.tokens = rate
        self.last_refill = time.time()

    async def acquire(self):
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.rate, self.tokens + elapsed * self.rate / self.per)
        self.last_refill = now

        if self.tokens < 1:
            wait = (1 - self.tokens) * self.per / self.rate
            await asyncio.sleep(wait)
            self.tokens = 0
        else:
            self.tokens -= 1


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, recovery_time: float = 60.0):
        self.failure_count = 0
        self.failure_threshold = failure_threshold
        self.recovery_time = recovery_time
        self.state = "closed"
        self.last_failure_time = 0

    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold:
            self.state = "open"
            logger.warning("Circuit breaker OPEN")

    def record_success(self):
        self.failure_count = 0
        self.state = "closed"

    def can_execute(self) -> bool:
        if self.state == "closed":
            return True
        if time.time() - self.last_failure_time > self.recovery_time:
            self.state = "half-open"
            return True
        return False


class BaseScraper(ABC):
    SOURCE: str = ""

    def __init__(self):
        self.rate_limiter = RateLimiter(rate=2, per=1.0)
        self.circuit_breaker = CircuitBreaker()
        self.session: httpx.AsyncClient | None = None
        self.user_agents = [
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        ]

    async def __aenter__(self):
        self.session = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": random.choice(self.user_agents)},
        )
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.aclose()

    async def fetch(self, url: str) -> str | None:
        if not self.circuit_breaker.can_execute():
            logger.warning(f"Circuit breaker open, skipping {url}")
            return None

        await self.rate_limiter.acquire()

        try:
            resp = await self.session.get(url)
            if resp.status_code == 200:
                self.circuit_breaker.record_success()
                return resp.text
            elif resp.status_code == 429:
                logger.warning(f"Rate limited on {url}")
                self.circuit_breaker.record_failure()
                await asyncio.sleep(5)
                return None
            else:
                logger.warning(f"HTTP {resp.status_code} on {url}")
                self.circuit_breaker.record_failure()
                return None
        except Exception as e:
            logger.error(f"Fetch error: {e}")
            self.circuit_breaker.record_failure()
            return None

    @abstractmethod
    async def scrape_category(self, category: str, max_pages: int = 5) -> list[dict]:
        pass

    @abstractmethod
    async def scrape_product(self, source_id: str) -> dict | None:
        pass

    @abstractmethod
    def parse_product(self, data: dict) -> dict:
        pass

    def normalize_product(self, raw: dict) -> dict:
        return {
            "source": self.SOURCE,
            "source_id": raw.get("source_id", ""),
            "source_url": raw.get("url", ""),
            "name": raw.get("name", ""),
            "brand": raw.get("brand", ""),
            "category": raw.get("category", ""),
            "subcategory": raw.get("subcategory", ""),
            "gender": raw.get("gender", "unisex"),
            "price": raw.get("price"),
            "original_price": raw.get("original_price"),
            "discount_pct": raw.get("discount_pct"),
            "currency": "INR",
            "color": raw.get("color"),
            "color_family": raw.get("color_family"),
            "sizes": raw.get("sizes", []),
            "images": raw.get("images", []),
            "thumbnail": raw.get("thumbnail", ""),
            "attributes": raw.get("attributes", {}),
            "rating": raw.get("rating"),
            "review_count": raw.get("review_count", 0),
            "availability": raw.get("availability", True),
            "last_scraped": datetime.utcnow().isoformat(),
        }
