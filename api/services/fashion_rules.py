"""
Fashion Rules Engine — machine-readable outfit grammar + search query compiler.

Loads api/data/fashion_rules_v1.json and exposes:
  - Occasion canonicalisation (frontend free-text → taxonomy)
  - Outfit blueprint generation (archetype → concrete components)
  - Search query compilation (blueprint → Google Shopping queries with exclusions)
  - Hard validation (occasion / pairing / formality / cultural gates)

Design principles:
  - Deterministic. No LLM in the hot path.
  - Hard constraints are gates, not scores.
  - LLM decisions (if any) happen BEFORE this layer (intent) and AFTER
    (explanation), never inside query compilation.
"""
from __future__ import annotations

import functools
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger("drishti.reco.fashion_rules")

_RULES_PATH = Path(__file__).resolve().parent.parent / "data" / "fashion_rules_v1.json"

# Colour family → human query words (retrieval language)
_COLOUR_QUERY_WORDS: dict[str, str] = {
    "cream": "cream", "white": "white", "black": "black", "grey": "grey",
    "navy": "navy", "blue": "blue", "green": "green", "brown": "brown",
    "red": "red", "pink": "pink", "yellow": "yellow", "purple": "purple",
    "orange": "orange",
}

# Extra colour synonyms that help Indian shopping queries
_COLOUR_SYNONYMS: dict[str, list[str]] = {
    "cream": ["ivory", "beige", "off white"],
    "blue": ["sky blue", "powder blue", "light blue"],
    "green": ["sage green", "olive green"],
    "pink": ["dusty pink", "blush pink"],
    "yellow": ["mustard", "lemon yellow"],
}

_EMBELLISHMENT_RANK = {"none": 0, "low": 1, "medium": 2, "heavy": 3}


# ══════════════════════════════════════════════════════════════
# LOADER
# ══════════════════════════════════════════════════════════════

@functools.lru_cache(maxsize=1)
def load_rules() -> dict:
    """Load and cache the fashion rules JSON."""
    with open(_RULES_PATH, encoding="utf-8") as f:
        return json.load(f)


def rules() -> dict:
    return load_rules()


# ══════════════════════════════════════════════════════════════
# OCCASION RESOLUTION
# ══════════════════════════════════════════════════════════════

def canonicalize_occasion(raw: str) -> str:
    """Map frontend free-text / aliases to a canonical occasion id.

    Unknown text falls back to 'casual'. Sub-events (haldi, sangeet…)
    map to their parent occasion; the raw string is kept separately.
    """
    r = (raw or "").strip().lower()
    if not r:
        return "casual"
    occ = rules()["occasions"]
    if r in occ:
        return r
    aliases = rules()["occasion_aliases"]
    if r in aliases:
        return aliases[r]
    # fuzzy: try alias keys contained in the raw string
    for key, val in aliases.items():
        if key in r:
            return val
    for key in occ:
        if key in r:
            return key
    return "casual"


def resolve_sub_event(raw: str) -> str:
    """Extract wedding/festive sub-event if present (haldi, sangeet…)."""
    r = (raw or "").strip().lower()
    occ = rules()["occasions"].get("wedding", {})
    for sub in occ.get("sub_events", {}):
        if sub in r:
            return sub
    return ""


def resolve_dress_code(occasion: str, style: str = "") -> str:
    occ = rules()["occasions"].get(occasion, {})
    code = occ.get("dress_code", "casual")
    # Style can elevate formality within occasion, never override occasion
    if style in ("corporate", "classic", "old money") and code == "casual":
        return "smart_casual"
    return code


def occasion_config(occasion: str) -> dict:
    return rules()["occasions"].get(occasion) or rules()["occasions"]["casual"]


def negative_keywords(occasion: str, limit: int | None = None) -> list[str]:
    kw = list(occasion_config(occasion).get("negative_keywords", []))
    lim = limit if limit is not None else rules()["query_compiler"]["max_exclusions"]
    return kw[:lim]


# ══════════════════════════════════════════════════════════════
# ARCHETYPES → OUTFIT BLUEPRINTS
# ══════════════════════════════════════════════════════════════

def _gender_key(gender: str) -> str:
    g = (gender or "").strip().lower()
    if g in ("female", "woman", "girl", "f", "women"):
        return "female"
    if g in ("male", "man", "boy", "m", "men"):
        return "male"
    return "female"  # default — wizard collects binary + non-binary; female is safer for ethnic coverage


