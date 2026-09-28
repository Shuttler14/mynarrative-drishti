#!/usr/bin/env python3
"""Fashion Recommendation Evaluation System — benchmark, scorecard, release gates.

Never judge a change by "it looks better." Every recommender version must pass
the SAME gates (bench/gates.json):

  offline   deterministic engine benchmark: intent accuracy, hard-filter
            false-passes, outfit validity + P50/P95/P99 engine latencies
  live      deployed /api/reco/outfits: P50/P95/P99 end-to-end, error rate,
            independent re-validation of returned outfits, deployed version
  vton      garment extraction success + latency, try-on success + latency,
            end-to-end VTON (extract + try-on), human-label gates if labelled
  metrics   personalization rates from user_events (save / vton / purchase)
  scorecard merge latest results of each kind → dashboard + gate verdicts,
            saved under bench/results/ for version-over-version deltas
  gates     offline + scorecard  (exit 1 on hard-gate fail) — quick loop
  release   offline + live + vton + metrics + scorecard --strict (full gate)

Typical flow when changing the recommender:
  1. bump RECO_VERSION (api/services/reco_version.py) + docs/RECO_VERSIONS.md
  2. python bench/run_bench.py gates        (fast, deterministic)
  3. python bench/run_bench.py release      (full: prod latency + VTON)
  4. commit only if both are green.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import glob
import json
import math
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parent
REPO = ROOT.parent
GOLDEN = ROOT / "golden"
RESULTS = ROOT / "results"
GATES = json.loads((ROOT / "gates.json").read_text())

sys.path.insert(0, str(REPO))

UA = {"User-Agent": "Mozilla/5.0 (DrishtiBench/1.0)"}
STALE_HOURS = 24
OFFLINE_REPS = {"intent": 20, "filter": 10, "validate": 10, "blueprint": 5}


# ────────────────────────────── helpers ──────────────────────────────

def pct(vals, p):
    """Nearest-rank percentile."""
    if not vals:
        return None
    s = sorted(vals)
    k = max(0, math.ceil(p / 100 * len(s)) - 1)
    return round(s[k], 4)


def lat_block(vals):
    return {
        "p50": pct(vals, 50),
        "p95": pct(vals, 95),
        "p99": pct(vals, 99),
        "n": len(vals),
        "max": round(max(vals), 2) if vals else None,
    }


def now_iso():
    return dt.datetime.now().isoformat(timespec="seconds")


def stamp():
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def local_version():
    from api.services.reco_version import RECO_VERSION
    return RECO_VERSION


def save_result(kind, data):
    RESULTS.mkdir(parents=True, exist_ok=True)
    data.setdefault("_ts", now_iso())
    data.setdefault("_kind", kind)
    path = RESULTS / f"{kind}_{data.get('version', 'x')}_{stamp()}.json"
    path.write_text(json.dumps(data, indent=1, default=str))
    print(f"  saved → {path.relative_to(REPO)}")
    return path


def latest(kind, max_age_h=None):
    files = sorted(glob.glob(str(RESULTS / f"{kind}_*.json")), key=os.path.getmtime)
    if not files:
        return None
    path = files[-1]
    data = json.loads(pathlib.Path(path).read_text())
    data["_file"] = str(pathlib.Path(path).relative_to(REPO))
    ts = data.get("_ts")
    if max_age_h and ts:
        age = (dt.datetime.now() - dt.datetime.fromisoformat(ts)).total_seconds() / 3600
        data["_age_h"] = round(age, 1)
        data["_stale"] = age > max_age_h
    return data


def http_json(url, payload=None, method=None, timeout=40, headers=None):
    """Returns (status_code_or_0, elapsed_ms, parsed_dict_or_{})."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method or ("POST" if data else "GET"),
        headers={**UA, "Content-Type": "application/json", **(headers or {})},
    )
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode() or "{}")
            return r.status, (time.perf_counter() - t) * 1000, body
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode() or "{}")
        except Exception:
            body = {}
        return e.code, (time.perf_counter() - t) * 1000, body
    except Exception:
        return 0, (time.perf_counter() - t) * 1000, {}


