"""Gowin・Vivado のビルダーが共有する部品（結果の型・エラー・成果物の削除・入力のハッシュ・ツールの起動・版の書き出し）。

ビルダーごとに違うのは、ツールの起動の引数・入力のファイルの並び・成果物とログの名前・後検証（タイミング等）だけ。
共通の手続き（読み取り専用の成果物の削除、タイムアウトとログの扱い、入力ハッシュの計算、前回の成功の再利用の判定）は
ここに 1 か所で持つ。
"""

import hashlib
import os
import shutil
import stat
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .project import BuildInfo, BuildStamp
from .timing import TimingSummary

__all__ = ["INPUTS_STAMP", "BuildError", "BuildResult", "force_remove", "git_state", "inputs_digest",
           "is_reusable", "publish_files", "run_tool", "write_build_stamp"]

INPUTS_STAMP = "inputs.sha256"           # 成功したビルドの入力のハッシュ


class BuildError(RuntimeError):
    """ビルドが失敗した、または成果物が信頼できない。"""


@dataclass(frozen=True)
class BuildResult:
    bitstream: Path
    log: Path
    timing: TimingSummary


def _make_writable_and_retry(function, path, _exc):
    os.chmod(path, stat.S_IWRITE)
    function(path)


def force_remove(path: Path) -> None:
    """読み取り専用属性を持つファイル・ディレクトリも含めて削除する。"""
    if path.is_dir():
        shutil.rmtree(path, onexc=_make_writable_and_retry)
    elif path.exists():
        os.chmod(path, stat.S_IWRITE)
        path.unlink()


def git_state(repo: Path) -> tuple[str | None, bool]:
    """repo の HEAD のコミット（取れなければ None）と、repo の下にコミットしていない変更があるか。"""
    def git(*args: str) -> str | None:
        try:
            r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return r.stdout if r.returncode == 0 else None
    head = git("rev-parse", "HEAD")
    if head is None:
        return None, False
    status = git("status", "--porcelain", "--", ".")
    return head.strip(), bool(status and status.strip())


def inputs_digest(files: Iterable[Path]) -> str:
    """ビルドの入力のファイル（パスと中身）のハッシュ。改行は揃える（git のブランチ切り替えで CRLF / LF が変わっても
    作り直さない。ツールは同じに扱う）。"""
    h = hashlib.sha256()
    for path in files:
        h.update(str(path).encode() + b"\0" + Path(path).read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return h.hexdigest()


def is_reusable(bitstream: Path, build_dir: Path, digest: str, stamp: BuildStamp | None) -> bool:
    """成果物があり、成功の印（INPUTS_STAMP）が今の入力のハッシュと一致し、版のパッケージもあるか。"""
    mark = build_dir / INPUTS_STAMP
    return (bitstream.is_file() and mark.is_file() and mark.read_text(encoding="utf-8") == digest
            and (stamp is None or stamp.target(build_dir).is_file()))


def write_build_stamp(stamp: BuildStamp, build_dir: Path, digest: str,
                      git: Callable[[Path], tuple[str | None, bool]], now: Callable[[], float]) -> None:
    """版のパッケージ（入力の要約・時刻・git の状態）を build_dir に書く。"""
    commit, dirty = git(stamp.repo)
    info = BuildInfo(digest=digest, time=int(now()), git=commit, dirty=dirty)
    stamp.target(build_dir).write_text(stamp.render(info), encoding="utf-8", newline="\n")


def run_tool(run, command: Sequence[str], *, cwd: Path, timeout_s: float, log: Path, tool: str) -> str:
    """ツールを起動して出力（stdout + stderr）を log に書き、その文字列を返す。
    タイムアウトと 0 以外の終了コードは BuildError。"""
    try:
        result = run(list(command), cwd=cwd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as e:
        log.write_text(str(e.stdout or "") + str(e.stderr or ""), encoding="utf-8")
        raise BuildError(f"{tool} が {timeout_s} 秒で終わらない（{log}）") from e
    out = (result.stdout or "") + (result.stderr or "")
    log.write_text(out, encoding="utf-8")
    if result.returncode != 0:
        raise BuildError(f"{tool} が終了コード {result.returncode} で失敗（{log}）")
    return out


def publish_files(result: BuildResult, build_dir: Path) -> None:
    """採用した版のビットストリームと成功したビルドの印を build_dir 直下に写す（Gowin の出力は読み取り専用属性）。
    すでに直下にあるものは写さない。"""
    for source in (result.bitstream, result.bitstream.parent / INPUTS_STAMP):
        target = build_dir / source.name
        if source.resolve() != target.resolve():
            force_remove(target)
            shutil.copyfile(source, target)
