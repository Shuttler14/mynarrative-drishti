"""Release gate — the offline benchmark MUST pass for the current codebase.

Any change to the recommender (agent or human) fails this test if it breaks
golden-dataset accuracy or the hard-rule gates. Live/VTON latency gates are
enforced by `python bench/run_bench.py release` before shipping.
"""
import importlib.util
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

_spec = importlib.util.spec_from_file_location("run_bench", REPO / "bench" / "run_bench.py")
run_bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_bench)


def _offline_hard_gates():
    return [g for g in run_bench.GATES["hard_gates"] if g["source"] == "offline"]


def test_offline_benchmark_is_green():
    result = run_bench.run_offline(save=False)
    metrics = result["metrics"]
    assert not result["failures"], "golden label mismatches:\n" + "\n".join(result["failures"])
    for gate in _offline_hard_gates():
        actual = metrics.get(gate["metric"])
        assert actual is not None, f"gate {gate['id']}: metric {gate['metric']} missing"
        ok = run_bench.cmp_ok(gate["cmp"], actual, gate["value"])
        assert ok, (f"HARD GATE FAILED: {gate['id']} — {gate['metric']}={actual} "
                    f"(want {gate['cmp']} {gate['value']})")


def test_version_registry_matches_golden_expectations():
    """RECO_VERSION must be defined and semver-ish; docs must mention it."""
    from api.services.reco_version import RECO_VERSION, RECO_NAME
    assert RECO_VERSION and RECO_NAME
    docs = (REPO / "docs" / "RECO_VERSIONS.md").read_text()
    assert f"v{RECO_VERSION}" in docs, (
        f"RECO_VERSION {RECO_VERSION} not documented in docs/RECO_VERSIONS.md — "
        "bump protocol: update both together")
