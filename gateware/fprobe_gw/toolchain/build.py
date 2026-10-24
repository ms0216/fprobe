"""Gowin EDA（gw_sh）で GowinProject をビルドし、成果物とタイミングを検証する。

gw_sh は絶対パスで直接実行する（cmd がカレントディレクトリのバッチを探さない環境があるため）。
入力（TCL・SDC・CST・ソース・IP）が前回の成功したビルドと同じなら gw_sh を省き、前回の成果物を再び検証して使う。
プロジェクトに版のパッケージ（BuildStamp）があれば、作り直すときに入力の要約・時刻・git の状態を書いてから gw_sh を
動かす（ビットストリームがケイパビリティで自分の版を申告する。U49）。
"""

import dataclasses
import hashlib
import os
import re
from concurrent.futures import ThreadPoolExecutor
import shutil
import stat
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .gowin import GowinToolchain
from .project import BuildInfo, BuildStamp, GowinIp, GowinProject
from .timing import TimingReportError, TimingSummary, parse_timing_report

__all__ = ["BuildError", "BuildResult", "GowinBuilder", "git_state", "margin", "undriven_nets"]

TIMING_REPORT = Path("impl") / "pnr" / "project_tr_content.html"
INPUTS_STAMP = "inputs.sha256"           # 成功したビルドの入力のハッシュ
BUILD_TIMEOUT_S = 60 * 60                # 1 回のビルドの上限（通常は 5〜8 分。Primer の fprobe は配線の限界に近く、20 分では配線が終わらないことがある。2026-10-24）


class BuildError(RuntimeError):
    """ビルドが失敗した、または成果物が信頼できない。"""


def _make_writable_and_retry(function, path, _exc):
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _force_remove(path: Path) -> None:
    """読み取り専用属性を持つファイル・ディレクトリも含めて削除する。"""
    if path.is_dir():
        shutil.rmtree(path, onexc=_make_writable_and_retry)
    elif path.exists():
        os.chmod(path, stat.S_IWRITE)
        path.unlink()


@dataclass(frozen=True)
class BuildResult:
    bitstream: Path
    log: Path
    timing: TimingSummary


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


