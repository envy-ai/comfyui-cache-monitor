Provides a graphical monitor sidebar for ComfyUI's model cache, both for VRAM and system RAM, so you can see wtf it's actually doing.

When known, a model's source filename appears below its class name; hover over the shortened filename to see it in full.

Pinned models that leave ComfyUI's active model registry remain visible as **Retained**. Unpinning a retained model immediately releases its dynamic RAM and VRAM caches, so old pinned generations cannot remain hidden and unmanageable.

The **LoRAs** table shows LoRAs loaded by ComfyUI's standard LoRA loader nodes. Pinning one copies its file-backed tensors into system RAM and reuses that copy across prompts and execution-cache resets. Unpinning releases the retained copy once no cached workflow output refers to it. LoRA pins last only until ComfyUI exits.

The **X** beside a model removes that item from ComfyUI's execution and model caches, releasing its system RAM and VRAM. During a running prompt, removal happens at the next safe execution boundary. If the current node is using that model, removal waits until the node finishes; a later node can load the model again if needed.

The sidebar's **Wait for external VRAM** checkbox pauses an active prompt at model loading when another process is holding the VRAM ComfyUI has determined it needs. Before pausing, it evicts enough unpinned model weight caches from system RAM to preserve loading headroom. Models can still be unpinned or removed with **X** while paused; removals are processed by the waiting prompt unless its current node is using that model. The prompt resumes automatically when enough memory becomes available. Uncheck it or cancel the prompt to stop waiting. Requirements that cannot fit even if the external allocation is released still fail normally.

The optional **Aggressively evict finished models** checkbox releases unpinned models from VRAM and system RAM immediately after their final workflow consumer finishes, including during a prompt. It is disabled by default because evicted models must be loaded from disk again if a later prompt needs them. Pinned models are never selected by this cleanup.

Also adds a special API endpoint that flushes models out of VRAM without also evicting them from system RAM:

/comfyui-cache-monitor/release_vram

Just POST nothing to it, and it'll clear the VRAM and give you some information about how much was recovered. It will *not* remove models from system RAM cache, so as long as something else doesn't cause them to be evicted, using them again should be nearly instantaneous.

The cache controls are also available over the local ComfyUI API for integrations such as comfyui-mcp:

- `GET /comfyui-cache-monitor/model-cache` lists active and retained models, recent removals, RAM/VRAM totals, and external-VRAM wait state.
- `POST /comfyui-cache-monitor/release_vram` releases model VRAM while retaining RAM caches.
- `POST /comfyui-cache-monitor/vram-wait` with `{"enabled": true|false}` controls external-VRAM waiting.
- `POST /comfyui-cache-monitor/aggressive-eviction` with `{"enabled": true|false}` controls final-use model eviction.
- `POST /comfyui-cache-monitor/model-pin` with `{"cache_id": "...", "pinned": true|false}` pins or unpins a cached model in RAM.
- `POST /comfyui-cache-monitor/lora-pin` with `{"cache_id": "...", "pinned": true|false}` pins or unpins a listed LoRA in RAM.
- `POST /comfyui-cache-monitor/model-remove` with `{"cache_id": "..."}` dismisses a cached model and releases its RAM/VRAM.
- `POST /comfyui-cache-monitor/model-load` with `{"cache_id": "..."}` loads an already-cached model into VRAM using ComfyUI's normal memory policy.

<img width="811" height="1268" alt="Screenshot 2026-08-14 at 20-19-37 Minimax H3 r2v turbo 8-step weighted prompt - ComfyUI" src="https://github.com/user-attachments/assets/d40e1940-f8f8-4b6c-9f9c-80afd3d17154" />