def http_multipart(url, file_path, timeout=90):
    boundary = uuid.uuid4().hex
    raw = pathlib.Path(file_path).read_bytes()
    fname = pathlib.Path(file_path).name
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{fname}"\r\n'
        f"Content-Type: image/jpeg\r\n\r\n"
    ).encode() + raw + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        **UA, "Content-Type": f"multipart/form-data; boundary={boundary}"})
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, (time.perf_counter() - t) * 1000, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            b = json.loads(e.read().decode() or "{}")
        except Exception:
            b = {}
        return e.code, (time.perf_counter() - t) * 1000, b
    except Exception:
        return 0, (time.perf_counter() - t) * 1000, {}


def data_uri(path):
    b64 = base64.b64encode(pathlib.Path(path).read_bytes()).decode()
    return f"data:image/jpeg;base64,{b64}"


def resolve_asset(spec):
    if "url" in spec:
        return spec["url"], False
    return data_uri(GOLDEN / spec["file"]), True


def cmp_ok(op, actual, expected):
    if actual is None:
        return None
    return {"<=": actual <= expected, ">=": actual >= expected,
            "<": actual < expected, ">": actual > expected}[op] is True


# ────────────────────────────── 1. offline ──────────────────────────────

def run_offline(save=True):
    from api.services.fashion_engine import build_intent, hard_filter, enrich_product
    from api.services import fashion_rules as fr

    print("== OFFLINE benchmark (deterministic, no network) ==")
    cases = json.loads((GOLDEN / "reco_cases.json").read_text())
    pool = json.loads((GOLDEN / "fixture_products.json").read_text())["products"]
    for p in pool:
        enrich_product(p)
    by_id = {p["id"]: p for p in pool}
    version = local_version()

    def timed(fn, reps):
        samples, last = [], None
        for _ in range(reps):
            t = time.perf_counter()
            last = fn()
            samples.append((time.perf_counter() - t) * 1000)
        return last, samples

    # ── intent: occasion accuracy + latency
    occ_ok = dc_ok = 0
    intent_lat, fails = [], []
    for ic in cases["intent_cases"]:
        _, samples = timed(
            lambda ic=ic: build_intent(occasion=ic["occasion"], style=ic.get("style", "")),
            OFFLINE_REPS["intent"])
        intent_lat += samples
        got = build_intent(occasion=ic["occasion"], style=ic.get("style", ""))
        exp = ic["expect"]
        if got["occasion"] == exp["occasion"]:
            occ_ok += 1
        else:
            fails.append(f"{ic['id']}: occasion {got['occasion']} != {exp['occasion']}")
        if got["dress_code"] == exp["dress_code"]:
            dc_ok += 1
        else:
            fails.append(f"{ic['id']}: dress_code {got['dress_code']} != {exp['dress_code']}")

    # ── hard filter: false-passes = hard-rule violations
    reject_n = accept_n = false_pass = accept_miss = 0
    filter_lat = []
    for fc in cases["filter_cases"]:
        intent = build_intent(occasion=fc["occasion"], style=fc.get("style", ""),
                              gender=fc.get("gender", ""), user_prefs=fc.get("user_prefs"))
        kept, samples = timed(lambda: {p["id"] for p in hard_filter(pool, intent, None)},
                              OFFLINE_REPS["filter"])
        filter_lat += samples
        for pid in fc["reject"]:
            reject_n += 1
            if pid in kept:
                false_pass += 1
                fails.append(f"{fc['id']}: HARD false-pass {pid} ({by_id[pid]['title']})")
        for pid in fc["accept"]:
            accept_n += 1
            if pid not in kept:
                accept_miss += 1
                fails.append(f"{fc['id']}: miss-accept {pid} ({by_id[pid]['title']})")

    # ── outfit validation: validity + violation recall
    valid_n = valid_pass = invalid_n = invalid_caught = 0
    validate_lat = []
    for oc in cases["outfit_cases"]:
        intent = build_intent(occasion=oc["occasion"])
        products = [by_id[i] for i in oc["products"]]
        res, samples = timed(lambda: fr.validate_outfit(products, intent),
                             OFFLINE_REPS["validate"])
        validate_lat += samples
        if oc["expect_passed"]:
            valid_n += 1
            if res["passed"]:
                valid_pass += 1
            else:
                fails.append(f"{oc['id']}: valid outfit REJECTED {res['violations']}")
        else:
            invalid_n += 1
            caught = (not res["passed"]) and any(
                oc["expect_contains"] in v for v in res["violations"])
            if caught:
                invalid_caught += 1
            else:
                fails.append(f"{oc['id']}: invalid outfit MISSED passed={res['passed']} {res['violations']}")

    # ── outfit generation (blueprint assembly) latency
    blueprint_lat = []
    for ic in cases["intent_cases"]:
        intent = build_intent(occasion=ic["occasion"], style=ic.get("style", ""))
        _, samples = timed(lambda intent=intent: fr.build_outfit_blueprints(intent, n=3),
                           OFFLINE_REPS["blueprint"])
        blueprint_lat += samples

    n_intent = len(cases["intent_cases"])
    metrics = {
        "occasion_accuracy_pct": round(100 * occ_ok / n_intent, 2),
        "dress_code_accuracy_pct": round(100 * dc_ok / n_intent, 2),
        "hard_rule_violation_pct": round(100 * false_pass / max(reject_n, 1), 2),
        "filter_reject_n": reject_n,
        "filter_accept_recall_pct": round(100 * (accept_n - accept_miss) / max(accept_n, 1), 2),
        "filter_accept_n": accept_n,
        "outfit_validity_pct": round(100 * valid_pass / max(valid_n, 1), 2),
        "outfit_valid_n": valid_n,
        "violation_recall_pct": round(100 * invalid_caught / max(invalid_n, 1), 2),
        "outfit_invalid_n": invalid_n,
        "intent_ms": lat_block(intent_lat),
        "filter_ms": lat_block(filter_lat),
        "validation_ms": lat_block(validate_lat),
        "outfit_generation_ms": lat_block(blueprint_lat),
        "fixture_products": len(pool),
        "cases": n_intent + len(cases["filter_cases"]) + len(cases["outfit_cases"]),
    }
    # flat latency fields for gate evaluation
    metrics["intent_ms_p95"] = metrics["intent_ms"]["p95"]
    metrics["validation_ms_p95"] = metrics["validation_ms"]["p95"]
    metrics["outfit_generation_ms_p95"] = metrics["outfit_generation_ms"]["p95"]
    result = {"version": version, "metrics": metrics, "failures": fails}
    print(f"  intent occ {metrics['occasion_accuracy_pct']}% | hard violations "
          f"{metrics['hard_rule_violation_pct']}% ({false_pass}/{reject_n}) | "
          f"validity {metrics['outfit_validity_pct']}% | recall {metrics['violation_recall_pct']}% | "
          f"intent p95 {metrics['intent_ms_p95']}ms | fails: {len(fails)}")
    for f in fails:
        print(f"    ✗ {f}")
    if save:
        save_result("offline", result)
    return result


