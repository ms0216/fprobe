"""プロファイル・カバレッジを、ほかのツールの形式へ（U18）。

| 形式 | 開くもの | 元 |
|---|---|---|
| gmon.out | gprof（arm-none-eabi-gprof app.elf gmon.out）| PC サンプルの番地の度数（OpenOCD の profile と同じ形）、テストポイントの呼び出しの組（arc）|
| callgrind | KCacheGrind / QCacheGrind | PC サンプル（関数・行の自分の数）、テストポイントの呼び出しの木（呼び出し・含む時間）|
| lcov（.info）| genhtml・IDE のカバレッジ表示・CI | PC サンプルの近似のカバレッジ、テストポイントの C0 / C1（BRDA）|
| gcov（.gcov のテキスト）| gcovr 等・人 | 同上（ソースがあれば行の中身も）|
| dot | Graphviz | テストポイントの呼び出しの木（実の呼び出し）、PC サンプル + 逆アセンブルの静的な呼び出し（破線）|

カバレッジは FileCoverage（ファイルごとの行の回数・関数・分岐）にまとめてから各形式に書く。

仕様: docs/superpowers/specs/2026-10-18-u18-u26-profile-export-stack-design.md
"""

import posixpath
import struct
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

__all__ = ["FileCoverage", "FunctionRecord", "callgrind_from_profile", "callgrind_from_tree", "coverage_from_profile",
           "dwarf_declarations",
           "coverage_from_testpoint", "dot_from_profile", "dot_from_tree", "gmon_bytes", "render_gcov", "render_lcov",
           "tree_edges", "write_gcov"]

# --- gmon.out -----------------------------------------------------------------------------------

_GMON_TAG_TIME_HIST = 0
_GMON_TAG_CG_ARC = 1
_BIN_BYTES = 2                     # 1 つの度数の箱が受け持つ番地の幅（Thumb の命令の最小）
_MAX_BINS = 1 << 22


