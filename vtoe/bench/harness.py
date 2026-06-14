"""Validate the FASHN-comparison claims on YOUR T4 before publishing them.

Measures, per quality tier and per garment sub-type:
- p50 / p95 latency (warm; first run excluded as cold-load)
- peak VRAM
- face similarity, garment (CLIP) similarity
- optional FID against a folder of real fashion photos

Run:
  python -m vtoe.bench.harness --pairs bench/pairs.json --runs 20 \
         --quality balanced --real-photos bench/real_fashion/
"""
from __future__ import annotations
import argparse
import json
import statistics
import time

import torch

from vtoe.config import settings
from vtoe.pipeline.engine import TryOnEngine
from vtoe.utils.imaging import decode_image
from vtoe.utils.memory import empty_cache, vram_gb


def _peak_vram_reset():
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()


def _peak_vram_gb() -> float:
    return torch.cuda.max_memory_allocated() / 1e9 if torch.cuda.is_available() else 0.0


def run_bench(pairs: list[dict], runs: int, quality: str,
              real_photos: str | None) -> dict:
    engine = TryOnEngine()
    latencies: list[int] = []
    face_sims: list[float] = []
    garm_sims: list[float] = []
    peak_vram = 0.0
    by_subtype: dict[str, list[int]] = {}
    outputs_for_fid: list = []

    p0 = pairs[0]
    engine.run(person_img=decode_image(p0["person"]), garment_img=decode_image(p0["garment"]),
               garment_type=p0.get("garment_type", "ethnic"), sub_type=p0.get("sub_type"),
               quality=quality, preserve_face=True, steps=None, guidance=None, seed=42)
    empty_cache()

    for i in range(runs):
        p = pairs[i % len(pairs)]
        _peak_vram_reset()
        t0 = time.time()
        res = engine.run(
            person_img=decode_image(p["person"]), garment_img=decode_image(p["garment"]),
            garment_type=p.get("garment_type", "ethnic"), sub_type=p.get("sub_type"),
            quality=quality, preserve_face=True, steps=None, guidance=None, seed=i)
        ms = int((time.time() - t0) * 1000)

        latencies.append(ms)
        face_sims.append(res["face_similarity"])
        garm_sims.append(res["garment_similarity"])
        peak_vram = max(peak_vram, _peak_vram_gb())
        st = res["metadata"]["garment_sub_type"]
        by_subtype.setdefault(st, []).append(ms)
        outputs_for_fid.append(res["result"])
        empty_cache()

    def pct(xs, q):
        xs = sorted(xs)
        return xs[min(len(xs) - 1, int(len(xs) * q))]

    report = {
        "quality_tier": quality,
        "runs": runs,
        "latency_ms": {"p50": pct(latencies, 0.5), "p95": pct(latencies, 0.95),
                       "mean": int(statistics.mean(latencies))},
        "peak_vram_gb": round(peak_vram, 2),
        "vram_budget_gb": settings.vram_budget_gb,
        "fits_t4": peak_vram < 15.0,
        "face_similarity": {"mean": round(statistics.mean(face_sims), 3),
                            "min": round(min(face_sims), 3),
                            "pass_rate@0.95": round(sum(s >= 0.95 for s in face_sims) / runs, 3)},
        "garment_similarity": {"mean": round(statistics.mean(garm_sims), 3),
                               "pass_rate@0.85": round(sum(s >= 0.85 for s in garm_sims) / runs, 3)},
        "latency_by_subtype_ms": {k: int(statistics.mean(v)) for k, v in by_subtype.items()},
    }

    if real_photos:
        report["fid"] = _compute_fid(outputs_for_fid, real_photos)

    report["target_check"] = {
        "p50<=10s": report["latency_ms"]["p50"] <= 10_000,
        "p95<=20s": report["latency_ms"]["p95"] <= 20_000,
        "face>=0.97_mean": report["face_similarity"]["mean"] >= 0.97,
        "garment>=0.93_mean": report["garment_similarity"]["mean"] >= 0.93,
    }
    return report


def _compute_fid(outputs, real_dir: str) -> float:
    import tempfile, os
    from cleanfid import fid
    with tempfile.TemporaryDirectory() as tmp:
        for i, img in enumerate(outputs):
            img.save(os.path.join(tmp, f"{i}.png"))
        return round(fid.compute_fid(tmp, real_dir), 2)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", required=True, help="JSON list of {person, garment, sub_type?}")
    ap.add_argument("--runs", type=int, default=20)
    ap.add_argument("--quality", default="balanced")
    ap.add_argument("--real-photos", default=None)
    a = ap.parse_args()
    pairs = json.load(open(a.pairs))
    report = run_bench(pairs, a.runs, a.quality, a.real_photos)
    print(json.dumps(report, indent=2))
