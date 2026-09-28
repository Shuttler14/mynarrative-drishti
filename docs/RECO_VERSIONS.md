# Recommender Version History (RECO-v*)

Single source of truth for recommender releases. The live version is exposed at
`GET /api/reco/version` and stamped on every `/api/reco/outfits` response as
`reco_version` (registry: `api/services/reco_version.py`).

## Versioning protocol

| Field | Rule |
|---|---|
| **major** | Architecture / source-of-truth change (vector → marketplace → intent engine) |
| **minor** | Rule / gate / behaviour change — **must pass `python bench/run_bench.py gates` before ship** |
| **patch** | Non-behavioural (logging, refactor) — no gate requirement |
| **Companion versions** | `EXTRACT-v*` (garment band-crop), `FASHION_RULES-v*` (outfit grammar JSON) |

Every release: (1) bump `RECO_VERSION` in `api/services/reco_version.py`,
(2) add a row here with commit hash, (3) run the benchmark gates,
(4) save the scorecard (`bench/results/scorecard_v*.json`) for v→v deltas.

## Release log

| Version | Released | Name | Key changes | Commits |
|---|---|---|---|---|
| **RECO-v1.0** | 2026-06-13 → 06-14 | Vector Catalog Reco | Shopify products → CLIP embeddings → Qdrant vector search; first real recommendations | `c024fb6`, `adc409f`, `093b5f9`, `5e87ced` |
| **RECO-v2.0** | 2026-07-03 | Marketplace-First Search | Dropped 222-product Shopify catalog as primary; direct Amazon/Myntra/Flipkart/AJIO/Nykaa scrapers, occasion→query mapping, parallel + retry + bot-detection, Flipkart JSON parser | `9e4741e`, `9e6db9d`, `64b6005`, `b902809`, `d80253f`, `f5f6891` |
| **RECO-v3.0** | 2026-07-04 | Google Shopping + Scrapers | SerpApi Google Shopping as primary (20 products / 5 platforms), scrapers demoted to fallback, 16s end-to-end, price comparison | `92f9399`*→`92f9397`, `ece193c` |
| **RECO-v3.1** | 2026-07-21 → 07-26 | Production Hardening | Config externalization, gender-aware pricing, Google thumbnail proxying to data URIs so VTON can consume images | `90554d5`, `a9584dc` |
| **RECO-v4.0** | 2026-09-20 | Fashion DNA + Intelligence | `fashion_engine.py` intent system (`build_intent`, scoring, `hard_filter`), `fashion_rules_v1.json` outfit grammar, price intelligence, weather/geolocation, Fashion DNA soft prefs, pinned VTON | `99c20ec` |
| **RECO-v4.1** | 2026-09-24 | Office Diversity + Slot Fixes | Office outfit diversification (no more shirts-only), skip bottom slot when full-outfit (saree/dress/kurta set) selected | `2c802f5`, `e481b59` |
| **RECO-v4.2** | 2026-09-25 | Formal-Only Gate + Stability | Office/western formal-only gate (`_office_western_strict` + `_office_formal_required` + `_OFFICE_FORMAL_SIGNALS`), scraper 6s/source + 10/15s deadlines, office-office rescue, **first benchmarked release (this file + `bench/`)** | `9832a87` |
| **RECO-v4.3** | 2026-09-27 | Slot Coverage + Image Gate | `_fill_missing_slots` (gate-starved top/bottom top-up, formal-only — office never relaxes), slot-coverage re-scrape retry (slot + thin-count), imageless-product gate in `recommend_products`, office formal enforced through fill/rescue/degrade (empty beats ethnic-casual; footwear-only office draws ship empty), office golden-contract tests + work-outfit 24-combo variation matrix | `d217d34`, `6dd9fae`, `692afa0`, `42d2764`, `4fcc7ef`, `c9d7929` |
| **RECO-v4.4** | 2026-09-28 | Look Assembly + See Variations | `look_assembly.py` (HARD→RELEVANCE→DIVERSITY: archetype blueprint owns a look, products unique across looks, duplicate skeletons dropped — never forced to 4), `/api/reco/outfits` returns `looks[].products` (backend-grouped outfits, flat list kept as union for compat), `POST /api/reco/variations` (anchor archetype + colour/fabric rotation, parent products/titles excluded), blueprint→query id map fixed for `_office_formalize` rewrites, archetype-level personalization (`users.style_profile.archetype_weights` fed by `variation_request`/`look_save`/`vton_completed`/`purchase`, soft 0.35 re-rank in `get_archetypes`), wizard `?v=5.4.0`: renders backend looks, ✦ See Variations button, archetype ids on all look events, TEST1–6 spec suite | `f6e132a`, `e359700`, `a367f5b` |

