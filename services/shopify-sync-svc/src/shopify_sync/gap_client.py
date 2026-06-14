from __future__ import annotations
import httpx

from shopify_sync.config import settings
from shopify_sync.models import CanonicalProduct


class GAPClient:
    async def enqueue(self, product: CanonicalProduct) -> None:
        if not settings.enable_gap or not product.primary_image:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                await c.post(f"{settings.gap_api_url}/v1/gap/enqueue", json={
                    "product_id": str(product.product_id),
                    "image_urls": product.images,
                    "category": product.ontology_node_id or product.category or "top",
                    "gender": product.gender,
                    "ontology_node_id": product.ontology_node_id,
                })
        except Exception:
            pass
