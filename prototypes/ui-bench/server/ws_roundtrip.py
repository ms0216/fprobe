"""サーバ（デーモン）側で間引くときの往復: JSON-RPC over WebSocket（ループバック）で 1 フレーム分の間引きを
頼み、届くまでの時間と大きさを測る。間引きは numpy（py/core.py）。いまの API の形（la.view の文字列）に合わせる。

使い方: python server/ws_roundtrip.py [--binary]  （ui-bench の venv。結果は results/ws-roundtrip-numpy[-binary].json）
  --binary: 返事を 1 つのバイナリのメッセージ（波形 u8 + 段ごとに件数 u32 か区間 f32）にする
"""

import asyncio
import json
import sys
import time
from pathlib import Path

import numpy as np
import websockets

sys.path.insert(0, str(Path(__file__).parent.parent / "py"))
import core  # noqa: E402

CHARS = np.frombuffer(b"01x", np.uint8)


def view(data: core.Data, wave: np.ndarray, t0: float, t1: float) -> dict:
    core.decimate_wave(data.channels, t0, t1, wave)
    levels = [CHARS[wave[c]].tobytes().decode() for c in range(core.CH)]
    lanes = []
    for lane in data.lanes:
        out = core.decimate_lane(lane, t0, t1)
        if out[0] == "sparse":
            lanes.append({"items": np.stack([out[1], out[2], out[3]], 1).round(1).tolist()})
        else:
            lanes.append({"counts": out[1].tolist()})
    return {"levels": levels, "lanes": lanes}


def view_binary(data: core.Data, wave: np.ndarray, t0: float, t1: float) -> bytes:
    core.decimate_wave(data.channels, t0, t1, wave)
    parts = [wave.tobytes()]
    for lane in data.lanes:
        out = core.decimate_lane(lane, t0, t1)
        body = (np.stack([out[1], out[2], out[3]], 1).astype("<f4") if out[0] == "sparse" else out[1].astype("<u4")).tobytes()
        parts.append(np.array([out[0] == "sparse", len(body)], "<u4").tobytes() + body)
    return b"".join(parts)


async def main() -> None:
    binary = "--binary" in sys.argv
    data = core.Data()
    wave = np.zeros((core.CH, core.W), np.uint8)

    async def handler(ws):
        async for msg in ws:
            req = json.loads(msg)
            p = req["params"]
            if binary:
                await ws.send(view_binary(data, wave, p["t0"], p["t1"]))
            else:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": view(data, wave, p["t0"], p["t1"])}))

    async with websockets.serve(handler, "127.0.0.1", 8767, max_size=None):
        async with websockets.connect("ws://127.0.0.1:8767", max_size=None) as ws:
            times, sizes = [], []
            for i, (t0, t1) in enumerate(data.views):
                t = time.perf_counter()
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": i, "method": "timeline.view",
                                          "params": {"t0": t0, "t1": t1, "width": core.W}}))
                reply = await ws.recv()
                if not binary:
                    json.loads(reply)
                if i >= data.meta["warmup"]:
                    times.append((time.perf_counter() - t) * 1e3)
                    sizes.append(len(reply))
    name = "ws-roundtrip-numpy" + ("-binary" if binary else "")
    result = {"impl": name, "roundtrip_ms": core.stats(times), "reply_kb": core.stats([s / 1024 for s in sizes])}
    (core.RESULTS / f"{name}.json").write_text(json.dumps(result, indent=1))
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
