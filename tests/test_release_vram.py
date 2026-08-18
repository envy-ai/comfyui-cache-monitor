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
        model_cache._pinned_models.clear()
        model_cache._pinned_model_keys = frozenset()
        model_cache._executor_ref = None

    def tearDown(self):
        model_cache._pinned_models.clear()
        model_cache._pinned_models.update(self.original_pinned_models)
        model_cache._pinned_model_keys = self.original_pinned_model_keys
        model_cache._executor_ref = self.original_executor_ref

    def pin(self, patcher):
        model_cache._pinned_models[patcher.clone_base_uuid] = patcher
        model_cache._pinned_model_keys = frozenset(model_cache._pinned_models)

    def model_patcher(self):
        torch = model_cache.comfy.model_patcher.torch
        device = torch.device("cpu")
        return model_cache.comfy.model_patcher.ModelPatcher(torch.nn.Linear(1, 1), device, device)

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

    def test_report_includes_retained_pins_and_process_rss(self):
        active = FakePatcher(loaded_bytes=0, ram_bytes=10 * 1024)
        retained = FakePatcher(loaded_bytes=0, ram_bytes=20 * 1024)
        self.pin(retained)
        loaded_model = SimpleNamespace(model=active, device=active.load_device)
        process = SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=99 * 1024))

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
        wait_for_vram.assert_called_once_with(10**12, "cuda:0")

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


class VramWaitTests(unittest.TestCase):
    GIB = 1024 ** 3

    def tearDown(self):
        model_cache.set_vram_wait_enabled(False)

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
            mock.patch.object(model_cache._vram_wait_condition, "wait") as wait,
        ):
            model_cache._wait_for_required_vram(4 * self.GIB, device)

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


if __name__ == "__main__":
    unittest.main()
