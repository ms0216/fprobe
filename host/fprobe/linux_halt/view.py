"""Linux のプロセスの全コア停止デバッグ（HaltDebugger）を、マイコンのデバッガと同じ API の形で見せる層。

画面（host/ui/src/lib/debugger.ts）はマイコンのデバッガの API（target.status・debug.source.*・debug.breakpoint.*・
debug.backtrace・debug.frame_variables・target.registers.read 等）だけを読む。attach している間、各サービスはその
メソッドの入口でこの層に渡す（名前・引数・応答の形は同じ。仕様 docs/superpowers/specs/2026-11-03-linux-halt-ui-design.md）。
応答は事実だけ（判断・推定を入れない）。合わない欄は仕様の範囲で欠くか、理由の誤りにする。
"""
from __future__ import annotations

import time
from datetime import datetime

from ..symbols import Location
from .debugger import HaltDebugger
from .debugger_common import SIGNAL_NAMES, exited_message
from .engine import EngineError
from .view_breakpoints import ViewBreakpointsMixin
from .view_inspect import ViewInspectMixin
from .view_sources import SOURCE_LIMIT, ViewSourcesMixin, prefetch_libraries, prefetch_sources

__all__ = ["LinuxDebugView", "SOURCE_LIMIT", "prefetch_libraries", "prefetch_sources"]

# 停止の種類 → target.status の halt_reason（同じ事実のときだけ。ほかは欠いて linux.stop_kind に元の種類）
HALT_REASON = {"swbreak": "breakpoint", "hwbreak": "breakpoint", "watch": "watchpoint", "int": "request",
               "step": "request", "attach": "request"}


