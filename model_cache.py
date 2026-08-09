import asyncio
from collections import deque
from datetime import datetime, timezone
from threading import Lock

import comfy.model_management


_known_models = {}
_removed_models = deque(maxlen=10)
_history_lock = Lock()
_history_task = None


def _observe_model_cache():
    current = {}
    for loaded_model in list(comfy.model_management.current_loaded_models):
        patcher = loaded_model.model
        if patcher is None:
            continue
        current[id(patcher)] = {
            "model": patcher.model.__class__.__name__,
            "patcher": patcher.__class__.__name__,
            "device": str(loaded_model.device),
        }

    with _history_lock:
        for cache_id in _known_models.keys() - current.keys():
            removed = _known_models[cache_id].copy()
            removed["removed_at"] = datetime.now(timezone.utc).isoformat()
            _removed_models.appendleft(removed)
        _known_models.clear()
        _known_models.update(current)


async def _watch_model_cache():
    _observe_model_cache()
    while True:
        await asyncio.sleep(1)
        _observe_model_cache()


def start_model_cache_history():
    global _history_task
    if _history_task is None or _history_task.done():
        _history_task = asyncio.create_task(_watch_model_cache())


def release_vram():
    """Offload active model weights while retaining their RAM-backed caches.

    ComfyUI's regular ``unload_all_models`` path fully detaches model patchers.
    Dynamic/AIMDO patchers then discard their pinned host weight pools.  Calling
    each patcher's partial-unload operation directly releases its GPU-resident
    weights without removing it from the active registry or unpinning the base
    weights that make the next load fast.
    """
    model_management = comfy.model_management
    released_models = []
    total_before = 0
    total_after = 0

    for loaded_model in list(model_management.current_loaded_models):
        patcher = loaded_model.model
        if patcher is None:
            continue

        before = int(patcher.loaded_size())
        total_before += before
        if before > 0:
            patcher.partially_unload(patcher.offload_device, before)
        after = int(patcher.loaded_size())
        total_after += after
        released_models.append({
            "model": patcher.model.__class__.__name__,
            "patcher": patcher.__class__.__name__,
            "vram_bytes_before": before,
            "vram_bytes_after": after,
            "released_bytes": max(0, before - after),
            "system_ram_bytes": (
                int(patcher.loaded_ram_size())
                if patcher.is_dynamic()
                else max(0, int(patcher.model_size()) - after)
            ),
        })

    model_management.soft_empty_cache()
    _observe_model_cache()
    return {
        "released": True,
        "models": released_models,
        "vram_bytes_before": total_before,
        "vram_bytes_after": total_after,
        "released_bytes": max(0, total_before - total_after),
    }


def get_model_cache_info():
    model_management = comfy.model_management
    cpu_device = model_management.torch.device("cpu")
    cached_models = []
    vram_cache = {}
    system_ram_cache = 0

    for loaded_model in list(model_management.current_loaded_models):
        patcher = loaded_model.model
        if patcher is None:
            continue

        device = patcher.current_loaded_device()
        total_bytes = int(patcher.model_size())
        vram_bytes = int(patcher.loaded_size()) if device.type not in ("cpu", "mps") else 0
        system_ram_bytes = int(patcher.loaded_ram_size()) if patcher.is_dynamic() else total_bytes - vram_bytes
        system_ram_cache += system_ram_bytes
        if vram_bytes:
            vram_cache[str(device)] = vram_cache.get(str(device), 0) + vram_bytes

        cached_models.append({
            "model": patcher.model.__class__.__name__,
            "patcher": patcher.__class__.__name__,
            "device": str(device),
            "dynamic": patcher.is_dynamic(),
            "total_weight_bytes": total_bytes,
            "vram_bytes": vram_bytes,
            "system_ram_bytes": system_ram_bytes,
        })

    vram = []
    reserved_bytes = int(model_management.extra_reserved_memory())
    for device in model_management.get_all_torch_devices():
        if device.type in ("cpu", "mps"):
            continue
        available_bytes = int(model_management.get_free_memory(device))
        vram.append({
            "device": str(device),
            "name": model_management.get_torch_device_name(device),
            "total_bytes": int(model_management.get_total_memory(device)),
            "available_bytes": available_bytes,
            "available_for_model_cache_bytes": max(0, available_bytes - reserved_bytes),
            "reserved_bytes": reserved_bytes,
            "cached_model_bytes": vram_cache.get(str(device), 0),
        })

    with _history_lock:
        removed_models = list(_removed_models)

    return {
        "models": cached_models,
        "removed_models": removed_models,
        "system_ram": {
            "total_bytes": int(model_management.get_total_memory(cpu_device)),
            "available_bytes": int(model_management.get_free_memory(cpu_device)),
            "cached_model_bytes": system_ram_cache,
        },
        "vram": vram,
    }
