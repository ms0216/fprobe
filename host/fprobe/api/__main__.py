"""python -m fprobe.api gen: API の仕様から生成物を作り直す。"""

import sys

from .gen import write_all

if __name__ == "__main__":
    if sys.argv[1:] != ["gen"]:
        raise SystemExit("使い方: python -m fprobe.api gen")
    for path in write_all():
        print(path)
