"""`HaltSession` の部品: `debug_*`（API の linux.halt.debug.*）。gdb を通さずに AI が止めて調べる持ち主 `HaltDebugger` を開き、
その操作を呼ぶだけの薄い層（設計: docs/superpowers/specs/2026-11-03-linux-halt-ai-tools-design.md）。
"""
from __future__ import annotations

from .debugger import HaltDebugger
from .engine import EngineError
from .halt_facts import journal_facts
from .image import ElfImage


class HaltDebugMixin:
    """linux.halt.debug の操作（`HaltSession` の状態 `cores`・`_debug`・`_prepare_engine` 等を使う）。"""

    def _debugger(self) -> HaltDebugger:
        if self.closed:
            raise EngineError("the session is closed")
        if self._debug is None:
            raise EngineError("linux.halt.debug is not attached (linux.halt.debug.attach first)")
        return self._debug

    def _debug_summary(self) -> dict:
        d = self._debug
        return {"pid": d.proc.tgid, "comm": d.proc.comm, "closed": d.closed, "running": d.engine.running,
                "breakpoints": len(d.breakpoints)}

    def debug_attach(self, *, pid: int, elf: str, skew_limit_ns: int, thread_id: str, kernel_isa: str,
                     max_step_retries: int, bp_unmapped: str, max_halt_s: float,
                     libraries: list[dict] | None = None) -> dict:
        """プロセス pid を gdb を通さずに止めて調べる持ち主を開く: 全コアを止めてプロセスを読み、前のデバッガが残した BKPT を
        戻し、ELF（PC の上のパス）の読み込み位置を補助ベクタから求め、全コアを**止めたまま**返す。max_halt_s 秒、呼び出しが
        無いまま止めたままなら、見張りが全コアを走らせる。"""
        self._not_busy("linux.halt.debug.attach")
        self._check_engine_choices(thread_id, kernel_isa, bp_unmapped)
        if not (isinstance(max_halt_s, (int, float)) and max_halt_s > 0):
            raise EngineError("max_halt_s must be a number of seconds > 0 (got %r)" % (max_halt_s,))
        if not (isinstance(skew_limit_ns, int) and skew_limit_ns >= 0):
            raise EngineError("skew_limit_ns must be an integer >= 0 (got %r)" % (skew_limit_ns,))
        if not (isinstance(max_step_retries, int) and max_step_retries >= 1):
            raise EngineError("max_step_retries must be an integer >= 1 (got %r)" % (max_step_retries,))
        try:
            image = ElfImage(elf)                              # コアに触る前に ELF を確かめる
        except (OSError, ValueError) as e:
            raise EngineError("elf %r: %s" % (elf, e)) from None
        self._ensure_halting_debug()
        journal = journal_facts(self.options.journal)

        def prepare():
            return self._prepare_engine(pid=pid, skew_limit_ns=skew_limit_ns, thread_id=thread_id,
                                        kernel_isa=kernel_isa, max_step_retries=max_step_retries,
                                        bp_unmapped=bp_unmapped)
        if all(self.cores.halted()):
            (proc, result, engine), stop = prepare(), None
        else:
            (proc, result, engine), stop, _ = self._reading(prepare)
        try:
            attach = engine.attach()                          # 全コアを止める（止めたまま返す）
            debugger = HaltDebugger(engine, image, skew_limit_ns=skew_limit_ns, max_halt_s=float(max_halt_s),
                                    lock=self._device_lock, libraries=libraries,
                                    attach_stop=attach, cleanup=self._cleanup_reconnecting)
        except BaseException:
            self._cleanup_reconnecting(engine)                # 途中で失敗: BKPT・HW の bp を外して全コアを走らせる
            raise
        self._debug = debugger
        return {**debugger.facts(), "threads": debugger.threads_brief(),
                "recovered": {"restored": result.restored, "stale": result.stale, "journal_before": journal},
                "stop_for_reading": stop}

    def debug_status(self) -> dict:
        return self._debugger().facts()

    def debug_break_insert(self, *, location: str, kind: str) -> dict:
        return self._debugger().break_insert(location, kind)

    def debug_break_remove(self, *, id: int) -> dict:  # noqa: A002 - API の引数名
        return self._debugger().break_remove(id)

    def debug_run(self, *, wait_s: float) -> dict:
        return self._debugger().run(wait_s)

    def debug_halt(self) -> dict:
        return self._debugger().halt()

    def debug_threads(self, *, variables: bool, expressions: list, max_elements: int) -> dict:
        return self._debugger().threads(variables, list(expressions), max_elements)

    def debug_read(self, *, expressions: list, tid: int, frame: int, max_elements: int) -> dict:
        return self._debugger().read(list(expressions), tid, frame, max_elements)

    def debug_memory(self, *, address: int, length: int) -> dict:
        return self._debugger().memory(address, length)

    def debug_step(self, *, tid: int, mode: str, max_instructions: int, timeout_s: float) -> dict:
        return self._debugger().step(tid, mode, max_instructions, timeout_s)

    def debug_select(self, *, tid: int) -> dict:
        return self._debugger().select_thread(tid)

    def debug_close(self) -> dict:
        return self._debugger().close()