def get_archetypes(gender: str, occasion: str, count: int = 3, intent: dict | None = None) -> list[dict]:
    """Return up to `count` archetypes ordered safe → elevated → personality.

    Office + strong western preference hard-filters to western-only blueprints
    (no kurta/saree discovery). Otherwise soft re-rank by culture affinity while
    reserving the last slot for controlled discovery (the "2 preferred + 1
    discovery" rule). Hard occasion gates still apply later.
    """
    arch = rules()["archetypes"]
    g = _gender_key(gender)
    pool = arch.get(g, {}).get(occasion) or arch.get(g, {}).get("casual", [])
    # stable order: safe first, then elevated, then personality
    order = {"safe": 0, "elevated": 1, "personality": 2}
    pool = sorted(pool, key=lambda a: order.get(a.get("variant", "safe"), 0))

    wear = {}
    if intent:
        wear = intent.get("wear_weights") or (intent.get("user_prefs") or {}).get("wear_weights") or {}

    # Office/interview + strong western → formal western only (user requirement)
    if wear and occasion in ("work", "interview", "office"):
        w_west = float(wear.get("western") or 0)
        w_ind = float(wear.get("indian") or 0)
        if w_west >= 0.75 and w_west > w_ind:
            western_pool = [a for a in pool if _archetype_is_western(a)]
            if western_pool:
                pool = western_pool

    if wear and len(pool) >= 2:
        pool = _re_rank_archetypes_by_affinity(pool, wear, count)

    return pool[:count] if count else pool


def _archetype_is_western(arch: dict) -> bool:
    """True when every body-garment component is western (or universal)."""
    garments = rules()["garments"]
    body = [c for c in arch.get("components", []) if c.get("slot") not in ("shoes", "accessory")]
    if not body:
        return False
    for comp in body:
        culture = (garments.get(comp.get("garment", "")) or {}).get("culture", "").lower()
        if culture not in ("western", "universal"):
            return False
    return True


def _archetype_culture_affinity(arch: dict, wear: dict) -> float:
    """Mean wear weight for the archetype's component garments' cultures."""
    garments = rules()["garments"]
    scores: list[float] = []
    for comp in arch.get("components", []):
        g = garments.get(comp.get("garment", ""))
        if not g:
            continue
        culture = (g.get("culture") or "").lower()
        if culture in ("western", "indian"):
            scores.append(float(wear.get(culture) or 0.5))
        elif culture == "universal":
            scores.append(0.55)
        else:
            scores.append(0.5)
    if not scores:
        return 0.5
    return sum(scores) / len(scores)


def _re_rank_archetypes_by_affinity(pool: list[dict], wear: dict, count: int) -> list[dict]:
    """2 preferred + 1 discovery: sort by affinity, keep least-preferred as last slot."""
    if not pool:
        return pool
    scored = [( _archetype_culture_affinity(a, wear), i, a) for i, a in enumerate(pool)]
    # Prefer higher affinity; stable by original index
    scored.sort(key=lambda t: (-t[0], t[1]))
    ordered = [a for _, _, a in scored]

    if count and len(ordered) >= 2:
        # Top (count-1) by affinity + last = discovery (lowest affinity among remaining)
        preferred_n = max(1, count - 1)
        top = ordered[:preferred_n]
        # Discovery = lowest affinity not already in top, prefer from full original set
        discovery_candidates = [a for _, _, a in sorted(scored, key=lambda t: (t[0], t[1]))]
        discovery = next((a for a in discovery_candidates if a not in top), None)
        if discovery is not None:
            return top + [discovery]
    return ordered


def _pick_colour(component: dict, intent: dict, variant: str, index: int) -> str:
    """Deterministic-ish colour pick from component preference ∩ intent palette."""
    preferred = component.get("colours") or []
    palette = intent.get("palette") or set()
    occ_pal = occasion_config(intent.get("occasion", "casual")).get("palette", [])

    # Prefer component colours that are also in occasion palette
    candidates = [c for c in preferred if c in occ_pal] or list(preferred)
    if variant == "safe":
        # safe → neutrals first
        neutrals = {"white", "black", "grey", "cream", "navy", "blue"}
        candidates = [c for c in candidates if c in neutrals] or candidates
    elif variant == "personality":
        # personality → non-neutrals first
        neutrals = {"white", "black", "grey", "cream", "navy"}
        bold = [c for c in candidates if c not in neutrals]
        candidates = bold or candidates

    if not candidates:
        return ""
    # rotate by index so blueprints get different colours
    return candidates[index % len(candidates)]


