import asyncio
import logging
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timezone
from threading import Lock

import comfy.model_management
import comfy.model_patcher
import comfy_execution.caching
import execution


_known_models = {}
_removed_models = deque(maxlen=10)
_history_lock = Lock()
_history_task = None
_pinned_models = {}
_pinned_model_keys = frozenset()
_pin_lock = Lock()
_pinning_hooks_installed = False
_original_free_memory = None
_original_models_for_pin_eviction = None
_original_ram_release = None
_original_executor_reset = None


def _model_key(patcher):
    return getattr(patcher, "clone_base_uuid", id(patcher))


def _is_model_pinned(patcher):
    return _model_key(patcher) in _pinned_model_keys


def _update_pinned_model_references(patchers):
    with _pin_lock:
        for patcher in patchers:
            key = _model_key(patcher)
            if key in _pinned_models:
                _pinned_models[key] = patcher


def set_model_pinned(cache_id, pinned):
    global _pinned_model_keys

    if not isinstance(cache_id, str) or not cache_id.isdecimal():
        raise ValueError("cache_id must be a model id string")
    if not isinstance(pinned, bool):
        raise ValueError("pinned must be a boolean")

    patcher = None
    for loaded_model in list(comfy.model_management.current_loaded_models):
        candidate = loaded_model.model
        if candidate is not None and str(id(candidate)) == cache_id:
            patcher = candidate
            break
    if patcher is None:
        raise LookupError("model is no longer in the active registry")

    key = _model_key(patcher)
    with _pin_lock:
        if pinned:
            _pinned_models[key] = patcher
        else:
            _pinned_models.pop(key, None)
        _pinned_model_keys = frozenset(_pinned_models)

    _observe_model_cache()
    return {
        "cache_id": cache_id,
        "model": patcher.model.__class__.__name__,
        "pinned": pinned,
    }


