"""Capture the masthead mascot actually tracking, as an animated WebP.

The still screenshot cannot show the one thing the caption promises, so this
walks a synthetic cursor round the mascot, shoots the same clip region at each
step, and finishes on a poke. Every frame is the real page responding to a real
pointermove — the same events a hand would send.
"""
import base64, io, json, math, time, urllib.request
from pathlib import Path
from PIL import Image
from websockets.sync.client import connect

page = next(t for t in json.load(urllib.request.urlopen("http://[::1]:9222/json")) if t["type"] == "page")
ws = connect(page["webSocketDebuggerUrl"], max_size=None); n = 0
def cmd(m, **p):
    global n; n += 1
    ws.send(json.dumps({"id": n, "method": m, "params": p}))
    while True:
        r = json.loads(ws.recv())
        if r.get("id") == n:
            if "error" in r: raise RuntimeError(r["error"])
            return r.get("result", {})
def js(e): return cmd("Runtime.evaluate", expression=e, returnByValue=True).get("result", {}).get("value")

cmd("Emulation.setDeviceMetricsOverride", width=1280, height=900, deviceScaleFactor=2, mobile=False)
cmd("Page.navigate", url="http://127.0.0.1:8797/"); time.sleep(2.5)

box = json.loads(js('''(() => { const b = document.getElementById("demo-mascot").getBoundingClientRect();
  return JSON.stringify({x: b.left, y: b.top, w: b.width, h: b.height}); })()'''))
PAD, FOOT = 70, 116          # FOOT leaves the caption room rather than clipping it
clip = {"x": box["x"] - PAD, "y": box["y"] - PAD,
        "width": box["w"] + PAD * 2, "height": box["h"] + PAD + FOOT, "scale": 1}
cx, cy = box["x"] + box["w"] / 2, box["y"] + box["h"] / 2

def shot():
    d = cmd("Page.captureScreenshot", format="png", clip=clip)
    return Image.open(io.BytesIO(base64.b64decode(d["data"]))).convert("RGB")

frames, holds = [], []
STEPS, R = 16, 300
for i in range(STEPS):                       # a full lap, so every cell is used
    a = -math.pi / 2 + 2 * math.pi * i / STEPS
    js(f'dispatchEvent(new PointerEvent("pointermove",{{clientX:{cx + math.cos(a) * R},clientY:{cy + math.sin(a) * R}}}))')
    time.sleep(0.05)
    frames.append(shot()); holds.append(150)

js('document.getElementById("demo-mascot").click()')   # then the poke
for _ in range(4):
    time.sleep(0.12)
    frames.append(shot()); holds.append(150)
time.sleep(0.8)
frames.append(shot()); holds.append(700)

# Captured at 2x for clean edges, shipped at half that — it renders small in a
# README and the weight is nineteen frames, not one.
w = 520
frames = [f.resize((w, round(w * f.height / f.width)), Image.LANCZOS) for f in frames]
frames[0].save(Path("/tmp/track/tracking.webp"), save_all=True, append_images=frames[1:],
               duration=holds, loop=0, quality=82, method=6)
print("frames:", len(frames), "size:", frames[0].size)
