from __future__ import annotations
from dataclasses import dataclass

from PIL import Image

from vtoe.config import settings
from vtoe.face.verifier import FaceVerifier
from vtoe.quality.metrics import clip_similarity
from vtoe.utils.imaging import has_black_artifacts


@dataclass
class QualityResult:
    passed: bool
    face_similarity: float
    garment_similarity: float
    quality_score: float
    reasons: list[str]


def run(original: Image.Image, garment: Image.Image, output: Image.Image,
        *, preserve_face: bool) -> QualityResult:
    reasons: list[str] = []
    face_sim = FaceVerifier().similarity(original, output) if preserve_face else 1.0
    garm_sim = clip_similarity(garment, output)

    if has_black_artifacts(output):
        reasons.append("black_artifacts")
    if preserve_face and 0 <= face_sim < settings.face_sim_min:
        reasons.append(f"face_sim<{settings.face_sim_min}")
    if garm_sim < settings.garment_sim_min:
        reasons.append(f"garment_sim<{settings.garment_sim_min}")

    score = round(0.5 * max(face_sim, 0) + 0.5 * garm_sim, 3)
    return QualityResult(not reasons, round(face_sim, 3), round(garm_sim, 3), score, reasons)
