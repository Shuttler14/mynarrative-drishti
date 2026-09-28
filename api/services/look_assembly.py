"""Look assembly layer (RECO-v4.4).

Sits AROUND the existing recommender — never replaces it:

    candidates (already scored + hard-filtered by recommend_products)
    → outfit archetypes (existing fashion_rules blueprints)
    → per-archetype OUTFIT assembly   (relevance: _final + blueprint affinity)
    → structural diversity selection   (HARD VALIDITY → RELEVANCE → DIVERSITY)

Also provides the "See Variations" mode: clone one anchor archetype into n
variation blueprints (rotated colour/fabric), compile per-clone queries, and
assemble one look per clone — same direction, different executions.

Shared primitives (_title_key) come from fashion_engine so title-dedupe
behaviour stays identical across selection and assembly.
"""
from __future__ import annotations

import copy
import hashlib
import logging

from api.services import fashion_rules as frules
from api.services.fashion_engine import _title_key

logger = logging.getLogger("drishti.reco.looks")

# Body slots a look needs. `full` garments own both primary and bottom.
_PRIMARY_SLOTS = ("top", "full", "primary")
_BODY_COMPLETE_SLOTS = ("full",)

# Role nouns stripped from component garment ids for title matching:
# "cigarette_pants" → {"cigarette"}, "kurta_set" → {"kurta"}.
_GARMENT_ROLE_WORDS = {
    "set", "suit", "dress", "pant", "pants", "shoe", "shoes", "top", "tops",
    "with", "and", "the",
}


def _pid(p: dict) -> tuple:
    return (p.get("source", ""), p.get("product_id", ""))


def _slot_of(p: dict) -> str:
    return (p.get("fashion") or {}).get("slot") or ""


def _cat_of(p: dict) -> str:
    return (p.get("fashion") or {}).get("category") or ""


def _score_of(p: dict) -> float:
    return float(p.get("_final") or 0.0)


def _slot_wants(need: str, slot: str) -> bool:
    if need == "primary":
        return slot in _PRIMARY_SLOTS
    if need == "bottom":
        return slot == "bottom"
    if need == "shoes":
        return slot == "shoes"
    if need == "accessory":
        return slot == "accessory"
    return slot == need


def _needs_from_components(components: list[dict]) -> list[str]:
    """Derive the slots a look must fill from its archetype components."""
    slots = {(c.get("slot") or "") for c in components}
    if "full" in slots:
        return ["primary", "shoes"] if "shoes" in slots else ["primary"]
    needs: list[str] = []
    if slots & set(_PRIMARY_SLOTS):
        needs.append("primary")
    if "bottom" in slots:
        needs.append("bottom")
    if "shoes" in slots:
        needs.append("shoes")
    if "accessory" in slots:
        needs.append("accessory")
    return needs or ["primary"]


def look_signature(products: list[dict], bp: dict | None = None) -> dict:
    """Structural signature of an outfit — colours/price/brand excluded.

    Product-derived (what it actually IS):
      body  = top/full garment category (the direction's anchor)
      lower = bottom category (None when absent/full)
      feet  = shoes category
      cult  = primary garment culture (western/indian/…)
    Archetype-derived (what it is SUPPOSED to be) when a blueprint is given:
      arch, body_g/lower_g/feet_g = archetype id + component garment ids —
      differentiates e.g. cigarette-pants vs chinos, which the product
      category vocab ("trousers") merges.
    """
    sig: dict = {"body": None, "lower": None, "feet": None, "cult": None}
    for p in products:
        slot, cat = _slot_of(p), _cat_of(p)
        if slot in ("top", "full") and sig["body"] is None:
            sig["body"] = cat or None
        elif slot == "bottom" and sig["lower"] is None:
            sig["lower"] = cat or None
        elif slot == "shoes" and sig["feet"] is None:
            sig["feet"] = cat or None
        if slot in ("top", "full") and sig["cult"] is None:
            sig["cult"] = ((p.get("fashion") or {}).get("culture") or "") or None
    if bp:
        comps = {c.get("slot"): c.get("garment") for c in (bp.get("components") or [])}
        sig["arch"] = bp.get("archetype") or None
        sig["body_g"] = (comps.get("primary") or comps.get("top")
                         or comps.get("full") or sig["body"])
        sig["lower_g"] = comps.get("bottom")
        sig["feet_g"] = comps.get("shoes")
    return sig


