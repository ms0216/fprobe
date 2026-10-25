"""シンボル・ブレークポイント・実行（elf_load・breakpoint_*・watchpoint_*・run_until_halt・variable_*・backtrace）の mixin。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う。Probe の _lock・target・dbg・_symbols と、BreakpointActions の _set_action 等を使う。
止まった場所の書き方（_place・_place_of・_where）もここ。部品は fprobe.debug（Debugger）・fprobe.symbols。
"""

from .debug import Stop
from .probe_inspect import SOURCE_LIMIT
from .symbols import Location, Symbols, is_line_location, is_network_path


class DebugTools:
    """Probe に混ぜるシンボル・ブレークポイント・実行・変数の道具。"""

    def _where(self, address: int) -> str:
        return str(self._symbols.describe(address)) if self._symbols else f"0x{address:08X}"

    def _place_of(self, address: int) -> dict:
        return self._place(self._symbols.describe(address)) if self._symbols else {"where": f"0x{address:08X}"}

    @staticmethod
    def _place(location: Location) -> dict:
        """止まった場所: 表示用の where と、分かるときだけ function・file・line（画面が文字列を解釈しないため）。"""
        place = {"where": str(location)}
        for key in ("function", "file", "line"):
            if getattr(location, key) is not None:
                place[key] = getattr(location, key)
        return place

    def elf_load(self, path: str) -> dict:
        """ELF のシンボルと行番号を読み込む（以後、場所を関数名・変数名・"ファイル:行" で指定でき、止まった場所が
        関数名と行で返る）。flash で ELF を書いたときは自動で読む。"""
        with self._lock:
            self._symbols = Symbols(path)
            self._forget_elf()
            if self._dbg is not None:
                self._dbg.symbols = self._symbols
            self._elf_loaded()
            return {"path": str(self._symbols.path), "entry": self._symbols.entry,
                    "segments": [{"address": s.address, "bytes": len(s.data)} for s in self._symbols.segments],
                    **self.source_list()}

    def source_list(self) -> dict:
        """読み込んだ ELF の行番号の情報が参照するソース（名前・場所・この PC にあるか）。同じ名前の別のファイルは
        files には最初のものだけで、same_name に名前 → 場所の一覧（"ファイル:行" はディレクトリを添えて選ぶ）。"""
        with self._lock:
            if self._symbols is None:
                return {"files": [], "same_name": {}}
            relocated = self._symbols.relocated()
            return {"files": [{"name": name, "path": str(path), "exists": not is_network_path(path) and path.is_file(),
                               **({"built_path": str(relocated[name]["built"])} if name in relocated else {})}
                              for name, path in sorted(self._symbols.sources().items())],
                    "same_name": {name: [str(p) for p in paths]
                                  for name, paths in sorted(self._symbols.source_conflicts().items())}}

    def source_read(self, name: str) -> dict:
        """ソースの本文と、命令のある行（ブレークポイントを置ける行）。読めるのは ELF が参照するファイルだけ。"""
        with self._lock:
            if self._symbols is None:
                raise RuntimeError("ELF が読み込まれていない（debug_elf_load）")
            path = self._symbols.sources().get(name)
            if path is None:
                raise KeyError(f"{name!r} はこの ELF のソースではない")
            if is_network_path(path):
                raise ValueError(f"外の共有のソースは開かない: {path}")
            if path.stat().st_size > SOURCE_LIMIT:
                raise ValueError(f"{name} は大きすぎる（{SOURCE_LIMIT // (1 << 20)} MB まで）")
            return {"name": name, "path": str(path), "text": path.read_text(encoding="utf-8", errors="replace"),
                    "lines": self._symbols.lines_of(name)}

    def symbol_info(self, location: str) -> dict:
        """場所（関数名・変数名・"ファイル:行"・"0x..."）の番地と、その番地の関数名・ファイル:行（where の文字と、
        欄 function・offset・file・line。分からない欄は None）。"""
        with self._lock:
            if self._symbols is None:
                raise RuntimeError("ELF が読み込まれていない（elf_load）")
            address = self._symbols.resolve(location)
            place = self._symbols.describe(address)
            out = {"address": address, "where": str(place), "function": place.function, "offset": place.offset,
                   "file": place.file, "line": place.line}
            if not location.startswith("0x") and not is_line_location(location):
                s = self._symbols.symbol(location)
                out.update(kind=s.kind, size=s.size)
            return out

    def program_ram(self, path: str, vector_table: int | None = None) -> dict:
        """ELF を RAM に読み込み、起動できる状態で止める（Flash には書かない。RAM で動く試験用のプログラム向け）。
        vector_table（ベクタ表の番地。使う側が明示する）を渡せばその表の初期の MSP・リセットの処理から（VTOR も
        設定）、渡さなければ ELF の入口（e_entry）から（SP・VTOR は変えない。ベクタ表の場所を推定しない）。
        ターゲットはリセットされる。続けて run_until_halt / target_resume で走らせる。結果の start = {from:
        vector_table / entry, entry, vector_table}。"""
        with self._lock:
            self.elf_load(path)
            stop, start = self.dbg.load_to_ram(vector_table)
            self._image_written(elf=True)
            return {**self._stop_dict(stop), "start": start}

    def breakpoint_set(self, location: str, condition: str | None = None, ignore_count: int = 0,
                       log: list[str] | None = None, message: str | None = None) -> dict:
        """ブレークポイントを置く。location: 関数名・"ファイル:行"・"0x..."。condition: C の式（偽なら止まらない。
        ローカル変数も使える）、ignore_count: 最初の n 回は止まらない、log: 当たるたびに読む式の一覧（止まらずに
        breakpoint_log と記録に残す。message は添える文）。動作のあるブレークポイントは当たるたびにコアを数 ms 止める。"""
        with self._lock:
            address = self.dbg.breakpoint(location if not location.startswith("0x") else int(location, 16))
            self._disabled.pop(address, None)                       # 無効にしてあったものは置き直しで有効に
            self._set_action(address, condition, ignore_count, log, message)
            if self._actions:
                self._watch_stops(self.target.get_state().name != "HALTED")
            return {"address": address, **self._place_of(address)}

    def breakpoint_clear(self, location: str | None = None) -> dict:
        """ブレークポイントを外す（location 省略ですべて。ウォッチポイントも含めて外す）。"""
        with self._lock:
            if location is None:
                self.dbg.clear()
                self._drop_actions()
                self._heap_trace_detached()                       # malloc / free の記録のブレークポイントも外れた
                self._gdb_trace_detached()                        # GDB のトレースポイントも外れた
            else:
                address = self.dbg.breakpoint_address(location if not location.startswith("0x") else int(location, 16))
                if address not in self._disabled:
                    self.dbg.remove_breakpoint(address)
                self._drop_actions(address)
            return self.breakpoint_list()

    def breakpoint_list(self) -> dict:
        """置いているブレークポイント（enabled = false は breakpoint_enable で無効にしたもの。機器には置いていない）と
        ウォッチポイント。"""
        with self._lock:
            placed = [{"address": a, "location": n, "enabled": True, **self._place_of(a), **self._breakpoint_kind(a),
                       **({"action": self._actions[a].describe()} if a in self._actions else {}),
                       **({"tool": self._break_hooks[a][0]} if a in self._break_hooks else {})}
                      for a, n in self.dbg.breakpoints.items()]
            disabled = [{"address": a, "location": n, "enabled": False, **self._place_of(a),
                         **({"action": action.describe()} if action is not None else {})}
                        for a, (n, action) in self._disabled.items()]
            return {"breakpoints": sorted(placed + disabled, key=lambda b: b["address"]),
                    "watchpoints": [{"address": w.address, "size": w.size, "kind": w.kind}
                                    for w in self.dbg.watches.values()]}

    def watchpoint_set(self, location: str, kind: str = "write", size: int | None = None) -> dict:
        """ウォッチポイントを置く（変数名か "0x..."）。kind: write / read / access。size は変数名なら自動。"""
        with self._lock:
            w = self.dbg.watchpoint(location if not location.startswith("0x") else int(location, 16), size, kind)
            return {"address": w.address, "size": w.size, "kind": w.kind}

    def watchpoint_clear(self, location: str) -> dict:
        """ウォッチポイントを外す。"""
        with self._lock:
            self.dbg.remove_watchpoint(location if not location.startswith("0x") else int(location, 16))
            return self.breakpoint_list()

    def run_until_halt(self, timeout_s: float = 5.0) -> dict:
        """実行を再開し、止まるまで（ブレークポイント・ウォッチポイント等）か timeout_s まで待つ。時間切れなら止める。
        返す reason: breakpoint / watchpoint / step / user / timeout 等、where: 止まった関数名 + オフセットとファイル:行。
        セミホスティング（BKPT 0xAB）は処理して実行を続け、SYS_EXIT なら reason = "exit" と exit_code を返す。"""
        return self._run_until_halt(timeout_s)

    def variable_read(self, name: str, index: int | None = None) -> dict:
        """変数を名前で読む（1 / 2 / 4 / 8 バイトは整数、それ以外は 16 進文字列）。index は 4 バイト要素の配列の添字。"""
        with self._lock:
            value = self.dbg.read_variable(name, index)
            return {"name": name, "value": value if isinstance(value, int) else value.hex()}

    def variable_write(self, name: str, value: int, index: int | None = None) -> None:
        """変数を名前で書く（1 / 2 / 4 / 8 バイトの整数、または 4 バイト要素の配列の index 番目）。"""
        with self._lock:
            self.dbg.write_variable(name, value, index)

    def backtrace(self, variables: bool = False, max_frames: int = 64, max_elements: int = 64,
                  assume_leaf: bool = False) -> list[dict]:
        """呼び出し履歴（ELF に CFI があれば CFI で巻き戻し、例外のフレームも越える。無ければいまの位置と戻り先）。
        各段は index・pc・where・note（"exception" = 例外で中断された位置）・cfa・function・offset・file・line。
        CFI で巻き戻したときは最後の段に stop（その先へ巻き戻さなかった理由と、上限で切ったなら使った max_frames）。
        assume_leaf なら CFI の無い最内の関数を葉と仮定して LR から呼び出し元を出す（その段の note は lr_without_cfi）。
        variables なら各段の引数とローカル変数（arguments・locals）も。停止中に呼ぶ。"""
        with self._lock:
            return self._backtrace(variables, max_frames, max_elements, assume_leaf)

    @staticmethod
    def _stop_dict(stop: Stop) -> dict:
        out = {"reason": stop.reason, "pc": stop.pc, **DebugTools._place(stop.where), "elapsed_s": stop.elapsed_s}
        if stop.halt_bits is not None:
            out["halt_bits"] = stop.halt_bits
        return out
