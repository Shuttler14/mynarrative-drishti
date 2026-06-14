import uuid
import pytest
from shopify_sync.models import ShopifyProduct, ShopifyVariant, ShopifyImage
from shopify_sync.normalize import product_uuid
from shopify_sync.sync_service import SyncService

pytestmark = pytest.mark.asyncio


class FakeQdrant:
    def __init__(self): self.points = {}; self.flags = {}
    async def upsert(self, p, v, tryon_ready=False): self.points[str(p.product_id)] = v
    async def delete(self, pid): self.points.pop(str(pid), None)
    async def set_tryon_ready(self, pid, r): self.flags[str(pid)] = r


class FakeEmbedder:
    async def embed(self, p): return [0.1] * 512


class FakeGAP:
    def __init__(self): self.enqueued = []
    async def enqueue(self, p): self.enqueued.append(p.product_id)


def _sp(status="active"):
    return ShopifyProduct(id=42, title="Red Saree", vendor="Biba", product_type="Sarees",
                          status=status, tags="women",
                          images=[ShopifyImage(src="http://img/1.jpg")],
                          variants=[ShopifyVariant(id=1, title="Free", price="2999.00", available=True)])


async def test_active_product_synced():
    q, g = FakeQdrant(), FakeGAP()
    svc = SyncService(q, FakeEmbedder(), g)
    await svc.upsert_product(_sp())
    assert str(product_uuid(42)) in q.points
    assert product_uuid(42) in g.enqueued


async def test_draft_product_removed():
    q = FakeQdrant()
    svc = SyncService(q, FakeEmbedder(), FakeGAP())
    await svc.upsert_product(_sp())
    await svc.upsert_product(_sp(status="draft"))
    assert str(product_uuid(42)) not in q.points


async def test_asset_ready_flips_flag():
    q = FakeQdrant()
    svc = SyncService(q, FakeEmbedder(), FakeGAP())
    pid = uuid.uuid4()
    await svc.on_asset_ready(pid, True)
    assert q.flags[str(pid)] is True
