"""Pick the best Gemma model for the detected hardware.

Rule of thumb: the highest-quality model that runs at a usable speed.
  * GPU (CUDA/Vulkan): model + vision projector + KV cache should fit in
    VRAM. A dense model may spill up to ~20% of its layers to the CPU. MoE
    models may keep their experts in system RAM while the shared layers stay
    on the GPU (only ~4B params are active per token, so this stays fast).
  * Apple Silicon: unified memory, ~65% of RAM is usable by the GPU.
  * CPU only: limited to models with <= ~4B active params per token,
    otherwise generation is too slow to be useful.
"""
import json
from dataclasses import dataclass

from . import paths
from .hardware import GB

# Memory kept free for the OS, desktop, browser and llama.cpp compute buffers.
VRAM_RESERVE_GB = 1.0
COMPUTE_OVERHEAD_GB = 0.5
RAM_USABLE_FRACTION = 0.7       # of currently *available* RAM
METAL_USABLE_FRACTION = 0.65    # of total RAM on Apple Silicon
CPU_MAX_ACTIVE_PARAMS_B = 4.5
DISK_MARGIN = 1.1               # need 10% more free disk than the download
DENSE_PARTIAL_MAX = 1.25        # dense model may be up to 25% larger than free VRAM


def load_catalog():
    return json.loads(paths.CATALOG_FILE.read_text(encoding="utf-8"))


def _gib(decimal_gb):
    return decimal_gb * 1e9 / GB


def download_gb(model):
    return model["size_gb"] + (model["mmproj"]["size_gb"] if model.get("mmproj") else 0)


@dataclass
class Candidate:
    model: dict
    fits: bool
    placement: str      # "gpu" | "gpu+cpu (MoE experts on CPU)" | "cpu" | reason it doesn't fit


def evaluate(model, hw, embedding_mem_gb, already_downloaded=False):
    weights = _gib(download_gb(model))
    need = weights + model["kv_gb"] + COMPUTE_OVERHEAD_GB
    if not already_downloaded and hw.disk_free_gb < _gib(download_gb(model)) * DISK_MARGIN:
        return Candidate(model, False, f"not enough disk space ({download_gb(model):.1f} GB needed)")

    ram_budget = hw.ram_available_gb * RAM_USABLE_FRACTION
    if hw.backend == "metal":
        budget = hw.ram_total_gb * METAL_USABLE_FRACTION - embedding_mem_gb
        return Candidate(model, need <= budget, "gpu" if need <= budget else "too large for memory")

    if hw.backend in ("cuda", "vulkan"):
        vram = hw.best_gpu.vram_gb - VRAM_RESERVE_GB - embedding_mem_gb
        if need <= vram:
            return Candidate(model, True, "gpu")
        if model["moe"] and vram >= 3 and need <= vram + ram_budget:
            return Candidate(model, True, "gpu+cpu (MoE experts on CPU)")
        # Dense model mostly (>= 80%) on the GPU: a few layers on the CPU
        # cost some speed but it stays usable.
        if need <= vram * DENSE_PARTIAL_MAX and need - vram <= ram_budget:
            return Candidate(model, True, "mostly gpu (a few layers on CPU)")
        # Dense models fall through to the CPU rule: fine for small ones.

    if model["active_params_b"] > CPU_MAX_ACTIVE_PARAMS_B:
        return Candidate(model, False, "too slow without a large enough GPU")
    if need + embedding_mem_gb > ram_budget:
        return Candidate(model, False, "too large for free RAM")
    return Candidate(model, True, "cpu")


def rank(hw, catalog, downloaded_ids=()):
    emb_mem = catalog["embedding_model"]["memory_gb"]
    candidates = [evaluate(m, hw, emb_mem, m["id"] in downloaded_ids) for m in catalog["chat_models"]]
    # Best first: fitting before non-fitting, then GPU-resident before
    # partial/CPU (speed), then quality.
    speed = {"gpu": 3, "mostly gpu (a few layers on CPU)": 2, "gpu+cpu (MoE experts on CPU)": 1, "cpu": 0}
    candidates.sort(key=lambda c: (c.fits, c.model["quality"], speed.get(c.placement, -1)), reverse=True)
    return candidates


def find_model(catalog, model_id):
    for m in catalog["chat_models"]:
        if m["id"].lower() == model_id.lower():
            return m
    return None
