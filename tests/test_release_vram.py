import importlib.util
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest import mock


PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_SPEC = importlib.util.spec_from_file_location("model_cache", PACKAGE_ROOT / "model_cache.py")
model_cache = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = model_cache
MODULE_SPEC.loader.exec_module(model_cache)


class FakePatcher:
    def __init__(self, loaded_bytes, ram_bytes, dynamic=True):
        self._loaded_bytes = loaded_bytes
        self._ram_bytes = ram_bytes
        self._dynamic = dynamic
        self.clone_base_uuid = object()
        self.load_device = model_cache.comfy.model_patcher.torch.device("cuda:0")
        self.offload_device = "cpu"
        self.model = SimpleNamespace(dynamic_pins={self.load_device: {"current_prompt": False}})
        self.partial_unload_calls = []
        self.partial_unload_ram_calls = []

    def loaded_size(self):
        return self._loaded_bytes

    def partially_unload(self, device, amount):
        self.partial_unload_calls.append((device, amount))
        self._loaded_bytes = 0
        return amount

    def loaded_ram_size(self):
        return self._ram_bytes

    def partially_unload_ram(self, amount):
        self.partial_unload_ram_calls.append(amount)
        released = self._ram_bytes
        self._ram_bytes = 0
        return released

    def model_size(self):
        return self._ram_bytes + self._loaded_bytes

    def is_dynamic(self):
        return self._dynamic

    def current_loaded_device(self):
        return self.load_device

    def model_patches_models(self):
        return []

    def get_nested_additional_models(self):
        return []


class ReleaseVramTests(unittest.TestCase):
    def test_releases_vram_without_removing_or_detaching_cached_models(self):
        patcher = FakePatcher(loaded_bytes=8 * 1024, ram_bytes=32 * 1024)
        loaded_model = SimpleNamespace(model=patcher, device="cuda:0")
        active_models = [loaded_model]

        with (
            mock.patch.object(
                model_cache.comfy.model_management,
                "current_loaded_models",
                active_models,
            ),
            mock.patch.object(
                model_cache.comfy.model_management,
                "soft_empty_cache",
            ) as empty_cache,
        ):
            result = model_cache.release_vram()

        self.assertEqual(patcher.partial_unload_calls, [("cpu", 8 * 1024)])
        self.assertEqual(active_models, [loaded_model])
        self.assertEqual(result["released_bytes"], 8 * 1024)
        self.assertEqual(result["vram_bytes_after"], 0)
        self.assertEqual(result["models"][0]["system_ram_bytes"], 32 * 1024)
        empty_cache.assert_called_once_with()


