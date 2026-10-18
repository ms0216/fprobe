"""results/*.json を Markdown の表にする（描画の試作だけ）。python summarize.py"""

import json
from pathlib import Path

RESULTS = Path(__file__).parent / "results"
FRAME_60 = 1000 / 60


def main() -> None:
    rows = []
    for f in sorted(RESULTS.glob("*.json")):
        r = json.loads(f.read_text())
        if "interval_ms" not in r:
            continue
        w, i = r["work_ms"], r["interval_ms"]
        over = sum(x > FRAME_60 for x in r.get("raw_interval", [])) / max(1, len(r.get("raw_interval", [])))
        rows.append(f"| {r['impl']} | {r['load_ms']:.0f} | {w['p50']:.1f} / {w['p95']:.1f} / {w['p99']:.1f} | "
                    f"{i['p50']:.1f} / {i['p95']:.1f} / {i['p99']:.1f} / {i['max']:.1f} | {100 * over:.1f} % | "
                    f"{r.get('process_peak_mb', '-')} |")
    print("| 試作 | 読み込み ms | 作業 ms p50/p95/p99 | フレーム間隔 ms p50/p95/p99/max | 16.7 ms 超え | プロセスの最大 MB |")
    print("|---|---|---|---|---|---|")
    print("\n".join(rows))


if __name__ == "__main__":
    main()
