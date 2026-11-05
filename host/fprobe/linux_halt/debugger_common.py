"""`HaltDebugger` の部品が共有する定数と、停止の報告の形、Inspector の読み手。

レジスタ名・run が錠を放す間隔・ステップの種類・32 ビットのマスク。`stop_report` は Engine の StopResult を応答の形にし、
`_ThreadAccess` は DWARF の Inspector に 1 つのスレッドのレジスタとプロセスの仮想メモリを見せる。
"""
from __future__ import annotations

from ..dwarf import AccessError
from .mmu import Unmapped

REG_NAMES = [f"r{i}" for i in range(13)] + ["sp", "lr", "pc", "cpsr"]
RUN_SLICE_S = 0.05      # run が錠（_cv）を放す間隔: 待っている間も状態の読み・止める要求・見張りの poll を通す
STEP_MODES = ("into", "over")
MASK = 0xFFFFFFFF
# Linux（ARM）のシグナル番号と名前（静的な事実の表。応答にはカーネルが知らせた番号を載せ、名前はこの表から引く）
SIGNAL_NAMES = {1: "SIGHUP", 2: "SIGINT", 3: "SIGQUIT", 4: "SIGILL", 5: "SIGTRAP", 6: "SIGABRT", 7: "SIGBUS", 8: "SIGFPE",
                9: "SIGKILL", 10: "SIGUSR1", 11: "SIGSEGV", 12: "SIGUSR2", 13: "SIGPIPE", 14: "SIGALRM", 15: "SIGTERM",
                16: "SIGSTKFLT", 17: "SIGCHLD", 18: "SIGCONT", 19: "SIGSTOP", 20: "SIGTSTP", 21: "SIGTTIN", 22: "SIGTTOU",
                23: "SIGURG", 24: "SIGXCPU", 25: "SIGXFSZ", 26: "SIGVTALRM", 27: "SIGPROF", 28: "SIGWINCH", 29: "SIGIO",
                30: "SIGPWR", 31: "SIGSYS"}


def exit_text(code: int | None) -> str:
    """カーネルの終了コード（(終了状態 << 8) | 終了させたシグナル。0 ならシグナルなし）の読み方を事実として言う。"""
    if code is None:
        return ""
    return f", code {code} = exit status {code >> 8}" + (f", killed by signal {code & 0x7F}" if code & 0x7F else "")


def exited_message(pid: int | None, kind: str | None, code: int | None) -> str:
    """プロセスの終了を見た後の断りの文（最初に見た操作でも、以後の操作でも同じ文）。終了コードの読み方と、次にすることを添える。"""
    who = "プロセス" if pid is None else f"プロセス {pid}"
    return (f"{who} は終了している（{kind or 'exit'}{exit_text(code)}）。code はカーネルの終了コード = (終了状態 << 8) | 終了させたシグナル。"
            "linux.halt.debug.close して、新しいプロセスに attach し直す")


def call_made(prev_pc: int, width: int, lr: int) -> bool:
    """直前に実行した命令（prev_pc の width バイト）が呼び出し（BL / BLX）だったか。呼び出しは LR を「次の命令の番地」にする
    （Thumb は bit 0 = 1 を付ける）。戻り（BX LR / POP {pc}）・分岐は LR をそうしない。命令セットの事実だけで決め、推測しない。"""
    return (lr & ~1) == (prev_pc & ~1) + width


def stop_report(result, skew_limit_ns: int) -> dict | None:
    """Engine の StopResult → 応答の形（バックエンドの事実そのままと、skew_limit_ns との比較。gdb_status の last_stop と同じ形）。"""
    if result is None:
        return None
    r = result.report
    return {"method": r.method, "cores_halted": list(r.cores_halted), "skew_upper_bound_ns": r.skew_upper_bound_ns,
            "basis": r.basis, "assumptions": list(r.assumptions),
            "bound_without_assumptions_ns": r.bound_without_assumptions_ns, "skew_verdict": result.skew_verdict,
            "skew_limit_ns": skew_limit_ns}


class _ThreadAccess:
    """Inspector の読み手: 1 つのスレッドのレジスタ（止まった時点の値）と、プロセスの仮想メモリ。"""

    def __init__(self, regs: list[int], mem):
        self._regs, self._mem = regs, mem

    def read_memory(self, address: int, length: int) -> bytes:
        try:
            return self._mem.read(address & MASK, length)
        except Unmapped as e:
            raise AccessError(str(e)) from None

    def write_memory(self, address: int, data: bytes) -> None:
        try:
            self._mem.write(address & MASK, data)
        except Unmapped as e:
            raise AccessError(str(e)) from None

    def read_register(self, name: str) -> int:
        name = {"r13": "sp", "r14": "lr", "r15": "pc"}.get(name.lower(), name.lower())
        if name in REG_NAMES[:16]:
            return self._regs[REG_NAMES.index(name)]
        raise AccessError(f"register {name} is not read for a Linux user thread")