class ModelPinningTests(unittest.TestCase):
    def setUp(self):
        self.original_pinned_models = model_cache._pinned_models.copy()
        self.original_pinned_model_keys = model_cache._pinned_model_keys
        self.original_executor_ref = model_cache._executor_ref
        self.original_pinned_removal_attempts = list(model_cache._pinned_removal_attempts)
        model_cache._pinned_models.clear()
        model_cache._pinned_model_keys = frozenset()
        model_cache._executor_ref = None
        model_cache._pinned_removal_attempts.clear()

    def tearDown(self):
        model_cache._pinned_models.clear()
        model_cache._pinned_models.update(self.original_pinned_models)
        model_cache._pinned_model_keys = self.original_pinned_model_keys
        model_cache._executor_ref = self.original_executor_ref
        model_cache._pinned_removal_attempts.clear()
        model_cache._pinned_removal_attempts.extend(self.original_pinned_removal_attempts)

    def pin(self, patcher):
        model_cache._pinned_models[patcher.clone_base_uuid] = patcher
        model_cache._pinned_model_keys = frozenset(model_cache._pinned_models)

    def model_patcher(self):
        torch = model_cache.comfy.model_patcher.torch
        device = torch.device("cpu")
        return model_cache.comfy.model_patcher.ModelPatcher(torch.nn.Linear(1, 1), device, device)

    def test_model_filename_follows_patcher_clones(self):
        patcher = self.model_patcher()
        entry = SimpleNamespace(outputs=[(patcher,)])

        model_cache._record_model_filename(
            {"inputs": {"unet_name": "d/example_model.safetensors"}}, entry,
        )

        self.assertEqual(model_cache._model_filenames.get(patcher.model), "example_model.safetensors")
        self.assertEqual(model_cache._model_filenames.get(patcher.clone().model), "example_model.safetensors")

    def test_pin_is_session_state_and_can_be_removed(self):
        patcher = FakePatcher(loaded_bytes=0, ram_bytes=32 * 1024)
        loaded_model = SimpleNamespace(model=patcher, device="cuda:0")

        with mock.patch.object(
            model_cache.comfy.model_management,
            "current_loaded_models",
            [loaded_model],
        ):
            result = model_cache.set_model_pinned(str(id(patcher)), True)
            self.assertTrue(result["pinned"])
            self.assertTrue(model_cache._is_model_pinned(patcher))

            result = model_cache.set_model_pinned(str(id(patcher)), False)

        self.assertFalse(result["pinned"])
        self.assertFalse(model_cache._is_model_pinned(patcher))
        self.assertEqual(patcher.partial_unload_ram_calls, [])

    def test_retained_model_can_be_unpinned_and_releases_its_memory(self):
        patcher = FakePatcher(loaded_bytes=8 * 1024, ram_bytes=32 * 1024)
        self.pin(patcher)

        with mock.patch.object(
            model_cache.comfy.model_management,
            "current_loaded_models",
            [],
        ):
            result = model_cache.set_model_pinned(str(id(patcher)), False)

        self.assertFalse(result["active"])
        self.assertEqual(result["released_vram_bytes"], 8 * 1024)
        self.assertEqual(result["released_ram_bytes"], 32 * 1024)
        self.assertEqual(patcher.partial_unload_calls, [("cpu", 8 * 1024)])
        self.assertEqual(patcher.partial_unload_ram_calls, [1e30])
        self.assertFalse(model_cache._is_model_pinned(patcher))

    def test_unpinning_wakes_an_external_vram_wait(self):
        patcher = FakePatcher(loaded_bytes=0, ram_bytes=32 * 1024)
        loaded_model = SimpleNamespace(model=patcher, device=patcher.load_device)
        self.pin(patcher)

        with (
            mock.patch.object(model_cache.comfy.model_management, "current_loaded_models", [loaded_model]),
            mock.patch.object(model_cache._vram_wait_condition, "notify_all") as notify,
        ):
            model_cache.set_model_pinned(str(id(patcher)), False)

        notify.assert_called_once_with()
        self.assertFalse(model_cache._is_model_pinned(patcher))

    def test_retained_model_can_be_loaded_back_to_vram(self):
        patcher = FakePatcher(loaded_bytes=0, ram_bytes=32 * 1024)
        self.pin(patcher)

        def load_models_gpu(models):
            self.assertEqual(models, [patcher])
            patcher._loaded_bytes = 8 * 1024

        with (
            mock.patch.object(model_cache.comfy.model_management, "current_loaded_models", []),
            mock.patch.object(model_cache.comfy.model_management, "load_models_gpu", side_effect=load_models_gpu),
        ):
            result = model_cache.load_model_to_vram(str(id(patcher)))

        self.assertFalse(result["was_active"])
        self.assertTrue(result["active"])
        self.assertEqual(result["loaded_vram_bytes"], 8 * 1024)
        self.assertEqual(result["system_ram_bytes"], 32 * 1024)

    def test_load_model_requires_a_current_cache_id(self):
        with mock.patch.object(model_cache.comfy.model_management, "current_loaded_models", []):
            with self.assertRaisesRegex(LookupError, "no longer cached"):
                model_cache.load_model_to_vram("123")

    def test_report_includes_retained_pins_and_process_rss(self):
        active = FakePatcher(loaded_bytes=0, ram_bytes=10 * 1024)
        retained = FakePatcher(loaded_bytes=0, ram_bytes=20 * 1024)
        self.pin(retained)
        loaded_model = SimpleNamespace(model=active, device=active.load_device)
        process = SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=99 * 1024))
        attempt = {
            "model": "PinnedModel",
            "patcher": "FakePatcher",
            "device": "cuda:0",
            "pinned": True,
            "attempted_at": "2026-08-19T00:00:00+00:00",
        }
        model_cache._pinned_removal_attempts.appendleft(attempt)

        with (
            mock.patch.object(
                model_cache.comfy.model_management,
                "current_loaded_models",
                [loaded_model],
            ),
            mock.patch.object(model_cache.comfy.model_management, "extra_reserved_memory", return_value=0),
            mock.patch.object(model_cache.comfy.model_management, "get_all_torch_devices", return_value=[]),
            mock.patch.object(model_cache.comfy.model_management, "get_total_memory", return_value=128 * 1024),
            mock.patch.object(model_cache.comfy.model_management, "get_free_memory", return_value=64 * 1024),
            mock.patch.object(model_cache.psutil, "Process", return_value=process),
        ):
            result = model_cache.get_model_cache_info()

        self.assertEqual([model["active"] for model in result["models"]], [True, False])
        self.assertEqual(result["system_ram"]["active_model_bytes"], 10 * 1024)
        self.assertEqual(result["system_ram"]["retained_model_bytes"], 20 * 1024)
        self.assertEqual(result["system_ram"]["cached_model_bytes"], 30 * 1024)
        self.assertEqual(result["system_ram"]["pinned_model_bytes"], 20 * 1024)
        self.assertEqual(result["system_ram"]["process_rss_bytes"], 99 * 1024)
        self.assertEqual(result["pinned_removal_attempts"], [attempt])
        self.assertEqual(result["aggressive_eviction"], {"enabled": False})
        self.assertEqual(result["loras"], [])

    def test_free_memory_offloads_pinned_vram_but_protects_ram_registry(self):
        patcher = FakePatcher(loaded_bytes=8 * 1024, ram_bytes=32 * 1024)
        loaded_model = SimpleNamespace(model=patcher, device="cuda:0")
        self.pin(patcher)
        original_free_memory = mock.Mock(return_value=["unloaded-other-model"])

        with (
            mock.patch.object(
                model_cache.comfy.model_management,
                "current_loaded_models",
                [loaded_model],
            ),
            mock.patch.object(model_cache, "_original_free_memory", original_free_memory),
            mock.patch.object(model_cache, "_wait_for_required_vram") as wait_for_vram,
        ):
            result = model_cache._free_memory_with_pins(10**12, "cuda:0")

        self.assertEqual(result, ["unloaded-other-model"])
        self.assertEqual(patcher.partial_unload_calls, [("cpu", 8 * 1024)])
        self.assertIn(loaded_model, original_free_memory.call_args.kwargs["keep_loaded"])
        wait_for_vram.assert_called_once_with(10**12, "cuda:0", keep_loaded=[loaded_model])

    def test_ram_pin_eviction_skips_pinned_models(self):
        pinned = FakePatcher(loaded_bytes=0, ram_bytes=32 * 1024)
        unpinned = FakePatcher(loaded_bytes=0, ram_bytes=16 * 1024)
        self.pin(pinned)

        with mock.patch.object(
            model_cache,
            "_original_models_for_pin_eviction",
            mock.Mock(return_value=iter([pinned, unpinned])),
        ):
            result = list(model_cache._models_for_pin_eviction_without_pinned(False))

        self.assertEqual(result, [unpinned])

    def test_ram_pressure_cache_does_not_offer_pinned_output_for_eviction(self):
        patcher = self.model_patcher()
        self.pin(patcher)
        pinned_entry = SimpleNamespace(outputs=[patcher])
        other_entry = SimpleNamespace(outputs=["image"])
        cache = SimpleNamespace(cache={"pinned": pinned_entry, "other": other_entry})

        def release(cache, target, free_active=False, min_entry_size=0):
            self.assertNotIn("pinned", cache.cache)
            cache.cache.pop("other")
            return 123

        with mock.patch.object(model_cache, "_original_ram_release", release):
            freed = model_cache._ram_release_without_pinned(cache, 1024, free_active=True)

        self.assertEqual(freed, 123)
        self.assertEqual(cache.cache, {"pinned": pinned_entry})

    def test_execution_cache_reset_keeps_only_pinned_model_outputs(self):
        patcher = self.model_patcher()
        self.pin(patcher)
        pinned_entry = SimpleNamespace(outputs=[patcher])
        other_entry = SimpleNamespace(outputs=["image"])
        outputs = SimpleNamespace(
            cache={"pinned": pinned_entry, "other": other_entry},
            subcaches={},
            used_generation={"pinned": 1, "other": 1},
            timestamps={"pinned": 1, "other": 1},
            children={},
        )
        objects = SimpleNamespace(cache={"loader": object()}, subcaches={})
        class Executor:
            pass

        executor = Executor()
        executor.caches = SimpleNamespace(outputs=outputs, objects=objects)
        executor.status_messages = ["old"]
        executor.success = False
        original_reset = mock.Mock()

        with mock.patch.object(model_cache, "_original_executor_reset", original_reset):
            model_cache._executor_reset_with_pins(executor)

        original_reset.assert_not_called()
        self.assertEqual(outputs.cache, {"pinned": pinned_entry})
        self.assertEqual(outputs.used_generation, {"pinned": 1})
        self.assertEqual(objects.cache, {})
        self.assertEqual(executor.status_messages, [])
        self.assertTrue(executor.success)

    def test_remove_model_drops_its_executor_entries_and_memory(self):
        class Executor:
            pass

        patcher = FakePatcher(loaded_bytes=8 * 1024, ram_bytes=32 * 1024)
        self.pin(patcher)
        target_entry = SimpleNamespace(outputs=[patcher])
        other_entry = SimpleNamespace(outputs=["image"])
        outputs = SimpleNamespace(
            cache={"target": target_entry, "other": other_entry},
            subcaches={},
            used_generation={"target": 1, "other": 1},
            timestamps={"target": 1, "other": 1},
            children={},
        )
        objects = SimpleNamespace(cache={}, subcaches={})
        executor = Executor()
        executor.caches = SimpleNamespace(all=[outputs, objects])
        model_cache._executor_ref = model_cache.weakref.ref(executor)
        loaded_model = SimpleNamespace(model=patcher, device=patcher.load_device)
        other_patcher = FakePatcher(loaded_bytes=4 * 1024, ram_bytes=16 * 1024)
        other_loaded_model = SimpleNamespace(model=other_patcher, device=other_patcher.load_device)

        with (
            mock.patch.object(model_cache.comfy.model_patcher, "ModelPatcher", FakePatcher),
            mock.patch.object(
                model_cache.comfy.model_management,
                "current_loaded_models",
                [loaded_model, other_loaded_model],
            ),
            mock.patch.object(model_cache.comfy.model_management, "free_memory") as free_memory,
            mock.patch.object(model_cache.comfy.model_management, "soft_empty_cache") as empty_cache,
        ):
            result = model_cache.remove_model_from_cache(str(id(patcher)))

        self.assertEqual(outputs.cache, {"other": other_entry})
        self.assertEqual(outputs.used_generation, {"other": 1})
        self.assertFalse(model_cache._is_model_pinned(patcher))
        self.assertEqual(result["cache_entries_removed"], 1)
        self.assertEqual(result["removed_ram_bytes"], 32 * 1024)
        self.assertEqual(result["released_ram_bytes"], 32 * 1024)
        self.assertEqual(result["released_vram_bytes"], 8 * 1024)
        free_memory.assert_called_once_with(1e30, patcher.load_device, keep_loaded=[other_loaded_model])
        self.assertEqual(other_patcher.partial_unload_calls, [])
        empty_cache.assert_called_once_with()

    def test_remove_model_refuses_a_model_in_use_by_current_prompt(self):
        patcher = FakePatcher(loaded_bytes=8 * 1024, ram_bytes=32 * 1024)
        patcher.model.dynamic_pins[patcher.load_device]["current_prompt"] = True
        loaded_model = SimpleNamespace(model=patcher, device=patcher.load_device)

        with mock.patch.object(model_cache.comfy.model_management, "current_loaded_models", [loaded_model]):
            with self.assertRaisesRegex(RuntimeError, "current prompt"):
                model_cache.remove_model_from_cache(str(id(patcher)))


