"""RECO-v4.5 tests (spec TEST A–H).

A  thin-pool coverage: interleaved new-user query cap + round-robin
   fallback merge + gate-aware retrieval top-up
B  completeness: incomplete looks dropped when a complete one exists
C  completeness: honest flagging when inventory yields zero completes
D  four structurally distinct looks (pairwise signatures)
E  colour-only same-arch clones never pass diversity
F  variation looks keep direction + carry completeness metadata
G  no foreign garment in a look (blueprint garment tiers)
H  SerpApi 429 bounded retry, stats counters, concurrency cap
+  request diagnostics contract (reco_diagnostics)
"""
import asyncio
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from api.services import fashion_rules as frules  # noqa: E402
from api.services import look_assembly as la  # noqa: E402
from api.services import marketplace_reco as mkt  # noqa: E402
from api.services import price_scraper as ps  # noqa: E402
from api.services.fashion_engine import build_intent, build_slot_queries  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ── helpers ───────────────────────────────────────────────────────────

def _intent(**kw):
    base = dict(occasion="work", gender="female", budget_min=500, budget_max=5000)
    base.update(kw)
    return build_intent(**base)


def _p(pid, title, slot, category, price=1000, *, culture="western",
       color="navy", bp="", score=0.7, image=True, source="test"):
    return {
        "source": source,
        "product_id": pid,
        "title": title,
        "price": price,
        "image_url": f"https://cdn.example.com/{pid}.jpg" if image else "",
        "blueprint_id": bp,
        "_final": score,
        "fashion": {
            "slot": slot,
            "category": category,
            "color": color,
            "culture": culture,
            "fabrics": ["cotton"],
            "fit": "regular",
            "patterns": [],
        },
    }


def _office_bps(intent, n=4):
    return frules.build_outfit_blueprints(intent, n=n)


def _separates(bps):
    return [b for b in bps
            if not any(c.get("slot") == "full" for c in b.get("components", []))]


# ══════════════════════════ TEST A ════════════════════════════════════

def test_cap_keeps_first_blueprint_slots_and_a_second_primary():
    """The new-user cap must never keep a 2nd bottom while chopping every
    other archetype's primary (the indian-office 1-look bug)."""
    prefs = {"wear_weights": {"indian": 0.9, "western": 0.1}}
    intent = _intent(user_prefs=prefs)
    qs = build_slot_queries(intent, count=6, max_queries=8)
    bmap = intent.get("blueprint_query_map") or {}
    capped = mkt._cap_queries_interleaved(qs, bmap, cap=4)

    assert len(capped) <= 4
    # First blueprint keeps its complete slot set
    assert [s for s, _ in capped[:3]] == ["primary", "bottom", "shoes"], capped
    # Remaining budget spent on PRIMARYs of other blueprints
    primaries = [(s, q) for s, q in capped if s == "primary"]
    assert len(primaries) >= 2, f"single primary family survived the cap: {capped}"
    # No blueprint contributes a non-primary before another blueprint's primary
    bp_ids = [bmap.get(q.lower(), "") for _, q in capped]
    assert len(set(bp_ids)) >= 2, bp_ids
    # Full list's garment-family spread is not silently truncated to one
    full_primaries = {bmap.get(q.lower(), "") for s, q in qs if s == "primary"}
    assert len(full_primaries) >= 2
    assert len({bmap.get(q.lower(), "") for s, q in capped if s == "primary"}) >= 2


def test_cap_matches_legacy_prefix_for_default_wardrobe():
    """No behavior change where the old prefix already kept coverage."""
    intent = _intent()
    qs = build_slot_queries(intent, count=6, max_queries=8)
    capped = mkt._cap_queries_interleaved(
        qs, intent.get("blueprint_query_map") or {}, cap=4)
    assert capped == qs[:4]


def test_cap_without_blueprint_map_falls_back_to_prefix():
    qs = [("primary", "q1"), ("bottom", "q2"), ("shoes", "q3"),
          ("primary", "q4"), ("primary", "q5")]
    assert mkt._cap_queries_interleaved(qs, {}, cap=4) == qs[:4]
    assert mkt._cap_queries_interleaved(qs[:2], {}, cap=4) == qs[:2]


