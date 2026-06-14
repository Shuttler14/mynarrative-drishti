from __future__ import annotations
from PIL import Image

from vtoe.models.loaders import registry
from vtoe.utils.imaging import to_cv


class FaceDetector:
    def detect_largest(self, img: Image.Image):
        app = registry.get("arcface")
        faces = app.get(to_cv(img))
        if not faces:
            return None
        return max(faces, key=lambda f: (f.bbox[2]-f.bbox[0])*(f.bbox[3]-f.bbox[1]))

    def is_frontal(self, face) -> bool:
        return abs(getattr(face, "pose", [0, 0, 0])[1]) < 45
