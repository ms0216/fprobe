"""デバッガの変数・式・呼び出し履歴の部分（DWARF の索引 Inspector 越し）: 変数の読み書き、バックトレース、フレームの
変数、C の式の評価と書き込み、型の情報。Debugger に混ぜて使う（self.target・self.symbols は Debugger が持つ）。"""

import functools
from dataclasses import replace
from pathlib import Path

from .debug_types import Frame, PyocdAccess
from .dwarf.info import DebugInfo
from .dwarf.inspector import Inspector


@functools.lru_cache(maxsize=4)
def _load_debug_info(path: str, stamp: tuple[int, int]) -> DebugInfo | None:
    """ELF の DWARF の索引（パスと更新時刻・大きさで共有。Debugger を作り直しても読み直さない）。無ければ None。"""
    try:
        return DebugInfo(path)
    except ValueError:
        return None


class InspectMixin:
    # --- 変数 ------------------------------------------------------------------------

    def read_variable(self, name: str, index: int | None = None) -> int | bytes:
        """変数を読む。大きさ 1 / 2 / 4 / 8 なら符号なし整数、それ以外（配列・構造体）はバイト列。
        index を指定すると 4 バイトの要素の配列として index 番目を読む。"""
        var = self.symbols.variable(name)
        if index is not None:
            if not 0 <= index * 4 < var.size:
                raise IndexError(f"{name} は {var.size // 4} 要素")
            return self.target.read32(var.address + 4 * index)
        data = bytes(self.target.read_memory_block8(var.address, var.size))
        return int.from_bytes(data, "little") if var.size in (1, 2, 4, 8) else data

    def write_variable(self, name: str, value: int, index: int | None = None) -> None:
        var = self.symbols.variable(name)
        if index is not None:
            self.target.write32(var.address + 4 * index, value)
        else:
            if var.size not in (1, 2, 4, 8):
                raise ValueError(f"{name} は {var.size} バイト。整数として書けるのは 1 / 2 / 4 / 8 バイト")
            self.target.write_memory_block8(var.address, list(value.to_bytes(var.size, "little")))
        self.target.flush()

    # --- 呼び出しの様子 ------------------------------------------------------------------

    def backtrace(self, max_frames: int = 64, inspector: Inspector | None = None,
                  assume_leaf: bool = False) -> list[Frame]:
        """呼び出し履歴。ELF に CFI（.debug_frame）があれば CFI で巻き戻す（例外のフレームも越える）。
        無ければ簡易版（いまの PC、LR。例外の中なら積まれたフレームの PC・LR）。LR から出したフレームは、LR が戻り先の
        ままだと確かめていないので note = "lr_without_cfi"。停止中に呼ぶ。
        inspector を渡すとその巻き戻しを使う（続けて同じ窓口でフレームの変数を読むとき、巻き戻しは 1 回）。"""
        inspector = inspector if inspector is not None else self.inspector(max_frames, assume_leaf)
        if inspector is not None and len(inspector.unwinder.table):
            frames = []
            for state in inspector.frames():
                where = self.where(state.lookup_pc)
                if state.lookup_pc != state.pc:                      # 呼び出し元: 番地は戻り先、行は呼び出し命令
                    where = replace(where, address=state.pc,
                                    offset=None if where.offset is None else where.offset + state.pc - state.lookup_pc)
                frames.append(Frame(state.pc, where, state.note, state.cfa, state.stop))
            return frames
        return self._simple_backtrace()

    def _simple_backtrace(self) -> list[Frame]:
        regs = dict(zip(("pc", "lr", "xpsr", "msp", "psp"),
                        self.target.read_core_registers_raw(["pc", "lr", "xpsr", "msp", "psp"])))
        frames = [Frame(regs["pc"], self.where(regs["pc"]))]
        lr = regs["lr"]
        if regs["xpsr"] & 0x1FF and lr >> 24 == 0xFF:             # 例外の中で、LR が EXC_RETURN
            sp = regs["psp"] if lr & 0x4 else regs["msp"]
            stacked = self.target.read_memory_block32(sp, 8)       # r0 r1 r2 r3 r12 lr pc xpsr
            frames.append(Frame(stacked[6], self.where(stacked[6]), "exception"))
            frames.append(Frame(stacked[5] & ~1, self.where(stacked[5] & ~1), "lr_without_cfi"))
        else:
            frames.append(Frame(lr & ~1, self.where(lr & ~1), "lr_without_cfi"))
        return frames

    # --- ソースの言葉の変数（DWARF） ---------------------------------------------------------

    def debug_info(self) -> DebugInfo | None:
        """ELF の DWARF（型・変数・CFI）。DWARF が無い・ELF が無いなら None。symbols を替えれば、その ELF のものになる。"""
        if self.symbols is None or getattr(self.symbols, "path", None) is None:
            return None
        path = Path(self.symbols.path)
        stat = path.stat()
        return _load_debug_info(str(path), (stat.st_mtime_ns, stat.st_size))

    def inspector(self, max_frames: int = 64, assume_leaf: bool = False) -> Inspector | None:
        """いまの停止の様子を調べる窓口（停止のたびに作る）。DWARF が無ければ None。assume_leaf は dwarf.unwind.Unwinder。"""
        info = self.debug_info()
        if info is None:
            return None
        return Inspector(info, PyocdAccess(self.target), self.symbols, max_frames, assume_leaf)

    def _require_inspector(self) -> Inspector:
        inspector = self.inspector()
        if inspector is None:
            raise RuntimeError("ELF に DWARF（-g）が無い: 式・ローカル変数は読めない")
        return inspector

    def frame_variables(self, frame: int = 0, max_elements: int = 64) -> dict:
        """そのフレームの引数とローカル変数: {"function", "arguments": [{name, type, value}], "locals": [...]}。"""
        return self._require_inspector().frame_variables(frame, max_elements)

    def evaluate(self, expression: str, frame: int = 0, max_elements: int = 64, live: bool = False) -> dict:
        """C の式を評価して読む: {"expression", "type", "address"（メモリ上なら）, "value"}。live: フレームを使わずに
        読む（走っているコア。大域・静的の変数とメモリだけ）。"""
        if live:
            return self._require_inspector().read_global(expression, max_elements)
        return self._require_inspector().read(expression, frame, max_elements)

    def write_expression(self, expression: str, value: int | float | str, frame: int = 0) -> None:
        """C の式の指す値（メモリ上の基本型・列挙（名前でも）・ポインタ・ビットフィールド）に書く。"""
        self._require_inspector().write(expression, value, frame)

    def type_info(self, name: str) -> dict:
        """型の名前（"struct Sensor"・typedef 名等）→ 大きさ・メンバー・列挙子。"""
        return self._require_inspector().type_info(name)