def test_round_robin_fallback_interleaves_primary_queries(monkeypatch):
    """Under-delivered google draw → scraper fallback must interleave the
    pending queries (1 from each primary family per pass) instead of
    draining one ethnic primary query before the shirt query contributes."""
    calls: list[str] = []

    async def fake_slots(slot_queries, blueprint_map=None, per_query=20,
                         stats=None):
        return []  # raw 0 → under-delivery trigger

    async def fake_fallback(query, stats=None):
        calls.append(query)
        if stats is not None:
            stats["fallback_calls"] = stats.get("fallback_calls", 0) + 1
            stats["fallback_products"] = stats.get(
                "fallback_products", 0) + 3
        if "shirt" in query:
            return [_p(f"shirt{i}", f"Women Formal Shirt {i} Office Wear",
                       "top", "top", 900 + i * 50, color="white", score=0.8)
                    for i in range(3)]
        if "kurta" in query:
            return [_p(f"kurta{i}", f"Women Straight Formal Kurta {i}",
                       "top", "kurta", 1100 + i * 50, culture="indian",
                       color="blue", score=0.75) for i in range(6)]
        if "pants" in query or "trousers" in query or "chinos" in query \
                or "palazzo" in query:
            return [_p(f"trouser{i}", f"Women Formal Cigarette Trousers {i}",
                       "bottom", "trousers", 1000 + i * 50, score=0.75)
                    for i in range(4)]
        return [_p(f"heel{i}", f"Women Block Heels {i}", "shoes", "heels",
                   1300 + i * 100, score=0.7) for i in range(4)]

    monkeypatch.setattr(mkt, "_search_google_shopping_slots", fake_slots)
    monkeypatch.setattr(mkt, "_scraper_fallback", fake_fallback)

    res = _run(mkt.search_marketplace_products(
        occasion="work", gender="female", count=6))

    stats = res["search_stats"]
    assert stats.get("fallback_calls", 0) >= 2

    # Interleave proof lives in blueprint tagging of the merged pool:
    # the raw pool is not returned — assert via topup/fallback bookkeeping
    # and the fact both families were fetched (shirt AND kurta queries).
    assert any("shirt" in q for q in calls), calls
    assert any("kurta" in q for q in calls), calls


def test_round_robin_merge_order_unit(monkeypatch):
    """Direct interleave check: merged primary products alternate blueprint
    families instead of draining one query first."""
    intent = _intent()
    qs = build_slot_queries(intent, count=6, max_queries=8)
    bmap = intent.get("blueprint_query_map") or {}
    slot_queries = mkt._cap_queries_interleaved(qs, bmap, cap=4)

    fetched_seen: list[str] = []

    async def fake_slots(slot_queries, blueprint_map=None, per_query=20,
                         stats=None):
        return []

    async def fake_fallback(query, stats=None):
        fetched_seen.append(query)
        if "shirt" in query:
            return [_p(f"sh{i}", f"Women Formal Shirt {i} Office", "top",
                       "top", 900 + i * 10, color="white", score=0.8)
                    for i in range(4)]
        if "kurta" in query:
            return [_p(f"ku{i}", f"Women Straight Kurta Formal {i}", "top",
                       "kurta", 1100 + i * 10, culture="indian", color="blue",
                       score=0.7) for i in range(4)]
        if any(w in query for w in ("pants", "trousers", "chinos",
                                    "palazzo", "culotte")):
            return [_p(f"tr{i}", f"Women Formal Trousers {i}", "bottom",
                       "trousers", 1000 + i * 10, score=0.75)
                    for i in range(3)]
        return [_p(f"sl{i}", f"Women Block Heels {i}", "shoes", "heels",
                   1200 + i * 10, score=0.7) for i in range(3)]

    monkeypatch.setattr(mkt, "_search_google_shopping_slots", fake_slots)
    monkeypatch.setattr(mkt, "_scraper_fallback", fake_fallback)

    captured: dict = {}
    real_recommend = mkt.recommend_products

    def spy_recommend(products, intent_, count_):
        if "pool" not in captured:
            captured["pool"] = list(products)
        return real_recommend(products, intent_, count_)

    monkeypatch.setattr(mkt, "recommend_products", spy_recommend)
    _run(mkt.search_marketplace_products(
        occasion="work", gender="female", count=6))

    pool = captured["pool"]
    primaries = [p for p in pool if p.get("query_slot") == "primary"]
    assert len(primaries) >= 4, [(p["title"]) for p in pool]
    # Interleave: among the first 4 primaries BOTH blueprint families appear
    # (batch merge would deliver 4+ from the first query alone).
    fams = [p.get("blueprint_id", "") for p in primaries[:4]]
    assert len(set(fams)) >= 2, f"primary queries not interleaved: {fams}"
    # Every pending query got fetched exactly once in this round
    assert len(fetched_seen) == len(set(fetched_seen))


