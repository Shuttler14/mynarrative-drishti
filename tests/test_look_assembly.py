"""RECO-v4.4 look-assembly tests (spec TEST1–6).

Layer contract: candidates arrive already hard-filtered + scored by the
existing recommender; assembly assigns products to outfit archetypes and
applies structural diversity. HARD VALIDITY → RELEVANCE → DIVERSITY.
"""
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from api.services import fashion_rules as frules  # noqa: E402
from api.services import look_assembly as la  # noqa: E402
from api.services.fashion_engine import build_intent, hard_filter  # noqa: E402


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


def _intent(**kw):
    base = dict(occasion="work", gender="female", budget_min=500, budget_max=5000)
    base.update(kw)
    return build_intent(**base)


def _blueprints(intent, n=4):
    return frules.build_outfit_blueprints(intent, n=n)


# ── Pool fixtures ─────────────────────────────────────────────────────

def _office_pool():
    """Rich business-casual pool: enough structure for four directions."""
    items = []
    # shirts (WF-02 + WF-07 both need one)
    for i, c in enumerate(("white", "blue", "grey", "ivory")):
        items.append(_p(f"shirt{i}", f"Women Formal {c.title()} Shirt", "top", "top",
                        900 + i * 100, color=c, score=0.85 - i * 0.03))
    # kurtas (WF-01)
    for i, c in enumerate(("navy", "teal")):
        items.append(_p(f"kurta{i}", f"Women Straight Formal Kurta {c.title()}", "top",
                        "kurta", 1100 + i * 100, culture="indian", color=c, score=0.8))
    # blazers (WF-06)
    for i, c in enumerate(("black", "charcoal")):
        items.append(_p(f"blazer{i}", f"Women Tailored Blazer {c.title()}", "top",
                        "outerwear", 2000 + i * 200, color=c, score=0.82))
    # cigarette trousers ×4 (three looks want them; unique products)
    for i, c in enumerate(("black", "navy", "grey", "beige")):
        items.append(_p(f"trouser{i}", f"Women Cigarette Trousers {c.title()}", "bottom",
                        "trousers", 1000 + i * 50, color=c, score=0.8))
    # chinos ×2 (WF-07)
    for i, c in enumerate(("khaki", "tan")):
        items.append(_p(f"chino{i}", f"Women Chinos {c.title()}", "bottom", "trousers",
                        1050 + i * 50, color=c, score=0.78))
    # heels ×2 (WF-01, WF-06) + loafers ×2 (WF-02, WF-07)
    for i, c in enumerate(("black", "nude")):
        items.append(_p(f"heel{i}", f"Women Block Heels {c.title()}", "shoes", "heels",
                        1300 + i * 100, color=c, score=0.75))
        items.append(_p(f"loafer{i}", f"Women Leather Loafers {c.title()}", "shoes",
                        "shoes", 1400 + i * 100, color=c, score=0.76))
    return items


def _wedding_pool():
    items = []
    for i, c in enumerate(("red", "gold", "maroon", "pink")):
        items.append(_p(f"saree{i}", f"Women Silk Saree {c.title()}", "full", "saree",
                        4000 + i * 200, culture="indian", color=c, score=0.85))
    for i, c in enumerate(("cream", "white")):
        items.append(_p(f"kset{i}", f"Women Kurta Set {c.title()}", "full", "kurta_set",
                        2500 + i * 100, culture="indian", color=c, score=0.8))
    items.append(_p("lehenga0", "Women Bridal Lehenga Red", "full", "lehenga",
                    6000, culture="indian", color="red", score=0.83))
    items.append(_p("anarkali0", "Women Anarkali Green", "full", "anarkali",
                    3500, culture="indian", color="green", score=0.81))
    for i, c in enumerate(("gold", "bronze")):
        items.append(_p(f"eheel{i}", f"Women Ethnic Heels {c.title()}", "shoes",
                        "heels", 1500 + i * 100, culture="indian", color=c, score=0.7))
    items.append(_p("jutti0", "Women Juttis Gold", "shoes", "shoes", 900,
                    culture="indian", color="gold", score=0.7))
    return items


