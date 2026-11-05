"""API の仕様から生成する: TypeScript の型（画面用）、文書（docs/api.md）。"""

import json
from pathlib import Path

from .spec import SPEC_PATH, Spec, load_hints, load_spec
from .typeschema import refresh_defs

__all__ = ["render_hints_markdown", "render_markdown", "render_typescript", "write_all"]

ROOT = SPEC_PATH.parents[2]
TS_PATH = SPEC_PATH.parent / "generated" / "fprobe_api.ts"
MD_PATH = ROOT / "docs" / "api.md"
HINTS_MD_PATH = ROOT / "docs" / "guide" / "hints.md"
HEADER = "自動生成ファイル。直接編集しないこと（python -m fprobe.api gen で再生成）。生成元: host/api/fprobe_api.json"


def _ident(name: str) -> str:
    return name if name.isidentifier() else json.dumps(name)


def _union(parts) -> str:
    """和の型（同じ項は 1 つに。integer と number はどちらも number）。"""
    return " | ".join(dict.fromkeys(parts))


def _ts_type(schema: dict) -> str:
    """JSON Schema → TypeScript の型（1 行）。応答の形の約束（spec.result_schema_problems）の範囲を全部書ける。"""
    if "$ref" in schema:
        return schema["$ref"].removeprefix("#/$defs/")
    if "const" in schema:
        return json.dumps(schema["const"], ensure_ascii=False)
    if "enum" in schema:
        return " | ".join(json.dumps(v, ensure_ascii=False) for v in schema["enum"])
    if "anyOf" in schema:
        return _union(_ts_type(s) for s in schema["anyOf"])
    t = schema.get("type")
    if isinstance(t, list):
        return _union(_ts_type({**schema, "type": x}) for x in t)
    if t == "array":
        if "prefixItems" in schema:
            return "[" + ", ".join(_ts_type(s) for s in schema["prefixItems"]) + "]"
        items = schema.get("items")
        return f"Array<{_ts_type(items) if isinstance(items, dict) else 'unknown'}>"
    if t == "object":
        props = schema.get("properties")
        if props:
            required = set(schema.get("required", []))
            return "{ " + "; ".join(f"{_ident(k)}{'' if k in required else '?'}: {_ts_type(v)}"
                                    for k, v in props.items()) + " }"
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            return f"{{ [key: string]: {_ts_type(extra)} }}"
        return "Record<string, never>" if props is not None and extra is False else "Record<string, unknown>"
    return {"integer": "number", "number": "number", "string": "string", "boolean": "boolean",
            "null": "null"}.get(t, "unknown")


def _ts_params(schema: dict) -> str:
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    return "{ " + "; ".join(f"{k}{'' if k in required else '?'}: {_ts_type(v)}" for k, v in props.items()) + " }"


def render_typescript(spec: Spec) -> str:
    lines = [f"// {HEADER}", f"export const API_VERSION = {spec.api_version};", "",
             "// 応答の共通の形（仕様の $defs）"]
    for name, schema in spec.defs.items():
        doc = schema.get("description")
        if doc:
            lines.append(f"/** {doc} */")
        lines.append(f"export type {name} = {_ts_type(schema)};")
    lines += ["", "export interface Methods {"]
    for m in spec.methods.values():
        binary = "; binary: true" if m.binary else ""
        lines.append(f"  {json.dumps(m.name)}: {{ kind: {json.dumps(m.kind)}{binary}; params: {_ts_params(m.params)}; "
                     f"result: {_ts_type(m.result)} }};")
    lines += ["}", "",
              "/** メソッド M の引数・応答の型 */",
              'export type Params<M extends keyof Methods> = Methods[M]["params"];',
              'export type Result<M extends keyof Methods> = Methods[M]["result"];', "",
              "export type Topic = " + " | ".join(json.dumps(t) for t in spec.notifications) + ";", ""]
    return "\n".join(lines)


