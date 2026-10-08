import asyncio
import logging
import os
import weakref
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timezone
from threading import Condition, Lock

import psutil
import torch

import comfy.model_management
import comfy.model_patcher
import comfy_execution.caching
import execution
import folder_paths
import nodes
from comfy_execution.graph_utils import is_link
from comfy_execution.utils import get_executing_context


_known_models = {}
_model_filenames = weakref.WeakKeyDictionary()
_removed_models = deque(maxlen=10)
_pinned_removal_attempts = deque(maxlen=10)
_history_lock = Lock()
_history_task = None
_pinned_models = {}
_pinned_model_keys = frozenset()
_pinned_loras = {}
_pin_lock = Lock()
_pinning_hooks_installed = False
_original_lora_load = None
_original_free_memory = None
_original_models_for_pin_eviction = None
_original_ram_release = None
_original_executor_reset = None
_original_executor_execute_async = None
_original_stage_node_execution = None
_original_complete_node_execution = None
_executor_ref = None
_execution_list_ref = None
_execution_state_lock = Lock()
_prompt_running = False
_pending_lora_invalidation = set()
_pending_model_removals = deque()
_pending_model_removal_ids = set()
_pending_model_removal_lock = Lock()
_vram_wait_condition = Condition()
_vram_wait_enabled = False
_vram_wait_status = None
_aggressive_eviction_enabled = False


def get_aggressive_eviction_info():
    return {"enabled": _aggressive_eviction_enabled}


def set_aggressive_eviction_enabled(enabled):
    global _aggressive_eviction_enabled

    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")

    _aggressive_eviction_enabled = enabled
    return get_aggressive_eviction_info()


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


def _evict_unpinned_ram_caches(memory_required, keep_loaded=()):
    protected_keys = {
        _model_key(loaded_model.model)
        for loaded_model in keep_loaded
        if loaded_model.model is not None
    }
    ram_to_free = max(0, int(memory_required) - int(psutil.virtual_memory().available))
    evicted = 0
    released_bytes = 0

    for loaded_model in reversed(list(comfy.model_management.current_loaded_models)):
        patcher = loaded_model.model
        if patcher is None or _model_key(patcher) in protected_keys or _is_model_pinned(patcher):
            continue
        if int(patcher.loaded_ram_size()) <= 0:
            continue

        released = int(patcher.partially_unload_ram(1e30))
        if released <= 0:
            continue
        evicted += 1
        released_bytes += released
        if released_bytes >= ram_to_free:
            break

    if evicted:
        _observe_model_cache()
        logging.info(
            "Evicted %d unpinned model RAM caches while waiting for external VRAM (%.1f GiB released).",
            evicted,
            released_bytes / 1024 ** 3,
        )
    return evicted, released_bytes


def _wait_for_required_vram(memory_required, device, keep_loaded=()):
    global _vram_wait_status

    context = get_executing_context()
    device_type = getattr(device, "type", None)
    with _vram_wait_condition:
        enabled = _vram_wait_enabled
    if not enabled or context is None or device is None or device_type in (None, "cpu", "mps"):
        return

    _process_pending_model_removals(_get_execution_list())
    total_bytes, available_bytes, external_bytes = _vram_memory_info(device)
    required_bytes = int(memory_required)
    shortfall_bytes = required_bytes - available_bytes
    if shortfall_bytes <= 0 or required_bytes > total_bytes or external_bytes < shortfall_bytes:
        return

    _evict_unpinned_ram_caches(required_bytes, keep_loaded=keep_loaded)
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
            if _process_pending_model_removals(_get_execution_list()):
                total_bytes, available_bytes, external_bytes = _vram_memory_info(device)
                if available_bytes >= required_bytes:
                    break
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


def _find_cached_patcher(cache_id):
    if not isinstance(cache_id, str) or not cache_id.isdecimal():
        raise ValueError("cache_id must be a model id string")

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
        raise LookupError("model is no longer cached")
    return patcher, active


def _cached_lora_loaders(cache):
    for key, value in list(getattr(cache, "cache", {}).items()):
        if isinstance(value, nodes.LoraLoader):
            yield key[0], value
    for subcache in list(getattr(cache, "subcaches", {}).values()):
        yield from _cached_lora_loaders(subcache)


