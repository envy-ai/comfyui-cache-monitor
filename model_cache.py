import asyncio
import logging
import weakref
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timezone
from threading import Condition, Lock

import psutil

import comfy.model_management
import comfy.model_patcher
import comfy_execution.caching
import execution
from comfy_execution.utils import get_executing_context


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
_executor_ref = None
_vram_wait_condition = Condition()
_vram_wait_enabled = False
_vram_wait_status = None


def get_vram_wait_info():
    with _vram_wait_condition:
        info = {
            "enabled": _vram_wait_enabled,
            "waiting": _vram_wait_status is not None,
        }
        if _vram_wait_status is not None:
            info.update(_vram_wait_status)
        return info


def set_vram_wait_enabled(enabled):
    global _vram_wait_enabled
    global _vram_wait_status

    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")

    with _vram_wait_condition:
        _vram_wait_enabled = enabled
        if not enabled:
            _vram_wait_status = None
        _vram_wait_condition.notify_all()
    return get_vram_wait_info()


def _aimdo_vram_bytes(device):
    total_bytes = 0
    seen_vbars = set()
    for loaded_model in list(comfy.model_management.current_loaded_models):
        patcher = loaded_model.model
        if loaded_model.device != device or not isinstance(patcher, comfy.model_patcher.ModelPatcherDynamic):
            continue
        vbar = patcher._vbar_get()
        if vbar is None or id(vbar) in seen_vbars:
            continue
        seen_vbars.add(id(vbar))
        total_bytes += int(vbar.loaded_size())

    seen_buffers = set()
    for buffer in comfy.model_management.STREAM_AIMDO_CAST_BUFFERS.values():
        if buffer.device != device.index or id(buffer) in seen_buffers:
            continue
        seen_buffers.add(id(buffer))
        total_bytes += int(buffer.size())
    return total_bytes


def _vram_memory_info(device):
    total_bytes, torch_reserved_bytes = comfy.model_management.get_total_memory(device, torch_total_too=True)
    available_bytes, torch_available_bytes = comfy.model_management.get_free_memory(device, torch_free_too=True)
    driver_available_bytes = max(0, available_bytes - torch_available_bytes)
    external_bytes = max(0, total_bytes - driver_available_bytes - torch_reserved_bytes - _aimdo_vram_bytes(device))
    return int(total_bytes), int(available_bytes), int(external_bytes)


