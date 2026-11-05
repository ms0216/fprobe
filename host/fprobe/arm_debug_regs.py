"""ArmDebugCore のレジスタ操作（r0〜r14・PC・CPSR の読み書き、CP15 とバンクの SP、特権モードへの一時切り替え）の部品（U32）。

ArmDebugCore（fprobe.arm_debug）に mixin される。覚えたレジスタ（_cache・_dirty）と命令の列の実行（_run）は本体が持つ。
"""

import contextlib

from .arm_debug_defs import (
    CP15, CP15_READABLE, MODE_MASK, MODE_SYS, MODE_USR, WORD_MASK, _from_dtr, _to_dtr
)


class _RegistersMixin:
    """レジスタ・CP15・バンクの SP の読み書き。"""

    def _get(self, rt: int) -> int:
        return self._run([("read", _to_dtr(rt))])[0]

    def _put(self, rt: int, value: int) -> None:
        self._run([("put", value, _from_dtr(rt))])

    def _set_r0(self, value: int) -> None:
        self._put(0, value)

    def _set_cached(self, name: str, value: int) -> None:
        self._cache[name] = value & WORD_MASK
        self._dirty.add(name)

    # --- レジスタ --------------------------------------------------------------------------------------

    @staticmethod
    def register_number(name: str) -> int | str:
        key = name.strip().lower()
        if key in ("pc", "r15"):
            return "pc"
        if key == "cpsr":
            return "cpsr"
        aliases = {"sp": 13, "lr": 14, "fp": 11, "ip": 12, "sb": 9, "sl": 10}
        if key in aliases:
            return aliases[key]
        if key.startswith("r") and key[1:].isdigit() and 0 <= int(key[1:]) <= 14:
            return int(key[1:])
        raise ValueError(f"レジスタ {name!r} を知らない（r0〜r15・sp・lr・pc・cpsr。今のモードのもの。"
                         "他のモードのバンクのレジスタ・SPSR・CP15 は未対応）")

    def read_register(self, name: str) -> int:
        return self.read_registers([name])[0]

    def read_registers(self, names: list[str]) -> list[int]:
        """レジスタをまとめて読む（r0・r1・PC・CPSR は覚えた値、他は 1 つの列 = ARMv7 なら 1 回の往復）。"""
        numbers = [self.register_number(n) for n in names]
        cache = self._require_halted()
        direct = [n for n in numbers if isinstance(n, int) and n > 1]
        values = dict(zip(direct, self._run([("read", _to_dtr(n)) for n in direct]))) if direct else {}
        return [cache[n] if n in ("pc", "cpsr") else cache[f"r{n}"] if n in (0, 1) else values[n] for n in numbers]

    def write_register(self, name: str, value: int) -> None:
        self.write_registers([(name, value)])

    def write_registers(self, pairs: list[tuple[str, int]]) -> None:
        """レジスタをまとめて書く（r0・r1・PC・CPSR は覚えて再開の前に戻す。他は 1 つの列 = ARMv7 なら 1 回の往復。列は各レジスタに
        決まった値を入れるだけなので、頭からやり直せる。r0 は借りているので壊してよい）。"""
        self._require_halted()
        steps = []
        for name, value in pairs:
            number = self.register_number(name)
            if number in ("pc", "cpsr"):
                self._set_cached(number, value)
            elif number in (0, 1):
                self._set_cached(f"r{number}", value)
            else:
                steps.append(("put", value, _from_dtr(number)))
        if steps:
            self._run(steps)

    def _read_cp15(self, name: str) -> int:
        return self._run(self._cp15_steps(name))[0]

    @staticmethod
    def _cp15_steps(name: str) -> list[tuple]:
        return [("exec", CP15[name]), ("read", _to_dtr(0))]

    @contextlib.contextmanager
    def privileged(self):
        """止めたコアが USR モードなら、中の間だけ SYS モードにする（レジスタは USR と共通で、バンクの違いが出ない）。USR モードの
        ままだと、カーネルのメモリへの LDR/STR はアボートし、キャッシュ操作（CP15）は未定義命令になる（OpenOCD の cortex_a も
        メモリアクセスの前に同じことをする）。出るときに CPSR を元に戻す。"""
        cache = self._require_halted()
        cpsr = cache["cpsr"]
        if cpsr & MODE_MASK != MODE_USR:
            yield
            return
        self._dirty.add("r0")
        self._run(self._mode_steps((cpsr & ~MODE_MASK) | MODE_SYS))
        try:
            yield
        finally:
            self._run(self._mode_steps(cpsr))

    def _state_steps(self, cp15: tuple[str, ...], sp_modes: tuple[int, ...]) -> list[tuple]:
        """CP15 のレジスタ（cp15 の順）とモードごとの SP（sp_modes の順）を読む列。USR モードなら CP15 は SYS モードで読む。
        モードを切り替える列は、頭で元のモード（USR なら SYS）を書き直し、最後に元の CPSR に戻す: 途中まで実行された後に頭から
        やり直しても、同じモードで同じレジスタを読む。"""
        cpsr = self._cache["cpsr"]
        mode = cpsr & MODE_MASK
        home = (cpsr & ~MODE_MASK) | MODE_SYS if mode == MODE_USR else cpsr
        switches = mode == MODE_USR and cp15 or any(m != mode and not (mode == MODE_USR and m == MODE_SYS)
                                                     for m in sp_modes)
        steps = self._mode_steps(home) if switches else []
        for name in cp15:
            steps += self._cp15_steps(name)
        for m in sp_modes:
            if m == mode or (mode == MODE_USR and m == MODE_SYS) or (mode == MODE_SYS and m == MODE_USR):
                steps.append(("read", _to_dtr(13)))          # USR と SYS は SP が共通
            else:
                steps += [*self._mode_steps((cpsr & ~MODE_MASK) | m), ("read", _to_dtr(13)), *self._mode_steps(home)]
        if switches:
            steps += self._mode_steps(cpsr)
        return steps

    def read_cp15(self, name: str) -> int:
        """CP15 のレジスタ（ttbr0 / ttbr1 / ttbcr / contextidr / sctlr）を、止めたコアに読ませる。借りた r0 は再開の前に戻る。"""
        if name not in CP15_READABLE:
            raise ValueError(f"CP15 のレジスタ {name!r} は読めない（{', '.join(CP15_READABLE)}）")
        return self.read_state((name,), ())[0]

    def read_banked_sp(self, mode: int) -> int:
        """モード（0x10 USR・0x11 FIQ・0x12 IRQ・0x13 SVC・0x17 ABT・0x1B UND・0x1F SYS）の SP。今のモードならそのまま読む。
        別のモードは、CPSR のモードを書き替えて読み、すぐ元の CPSR に戻す（OpenOCD の arm_dpm と同じ手順）。"""
        return self.read_state((), (mode,))[0]
