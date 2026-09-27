"""
Fashion Intelligence Engine — knowledge-graph-lite recommendation layer.

Implements the My Narrative blueprint:
  USER INTENT → SLOT QUERIES → HARD FILTERS → FASHION ATTRIBUTE ENRICHMENT →
  MATCH SCORING → DIVERSITY SELECTION → LABELLED, VTON-READY PICKS

Design principles (from the architecture doc):
  - Product (not brand) is the primary object; brand is one ranking attribute.
  - Google Shopping is the discovery layer; intelligence lives here.
  - Deterministic, cheap scoring. No LLM calls in the hot path.
  - Style/occasion/fit/colour/budget matching via a controlled taxonomy.
  - Outfit assembly: search TOP / BOTTOM / SHOES slots independently,
    then compose a diverse, purchase-ready set of looks.
"""
from __future__ import annotations

import logging
import math
import re

from api.services.price_intelligence import (
    LEVEL_WINDOWS,
    price_distribution,
    resolve_band,
)
from api.services import fashion_rules as frules

logger = logging.getLogger("drishti.reco.fashion")

# ══════════════════════════════════════════════════════════════
# FASHION KNOWLEDGE GRAPH (controlled vocabularies)
# ══════════════════════════════════════════════════════════════

_COLOR_FAMILIES: list[tuple[str, list[str]]] = [
    ("cream", ["off-white", "off white", "cream", "beige", "nude", "oatmeal", "ivory",
               "eggshell", "taupe", "camel", "sand", "tan"]),
    ("white", ["white", "pearl"]),
    ("black", ["black", "onyx"]),
    ("grey", ["grey", "gray", "charcoal", "silver", "graphite", "ash"]),
    ("navy", ["navy", "midnight blue", "ink blue"]),
    ("blue", ["blue", "indigo", "teal", "turquoise", "cobalt", "azure", "aqua", "sky"]),
    ("green", ["green", "olive", "khaki", "sage", "mint", "emerald", "moss"]),
    ("brown", ["brown", "chocolate", "coffee", "walnut", "chestnut", "rust", "bronze", "copper"]),
    ("red", ["red", "maroon", "burgundy", "wine", "crimson", "cherry", "scarlet"]),
    ("pink", ["pink", "blush", "rose", "peach", "coral", "magenta", "fuchsia", "rani"]),
    ("yellow", ["yellow", "mustard", "gold", "golden", "amber", "lemon", "marigold"]),
    ("purple", ["purple", "lavender", "violet", "lilac", "plum", "mauve"]),
    ("orange", ["orange", "tangerine", "apricot"]),
]

_FABRICS: list[tuple[str, list[str]]] = [
    ("linen", ["linen"]),
    ("denim", ["denim"]),
    ("silk", ["silk"]),
    ("chiffon", ["chiffon", "georgette", "crepe"]),
    ("satin", ["satin"]),
    ("velvet", ["velvet"]),
    ("wool", ["wool", "tweed", "fleece", "cashmere", "knit"]),
    ("leather", ["leather", "faux leather"]),
    ("rayon", ["rayon", "viscose", "modal", "lyocell"]),
    ("cotton", ["cotton", "khadi", "chikankari"]),
    ("polyester", ["polyester", "poly", "nylon", "spandex", "elastane", "lycra"]),
]

_FITS: list[tuple[str, list[str]]] = [
    ("oversized", ["oversized", "oversize", "drop shoulder", "baggy", "jumbo"]),
    ("relaxed", ["relaxed", "comfort fit", "easy fit", "loose"]),
    ("slim", ["slim", "tailored", "fitted", "bodycon", "pencil"]),
    ("straight", ["straight"]),
    ("flared", ["flared", "flare", "a-line", "aline", "anarkali", "fit and flare", "umbrella"]),
    ("skinny", ["skinny"]),
    ("regular", ["regular", "classic fit"]),
]

_PATTERNS: list[tuple[str, list[str]]] = [
    ("embroidered", ["embroider", "sequin", "zari", "mirror work", "thread work",
                     "gota", "phulkari", "chikankari", "embellish", "gota patti"]),
    ("floral", ["floral", "flower", "bloom"]),
    ("printed", ["print", "graphic", "typograph", "abstract", "tie dye", "tie-dye",
                 "ombre", "paisley", "animal"]),
    ("striped", ["stripe", "pinstripe"]),
    ("checked", ["check", "plaid", "gingham"]),
    ("textured", ["textured", "jacquard", "dobby", "waffle", "ribbed", "self design"]),
    ("solid", ["solid", "plain"]),
]

# (regex, category, slot, vton_friendly) — order matters (most specific first)
_CATEGORY_RULES: list[tuple[str, str, str, bool]] = [
    (r"\bt[- ]?shirts?\b|\btees?\b", "tshirt", "top", True),
    (r"\bpolos?\b", "polo", "top", True),
    # Footwear BEFORE ethnic body garments so "ethnic jutti shoes" never becomes sherwani
    (r"\bjuttis?\b|\bmojaris?\b|\bmojari\b|\bmojri\b|\bkhussa\b|\bkolhapuri\b|\bchappal\b|\bslip[- ]?ons?\b|\bfootwear\b|\bpumps?\b", "shoes", "shoes", False),
    # Full multi-garment sets BEFORE trousers so "Kurta Palazzo Set" stays full, not bottom
    (r"\bkurta set\b|\bsalwar\b|\bchuridar set\b|\bsuit set\b|\bethnic set\b|\bkurta pajama\b|\bkurta palazzo\b|\bkurti palazzo\b", "kurta_set", "full", True),
    # Bottoms BEFORE kurta/shirt so "Kurti Pants" / "shirt with trousers" classify correctly
    (r"\bjeans?\b|\bdenim pants?\b", "jeans", "bottom", False),
    (r"\btrousers?\b|\bpants?\b|\bchinos?\b|\bpalazzos?\b|\bcigarette pants?\b|\bkurti pants?\b|\bkurta pants?\b|\bkurti pant\b|\bkurta pant\b", "trousers", "bottom", False),
    (r"\bjoggers?\b|\btrack ?pants?\b|\bsweatpants?\b|\bleggings?\b|\btights?\b|\bjeggings?\b", "joggers", "bottom", False),
    (r"\bshorts?\b|\bcapri\b", "shorts", "bottom", False),
    (r"\bskirts?\b", "skirt", "bottom", False),
    (r"\bsherwanis?\b", "sherwani", "full", True),
    (r"\bdhoti\b|\bveshti\b|\bmundu\b|\bpanche\b", "dhoti", "bottom", True),
    (r"\banarkali\b", "anarkali", "full", True),
    (r"\bsharara\b|\bgharara\b", "sharara", "full", True),
    (r"\bkurta\b|\bkurti\b", "kurta", "top", True),
    (r"\bsarees?\b|\bsari\b", "saree", "full", True),
    (r"\blehengas?\b|\bghagra\b|\bchaniya choli\b", "lehenga", "full", True),
    (r"\bdress(es)?\b|\bfrock\b|\bgown\b|\bmaxi\b|\bmidi\b|\bjumpsuit\b|\bromper\b|\bplaysuit\b", "dress", "full", True),
    (r"\bco[- ]?ord\b|\bcoord set\b", "co-ord", "full", True),
    (r"\bblazers?\b|\bnehru jacket\b|\bbandhgala\b|\bwaistcoat\b|\bjackets?\b|\bcoats?\b|\bshrugs?\b|\bcardigans?\b|\bhoodies?\b|\bsweatshirts?\b|\bsweaters?\b|\bpullovers?\b", "outerwear", "top", True),
    # Generic shirt/top AFTER bottoms so multi-garment titles keep the bottom slot
    (r"\bshirts?\b|\bblouses?\b|\btops?\b|\btunics?\b", "top", "top", True),
    (r"\bheels?\b|\bpumps?\b|\bstilettos?\b|\bwedges?\b", "heels", "shoes", False),
    (r"\bsandals?\b|\bflats?\b|\bballerinas?\b|\bslides?\b|\bflip[- ]?flops?\b", "sandals", "shoes", False),
    (r"\bsneakers?\b|\bloafers?\b|\boxfords?\b|\bshoes?\b|\brunning shoes?\b", "shoes", "shoes", False),
    (r"\bbags?\b|\bhandbags?\b|\bclutch(es)?\b|\bpurse\b|\bbackpack\b|\btote\b", "bag", "accessory", False),
    (r"\bwatch(es)?\b", "watch", "accessory", False),
    (r"\bsunglass(es)?\b|\bshades\b|\beyewear\b", "sunglasses", "accessory", False),
    (r"\bbelts?\b", "belt", "accessory", False),
    (r"\bjewel(le)?ry\b|\bnecklaces?\b|\bearrings?\b|\bbangles?\b|\bbracelets?\b", "jewellery", "accessory", False),
]

