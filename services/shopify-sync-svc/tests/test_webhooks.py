import base64, hashlib, hmac, json
import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio
SECRET = "test-secret"


def _sign(body: bytes) -> str:
    return base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()


@pytest.fixture
async def client(monkeypatch):
    from shopify_sync.config import settings
    settings.webhook_secret = SECRET
    from shopify_sync import main

    class FakeSync:
        def __init__(self): self.upserts, self.deletes = [], []
        async def upsert_product(self, sp): self.upserts.append(sp.id)
        async def delete_product(self, sid): self.deletes.append(sid)

    app = main.app
    app.state.sync = FakeSync()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, app.state.sync


async def test_valid_create_webhook(client):
    c, sync = client
    body = json.dumps({"id": 7, "title": "X", "variants": [], "images": []}).encode()
    r = await c.post("/webhooks/shopify/products/create", content=body,
                     headers={"X-Shopify-Hmac-SHA256": _sign(body)})
    assert r.status_code == 200 and 7 in sync.upserts


async def test_forged_webhook_rejected(client):
    c, _ = client
    body = json.dumps({"id": 7}).encode()
    r = await c.post("/webhooks/shopify/products/create", content=body,
                     headers={"X-Shopify-Hmac-SHA256": "forged"})
    assert r.status_code == 401