def _loaded_loras():
    executor = _executor_ref() if _executor_ref is not None else None
    if executor is None or not hasattr(executor, "caches"):
        return {}
    loras = {}
    for node_id, loader in _cached_lora_loaders(executor.caches.objects):
        loaded = loader.loaded_lora
        if loaded is not None:
            loras.setdefault(loaded[0], []).append((node_id, loader, loaded))
    return loras


def _lora_bytes(loaded):
    return sum(value.numel() * value.element_size() for value in loaded[1].values() if isinstance(value, torch.Tensor))


def _invalidate_lora_outputs(node_ids):
    executor = _executor_ref() if _executor_ref is not None else None
    if executor is None or not hasattr(executor, "caches"):
        return
    cache = executor.caches.outputs
    prompt = getattr(cache, "dynprompt", None)
    if prompt is None:
        return

    affected = set(node_ids)
    changed = True
    while changed:
        changed = False
        for node_id in prompt.all_node_ids():
            if node_id in affected:
                continue
            if any(is_link(value) and value[0] in affected for value in prompt.get_node(node_id)["inputs"].values()):
                affected.add(node_id)
                changed = True

    def clear(cache):
        for node_id in affected:
            key = cache.cache_key_set.keys.get(node_id)
            if key in cache.cache:
                cache.cache.pop(key)
                _remove_cache_key_metadata(cache, key)
        for subcache in cache.subcaches.values():
            clear(subcache)

    clear(cache)


def set_lora_pinned(cache_id, pinned):
    if not isinstance(cache_id, str) or not isinstance(pinned, bool):
        raise ValueError("cache_id and pinned are required")

    loaded = _loaded_loras()
    with _pin_lock:
        current = _pinned_loras.get(cache_id)
    if cache_id not in loaded and current is None:
        raise LookupError("LoRA is no longer cached")

    if pinned and current is None:
        source = loaded[cache_id][0][2]
        pinned_copy = (cache_id, {key: value.to(device="cpu", copy=True) for key, value in source[1].items()}, source[2])
        with _pin_lock:
            _pinned_loras[cache_id] = pinned_copy
        for _, loader, _ in loaded[cache_id]:
            loader.loaded_lora = pinned_copy
    elif not pinned and current is not None:
        with _pin_lock:
            _pinned_loras.pop(cache_id, None)
        for _, loader, _ in loaded.get(cache_id, []):
            if loader.loaded_lora is current:
                loader.loaded_lora = None

    node_ids = {node_id for node_id, _, _ in loaded.get(cache_id, [])}
    if node_ids:
        with _execution_state_lock:
            if _prompt_running:
                _pending_lora_invalidation.update(node_ids)
            else:
                _invalidate_lora_outputs(node_ids)
    return {"cache_id": cache_id, "pinned": pinned, "ram_bytes": _lora_bytes(_pinned_loras[cache_id]) if pinned else 0}


def _load_lora_with_pins(self, model, clip, lora_name, strength_model, strength_clip):
    if strength_model or strength_clip:
        path = folder_paths.get_full_path_or_raise("loras", lora_name)
        with _pin_lock:
            pinned = _pinned_loras.get(path)
        if pinned is not None:
            self.loaded_lora = pinned
    return _original_lora_load(self, model, clip, lora_name, strength_model, strength_clip)


def set_model_pinned(cache_id, pinned):
    global _pinned_model_keys

    if not isinstance(pinned, bool):
        raise ValueError("pinned must be a boolean")

    patcher, active = _find_cached_patcher(cache_id)

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
    with _vram_wait_condition:
        _vram_wait_condition.notify_all()
    return {
        "cache_id": cache_id,
        "model": patcher.model.__class__.__name__,
        "pinned": pinned,
        "active": active,
        "released_ram_bytes": released_ram_bytes,
        "released_vram_bytes": released_vram_bytes,
    }


