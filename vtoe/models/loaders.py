from __future__ import annotations
import torch

from vtoe.config import settings
from vtoe.models.registry import ModelRegistry

DTYPE = torch.float16

registry = ModelRegistry(settings.vram_budget_gb)


def _yolo():
    from ultralytics import YOLO
    return YOLO(settings.yolo)


def _birefnet():
    from transformers import AutoModelForImageSegmentation
    m = AutoModelForImageSegmentation.from_pretrained(
        settings.birefnet, trust_remote_code=True).to(settings.device).half().eval()
    return m


def _pose():
    from controlnet_aux import OpenposeDetector
    return OpenposeDetector.from_pretrained("lllyasviel/Annotators")


def _arcface():
    from insightface.app import FaceAnalysis
    app = FaceAnalysis(name=settings.arcface,
                       allowed_modules=["detection", "recognition"])
    app.prepare(ctx_id=0, det_size=(640, 640))
    return app


def _face_swapper():
    import insightface
    return insightface.model_zoo.get_model("inswapper_128.onnx")


def _codeformer():
    from vtoe.face._codeformer import CodeFormerRestorer
    return CodeFormerRestorer(settings)


def _clip():
    from transformers import CLIPModel, CLIPProcessor
    model = CLIPModel.from_pretrained(settings.clip).to(settings.device).half().eval()
    proc = CLIPProcessor.from_pretrained(settings.clip)
    return model, proc


def _idm():
    from vtoe.pipeline._idm_pipe import build_idm_pipeline
    return build_idm_pipeline(settings, DTYPE)


def _cat():
    from diffusers import AutoPipelineForInpainting
    pipe = AutoPipelineForInpainting.from_pretrained(
        settings.catvton, torch_dtype=DTYPE, cache_dir=settings.model_cache
    ).to(settings.device)
    pipe.set_progress_bar_config(disable=True)
    try:
        pipe.enable_xformers_memory_efficient_attention()
    except Exception:
        pass
    return pipe


for name, fn in {"yolo": _yolo, "birefnet": _birefnet, "pose": _pose,
                 "arcface": _arcface, "swapper": _face_swapper, "codeformer": _codeformer,
                 "clip": _clip, "idm": _idm, "cat": _cat}.items():
    registry.register(name, fn)