_STYLE_LEXICON: dict[str, dict[str, int]] = {
    "minimalist": {"plain": 3, "solid": 3, "minimal": 3, "essential": 2, "basic": 2,
                   "clean": 2, "monochrome": 2},
    "streetwear": {"oversized": 3, "graphic": 3, "hoodie": 3, "cargo": 2, "drop shoulder": 3,
                   "baggy": 2, "joggers": 1, "sneaker": 1},
    "classic": {"polo": 2, "chinos": 2, "tailored": 2, "button down": 2, "striped": 1, "loafer": 1},
    "boho": {"floral": 3, "embroidered": 2, "flowy": 2, "maxi": 2, "crochet": 3, "fringe": 2, "gypsy": 3},
    "athleisure": {"sports": 3, "track": 3, "running": 2, "dry fit": 3, "active": 2,
                   "gym": 3, "leggings": 2},
    "glam": {"sequin": 3, "shimmer": 3, "satin": 2, "metallic": 2, "bodycon": 2,
             "glitter": 3, "party": 1},
    "y2k": {"crop": 2, "low rise": 3, "butterfly": 3, "baby tee": 3, "velour": 2},
    "cottagecore": {"floral": 3, "puff sleeve": 3, "prairie": 3, "lace": 2, "pastel": 2, "linen": 1},
    "corporate": {"formal": 3, "blazer": 3, "trousers": 2, "button down": 2, "office": 2, "tailored": 2},
    "resort": {"linen": 3, "kaftan": 3, "tropical": 2, "breezy": 2, "beach": 2},
    "ethnic": {"kurta": 3, "saree": 3, "lehenga": 3, "ethnic": 3, "zari": 2},
    "old money": {"polo": 2, "chinos": 2, "cashmere": 3, "tailored": 2, "classic": 2,
                  "loafer": 2, "cream": 1},
    "indo-western": {"indo": 3, "fusion": 3, "cape": 2},
}

_OCCASION_LEXICON: dict[str, dict[str, int]] = {
    "casual": {"t-shirt": 2, "tshirt": 2, "jeans": 2, "top": 1, "shirt": 1, "sneaker": 1, "cotton": 1},
    "work": {"formal": 3, "blazer": 2, "trousers": 2, "shirt": 2, "button down": 2, "loafer": 1, "pencil": 1},
    "office": {"formal": 3, "blazer": 2, "trousers": 2, "shirt": 2, "button down": 2, "loafer": 1},
    "date": {"dress": 2, "shirt": 1, "satin": 1, "heels": 2, "midi": 2, "stylish": 1},
    "party": {"party": 2, "sequin": 3, "dress": 2, "heels": 2, "bodycon": 2, "satin": 2,
              "shimmer": 2, "blazer": 1},
    "wedding": {"kurta": 3, "saree": 3, "lehenga": 3, "sherwani": 3, "ethnic": 2,
                "embroidered": 2, "zari": 2, "anarkali": 2},
    "festive": {"kurta": 3, "saree": 3, "lehenga": 3, "ethnic": 2, "embroidered": 2,
                "zari": 2, "anarkali": 2, "silk": 1},
    "travel": {"joggers": 2, "sneaker": 2, "cotton": 1, "lightweight": 2, "cargo": 2, "t-shirt": 1},
    "gym": {"sports": 3, "gym": 3, "leggings": 2, "track": 2, "running": 2, "dry fit": 3,
            "active": 2, "tights": 2},
    "beach": {"linen": 3, "shorts": 2, "kaftan": 3, "swim": 3, "tropical": 2},
    "brunch": {"top": 1, "dress": 1, "linen": 1, "sneaker": 1, "shirt": 1},
    "college": {"t-shirt": 2, "tshirt": 2, "jeans": 2, "sneaker": 1, "oversized": 1},
    "interview": {"formal": 3, "blazer": 3, "trousers": 2, "shirt": 2, "button down": 2},
    "traditional": {"kurta": 3, "saree": 3, "ethnic": 3, "traditional": 3, "temple": 2, "cotton": 1},
}

_STYLE_CONFLICTS: dict[str, list[str]] = {
    "minimalist": ["print", "floral", "graphic", "embroider", "sequin", "tie dye",
                   "abstract", "animal", "applique", "colorblock"],
    "corporate": ["print", "floral", "graphic", "sequin", "oversized", "cargo"],
    "old money": ["print", "graphic", "sequin", "tie dye"],
    "classic": ["sequin", "graphic", "butterfly"],
}

_STYLE_PALETTES: dict[str, set[str]] = {
    "minimalist": {"white", "black", "grey", "cream", "navy"},
    "classic": {"navy", "white", "cream", "brown", "grey"},
    "corporate": {"navy", "white", "grey", "black"},
    "glam": {"black", "red", "pink", "purple"},
    "boho": {"cream", "brown", "green", "pink", "orange"},
    "y2k": {"pink", "purple", "blue"},
    "streetwear": {"black", "grey", "white", "green"},
    "athleisure": {"black", "grey", "blue"},
    "cottagecore": {"cream", "pink", "green", "white"},
    "resort": {"cream", "white", "blue", "green"},
    "old money": {"cream", "navy", "white", "brown", "green"},
}

_OCCASION_PALETTES: dict[str, set[str]] = {
    "wedding": {"red", "pink", "gold", "yellow", "purple", "cream", "orange"},
    "festive": {"red", "pink", "gold", "yellow", "purple", "cream", "orange"},
    "party": {"black", "red", "pink", "blue", "green", "purple"},
    "gym": {"black", "grey", "blue", "pink"},
    "travel": {"blue", "green", "grey", "cream", "black"},
    "beach": {"white", "cream", "blue", "green"},
    "work": {"navy", "white", "grey", "black", "cream"},
    "office": {"navy", "white", "grey", "black", "cream"},
}

_STYLE_QUERY_MODIFIERS: dict[str, str] = {
    "minimalist": "minimal solid",
    "streetwear": "oversized graphic",
    "classic": "classic fit",
    "boho": "boho floral",
    "athleisure": "dry fit",
    "glam": "sequin",
    "y2k": "y2k",
    "cottagecore": "cottagecore floral",
    "corporate": "office formal",
    "resort": "linen resort",
    "ethnic": "ethnic",
    "old money": "classic",
    "indo-western": "indo western",
}

_FABRIC_HINT_OCCASIONS = {"casual", "travel", "beach", "brunch", "college", "date"}

_WEATHER_FABRICS: dict[str, list[str]] = {
    "hot": ["linen", "cotton"],
    "humid": ["linen", "cotton"],
    "mild": ["cotton"],
    "cold": ["wool", "knit"],
    "rainy": ["quick dry"],
}

_AVOID_FABRICS: dict[str, list[str]] = {
    "hot": ["wool", "velvet", "leather", "fleece"],
    "humid": ["wool", "velvet", "leather", "polyester"],
    "cold": ["chiffon"],
}

_BODY_FIT_FAMILY: dict[str, str] = {
    "athletic": "slim",
    "hourglass": "slim",
    "rectangle": "regular",
    "triangle": "regular",
    "inverted_triangle": "regular",
    "oval": "relaxed",
}

_BAND_BY_SEGMENT: dict[str, tuple[float, float]] = {
    "value": (0, 1500),
    "budget": (0, 1500),
    "premium": (1500, 3500),
    "mid": (1500, 3500),
    "luxury": (3500, 10**9),
}
_BAND_BY_RANGE: dict[str, tuple[float, float]] = {
    "under_1500": (0, 1500),
    "1500_3500": (1500, 3500),
    "above_3500": (3500, 10**9),
}

_MALE_MARKERS = [" men ", " mens ", "men's", " male ", " boys ", " boy ", " gents ", " gent "]
_FEMALE_MARKERS = [" women ", " womens ", "women's", " woman ", " female ", " girls ", " girl ",
                   " ladies ", " lady "]
_KIDS_MARKERS = [" kid ", " kids", " kid's", " boys ", " boy ", " girls ", " girl ", " baby ",
                 " toddler ", " infant ", " child "]

