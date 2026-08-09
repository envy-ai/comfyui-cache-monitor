from typing_extensions import override

from comfy_api.latest import ComfyExtension, io

from .api import register_routes
from .model_cache import start_model_cache_history
from .nodes import ComfyUICacheMonitorInfo


WEB_DIRECTORY = "./web"


class ComfyUICacheMonitorExtension(ComfyExtension):
    @override
    async def on_load(self) -> None:
        register_routes()
        start_model_cache_history()

    @override
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [ComfyUICacheMonitorInfo]


async def comfy_entrypoint() -> ComfyUICacheMonitorExtension:
    return ComfyUICacheMonitorExtension()
