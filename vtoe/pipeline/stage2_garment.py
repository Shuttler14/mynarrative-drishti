from __future__ import annotations
from dataclasses import dataclass

import numpy as np
from PIL import Image

from vtoe.config import settings
from vtoe.ethnic.classifier import EthnicClassifier
from vtoe.ethnic.drape_params import DrapeParams, get as get_drape
from vtoe.pipeline.stage1_person import _birefnet_mask
from vtoe.utils.imaging import fit_canvas
from vtoe.utils.memory import vram_guard


@dataclass
class GarmentAnalysis:
    garment: Image.Image
    garment_mask: Image.Image
    sub_type: str
    drape: DrapeParams
    segmented: bool


def run(garment_img: Image.Image, garment_type: str, sub_type: str | None) -> GarmentAnalysis:
    g = fit_canvas(garment_img, settings.out_w, settings.out_h)
    with vram_guard("stage2.segment"):
        mask = _birefnet_mask(g)
    segmented = np.asarray(mask).mean() > 5
    if sub_type is None and garment_type == "ethnic":
        with vram_guard("stage2.classify"):
            sub_type = EthnicClassifier().classify(g)
    drape = get_drape(sub_type, garment_type)
    return GarmentAnalysis(g, mask, sub_type or garment_type, drape, segmented)
