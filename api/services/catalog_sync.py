"""
Shopify Product Sync → Qdrant Vector Store

Syncs products from Shopify Storefront API, generates CLIP embeddings,
and stores them in Qdrant for real-time recommendations.

Usage:
  python -m vtoe.services.catalog_sync --full          # full sync
  python -m vtoe.services.catalog_sync --incremental   # only new/updated
  python -m vtoe.services.catalog_sync --webhook       # process single product webhook
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
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
COLLECTION_NAME = "shopify_products"
VECTOR_SIZE = 512  # CLIP ViT-B/32 output dim


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
    """Fetch products from Shopify Storefront API."""
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


# ── CLIP Embedding ──

_clip_model = None


def _get_clip_model():
    """Lazy-load CLIP model."""
    global _clip_model
    if _clip_model is None:
        import torch
        from transformers import CLIPModel, CLIPProcessor

        model_id = "openai/clip-vit-base-patch32"
        _clip_model = CLIPModel.from_pretrained(model_id)
        _clip_processor = CLIPProcessor.from_pretrained(model_id)
        _clip_model.eval()
        logger.info("CLIP model loaded")
    return _clip_model


def _get_clip_processor():
    """Lazy-load CLIP processor."""
    from transformers import CLIPProcessor
    return CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")


def generate_embedding(text: str, image_url: str | None = None) -> list[float]:
    """Generate CLIP embedding from text + optional image."""
    import torch
    from PIL import Image
    from io import BytesIO
    import requests

    model = _get_clip_model()
    processor = _get_clip_processor()

    # Prepare inputs
    texts = [text]

    # Try to load image
    image = None
    if image_url:
        try:
            resp = requests.get(image_url, timeout=10)
            if resp.status_code == 200:
                image = Image.open(BytesIO(resp.content)).convert("RGB")
        except Exception:
            pass

    # Generate embedding
    if image:
        inputs = processor(text=texts, images=image, return_tensors="pt", padding=True)
        with torch.no_grad():
            outputs = model(**inputs)
            # Average image and text embeddings
            img_emb = outputs.image_embeds[0]
            txt_emb = outputs.text_embeds[0]
            combined = (img_emb + txt_emb) / 2
    else:
        inputs = processor(text=texts, return_tensors="pt", padding=True)
        with torch.no_grad():
            outputs = model(**inputs)
            combined = outputs.text_embeds[0]

    # Normalize
    combined = combined / combined.norm(p=2, dim=-1, keepdim=True)
    return combined.tolist()


# ── Qdrant Operations ──

def get_qdrant_client() -> QdrantClient:
    """Get Qdrant client."""
    return QdrantClient(url=QDRANT_URL)


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
        print(json.dumps(result, indent=2))
    else:
        print("Use --full for full sync, or import and call sync_single_product()")