class LinuxDebugView(ViewInspectMixin, ViewBreakpointsMixin, ViewSourcesMixin):
    """1 つの HaltDebugger をマイコンの API の形で見せる。"""

    def __init__(self, debugger: HaltDebugger, *, breakpoint_kind: str | None, sources: dict[str, dict] | None = None,
                 step_max_instructions: int | None = None, step_timeout_s: float | None = None):
        """sources: attach のときに対象から取り寄せたソース（prefetch_sources の結果）。breakpoint_kind・
        step_max_instructions・step_timeout_s: 画面の道（種類・上限の引数を持たない API）で使う値。attach で使う側が
        明示する（渡さなければその道は断る。既定を持たない）。"""
        if breakpoint_kind not in (None, "sw", "hw"):
            raise EngineError("breakpoint_kind must be 'sw' or 'hw' (got %r)" % (breakpoint_kind,))
        self.d = debugger
        self.breakpoint_kind = breakpoint_kind
        self.step_max_instructions, self.step_timeout_s = step_max_instructions, step_timeout_s
        self._disabled: dict[str, dict] = {}            # 無効にしたブレークポイント: location → 置いたときの事実
        self._view_previous: dict[tuple[int, int], bytes] = {}      # memory_view の compare 用: (番地, 長さ) → 前回の読み
        self.sources = dict(sources or {})
        self.loaded_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
        try:
            stat = debugger.image.path.stat()
            self._elf_stamp: tuple | None = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            self._elf_stamp = None

    # ------------------------------------------------------------------ 状態
    def target_status(self) -> dict:
        """target.status の形（state・止まっていれば今のスレッドの pc と場所）と、linux 欄（事実）。走っていれば、対象の
        止まりがあったかを 1 回だけ確かめる（HaltDebugger.poll。待たない。target.resume の後に止まりを拾う道）。止めた
        ままの時間は数え直さない（デーモンの見張りが周期的に読む）。"""
        self.d.poll()                                    # 走っていれば当たりを 1 回だけ確かめる（待たない）
        return self._status_from(self.d.status_facts())

    def target_status_unqueued(self) -> dict | None:
        """作業スレッドを通さない読み（デーモンの見張り）: 鍵がすぐ取れれば target_status と同じ（poll もする）、取れなければ
        鍵を待たずに最後に分かった事実の写しから（行き詰まらない）。写しがまだ無ければ None。"""
        lock = self.d._cv
        if lock.acquire(blocking=False):
            try:
                return self.target_status()
            finally:
                lock.release()
        facts = self.d.snapshot()
        return None if facts is None else self._status_from(facts)

    def _status_from(self, f: dict) -> dict:
        stop = f["last_stop"]
        stop_kind = stop["kind"] if stop is not None else "attach"
        out: dict = {"state": "RUNNING" if f["running"] else "HALTED", "target": "linux"}
        if not f["running"] and f["pc"] is not None:
            where = f["where"]
            link = self.d._link(f["pc"])
            loc = self.d.symbols.describe(link)
            out["pc"] = f["pc"]
            out["where"] = str(Location(f["pc"], loc.function, loc.offset, loc.file, loc.line)) \
                if loc.function is not None or loc.file else f"0x{f['pc']:08X}"
            if loc.function is None and not loc.file:
                _, _, text = self._in_library(f["pc"])
                if text is not None:
                    out["where"] = text
            for key in ("function", "file", "line"):
                if where[key] is not None:
                    out[key] = where[key]
            if stop_kind in HALT_REASON:
                out["halt_reason"] = HALT_REASON[stop_kind]
        out["linux"] = {"pid": f["pid"], "comm": f["comm"], "tid": f["tid"], "tid_from": f["tid_from"],
                        "thread_comm": next((t["comm"] for t in f["threads"] if t["tid"] == f["tid"]), None),
                        "threads": f["threads"], "stop_kind": None if f["running"] else stop_kind,
                        "registers_from": f["registers_from"], "pc_error": f["pc_error"], "elf": f["elf"],
                        "load_bias": f["load_bias"],
                        "breakpoint_edit_requires_halt": False}  # 走行中の置き外しは、エンジンが全コアを短く止めて行い、また走らせる
        return out

    def read_words(self, addresses: list[int]) -> dict[str, int]:
        """system.status の memory: プロセスの仮想メモリの 32 ビットの語（止まっているときだけ。置いた BKPT は元の命令で）。"""
        def go():
            out = {}
            for a in addresses:
                got = self.d.memory(a, 4)
                if got["data"] is None:
                    raise ValueError(f"0x{a:08X}: {got['reason']}")
                out[f"0x{a:08X}"] = int.from_bytes(bytes.fromhex(got["data"]), "little")
            return out
        return self.d.use(go, halted=True) if addresses else {}

    # ------------------------------------------------------------------ 実行（画面の続行・停止・ステップ）
    @staticmethod
    def _no_core(core: int | None) -> None:
        if core is not None:
            raise ValueError("core は複数ドロップの部品（RP2040）の引数: Linux のプロセスの全コア停止では使わない")

    def _limits(self, what: str) -> tuple[int, float]:
        if self.step_max_instructions is None or self.step_timeout_s is None:
            raise RuntimeError(f"{what}: linux.halt.debug.attach に step_max_instructions と step_timeout_s を渡していない"
                               "（上限の引数を持たない画面の道のステップには、使う側が明示した上限が要る）")
        return self.step_max_instructions, self.step_timeout_s

    def target_resume(self, core: int | None = None) -> dict:
        """全コアを再開して、待たずに返す（HaltDebugger.run(0)。すぐの当たりは拾う）。止まったことは target.status の
        poll（デーモンの見張り）が拾う。"""
        self._no_core(core)
        self.d.run(0)
        return self.target_status()

    def target_halt(self, core: int | None = None) -> dict:
        self._no_core(core)
        self.d.request_halt()
        self.d.halt()
        return self.target_status()

    def target_step(self, count: int = 1) -> dict:
        _, timeout_s = self._limits("target.step")
        r = self.d.step_instructions(self.d.current_tid(), count, timeout_s)
        return {**self.target_status(), "steps_done": r["steps_done"]}

    def target_reset(self, halt: bool = False, kind: str = "default") -> dict:
        raise RuntimeError("Linux のプロセスに attach している間はリセットしない（linux.halt.debug.close してから）")

    def run_until_halt(self, timeout_s: float = 5.0) -> dict:
        raise RuntimeError("Linux のプロセスでは debug.run_until_halt は使わない: 時間切れで止める既定を持つため。"
                           "linux.halt.debug.run（wait_s を明示。時間切れでも走らせたまま）か target.resume を使う")

    def step_line(self, mode: str = "over", location: str | None = None, timeout_s: float = 5.0,
                  mask_interrupts: bool = True) -> dict:
        """debug.step の形。over / into = 1 行、out = 呼び出し元へ戻るまで、until + location = そこまで（Run to Cursor）。
        上限は attach の step_max_instructions・step_timeout_s（debug.step の timeout_s・mask_interrupts はマイコンの
        引数で、Linux の道では使わない）。"""
        max_instructions, limit_s = self._limits("debug.step")
        tid = self.d.current_tid()
        t0 = time.monotonic()
        if mode in ("over", "into"):
            if location is not None:
                raise ValueError("location は until だけ")
            r = self.d.step(tid, mode, max_instructions, limit_s)
        elif mode == "out":
            r = self.d.step_out(tid, limit_s)
        elif mode == "until":
            if location is None:
                raise ValueError("Linux のプロセスの until には location（Run to Cursor の場所）が要る")
            r = self.d.run_to_location(tid, location, limit_s)
        else:
            raise ValueError(f"mode は over / into / out / until（{mode!r}）")
        return self._step_reply(r, time.monotonic() - t0, self.d.proc.tgid)

    @staticmethod
    def _step_reply(r: dict, elapsed_s: float, pid: int | None = None) -> dict:
        """HaltDebugger の止まり → debug.step の形。reason: step（終えた）/ timeout（時間の上限）/ max_instructions（命令の数の
        上限）/ breakpoint・watchpoint
        （途中の当たり）/ そのほかの種類の名前。終了したプロセスには pc が無いので、その事実を誤りにする
        （以後の操作の断りと同じ文: exited_message）。"""
        if r.get("address") is None:
            raise EngineError(exited_message(pid, r.get("kind"), r.get("code")))
        if r.get("timed_out"):
            reason = "timeout"
        elif r["kind"] == "step":                               # 着かずに命令の数の上限を使い切った: max_instructions
            reason = "step" if r.get("reached_new_line", True) else "max_instructions"
        else:
            reason = {"swbreak": "breakpoint", "hwbreak": "breakpoint", "watch": "watchpoint"}.get(r["kind"], r["kind"])
        pc = r["address"]
        out = {"reason": reason, "pc": pc, "where": _where_text(pc, r), "elapsed_s": elapsed_s, "time_source": "host"}
        for key in ("function", "file", "line"):
            if r.get(key) is not None:
                out[key] = r[key]
        if "instructions" in r:
            out["steps"] = r["instructions"]
        if r.get("kind") == "signal" and r.get("signal") is not None:    # どのシグナルで止まったか（番号はカーネルの事実、名前は表）
            out["signal"] = r["signal"]
            if r["signal"] in SIGNAL_NAMES:
                out["signal_name"] = SIGNAL_NAMES[r["signal"]]
        return out



def _where_text(pc: int, r: dict) -> str:
    if r.get("function") is None and not r.get("file"):
        return f"0x{pc:08X}"
    head = r["function"] if r.get("function") is not None else f"0x{pc:08X}"
    return f"{head} ({r['file']}:{r['line']})" if r.get("file") else head
