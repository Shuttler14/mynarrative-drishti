from __future__ import annotations
import structlog

from shopify_sync.embedder import Embedder
from shopify_sync.gap_client import GAPClient
from shopify_sync.models import ShopifyProduct
from shopify_sync.normalize import normalize, product_uuid
from shopify_sync.qdrant_store import QdrantStore

logger = structlog.get_logger(__name__)


class SyncService:
    def __init__(self, qdrant: QdrantStore, embedder: Embedder, gap: GAPClient) -> None:
        self.qdrant, self.embedder, self.gap = qdrant, embedder, gap

    async def upsert_product(self, sp: ShopifyProduct) -> None:
        product = normalize(sp)
        log = logger.bind(shopify_id=sp.id, product_id=str(product.product_id))

        if product.status != "active":
            await self.delete_product(sp.id)
            return

        vector = await self.embedder.embed(product)
        if vector is None:
            log.warning("embed.failed_no_vector")
            return
        await self.qdrant.upsert(product, vector)
        await self.gap.enqueue(product)
        log.info("product.synced", node=product.ontology_node_id, price=product.price)

    async def delete_product(self, shopify_id: int) -> None:
        pid = product_uuid(shopify_id)
        await self.qdrant.delete(pid)
        logger.info("product.deleted", shopify_id=shopify_id, product_id=str(pid))

    async def on_asset_ready(self, product_id, tryon_ready: bool) -> None:
        await self.qdrant.set_tryon_ready(product_id, tryon_ready)