def _pick_fabric(component: dict, intent: dict, slot: str = "primary") -> str:
    preferred = component.get("fabrics") or []
    # Fabric hints only apply to body garments — never shoes/accessories
    if slot in ("shoes", "accessory"):
        return preferred[0] if preferred else ""
    if not preferred:
        hints = intent.get("fabric_hints") or []
        return hints[0] if hints else ""
    hints = intent.get("fabric_hints") or []
    for f in preferred:
        if f in hints:
            return f
    avoid = intent.get("avoid_fabrics") or []
    for f in preferred:
        if f not in avoid:
            return f
    return preferred[0]


def build_outfit_blueprints(intent: dict, n: int = 3) -> list[dict]:
    """Turn intent into n concrete outfit blueprints (safe/elevated/personality).

    Each blueprint:
      {
        id, archetype, variant, label,
        components: [{slot, garment, colour, fabric, search_term, formality}],
        hard_constraints: [...],
        occasion, dress_code
      }
    """
    occasion = intent.get("occasion", "casual")
    gender = intent.get("gender", "")
    style = intent.get("style", "")
    if isinstance(style, (list, tuple, set)):
        style = next(iter(style), "") if style else ""
    style = str(style or "").strip().lower()
    sub_event = intent.get("sub_event") or resolve_sub_event(intent.get("occasion_raw", ""))
    if sub_event:
        intent["sub_event"] = sub_event

    archetypes = get_archetypes(gender, occasion, count=n, intent=intent)
    if not archetypes:
        return []

    occ = occasion_config(occasion)
    garments = rules()["garments"]
    style_attrs = rules()["style_attributes"].get(style, {})

    blueprints: list[dict] = []
    for bi, arch in enumerate(archetypes):
        variant = arch.get("variant", "safe")
        components = []
        for ci, comp in enumerate(arch.get("components", [])):
            g_id = comp.get("garment", "")
            g = garments.get(g_id)
            if not g:
                continue
            colour = _pick_colour(comp, intent, variant, bi + ci)
            slot = comp.get("slot") or g.get("slot", "primary")
            fabric = _pick_fabric(comp, intent, slot)
            gender_key = _gender_key(gender)
            search_base = (g.get("search", {}) or {}).get(gender_key) or \
                          (g.get("search", {}) or {}).get("female") or \
                          (g.get("search", {}) or {}).get("male") or ""
            if not search_base:
                continue
            # Strip gender word from search term — compiler adds it once
            gw = "women" if gender_key == "female" else "men"
            cleaned = search_base
            for gword in (gw, "women", "men", "womens", "mens"):
                cleaned = re.sub(rf"\b{gword}\b", "", cleaned, flags=re.I)
            cleaned = re.sub(r"\s+", " ", cleaned).strip() or search_base
            components.append({
                "slot": slot,
                "garment": g_id,
                "colour": colour,
                "fabric": fabric,
                "search_term": cleaned,
                "formality": g.get("formality", 2),
                "detect_categories": g.get("detect_categories", []),
                "never_with": g.get("never_with", []),
                "office_ok": g.get("office_ok", True),
                "embellishment_max": g.get("embellishment_max", "medium"),
                "culture": (g.get("culture") or "").lower(),
            })
        if not components:
            continue
        blueprints.append({
            "id": f"{arch.get('id', 'OB')}-{bi}",
            "archetype": arch.get("id", ""),
            "variant": variant,
            "label": arch.get("label", ""),
            "components": components,
            "occasion": occasion,
            "dress_code": resolve_dress_code(occasion, style),
            "sub_event": sub_event,
            "hard_constraints": [
                f"no_{occ.get('embellishment_max', 'heavy')}_embellishment"
                if occ.get("embellishment_max") in ("none", "low")
                else "standard",
            ],
            "style_query_terms": style_attrs.get("query_terms", []),
        })
    return blueprints


# ══════════════════════════════════════════════════════════════
# SEARCH QUERY COMPILER
# ══════════════════════════════════════════════════════════════

def _gender_word(gender: str) -> str:
    g = _gender_key(gender)
    return "women" if g == "female" else "men"


