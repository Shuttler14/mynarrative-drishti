"""Slot-coverage fill/rescue: office looks must never ship footwear-only
AND never ship non-formal inventory (RECO-v4.3 contract).

`_fill_missing_slots` tops up missing top/bottom slots from the gender-filtered
inventory after the band rescue. Single pass, full hard gate (band bypassed,
style gates kept) — office never relaxes the formal signal: a starved office
draw degrades to fewer/empty items, not to ethnic-casual inventory.
"""
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from api.services import fashion_engine as fe  # noqa: E402


def _p(title, slot, price, category=None):
    return {
        "title": title,
        "price": price,
        "image_url": f"https://cdn.example.com/{abs(hash((title, price)))}.jpg",
        "id": f"id-{abs(hash((title, price)))}",
        "fashion": {
            "slot": slot,
            "category": category or slot,
            "color": "neutral",
            "patterns": [],
            "fabrics": [],
            "fits": [],
            "culture": "western",
        },
    }


LOAFER = _p("Women Classic Leather Loafer", "shoes", 700, "shoes")
FORMAL_SHIRT = _p("Women Linen Shirt", "top", 1500, "shirt")
PLAIN_TOP = _p("Women Drape Cowl Neck Top", "top", 2000, "top")  # no formal signal
FORMAL_BOTTOM = _p("Women Tailored Relaxed Fit Culotte", "bottom", 800, "bottom")
SPORT_SANDAL = _p("womens Ss 101 Sport Sandal", "shoes", 599, "shoes")


def _intent():
    return fe.build_intent(occasion="work", gender="female", budget_min=500, budget_max=1000)


def test_fill_adds_formal_top_and_bottom_to_shoes_only_pool():
    intent = _intent()
    out = fe._fill_missing_slots(
        [LOAFER], [FORMAL_SHIRT, PLAIN_TOP, FORMAL_BOTTOM], intent)
    slots = [(p.get("fashion") or {}).get("slot") for p in out]
    assert "shoes" in slots, out
    assert "top" in slots, f"top still missing: {slots}"
    assert "bottom" in slots, f"bottom still missing: {slots}"
    tops = [p["title"] for p in out if (p.get("fashion") or {}).get("slot") == "top"]
    assert tops == ["Women Linen Shirt"], (
        f"only formal-signal tops may fill an office draw, got {tops}")


def test_office_fill_never_relaxes_formal_signal():
    """No formal top in inventory → fill adds the formal bottom but never a
    signal-less top (the old relaxed pass shipped 'Drape Cowl Neck Top' etc.)."""
    intent = _intent()
    out = fe._fill_missing_slots([LOAFER], [PLAIN_TOP, FORMAL_BOTTOM], intent)
    slots = [(p.get("fashion") or {}).get("slot") for p in out]
    assert "bottom" in slots, slots
    assert "top" not in slots, f"signal-less top leaked into office fill: {slots}"


def test_fill_caps_three_per_missing_slot():
    intent = _intent()
    formal_tops = [_p(f"Women Formal Drape Top style {i}", "top", 1200 + i) for i in range(5)]
    out = fe._fill_missing_slots([LOAFER], formal_tops + [FORMAL_BOTTOM], intent)
    tops = [p for p in out if (p.get("fashion") or {}).get("slot") == "top"]
    assert len(tops) == 3, [p["title"] for p in tops]


def test_fill_noop_when_pool_complete():
    intent = _intent()
    pool = [FORMAL_SHIRT, FORMAL_BOTTOM, LOAFER]
    out = fe._fill_missing_slots(pool, [PLAIN_TOP], intent)
    assert [id(p) for p in out] == [id(p) for p in pool]


def test_fill_skips_empty_pool():
    intent = _intent()
    assert fe._fill_missing_slots([], [FORMAL_SHIRT, FORMAL_BOTTOM, LOAFER], intent) == []