_KNOWN_BRANDS = sorted({
    "allen solly", "van heusen", "louis philippe", "marks & spencer", "house of masaba",
    "the north face", "the souled store", "g-star raw", "kook n keech", "max fashion",
    "jack & jones", "united colors of benetton", "tommy hilfiger", "calvin klein",
    "ralph lauren", "peter england", "brooks brothers", "hugo boss", "massimo dutti",
    "ritu kumar", "anita dongre", "kalki fashion", "bombay trooper", "campus sutra",
    "clovia botaniq", "royal enfield", "under armour", "forever new", "house of cb",
    "asos design", "twenty dresses", "global desi", "w for woman",
    "levis", "levi's", "zara", "h&m", "uniqlo", "superdry", "diesel", "guess",
    "allsaints", "netplay", "zudio", "excalibur", "arrow", "fablestreet", "raymond",
    "berrylush", "sassafras", "athena", "tokyo talkies", "faballey", "kazo", "raream",
    "mango", "vero moda", "rsvp", "revolve", "bebe", "anouk", "libas", "sangria",
    "vishudh", "aurelia", "soch", "biba", "fabindia", "indya", "koskii", "manyavar",
    "nalli", "quechua", "forclaz", "wildcraft", "trekman", "fuaark", "gokyo",
    "columbia", "wrogn", "woodland", "quiksilver", "patagonia", "vuori", "salomon",
    "hrx", "domyos", "symactive", "puma", "reebok", "skechers", "blissclub",
    "cultsport", "adidas", "nike", "lululemon", "asics", "gymshark", "alo yoga",
    "nykaa", "meesho", "snapdeal", "bewakoof", "snitch", "roadster", "souled store",
    "metro", "catwalk", "inc.5", "inc 5", "mochi", "bata", "clarks", "woodland shoes",
    "sabyasachi", "manish malhotra", "tarun tahiliani", "abhinav mishra", "suta",
    "amit aggarwal", "aachho", "jaipur kurta", "shree", "rangriti", "zola", "tatacliq",
    "gini & jony", "max", "life", "pantaloons", "westside", "lifestyle", "shoppers stop",
}, key=len, reverse=True)


_TITLE_STOPWORDS = {"women", "womens", "women's", "men", "mens", "men's", "the", "new", "buy",
                    "pack", "combo", "unisex", "girls", "girl", "boys", "boy", "ladies",
                    "premium", "stylish", "trendy", "latest", "fashion", "branded", "pure",
                    "cotton", "solid", "printed", "casual", "formal", "party", "regular",
                    "slim", "fit", "plus", "size", "free", "set", "pcs", "piece", "combo"}


def extract_brand(title: str) -> str:
    """Best-effort brand extraction from a product title."""
    tl = f" {(title or '').lower()} "
    for b in _KNOWN_BRANDS:
        if f" {b} " in tl or tl.strip().startswith(b):
            return b
    words = re.findall(r"[A-Za-z][A-Za-z'&.-]{2,}", title or "")
    for w in words[:4]:
        wl = w.lower()
        if wl not in _TITLE_STOPWORDS:
            return wl
    return ""


# ══════════════════════════════════════════════════════════════
# INTENT
# ══════════════════════════════════════════════════════════════

def build_intent(
    occasion: str = "",
    style: str = "",
    gender: str = "",
    price_segment: str = "",
    price_range: str = "",
    weather: dict | None = None,
    city: str = "",
    brands: list[str] | None = None,
    budget_level: str = "",
    budget_min: float | None = None,
    budget_max: float | None = None,
    budget_point: float | None = None,
    budget_tolerance_pct: float = 20.0,
    user_prefs: dict | None = None,
) -> dict:
    """Normalise a user request into a structured fashion intent.

    Budget modes (priority order):
      range  → budget_min/budget_max (slider)
      around → budget_point ± tolerance (intuitive point budget)
      level  → value / contemporary / premium / luxury (resolved against the
               live price distribution at recommendation time)
      legacy → old price_segment / price_range slabs (backward compatible)
    user_prefs: soft Fashion DNA preferences (wear_weights, vibes, brands,
    adventure_level). Never overrides hard occasion/dress_code gates.
    """
    occasion = (occasion or "casual").strip().lower()
    style = (style or "").strip().lower()
    gender = (gender or "").strip().lower()
    if gender in ("woman", "girl", "f"):
        gender = "female"
    elif gender in ("man", "boy", "m"):
        gender = "male"

    # Canonical occasion + dress code from the rules engine
    occasion_canonical = frules.canonicalize_occasion(occasion)
    sub_event = frules.resolve_sub_event(occasion)
    dress_code = frules.resolve_dress_code(occasion_canonical, style)
    style_terms = frules.style_query_terms(style)

    # Soft style bias from prefs (does not replace explicit request style)
    prefs = user_prefs or {}
    wear_weights = prefs.get("wear_weights") or {}
    vibes = prefs.get("vibes") or []
    # Strong single-culture preference can seed style terms when none given
    if not style_terms and wear_weights:
        top_wear = max(wear_weights, key=lambda k: wear_weights.get(k) or 0) if wear_weights else ""
        top_val = float(wear_weights.get(top_wear) or 0)
        if top_val >= 0.75:
            if top_wear == "western" and occasion_canonical in ("work", "interview", "date", "casual"):
                style_terms = frules.style_query_terms("corporate") or ["tailored"]
            elif top_wear == "indian" and occasion_canonical in ("wedding", "festive", "traditional"):
                style_terms = frules.style_query_terms("ethnic") or ["ethnic"]
            elif top_wear == "indo_western":
                style_terms = frules.style_query_terms("indo-western") or ["fusion"]

    level = (budget_level or "").strip().lower()
    if level in ("budget", "cheap", "affordable"):
        level = "value"
    elif level in ("mid", "midrange", "mid-range"):
        level = "contemporary"

    band: tuple[float, float] | None = None
    budget_mode = "none"

    if budget_min is not None or budget_max is not None:
        lo = float(budget_min) if budget_min is not None else 0.0
        hi = float(budget_max) if budget_max is not None else 10**9
        if hi > lo:
            band = (lo, hi)
            budget_mode = "range"
    elif budget_point is not None and budget_point > 0:
        tol = max(5.0, min(float(budget_tolerance_pct or 20.0), 60.0)) / 100.0
        band = (max(0.0, budget_point * (1 - tol)), budget_point * (1 + tol))
        budget_mode = "around"
    elif level in LEVEL_WINDOWS:
        budget_mode = "level"
    else:
        legacy = _BAND_BY_RANGE.get((price_range or "").strip().lower())
        if legacy is None:
            legacy = _BAND_BY_SEGMENT.get((price_segment or "").strip().lower())
        if legacy is not None:
            band = legacy
            budget_mode = "legacy"
            level = _segment_to_level((price_segment or price_range or "").strip().lower())

    weather = weather or {}
    condition = str(weather.get("condition", "")).lower()
    temp = weather.get("temp")
    if not condition and isinstance(temp, (int, float)):
        condition = "hot" if temp >= 28 else "cold" if temp <= 15 else "mild"

    palette: set[str] = set(_STYLE_PALETTES.get(style, set()))
    palette |= _OCCASION_PALETTES.get(occasion_canonical, set()) or \
        set(frules.occasion_config(occasion_canonical).get("palette", []))

    # Vibe → style colour lexicon soft merge (never drops occasion palette)
    for vibe in vibes:
        v = str(vibe).strip().lower()
        if v in _STYLE_PALETTES:
            palette |= set(_STYLE_PALETTES[v])

    # Merge preference brands into request brands (union)
    pref_brands = [str(b).strip().lower() for b in (prefs.get("brands") or []) if b]
    merged_brands = list(dict.fromkeys(
        [b.strip().lower() for b in (brands or []) if b and b.strip()] + pref_brands
    ))

    intent = {
        "occasion": occasion_canonical,
        "occasion_raw": occasion,
        "sub_event": sub_event,
        "dress_code": dress_code,
        "style_query_terms": style_terms,
        "style": style,
        "gender": gender,
        "band": band,
        "band_explicit": band is not None or budget_mode == "level",
        "strict_band": budget_mode in ("range", "around", "legacy"),
        "budget_mode": budget_mode,
        "budget_level": level,
        "segment": (price_segment or "").strip().lower(),
        "weather": condition,
        "fabric_hints": _WEATHER_FABRICS.get(condition, []),
        "avoid_fabrics": _AVOID_FABRICS.get(condition, []),
        "palette": palette,
        "city": city,
        "brands": merged_brands,
        "body_shape": "",
        # Fashion DNA soft prefs (hard gates ignore these)
        "user_prefs": prefs,
        "wear_weights": wear_weights,
        "vibes": list(vibes),
        "adventure_level": (prefs.get("adventure_level") or "balanced"),
    }
    return intent


def _segment_to_level(segment: str) -> str:
    return {
        "value": "value", "budget": "value", "under_1500": "value",
        "mid": "contemporary", "premium": "contemporary", "1500_3500": "contemporary",
        "luxury": "luxury", "above_3500": "luxury",
    }.get(segment, "contemporary")