# ────────────────────────────── 2. live ──────────────────────────────

LIVE_CASES = [
    {"id": "L1", "occasion": "office", "style": "western", "gender": "female", "wear": ["western"],
     "_note": "strict western office (v4.2 gate)"},
    {"id": "L2", "occasion": "job interview", "gender": "male", "_note": "business_formal"},
    {"id": "L3", "occasion": "wedding", "gender": "female", "_note": "ethnic wedding guest"},
    {"id": "L4", "occasion": "casual", "gender": "female", "_note": "everyday"},
    {"id": "L5", "occasion": "cocktail", "gender": "male", "_note": "party → cocktail"},
    {"id": "L6", "occasion": "diwali", "gender": "female", "_note": "festive"},
]


def run_live(base, n=None):
    print(f"== LIVE benchmark vs {base} ==")
    from api.services.fashion_engine import enrich_product, hard_filter, build_intent
    from api.services import fashion_rules as fr

    status_v, _, ver_body = http_json(f"{base}/api/reco/version", timeout=10)
    deployed = ver_body.get("reco_version") if status_v == 200 else "unknown"
    cases = LIVE_CASES[:n] if n else LIVE_CASES
    lats, errors, successes = [], 0, 0
    validity_pass = validity_n = 0
    server_valid_pass = server_valid_n = 0
    relevance_hits = relevance_n = 0
    details = []

    for c in cases:
        payload = {"occasion": c["occasion"], "style": c.get("style", ""),
                   "gender": c.get("gender", ""), "wear": c.get("wear", []), "count": 6}
        st, ms, body = http_json(f"{base}/api/reco/outfits", payload, timeout=40)
        lats.append(ms)
        recs = body.get("recommendations") or body.get("outfits") or []
        ok = st == 200 and len(recs) > 0
        if not ok:
            errors += 1
        else:
            successes += 1

        # server-side validation verdict
        sv = body.get("outfit_validation") or {}
        if sv:
            server_valid_n += 1
            server_valid_pass += 1 if sv.get("passed") else 0

        # independent re-validation: enrich + per-slot assembly
        if recs:
            intent = build_intent(occasion=c["occasion"], style=c.get("style", ""),
                                  gender=c.get("gender", ""),
                                  user_prefs={"wear_weights": {w: 1.0 for w in c.get("wear", [])}} if c.get("wear") else None)
            enriched = []
            for r in recs:
                p = enrich_product({"title": r.get("title", ""), "price": r.get("price") or 1})
                enriched.append(p)
            kept = hard_filter(enriched, intent, None)
            relevance_hits += len(kept)
            relevance_n += len(enriched)

            fulls = [p for p in enriched if p["fashion"]["slot"] == "full"]
            if fulls:
                outfit = fulls[:1] + [p for p in enriched if p["fashion"]["slot"] == "shoes"][:1]
            else:
                outfit = []
                for slot in ("top", "bottom", "shoes"):
                    hits = [p for p in enriched if p["fashion"]["slot"] == slot]
                    if hits:
                        outfit.append(hits[0])
            if outfit:
                v = fr.validate_outfit(outfit, intent)
                validity_n += 1
                validity_pass += 1 if v.get("passed") else 0
        details.append({"id": c["id"], "status": st, "ms": round(ms, 1),
                        "count": len(recs), "server_valid": sv.get("passed"),
                        "source": body.get("source"), "reco_version": body.get("reco_version")})
        print(f"  {c['id']} {c['occasion']:<14} {st} {ms:7.0f}ms  n={len(recs)} "
              f"src={body.get('source')} srv_valid={sv.get('passed')}")

    err_pct = round(100 * errors / max(len(cases), 1), 2)
    metrics = {
        "reco_ready_ms": lat_block(lats),
        "reco_ready_ms_p95": pct(lats, 95),
        "error_rate_pct": err_pct,
        "success_n": successes,
        "live_outfit_validity_pct": round(100 * validity_pass / max(validity_n, 1), 2),
        "server_outfit_validity_pct": round(100 * server_valid_pass / max(server_valid_n, 1), 2),
        "product_relevance_pct": round(100 * relevance_hits / max(relevance_n, 1), 2),
        "relevance_n": relevance_n,
    }
    result = {"version": local_version(), "deployed_version": deployed,
              "metrics": metrics, "details": details}
    print(f"  P95 {metrics['reco_ready_ms_p95']}ms | err {err_pct}% | "
          f"validity {metrics['live_outfit_validity_pct']}% | deployed v{deployed}")
    save_result("live", result)
    return result