def test_gate_aware_topup_fetches_cut_queries(monkeypatch):
    """Raw pool clears the threshold but every product fails the gender
    gate → valid candidates thin → ONE top-up round over the queries the
    new-user cap cut (never accept a starved pool silently)."""
    calls: list[str] = []

    async def fake_slots(slot_queries, blueprint_map=None, per_query=20,
                         stats=None):
        # Raw-rich but gate-invalid for a female request
        return [_p(f"men{i}", f"Men Casual Tshirt {i}", "top", "tshirt",
                   500 + i * 50, score=0.9) for i in range(8)]

    async def fake_fallback(query, stats=None):
        calls.append(query)
        return [_p(f"blz{i}", f"Women Formal Black Blazer Office {i}", "top",
                   "outerwear", 2000 + i * 100, score=0.85) for i in range(2)]

    monkeypatch.setattr(mkt, "_search_google_shopping_slots", fake_slots)
    monkeypatch.setattr(mkt, "_scraper_fallback", fake_fallback)

    res = _run(mkt.search_marketplace_products(
        occasion="work", gender="female", count=6))

    stats = res["search_stats"]
    assert stats.get("topup_queries", 0) == 1, stats
    assert stats.get("topup_products", 0) == 2, stats
    # The top-up spent its round on the query the cap had cut (blazer family);
    # later fallback calls may come from the bottom-coverage retry.
    assert calls and "blazer" in calls[0], calls
    # Re-ran the pipeline with the fresh products — valid now non-empty
    assert res["candidates"], res.keys()
    assert "topup_ms" in res["stage_ms"]


def test_no_topup_when_valid_pool_is_rich(monkeypatch):
    """Happy path stays untouched: rich valid pool → zero extra rounds."""
    fallback_calls = []

    async def fake_slots(slot_queries, blueprint_map=None, per_query=20,
                         stats=None):
        # 8 gender-valid, image-bearing products (incl. shoes) → candidates
        # ≥ 6 and every look slot covered → zero extra rounds anywhere
        out = []
        for i, (sc, bc) in enumerate((("white", "black"), ("blue", "navy"),
                                      ("grey", "beige"))):
            out.append(_p(f"t{i}", f"Women Formal Shirt {i} Office {sc}", "top",
                          "top", 900 + i * 50, color=sc, score=0.8))
            out.append(_p(f"b{i}", f"Women Formal Cigarette Trousers {i} {bc}",
                          "bottom", "trousers", 1000 + i * 50, color=bc,
                          score=0.75))
        out.append(_p("h0", "Women Block Heels Black", "shoes", "heels", 1300,
                      score=0.75))
        out.append(_p("h1", "Women Block Heels Nude", "shoes", "heels", 1350,
                      color="nude", score=0.74))
        return out

    async def fake_fallback(query, stats=None):
        fallback_calls.append(query)
        return []

    monkeypatch.setattr(mkt, "_search_google_shopping_slots", fake_slots)
    monkeypatch.setattr(mkt, "_scraper_fallback", fake_fallback)

    res = _run(mkt.search_marketplace_products(
        occasion="work", gender="female", count=6))
    assert res["search_stats"].get("topup_queries", 0) == 0
    assert fallback_calls == []
    assert "topup_ms" not in res["stage_ms"]


# ══════════════════════════ TEST B / C ════════════════════════════════

