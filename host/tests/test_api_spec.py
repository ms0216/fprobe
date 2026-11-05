"""fprobe.api.spec / gen: API の仕様（host/api/fprobe_api.json）の読み込み・検証・生成物の一致。"""

import dataclasses

import pytest

from fprobe.api import API_VERSION
from fprobe.api.gen import render_markdown, render_typescript, write_all
from fprobe.api.spec import InvalidParams, InvalidResult, load_spec, result_schema_problems
from fprobe.caps import Capabilities
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
    assert f"export const API_VERSION = {API_VERSION};" in ts and '"debug.run_until_halt"' in ts
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


CALLER = {"ui.state.set", "watch.set", "rtos.timeline.start", "profile.start", "rtt.start", "cycles.clock",
          "recording.mark", "rules.check", "inject.run", "linux.halt.open"}


def test_who_started_or_placed_something_is_the_callers_fact(spec):
    # だれがサンプラ・watch を始めたか、だれが印を置いたかは、画面が操作の記録から推さず、デーモンが渡す呼び出し元を
    # 実装が事実として残す（仕様の印 caller）
    import inspect
    marked = {name for name, m in spec.methods.items() if m.caller}
    assert marked == CALLER
    for name in marked:
        assert "caller" in inspect.signature(getattr(Probe, spec.methods[name].impl)).parameters, name


def test_the_count_window_of_fpga_conditions_is_given_by_the_caller(spec):
    # 条件の回数を数える範囲（最後の何件か）は使う側が明示する。既定の値を持たない
    m = spec.methods["fpga_obs.count"]
    assert set(m.params["required"]) == {"conditions", "last"}
    assert "default" not in m.params["properties"]["last"]


def test_every_result_is_a_written_shape(spec):
    # 応答の形はすべて書いてある: 型の無い節・中身を書かない object・items の無い配列・使われない定義が無い
    # （result_schema_problems の約束。何でもよい値は $defs の Json を明示して引く）
    assert result_schema_problems(spec) == []


def test_dataclass_shapes_are_generated_from_the_implementation(spec):
    # 実装の dataclass をそのまま返す形（x-from）は、仕様に手で書かず dataclass から作る（gen が作り直し、
    # test_generated_files_match_the_spec が古さを見る）。ケイパビリティは caps.Capabilities の全部の欄
    caps = spec.defs["Capabilities"]
    assert caps["x-from"] == "fprobe.caps:Capabilities"
    assert list(caps["properties"]) == [f.name for f in dataclasses.fields(Capabilities)]
    assert caps["properties"]["inject"] == {"type": "boolean"}           # 作った後は必ず bool（metadata の api_type）
    assert spec.methods["probe.info"].result["properties"]["capabilities"] == {"$ref": "#/$defs/Capabilities"}


def test_results_are_validated_against_the_spec(spec):
    with pytest.raises(InvalidResult, match=r"probe\.info: \['capabilities'\]"):
        spec.validate_result("probe.info", {"capabilities": {}, "link_open": None, "time": 0, "target_connected": False,
                                            "device": None, "name": None, "version": None})
    with pytest.raises(InvalidResult, match="probe.info"):
        spec.validate_result("probe.info", {"surprise": 1})


def test_type_schema_follows_the_jsonable_shape():
    from typing import Literal
    from fprobe.api.typeschema import type_schema
    defs: dict = {}
    assert type_schema(int | None, defs) == {"type": ["integer", "null"]}
    assert type_schema(tuple[int, ...], defs) == {"type": "array", "items": {"type": "integer"}}
    assert type_schema(tuple[int, str], defs) == {"type": "array", "prefixItems": [{"type": "integer"}, {"type": "string"}],
                                                  "items": False}
    assert type_schema(dict[int, bytes], defs) == {"type": "object", "additionalProperties": {"type": "string"}}
    assert type_schema(Literal["a", "b"], defs) == {"enum": ["a", "b"]}
    assert type_schema(Capabilities, defs) == {"$ref": "#/$defs/Capabilities"} and "LaChannel" in defs
    with pytest.raises(TypeError):
        type_schema(object, defs)


@pytest.mark.parametrize("method, params, expected", [
    ("target.halt", {"force": True}, "未知の引数: force"),
    ("debug.breakpoint.set", {}, "必須の引数が無い"),
    ("debug.run_until_halt", {"timeout_s": "soon"}, "型が違う"),
])
def test_param_errors_say_what_is_wrong_in_japanese(spec, method, params, expected):
    """引数の誤りは、jsonschema の英文でなく、どの引数がどう違うかの日本語の文で返す（人にも AI にも読める）。"""
    with pytest.raises(InvalidParams) as raised:
        spec.validate_params(method, params)
    text = str(raised.value)
    assert expected in text and "is a required property" not in text and "Additional properties" not in text


def test_a_hex_string_for_an_integer_says_to_send_a_json_number(spec):
    """整数の引数に '0x20000000' を渡したら、JSON の数で渡すことと、その 10 進の値を言う。"""
    with pytest.raises(InvalidParams) as raised:
        spec.validate_params("target.memory.read", {"address": "0x20000000", "length": 16})
    assert "536870912" in str(raised.value) and "JSON の数" in str(raised.value)


def test_a_huge_bad_value_does_not_blow_up_the_error_message(spec):
    """誤りの文に載せる値は先頭だけにして、全体の長さを言う（独立のレビュー: 500KB の値が 500KB の誤りの文になった）。"""
    with pytest.raises(InvalidParams) as raised:
        spec.validate_params("target.connect", {"target": "x", "frequency_hz": "9" * 100_000})
    text = str(raised.value)
    assert len(text) < 600 and "全体 100002 文字" in text