def too_similar(a: dict, b: dict) -> bool:
    """Meaningful-difference rule (RECO-v4.4).

    Same archetype (or no archetype info): execution swaps of ONE direction
    — colour, fabric, print, same-garment product swaps — are NOT different
    looks: same body + same bottom (or an unverifiable missing bottom)
    collapses. Different top category always counts as a new direction.

    Different archetypes: they are different DIRECTIONS unless their full
    garment skeleton is identical (archetype + component garment ids +
    product categories all match) — cigarette-pants vs chinos stays two
    directions even though both products categorise as 'trousers'.
    """
    if a.get("body") != b.get("body"):
        return False
    if a.get("body") is None:
        return True
    la_, lb_ = a.get("lower"), b.get("lower")
    if a.get("arch") != b.get("arch"):
        return bool(
            la_ == lb_
            and a.get("feet") == b.get("feet")
            and a.get("body_g") == b.get("body_g")
            and a.get("lower_g") == b.get("lower_g")
            and a.get("feet_g") == b.get("feet_g")
        )
    # Same direction: the bottom alone separates executions — unless one
    # side has no bottom to compare (thin look can't prove a difference).
    if la_ is None or lb_ is None:
        return True
    return la_ == lb_


def _conflicts(sig: dict, accepted: list[dict]) -> bool:
    return any(too_similar(sig, a.get("signature") or {}) for a in accepted)


def _assemble_one(
    bp: dict,
    pool: list[dict],
    used: set,
    intent: dict,
    *,
    exclude_ids: set,
    exclude_titles: list[str],
    avoid: tuple = (),
    own_bp_id: str = "",
    strict_primary: bool = False,
) -> dict | None:
    """Build the best-scoring assignment for one blueprint.

    avoid: tuples like ("body", cat)/("lower", cat) already used by accepted
    looks — soft penalty so this look reaches for a different skeleton, while
    remaining gate-valid (relevance always outranks the penalty).

    strict_primary (variations): when the pool holds no tier0/tier1 product
    for the primary slot, DROP instead of borrowing a foreign garment — a
    "Kurta + Palazzo" variation must never ship as a blazer (initial
    discovery keeps the tier2 rescue because a top beats an empty look).
    """
    components = bp.get("components") or []
    needs = _needs_from_components(components)
    comp_by_slot = {c.get("slot"): c for c in components}
    detect_by_need: dict[str, list] = {}
    for need in needs:
        comp = comp_by_slot.get(need) or next(
            (c for c in components if c.get("slot") == need), None)
        if comp is None and need == "primary":
            comp = next((c for c in components if c.get("slot") in _PRIMARY_SLOTS), None)
        # Ordered: first entry is the garment's true category (see
        # _CATEGORY_RULES "most specific first"); later entries are the
        # shared/overlapping vocabulary that a western_shirt also claims.
        detect_by_need[need] = list((comp or {}).get("detect_categories") or [])

    assign: dict[str, dict] = {}
    body_complete = False
    for need in needs:
        if need == "bottom" and body_complete:
            continue
        comp = comp_by_slot.get(need) or next(
            (c for c in components if c.get("slot") == need), None)
        # Title words of the archetype garment, minus role nouns shared by
        # many products — used as a non-binding title-match rank bonus.
        garment_words = {
            w for w in str((comp or {}).get("garment") or "").split("_")
            if w not in _GARMENT_ROLE_WORDS
        }
        eligible = [
            p for p in pool
            if _pid(p) not in used and _pid(p) not in exclude_ids
            and _slot_wants(need, _slot_of(p))
            and p.get("image_url")
            and not any(_similar_title(p.get("title") or "", t) for t in exclude_titles)
        ]
        if not eligible:
            continue

        # Garment-level slot match (Enhancement 8) as STRICT TIERS, not a
        # bonus: a bonus can be outrun by a raw-score gap (live: office
        # blazers at _final 0.9 beat a kurta at 0.6+0.25 for WF-05's primary).
        # tier 0 = the component's true category (detect[0])
        # tier 1 = shared vocabulary (western_shirt also claims 'tshirt')
        # tier 2 = fallback — used only when the pool has nothing better
        def tier(p: dict) -> int:
            det = detect_by_need.get(need) or []
            if not det:
                return 1
            cat = _cat_of(p)
            if cat == det[0]:
                return 0
            return 1 if cat in det else 2

        def rank(p: dict) -> float:
            s = _score_of(p)
            if own_bp_id and p.get("blueprint_id") == own_bp_id:
                s += 0.06
            # Title names the archetype's exact garment ("Women Chinos Khaki"
            # for a chinos component) → non-binding tie-break within a tier.
            if garment_words and garment_words <= _title_key(p):
                s += 0.10
            # Soft structural avoid (diversity): heavy enough to prefer a
            # different skeleton when available, never enough to override
            # validity (all these products are already gate-passed).
            kind = {"primary": "body", "bottom": "lower", "shoes": "feet"}.get(need)
            if kind and (kind, _cat_of(p)) in avoid:
                s -= 0.5
            return s

        # Existing outfit pairing gate (saree × sneakers etc.) — first
        # candidate that pairs with what's already assigned wins.
        chosen = None
        for cand in sorted(eligible, key=lambda x: (tier(x), -rank(x))):
            if frules.outfit_pairing_ok(cand, list(assign.values())):
                chosen = cand
                break
        if chosen is None:
            continue
        if need == "primary" and strict_primary and tier(chosen) == 2:
            return None  # direction contract: no foreign-garment rescue
        pick = chosen
        assign[need] = pick
        used.add(_pid(pick))
        if need == "primary" and _slot_of(pick) in _BODY_COMPLETE_SLOTS:
            body_complete = True

    products = [assign[n] for n in needs if n in assign]
    if not any(_slot_of(p) in _PRIMARY_SLOTS for p in products):
        # A look without a hero cannot be shown or tried on.
        return None

    sig = look_signature(products, bp)
    scores = [_score_of(p) for p in products] or [0.0]
    return {
        "id": bp.get("id", ""),
        "outfit_id": _outfit_id(bp, products),
        "archetype_id": bp.get("archetype", ""),
        "blueprint_id": bp.get("id", ""),
        "variant": bp.get("variant", "safe"),
        "label": bp.get("label", ""),
        "why": bp.get("why") or frules.explain_blueprint(bp, intent),
        "occasion": bp.get("occasion") or intent.get("occasion", ""),
        "dress_code": bp.get("dress_code") or intent.get("dress_code", ""),
        "products": products,
        "signature": sig,
        "score": round(sum(scores) / len(scores), 4),
        "is_variation": bool(bp.get("is_variation")),
        "variation_index": int(bp.get("variation_index", -1)),
    }


