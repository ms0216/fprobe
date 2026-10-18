"""時間軸の部品の性能を測る（試作 prototypes/ui-bench/run_web.py と同じ測り方: Playwright で入っている Edge / Chrome を
開き、垂直同期を外して 900 フレームの視点の列を描く。ブラウザはダウンロードしない）。

    python prototypes/ui-bench/gen_data.py && python host/ui/bench/make_tile.py     # データ
    (cd host/ui && npx vite build --config bench/vite.config.ts)                      # ページ
    python host/ui/bench/run.py --renderer webgl2 --decimate worker [--channel msedge] [--vsync] [--tile rows]

要るもの: playwright・psutil（試作の測定の venv）。結果: host/ui/bench/results/<channel>[-vsync]-<renderer>-<decimate>.json
"""

import argparse
import json
import sys
import tempfile
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "prototypes" / "ui-bench"))
from memwatch import MemWatch, by_cmdline  # noqa: E402

PORT = 8766


class Handler(SimpleHTTPRequestHandler):
    """bench/dist を配り、/data/ だけ bench/data から。"""
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".js": "text/javascript"}

    def translate_path(self, path):
        if path.startswith("/data/"):
            return str(HERE / "data" / path[len("/data/"):].split("?")[0])
        return super().translate_path(path)

    def log_message(self, *args):
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="msedge", choices=["msedge", "chrome"])
    ap.add_argument("--renderer", default="webgl2", choices=["webgl2", "canvas2d"])
    ap.add_argument("--decimate", default="worker", choices=["worker", "inline"])
    ap.add_argument("--vsync", action="store_true")
    ap.add_argument("--spin", action="store_true", help="worker: 新しい結果の無い描画の機会にも描き直す（垂直同期なしの実力）")
    ap.add_argument("--tile", default="", choices=["", "rows"], help="rows: 段の多いタイル（make_rows_tile.py）")
    a = ap.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), partial(Handler, directory=str(HERE / "dist")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    tag = (f"{'rows-' if a.tile else ''}{a.channel}{'-vsync' if a.vsync else ''}-{a.renderer}-{a.decimate}"
           f"{'-spin' if a.spin else ''}")
    args = [] if a.vsync else ["--disable-gpu-vsync", "--disable-frame-rate-limit"]
    with tempfile.TemporaryDirectory(prefix="fprobe-bench-") as profile, sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(profile, channel=a.channel, headless=False, args=args,
                                                    viewport={"width": 1600, "height": 820}, device_scale_factor=1)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"http://127.0.0.1:{PORT}/index.html?r={a.renderer}&d={a.decimate}&spin={int(a.spin)}&tile={a.tile}")
        procs = []
        for _ in range(50):
            procs = by_cmdline(profile)
            if procs:
                break
            time.sleep(0.1)
        with MemWatch(lambda: procs) as mem:
            page.wait_for_function("document.title === 'done'", timeout=600_000)
        result = page.evaluate("window.__result")
        ctx.close()
    server.shutdown()
    result["tag"] = tag
    result["process_peak_mb"] = round(mem.peak_mb, 1)
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    (out / f"{tag}.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    keys = ("impl", "load_ms", "work_ms", "decimate_ms", "render_ms", "interval_ms", "process_peak_mb")
    print(json.dumps({k: result[k] for k in keys}, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
