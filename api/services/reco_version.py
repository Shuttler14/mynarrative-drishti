"""Recommender version registry — single source of truth for the scorecard.

Bump protocol (see docs/RECO_VERSIONS.md):
  major — architecture/source change (e.g. vector → marketplace → intent engine)
  minor — rule/gate/behaviour change (must pass bench/gates before ship)
  patch — non-behavioural (logging, refactor) — does NOT bump for gates
Every bump: update RECO_VERSIONS.md, run `python bench/run_bench.py gates`,
save the scorecard under the new version.
"""

RECO_VERSION = "4.5"
RECO_NAME = "Coverage Interleave + Look Completeness + Diagnostics"
RECO_RELEASED = "2026-09-28"

# Companion pipeline versions (reported alongside, versioned independently)
EXTRACT_VERSION = "b5"      # garment band-crop (api/services/garment_extract.py)
FASHION_RULES_VERSION = "v1.1"  # api/data/fashion_rules_v1.json


def version_info() -> dict:
    return {
        "reco_version": RECO_VERSION,
        "name": RECO_NAME,
        "released": RECO_RELEASED,
        "extract_version": EXTRACT_VERSION,
        "fashion_rules": FASHION_RULES_VERSION,
    }
