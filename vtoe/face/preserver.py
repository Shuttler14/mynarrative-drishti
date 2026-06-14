from __future__ import annotations
import cv2
import numpy as np
from PIL import Image

from vtoe.face.detector import FaceDetector
from vtoe.models.loaders import registry
from vtoe.utils.imaging import to_cv, to_pil


class FacePreserver:
    def __init__(self) -> None:
        self.detector = FaceDetector()

    def face_region_mask(self, img: Image.Image) -> Image.Image:
        face = self.detector.detect_largest(img)
        mask = np.zeros((img.height, img.width), np.uint8)
        if face is None:
            return Image.fromarray(mask)
        x0, y0, x1, y1 = map(int, face.bbox)
        pad = int(0.3 * (y1 - y0))
        cv2.ellipse(mask, ((x0+x1)//2, (y0+y1)//2),
                    ((x1-x0)//2 + pad, (y1-y0)//2 + pad), 0, 0, 360, 255, -1)
        return Image.fromarray(mask)

    def restore(self, original: Image.Image, generated: Image.Image,
                *, use_codeformer: bool) -> Image.Image:
        app = registry.get("arcface")
        gen_cv = to_cv(generated.resize(original.size))
        orig_cv = to_cv(original)

        src_faces = app.get(orig_cv)
        dst_faces = app.get(gen_cv)
        if not src_faces or not dst_faces:
            return self._poisson_fallback(original, generated)

        src = max(src_faces, key=lambda f: f.bbox[2]-f.bbox[0])
        dst = max(dst_faces, key=lambda f: f.bbox[2]-f.bbox[0])
        swapper = registry.get("swapper")
        out = swapper.get(gen_cv, dst, src, paste_back=True)

        if use_codeformer:
            out = to_cv(registry.get("codeformer").enhance(to_pil(out)))
        return to_pil(out).resize(generated.size)

    def _poisson_fallback(self, original: Image.Image, generated: Image.Image) -> Image.Image:
        face = self.detector.detect_largest(original)
        if face is None:
            return generated
        orig = to_cv(original)
        gen = to_cv(generated.resize(original.size))
        x0, y0, x1, y1 = map(int, face.bbox)
        m = np.zeros(orig.shape[:2], np.uint8)
        cv2.ellipse(m, ((x0+x1)//2, (y0+y1)//2), ((x1-x0)//2, (y1-y0)//2), 0, 0, 360, 255, -1)
        blended = cv2.seamlessClone(orig, gen, m, ((x0+x1)//2, (y0+y1)//2), cv2.NORMAL_CLONE)
        return to_pil(blended).resize(generated.size)
