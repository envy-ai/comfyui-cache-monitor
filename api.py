from aiohttp import web

from server import PromptServer

from .model_cache import get_model_cache_info, release_vram


def register_routes():
    routes = PromptServer.instance.routes

    @routes.get("/comfyui-cache-monitor/model-cache")
    async def get_model_cache(request):
        return web.json_response(get_model_cache_info())

    @routes.post("/comfyui-cache-monitor/release_vram")
    async def post_release_vram(request):
        try:
            return web.json_response(release_vram())
        except Exception as exc:
            return web.json_response(
                {"released": False, "error": str(exc)},
                status=500,
            )
