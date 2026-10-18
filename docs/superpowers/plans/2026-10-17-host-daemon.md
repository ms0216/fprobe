# fprobe-daemon（デーモンと API）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** fprobe を 1 つ握るデーモンを作り、JSON-RPC 2.0 over WebSocket で全機能を出す。AI（MCP）と人間（後の画面）が同時につながり、操作は記録として流れ、人間が一時停止・交代できる。

**Architecture:** API の正は手書きの JSON Schema（`host/api/fprobe_api.json`）。各メソッドは既存の `mcp_server.Probe` のメソッド（`impl`）に対応する。デーモンは Starlette の WebSocket で要求を受け、1 本の作業スレッド（調停）で順に実行し、結果と記録（activity）を通知する。MCP はデーモンの利用者になり、道具は仕様から生成する。

**Tech Stack:** Python 3.13、Starlette 1.8 + uvicorn 0.54（導入済み）、websockets 17（同期クライアント、導入済み）、jsonschema 4.26（導入済み）、MCP SDK 2.3、pytest。

**Spec:** `docs/superpowers/specs/2026-10-17-host-daemon-design.md`

## Global Constraints

- 既定の待ち受けは `127.0.0.1:7820`。`--lan` のときだけ `0.0.0.0` で待ち受け、トークン（起動時に生成して表示、`--token` で指定可）の無い接続を断る。
- API の版は `API_VERSION = 1`。`session.hello` で一致しなければ誤り（コード -32010）で断る。
- `session.hello` より前の要求は誤り（-32011）。
- 操作の種類は `read`（いつでも・誰でも）と `control`（一時停止中は human 以外を保留）の 2 つ。
- 利用者の種類は `human` / `ai` / `script`。`session.control.pause` / `resume` は human だけ。
- 記録は `~/.fprobe/activity/<YYYYmmdd-HHMMSS>.jsonl`（環境変数 `FPROBE_HOME` で `~/.fprobe` を変えられる）。
- 依存を増やさない（上の導入済みのもので作る）。コメント・文書は日本語、識別子は英語。
- 機器（USB / COM / Ethernet）は起動時の引数で 1 つ。PYNQ-Z2・ST-LINK には触れない（fprobe の VID:PID とシリアル、Primer の FACTORYAIOT_PRO で選ぶ既存の仕組みを使う）。

## Review Focus

- 利用者が要求の途中で切断した: 実行中の操作は最後まで走らせ、結果は捨てる。保留中の要求は取り消す。デーモンは落ちない。→ Task 3 の試験 `test_disconnect_drops_pending`。
- 一時停止した human が切断した: 一時停止は自動で解ける（AI が永久に待たない）。→ Task 2 の試験 `test_pause_released_when_pauser_leaves`。
- 機器の誤り（ControlError・pyOCD の例外・未接続）: JSON-RPC の誤り（-32020 機器、-32021 未接続 = 例外の文に「未接続」）で返し、デーモンは次の要求を受け続ける。→ Task 3 の試験 `test_device_error_is_reported_and_daemon_survives`。
- 仕様に合わない引数（型違い・必須が無い・未知の引数）: 機器に触れずに -32602 で返す。→ Task 1 の試験 `test_params_are_validated`。
- 同じメソッドを 2 つの利用者がほぼ同時に呼ぶ: 必ず 1 つずつ実行される（機器と pyOCD はスレッド安全でない）。→ Task 2 の試験 `test_operations_never_overlap`。

---

## File Structure

| ファイル | 責務 |
|---|---|
| `host/api/fprobe_api.json` | API の正（メソッド・通知・誤りのコード） |
| `host/fprobe/api/__init__.py` | パッケージ（`API_VERSION`、誤りのコードの定数） |
| `host/fprobe/api/spec.py` | 仕様の読み込みと引数の検証 |
| `host/fprobe/api/gen.py` | 生成: TypeScript の型（`host/api/generated/fprobe_api.ts`）、文書（`docs/api.md`） |
| `host/fprobe/api/__main__.py` | `python -m fprobe.api gen` |
| `host/fprobe/api/arbiter.py` | 調停（作業スレッド、一時停止・保留・再開） |
| `host/fprobe/api/activity.py` | 記録（JSON Lines と購読者への配信） |
| `host/fprobe/api/daemon.py` | デーモン本体（Starlette の WebSocket、JSON-RPC、認証、hello、通知）と `main` |
| `host/fprobe/api/client.py` | 同期の利用者ライブラリ（MCP・CLI・試験が使う） |
| `host/fprobe/mcp_server.py` | （変更）MCP はデーモンの利用者。道具を仕様から生成 |
| `host/pyproject.toml` | （変更）`fprobe-daemon` のスクリプト |
| `host/tests/test_api_spec.py`・`test_api_arbiter.py`・`test_api_daemon.py`・`test_api_mcp.py` | 単体試験 |
| `gateware/tests/hil/test_daemon_hw.py` | 実機試験 |

---

### Task 1: API の仕様と検証・生成

**Files:**
- Create: `host/api/fprobe_api.json`、`host/fprobe/api/__init__.py`、`host/fprobe/api/spec.py`、`host/fprobe/api/gen.py`、`host/fprobe/api/__main__.py`
- Create (generated): `host/api/generated/fprobe_api.ts`、`docs/api.md`
- Test: `host/tests/test_api_spec.py`

**Interfaces:**
- Produces: `fprobe.api.API_VERSION: int`、誤りのコードの定数 `E_VERSION=-32010, E_NO_HELLO=-32011, E_NOT_ALLOWED=-32012, E_DEVICE=-32020, E_NOT_CONNECTED=-32021, E_TIMEOUT=-32022, E_INVALID_PARAMS=-32602, E_METHOD_NOT_FOUND=-32601`
- Produces: `fprobe.api.spec.Method`（`name: str, kind: str, summary: str, impl: str, params: dict, result: dict`）、`Spec`（`api_version: int, methods: dict[str, Method], notifications: dict[str, dict]`）、`load_spec(path: Path | None = None) -> Spec`、`Spec.validate_params(name: str, params: dict) -> None`（不正なら `InvalidParams(message)` を送出）、`InvalidParams(Exception)`
- Produces: `fprobe.api.gen.render_typescript(spec) -> str`、`render_markdown(spec) -> str`、`write_all() -> list[Path]`

- [ ] **Step 1: 仕様のファイルを、既存の Probe から一度だけ起こす**

`host/api/fprobe_api.json` は以後手で直す正。最初の内容は、既存の `mcp_server.Probe` の道具（`build_server` の一覧）の引数の型と docstring から一時的なスクリプトで作る（スクリプトは残さない）。名前の対応は次の表に従う（表に無い道具は `<分野>.<道具名の後半>`）:

