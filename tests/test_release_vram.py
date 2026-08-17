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
        self.offload_device = "cpu"
        self.model = SimpleNamespace()
        self.partial_unload_calls = []

    def loaded_size(self):
        return self._loaded_bytes

    def partially_unload(self, device, amount):
        self.partial_unload_calls.append((device, amount))
        self._loaded_bytes = 0
        return amount

    def loaded_ram_size(self):
        return self._ram_bytes

    def model_size(self):
        return self._ram_bytes + self._loaded_bytes

    def is_dynamic(self):
        return self._dynamic


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
        model_cache._pinned_models.clear()
        model_cache._pinned_model_keys = frozenset()

    def tearDown(self):
        model_cache._pinned_models.clear()
        model_cache._pinned_models.update(self.original_pinned_models)
        model_cache._pinned_model_keys = self.original_pinned_model_keys

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
        ):
            result = model_cache._free_memory_with_pins(10**12, "cuda:0")

        self.assertEqual(result, ["unloaded-other-model"])
        self.assertEqual(patcher.partial_unload_calls, [("cpu", 8 * 1024)])
        self.assertIn(loaded_model, original_free_memory.call_args.kwargs["keep_loaded"])

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
        executor = SimpleNamespace(
            caches=SimpleNamespace(outputs=outputs, objects=objects),
            status_messages=["old"],
            success=False,
        )
        original_reset = mock.Mock()

        with mock.patch.object(model_cache, "_original_executor_reset", original_reset):
            model_cache._executor_reset_with_pins(executor)

        original_reset.assert_not_called()
        self.assertEqual(outputs.cache, {"pinned": pinned_entry})
        self.assertEqual(outputs.used_generation, {"pinned": 1})
        self.assertEqual(objects.cache, {})
        self.assertEqual(executor.status_messages, [])
        self.assertTrue(executor.success)


if __name__ == "__main__":
    unittest.main()