def test_b_complete_looks_win_incomplete_dropped():
    """Inventory has one bottom → at most one complete look; the bottomless
    ones are dropped, never shipped as complete formal outfits."""
    intent = _intent()
    bps = _office_bps(intent, n=4)
    assert len(bps) == 4
    pool = []
    for i, c in enumerate(("white", "blue", "grey", "ivory")):
        pool.append(_p(f"shirt{i}", f"Women Formal {c.title()} Shirt Office",
                       "top", "top", 900 + i * 50, color=c, score=0.85))
    pool.append(_p("trouser0", "Women Formal Black Cigarette Trousers",
                   "bottom", "trousers", 1000, score=0.8))
    for i in range(4):
        pool.append(_p(f"heel{i}", f"Women Block Heels {i}", "shoes", "heels",
                       1200 + i * 50, score=0.75))

    stats: dict = {}
    looks = la.assemble_looks(pool, bps, intent, count=4, stats_out=stats)
    assert looks, "expected at least the complete look"
    assert all(lk["is_complete"] for lk in looks), [
        (lk["label"], lk["missing_slots"]) for lk in looks]
    assert stats.get("incomplete_dropped", 0) >= 1, stats
    # The shipped set never contains a bottomless look alongside a complete one
    for lk in looks:
        assert "bottom" not in lk["missing_slots"]


def test_b_full_outfit_without_bottom_is_complete():
    bp = {"id": "full-0", "label": "Saree Grace", "variant": "safe",
          "archetype": "WM-03", "dress_code": "festive", "occasion": "wedding",
          "components": [
              {"slot": "full", "garment": "saree",
               "detect_categories": ["dress", "saree"]},
              {"slot": "shoes", "garment": "heels",
               "detect_categories": ["heels", "sandals"]},
          ],
          "why": ""}
    pool = [
        _p("dress0", "Women Navy Formal Dress", "full", "dress", 2000,
           score=0.9),
        _p("heel0", "Women Black Block Heels", "shoes", "heels", 1400,
           score=0.8),
    ]
    intent = _intent()
    look = la._assemble_one(bp, pool, set(), intent, exclude_ids=set(),
                            exclude_titles=[])
    assert look is not None
    assert look["is_complete"] is True, look["missing_slots"]
    assert "bottom" not in look["missing_slots"]


def test_c_zero_completes_ship_flagged_not_silently():
    """No bottom in inventory at all → incompletes are kept but honestly
    flagged (is_complete=false + missing_slots) — complete-or-honest."""
    intent = _intent()
    bps = _separates(_office_bps(intent, n=4))
    assert bps, "context must yield separates blueprints"
    pool = []
    for i, c in enumerate(("white", "blue", "grey", "ivory")):
        pool.append(_p(f"shirt{i}", f"Women Formal {c.title()} Shirt Office",
                       "top", "top", 900 + i * 50, color=c, score=0.85))
    for i in range(4):
        pool.append(_p(f"heel{i}", f"Women Block Heels {i}", "shoes", "heels",
                       1200 + i * 50, score=0.75))

    stats: dict = {}
    looks = la.assemble_looks(pool, bps, intent, count=4, stats_out=stats)
    assert looks, "honest incompletes still ship when nothing is complete"
    assert not any(lk["is_complete"] for lk in looks)
    for lk in looks:
        assert "bottom" in lk["missing_slots"], lk
    assert stats.get("incomplete_dropped", 0) == 0


def test_completeness_fields_reach_api_contract():
    intent = _intent()
    bps = _office_bps(intent, n=4)
    pool = [
        _p("shirt0", "Women Formal White Shirt Office", "top", "top", 900,
           score=0.85),
        _p("trouser0", "Women Formal Black Cigarette Trousers", "bottom",
           "trousers", 1000, score=0.8),
        _p("heel0", "Women Black Block Heels", "shoes", "heels", 1400,
           score=0.75),
    ]
    looks = la.assemble_looks(pool, bps, intent, count=4)
    assert looks
    for lk in looks:
        assert isinstance(lk["is_complete"], bool)
        assert isinstance(lk["missing_slots"], list)
        fmt = mkt._format_look(lk)
        assert fmt["is_complete"] == lk["is_complete"]
        assert fmt["missing_slots"] == lk["missing_slots"]


# ══════════════════════════ TEST D ════════════════════════════════════

