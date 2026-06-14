from __future__ import annotations

LORAS: dict[str, tuple[str, float]] = {
    "saree":    ("mynarrative/lora-saree", 0.8),
    "lehenga":  ("mynarrative/lora-lehenga", 0.75),
    "anarkali": ("mynarrative/lora-kurta", 0.6),
    "kurta":    ("mynarrative/lora-kurta", 0.6),
    "sherwani": ("mynarrative/lora-sherwani", 0.7),
    "dupatta":  ("mynarrative/lora-dupatta", 0.5),
}


def for_subtype(sub_type: str | None) -> tuple[str, float] | None:
    return LORAS.get(sub_type or "")
