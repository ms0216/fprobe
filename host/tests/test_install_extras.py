"""入口（ライブラリ・デーモン・MCP・試験）ごとに、使う第三者のパッケージが pyproject の依存・extras に入っていること。

まっさらな venv に入れて動かす検査は tools/install/check_extras.py（PyPI が要る）。ここは機器も網も要らない静的な照合で、
`pip install -e "host[dev,mcp]"` で uvicorn・websockets が入らなかった不具合の再発を止める。
"""

import re
import tomllib
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _name(requirement: str) -> str:
    return re.split(r"[\[<>=!~ ;]", requirement, maxsplit=1)[0].lower().replace("_", "-")


def closure(entry: str) -> set[str]:
    """extras `entry`（"" なら本体だけ）で入る配布名の集合。fprobe[...] の自己参照は辿る。"""
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    extras = project["optional-dependencies"]
    found = {_name(r) for r in project["dependencies"]}
    pending = [entry] if entry else []
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for r in extras[current]:
            if _name(r) == "fprobe":
                pending += re.search(r"\[(.*)\]", r).group(1).replace(" ", "").split(",")
            else:
                found.add(_name(r))
    return found


# 入口 -> その入口が import する第三者のパッケージ（配布名）。import 名と配布名が違うもの: serial=pyserial, yaml=pyyaml
NEEDS = {
    "": {"pyusb", "libusb-package"},
    "debug": {"pyocd", "capstone"},
    "client": {"websockets"},
    "daemon": {"uvicorn", "starlette", "websockets", "jsonschema", "pyyaml", "pyserial", "pyocd"},
    "mcp": {"mcp", "uvicorn", "starlette", "websockets", "jsonschema", "pyyaml", "pyserial", "pyocd"},
    "dev": {"pytest", "httpx", "mcp", "uvicorn", "starlette", "websockets", "jsonschema", "pyyaml", "mpremote"},
}


@pytest.mark.parametrize("entry", NEEDS)
def test_entry_installs_what_it_imports(entry):
    missing = NEEDS[entry] - closure(entry)
    assert not missing, f"extras '{entry or '(本体)'}' に入らない: {sorted(missing)}"


def test_every_third_party_import_in_the_package_is_declared_somewhere():
    """fprobe の中の第三者の import は、どれかの extras か本体の依存で宣言されている（宣言漏れを防ぐ）。"""
    import sys
    declared = closure("dev")
    # import 名 -> 配布名（pyOCD が連れてくる elftools・intelhex・cmsis_pack_manager は pyocd の依存）
    alias = {"serial": "pyserial", "yaml": "pyyaml", "usb": "pyusb", "libusb_package": "libusb-package",
             "elftools": "pyocd", "intelhex": "pyocd", "cmsis_pack_manager": "pyocd"}
    imports: set[str] = set()
    for path in (Path(__file__).resolve().parents[1] / "fprobe").rglob("*.py"):
        for m in re.finditer(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z_0-9]*)", path.read_text(encoding="utf-8"), re.M):
            imports.add(m.group(1))
    third = {i for i in imports if i not in sys.stdlib_module_names and i != "fprobe"}
    undeclared = {alias.get(i, i).lower().replace("_", "-") for i in third} - declared
    assert not undeclared, f"宣言されていない第三者のパッケージ: {sorted(undeclared)}"
