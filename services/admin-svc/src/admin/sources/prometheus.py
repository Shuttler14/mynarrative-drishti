from __future__ import annotations
import httpx

from admin.config import settings


class PrometheusClient:
    """Thin PromQL client. The dashboard derives all 'live' KPIs from the metrics
    the services already export via drishti-observability + VTOE/GAP counters."""

    def __init__(self, base: str | None = None) -> None:
        self.base = base or settings.prometheus_url

    async def query(self, promql: str) -> float:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{self.base}/api/v1/query", params={"query": promql})
            r.raise_for_status()
            result = r.json()["data"]["result"]
        if not result:
            return 0.0
        return float(result[0]["value"][1])

    async def query_each(self, promql: str) -> dict[str, float]:
        """Returns {label_value: value} for grouped queries (e.g. by model)."""
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{self.base}/api/v1/query", params={"query": promql})
            r.raise_for_status()
            result = r.json()["data"]["result"]
        out = {}
        for series in result:
            label = next(iter(series["metric"].values()), "all")
            out[label] = float(series["value"][1])
        return out
