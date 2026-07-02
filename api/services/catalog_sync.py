"""
Shopify Product Sync → Qdrant Vector Store

Syncs products from Shopify Storefront API, generates text embeddings
via fastembed (ONNX-based, lightweight), and stores them in Qdrant
for real-time recommendations.

Usage:
  python -m api.services.catalog_sync --full          # full sync
  python -m api.services.catalog_sync --incremental   # only new/updated
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointIdsList,
    PointStruct,
    VectorParams,
)

logger = logging.getLogger("drishti.catalog_sync")


# ── Config ──

SHOPIFY_STORE_URL = os.getenv("SHOPIFY_STORE_URL", "https://mynarrative.in")
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
SHOPIFY_ADMIN_TOKEN = os.getenv("SHOPIFY_ADMIN_TOKEN", "")
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
COLLECTION_NAME = "shopify_products"
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
VECTOR_SIZE = 384


# ── Shopify Storefront API ──

PRODUCTS_QUERY = """
query Products($first: Int!, $after: String) {
  products(first: $first, after: $after) {
    pageInfo {
      hasNextPage
      endCursor
    }
    edges {
      node {
        id
        title
        description
        productType
        tags
        vendor
        priceRange {
          minVariantPrice {
            amount
            currencyCode
          }
          maxVariantPrice {
            amount
            currencyCode
          }
        }
        images(first: 5) {
          edges {
            node {
              url
              altText
              width
              height
            }
          }
        }
        variants(first: 10) {
          edges {
            node {
              id
              title
              price {
                amount
                currencyCode
              }
              availableForSale
              selectedOptions {
                name
                value
              }
            }
          }
        }
        collections(first: 5) {
          edges {
            node {
              title
            }
          }
        }
      }
    }
  }
}
"""


async def fetch_shopify_products(
    first: int = 50, after: str | None = None
) -> dict:
    """Fetch products from Shopify — tries Admin REST API first, falls back to Storefront GraphQL."""
    # Try Admin REST API first (more reliable)
    if SHOPIFY_ADMIN_TOKEN:
        return await _fetch_admin_products(first, after)
    # Fallback to Storefront GraphQL
    return await _fetch_storefront_products(first, after)


async def _fetch_admin_products(first: int = 50, after: str | None = None) -> dict:
    """Fetch products via Shopify Admin REST API (paginated with page_info)."""
    url = f"{SHOPIFY_STORE_URL}/admin/api/2024-01/products.json?limit={first}"
    if after:
        url += f"&page_info={after}"
    headers = {"X-Shopify-Access-Token": SHOPIFY_ADMIN_TOKEN}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    products_raw = data.get("products", [])

    # Convert Admin API format to GraphQL-like format for _parse_product
    edges = []
    for p in products_raw:
        node = {
            "id": f"gid://shopify/Product/{p['id']}",
            "title": p.get("title", ""),
            "description": p.get("body_html", ""),
            "productType": p.get("product_type", ""),
            "tags": p.get("tags", "").split(", ") if p.get("tags") else [],
            "vendor": p.get("vendor", ""),
            "priceRange": {
                "minVariantPrice": {
                    "amount": p.get("variants", [{}])[0].get("price", "0") if p.get("variants") else "0",
                    "currencyCode": "INR",
                },
                "maxVariantPrice": {
                    "amount": p.get("variants", [{}])[0].get("price", "0") if p.get("variants") else "0",
                    "currencyCode": "INR",
                },
            },
            "images": {
                "edges": [
                    {"node": {"url": img.get("src", ""), "altText": img.get("alt", "")}}
                    for img in p.get("images", [])[:5]
                ]
            },
            "variants": {
                "edges": [
                    {
                        "node": {
                            "id": f"gid://shopify/ProductVariant/{v['id']}",
                            "title": v.get("title", ""),
                            "price": {"amount": v.get("price", "0"), "currencyCode": "INR"},
                            "availableForSale": v.get("available", True),
                            "selectedOptions": [
                                {"name": opt["name"], "value": opt["value"]}
                                for opt in v.get("option_values", [])
                            ] if v.get("option_values") else [],
                        }
                    }
                    for v in p.get("variants", [])[:10]
                ]
            },
            "collections": {"edges": []},
        }

        # Update max price from variants
        prices = [float(v.get("price", "0")) for v in p.get("variants", []) if v.get("price")]
        if prices:
            node["priceRange"]["maxVariantPrice"]["amount"] = str(max(prices))

        edges.append({"node": node})

    # Check for pagination via Link header
    has_next = False
    next_cursor = None
    link_header = resp.headers.get("Link", "")
    if 'rel="next"' in link_header:
        has_next = True
        # Extract page_info from Link header
        import re
        match = re.search(r'<[^>]*[?&]page_info=([^&>]+)>; rel="next"', link_header)
        if match:
            next_cursor = match.group(1)

    return {
        "data": {
            "products": {
                "edges": edges,
                "pageInfo": {"hasNextPage": has_next, "endCursor": next_cursor},
            }
        }
    }


async def _fetch_storefront_products(first: int = 50, after: str | None = None) -> dict:
    """Fetch products from Shopify Storefront GraphQL API."""
    url = f"{SHOPIFY_STORE_URL}/api/2024-01/graphql.json"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Storefront-Access-Token": SHOPIFY_ACCESS_TOKEN,
    }
    payload = {
        "query": PRODUCTS_QUERY,
        "variables": {"first": first, "after": after},
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def fetch_all_products(max_products: int = 1000) -> list[dict]:
    """Fetch all products with pagination."""
    products = []
    cursor = None
    page = 0

    while len(products) < max_products:
        page += 1
        data = await fetch_shopify_products(first=50, after=cursor)
        edges = data.get("data", {}).get("products", {}).get("edges", [])

        if not edges:
            break

        for edge in edges:
            node = edge["node"]
            product = _parse_product(node)
            products.append(product)

        page_info = data["data"]["products"]["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]

        logger.info(f"Page {page}: fetched {len(edges)} products (total: {len(products)})")
        await asyncio.sleep(0.5)  # rate limit

    return products


def _parse_product(node: dict) -> dict:
    """Parse Shopify product node into our format."""
    # Extract garment category from product type and tags
    product_type = (node.get("productType") or "").lower()
    tags = [t.lower() for t in (node.get("tags") or [])]

    # Determine garment category
    category = _classify_garment(product_type, tags)

    # Get main image URL
    images = node.get("images", {}).get("edges", [])
    image_url = images[0]["node"]["url"] if images else None

    # Get price
    price_range = node.get("priceRange", {})
    min_price = price_range.get("minVariantPrice", {})
    price = float(min_price.get("amount", 0))

    # Get variants
    variants = []
    for v in node.get("variants", {}).get("edges", []):
        vn = v["node"]
        variants.append({
            "id": vn["id"].split("/")[-1],
            "title": vn["title"],
            "price": float(vn["price"]["amount"]),
            "available": vn["availableForSale"],
            "options": {o["name"]: o["value"] for o in vn.get("selectedOptions", [])},
        })

    # Get collections
    collections = [
        e["node"]["title"]
        for e in node.get("collections", {}).get("edges", [])
    ]

    return {
        "shopify_id": node["id"].split("/")[-1],
        "title": node["title"],
        "description": node.get("description", ""),
        "product_type": product_type,
        "category": category,
        "vendor": node.get("vendor", ""),
        "tags": tags,
        "price": price,
        "currency": min_price.get("currencyCode", "INR"),
        "image_url": image_url,
        "variants": variants,
        "collections": collections,
        "url": f"{SHOPIFY_STORE_URL}/products/{node['title'].lower().replace(' ', '-')}",
    }


def _classify_garment(product_type: str, tags: list[str]) -> str:
    """Classify garment into VTOE categories."""
    text = f"{product_type} {' '.join(tags)}".lower()

    ethnic_keywords = {
        "saree": ["saree", "sari", "sari"],
        "lehenga": ["lehenga", "lehenga choli", "ghagra"],
        "kurta": ["kurta", "kurti", "kurta pajama"],
        "sherwani": ["sherwani", "bandhgala"],
        "dupatta": ["dupatta", "stole", "scarf"],
        "anarkali": ["anarkali"],
        "salwar": ["salwar", "churidar", "legging"],
    }

    for category, keywords in ethnic_keywords.items():
        if any(kw in text for kw in keywords):
            return f"ethnic_{category}"

    western_keywords = {
        "top": ["top", "t-shirt", "blouse", "shirt"],
        "bottom": ["jeans", "trouser", "pant", "short"],
        "dress": ["dress", "gown", "frock"],
        "outerwear": ["jacket", "coat", "blazer", "cardigan"],
    }

    for category, keywords in western_keywords.items():
        if any(kw in text for kw in keywords):
            return category

    return "other"


# ── Text Embedding (fastembed - ONNX, no torch required) ──

_text_embedder = None


def _get_text_embedder():
    """Lazy-load fastembed TextEmbedding model."""
    global _text_embedder
    if _text_embedder is None:
        from fastembed import TextEmbedding
        _text_embedder = TextEmbedding(model_name=EMBEDDING_MODEL)
        logger.info(f"fastembed model loaded: {EMBEDDING_MODEL}")
    return _text_embedder


def generate_embedding(text: str, image_url: str | None = None) -> list[float]:
    """Generate text embedding via fastembed (image_url ignored, text-only)."""
    embedder = _get_text_embedder()
    embeddings = list(embedder.embed([text]))
    return embeddings[0].tolist()


# ── Qdrant Operations ──

def get_qdrant_client() -> QdrantClient:
    """Get Qdrant client with HTTPS enforcement in production."""
    from api.config import get_settings
    settings = get_settings()

    url = QDRANT_URL
    api_key = None

    # Check for API key in environment
    qdrant_api_key = os.getenv("QDRANT_API_KEY", "")
    if qdrant_api_key:
        api_key = qdrant_api_key

    # In production, use host/port/https params (avoids SSL handshake issues with url= param)
    if settings.ENV == "production" and url.startswith("https://"):
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port or 443
        return QdrantClient(host=host, port=port, https=True, api_key=api_key, timeout=60)

    return QdrantClient(url=url, api_key=api_key, prefer_grpc=False, timeout=30)


def ensure_collection(client: QdrantClient):
    """Create collection if it doesn't exist."""
    collections = client.get_collections().collections
    existing = [c.name for c in collections]

    if COLLECTION_NAME not in existing:
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        logger.info(f"Created collection: {COLLECTION_NAME}")


