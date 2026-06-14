from __future__ import annotations
import random
import time
import uuid

from PIL import Image

from vtoe.config import settings
from vtoe.models.loaders import registry
from vtoe.pipeline import stage1_person, stage2_garment, stage3_tryon, stage4_post, stage5_quality
from vtoe.utils.memory import VRAMError, empty_cache


class TryOnEngine:
    def run(self, *, person_img: Image.Image, garment_img: Image.Image,
            garment_type: str, sub_type: str | None, quality: str,
            preserve_face: bool, steps: int | None, guidance: float | None,
            seed: int | None) -> dict:
        job_id = str(uuid.uuid4())
        t0 = time.time()
        steps = steps or settings.steps_by_quality[quality]
        use_codeformer = quality == "quality"
        seed = seed if seed is not None else random.randint(0, 2**31 - 1)

        person = stage1_person.run(person_img)
        garment = stage2_garment.run(garment_img, garment_type, sub_type)
        guidance = guidance or garment.drape.guidance

        model = "idm"
        retries = 0
        best = None
        for attempt in range(settings.max_retries + 1):
            try:
                gen, used, loras = stage3_tryon.run(
                    person, garment, model=model, steps=steps, guidance=guidance, seed=seed)
                final = stage4_post.run(gen, person, preserve_face=preserve_face,
                                        use_codeformer=use_codeformer)
                q = stage5_quality.run(person.person, garment.garment, final,
                                       preserve_face=preserve_face)
            except VRAMError as e:
                empty_cache(); model = "cat"; steps = min(steps, 20)
                retries += 1
                continue

            if best is None or q.quality_score > best[1].quality_score:
                best = (final, q, used, loras)
            if q.passed:
                break
            retries += 1
            seed = random.randint(0, 2**31 - 1)
            steps = min(steps + 10, 50)
            if "garment_sim" in " ".join(q.reasons) and garment_type == "ethnic":
                model = "cat"

        final, q, used, loras = best
        return {
            "job_id": job_id,
            "status": "completed" if q.passed else "completed_low_quality",
            "result": final,
            "processing_time_ms": int((time.time() - t0) * 1000),
            "quality_score": q.quality_score,
            "face_similarity": q.face_similarity,
            "garment_similarity": q.garment_similarity,
            "metadata": {
                "person_detected": person.person_detected,
                "face_detected": person.face is not None,
                "garment_segmented": garment.segmented,
                "garment_sub_type": garment.sub_type,
                "model_used": used,
                "loras": loras,
                "retries": retries,
                "quality_reasons": q.reasons,
            },
        }
