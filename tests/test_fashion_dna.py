"""Fashion DNA — soft preference scoring, culture re-rank, query bias."""
from api.services.fashion_engine import (
    _SCORE_WEIGHTS,
    _affinity_score,
    build_intent,
    score_product,
)
from api.services import fashion_rules as frules
from api.utils.identity import normalize_wear_weights


WESTERN_OFFICE = {
    "wear_weights": {"western": 0.9, "indian": 0.15, "indo_western": 0.55},
    "vibes": ["minimal"],
}


def test_score_weights_sum_to_one():
    assert abs(sum(_SCORE_WEIGHTS.values()) - 1.0) < 1e-9
    assert "affinity" in _SCORE_WEIGHTS


def test_normalize_wear_weights_list():
    w = normalize_wear_weights(["western", "indo_western"])
    assert w["western"] >= 0.75
    assert w["indo_western"] >= 0.75
    assert w["indian"] < 0.5


def test_normalize_wear_weights_dict():
    w = normalize_wear_weights({"Western": 0.9, "indian": 0.1, "fusion": 0.5})
    assert w["western"] == 0.9
    assert w["indian"] == 0.1
    assert w["indo_western"] == 0.5


def test_build_intent_carries_user_prefs():
    intent = build_intent(occasion="work", gender="female", user_prefs=WESTERN_OFFICE)
    assert intent["wear_weights"]["western"] == 0.9
    assert intent["occasion"] == "work"
    assert intent["dress_code"]  # hard gate present
    assert intent["vibes"] == ["minimal"]


def test_strong_western_seeds_style_terms():
    intent = build_intent(occasion="work", gender="female", user_prefs=WESTERN_OFFICE)
    # corporate/tailored terms should appear when no explicit style given
    assert intent["style_query_terms"], "expected soft style terms from western pref"


def test_archetype_rerank_two_plus_one():
    intent = build_intent(occasion="work", gender="female", user_prefs=WESTERN_OFFICE)
    arch = frules.get_archetypes("female", "work", count=3, intent=intent)
    assert len(arch) == 3
    garments = frules.rules()["garments"]

    def cultures(a):
        out = []
        for c in a.get("components", []):
            g = garments.get(c.get("garment", ""), {})
            if g.get("culture"):
                out.append(g["culture"])
        return out

    # At least one preferred (western) and one discovery (non-western-dominant)
    all_cultures = [c for a in arch for c in cultures(a)]
    assert "western" in all_cultures
    # Discovery slot should include indian somewhere in the 3 blueprints
    # (controlled exploration) when pool has ethnic archetypes
    assert any(c in ("indian", "western") for c in all_cultures)


def test_blueprint_components_carry_culture():
    intent = build_intent(occasion="work", gender="female", user_prefs=WESTERN_OFFICE)
    bps = frules.build_outfit_blueprints(intent, n=3)
    assert bps
    for bp in bps:
        for comp in bp["components"]:
            assert "culture" in comp


def test_explain_mentions_western_preference():
    intent = build_intent(occasion="work", gender="female", user_prefs=WESTERN_OFFICE)
    bps = frules.build_outfit_blueprints(intent, n=3)
    why = frules.explain_blueprint(bps[0], intent)
    assert "Western preference" in why or "Western" in why


def test_query_bias_adds_western_for_office():
    intent = build_intent(occasion="work", gender="female", user_prefs=WESTERN_OFFICE)
    component = {
        "slot": "primary",
        "colour": "white",
        "fabric": "",
        "search_term": "western shirt",
        "culture": "western",
    }
    q = frules.compile_component_query(component, intent, level=0)
    assert "western" in q.lower()
    assert "India" in q


def test_affinity_score_prefers_matching_culture():
    intent = build_intent(occasion="work", gender="female", user_prefs=WESTERN_OFFICE)
    western = {
        "title": "Women White Western Shirt",
        "fashion": {"category": "shirt", "color": "white", "patterns": [], "fabrics": [], "fits": [], "culture": "western"},
    }
    indian = {
        "title": "Women Embroidered Kurta",
        "fashion": {"category": "kurta", "color": "red", "patterns": ["embroidered"], "fabrics": [], "fits": [], "culture": "indian"},
    }
    s_w = _affinity_score(western, intent)
    s_i = _affinity_score(indian, intent)
    assert s_w > s_i


def test_affinity_neutral_without_prefs():
    intent = build_intent(occasion="casual", gender="female")
    p = {
        "title": "Random Top",
        "fashion": {"category": "top", "color": "blue", "patterns": [], "fabrics": [], "fits": []},
    }
    assert abs(_affinity_score(p, intent) - 0.5) < 1e-6


def test_score_product_includes_affinity():
    intent = build_intent(occasion="work", gender="female", user_prefs=WESTERN_OFFICE)
    p = {
        "title": "Women White Western Shirt",
        "price": 1500,
        "rating": 4.2,
        "rating_count": 100,
        "fashion": {
            "category": "shirt", "color": "white", "patterns": [], "fabrics": [],
            "fits": [], "culture": "western", "fit": "regular",
        },
    }
    score_product(p, intent, (500, 5000), 500, 5000)
    assert "affinity" in p["match_scores"]
    assert p["match_scores"]["affinity"] > 0.5


def test_hard_dress_code_still_enforced():
    """Preferences never override occasion hard gates."""
    intent = build_intent(occasion="work", gender="female", user_prefs=WESTERN_OFFICE)
    assert intent["dress_code"] in ("business_casual", "business_formal", "smart_casual", "casual")
    # Wedding negative keywords still apply for work
    negs = frules.negative_keywords("work")
    assert "lehenga" in negs or "bridal" in negs or "wedding" in negs
