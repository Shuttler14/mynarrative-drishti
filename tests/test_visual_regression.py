"""Visual regression: when run on a real GPU (RUN_GPU=1), produces try-on
outputs for a fixed seed and compares against committed golden hashes /
SSIM, catching silent pipeline regressions. Skipped in CI by default."""
import os
import pytest

pytestmark = pytest.mark.skipif(os.getenv("RUN_GPU") != "1",
                                reason="GPU + golden assets required")


def test_saree_output_matches_golden(tmp_path):
    import json
    from skimage.metrics import structural_similarity as ssim
    import numpy as np
    from PIL import Image
    from vtoe.pipeline.engine import TryOnEngine
    from vtoe.utils.imaging import decode_image

    pairs = json.load(open("bench/golden/pairs.json"))
    p = pairs[0]
    res = TryOnEngine().run(
        person_img=decode_image(p["person"]), garment_img=decode_image(p["garment"]),
        garment_type="ethnic", sub_type="saree", quality="balanced",
        preserve_face=True, steps=30, guidance=2.5, seed=12345)
    golden = Image.open("bench/golden/saree_out.png").resize(res["result"].size)
    score = ssim(np.asarray(golden.convert("L")),
                 np.asarray(res["result"].convert("L")))
    assert score >= 0.85, f"visual regression: ssim={score:.3f}"
    assert res["face_similarity"] >= 0.90