| 道具（impl） | メソッド名 | kind |
|---|---|---|
| probe_info / csr_read / csr_write / target_voltage | probe.info / probe.csr.read / probe.csr.write / probe.voltage | read / read / control / read |
| target_connect / target_disconnect / target_status / target_halt / target_resume / target_step / target_reset | target.connect / target.disconnect / target.status / target.halt / target.resume / target.step / target.reset | control（status だけ read） |
| read_registers / write_register / read_memory / write_memory / flash / program_ram | target.registers.read / target.registers.write / target.memory.read / target.memory.write / target.flash / target.program_ram | control |
| elf_load / symbol_info / breakpoint_set / breakpoint_clear / breakpoint_list / watchpoint_set / watchpoint_clear / run_until_halt / variable_read / variable_write / backtrace | debug.elf.load / debug.symbol.info / debug.breakpoint.set / debug.breakpoint.clear / debug.breakpoint.list / debug.watchpoint.set / debug.watchpoint.clear / debug.run_until_halt / debug.variable.read / debug.variable.write / debug.backtrace | control（symbol.info と breakpoint.list は read） |
| la_capture / la_arm / la_collect / la_deep_capture / la_deep_arm / la_deep_collect / la_stream | la.capture / la.arm / la.collect / la.deep.capture / la.deep.arm / la.deep.collect / la.stream | control |
| events_start / events_stop / events_read | events.start / events.stop / events.read | control |
| xtrig_* / swo_* / uart_* | xtrig.* / swo.* / uart.*（後半の `_` は `.` に） | control（xtrig_status・xtrig_sequence_status は read） |

一時的なスクリプト（scratch に置く）。`la_capture` だけは Probe 側が `**trigger` を取るので、引数は
`mcp_server._la_capture(probe)` が返す関数（引数を明示した版）の署名から取る:

```python
import inspect, json, types, typing
from fprobe.mcp_server import Probe, _la_capture

# impl → (メソッド名, kind)。build_server の一覧と同じ 50 個
MAP = {
    "probe_info": ("probe.info", "read"), "csr_read": ("probe.csr.read", "read"),
    "csr_write": ("probe.csr.write", "control"), "target_voltage": ("probe.voltage", "read"),
    "target_connect": ("target.connect", "control"), "target_disconnect": ("target.disconnect", "control"),
    "target_status": ("target.status", "read"), "target_halt": ("target.halt", "control"),
    "target_resume": ("target.resume", "control"), "target_step": ("target.step", "control"),
    "target_reset": ("target.reset", "control"), "read_registers": ("target.registers.read", "control"),
    "write_register": ("target.registers.write", "control"), "read_memory": ("target.memory.read", "control"),
    "write_memory": ("target.memory.write", "control"), "flash": ("target.flash", "control"),
    "program_ram": ("target.program_ram", "control"),
    "elf_load": ("debug.elf.load", "control"), "symbol_info": ("debug.symbol.info", "read"),
    "breakpoint_set": ("debug.breakpoint.set", "control"), "breakpoint_clear": ("debug.breakpoint.clear", "control"),
    "breakpoint_list": ("debug.breakpoint.list", "read"), "watchpoint_set": ("debug.watchpoint.set", "control"),
    "watchpoint_clear": ("debug.watchpoint.clear", "control"), "run_until_halt": ("debug.run_until_halt", "control"),
    "variable_read": ("debug.variable.read", "control"), "variable_write": ("debug.variable.write", "control"),
    "backtrace": ("debug.backtrace", "control"),
    "la_capture": ("la.capture", "control"), "la_arm": ("la.arm", "control"), "la_collect": ("la.collect", "control"),
    "la_deep_capture": ("la.deep.capture", "control"), "la_deep_arm": ("la.deep.arm", "control"),
    "la_deep_collect": ("la.deep.collect", "control"), "la_stream": ("la.stream", "control"),
    "events_start": ("events.start", "control"), "events_stop": ("events.stop", "control"),
    "events_read": ("events.read", "control"),
    "xtrig_route": ("xtrig.route", "control"), "xtrig_set_inject": ("xtrig.set_inject", "control"),
    "xtrig_set_match": ("xtrig.set_match", "control"), "xtrig_set_sequence": ("xtrig.set_sequence", "control"),
    "xtrig_sequence_status": ("xtrig.sequence_status", "read"), "xtrig_fire": ("xtrig.fire", "control"),
    "xtrig_status": ("xtrig.status", "read"),
    "swo_start": ("swo.start", "control"), "swo_read": ("swo.read", "control"), "swo_stop": ("swo.stop", "control"),
    "uart_write": ("uart.write", "control"), "uart_read": ("uart.read", "control"),
}
TYPES = {int: {"type": "integer"}, float: {"type": "number"}, str: {"type": "string"}, bool: {"type": "boolean"}}


def schema_of(annotation):
    origin, args = typing.get_origin(annotation), typing.get_args(annotation)
    if origin in (typing.Union, types.UnionType) and type(None) in args:
        return {"anyOf": [schema_of(next(a for a in args if a is not type(None))), {"type": "null"}]}
    if origin is list:
        return {"type": "array", "items": schema_of(args[0]) if args else {}}
    return TYPES.get(annotation, {})


def params_of(fn, skip_self):
    sig, hints = inspect.signature(fn), typing.get_type_hints(fn)
    props, required = {}, []
    for p in list(sig.parameters.values())[1 if skip_self else 0:]:
        s = schema_of(hints.get(p.name, str))
        if p.default is inspect.Parameter.empty:
            required.append(p.name)
        else:
            s["default"] = p.default
        props[p.name] = s
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


out = {"api_version": 1, "methods": {}, "notifications": {
    "activity": {"summary": "操作の記録（番号、時刻、利用者、メソッド、引数・結果の要約か誤り、所要時間）"},
    "control": {"summary": "操作権の状態（paused、paused_by、保留の数 held）"},
    "target.state": {"summary": "ターゲットの実行状態（実行中 / 停止と理由・場所）"},
    "uart.data": {"summary": "ターゲットの UART から受け取ったバイト"},
    "swo.data": {"summary": "SWO から受け取ったバイト"}}}
for impl, (name, kind) in MAP.items():
    fn = _la_capture(Probe()) if impl == "la_capture" else getattr(Probe, impl)
    out["methods"][name] = {"kind": kind, "summary": inspect.getdoc(fn) or "", "impl": impl,
                            "params": params_of(fn, skip_self=impl != "la_capture"), "result": {}}
json.dump(out, open("host/api/fprobe_api.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
```

`session.*`（hello・status・subscribe・control.pause・control.resume）は `impl` を持たないデーモン自身のメソッドとして手で足す:

```json
"session.hello": {"kind": "read", "summary": "名乗る（最初に必ず呼ぶ）。api_version が合わなければ断られる。",
  "params": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"enum": ["human", "ai", "script"]},
    "api_version": {"type": "integer"}, "token": {"type": ["string", "null"], "default": null}},
    "required": ["name", "role", "api_version"], "additionalProperties": false},
  "result": {"type": "object"}},
"session.status": {"kind": "read", "summary": "デーモンの状態（機器、利用者の一覧、一時停止の状態、保留の数）。",
  "params": {"type": "object", "properties": {}, "additionalProperties": false}, "result": {"type": "object"}},
"session.subscribe": {"kind": "read", "summary": "受け取る通知の種類を設定する。",
  "params": {"type": "object", "properties": {"topics": {"type": "array", "items": {"enum": ["activity", "control", "target.state", "uart.data", "swo.data"]}}},
    "required": ["topics"], "additionalProperties": false}, "result": {"type": "object"}},
"session.control.pause": {"kind": "read", "summary": "一時停止（human だけ）。以後 human 以外の control の要求は保留される。",
  "params": {"type": "object", "properties": {}, "additionalProperties": false}, "result": {"type": "object"}},
"session.control.resume": {"kind": "read", "summary": "再開（human だけ）。保留していた要求を受けた順に処理する。",
  "params": {"type": "object", "properties": {}, "additionalProperties": false}, "result": {"type": "object"}}
```

