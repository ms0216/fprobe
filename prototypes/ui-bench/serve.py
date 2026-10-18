"""試作の配信: ui-bench を静的に配り、POST /result で測定結果を results/<impl>.json に書く（ループバックだけ）。

使い方: python serve.py [--port 8765]  → http://127.0.0.1:8765/web/?r=canvas
"""

import argparse
import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
RESULTS = ROOT / "results"


class Handler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".js": "text/javascript"}

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        RESULTS.mkdir(exist_ok=True)
        (RESULTS / f"{body['impl']}.json").write_text(json.dumps(body, indent=1))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


def start(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(ROOT)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    port = ap.parse_args().port
    start(port)
    print(f"http://127.0.0.1:{port}/web/?r=canvas  (Ctrl+C で終わる)")
    threading.Event().wait()
