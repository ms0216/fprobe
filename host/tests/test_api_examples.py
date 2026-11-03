"""仕様（host/api/fprobe_api.json）の引数の examples（画面の入力欄の最初の値・MCP の AI にも見える「例・推奨の値」）が、
その引数の型・範囲に合う（例が仕様の検査で断られない）。"""
from fprobe.api.spec import load_spec

WITH_EXAMPLES = ("linux.halt.open", "linux.halt.debug.attach", "la.capture")


def test_every_example_is_accepted_by_its_own_parameter_schema():
    spec = load_spec()
    found = 0
    for name in WITH_EXAMPLES:
        props = spec.methods[name].params["properties"]
        for param, schema in props.items():
            for value in schema.get("examples", []):
                found += 1
                assert value is not None
                assert isinstance(value, (bool, int, float, str)), (name, param)
                if "enum" in schema:
                    assert value in schema["enum"], (name, param, value)
                if "minimum" in schema:
                    assert value >= schema["minimum"], (name, param, value)
                if "maximum" in schema:
                    assert value <= schema["maximum"], (name, param, value)
    assert found >= 17
