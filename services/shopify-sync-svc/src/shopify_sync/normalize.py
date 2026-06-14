from __future__ import annotations
import re
import uuid

from shopify_sync.models import CanonicalProduct, ShopifyProduct

_NS = uuid.UUID("8f1b0a2c-0000-5000-8000-000000000abc")


def product_uuid(shopify_id: int) -> uuid.UUID:
    return uuid.uuid5(_NS, f"shopify:{shopify_id}")


_GENDER = {"men": "male", "man": "male", "women": "female", "woman": "female",
           "unisex": "unisex", "kids": "kids", "boys": "male", "girls": "female"}

_ONTOLOGY = {
    "saree": ("g_saree", "full"), "lehenga": ("g_lehenga", "full"),
    "kurti": ("g_kurti", "top"), "kurta": ("g_kurta", "top"),
    "anarkali": ("g_anarkali", "full"), "salwar": ("g_salwar", "full"),
    "sherwani": ("g_sherwani", "full"), "palazzo": ("g_palazzo", "bottom"),
    "dupatta": ("g_dupatta", "drape"), "dress": ("g_dress", "full"),
    "shirt": ("g_shirt", "top"), "tshirt": ("g_tshirt", "top"), "t-shirt": ("g_tshirt", "top"),
    "jeans": ("g_jeans", "bottom"), "chinos": ("g_chinos", "bottom"),
    "blazer": ("g_blazer_w", "outerwear"), "shoe": ("g_footwear", "footwear"),
}

_COLOR_FAMILIES = {
    "red": ["red", "maroon", "rust", "wine"], "blue": ["blue", "navy", "teal"],
    "green": ["green", "olive", "mint"], "black": ["black"], "white": ["white", "ivory", "cream"],
    "yellow": ["yellow", "mustard", "gold"], "pink": ["pink", "rose", "magenta"],
    "orange": ["orange", "peach"], "purple": ["purple", "lavender"], "brown": ["brown", "beige", "tan"],
}


def _tags_list(tags) -> list[str]:
    if isinstance(tags, list):
        return [t.lower() for t in tags]
    return [t.strip().lower() for t in (tags or "").split(",") if t.strip()]


def _classify(p: ShopifyProduct) -> tuple[str | None, str | None, str]:
    haystack = " ".join([p.product_type or "", p.title] + _tags_list(p.tags)).lower()
    for key, (node, role) in _ONTOLOGY.items():
        if key in haystack:
            return node, key, role
    return None, p.product_type, "top"


def _gender(p: ShopifyProduct) -> str | None:
    hay = " ".join([p.product_type or "", *(_tags_list(p.tags))]).lower()
    for k, v in _GENDER.items():
        if k in hay:
            return v
    return None


def _color(p: ShopifyProduct) -> tuple[str | None, str | None]:
    candidates = [v.option2 for v in p.variants if v.option2] + _tags_list(p.tags)
    for c in candidates:
        cl = (c or "").lower()
        for fam, words in _COLOR_FAMILIES.items():
            if any(w in cl for w in words):
                return fam, None
    return None, None


def _canonical_title(title: str, brand: str | None) -> str:
    t = title.lower()
    if brand:
        t = t.replace(brand.lower(), "")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", t)).strip()


def _price_paise(p: ShopifyProduct) -> int | None:
    prices = [float(v.price) for v in p.variants if v.price]
    return int(min(prices) * 100) if prices else None


def normalize(p: ShopifyProduct) -> CanonicalProduct:
    node, category, role = _classify(p)
    color_family, color_hex = _color(p)
    return CanonicalProduct(
        product_id=product_uuid(p.id), shopify_id=p.id, title=p.title,
        canonical_title=_canonical_title(p.title, p.vendor), brand=p.vendor,
        category=category, gender=_gender(p), color_family=color_family, color_hex=color_hex,
        ontology_node_id=node, role=role, price=_price_paise(p),
        in_stock=any(v.available for v in p.variants) if p.variants else True,
        primary_image=p.images[0].src if p.images else None,
        images=[i.src for i in p.images], status=p.status,
        attributes={"product_type": p.product_type, "tags": _tags_list(p.tags)},
    )
