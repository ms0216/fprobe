"""テストポイントの自動挿入: C のソース → 挿入した写し + ID の対応表。

元のソースは変えない。写しは元の文字の位置に（改行を含まない）文字列を差し込んだもので、行番号は元と同じ。
先頭に `#include "<相対>/fprobe_tp.h"` と `#line 1 "<元のパス>"` を足すので、写しで作った ELF の DWARF は元の行を指す。
仕様: docs/superpowers/specs/2026-10-17-f7-testpoint-design.md の 2 節。

    result = instrument_tree(Path("src"), Path("build/tp"), options=Options(variables=(WatchVar("state"),)),
                             previous=ProbeTable.load(...))   # 前回の ID を保つ（任意）

挿入の設定・結果は instrument_model.py、1 つの関数の挿入は instrument_function.py。
"""

import hashlib
import os
from collections.abc import Iterable
from pathlib import Path

from .clex import CLexError, Token, tokenize
from .cparse import CParseError, Function, Skipped, parse_file
from .instrument_function import _Edits, _FunctionInstrumenter
from .instrument_model import FileResult, InstrumentResult, Options, WatchVar, header_path
from .probes import MAX_ID, FunctionInfo, Probe, ProbeTable

__all__ = ["FileResult", "InstrumentResult", "Options", "WatchVar", "header_path", "instrument_source",
           "instrument_tree"]

FunctionKey = tuple[str, str, int]


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