def _outfit_id(bp: dict, products: list[dict]) -> str:
    key = "|".join(f"{_pid(p)[0]}:{_pid(p)[1]}" for p in products)
    digest = hashlib.md5(f"{bp.get('id', '')}|{key}".encode()).hexdigest()[:10]
    return f"{bp.get('archetype') or bp.get('id') or 'look'}-{digest}"


def _similar_title(a: str, b: str) -> bool:
    ka = _title_key({"title": a})
    kb = _title_key({"title": b})
    if not ka or not kb:
        return False
    inter = len(ka & kb)
    union = len(ka | kb)
    return bool(union) and (inter / union) > 0.85


def assemble_looks(
    candidates: list[dict],
    blueprints: list[dict],
    intent: dict,
    *,
    count: int = 4,
    exclude_product_ids=(),
    exclude_titles=(),
    enforce_diversity: bool = True,
) -> list[dict]:
    """Assemble up to `count` structurally distinct, gate-valid looks.

    - Every product in `candidates` is already hard-filtered and scored by
      the existing recommender; assembly only assigns products to archetypes.
    - Products are unique across looks (no shared trousers everywhere).
    - enforce_diversity=True (initial discovery): a look whose signature
      duplicates an accepted one is DROPPED — valid-but-identical is not a
      new direction; we never force a 4th if only 2 directions exist.
    - enforce_diversity=False (variations): same-archetype looks skip the
      structural block; uniqueness + title dedupe keep them distinct.
    """
    exclude_ids = {
        (str(x.get("source", "")), str(x.get("product_id", "")))
        if isinstance(x, dict) else None
        for x in (exclude_product_ids or [])
        if isinstance(x, dict)
    }
    ex_id_strs = {
        str(x) for x in (exclude_product_ids or []) if not isinstance(x, dict)
    }
    ex_titles = [str(t) for t in (exclude_titles or []) if t]

    pool = [
        p for p in (candidates or [])
        if p.get("image_url") and _pid(p) not in exclude_ids
        and str(p.get("product_id") or "") not in ex_id_strs
    ]
    if not pool or not blueprints:
        return []

    assembled: list[dict] = []
    used: set = set()
    strict = not enforce_diversity  # variations: archetype is the contract
    for bp in blueprints[:count]:
        look = _assemble_one(
            bp, pool, used, intent,
            exclude_ids=exclude_ids, exclude_titles=ex_titles,
            own_bp_id=bp.get("id", ""),
            strict_primary=strict,
        )
        if look:
            assembled.append(look)
        # else: pool exhausted for this archetype → skip (never ship invalid)

    if not enforce_diversity:
        assembled.sort(key=lambda l: (l.get("variation_index", 0), -l["score"]))
        return assembled[:count]

    # Diversity pass: greedy accept in relevance order. A look whose
    # skeleton duplicates an accepted one is DROPPED — two archetypes
    # wearing the same outfit ARE one direction; we never force four and
    # never relabel a look with products from a different garment family.
    ordered = sorted(assembled, key=lambda l: -l["score"])
    accepted: list[dict] = []

    def _overlaps(look: dict) -> bool:
        pids = {_pid(p) for p in look["products"]}
        return any(
            pids & {_pid(p) for p in a["products"]} for a in accepted
        )

    for look in ordered:
        if not _conflicts(look["signature"], accepted) and not _overlaps(look):
            accepted.append(look)

    accepted.sort(key=lambda l: -l["score"])
    return accepted[:count]


