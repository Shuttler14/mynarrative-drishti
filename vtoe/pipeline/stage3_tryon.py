from __future__ import annotations

import torch
from PIL import Image

from vtoe.config import settings
from vtoe.ethnic.lora_registry import for_subtype
from vtoe.face.preserver import FacePreserver
from vtoe.models.loaders import registry
from vtoe.pipeline._idm_pipe import build_agnostic
from vtoe.pipeline.stage1_person import PersonAnalysis
from vtoe.pipeline.stage2_garment import GarmentAnalysis
from vtoe.utils.memory import vram_guard

_LOADED_LORA: dict[str, str] = {}


def _apply_lora(pipe, sub_type: str) -> list[str]:
    spec = for_subtype(sub_type)
    if not spec or not settings.enable_ethnic_lora:
        if _LOADED_LORA.get("idm"):
            pipe.unload_lora_weights(); _LOADED_LORA.pop("idm", None)
        return []
    repo, weight = spec
    if _LOADED_LORA.get("idm") == repo:
        return [repo]
    try:
        pipe.unload_lora_weights()
        pipe.load_lora_weights(repo, adapter_name="ethnic", cache_dir=settings.model_cache)
        pipe.set_adapters(["ethnic"], adapter_weights=[weight])
        _LOADED_LORA["idm"] = repo
        return [repo]
    except Exception:
        return []


def run(person: PersonAnalysis, garment: GarmentAnalysis, *, model: str,
        steps: int, guidance: float, seed: int) -> tuple[Image.Image, str, list[str]]:
    fp = FacePreserver()
    face_mask = fp.face_region_mask(person.person)
    agnostic, mask = build_agnostic(person.person, person.person_mask, face_mask,
                                    garment.drape, garment.drape.extend_to_floor)
    gen = torch.Generator(device=settings.device).manual_seed(seed)
    loras: list[str] = []

    with vram_guard("stage3.generate"):
        if model == "cat":
            pipe = registry.get("cat")
            out = pipe(image=agnostic, mask_image=mask, ip_adapter_image=garment.garment,
                       prompt=f"person wearing {garment.sub_type}, {garment.drape.prompt_suffix}",
                       num_inference_steps=steps, strength=0.99,
                       width=settings.out_w, height=settings.out_h, generator=gen).images[0]
            used = "catvton"
        else:
            pipe = registry.get("idm")
            loras = _apply_lora(pipe, garment.sub_type)
            pipe.set_ip_adapter_scale(garment.drape.ip_adapter_scale)
            out = pipe(
                prompt=f"a person wearing {garment.sub_type}, {garment.drape.prompt_suffix}, photorealistic",
                negative_prompt="distorted, deformed garment, extra limbs, blurry, flat overlay, warped pattern",
                image=agnostic, mask_image=mask, control_image=person.pose_image,
                ip_adapter_image=garment.garment,
                num_inference_steps=steps, guidance_scale=guidance, strength=0.99,
                width=settings.out_w, height=settings.out_h, generator=gen).images[0]
            used = "idm-vton"
    return out, used, loras