def _wait_for_required_vram(memory_required, device):
    global _vram_wait_status

    context = get_executing_context()
    device_type = getattr(device, "type", None)
    with _vram_wait_condition:
        enabled = _vram_wait_enabled
    if not enabled or context is None or device is None or device_type in (None, "cpu", "mps"):
        return

    total_bytes, available_bytes, external_bytes = _vram_memory_info(device)
    required_bytes = int(memory_required)
    shortfall_bytes = required_bytes - available_bytes
    if shortfall_bytes <= 0 or required_bytes > total_bytes or external_bytes < shortfall_bytes:
        return

    logging.info(
        "Waiting for external VRAM on %s: %.1f GiB available, %.1f GiB required.",
        device,
        available_bytes / 1024 ** 3,
        required_bytes / 1024 ** 3,
    )
    resumed = False
    try:
        while available_bytes < required_bytes:
            comfy.model_management.throw_exception_if_processing_interrupted()
            with _vram_wait_condition:
                if not _vram_wait_enabled:
                    return
                _vram_wait_status = {
                    "prompt_id": context.prompt_id,
                    "node_id": context.node_id,
                    "device": str(device),
                    "required_bytes": required_bytes,
                    "available_bytes": available_bytes,
                    "external_bytes": external_bytes,
                }
                _vram_wait_condition.wait(timeout=1.0)
            total_bytes, available_bytes, external_bytes = _vram_memory_info(device)
        resumed = True
    finally:
        with _vram_wait_condition:
            _vram_wait_status = None

    if resumed:
        logging.info("Required VRAM is available on %s; resuming prompt.", device)


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
    active = False
    for loaded_model in list(comfy.model_management.current_loaded_models):
        candidate = loaded_model.model
        if candidate is not None and str(id(candidate)) == cache_id:
            patcher = candidate
            active = True
            break
    if patcher is None:
        with _pin_lock:
            patcher = next(
                (candidate for candidate in _pinned_models.values() if str(id(candidate)) == cache_id),
                None,
            )
    if patcher is None:
        raise LookupError("model is no longer retained")

    key = _model_key(patcher)
    with _pin_lock:
        if pinned:
            _pinned_models[key] = patcher
        else:
            _pinned_models.pop(key, None)
        _pinned_model_keys = frozenset(_pinned_models)

    released_ram_bytes = 0
    released_vram_bytes = 0
    if not pinned and not active:
        active_keys = {
            _model_key(loaded_model.model)
            for loaded_model in list(comfy.model_management.current_loaded_models)
            if loaded_model.model is not None
        }
        if key not in active_keys:
            loaded_bytes = int(patcher.loaded_size())
            if loaded_bytes > 0:
                patcher.partially_unload(patcher.offload_device, loaded_bytes)
                released_vram_bytes = max(0, loaded_bytes - int(patcher.loaded_size()))
            released_ram_bytes = int(patcher.partially_unload_ram(1e30))

    _observe_model_cache()
    return {
        "cache_id": cache_id,
        "model": patcher.model.__class__.__name__,
        "pinned": pinned,
        "active": active,
        "released_ram_bytes": released_ram_bytes,
        "released_vram_bytes": released_vram_bytes,
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


def _value_contains_model_key(value, model_key):
    if isinstance(value, Mapping):
        return any(_value_contains_model_key(item, model_key) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_value_contains_model_key(item, model_key) for item in value)

    patchers = []
    patcher = value if isinstance(value, comfy.model_patcher.ModelPatcher) else getattr(value, "patcher", None)
    if isinstance(patcher, comfy.model_patcher.ModelPatcher):
        patchers.append(patcher)
    get_models = getattr(value, "get_models", None)
    if callable(get_models):
        patchers.extend(get_models())

    for patcher in patchers:
        if not isinstance(patcher, comfy.model_patcher.ModelPatcher):
            continue
        models = [patcher] + patcher.model_patches_models() + patcher.get_nested_additional_models()
        if any(_model_key(model) == model_key for model in models):
            return True
    return False


def _remove_model_cache_entries(cache, model_key):
    removed = 0
    values = getattr(cache, "cache", None)
    if values is not None:
        for key, cache_entry in list(values.items()):
            value = getattr(cache_entry, "outputs", cache_entry)
            if not _value_contains_model_key(value, model_key):
                continue
            values.pop(key)
            _remove_cache_key_metadata(cache, key)
            removed += 1

    for subcache in getattr(cache, "subcaches", {}).values():
        removed += _remove_model_cache_entries(subcache, model_key)
    return removed


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

    unloaded_models = _original_free_memory(
        memory_required,
        device,
        keep_loaded=protected,
        for_dynamic=for_dynamic,
        pins_required=pins_required,
        ram_required=ram_required,
        retain_ram_cache=retain_ram_cache,
    )
    _wait_for_required_vram(memory_required, device)
    return unloaded_models


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
    global _executor_ref

    _executor_ref = weakref.ref(self)
    if not _pinned_model_keys or not hasattr(self, "caches"):
        return _original_executor_reset(self)

    retained = _retain_pinned_cache_entries(self.caches.outputs)
    _clear_cache_entries(self.caches.objects)
    if not retained:
        return _original_executor_reset(self)

    self.status_messages = []
    self.success = True
    logging.info("Cleared execution cache while retaining pinned model outputs.")


def remove_model_from_cache(cache_id):
    global _pinned_model_keys

    if not isinstance(cache_id, str) or not cache_id.isdecimal():
        raise ValueError("cache_id must be a model id string")

    patcher = None
    for loaded_model in list(comfy.model_management.current_loaded_models):
        candidate = loaded_model.model
        if candidate is not None and str(id(candidate)) == cache_id:
            patcher = candidate
            break
    if patcher is None:
        with _pin_lock:
            patcher = next(
                (candidate for candidate in _pinned_models.values() if str(id(candidate)) == cache_id),
                None,
            )
    if patcher is None:
        raise LookupError("model is no longer cached")

    if patcher.is_dynamic():
        pin_state = patcher.model.dynamic_pins.get(patcher.load_device)
        if pin_state is not None and pin_state["current_prompt"]:
            raise RuntimeError("model is in use by the current prompt")

    executor = _executor_ref() if _executor_ref is not None else None
    if executor is None or not hasattr(executor, "caches"):
        raise RuntimeError("execution cache is not available")

    model_key = _model_key(patcher)
    model_name = patcher.model.__class__.__name__
    vram_before = int(patcher.loaded_size())
    ram_before = (
        int(patcher.loaded_ram_size())
        if patcher.is_dynamic()
        else max(0, int(patcher.model_size()) - vram_before)
    )
    cache_entries_removed = 0
    for cache in executor.caches.all:
        cache_entries_removed += _remove_model_cache_entries(cache, model_key)

    with _pin_lock:
        _pinned_models.pop(model_key, None)
        _pinned_model_keys = frozenset(_pinned_models)

    loaded_models = list(comfy.model_management.current_loaded_models)
    keep_loaded = [
        loaded_model
        for loaded_model in loaded_models
        if loaded_model.model is None or _model_key(loaded_model.model) != model_key
    ]
    target_devices = {
        loaded_model.device
        for loaded_model in loaded_models
        if loaded_model.model is not None and _model_key(loaded_model.model) == model_key
    }
    for device in target_devices:
        comfy.model_management.free_memory(1e30, device, keep_loaded=keep_loaded)

    loaded_bytes = int(patcher.loaded_size())
    if loaded_bytes > 0:
        patcher.partially_unload(patcher.offload_device, loaded_bytes)
    released_ram_bytes = int(patcher.partially_unload_ram(1e30))
    comfy.model_management.soft_empty_cache()
    _observe_model_cache()
    return {
        "removed": True,
        "cache_id": cache_id,
        "model": model_name,
        "cache_entries_removed": cache_entries_removed,
        "removed_ram_bytes": ram_before,
        "released_ram_bytes": released_ram_bytes,
        "released_vram_bytes": max(0, vram_before - int(patcher.loaded_size())),
    }


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
    tracked_models = []
    vram_cache = {}
    active_keys = set()

    for loaded_model in list(model_management.current_loaded_models):
        patcher = loaded_model.model
        if patcher is None:
            continue

        active_keys.add(_model_key(patcher))
        device = patcher.current_loaded_device()
        total_bytes = int(patcher.model_size())
        vram_bytes = int(patcher.loaded_size()) if device.type not in ("cpu", "mps") else 0
        system_ram_bytes = int(patcher.loaded_ram_size()) if patcher.is_dynamic() else total_bytes - vram_bytes
        pinned = _is_model_pinned(patcher)
        model_info = {
            "cache_id": str(id(patcher)),
            "model": patcher.model.__class__.__name__,
            "patcher": patcher.__class__.__name__,
            "device": str(device),
            "dynamic": patcher.is_dynamic(),
            "pinned": pinned,
            "active": True,
            "total_weight_bytes": total_bytes,
            "vram_bytes": vram_bytes,
            "system_ram_bytes": system_ram_bytes,
        }
        cached_models.append(model_info)
        tracked_models.append((patcher, model_info))

    with _pin_lock:
        retained_models = list(_pinned_models.items())

    for key, patcher in retained_models:
        if key in active_keys:
            continue

        device = patcher.current_loaded_device()
        total_bytes = int(patcher.model_size())
        vram_bytes = int(patcher.loaded_size()) if device.type not in ("cpu", "mps") else 0
        system_ram_bytes = int(patcher.loaded_ram_size()) if patcher.is_dynamic() else total_bytes - vram_bytes
        model_info = {
            "cache_id": str(id(patcher)),
            "model": patcher.model.__class__.__name__,
            "patcher": patcher.__class__.__name__,
            "device": str(device),
            "dynamic": patcher.is_dynamic(),
            "pinned": True,
            "active": False,
            "total_weight_bytes": total_bytes,
            "vram_bytes": vram_bytes,
            "system_ram_bytes": system_ram_bytes,
        }
        cached_models.append(model_info)
        tracked_models.append((patcher, model_info))

    model_memory = {}
    counted_vram = set()
    for patcher, model_info in tracked_models:
        owner = (id(patcher.model), str(patcher.load_device))
        memory = model_memory.setdefault(owner, {
            "bytes": model_info["system_ram_bytes"],
            "active": False,
            "pinned": False,
        })
        memory["bytes"] = max(memory["bytes"], model_info["system_ram_bytes"])
        memory["active"] = memory["active"] or model_info["active"]
        memory["pinned"] = memory["pinned"] or model_info["pinned"]

        if model_info["vram_bytes"]:
            device = model_info["device"]
            vram_owner = (owner, device)
            if vram_owner not in counted_vram:
                counted_vram.add(vram_owner)
                vram_cache[device] = vram_cache.get(device, 0) + model_info["vram_bytes"]

    active_model_bytes = sum(memory["bytes"] for memory in model_memory.values() if memory["active"])
    retained_model_bytes = sum(memory["bytes"] for memory in model_memory.values() if not memory["active"])
    pinned_model_bytes = sum(memory["bytes"] for memory in model_memory.values() if memory["pinned"])

    system_ram_cache = active_model_bytes + retained_model_bytes

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
            "active_model_bytes": active_model_bytes,
            "retained_model_bytes": retained_model_bytes,
            "pinned_model_bytes": pinned_model_bytes,
            "process_rss_bytes": int(psutil.Process().memory_info().rss),
        },
        "vram": vram,
        "vram_wait": get_vram_wait_info(),
    }
