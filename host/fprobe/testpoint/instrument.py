"""テストポイントの自動挿入: C のソース → 挿入した写し + ID の対応表。

元のソースは変えない。写しは元の文字の位置に（改行を含まない）文字列を差し込んだもので、行番号は元と同じ。
先頭に `#include "<相対>/fprobe_tp.h"` と `#line 1 "<元のパス>"` を足すので、写しで作った ELF の DWARF は元の行を指す。
仕様: docs/superpowers/specs/2026-10-17-f7-testpoint-design.md の 2 節。

    result = instrument_tree(Path("src"), Path("build/tp"), options=Options(variables=(WatchVar("state"),)),
                             previous=ProbeTable.load(...))   # 前回の ID を保つ（任意）
"""

import fnmatch
import hashlib
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .clex import CLexError, Token, tokenize
from .cparse import (CParseError, Compound, Function, If, Label, Loop, Node, Opaque, Return, Simple, Skipped,
                     Switch, parse_file)
from .probes import MAX_ID, FunctionInfo, Probe, ProbeTable

__all__ = ["FileResult", "InstrumentResult", "Options", "WatchVar", "header_path", "instrument_source",
           "instrument_tree"]

RETURN_VAR = "fprobe_tp_r"
_ASSIGN_OPS = frozenset({"=", "+=", "-=", "*=", "/=", "%=", "<<=", ">>=", "&=", "^=", "|="})
_SIDE_EFFECTS = _ASSIGN_OPS | {"++", "--", "("}
_MACRO_LIKE = re.compile(r"^[A-Z_][A-Z0-9_]+$")

FunctionKey = tuple[str, str, int]


def header_path() -> Path:
    """ターゲット側のヘッダ（写しの出力先にも置く）。"""
    return Path(__file__).with_name("fprobe_tp.h")


@dataclass(frozen=True)
class WatchVar:
    name: str
    value: bool = True              # False: 値を出さない（構造体など整数にできない変数）

    @classmethod
    def parse(cls, spec: str) -> "WatchVar":
        """"name" または "name:novalue"。"""
        name, _, flag = spec.partition(":")
        if not name.isidentifier() or flag not in ("", "novalue"):
            raise ValueError(f"変数の指定が読めない: {spec!r}（name または name:novalue）")
        return cls(name, flag != "novalue")


@dataclass(frozen=True)
class Options:
    entry_exit: bool = True
    branches: bool = True
    variables: tuple[WatchVar, ...] = ()
    include: tuple[str, ...] = ()        # 関数名の glob（空 = すべて）
    exclude: tuple[str, ...] = ()
    only_changed: bool = False           # 前回（baseline）から変わった・新しい関数にだけ入れる
    macro: str = "FPROBE_TP"
    value_macro: str = "FPROBE_TP_VALUE"

    def signature(self) -> str:
        """指紋に混ぜる（挿入の形を変える設定が変われば入れ直す）。"""
        return repr((self.entry_exit, self.branches, sorted((v.name, v.value) for v in self.variables), self.macro,
                     self.value_macro))

    def selects(self, name: str) -> bool:
        if self.include and not any(fnmatch.fnmatchcase(name, p) for p in self.include):
            return False
        return not any(fnmatch.fnmatchcase(name, p) for p in self.exclude)


@dataclass
class FileResult:
    path: str
    instrumented: list[str] = field(default_factory=list)      # 挿入した関数
    reused: list[str] = field(default_factory=list)            # 前回の ID をそのまま使った関数
    changed: list[str] = field(default_factory=list)           # 前回（baseline）から変わった・新しい関数
    skipped: list[Skipped] = field(default_factory=list)       # 挿入しなかった関数と理由
    warnings: list[str] = field(default_factory=list)
    error: str | None = None                                   # ファイル全体が読めない（写しは元のまま）
    probes: int = 0


@dataclass
class InstrumentResult:
    table: ProbeTable
    files: list[FileResult]
    removed: list[str]                  # 前回あって今回無い関数（"ファイル:名前"）
    written: list[Path]                 # 中身が変わって書いたファイル

    @property
    def probe_count(self) -> int:
        return len(self.table)


# --- 編集 -------------------------------------------------------------------------------------

