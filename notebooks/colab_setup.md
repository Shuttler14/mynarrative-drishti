# Colab T4 Worker Setup

## Step 1: GPU Runtime
Runtime → Change runtime type → T4 GPU

## Step 2: Install + Run
```python
!pip install -q torch diffusers transformers peft accelerate xformers \
  insightface onnxruntime-gpu controlnet-aux ultralytics fastapi uvicorn \
  pyngrok structlog opencv-python-headless basicsr facexlib clean-fid

!git clone https://github.com/Shuttler14/mynarrative-drishti.git && cd mynarrative-drishti

import nest_asyncio, uvicorn, threading
nest_asyncio.apply()

from pyngrok import ngrok
public_url = ngrok.connect(8001)
print("Public URL:", public_url)

threading.Thread(target=lambda: uvicorn.run("vtoe.api.server:app", host="0.0.0.0", port=8001),
                 daemon=True).start()
```

## Step 3: Heartbeat (anti-idle)
```python
import time, requests, threading

def heartbeat():
    while True:
        try:
            requests.post("https://your-oracle-host/vtoe/heartbeat",
                           json={"url": str(public_url)}, timeout=5)
        except Exception:
            pass
        time.sleep(300)  # 5 min

threading.Thread(target=heartbeat, daemon=True).start()
```

## Notes
- Colab caps sessions at 12h and reclaims idle GPUs
- Treat the worker as ephemeral
- The Oracle control plane should mark URL dead after 15min of missed heartbeats
- For real production traffic, budget a paid GPU
- Use Colab for dev/eval only