class ModelHistoryTests(unittest.TestCase):
    def setUp(self):
        self.original_known_models = model_cache._known_models.copy()
        self.original_removed_models = list(model_cache._removed_models)
        self.original_pinned_removal_attempts = list(model_cache._pinned_removal_attempts)
        self.original_pinned_models = model_cache._pinned_models.copy()
        self.original_pinned_model_keys = model_cache._pinned_model_keys
        model_cache._known_models.clear()
        model_cache._removed_models.clear()
        model_cache._pinned_removal_attempts.clear()
        model_cache._pinned_models.clear()
        model_cache._pinned_model_keys = frozenset()

    def tearDown(self):
        model_cache._known_models.clear()
        model_cache._known_models.update(self.original_known_models)
        model_cache._removed_models.clear()
        model_cache._removed_models.extend(self.original_removed_models)
        model_cache._pinned_removal_attempts.clear()
        model_cache._pinned_removal_attempts.extend(self.original_pinned_removal_attempts)
        model_cache._pinned_models.clear()
        model_cache._pinned_models.update(self.original_pinned_models)
        model_cache._pinned_model_keys = self.original_pinned_model_keys

    def observe(self, loaded_models):
        with mock.patch.object(
            model_cache.comfy.model_management,
            "current_loaded_models",
            loaded_models,
        ):
            model_cache._observe_model_cache()

    def pin(self, patcher):
        model_cache._pinned_models[patcher.clone_base_uuid] = patcher
        model_cache._pinned_model_keys = frozenset(model_cache._pinned_models)

    def test_blocked_pinned_removal_is_not_reported_as_an_active_registry_removal(self):
        patcher = FakePatcher(loaded_bytes=0, ram_bytes=32 * 1024)
        loaded_model = SimpleNamespace(model=patcher, device=patcher.load_device)
        self.observe([loaded_model])
        self.pin(patcher)

        self.observe([])

        self.assertEqual(list(model_cache._removed_models), [])
        self.assertEqual(len(model_cache._pinned_removal_attempts), 1)
        self.assertTrue(model_cache._pinned_removal_attempts[0]["pinned"])
        self.assertIn("attempted_at", model_cache._pinned_removal_attempts[0])
        self.assertNotIn("removed_at", model_cache._pinned_removal_attempts[0])

    def test_unpinned_registry_removal_stays_in_removed_history(self):
        patcher = FakePatcher(loaded_bytes=0, ram_bytes=32 * 1024)
        loaded_model = SimpleNamespace(model=patcher, device=patcher.load_device)
        self.observe([loaded_model])

        self.observe([])

        self.assertEqual(list(model_cache._pinned_removal_attempts), [])
        self.assertEqual(len(model_cache._removed_models), 1)
        self.assertIn("removed_at", model_cache._removed_models[0])
        self.assertNotIn("attempted_at", model_cache._removed_models[0])

    def test_pinned_removal_attempt_history_keeps_only_the_last_ten(self):
        for _ in range(11):
            patcher = FakePatcher(loaded_bytes=0, ram_bytes=32 * 1024)
            loaded_model = SimpleNamespace(model=patcher, device=patcher.load_device)
            self.pin(patcher)
            self.observe([loaded_model])
            self.observe([])

        self.assertEqual(len(model_cache._pinned_removal_attempts), 10)
        self.assertEqual(list(model_cache._removed_models), [])