\* typo guard: v3.0 commits are `92f9397` (SerpApi) and `ece193c` (16s reco + pricing).

## Companion pipeline versions

| Version | Released | Name | Changes | Commits |
|---|---|---|---|---|
| EXTRACT-v1 | 2026-07-02 | rembg IS-Net pipeline | Local rembg + R2 upload + VTON integration | `be69487`, `544b77f`, `d198ddc` |
| EXTRACT-v2 | 2026-07-02 | Replicate rembg | Local inference replaced by hosted silueta/rembg | `51814f5`, `51509cf` |
| **EXTRACT-b3** | 2026-09-25 | Boundary band-crop | Median-luminance boundary detection, framing detection, feet rule — VTON receives **only the target garment**; presigned R2 URLs | `9832a87` |
| EXTRACT-b4 | 2026-09-27 | Segment bottom-pick | Single-boundary scan inverted on mid-framed shots (hem = first change inside pants → shoes shipped); segment-based pick, coordinate fix, full-framing fallback | `d0238d3` |
| **EXTRACT-b5** | 2026-09-27 | Head-aware bottom pick | b4 picked head+kurta as "bottom" on waist-boundary-less listing shots (pale kurta, bare legs); head zone now ineligible, headless frames keep upper-half rule, no candidate → keep whole silhouette | `d217d34` |
| FASHION_RULES-v1 | 2026-09-20 | Outfit grammar v1 | Hard exclusions, occasion gates, garment never_with, dress codes | `99c20ec`, `9832a87` |
| **FASHION_RULES-v1.1** | 2026-09-28 | 4-blueprint plan + light components | `query_compiler.blueprint_plan` gains `blueprint: 3` (4th slot query per archetype), light blueprints carry `components` (assembly fills them per slot) — companion to RECO-v4.4 look assembly | `f6e132a`, `a367f5b` |

## How each version was judged

- **v1.0–v3.1**: manual spot checks only (no automated gates existed).
- **v4.0–v4.1**: local pytest suites (ethnic/agnostic/retry) + manual QA.
- **v4.2 onwards**: full benchmark required —
  `python bench/run_bench.py gates` (offline accuracy + engine latency),
  `python bench/run_bench.py live` (prod P95 latency + response validity),
  `python bench/run_bench.py vton` (extract/try-on latency + success),
  scorecard delta vs previous version in `bench/results/`.

## Baseline scorecard — RECO-v4.2 (2026-09-25, first benchmarked release)

Run: `python bench/run_bench.py release` → 10/12 hard gates PASS.

| Gate | Actual | Verdict |
|---|---|---|
| Hard-rule violations ≤1% | 0.0% (0/39 rejects) | PASS |
| Outfit validity ≥99% | 100% (14 golden outfits) | PASS |
| Occasion accuracy ≥98% | 100% (32 intent cases) | PASS |
| Intent / validation latency ≤500ms p95 | 0.003 / 0.027 ms | PASS |
| Reco error rate ≤1% | 0% (6/6 live) | PASS |
| Live outfit validity ≥99% | 100% (independent re-validation) | PASS |
| Extraction success ≥98% | 100% (5/5) | PASS |
| VTON failure ≤1% | 0% (5/5) | PASS |
| **Reco P95 ≤4s** | **7840ms** | **FAIL** — retrieval (Google/scraper) dominates |
| **E2E VTON P95 ≤10s** | **44167ms** | **FAIL** — extract ~10s + Replicate IDM-VTON queue 19–33s |
| Garment leakage ≤2% | n/a | NO-DATA — needs human labels (`bench/golden/vton_cases.json`) |

Personalization baseline (prod events, tiny n): impressions 2, saves 1 (50%),
vton_completed 0 (event not yet fired by widget), purchases 0.