def _slot_priority(intent: dict, count: int) -> list[str]:
    """Which slots matter for this request, in priority order."""
    slots = ["primary", "bottom"]
    if count >= 5:
        slots.append("shoes")
    if count >= 9:
        slots.append("accessory")
    return slots


# ══════════════════════════════════════════════════════════════
# SLOT-BASED QUERY GENERATION
# ══════════════════════════════════════════════════════════════

_OCCASION_SLOTS: dict[str, dict[str, dict[str, list[str]]]] = {
    "female": {
        "casual": {"primary": ["women casual top", "women oversized t-shirt"],
                   "bottom": ["women jeans"], "shoes": ["women white sneakers"]},
        "work": {"primary": ["women formal shirt", "women blouse"],
                 "bottom": ["women formal trousers"], "shoes": ["women loafers"]},
        "office": {"primary": ["women formal shirt", "women blouse"],
                   "bottom": ["women formal trousers"], "shoes": ["women loafers"]},
        "date": {"primary": ["women midi dress", "women stylish top"],
                 "bottom": [], "shoes": ["women heels"]},
        "party": {"primary": ["women party dress", "women sequin top"],
                  "bottom": [], "shoes": ["women heels"]},
        "wedding": {"primary": ["women kurta set", "women saree"],
                    "bottom": [], "shoes": ["women ethnic heels"]},
        "festive": {"primary": ["women kurta set", "women ethnic top"],
                    "bottom": [], "shoes": []},
        "travel": {"primary": ["women cotton top", "women oversized t-shirt"],
                   "bottom": ["women joggers"], "shoes": ["women sneakers"]},
        "gym": {"primary": ["women sports top", "women gym t-shirt"],
                "bottom": ["women gym leggings"], "shoes": ["women running shoes"]},
        "beach": {"primary": ["women beach dress", "women linen top"],
                  "bottom": ["women beach shorts"], "shoes": ["women sandals"]},
        "brunch": {"primary": ["women casual dress", "women linen top"],
                   "bottom": [], "shoes": ["women sandals"]},
        "college": {"primary": ["women casual top", "women t-shirt"],
                    "bottom": ["women jeans"], "shoes": ["women sneakers"]},
    },
    "male": {
        "casual": {"primary": ["men casual shirt", "men oversized t-shirt"],
                   "bottom": ["men jeans"], "shoes": ["men white sneakers"]},
        "work": {"primary": ["men formal shirt", "men blazer"],
                 "bottom": ["men formal trousers"], "shoes": ["men formal shoes"]},
        "office": {"primary": ["men formal shirt", "men blazer"],
                   "bottom": ["men formal trousers"], "shoes": ["men formal shoes"]},
        "date": {"primary": ["men stylish shirt", "men polo t-shirt"],
                 "bottom": ["men chinos"], "shoes": ["men loafers"]},
        "party": {"primary": ["men party shirt", "men satin shirt"],
                  "bottom": ["men party trousers"], "shoes": ["men loafers"]},
        "wedding": {"primary": ["men kurta", "men nehru jacket"],
                    "bottom": [], "shoes": ["men ethnic shoes"]},
        "festive": {"primary": ["men kurta", "men ethnic shirt"],
                    "bottom": [], "shoes": []},
        "travel": {"primary": ["men cotton t-shirt", "men casual shirt"],
                   "bottom": ["men cargo pants"], "shoes": ["men sneakers"]},
        "gym": {"primary": ["men gym t-shirt", "men sports t-shirt"],
                "bottom": ["men track pants"], "shoes": ["men running shoes"]},
        "beach": {"primary": ["men beach shirt", "men linen shirt"],
                  "bottom": ["men beach shorts"], "shoes": ["men flip flops"]},
        "brunch": {"primary": ["men casual shirt", "men polo t-shirt"],
                   "bottom": ["men chinos"], "shoes": ["men loafers"]},
        "college": {"primary": ["men oversized t-shirt", "men casual shirt"],
                    "bottom": ["men jeans"], "shoes": ["men sneakers"]},
    },
}

_NEUTRAL_SLOTS: dict[str, dict[str, list[str]]] = {
    "casual": {"primary": ["casual shirt", "t-shirt"], "bottom": ["jeans"], "shoes": ["sneakers"]},
    "work": {"primary": ["formal shirt"], "bottom": ["formal trousers"], "shoes": ["formal shoes"]},
    "party": {"primary": ["party dress", "party shirt"], "bottom": [], "shoes": ["heels"]},
    "wedding": {"primary": ["kurta set", "ethnic wear"], "bottom": [], "shoes": []},
    "travel": {"primary": ["cotton t-shirt"], "bottom": ["joggers"], "shoes": ["sneakers"]},
}


# Query variation synonyms — randomly pick one per base query to diversify results
_QUERY_VARIATIONS: dict[str, list[str]] = {
    # Men office
    "men formal shirt": ["men formal shirt", "men formal shirt slim fit", "men cotton formal shirt", "men wrinkle-free shirt", "men office shirt"],
    "men blazer": ["men blazer", "men slim blazer", "men single breasted blazer", "men formal blazer", "men office blazer"],
    "men formal trousers": ["men formal trousers", "men slim fit trousers", "men flat front trousers", "men office trousers", "men dress pants"],
    "men formal shoes": ["men formal shoes", "men oxford shoes", "men derby shoes", "men leather shoes", "men lace-up shoes"],
    # Men casual
    "men casual shirt": ["men casual shirt", "men linen shirt", "men half-sleeve shirt", "men printed shirt", "men relaxed fit shirt"],
    "men oversized t-shirt": ["men oversized t-shirt", "men drop shoulder t-shirt", "men boxy fit t-shirt", "men graphic tee"],
    "men jeans": ["men jeans", "men slim fit jeans", "men straight jeans", "men tapered jeans", "men stretch jeans"],
    "men white sneakers": ["men white sneakers", "men court shoes", "men canvas sneakers", "men low top sneakers"],
    # Men party
    "men party shirt": ["men party shirt", "men printed shirt", "men satin shirt", "men statement shirt"],
    "men satin shirt": ["men satin shirt", "men silk shirt", "men shiny shirt"],
    "men party trousers": ["men party trousers", "men slim trousers", "men tapered trousers"],
    # Women office
    "women formal shirt": ["women formal shirt", "women crisp shirt", "women cotton shirt", "women office shirt", "women button-down shirt"],
    "women blouse": ["women blouse", "women formal blouse", "women office blouse", "women tailored blouse"],
    "women formal trousers": ["women formal trousers", "women slim trousers", "women office trousers", "women tailored trousers", "women pencil trousers"],
    "women loafers": ["women loafers", "women formal flats", "women pointed flats", "women leather loafers"],
    # Women casual
    "women casual top": ["women casual top", "women relaxed top", "women everyday top", "women cotton top"],
    "women oversized t-shirt": ["women oversized t-shirt", "women drop shoulder tee", "women boxy t-shirt"],
    "women jeans": ["women jeans", "women skinny jeans", "women straight jeans", "women mom jeans"],
    # Women party
    "women party dress": ["women party dress", "women cocktail dress", "women mini dress", "women sequin dress"],
    "women sequin top": ["women sequin top", "women sparkly top", "women glam top"],
}

import random as _random

def _office_formalize(queries: list[tuple[str, str]], intent: dict) -> list[tuple[str, str]]:
    """Office draws must ASK for formal-titled results on every wear mode.

    Blueprint queries like "women blue straight kurta cotton office wear"
    return titles with no formal signal ("office wear" rarely survives into
    the product title), which the office gate then rejects → starved pools.
    Gate wording is unchanged; only the search query is steered. (RECO-v4.3)
    """
    if not _office_formal_required(intent):
        return queries
    out: list[tuple[str, str]] = []
    for slot, q in queries:
        if "formal" not in q.lower():
            q = f"formal {q}"
        out.append((slot, q))
    return out


