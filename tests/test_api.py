import pytest
from httpx import ASGITransport, AsyncClient

import vtoe.api.server as srv
from vtoe.utils.imaging import encode_image

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client(monkeypatch, person_image, garment_image):
    def fake_run(**kwargs):
        return {"job_id": "test-job", "status": "completed", "result": person_image,
                "processing_time_ms": 8500, "quality_score": 0.93,
                "face_similarity": 0.97, "garment_similarity": 0.91,
                "metadata": {"person_detected": True, "face_detected": True,
                             "garment_segmented": True, "garment_sub_type": "saree",
                             "model_used": "idm-vton", "loras": ["lora-saree"],
                             "retries": 0, "quality_reasons": []}}
    monkeypatch.setattr(srv.engine, "run", fake_run)
    async with AsyncClient(transport=ASGITransport(app=srv.app), base_url="http://t") as c:
        yield c, encode_image(person_image), encode_image(garment_image)


async def test_tryon_endpoint(client):
    c, person, garment = client
    r = await c.post("/v1/try-on", json={
        "person_image": person, "garment_image": garment,
        "garment_type": "ethnic", "ethnic_sub_type": "saree", "quality": "balanced"})
    assert r.status_code == 200
    body = r.json()
    assert body["face_similarity"] == 0.97
    assert body["metadata"]["loras"] == ["lora-saree"]
    assert body["result_image"]


async def test_invalid_quality_rejected(client):
    c, person, garment = client
    r = await c.post("/v1/try-on", json={
        "person_image": person, "garment_image": garment, "quality": "ultra"})
    assert r.status_code == 422


async def test_engines_lists_ethnic_subtypes(client):
    c, _, _ = client
    r = await c.get("/v1/engines")
    assert "saree" in r.json()["ethnic_subtypes"]


async def test_health_reports_gpu(client):
    c, _, _ = client
    r = await c.get("/v1/health")
    assert r.json()["status"] == "ok" and "gpu_total_gb" in r.json()
