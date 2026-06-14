from __future__ import annotations
import uuid

from qdrant_client import AsyncQdrantClient, models as qm

from shopify_sync.config import settings
from shopify_sync.models import CanonicalProduct


class QdrantStore:
    def __init__(self, client: AsyncQdrantClient) -> None:
        self.qd = client

    async def ensure_collection(self) -> None:
        existing = {c.name for c in (await self.qd.get_collections()).collections}
        if settings.product_collection in existing:
            return
        await self.qd.create_collection(
            settings.product_collection,
            vectors_config=qm.VectorParams(size=settings.embed_dim, distance=qm.Distance.COSINE),
        )
        for field, schema in [("gender", "keyword"), ("in_stock", "bool"),
                              ("price", "integer"), ("ontology_node_id", "keyword"),
                              ("tryon_ready", "bool"), ("color_family", "keyword")]:
            await self.qd.create_payload_index(settings.product_collection, field_name=field,
                                               field_schema=schema)

    async def upsert(self, product: CanonicalProduct, vector: list[float],
                     *, tryon_ready: bool = False) -> None:
        await self.qd.upsert(settings.product_collection, points=[qm.PointStruct(
            id=str(product.product_id), vector=vector,
            payload={
                "product_id": str(product.product_id), "shopify_id": product.shopify_id,
                "title": product.title, "brand": product.brand, "category": product.category,
                "gender": product.gender, "color_family": product.color_family,
                "color_hex": product.color_hex, "ontology_node_id": product.ontology_node_id,
                "role": product.role, "price": product.price, "in_stock": product.in_stock,
                "primary_image": product.primary_image, "tryon_ready": tryon_ready,
            })])

    async def set_tryon_ready(self, product_id: uuid.UUID, ready: bool) -> None:
        await self.qd.set_payload(settings.product_collection, payload={"tryon_ready": ready},
                                  points=[str(product_id)])

    async def delete(self, product_id: uuid.UUID) -> None:
        await self.qd.delete(settings.product_collection,
                             points_selector=qm.PointIdsList(points=[str(product_id)]))
