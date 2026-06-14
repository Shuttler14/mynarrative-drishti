from __future__ import annotations
import numpy as np
from PIL import Image

from vtoe.models.loaders import registry
from vtoe.utils.imaging import to_cv


class FaceVerifier:
    def similarity(self, a: Image.Image, b: Image.Image) -> float:
        app = registry.get("arcface")
        fa, fb = app.get(to_cv(a)), app.get(to_cv(b))
        if not fa or not fb:
            return -1.0
        ea = max(fa, key=lambda f: f.bbox[2]-f.bbox[0]).normed_embedding
        eb = max(fb, key=lambda f: f.bbox[2]-f.bbox[0]).normed_embedding
        return float(np.dot(ea, eb))
