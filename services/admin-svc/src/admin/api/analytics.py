from __future__ import annotations
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from admin.schemas import Overview, VTOEStats
from admin.sources.events import EventAggregates
from admin.sources.prometheus import PrometheusClient

router = APIRouter(prefix="/admin/v1/analytics", tags=["analytics"])


def prom(request: Request) -> PrometheusClient:
    return request.app.state.prom


def events(request: Request) -> EventAggregates:
    return request.app.state.events


@router.get("/overview", response_model=Overview)
async def overview(
    p: Annotated[PrometheusClient, Depends(prom)],
    e: Annotated[EventAggregates, Depends(events)],
) -> Overview:
    tryon_ok = await p.query('sum(increase(vtoe_jobs_total{result="ok"}[24h]))')
    tryon_err = await p.query('sum(increase(vtoe_jobs_total{result="error"}[24h]))')
    total = tryon_ok + tryon_err
    return Overview(
        tryons_24h=int(total),
        tryon_success_rate=round(tryon_ok / total, 3) if total else 0.0,
        tryon_p50_s=round(await p.query(
            'histogram_quantile(0.5, sum(rate(vtoe_duration_seconds_bucket[1h])) by (le))'), 1),
        tryon_p95_s=round(await p.query(
            'histogram_quantile(0.95, sum(rate(vtoe_duration_seconds_bucket[1h])) by (le))'), 1),
        tryon_queue_depth=int(await p.query("sum(vtoe_queue_depth)") or 0),
        reco_requests_24h=int(await p.query(
            'sum(increase(reco_requests_total[24h]))')),
        reco_cache_hit_rate=await _ratio(
            p, 'reco_requests_total{result="cache_hit"}', "reco_requests_total"),
        reco_ctr_24h=round(await e.reco_ctr(), 3),
        tryon_to_save_24h=round(await e.tryon_to_save(), 3),
        products_synced=int(await p.query("shopify_products_synced_total")),
        gpu_inflight=int(await p.query("sum(vtoe_jobs_inflight)") or 0),
        error_rate_5m=round(await _ratio(
            p, 'http_requests_total{status=~"5.."}', "http_requests_total", "[5m]"), 4),
    )


@router.get("/vtoe", response_model=VTOEStats)
async def vtoe(p: Annotated[PrometheusClient, Depends(prom)]) -> VTOEStats:
    return VTOEStats(
        by_model=await p.query_each('sum(increase(vtoe_jobs_total[24h])) by (model)'),
        by_priority_depth={k: int(v) for k, v in
                           (await p.query_each("vtoe_stream_depth by (priority)")).items()},
        avg_gpu_ms=round(await p.query(
            "sum(rate(vtoe_stage_ms_sum[1h])) / sum(rate(vtoe_stage_ms_count[1h]))") or 0, 1),
        retry_rate=round(await _ratio(
            p, 'vtoe_jobs_total{result="error"}', "vtoe_jobs_total", "[24h]"), 3),
    )


async def _ratio(p: PrometheusClient, num: str, den: str, win: str = "[24h]") -> float:
    n = await p.query(f"sum(increase({num}{win}))")
    d = await p.query(f"sum(increase({den}{win}))")
    return n / d if d else 0.0