- [ ] **Step 2: 失敗する試験を書く**

```python
# host/tests/test_api_spec.py
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
```

- [ ] **Step 3: 試験が失敗することを確かめる**

Run: `.venv/Scripts/python -m pytest host/tests/test_api_spec.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'fprobe.api'`）

- [ ] **Step 4: 実装する**

```python
# host/fprobe/api/__init__.py
"""fprobe の API（デーモンと利用者の約束）。仕様の正は host/api/fprobe_api.json。"""

API_VERSION = 1

# 誤りのコード（JSON-RPC の error.code。-32000〜-32099 はアプリケーション定義）
E_METHOD_NOT_FOUND = -32601
E_INVALID_PARAMS = -32602
E_VERSION = -32010
E_NO_HELLO = -32011
E_NOT_ALLOWED = -32012
E_DEVICE = -32020
E_NOT_CONNECTED = -32021
E_TIMEOUT = -32022
```

```python
# host/fprobe/api/spec.py
"""API の仕様（host/api/fprobe_api.json）の読み込みと引数の検証。"""

import json
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator

__all__ = ["SPEC_PATH", "InvalidParams", "Method", "Spec", "load_spec"]

SPEC_PATH = Path(__file__).resolve().parents[2] / "api" / "fprobe_api.json"


class InvalidParams(Exception):
    """引数が仕様に合わない。"""


@dataclass(frozen=True)
class Method:
    name: str
    kind: str            # "read" / "control"
    summary: str
    impl: str            # Probe のメソッド名（session.* は ""）
    params: dict
    result: dict


@dataclass(frozen=True)
class Spec:
    api_version: int
    methods: dict[str, Method]
    notifications: dict[str, dict]

    def validate_params(self, name: str, params: dict) -> None:
        method = self.methods[name]                     # 未知のメソッドは KeyError
        errors = sorted(Draft202012Validator(method.params).iter_errors(params), key=lambda e: e.path)
        if errors:
            e = errors[0]
            where = ".".join(str(p) for p in e.path) or "(引数)"
            raise InvalidParams(f"{name}: {where}: {e.message}")


def load_spec(path: Path | None = None) -> Spec:
    data = json.loads((path or SPEC_PATH).read_text(encoding="utf-8"))
    methods = {name: Method(name, m["kind"], m.get("summary", ""), m.get("impl", ""), m["params"], m.get("result", {}))
               for name, m in data["methods"].items()}
    return Spec(data["api_version"], methods, data.get("notifications", {}))
```

```python
# host/fprobe/api/gen.py
"""API の仕様から生成する: TypeScript の型（画面用）、文書（docs/api.md）。"""

import json
from pathlib import Path

from .spec import SPEC_PATH, Spec, load_spec

__all__ = ["render_markdown", "render_typescript", "write_all"]

ROOT = SPEC_PATH.parents[2]
TS_PATH = SPEC_PATH.parent / "generated" / "fprobe_api.ts"
MD_PATH = ROOT / "docs" / "api.md"
HEADER = "自動生成ファイル。直接編集しないこと（python -m fprobe.api gen で再生成）。生成元: host/api/fprobe_api.json"


def _ts_type(schema: dict) -> str:
    if "enum" in schema:
        return " | ".join(json.dumps(v) for v in schema["enum"])
    if "anyOf" in schema:
        return " | ".join(_ts_type(s) for s in schema["anyOf"])
    t = schema.get("type")
    if isinstance(t, list):
        return " | ".join(_ts_type({"type": x}) for x in t)
    return {"integer": "number", "number": "number", "string": "string", "boolean": "boolean", "null": "null",
            "array": f"Array<{_ts_type(schema.get('items', {}))}>", "object": "Record<string, unknown>"}.get(t, "unknown")


def render_typescript(spec: Spec) -> str:
    lines = [f"// {HEADER}", f"export const API_VERSION = {spec.api_version};", "", "export interface Methods {"]
    for m in spec.methods.values():
        props = m.params.get("properties", {})
        required = set(m.params.get("required", []))
        fields = "; ".join(f"{k}{'' if k in required else '?'}: {_ts_type(v)}" for k, v in props.items())
        lines.append(f"  {json.dumps(m.name)}: {{ kind: {json.dumps(m.kind)}; params: {{ {fields} }} }};")
    lines += ["}", "", "export type Topic = " + " | ".join(json.dumps(t) for t in spec.notifications) + ";", ""]
    return "\n".join(lines)


def render_markdown(spec: Spec) -> str:
    out = [f"<!-- {HEADER} -->", "# fprobe API", "",
           f"JSON-RPC 2.0 over WebSocket（`ws://127.0.0.1:7820/api`）。API の版 {spec.api_version}。"
           "仕様: docs/superpowers/specs/2026-10-17-host-daemon-design.md", ""]
    for m in spec.methods.values():
        out += [f"## {m.name}", "", f"種類: `{m.kind}`。{m.summary}", ""]
        props = m.params.get("properties", {})
        required = set(m.params.get("required", []))
        if props:
            out += ["| 引数 | 型 | 必須 |", "|---|---|---|"]
            out += [f"| `{k}` | `{_ts_type(v)}` | {'○' if k in required else ''} |" for k, v in props.items()]
            out.append("")
    out += ["## 通知", ""] + [f"- `{k}`: {v.get('summary', '')}" for k, v in spec.notifications.items()] + [""]
    return "\n".join(out)


def write_all(check: bool = False) -> list[Path]:
    """生成物を書く。check=True なら書かずに一致を確かめ、違えば AssertionError。"""
    spec = load_spec()
    outputs = {TS_PATH: render_typescript(spec), MD_PATH: render_markdown(spec)}
    for path, text in outputs.items():
        if check:
            assert path.read_text(encoding="utf-8") == text, f"{path} が古い（python -m fprobe.api gen）"
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
    return list(outputs)
```

```python
# host/fprobe/api/__main__.py
"""python -m fprobe.api gen: API の仕様から生成物を作り直す。"""

import sys

from .gen import write_all

if __name__ == "__main__":
    if sys.argv[1:] != ["gen"]:
        raise SystemExit("使い方: python -m fprobe.api gen")
    for path in write_all():
        print(path)
```

- [ ] **Step 5: 生成して試験が通ることを確かめる**

Run: `.venv/Scripts/python -m fprobe.api gen && .venv/Scripts/python -m pytest host/tests/test_api_spec.py -q`
Expected: `3 passed`

- [ ] **Step 6: Commit**

```bash
git add host/api host/fprobe/api host/tests/test_api_spec.py docs/api.md
git commit -m "feat(api): API の仕様（JSON Schema）と検証・TypeScript の型と文書の生成"
```

---

### Task 2: 調停と記録

**Files:**
- Create: `host/fprobe/api/arbiter.py`、`host/fprobe/api/activity.py`
- Test: `host/tests/test_api_arbiter.py`

**Interfaces:**
- Consumes: なし（純粋な部品。機器は呼び出し側が関数で渡す）
- Produces: `Client`（`id: int, name: str, role: str`）、`Arbiter(on_control: Callable[[dict], None])`、`Arbiter.submit(client: Client, kind: str, fn: Callable[[], Any]) -> concurrent.futures.Future`、`Arbiter.pause(client) -> dict`、`Arbiter.resume(client) -> dict`、`Arbiter.leave(client) -> None`（その利用者の保留を取り消し、一時停止を解く）、`Arbiter.state() -> dict`（`{"paused": bool, "paused_by": str | None, "held": int}`）、`Arbiter.close()`、`NotAllowed(Exception)`
- Produces: `ActivityLog(directory: Path, on_entry: Callable[[dict], None])`、`ActivityLog.record(entry: dict) -> dict`（番号と時刻を付けて追記・配信して返す）、`ActivityLog.path: Path`、`summarize(value: Any, limit: int = 200) -> Any`（大きな結果を要約）

- [ ] **Step 1: 失敗する試験を書く**

```python
# host/tests/test_api_arbiter.py
"""fprobe.api.arbiter / activity: 操作の調停（1 本の作業スレッド、一時停止・保留・再開）と記録。"""