class _Edits:
    """元の文字の位置への差し込み・置き換え。同じ位置は作った順に並ぶ（外の開きを先に、内の閉じを先に作る）。"""

    def __init__(self):
        self._items: list[tuple[int, int, int, str]] = []

    def insert(self, pos: int, text: str) -> None:
        self._items.append((pos, pos, len(self._items), text))

    def replace(self, start: int, end: int, text: str) -> None:
        self._items.append((start, end, len(self._items), text))

    def __bool__(self) -> bool:
        return bool(self._items)

    def apply(self, src: str) -> str:
        out, pos = [], 0
        for start, end, _, text in sorted(self._items, key=lambda e: (e[0], e[2])):
            if start < pos:
                raise AssertionError("挿入が重なった")
            if "\n" in text:
                raise AssertionError("挿入に改行が入った（行番号がずれる）")
            out.append(src[pos:start])
            out.append(text)
            pos = end
        out.append(src[pos:])
        return "".join(out)


# --- 関数ごとの挿入 ----------------------------------------------------------------------------

class _FunctionInstrumenter:
    """1 つの関数の文の木を歩き、印の差し込みと印の記述（dict）を作る。ID は take_id の順（ソースの順）。"""

    def __init__(self, toks: list[Token], match: dict[int, int], fn: Function, file: str, options: Options,
                 edits: _Edits, take_id: Callable[[], int], warnings: list[str]):
        self.t, self.match, self.fn, self.file = toks, match, fn, file
        self.o = options
        self.edits = edits
        self.take_id = take_id
        self.warnings = warnings
        self.probes: list[dict] = []
        self.watch = {v.name: v for v in options.variables}
        self.switches: list[list[dict]] = []    # 歩いている switch ごとのラベルの印（内側が末尾）

    def tp(self, probe_id: int) -> str:
        return f"{self.o.macro}({probe_id});"

    def probe(self, kind: str, tok: int, **fields) -> int:
        probe_id = self.take_id()
        self.probes.append({"id": probe_id, "file": self.file, "line": self.t[tok].line, "function": self.fn.name,
                            "kind": kind, **fields})
        return probe_id

    def run(self) -> list[dict]:
        body = self.fn.body
        if self.o.entry_exit:
            self.edits.insert(self.t[body.open].end, " " + self.tp(self.probe("entry", self.fn.name_token)))
        self.walk(body)
        last = body.items[-1] if body.items else None
        if self.o.entry_exit and not isinstance(last, Return):
            # 本体の終わりに落ちる出口（届かない関数もある: ソースに無い印として C0 から除く）
            end = self.probe("exit", body.close, label="end", implicit=True)
            self.edits.insert(self.t[body.close].start, self.tp(end) + " ")
        return self.probes

    # --- 歩く ---

    def walk(self, node: Node) -> None:
        if isinstance(node, Compound):
            for item in node.items:
                self.walk(item)
        elif isinstance(node, If):
            self.walk_if(node)
        elif isinstance(node, Switch):
            self.walk_switch(node)
        elif isinstance(node, Loop):
            self.walk_loop(node)
        elif isinstance(node, Return):
            self.walk_return(node)
        elif isinstance(node, Label):
            self.walk_label(node)
        elif isinstance(node, Opaque):
            self.walk(node.body)
        elif isinstance(node, Simple):
            self.walk_simple(node)
        else:
            raise TypeError(node)

    def block(self, child: Node, probe_id: int | None) -> None:
        """if・else・ループの子の文。印を先頭に置き、括弧の無い子は { } で包む。"""
        mark = "" if probe_id is None else " " + self.tp(probe_id)
        if isinstance(child, Compound):
            if mark:
                self.edits.insert(self.t[child.open].end, mark)
            self.walk(child)
            return
        self.edits.insert(self.t[child.first].start, "{" + mark + " ")
        self.walk(child)
        self.edits.insert(self.t[child.last].end, " }")

    def walk_if(self, node: If) -> None:
        then_id = else_id = None
        if self.o.branches:
            then_id = self.probe("then", node.first, label="if")
            self.probes[-1]["decision"] = then_id
            else_id = self.probe("else", node.else_kw if node.else_kw is not None else node.first, decision=then_id,
                                 label="else", implicit=node.els is None)
        self.block(node.then, then_id)
        if node.els is not None:
            self.block(node.els, else_id)
        elif else_id is not None:
            self.edits.insert(self.t[node.then.last].end, f" else {{ {self.tp(else_id)} }}")

    def walk_switch(self, node: Switch) -> None:
        body = node.body
        wrap = not isinstance(body, Compound)
        if wrap:
            self.edits.insert(self.t[body.first].start, "{ ")
        self.switches.append([])
        self.walk(body)
        labels = self.switches.pop()
        if self.o.branches:
            if not node.has_default:
                implicit = self.probe("default", node.first, label="default", implicit=True)
                labels.append(self.probes[-1])
                text = f"break; default: {self.tp(implicit)} break;"
                if wrap:
                    self.edits.insert(self.t[body.last].end, " " + text)
                else:
                    self.edits.insert(self.t[body.close].start, text + " ")
            for p in labels:
                p["decision"] = labels[0]["id"]
        if wrap:
            self.edits.insert(self.t[body.last].end, " }")

    def walk_loop(self, node: Loop) -> None:
        body_id = exit_id = None
        if self.o.branches:
            body_id = self.probe("loop_body", node.first, label=node.kind)
            self.probes[-1]["decision"] = body_id
            exit_id = self.probe("loop_exit", node.first, label=node.kind, decision=body_id)
        self.block(node.body, body_id)
        if exit_id is not None:
            self.edits.insert(self.t[node.last].end, " " + self.tp(exit_id))

    def walk_return(self, node: Return) -> None:
        if not self.o.entry_exit:
            return
        exit_id = self.probe("exit", node.first, label="return")
        kw, semi = self.t[node.first], self.t[node.last]
        expr = self.t[node.first + 1]
        # return の後の空白も置き換える（同じ行のときだけ。改行を消すと行番号がずれる）
        kw_end = expr.start if expr.kind != "pp" and expr.line == kw.line else kw.end
        if not node.has_expr:
            self.edits.insert(kw.start, "{ " + self.tp(exit_id) + " ")
            self.edits.insert(semi.end, " }")
        elif self.fn.is_void:                   # void の式（GNU の拡張）: 評価してから出口
            self.edits.replace(kw.start, kw_end, "{ (void)(")
            self.edits.replace(semi.start, semi.end, f"); {self.tp(exit_id)} return; }}")
        else:                                   # 式を先に評価する（式の中の呼び出しが子として入れ子になる）
            rtype = self.fn.return_type
            if rtype is None or any(_MACRO_LIKE.match(w) for w in rtype.split()):
                rtype = "__auto_type"
            self.edits.replace(kw.start, kw_end, f"{{ {rtype} {RETURN_VAR} = (")
            self.edits.replace(semi.start, semi.end, f"); {self.tp(exit_id)} return {RETURN_VAR}; }}")

    def walk_label(self, node: Label) -> None:
        if node.kind in ("case", "default") and self.o.branches:
            label = "default" if node.kind == "default" else \
                "case " + " ".join(t.text for t in self.t[node.first + 1:node.colon] if t.kind != "pp")
            probe_id = self.probe(node.kind, node.first, label=label)
            self.switches[-1].append(self.probes[-1])
            self.edits.insert(self.t[node.colon].end, " " + self.tp(probe_id))
        if node.stmt is not None:
            self.walk(node.stmt)

    def walk_simple(self, node: Simple) -> None:
        if not self.watch or self.t[node.first].text in ("static", "extern", "typedef"):
            return                               # 静的な局所変数の初期化は 1 回だけで、この文の時点ではない
        for name, lvalue in self._writes(node):
            if node.no_semicolon:
                self.warnings.append(f"{self.file}:{self.t[node.first].line}: ; の無い文の {name} の書き込みには入れない")
                continue
            text = " ".join(t.text for t in lvalue)
            probe_id = self.probe("var", node.first, variable=name, label=text)
            if self.watch[name].value and not any(t.text in _SIDE_EFFECTS for t in lvalue):
                mark = f" {self.o.value_macro}({probe_id}, ({text}));"
            else:
                mark = " " + self.tp(probe_id)
            self.edits.insert(self.t[node.last].end, mark)

    # --- 変数の書き込みを探す ---

    def _writes(self, node: Simple) -> list[tuple[str, list[Token]]]:
        found = []
        for k in range(node.first, node.last + 1):
            tok = self.t[k]
            if tok.kind != "punct":
                continue
            if tok.text in _ASSIGN_OPS:
                span = self._lvalue_before(k - 1, node.first)
            elif tok.text in ("++", "--"):
                span = self._lvalue_before(k - 1, node.first) or self._lvalue_after(k + 1, node.last)
            else:
                continue
            if span and span[0].text in self.watch:
                found.append((span[0].text, span))
        return found

    def _lvalue_before(self, j: int, lo: int) -> list[Token] | None:
        """j で終わる後置の式（a・a[i]・a.b・a->b）の字句。最初が監視の候補の識別子。"""
        end = j
        while j >= lo:
            tok = self.t[j]
            if tok.kind == "punct" and tok.text == "]":
                j = self.match[j] - 1
                continue
            if tok.kind != "id":
                return None
            if j - 1 >= lo and self.t[j - 1].kind == "punct" and self.t[j - 1].text in (".", "->"):
                j -= 2
                continue
            if j - 1 >= lo and self.t[j - 1].text == "*" and not (
                    j - 2 >= lo and (self.t[j - 2].kind == "id" or self.t[j - 2].text == "*")):
                return None                      # *p = …（ポインタ経由の書き込みは対象外）。int *p = … は宣言
            return self.t[j:end + 1]
        return None

    def _lvalue_after(self, j: int, hi: int) -> list[Token] | None:
        if j > hi or self.t[j].kind != "id":
            return None
        start = j
        j += 1
        while j <= hi:
            tok = self.t[j]
            if tok.kind == "punct" and tok.text == "[":
                j = self.match[j] + 1
            elif tok.kind == "punct" and tok.text in (".", "->") and j + 1 <= hi and self.t[j + 1].kind == "id":
                j += 2
            else:
                break
        return self.t[start:j]