# ──────────────────────────────────────────────────────────────────────
# See Variations — same archetype, different execution
# ──────────────────────────────────────────────────────────────────────

def build_variation_blueprints(
    intent: dict,
    archetype_id: str,
    n: int = 4,
) -> list[dict]:
    """Clone the parent archetype into n variation blueprints.

    Colour/fabric rotate per clone (existing _pick_colour/_pick_fabric);
    garment structure — the fashion direction — is frozen by the anchor.
    """
    pool_bps = frules.build_outfit_blueprints(intent, n=8)
    if not pool_bps:
        return []
    anchor = next(
        (b for b in pool_bps if b.get("archetype") == archetype_id), None)
    if anchor is None:
        logger.info(f"Archetype {archetype_id} not in occasion pool — using nearest")
        anchor = pool_bps[0]
    variant = anchor.get("variant", "safe")
    clones: list[dict] = []
    for i in range(n):
        c = copy.deepcopy(anchor)
        c["id"] = f"{anchor['id']}~v{i}"
        c["archetype"] = anchor.get("archetype", "")
        c["variant"] = variant
        c["label"] = anchor.get("label", "")
        c["is_variation"] = True
        c["variation_index"] = i
        for ci, comp in enumerate(c.get("components") or []):
            comp["colour"] = frules._pick_colour(comp, intent, variant, i * 5 + ci)
            comp["fabric"] = frules._pick_fabric(
                comp, intent, comp.get("slot", "primary"))
        clones.append(c)
    return clones


def compile_variation_queries(
    blueprints: list[dict],
    intent: dict,
    max_queries: int = 9,
) -> list[tuple[str, str]]:
    """Per-clone slot queries with colour-aware dedupe (clones share the
    anchor's garments — plain slot::garment dedupe would collapse them).

    Writes intent['blueprint_query_map'] (query → clone blueprint id) like
    fashion_engine.build_slot_queries does for the initial-discovery path.
    """
    excludes = frules.negative_keywords(intent.get("occasion", "casual"))
    plan = (
        ["primary", "bottom", "shoes"],
        ["primary", "bottom"],
        ["primary", "bottom"],
        ["primary"],
    )
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for i, bp in enumerate(blueprints):
        if len(out) >= max_queries:
            break
        slots = plan[i] if i < len(plan) else ["primary"]
        comps = bp.get("components") or []
        comps_by_slot = {c.get("slot"): c for c in comps}
        if any(c.get("slot") == "full" for c in comps):
            slots = [s for s in slots if s != "bottom"]
        for slot in slots:
            if len(out) >= max_queries:
                break
            comp = comps_by_slot.get(slot) or next(
                (c for c in comps if c.get("slot") == slot), None)
            if not comp:
                continue
            q = frules.compile_component_query(
                comp, intent, level=0, exclude_terms=excludes)
            key = f"{slot}::{comp.get('garment', '')}::{comp.get('colour', '')}"
            if key in seen:
                continue
            seen.add(key)
            out.append((slot, q, bp["id"]))

    from api.services.fashion_engine import _office_formalize
    formalized = _office_formalize([(s, q) for s, q, _ in out], intent)
    # Order-preserving positional map (see fashion_engine.build_slot_queries):
    # formalize rewrites/appends text, so key the FINAL text → clone id here.
    bp_ids = [bp_id for _s, _q, bp_id in out]
    intent["blueprint_query_map"] = {
        q.lower(): (bp_ids[i] if i < len(bp_ids) else "")
        for i, (_s, q) in enumerate(formalized)
    }
    return formalized
