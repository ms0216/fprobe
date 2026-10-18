"""dockview-core の ESM 版はスタイルを含まない（UMD 版 dist/dockview-core.js だけが読み込み時に <style> を入れる）。
UMD 版に埋め込まれた CSS を取り出して src/vendor/dockview.css に置く（dockview-core を上げたら作り直す）。

    python host/ui/scripts/extract_dockview_css.py
"""

import json
import re
from pathlib import Path

UI = Path(__file__).resolve().parents[1]
SRC = UI / "node_modules" / "dockview-core" / "dist" / "dockview-core.js"
OUT = UI / "src" / "vendor" / "dockview.css"


def main() -> None:
    text = SRC.read_text(encoding="utf-8")
    version = re.search(r"@version (\S+)", text).group(1)
    m = re.search(r'\.textContent = ("(?:[^"\\]|\\.)*");', text)
    css = json.loads(m.group(1))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(f"/* dockview-core {version} のスタイル（MIT License, Copyright (c) 2021 mathuo）。\n"
                   f"   scripts/extract_dockview_css.py が node_modules/dockview-core/dist/dockview-core.js から取り出した。"
                   f"直接編集しない */\n{css}", encoding="utf-8", newline="\n")
    print(f"{OUT}: {len(css):,} 文字")


if __name__ == "__main__":
    main()
