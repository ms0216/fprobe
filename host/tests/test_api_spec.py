"""fprobe.api.spec / gen: API の仕様（host/api/fprobe_api.json）の読み込み・検証・生成物の一致。"""

import pytest

from fprobe.api import API_VERSION
from fprobe.api.gen import render_markdown, render_typescript, write_all
from fprobe.api.spec import InvalidParams, load_spec
from fprobe.mcp_server import Probe


@pytest.fixture(scope="module")
def spec():
    return load_spec()


def test_every_method_has_an_implementation_or_is_a_session_method(spec):
    assert spec.api_version == API_VERSION
    for m in spec.methods.values():
        assert m.kind in ("read", "control"), m.name
        if m.name.startswith("session."):
            assert m.impl == ""
        else:
            assert callable(getattr(Probe, m.impl, None)), m.name


def test_params_are_parameters_of_the_implementation(spec):
    # デーモンは impl(**params) で呼ぶ。仕様の引数はすべて Probe のメソッドの引数で、必須の引数は仕様でも必須
    import inspect
    for m in spec.methods.values():
        if m.name.startswith("session."):
            continue
        sig = inspect.signature(getattr(Probe, m.impl))
        names = {n for n in sig.parameters if n != "self"}
        props = set(m.params.get("properties", {}))
        assert props <= names or any(p.kind is p.VAR_KEYWORD for p in sig.parameters.values()), (m.name, props - names)
        needed = {n for n, p in sig.parameters.items()
                  if n != "self" and p.default is p.empty and p.kind is not p.VAR_KEYWORD}
        assert needed <= set(m.params.get("required", [])), (m.name, needed)


def test_params_are_validated(spec):
    spec.validate_params("target.halt", {})
    spec.validate_params("debug.run_until_halt", {"timeout_s": 2.5})
    with pytest.raises(InvalidParams):
        spec.validate_params("debug.run_until_halt", {"timeout_s": "soon"})       # 型違い
    with pytest.raises(InvalidParams):
        spec.validate_params("debug.breakpoint.set", {})                          # 必須が無い
    with pytest.raises(InvalidParams):
        spec.validate_params("target.halt", {"force": True})                      # 未知の引数
    with pytest.raises(KeyError):
        spec.validate_params("no.such.method", {})


def test_generated_files_match_the_spec(spec):
    ts = render_typescript(spec)
    assert "export const API_VERSION = 1;" in ts and '"debug.run_until_halt"' in ts
    md = render_markdown(spec)
    assert "## debug.run_until_halt" in md
    for path in write_all(check=True):                                            # 生成済みのファイルと一致
        assert path.exists()


def test_vector_catch_reads_are_separate_from_setting(spec):
    """画面は周期的にベクタキャッチを読む: 読みは状態を変えない読み（操作の記録に残らない）、設定は catch が要る。"""
    get, put = spec.methods["target.vector_catch.get"], spec.methods["target.vector_catch"]
    assert get.kind == "control" and get.observes
    assert not put.observes and put.params["required"] == ["catch"]


IRREVERSIBLE = {"target.flash", "target.flash.erase", "target.option_bytes.write", "target.unlock",
                "target.flash.algo.remove",
                # 使い方によって元に戻せない（利用者、2026-10-22）: 書き込み先を渡すと消して書く・nRF の CTRL-AP の
                # ERASEALL・Flash の書き換え・PC のファイルの上書き
                "target.flash.benchmark", "target.ap.write", "debug.breakpoint.flash", "target.flash.read"}


def test_irreversible_methods_are_declared_in_the_spec(spec):
    # 取り返しのつかない操作（画面の赤い確かめ）は名前から推測せず、仕様の印で決める
    marked = {name for name, m in spec.methods.items() if m.irreversible}
    assert marked == IRREVERSIBLE
    assert all(spec.methods[name].kind == "control" for name in marked)