import threading
import time

import pytest

from fprobe.api.activity import ActivityLog, summarize
from fprobe.api.arbiter import Arbiter, Client, NotAllowed

HUMAN, AI = Client(1, "gui", "human"), Client(2, "claude", "ai")


@pytest.fixture
def arbiter():
    events = []
    a = Arbiter(on_control=events.append)
    a.events = events
    yield a
    a.close()


def test_operations_never_overlap(arbiter):
    running, overlaps = [0], []

    def op():
        running[0] += 1
        overlaps.append(running[0])
        time.sleep(0.01)
        running[0] -= 1
        return 1

    futures = [arbiter.submit(c, "control", op) for c in (HUMAN, AI) for _ in range(10)]
    assert sum(f.result(5) for f in futures) == 20 and max(overlaps) == 1


def test_pause_holds_ai_control_but_not_reads_or_human(arbiter):
    arbiter.pause(HUMAN)
    held = arbiter.submit(AI, "control", lambda: "ai")
    assert arbiter.submit(AI, "read", lambda: "read").result(2) == "read"
    assert arbiter.submit(HUMAN, "control", lambda: "human").result(2) == "human"
    time.sleep(0.05)
    assert not held.done() and arbiter.state() == {"paused": True, "paused_by": "gui", "held": 1}
    arbiter.resume(HUMAN)
    assert held.result(2) == "ai" and arbiter.state()["held"] == 0
    assert [e["paused"] for e in arbiter.events] == [True, False]


def test_only_humans_can_pause(arbiter):
    with pytest.raises(NotAllowed):
        arbiter.pause(AI)


def test_pause_released_when_pauser_leaves(arbiter):
    arbiter.pause(HUMAN)
    held = arbiter.submit(AI, "control", lambda: 7)
    arbiter.leave(HUMAN)
    assert held.result(2) == 7 and arbiter.state()["paused"] is False


def test_leaving_cancels_own_held_requests(arbiter):
    arbiter.pause(HUMAN)
    held = arbiter.submit(AI, "control", lambda: 7)
    arbiter.leave(AI)
    assert held.cancelled()


def test_errors_propagate_to_the_caller(arbiter):
    def boom():
        raise RuntimeError("device")
    with pytest.raises(RuntimeError, match="device"):
        arbiter.submit(AI, "control", boom).result(2)
    assert arbiter.submit(AI, "control", lambda: "still alive").result(2) == "still alive"


def test_activity_is_numbered_written_and_delivered(tmp_path):
    seen = []
    log = ActivityLog(tmp_path, on_entry=seen.append)
    a = log.record({"client": "claude", "method": "target.halt"})
    b = log.record({"client": "gui", "method": "probe.info"})
    assert (a["seq"], b["seq"]) == (1, 2) and "time" in a
    assert log.path.read_text(encoding="utf-8").count("\n") == 2 and seen == [a, b]


def test_large_results_are_summarized():
    assert summarize("ab" * 500)["length"] == 1000
    assert summarize(list(range(1000)))["length"] == 1000
    assert summarize({"x": 1}) == {"x": 1}
```

- [ ] **Step 2: 試験が失敗することを確かめる**

Run: `.venv/Scripts/python -m pytest host/tests/test_api_arbiter.py -q`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 実装する**

```python
# host/fprobe/api/arbiter.py
"""操作の調停: 機器（fprobe・pyOCD）はスレッド安全でないので、すべての操作を 1 本の作業スレッドで順に実行する。
一時停止中（human が pause）は、human 以外の control の要求を保留し、resume で受けた順に流す。"""

import queue
import threading
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable

__all__ = ["Arbiter", "Client", "NotAllowed"]


class NotAllowed(Exception):
    """この利用者には許されない操作（一時停止・再開は human だけ）。"""


@dataclass(frozen=True)
class Client:
    id: int
    name: str
    role: str            # "human" / "ai" / "script"


class Arbiter:
    def __init__(self, on_control: Callable[[dict], None]):
        self._on_control = on_control
        self._lock = threading.Lock()
        self._ready: queue.Queue = queue.Queue()
        self._held: deque[tuple[Client, Callable[[], Any], Future]] = deque()
        self._paused_by: Client | None = None
        self._thread = threading.Thread(target=self._work, name="fprobe-arbiter", daemon=True)
        self._thread.start()

    def submit(self, client: Client, kind: str, fn: Callable[[], Any]) -> Future:
        future: Future = Future()
        with self._lock:
            if self._paused_by is not None and kind == "control" and client.role != "human":
                self._held.append((client, fn, future))
                held = True
            else:
                held = False
        if held:
            self._notify()
        else:
            self._ready.put((fn, future))
        return future

    def pause(self, client: Client) -> dict:
        if client.role != "human":
            raise NotAllowed("一時停止できるのは human だけ")
        with self._lock:
            self._paused_by = client
        self._notify()
        return self.state()

    def resume(self, client: Client) -> dict:
        if client.role != "human":
            raise NotAllowed("再開できるのは human だけ")
        self._release()
        return self.state()

    def leave(self, client: Client) -> None:
        with self._lock:
            kept = deque()
            for item in self._held:
                if item[0].id == client.id:
                    item[2].cancel()
                else:
                    kept.append(item)
            self._held = kept
            pauser_left = self._paused_by is not None and self._paused_by.id == client.id
        if pauser_left:
            self._release()                    # 止めた人がいなくなったら AI を永久に待たせない
        else:
            self._notify()

    def state(self) -> dict:
        with self._lock:
            return {"paused": self._paused_by is not None,
                    "paused_by": self._paused_by.name if self._paused_by else None, "held": len(self._held)}

    def close(self) -> None:
        self._ready.put(None)
        self._thread.join(timeout=5)

    def _release(self) -> None:
        with self._lock:
            self._paused_by = None
            held, self._held = self._held, deque()
        for _, fn, future in held:
            self._ready.put((fn, future))
        self._notify()

    def _notify(self) -> None:
        self._on_control(self.state())

    def _work(self) -> None:
        while True:
            item = self._ready.get()
            if item is None:
                return
            fn, future = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn())
            except BaseException as e:          # 機器の誤りも含めて呼び出し側へ。作業スレッドは止めない
                future.set_exception(e)
```

```python
# host/fprobe/api/activity.py
"""操作の記録: 1 操作 = 1 件。JSON Lines のファイルに追記し、購読者へ配る。"""

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

__all__ = ["ActivityLog", "summarize"]