def compile_component_query(
    component: dict,
    intent: dict,
    level: int = 0,
    exclude_terms: list[str] | None = None,
) -> str:
    """Compile one blueprint component into a Google Shopping query.

    Formula: {gender} {colour} {garment} {fabric?} {occasion_word} India {-exclusions}
    Levels relax fabric → colour → occasion (never gender/garment core).
    """
    compiler = rules()["query_compiler"]
    drops = set()
    for lv in compiler["relaxation_levels"]:
        if lv["level"] == level:
            drops = set(lv.get("drop", []))
            break

    occasion = intent.get("occasion", "casual")
    occ = occasion_config(occasion)
    gender_w = _gender_word(intent.get("gender", ""))
    style_terms = intent.get("style_query_terms") or []

    parts: list[str] = [gender_w]

    colour = component.get("colour") or ""
    if colour and "colour" not in drops:
        parts.append(colour)

    search_term = component.get("search_term") or ""
    if search_term:
        parts.append(search_term)

    fabric = component.get("fabric") or ""
    if fabric and "fabric" not in drops and fabric not in search_term:
        parts.append(fabric)

    occ_word = occ.get("query_word", "")
    # sub-event overrides occasion word for wedding
    sub = intent.get("sub_event") or ""
    if sub and occasion in ("wedding", "festive"):
        sub_cfg = occ.get("sub_events", {}).get(sub, {})
        occ_word = sub_cfg.get("query_word", occ_word)
    if occ_word and "occasion_word" not in drops and occ_word not in " ".join(parts):
        parts.append(occ_word)

    # Style terms (translated physical attributes, not vibes) — body garments only
    if component.get("slot") not in ("shoes", "accessory"):
        for term in style_terms[:1]:
            if term and term not in parts:
                parts.append(term)

    # Soft culture bias from Fashion DNA wear preference (e.g. western office)
    wear = intent.get("wear_weights") or (intent.get("user_prefs") or {}).get("wear_weights") or {}
    component_culture = (component.get("culture") or "").lower()
    if wear and component.get("slot") not in ("shoes", "accessory"):
        # If product is western and user strongly prefers western → add "western"
        if component_culture == "western" and float(wear.get("western") or 0) >= 0.75:
            if "western" not in parts and not any("western" in p for p in parts):
                parts.append("western")
        elif component_culture == "indian" and float(wear.get("indian") or 0) >= 0.75:
            if "indian" not in " ".join(parts).lower() and "ethnic" not in parts:
                # only add ethnic/indian for wedding/festive/traditional contexts
                if intent.get("occasion") in ("wedding", "festive", "traditional"):
                    parts.append("ethnic")

    parts.append("India")

    query = " ".join(p for p in parts if p)
    query = re.sub(r"\s+", " ", query).strip()

    # Exclusions — 3–7 high-impact, occasion-aware
    excludes = exclude_terms if exclude_terms is not None else negative_keywords(occasion)
    # Soft western-office: bias excludes toward ethnic statement pieces when pref is strong western
    if wear and float(wear.get("western") or 0) >= 0.85 and occasion in ("work", "interview"):
        for extra in ("lehenga", "saree", "bridal"):
            if extra not in excludes:
                excludes = list(excludes) + [extra]
    # Never exclude a word that appears in the positive query
    positive = query.lower()
    final_excl = []
    for e in excludes:
        if e.lower() in positive:
            continue
        if e.lower() in {x.lower() for x in final_excl}:
            continue
        final_excl.append(e)
    if final_excl:
        query = query + " " + " ".join(f"-{e.replace(' ', '-')}" for e in final_excl)
    return query