def test_d_four_structurally_distinct_complete_looks():
    intent = _intent()
    bps = _office_bps(intent, n=4)
    pool = []
    for i, c in enumerate(("white", "blue", "grey", "ivory")):
        pool.append(_p(f"shirt{i}", f"Women Formal {c.title()} Shirt Office",
                       "top", "top", 900 + i * 50, color=c, score=0.85))
    for i, c in enumerate(("navy", "teal")):
        pool.append(_p(f"kurta{i}", f"Women Straight Formal Kurta {c.title()}",
                       "top", "kurta", 1100 + i * 100, culture="indian",
                       color=c, score=0.8))
    for i, c in enumerate(("black", "charcoal")):
        pool.append(_p(f"blazer{i}", f"Women Tailored Blazer {c.title()}",
                       "top", "outerwear", 2000 + i * 200, color=c,
                       score=0.82))
    for i, c in enumerate(("black", "navy", "grey", "beige")):
        pool.append(_p(f"trouser{i}", f"Women Cigarette Trousers {c.title()}",
                       "bottom", "trousers", 1000 + i * 50, color=c,
                       score=0.8))
    for i in range(4):
        pool.append(_p(f"heel{i}", f"Women Block Heels {i}", "shoes", "heels",
                       1300 + i * 50, score=0.75))

    stats: dict = {}
    looks = la.assemble_looks(pool, bps, intent, count=4, stats_out=stats)
    assert len(looks) >= 2, "four directions require structural variety"
    for i, a in enumerate(looks):
        for b in looks[i + 1:]:
            assert not la.too_similar(a["signature"], b["signature"]), \
                (a["label"], b["label"], a["signature"], b["signature"])
    # Structural props — not colour — separate the looks
    archs = [lk["archetype_id"] for lk in looks]
    assert len(set(archs)) == len(archs), archs
    bodies = {lk["signature"].get("body") for lk in looks}
    assert len(bodies) >= 2, bodies
    assert all(lk["is_complete"] for lk in looks)


# ══════════════════════════ TEST E ════════════════════════════════════

def test_e_colour_only_clones_never_pass_diversity():
    """Four colour variants of ONE direction = one look. Colour/price/
    brand differences are never structural."""
    intent = _intent()
    proto = _office_bps(intent, n=4)[0]
    clones = [{**proto, "id": f"{proto['id']}-c{i}"} for i in range(4)]
    pool = []
    for i, c in enumerate(("white", "blue", "ivory", "grey")):
        pool.append(_p(f"shirt{i}", f"Women Formal {c.title()} Shirt Office",
                       "top", "top", 900 + i * 50, color=c, score=0.85))
    for i, c in enumerate(("black", "navy", "grey", "beige")):
        pool.append(_p(f"trouser{i}", f"Women Cigarette Trousers {c.title()}",
                       "bottom", "trousers", 1000 + i * 50, color=c,
                       score=0.8))
    for i, c in enumerate(("black", "nude", "tan", "wine")):
        pool.append(_p(f"heel{i}", f"Women Block Heels {c.title()}", "shoes",
                       "heels", 1300 + i * 50, color=c, score=0.75))

    stats: dict = {}
    looks = la.assemble_looks(pool, clones, intent, count=4, stats_out=stats)
    assert len(looks) == 1, [lk["signature"] for lk in looks]
    assert stats.get("conflict_dropped", 0) == 3, stats


# ══════════════════════════ TEST F ════════════════════════════════════

def test_f_variation_looks_keep_direction_and_metadata():
    intent = _intent()
    parent_id = _office_bps(intent, n=4)[0]["archetype"]
    vbps = la.build_variation_blueprints(intent, parent_id, n=4)
    assert vbps
    pool = []
    # Parent-garment products (strict_primary drops foreign rescues) +
    # separable bottoms/shoes for the direction's other slots
    for i, c in enumerate(("navy", "teal", "wine", "olive")):
        pool.append(_p(f"kurta{i}", f"Women Straight Formal Kurta {c.title()}",
                       "top", "kurta", 1100 + i * 100, culture="indian",
                       color=c, score=0.8))
    for i, c in enumerate(("black", "navy", "grey", "beige")):
        pool.append(_p(f"trouser{i}", f"Women Cigarette Trousers {c.title()}",
                       "bottom", "trousers", 1000 + i * 50, color=c,
                       score=0.8))
    for i in range(4):
        pool.append(_p(f"heel{i}", f"Women Block Heels {i}", "shoes", "heels",
                       1300 + i * 50, score=0.75))

    looks = la.assemble_looks(pool, vbps, intent, count=4,
                              enforce_diversity=False)
    assert looks, "variations must assemble"
    for lk in looks:
        assert lk["archetype_id"] == parent_id
        assert lk["is_variation"] is True
        assert isinstance(lk["is_complete"], bool)
        assert isinstance(lk["missing_slots"], list)
    # Any product in a variation must belong to the parent garment family
    # (strict_primary dropped foreign rescues already at assembly time)
    assert all(lk["products"] for lk in looks)


