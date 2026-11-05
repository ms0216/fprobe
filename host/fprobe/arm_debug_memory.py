"""ArmDebugCore のメモリ操作（コアに LDR / STR を実行させて読み書き。ARMv7 の DCC 速いモードでの読み）の部品（U32）。

ArmDebugCore（fprobe.arm_debug）に mixin される。書いた範囲のキャッシュ揃え（_sync_caches）は arm_debug_cache。
"""

from .arm_debug_defs import (
    DSCR_EXTDCC, DSCR_EXTDCC_FAST, DSCR_HALTED, DSCR_INSTRCOMPL, DSCR_MOE_SHIFT, DSCR_RESTARTED, DSCR_RXFULL,
    DSCR_STICKY, DSCR_TXFULL, DSCR_UND, LDC_DTRTX, LOAD_STORE, ArmDebugError, _FAST_READ_ROUND_TRIPS, _to_dtr
)


class _MemoryMixin:
    """メモリの読み書き。"""

    def _check_abort(self, dscr: int | None, address: int, what: str) -> None:
        if dscr is None or not dscr & DSCR_STICKY:
            return
        self.port.clear_sticky()
        if dscr & DSCR_UND:
            raise ArmDebugError(f"0x{address:08X} の{what}: コアが命令を未定義として扱った（DSCR 0x{dscr:08X}）")
        raise ArmDebugError(f"0x{address:08X} の{what}でアボート（番地はコアの見る番地 = MMU が有効なら仮想番地。"
                            f"変換が無い・アクセス権が無い・存在しない番地。DSCR 0x{dscr:08X}）")

    def read_memory(self, address: int, count: int, size: int = 4) -> list[int]:
        """size（1 / 2 / 4）バイトの値を count 個、コアに読ませる。"""
        load = LOAD_STORE[size][0]
        self._require_halted()
        self._dirty |= {"r0", "r1"}
        self._set_r0(address)
        out = []
        pair = getattr(self.port, "execute_once_and_read", None)   # 読みの命令と受け取りを 1 回の往復で（ARMv7）
        for i in range(count):
            here = address + i * size
            if pair is not None:
                dscr, value = pair(load, _to_dtr(1))
                self._check_abort(dscr, here, "読み")
                out.append(value)
                continue
            self._check_abort(self.port.execute(load), here, "読み")
            try:
                out.append(self._get(1))
            except ArmDebugError:
                self._check_abort(self.port.dscr(), here, "読み")
                raise
        # 最後の命令の後の DSCR（まとめた道は、その往復で最後の命令の後に読んだもの。読み直しと同じ時点）
        last = self.port.last_dscrs[-1] if pair is not None and getattr(self.port, "last_dscrs", None) else self.port.dscr()
        self._check_abort(last, address, "読み")
        return out

    def read_memory_fast(self, address: int, count: int) -> list[int]:
        """32 ビットの語を count 個、ARMv7 の DCC の速いモードで読む（OpenOCD の cortex_a_read_cpu_memory_fast と同じ手順）。
        LDC を ITR に書き、DTRTX を count 回まとめて読む（読むたびにコアが次の語を読む。1 語ごとの往復が無い）。
        最後の 1 語は非ブロックのモードに戻してから読む。ポートがまとめて読めなければ read_memory と同じ。
        往復の数で道を選ぶ: 速いモードは語の数によらず 4 回（番地を置く・速いモードと ITR とまとめた読み・戻すと DSCR・最後の語）、
        1 語ずつの道（ARMv7 の execute_once_and_read）は 1 + 語の数。少ない方（語が 3 未満なら 1 語ずつ）を使う。"""
        block = getattr(self.port, "read_dtrtx_fast", None)
        per_word = getattr(self.port, "execute_once_and_read", None) is not None
        if not self.v7 or block is None or count < 2 or (per_word and 1 + count < _FAST_READ_ROUND_TRIPS):
            return self.read_memory(address, count)
        self._require_halted()
        self._dirty |= {"r0"}
        self._set_r0(address)
        dscr = self.port.last_dscrs[-1]                    # 番地を置いた命令の後の DSCR（設定のビットはこれで足りる）
        normal = dscr & ~DSCR_EXTDCC & ~(DSCR_HALTED | DSCR_RESTARTED | DSCR_STICKY | DSCR_INSTRCOMPL
                                          | DSCR_TXFULL | DSCR_RXFULL | (0xF << DSCR_MOE_SHIFT))
        failed = True
        try:
            # 速いモードでは ITR に書いても実行されず、DTRTX を読むと「今の値を返してから」命令を実行する（実機の Cortex-A7、
            # 2026-10-23）。よって最初の読みは前の値（捨てる）で、count 回の読みで count−1 語、最後の 1 語は下で読む。
            # 速いモードにする DSCR の書きと ITR の書きは 1 回の往復で
            out = block(LDC_DTRTX, count, fast_dscr=normal | DSCR_EXTDCC_FAST)[1:]
            failed = False
        finally:
            dscr = self.port.write_dscr_and_read(normal)    # どの道でも非ブロックのモードに戻す（戻した後の DSCR も同じ往復で）
            if failed:
                self.port.clear_sticky()                    # アボートの印を残したまま、再開の手順の命令を流さない
        if not dscr & DSCR_INSTRCOMPL:
            dscr = self._wait(DSCR_INSTRCOMPL, "速い読みの最後の命令が終わらない")
        self._check_abort(dscr, address, f"{count} 語の速い読み")
        out.append(self.port.from_core(dscr))               # その DSCR に TXfull が見えていれば、待たずに読む
        return out

    def write_memory(self, address: int, values: list[int], size: int = 4) -> None:
        """size バイトの値の列を、コアに書かせる。書いた範囲は命令として実行されても良いようにキャッシュを揃える。"""
        if not values:
            return
        store = LOAD_STORE[size][1]
        self._require_halted()
        self._dirty |= {"r0", "r1"}
        self._set_r0(address)
        mask = (1 << (8 * size)) - 1
        for i, value in enumerate(values):
            self._put(1, value & mask)
            self._check_abort(self.port.execute(store), address + i * size, "書き込み")
        self._check_abort(self.port.dscr(), address, "書き込み")
        self._sync_caches(address, len(values) * size)