def compile_blueprint_queries(
    blueprints: list[dict],
    intent: dict,
    max_queries: int = 6,
) -> list[dict]:
    """Compile slot queries across blueprints using the search plan.

    Returns [{slot, query, blueprint_id, level, variant}] ready for parallel search.
    Default plan: blueprint0 → primary+bottom+shoes, blueprint1 → primary+bottom,
    blueprint2 → primary. Full-outfit blueprints skip bottom.
    """
    compiler = rules()["query_compiler"]
    plan = compiler.get("blueprint_plan", [])
    excludes = negative_keywords(intent.get("occasion", "casual"))

    out: list[dict] = []
    seen_q: set[str] = set()

    for bp_i, bp in enumerate(blueprints):
        if len(out) >= max_queries:
            break
        slots_wanted = next(
            (p["slots"] for p in plan if p["blueprint"] == bp_i),
            ["primary", "bottom", "shoes"] if bp_i == 0 else ["primary"],
        )
        comps_by_slot = {c["slot"]: c for c in bp.get("components", [])}

        # Full-outfit garments own the primary slot — no bottom search
        primary_comp = comps_by_slot.get("primary")
        is_full = bool(primary_comp and primary_comp.get("slot") == "full") or \
                  any(c.get("slot") == "full" for c in bp.get("components", []))
        if is_full:
            slots_wanted = [s for s in slots_wanted if s != "bottom"]
            # also skip if primary is the full garment itself
            if primary_comp and primary_comp.get("slot") == "full":
                pass

        for slot in slots_wanted:
            if len(out) >= max_queries:
                break
            comp = comps_by_slot.get(slot)
            if not comp:
                # try matching by garment slot for shoes/accessory naming
                comp = next((c for c in bp["components"] if c["slot"] == slot), None)
            if not comp:
                continue
            q = compile_component_query(comp, intent, level=0, exclude_terms=excludes)
            # Dedupe by slot+garment (not full query text) so colour rotation
            # doesn't silently drop a needed bottom/shoes search, while still
            # preventing identical slot+garment queries from stacking.
            key = f"{slot}::{comp.get('garment', '')}"
            if key in seen_q:
                continue
            seen_q.add(key)
            out.append({
                "slot": slot,
                "query": q,
                "blueprint_id": bp["id"],
                "variant": bp.get("variant", "safe"),
                "level": 0,
                "garment": comp.get("garment", ""),
            })

    # Guarantee at least one bottom + one shoes query unless a full-outfit
    # blueprint already owns the look (saree/dress/kurta-set paths).
    has_full = any(
        c.get("slot") == "full"
        for bp in blueprints
        for c in bp.get("components", [])
    )
    if not has_full:
        present = {x["slot"] for x in out}
        for bp in blueprints:
            comps_by_slot = {c["slot"]: c for c in bp.get("components", [])}
            for needed in ("bottom", "shoes"):
                if needed in present or len(out) >= max_queries:
                    continue
                comp = comps_by_slot.get(needed)
                if not comp:
                    continue
                q = compile_component_query(comp, intent, level=0, exclude_terms=excludes)
                out.append({
                    "slot": needed,
                    "query": q,
                    "blueprint_id": bp["id"],
                    "variant": bp.get("variant", "safe"),
                    "level": 0,
                    "garment": comp.get("garment", ""),
                })
                present.add(needed)
    return out


def relax_query(query: str, level: int) -> str:
    """Progressive fallback: strip -exclusions first, then trailing attribute words.

    Never relax gender (women/men) or 'India'.
    """
    # Level 1: drop exclusions
    if level >= 1:
        query = re.sub(r"\s-\S+", "", query)
    # Level 2: drop last non-gender content word before India (approx. fabric/colour)
    if level >= 2:
        words = query.split()
        if "India" in words:
            idx = words.index("India")
            # remove one qualifier just before India if present
            if idx >= 2:
                # skip removing gender (index 0)
                removable = idx - 1
                if removable > 0:
                    words.pop(removable)
                query = " ".join(words)
    # Level 3: strip everything after garment core is hard without parsing —
    # just drop exclusions + one more qualifier
    if level >= 3:
        words = query.split()
        if "India" in words and len(words) > 4:
            # keep gender + 1-2 core words + India
            gender = words[0]
            core = [w for w in words[1:-1]][:2]
            query = " ".join([gender] + core + ["India"])
    return re.sub(r"\s+", " ", query).strip()


# ══════════════════════════════════════════════════════════════
# VALIDATION — hard gates
# ══════════════════════════════════════════════════════════════

_TITLE_REJECT_CACHE: dict[str, list[str]] = {}
_EMBELLISHMENT_HEAVY = (
    "bridal", "sequin", "zari", "mirror work", "heavy embroidery",
    "heavy work", "gotapatti", "gota patti", "shimmer", "dazzling",
    "beaded", "feather", "bodycon party",
)


def _embellishment_level(title: str, patterns: list[str]) -> str:
    t = (title or "").lower()
    if any(k in t for k in _EMBELLISHMENT_HEAVY):
        return "heavy"
    if "embroidered" in patterns or "printed" in patterns and "heavy" in t:
        return "medium"
    if "embroidered" in patterns:
        return "low"
    return "none" if not patterns else "low"


