"""Slot-coverage fill: office/wee looks must never ship footwear-only.

`_fill_missing_slots` tops up missing top/bottom slots from the gender-filtered
inventory after the band rescue. Two-pass: formal-signal ON first (dress code),
then relaxed — occasion/gender/casual/ethnic/price gates stay in both passes.
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
PLAIN_TOP = _p("Women Drape Cowl Neck Top", "top", 2000, "top")
PLAIN_BOTTOM = _p("Women Relaxed Fit Culotte", "bottom", 800, "bottom")


def _intent():
    return fe.build_intent(occasion="work", gender="female", budget_min=500, budget_max=1000)


def test_fill_adds_formal_top_and_plain_bottom_to_shoes_only_pool():
    intent = _intent()
    out = fe._fill_missing_slots([LOAFER], [FORMAL_SHIRT, PLAIN_TOP, PLAIN_BOTTOM], intent)
    slots = [(p.get("fashion") or {}).get("slot") for p in out]
    assert "shoes" in slots, out
    assert "top" in slots, f"top still missing: {slots}"
    assert "bottom" in slots, f"bottom still missing: {slots}"
    # pass 1 (formal ON) recovered the dress-code shirt before the relaxed pass
    tops = [p["title"] for p in out if (p.get("fashion") or {}).get("slot") == "top"]
    assert "Women Linen Shirt" in tops, tops
    assert tops[0] == "Women Linen Shirt", (
        f"formal-signal top must be filled first, got {tops}")


def test_fill_relaxes_formal_signal_when_no_formal_top_exists():
    intent = _intent()
    out = fe._fill_missing_slots([LOAFER], [PLAIN_TOP, PLAIN_BOTTOM], intent)
    slots = [(p.get("fashion") or {}).get("slot") for p in out]
    assert "top" in slots and "bottom" in slots, slots


def test_fill_caps_three_per_missing_slot():
    intent = _intent()
    plain_tops = [_p(f"Women Drape Top style {i}", "top", 1200 + i) for i in range(5)]
    out = fe._fill_missing_slots([LOAFER], plain_tops + [PLAIN_BOTTOM], intent)
    tops = [p for p in out if (p.get("fashion") or {}).get("slot") == "top"]
    assert len(tops) == 3, [p["title"] for p in tops]


def test_fill_noop_when_pool_complete():
    intent = _intent()
    pool = [FORMAL_SHIRT, PLAIN_BOTTOM, LOAFER]
    out = fe._fill_missing_slots(pool, [PLAIN_TOP], intent)
    assert [id(p) for p in out] == [id(p) for p in pool]


def test_fill_skips_empty_pool_for_degrade_chain():
    intent = _intent()
    assert fe._fill_missing_slots([], [FORMAL_SHIRT, PLAIN_BOTTOM, LOAFER], intent) == []


def test_fill_respects_hard_gates_in_relaxed_pass():
    intent = _intent()
    hoodie = _p("Women Oversized Hoodie", "top", 900, "hoodie")
    mens_top = _p("Men Drape Cowl Neck Top", "top", 900, "top")
    out = fe._fill_missing_slots([LOAFER], [hoodie, mens_top, PLAIN_TOP], intent)
    titles = [p["title"] for p in out]
    assert "Women Oversized Hoodie" not in titles, "casual reject must survive the relaxed pass"
    assert "Men Drape Cowl Neck Top" not in titles, "gender gate must survive the relaxed pass"
    assert "Women Drape Cowl Neck Top" in titles


def test_recommend_products_office_never_returns_footwear_only():
    """End-to-end: tight band + formal gate → pool starved; fill restores coverage."""
    intent = _intent()
    # plain top priced outside the band dies at the band gate before scoring;
    # plain bottom dies at the formal gate; loafer passes both.
    products = [LOAFER, PLAIN_TOP, PLAIN_BOTTOM]
    result = fe.recommend_products(products, intent, count=3)
    selected = result["selected"]
    assert selected, "recommendations must not be empty"
    slots = {(p.get("fashion") or {}).get("slot") for p in selected}
    assert "top" in slots, f"footwear-only regression: {[(p['title'], (p.get('fashion') or {}).get('slot')) for p in selected]}"
    assert "shoes" in slots


def test_imageless_products_are_never_selected():
    """A product with no image is a blank widget card + VTON 400 — must die."""
    intent = _intent()
    ghost = _p("Women Linen Shirt", "top", 600, "shirt")
    ghost["image_url"] = ""
    result = fe.recommend_products([ghost, PLAIN_BOTTOM, LOAFER], intent, count=3)
    titles = [p["title"] for p in result["selected"]]
    assert "Women Linen Shirt" not in titles, "imageless product leaked into selection"
    assert result["selected"], "remaining inventory must still produce a selection"


def test_office_formal_signal_contract_matches_golden():
    """Golden f02/f07: bare kurta is NOT a formal signal for office; a real
    formal marker is; set pieces ("with Dupatta") stay rejected either way."""
    plain_kurta = _p("Women Cotton Straight Fit Kurta", "top", 499, "kurta")
    formal_kurta = _p("Men's Formal Silk Kurta", "top", 2199, "kurta")
    set_kurta = _p("Women's Cotton Kurta with Dupatta", "top", 1599, "kurta")

    intent_plain = fe.build_intent(occasion="work", gender="")
    intent_indian = fe.build_intent(
        occasion="work", gender="", user_prefs={"wear_weights": {"indian": 1.0}})

    assert not fe.hard_filter([plain_kurta], intent_plain, None), (
        "bare kurta must not satisfy the plain-office formal gate (golden f02)")
    assert not fe.hard_filter([set_kurta], intent_plain, None), (
        "kurta-with-dupatta must not pass plain office (golden f02)")
    assert not fe.hard_filter([set_kurta], intent_indian, None), (
        "set piece must not pass indian office either (golden f07)")
    assert fe.hard_filter([formal_kurta], intent_indian, None), (
        "a formally-marked kurta must pass indian office (golden f07 p39)")
