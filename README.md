Provides a graphical monitor sidebar for ComfyUI's model cache, both for VRAM and system RAM, so you can see wtf it's actually doing.

Pinned models that leave ComfyUI's active model registry remain visible as **Retained**. Unpinning a retained model immediately releases its dynamic RAM and VRAM caches, so old pinned generations cannot remain hidden and unmanageable.

The **X** beside a model removes that item from ComfyUI's execution and model caches, releasing its system RAM and VRAM. Removal is refused while a prompt is running.

The sidebar's **Wait for external VRAM** checkbox pauses an active prompt at model loading when another process is holding the VRAM ComfyUI has determined it needs. The prompt resumes automatically when enough memory becomes available. Uncheck it or cancel the prompt to stop waiting. Requirements that cannot fit even if the external allocation is released still fail normally.

Also adds a special API endpoint that flushes models out of VRAM without also evicting them from system RAM:

/comfyui-cache-monitor/release_vram

Just POST nothing to it, and it'll clear the VRAM and give you some information about how much was recovered. It will *not* remove models from system RAM cache, so as long as something else doesn't cause them to be evicted, using them again should be nearly instantaneous.

<img width="811" height="1268" alt="Screenshot 2026-08-14 at 20-19-37 Minimax H3 r2v turbo 8-step weighted prompt - ComfyUI" src="https://github.com/user-attachments/assets/d40e1940-f8f8-4b6c-9f9c-80afd3d17154" />