class QueuedRemovalTests(unittest.TestCase):
    def setUp(self):
        self.original_prompt_running = model_cache._prompt_running
        self.original_pending = list(model_cache._pending_model_removals)
        self.original_pending_ids = set(model_cache._pending_model_removal_ids)
        model_cache._prompt_running = True
        model_cache._pending_model_removals.clear()
        model_cache._pending_model_removal_ids.clear()

    def tearDown(self):
        model_cache._prompt_running = self.original_prompt_running
        model_cache._pending_model_removals.clear()
        model_cache._pending_model_removals.extend(self.original_pending)
        model_cache._pending_model_removal_ids.clear()
        model_cache._pending_model_removal_ids.update(self.original_pending_ids)

    def test_removal_waits_only_while_the_current_node_uses_the_model(self):
        patcher = FakePatcher(loaded_bytes=8 * 1024, ram_bytes=32 * 1024)
        patcher.model.dynamic_pins[patcher.load_device]["current_prompt"] = True
        loaded_model = SimpleNamespace(model=patcher, device=patcher.load_device)
        cache_entry = SimpleNamespace(outputs=[patcher])
        execution_list = SimpleNamespace(
            staged_node_id="sampler",
            execution_cache={"sampler": {"loader": cache_entry}},
        )

        with (
            mock.patch.object(model_cache.comfy.model_patcher, "ModelPatcher", FakePatcher),
            mock.patch.object(model_cache.comfy.model_management, "current_loaded_models", [loaded_model]),
            mock.patch.object(model_cache, "_remove_model_from_cache") as remove_model,
        ):
            queued = model_cache.queue_model_removal(str(id(patcher)))
            self.assertEqual(model_cache._process_pending_model_removals(execution_list), 0)
            remove_model.assert_not_called()

            execution_list.staged_node_id = None
            self.assertEqual(model_cache._process_pending_model_removals(execution_list), 1)

        self.assertTrue(queued["queued"])
        remove_model.assert_called_once_with(str(id(patcher)), allow_current_prompt=True)
        self.assertEqual(list(model_cache._pending_model_removals), [])

    def test_duplicate_removal_requests_are_coalesced(self):
        patcher = FakePatcher(loaded_bytes=8 * 1024, ram_bytes=32 * 1024)
        loaded_model = SimpleNamespace(model=patcher, device=patcher.load_device)

        with mock.patch.object(model_cache.comfy.model_management, "current_loaded_models", [loaded_model]):
            first = model_cache.queue_model_removal(str(id(patcher)))
            second = model_cache.queue_model_removal(str(id(patcher)))

        self.assertFalse(first["already_queued"])
        self.assertTrue(second["already_queued"])
        self.assertEqual(list(model_cache._pending_model_removals), [str(id(patcher))])

    def test_removal_waits_for_an_active_async_node(self):
        patcher = FakePatcher(loaded_bytes=8 * 1024, ram_bytes=32 * 1024)
        loaded_model = SimpleNamespace(model=patcher, device=patcher.load_device)
        cache_entry = SimpleNamespace(outputs=[patcher])
        execution_list = SimpleNamespace(
            staged_node_id=None,
            execution_cache={"async": {"loader": cache_entry}},
            pendingNodes={"async": True},
            blockCount={"async": 1},
            blocking={},
        )

        with (
            mock.patch.object(model_cache.comfy.model_patcher, "ModelPatcher", FakePatcher),
            mock.patch.object(model_cache.comfy.model_management, "current_loaded_models", [loaded_model]),
            mock.patch.object(model_cache, "_remove_model_from_cache") as remove_model,
        ):
            model_cache.queue_model_removal(str(id(patcher)))
            self.assertEqual(model_cache._process_pending_model_removals(execution_list), 0)
            remove_model.assert_not_called()

            execution_list.blockCount["async"] = 0
            self.assertEqual(model_cache._process_pending_model_removals(execution_list), 1)

        remove_model.assert_called_once_with(str(id(patcher)), allow_current_prompt=True)

    def test_node_completion_runs_once_then_processes_removals(self):
        execution_list = SimpleNamespace(
            staged_node_id="1",
            dynprompt=SimpleNamespace(get_node=lambda node_id: {"inputs": {}}),
            output_cache=SimpleNamespace(get_local=lambda node_id: None),
        )
        result = object()
        with (
            mock.patch.object(model_cache, "_original_complete_node_execution", return_value=result) as complete,
            mock.patch.object(model_cache, "_process_pending_model_removals") as process_removals,
            mock.patch.object(model_cache, "_run_aggressive_eviction") as aggressive_eviction,
        ):
            self.assertIs(model_cache._complete_node_execution_with_eviction(execution_list), result)

        complete.assert_called_once_with(execution_list)
        process_removals.assert_called_once_with(execution_list)
        aggressive_eviction.assert_called_once_with(execution_list)

