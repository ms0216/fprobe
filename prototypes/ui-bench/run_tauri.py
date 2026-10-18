"""Tauri の試作を測る: serve.py を立て、uibench-tauri.exe で web/ のベンチを開き、結果が届いたら閉じる。

使い方: python run_tauri.py --renderer webgl [--vsync]
"""

import argparse
import json
import os
import subprocess
import time

import serve
from memwatch import MemWatch, tree

PORT = 8765
EXE = serve.ROOT / "tauri" / "target" / "release" / "uibench-tauri.exe"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--renderer", default="webgl", choices=["canvas", "canvasrect", "webgl"])
    ap.add_argument("--vsync", action="store_true")
    a = ap.parse_args()
    serve.start(PORT)
    tag = "tauri" + ("-vsync" if a.vsync else "")
    path = serve.RESULTS / f"{tag}-{a.renderer}.json"
    path.unlink(missing_ok=True)
    env = dict(os.environ)
    if not a.vsync:
        env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--disable-gpu-vsync --disable-frame-rate-limit"
    url = f"http://127.0.0.1:{PORT}/web/?r={a.renderer}&mode=bench&tag={tag}"
    proc = subprocess.Popen([str(EXE), url], env=env)
    with MemWatch(lambda: tree(proc.pid)) as mem:
        deadline = time.time() + 300
        while not path.exists() and time.time() < deadline:
            time.sleep(0.2)
    for p in reversed(tree(proc.pid)):
        p.kill()
    result = json.loads(path.read_text())
    result["process_peak_mb"] = round(mem.peak_mb, 1)
    path.write_text(json.dumps(result, indent=1))
    print(json.dumps({k: result[k] for k in ("impl", "load_ms", "work_ms", "interval_ms", "process_peak_mb")}, indent=1))


if __name__ == "__main__":
    main()
