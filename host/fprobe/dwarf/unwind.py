"""CFI（.debug_frame、無ければ .eh_frame）による呼び出し履歴の巻き戻しと、Cortex-M の例外のフレーム。

    table = CallFrameTable.from_dwarf(info.dwarf)
    frames = Unwinder(table).unwind(access)         # [FrameState(index, pc, lookup_pc, cfa, registers, note)]

レジスタの規則の無いレジスタは AAPCS に従う: callee-saved（r4〜r11、s16〜s31、sp、lr）は「同じ値」、caller-saved
（r0〜r3、r12、s0〜s15）は呼び出し元のフレームでは「分からない」（嘘の値を出さない）。
戻り先が EXC_RETURN なら、例外の入口でハードが積んだフレーム（r0 r1 r2 r3 r12 lr pc xpsr [+ s0〜s15 FPSCR]）から
中断された処理のレジスタを復元する。
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from elftools.dwarf.callframe import CFARule, FDE, RegisterRule
from elftools.dwarf.dwarf_expr import DWARFExprParser

from .access import AccessError, TargetAccess, register_name
from .location import EvalContext, NotAvailable, evaluate_value

__all__ = ["CallFrameTable", "FrameState", "RegisterFile", "Unwinder", "is_exc_return"]

SP, LR, PC = 13, 14, 15
S0 = 64
CALLEE_SAVED = frozenset([4, 5, 6, 7, 8, 9, 10, 11, SP, LR] + [S0 + n for n in range(16, 32)])
FPCCR = 0xE000EF34
FPCCR_LSPACT = 1 << 0
XPSR_ALIGNED = 1 << 9             # 例外の入口で SP を 8 バイトに揃えるため 4 バイト詰めた
MAX_FRAMES = 64


def is_exc_return(value: int) -> bool:
    """EXC_RETURN（例外から戻る特別な LR の値）か。v7-M: 0xFFFFFFE1/E9/ED/F1/F9/FD、v8-M も bit31〜7 は 1、bit1 は 0。"""
    return value & 0xFFFFFF80 == 0xFFFFFF80 and value & 0x2 == 0


class RegisterFile:
    """1 つのフレームのレジスタ（DWARF の番号 → 値）。

    known: このフレームで分かった値。parent: callee-saved を受け継ぐ元（呼ばれた側のフレーム）。
    access: フレーム 0 だけ（ターゲットから必要なときに読む）。special: msp / psp / xpsr。
    """

    def __init__(self, known: dict[int, int] | None = None, parent: RegisterFile | None = None,
                 inherit: frozenset[int] = CALLEE_SAVED, access: TargetAccess | None = None,
                 special: dict[str, int | None] | None = None):
        self.known = dict(known or {})
        self.parent = parent
        self.inherit = inherit
        self.access = access
        self._special = dict(special or {})

    def read(self, number: int) -> int:
        if number in self.known:
            return self.known[number]
        if 256 <= number < 288:                                   # d*n* = s*2n*（下位）:s*2n+1*（上位）
            k = number - 256
            return self.read(S0 + 2 * k) | self.read(S0 + 2 * k + 1) << 32
        if self.access is not None:
            try:
                value = self.access.read_register(register_name(number))
            except AccessError as e:
                raise NotAvailable(f"レジスタ {register_name(number)} が読めない（{e}）") from None
            self.known[number] = value
            return value
        if self.parent is not None and number in self.inherit:
            return self.parent.read(number)
        raise NotAvailable(f"レジスタ {register_name(number)} はこのフレームでは復元できない")

    def special(self, name: str) -> int | None:
        """msp / psp / xpsr（分からなければ None）。"""
        if name in self._special:
            return self._special[name]
        if self.access is not None:
            try:
                value = self.access.read_register(name)
            except AccessError:
                value = None
            self._special[name] = value
            return value
        if self.parent is not None and name != "xpsr":
            return self.parent.special(name)
        return None

    def specials(self) -> dict[str, int | None]:
        return {name: self.special(name) for name in ("msp", "psp")}

    def try_read(self, number: int) -> int | None:
        try:
            return self.read(number)
        except NotAvailable:
            return None


@dataclass
class FrameState:
    index: int
    pc: int                       # そのフレームの PC（呼び出し元では戻り先、例外で中断された処理では正確な番地）
    lookup_pc: int                # 関数・行・変数を引く番地（戻り先なら −1 = 呼び出し命令の中）
    cfa: int | None               # このフレームの CFA（呼び出し元の sp）。分からなければ None
    registers: RegisterFile = field(repr=False)
    note: str = ""                # "exception" = 例外の入口で積まれたフレームから復元した、"lr_without_cfi" = 最内の
                                  # 関数に CFI が無く、葉と仮定して LR から呼び出し元を出した（assume_leaf のときだけ）
    stop: dict | None = None      # 最後のフレームだけ: その先へ巻き戻さなかった理由（Unwinder.unwind の説明）


@dataclass(frozen=True)
class _Entry:
    start: int
    end: int
    fde: FDE


class CallFrameTable:
    """FDE を番地で引き、その番地の行（CFA の規則・各レジスタの規則）を返す。"""

    def __init__(self, entries):
        fdes = [e for e in entries if isinstance(e, FDE)]
        self._entries = sorted((_Entry(f["initial_location"], f["initial_location"] + f["address_range"], f)
                                for f in fdes), key=lambda e: e.start)
        self._starts = [e.start for e in self._entries]
        self._reach: list[int] = []                       # 先頭からそこまでの終わりの番地の最大（find の打ち切り）
        for e in self._entries:
            self._reach.append(max(e.end, self._reach[-1]) if self._reach else e.end)
        self._decoded: dict[int, list] = {}
        self._parsers: dict[int, DWARFExprParser] = {}

    @classmethod
    def from_dwarf(cls, dwarf) -> CallFrameTable:
        if dwarf.has_CFI():
            return cls(dwarf.CFI_entries())
        if dwarf.has_EH_CFI():
            return cls(dwarf.EH_CFI_entries())
        return cls([])

    def __len__(self) -> int:
        return len(self._entries)

    def find(self, pc: int) -> _Entry | None:
        """pc を含む FDE。重なっていれば開始番地の小さいほう（開始番地の順に先頭から探したときと同じ）。"""
        found = None
        j = bisect.bisect_right(self._starts, pc) - 1    # 開始番地が pc 以下の最後
        while j >= 0 and self._reach[j] > pc:            # それより前に pc まで届くものが無ければ打ち切る
            if pc < self._entries[j].end:
                found = self._entries[j]
            j -= 1
        return found

    def row_at(self, pc: int) -> tuple[dict, int, DWARFExprParser] | None:
        """(行, 戻り先のレジスタ番号, 式の字句解析器)。行は {"cfa": CFARule, 番号: RegisterRule}。"""
        entry = self.find(pc)
        if entry is None:
            return None
        table = self._decoded.get(entry.start)
        if table is None:
            table = self._decoded[entry.start] = entry.fde.get_decoded().table
        row = None
        for line in table:
            if line["pc"] <= pc:
                row = line
            else:
                break
        if row is None:
            return None
        cie = entry.fde.cie
        parser = self._parsers.get(id(cie))
        if parser is None:
            parser = self._parsers[id(cie)] = DWARFExprParser(entry.fde.structs)
        return row, cie["return_address_register"], parser


class Unwinder:
    """assume_leaf: CFI の無い最内の関数（フレーム 0）を「葉（何も積んでいない）」と仮定し、LR を戻り先とする。
    仮定なので使う側が明示したときだけ使い、出したフレームの note を "lr_without_cfi" にする（関数が LR を積んで
    いれば偽の呼び出し元になる）。既定は仮定せず、そのフレームで止めて stop に "no_cfi" を書く。"""

    def __init__(self, table: CallFrameTable, max_frames: int = MAX_FRAMES, assume_leaf: bool = False):
        self.table = table
        self.max_frames = max_frames
        self.assume_leaf = assume_leaf

    def unwind(self, access: TargetAccess) -> list[FrameState]:
        """フレーム 0（いまの PC）から、呼び出し元へ巻き戻せるところまで。最後のフレームの stop に止めた理由:
        - {"reason": "max_frames", "max_frames": N}: 上限の段数に達した（その先は辿っていない）
        - {"reason": "no_cfi", "pc": 番地}: その番地に CFI（呼び出し元の求め方）が無い
        - {"reason": "not_available", "detail": 文}: 要るレジスタかスタックが読めない
        - {"reason": "return_address", "value": 値}: 戻り先が 0 か 0xFFFFFFFF（リセット直後の LR 等。呼び出し元が無い）
        - {"reason": "cfa_below_sp", "cfa": 値, "sp": 値}: 呼び出し元の SP（CFA）がいまの SP より下（スタックの向きと逆）
        - {"reason": "pc_zero"}: 呼び出し元の PC が 0
        - {"reason": "repeated_frame", "pc": 値, "sp": 値}: 前に出したフレームと同じ (pc, sp) に戻った"""
        registers = RegisterFile(access=access)
        try:
            pc = registers.read(PC)
        except NotAvailable:
            return []
        frames = [FrameState(0, pc, pc, None, registers)]
        seen = {(pc, registers.try_read(SP))}
        while True:
            if len(frames) >= self.max_frames:
                frames[-1].stop = {"reason": "max_frames", "max_frames": self.max_frames}
                break
            caller, stop = self._step(frames[-1], access)
            if caller is None:
                frames[-1].stop = stop
                break
            key = (caller.pc, caller.registers.try_read(SP))
            if caller.pc == 0:
                frames[-1].stop = {"reason": "pc_zero"}
                break
            if key in seen:
                frames[-1].stop = {"reason": "repeated_frame", "pc": key[0], "sp": key[1]}
                break
            seen.add(key)
            frames.append(caller)
        return frames

    # --- 1 段 ----------------------------------------------------------------------------

    def _step(self, frame: FrameState, access: TargetAccess) -> tuple[FrameState | None, dict | None]:
        """(呼び出し元のフレーム, None) か、巻き戻せないなら (None, 止めた理由)。"""
        regs = frame.registers
        found = self.table.row_at(frame.lookup_pc)
        note = ""
        try:
            if found is None:
                if frame.index != 0 or not self.assume_leaf:
                    return None, {"reason": "no_cfi", "pc": frame.lookup_pc}
                cfa = regs.read(SP)                       # 葉と仮定する（使う側が明示した）: 結果のフレームに書く
                restored = RegisterFile({SP: cfa, LR: regs.read(LR)}, parent=regs)
                return_register = LR
                note = "lr_without_cfi"
            else:
                row, return_register, parser = found
                cfa = self._cfa(row["cfa"], regs, access, parser)
                restored = self._restore(row, regs, cfa, access, parser)
            frame.cfa = cfa
            return_address = restored.read(return_register)
        except NotAvailable as e:
            return None, {"reason": "not_available", "detail": str(e)}
        if return_address == 0xFFFFFFFF or return_address == 0:
            return None, {"reason": "return_address", "value": return_address}
        if is_exc_return(return_address):
            return self._exception(frame, restored, cfa, return_address, access)
        sp = regs.try_read(SP)
        if cfa < (sp or 0):
            return None, {"reason": "cfa_below_sp", "cfa": cfa, "sp": sp}
        pc = return_address & ~1
        restored.known[PC] = pc
        return FrameState(frame.index + 1, pc, pc - 1, None, restored, note), None

    @staticmethod
    def _context(regs: RegisterFile, access: TargetAccess, cfa: int | None = None) -> EvalContext:
        return EvalContext(access.read_memory, regs.read, cfa=cfa)

    def _cfa(self, rule: CFARule, regs: RegisterFile, access: TargetAccess, parser) -> int:
        if rule.expr is not None:
            return evaluate_value(parser.parse_expr(bytes(rule.expr)), self._context(regs, access))
        if rule.reg is None:
            raise NotAvailable("CFA の規則が無い")
        return (regs.read(rule.reg) + rule.offset) & 0xFFFFFFFF

    def _restore(self, row: dict, regs: RegisterFile, cfa: int, access: TargetAccess, parser) -> RegisterFile:
        known: dict[int, int] = {SP: cfa}
        lost: set[int] = set()
        for number, rule in row.items():
            if not isinstance(number, int):
                continue
            if rule.type == RegisterRule.UNDEFINED:
                lost.add(number)
                continue
            if rule.type == RegisterRule.SAME_VALUE:
                value = regs.try_read(number)
                if value is not None:
                    known[number] = value
                continue
            width = 8 if 256 <= number < 288 else 4
            if rule.type == RegisterRule.OFFSET:
                value = int.from_bytes(self._memory(access, cfa + rule.arg, width), "little")
            elif rule.type == RegisterRule.VAL_OFFSET:
                value = (cfa + rule.arg) & 0xFFFFFFFF
            elif rule.type == RegisterRule.REGISTER:
                value = regs.read(rule.arg)
            elif rule.type in (RegisterRule.EXPRESSION, RegisterRule.VAL_EXPRESSION):
                address = evaluate_value(parser.parse_expr(bytes(rule.arg)), self._context(regs, access), [cfa])
                value = address if rule.type == RegisterRule.VAL_EXPRESSION else \
                    int.from_bytes(self._memory(access, address, width), "little")
            else:
                lost.add(number)
                continue
            if width == 8:                                # d レジスタの退避（vpush）は s の 2 つ
                k = number - 256
                known[S0 + 2 * k], known[S0 + 2 * k + 1] = value & 0xFFFFFFFF, value >> 32
            else:
                known[number] = value
        return RegisterFile(known, parent=regs, inherit=CALLEE_SAVED - lost)

    @staticmethod
    def _memory(access: TargetAccess, address: int, length: int) -> bytes:
        try:
            return access.read_memory(address & 0xFFFFFFFF, length)
        except AccessError as e:
            raise NotAvailable(f"スタック 0x{address & 0xFFFFFFFF:08X} が読めない（{e}）") from None

    def _exception(self, frame: FrameState, restored: RegisterFile, cfa: int, exc_return: int,
                   access: TargetAccess) -> tuple[FrameState | None, dict | None]:
        """例外の入口で積まれたフレームから、中断された処理のレジスタを復元する。"""
        on_psp = bool(exc_return & 0x4)
        specials = restored.specials()
        stack = specials["psp"] if on_psp else cfa        # MSP: ハンドラの CFA = 例外の入口の MSP
        if stack is None:
            return None, {"reason": "not_available", "detail": "psp が読めない（例外のフレームの場所）"}
        try:
            words = self._memory(access, stack, 32)
        except NotAvailable as e:
            return None, {"reason": "not_available", "detail": str(e)}
        r0, r1, r2, r3, r12, lr, pc, xpsr = (int.from_bytes(words[i:i + 4], "little") for i in range(0, 32, 4))
        known = {0: r0, 1: r1, 2: r2, 3: r3, 12: r12, LR: lr, PC: pc & ~1}
        size = 0x20
        if not exc_return & 0x10:                         # FP の拡張フレーム: s0〜s15・FPSCR・予約
            size = 0x68
            known.update(self._stacked_fp(access, stack))
        if xpsr & XPSR_ALIGNED:
            size += 4
        known[SP] = (stack + size) & 0xFFFFFFFF
        special = {"msp": cfa if on_psp else known[SP], "psp": known[SP] if on_psp else specials["psp"],
                   "xpsr": xpsr}
        registers = RegisterFile(known, parent=restored, inherit=CALLEE_SAVED - {SP, LR}, special=special)
        frame.cfa = cfa
        return FrameState(frame.index + 1, pc & ~1, pc & ~1, None, registers, "exception"), None

    def _stacked_fp(self, access: TargetAccess, stack: int) -> dict[int, int]:
        """FP の拡張フレームの s0〜s15。遅延退避がまだ（FPCCR.LSPACT = 1）・FPCCR が読めないなら分からないとする。"""
        try:
            fpccr = int.from_bytes(access.read_memory(FPCCR, 4), "little")
            if fpccr & FPCCR_LSPACT:
                return {}
            data = access.read_memory(stack + 0x20, 64)
        except AccessError:
            return {}
        return {S0 + n: int.from_bytes(data[4 * n:4 * n + 4], "little") for n in range(16)}
