"""cocotb + GHDL による VHDL シミュレーションの起動。

1 つの cocotb モジュールに独立したテストが多数あるとき、`parallel=True` を渡すと、テストを複数のプロセスに分けて
同時に走らせる（GHDL の mcode は 1 プロセス 1 コアなので、長い 1 モジュールがそのまま全体の長さになるのを避ける）。
分けても確かめる内容は同じ: どのテストも自分で DUT をリセットして始める前提で、1 件ずつ別の GHDL プロセスで走り、
全件の合格が条件。分けられない・分ける意味が無いとき（テスト数が少ない、同時実行数が 1）は従来どおり 1 プロセス。
同時実行数は環境変数 FPROBE_SIM_JOBS（既定は CPU 数の半分、最大 8。1 で無効）。pytest-xdist の -n と併用するときは 1 にする。
"""

import ast
import importlib.util
import os
import re
from collections.abc import Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from .ghdl import GhdlToolchain

__all__ = ["JOBS_ENV_VAR", "SimulationFailed", "cocotb_test_names", "run_cocotb", "sim_jobs"]

JOBS_ENV_VAR = "FPROBE_SIM_JOBS"


class SimulationFailed(AssertionError):
    """cocotb のテストが失敗した、または 1 件も実行されなかった。"""


@contextmanager
def _process_environment(env: Mapping[str, str]):
    # cocotb の runner は ghdl を自プロセスの PATH から探し、子プロセスにも環境を引き継ぐ
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _simulate(toplevel: str, sources: list[Path], test_module: str, generics: dict[str, object],
              build_dir: Path, test_filter: str | None, environ: dict[str, str]) -> tuple[int, int, Path]:
    """build_dir に解析して実行し、（件数, 失敗数, 結果ファイル）を返す。モジュール直下に置くのはプロセス間で渡すため。"""
    from cocotb_tools.check_results import get_results
    from cocotb_tools.runner import get_runner

    toolchain = GhdlToolchain.locate()
    with _process_environment(toolchain.environment(environ)):   # 呼び出し側の環境（試験が設定した変数）を渡す
        runner = get_runner("ghdl")
        try:
            runner.build(sources=sources, hdl_toplevel=toplevel,
                         build_dir=build_dir, build_args=["--std=08"], always=True)
        except RuntimeError as e:
            raise SimulationFailed(f"{toplevel}: GHDL の解析・合成に失敗（ログ上部のエラーを参照）") from e
        results = build_dir / "results.xml"
        results.unlink(missing_ok=True)   # 前回の結果を今回の結果と取り違えない
        try:
            runner.test(hdl_toplevel=toplevel, test_module=test_module,
                        test_dir=build_dir, build_dir=build_dir, results_xml=str(results),
                        test_args=["--std=08"], parameters=dict(generics),
                        test_filter=test_filter)
        except SystemExit:
            # cocotb の runner はテスト失敗時に SystemExit を送出する。件数は結果 XML から得る
            pass
        except RuntimeError as e:
            # elaboration 時の assert 失敗などでシミュレータ自体が異常終了した
            raise SimulationFailed(f"{toplevel}: シミュレーションの実行に失敗（ログ参照）") from e
    if not results.is_file():
        raise SimulationFailed(f"{toplevel} / {test_module}: 結果ファイルが無い（ビルド失敗の可能性）")
    total, failed = get_results(results)
    return total, failed, results


def sim_jobs() -> int:
    """分けて走らせるときの同時実行数（FPROBE_SIM_JOBS。未指定は CPU 数の半分、最大 8）。"""
    value = os.environ.get(JOBS_ENV_VAR)
    if value:
        return max(1, int(value))
    return max(1, min(8, (os.cpu_count() or 2) // 2))


def cocotb_test_names(test_module: str) -> list[str] | None:
    """モジュールのソースから @cocotb.test() の関数名を順に得る。確実に数え切れないときは None（分けない）。"""
    spec = importlib.util.find_spec(test_module)
    if spec is None or spec.origin is None:
        return None
    source = Path(spec.origin).read_text(encoding="utf-8")
    names: list[str] = []
    for node in ast.parse(source).body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for deco in node.decorator_list:
            target = deco.func if isinstance(deco, ast.Call) else deco
            if ast.unparse(target) in ("cocotb.test", "test"):
                names.append(node.name)
    # テストの生成（TestFactory・parametrize・関数呼び出しの形の登録）など、数え漏れうる書き方があれば分けない
    if not names or len(names) != len(set(names)) or len(re.findall(r"cocotb\.test\b|TestFactory|parametrize", source)) != len(names):
        return None
    return names


_pool: ProcessPoolExecutor | None = None


def _executor(jobs: int) -> ProcessPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ProcessPoolExecutor(max_workers=jobs)
    return _pool


def _selected_names(test_module: str, test_filter: str | None) -> list[str] | None:
    names = cocotb_test_names(test_module)
    if names is None or test_filter is None:
        return names
    # cocotb の絞り込みはテストの完全名（モジュール名.関数名）への正規表現の検索
    return [n for n in names if re.search(test_filter, f"{test_module}.{n}")]


def run_cocotb(*, toplevel: str, sources: Sequence[Path], test_module: str,
               generics: Mapping[str, object] | None = None,
               build_dir: Path | None = None, test_filter: str | None = None, parallel: bool = False,
               with_previous: Sequence[str] = ()) -> None:
    """VHDL を GHDL でビルドし cocotb テストを実行する。test_filter はテスト名の正規表現（デバッグ用）。

    parallel=True: モジュール内のテストを 1 件（または下記のグループ）ずつ別のプロセスで、同時実行数ぶん並べて走らせる。
    with_previous: 直前のテストの状態を引き継ぐ前提のテスト名。直前のテストと同じプロセスで続けて走らせる。
    """
    build_dir = Path(build_dir or Path(__file__).resolve().parents[2] / "build" / "sim" / toplevel).resolve()
    resolved = [Path(s).resolve() for s in sources]
    generics = dict(generics or {})
    jobs = sim_jobs() if parallel else 1
    names = _selected_names(test_module, test_filter) if jobs > 1 else None
    if names is None or len(names) < 2:
        total, failed, results = _simulate(toplevel, resolved, test_module, generics, build_dir, test_filter,
                                          dict(os.environ))
        if total == 0 or failed:
            raise SimulationFailed(
                f"{toplevel} / {test_module}: {failed}/{total} 件失敗（結果: {results}）")
        return

    # 先頭から順に 1 グループずつ配る（空いたプロセスが次を取る）。with_previous のテストは直前のテストと同じグループ。
    # 各グループの件数が合わなければ失敗（取りこぼしを合格にしない）
    groups: list[list[str]] = []
    for name in names:
        if name in with_previous and groups:
            groups[-1].append(name)
        else:
            groups.append([name])
    pool = _executor(jobs)
    futures = []
    for i, group in enumerate(groups):
        names_re = "|".join(re.escape(n) for n in group)
        futures.append((group, pool.submit(_simulate, toplevel, resolved, test_module, generics, build_dir / f"job{i}",
                                           rf"{re.escape(test_module)}\.({names_re})$", dict(os.environ))))
    bad: list[str] = []
    for group, future in futures:
        try:
            total, failed, results = future.result()
        except SimulationFailed as e:
            bad.append(f"{group}: {e}")
            continue
        if total != len(group) or failed:
            bad.append(f"{group}: {failed}/{total} 件失敗（期待 {len(group)} 件、結果: {results}）")
    if bad:
        raise SimulationFailed(f"{toplevel} / {test_module}: " + "; ".join(bad))
