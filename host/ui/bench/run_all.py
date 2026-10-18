"""時間軸の部品の測定を一通り行い、表にする（窓が次々に開く。測っている間は PC を操作しない）。

    python host/ui/bench/run_all.py            # 測って results/*.json と表
    python host/ui/bench/run_all.py --table    # 測らずに表だけ
"""

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS = [
    ["--renderer", "webgl2", "--decimate", "worker", "--vsync"],
    ["--renderer", "webgl2", "--decimate", "worker", "--spin"],
    ["--renderer", "webgl2", "--decimate", "worker", "--spin", "--channel", "chrome"],
    ["--renderer", "webgl2", "--decimate", "inline"],
    ["--renderer", "canvas2d", "--decimate", "worker", "--vsync"],
    ["--renderer", "canvas2d", "--decimate", "worker", "--spin"],
    ["--renderer", "canvas2d", "--decimate", "inline"],
    # 段の多いタイル（G01 の段の型すべて: 波形 16 + タスク 8 + 割り込み 4 + 値 4 + 解読 2 + 見出し 4）
    ["--tile", "rows", "--renderer", "webgl2", "--decimate", "worker", "--vsync"],
    ["--tile", "rows", "--renderer", "webgl2", "--decimate", "worker", "--spin"],
    ["--tile", "rows", "--renderer", "webgl2", "--decimate", "inline"],
    ["--tile", "rows", "--renderer", "canvas2d", "--decimate", "worker", "--vsync"],
]


def table() -> str:
    rows = ["| 測定 | 描画のスレッドの作業 p50 / p95 / p99 | 間引き（Worker か同じスレッド）p50 / p95 / p99 | "
            "フレーム間隔 p50 / p95 / p99 / max | 16.7 ms を超えた割合 | プロセスの最大 MB |",
            "|---|---|---|---|---|---|"]
    for path in sorted((HERE / "results").glob("*.json")):
        r = json.loads(path.read_text(encoding="utf-8"))
        f = lambda s, keys=("p50", "p95", "p99"): " / ".join(f"{s[k]:.1f}" for k in keys)  # noqa: E731
        rows.append(f"| {r['tag']} | {f(r['work_ms'])} | {f(r['decimate_ms'])} | "
                    f"{f(r['interval_ms'], ('p50', 'p95', 'p99', 'max'))} | {100 * r['interval_ms']['over_16_7']:.1f} % | "
                    f"{r['process_peak_mb']:.0f} |")
    return "\n".join(rows)


def main() -> None:
    if "--table" not in sys.argv:
        for args in RUNS:
            subprocess.run([sys.executable, str(HERE / "run.py"), *args], check=True, stdout=subprocess.DEVNULL)
    print(table())


if __name__ == "__main__":
    main()
