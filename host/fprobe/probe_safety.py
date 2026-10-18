"""Probe の道具: 止めると壊れるターゲットの安全（止める前の見分けと、止まらずに壊れた状態の見張り）。

Probe（fprobe.mcp_server）に混ぜて使う mixin。Probe の _lock・target・_symbols・_monitor を使う。

- halt_risk: ELF だけで、コアを止めると壊れうるか（無線・USB・ウォッチドッグ・RTOS。fprobe.halt_risk）と、ELF に
  止めずにデバッグするモニタ（target.debug_mode の monitor）があるか。機器には触らない。
- 致命的エラーの見張り: target_status が走っているコアを見るたびに、止めずに読める材料（DHCSR のロックアップ、
  DWT_PCSR の PC の見本、RTOS・ランタイムのティックの変数）を fprobe.liveness に渡し、致命的エラーで回り続けて
  いれば状態に fatal を付ける（デーモンの見張りが 1 秒ごとに読むので、通知 target.state で画面・AI に届く）。
  PCSR は DEMCR.TRCENA が要るので、無ければ立てる（デバッグの登録簿だけ。ターゲットの動きは変えない）。
"""

import time

from .halt_risk import assess
from .liveness import TICK_VARIABLES, Liveness
from .profile.dwt import DEMCR, DEMCR_TRCENA, DWT_PCSR

__all__ = ["DHCSR", "PC_SAMPLES", "SafetyTools"]

DHCSR = 0xE000EDF0
S_LOCKUP = 1 << 19
PC_SAMPLES = 4                         # 見張りの 1 回に読む PC の見本の数
NOT_CORTEX_M = ("riscv", "armv7-a", "armv6")   # pyOCD を使わない接続の debug_architecture（DHCSR・DWT が無い）


class SafetyTools:
    def _init_safety(self) -> None:
        self._liveness = Liveness(self._function_name)
        self._tick_symbols: tuple[object, int | None] | None = None      # (ELF, ティックの変数の番地)

    # --- 止める前の見分け --------------------------------------------------------------------------

    def halt_risk(self) -> dict:
        """コアを止める操作（中断・ブレークポイント・ステップ・関数の呼び出し）で壊れうるか（ELF だけ。機器には
        触らない）: risky、reasons（重い順。kind = radio / usb / watchdog / rtos、count、symbols = 手がかりの
        シンボルの先頭、rtos = RTOS の名前）、monitor_in_elf（止めずにデバッグするモニタがあるか。あれば
        target_debug_mode_set の mode="monitor" へ）。"""
        symbols = self._symbols
        if symbols is None:
            raise RuntimeError("ELF が読み込まれていない（debug_elf_load を先に）")
        return {"elf": str(symbols.path), **assess(symbols.names())}

    # --- 致命的エラーの見張り -----------------------------------------------------------------------

    def _function_name(self, pc: int) -> str | None:
        f = self._symbols.function_at(pc) if self._symbols is not None else None
        return f.name if f is not None else None

    def _tick_address(self) -> int | None:
        """ELF のティックの変数の番地（無ければ None。ELF ごとに 1 回引く）。"""
        symbols = self._symbols
        if symbols is None:
            return None
        if self._tick_symbols is None or self._tick_symbols[0] is not symbols:
            address = None
            for name in TICK_VARIABLES:
                try:
                    address = symbols.variable(name).address
                    break
                except KeyError:
                    continue
            self._tick_symbols = (symbols, address)
        return self._tick_symbols[1]

    def _fatal_state(self, state: str) -> dict | None:
        """走っているコアが致命的エラーで回り続けていれば、その様子（_lock の中で。target_status から）。読めなければ
        None（見張りは状態の読みを妨げない）。"""
        if state not in ("RUNNING", "LOCKUP") or getattr(self, "_monitor", None) is not None \
                or getattr(self.target, "debug_architecture", None) in NOT_CORTEX_M:     # DHCSR・DWT は Cortex-M だけ
            self._liveness.reset()
            return None
        try:
            target = self.target
            lockup = state == "LOCKUP" or bool(target.read_memory(DHCSR) & S_LOCKUP)
            pcs: list[int | None] = []
            if not lockup:
                demcr = target.read_memory(DEMCR)
                if not demcr & DEMCR_TRCENA:
                    target.write_memory(DEMCR, demcr | DEMCR_TRCENA)
                pcs = [target.read_memory(DWT_PCSR) for _ in range(PC_SAMPLES)]
            tick_address = self._tick_address()
            tick = target.read_memory(tick_address) if tick_address is not None and not lockup else None
        except Exception:                                                  # 読めない（切れかけ等）: 決めない
            return None
        return self._liveness.observe(lockup, pcs, tick, time.time())
