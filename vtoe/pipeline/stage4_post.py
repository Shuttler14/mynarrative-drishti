from __future__ import annotations
import cv2
import numpy as np
from PIL import Image

from vtoe.config import settings
from vtoe.face.preserver import FacePreserver
from vtoe.pipeline.stage1_person import PersonAnalysis
from vtoe.utils.imaging import to_cv, to_pil
from vtoe.utils.memory import vram_guard


def _harmonize_lighting(render: Image.Image, source: Image.Image) -> Image.Image:
    r = cv2.cvtColor(np.asarray(render), cv2.COLOR_RGB2LAB).astype(np.float32)
    s = cv2.cvtColor(np.asarray(source.resize(render.size)), cv2.COLOR_RGB2LAB).astype(np.float32)
    rb = cv2.GaussianBlur(r[..., 0], (0, 0), 25)
    sb = cv2.GaussianBlur(s[..., 0], (0, 0), 25)
    r[..., 0] = np.clip(r[..., 0] + (sb - rb) * 0.5, 0, 255)
    return Image.fromarray(cv2.cvtColor(r.astype(np.uint8), cv2.COLOR_LAB2RGB))


def run(generated: Image.Image, person: PersonAnalysis, *,
        preserve_face: bool, use_codeformer: bool) -> Image.Image:
    out = _harmonize_lighting(generated, person.person)
    if preserve_face and person.face is not None:
        with vram_guard("stage4.face"):
            out = FacePreserver().restore(person.person, out, use_codeformer=use_codeformer)
    return out