def _casual_pool():
    items = []
    for i, c in enumerate(("white", "black", "olive", "rust")):
        items.append(_p(f"tee{i}", f"Women Cotton T-Shirt {c.title()}", "top", "tshirt",
                        500 + i * 50, color=c, score=0.8))
    for i, c in enumerate(("blue", "grey")):
        items.append(_p(f"cshirt{i}", f"Women Casual Shirt {c.title()}", "top", "top",
                        800 + i * 50, color=c, score=0.79))
    items.append(_p("kurta9", "Women Printed Kurta Indigo", "top", "kurta", 900,
                    culture="indian", color="indigo", score=0.77))
    for i, c in enumerate(("blue", "black")):
        items.append(_p(f"jean{i}", f"Women Jeans {c.title()}", "bottom", "jeans",
                        1200 + i * 100, color=c, score=0.78))
    for i, c in enumerate(("white", "black")):
        items.append(_p(f"sneak{i}", f"Women Sneakers {c.title()}", "shoes", "shoes",
                        1600 + i * 100, color=c, score=0.75))
        items.append(_p(f"loaf{i}", f"Women Loafers {c.title()}", "shoes", "shoes",
                        1500 + i * 100, color=c, score=0.74))
    return items


def _bodies(looks):
    return [lk["signature"].get("body") for lk in looks]


# ── TEST 1: business casual office → four structurally different looks ─

def test_office_four_distinct_directions():
    intent = _intent()
    bps = _blueprints(intent, n=4)
    assert len(bps) == 4, [b["archetype"] for b in bps]
    looks = la.assemble_looks(_office_pool(), bps, intent, count=4)
    assert len(looks) == 4, [lk["label"] for lk in looks]

    # No two looks share a skeleton (body+lower)
    for i, a in enumerate(looks):
        for b in looks[i + 1:]:
            assert not la.too_similar(a["signature"], b["signature"]), (
                f"duplicate skeleton: {a['label']} vs {b['label']}")
    # Products unique across looks
    pids = [p["product_id"] for lk in looks for p in lk["products"]]
    assert len(pids) == len(set(pids)), "product leaked across looks"
    # Not four colour swaps of one shirt: ≥3 distinct body categories
    assert len(set(_bodies(looks))) >= 3, _bodies(looks)
    # Every look has a hero
    for lk in looks:
        assert any(p["fashion"]["slot"] in ("top", "full") for p in lk["products"]), lk


# ── TEST 2: wedding → four coloured sarees must NOT be four looks ─────

def test_four_coloured_sarees_collapse_to_one():
    intent = build_intent(occasion="wedding", gender="female")
    real = frules.build_outfit_blueprints(intent, n=8)
    saree_bp = next(b for b in real if b["archetype"] == "FW-02")  # Saree + Ethnic Heels
    # Simulate four saree archetypes (the historical failure mode)
    fake = []
    import copy as _copy
    for i in range(4):
        b = _copy.deepcopy(saree_bp)
        b["id"] = f"{b['id']}-x{i}"
        fake.append(b)
    looks = la.assemble_looks(_wedding_pool(), fake, intent, count=4)
    assert len(looks) == 1, [lk["signature"] for lk in looks]

    # But real distinct wedding archetypes → multiple directions
    real_bps = _blueprints(intent, n=4)
    looks2 = la.assemble_looks(_wedding_pool(), real_bps, intent, count=4)
    assert len(looks2) >= 3, [lk["label"] for lk in looks2]
    for i, a in enumerate(looks2):
        for b in looks2[i + 1:]:
            assert not la.too_similar(a["signature"], b["signature"])


# ── TEST 3: casual weekend → distinct archetypes, never forced to four ─

def test_casual_distinct_archetypes_and_no_forcing():
    intent = build_intent(occasion="casual", gender="female")
    bps = _blueprints(intent, n=4)  # only FC-01..03 exist
    assert len(bps) == 3, "casual occasion has exactly 3 archetypes"
    looks = la.assemble_looks(_casual_pool(), bps, intent, count=4)
    # Three archetypes → at most three looks (never invent a 4th)
    assert len(looks) <= 3
    assert len(set(_bodies(looks))) == len(looks), (
        "four t-shirt colours leaked as distinct looks", _bodies(looks))