# ────────────────────────────── 3. vton ──────────────────────────────

def run_vton(base, tryon_n=None):
    print(f"== VTON benchmark vs {base} ==")
    cases = json.loads((GOLDEN / "vton_cases.json").read_text())["cases"]
    if tryon_n:
        cases = cases[:tryon_n]
    extract_lats, tryon_lats, e2e_lats = [], [], []
    extract_ok = extract_n = tryon_ok = tryon_n_done = 0
    details = []

    for c in cases:
        # ── extraction
        g = c["garment"]
        ex_ms = None
        garment_uri = None
        if g.get("extract", True):
            if "file" in g:
                st, ms, body = http_multipart(f"{base}/api/vton/extract-garment-upload",
                                              str(GOLDEN / g["file"]))
            else:
                payload = {"image_url": g["url"], "upload": True,
                           "garment_type": g.get("garment_type", "top")}
                st, ms, body = http_json(f"{base}/api/vton/extract-garment", payload, timeout=90)
            extract_n += 1
            if st == 200 and body.get("garment_image"):
                extract_ok += 1
                ex_ms = ms
                extract_lats.append(ms)
                garment_uri = body["garment_image"]
            else:
                print(f"  {c['id']} extract FAILED status={st} {body.get('detail') or body.get('error')}")
        else:
            garment_uri = g.get("url")

        # ── try-on
        person_uri, _ = resolve_asset(c["person"])
        if garment_uri:
            t0 = time.perf_counter()
            st, ms, body = http_json(f"{base}/api/vton/try-on", {
                "person_image_url": person_uri,
                "garment_image_url": garment_uri,
                "garment_types": [c.get("category", "top")],
                "extract_garment": False,
                "vto_engine": "idm-vton",
            }, timeout=180)
            tryon_n_done += 1
            done = st == 200 and body.get("status") in ("completed", "success") and (
                body.get("result_image") or body.get("result_url"))
            if done:
                tryon_ok += 1
                tryon_lats.append(ms)
                if ex_ms is not None:
                    e2e_lats.append(ex_ms + ms)
                else:
                    e2e_lats.append(ms)
            print(f"  {c['id']} extract={ex_ms and round(ex_ms)}ms tryon={round(ms)}ms "
                  f"→ {'OK' if done else 'FAIL ' + str(body.get('message') or body.get('status'))}")
            details.append({"id": c["id"], "extract_ms": ex_ms and round(ex_ms, 1),
                            "tryon_ms": round(ms, 1), "ok": bool(done),
                            **({"stages": body["stages"]} if isinstance(body.get("stages"), dict) else {})})
        else:
            details.append({"id": c["id"], "extract_ms": None, "tryon_ms": None,
                            "ok": False, "note": "no garment (extract failed)"})

    ex_fail_pct = round(100 * (extract_n - extract_ok) / max(extract_n, 1), 2)
    vton_fail_pct = round(100 * (tryon_n_done - tryon_ok) / max(tryon_n_done, 1), 2)
    metrics = {
        "extraction_success_pct": round(100 * extract_ok / max(extract_n, 1), 2),
        "extraction_n": extract_n,
        "extraction_ms": lat_block(extract_lats),
        "vton_success_pct": round(100 * tryon_ok / max(tryon_n_done, 1), 2),
        "vton_failure_pct": vton_fail_pct,
        "vton_n": tryon_n_done,
        "vton_generation_ms": lat_block(tryon_lats),
        "vton_generation_ms_p95": pct(tryon_lats, 95),
        "e2e_vton_ms": lat_block(e2e_lats),
        "e2e_vton_ms_p95": pct(e2e_lats, 95),
        "extraction_failure_pct": ex_fail_pct,
    }
    result = {"version": local_version(), "metrics": metrics, "details": details}
    print(f"  extract {metrics['extraction_success_pct']}% | try-on {metrics['vton_success_pct']}% | "
          f"e2e p95 {metrics['e2e_vton_ms_p95']}ms")
    save_result("vton", result)
    return result


