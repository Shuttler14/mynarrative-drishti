from __future__ import annotations

import json
import re
from scrapers.base import BaseScraper


class MyntraScraper(BaseScraper):
    SOURCE = "myntra"

    BASE_URL = "https://www.myntra.com"
    API_BASE = "https://www.myntra.com/gateway/v2/search/query"

    CATEGORY_MAP = {
        "men-tshirts": "1",
        "men-shirts": "2",
        "men-jeans": "4",
        "men-trousers": "5",
        "men-jackets": "6",
        "women-dresses": "10",
        "women-tops": "11",
        "women-jeans": "14",
        "women-ethnic": "18",
        "women-sarees": "19",
    }

    async def scrape_category(self, category: str, max_pages: int = 5) -> list[dict]:
        products = []
        category_id = self.CATEGORY_MAP.get(category, "1")

        for page in range(1, max_pages + 1):
            url = f"{self.API_BASE}?p={page}&rows=50&plaession=true&searchData={category_id}"
            html = await self.fetch(url)

            if not html:
                break

            try:
                data = json.loads(html)
                items = data.get("searchData", {}).get("products", [])

                if not items:
                    break

                for item in items:
                    try:
                        product = self.parse_product(item)
                        products.append(self.normalize_product(product))
                    except Exception as e:
                        continue

            except (json.JSONDecodeError, KeyError):
                break

        return products

    async def scrape_product(self, source_id: str) -> dict | None:
        url = f"{self.BASE_URL}/{source_id}"
        html = await self.fetch(url)

        if not html:
            return None

        try:
            match = re.search(r'window.__PRELOADED_STATE__\s*=\s*({.+?})\s*;', html)
            if match:
                data = json.loads(match.group(1))
                product_data = data.get("pdpData", {})
                return self.normalize_product(self.parse_product(product_data))
        except Exception as e:
            pass

        return None

    def parse_product(self, data: dict) -> dict:
        return {
            "source_id": str(data.get("id", data.get("productId", ""))),
            "url": data.get("url", data.get("landingPageUrl", "")),
            "name": data.get("name", data.get("productName", "")),
            "brand": data.get("brandName", data.get("brand", "")),
            "category": data.get("categoryName", data.get("category", "")),
            "subcategory": data.get("subCategoryName", data.get("subCategory", "")),
            "gender": data.get("gender", "unisex"),
            "price": data.get("price", {}).get("amount") or data.get("sellingPrice"),
            "original_price": data.get("price", {}).get("mrp") or data.get("mrp"),
            "discount_pct": data.get("discountPercent"),
            "color": data.get("colour"),
            "color_family": data.get("colorFamily"),
            "sizes": data.get("sizes", []),
            "images": [
                img.get("src", img.get("imageUrl", ""))
                for img in data.get("images", data.get("media", []))
            ],
            "thumbnail": data.get("searchImage", data.get("thumbnail", "")),
            "attributes": data.get("attributes", {}),
            "rating": data.get("rating", {}).get("average"),
            "review_count": data.get("rating", {}).get("count", 0),
            "availability": data.get("inventoryInfo", [{}])[0].get("available", True)
            if data.get("inventoryInfo")
            else True,
        }
