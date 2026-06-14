import pytest
import respx
import httpx
from admin.sources.prometheus import PrometheusClient

pytestmark = pytest.mark.asyncio


@respx.mock
async def test_query_parses_value():
    respx.get("http://p/api/v1/query").mock(return_value=httpx.Response(200, json={
        "data": {"result": [{"metric": {}, "value": [0, "42.5"]}]}}))
    assert await PrometheusClient("http://p").query("up") == 42.5


@respx.mock
async def test_empty_result_returns_zero():
    respx.get("http://p/api/v1/query").mock(return_value=httpx.Response(200, json={
        "data": {"result": []}}))
    assert await PrometheusClient("http://p").query("up") == 0.0