# ────────────────────────────── 4. metrics ──────────────────────────────

_METRICS_SCRIPT = r'''
import asyncio, os, json, asyncpg
u = os.environ.get("DATABASE_URL", "")
u = u.replace("postgresql+asyncpg://", "postgresql://").replace("postgres://", "postgresql://")
async def main():
    c = await asyncpg.connect(u, timeout=15)
    rows = await c.fetch("select event_type, count(*)::int n from user_events group by 1")
    pair = await c.fetchrow(
        "select (select count(distinct user_id) from user_events where event_type='vton_completed')::int as v,"
        "(select count(distinct v.user_id) from user_events v join user_events s on s.user_id=v.user_id"
        " where v.event_type='vton_completed' and s.event_type='look_save')::int as vs")
    await c.close()
    return {r["event_type"]: r["n"] for r in rows}, dict(pair)
counts, pair = asyncio.run(main())
print("BENCHJSON:" + json.dumps({"counts": counts, "pair": pair}))
'''


def _metrics_from_events(counts, pair, source):
    imp = counts.get("impression", 0)
    save_r = counts.get("look_save", 0)
    vton = counts.get("vton_completed", 0)
    purchase = counts.get("purchase", 0)
    metrics = {
        "available": True,
        "source": source,
        "events": counts,
        "impressions": imp,
        "save_rate_pct": round(100 * save_r / imp, 2) if imp else None,
        "vton_rate_pct": round(100 * vton / imp, 2) if imp else None,
        "purchase_rate_pct": round(100 * purchase / imp, 2) if imp else None,
        "vton_to_save_pct": round(100 * pair.get("vs", 0) / max(pair.get("v", 0), 1), 2)
        if pair.get("v") else None,
        "personalization_lift_pct": None,  # needs ≥20 interactions/user history
    }
    print(f"  [{source}] impressions={imp} saves={save_r} vton={vton} purchases={purchase} | "
          f"save={metrics['save_rate_pct']}% vton={metrics['vton_rate_pct']}% "
          f"vton→save={metrics['vton_to_save_pct']}%")
    save_result("metrics", {"version": local_version(), "metrics": metrics})
    return metrics