def gmon_bytes(pcs: Iterable[int], *, rate_hz: float, low: int | None = None, high: int | None = None,
               arcs: Iterable[tuple[int, int, int]] = ()) -> tuple[bytes, dict]:
    """gprof の gmon.out（32 ビット・リトルエンディアン）。番地の度数（[low, high) を 2 バイトごとの箱に。省くと
    サンプルの最小〜最大）と、呼び出しの組 arcs（(呼び出し元の番地, 呼び先の番地, 回数)）。
    箱は 16 ビットなので、超える数は全部を同じ数で割り、率（prof_rate）も同じだけ割る（時間 = 数 / 率 は保つ）。
    OpenOCD の profile は 65535 で頭打ちにする。"""
    pcs = [pc & ~1 for pc in pcs]
    out = bytearray(b"gmon" + struct.pack("<I", 1) + bytes(12))
    info: dict = {"samples": 0, "outside": 0, "scale": 1, "low": None, "high": None, "bins": 0}
    if pcs:
        lo = min(pcs) if low is None else low
        hi = (max(pcs) + _BIN_BYTES) if high is None else high
        if hi <= lo:
            raise ValueError(f"番地の範囲が空: 0x{lo:08X}〜0x{hi:08X}")
        width = _BIN_BYTES
        while (hi - lo + width - 1) // width > _MAX_BINS:
            width *= 2
        count = (hi - lo + width - 1) // width
        bins = [0] * count
        for pc in pcs:
            if lo <= pc < hi:
                bins[(pc - lo) // width] += 1
            else:
                info["outside"] += 1
        peak = max(bins, default=0)
        scale = max(1, -(-peak // 0xFFFF))
        if scale > 1:
            bins = [(n + scale // 2) // scale for n in bins]
        rate = max(1, round(rate_hz / scale))
        out += bytes([_GMON_TAG_TIME_HIST])
        out += struct.pack("<IIII", lo, lo + count * width, count, rate)
        out += b"seconds".ljust(15, b"\0") + b"s"
        out += struct.pack(f"<{count}H", *bins)
        info.update(samples=len(pcs) - info["outside"], scale=scale, low=lo, high=lo + count * width, bins=count,
                    bin_bytes=width, prof_rate=rate)
    n_arcs = 0
    for source, target, calls in arcs:
        out += bytes([_GMON_TAG_CG_ARC]) + struct.pack("<III", source & ~1, target & ~1, calls)
        n_arcs += 1
    info["arcs"] = n_arcs
    return bytes(out), info


# --- callgrind ----------------------------------------------------------------------------------

def _callgrind_header(events: str, cmd: str, extra: Sequence[str] = ()) -> list[str]:
    return ["# callgrind format", "version: 1", "creator: fprobe", *(["cmd: " + cmd] if cmd else []),
            *extra, "positions: line", f"events: {events}", ""]


def callgrind_from_profile(profile, *, cmd: str = "") -> str:
    """PC サンプルの関数・行ごとの自分の数（呼び出しの関係は PC サンプリングでは分からないので書かない）。"""
    lines = _callgrind_header("Samples", cmd, ["# PC sampling: self samples only (no call stacks)"])
    per: dict[tuple[str, str | None], Counter] = {}
    for (function, file, line), n in profile.line_counts.items():
        per.setdefault((function, file), Counter())[line or 0] += n
    for (function, file), counts in sorted(per.items(), key=lambda kv: (kv[0][1] or "", kv[0][0])):
        lines += [f"fl={file or '???'}", f"fn={function}"]
        lines += [f"{line} {n}" for line, n in sorted(counts.items())]
        lines.append("")
    lines.append(f"totals: {sum(profile.function_counts.values())}")
    return "\n".join(lines) + "\n"


def tree_edges(tree) -> dict[tuple[str, str], tuple[int, int]]:
    """呼び出しの木（fprobe.testpoint.CallTreeNode）→ {(呼び出し元, 呼び先): (回数, 含む時間)}（文脈をまとめる）。"""
    edges: dict[tuple[str, str], tuple[int, int]] = {}
    work = [tree]
    while work:
        node = work.pop()
        for child in node.children.values():
            if node.function:
                count, inclusive = edges.get((node.function, child.function), (0, 0))
                edges[(node.function, child.function)] = (count + child.count, inclusive + child.inclusive)
            work.append(child)
    return edges


def _tree_self(tree) -> Counter:
    own: Counter = Counter()
    work = [tree]
    while work:
        node = work.pop()
        if node.function:
            own[node.function] += node.exclusive
        work += node.children.values()
    return own


def callgrind_from_tree(tree, hz: float, *, file_of: Callable[[str], str | None],
                        line_of: Callable[[str], int | None], cmd: str = "") -> str:
    """テストポイントの呼び出しの木: 関数ごとの自分の時間と、呼び出し（calls= 回数）ごとの含む時間（単位はタイムベースの
    刻み = 1 / hz 秒）。呼び出しの場所の行は分からないので呼び出し元の関数の行。"""
    lines = _callgrind_header("Ticks", cmd, [f"# 1 Tick = {1e9 / hz:.6g} ns", "event: Ticks : time (1/hz s)"])
    own = _tree_self(tree)
    edges = tree_edges(tree)
    names = sorted(set(own) | {a for a, _ in edges} | {b for _, b in edges})
    for name in names:
        line = line_of(name) or 0
        lines += [f"fl={file_of(name) or '???'}", f"fn={name}", f"{line} {own[name]}"]
        for (caller, callee), (count, inclusive) in sorted(edges.items()):
            if caller != name:
                continue
            lines += [f"cfl={file_of(callee) or '???'}", f"cfn={callee}", f"calls={count} {line_of(callee) or 0}",
                      f"{line} {inclusive}"]
        lines.append("")
    lines.append(f"totals: {sum(own.values())}")
    return "\n".join(lines) + "\n"


# --- カバレッジ（lcov・gcov）---------------------------------------------------------------------

@dataclass
class FunctionRecord:
    line: int
    count: int
    returned: int | None = None              # 戻った回数（分かるとき）
    blocks: tuple[int, int] | None = None    # (通った塊, 塊)（分かるとき）


@dataclass
class FileCoverage:
    path: str
    lines: dict[int, int] = field(default_factory=dict)                  # 行 → 回数（0 = 通らなかった）
    functions: dict[str, FunctionRecord] = field(default_factory=dict)
    branches: dict[int, list[tuple[int, int, int | None]]] = field(default_factory=dict)  # 行 → [(組, 枝, 回数)]


Declarations = Callable[[int], tuple[str | None, int | None]]


def dwarf_declarations(info) -> Declarations:
    """関数の先頭の番地 → DWARF の関数（DW_TAG_subprogram）の (DW_AT_decl_file, DW_AT_decl_line)。info は
    fprobe.dwarf.info.DebugInfo。その番地から始まる関数が無い・属性が無ければ None。ファイルは行の表と同じ
    ファイル名（ディレクトリを除く）。"""
    def declared(address: int) -> tuple[str | None, int | None]:
        function = info.function_at(address)
        if function is None or function.low != address:
            return None, None
        return function.decl_file, function.decl_line
    return declared


def coverage_from_profile(cov, profile, declarations: Declarations) -> tuple[list[FileCoverage], list[str]]:
    """PC サンプリングの近似のカバレッジ（fprobe.profile.coverage の結果）と、行ごとのサンプル数。サンプルが無い行は 0
    （通らなかったとは限らない）。関数の記録（FN）は declarations（関数の番地 → (DW_AT_decl_file, DW_AT_decl_line)。
    dwarf_declarations）のファイル・行に置く（行の多いファイルを所在とみなさない）。宣言の分からない関数は FN を
    書かず、その名前の一覧を 2 つ目に返す。"""
    files: dict[str, FileCoverage] = {}
    undeclared: list[str] = []
    for f in cov.functions:
        for file, line in f.lines:
            fc = files.setdefault(file, FileCoverage(file))
            fc.lines[line] = fc.lines.get(line, 0) + profile.line_counts.get((f.function, file, line), 0)
        file, line = declarations(f.address)
        if file is None or line is None:
            undeclared.append(f.function)
            continue
        files.setdefault(file, FileCoverage(file)).functions[f.function] = FunctionRecord(
            line, profile.function_counts.get(f.function, 0))
    return [files[k] for k in sorted(files)], undeclared


def coverage_from_testpoint(report, table) -> list[FileCoverage]:
    """テストポイントの C0（ソースにある塊の印 → 行の回数）・関数（入口の回数・戻った回数・塊）・C1（分岐の組 → BRDA。
    組のどの枝も通らなければ回数は None = 判定まで来なかった）。"""
    from ..testpoint.probes import BLOCK_KINDS
    hits = report.hit_counts
    files: dict[str, FileCoverage] = {}
    names = table.function_names()
    starts = {(f.file, f.name): f.line for f in table.functions}
    per_function: dict[tuple[str, str], list] = {}
    for p in table.probes.values():
        fc = files.setdefault(p.file, FileCoverage(p.file))
        per_function.setdefault((p.file, p.function), []).append(p)
        if p.kind in BLOCK_KINDS and not p.implicit:
            fc.lines[p.line] = max(fc.lines.get(p.line, 0), hits[p.id])
    for (file, function), probes in per_function.items():
        entries = [p for p in probes if p.kind == "entry"]
        if not entries:
            continue
        blocks = [p for p in probes if p.kind in BLOCK_KINDS and not p.implicit]
        files[file].functions[names[(file, function)]] = FunctionRecord(
            starts.get((file, function), min(p.line for p in entries)), sum(hits[p.id] for p in entries),
            sum(hits[p.id] for p in probes if p.kind == "exit"),
            (sum(1 for p in blocks if hits[p.id]), len(blocks)))
    for decision, group in table.decisions().items():
        reached = any(hits[p.id] for p in group)
        line = group[0].line
        files[group[0].file].branches.setdefault(line, []).extend(
            (decision, n, hits[p.id] if reached else None) for n, p in enumerate(group))
    return [files[k] for k in sorted(files)]


def _source_path(path: str, source_root: str | Path | None) -> str:
    if source_root is None or PurePosixPath(path.replace("\\", "/")).is_absolute() or Path(path).is_absolute():
        return path.replace("\\", "/")
    return posixpath.join(str(source_root).replace("\\", "/"), path.replace("\\", "/"))


def render_lcov(files: Sequence[FileCoverage], test_name: str, *, source_root: str | Path | None = None) -> str:
    """lcov の追跡ファイル（.info）。SF はソースの道（source_root で相対の道を付け足す）。"""
    out = []
    for fc in files:
        out += [f"TN:{test_name}", f"SF:{_source_path(fc.path, source_root)}"]
        functions = sorted(fc.functions.items(), key=lambda kv: (kv[1].line, kv[0]))
        out += [f"FN:{r.line},{name}" for name, r in functions]
        out += [f"FNDA:{r.count},{name}" for name, r in functions]
        out += [f"FNF:{len(functions)}", f"FNH:{sum(1 for _, r in functions if r.count)}"]
        branches = [(line, b) for line in sorted(fc.branches) for b in fc.branches[line]]
        out += [f"BRDA:{line},{block},{branch},{'-' if taken is None else taken}"
                for line, (block, branch, taken) in branches]
        out += [f"BRF:{len(branches)}", f"BRH:{sum(1 for _, (_, _, t) in branches if t)}"]
        out += [f"DA:{line},{n}" for line, n in sorted(fc.lines.items())]
        out += [f"LF:{len(fc.lines)}", f"LH:{sum(1 for n in fc.lines.values() if n)}", "end_of_record"]
    return "\n".join(out) + "\n"


def render_gcov(fc: FileCoverage, source: Sequence[str] | None) -> str:
    """gcov のテキスト（gcov -b の形: 回数・##### = 通らなかった・- = 命令の無い行、関数の行・枝の行）。
    source（ソースの行の列）が無ければ、分かる行だけ。"""
    out = [f"{'-':>9}:{0:>5}:Source:{fc.path}"]
    by_line: dict[int, list[tuple[str, FunctionRecord]]] = {}
    for name, r in fc.functions.items():
        by_line.setdefault(r.line, []).append((name, r))
    numbers = range(1, len(source) + 1) if source is not None else sorted(set(fc.lines) | set(fc.branches))
    for n in numbers:
        for name, r in by_line.get(n, []):
            if r.returned is not None and r.blocks is not None:
                returned = round(100 * r.returned / r.count) if r.count else 0
                blocks = round(100 * r.blocks[0] / r.blocks[1]) if r.blocks[1] else 0
                out.append(f"function {name} called {r.count} returned {returned}% blocks executed {blocks}%")
            else:
                out.append(f"function {name} called {r.count}")
        text = source[n - 1] if source is not None else ""
        count = fc.lines.get(n)
        mark = "-" if count is None else "#####" if count == 0 else str(count)
        out.append(f"{mark:>9}:{n:>5}:{text}")
        for _block, branch, taken in fc.branches.get(n, []):
            out.append(f"branch {branch:>2} never executed" if taken is None else f"branch {branch:>2} taken {taken}")
    return "\n".join(out) + "\n"


def write_gcov(files: Sequence[FileCoverage], out_dir: str | Path, *,
               source_root: str | Path | None = None) -> list[Path]:
    """ファイルごとの .gcov を out_dir に（名前は元の名前 + .gcov。同じ名前が重なれば道の / を # にした名前）。
    source_root の下にソースがあれば行の中身も書く。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = Counter(PurePosixPath(fc.path.replace("\\", "/")).name for fc in files)
    written = []
    for fc in files:
        posix = fc.path.replace("\\", "/")
        name = PurePosixPath(posix).name
        if base[name] > 1:
            name = posix.strip("/").replace("/", "#")
        source = None
        candidate = Path(_source_path(fc.path, source_root))
        if candidate.is_file():
            source = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        path = out_dir / f"{name}.gcov"
        path.write_text(render_gcov(fc, source), encoding="utf-8", newline="\n")
        written.append(path)
    return written


# --- dot ----------------------------------------------------------------------------------------

def _color(heat: float) -> str:
    """0（青）〜 1（赤）の熱の色（Graphviz の HSV）。"""
    heat = min(max(heat, 0.0), 1.0)
    return f"{(1 - heat) * 0.66:.3f} {0.25 + 0.6 * heat:.3f} 1.000"


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _render_dot(title: str, nodes: dict[str, tuple[float, str]],
                edges: dict[tuple[str, str], tuple[str, bool]]) -> str:
    out = [f"digraph {_quote(title)} {{", '  node [shape=box, style="rounded,filled", fontname="sans-serif"];',
           '  edge [fontname="sans-serif"];']
    for name, (heat, label) in nodes.items():
        out.append(f"  {_quote(name)} [label={_quote(label)}, fillcolor={_quote(_color(heat))}];")
    for (a, b), (label, dashed) in sorted(edges.items()):
        style = ', style="dashed"' if dashed else ""
        out.append(f"  {_quote(a)} -> {_quote(b)} [label={_quote(label)}{style}];")
    out.append("}")
    return "\n".join(out) + "\n"


def dot_from_tree(tree, functions: dict, hz: float, *, min_fraction: float = 0.0) -> str:
    """テストポイントの呼び出しの木: 関数（含む時間・自分の時間の割合・回数）と呼び出し（回数）。割合の分母は一番外の
    呼び出しの含む時間の合計。min_fraction 未満の関数は省く。"""
    total = sum(child.inclusive for child in tree.children.values()) or 1
    own = _tree_self(tree)
    nodes = {}
    for name, stats in functions.items():
        inclusive = stats.total_inclusive / total
        if inclusive < min_fraction:
            continue
        nodes[name] = (inclusive, f"{name}\n含む {100 * inclusive:.1f}%（{stats.total_inclusive / hz * 1e6:.1f} µs）\n"
                                  f"自分 {100 * own[name] / total:.1f}%\n×{stats.calls}")
    edges = {(a, b): (f"×{count}", False) for (a, b), (count, _) in tree_edges(tree).items()
             if a in nodes and b in nodes}
    return _render_dot("testpoint call graph", nodes, edges)


def dot_from_profile(profile, graph=None, *, min_fraction: float = 0.0) -> str:
    """PC サンプル: 関数（自分の割合）。graph（fprobe.callgraph.CallGraph）があれば、載せた関数の間の静的な呼び出しを
    破線で（回数は分からない）。"""
    total = profile.total or 1
    nodes = {}
    for name, n in profile.function_counts.items():
        if n / total < min_fraction:
            continue
        nodes[name] = (n / total, f"{name}\n{100 * n / total:.1f}%（{n}）")
    edges = {}
    if graph is not None:
        for name in nodes:
            flow = graph.functions.get(name)
            for site in flow.calls if flow is not None else ():
                if site.callee in nodes and site.callee != name:
                    edges[(name, site.callee)] = ("tail" if site.kind == "tail" else "", True)
    return _render_dot("pc sampling", nodes, edges)