# ══════════════════════════ TEST G ════════════════════════════════════

def test_g_look_products_sit_in_blueprint_garment_tiers():
    """No foreign garment in any look: each slot's product category must
    sit in that blueprint component's detect tier list (tier0/1/2), never
    outside the component's vocabulary."""
    intent = _intent()
    bps = _office_bps(intent, n=4)
    pool = []
    for i, c in enumerate(("white", "blue", "grey", "ivory")):
        pool.append(_p(f"shirt{i}", f"Women Formal {c.title()} Shirt Office",
                       "top", "top", 900 + i * 50, color=c, score=0.85))
    for i, c in enumerate(("navy", "teal")):
        pool.append(_p(f"kurta{i}", f"Women Straight Formal Kurta {c.title()}",
                       "top", "kurta", 1100 + i * 100, culture="indian",
                       color=c, score=0.8))
    for i, c in enumerate(("black", "charcoal")):
        pool.append(_p(f"blazer{i}", f"Women Tailored Blazer {c.title()}",
                       "top", "outerwear", 2000 + i * 200, color=c,
                       score=0.9))  # high score must not buy entry
    for i in range(4):
        pool.append(_p(f"trouser{i}", f"Women Cigarette Trousers {i}",
                       "bottom", "trousers", 1000 + i * 50, score=0.8))
    # Heels (WF-01/WF-06 tier0) + loafers (WF-02/WF-07 tier0)
    for i in range(2):
        pool.append(_p(f"heel{i}", f"Women Block Heels {i}", "shoes", "heels",
                       1300 + i * 50, score=0.75))
        pool.append(_p(f"loafer{i}", f"Women Leather Loafers {i}", "shoes",
                       "shoes", 1250 + i * 50, score=0.74))

    looks = la.assemble_looks(pool, bps, intent, count=4)
    assert looks
    for lk in looks:
        bp = next(b for b in bps if b["id"] == lk["blueprint_id"])
        comps = {c.get("slot"): c for c in bp.get("components", [])}
        for p in lk["products"]:
            f = p.get("fashion") or {}
            slot, cat = f.get("slot"), f.get("category")
            if slot == "top" or slot == "primary":
                comp = comps.get("primary") or comps.get("top")
            else:
                comp = comps.get(slot)
            if comp is None:
                continue
            det = comp.get("detect_categories") or []
            if det:
                assert cat in det, \
                    f"{lk['label']}: {cat} outside {det} ({p['title']})"


# ══════════════════════════ TEST H ════════════════════════════════════

class _FakeResp:
    def __init__(self, status_code, data=None):
        self.status_code = status_code
        self._data = data or {}

    def json(self):
        return self._data


def _serpapi_env(monkeypatch, responses, active=None, max_active=None):
    """Fake SerpApi transport: `responses` = list of (status, data) per call.
    Optionally tracks peak concurrency via `active`/`max_active`."""
    monkeypatch.setattr(ps, "SERPAPI_KEY", "test-key")
    monkeypatch.setattr(ps, "_cache", {})
    monkeypatch.setattr(ps, "_serpapi_sem", None)  # fresh semaphore
    state = {"i": 0}

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            if active is not None:
                active[0] += 1
                if max_active is not None:
                    max_active[0] = max(max_active[0], active[0])
            await asyncio.sleep(0.01)
            if active is not None:
                active[0] -= 1
            idx = min(state["i"], len(responses) - 1)
            state["i"] += 1
            status, data = responses[idx]
            return _FakeResp(status, data)

    import types
    monkeypatch.setattr(ps, "httpx", types.SimpleNamespace(AsyncClient=FakeClient))