def title_rejected(product: dict, occasion: str) -> bool:
    """True if product title hits occasion title_reject keywords."""
    gate = rules().get("occasion_category_gates", {}).get(occasion)
    if not gate:
        return False
    t = (product.get("title") or "").lower()
    for kw in gate.get("reject_title_keywords", []):
        if kw in t:
            return True
    return False


def category_rejected(product: dict, occasion: str) -> bool:
    gate = rules().get("occasion_category_gates", {}).get(occasion)
    if not gate:
        return False
    cat = _product_category(product)
    return cat in gate.get("reject_categories", [])


def embellishment_rejected(product: dict, occasion: str) -> bool:
    gate = rules().get("occasion_category_gates", {}).get(occasion)
    if not gate:
        return False
    max_allowed = gate.get("reject_if_embellishment")
    if not max_allowed:
        return False
    f = product.get("fashion", {}) or {}
    level = f.get("embellishment") or _embellishment_level(
        product.get("title", ""), f.get("patterns", [])
    )
    # Reject at or above the threshold (work: reject heavy+; gym: reject any+)
    return _EMBELLISHMENT_RANK.get(level, 0) >= _EMBELLISHMENT_RANK.get(max_allowed, 99)


def occasion_ok(product: dict, occasion: str) -> bool:
    """Hard gate: does this product belong to the requested occasion?"""
    return not (
        title_rejected(product, occasion)
        or category_rejected(product, occasion)
        or embellishment_rejected(product, occasion)
    )


def pairing_violation(cat_a: str, cat_b: str, title_a: str = "", title_b: str = "") -> bool:
    """Hard gate: are these two garment categories ever combined?"""
    if not cat_a or not cat_b or cat_a == cat_b:
        return False

    # Hard exclusions first — these are authoritative for the category pair.
    # Keyword-gated exclusions: if keywords absent, the pair is allowed
    # (e.g. saree + shoes OK unless sneakers/running in the shoe title).
    pair_covered = False
    for ex in rules().get("hard_exclusions", []):
        a_set = ex.get("a_categories", [])
        b_set = ex.get("b_categories", [])
        hit_ab = cat_a in a_set and cat_b in b_set
        hit_ba = cat_b in a_set and cat_a in b_set
        if not (hit_ab or hit_ba):
            continue
        pair_covered = True
        kw = ex.get("b_keywords")
        if not kw:
            return True
        # Keywords checked against the title on the b-side (or either if ambiguous)
        check_title = (title_b or "") if hit_ab else (title_a or "")
        # For symmetric keyword gates also allow either title
        both_titles = f"{title_a or ''} {title_b or ''}".lower()
        if any(k in check_title.lower() for k in kw) or any(k in both_titles for k in kw):
            return True
        return False  # conditional exclusion not triggered → allowed

    if pair_covered:
        return False

    # No hard_exclusion for this pair → consult per-garment never_with lists.
    # Declarations are scoped to the garment TYPE the title actually matches:
    # without this, never_with unions across every type sharing a category
    # (oxford_shoes' bare "jeans" ban killed jeans + canvas sneakers too).
    garments = rules()["garments"]

    def _declarers(cat: str, title: str) -> list[dict]:
        t = (title or "").lower()
        matched = []
        for g in garments.values():
            if cat not in (g.get("detect_categories") or []):
                continue
            kws = g.get("detect_keywords") or []
            if kws and not any(k in t for k in kws):
                continue
            matched.append(g)
        return matched

    never_a: set[str] = set()
    for g in _declarers(cat_a, title_a):
        never_a.update(g.get("never_with") or [])
    never_b: set[str] = set()
    for g in _declarers(cat_b, title_b):
        never_b.update(g.get("never_with") or [])
    if not never_a and not never_b:
        # Fallback: exotic title matched no detect_keywords → old category-only
        # behaviour so declarations can't silently vanish
        for g in garments.values():
            if cat_a in (g.get("detect_categories") or []):
                never_a.update(g.get("never_with") or [])
            if cat_b in (g.get("detect_categories") or []):
                never_b.update(g.get("never_with") or [])

    # Multi-word never_with entries are title keywords; bare words are category/garment ids
    def _never_hits(never: set[str], other_cat: str, other_title: str) -> bool:
        t = (other_title or "").lower()
        for entry in never:
            if " " in entry:
                if entry in t:
                    return True
            elif entry == other_cat:
                return True
        return False

    if _never_hits(never_a, cat_b, title_b):
        return True
    if _never_hits(never_b, cat_a, title_a):
        return True
    return False


