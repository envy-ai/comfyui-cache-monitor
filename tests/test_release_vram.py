import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest import mock


PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

import model_cache


class FakePatcher:
    def __init__(self, loaded_bytes, ram_bytes, dynamic=True):
        self._loaded_bytes = loaded_bytes
        self._ram_bytes = ram_bytes
        self._dynamic = dynamic
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


if __name__ == "__main__":
    unittest.main()