def test_office_fill_enforces_casual_gender_and_formal_gates():
    intent = _intent()
    hoodie = _p("Women Oversized Hoodie", "top", 900, "hoodie")
    mens_top = _p("Men Drape Cowl Neck Top", "top", 900, "top")
    out = fe._fill_missing_slots([LOAFER], [hoodie, mens_top, PLAIN_TOP], intent)
    titles = [p["title"] for p in out]
    assert "Women Oversized Hoodie" not in titles, "casual reject must hold in fill"
    assert "Men Drape Cowl Neck Top" not in titles, "gender gate must hold in fill"
    assert "Women Drape Cowl Neck Top" not in titles, "signal-less top must hold in fill"
    assert out == [LOAFER], titles


def test_rescue_keeps_style_gates_for_office():
    """Band bypass is rescue's only exemption — sport sandals and signal-less
    bottoms must not enter an office pool through it."""
    intent = _intent()
    ethnic_set = _p("Women's Cotton Kurta with Dupatta", "full", 595, "kurta_set")
    out = fe._rescue_slot_coverage(
        [], [SPORT_SANDAL, PLAIN_TOP, FORMAL_BOTTOM, ethnic_set], intent)
    titles = [p["title"] for p in out]
    assert "Women Tailored Relaxed Fit Culotte" in titles, titles
    assert SPORT_SANDAL["title"] not in titles, "sport shoe slipped past office gates"
    assert "Women Drape Cowl Neck Top" not in titles, "rescue must not pull tops"


def test_recommend_products_office_never_returns_footwear_only():
    """End-to-end: tight band + formal gate → pool starved; fill restores coverage."""
    intent = _intent()
    # shirt is priced outside the band (dies at the band gate before scoring);
    # bottom sits in-band; loafer passes both. Fill pulls the shirt back in.
    products = [LOAFER, FORMAL_SHIRT, FORMAL_BOTTOM]
    result = fe.recommend_products(products, intent, count=3)
    selected = result["selected"]
    assert selected, "recommendations must not be empty"
    slots = {(p.get("fashion") or {}).get("slot") for p in selected}
    assert "top" in slots, f"footwear-only regression: {[(p['title'], (p.get('fashion') or {}).get('slot')) for p in selected]}"
    assert "shoes" in slots


def test_office_draw_without_formal_body_ships_empty_not_shoes():
    """No formal body garment exists → empty beats footwear-only (and beats
    the old non-formal degrade: PLAIN_TOP stays out)."""
    intent = _intent()
    result = fe.recommend_products([LOAFER, PLAIN_TOP], intent, count=3)
    assert result["selected"] == [], [
        (p["title"], (p.get("fashion") or {}).get("slot")) for p in result["selected"]]
    assert result["validation"].get("passed") is False


def test_imageless_products_are_never_selected():
    """A product with no image is a blank widget card + VTON 400 — must die."""
    intent = _intent()
    ghost = _p("Women Linen Shirt", "top", 600, "shirt")
    ghost["image_url"] = ""
    result = fe.recommend_products([ghost, FORMAL_BOTTOM, LOAFER], intent, count=3)
    titles = [p["title"] for p in result["selected"]]
    assert "Women Linen Shirt" not in titles, "imageless product leaked into selection"
    assert result["selected"], "remaining inventory must still produce a selection"


def test_office_formal_signal_contract_matches_golden():
    """Golden f02/f07: bare kurta is NOT a formal signal for office; a real
    formal marker is; set pieces ("with Dupatta") stay rejected either way."""
    plain_kurta = _p("Women Cotton Straight Fit Kurta", "top", 499, "kurta")
    formal_kurta = _p("Men's Formal Silk Kurta", "top", 2199, "kurta")
    set_kurta = _p("Women's Cotton Kurta with Dupatta", "top", 1599, "kurta")
    office = fe.build_intent(occasion="work", gender="")
    passed = fe.hard_filter([plain_kurta, formal_kurta, set_kurta], office, None)
    titles = [p["title"] for p in passed]
    assert titles == ["Men's Formal Silk Kurta"], titles