def _product_category(product: dict) -> str:
    f = product.get("fashion") or {}
    if isinstance(f, dict) and f.get("category"):
        return str(f["category"])
    return str(product.get("category") or "")


def outfit_pairing_ok(product: dict, selected: list[dict]) -> bool:
    """Check a candidate product against already-selected outfit members."""
    cat = _product_category(product)
    title = product.get("title", "")
    for s in selected:
        s_cat = _product_category(s)
        if pairing_violation(cat, s_cat, title, s.get("title", "")):
            return False
    return True


def validate_outfit(products: list[dict], intent: dict) -> dict:
    """Run the hard validation layers on an assembled outfit.

    Returns {passed, scores, violations, confidence}.
    Soft scores feed ranking/confidence; only `passed` gates display.
    """
    occasion = intent.get("occasion", "casual")
    occ = occasion_config(occasion)
    dress_code = intent.get("dress_code") or resolve_dress_code(occasion, intent.get("style", ""))

    violations: list[str] = []
    scores: dict[str, float] = {
        "occasion": 1.0,
        "garment_compatibility": 1.0,
        "formality": 1.0,
        "cultural": 1.0,
        "colour": 1.0,
        "climate": 1.0,
        "practicality": 1.0,
    }

    if not products:
        return {"passed": False, "scores": scores, "violations": ["empty_outfit"], "confidence": 0.0}

    # Layer 1: occasion membership
    for p in products:
        if not occasion_ok(p, occasion):
            violations.append(f"occasion_mismatch:{p.get('title', '')[:40]}")
            scores["occasion"] = min(scores["occasion"], 0.2)

    # Layer 3: garment pairing
    for i, a in enumerate(products):
        for b in products[i + 1:]:
            ca = _product_category(a)
            cb = _product_category(b)
            if pairing_violation(ca, cb, a.get("title", ""), b.get("title", "")):
                violations.append(f"pairing:{ca}+{cb}")
                scores["garment_compatibility"] = 0.0

    # Layer 5: formality consistency — body garments only (shoes/accessories
    # naturally sit at different formality and must not fail the outfit)
    body_slots = {"top", "bottom", "full"}
    formalities = []
    for p in products:
        f = p.get("fashion", {}) or {}
        if f.get("slot") not in body_slots and f.get("category") not in (
            "kurta", "kurta_set", "saree", "lehenga", "sherwani", "dress", "co-ord",
            "anarkali", "sharara", "top", "tshirt", "polo", "outerwear", "trousers",
            "jeans", "joggers", "shorts", "skirt",
        ):
            continue
        g = _garment_by_category(f.get("category", ""))
        formalities.append(g.get("formality", 2) if g else 2)
    if len(formalities) >= 2:
        spread = max(formalities) - min(formalities)
        if spread >= 4:
            scores["formality"] = 0.2
            violations.append(f"formality_spread:{spread}")
        elif spread == 3:
            scores["formality"] = 0.55
        elif spread == 2:
            scores["formality"] = 0.75

    # Layer 6: climate
    weather = intent.get("weather", "")
    avoid = intent.get("avoid_fabrics") or []
    for p in products:
        fabrics = (p.get("fashion", {}) or {}).get("fabrics", [])
        if any(f in avoid for f in fabrics):
            scores["climate"] = min(scores["climate"], 0.4)

    # Layer 12: cultural sensitivity — ethnic formals shouldn't mix with
    # random western active/casual pieces beyond known fusion formulas
    ethnic_full = {"saree", "lehenga", "sherwani", "anarkali", "kurta_set"}
    cats = {(p.get("fashion", {}) or {}).get("category", "") for p in products}
    if cats & ethnic_full and cats & {"joggers", "shorts"}:
        violations.append("cultural_mix")
        scores["cultural"] = 0.0

    # Hard floors from rules
    floors = rules()["confidence_thresholds"]["hard_score_floor"]
    passed = not violations and all(
        scores.get(k, 0) >= v for k, v in floors.items()
    )
    # pairing/occasion/cultural zeroes always fail
    if scores["garment_compatibility"] == 0 or scores["cultural"] == 0:
        passed = False
    if scores["occasion"] < floors.get("occasion", 0.35):
        passed = False

    confidence = round(sum(scores.values()) / len(scores), 3)
    thr = rules()["confidence_thresholds"]
    level = "high" if confidence >= thr["high"] else "medium" if confidence >= thr["medium"] else "low"

    return {
        "passed": passed,
        "scores": {k: round(v, 3) for k, v in scores.items()},
        "violations": violations,
        "confidence": confidence,
        "level": level,
        "dress_code": dress_code,
    }


