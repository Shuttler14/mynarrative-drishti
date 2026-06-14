import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio


class FakeProm:
    def __init__(self, scalars, grouped=None):
        self.scalars = scalars
        self.grouped = grouped or {}

    async def query(self, q):
        # sort by key length descending so more specific keys match first
        for k, v in sorted(self.scalars.items(), key=lambda x: -len(x[0])):
            if k in q:
                return v
        return 0.0

    async def query_each(self, q):
        return self.grouped


class FakeEvents:
    async def reco_ctr(self, **k):
        return 0.12

    async def tryon_to_save(self, **k):
        return 0.34


@pytest.fixture
async def client():
    from admin.main import app
    app.state.prom = FakeProm({
        'vtoe_jobs_total{result="ok"}': 950,
        'vtoe_jobs_total{result="error"}': 50,
        "vtoe_duration_seconds_bucket": 12.0,
        "reco_requests_total": 5000,
        'reco_requests_total{result="cache_hit"}': 3500,
        "shopify_products_synced_total": 1200,
        "vtoe_queue_depth": 3,
        "vtoe_jobs_inflight": 2,
        'http_requests_total{status=~"5.."}': 5,
        "http_requests_total": 10000,
    })
    app.state.events = FakeEvents()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_overview_computes_kpis(client):
    r = await client.get("/admin/v1/analytics/overview")
    assert r.status_code == 200
    b = r.json()
    assert b["tryons_24h"] == 1000
    assert b["tryon_success_rate"] == 0.95
    assert b["reco_cache_hit_rate"] == 0.7
    assert b["reco_ctr_24h"] == 0.12
    assert b["products_synced"] == 1200


async def test_no_data_is_zero_not_crash(client):
    client._transport.app.state.prom = FakeProm({})
    r = await client.get("/admin/v1/analytics/overview")
    assert r.status_code == 200
    assert r.json()["tryon_success_rate"] == 0.0


async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