def build_slot_queries(intent: dict, count: int = 6, max_queries: int = 6) -> list[tuple[str, str]]:
    """Generate slot-labelled Google Shopping queries via the outfit blueprint compiler.

    Flow: intent → outfit archetypes → concrete blueprints → per-slot queries
    with occasion-aware exclusions. Falls back to legacy static slots if the
    rules engine returns nothing.
    """
    try:
        blueprints = frules.build_outfit_blueprints(intent, n=3)
        # Preserve any `why` already attached by marketplace_reco
        prior_why = {
            b.get("id"): b.get("why", "")
            for b in (intent.get("blueprints") or [])
            if isinstance(b, dict)
        }
        intent["blueprints"] = [
            {"id": b["id"], "label": b["label"], "variant": b["variant"],
             "archetype": b["archetype"], "dress_code": b["dress_code"],
             "why": prior_why.get(b["id"], frules.explain_blueprint(b, intent))}
            for b in blueprints
        ]
        compiled = frules.compile_blueprint_queries(blueprints, intent, max_queries=max_queries)
        if compiled:
            return _office_formalize([(c["slot"], c["query"]) for c in compiled], intent)
    except Exception as exc:
        logger.warning(f"Blueprint query compile failed, falling back to static slots: {exc}")

    # ── Legacy static fallback (pre-rules-engine path) ──
    gender = intent.get("gender", "")
    occasion = intent.get("occasion", "casual")
    style = intent.get("style", "")

    slot_map = _OCCASION_SLOTS.get(gender, {}).get(occasion)
    if slot_map is None:
        slot_map = _NEUTRAL_SLOTS.get(occasion) or _NEUTRAL_SLOTS["casual"]

    style_mod = _STYLE_QUERY_MODIFIERS.get(style, "")
    fabric_hint = (intent.get("fabric_hints") or [""])[0]
    if occasion not in _FABRIC_HINT_OCCASIONS:
        fabric_hint = ""

    queries: list[tuple[str, str]] = []
    seen = set()

    for slot in _slot_priority(intent, count):
        candidates = list(slot_map.get(slot, []))
        for i, base in enumerate(candidates):
            q = base
            if base in _QUERY_VARIATIONS:
                q = _random.choice(_QUERY_VARIATIONS[base])
            if style_mod and i == 0:
                mod_words = style_mod.split()
                if not all(w in q for w in mod_words):
                    q = f"{mod_words[0]} {q}"
            if i == 0 and fabric_hint and slot == "primary" and fabric_hint not in q:
                q = f"{q} {fabric_hint}"
            q = re.sub(r"\s+", " ", q).strip()
            if q.lower() not in seen:
                seen.add(q.lower())
                queries.append((slot, q))
        if len(queries) >= max_queries:
            break

    return _office_formalize(queries[:max_queries], intent)


# ══════════════════════════════════════════════════════════════
# ATTRIBUTE ENRICHMENT
# ══════════════════════════════════════════════════════════════

def _match_vocab(text: str, vocab: list[tuple[str, list[str]]]) -> list[str]:
    hits = []
    for name, keys in vocab:
        for k in keys:
            if re.search(rf"\b{re.escape(k)}\b", text):
                hits.append(name)
                break
    return hits


def _detect_category(title: str) -> tuple[str, str, bool]:
    t = f" {(title or '').lower()} "
    for pattern, category, slot, vton in _CATEGORY_RULES:
        if re.search(pattern, t):
            return category, slot, vton
    return "product", "top", True


def enrich_product(product: dict) -> dict:
    """Attach a structured fashion attribute block to a raw product."""
    title = (product.get("title") or "").lower()
    haystack = f" {title} "

    category, slot, vton = _detect_category(title)
    slot_hint = product.get("query_slot")
    if category == "product" and slot_hint:
        slot = "top" if slot_hint == "primary" else slot_hint
    elif slot_hint == "bottom" and category in ("product", "top") and not re.search(
        r"\b(shirts?|blouses?|t[- ]?tees?|polos?|tunics?)\b", haystack
    ):
        # Bottom-query hit with weak/generic top label → keep bottom slot
        # (strong top nouns still win; "Kurti Pants" already classifies as bottom)
        if category == "product":
            slot = "bottom"
    elif slot_hint == "shoes" and category == "product":
        slot = "shoes"

    colors = _match_vocab(haystack, _COLOR_FAMILIES)
    fabrics = _match_vocab(haystack, _FABRICS)
    fits = _match_vocab(haystack, _FITS)
    patterns = _match_vocab(haystack, _PATTERNS)
    embellishment = frules.detect_embellishment(title, patterns)

    product["fashion"] = {
        "category": category,
        "slot": slot,
        "vton_friendly": vton,
        "color": colors[0] if colors else "",
        "colors": colors,
        "fabrics": fabrics,
        "fit": fits[0] if fits else "",
        "fits": fits,
        "patterns": patterns,
        "embellishment": embellishment,
    }
    product["brand"] = extract_brand(product.get("title", ""))
    return product


# ══════════════════════════════════════════════════════════════
# HARD FILTERS
# ══════════════════════════════════════════════════════════════

def gender_ok(title: str, gender: str) -> bool:
    if not gender:
        return True
    t = f" {title.lower()} "
    if any(k in t for k in _KIDS_MARKERS):
        return False
    male = any(m in t for m in _MALE_MARKERS)
    female = any(m in t for m in _FEMALE_MARKERS)
    if gender == "female":
        return not male or female
    if gender == "male":
        return not female or male
    return True


def _gender_ok(title: str, gender: str) -> bool:
    return gender_ok(title, gender)


def _price_in_band(price: float, band: tuple[float, float] | None, *, enforce_floor: bool = True) -> bool:
    if not band:
        return True
    lo, hi = band
    if price > hi:
        return False
    # Floor is soft for user budget ranges (RECO-v4.3): the wizard derives a
    # per-piece floor from the total look budget (₹4000 → ₹929) but Indian
    # marketplace formalwear mostly sells at ₹300–900 — a hard floor starved
    # every office draw. Budget scoring still prefers in-band items. Level
    # mode keeps the floor: there the band IS a quality window, not a budget.
    return price >= lo if enforce_floor else True


def _office_western_strict(intent: dict) -> bool:
    """Office/interview + strong western preference → formal western only."""
    occ = intent.get("occasion", "")
    if occ not in ("work", "interview", "office"):
        return False
    wear = intent.get("wear_weights") or (intent.get("user_prefs") or {}).get("wear_weights") or {}
    w = float(wear.get("western") or 0)
    i = float(wear.get("indian") or 0)
    return w >= 0.75 and w > i


def _office_formal_required(intent: dict) -> bool:
    """Any work/interview/office occasion demands formal-signal titles.

    Applies even without wear chips — "Young root fashion" (no garment
    nouns, no formal cues) must not pass as office inventory. Ethnic
    rejection stays gated on _office_western_strict (Indian office wear
    is still valid when the user didn't ask for western).
    """
    return intent.get("occasion", "") in ("work", "interview", "office")


# Casual body garments that must never appear in a western office look
_OFFICE_CASUAL_REJECT = (
    "t-shirt", "tshirt", "tank top", "camisole", "hoodie", "sweatshirt",
    "jogger", "track pant", "shorts", "sneaker", "flip flop", "slider",
    "ripped", "crop top", "pyjama", "loungewear", "nightwear", "denim jacket",
    "graphic tee", "oversized",
)
# Ethnic pieces excluded when user explicitly wants western office
_OFFICE_ETHNIC_REJECT = (
    "kurta", "kurti", "saree", "sari", "lehenga", "salwar", "anarkali",
    "sharara", "gharara", "dhoti", "sherwani", "churidar", "dupatta",
    "indo-western", "indo western", "juttis", "mojari", "ethnic",
)
# Body garments must show at least one formal/office signal (user: "only formal")
# NOTE: casual rejects (t-shirt/hoodie/...) run FIRST, so bare "shirt" is safe here.
# Contract (golden f02/f07): bare "kurta" is NOT a signal — ethnic tops pass only
# with a real formal marker ("Formal Silk Kurta"); set pieces ("...with Dupatta")
# stay rejected for office.
_OFFICE_FORMAL_SIGNALS = (
    "formal", "office", "business", "tailored", "blazer", "suit",
    "trouser", "chino", "pencil", "button-down", "button down", "oxford",
    "structured", "corporate", "work wear", "workwear", "crisp", "shirting",
    "cigarette", "wide leg", "straight leg", "dress shirt", "waistcoat",
    "shirt", "blouse", "loafer", "heel", "flat shoe", "sandal",
)