def _garment_by_category(category: str) -> dict | None:
    """Reverse lookup: detected product category → a representative garment def."""
    garments = rules()["garments"]
    for g in garments.values():
        if category in g.get("detect_categories", []):
            return g
    return None


# ══════════════════════════════════════════════════════════════
# PRODUCT ENRICHMENT HELPERS (embellishment etc.)
# ══════════════════════════════════════════════════════════════

def detect_embellishment(title: str, patterns: list[str]) -> str:
    return _embellishment_level(title, patterns)


def style_query_terms(style: str) -> list[str]:
    if isinstance(style, (list, tuple, set)):
        style = next(iter(style), "") if style else ""
    style = str(style or "").strip().lower()
    # Aliases for wizard chips that don't exist as style_attributes keys
    aliases = {
        "western": "corporate",
        "fusion": "indo-western",
        "smart": "corporate",
        "smart casual": "corporate",
        "office": "corporate",
        "casual": "minimalist",
        "vintage": "classic",
        "preppy": "classic",
        "indian": "ethnic",
        "ethnic": "ethnic",
    }
    key = aliases.get(style, style)
    attrs = rules()["style_attributes"].get(key) or rules()["style_attributes"].get(style) or {}
    return attrs.get("query_terms", [])


def style_avoid_title(style: str) -> list[str]:
    if isinstance(style, (list, tuple, set)):
        style = next(iter(style), "") if style else ""
    style = str(style or "").strip().lower()
    aliases = {
        "western": "corporate",
        "fusion": "indo-western",
        "smart": "corporate",
        "smart casual": "corporate",
        "office": "corporate",
        "casual": "minimalist",
        "vintage": "classic",
        "preppy": "classic",
        "indian": "ethnic",
    }
    key = aliases.get(style, style)
    attrs = rules()["style_attributes"].get(key) or rules()["style_attributes"].get(style) or {}
    return attrs.get("avoid_title", [])


# ══════════════════════════════════════════════════════════════
# LOOK EXPLANATION (post-validation, can be LLM-enhanced later)
# ══════════════════════════════════════════════════════════════

def explain_blueprint(bp: dict, intent: dict) -> str:
    """Deterministic 'why this works' line for a blueprint."""
    occ = intent.get("occasion", "casual")
    dress = bp.get("dress_code", "")
    labels = [c.get("garment", "").replace("_", " ") for c in bp.get("components", [])]
    joined = " + ".join(l for l in labels if l)
    city = intent.get("city") or ""
    weather = intent.get("weather") or ""
    parts = [f"{bp.get('label', 'Look')} ({joined})"]
    if occ in ("work", "interview"):
        parts.append("keeps the silhouette clean and client-appropriate")
    elif occ in ("wedding", "festive"):
        parts.append("fits the occasion's ethnic dress code")
    elif occ == "casual":
        parts.append("easy, everyday-ready combination")
    if weather in ("hot", "humid") and city:
        parts.append(f"breathable for {city}'s climate")
    elif weather == "hot":
        parts.append("breathable fabric choice")

    # Mention Fashion DNA preference when it drove this blueprint.
    # Prefer the primary garment's culture (accessories/shoes must not flip the claim).
    wear = intent.get("wear_weights") or (intent.get("user_prefs") or {}).get("wear_weights") or {}
    if wear:
        comps = [c for c in bp.get("components", []) if c.get("culture")]
        primary = next(
            (c for c in comps if c.get("slot") in ("primary", "top") or c is comps[0]),
            comps[0] if comps else None,
        )
        cultures = [(c.get("culture") or "").lower() for c in comps]
        if primary:
            p_culture = (primary.get("culture") or "").lower()
            p_w = float(wear.get(p_culture) or 0)
            if p_culture in ("western", "indian", "indo_western") and p_w >= 0.7:
                culture_label = {
                    "western": "Western", "indian": "Indian", "indo_western": "Indo-Western",
                }.get(p_culture, p_culture)
                parts.append(f"matches your {culture_label} preference for {occ}")
            elif p_w <= 0.25 and any(float(wear.get(c) or 0) >= 0.7 for c in cultures if c != p_culture):
                # Discovery slot: intentionally outside usual preference
                parts.append("a controlled pick outside your usual style")

    return ". ".join(parts) + "."