def upsert_product(client: QdrantClient, product: dict, embedding: list[float]):
    """Upsert a product into Qdrant."""
    point_id = hashlib.md5(product["shopify_id"].encode()).hexdigest()

    payload = {
        "shopify_id": product["shopify_id"],
        "title": product["title"],
        "description": product.get("description", "")[:500],
        "category": product["category"],
        "product_type": product["product_type"],
        "vendor": product.get("vendor", ""),
        "price": product["price"],
        "currency": product.get("currency", "INR"),
        "image_url": product.get("image_url", ""),
        "url": product.get("url", ""),
        "tags": json.dumps(product.get("tags", [])),
        "collections": json.dumps(product.get("collections", [])),
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }

    client.upsert(
        collection_name=COLLECTION_NAME,
        points=[
            PointStruct(
                id=point_id,
                vector=embedding,
                payload=payload,
            )
        ],
    )


def search_similar(
    client: QdrantClient,
    query_embedding: list[float],
    category: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Search for similar products."""
    query_filter = None
    if category:
        query_filter = Filter(
            must=[FieldCondition(key="category", match=MatchValue(value=category))]
        )

    results = client.query_points(
        collection_name=COLLECTION_NAME,
        query=query_embedding,
        query_filter=query_filter,
        limit=limit,
    )

    return [
        {
            "id": r.id,
            "score": r.score,
            **r.payload,
        }
        for r in results.points
    ]


def delete_product(client: QdrantClient, shopify_id: str):
    """Delete a product from Qdrant."""
    point_id = hashlib.md5(shopify_id.encode()).hexdigest()
    client.delete(
        collection_name=COLLECTION_NAME,
        points_selector=PointIdsList(points=[point_id]),
    )


# ── Sync Orchestration ──

async def full_sync(max_products: int = 1000):
    """Full sync: fetch all products from Shopify and index in Qdrant."""
    logger.info("Starting full sync...")
    start = time.time()

    client = get_qdrant_client()
    ensure_collection(client)

    products = await fetch_all_products(max_products)
    logger.info(f"Fetched {len(products)} products from Shopify")

    indexed = 0
    for product in products:
        try:
            # Generate embedding from title + description + category
            text = f"{product['title']} {product['description'][:200]} {product['category']}"
            embedding = generate_embedding(text, product.get("image_url"))

            upsert_product(client, product, embedding)
            indexed += 1

            if indexed % 50 == 0:
                logger.info(f"Indexed {indexed}/{len(products)}")

        except Exception as e:
            logger.error(f"Failed to index {product['shopify_id']}: {e}")

    elapsed = time.time() - start
    logger.info(f"Full sync complete: {indexed}/{len(products)} indexed in {elapsed:.1f}s")
    return {"indexed": indexed, "total": len(products), "elapsed_seconds": elapsed}


def sync_single_product(product: dict):
    """Sync a single product (from webhook)."""
    client = get_qdrant_client()
    ensure_collection(client)

    text = f"{product['title']} {product.get('description', '')[:200]} {product.get('category', '')}"
    embedding = generate_embedding(text, product.get("image_url"))
    upsert_product(client, product, embedding)
    logger.info(f"Synced product: {product['shopify_id']}")


def delete_single_product(shopify_id: str):
    """Delete a single product (from webhook)."""
    client = get_qdrant_client()
    delete_product(client, shopify_id)
    logger.info(f"Deleted product: {shopify_id}")


# ── CLI ──

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO)

    ap = argparse.ArgumentParser(description="Shopify → Qdrant catalog sync")
    ap.add_argument("--full", action="store_true", help="Full sync")
    ap.add_argument("--max", type=int, default=1000, help="Max products")
    a = ap.parse_args()

    if a.full:
        result = asyncio.run(full_sync(a.max))
        logger.info(json.dumps(result, indent=2))
    else:
        logger.info("Use --full for full sync, or import and call sync_single_product()")