def hard_filter(
    products: list[dict],
    intent: dict,
    band: tuple[float, float] | None,
    *,
    require_formal_signal: bool = True,
) -> list[dict]:
    occasion = intent.get("occasion", "casual")
    strict_western = _office_western_strict(intent)
    office_formal = _office_formal_required(intent)
    # User-budget bands (range/around/legacy): ceiling hard, floor soft.
    # Level bands keep both — the band is the level's quality window.
    keep_floor = intent.get("budget_mode") == "level"
    out = []
    for p in products:
        price = p.get("price") or 0
        if price <= 0:
            continue
        if not _price_in_band(price, band, enforce_floor=keep_floor):
            continue
        if not _gender_ok(p.get("title", ""), intent.get("gender", "")):
            continue
        # Hard gate: product must belong to the requested occasion
        if not frules.occasion_ok(p, occasion):
            continue
        if strict_western or office_formal:
            t = f" {(p.get('title') or '').lower()} "
            slot = (p.get("fashion") or {}).get("slot", "")
            if slot in ("shoes", "accessory"):
                # Footwear: formal-adjacent only (no beach/ethnic/casual sport)
                if any(k in t for k in ("flip flop", "slider", "jutti", "mojari", "kolhapuri", "sport", "running", "trainer", "beach", "floaters")):
                    continue
            else:
                if any(k in t for k in _OFFICE_CASUAL_REJECT):
                    continue
                if strict_western:
                    # "Kurta placket"/"kurta neck" is a shirt collar detail — not ethnic wear
                    t_ethnic = t.replace("kurta placket", " ").replace("kurta neckline", " ").replace("kurti neckline", " ")
                    if any(k in t_ethnic for k in _OFFICE_ETHNIC_REJECT):
                        continue
                # Must look formal for office (reject brand-only/noise titles too)
                if office_formal and require_formal_signal and not any(sig in t for sig in _OFFICE_FORMAL_SIGNALS):
                    continue
            if any(k in t for k in ("streetwear", "clubwear", "rave", "festival")):
                continue
        out.append(p)
    return out


def _widen(band: tuple[float, float] | None) -> tuple[float, float] | None:
    if not band:
        return None
    lo, hi = band
    if hi >= 10**9:
        return (max(0, lo * 0.7), hi)
    return (max(0, lo * 0.7), hi * 1.4)


def _rescue_slot_coverage(pool: list[dict], relevant: list[dict], intent: dict) -> list[dict]:
    """Pull bottoms/shoes from the gender-filtered pool even if outside the budget band.

    A tight band (e.g. ₹929–2071 from dnaBudget) often eliminates footwear and
    trousers while leaving cheap tops — which is how office looks became shirts-only.
    Band is the ONLY gate this bypasses: style/casual/ethnic/formal/occasion gates
    still run per item (a "Sport Sandal" must not sneak into an office look).
    """
    have = {(p.get("fashion") or {}).get("slot", "") for p in pool}
    missing = {s for s in ("bottom", "shoes") if s not in have}
    if not missing:
        return pool

    in_pool = {id(p) for p in pool}
    added_counts: dict[str, int] = {}
    extras: list[dict] = []
    for p in relevant:
        if not missing:
            break
        if id(p) in in_pool:
            continue
        slot = (p.get("fashion") or {}).get("slot", "")
        if slot not in missing:
            continue
        # Band bypass is the point of rescue; every other gate still applies.
        if not hard_filter([p], intent, None, require_formal_signal=True):
            continue
        extras.append(p)
        in_pool.add(id(p))
        added_counts[slot] = added_counts.get(slot, 0) + 1
        if added_counts[slot] >= 4:
            missing.discard(slot)
    if extras:
        logger.info(
            "Budget-band rescue added %d items for slots %s",
            len(extras),
            sorted(added_counts),
        )
    return pool + extras


def _fill_missing_slots(pool: list[dict], relevant: list[dict], intent: dict) -> list[dict]:
    """Top-up missing top/bottom slots from the gender-filtered inventory.

    A non-empty pool can still be starved: office looks came back footwear-only
    when every body garment died at the formal-signal gate (or the tight band —
    the band rescue only covers bottom/shoes). Fill from `relevant` with the
    full hard gate ON (band bypassed, all style gates kept). Office never
    relaxes the formal signal: a starved office draw degrades to fewer/no
    items, not to ethnic-casual inventory (RECO-v4.3 contract).
    """
    if not pool:
        # Empty pools are handled by the caller's degradation chain —
        # don't preempt it here.
        return pool
    have = {(p.get("fashion") or {}).get("slot", "") for p in pool}
    need: set[str] = set()
    if not (have & {"top", "full"}):
        need.add("top")
    if "bottom" not in have:
        need.add("bottom")
    if not need:
        return pool

    in_pool = {id(p) for p in pool}
    added: dict[str, int] = {}
    extras: list[dict] = []

    def _pass() -> None:
        for p in relevant:
            if not need:
                return
            if id(p) in in_pool:
                continue
            slot = (p.get("fashion") or {}).get("slot", "")
            if slot == "full" and "top" in need:
                slot = "top"
            if slot not in need:
                continue
            if not hard_filter([p], intent, None, require_formal_signal=True):
                continue
            extras.append(p)
            in_pool.add(id(p))
            added[slot] = added.get(slot, 0) + 1
            if added[slot] >= 3:
                need.discard(slot)

    _pass()
    if extras:
        logger.info(
            "Slot-coverage fill added %d items for slots %s",
            len(extras),
            sorted(added),
        )
    return pool + extras


# ══════════════════════════════════════════════════════════════
# MATCH SCORING
# ══════════════════════════════════════════════════════════════

_SCORE_WEIGHTS = {
    "style": 0.20,
    "occasion": 0.21,
    "fit": 0.15,
    "color": 0.10,
    "budget": 0.10,
    "quality": 0.08,
    "popularity": 0.04,
    "brand": 0.04,
    "affinity": 0.08,
}


def _lexicon_score(haystack: str, lexicon: dict[str, int], cap: float = 5.0) -> float:
    total = 0
    for word, weight in lexicon.items():
        if word in haystack:
            total += weight
    return min(total / cap, 1.0)


def _style_score(p: dict, intent: dict) -> float:
    style = intent.get("style", "")
    f = p["fashion"]
    haystack = f" {(p.get('title') or '').lower()} {' '.join(f['fabrics'])} {' '.join(f['patterns'])} {f['category']} "
    score = 0.4
    if style and style in _STYLE_LEXICON:
        score = 0.15 + 0.85 * _lexicon_score(haystack, _STYLE_LEXICON[style])
    for conflict in _STYLE_CONFLICTS.get(style, []):
        if conflict in haystack:
            score = max(0.05, score - 0.25)
            break
    if intent.get("palette") and f.get("color") in intent["palette"]:
        score = min(1.0, score + 0.15)
    return score


def _occasion_score(p: dict, intent: dict) -> float:
    occasion = intent.get("occasion", "")
    f = p["fashion"]
    haystack = f" {(p.get('title') or '').lower()} {f['category']} {' '.join(f['patterns'])} "
    score = 0.45
    if occasion in _OCCASION_LEXICON:
        score = 0.2 + 0.8 * _lexicon_score(haystack, _OCCASION_LEXICON[occasion])
    return score


def _fit_score(p: dict, intent: dict) -> float:
    body_shape = intent.get("body_shape") or ""
    fits = p["fashion"]["fits"]
    preferred = _BODY_FIT_FAMILY.get(body_shape)
    if preferred and fits:
        return 1.0 if preferred in fits else 0.6
    if fits:
        return 0.75
    return 0.55


def _color_score(p: dict, intent: dict) -> float:
    color = p["fashion"].get("color")
    palette = intent.get("palette") or set()
    if not color:
        return 0.6
    if color in palette:
        return 1.0
    if color in ("white", "black", "grey", "cream", "navy"):
        return 0.8
    return 0.6


def _budget_score(p: dict, intent: dict, band: tuple[float, float] | None, price_lo: float, price_hi: float) -> float:
    price = p.get("price") or 0
    segment = intent.get("segment", "")
    span = max(price_hi - price_lo, 1)
    pos = min(max((price - price_lo) / span, 0), 1)
    if segment in ("value", "budget"):
        return 1.0 - 0.35 * pos
    if segment in ("luxury",):
        return 0.65 + 0.35 * pos
    if segment in ("premium", "mid"):
        return 1.0 - 0.2 * abs(pos - 0.5) * 2
    if band:
        return 1.0 if _price_in_band(price, band) else 0.5
    return 0.7


def _quality_score(p: dict) -> float:
    rating = p.get("rating") or 0
    if not rating:
        return 0.5
    score = min(rating / 5.0, 1.0)
    if rating < 3.2:
        score *= 0.6
    return score


def _popularity_score(p: dict) -> float:
    reviews = p.get("rating_count") or 0
    return min(math.log10(reviews + 1) / 4.0, 1.0)


def _brand_score(p: dict, intent: dict) -> float:
    brands = intent.get("brands") or []
    if not brands:
        return 0.5
    title = (p.get("title") or "").lower()
    return 1.0 if any(b in title for b in brands) else 0.0


_WESTERN_TITLE_HINTS = (
    "blazer", "shirt", "trouser", "jean", "chino", "skirt", "dress",
    "trench", "coat", "jacket", "sweater", "hoodie", "sneaker", "loafer",
    "oxford", "pump", "heel", "top", "tee", "t-shirt", "crop", "denim",
)
_INDIAN_TITLE_HINTS = (
    "kurta", "kurti", "saree", "sari", "lehenga", "churidar", "sherwani",
    "dhoti", "anarkali", "salwar", "dupatta", "palazzo ethnic", "juttis",
    "mojari", "kolhapuri", "bandhgala", "nehru jacket", "indian", "ethnic",
)
_INDO_WESTERN_HINTS = (
    "indo western", "indo-western", "fusion", "kurta with jeans",
    "kurta dress", "ethnic jacket", "dhoti pants",
)


