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
    if t == "array":
        return f"Array<{_ts_type(schema.get('items', {}))}>"
    return {"integer": "number", "number": "number", "string": "string", "boolean": "boolean", "null": "null",
            "object": "Record<string, unknown>"}.get(t, "unknown")


def render_typescript(spec: Spec) -> str:
    lines = [f"// {HEADER}", f"export const API_VERSION = {spec.api_version};", "", "export interface Methods {"]
    for m in spec.methods.values():
        props = m.params.get("properties", {})
        required = set(m.params.get("required", []))
        fields = "; ".join(f"{k}{'' if k in required else '?'}: {_ts_type(v)}" for k, v in props.items())
        binary = "; binary: true" if m.binary else ""
        lines.append(f"  {json.dumps(m.name)}: {{ kind: {json.dumps(m.kind)}{binary}; params: {{ {fields} }} }};")
    lines += ["}", "", "export type Topic = " + " | ".join(json.dumps(t) for t in spec.notifications) + ";", ""]
    return "\n".join(lines)


def render_markdown(spec: Spec) -> str:
    out = [f"<!-- {HEADER} -->", "# fprobe API", "",
           f"JSON-RPC 2.0 over WebSocket（`ws://127.0.0.1:7820/api`）。API の版 {spec.api_version}。"
           "仕様: docs/superpowers/specs/2026-10-17-host-daemon-design.md", ""]
    for m in spec.methods.values():
        binary = "応答はバイナリのフレーム（WebSocket の利用者だけ。MCP の道具には無い）。" if m.binary else ""
        out += [f"## {m.name}", "", f"種類: `{m.kind}`。{binary}{m.summary}", ""]
        props = m.params.get("properties", {})
        required = set(m.params.get("required", []))
        if props:
            out += ["| 引数 | 型 | 必須 |", "|---|---|---|"]
            out += [f"| `{k}` | `{_ts_type(v)}`{'' if v.get('x-mcp', True) else '（MCP の道具には出さない）'} | "
                    f"{'○' if k in required else ''} |" for k, v in props.items()]
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