def render_markdown(spec: Spec) -> str:
    out = [f"<!-- {HEADER} -->", "# fprobe API", "",
           f"JSON-RPC 2.0 over WebSocket（`ws://127.0.0.1:7820/api`）。API の版 {spec.api_version}。"
           "仕様: docs/superpowers/specs/2026-10-17-host-daemon-design.md", "",
           "Python から: `c = fprobe.api.client.DaemonClient(url, 名前, 役割, token=…)` → `c.call(\"メソッド\", 引数=値, …)`"
           "（引数はキーワードで。戻り値は dict）。", ""]
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
        out += ["応答:", "", "```ts", _ts_type(m.result), "```", ""]
    if spec.defs:
        out += ["## 応答の共通の形", ""]
        for name, schema in spec.defs.items():
            out += [f"### {name}", "", *([schema["description"], ""] if schema.get("description") else []),
                    "```ts", _ts_type(schema), "```", ""]
    out += ["## 通知", ""] + [f"- `{k}`: {v.get('summary', '')}" for k, v in spec.notifications.items()] + [""]
    return "\n".join(out)


def spec_text(data: dict) -> str:
    """仕様の JSON の書き方（2 字の字下げ・日本語はそのまま・末尾に改行。$defs は api_version の次）。"""
    ordered = {"api_version": data["api_version"], "$defs": data.get("$defs", {}),
               **{k: v for k, v in data.items() if k not in ("api_version", "$defs")}}
    return json.dumps(ordered, indent=2, ensure_ascii=False) + "\n"


def render_hints_markdown() -> str:
    """使い方の落とし穴の記録（host/api/hints.json）→ 人が読む文書（docs/guide/hints.md）。出どころは hints.json の 1 つだけ。"""
    doc = load_hints()
    lines = ["# 使い方の落とし穴（ヒント）", "", f"<!-- {HEADER.replace('host/api/fprobe_api.json', 'host/api/hints.json')} -->", "",
             doc["about"], "",
             "API では `session.hints`（`method` でそのメソッドに付いたヒント、`id` で 1 件）。失敗の応答の `data.hint_ids` にも id が出る。"
             "画面は Activity パネルでエラーの行を選ぶと出す。", ""]
    for h in doc["hints"]:
        lines += [f"## {h['title']}", "", f"`{h['id']}` — 付いたメソッド: " + ", ".join(f"`{m}`" for m in h["methods"]), "",
                  f"**症状**: {h['symptom']}", ""]
        if h["facts_to_check"]:
            lines += ["**確かめる事実**:", ""] + [f"- `{c['api']}` — {c['reads']}" for c in h["facts_to_check"]] + [""]
        if h.get("when"):
            lines += ["**付く条件**（失敗の種類）: " + ", ".join(f"{k} = {' / '.join(v)}" for k, v in h["when"].items()), ""]
        lines += ["**手順**:", ""] + [f"{i}. {step}" for i, step in enumerate(h["recipe"], 1)] + [""]
        if h["docs"]:
            lines += ["関連: " + ", ".join(f"`{d}`" for d in h["docs"]), ""]
    return "\n".join(lines) + "\n"


def refreshed_spec_text() -> str:
    """仕様の $defs のうち実装の型から作るもの（"x-from"）を作り直した、仕様の JSON の全文。"""
    data = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    data["$defs"] = refresh_defs(data.get("$defs", {}))
    return spec_text(data)


def write_all(check: bool = False) -> list[Path]:
    """生成物を書く（仕様の $defs の x-from・TypeScript の型・文書）。check=True なら書かずに一致を確かめ、違えば
    AssertionError。"""
    spec_now = refreshed_spec_text()
    if check:
        assert SPEC_PATH.read_text(encoding="utf-8") == spec_now, \
            f"{SPEC_PATH} の $defs（x-from: 実装の型から作る形）が古い（python -m fprobe.api gen）"
    else:
        SPEC_PATH.write_text(spec_now, encoding="utf-8", newline="\n")
    spec = load_spec()
    outputs = {TS_PATH: render_typescript(spec), MD_PATH: render_markdown(spec), HINTS_MD_PATH: render_hints_markdown()}
    for path, text in outputs.items():
        if check:
            assert path.read_text(encoding="utf-8") == text, f"{path} が古い（python -m fprobe.api gen）"
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
    return [SPEC_PATH, *outputs]
