from aiohttp import web

from server import PromptServer

from .model_cache import get_model_cache_info, release_vram, set_model_pinned


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

    @routes.post("/comfyui-cache-monitor/model-pin")
    async def post_model_pin(request):
        json_data = await request.json()
        try:
            return web.json_response(set_model_pinned(json_data.get("cache_id"), json_data.get("pinned")))
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except LookupError as exc:
            return web.json_response({"error": str(exc)}, status=404)
