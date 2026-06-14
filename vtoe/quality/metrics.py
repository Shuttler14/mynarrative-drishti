from __future__ import annotations
import torch
import torch.nn.functional as F
from PIL import Image

from vtoe.models.loaders import registry


def clip_similarity(a: Image.Image, b: Image.Image) -> float:
    model, proc = registry.get("clip")
    inputs = proc(images=[a, b], return_tensors="pt").to(model.device)
    with torch.inference_mode():
        feats = model.get_image_features(pixel_values=inputs.pixel_values.half())
    feats = F.normalize(feats, dim=-1)
    return float((feats[0] @ feats[1]).item())
