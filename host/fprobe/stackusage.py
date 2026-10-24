"""スタックの使用量（U26）: 静的な最大（コンパイラの .su と逆アセンブルの呼び出しグラフ）と、塗りによる実測。

静的な最大: 関数の枠は .su（gcc / clang の -fstack-usage）があればそれ、無ければ逆アセンブル（fprobe.callgraph）の
SP の動きから。呼び出しの場所の深さ + 呼び先の最大 の最大を、呼び先から順に求める。次の場合は「上限不明」
（bounded = false、reasons に理由。max は分かる分の下限）:
  - 再帰（呼び出しグラフの輪。cycles に輪の関数）
  - 関数ポインタの呼び出し（indirect に呼び先の候補を渡すか、assume_address_taken で番地を取られた関数のどれか）
  - 可変長の確保（alloca・可変長配列。.su の "dynamic"。"dynamic,bounded" は上限ありとして扱う）
  - ELF に無い呼び先（ROM の関数など）・解析できない関数
根: リセット（ベクタ表の 1）、例外の処理（ベクタ表。同じ関数は 1 つにまとめる）、どこからも呼ばれない関数
（番地を取られた関数 = 関数ポインタの呼び先の候補は address_taken）。MSP の見積もり = スレッド（リセット）の最大
+ 例外の処理（例外の枠 = 32 バイト + 揃えの 4、FPU を使うなら 104 + 4）の 1 段 / 全部が入れ子になる最悪。

実測（IAR の Stack の塗り）: paint_range で塗る範囲（いまの SP より下）を決めて決まった値で埋め、走らせた後に
measure_stack で底から塗りが残っているバイト数を数える（使った最大の深さ = 上端 − 塗りの残りの上端）。

仕様: docs/superpowers/specs/2026-10-18-u18-u26-profile-export-stack-design.md
"""

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .callgraph import CallGraph, CallSite
from .rtos.stackcheck import untouched_bytes

__all__ = ["BASIC_FRAME", "EXTENDED_FRAME", "SuEntry", "analyze_stack", "load_su", "measure_stack", "paint_range",
           "parse_su"]

BASIC_FRAME = 32 + 4              # 例外の入口で積む 8 語 + 8 バイトに揃える詰め物
EXTENDED_FRAME = 104 + 4          # FPU の文脈を含む枠（S0〜S15・FPSCR・予約）+ 詰め物
_SU_LINE = re.compile(r"^(.*?):(\d+):(?:(\d+):)?(.*)$")


@dataclass(frozen=True)
class SuEntry:
    file: str
    line: int
    function: str                  # gcc の C++ は "int ns::f(int)" の形
    size: int
    qualifier: str                 # static / dynamic / dynamic,bounded
    source: str = ""               # 読んだ .su

    @property
    def bounded(self) -> bool:
        return self.qualifier != "dynamic"

    @property
    def name(self) -> str:
        """関数の名前（C++ の引数・戻りの型を除いた "ns::f"）。"""
        text = self.function
        if "(" in text:
            text = text[:text.index("(")]
        return text.split()[-1] if text.split() else text


def parse_su(text: str, source: str = "") -> list[SuEntry]:
    """.su の行 "ファイル:行[:列]:関数<TAB>バイト<TAB>static|dynamic|dynamic,bounded"（gcc は列あり、clang は無し）。"""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split("\t")
        m = _SU_LINE.match(parts[0])
        if len(parts) < 3 or m is None or not parts[1].strip().isdigit():
            raise ValueError(f"{source}:{n}: .su の行として読めない: {line!r}")
        out.append(SuEntry(m.group(1), int(m.group(2)), m.group(4), int(parts[1]), parts[2].strip(), source))
    return out


def load_su(paths: Iterable[str | Path]) -> list[SuEntry]:
    """.su のファイル、またはその置かれたディレクトリ（下も探す）から読む。"""
    out = []
    for path in paths:
        path = Path(path)
        files = sorted(path.rglob("*.su")) if path.is_dir() else [path]
        for f in files:
            out += parse_su(f.read_text(encoding="utf-8", errors="replace"), str(f))
    return out


# --- 静的な最大 ---------------------------------------------------------------------------------

@dataclass
class _Result:
    frame: int
    frame_source: str
    max: int
    bounded: bool
    reasons: list[str]
    path: list[str]
    unknown_from: set[str]


