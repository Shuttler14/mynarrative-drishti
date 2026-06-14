import pytest
from PIL import Image

import vtoe.pipeline.engine as engine_mod
from vtoe.pipeline.engine import TryOnEngine
from vtoe.pipeline.stage1_person import PersonAnalysis
from vtoe.pipeline.stage2_garment import GarmentAnalysis
from vtoe.pipeline.stage5_quality import QualityResult
from vtoe.ethnic.drape_params import get as get_drape
from vtoe.utils.memory import VRAMError


@pytest.fixture
def patched(monkeypatch, person_image, garment_image):
    pa = PersonAnalysis(person_image, Image.new("L", (768, 1024), 255),
                        Image.new("RGB", (768, 1024)), object(), True)
    ga = GarmentAnalysis(garment_image, Image.new("L", (768, 1024), 255),
                         "saree", get_drape("saree", "ethnic"), True)
    monkeypatch.setattr(engine_mod.stage1_person, "run", lambda img: pa)
    monkeypatch.setattr(engine_mod.stage2_garment, "run", lambda *a: ga)
    monkeypatch.setattr(engine_mod.stage4_post, "run",
                        lambda gen, person, **k: gen)
    return pa, ga


def test_passes_first_try(patched, person_image, garment_image):
    import vtoe.pipeline.engine as e
    e_calls = {"gen": 0}
    def gen(*a, **k): e_calls["gen"] += 1; return person_image, "idm-vton", ["lora-saree"]
    e.stage3_tryon.run = gen
    e.stage5_quality.run = lambda *a, **k: QualityResult(True, 0.98, 0.91, 0.94, [])
    res = TryOnEngine().run(person_img=person_image, garment_img=garment_image,
                            garment_type="ethnic", sub_type="saree", quality="balanced",
                            preserve_face=True, steps=None, guidance=None, seed=1)
    assert res["status"] == "completed"
    assert res["metadata"]["retries"] == 0 and e_calls["gen"] == 1


def test_retries_on_low_quality_then_keeps_best(patched, person_image, garment_image):
    import vtoe.pipeline.engine as e
    scores = iter([
        QualityResult(False, 0.80, 0.70, 0.75, ["face_sim<0.95"]),
        QualityResult(True, 0.96, 0.88, 0.92, []),
    ])
    e.stage3_tryon.run = lambda *a, **k: (person_image, "idm-vton", [])
    e.stage5_quality.run = lambda *a, **k: next(scores)
    res = TryOnEngine().run(person_img=person_image, garment_img=garment_image,
                            garment_type="ethnic", sub_type="saree", quality="balanced",
                            preserve_face=True, steps=None, guidance=None, seed=1)
    assert res["metadata"]["retries"] == 1
    assert res["quality_score"] == 0.92


def test_oom_downgrades_to_catvton(patched, person_image, garment_image):
    import vtoe.pipeline.engine as e
    state = {"first": True}
    def gen(person, garment, *, model, **k):
        if state["first"]:
            state["first"] = False
            raise VRAMError("stage3.generate", "cuda oom")
        assert model == "cat"
        return person_image, "catvton", []
    e.stage3_tryon.run = gen
    e.stage5_quality.run = lambda *a, **k: QualityResult(True, 0.97, 0.9, 0.93, [])
    res = TryOnEngine().run(person_img=person_image, garment_img=garment_image,
                            garment_type="ethnic", sub_type="saree", quality="balanced",
                            preserve_face=True, steps=None, guidance=None, seed=1)
    assert res["metadata"]["model_used"] == "catvton"
    assert res["metadata"]["retries"] >= 1