def summarize(value: Any, limit: int = 200) -> Any:
    """記録に残す形: 長い文字列・列は長さと先頭だけ。"""
    if isinstance(value, (str, bytes)) and len(value) > limit:
        head = value[:limit] if isinstance(value, str) else value[:limit].hex()
        return {"length": len(value), "head": head}
    if isinstance(value, (list, tuple)) and len(value) > 32:
        return {"length": len(value), "head": list(value[:8])}
    return value


class ActivityLog:
    def __init__(self, directory: Path, on_entry: Callable[[dict], None]):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{datetime.now():%Y%m%d-%H%M%S}.jsonl"
        self._on_entry = on_entry
        self._lock = threading.Lock()
        self._seq = 0

    def record(self, entry: dict) -> dict:
        with self._lock:
            self._seq += 1
            entry = {"seq": self._seq, "time": datetime.now().isoformat(timespec="milliseconds"), **entry}
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        self._on_entry(entry)
        return entry
```

- [ ] **Step 4: 試験が通ることを確かめる**

Run: `.venv/Scripts/python -m pytest host/tests/test_api_arbiter.py -q`
Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
git add host/fprobe/api/arbiter.py host/fprobe/api/activity.py host/tests/test_api_arbiter.py
git commit -m "feat(api): 操作の調停（1 本の作業スレッド、一時停止・保留・再開）と記録"
```

---

### Task 3: デーモン本体と利用者ライブラリ

**Files:**
- Create: `host/fprobe/api/daemon.py`、`host/fprobe/api/client.py`
- Modify: `host/pyproject.toml`（`[project.scripts]` に `fprobe-daemon = "fprobe.api.daemon:main"`）
- Test: `host/tests/test_api_daemon.py`

**Interfaces:**
- Consumes: `load_spec`、`Spec.validate_params`、`InvalidParams`（Task 1）、`Arbiter`・`Client`・`NotAllowed`・`ActivityLog`・`summarize`（Task 2）、誤りのコード（Task 1）
- Produces: `create_app(backend: Any, *, token: str | None, activity_dir: Path, spec: Spec | None = None) -> starlette.applications.Starlette`（`backend` は `Probe` と同じ名前のメソッドを持つ物）、`Daemon`（アプリの状態: `arbiter`、`activity`）、`main()`（引数 `--serial/--port/--baud/--eth/--host/--port-number/--lan/--token`）
- Produces: `DaemonClient(url: str, name: str, role: str, token: str | None = None)`、`.call(method: str, **params) -> Any`（誤りは `ApiError(code, message)` を送出）、`.subscribe(topics: list[str], callback: Callable[[str, dict], None])`、`.close()`、`ApiError(Exception)`（`.code: int`）

- [ ] **Step 1: 失敗する試験を書く**

```python
# host/tests/test_api_daemon.py
"""fprobe.api.daemon / client: 偽の機器（backend）でデーモンを動かし、2 つの利用者から使う。"""

import threading
import time

import pytest
import uvicorn

from fprobe.api import E_INVALID_PARAMS, E_NO_HELLO, E_NOT_CONNECTED, E_VERSION
from fprobe.api.client import ApiError, DaemonClient
from fprobe.api.daemon import create_app


class FakeBackend:
    """Probe と同じ名前のメソッドの一部を持つ偽物。"""

    def __init__(self):
        self.halted = False

    def target_halt(self):
        time.sleep(0.01)
        self.halted = True
        return {"state": "HALTED"}

    def target_status(self):
        return {"state": "HALTED" if self.halted else "RUNNING"}

    def run_until_halt(self, timeout_s=5.0):
        raise RuntimeError("ターゲット未接続（target_connect を先に呼ぶ）")


@pytest.fixture
def daemon(tmp_path):
    app = create_app(FakeBackend(), token=None, activity_dir=tmp_path)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"ws://127.0.0.1:{port}/api", app
    server.should_exit = True
    thread.join(5)


def test_two_clients_operate_and_activity_flows(daemon):
    url, app = daemon
    gui = DaemonClient(url, "gui", "human")
    ai = DaemonClient(url, "claude", "ai")
    seen = []
    gui.subscribe(["activity"], lambda topic, data: seen.append(data))
    assert ai.call("target.halt") == {"state": "HALTED"}
    assert gui.call("target.status") == {"state": "HALTED"}
    deadline = time.monotonic() + 2
    while len(seen) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [(e["client"], e["method"]) for e in seen[:2]] == [("claude", "target.halt"), ("gui", "target.status")]
    gui.close(); ai.close()


def test_pause_holds_the_ai_until_resume(daemon):
    url, _ = daemon
    gui = DaemonClient(url, "gui", "human")
    ai = DaemonClient(url, "claude", "ai")
    gui.call("session.control.pause")
    result = []
    t = threading.Thread(target=lambda: result.append(ai.call("target.halt")))
    t.start()
    time.sleep(0.2)
    assert result == [] and gui.call("session.status")["control"]["held"] == 1
    gui.call("session.control.resume")
    t.join(2)
    assert result == [{"state": "HALTED"}]
    gui.close(); ai.close()


def test_params_and_errors(daemon):
    url, _ = daemon
    ai = DaemonClient(url, "claude", "ai")
    with pytest.raises(ApiError) as e:
        ai.call("debug.run_until_halt", timeout_s="soon")
    assert e.value.code == E_INVALID_PARAMS


def test_device_error_is_reported_and_daemon_survives(daemon):
    url, _ = daemon
    ai = DaemonClient(url, "claude", "ai")
    with pytest.raises(ApiError) as e:
        ai.call("debug.run_until_halt", timeout_s=1.0)
    assert e.value.code == E_NOT_CONNECTED and "未接続" in str(e.value)       # 未接続は専用のコード
    assert ai.call("target.status")["state"] in ("HALTED", "RUNNING")


def test_hello_and_version_are_required(daemon):
    url, _ = daemon
    with pytest.raises(ApiError) as e:
        DaemonClient(url, "old", "ai", api_version=999)
    assert e.value.code == E_VERSION
    raw = DaemonClient.raw(url)
    assert raw.request("target.status", {})["error"]["code"] == E_NO_HELLO


def test_disconnect_drops_pending(daemon):
    url, _ = daemon
    gui = DaemonClient(url, "gui", "human")
    ai = DaemonClient(url, "claude", "ai")
    gui.call("session.control.pause")
    threading.Thread(target=lambda: _swallow(lambda: ai.call("target.halt")), daemon=True).start()
    time.sleep(0.2)
    ai.close()
    time.sleep(0.2)
    assert gui.call("session.status")["control"]["held"] == 0
    gui.close()


def _swallow(fn):
    try:
        fn()
    except Exception:
        pass


def test_lan_requires_the_token(tmp_path):
    from starlette.testclient import TestClient
    app = create_app(FakeBackend(), token="s3cret", activity_dir=tmp_path)
    with TestClient(app) as tc, tc.websocket_connect("/api") as ws:
        ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "session.hello",
                      "params": {"name": "x", "role": "ai", "api_version": 1}})
        assert ws.receive_json()["error"]["code"] == -32012
```

- [ ] **Step 2: 試験が失敗することを確かめる**

Run: `.venv/Scripts/python -m pytest host/tests/test_api_daemon.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'fprobe.api.daemon'`）

- [ ] **Step 3: デーモンを実装する**

