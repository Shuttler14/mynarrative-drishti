import pytest

from vtoe.models.registry import ModelRegistry


class Dummy:
    def __init__(self, name): self.name = name
    def to(self, dev): return self


def test_sticky_models_not_evicted_first():
    reg = ModelRegistry(budget_gb=14.5)
    for n in ["idm", "birefnet", "clip"]:
        reg.register(n, lambda n=n: Dummy(n))
    [reg.get(n) for n in ["idm", "birefnet", "clip"]]
    reg._evict("birefnet")
    assert "birefnet" not in reg._models and "idm" in reg._models


def test_evict_all_except():
    reg = ModelRegistry(budget_gb=14.5)
    for n in ["idm", "cat", "clip"]:
        reg.register(n, lambda n=n: Dummy(n))
        reg.get(n)
    reg.evict_all_except({"idm"})
    assert set(reg._models) == {"idm"}
