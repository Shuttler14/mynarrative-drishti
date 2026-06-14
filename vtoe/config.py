from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum


class Quality(str, Enum):
    fast = "fast"
    balanced = "balanced"
    quality = "quality"


@dataclass
class Settings:
    device: str = "cuda"
    dtype: str = "float16"
    out_w: int = 768
    out_h: int = 1024
    model_cache: str = "/content/models"

    vram_budget_gb: float = 14.5

    idm_vton: str = "yisol/IDM-VTON"
    catvton: str = "zhengchong/CatVTON"
    birefnet: str = "ZhengPeng7/BiRefNet"
    controlnet_pose: str = "thibaud/controlnet-openpose-sdxl-1.0"
    ip_adapter: str = "h94/IP-Adapter"
    arcface: str = "buffalo_l"
    codeformer: str = "sczhou/CodeFormer"
    clip: str = "openai/clip-vit-large-patch14"
    yolo: str = "yolov8n.pt"

    face_sim_min: float = 0.95
    garment_sim_min: float = 0.85
    pose_match_min: float = 0.90
    max_retries: int = 2

    enable_ethnic_lora: bool = True

    steps_by_quality: dict = field(default_factory=lambda: {
        "fast": 20, "balanced": 30, "quality": 50})


settings = Settings()
