from __future__ import annotations
import io
from functools import lru_cache

import httpx
import torch
import torch.nn.functional as F
from PIL import Image

from shopify_sync.config import settings
from shopify_sync.models import CanonicalProduct


@lru_cache(maxsize=1)
def _clip():
    from transformers import CLIPModel, CLIPProcessor
    model = CLIPModel.from_pretrained(settings.clip_model, cache_dir=settings.model_cache).to(
        settings.device).eval()
    proc = CLIPProcessor.from_pretrained(settings.clip_model, cache_dir=settings.model_cache)
    return model, proc


class Embedder:
    async def embed(self, product: CanonicalProduct) -> list[float] | None:
        model, proc = _clip()
        img = await self._fetch_image(product.primary_image) if product.primary_image else None
        text = f"{product.brand or ''} {product.title}".strip()

        with torch.inference_mode():
            txt = proc(text=[text], return_tensors="pt", padding=True, truncation=True).to(settings.device)
            tvec = F.normalize(model.get_text_features(**txt), dim=-1)[0]
            if img is not None:
                im = proc(images=img, return_tensors="pt").to(settings.device)
                ivec = F.normalize(model.get_image_features(**im), dim=-1)[0]
                fused = F.normalize(0.7 * ivec + 0.3 * tvec, dim=-1)
            else:
                fused = tvec
        return fused.cpu().tolist()

    @staticmethod
    async def _fetch_image(url: str) -> Image.Image | None:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
                r = await c.get(url)
                r.raise_for_status()
            return Image.open(io.BytesIO(r.content)).convert("RGB")
        except Exception:
            return None