class GowinBuilder:
    def __init__(self, toolchain: GowinToolchain, *, run=subprocess.run, timeout_s: float = BUILD_TIMEOUT_S,
                 now: Callable[[], float] = time.time,
                 git: Callable[[Path], tuple[str | None, bool]] = git_state):
        self._toolchain = toolchain
        self._run = run
        self.timeout_s = timeout_s
        self._now = now          # 版の時刻
        self._git = git          # 版の git の状態

    def build(self, project: GowinProject, build_dir: Path) -> BuildResult:
        build_dir = Path(build_dir)
        name = project.name
        bitstream = build_dir / f"{name}.fs"
        log = build_dir / "gw_sh.log"
        for ip in project.ips:
            self._generate_ip(project, ip, build_dir)
        tcl = project.prepare(build_dir)
        digest = _inputs_digest(project, build_dir, tcl)
        stamp = build_dir / INPUTS_STAMP
        reuse = (bitstream.is_file() and stamp.is_file() and stamp.read_text(encoding="utf-8") == digest
                 and (project.stamp is None or project.stamp.target(build_dir).is_file()))

        if not reuse:
            # 前回の成果物が残っていると、失敗したビルドで古いビットストリームや
            # 古いタイミングレポートを信じてしまう。Gowin は成果物を読み取り専用で出力するため、
            # 属性を解除してから確実に削除する（削除できなければ例外で止める）
            stamp.unlink(missing_ok=True)
            _force_remove(bitstream)
            _force_remove(build_dir / "impl")
            if project.stamp is not None:
                self._write_stamp(project.stamp, build_dir, digest)
            try:
                result = self._run([str(self._toolchain.gw_sh), tcl.name], cwd=build_dir,
                                   capture_output=True, text=True, timeout=self.timeout_s)
            except subprocess.TimeoutExpired as e:
                log.write_text(str(e.stdout or "") + str(e.stderr or ""), encoding="utf-8")
                raise BuildError(f"gw_sh が {self.timeout_s} 秒で終わらない（{log}）") from e
            log.write_text((result.stdout or "") + (result.stderr or ""), encoding="utf-8")
            if result.returncode != 0:
                raise BuildError(f"gw_sh が終了コード {result.returncode} で失敗（{log}）")
            undriven = undriven_nets((result.stdout or "") + (result.stderr or ""))
            if undriven:
                _force_remove(bitstream)                 # 使えないビットストリームは残さない
                raise BuildError("駆動されない配線がある（実機では値が定まらず配置で変わる。シミュレーションでは初期値で"
                                 f"隠れる）。すべて明示的に駆動すること（{log}）:\n  " + "\n  ".join(undriven))
            if not bitstream.is_file():
                raise BuildError(f"ビットストリーム {bitstream} が生成されていない（{log}）")

        report = build_dir / TIMING_REPORT
        try:
            timing = parse_timing_report(report.read_text(encoding="utf-8", errors="replace"))
        except (OSError, TimingReportError) as e:
            raise BuildError(f"タイミングレポート {report} を解析できない: {e}") from e
        try:
            _check_constrained_clocks(project, timing, report)
            if not timing.met:
                raise BuildError(f"タイミング制約未達（{report}）:\n" + timing.describe())
        except BuildError:
            # タイミングを満たさないビットストリームは残さない（配置の別の候補を誤って書き込み、「配置で壊れる」と
            # 誤解した。2026-10-17）。書き込みの道具も成功の印 INPUTS_STAMP の無いものは拒む
            _force_remove(bitstream)
            raise
        stamp.write_text(digest, encoding="utf-8")
        return BuildResult(bitstream=bitstream, log=log, timing=timing)


    def build_best(self, project: GowinProject, build_dir: Path, place_options=(1, 0, 2), *,
                   parallel: int = 3, fallback_route: int = 0) -> BuildResult:
        """配置アルゴリズム（-place_option）を変えた版を build_dir/p<n> に並列にビルドし、タイミングを満たした中で
        余裕（最も厳しいクロックの Fmax / 制約 - 1）が最大のものを返す。回路が混んでいると、小さな変更でも配置次第で
        タイミングを落とすため（CLS 90% 超）。どの版も満たさず、プロジェクトの -route_option が fallback_route と違えば、
        同じ配置で -route_option fallback_route の版（build_dir/p<n>r<m>）も試す（route_option 1 は sys の余裕が出やすいが、
        混んだ回路では配線しきれない。2026-10-23、Primer の fprobe）。前回成功した版が入力そのままなら、ビルドせずに
        それを使う。採用した版のビットストリームと成功したビルドの印は build_dir 直下にも写す（決まった場所。ホストが構成を
        書き替えるときに使う: build/<design>/top.fs）。"""
        build_dir = Path(build_dir)
        stages = [{f"p{n}": dataclasses.replace(project, options=_with_place_option(project.options, n))
                   for n in place_options}]
        route = _route_option(project.options)
        if route is not None and route != fallback_route:
            stages.append({f"p{n}r{fallback_route}": dataclasses.replace(
                project, options=_with_route_option(_with_place_option(project.options, n), fallback_route))
                for n in place_options})
        for variants in stages:
            for name, variant in variants.items():
                if self.is_current(variant, build_dir / name):
                    return _publish(self.build(variant, build_dir / name), build_dir)    # 検証して再利用
        errors: list[str] = []
        for variants in stages:
            for name in variants:                                      # IP は先に 1 回だけ用意する（並列で触らない）
                for ip in project.ips:
                    self._generate_ip(project, ip, build_dir / name)
            passed: list[BuildResult] = []
            order = list(variants)
            while order and not passed:
                batch, order = order[:parallel], order[parallel:]
                with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                    futures = {name: pool.submit(self.build, variants[name], build_dir / name) for name in batch}
                for name, future in futures.items():
                    try:
                        passed.append(future.result())
                    except BuildError as e:
                        errors.append(f"{name}: {str(e).splitlines()[0]}")
            if passed:
                return _publish(max(passed, key=lambda r: margin(r.timing)), build_dir)
        raise BuildError("どの配置でもタイミングを満たさない:\n" + "\n".join(errors))

    def is_current(self, project: GowinProject, build_dir: Path) -> bool:
        """build_dir の成果物が、いまの入力から成功したビルドのものか（ビルドはしない）。"""
        build_dir = Path(build_dir)
        stamp, bitstream = build_dir / INPUTS_STAMP, build_dir / f"{project.name}.fs"
        if not (stamp.is_file() and bitstream.is_file()):
            return False
        try:
            for ip in project.ips:
                if not ip.source(build_dir).is_file():
                    return False
            if project.stamp is not None and not project.stamp.target(build_dir).is_file():
                return False
            tcl = project.prepare(build_dir)
            return stamp.read_text(encoding="utf-8") == _inputs_digest(project, build_dir, tcl)
        except OSError:
            return False

    def _write_stamp(self, stamp: BuildStamp, build_dir: Path, digest: str) -> None:
        commit, dirty = self._git(stamp.repo)
        info = BuildInfo(digest=digest, time=int(self._now()), git=commit, dirty=dirty)
        stamp.target(build_dir).write_text(stamp.render(info), encoding="utf-8", newline="\n")

    def _generate_ip(self, project: GowinProject, ip: GowinIp, build_dir: Path) -> None:
        """IP を生成する。設定（生成用の TCL）が前回と同じで生成物があれば省く（生成は 1 つ十数秒かかる）。"""
        directory = ip.directory(build_dir)
        if ip.prebuilt is not None:
            source = self._toolchain.home / ip.prebuilt
            if not source.is_file():
                raise BuildError(f"IP {ip.module} の生成済みネットリスト {source} が無い")
            target = ip.source(build_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            _force_remove(target)
            shutil.copyfile(source, target)
            return
        script = ip.render_tcl(project.part, project.device_version)
        stamp = directory / "generate.tcl"
        if ip.source(build_dir).is_file() and stamp.is_file() and stamp.read_text(encoding="utf-8") == script:
            return
        _force_remove(directory)
        directory.mkdir(parents=True)
        stamp.write_text(script, encoding="utf-8")
        log = directory / "gw_sh.log"
        result = self._run([str(self._toolchain.gw_sh), stamp.name], cwd=directory, capture_output=True, text=True)
        log.write_text((result.stdout or "") + (result.stderr or ""), encoding="utf-8")
        if result.returncode != 0 or not ip.source(build_dir).is_file() or "ERROR" in (result.stdout or ""):
            stamp.unlink(missing_ok=True)
            raise BuildError(f"IP {ip.module} の生成に失敗（{log}）")


# レポートの制約周波数は MHz 小数 3 桁表示なので、その丸めを許す相対誤差
_CONSTRAINT_TOLERANCE = 1e-4


_UNDRIVEN = re.compile(r"""EX1998\) : Net '([^']+)' does not have a driver\("([^"]*)":(\d+)\)""")


def _publish(result: BuildResult, build_dir: Path) -> BuildResult:
    """採用した版のビットストリームと成功したビルドの印を build_dir 直下に写す（Gowin の出力は読み取り専用属性）。"""
    for source in (result.bitstream, result.bitstream.parent / INPUTS_STAMP):
        target = build_dir / source.name
        if source.resolve() != target.resolve():
            _force_remove(target)
            shutil.copyfile(source, target)
    return result


def undriven_nets(log: str) -> list[str]:
    """合成の警告 EX1998（駆動されない配線）を「配線（ファイル:行）」で、重複を除いて出た順に。"""
    out: list[str] = []
    for net, path, line in _UNDRIVEN.findall(log):
        item = f"{net}（{re.split(r'[\\/]', path)[-1]}:{line}）"
        if item not in out:
            out.append(item)
    return out


def margin(timing: TimingSummary) -> float:
    """最も厳しい（制約のある）クロックの余裕: Fmax / 制約 - 1。"""
    return min((c.fmax_mhz / c.constraint_mhz - 1 for c in timing.clocks
                if c.constraint_mhz and c.fmax_mhz is not None), default=0.0)


def _route_option(options: tuple[str, ...]) -> int | None:
    for o in options:
        m = re.search(r"-route_option (\d)", o)
        if m:
            return int(m.group(1))
    return None


def _with_route_option(options: tuple[str, ...], n: int) -> tuple[str, ...]:
    return tuple(re.sub(r"-route_option \d", f"-route_option {n}", o) for o in options)


def _with_place_option(options: tuple[str, ...], n: int) -> tuple[str, ...]:
    out = tuple(re.sub(r"-place_option \d", f"-place_option {n}", o) for o in options)
    return out if any("-place_option" in o for o in out) else (*out, f"-place_option {n}")


def _inputs_digest(project: GowinProject, build_dir: Path, tcl: Path) -> str:
    """ビルドの入力（生成した TCL・SDC・CST と、ソース・IP の中身）のハッシュ。版のパッケージは含めない（版がこの
    ハッシュを含むため。TCL がビルドディレクトリの版のパッケージを指すことは含まれる）。"""
    h = hashlib.sha256()
    stamped = project.stamp.source if project.stamp is not None else None
    files = [tcl, build_dir / f"{project.name}.sdc", build_dir / f"{project.name}.cst",
             *sorted(Path(s) for s in project.sources if s != stamped), *(ip.source(build_dir) for ip in project.ips)]
    for path in files:
        # 改行は揃える（git のブランチ切り替えで CRLF / LF が変わっても作り直さない。ツールは同じに扱う）
        h.update(str(path).encode() + b"\0" + Path(path).read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return h.hexdigest()


def _check_constrained_clocks(project: GowinProject, timing: TimingSummary, report: Path) -> None:
    """SDC で制約したクロックが実際に解析されたかを確かめる。

    ネット名の不一致などで制約が外れると、そのクロックはレポートに現れず
    「違反なし」に見えてしまうため、ここで明示的に検出する。
    """
    reported = {c.name: c for c in timing.clocks}
    for clock in project.clocks:
        found = reported.get(clock.name)
        if found is None:
            raise BuildError(f"制約したクロック {clock.name} がタイミングレポート {report} に無い"
                             "（SDC の対象名がネットリストと一致していない可能性）")
        expected_mhz = clock.frequency_hz / 1e6
        if found.constraint_mhz is not None and \
                abs(found.constraint_mhz - expected_mhz) > expected_mhz * _CONSTRAINT_TOLERANCE:
            raise BuildError(f"クロック {clock.name} の制約が {found.constraint_mhz} MHz で、"
                             f"要求 {expected_mhz:.3f} MHz と一致しない（{report}）")
