from __future__ import annotations
import cv2
import numpy as np
import torch
from PIL import Image


def build_idm_pipeline(settings, dtype):
    from diffusers import ControlNetModel, StableDiffusionXLControlNetInpaintPipeline
    controlnet = ControlNetModel.from_pretrained(
        settings.controlnet_pose, torch_dtype=dtype, cache_dir=settings.model_cache)
    pipe = StableDiffusionXLControlNetInpaintPipeline.from_pretrained(
        settings.idm_vton, controlnet=controlnet, torch_dtype=dtype,
        cache_dir=settings.model_cache).to(settings.device)
    pipe.load_ip_adapter(settings.ip_adapter, subfolder="sdxl_models",
                         weight_name="ip-adapter_sdxl.bin")
    pipe.set_progress_bar_config(disable=True)
    pipe.enable_vae_slicing()
    pipe.enable_vae_tiling()
    try:
        pipe.enable_xformers_memory_efficient_attention()
    except Exception:
        pass
    pipe.enable_model_cpu_offload()
    return pipe


def build_agnostic(person: Image.Image, person_mask: Image.Image,
                   face_mask: Image.Image, drape, extend_to_floor: bool):
    h, w = person.height, person.width
    region = np.asarray(person_mask.resize((w, h)))
    region = (region > 100).astype(np.uint8) * 255
    if extend_to_floor:
        region = cv2.dilate(region, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (25, 25)))
    fm = np.asarray(face_mask.resize((w, h)))
    region[fm > 100] = 0
    region = cv2.dilate(region, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11)))

    arr = np.asarray(person).copy()
    grey = np.full_like(arr, 128)
    m = region > 0
    arr[m] = grey[m]
    return Image.fromarray(arr), Image.fromarray(region)
