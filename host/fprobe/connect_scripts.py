"""接続の手順の上書き（U30）: 利用者の Python（pyOCD のユーザスクリプト = デリゲート）で接続・リセット・解除等の手順を
差し替える。J-Link Script Files・LinkServer のスクリプト・pyOCD の user script に当たる。

- スクリプトは pyOCD の作法のまま（`will_connect` / `did_init_target` / `will_reset` 等の関数を定義する。名前空間に
  session・target・probe・dp・aps・LOG 等がある）。target_connect の script で渡すと pyOCD の user_script になる。
- check_script: 実行せずに（ast で）読み、定義された手順・名前の誤り（似た名前の提案）・引数の名前の誤り・構文の誤りを返す。
  手順の一覧と引数は pyOCD の TargetDelegateInterface から取る（pyOCD が増やせば追従する）。
- 内蔵の手順（"builtin:<名前>"）: fprobe/data/connect_scripts/*.py。
"""

import ast
import difflib
import inspect
from dataclasses import dataclass, field
from pathlib import Path

from pyocd.core.target_delegate import TargetDelegateInterface

__all__ = ["BUILTIN_DIR", "HOOKS", "ScriptCheck", "builtin_scripts", "check_script", "resolve_script"]

BUILTIN_DIR = Path(__file__).resolve().parent / "data" / "connect_scripts"
BUILTIN_PREFIX = "builtin:"


def _hooks() -> dict[str, dict]:
    out = {}
    for name, fn in inspect.getmembers(TargetDelegateInterface, inspect.isfunction):
        if name.startswith("_"):
            continue
        params = [p for p in inspect.signature(fn).parameters if p != "self"]
        doc = (inspect.getdoc(fn) or "").replace("@brief", "").strip().splitlines()
        out[name] = {"params": params, "summary": doc[0].lstrip("! ").strip() if doc else ""}
    return out


HOOKS = _hooks()


@dataclass
class ScriptCheck:
    path: str
    hooks: list[dict] = field(default_factory=list)          # 定義された手順（name・line・params）
    commands: list[str] = field(default_factory=list)        # @command で足したコマンド
    problems: list[str] = field(default_factory=list)
    error: str | None = None                                 # 構文の誤り

    @property
    def ok(self) -> bool:
        return self.error is None and not self.problems

    def as_dict(self) -> dict:
        return {"path": self.path, "ok": self.ok, "hooks": self.hooks, "commands": self.commands,
                "problems": self.problems, "error": self.error}


def builtin_scripts() -> list[dict]:
    """内蔵の手順の一覧（名前と、先頭の docstring の 1 行目）。"""
    out = []
    for path in sorted(BUILTIN_DIR.glob("*.py")):
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
        out.append({"name": BUILTIN_PREFIX + path.stem, "summary": doc.splitlines()[0] if doc else "",
                    "hooks": [h["name"] for h in check_script(str(path)).hooks]})
    return out


def resolve_script(script: str) -> Path:
    """"builtin:<名前>" かファイルの道筋を、実在する絶対パスに。"""
    if script.startswith(BUILTIN_PREFIX):
        name = script[len(BUILTIN_PREFIX):]
        path = BUILTIN_DIR / f"{name}.py"
        if not name.isidentifier() or not path.is_file():
            names = ", ".join(s["name"] for s in builtin_scripts())
            raise ValueError(f"内蔵の手順 {script!r} は無い（{names}）")
        return path
    path = Path(script).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"スクリプト {script!r} が無い")
    return path


def check_script(script: str) -> ScriptCheck:
    """スクリプトを実行せずに調べる。script は "builtin:<名前>" かファイル。"""
    path = resolve_script(script)
    result = ScriptCheck(str(path))
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as e:
        result.error = f"{e.msg}（{e.lineno} 行）"
        return result
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if any(_is_command(d) for d in node.decorator_list):
            result.commands.append(node.name)
            continue
        params = [a.arg for a in node.args.args]
        if node.name in HOOKS:
            known = HOOKS[node.name]["params"]
            result.hooks.append({"name": node.name, "line": node.lineno, "params": params,
                                 "summary": HOOKS[node.name]["summary"]})
            unknown = [p for p in params if p not in known]
            if unknown:
                result.problems.append(f"{node.name}（{node.lineno} 行）の引数 {', '.join(unknown)} を pyOCD は渡さない"
                                       f"（渡すのは {', '.join(known)}。名前で選んで渡される）")
            continue
        close = difflib.get_close_matches(node.name, HOOKS, n=1, cutoff=0.8)
        if close or node.name.startswith(("will_", "did_")):
            hint = f"。{close[0]} のつもり？" if close else ""
            result.problems.append(f"{node.name}（{node.lineno} 行）は pyOCD の手順の名前ではない（呼ばれない）{hint}")
    return result


def _is_command(decorator: ast.expr) -> bool:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    return isinstance(target, ast.Name) and target.id == "command"