def _detect_product_culture(p: dict, fashion: dict) -> str:
    """Best-effort culture label for a product (western/indian/indo_western/universal)."""
    if fashion.get("culture"):
        return str(fashion["culture"]).lower()
    title = (p.get("title") or "").lower()
    cat = (fashion.get("category") or "").lower()
    hay = f" {title} {cat} "
    if any(h in hay for h in _INDO_WESTERN_HINTS):
        return "indo_western"
    indian = sum(1 for h in _INDIAN_TITLE_HINTS if h in hay)
    western = sum(1 for h in _WESTERN_TITLE_HINTS if h in hay)
    if indian and western:
        return "indo_western"
    if indian >= western and indian > 0:
        return "indian"
    if western > 0:
        return "western"
    return "universal"


def _affinity_score(p: dict, intent: dict) -> float:
    """Soft preference affinity: culture match + vibes + brand + DNA colour.

    0.5 neutral when no prefs present. Hard gates already filtered the pool —
    this only reorders eligible products.
    """
    prefs = intent.get("user_prefs") or {}
    wear = intent.get("wear_weights") or prefs.get("wear_weights") or {}
    vibes = intent.get("vibes") or prefs.get("vibes") or []
    if not wear and not vibes:
        return 0.5

    f = p.get("fashion") or {}
    score = 0.5

    culture = _detect_product_culture(p, f)
    if wear and culture in ("western", "indian", "indo_western"):
        w = float(wear.get(culture) or 0)
        # Map preference weight → score contribution around neutral
        score = 0.35 + 0.65 * w
        if culture == "universal":
            score = max(score, 0.55)
    elif wear:
        # Unknown culture: slight neutral-high to avoid burying discoveries
        score = 0.55

    # Vibe lexicon match against title/category/patterns
    if vibes:
        hay = f" {(p.get('title') or '').lower()} {f.get('category','')} {' '.join(f.get('patterns') or [])} "
        vibe_hits = 0
        for vibe in vibes:
            v = str(vibe).strip().lower()
            if not v:
                continue
            if v in hay or (v == "minimal" and any(x in hay for x in ("solid", "plain", "classic", "tailored"))) or \
               (v == "classic" and any(x in hay for x in ("classic", "formal", "tailored", "structured"))) or \
               (v == "trendy" and any(x in hay for x in ("trendy", "fashion", "latest", "printed"))) or \
               (v == "smart" and any(x in hay for x in ("smart", "formal", "office", "tailored", "blazer"))) or \
               (v == "relaxed" and any(x in hay for x in ("relaxed", "casual", "comfort", "soft"))):
                vibe_hits += 1
        if vibe_hits:
            score = min(1.0, score + 0.1 * min(vibe_hits, 2))

    # DNA colour preference
    dna_colors = (prefs.get("dna") or {}).get("colours") or prefs.get("colors") or []
    color = (f.get("color") or "").lower()
    if dna_colors and color and color in {str(c).lower() for c in dna_colors}:
        score = min(1.0, score + 0.08)

    return max(0.0, min(1.0, score))


def score_product(p: dict, intent: dict, band: tuple[float, float] | None,
                  price_lo: float, price_hi: float) -> dict:
    scores = {
        "style": _style_score(p, intent),
        "occasion": _occasion_score(p, intent),
        "fit": _fit_score(p, intent),
        "color": _color_score(p, intent),
        "budget": _budget_score(p, intent, band, price_lo, price_hi),
        "quality": _quality_score(p),
        "popularity": _popularity_score(p),
        "brand": _brand_score(p, intent),
        "affinity": _affinity_score(p, intent),
    }
    final = sum(scores[k] * _SCORE_WEIGHTS[k] for k in _SCORE_WEIGHTS)
    p["match_scores"] = {k: round(v, 3) for k, v in scores.items()}
    p["_final"] = round(final, 4)
    return p


# ══════════════════════════════════════════════════════════════
# DIVERSITY SELECTION + LOOK LABELS
# ══════════════════════════════════════════════════════════════

def _title_key(p: dict) -> frozenset:
    words = re.findall(r"[a-z0-9]+", (p.get("title") or "").lower())
    stop = {"for", "with", "and", "the", "a", "of", "in", "to", "pack", "pcs", "piece",
            "set", "1", "2", "3", "new", "latest"}
    return frozenset(w for w in words if w not in stop and len(w) > 1)


def _slot_order(count: int) -> list[str]:
    """Target slot sequence for a flat recommendation list.

    Small counts still reserve room for bottoms/footwear so the UI can
    assemble a complete look (top + bottom + shoes) whenever inventory allows.
    """
    if count <= 0:
        return []
    if count == 1:
        return ["top"]
    if count == 2:
        return ["top", "bottom"]
    if count == 3:
        return ["top", "bottom", "shoes"]
    order = ["top"] * count
    for pos, slot in {2: "bottom", 4: "shoes", 6: "accessory", 8: "bottom"}.items():
        if pos < count:
            order[pos] = slot
    return order


def _bucket(p: dict) -> str:
    slot = p["fashion"]["slot"]
    if slot in ("top", "full"):
        return "top"
    return slot