```python
# host/fprobe/api/daemon.py
"""fprobe-daemon: fprobe を 1 つ握り、JSON-RPC 2.0 over WebSocket で全機能を出す。
仕様: docs/superpowers/specs/2026-10-17-host-daemon-design.md

    fprobe-daemon [--serial 0001 | --port COM3 | --eth 192.168.77.2] [--lan [--token T]]
"""

import argparse
import asyncio
import itertools
import os
import secrets
import time
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import (API_VERSION, E_DEVICE, E_INVALID_PARAMS, E_METHOD_NOT_FOUND, E_NO_HELLO, E_NOT_ALLOWED,
               E_NOT_CONNECTED, E_VERSION)
from .activity import ActivityLog, summarize
from .arbiter import Arbiter, Client, NotAllowed
from .spec import InvalidParams, Spec, load_spec

__all__ = ["DEFAULT_PORT", "create_app", "main"]

DEFAULT_PORT = 7820


def fprobe_home() -> Path:
    return Path(os.environ.get("FPROBE_HOME", Path.home() / ".fprobe"))


class _Rpc(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class _Connection:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.client: Client | None = None
        self.topics: set[str] = set()


class Daemon:
    def __init__(self, backend: Any, token: str | None, activity_dir: Path, spec: Spec):
        self.backend, self.token, self.spec = backend, token, spec
        self.connections: set[_Connection] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._ids = itertools.count(1)
        self.arbiter = Arbiter(on_control=lambda state: self.publish("control", state))
        self.activity = ActivityLog(activity_dir, on_entry=lambda entry: self.publish("activity", entry))

    # 作業スレッドからも呼ばれる: イベントループへ渡して配る
    def publish(self, topic: str, data: dict) -> None:
        if self.loop is None:
            return
        message = {"jsonrpc": "2.0", "method": topic, "params": data}
        for conn in list(self.connections):
            if topic in conn.topics:
                asyncio.run_coroutine_threadsafe(conn.ws.send_json(message), self.loop)

    async def handle(self, conn: _Connection, request: dict) -> dict | None:
        rid, method, params = request.get("id"), request.get("method", ""), request.get("params") or {}
        try:
            result = await self._dispatch(conn, method, params)
            return None if rid is None else {"jsonrpc": "2.0", "id": rid, "result": result}
        except _Rpc as e:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": e.code, "message": str(e)}}

    async def _dispatch(self, conn: _Connection, method: str, params: dict) -> Any:
        if method not in self.spec.methods:
            raise _Rpc(E_METHOD_NOT_FOUND, f"{method} は無い")
        try:
            self.spec.validate_params(method, params)
        except InvalidParams as e:
            raise _Rpc(E_INVALID_PARAMS, str(e)) from None
        if method == "session.hello":
            return self._hello(conn, params)
        if conn.client is None:
            raise _Rpc(E_NO_HELLO, "最初に session.hello を呼ぶ")
        if method == "session.status":
            return {"api_version": API_VERSION, "control": self.arbiter.state(),
                    "clients": [c.client.__dict__ for c in self.connections if c.client]}
        if method == "session.subscribe":
            conn.topics = set(params["topics"])
            return {"topics": sorted(conn.topics)}
        if method in ("session.control.pause", "session.control.resume"):
            try:
                act = self.arbiter.pause if method.endswith("pause") else self.arbiter.resume
                return act(conn.client)
            except NotAllowed as e:
                raise _Rpc(E_NOT_ALLOWED, str(e)) from None
        return await self._call(conn.client, self.spec.methods[method], params)

    def _hello(self, conn: _Connection, params: dict) -> dict:
        if self.token is not None and params.get("token") != self.token:
            raise _Rpc(E_NOT_ALLOWED, "トークンが違う（--lan のデーモンにはトークンが要る）")
        if params["api_version"] != API_VERSION:
            raise _Rpc(E_VERSION, f"API の版 {params['api_version']} はこのデーモン（{API_VERSION}）と合わない")
        conn.client = Client(next(self._ids), params["name"], params["role"])
        return {"api_version": API_VERSION, "client_id": conn.client.id, "control": self.arbiter.state()}

    async def _call(self, client: Client, method, params: dict) -> Any:
        fn = getattr(self.backend, method.impl)
        started = time.monotonic()
        future = self.arbiter.submit(client, method.kind, lambda: fn(**params))
        entry = {"client": client.name, "role": client.role, "method": method.name, "params": summarize(params)}
        try:
            result = await asyncio.wrap_future(future)
        except Exception as e:
            code = E_NOT_CONNECTED if "未接続" in str(e) else E_DEVICE
            self.activity.record({**entry, "error": str(e), "seconds": round(time.monotonic() - started, 4)})
            raise _Rpc(code, f"{type(e).__name__}: {e}") from None
        self.activity.record({**entry, "result": summarize(result), "seconds": round(time.monotonic() - started, 4)})
        return result


def create_app(backend: Any, *, token: str | None, activity_dir: Path, spec: Spec | None = None) -> Starlette:
    daemon = Daemon(backend, token, activity_dir, spec or load_spec())

    async def api(ws: WebSocket) -> None:
        await ws.accept()
        daemon.loop = asyncio.get_running_loop()
        conn = _Connection(ws)
        daemon.connections.add(conn)
        tasks: set[asyncio.Task] = set()

        async def serve(request: dict) -> None:
            reply = await daemon.handle(conn, request)
            if reply is not None:
                await ws.send_json(reply)

        try:
            while True:
                request = await ws.receive_json()
                task = asyncio.create_task(serve(request))      # 長い操作の間も他の要求（read）を受ける
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except WebSocketDisconnect:
            pass
        finally:
            daemon.connections.discard(conn)
            if conn.client is not None:
                daemon.arbiter.leave(conn.client)
            for task in tasks:
                task.cancel()

    app = Starlette(routes=[WebSocketRoute("/api", api)])
    app.state.daemon = daemon
    return app


def main() -> None:
    import uvicorn
    from ..mcp_server import Probe
    from ..serial_link import DEFAULT_BAUD
    parser = argparse.ArgumentParser(description="fprobe-daemon: fprobe を握って API を出す")
    parser.add_argument("--serial", help="fprobe の USB シリアル番号")
    parser.add_argument("--port", help="SerialLink の COM ポート")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument("--eth", help="EthLink のボードの IP アドレス")
    parser.add_argument("--listen", type=int, default=DEFAULT_PORT, help="待ち受けるポート")
    parser.add_argument("--lan", action="store_true", help="LAN にも開く（トークンが要る）")
    parser.add_argument("--token", help="--lan のトークン（省略時は生成して表示）")
    args = parser.parse_args()
    token = (args.token or secrets.token_urlsafe(16)) if args.lan else None
    if token:
        print(f"トークン: {token}")
    backend = Probe(args.serial, args.port, args.baud, args.eth)
    app = create_app(backend, token=token, activity_dir=fprobe_home() / "activity")
    try:
        uvicorn.run(app, host="0.0.0.0" if args.lan else "127.0.0.1", port=args.listen, log_level="warning")
    finally:
        app.state.daemon.arbiter.close()
        backend.close()
```

- [ ] **Step 4: 利用者ライブラリを実装する**