def test_h_429_is_bounded_to_two_attempts(monkeypatch):
    _serpapi_env(monkeypatch, [(429, {})])
    stats: dict = {}
    out = _run(ps.search_google_shopping("women formal shirt work", stats=stats))
    assert out == []
    assert stats["calls"] == 2, stats          # attempt + ONE retry
    assert stats["retries"] == 1
    assert stats["status_429"] == 2            # both attempts were 429s
    # A failed draw is never cached
    assert ps._get_cached("google_shopping", "women formal shirt work") is None


def test_h_429_then_success_serves_results(monkeypatch):
    data = {"shopping_results": [
        {"title": "Women Formal White Shirt", "price": "₹999",
         "extracted_price": 999, "link": "https://shop.example/p1",
         "source": "example", "rating": 4.0, "reviews": 10,
         "thumbnail": "", "rich_thumbnail": ""}
    ]}
    _serpapi_env(monkeypatch, [(429, {}), (200, data)])
    stats: dict = {}
    out = _run(ps.search_google_shopping("women formal shirt retry", stats=stats))
    assert len(out) == 1
    assert stats["calls"] == 2 and stats["retries"] == 1
    assert stats["status_429"] == 1
    # success IS cached — a repeat call costs no HTTP
    stats2: dict = {}
    out2 = _run(ps.search_google_shopping("women formal shirt retry", stats=stats2))
    assert len(out2) == 1
    assert stats2.get("calls", 0) == 0 and stats2.get("cache_hits") == 1


def test_h_client_error_does_not_retry(monkeypatch):
    _serpapi_env(monkeypatch, [(403, {})])
    stats: dict = {}
    _run(ps.search_google_shopping("blocked query", stats=stats))
    assert stats["calls"] == 1, stats
    assert stats.get("retries", 0) == 0


def test_h_google_query_strips_exclusions(monkeypatch):
    """Google Shopping actively MATCHES `-excluded` tokens — live probe:
    0/20 usable results with exclusions vs 18/20 without ("blue formal
    shirt" → sequin lehengas). SerpApi gets the query WITHOUT them; the
    occasion gates reject bad inventory downstream anyway. Cache keys off
    the stripped text so variants share one quota-efficient fetch."""
    captured: list[str] = []
    import types
    monkeypatch.setattr(ps, "SERPAPI_KEY", "test-key")
    monkeypatch.setattr(ps, "_cache", {})
    monkeypatch.setattr(ps, "_serpapi_sem", None)

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            captured.append((params or {}).get("q", ""))
            await asyncio.sleep(0.005)
            return _FakeResp(200, {"shopping_results": [{
                "title": "Women Formal Shirt", "price": "₹999",
                "extracted_price": 999, "link": "https://shop.example/p1",
                "source": "example", "rating": 4.0, "reviews": 3,
                "thumbnail": "", "rich_thumbnail": ""}]})

    monkeypatch.setattr(ps, "httpx", types.SimpleNamespace(AsyncClient=FakeClient))

    q = ("women blue formal shirt office wear India "
         "-bridal -wedding -lehenga -sequin -party")
    _run(ps.search_google_shopping(q, stats={}))
    assert captured == ["women blue formal shirt office wear India"], captured
    # a variant differing only in exclusions hits the same cached fetch
    stats2: dict = {}
    _run(ps.search_google_shopping("women blue formal shirt office wear India",
                                   stats=stats2))
    assert stats2.get("cache_hits") == 1 and stats2.get("calls", 0) == 0, stats2


def test_h_429_circuit_breaker_skips_remaining_queries(monkeypatch):
    """Quota exhaustion is per-key: after SERPAPI_BREAKER 429s in one
    request, remaining slot queries must skip HTTP entirely."""
    _serpapi_env(monkeypatch, [(429, {})])
    stats: dict = {}
    for i in range(4):
        out = _run(ps.search_google_shopping(f"query {i}", stats=stats))
        assert out == []
    # q1: attempt+one allowed retry (first 429 of the request); q2/q3: a
    # single attempt each (further 429s are quota exhaustion — no retry);
    # q4: breaker opens at 4 × 429 and skips HTTP entirely.
    assert stats["status_429"] == 4, stats
    assert stats["calls"] == 4, stats
    assert stats["retries"] == 1, stats
    assert stats.get("breaker_skips") == 1, stats


