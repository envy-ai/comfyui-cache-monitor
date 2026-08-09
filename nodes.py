import json

from comfy_api.latest import io

from .model_cache import get_model_cache_info


class ComfyUICacheMonitorInfo(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="ComfyUICacheMonitorInfo",
            display_name="Model Cache Info",
            category="utils/system",
            description=(
                "Returns models in ComfyUI's active model registry and "
                "available system RAM and VRAM as JSON."
            ),
            not_idempotent=True,
            outputs=[io.String.Output(display_name="cache_info")],
        )

    @classmethod
    def execute(cls) -> io.NodeOutput:
        return io.NodeOutput(json.dumps(get_model_cache_info(), indent=2))
