from __future__ import annotations
import gc
import contextlib

import torch


def vram_gb() -> tuple[float, float]:
    if not torch.cuda.is_available():
        return 0.0, 0.0
    free, total = torch.cuda.mem_get_info()
    return (total - free) / 1e9, total / 1e9


def empty_cache() -> None:
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.ipc_collect()


@contextlib.contextmanager
def vram_guard(stage: str):
    used_before, total = vram_gb()
    try:
        yield
    except torch.cuda.OutOfMemoryError as e:
        empty_cache()
        raise VRAMError(stage, str(e)) from e
    finally:
        empty_cache()


class VRAMError(RuntimeError):
    def __init__(self, stage: str, msg: str) -> None:
        super().__init__(f"OOM during {stage}: {msg}")
        self.stage = stage