# ── TEST 4: See Variations stay in the selected direction ─────────────

def test_variations_stay_in_parent_archetype():
    intent = _intent()
    parent_arch = "WF-02"  # Formal Shirt + Tailored Trousers
    clones = la.build_variation_blueprints(intent, parent_arch, n=4)
    assert len(clones) == 4
    assert all(c["archetype"] == parent_arch for c in clones), [
        c["archetype"] for c in clones]
    assert all(c["is_variation"] for c in clones)
    assert all(c["id"] != clones[0]["id"] for c in clones[1:])

    queries = la.compile_variation_queries(clones, intent, max_queries=9)
    assert queries, "variation queries empty"
    qmap = intent.get("blueprint_query_map") or {}
    assert qmap, "blueprint query map not written"
    assert all(s in ("primary", "bottom", "shoes") for s, _ in queries)

    # Pool with parent products among them; parent must not reappear
    pool = _office_pool()
    parent_ids = {"shirt0", "trouser0", "loafer0"}
    looks = la.assemble_looks(
        pool, clones, intent, count=4,
        exclude_product_ids=list(parent_ids),
        exclude_titles=["Women Formal White Shirt"],
        enforce_diversity=False,
    )
    assert looks, "variations empty"
    served = {p["product_id"] for lk in looks for p in lk["products"]}
    assert not (served & parent_ids), served & parent_ids
    assert not any(p["title"] == "Women Formal White Shirt"
                   for lk in looks for p in lk["products"])
    # All recognisably the SAME direction (same archetype), no saree/other arch
    for lk in looks:
        assert lk["archetype_id"] == parent_arch, lk
        for p in lk["products"]:
            assert p["fashion"]["category"] not in ("saree", "lehenga", "anarkali")
    # Products unique across variations (no duplicate serves)
    pids = [p["product_id"] for lk in looks for p in lk["products"]]
    assert len(pids) == len(set(pids)), "variation re-served a product"
    # Variation mode must NOT collapse to 1 just because skeletons match
    assert len(looks) >= 2, len(looks)


# ── TEST 5: wedding context dominates western preference ──────────────

def test_wedding_context_dominates_western_preference():
    intent = build_intent(
        occasion="wedding", gender="female",
        user_prefs={"wear_weights": {"western": 0.9, "indian": 0.1,
                                      "indo_western": 0.0}},
    )
    bps = frules.get_archetypes("female", "wedding", count=4, intent=intent)
    assert bps, "wedding archetypes"
    assert all(a.get("id", "").startswith("FW") for a in bps), [
        a["id"] for a in bps]  # preference never swaps the occasion

    # Real pipeline: pool goes through the existing hard gates first
    pool = [
        *_wedding_pool(),
        _p("tee9", "Women Graphic Print T-Shirt", "top", "tshirt", 500),
        _p("shoe9", "Women White Sneakers", "shoes", "sneakers", 1200),
    ]
    filtered = hard_filter(pool, intent, None)
    blueprints = frules.build_outfit_blueprints(intent, n=4)
    looks = la.assemble_looks(filtered, blueprints, intent, count=4)
    assert looks, "no wedding looks assembled"
    for lk in looks:
        assert lk["occasion"] == "wedding", lk
        val = frules.validate_outfit(lk["products"], intent)
        assert val.get("passed"), (lk["label"], val.get("violations"))


# ── TEST 6: closet awareness — feature does not exist yet ─────────────

def test_closet_awareness_not_implemented_yet():
    pytest.skip(
        "No closet/wardrobe persistence exists in this system "
        "(only ephemeral StyleAnalysisRequest.wardrobe_items) — "
        "Enhancement 11 deferred until closet tables ship.")


# ── Unit pins: similarity rules + gates ───────────────────────────────

