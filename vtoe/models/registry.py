from __future__ import annotations
import threading
from collections.abc import Callable

from vtoe.utils.memory import empty_cache, vram_gb


class ModelRegistry:
    STICKY = {"idm", "cat"}

    def __init__(self, budget_gb: float) -> None:
        self.budget = budget_gb
        self._models: dict[str, object] = {}
        self._factories: dict[str, Callable] = {}
        self._lock = threading.Lock()

    def register(self, name: str, factory: Callable) -> None:
        self._factories[name] = factory

    def get(self, name: str):
        with self._lock:
            if name in self._models:
                return self._models[name]
            self._make_room(name)
            self._models[name] = self._factories[name]()
            return self._models[name]

    def _make_room(self, incoming: str) -> None:
        used, total = vram_gb()
        if used < self.budget * 0.7:
            return
        for name in list(self._models):
            if name == incoming:
                continue
            if name not in self.STICKY:
                self._evict(name)
                if vram_gb()[0] < self.budget * 0.6:
                    return
        for name in list(self._models):
            if name != incoming:
                self._evict(name)

    def _evict(self, name: str) -> None:
        model = self._models.pop(name, None)
        try:
            model.to("cpu")
        except Exception:
            pass
        del model
        empty_cache()

    def evict_all_except(self, keep: set[str]) -> None:
        with self._lock:
            for name in list(self._models):
                if name not in keep:
                    self._evict(name)
