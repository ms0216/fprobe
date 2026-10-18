"""ブラウザの試作を Playwright で測る（入っている Chrome / Edge を使う。ブラウザのダウンロードはしない）。

使い方: python run_web.py --channel msedge --renderer canvas [--vsync]
       python run_web.py --channel msedge --page egui     （egui の WASM 版。先に egui/build_wasm.sh）
  既定は垂直同期を外す（--disable-gpu-vsync --disable-frame-rate-limit）。フレームの間隔 = 描画の実力。
結果: results/<channel>[-vsync]-<renderer>.json（ページが送る）に、プロセスの木の作業セットの最大を足す。
"""

import argparse
import json
import tempfile
import time

from playwright.sync_api import sync_playwright

import serve
from memwatch import MemWatch, by_cmdline

PORT = 8765


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="msedge", choices=["msedge", "chrome"])
    ap.add_argument("--renderer", default="canvas", choices=["canvas", "canvasrect", "webgl"])
    ap.add_argument("--vsync", action="store_true")
    ap.add_argument("--page", default="web", choices=["web", "egui"])
    a = ap.parse_args()
    serve.start(PORT)
    tag = a.channel + ("-vsync" if a.vsync else "")
    args = [] if a.vsync else ["--disable-gpu-vsync", "--disable-frame-rate-limit"]
    with tempfile.TemporaryDirectory(prefix="uibench-") as profile, sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            profile, channel=a.channel, headless=False, args=args,
            viewport={"width": 1600, "height": 820}, device_scale_factor=1)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if a.page == "egui":
            page.goto(f"http://127.0.0.1:{PORT}/egui/web/?mode=bench")
        else:
            page.goto(f"http://127.0.0.1:{PORT}/web/?r={a.renderer}&mode=bench&tag={tag}")
        procs = []
        for _ in range(50):
            procs = by_cmdline(profile)
            if procs:
                break
            time.sleep(0.1)
        with MemWatch(lambda: procs) as mem:
            page.wait_for_function("document.title === 'done'", timeout=300_000)
        ctx.close()
    path = serve.RESULTS / ("egui-wasm.json" if a.page == "egui" else f"{tag}-{a.renderer}.json")
    result = json.loads(path.read_text())
    result["process_peak_mb"] = round(mem.peak_mb, 1)
    path.write_text(json.dumps(result, indent=1))
    print(json.dumps({k: result[k] for k in ("impl", "load_ms", "work_ms", "interval_ms", "process_peak_mb")}, indent=1))


if __name__ == "__main__":
    main()