# --- ファイル ----------------------------------------------------------------------------------

class _Ids:
    def __init__(self, next_id: int):
        self.next_id = next_id

    def take(self) -> int:
        if self.next_id > MAX_ID:
            raise OverflowError("テストポイントの ID が 65535 を超えた（renumber して作り直す）")
        self.next_id += 1
        return self.next_id - 1


def _fingerprint(toks: list[Token], fn: Function, options: Options) -> str:
    h = hashlib.sha1(options.signature().encode())
    for t in toks[fn.first:fn.body.close + 1]:
        h.update(t.text.encode())
        h.update(b"\0")
    return h.hexdigest()


def _c_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def instrument_source(src: str, file: str, *, options: Options = Options(), next_id: int = 1,
                      previous: dict[FunctionKey, FunctionInfo] | None = None,
                      baseline: dict[FunctionKey, FunctionInfo] | None = None,
                      include: str = "fprobe_tp.h", line_path: str | None = None
                      ) -> tuple[str, list[Probe], list[FunctionInfo], FileResult, int]:
    """1 つのソースに挿入する。(写し, 印, 関数の情報, 結果, 次の ID)。

    previous: 指紋が同じ関数は前回の ID を使う。baseline: only_changed の比較の元（既定 previous）。
    include: 先頭の #include のパス。line_path: #line のファイル名（既定 file）。
    """
    previous = previous or {}
    baseline = previous if baseline is None else baseline
    result = FileResult(file)
    try:
        toks = tokenize(src)
        parsed = parse_file(toks)
    except (CLexError, CParseError) as e:
        result.error = str(e)
        return src, [], [], result, next_id
    result.skipped.extend(parsed.skipped)
    edits = _Edits()
    ids = _Ids(next_id)
    probes: list[Probe] = []
    infos: list[FunctionInfo] = []
    seen: dict[str, int] = {}
    for fn in parsed.functions:
        index = seen.get(fn.name, 0)
        seen[fn.name] = index + 1
        key = (file, fn.name, index)
        fp = _fingerprint(toks, fn, options)
        info = FunctionInfo(file, fn.name, index, fn.line, toks[fn.body.close].line, fp, False)
        infos.append(info)
        base = baseline.get(key)
        changed = base is None or base.fingerprint != fp
        if changed:
            result.changed.append(fn.name)
        if not options.selects(fn.name) or (options.only_changed and not changed):
            continue
        if "naked" in fn.attributes:
            result.skipped.append(Skipped(fn.name, fn.line, "naked 属性の関数（C を足すと壊れる）"))
            continue
        old = previous.get(key)
        same = old is not None and old.instrumented and old.fingerprint == fp
        reuse = list(old.ids) if same else []

        def take(reuse=reuse):
            return reuse.pop(0) if reuse else ids.take()

        raw = _FunctionInstrumenter(toks, parsed.match, fn, file, options, edits, take, result.warnings).run()
        if reuse:                              # 指紋が同じなら数も同じ（決定的）
            raise AssertionError(f"{file}:{fn.name}: 前回の ID の数が合わない")
        probes.extend(Probe(**p) for p in raw)
        info.instrumented = True
        info.ids = [p["id"] for p in raw]
        result.instrumented.append(fn.name)
        if same:
            result.reused.append(fn.name)
    result.probes = len(probes)
    if not edits:
        return src, probes, infos, result, ids.next_id
    head = f'#include "{include}"\n#line 1 {_c_string(line_path or file)}\n'
    return head + edits.apply(src), probes, infos, result, ids.next_id


