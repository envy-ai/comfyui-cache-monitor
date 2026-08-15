Provides a graphical monitor sidebar for ComfyUI's model cache, both for VRAM and system RAM, so you can see wtf it's actually doing.

Also adds a special API endpoint that flushes models out of VRAM without also evicting them from system RAM:

/comfyui-cache-monitor/release_vram

Just POST nothing to it, and it'll clear the VRAM and give you some information about how much was recovered. It will *not* remove models from system RAM cache, so as long as something else doesn't cause them to be evicted, using them again should be nearly instantaneous.<img width="1999" height="1374" alt="Screenshot 2026-08-14 at 20-19-37 Minimax H3 r2v turbo 8-step weighted prompt - ComfyUI" src="https://github.com/user-attachments/assets/69d18e6b-0073-4647-bb11-fa56c45e11ca" />

<img width="811" height="1268" alt="Screenshot 2026-08-14 at 20-19-37 Minimax H3 r2v turbo 8-step weighted prompt - ComfyUI" src="https://github.com/user-attachments/assets/d40e1940-f8f8-4b6c-9f9c-80afd3d17154" />
