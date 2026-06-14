from __future__ import annotations
import base64
import io

import cv2
import numpy as np
from PIL import Image, ImageOps


def decode_image(data: str) -> Image.Image:
    if data.startswith("data:"):
        data = data.split(",", 1)[1]
    raw = base64.b64decode(data)
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def encode_image(img: Image.Image, fmt: str = "PNG") -> str:
    buf = io.BytesIO()
    img.save(buf, format=fmt, optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def fit_canvas(img: Image.Image, w: int, h: int) -> Image.Image:
    return ImageOps.pad(img, (w, h), color=(255, 255, 255), method=Image.LANCZOS)


def to_cv(img: Image.Image) -> np.ndarray:
    return cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)


def to_pil(arr: np.ndarray) -> Image.Image:
    return Image.fromarray(cv2.cvtColor(arr, cv2.COLOR_BGR2RGB))


def has_black_artifacts(img: Image.Image, threshold: float = 0.02) -> bool:
    a = np.asarray(img.convert("RGB"))
    black = np.all(a < 8, axis=-1)
    return black.mean() > threshold
