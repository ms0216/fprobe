"""入口（extras）ごとに、まっさらな venv へ入れて、その入口が動くことを確かめる（入れ方の試験）。

python tools/install/check_extras.py [--keep] [入口 ...]    入口: core debug daemon mcp dev（既定は全部）
各入口で、`pip install -e "host[<extras>]"` だけを入れた venv を作り、その入口が使う import とコマンドを動かす。
足りない依存（例: デーモンの uvicorn・websockets）があれば、その入口が落ちて分かる。ネットワーク（PyPI）が要る。
"""

import argparse
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 入口 -> (extras, [(説明, python -I -c の中身 または コマンドの引数)])。-c は venv の python で、cwd=一時ディレクトリ。
ENTRIES: dict[str, tuple[str, list[tuple[str, list[str]]]]] = {
    "core": ("", [
        ("ライブラリ", ["python", "-c", "import fprobe, fprobe.device, fprobe.la_vcd"]),
        ("fprobe CLI", ["fprobe", "--help"]),
    ]),
    "debug": ("debug", [
        ("pyOCD と同時利用", ["python", "-c", "import fprobe.session, fprobe.disasm"]),
    ]),
    "daemon": ("daemon", [
        ("デーモンの import", ["python", "-c", "import fprobe.api.daemon, fprobe.api.client, uvicorn, websockets, starlette"]),
        ("fprobe-daemon", ["fprobe-daemon", "--help"]),
    ]),
    "mcp": ("mcp", [
        ("MCP の import", ["python", "-c", "import fprobe.mcp_server, fprobe.api.daemon"]),
        ("fprobe-mcp", ["fprobe-mcp", "--help"]),
    ]),
    "dev": ("dev", [
        # 全体の試験（--collect-only は全メソッドの照合の検査が落ちるので使わない）の代わりに、デーモン・GDB の結合の試験を走らせる
        ("デーモン・GDB の結合の試験", ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", str(ROOT / "host" / "tests" / "test_api_daemon.py"),
                              str(ROOT / "host" / "tests" / "test_gdb_server.py")]),
    ]),
}


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8")


def check(name: str, work: Path) -> list[str]:
    """入口 name を試す。失敗の説明の一覧（空なら通った）。"""
    extras, steps = ENTRIES[name]
    env_dir = work / f"venv-{name}"
    venv.EnvBuilder(with_pip=True).create(env_dir)
    bindir = env_dir / ("Scripts" if sys.platform == "win32" else "bin")
    python = str(bindir / ("python.exe" if sys.platform == "win32" else "python"))
    spec = str(ROOT / "host") + (f"[{extras}]" if extras else "")
    done = run([python, "-m", "pip", "install", "-q", "-e", spec], work)
    if done.returncode != 0:
        return [f"{name}: pip install {spec} が失敗\n{done.stderr[-800:]}"]
    problems = []
    for what, cmd in steps:
        exe = python if cmd[0] == "python" else str(bindir / cmd[0])
        done = run([exe, *cmd[1:]], work)
        if done.returncode != 0:
            problems.append(f"{name}: {what} が失敗: {' '.join(cmd)}\n{(done.stderr or done.stdout)[-800:]}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("entries", nargs="*", choices=list(ENTRIES))
    parser.add_argument("--keep", action="store_true", help="作った venv を残す（場所を表示）")
    args = parser.parse_args()
    names = args.entries or list(ENTRIES)
    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="fprobe-install-", ignore_cleanup_errors=True) as tmp:
        work = Path(tmp)
        for name in names:
            found = check(name, work)
            print(f"{'NG' if found else 'OK'}  {name}", flush=True)
            problems += found
        if args.keep:
            print(f"venv: {work}（終了後に消える。--keep の間は Ctrl+C まで待つ）")
            input()
    for p in problems:
        print(p, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