def test_signature_ignores_colour_and_price():
    a = [_p("1", "White Shirt", "top", "shirt", 500, color="white"),
         _p("2", "Black Trousers", "bottom", "trousers", 600, color="black")]
    b = [_p("3", "Blue Shirt", "top", "shirt", 2500, color="blue"),
         _p("4", "Navy Trousers", "bottom", "trousers", 2600, color="navy")]
    assert la.look_signature(a) == la.look_signature(b)
    assert la.too_similar(la.look_signature(a), la.look_signature(b))


def test_different_top_category_is_a_new_direction():
    a = [_p("1", "Formal Shirt", "top", "shirt"),
         _p("2", "Trousers", "bottom", "trousers")]
    b = [_p("3", "Blouse", "top", "blouse"),
         _p("4", "Wide Leg Pants", "bottom", "trousers")]
    assert not la.too_similar(la.look_signature(a), la.look_signature(b))


def test_full_outfit_never_matches_separates():
    a = [_p("1", "Saree", "full", "saree"), _p("2", "Heels", "shoes", "heels")]
    b = [_p("3", "Shirt", "top", "shirt"), _p("4", "Trousers", "bottom", "trousers")]
    assert not la.too_similar(la.look_signature(a), la.look_signature(b))


def test_imageless_products_never_enter_a_look():
    intent = _intent()
    bps = _blueprints(intent, n=4)
    pool = _office_pool()
    for p in pool:
        p["image_url"] = ""  # simulate image gate failure downstream
    looks = la.assemble_looks(pool, bps, intent, count=4)
    assert all(p["image_url"] for lk in looks for p in lk["products"])
    # Pool without any image → empty, never imageless looks
    assert looks == []


def test_variations_require_archetype_id():
    intent = _intent()
    clones = la.build_variation_blueprints(intent, "", n=4)
    # Unknown archetype → nearest anchor fallback, still valid blueprint ids
    assert clones
    assert all(c.get("id") for c in clones)


# ── Path A wiring: get_marketplace_recommendations response contract ──

def test_reco_endpoint_returns_assembled_looks(monkeypatch):
    """Offline integration: search is mocked, everything downstream (assembly,
    format, union contract, budget/brands blocks) runs for real."""
    import asyncio

    from api.services import marketplace_reco as mkt

    intent = _intent()
    result = {
        "selected": _office_pool()[:6],
        "band": (900, 1600),
        "distribution": {},
        "candidates": _office_pool(),
        "intent": intent,
        "validation": {"passed": True, "confidence": 0.8, "level": "medium",
                       "dress_code": "business_casual", "violations": [],
                       "scores": {}},
        "blueprints": _blueprints(intent, n=4),
    }

    async def fake_search(**kw):
        return result

    monkeypatch.setattr(mkt, "search_marketplace_products", fake_search)
    resp = asyncio.run(mkt.get_marketplace_recommendations(
        occasion="work", gender="female", count=6))

    looks = resp["looks"]
    assert len(looks) == 4, [lk["label"] for lk in looks]
    for lk in looks:
        assert lk["products"], lk
        assert lk["outfit_id"] and lk["archetype_id"]
        assert lk["total_price"] >= 0
    # Flat contract = deduped union of look products
    flat = [(p["source"], p["product_id"]) for p in resp["recommendations"]]
    assert len(flat) == len(set(flat)), "flat recommendations repeated a product"
    in_looks = {(p["source"], p["product_id"])
                for lk in looks for p in lk["products"]}
    assert set(flat) <= in_looks
    # Skeletons all distinct after diversity pass
    for i, a in enumerate(looks):
        for b in looks[i + 1:]:
            assert not la.too_similar(a["signature"], b["signature"])
    # Budget + validation blocks still present (backward compat)
    assert "budget_intelligence" in resp and "outfit_validation" in resp

    # Exclusions reach assembly through the same entry point
    resp2 = asyncio.run(mkt.get_marketplace_recommendations(
        occasion="work", gender="female", count=6,
        exclude_product_ids=["shirt0"]))
    served2 = {p["product_id"] for lk in resp2["looks"] for p in lk["products"]}
    assert "shirt0" not in served2
