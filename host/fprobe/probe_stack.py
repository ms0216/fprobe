"""Probe の道具: メモリ・スタックの使用量（U26）。ELF のメモリ使用量（Build Analyzer 相当）、静的な最大のスタック
（.su と逆アセンブルの呼び出しグラフ）、ベアメタルの MSP のスタックの塗りと実測。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う mixin。Probe の _lock・_debug・target・_symbols を使う。機器に触るのは
必ず _lock の中（elf_size・stack_static は ELF だけを読み、機器に触らない）。

- 領域（MEMORY）: regions（直接）> map_file（GNU ld の -Map）> linker_script > 接続中のターゲットのメモリマップ。
- .su: su_paths を省くと ELF のディレクトリの下を探す（STM32CubeIDE の Debug/ 等。.o の横に .su がある）。
- 塗り: 止めて、いまの MSP − margin より下を fill で埋める（使っている所は壊さない）。走らせた後に stack_usage が
  底から塗りの残りを数える（走行中でも読める）。RTOS のタスクのスタックは rtos_tasks（塗りは RTOS がする）。

仕様: docs/superpowers/specs/2026-10-18-u18-u26-profile-export-stack-design.md
"""

from dataclasses import dataclass
from pathlib import Path

from pyocd.core.target import Target

from .callgraph import CallGraph
from .disasm import Disassembler
from .elfsize import ElfLayout, MemoryRegion, StackBounds, parse_linker_regions, parse_map_regions, size_report, \
    stack_bounds
from .stackusage import analyze_stack, load_su, measure_stack, paint_range
from .symbols import is_network_path

__all__ = ["DEFAULT_FILL", "StackTools"]

DEFAULT_FILL = 0xCD                      # IAR と同じ塗りの値
_CHUNK = 4096


@dataclass
class _Paint:
    bounds: StackBounds
    end: int                             # 塗った上端（ここから上は塗っていない = 塗ったときに使っていた）
    fill: int


def _local_file(path: str, what: str) -> Path:
    if is_network_path(path):
        raise ValueError(f"外の共有の{what}は開かない: {path}")
    return Path(path)


