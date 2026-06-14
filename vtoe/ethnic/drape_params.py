from __future__ import annotations
from dataclasses import dataclass


@dataclass
class DrapeParams:
    mask_categories: list[str]
    extend_to_floor: bool
    ip_adapter_scale: float
    prompt_suffix: str
    guidance: float


PARAMS: dict[str, DrapeParams] = {
    "saree":     DrapeParams(["upper", "dress", "skirt"], True, 0.85,
                             "draped saree with pleats and pallu, natural fabric flow", 2.5),
    "lehenga":   DrapeParams(["upper", "skirt", "dress"], True, 0.85,
                             "flared lehenga skirt with choli, heavy embroidery preserved", 2.5),
    "kurta":     DrapeParams(["upper", "dress"], False, 0.7,
                             "long kurta with natural fall, button placket preserved", 2.0),
    "sherwani":  DrapeParams(["upper", "coat", "dress"], False, 0.75,
                             "sherwani with collar and buttons, structured fit", 2.0),
    "dupatta":   DrapeParams(["upper"], False, 0.6,
                             "draped dupatta flowing over shoulder", 1.8),
    "anarkali":  DrapeParams(["upper", "dress"], True, 0.8,
                             "anarkali suit with flared bottom, empire silhouette", 2.3),
    "salwar":    DrapeParams(["upper", "dress", "pants"], False, 0.7,
                             "salwar kameez, natural fall", 2.0),
    "western":   DrapeParams(["upper", "dress", "pants", "skirt"], False, 0.6,
                             "well-fitted garment, photorealistic", 2.0),
}


def get(sub_type: str | None, garment_type: str) -> DrapeParams:
    return PARAMS.get(sub_type or "", PARAMS.get(garment_type, PARAMS["western"]))