def load_model_to_vram(cache_id):
    patcher, was_active = _find_cached_patcher(cache_id)
    before = int(patcher.loaded_size())

    comfy.model_management.load_models_gpu([patcher])
    after = int(patcher.loaded_size())
    _observe_model_cache()
    return {
        "loaded": True,
        "cache_id": cache_id,
        "model": patcher.model.__class__.__name__,
        "was_active": was_active,
        "active": True,
        "vram_bytes_before": before,
        "vram_bytes_after": after,
        "loaded_vram_bytes": max(0, after - before),
        "system_ram_bytes": (
            int(patcher.loaded_ram_size())
            if patcher.is_dynamic()
            else max(0, int(patcher.model_size()) - after)
        ),
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


def _remove_model_cache_entries(cache, model_key, protect_pinned=False):
    removed = 0
    values = getattr(cache, "cache", None)
    if values is not None:
        for key, cache_entry in list(values.items()):
            value = getattr(cache_entry, "outputs", cache_entry)
            if not _value_contains_model_key(value, model_key):
                continue
            if protect_pinned and _cache_entry_contains_pinned_model(cache_entry):
                continue
            values.pop(key)
            _remove_cache_key_metadata(cache, key)
            removed += 1

    for subcache in getattr(cache, "subcaches", {}).values():
        removed += _remove_model_cache_entries(subcache, model_key, protect_pinned=protect_pinned)
    return removed


def _remember_model_patcher(patcher, patchers):
    if not isinstance(patcher, comfy.model_patcher.ModelPatcher):
        return

    key = _model_key(patcher)
    models = patchers.setdefault(key, {})
    if id(patcher) in models:
        return
    models[id(patcher)] = patcher
    for nested in patcher.model_patches_models() + patcher.get_nested_additional_models():
        _remember_model_patcher(nested, patchers)


def _collect_model_patchers(value, patchers):
    if isinstance(value, Mapping):
        for item in value.values():
            _collect_model_patchers(item, patchers)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _collect_model_patchers(item, patchers)
        return

    patcher = value if isinstance(value, comfy.model_patcher.ModelPatcher) else getattr(value, "patcher", None)
    _remember_model_patcher(patcher, patchers)
    get_models = getattr(value, "get_models", None)
    if callable(get_models):
        for model in get_models():
            _remember_model_patcher(model, patchers)


def _collect_cached_model_patchers(cache, patchers):
    for cache_entry in getattr(cache, "cache", {}).values():
        _collect_model_patchers(getattr(cache_entry, "outputs", cache_entry), patchers)
    for subcache in getattr(cache, "subcaches", {}).values():
        _collect_cached_model_patchers(subcache, patchers)


def _record_model_filename(node, cache_entry):
    if cache_entry is None:
        return
    inputs = node["inputs"]
    names = [
        os.path.basename(inputs[key])
        for key in ("ckpt_name", "unet_name", "vae_name", "clip_name", "clip_name1", "clip_name2", "clip_name3", "clip_name4", "control_net_name", "style_model_name", "gligen_name")
        if isinstance(inputs.get(key), str)
    ]
    if not names:
        return
    filename = ", ".join(names)

    def record(value):
        if isinstance(value, Mapping):
            for item in value.values():
                record(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                record(item)
        else:
            patcher = value if isinstance(value, comfy.model_patcher.ModelPatcher) else getattr(value, "patcher", None)
            if isinstance(patcher, comfy.model_patcher.ModelPatcher) and isinstance(patcher.model, torch.nn.Module):
                _model_filenames[patcher.model] = filename

    record(getattr(cache_entry, "outputs", cache_entry))


def _model_filename(patcher):
    return _model_filenames.get(patcher.model) if isinstance(patcher.model, torch.nn.Module) else None


def _execution_list_uses_model(execution_list, model_key):
    for node_cache in execution_list.execution_cache.values():
        for cache_entry in node_cache.values():
            if _value_contains_model_key(getattr(cache_entry, "outputs", cache_entry), model_key):
                return True

    for node_id in execution_list.pendingNodes:
        for value in execution_list.dynprompt.get_node(node_id)["inputs"].values():
            if not is_link(value):
                continue
            cache_entry = execution_list.output_cache.get_local(value[0])
            if cache_entry is not None and _value_contains_model_key(cache_entry.outputs, model_key):
                return True
    return False


def _current_node_uses_model(execution_list, model_key):
    active_nodes = set()
    staged_node_id = getattr(execution_list, "staged_node_id", None)
    if staged_node_id is not None:
        active_nodes.add(staged_node_id)

    blocking = getattr(execution_list, "blocking", {})
    for node_id in getattr(execution_list, "pendingNodes", {}):
        dependency_blocks = sum(node_id in blocked_nodes for blocked_nodes in blocking.values())
        if execution_list.blockCount.get(node_id, 0) > dependency_blocks:
            active_nodes.add(node_id)

    for node_id in active_nodes:
        for cache_entry in execution_list.execution_cache.get(node_id, {}).values():
            if _value_contains_model_key(getattr(cache_entry, "outputs", cache_entry), model_key):
                return True
    return False


def _get_execution_list():
    return _execution_list_ref() if _execution_list_ref is not None else None


def _evict_finished_models(execution_list=None):
    if not _aggressive_eviction_enabled:
        return 0

    executor = _executor_ref() if _executor_ref is not None else None
    caches = list(executor.caches.all) if executor is not None and hasattr(executor, "caches") else []
    if execution_list is not None and all(cache is not execution_list.output_cache for cache in caches):
        caches.append(execution_list.output_cache)

    patchers = {}
    for cache in caches:
        _collect_cached_model_patchers(cache, patchers)

    loaded_models = list(comfy.model_management.current_loaded_models)
    for loaded_model in loaded_models:
        if loaded_model.model is not None:
            _remember_model_patcher(loaded_model.model, patchers)

    evict_keys = {
        key
        for key in patchers
        if key not in _pinned_model_keys
        and (execution_list is None or not _execution_list_uses_model(execution_list, key))
    }
    if not evict_keys:
        return 0

    cache_entries_removed = 0
    for cache in caches:
        for key in evict_keys:
            cache_entries_removed += _remove_model_cache_entries(cache, key, protect_pinned=True)

    keep_loaded = [
        loaded_model
        for loaded_model in loaded_models
        if loaded_model.model is None or _model_key(loaded_model.model) not in evict_keys
    ]
    target_devices = {
        loaded_model.device
        for loaded_model in loaded_models
        if loaded_model.model is not None and _model_key(loaded_model.model) in evict_keys
    }
    free_memory = _original_free_memory or comfy.model_management.free_memory
    for device in target_devices:
        free_memory(1e30, device, keep_loaded=keep_loaded)

    for key in evict_keys:
        for patcher in patchers[key].values():
            loaded_bytes = int(patcher.loaded_size())
            if loaded_bytes > 0:
                patcher.partially_unload(patcher.offload_device, loaded_bytes)
            patcher.partially_unload_ram(1e30)

    comfy.model_management.soft_empty_cache()
    _observe_model_cache()
    logging.info(
        "Aggressively evicted %d unpinned models after their final use (%d cache entries removed).",
        len(evict_keys),
        cache_entries_removed,
    )
    return len(evict_keys)


def _run_aggressive_eviction(execution_list=None):
    try:
        _evict_finished_models(execution_list)
    except Exception:
        logging.exception("Unable to aggressively evict finished models.")


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
    _wait_for_required_vram(memory_required, device, keep_loaded=protected)
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


async def _stage_node_execution_with_cache_control(self):
    global _execution_list_ref

    _execution_list_ref = weakref.ref(self)
    return await _original_stage_node_execution(self)


def _complete_node_execution_with_eviction(self):
    node_id = self.staged_node_id
    _record_model_filename(self.dynprompt.get_node(node_id), self.output_cache.get_local(node_id))
    result = _original_complete_node_execution(self)
    _process_pending_model_removals(self)
    _run_aggressive_eviction(self)
    return result


async def _executor_execute_async_with_eviction(self, prompt, prompt_id, extra_data={}, execute_outputs=[]):
    global _executor_ref
    global _execution_list_ref
    global _prompt_running

    _executor_ref = weakref.ref(self)
    with _execution_state_lock:
        _prompt_running = True
    try:
        return await _original_executor_execute_async(self, prompt, prompt_id, extra_data, execute_outputs)
    finally:
        with _execution_state_lock:
            _process_pending_model_removals()
            _run_aggressive_eviction()
            if _pending_lora_invalidation:
                _invalidate_lora_outputs(_pending_lora_invalidation)
                _pending_lora_invalidation.clear()
            _execution_list_ref = None
            _prompt_running = False


def queue_model_removal(cache_id):
    patcher, _ = _find_cached_patcher(cache_id)
    with _pending_model_removal_lock:
        already_queued = cache_id in _pending_model_removal_ids
        if not already_queued:
            _pending_model_removals.append(cache_id)
            _pending_model_removal_ids.add(cache_id)
    with _vram_wait_condition:
        _vram_wait_condition.notify_all()
    with _execution_state_lock:
        if not _prompt_running:
            _process_pending_model_removals()
    return {
        "queued": True,
        "already_queued": already_queued,
        "cache_id": cache_id,
        "model": patcher.model.__class__.__name__,
    }


def _process_pending_model_removals(execution_list=None):
    with _pending_model_removal_lock:
        pending = list(_pending_model_removals)
        _pending_model_removals.clear()
        _pending_model_removal_ids.clear()

    removed = 0
    retry = []
    for cache_id in pending:
        try:
            patcher, _ = _find_cached_patcher(cache_id)
            if execution_list is not None and _current_node_uses_model(execution_list, _model_key(patcher)):
                retry.append(cache_id)
                continue
            _remove_model_from_cache(cache_id, allow_current_prompt=True)
            removed += 1
        except LookupError:
            continue
        except Exception:
            logging.exception("Unable to remove queued model from cache.")

    if retry:
        with _pending_model_removal_lock:
            for cache_id in retry:
                if cache_id not in _pending_model_removal_ids:
                    _pending_model_removals.append(cache_id)
                    _pending_model_removal_ids.add(cache_id)
    return removed


def _remove_model_from_cache(cache_id, allow_current_prompt=False):
    global _pinned_model_keys

    patcher, _ = _find_cached_patcher(cache_id)

    if patcher.is_dynamic():
        pin_state = patcher.model.dynamic_pins.get(patcher.load_device)
        if not allow_current_prompt and pin_state is not None and pin_state["current_prompt"]:
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
    free_memory = _original_free_memory or comfy.model_management.free_memory
    for device in target_devices:
        free_memory(1e30, device, keep_loaded=keep_loaded)

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


def remove_model_from_cache(cache_id):
    return _remove_model_from_cache(cache_id)


def install_model_pinning_hooks():
    global _pinning_hooks_installed
    global _original_free_memory
    global _original_models_for_pin_eviction
    global _original_ram_release
    global _original_executor_reset
    global _original_executor_execute_async
    global _original_stage_node_execution
    global _original_complete_node_execution
    global _original_lora_load

    if _pinning_hooks_installed:
        return

    model_management = comfy.model_management
    _original_free_memory = model_management.free_memory
    _original_models_for_pin_eviction = model_management.models_for_pin_eviction
    _original_ram_release = comfy_execution.caching.RAMPressureCache.ram_release
    _original_executor_reset = execution.PromptExecutor.reset
    _original_executor_execute_async = execution.PromptExecutor.execute_async
    _original_stage_node_execution = execution.ExecutionList.stage_node_execution
    _original_complete_node_execution = execution.ExecutionList.complete_node_execution
    _original_lora_load = nodes.LoraLoader.load_lora

    model_management.free_memory = _free_memory_with_pins
    model_management.models_for_pin_eviction = _models_for_pin_eviction_without_pinned
    comfy_execution.caching.RAMPressureCache.ram_release = _ram_release_without_pinned
    execution.PromptExecutor.reset = _executor_reset_with_pins
    execution.PromptExecutor.execute_async = _executor_execute_async_with_eviction
    execution.ExecutionList.stage_node_execution = _stage_node_execution_with_cache_control
    execution.ExecutionList.complete_node_execution = _complete_node_execution_with_eviction
    nodes.LoraLoader.load_lora = _load_lora_with_pins
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
            "_model_key": _model_key(patcher),
        }

    _update_pinned_model_references(patchers)

    with _history_lock:
        for cache_id in _known_models.keys() - current.keys():
            removed = _known_models[cache_id].copy()
            model_key = removed.pop("_model_key", None)
            observed_at = datetime.now(timezone.utc).isoformat()
            if model_key in _pinned_model_keys:
                removed["pinned"] = True
                removed["attempted_at"] = observed_at
                _pinned_removal_attempts.appendleft(removed)
            else:
                removed["removed_at"] = observed_at
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
            "filename": _model_filename(patcher),
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
            "filename": _model_filename(patcher),
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
        pinned_removal_attempts = list(_pinned_removal_attempts)

    loaded_loras = _loaded_loras()
    with _pin_lock:
        pinned_loras = dict(_pinned_loras)
    loras = []
    for path in sorted(loaded_loras.keys() | pinned_loras.keys()):
        loaded = pinned_loras.get(path) or loaded_loras[path][0][2]
        pinned = path in pinned_loras
        loras.append({
            "cache_id": path,
            "name": os.path.basename(path),
            "pinned": pinned,
            "active": path in loaded_loras,
            "total_bytes": _lora_bytes(loaded),
            "system_ram_bytes": _lora_bytes(loaded) if pinned else 0,
        })

    return {
        "models": cached_models,
        "loras": loras,
        "removed_models": removed_models,
        "pinned_removal_attempts": pinned_removal_attempts,
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
        "aggressive_eviction": get_aggressive_eviction_info(),
    }
