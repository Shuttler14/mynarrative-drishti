from pydantic import BaseModel


class Overview(BaseModel):
    tryons_24h: int
    tryon_success_rate: float
    tryon_p50_s: float
    tryon_p95_s: float
    tryon_queue_depth: int
    reco_requests_24h: int
    reco_cache_hit_rate: float
    reco_ctr_24h: float
    tryon_to_save_24h: float
    products_synced: int
    gpu_inflight: int
    error_rate_5m: float


class VTOEStats(BaseModel):
    by_model: dict[str, float]
    by_priority_depth: dict[str, int]
    avg_gpu_ms: float
    retry_rate: float
