import numpy as np
from PIL import Image

from vtoe.utils.imaging import (decode_image, encode_image, fit_canvas,
                                has_black_artifacts)


def test_encode_decode_roundtrip(garment_image):
    b64 = encode_image(garment_image)
    out = decode_image(b64)
    assert out.size == garment_image.size and out.mode == "RGB"


def test_decode_handles_data_uri(garment_image):
    b64 = encode_image(garment_image)
    out = decode_image(f"data:image/png;base64,{b64}")
    assert out.size == garment_image.size


def test_fit_canvas_exact_dims(garment_image):
    out = fit_canvas(garment_image, 768, 1024)
    assert out.size == (768, 1024)


def test_black_artifact_detection():
    clean = Image.new("RGB", (100, 100), (200, 100, 100))
    assert not has_black_artifacts(clean)
    holey = np.full((100, 100, 3), 200, np.uint8)
    holey[:30, :30] = 0
    assert has_black_artifacts(Image.fromarray(holey))