def _value_contains_pinned_model(value):
    if isinstance(value, Mapping):
        return any(_value_contains_pinned_model(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_value_contains_pinned_model(item) for item in value)

    patcher = value if isinstance(value, comfy.model_patcher.ModelPatcher) else getattr(value, "patcher", None)
    if not isinstance(patcher, comfy.model_patcher.ModelPatcher):
        return False
    if _is_model_pinned(patcher):
        return True
    return any(
        _is_model_pinned(model)
        for model in patcher.model_patches_models() + patcher.get_nested_additional_models()
    )


def _cache_entry_contains_pinned_model(cache_entry):
    return _value_contains_pinned_model(getattr(cache_entry, "outputs", cache_entry))


def _free_memory_with_pins(memory_required, device, keep_loaded=[], for_dynamic=False, pins_required=0, ram_required=0, retain_ram_cache=False):
    protected = list(keep_loaded)
    for loaded_model in list(comfy.model_management.current_loaded_models):
        patcher = loaded_model.model
        if patcher is None or not _is_model_pinned(patcher):
            continue
        if device is not None and loaded_model.device != device:
            continue
        if loaded_model in protected:
            continue

        loaded_size = int(patcher.loaded_size())
        if loaded_size > 0:
            patcher.partially_unload(patcher.offload_device, loaded_size)
        protected.append(loaded_model)

    return _original_free_memory(
        memory_required,
        device,
        keep_loaded=protected,
        for_dynamic=for_dynamic,
        pins_required=pins_required,
        ram_required=ram_required,
        retain_ram_cache=retain_ram_cache,
    )


def _models_for_pin_eviction_without_pinned(active, current_prompt=None):
    for patcher in _original_models_for_pin_eviction(active, current_prompt=current_prompt):
        if not _is_model_pinned(patcher):
            yield patcher


def _ram_release_without_pinned(self, target, free_active=False, min_entry_size=0):
    protected = {
        key: cache_entry
        for key, cache_entry in self.cache.items()
        if _cache_entry_contains_pinned_model(cache_entry)
    }
    for key in protected:
        self.cache.pop(key)
    try:
        return _original_ram_release(self, target, free_active=free_active, min_entry_size=min_entry_size)
    finally:
        self.cache.update(protected)


def _remove_cache_key_metadata(cache, key):
    for attribute in ("used_generation", "timestamps", "children"):
        values = getattr(cache, attribute, None)
        if values is not None:
            values.pop(key, None)


def _retain_pinned_cache_entries(cache):
    values = getattr(cache, "cache", None)
    if values is None:
        return False

    retained = False
    for key, cache_entry in list(values.items()):
        if _cache_entry_contains_pinned_model(cache_entry):
            retained = True
        else:
            values.pop(key)
            _remove_cache_key_metadata(cache, key)

    subcaches = getattr(cache, "subcaches", {})
    for key, subcache in list(subcaches.items()):
        if _retain_pinned_cache_entries(subcache):
            retained = True
        else:
            subcaches.pop(key)
    return retained


def _clear_cache_entries(cache):
    values = getattr(cache, "cache", None)
    if values is not None:
        values.clear()
    for attribute in ("used_generation", "timestamps", "children"):
        metadata = getattr(cache, attribute, None)
        if metadata is not None:
            metadata.clear()
    subcaches = getattr(cache, "subcaches", None)
    if subcaches is not None:
        subcaches.clear()


def _executor_reset_with_pins(self):
    if not _pinned_model_keys or not hasattr(self, "caches"):
        return _original_executor_reset(self)

    retained = _retain_pinned_cache_entries(self.caches.outputs)
    _clear_cache_entries(self.caches.objects)
    if not retained:
        return _original_executor_reset(self)

    self.status_messages = []
    self.success = True
    logging.info("Cleared execution cache while retaining pinned model outputs.")


def install_model_pinning_hooks():
    global _pinning_hooks_installed
    global _original_free_memory
    global _original_models_for_pin_eviction
    global _original_ram_release
    global _original_executor_reset

    if _pinning_hooks_installed:
        return

    model_management = comfy.model_management
    _original_free_memory = model_management.free_memory
    _original_models_for_pin_eviction = model_management.models_for_pin_eviction
    _original_ram_release = comfy_execution.caching.RAMPressureCache.ram_release
    _original_executor_reset = execution.PromptExecutor.reset

    model_management.free_memory = _free_memory_with_pins
    model_management.models_for_pin_eviction = _models_for_pin_eviction_without_pinned
    comfy_execution.caching.RAMPressureCache.ram_release = _ram_release_without_pinned
    execution.PromptExecutor.reset = _executor_reset_with_pins
    _pinning_hooks_installed = True


def _observe_model_cache():
    current = {}
    patchers = []
    for loaded_model in list(comfy.model_management.current_loaded_models):
        patcher = loaded_model.model
        if patcher is None:
            continue
        patchers.append(patcher)
        current[id(patcher)] = {
            "model": patcher.model.__class__.__name__,
            "patcher": patcher.__class__.__name__,
            "device": str(loaded_model.device),
            "pinned": _is_model_pinned(patcher),
        }

    _update_pinned_model_references(patchers)

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
    pinned_model_bytes = 0

    for loaded_model in list(model_management.current_loaded_models):
        patcher = loaded_model.model
        if patcher is None:
            continue

        device = patcher.current_loaded_device()
        total_bytes = int(patcher.model_size())
        vram_bytes = int(patcher.loaded_size()) if device.type not in ("cpu", "mps") else 0
        system_ram_bytes = int(patcher.loaded_ram_size()) if patcher.is_dynamic() else total_bytes - vram_bytes
        system_ram_cache += system_ram_bytes
        pinned = _is_model_pinned(patcher)
        if pinned:
            pinned_model_bytes += system_ram_bytes
        if vram_bytes:
            vram_cache[str(device)] = vram_cache.get(str(device), 0) + vram_bytes

        cached_models.append({
            "cache_id": str(id(patcher)),
            "model": patcher.model.__class__.__name__,
            "patcher": patcher.__class__.__name__,
            "device": str(device),
            "dynamic": patcher.is_dynamic(),
            "pinned": pinned,
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
            "pinned_model_bytes": pinned_model_bytes,
        },
        "vram": vram,
    }
