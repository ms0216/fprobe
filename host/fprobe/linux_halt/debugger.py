"""AI が JTAG の全コア停止で 1 つの Linux プロセスを止めて調べる道具の中身（API の linux.halt.debug.*。HaltSession が持つ）。

gdb を通さずに、Engine（全コアの停止・再開・ステップ・当たりの分類）・BreakpointTable（SW/HW・journal）・Process（スレッド・
ユーザ空間のメモリ）と、ELF の DWARF（Symbols・DebugInfo・Inspector。読み込み位置のずれ load bias を通す）を組む。
応答は事実の dict（判断・推定を入れない）。設計: docs/superpowers/specs/2026-11-03-linux-halt-ai-tools-design.md。

並行（鍵は 1 つ）: `self._cv` は**機器の錠そのもの**（HaltSession の lock = Probe の _lock の RLock）の Condition。API の
呼び出し・見張りのスレッド・コアの 1 回ずつの操作（HaltSession の `_Locked`）・ほかのサービスの機器の操作が、同じ 1 つの鍵を
取る。鍵が 2 つ（Condition と機器の錠）だと、取る順番が逆の道（機器の錠を持って poll を待つ作業スレッドと、Condition を
持って機器の錠を待つ読み）で行き詰まった（2026-11-03 実機。Pi の全コアが止まったまま残った）。鍵を取らずに読むのは
`snapshot()`（最後に分かった事実の写し）だけ。

部品: 共有の定数 `debugger_common`・事実 `debugger_facts`・ブレークポイント `debugger_break`・走行 `debugger_run`・読み書き
`debugger_read`・ステップ `debugger_step`。このファイルは持ち主の生成・鍵（`_call`）・見張り・閉じる。
"""
from __future__ import annotations

import threading
import time

from ..dwarf import DebugInfo
from ..symbols import Symbols
from .debugger_break import DebuggerBreakMixin
from .debugger_common import MASK, REG_NAMES, RUN_SLICE_S, STEP_MODES, _ThreadAccess, call_made, exited_message, stop_report
from .debugger_facts import DebuggerFactsMixin
from .debugger_read import DebuggerReadMixin
from .debugger_run import DebuggerRunMixin
from .debugger_step import DebuggerStepMixin
from .engine import Engine, EngineError
from .image import ElfImage, executable_image, shared_libraries
from .libsyms import LibraryModules

# 部品に分けた名前の再公開（view.py と試験が `fprobe.linux_halt.debugger` から取る）
__all__ = ["MASK", "REG_NAMES", "RUN_SLICE_S", "STEP_MODES", "HaltDebugger", "_ThreadAccess", "call_made", "exited_message", "stop_report"]