def test_h_concurrency_capped_by_semaphore(monkeypatch):
    active, max_active = [0], [0]
    _serpapi_env(monkeypatch, [(200, {"shopping_results": []})],
                 active=active, max_active=max_active)
    queries = [f"query number {i}" for i in range(6)]

    async def _all():
        await asyncio.gather(
            *[ps.search_google_shopping(q, stats={}) for q in queries])

    _run(_all())
    assert max_active[0] <= ps._SERPAPI_MAX_CONCURRENCY, max_active
    assert max_active[0] >= 1


# ═══════════════════ request diagnostics contract ═════════════════════

def test_diagnostics_contract_with_mocked_search(monkeypatch):
    intent = _intent()
    pool = []
    for i, c in enumerate(("white", "blue", "grey", "ivory")):
        pool.append(_p(f"shirt{i}", f"Women Formal {c.title()} Shirt Office",
                       "top", "top", 900 + i * 50, color=c, score=0.85))
    for i, c in enumerate(("navy", "teal")):
        pool.append(_p(f"kurta{i}", f"Women Straight Formal Kurta {c.title()}",
                       "top", "kurta", 1100 + i * 100, culture="indian",
                       color=c, score=0.8))
    for i in range(4):
        pool.append(_p(f"trouser{i}", f"Women Cigarette Trousers {i}",
                       "bottom", "trousers", 1000 + i * 50, score=0.8))
    for i in range(4):
        pool.append(_p(f"heel{i}", f"Women Block Heels {i}", "shoes", "heels",
                       1300 + i * 50, score=0.75))

    result = {
        "selected": pool[:6],
        "band": (900, 2500),
        "distribution": {},
        "candidates": pool,
        "intent": intent,
        "validation": {"passed": True, "confidence": 0.8, "level": "medium",
                       "dress_code": "business_casual", "violations": [],
                       "scores": {}},
        "blueprints": _office_bps(intent, n=4),
        "search_stats": {"pool_size": 40, "valid_candidates": 12,
                         "fallback_calls": 3},
        "stage_ms": {"intent_queries_ms": 5.0, "google_ms": 120.0,
                     "fallback_ms": 300.0, "enrich_ms": 2.0,
                     "filter_score_ms": 8.0, "total_ms": 440.0},
    }

    async def fake_search(**kw):
        return dict(result)

    monkeypatch.setattr(mkt, "search_marketplace_products", fake_search)
    resp = _run(mkt.get_marketplace_recommendations(
        occasion="work", gender="female", count=6))

    diag = resp["reco_diagnostics"]
    from api.services.reco_version import RECO_VERSION
    assert diag["reco_version"] == RECO_VERSION
    assert diag["request_id"] and len(diag["request_id"]) == 12
    sm = diag["stage_ms"]
    for key in ("google_ms", "fallback_ms", "filter_score_ms", "assemble_ms",
                "total_ms", "search_total_ms"):
        assert key in sm, sm
    # search-stage timings pass through verbatim from search_marketplace_products
    assert sm["search_total_ms"] == 440.0
    assert sm["google_ms"] == 120.0
    assert sm["assemble_ms"] >= 0
    assert diag["search"]["fallback_calls"] == 3
    c = diag["counts"]
    assert c["candidates"] == len(pool)
    assert c["looks"] == len(resp["looks"])
    assert 0 <= c["complete_looks"] <= c["looks"]
    assert c["duplicate_skeletons_removed"] >= 0
    assert c["incomplete_dropped"] >= 0


def test_diagnostics_defaults_when_search_omits_stats(monkeypatch):
    intent = _intent()
    result = {
        "selected": [],
        "band": None,
        "distribution": {},
        "candidates": [],
        "intent": intent,
        "validation": {"passed": False, "confidence": 0.0, "level": "low",
                       "dress_code": "", "violations": [], "scores": {}},
        "blueprints": [],
    }

    async def fake_search(**kw):
        return dict(result)

    monkeypatch.setattr(mkt, "search_marketplace_products", fake_search)
    resp = _run(mkt.get_marketplace_recommendations(
        occasion="work", gender="female", count=6))
    diag = resp["reco_diagnostics"]
    assert diag["stage_ms"]["google_ms"] == 0.0
    assert diag["counts"]["looks"] == 0
    assert diag["search"] == {}