def _write_if_changed(path: Path, text: str) -> bool:
    data = text.encode("utf-8")
    if path.exists() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        os.chmod(path, 0o644)
    path.write_bytes(data)
    return True


def _inside(path: Path, directory: Path) -> bool:
    return directory == path or directory in path.parents


def instrument_tree(root: Path, out_dir: Path, *, sources: Iterable[Path] | None = None,
                    copy: tuple[str, ...] = ("*.h", "*.inc"), options: Options = Options(),
                    previous: ProbeTable | None = None, baseline: ProbeTable | None = None,
                    map_path: Path | None = None, renumber: bool = False,
                    line_root: Path | None = None) -> InstrumentResult:
    """root の下の C のソース（既定 *.c すべて）を out_dir に同じ相対パスで写し、挿入する。copy に当たるファイル
    （ヘッダ）はそのまま写す（相対の #include が写しでも通る）。out_dir に fprobe_tp.h と対応表（map_path、既定
    out_dir/testpoints.json）を置く。previous を渡すと指紋が同じ関数の ID を保つ（renumber=True で 1 から振り直す）。
    #line のファイル名は元の絶対パス。line_root を渡すとそこからの相対パス（コンパイラをそこで動かす前提。ELF に
    作った場所の絶対パスが入らない）。
    写しは中身が変わったときだけ書く（ビルドの差分が最小になる）。"""
    root, out_dir = Path(root).resolve(), Path(out_dir).resolve()
    if sources is None:
        sources = [p for p in sorted(root.rglob("*.c")) if not _inside(p.resolve(), out_dir)]
    sources = [Path(s).resolve() for s in sources]
    use_previous = previous is not None and not renumber
    prev_map = {f.key: f for f in previous.functions} if use_previous else {}
    base_map = None if baseline is None else {f.key: f for f in baseline.functions}
    next_id = previous.next_id if use_previous else 1
    table = ProbeTable()
    files: list[FileResult] = []
    written: list[Path] = []
    header_out = out_dir / "fprobe_tp.h"
    if _write_if_changed(header_out, header_path().read_text(encoding="utf-8")):
        written.append(header_out)
    for src_path in sources:
        rel = src_path.relative_to(root).as_posix()
        dest = out_dir / rel
        include = os.path.relpath(header_out, dest.parent).replace(os.sep, "/")
        out, probes, infos, result, next_id = instrument_source(
            src_path.read_text(encoding="utf-8"), rel, options=options, next_id=next_id, previous=prev_map,
            baseline=base_map, include=include,
            line_path=(Path(os.path.relpath(src_path, line_root)) if line_root else src_path).as_posix())
        table.probes.update({p.id: p for p in probes})
        table.functions.extend(infos)
        table.skipped.extend({"file": rel, "function": s.name, "line": s.line, "reason": s.reason}
                             for s in result.skipped)
        if result.error:
            table.skipped.append({"file": rel, "function": "", "line": 0, "reason": result.error})
        files.append(result)
        if _write_if_changed(dest, out):
            written.append(dest)
    for pattern in copy:
        for other in sorted(root.rglob(pattern)):
            if _inside(other.resolve(), out_dir):
                continue
            dest = out_dir / other.relative_to(root)
            if _write_if_changed(dest, other.read_text(encoding="utf-8")):
                written.append(dest)
    table.next_id = next_id
    current = {f.key for f in table.functions}
    removed = [f"{f.file}:{f.name}" for f in (previous.functions if previous else []) if f.key not in current]
    table.save(out_dir / "testpoints.json" if map_path is None else Path(map_path))
    return InstrumentResult(table, files, removed, written)
