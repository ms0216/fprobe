"""ネイティブの試作を起動して終わるまで待ち、プロセスの木の作業セットの最大を結果に足す。

使い方: python run_native.py <結果の名前> -- <コマンド...>
  例: python run_native.py egui-native -- egui/target/release/uibench-egui.exe --bench --data data --out results/egui-native.json
結果のファイル（results/<名前>.json）は試作が書く。
"""

import json
import subprocess
import sys
from pathlib import Path

from memwatch import MemWatch, tree

ROOT = Path(__file__).parent


def main() -> None:
    name, cmd = sys.argv[1], sys.argv[sys.argv.index("--") + 1:]
    if (ROOT / cmd[0]).exists():                       # Windows は cwd からの相対の実行ファイルを探さない
        cmd[0] = str(ROOT / cmd[0])
    proc = subprocess.Popen(cmd, cwd=ROOT)
    with MemWatch(lambda: tree(proc.pid)) as mem:
        proc.wait(timeout=600)
    path = ROOT / "results" / f"{name}.json"
    result = json.loads(path.read_text())
    result["process_peak_mb"] = round(mem.peak_mb, 1)
    path.write_text(json.dumps(result, indent=1))
    print(json.dumps({k: result.get(k) for k in ("impl", "load_ms", "work_ms", "interval_ms", "process_peak_mb")}, indent=1))


if __name__ == "__main__":
    main()