def _diverse_select(scored: list[dict], count: int) -> list[dict]:
    buckets: dict[str, list[dict]] = {"top": [], "bottom": [], "shoes": [], "accessory": []}
    seen_titles = set()
    seen_combos: dict[tuple, int] = {}
    has_full_outfit = False  # Track if a saree/dress/kurta set was picked

    def _similar(a: frozenset, b: frozenset) -> bool:
        if not a or not b:
            return False
        inter = len(a & b)
        return inter / max(len(a | b), 1) > 0.85

    for p in scored:
        key = _title_key(p)
        if key in seen_titles:
            continue
        seen_titles.add(key)
        combo = (p["fashion"]["category"], p["fashion"].get("color", ""))
        if seen_combos.get(combo, 0) >= 3:
            continue
        seen_combos[combo] = seen_combos.get(combo, 0) + 1
        buckets[_bucket(p)].append(p)

    # Shuffle buckets for variety — same scores, different picks each time
    for b in buckets.values():
        _random.shuffle(b)

    order = _slot_order(count)
    # Per-slot caps (not a single combined non-top budget) so shoes aren't
    # starved after bottoms fill the old max_non_top allowance.
    max_bottoms = max(1, count // 3) if count >= 2 else 0
    max_shoes = 1 if count >= 3 else 0
    max_accessories = 1 if count >= 6 else 0
    selected: list[dict] = []
    used_ids: set[int] = set()
    cat_counts: dict[str, int] = {}

    def _admissible(p: dict) -> bool:
        b = _bucket(p)
        # Full outfits (saree, dress, kurta set) take the whole slot — no bottom needed
        if b == "top" and p["fashion"].get("slot") == "full":
            return True
        if b == "top":
            return True
        # Skip bottom if a full outfit is already selected
        if b == "bottom" and has_full_outfit:
            return False
        bottoms = len([x for x in selected if _bucket(x) == "bottom"])
        shoes = len([x for x in selected if _bucket(x) == "shoes"])
        accs = len([x for x in selected if _bucket(x) == "accessory"])
        if b == "bottom" and bottoms >= max_bottoms:
            return False
        if b == "shoes" and shoes >= max_shoes:
            return False
        if b == "accessory" and accs >= max_accessories:
            return False
        # Cap repeated categories — but allow more generic tops so count can fill
        # (many shirts share category="top"; capping at 2 starved 6-item lists).
        cat = p["fashion"]["category"]
        cat_cap = 4 if b == "top" else 2
        if cat_counts.get(cat, 0) >= cat_cap:
            return False
        return True

    def take(bucket: str) -> dict | None:
        pool = buckets.get(bucket) or []
        while pool:
            p = pool.pop(0)
            if id(p) in used_ids or not _admissible(p):
                continue
            if any(_similar(_title_key(p), _title_key(s)) for s in selected):
                continue
            # Hard pairing gate: no Frankenstein outfits (saree+pants etc.)
            if not frules.outfit_pairing_ok(p, selected):
                continue
            used_ids.add(id(p))
            cat_counts[p["fashion"]["category"]] = cat_counts.get(p["fashion"]["category"], 0) + 1
            # Mark full outfit so we skip bottom
            if _bucket(p) == "top" and p["fashion"].get("slot") == "full":
                nonlocal has_full_outfit
                has_full_outfit = True
            return p
        return None

    fill_order = ("top", "bottom", "shoes", "accessory")
    for slot in order:
        # Skip bottom if full outfit already selected
        if slot == "bottom" and has_full_outfit:
            continue
        p = take(slot)
        if p is None and slot in ("bottom", "shoes"):
            # Never silently replace a missing bottom/shoes with a shirt —
            # backfill pass below will try again from leftover inventory.
            continue
        if p is None:
            for b in fill_order:
                if b == slot:
                    continue
                if b == "bottom" and has_full_outfit:
                    continue
                p = take(b)
                if p:
                    break
        if p:
            selected.append(p)
        if len(selected) >= count:
            break

    # Backfill missing bottom/shoes when inventory exists (complete-outfit guarantee)
    for req in ("bottom", "shoes"):
        if any(_bucket(x) == req for x in selected):
            continue
        if req == "bottom" and has_full_outfit:
            continue
        # Temporarily relax the per-slot cap for the backfill take
        if req == "bottom":
            max_bottoms += 1
        elif req == "shoes":
            max_shoes += 1
        p = take(req)
        if req == "bottom":
            max_bottoms -= 1
        elif req == "shoes":
            max_shoes -= 1
        if not p:
            continue
        if len(selected) < count:
            selected.append(p)
        else:
            # Replace a trailing non-full top so the list still fits count
            replace_idx = None
            for i in range(len(selected) - 1, -1, -1):
                if _bucket(selected[i]) == "top" and selected[i]["fashion"].get("slot") != "full":
                    replace_idx = i
                    break
            if replace_idx is not None:
                selected[replace_idx] = p

    return selected[:count]


def _assign_labels(selected: list[dict], count: int) -> None:
    if not selected:
        return
    for p in selected:
        p["look_label"] = ""

    selected[0]["look_label"] = "Safe Pick"

    def best(candidates: list[dict], fn) -> dict | None:
        pool = [p for p in candidates if not p.get("look_label")]
        return max(pool, key=fn) if pool else None

    if count >= 4:
        prices = sorted(p.get("price", 0) for p in selected)
        median = prices[len(prices) // 2]

        def value_score(x: dict) -> float:
            if x.get("price", 0) > median:
                return -1
            rating = x.get("rating") or 0
            if rating and rating < 3.5:
                return -1
            return x["match_scores"]["quality"] + x["match_scores"]["budget"]

        p = best(selected, value_score)
        if p:
            p["look_label"] = "Best Value"

    if count >= 5:
        p = best([x for x in selected if (x.get("rating") or 0) >= 4.0 or not x.get("rating")],
                 lambda x: x.get("price", 0))
        if p:
            p["look_label"] = "Elevated"

    if count >= 6:
        def statement_score(x: dict) -> float:
            f = x["fashion"]
            pat = 1.0 if any(pt in ("embroidered", "floral", "printed") for pt in f["patterns"]) else 0.0
            col = 0.5 if f.get("color") in ("red", "pink", "yellow", "purple", "orange") else 0.0
            return pat + col
        p = best(selected, statement_score)
        if p and statement_score(p) > 0:
            p["look_label"] = "Statement"


def build_reason(p: dict, intent: dict) -> str:
    f = p["fashion"]
    parts: list[str] = []

    fabric_hints = intent.get("fabric_hints") or []
    matched_fabric = next((fb for fb in f["fabrics"] if fb in fabric_hints), None)
    if matched_fabric:
        parts.append(matched_fabric.title())

    if f.get("color"):
        parts.append(f["color"].title())

    rating = p.get("rating") or 0
    if rating:
        count = p.get("rating_count") or 0
        if count >= 1000:
            parts.append(f"{rating}★ ({count // 1000}k)")
        else:
            parts.append(f"{rating}★")

    style = intent.get("style", "")
    if style and style in _STYLE_LEXICON:
        haystack = f" {(p.get('title') or '').lower()} {f['category']} "
        if _lexicon_score(haystack, _STYLE_LEXICON[style]) > 0.25:
            parts.append(f"{style.title()} style")

    occasion = intent.get("occasion", "")
    if occasion:
        parts.append(f"{occasion.title()} ready")

    if not parts:
        parts.append("Matches your preferences")
    return " · ".join(parts[:3])


# ══════════════════════════════════════════════════════════════
# PIPELINE
# ══════════════════════════════════════════════════════════════

def recommend_products(products: list[dict], intent: dict, count: int) -> dict:
    """Full multi-stage pipeline: gender-filter → distribute → band → score → diversify.

    Returns {"selected", "band", "distribution", "candidates"} where candidates
    is the scored pool (used for algorithmic brand ranking).
    """
    # Gender/kids filter first — the distribution should reflect what is
    # actually available for this user, not the raw search pool.
    # Imageless products are unusable (blank widget card, VTON 400) — drop them.
    relevant = [
        p for p in products
        if gender_ok(p.get("title", ""), intent.get("gender", ""))
        and (p.get("image_url") or "").strip()
    ]

    dist = price_distribution(relevant)
    band = resolve_band(intent, dist)
    is_level = intent.get("budget_mode") == "level"

    if band:
        strict = hard_filter(relevant, intent, band)
        if is_level and len(strict) < min(count, 3):
            # Thin level window → expand downward to fill a usable pool
            wider = (max(0.0, band[0] * 0.5), band[1])
            wider_pool = hard_filter(relevant, intent, wider)
            pool = wider_pool if len(wider_pool) > len(strict) else strict
        elif strict:
            pool = strict
        else:
            pool = hard_filter(relevant, intent, _widen(band))
    else:
        pool = hard_filter(relevant, intent, None)

    # Rescue missing bottom/shoes inventory lost to a tight budget band
    pool = _rescue_slot_coverage(pool, relevant, intent)
    # Top-up missing top/bottom slots (gate-starved body garments)
    pool = _fill_missing_slots(pool, relevant, intent)

    # RECO-v4.3 contract: office never drops the formal signal. The old
    # "pool empty → require_formal_signal=False" degrade shipped ethnic sets
    # (kurta + dupatta) and casual tops as office looks. Recovery is the
    # band-widen above + gated rescue/fill + the marketplace re-scrape;
    # beyond that an honest empty draw beats a non-formal one.

    if not pool and is_level and relevant:
        # Level is a preference, not a hard budget — degrade to the
        # fanciest available items rather than returning nothing.
        # Style/occasion gates still run (gender/image already in `relevant`).
        level_pool = hard_filter(relevant, intent, None)
        if level_pool:
            pool = sorted(level_pool, key=lambda p: p.get("price", 0), reverse=True)[: max(count * 2, 10)]

    if not pool:
        return {
            "selected": [],
            "band": band,
            "distribution": dist,
            "candidates": [],
            "validation": {"passed": False, "confidence": 0.0,
                           "violations": ["empty_outfit"], "level": "low"},
        }

    prices = [p.get("price") or 0 for p in pool]
    price_lo, price_hi = min(prices), max(prices)

    scored = [score_product(p, intent, band, price_lo, price_hi) for p in pool]
    scored.sort(key=lambda p: p["_final"], reverse=True)

    selected = _diverse_select(scored, count)
    # Office must be a complete look: no body garment survived the gates →
    # ship empty (marketplace layer re-scrapes), never footwear-only.
    if _office_formal_required(intent) and not any(
        (p.get("fashion") or {}).get("slot") in ("top", "bottom", "full") for p in selected
    ):
        selected = []
    _assign_labels(selected, count)

    # Validate the assembled selection as an outfit system
    try:
        validation = frules.validate_outfit(selected, intent)
        # Defense in depth: an office selection must carry at least one
        # formal-signal body garment (should be unreachable — every pool
        # entry point runs the formal gate for office).
        if _office_formal_required(intent) and selected:
            body_titles = " ".join(
                (p.get("title") or "").lower()
                for p in selected
                if (p.get("fashion") or {}).get("slot") in ("top", "bottom", "full")
            )
            if not any(sig in f" {body_titles} " for sig in _OFFICE_FORMAL_SIGNALS):
                validation.setdefault("violations", []).append("office_no_formal_signal")
                validation["passed"] = False
        intent["outfit_validation"] = validation
        if not validation.get("passed") and validation.get("violations"):
            logger.info(f"Outfit validation issues: {validation['violations'][:5]}")
    except Exception as exc:
        logger.warning(f"Outfit validation failed: {exc}")
        intent["outfit_validation"] = {"passed": True, "confidence": 0.5, "level": "medium"}

    for p in scored:
        p["_reason"] = build_reason(p, intent)

    return {
        "selected": selected,
        "band": band,
        "distribution": dist,
        "candidates": scored,
        "validation": intent.get("outfit_validation", {}),
    }
