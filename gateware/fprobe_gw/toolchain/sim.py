"""cocotb + GHDL による VHDL シミュレーションの起動。"""

import os
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

from .ghdl import GhdlToolchain

__all__ = ["SimulationFailed", "run_cocotb"]


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


def run_cocotb(*, toplevel: str, sources: Sequence[Path], test_module: str,
               generics: Mapping[str, object] | None = None,
               build_dir: Path | None = None, test_filter: str | None = None) -> None:
    """VHDL を GHDL でビルドし cocotb テストを実行する。test_filter はテスト名の正規表現（デバッグ用）。"""
    from cocotb_tools.check_results import get_results
    from cocotb_tools.runner import get_runner

    toolchain = GhdlToolchain.locate()
    build_dir = Path(build_dir or Path(__file__).resolve().parents[2] / "build" / "sim" / toplevel).resolve()
    with _process_environment(toolchain.environment()):
        runner = get_runner("ghdl")
        try:
            runner.build(sources=[Path(s).resolve() for s in sources], hdl_toplevel=toplevel,
                         build_dir=build_dir, build_args=["--std=08"], always=True)
        except RuntimeError as e:
            raise SimulationFailed(f"{toplevel}: GHDL の解析・合成に失敗（ログ上部のエラーを参照）") from e
        results = build_dir / "results.xml"
        results.unlink(missing_ok=True)   # 前回の結果を今回の結果と取り違えない
        try:
            runner.test(hdl_toplevel=toplevel, test_module=test_module,
                        test_dir=build_dir, build_dir=build_dir, results_xml=str(results),
                        test_args=["--std=08"], parameters=dict(generics or {}),
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
    if total == 0 or failed:
        raise SimulationFailed(
            f"{toplevel} / {test_module}: {failed}/{total} 件失敗（結果: {results}）")