class HaltDebugger(DebuggerFactsMixin, DebuggerBreakMixin, DebuggerRunMixin, DebuggerReadMixin,
                   DebuggerStepMixin):
    """1 つのプロセスの持ち主（attach から close まで）。HaltSession.debug_attach が作る。"""

    def __init__(self, engine: Engine, image: ElfImage, *, skew_limit_ns: int, max_halt_s: float, attach_stop,
                 cleanup, clock=time.monotonic, wall=time.time, lock=None, libraries: list[dict] | None = None):
        """lock: 機器の錠（RLock。コアの口 _Locked と同じもの）。無ければ（CLI）自分の RLock。"""
        self.engine, self.proc, self.image = engine, engine.p, image
        self.symbols = Symbols(image.path)
        self.info = DebugInfo(image.path) if image.has_dwarf else None
        self.skew_limit_ns, self.max_halt_s = skew_limit_ns, max_halt_s
        # 共有ライブラリのシンボル（attach のとき取り寄せた分。無ければ空）: ライブラリの中の番地に名前を付ける・ARM / Thumb を知る
        self.libs = LibraryModules(libraries)
        self._cleanup, self._clock, self._wall = cleanup, clock, wall
        self.exe = executable_image(image, self.proc.auxv(), self.proc.mem.read)
        self.load_bias = self.exe["load_bias"]
        if self.load_bias is None:
            raise EngineError("the load address of %s in pid %d is not known: the saved auxv has neither AT_PHDR (with a "
                              "PT_PHDR in the ELF) nor AT_ENTRY" % (image.path, self.proc.tgid))
        self.libraries = shared_libraries(image, self.load_bias, self.proc.mem.read)
        self.attach_stop = stop_report(attach_stop, skew_limit_ns)
        self.breakpoints: dict[int, dict] = {}
        self._next_id = 1
        self.auto_resumed: list[dict] = []
        # 走行中のブレークポイントの置き外しのために、短く全コアを止めた回数と時間（事実。判定はしない）
        self.brief_halts: dict = {"count": 0, "total_s": 0.0, "max_s": 0.0}
        self.last_stop: dict | None = None
        # 画面（人）にも AI と同じものを見せるため、最後の run の結果（当たらなかった回も）と最後に読んだ全スレッドを持つ
        self.last_run: dict | None = None
        self.last_threads: dict | None = None
        self._selected: tuple[int, dict | None] | None = None    # 選んだスレッドと、選んだときの last_stop
        self._run_base: tuple[dict, int] | None = None        # 走らせ始めた run の時点の数え（poll の while_running の起点）
        self.closed = False
        self.close_result: dict | None = None
        self._cv = threading.Condition(lock if lock is not None else threading.RLock())
        self._facts: dict | None = None             # 最後に分かった事実の写し（snapshot。鍵を取らずに読む）
        # 止める要求（request_halt）: 走らせたままの待ち（run・run_to・poll）を、デーモンの作業スレッドの順番を待たずに
        # 破る（Engine の interrupted。待ちの中で全コアを止め、Stop("int")）
        self._interrupt = threading.Event()
        engine.interrupted = self._interrupt.is_set
        self._halted_since: float | None = None
        with self._cv:
            self._touch()
        self._watch = threading.Thread(target=self._watchdog, name="fprobe-linux-halt-debug-watch", daemon=True)
        self._watch.start()

    # ------------------------------------------------------------------ 共通
    def _call(self, fn, *, halted: bool | str):
        """API の 1 回の呼び出し: 見張りと交互に、閉じていないこと（と止まっていること）を確かめてから。
        halted: True = 止まっていること（走っていれば断る）、False = どちらでもよい、"auto" = 走っていれば短く全コアを止めて
        fn を実行し、終わったらまた走らせる（走行中にブレークポイントを置ける。一般のデバッガと同じ）。"""
        with self._cv:
            if self.closed:
                raise EngineError("the debugger is closed (linux.halt.debug.attach again)")
            if halted is not False and self.last_stop is not None and self.last_stop.get("kind") == "exit":
                # 終了を見たら固定する（死んだタスクの task_struct が書き換わるまで古い値が読めて、操作の成否がぶれた: 実機 2026-11-05）
                raise EngineError(exited_message(self.proc.tgid, "exit", self.last_stop.get("code")))
            if halted is True and self.engine.running:
                raise EngineError("the cores are running: stop them first (linux.halt.debug.halt, or "
                                  "linux.halt.debug.run until a stop)")
            if halted == "auto" and self.engine.running:
                return self._briefly_halted(fn)
            try:
                return fn()
            finally:
                self._touch()

    def _briefly_halted(self, fn):
        """走っている全コアを短く止めて fn を実行し、また走らせる（鍵の中で呼ぶ）。止めた瞬間に対象が自分で止まっていた
        ときは（ブレークポイントに当たった等）、その止まりを last_stop・last_run に残し、止めたまま fn を実行して返す
        （黙って走らせ直して当たりを失わない）。止めていた時間は brief_halts に数える（事実）。"""
        began = self._clock()
        stop = self.engine.halt()
        if stop.kind != "int":
            out = self._stop_dict(stop)
            self.last_stop = self.last_run = out
            try:
                return fn()
            finally:
                self._count_brief_halt(began)
                self._touch()
        try:
            return fn()
        finally:
            try:
                resumed = self.engine.run(0, clock=self._clock)    # 見張りの自動の再開と同じ（全コアを走らせたまま返る）
                if resumed.kind != "timeout":                      # 走らせ直す間に対象が止まった: その止まりを残す
                    out = self._stop_dict(resumed)
                    self.last_stop = self.last_run = out
            finally:
                self._count_brief_halt(began)
                self._touch()

    def _count_brief_halt(self, began: float) -> None:
        took = self._clock() - began
        self.brief_halts["count"] += 1
        self.brief_halts["total_s"] += took
        self.brief_halts["max_s"] = max(self.brief_halts["max_s"], took)

    def use(self, fn, *, halted: bool):
        """外の読み手（fprobe.linux_halt.view）が、API の 1 回の呼び出しとして fn を走らせる（_call と同じ: 見張りと交互、
        閉じていない・止まっていることの確かめ、止めたままの時間を数え直す）。"""
        return self._call(fn, halted=halted)

    def _touch(self) -> None:
        """止めたままの時間を数え直す（走っていれば数えない）。事実の写しも作り直す（鍵の中）。"""
        self._halted_since = None if self.engine.running or self.closed else self._clock()
        self._facts = self._facts_locked()
        self._cv.notify_all()

    def _watchdog(self) -> None:
        with self._cv:
            while not self.closed:
                if self._halted_since is None:
                    self._cv.wait()
                    continue
                left = self._halted_since + self.max_halt_s - self._clock()
                if left > 0:
                    self._cv.wait(left)
                    continue
                halted_s = self._clock() - self._halted_since
                entry = {"at_unix_s": self._wall(), "halted_s": halted_s}
                try:
                    stop = self.engine.run(0, clock=self._clock)
                    entry["result"] = self._stop_dict(stop)
                    if stop.kind != "timeout":
                        self.last_stop = entry["result"]
                except Exception as e:  # noqa: BLE001 - 見張りの失敗も事実として残す
                    entry["error"] = "%s: %s" % (type(e).__name__, e)
                self.auto_resumed.append(entry)
                self._touch()
                if "error" in entry:
                    return                                       # 再開に失敗したら繰り返さない（事実を残して止まる）

    # ------------------------------------------------------------------ 閉じる
    def close(self) -> dict:
        """ブレークポイントを全部外し、全コアを走らせ、停止デバッグを切る（HaltSession の後始末。journal も片付く）。"""
        with self._cv:
            if self.closed:
                return dict(self.close_result or {})
            n = len(self.breakpoints)
            result = self._cleanup(self.engine)
            self.closed = True
            self._touch()
            self.close_result = {"closed": True, "breakpoints_removed": n, **result}
            return dict(self.close_result)
