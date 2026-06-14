import numpy as np
from PIL import Image

from vtoe.ethnic.drape_params import get as get_drape
from vtoe.pipeline._idm_pipe import build_agnostic


def _mask(box, size=(768, 1024)):
    arr = np.zeros((size[1], size[0]), np.uint8)
    x0, y0, x1, y1 = box
    arr[y0:y1, x0:x1] = 255
    return Image.fromarray(arr)


def test_face_region_never_masked():
    person = Image.new("RGB", (768, 1024), (200, 180, 160))
    person_mask = _mask([280, 120, 490, 820])
    face_mask = _mask([320, 120, 450, 260])
    drape = get_drape("kurta", "ethnic")
    _, mask = build_agnostic(person, person_mask, face_mask, drape, extend_to_floor=False)
    m = np.asarray(mask)
    assert m[190, 385] == 0
    assert m[500, 385] > 0


def test_ethnic_extends_lower():
    person = Image.new("RGB", (768, 1024), (200, 180, 160))
    pm = _mask([280, 280, 490, 700])
    fm = _mask([320, 120, 450, 260])
    saree = get_drape("saree", "ethnic")
    _, mask = build_agnostic(person, pm, fm, saree, extend_to_floor=True)
    assert np.asarray(mask)[710, 385] > 0