```python
# host/fprobe/api/client.py
"""デーモンの利用者（同期）。MCP・CLI・スクリプトが使う。

    with DaemonClient("ws://127.0.0.1:7820/api", "my-script", "script") as c:
        c.call("target.halt")
"""

import itertools
import json
import threading
from concurrent.futures import Future
from typing import Any, Callable

from websockets.sync.client import connect

from . import API_VERSION

__all__ = ["ApiError", "DaemonClient"]


class ApiError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class DaemonClient:
    def __init__(self, url: str, name: str, role: str, token: str | None = None, api_version: int = API_VERSION,
                 _hello: bool = True):
        self._ws = connect(url, max_size=None, ping_interval=None)
        self._ids = itertools.count(1)
        self._pending: dict[int, Future] = {}
        self._lock = threading.Lock()
        self._listeners: list[tuple[set[str], Callable[[str, dict], None]]] = []
        self._reader = threading.Thread(target=self._read, name="fprobe-api-client", daemon=True)
        self._reader.start()
        if _hello:
            try:
                self.hello = self.call("session.hello", name=name, role=role, api_version=api_version, token=token)
            except BaseException:
                self.close()
                raise

    @classmethod
    def raw(cls, url: str) -> "DaemonClient":
        """hello せずにつなぐ（試験用）。request で生の応答を見る。"""
        return cls(url, "", "", _hello=False)

    def request(self, method: str, params: dict) -> dict:
        rid = next(self._ids)
        future: Future = Future()
        with self._lock:
            self._pending[rid] = future
        self._ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
        return future.result()

    def call(self, method: str, **params: Any) -> Any:
        reply = self.request(method, params)
        if "error" in reply:
            raise ApiError(reply["error"]["code"], reply["error"]["message"])
        return reply["result"]

    def subscribe(self, topics: list[str], callback: Callable[[str, dict], None]) -> None:
        self._listeners.append((set(topics), callback))
        self.call("session.subscribe", topics=sorted({t for ts, _ in self._listeners for t in ts}))

    def close(self) -> None:
        self._ws.close()
        self._reader.join(timeout=2)

    def __enter__(self) -> "DaemonClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _read(self) -> None:
        try:
            for text in self._ws:
                message = json.loads(text)
                if "id" in message and message["id"] is not None:
                    with self._lock:
                        future = self._pending.pop(message["id"], None)
                    if future is not None:
                        future.set_result(message)
                else:
                    for topics, callback in self._listeners:
                        if message["method"] in topics:
                            callback(message["method"], message["params"])
        except Exception:
            pass
        finally:
            with self._lock:
                pending, self._pending = self._pending, {}
            for future in pending.values():
                future.set_exception(ConnectionError("デーモンとの接続が切れた"))
```

- [ ] **Step 5: 試験が通ることを確かめる**

Run: `.venv/Scripts/python -m pytest host/tests/test_api_daemon.py -q`
Expected: `7 passed`

- [ ] **Step 6: `fprobe-daemon` のスクリプトを登録して Commit**

`host/pyproject.toml` の `[project.scripts]` に `fprobe-daemon = "fprobe.api.daemon:main"` を足し、
`.venv/Scripts/python -m pip install -e "host[dev,debug,mcp]" -q` で入れ直す。

```bash
git add host/fprobe/api/daemon.py host/fprobe/api/client.py host/tests/test_api_daemon.py host/pyproject.toml
git commit -m "feat(api): fprobe-daemon（JSON-RPC over WebSocket、認証、hello、通知）と利用者ライブラリ"
```

---

### Task 4: MCP をデーモンの利用者に

**Files:**
- Modify: `host/fprobe/mcp_server.py`（`build_server` を仕様から道具を生成する形に、`main` をデーモンにつなぐ形に。`Probe` クラスはデーモンの backend として残す）
- Test: `host/tests/test_api_mcp.py`

**Interfaces:**
- Consumes: `DaemonClient`・`ApiError`（Task 3）、`load_spec`（Task 1）、`create_app`・`DEFAULT_PORT`（Task 3）
- Produces: `build_client_server(client: DaemonClient, spec: Spec) -> MCPServer`（道具名は仕様のメソッド名の `.` を `_` にしたもの。例: `debug_run_until_halt`）、`ensure_daemon(args) -> str`（URL。つながらなければ `fprobe-daemon` を子プロセスで起動して待つ）

- [ ] **Step 1: 失敗する試験を書く**

```python
# host/tests/test_api_mcp.py
"""MCP はデーモンの利用者: 道具は仕様から生成し、呼び出しはデーモンへ転送する。"""

import asyncio
import inspect

from fprobe.api.spec import load_spec
from fprobe.mcp_server import build_client_server, tool_function


class FakeClient:
    def __init__(self):
        self.calls = []

    def call(self, method, **params):
        self.calls.append((method, params))
        return {"ok": method}


def test_tools_are_generated_from_the_spec():
    spec = load_spec()
    server = build_client_server(FakeClient(), spec)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert "debug_run_until_halt" in names and "target_halt" in names
    assert not any(n.startswith("session_") for n in names)            # セッションの操作は道具にしない


def test_a_tool_forwards_to_the_daemon_with_typed_arguments():
    spec = load_spec()
    client = FakeClient()
    fn = tool_function(client, spec.methods["debug.run_until_halt"])
    sig = inspect.signature(fn)
    assert sig.parameters["timeout_s"].default == 5.0
    assert fn(timeout_s=1.5) == {"ok": "debug.run_until_halt"}
    assert client.calls == [("debug.run_until_halt", {"timeout_s": 1.5})]
```

- [ ] **Step 2: 試験が失敗することを確かめる**

Run: `.venv/Scripts/python -m pytest host/tests/test_api_mcp.py -q`
Expected: FAIL（`ImportError: cannot import name 'build_client_server'`）

- [ ] **Step 3: 実装する（mcp_server.py に足す。Probe クラスと既存の build_server はデーモンの backend と既存の試験のために残す）**

```python
# host/fprobe/mcp_server.py に追加
import inspect as _inspect
import socket as _socket
import subprocess as _subprocess
import sys as _sys

from .api.client import DaemonClient
from .api.daemon import DEFAULT_PORT
from .api.spec import Method, Spec, load_spec

_PY_TYPES = {"integer": int, "number": float, "string": str, "boolean": bool}


def _annotation(schema: dict):
    if "anyOf" in schema:
        inner = [s for s in schema["anyOf"] if s.get("type") != "null"]
        return _annotation(inner[0]) | None if inner else type(None)
    if schema.get("type") == "array":
        return list[_annotation(schema.get("items", {}))]
    if "enum" in schema:
        return str
    return _PY_TYPES.get(schema.get("type"), str)


def tool_function(client, method: Method):
    """仕様のメソッドを、型付きの引数を持つ関数（MCP の道具）にする。呼ぶとデーモンへ転送する。"""
    props = method.params.get("properties", {})
    required = set(method.params.get("required", []))
    parameters = [
        _inspect.Parameter(name, _inspect.Parameter.KEYWORD_ONLY, annotation=_annotation(schema),
                           default=_inspect.Parameter.empty if name in required else schema.get("default"))
        for name, schema in props.items()]

    def tool(**kwargs):
        return client.call(method.name, **kwargs)

    tool.__signature__ = _inspect.Signature(parameters)
    tool.__annotations__ = {p.name: p.annotation for p in parameters}
    tool.__name__ = method.name.replace(".", "_")
    tool.__doc__ = method.summary
    return tool


def build_client_server(client, spec: Spec) -> MCPServer:
    server = MCPServer("fprobe", instructions=INSTRUCTIONS)
    for method in spec.methods.values():
        if method.name.startswith("session."):
            continue
        server.add_tool(tool_function(client, method), name=method.name.replace(".", "_"),
                        description=method.summary)
    return server


def ensure_daemon(args) -> str:
    """デーモンにつなぐ URL。動いていなければ子プロセスで起動し、待ち受けるまで待つ。"""
    url = f"ws://127.0.0.1:{DEFAULT_PORT}/api"
    if _listening(DEFAULT_PORT):
        return url
    command = [_sys.executable, "-m", "fprobe.api.daemon"]
    for flag, value in (("--serial", args.serial), ("--port", args.port), ("--eth", args.eth)):
        if value:
            command += [flag, value]
    if args.port:
        command += ["--baud", str(args.baud)]
    _subprocess.Popen(command, stdout=_subprocess.DEVNULL, stderr=_subprocess.DEVNULL,
                      creationflags=getattr(_subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    deadline = time.monotonic() + 20
    while not _listening(DEFAULT_PORT):
        if time.monotonic() > deadline:
            raise RuntimeError("fprobe-daemon が起動しない")
        time.sleep(0.2)
    return url


def _listening(port: int) -> bool:
    with _socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0
```