def _metrics_local():
    import asyncio
    import asyncpg
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
    url = (os.environ.get("DATABASE_URL") or "").replace("postgresql+asyncpg", "postgresql")
    if not url:
        raise RuntimeError("no DATABASE_URL")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    async def fetch():
        conn = await asyncpg.connect(url, timeout=8)
        try:
            rows = await conn.fetch("select event_type, count(*)::int n from user_events group by 1")
            pair = await conn.fetchrow(
                "select (select count(distinct user_id) from user_events where event_type='vton_completed')::int as v,"
                "(select count(distinct v.user_id) from user_events v join user_events s on s.user_id=v.user_id"
                " where v.event_type='vton_completed' and s.event_type='look_save')::int as vs")
            return {r["event_type"]: r["n"] for r in rows}, dict(pair)
        finally:
            await conn.close()

    return asyncio.run(fetch())


def _metrics_remote():
    import subprocess
    p = subprocess.run(
        ["fly", "ssh", "console", "--app", "drishti-api", "-C", "python3"],
        input=_METRICS_SCRIPT.encode(), capture_output=True, timeout=90)
    for line in p.stdout.decode(errors="replace").splitlines():
        if line.startswith("BENCHJSON:"):
            data = json.loads(line.split(":", 1)[1])
            return data["counts"], data["pair"]
    raise RuntimeError(f"remote query failed: {p.stdout.decode(errors='replace')[-200:]} "
                       f"{p.stderr.decode(errors='replace')[-200:]}")


def run_metrics():
    print("== PERSONALIZATION metrics (user_events) ==")
    for source, fn in (("local", _metrics_local), ("prod(fly)", _metrics_remote)):
        try:
            counts, pair = fn()
            return _metrics_from_events(counts, pair, source)
        except Exception as e:
            print(f"  {source} failed ({e.__class__.__name__}: {str(e)[:120]})")
    save_result("metrics", {"version": local_version(),
                            "metrics": {"available": False}, "error": "no reachable database"})
    return None


# ────────────────────────────── 5. scorecard ──────────────────────────────

def _label_metrics(vton_res):
    """Aggregate human labels from golden vton_cases (if any were filled).

    RECO-v4.5 — 7 dimensions (see bench/label_vton.py): garment_fidelity,
    leakage_pct, identity_ok, body_preservation, boundary_quality,
    artifacts (count), overall. Returns {} (→ NO-DATA) until at least one
    case carries labelled_by — never invents a score.
    """
    cases = json.loads((GOLDEN / "vton_cases.json").read_text())["cases"]
    fid, leak, ident, body, bnd, overall, clean, n = [], [], [], [], [], [], [], 0
    for c in cases:
        lb = c.get("labels") or {}
        if lb.get("labelled_by") is None:
            continue
        n += 1
        if lb.get("garment_fidelity") is not None:
            fid.append(float(lb["garment_fidelity"]))
        if lb.get("leakage_pct") is not None:
            leak.append(float(lb["leakage_pct"]))
        if lb.get("identity_ok") is not None:
            ident.append(1 if lb["identity_ok"] else 0)
        if lb.get("body_preservation") is not None:
            body.append(float(lb["body_preservation"]))
        if lb.get("boundary_quality") is not None:
            bnd.append(float(lb["boundary_quality"]))
        if lb.get("overall") is not None:
            overall.append(float(lb["overall"]))
        if lb.get("artifacts") is not None:
            clean.append(1 if int(lb["artifacts"]) == 0 else 0)
    if not n:
        return {}
    out = {"labelled_n": n}
    if fid:
        out["garment_fidelity_pct"] = round(sum(fid) / len(fid), 2)
    if leak:
        out["garment_leakage_pct"] = round(sum(leak) / len(leak), 2)
    if ident:
        out["identity_preservation_pct"] = round(100 * sum(ident) / len(ident), 2)
    if body:
        out["body_preservation_pct"] = round(sum(body) / len(body), 2)
    if bnd:
        out["boundary_quality_pct"] = round(sum(bnd) / len(bnd), 2)
    if overall:
        out["overall_visual_pct"] = round(sum(overall) / len(overall), 2)
    if clean:
        out["artifacts_free_pct"] = round(100 * sum(clean) / len(clean), 2)
    return out


