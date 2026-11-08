"""トークン形式の復号の C 版（host/fprobe/_la_tokens_c.c）を共有ライブラリにする（任意。無くても Python 版が同じ結果で動く）。

使い方（リポジトリの根から）:  .venv/Scripts/python.exe tools/build_la_tokens_c.py
gcc / clang / cc / cl（Visual Studio の開発者コマンドプロンプト）のどれかを使う。出力は host/fprobe/_la_tokens_c.(dll|so|dylib)
（git に入れない）。la_tokens が読み込めれば自動で使い、読めなければ numpy / 純 Python の版に落ちる。
環境変数 FPROBE_LA_TOKENS_C=0 で C 版を使わせない（試験・比較用）。
"""

import shutil
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "host" / "fprobe"
SRC = PKG / "_la_tokens_c.c"


def library_path() -> Path:
    ext = ".dll" if sys.platform == "win32" else ".dylib" if sys.platform == "darwin" else ".so"
    return PKG / f"_la_tokens_c{ext}"


def build() -> Path:
    out = library_path()
    for cc in ("gcc", "clang", "cc"):
        exe = shutil.which(cc)
        if exe:
            cmd = [exe, "-O3", "-shared", "-std=c99", "-fvisibility=hidden", "-o", str(out), str(SRC)]
            if sys.platform != "win32":
                cmd.insert(2, "-fPIC")
            subprocess.run(cmd, check=True)
            return out
    exe = shutil.which("cl")
    if exe:
        subprocess.run([exe, "/nologo", "/O2", "/LD", str(SRC), f"/Fe:{out}", f"/Fo:{out.with_suffix('.obj')}"],
                       check=True, cwd=PKG)
        return out
    raise SystemExit("C コンパイラ（gcc / clang / cc / cl）が見つからない。C 版は任意なので、無くても Python 版で動く。")


if __name__ == "__main__":
    print(build())