class AggressiveEvictionTests(unittest.TestCase):
    def setUp(self):
        self.original_pinned_models = model_cache._pinned_models.copy()
        self.original_pinned_model_keys = model_cache._pinned_model_keys
        self.original_executor_ref = model_cache._executor_ref
        self.original_enabled = model_cache._aggressive_eviction_enabled
        model_cache._pinned_models.clear()
        model_cache._pinned_model_keys = frozenset()
        model_cache._executor_ref = None
        model_cache.set_aggressive_eviction_enabled(False)

    def tearDown(self):
        model_cache._pinned_models.clear()
        model_cache._pinned_models.update(self.original_pinned_models)
        model_cache._pinned_model_keys = self.original_pinned_model_keys
        model_cache._executor_ref = self.original_executor_ref
        model_cache.set_aggressive_eviction_enabled(self.original_enabled)

    def make_cache(self, patcher):
        entry = SimpleNamespace(outputs=[patcher])
        cache = SimpleNamespace(
            cache={"loader": entry},
            subcaches={},
            used_generation={"loader": 1},
            timestamps={"loader": 1},
            children={"loader": []},
        )
        cache.get_local = lambda node_id: entry if node_id == "loader" else None
        return cache, entry

    def make_execution_list(self, cache, execution_cache=None, pending_inputs=None):
        pending_inputs = pending_inputs or {}
        return SimpleNamespace(
            output_cache=cache,
            execution_cache=execution_cache or {},
            pendingNodes={node_id: True for node_id in pending_inputs},
            dynprompt=SimpleNamespace(
                get_node=lambda node_id: {"inputs": pending_inputs[node_id]},
            ),
        )

    def test_is_disabled_by_default_and_requires_a_boolean(self):
        self.assertEqual(model_cache.get_aggressive_eviction_info(), {"enabled": False})
        with self.assertRaisesRegex(ValueError, "enabled must be a boolean"):
            model_cache.set_aggressive_eviction_enabled("true")

    def test_evicts_an_unpinned_model_after_its_final_consumer(self):
        patcher = FakePatcher(loaded_bytes=8 * 1024, ram_bytes=32 * 1024)
        loaded_model = SimpleNamespace(model=patcher, device=patcher.load_device)
        cache, _ = self.make_cache(patcher)
        execution_list = self.make_execution_list(cache)
        original_free_memory = mock.Mock()
        model_cache.set_aggressive_eviction_enabled(True)

        with (
            mock.patch.object(model_cache.comfy.model_patcher, "ModelPatcher", FakePatcher),
            mock.patch.object(model_cache.comfy.model_management, "current_loaded_models", [loaded_model]),
            mock.patch.object(model_cache, "_original_free_memory", original_free_memory),
            mock.patch.object(model_cache.comfy.model_management, "soft_empty_cache") as empty_cache,
            mock.patch.object(model_cache, "_observe_model_cache") as observe,
        ):
            evicted = model_cache._evict_finished_models(execution_list)

        self.assertEqual(evicted, 1)
        self.assertEqual(cache.cache, {})
        self.assertEqual(cache.used_generation, {})
        self.assertEqual(cache.timestamps, {})
        self.assertEqual(cache.children, {})
        original_free_memory.assert_called_once_with(1e30, patcher.load_device, keep_loaded=[])
        self.assertEqual(patcher.partial_unload_calls, [("cpu", 8 * 1024)])
        self.assertEqual(patcher.partial_unload_ram_calls, [1e30])
        empty_cache.assert_called_once_with()
        observe.assert_called_once_with()

    def test_keeps_a_model_needed_by_a_pending_consumer(self):
        patcher = FakePatcher(loaded_bytes=8 * 1024, ram_bytes=32 * 1024)
        loaded_model = SimpleNamespace(model=patcher, device=patcher.load_device)
        cache, entry = self.make_cache(patcher)
        execution_list = self.make_execution_list(
            cache,
            execution_cache={"future": {"loader": entry}},
        )
        model_cache.set_aggressive_eviction_enabled(True)

        with (
            mock.patch.object(model_cache.comfy.model_patcher, "ModelPatcher", FakePatcher),
            mock.patch.object(model_cache.comfy.model_management, "current_loaded_models", [loaded_model]),
            mock.patch.object(model_cache, "_original_free_memory") as original_free_memory,
        ):
            evicted = model_cache._evict_finished_models(execution_list)

        self.assertEqual(evicted, 0)
        self.assertEqual(cache.cache, {"loader": entry})
        original_free_memory.assert_not_called()

    def test_keeps_a_model_available_to_a_pending_lazy_input(self):
        patcher = FakePatcher(loaded_bytes=8 * 1024, ram_bytes=32 * 1024)
        loaded_model = SimpleNamespace(model=patcher, device=patcher.load_device)
        cache, entry = self.make_cache(patcher)
        execution_list = self.make_execution_list(
            cache,
            pending_inputs={"future": {"model": ["loader", 0]}},
        )
        model_cache.set_aggressive_eviction_enabled(True)

        with (
            mock.patch.object(model_cache.comfy.model_patcher, "ModelPatcher", FakePatcher),
            mock.patch.object(model_cache.comfy.model_management, "current_loaded_models", [loaded_model]),
            mock.patch.object(model_cache, "_original_free_memory") as original_free_memory,
        ):
            evicted = model_cache._evict_finished_models(execution_list)

        self.assertEqual(evicted, 0)
        self.assertEqual(cache.cache, {"loader": entry})
        original_free_memory.assert_not_called()

    def test_prompt_end_removes_an_unpinned_ram_only_cache(self):
        class Executor:
            pass

        patcher = FakePatcher(loaded_bytes=0, ram_bytes=32 * 1024)
        cache, _ = self.make_cache(patcher)
        executor = Executor()
        executor.caches = SimpleNamespace(all=[cache])
        model_cache._executor_ref = model_cache.weakref.ref(executor)
        model_cache.set_aggressive_eviction_enabled(True)

        with (
            mock.patch.object(model_cache.comfy.model_patcher, "ModelPatcher", FakePatcher),
            mock.patch.object(model_cache.comfy.model_management, "current_loaded_models", []),
            mock.patch.object(model_cache.comfy.model_management, "soft_empty_cache") as empty_cache,
            mock.patch.object(model_cache, "_observe_model_cache") as observe,
        ):
            evicted = model_cache._evict_finished_models()

        self.assertEqual(evicted, 1)
        self.assertEqual(cache.cache, {})
        self.assertEqual(patcher.partial_unload_calls, [])
        self.assertEqual(patcher.partial_unload_ram_calls, [1e30])
        empty_cache.assert_called_once_with()
        observe.assert_called_once_with()

    def test_never_evicts_a_pinned_model(self):
        patcher = FakePatcher(loaded_bytes=8 * 1024, ram_bytes=32 * 1024)
        loaded_model = SimpleNamespace(model=patcher, device=patcher.load_device)
        cache, entry = self.make_cache(patcher)
        execution_list = self.make_execution_list(cache)
        model_cache._pinned_models[patcher.clone_base_uuid] = patcher
        model_cache._pinned_model_keys = frozenset(model_cache._pinned_models)
        model_cache.set_aggressive_eviction_enabled(True)

        with (
            mock.patch.object(model_cache.comfy.model_patcher, "ModelPatcher", FakePatcher),
            mock.patch.object(model_cache.comfy.model_management, "current_loaded_models", [loaded_model]),
            mock.patch.object(model_cache, "_original_free_memory") as original_free_memory,
        ):
            evicted = model_cache._evict_finished_models(execution_list)

        self.assertEqual(evicted, 0)
        self.assertEqual(cache.cache, {"loader": entry})
        original_free_memory.assert_not_called()


