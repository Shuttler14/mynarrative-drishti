from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from qdrant_client import AsyncQdrantClient

from shopify_sync.api import admin, health, webhooks
from shopify_sync.config import settings
from shopify_sync.embedder import Embedder
from shopify_sync.gap_client import GAPClient
from shopify_sync.qdrant_store import QdrantStore
from shopify_sync.sync_service import SyncService

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    qdrant = AsyncQdrantClient(url=settings.qdrant_url)
    store = QdrantStore(qdrant)
    await store.ensure_collection()
    app.state.qdrant = qdrant
    app.state.embedder = Embedder()
    app.state.gap = GAPClient()

    class _Proxy:
        def __init__(self, store, embedder, gap):
            self.store = store
            self.embedder = embedder
            self.gap = gap

        async def upsert_product(self, sp):
            svc = SyncService(self.store, self.embedder, self.gap)
            await svc.upsert_product(sp)

        async def delete_product(self, sid):
            svc = SyncService(self.store, self.embedder, self.gap)
            await svc.delete_product(sid)

    app.state.sync = _Proxy(store, app.state.embedder, app.state.gap)
    logger.info("shopify-sync.started")
    yield
    await qdrant.close()


app = FastAPI(title="DRISHTI Shopify Sync", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(webhooks.router)
app.include_router(admin.router)
