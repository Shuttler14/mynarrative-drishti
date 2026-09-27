"""Band-crop regression tests — bottom garments must reach VTON, not shoes.

The b3 single-boundary scan inverted on mid-framed shots: the first luminance
change inside the pants is the hem, so band=(hem,1.0) kept legs/shoes and the
trousers never reached the model (user: "bottom wear product is not getting
shown over the user at all").
"""
import io

import numpy as np
from PIL import Image

from api.services.garment_extract import _post_process_garment

BLUE = (60, 110, 200)
BLACK = (25, 25, 30)
SKIN = (225, 180, 150)
SHOE = (245, 245, 248)
GREY = (120, 120, 125)


def _synth(parts: list[tuple[int, int, int, tuple[int, int, int]]],
            h: int = 800, w: int = 200, top_margin: int = 0) -> bytes:
    rgba = np.zeros((h, w, 4), np.uint8)
    for y0, y1, wpx, rgb in parts:
        x0 = (w - wpx) // 2
        rgba[y0 + top_margin:y1 + top_margin, x0:x0 + wpx] = (*rgb, 255)
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG")
    return buf.getvalue()


def _stats(out: bytes) -> tuple[tuple[int, int], float, float]:
    a = np.array(Image.open(io.BytesIO(out)).convert("RGB")).astype(int)
    lum = 0.299 * a[:, :, 0] + 0.587 * a[:, :, 1] + 0.114 * a[:, :, 2]
    nonwhite = lum < 245
    n = max(int(nonwhite.sum()), 1)
    dark = float((lum < 60).sum()) / n          # black pants share
    blue = float(((a[:, :, 2] > a[:, :, 0] + 40) & nonwhite).sum()) / n
    return (a.shape[1], a.shape[0]), dark, blue


MID_FRAMED = [  # shirt sliver, black pants, skin legs, white shoes
    (0, 90, 140, BLUE), (90, 560, 120, BLACK),
    (560, 730, 90, SKIN), (730, 800, 110, SHOE),
]
FULL_FRAMED = [  # head, shirt, pants, legs, narrow shoes (feet detection)
    (0, 70, 60, SKIN), (70, 300, 160, BLUE), (300, 640, 140, BLACK),
    (640, 760, 90, SKIN), (760, 800, 70, SHOE),
]
LOWER_FRAMED = [  # pants fill top of frame, hem + shoes below
    (0, 700, 120, BLACK), (700, 760, 90, SKIN), (760, 800, 70, SHOE),
]


def test_bottom_mid_framed_keeps_pants_not_shoes():
    _, dark, _ = _stats(_post_process_garment(_synth(MID_FRAMED), "bottom"))
    assert dark >= 0.5, f"pants share {dark:.2f} — shoes/legs leaked, trousers dropped"


def test_bottom_full_framed_keeps_pants():
    (w, h), dark, _ = _stats(_post_process_garment(_synth(FULL_FRAMED), "bottom"))
    assert dark >= 0.5, f"pants share {dark:.2f}"
    assert h > w, "crop must stay a tall trousers silhouette"


def test_bottom_lower_framed_keeps_pants_drops_shoes():
    _, dark, _ = _stats(_post_process_garment(_synth(LOWER_FRAMED), "bottom"))
    assert dark >= 0.6, f"pants share {dark:.2f} — shoes/legs leaked"


def test_bottom_with_top_margin_still_finds_pants():
    _, dark, _ = _stats(_post_process_garment(_synth(MID_FRAMED, top_margin=60), "bottom"))
    assert dark >= 0.5, f"pants share {dark:.2f} on margin image"


def test_top_still_keeps_shirt():
    _, _, blue = _stats(_post_process_garment(_synth(FULL_FRAMED), "top"))
    assert blue >= 0.4, f"shirt share {blue:.2f} — top extraction regressed"


def test_flatlay_passes_through_unbanded():
    out = _post_process_garment(_synth([(100, 500, 120, GREY)]), "bottom")
    (w, h), _, _ = _stats(out)
    assert h > 300, f"flat-lay got band-cropped to h={h}"
    a = np.array(Image.open(io.BytesIO(out)).convert("RGB")).astype(int)
    mid = a[a.shape[0] // 2, a.shape[1] // 2]
    assert abs(int(mid.mean()) - 120) < 25, f"flat-lay garment altered: center={mid}"