class VramWaitTests(unittest.TestCase):
    GIB = 1024 ** 3

    def setUp(self):
        self.original_pinned_models = model_cache._pinned_models.copy()
        self.original_pinned_model_keys = model_cache._pinned_model_keys
        self.original_prompt_running = model_cache._prompt_running
        self.original_pending = list(model_cache._pending_model_removals)
        self.original_pending_ids = set(model_cache._pending_model_removal_ids)
        model_cache._pinned_models.clear()
        model_cache._pinned_model_keys = frozenset()
        model_cache._pending_model_removals.clear()
        model_cache._pending_model_removal_ids.clear()

    def tearDown(self):
        model_cache.set_vram_wait_enabled(False)
        model_cache._pinned_models.clear()
        model_cache._pinned_models.update(self.original_pinned_models)
        model_cache._pinned_model_keys = self.original_pinned_model_keys
        model_cache._prompt_running = self.original_prompt_running
        model_cache._pending_model_removals.clear()
        model_cache._pending_model_removals.extend(self.original_pending)
        model_cache._pending_model_removal_ids.clear()
        model_cache._pending_model_removal_ids.update(self.original_pending_ids)

    def test_wait_evicts_enough_unpinned_ram_caches_and_keeps_protected_models(self):
        device = model_cache.comfy.model_management.torch.device("cuda:0")
        context = SimpleNamespace(prompt_id="prompt-1", node_id="7")
        first = FakePatcher(loaded_bytes=0, ram_bytes=self.GIB)
        second = FakePatcher(loaded_bytes=0, ram_bytes=2 * self.GIB)
        pinned = FakePatcher(loaded_bytes=0, ram_bytes=4 * self.GIB)
        protected = FakePatcher(loaded_bytes=0, ram_bytes=4 * self.GIB)
        loaded_models = [
            SimpleNamespace(model=protected, device=device),
            SimpleNamespace(model=pinned, device=device),
            SimpleNamespace(model=second, device=device),
            SimpleNamespace(model=first, device=device),
        ]
        model_cache._pinned_models[pinned.clone_base_uuid] = pinned
        model_cache._pinned_model_keys = frozenset(model_cache._pinned_models)
        available = iter([
            (2 * self.GIB, self.GIB // 2),
            (5 * self.GIB, self.GIB // 2),
        ])

        model_cache.set_vram_wait_enabled(True)
        with (
            mock.patch.object(model_cache, "get_executing_context", return_value=context),
            mock.patch.object(model_cache.comfy.model_management, "current_loaded_models", loaded_models),
            mock.patch.object(
                model_cache.comfy.model_management,
                "get_total_memory",
                return_value=(8 * self.GIB, self.GIB),
            ),
            mock.patch.object(
                model_cache.comfy.model_management,
                "get_free_memory",
                side_effect=available,
            ),
            mock.patch.object(
                model_cache.psutil,
                "virtual_memory",
                return_value=SimpleNamespace(available=self.GIB),
            ),
            mock.patch.object(model_cache, "_observe_model_cache") as observe,
            mock.patch.object(model_cache._vram_wait_condition, "wait") as wait,
        ):
            model_cache._wait_for_required_vram(4 * self.GIB, device, keep_loaded=[loaded_models[0]])

        self.assertEqual(first.partial_unload_ram_calls, [1e30])
        self.assertEqual(second.partial_unload_ram_calls, [1e30])
        self.assertEqual(pinned.partial_unload_ram_calls, [])
        self.assertEqual(protected.partial_unload_ram_calls, [])
        observe.assert_called_once_with()
        wait.assert_called_once_with(timeout=1.0)

    def test_waits_for_external_vram_and_resumes_when_requirement_is_met(self):
        device = model_cache.comfy.model_management.torch.device("cuda:0")
        context = SimpleNamespace(prompt_id="prompt-1", node_id="7")
        available = iter([
            (2 * self.GIB, self.GIB // 2),
            (5 * self.GIB, self.GIB // 2),
        ])
        statuses = []

        def observe_wait(timeout):
            statuses.append(model_cache.get_vram_wait_info())

        model_cache.set_vram_wait_enabled(True)
        with (
            mock.patch.object(model_cache, "get_executing_context", return_value=context),
            mock.patch.object(
                model_cache.comfy.model_management,
                "get_total_memory",
                return_value=(8 * self.GIB, self.GIB),
            ),
            mock.patch.object(
                model_cache.comfy.model_management,
                "get_free_memory",
                side_effect=available,
            ),
            mock.patch.object(
                model_cache.comfy.model_management,
                "throw_exception_if_processing_interrupted",
            ) as check_interrupted,
            mock.patch.object(model_cache._vram_wait_condition, "wait", side_effect=observe_wait) as wait,
        ):
            model_cache._wait_for_required_vram(4 * self.GIB, device)

        wait.assert_called_once_with(timeout=1.0)
        check_interrupted.assert_called_once_with()
        self.assertEqual(statuses[0]["prompt_id"], "prompt-1")
        self.assertEqual(statuses[0]["available_bytes"], 2 * self.GIB)
        self.assertEqual(model_cache.get_vram_wait_info(), {"enabled": True, "waiting": False})

    def test_manual_model_removal_is_processed_while_waiting(self):
        device = model_cache.comfy.model_management.torch.device("cuda:0")
        context = SimpleNamespace(prompt_id="prompt-1", node_id="7")
        patcher = FakePatcher(loaded_bytes=0, ram_bytes=2 * self.GIB)
        loaded_model = SimpleNamespace(model=patcher, device=device)
        available = iter([
            (2 * self.GIB, self.GIB // 2),
            (2 * self.GIB, self.GIB // 2),
            (5 * self.GIB, self.GIB // 2),
        ])
        model_cache._pinned_models[patcher.clone_base_uuid] = patcher
        model_cache._pinned_model_keys = frozenset(model_cache._pinned_models)
        model_cache._prompt_running = True

        def remove_while_waiting(timeout):
            model_cache.queue_model_removal(str(id(patcher)))

        model_cache.set_vram_wait_enabled(True)
        with (
            mock.patch.object(model_cache, "get_executing_context", return_value=context),
            mock.patch.object(model_cache, "_get_execution_list", return_value=None),
            mock.patch.object(model_cache.comfy.model_management, "current_loaded_models", [loaded_model]),
            mock.patch.object(
                model_cache.comfy.model_management,
                "get_total_memory",
                return_value=(8 * self.GIB, self.GIB),
            ),
            mock.patch.object(
                model_cache.comfy.model_management,
                "get_free_memory",
                side_effect=available,
            ),
            mock.patch.object(model_cache, "_remove_model_from_cache") as remove_model,
            mock.patch.object(
                model_cache._vram_wait_condition,
                "wait",
                side_effect=remove_while_waiting,
            ) as wait,
        ):
            model_cache._wait_for_required_vram(4 * self.GIB, device)

        wait.assert_called_once_with(timeout=1.0)
        remove_model.assert_called_once_with(str(id(patcher)), allow_current_prompt=True)
        self.assertEqual(list(model_cache._pending_model_removals), [])

    def test_model_can_be_unpinned_while_waiting(self):
        device = model_cache.comfy.model_management.torch.device("cuda:0")
        context = SimpleNamespace(prompt_id="prompt-1", node_id="7")
        patcher = FakePatcher(loaded_bytes=0, ram_bytes=2 * self.GIB)
        loaded_model = SimpleNamespace(model=patcher, device=device)
        available = iter([
            (2 * self.GIB, self.GIB // 2),
            (5 * self.GIB, self.GIB // 2),
        ])
        model_cache._pinned_models[patcher.clone_base_uuid] = patcher
        model_cache._pinned_model_keys = frozenset(model_cache._pinned_models)

        def unpin_while_waiting(timeout):
            model_cache.set_model_pinned(str(id(patcher)), False)

        model_cache.set_vram_wait_enabled(True)
        with (
            mock.patch.object(model_cache, "get_executing_context", return_value=context),
            mock.patch.object(model_cache.comfy.model_management, "current_loaded_models", [loaded_model]),
            mock.patch.object(
                model_cache.comfy.model_management,
                "get_total_memory",
                return_value=(8 * self.GIB, self.GIB),
            ),
            mock.patch.object(
                model_cache.comfy.model_management,
                "get_free_memory",
                side_effect=available,
            ),
            mock.patch.object(
                model_cache._vram_wait_condition,
                "wait",
                side_effect=unpin_while_waiting,
            ) as wait,
        ):
            model_cache._wait_for_required_vram(4 * self.GIB, device)

        wait.assert_called_once_with(timeout=1.0)
        self.assertFalse(model_cache._is_model_pinned(patcher))

    def test_does_not_wait_when_external_vram_cannot_cover_the_shortfall(self):
        device = model_cache.comfy.model_management.torch.device("cuda:0")
        context = SimpleNamespace(prompt_id="prompt-1", node_id="7")
        model_cache.set_vram_wait_enabled(True)

        with (
            mock.patch.object(model_cache, "get_executing_context", return_value=context),
            mock.patch.object(
                model_cache.comfy.model_management,
                "get_total_memory",
                return_value=(8 * self.GIB, 6 * self.GIB),
            ),
            mock.patch.object(
                model_cache.comfy.model_management,
                "get_free_memory",
                return_value=(self.GIB, self.GIB // 2),
            ),
            mock.patch.object(model_cache, "_evict_unpinned_ram_caches") as evict_ram,
            mock.patch.object(model_cache._vram_wait_condition, "wait") as wait,
        ):
            model_cache._wait_for_required_vram(4 * self.GIB, device)

        evict_ram.assert_not_called()
        wait.assert_not_called()

    def test_does_not_treat_aimdo_model_vram_as_external(self):
        device = model_cache.comfy.model_management.torch.device("cuda:0")
        context = SimpleNamespace(prompt_id="prompt-1", node_id="7")
        model_cache.set_vram_wait_enabled(True)

        with (
            mock.patch.object(model_cache, "get_executing_context", return_value=context),
            mock.patch.object(
                model_cache.comfy.model_management,
                "get_total_memory",
                return_value=(24 * self.GIB, self.GIB // 4),
            ),
            mock.patch.object(
                model_cache.comfy.model_management,
                "get_free_memory",
                return_value=(15 * self.GIB, self.GIB // 4),
            ),
            mock.patch.object(model_cache, "_aimdo_vram_bytes", return_value=8 * self.GIB + self.GIB // 2),
            mock.patch.object(model_cache._vram_wait_condition, "wait") as wait,
        ):
            model_cache._wait_for_required_vram(22 * self.GIB, device)

        wait.assert_not_called()

    def test_counts_shared_aimdo_allocations_once(self):
        device = model_cache.comfy.model_management.torch.device("cuda:0")
        vbar = SimpleNamespace(loaded_size=lambda: 8 * self.GIB)
        cast_buffer = SimpleNamespace(device=0, size=lambda: self.GIB // 2)

        class FakeDynamicPatcher:
            def _vbar_get(self):
                return vbar

        loaded_models = [
            SimpleNamespace(model=FakeDynamicPatcher(), device=device),
            SimpleNamespace(model=FakeDynamicPatcher(), device=device),
        ]
        with (
            mock.patch.object(model_cache.comfy.model_patcher, "ModelPatcherDynamic", FakeDynamicPatcher),
            mock.patch.object(model_cache.comfy.model_management, "current_loaded_models", loaded_models),
            mock.patch.object(
                model_cache.comfy.model_management,
                "STREAM_AIMDO_CAST_BUFFERS",
                {"first": cast_buffer, "second": cast_buffer},
            ),
        ):
            self.assertEqual(model_cache._aimdo_vram_bytes(device), 8 * self.GIB + self.GIB // 2)

    def test_setting_requires_a_boolean(self):
        with self.assertRaisesRegex(ValueError, "enabled must be a boolean"):
            model_cache.set_vram_wait_enabled("true")


class LoraPinningTests(unittest.TestCase):
    def setUp(self):
        self.original_executor_ref = model_cache._executor_ref
        self.original_pinned_loras = model_cache._pinned_loras.copy()
        self.original_prompt_running = model_cache._prompt_running
        self.original_pending_invalidation = model_cache._pending_lora_invalidation.copy()
        model_cache._pinned_loras.clear()
        model_cache._prompt_running = False
        model_cache._pending_lora_invalidation.clear()

        self.path = "/models/loras/test.safetensors"
        self.loader = model_cache.nodes.LoraLoader()
        self.original_tensor = model_cache.torch.tensor([1.0, 2.0])
        self.loader.loaded_lora = (self.path, {"weight": self.original_tensor}, {})
        objects = SimpleNamespace(cache={("3", "LoraLoader"): self.loader}, subcaches={})
        prompt_nodes = {
            "3": {"inputs": {}},
            "4": {"inputs": {"model": ["3", 0]}},
            "5": {"inputs": {}},
        }
        prompt = SimpleNamespace(all_node_ids=lambda: prompt_nodes.keys(), get_node=prompt_nodes.__getitem__)
        outputs = SimpleNamespace(
            cache={"lora": object(), "consumer": object(), "other": object()},
            cache_key_set=SimpleNamespace(keys={"3": "lora", "4": "consumer", "5": "other"}),
            dynprompt=prompt,
            subcaches={},
        )
        self.outputs = outputs
        executor = SimpleNamespace(caches=SimpleNamespace(objects=objects, outputs=outputs))
        model_cache._executor_ref = lambda: executor

    def tearDown(self):
        model_cache._pinned_loras.clear()
        model_cache._pinned_loras.update(self.original_pinned_loras)
        model_cache._executor_ref = self.original_executor_ref
        model_cache._prompt_running = self.original_prompt_running
        model_cache._pending_lora_invalidation.clear()
        model_cache._pending_lora_invalidation.update(self.original_pending_invalidation)

    def test_pin_copies_tensors_and_invalidates_dependent_outputs(self):
        result = model_cache.set_lora_pinned(self.path, True)

        pinned = self.loader.loaded_lora[1]["weight"]
        self.assertEqual(result["ram_bytes"], self.original_tensor.numel() * self.original_tensor.element_size())
        self.assertNotEqual(pinned.data_ptr(), self.original_tensor.data_ptr())
        self.assertEqual(set(self.outputs.cache), {"other"})

        with (
            mock.patch.object(model_cache.comfy.model_management, "current_loaded_models", []),
            mock.patch.object(model_cache.comfy.model_management, "extra_reserved_memory", return_value=0),
            mock.patch.object(model_cache.comfy.model_management, "get_all_torch_devices", return_value=[]),
            mock.patch.object(model_cache.comfy.model_management, "get_total_memory", return_value=1024),
            mock.patch.object(model_cache.comfy.model_management, "get_free_memory", return_value=512),
        ):
            loras = model_cache.get_model_cache_info()["loras"]
        self.assertEqual([(lora["name"], lora["pinned"], lora["active"]) for lora in loras], [("test.safetensors", True, True)])

        fresh_loader = model_cache.nodes.LoraLoader()
        with (
            mock.patch.object(model_cache.folder_paths, "get_full_path_or_raise", return_value=self.path),
            mock.patch.object(model_cache, "_original_lora_load", return_value=(None, None)),
        ):
            model_cache._load_lora_with_pins(fresh_loader, None, None, "test.safetensors", 1.0, 0)
        self.assertIs(fresh_loader.loaded_lora, self.loader.loaded_lora)

    def test_unpin_clears_loader_copy_and_invalidates_outputs(self):
        model_cache.set_lora_pinned(self.path, True)
        self.outputs.cache["lora"] = object()
        self.outputs.cache["consumer"] = object()

        result = model_cache.set_lora_pinned(self.path, False)

        self.assertFalse(result["pinned"])
        self.assertIsNone(self.loader.loaded_lora)
        self.assertEqual(set(self.outputs.cache), {"other"})
        self.assertNotIn(self.path, model_cache._pinned_loras)

    def test_pin_during_prompt_defers_output_invalidation(self):
        model_cache._prompt_running = True

        model_cache.set_lora_pinned(self.path, True)

        self.assertEqual(set(self.outputs.cache), {"lora", "consumer", "other"})
        self.assertEqual(model_cache._pending_lora_invalidation, {"3"})


if __name__ == "__main__":
    unittest.main()
