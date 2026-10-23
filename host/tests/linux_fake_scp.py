# 試験の偽の scp: ターゲットからの取り寄せ（"u@h:/path" → ローカル）は tests/data/linux/<basename> を写す。置く向きは何もしない。
import shutil
import sys
from pathlib import Path

src, dst = sys.argv[-2], sys.argv[-1]
if "@" in src and ":" in src:
    shutil.copyfile(Path(__file__).parent / "data" / "linux" / Path(src.split(":", 1)[1]).name, dst)
