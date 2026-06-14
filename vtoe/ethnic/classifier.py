from __future__ import annotations
from PIL import Image

from vtoe.models.loaders import registry

_ETHNIC = ["saree", "lehenga", "kurta", "sherwani", "dupatta", "anarkali", "salwar"]
_PROMPTS = {k: f"a {k}, indian ethnic wear" for k in _ETHNIC}
_PROMPTS["western"] = "western clothing, shirt dress jeans"


class EthnicClassifier:
    def classify(self, garment: Image.Image) -> str:
        model, proc = registry.get("clip")
        import torch
        import torch.nn.functional as F
        labels = list(_PROMPTS.values())
        inputs = proc(text=labels, images=garment, return_tensors="pt",
                      padding=True).to(model.device)
        with torch.inference_mode():
            out = model(**{k: v.half() if v.is_floating_point() else v for k, v in inputs.items()})
        probs = F.softmax(out.logits_per_image, dim=-1)[0]
        idx = int(probs.argmax())
        return list(_PROMPTS.keys())[idx]