def _match_su(graph: CallGraph, su: Sequence[SuEntry], file_of: Callable[[str], str | None] | None,
              notes: list[str]) -> dict[str, SuEntry]:
    by_name: dict[str, list[SuEntry]] = {}
    for e in su:
        by_name.setdefault(e.name, []).append(e)
    out = {}
    for name in graph.functions:
        found = by_name.get(name)
        if not found:
            continue
        if len(found) > 1 and file_of is not None and (file := file_of(name)):
            same = [e for e in found if Path(e.file).name == Path(file).name]
            found = same or found
        if len(found) > 1:
            notes.append(f"{name}: .su に同じ名前が {len(found)} 個（static 関数）。大きい方を使った")
        out[name] = max(found, key=lambda e: e.size)
    return out


def _sccs(graph: CallGraph) -> list[list[str]]:
    """Tarjan の強連結成分（呼び先が先に出る順）。"""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    out: list[list[str]] = []
    counter = 0
    for root in sorted(graph.functions, key=lambda n: graph.functions[n].address):
        if root in index:
            continue
        work = [(root, iter(graph.callees(root)))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on.add(root)
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in graph.functions:
                    continue
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on.add(child)
                    work.append((child, iter(graph.callees(child))))
                    advanced = True
                    break
                if child in on:
                    low[node] = min(low[node], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on.discard(member)
                    component.append(member)
                    if member == node:
                        break
                out.append(sorted(component, key=lambda n: graph.functions[n].address))
    return out


def analyze_stack(graph: CallGraph, su: Sequence[SuEntry] = (), *, indirect: dict[str, list[str]] | None = None,
                  assume_address_taken: bool = False, roots: Iterable[str] = (), entry: str | None = None,
                  exception_frame: int | None = None, file_of: Callable[[str], str | None] | None = None) -> dict:
    """静的な最大のスタック。indirect: 関数 → その中の関数ポインタの呼び先の候補。roots: 根に足す関数。
    entry: リセットの関数（省略でベクタ表の 1。ベクタ表は CallGraph.build の vector_table で使う側が渡す。無ければ
    例外の根は無く、msp は None）。exception_frame: 例外の枠（省略で FPU の命令があれば 108、無ければ 36）。
    file_of: 関数 → ソースのファイル（.su の同じ名前の static 関数を見分ける）。"""
    notes: list[str] = []
    indirect = dict(indirect or {})
    for caller, callees in indirect.items():
        for name in [caller, *callees]:
            if name not in graph.functions:
                raise KeyError(f"indirect の関数 {name!r} が ELF に無い")
    matched = _match_su(graph, su, file_of, notes)
    mismatched = []
    components = _sccs(graph)
    component_of = {n: i for i, c in enumerate(components) for n in c}
    cycles = [c for c in components if len(c) > 1 or c[0] in graph.callees(c[0])]
    cyclic = {n for c in cycles for n in c}
    results: dict[str, _Result] = {}
    for component in components:
        for name in component:
            flow = graph.functions[name]
            reasons: list[str] = []
            entry_su = matched.get(name)
            if entry_su is not None:
                frame, source, bounded = entry_su.size, "su", entry_su.bounded
                if not entry_su.bounded:
                    reasons.append(f"可変長の確保（.su: {entry_su.qualifier}）")
                elif entry_su.qualifier != "static":
                    notes.append(f"{name}: .su が {entry_su.qualifier}（コンパイラが上限を決めた可変の確保）")
                if frame != flow.frame_bound and not (flow.dynamic or flow.truncated):
                    mismatched.append(name)
            else:
                frame, source, bounded = flow.frame_bound, "disasm", not (flow.dynamic or flow.truncated)
                if flow.dynamic or flow.truncated:
                    reasons += flow.reasons
            if flow.uncertain:
                notes.append(f"{name}: 分岐の先が分からない所がある（枠はたどれた {flow.frame} に、たどれない命令の "
                             f"SP を下げる量の和 {flow.unreached_frame} を足した）")
            if name in cyclic:
                bounded = False
                cycle = next(c for c in cycles if name in c)
                reasons.append("再帰: " + " → ".join(cycle + [cycle[0]]))
            best, path, unknown_from = frame, [name], set()
            for site in flow.calls:
                depth = _site_depth(site, frame, source, flow.frame_bound)
                callees, why = _callees(graph, name, site, indirect, assume_address_taken)
                if why is not None:
                    bounded = False
                    reasons.append(why)
                for callee in callees:
                    if callee not in results:
                        if component_of.get(callee) == component_of[name]:
                            continue                                     # 輪の中（再帰として数えた）
                        bounded = False
                        reasons.append(f"{callee} を解析できない（{graph.failed.get(callee, 'ELF に無い')}）")
                        continue
                    r = results[callee]
                    if not r.bounded:
                        unknown_from |= r.unknown_from or {callee}
                    if depth + r.max > best or (depth + r.max == best and len(path) == 1):
                        best, path = depth + r.max, [name] + r.path
            if not bounded:
                unknown_from.add(name)
            all_bounded = bounded and not unknown_from
            results[name] = _Result(frame, source, best, all_bounded, reasons, path, unknown_from)
    if mismatched:
        notes.append(f".su と逆アセンブルの枠が食い違う関数: {', '.join(sorted(mismatched))}（.su を使った）")
    if not su:
        notes.append(".su が無いので枠は逆アセンブルの見積もり（-fstack-usage の .su を渡すとコンパイラの値になる）")
    if graph.failed:
        notes.append(f"読めなかった関数: {', '.join(sorted(graph.failed))}")
    frame_size = exception_frame if exception_frame is not None else (
        EXTENDED_FRAME if graph.uses_fpu() else BASIC_FRAME)
    # 例外の枠の大きさの出どころ（使う側が渡した / ELF に浮動小数点の命令がある → 拡張の枠 / 無い → 基本の枠）
    frame_source = ("argument" if exception_frame is not None else
                    "fpu_instructions" if graph.uses_fpu() else "no_fpu_instructions")
    root_rows = _roots(graph, results, roots, entry, frame_size)
    if graph.vector_table is None:
        # 例外の処理が分からないので、例外の分を足す MSP の見積もりは出さない（0 と見せない）
        notes.append("ベクタ表を渡していない（vector_table）: 例外の処理の根と MSP の見積もり（msp）は無い"
                     + ("" if entry is not None else "。リセットの根は entry で渡せる"))
        msp = None
    else:
        msp = _msp(root_rows, frame_size, graph, frame_source)
    return {"functions": [_row(name, results[name]) for name in
                          sorted(results, key=lambda n: (-results[n].max, graph.functions[n].address))],
            "roots": root_rows, "msp": msp, "cycles": cycles, "notes": notes}


def _site_depth(site: CallSite, frame: int, source: str, disasm_frame: int) -> int:
    """呼び出しの場所の深さ。.su の枠が逆アセンブルと違うときは、呼び出しは .su の枠の深さとみなす
    （末尾の呼び出しは逆アセンブルの深さ = 枠を戻した後）。"""
    if source == "su" and frame != disasm_frame and site.kind in ("call", "indirect"):
        return frame
    return min(site.depth, frame) if source == "su" and site.kind in ("tail", "indirect_tail") else site.depth


def _callees(graph: CallGraph, name: str, site: CallSite, indirect: dict[str, list[str]],
             assume_address_taken: bool) -> tuple[list[str], str | None]:
    if site.callee is not None:
        return [site.callee], None
    if site.kind in ("call", "tail") and site.target is not None:
        return [], f"0x{site.address:08X}: ELF に無い呼び先 0x{site.target:08X}"
    if name in indirect:
        return list(indirect[name]), None
    if assume_address_taken:
        return sorted(graph.address_taken), None
    return [], (f"0x{site.address:08X}: 関数ポインタの呼び出し（呼び先が分からない。indirect で候補を渡すか "
                "assume_address_taken）")


def _row(name: str, r: _Result) -> dict:
    return {"function": name, "frame": r.frame, "frame_source": r.frame_source, "max": r.max, "bounded": r.bounded,
            "reasons": r.reasons, "unknown_from": sorted(r.unknown_from - {name}) if r.unknown_from else [],
            "path": r.path}


def _roots(graph: CallGraph, results: dict[str, _Result], extra: Iterable[str], entry: str | None,
           exception_frame: int) -> list[dict]:
    rows: list[dict] = []
    reset = entry or graph.vectors.get(1)
    handlers: dict[str, list[int]] = {}
    for n, handler in sorted(graph.vectors.items()):
        if n >= 2:
            handlers.setdefault(handler, []).append(n)

    def add(name: str, kind: str, **more) -> None:
        if name not in results or any(r["root"] == name for r in rows):
            return
        r = results[name]
        rows.append({"root": name, "kind": kind, "max": r.max, "bounded": r.bounded, "path": r.path, **more})

    if reset is not None:
        add(reset, "reset")
    for handler, numbers in handlers.items():
        add(handler, "exception", exceptions=numbers, total=results[handler].max + exception_frame
            if handler in results else None)
    for name in extra:
        if name not in results:
            raise KeyError(f"根の関数 {name!r} が ELF に無い")
        add(name, "user")
    called = {c.callee for f in graph.functions.values() for c in f.calls
              if c.callee is not None and c.callee != f.name}
    for name in sorted(results, key=lambda n: graph.functions[n].address):
        if name not in called:
            add(name, "address_taken" if name in graph.address_taken else "uncalled")
    return rows


def _msp(roots: list[dict], exception_frame: int, graph: CallGraph, frame_source: str) -> dict:
    thread = next((r for r in roots if r["kind"] == "reset"), None)
    handlers = [r for r in roots if r["kind"] == "exception"]
    base = thread["max"] if thread else 0
    worst = max((h["total"] for h in handlers), default=0)
    bounded = (thread is None or thread["bounded"]) and all(h["bounded"] for h in handlers)
    return {"thread": thread["root"] if thread else None, "thread_max": base, "exception_frame": exception_frame,
            "exception_frame_source": frame_source,
            "fpu": graph.uses_fpu(), "one_level": base + worst, "all_nested": base + sum(h["total"] for h in handlers),
            "bounded": bounded,
            "notes": ["one_level = スレッドの最大 + 最も深い例外の処理 1 つ。all_nested = 例外の処理がすべて入れ子になる"
                      "最悪（優先度がすべて違うとき。同じ優先度は入れ子にならないので実際はこれ以下）。RTOS のタスクは"
                      "各タスクのスタック（rtos_tasks）で、MSP は割り込みだけになる"]}


# --- 塗り・実測 ---------------------------------------------------------------------------------

def paint_range(bottom: int, top: int, *, sp: int | None, margin: int = 64) -> tuple[int, int]:
    """塗る範囲 [bottom, end)。end = いまの SP − margin（使っている所を壊さない。4 バイトに揃える）。SP が上端
    （リセット直後）か分からなければ top まで（margin の分を「使った」と数えないように）。"""
    if not bottom < top:
        raise ValueError(f"範囲が空: 0x{bottom:08X}〜0x{top:08X}")
    end = top if sp is None or sp >= top else min(top, (sp - margin) & ~3)
    if end <= bottom:
        raise ValueError(f"SP 0x{sp:08X} が下端 0x{bottom:08X} に近すぎて塗れない")
    return bottom, end


def measure_stack(read: Callable[[int, int], bytes], bottom: int, top: int, fill: int, *, sp: int | None = None,
                  threshold: float | None = None) -> dict:
    """底から塗りの値が続くバイト数（= まだ一度も使っていない深さ）から、使った最大の深さ・割合（事実）。
    reached_bottom = 塗りが 1 バイトも残っていない、overflow = SP が下端より下。warning は threshold を明示したとき
    だけ（割合が threshold 以上。経験則の既定の閾値は持たない）。"""
    size = top - bottom
    untouched = untouched_bytes(read, bottom, size, fill)
    used = size - untouched
    overflow = sp is not None and sp < bottom
    reached_bottom = untouched == 0
    fraction = used / size if size else 0.0
    notes = ["塗りの値と同じ値を書いた所は使っていないと数える（実際の最大はわずかに大きいことがある）"]
    if reached_bottom:
        notes.append("塗りが下端まで 1 バイトも残っていない（下端より下まで書いたかは、ここからは分からない）")
    if overflow:
        notes.append("SP が下端より下")
    return {"bottom": bottom, "top": top, "size": size, "fill": fill, "untouched": untouched, "used": used,
            "fraction": fraction, "sp": sp, "current": None if sp is None else top - sp, "overflow": overflow,
            "reached_bottom": reached_bottom, "warning": None if threshold is None else fraction >= threshold,
            "threshold": threshold, "notes": notes}