`main()` を次に置き換える（引数はそのまま）:

```python
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--serial", help="fprobe の USB シリアル番号（複数台あるとき）")
    parser.add_argument("--port", help="SerialLink（UART のホストリンク）の COM ポート。指定すると USB ではなくこれで開く")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="SerialLink のボーレート")
    parser.add_argument("--eth", help="EthLink（UDP のホストリンク）のボードの IP アドレス。指定すると USB ではなくこれで開く")
    args = parser.parse_args()
    client = DaemonClient(ensure_daemon(args), "mcp", "ai")
    try:
        build_client_server(client, load_spec()).run("stdio")
    finally:
        client.close()
```

`daemon.py` の末尾に `if __name__ == "__main__": main()` を足す（`python -m fprobe.api.daemon` で起動できるように）。

- [ ] **Step 4: 試験が通ることを確かめる**

Run: `.venv/Scripts/python -m pytest host -q`
Expected: 全件 passed（既存の試験を含む）

- [ ] **Step 5: Commit**

```bash
git add host/fprobe/mcp_server.py host/fprobe/api/daemon.py host/tests/test_api_mcp.py
git commit -m "feat(mcp): MCP はデーモンの利用者。道具を API の仕様から生成し、デーモンが無ければ起動する"
```

---

### Task 5: 実機試験と文書

**Files:**
- Create: `gateware/tests/hil/test_daemon_hw.py`
- Modify: `README.md`（「CLI と MCP サーバ」の節にデーモンを追記）、`docs/reports/2026-10-16-day.md`

**Interfaces:**
- Consumes: `create_app`（Task 3）、`DaemonClient`（Task 3）、`Probe`（既存）

- [ ] **Step 1: 実機試験を書く**

```python
# gateware/tests/hil/test_daemon_hw.py
"""fprobe-daemon（実機）: AI 役と人間役の 2 つの利用者が同じ fprobe を使い、記録が流れ、一時停止で AI が待つ。
ボードは USB 版（fprobe）。環境変数 FPROBE_ETH_HOST があれば Ethernet 版。"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from fprobe.api.client import DaemonClient
from fprobe.api.daemon import create_app
from fprobe.mcp_server import Probe
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
ELF = Path(__file__).resolve().parents[3] / "host" / "firmware" / "sram_demo" / "sram_demo.elf"
ETH_HOST = os.environ.get("FPROBE_ETH_HOST")


@pytest.fixture(scope="module")
def url(tmp_path_factory):
    if ETH_HOST:
        subprocess.run([sys.executable, "-m", "fprobe_gw", "build", "fprobe_eth", "--program"], check=True)
        backend = Probe(eth=ETH_HOST)
    else:
        program("fprobe")
        backend = Probe(SERIAL)
    app = create_app(backend, token=None, activity_dir=tmp_path_factory.mktemp("activity"))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    yield f"ws://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}/api"
    server.should_exit = True
    thread.join(10)
    backend.close()


def test_ai_debugs_while_human_watches_and_pauses(url):
    gui = DaemonClient(url, "gui", "human")
    ai = DaemonClient(url, "claude", "ai")
    seen = []
    gui.subscribe(["activity"], lambda topic, data: seen.append(data["method"]))
    ai.call("target.connect", target="stm32f401xe", frequency_hz=4_000_000)
    ai.call("target.program_ram", path=str(ELF))
    ai.call("debug.breakpoint.set", location="reached_milestone")
    stop = ai.call("debug.run_until_halt", timeout_s=2)
    assert stop["reason"] == "breakpoint" and stop["where"].startswith("reached_milestone")

    gui.call("session.control.pause")
    result = []
    t = threading.Thread(target=lambda: result.append(ai.call("debug.variable.read", name="counter")))
    t.start()
    time.sleep(0.3)
    assert result == []                                          # 一時停止中は AI の操作が保留される
    assert gui.call("debug.variable.read", name="counter")["value"] == 256   # 人間は操作できる（交代）
    gui.call("session.control.resume")
    t.join(5)
    assert result[0]["value"] == 256

    ai.call("debug.breakpoint.clear")
    ai.call("target.reset")
    time.sleep(0.2)
    assert {"target.connect", "debug.run_until_halt", "debug.variable.read"} <= set(seen)
    gui.close(); ai.close()
```

- [ ] **Step 2: 実機で通ることを確かめる**

Run: `FPROBE_ETH_HOST=192.168.77.2 .venv/Scripts/python -m pytest gateware/tests/hil/test_daemon_hw.py --hil -q`（USB-OTG がつながっていれば環境変数なしでも）
Expected: `1 passed`

- [ ] **Step 3: README とレポートに追記する**

README の「CLI と MCP サーバ」の節の先頭に足す:

```markdown
- `fprobe-daemon [--serial 0001 | --port COM3 | --eth 192.168.77.2] [--lan]`: fprobe を 1 つ握り、API（JSON-RPC 2.0 over
  WebSocket、`ws://127.0.0.1:7820/api`、文書は [docs/api.md](docs/api.md)）で全機能を出す。AI（MCP）と人間（画面・スクリプト）が
  同時につながり、すべての操作は記録（`~/.fprobe/activity/*.jsonl`、通知 `activity`）として流れる。人間は
  `session.control.pause` で AI の操作を保留させ、交代できる。`fprobe-mcp` はデーモンの利用者で、デーモンが無ければ起動する。
```

`docs/reports/2026-10-16-day.md` の末尾に、第 2 段の結果（単体・HIL の件数）を 2〜3 行で足す。

- [ ] **Step 4: Commit**

```bash
git add gateware/tests/hil/test_daemon_hw.py README.md docs/reports/2026-10-16-day.md
git commit -m "test(api): デーモンの実機試験（AI がデバッグし人間が見て一時停止・交代）、README"
```

---

## Self-Review メモ

- 仕様 §1 の完了条件: 機能の対応（Task 3・4・5）、同時利用と記録（Task 3・5）、一時停止（Task 2・3・5）、生成と一致（Task 1）、localhost と LAN のトークン（Task 3）。
- 仕様 §4 の通知のうち `target.state`・`uart.data`・`swo.data` の押し出しは、画面（第 3 段）で使い道が決まってから足す
  （この計画では `activity` と `control` だけを流す。`session.subscribe` の enum には 5 つとも載せておく）。
- Review Focus の 5 件は、それぞれ Task 1〜3 の試験に入れた。
