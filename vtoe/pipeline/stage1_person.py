from __future__ import annotations
from dataclasses import dataclass

import numpy as np
import torch
from PIL import Image

from vtoe.config import settings
from vtoe.face.detector import FaceDetector
from vtoe.models.loaders import registry
from vtoe.utils.imaging import fit_canvas
from vtoe.utils.memory import vram_guard


@dataclass
class PersonAnalysis:
    person: Image.Image
    person_mask: Image.Image
    pose_image: Image.Image
    face: object | None
    person_detected: bool


def _birefnet_mask(img: Image.Image) -> Image.Image:
    model = registry.get("birefnet")
    from torchvision import transforms
    tf = transforms.Compose([transforms.Resize((1024, 1024)),
                             transforms.ToTensor(),
                             transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])])
    x = tf(img).unsqueeze(0).to(settings.device).half()
    with torch.inference_mode():
        pred = model(x)[-1].sigmoid().cpu()[0, 0]
    mask = (pred.numpy() * 255).astype(np.uint8)
    return Image.fromarray(mask).resize(img.size)


def run(person_img: Image.Image) -> PersonAnalysis:
    person = fit_canvas(person_img, settings.out_w, settings.out_h)
    with vram_guard("stage1.detect"):
        yolo = registry.get("yolo")
        det = yolo(person, classes=[0], verbose=False)
        detected = len(det[0].boxes) > 0
    with vram_guard("stage1.segment"):
        mask = _birefnet_mask(person)
    with vram_guard("stage1.pose"):
        pose_img = registry.get("pose")(person, hand_and_face=False)
    face = FaceDetector().detect_largest(person)
    return PersonAnalysis(person, mask, pose_img, face, detected)