def run_scorecard(strict=False):
    print("═" * 62)
    version = local_version()
    from api.services.reco_version import RECO_NAME, RECO_RELEASED
    off = latest("offline", STALE_HOURS)
    live = latest("live", STALE_HOURS)
    vton = latest("vton", STALE_HOURS)
    met = latest("metrics")
    all_sc = sorted(glob.glob(str(RESULTS / "scorecard_*.json")), key=os.path.getmtime)
    prev = json.loads(pathlib.Path(all_sc[-1]).read_text()) if all_sc else None

    sources = {"offline": off, "live": live, "vton": vton}
    merged = {}
    for k, res in sources.items():
        if res:
            merged.update(res.get("metrics") or {})
    labels = _label_metrics(vton)
    merged.update(labels)

    def fmt(v, suffix=""):
        if v is None:
            return "n/a"
        if suffix == "ms" and isinstance(v, (int, float)):
            return f"{round(v)}ms"
        return f"{v}{suffix}"

    print(f"RECOMMENDER v{version} — {RECO_NAME} ({RECO_RELEASED})")
    print("─" * 62)
    lm = live.get("metrics") if live else {}
    om = off.get("metrics") if off else {}
    vm = vton.get("metrics") if vton else {}
    print("⚡ PERFORMANCE")
    print(f"  P95 Recommendation      {fmt(lm.get('reco_ready_ms_p95'), 'ms'):<10} "
          f"(target {GATES['targets']['reco_ready_ms_p95']}ms)  "
          f"{('stale ' + str(live.get('_age_h')) + 'h') if live and live.get('_stale') else ''}")
    print(f"  P95 VTON                {fmt(vm.get('vton_generation_ms_p95'), 'ms'):<10} "
          f"(target {GATES['targets']['vton_generation_ms_p95']}ms)")
    print(f"  P95 End-to-End VTON     {fmt(vm.get('e2e_vton_ms_p95'), 'ms'):<10} "
          f"(target {GATES['targets']['e2e_vton_ms_p95']}ms)")
    print(f"  Reco error rate         {fmt(lm.get('error_rate_pct'), '%'):<10} "
          f"(max {GATES['targets']['error_rate_pct_max']}%)")
    print("🎯 RECOMMENDATION (offline golden)")
    print(f"  Outfit validity         {fmt(om.get('outfit_validity_pct'), '%'):<10} "
          f"(min {GATES['targets']['outfit_validity_pct_min']}%)")
    print(f"  Occasion accuracy       {fmt(om.get('occasion_accuracy_pct'), '%'):<10} "
          f"(min {GATES['targets']['occasion_accuracy_pct_min']}%)")
    print(f"  Hard-rule violations    {fmt(om.get('hard_rule_violation_pct'), '%'):<10} "
          f"(max 1%)")
    print(f"  Violation recall        {fmt(om.get('violation_recall_pct'), '%')}")
    print(f"  Product relevance@K     {fmt(lm.get('product_relevance_pct'), '%')}")
    print("🧠 PERSONALIZATION")
    mmet = (met or {}).get("metrics") or {}
    print(f"  Save rate               {fmt(mmet.get('save_rate_pct'), '%')}")
    print(f"  VTON rate               {fmt(mmet.get('vton_rate_pct'), '%')}")
    print(f"  VTON → Save             {fmt(mmet.get('vton_to_save_pct'), '%')}")
    print(f"  Purchase rate           {fmt(mmet.get('purchase_rate_pct'), '%')}")
    print("👗 VTON")
    print(f"  Extraction success      {fmt(vm.get('extraction_success_pct'), '%'):<10} "
          f"(min {GATES['targets']['garment_extraction_success_pct_min']}%)")
    print(f"  VTON failure rate       {fmt(vm.get('vton_failure_pct'), '%')}")
    print(f"  Garment fidelity        {fmt(labels.get('garment_fidelity_pct'), '%')}"
          f"  (labelled n={labels.get('labelled_n', 0)})")
    print(f"  Garment leakage         {fmt(labels.get('garment_leakage_pct'), '%')}")
    print(f"  Identity preservation   {fmt(labels.get('identity_preservation_pct'), '%')}")
    print(f"  Body preservation       {fmt(labels.get('body_preservation_pct'), '%')}")
    print(f"  Boundary quality        {fmt(labels.get('boundary_quality_pct'), '%')}")
    print(f"  Artifacts free          {fmt(labels.get('artifacts_free_pct'), '%')}")
    print(f"  Overall visual          {fmt(labels.get('overall_visual_pct'), '%')}")

    # ── gate evaluation
    print("─" * 62)
    print("RELEASE GATES")
    verdicts, failed, nodata = [], [], []
    for g in GATES["hard_gates"]:
        src = g["source"]
        if src == "vton_labels":
            res = sources["vton"]
            actual = labels.get(g["metric"])
        else:
            res = sources[src]
            actual = (res or {}).get("metrics", {}).get(g["metric"]) if res else None
        if not res or actual is None:
            reason = "no data" if not res else f"unlabelled (n={labels.get('labelled_n', 0)})"
            status = "NO-DATA"
            if strict and not g.get("optional_if_unlabelled"):
                status = "FAIL(strict)"
                nodata.append(g["id"])
        else:
            ok = cmp_ok(g["cmp"], actual, g["value"])
            status = "PASS" if ok else "FAIL"
            if not ok:
                failed.append(g["id"])
            if res.get("_stale"):
                status += f"(stale {res['_age_h']}h)"
        verdicts.append({"id": g["id"], "metric": g["metric"], "actual": actual,
                         "cmp": g["cmp"], "value": g["value"], "status": status})
        print(f"  {status:<16} {g['id']:<24} {fmt(actual)} {g['cmp']} {g['value']}")

    # ── delta vs previous scorecard
    if prev:
        print("─" * 62)
        print(f"📈 DELTA vs v{prev.get('version')} ({prev.get('_ts', '?')})")
        for key in ("reco_ready_ms_p95", "outfit_validity_pct", "occasion_accuracy_pct",
                    "hard_rule_violation_pct", "e2e_vton_ms_p95", "extraction_success_pct",
                    "product_relevance_pct"):
            a = (prev.get("metrics") or {}).get(key)
            b = merged.get(key)
            if a is not None and b is not None and a != b:
                print(f"  {key:<28} {a} → {b}  ({round(b - a, 2):+})")

    ok = not failed and not nodata
    card = {"version": version, "metrics": merged, "gates": verdicts,
            "failed_gates": failed, "missing_gates": nodata,
            "strict": strict, "passed": ok,
            "sources": {k: (v or {}).get("_file") for k, v in sources.items()}}
    save_result("scorecard", card)
    print("─" * 62)
    print(f"SCORECARD {'PASS ✓' if ok else 'FAIL ✗'}"
          + (f" — failed: {', '.join(failed)}" if failed else "")
          + (f" — missing: {', '.join(nodata)}" if nodata else ""))
    return ok


# ────────────────────────────── cli ──────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["offline", "live", "vton", "metrics",
                                        "scorecard", "gates", "release"])
    ap.add_argument("--base-url", default="https://drishti-api.fly.dev")
    ap.add_argument("--n", type=int, default=None, help="limit live cases")
    ap.add_argument("--try-on-n", type=int, default=None, help="limit vton try-on cases (cost!)")
    ap.add_argument("--strict", action="store_true", help="scorecard: missing sources fail gates")
    args = ap.parse_args()

    t0 = time.perf_counter()
    ok = True
    if args.command in ("offline", "gates", "release"):
        run_offline()
    if args.command in ("live", "release"):
        run_live(args.base_url, args.n)
    if args.command in ("vton", "release"):
        run_vton(args.base_url, args.try_on_n)
    if args.command in ("metrics", "release"):
        run_metrics()
    if args.command in ("scorecard", "gates", "release"):
        strict = args.strict or args.command == "release"
        ok = run_scorecard(strict=strict)
    print(f"total {round(time.perf_counter() - t0, 1)}s")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