class StackTools:
    """メモリ・スタックの使用量（Probe の mixin）。"""

    def _init_stack(self) -> None:
        self._stack_paint: _Paint | None = None
        self._stack_graph: tuple[object, tuple, CallGraph] | None = None   # (読んだ Symbols, ベクタ表, グラフ)

    # --- 共通 ----------------------------------------------------------------------------------

    def _layout_path(self, elf: str | None) -> Path:
        if elf is not None:
            return _local_file(elf, " ELF ")
        if self._symbols is None:
            raise RuntimeError("ELF が読み込まれていない（debug_elf_load か elf を指定）")
        return self._symbols.path

    def _regions(self, regions: list[dict] | None, map_file: str | None,
                 linker_script: str | None) -> tuple[list[MemoryRegion], str | None]:
        if regions is not None:
            return [MemoryRegion(r["name"], int(r["origin"]), int(r["length"]), r.get("attributes", ""))
                    for r in regions], "regions"
        if map_file is not None:
            text = _local_file(map_file, "マップファイル").read_text(encoding="utf-8", errors="replace")
            found = parse_map_regions(text)
            if not found:
                raise ValueError(f"{map_file} に Memory Configuration の表が無い（GNU ld の -Map の出力）")
            return found, "map_file"
        if linker_script is not None:
            text = _local_file(linker_script, "リンカスクリプト").read_text(encoding="utf-8", errors="replace")
            found = parse_linker_regions(text)
            if not found:
                raise ValueError(f"{linker_script} に MEMORY が無い")
            return found, "linker_script"
        debug = self._debug
        memory_map = getattr(getattr(debug, "target", None), "memory_map", None) if debug is not None else None
        if memory_map is not None:
            out = [MemoryRegion(r.name or ("flash" if r.is_flash else "ram"), r.start, r.length,
                                "rx" if r.is_flash else "rwx") for r in memory_map.regions]
            if out:
                return out, "target"
        return [], None

    # --- ELF のメモリ使用量 ----------------------------------------------------------------------

    def elf_size(self, elf: str | None = None, linker_script: str | None = None, map_file: str | None = None,
                 regions: list[dict] | None = None, top: int = 50, section: str | None = None,
                 region: str | None = None, kind: str | None = None, name: str | None = None) -> dict:
        """ELF のメモリ使用量（Build Analyzer）: 領域ごとの使用量・空き・使用率と内訳（.data の初期値は Flash にも数える）、
        区画、大きいシンボル（section・region・kind・name（glob）で絞り、大きい順に top 個）。"""
        layout = ElfLayout.from_elf(self._layout_path(elf))
        found, source = self._regions(regions, map_file, linker_script)
        return {**size_report(layout, found, top=top, section=section, region=region, kind=kind, name=name),
                "regions_source": source}

    # --- 静的な最大 ----------------------------------------------------------------------------

    def _call_graph(self, vector_table: int | None = None, vector_table_size: int | None = None,
                    branch_tables: list[dict] | None = None) -> CallGraph:
        if self._symbols is None:
            raise RuntimeError("ELF が読み込まれていない（debug_elf_load）")
        tables = {int(t["address"]): int(t["entries"]) for t in branch_tables or []}
        key = (vector_table, vector_table_size, tuple(sorted(tables.items())))
        cached = self._stack_graph
        if cached is not None and cached[0] is self._symbols and cached[1] == key:
            return cached[2]
        image = self._elf_image()                          # 読み込み済みの名前・行の表を使い回す（DWARF を読み直さない）
        graph = CallGraph.build(Disassembler(image, self._symbols, image, tables), vector_table=vector_table,
                                vector_table_size=vector_table_size)
        self._stack_graph = (self._symbols, key, graph)
        return graph

    def stack_static(self, su_paths: list[str] | None = None, indirect: dict[str, list[str]] | None = None,
                     assume_address_taken: bool = False, roots: list[str] | None = None, entry: str | None = None,
                     exception_frame: int | None = None, function: str | None = None, top: int = 50,
                     vector_table: int | None = None, vector_table_size: int | None = None,
                     branch_tables: list[dict] | None = None) -> dict:
        """静的な最大のスタック: 関数ごとの枠（.su か逆アセンブル）と呼び先を含む最大・最深の経路、根（リセット・例外・
        呼ばれない関数）、MSP の見積もり。再帰・関数ポインタ・可変長の確保は bounded=false と理由（max は下限）。
        su_paths を省くと ELF のディレクトリの下の .su。indirect = {関数: [関数ポインタの呼び先の候補]}。
        vector_table: ベクタ表の番地（使う側が渡す。名前で探さない）。無ければ例外の根と msp は無い。
        branch_tables: tbb / tbh の表の大きさ [{address, entries}]（使う側が渡す。無い表の分かれ先はたどらない）。"""
        graph = self._call_graph(vector_table, vector_table_size, branch_tables)
        if su_paths is None:
            su_paths = [str(self._symbols.path.parent)]
        su = load_su([_local_file(p, " .su ") for p in su_paths])
        symbols = self._symbols

        def file_of(name: str) -> str | None:
            try:
                return symbols.describe(symbols.symbol(name).address).file
            except KeyError:
                return None

        report = analyze_stack(graph, su, indirect=indirect, assume_address_taken=assume_address_taken,
                               roots=roots or (), entry=entry, exception_frame=exception_frame, file_of=file_of)
        rows = report["functions"]
        for row in rows:                       # 呼び出しの木（画面の Call Graph）: 番地と直接の呼び先
            flow = graph.functions[row["function"]]
            row.update(address=flow.address, calls=graph.callees(row["function"]))
        if function is not None:
            if function not in graph.functions:
                raise KeyError(f"関数 {function!r} が ELF に無い")
            rows = [r for r in rows if r["function"] == function]
        report.update(functions=rows[:top], functions_total=len(rows), su_paths=list(su_paths),   # 探した所（?48）
                      su_files=sorted({e.source for e in su}), address_taken=sorted(graph.address_taken),
                      vector_table=None if graph.vector_table is None else
                      {"address": graph.vector_table[0], "size": graph.vector_table[1]})
        return report

    # --- 塗り・実測 ----------------------------------------------------------------------------

    def _stack_bounds(self, bottom: int | None, top: int | None, names: dict) -> StackBounds:
        """names: top_symbol・vector_table・limit_symbol・size_symbol（使う側が渡す。fprobe.elfsize.stack_bounds）。"""
        if bottom is not None and top is not None:
            return StackBounds(top, bottom, None, "argument", "argument")
        if self._symbols is None:
            raise RuntimeError("ELF が読み込まれていない（debug_elf_load か bottom・top を指定）")
        found, _ = self._regions(None, None, None)
        bounds = stack_bounds(ElfLayout.from_elf(self._symbols.path), found, top=top, **names)
        if top is not None:
            bounds.top, bounds.top_source = top, "argument"
        if bottom is not None:
            bounds.bottom, bounds.bottom_source = bottom, "argument"
        return bounds

    def stack_paint(self, fill: int = DEFAULT_FILL, bottom: int | None = None, top: int | None = None,
                    margin: int = 64, top_symbol: str | None = None, vector_table: int | None = None,
                    limit_symbol: str | None = None, size_symbol: str | None = None) -> dict:
        """ベアメタルの MSP のスタックを塗る（止めて。リセット直後が最適）。範囲は top / bottom か、使う側が名前を
        渡す ELF のシンボル（上端 top_symbol か vector_table = ベクタ表の番地、下端 limit_symbol か上端 − size_symbol、
        無ければ上端の下の RAM の空き全部）。いまの MSP − margin より上は塗らない。"""
        if not 0 <= fill <= 0xFF:
            raise ValueError("fill は 1 バイト（0〜255）")
        with self._lock:
            target = self.target
            if target.get_state() != Target.State.HALTED:
                raise RuntimeError("コアを止めてから塗る（走行中は割り込みが塗る所を使うかもしれない）")
            bounds = self._stack_bounds(bottom, top, {"top_symbol": top_symbol, "vector_table": vector_table,
                                                      "limit_symbol": limit_symbol, "size_symbol": size_symbol})
            sp = target.read_core_register("msp")
            start, end = paint_range(bounds.bottom, bounds.top, sp=sp, margin=margin)
            pattern = bytes([fill]) * _CHUNK
            for at in range(start, end, _CHUNK):
                target.write_memory_block8(at, list(pattern[:min(_CHUNK, end - at)]))
            target.flush()
            self._stack_paint = _Paint(bounds, end, fill)
        return {"bottom": start, "end": end, "top": bounds.top, "fill": fill, "bytes": end - start, "msp": sp,
                "margin": margin, "bounds": bounds.as_dict()}                  # margin: MSP の下で塗らなかった幅

    def stack_usage(self, threshold: float | None = None, fill: int | None = None, bottom: int | None = None,
                    top: int | None = None, top_symbol: str | None = None, vector_table: int | None = None,
                    limit_symbol: str | None = None, size_symbol: str | None = None) -> dict:
        """MSP のスタックの使用量の実測: 底から塗りが残っているバイト数から、使った最大の深さ・割合（事実。
        reached_bottom = 塗りが残っていない、overflow = SP が下端より下、warning は threshold を明示したときだけ）。止まっていれば いまの深さ（current）も。走行中でも読める。
        範囲は stack_paint のもの（無ければ stack_paint と同じ引数の範囲）。塗りの値は fill か、stack_paint の記録の値
        （記録が無く fill も無ければ誤り: 塗りの値を慣習の値で仮定しない）。"""
        with self._lock:
            target = self.target
            paint = self._stack_paint
            names = {"top_symbol": top_symbol, "vector_table": vector_table, "limit_symbol": limit_symbol,
                     "size_symbol": size_symbol}
            if fill is None and paint is None:
                raise ValueError("stack_paint で塗った記録が無い: 塗りの値 fill（ファームが塗った値）を渡す")
            value = paint.fill if fill is None else fill
            if paint is not None and bottom is None and top is None and not any(v is not None for v in names.values()):
                bounds = paint.bounds
            else:
                bounds = self._stack_bounds(bottom, top, names)
            halted = target.get_state() == Target.State.HALTED
            sp = target.read_core_register("msp") if halted else None

            def read(address: int, length: int) -> bytes:
                return bytes(target.read_memory_block8(address, length))

            out = measure_stack(read, bounds.bottom, bounds.top, value, sp=sp, threshold=threshold)
        out["painted"] = paint is not None
        out["bounds"] = bounds.as_dict()
        if paint is None:
            out["notes"].append("stack_paint で塗った記録が無い: ファームが塗っていなければ値は意味を持たない")
        return out
