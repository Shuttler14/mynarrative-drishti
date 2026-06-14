import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from drishti_observability import setup
from drishti_observability.config import ObsSettings

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def app_client():
    app = FastAPI()
    setup(app, "test-svc", ObsSettings(env="local", sentry_dsn="", otel_endpoint=""))

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_sentry_disabled_without_dsn():
    from drishti_observability import sentry
    active = sentry.init("test-svc", ObsSettings(sentry_dsn=""))
    assert active is False


async def test_metrics_endpoint_served(app_client):
    await app_client.get("/ping")
    r = await app_client.get("/metrics")
    assert r.status_code == 200
    assert "http_requests_total" in r.text


async def test_request_id_header_present(app_client):
    r = await app_client.get("/ping")
    assert r.headers.get("x-request-id")


async def test_inbound_request_id_honored(app_client):
    r = await app_client.get("/ping", headers={"x-request-id": "abc123"})
    assert r.headers["x-request-id"] == "abc123"


async def test_healthz_skipped_from_metrics(app_client):
    # /healthz is in _SKIP set, should not error
    r = await app_client.get("/metrics")
    assert r.status_code == 200
