"""AI が JTAG の全コア停止で 1 つの Linux プロセスを止めて調べる道具の中身（API の linux.halt.debug.*。HaltSession が持つ）。

gdb を通さずに、Engine（全コアの停止・再開・ステップ・当たりの分類）・BreakpointTable（SW/HW・journal）・Process（スレッド・
ユーザ空間のメモリ）と、ELF の DWARF（Symbols・DebugInfo・Inspector。読み込み位置のずれ load bias を通す）を組む。
応答は事実の dict（判断・推定を入れない）。設計: docs/superpowers/specs/2026-11-03-linux-halt-ai-tools-design.md。

並行（鍵は 1 つ）: `self._cv` は**機器の錠そのもの**（HaltSession の lock = Probe の _lock の RLock）の Condition。API の
呼び出し・見張りのスレッド・コアの 1 回ずつの操作（HaltSession の `_Locked`）・ほかのサービスの機器の操作が、同じ 1 つの鍵を
取る。鍵が 2 つ（Condition と機器の錠）だと、取る順番が逆の道（機器の錠を持って poll を待つ作業スレッドと、Condition を
持って機器の錠を待つ読み）で行き詰まった（2026-11-03 実機。Pi の全コアが止まったまま残った）。鍵を取らずに読むのは
`snapshot()`（最後に分かった事実の写し）だけ。
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict

from ..dwarf import AccessError, DebugInfo, Inspector
from ..dwarf.cexpr import ExpressionError
from ..dwarf.location import NotAvailable
from ..symbols import Symbols
from .bp import NoFreeSlot
from .engine import Engine, EngineError, Stop
from .image import ElfImage, executable_image, shared_libraries
from .mmu import Unmapped

REG_NAMES = [f"r{i}" for i in range(13)] + ["sp", "lr", "pc", "cpsr"]
RUN_SLICE_S = 0.05      # run が錠（_cv）を放す間隔: 待っている間も状態の読み・止める要求・見張りの poll を通す
STEP_MODES = ("into", "over")
MASK = 0xFFFFFFFF


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


class HaltDebugger:
    """1 つのプロセスの持ち主（attach から close まで）。HaltSession.debug_attach が作る。"""

    def __init__(self, engine: Engine, image: ElfImage, *, skew_limit_ns: int, max_halt_s: float, attach_stop,
                 cleanup, clock=time.monotonic, wall=time.time, lock=None):
        """lock: 機器の錠（RLock。コアの口 _Locked と同じもの）。無ければ（CLI）自分の RLock。"""
        self.engine, self.proc, self.image = engine, engine.p, image
        self.symbols = Symbols(image.path)
        self.info = DebugInfo(image.path) if image.has_dwarf else None
        self.skew_limit_ns, self.max_halt_s = skew_limit_ns, max_halt_s
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
    def _call(self, fn, *, halted: bool):
        """API の 1 回の呼び出し: 見張りと交互に、閉じていないこと（と止まっていること）を確かめてから。"""
        with self._cv:
            if self.closed:
                raise EngineError("the debugger is closed (linux.halt.debug.attach again)")
            if halted and self.engine.running:
                raise EngineError("the cores are running: stop them first (linux.halt.debug.halt, or "
                                  "linux.halt.debug.run until a stop)")
            try:
                return fn()
            finally:
                self._touch()

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

    def _link(self, address: int) -> int:
        return (address - self.load_bias) & MASK

    def _where(self, address: int) -> dict:
        """実行時の番地 → 関数・ファイル・行（ELF のリンク時の番地 address − load_bias で引いた事実。外なら None）。"""
        loc = self.symbols.describe(self._link(address))
        return {"function": loc.function, "file": loc.file, "line": loc.line}

    def _bp_at(self, address: int) -> int | None:
        return next((i for i, b in self.breakpoints.items() if b["address"] == address), None)

    def _stop_dict(self, stop: Stop) -> dict:
        halted = stop.kind not in ("timeout",) and not self.engine.running
        out = {"kind": stop.kind, "tid": stop.tid or None, "address": None, "function": None, "file": None,
               "line": None, "breakpoint": None, "running": self.engine.running,
               "stop": stop_report(self.engine.last_stop_result, self.skew_limit_ns) if halted else None}
        if stop.kind in ("exit", "signal"):
            out["code"] = stop.code
            if stop.kind == "signal":
                out["signal"] = stop.signal
        if stop.kind == "watch":
            out.update(watch_address=stop.addr, access=stop.access, candidates=stop.candidates)
        pc = None
        if stop.kind in ("swbreak", "hwbreak", "trap"):
            pc = stop.addr
        elif halted and stop.tid and stop.kind not in ("exit",):
            try:
                pc = self.engine.get_regs(stop.tid)[15]
            except (KeyError, EngineError):
                pc = None
        if pc is not None:
            out["address"] = pc
            out.update(self._where(pc))
            out["breakpoint"] = self._bp_at(pc) if stop.kind in ("swbreak", "hwbreak") else None
        return out

    def _require_tid(self, tid: int) -> None:
        if tid not in self.proc.tids():
            raise EngineError("tid %d is not a thread of pid %d (threads: %s)" % (tid, self.proc.tgid,
                                                                                   sorted(self.proc.tids())))

    def _inspector(self, regs: list[int]) -> Inspector | None:
        if self.info is None:
            return None
        return Inspector(self.info, _ThreadAccess(regs, self.proc.mem), symbols=self.symbols, load_bias=self.load_bias)

    @staticmethod
    def _evaluate(insp: Inspector | None, expression: str, frame: int, max_elements: int) -> dict:
        if insp is None:
            return {"expression": expression, "error": "the ELF has no DWARF (no types and variables)"}
        try:
            return insp.read(expression, frame, max_elements)
        except (ExpressionError, KeyError, LookupError, NotAvailable, AccessError, IndexError, TypeError,
                ValueError) as e:
            return {"expression": expression, "error": "%s: %s" % (type(e).__name__, e)}

    # ------------------------------------------------------------------ 事実
    def facts(self) -> dict:
        with self._cv:
            stats = self.engine.stats()
            return {"pid": self.proc.tgid, "comm": self.proc.comm, "elf": self.image.facts(), "image": dict(self.exe),
                    "libraries": dict(self.libraries), "running": self.engine.running, "closed": self.closed,
                    "max_halt_s": self.max_halt_s, "skew_limit_ns": self.skew_limit_ns,
                    "breakpoints": [dict(b) for b in self.breakpoints.values()], "attach_stop": self.attach_stop,
                    "last_stop": self.last_stop, "last_run": self.last_run, "last_threads": self.last_threads,
                    "auto_resumed": [dict(a) for a in self.auto_resumed],
                    "stats": asdict(stats), "close": self.close_result}

    def halt_budget_left(self) -> float | None:
        """止めたまま放置しない見張りが全コアを走らせるまでの残りの秒（走っている・閉じていれば None）。"""
        with self._cv:
            if self._halted_since is None:
                return None
            return self._halted_since + self.max_halt_s - self._clock()

    def current_tid(self) -> int:
        """今のスレッド（current_thread の tid）。"""
        return self.current_thread()[0]

    def current_thread(self) -> tuple[int, str]:
        """今のスレッドと、その決まり方: selected（select_thread で選んだ。次の止まりまで）/ last_stop（最後の停止の
        スレッド）/ rule（Engine.default_tid: 対象のスレッドを走らせている最小番号のコア、無ければリーダー。規則であって
        判断ではない）。"""
        with self._cv:
            tids = self.proc.tids()
            if self._selected is not None and self._selected[1] is self.last_stop and self._selected[0] in tids:
                return self._selected[0], "selected"
            tid = (self.last_stop or {}).get("tid")
            if tid in tids:
                return tid, "last_stop"
            return self.engine.default_tid(), "rule"

    def select_thread(self, tid: int) -> dict:
        """今のスレッドを tid にする（画面の Threads の選択。レジスタ・呼び出し履歴・変数・ステップがこのスレッドで
        答える）。次に止まると、止まったスレッドに戻る。コアは動かさない。"""
        def go():
            self._require_tid(tid)
            self._selected = (tid, self.last_stop)
            return {"tid": tid, "tid_from": "selected"}
        return self._call(go, halted=False)

    def status_facts(self) -> dict:
        """鍵の中で事実を読む（コアは動かさない・止めたままの時間を数え直さない）。写しも作り直す。"""
        with self._cv:
            self._facts = self._facts_locked()
            return self._facts

    def snapshot(self) -> dict | None:
        """最後に分かった事実の写し（status_facts の形。鍵を取らない: 鍵を持つ長い操作の間も読める。古いことがある）。"""
        return self._facts

    def _facts_locked(self) -> dict:
        """事実（鍵の中で呼ぶ）。"""
        tid, tid_from = (None, None) if self.closed else self.current_thread()
        pc, registers_from, error = None, None, None
        if tid is not None and not self.engine.running:
            try:
                pc = self.engine.get_regs(tid)[15]
                registers_from = "core" if self.engine.live(tid) is not None else "pt_regs"
            except Exception as e:  # noqa: BLE001 - 状態の読みは止めない（読めなかった理由を事実として返す）
                error = "%s: %s" % (type(e).__name__, e)
        return {"pid": self.proc.tgid, "comm": self.proc.comm, "running": self.engine.running, "closed": self.closed,
                "tid": tid, "tid_from": tid_from, "pc": pc, "registers_from": registers_from, "pc_error": error,
                "where": None if pc is None else self._where(pc), "last_stop": self.last_stop,
                "attach_stop": self.attach_stop, "threads": self.threads_brief(), "elf": str(self.image.path),
                "load_bias": self.load_bias}

    def threads_brief(self) -> list[dict]:
        return [{"tid": t.pid, "comm": t.comm} for t in self.proc.threads]

    # ------------------------------------------------------------------ ブレークポイント
    def resolve(self, location: str) -> tuple[int, int, bool | None]:
        """場所 → (実行時の番地, リンク時の番地, prologue_end)。番地（"0x…"・10 進。実行時）・"ファイル:行"（その行の最初の
        番地）・関数名（前置きの後。prologue_end = 行表に prologue_end があったか。ほかは None）。解けなければ EngineError。"""
        text = location.strip()
        prologue_end = None
        try:
            if text.lower().startswith("0x") or text.isdigit():
                address = int(text, 0)
                return address, self._link(address), None
            link = self.symbols.resolve(text)
            if not (":" in text and text.rpartition(":")[2].isdigit()):     # 関数名: 前置きの後
                start = link
                link = self.symbols.after_prologue(start)
                prologue_end = self.symbols.has_prologue_end(start)
            return (link + self.load_bias) & MASK, link, prologue_end
        except (KeyError, ValueError) as e:
            raise EngineError("location %r: %s" % (location, e.args[0] if e.args else e)) from None

    def break_insert(self, location: str, kind: str) -> dict:
        def run():
            if kind not in ("sw", "hw"):
                raise EngineError("kind must be 'sw' or 'hw' (got %r)" % (kind,))
            address, link, prologue_end = self.resolve(location)
            function = self.symbols.function_at(link)
            if function is None:
                raise EngineError("location %r (0x%08x): no function of %s contains it, so its instruction set "
                                  "(ARM or Thumb) is not known" % (location, address, self.image.path.name))
            width = 2 if function.address in self.image.thumb_functions else 4
            if self._bp_at(address) is not None:
                raise EngineError("location %r (0x%08x): there is already breakpoint id %d"
                                  % (location, address, self._bp_at(address)))
            try:
                self.engine.bps.insert(self.proc.tgid, address, kind=kind, width=width)
            except Unmapped as e:
                raise EngineError("location %r (0x%08x): the page is not mapped (%s); bp_unmapped=%s"
                                  % (location, address, e, self.engine.bps.unmapped)) from None
            except NoFreeSlot as e:
                message = "location %r (0x%08x): no free hardware breakpoint (%s)" % (location, address, e)
                raise EngineError(message) from None
            placed = self.engine.bps.lookup(self.proc.tgid, address)
            bp = {"id": self._next_id, "location": location, "address": address, "link_address": link,
                  "kind": "hw" if placed.hw else "sw", "requested_kind": kind, "width": width,
                  "prologue_end": prologue_end, **self._where(address)}
            self.breakpoints[self._next_id] = bp
            self._next_id += 1
            return dict(bp)
        return self._call(run, halted=True)

    def break_remove(self, bp_id: int) -> dict:
        def run():
            bp = self.breakpoints.get(bp_id)
            if bp is None:
                raise EngineError("no breakpoint id %d (ids: %s)" % (bp_id, sorted(self.breakpoints)))
            self.engine.bps.remove(self.proc.tgid, bp["address"], kind=bp["kind"])
            del self.breakpoints[bp_id]
            return {"removed": dict(bp)}
        return self._call(run, halted=True)

    # ------------------------------------------------------------------ 走らせる・止める
    def run(self, wait_s: float) -> dict:
        """全コアを再開して（走っていれば再開せずに）、対象の止まりを最大 wait_s 秒待つ。待つ間は RUN_SLICE_S ごとに錠を
        放す（状態の読み・見張りの poll が通る）。止める要求（request_halt）が来れば待ちの中で全コアを止めて返す
        （kind = int）。待つ間にほかの道（poll・halt）が止めれば、その止まり（last_run）を返す。"""
        if not (isinstance(wait_s, (int, float)) and wait_s >= 0):
            raise EngineError("wait_s must be >= 0")
        deadline = self._clock() + wait_s
        first, base = True, None
        while True:
            with self._cv:
                if self.closed:
                    raise EngineError("the debugger is closed (linux.halt.debug.attach again)")
                if first:
                    base = dict(self.engine.stats().counters), self.engine.foreign_hit_seq
                    if not self.engine.running:
                        self._run_base = base
                    first = False
                elif not self.engine.running:                    # 待つ間にほかの道が止めた
                    self._touch()
                    return dict(self.last_run if self.last_run is not None else self.last_stop or {})
                stop = self.engine.run(max(0.0, min(RUN_SLICE_S, deadline - self._clock())), clock=self._clock)
                if stop.kind == "timeout" and self._clock() < deadline:
                    if self._facts is None or not self._facts["running"]:
                        self._touch()                            # 走り出した: 写しに（鍵を取らない読みが見る）
                    continue                                     # 錠を放してから続きを待つ
                self._interrupt.clear()
                out = self._stop_dict(stop)
                self._while_running(out, base)                   # この run の間
                self.last_run = out
                if out["kind"] != "timeout":
                    self.last_stop = out
                self._touch()
                return out

    def request_halt(self) -> None:
        """止める要求だけを立てる（錠を取らない・すぐ返る）。走らせたままの待ち（run・run_to・poll）がそれを見て全コアを
        止める。デーモンが linux.halt.debug.halt・target.halt を受けた時点で呼ぶ（作業スレッドの順番を待たない）。
        続けて halt() を呼ぶと、止まっていればその止まりを返す。"""
        self._interrupt.set()

    def _while_running(self, out: dict, base: tuple[dict, int]) -> None:
        """base（数えと対象外の当たりの番号）から今までに、対象の当たりとして数えなかったもの（事実だけ。見分け方が
        合っているかの判断はしない）。"""
        before, seq = base
        after = self.engine.stats().counters
        out["while_running"] = {
            "counters": {k: after[k] - before.get(k, 0) for k in after if after[k] != before.get(k, 0)},
            "foreign_hits": [dict(h) for h in self.engine.foreign_hits if h["seq"] > seq],
            "thread_id_method": self.engine.thread_id,
            "target_threads": sorted(self.proc.tids())}

    def poll(self) -> dict | None:
        """走らせたままの全コアに、対象の止まりがあったかを 1 回だけ確かめる（待たない。Engine.run(0) の続き）。走って
        いなければ None。止まっていれば run と同じ形（while_running は走らせ始めた run からの増分）を last_run・last_stop に
        入れて返す。止まっていなければ None（何も変えない）。止めたままの時間は数え直さない（見張りの読みで、放置の
        見張りを効かなくしない）。"""
        with self._cv:
            if self.closed or not self.engine.running:
                return None
            stop = self.engine.run(0, clock=self._clock)
            if stop.kind == "timeout":
                return None
            out = self._stop_dict(stop)
            self._while_running(out, self._run_base or (dict(self.engine.stats().counters), self.engine.foreign_hit_seq))
            self.last_run = self.last_stop = out
            self._touch()                        # 止まった: ここから止めたままの時間を数える
            return dict(out)

    def halt(self) -> dict:
        """走っている全コアを要求で止める。止まっていれば（止める要求で待ちが止めた後も）最後の止まりを返す。"""
        def go():
            try:
                if not self.engine.running:
                    if self.last_stop is not None:            # 止める要求で待ちが止めた後・もう止まっている
                        return dict(self.last_stop)
                    return self._stop_dict(Stop("int", self.engine.default_tid()))
                out = self._stop_dict(self.engine.halt())
                self.last_stop = out
                return out
            finally:
                self._interrupt.clear()
        return self._call(go, halted=False)

    # ------------------------------------------------------------------ 読む
    def threads(self, variables: bool, expressions: list[str], max_elements: int) -> dict:
        def read():
            rows = []
            on_core = {s.tid: (n, s) for n, s in sorted(self.engine.core.items()) if s.halted}
            for t in self.proc.threads:
                core = on_core.get(t.pid)
                row = {"tid": t.pid, "comm": t.comm, "on_core": core[0] if core else None,
                       "mode": ("user" if core[1].user else "kernel") if core else None,
                       "registers_from": "core" if self.engine.live(t.pid) is not None else "pt_regs"}
                try:
                    regs = self.engine.get_regs(t.pid)
                except (KeyError, EngineError, OSError) as e:
                    row.update(registers=None, pc=None, function=None, file=None, line=None,
                               error="%s: %s" % (type(e).__name__, e))
                    rows.append(row)
                    continue
                row["registers"] = dict(zip(REG_NAMES, regs))
                row["pc"] = regs[15]
                row.update(self._where(regs[15]))
                if variables or expressions:
                    insp = self._inspector(regs)
                    if variables:
                        if insp is None:
                            row.update(arguments=None, locals=None, variables_error="the ELF has no DWARF")
                        else:
                            try:
                                fv = insp.frame_variables(0, max_elements)
                                row.update(arguments=fv["arguments"], locals=fv["locals"])
                            except (IndexError, NotAvailable, AccessError) as e:
                                row.update(arguments=None, locals=None,
                                           variables_error="%s: %s" % (type(e).__name__, e))
                    row["expressions"] = [self._evaluate(insp, x, 0, max_elements) for x in expressions]
                rows.append(row)
            out = {"pid": self.proc.tgid, "threads": rows, "stop": (self.last_stop or {}).get("stop")
                   or self.attach_stop, "load_bias": self.load_bias}
            self.last_threads = out
            return out
        return self._call(read, halted=True)

    def read(self, expressions: list[str], tid: int, frame: int, max_elements: int) -> dict:
        def go():
            self._require_tid(tid)
            regs = self.engine.get_regs(tid)
            insp = self._inspector(regs)
            where = None
            if insp is not None:
                try:
                    state = insp.frame(frame)
                    where = {"index": frame, "pc": state.pc, **insp.where(state)}
                except IndexError as e:
                    raise EngineError("frame %d of tid %d: %s" % (frame, tid, e)) from None
            return {"tid": tid, "frame": where,
                    "values": [self._evaluate(insp, x, frame, max_elements) for x in expressions]}
        return self._call(go, halted=True)

    def memory(self, address: int, length: int) -> dict:
        def go():
            try:
                data = self.proc.mem.read(address, length)
            except Unmapped as e:
                return {"address": address, "length": length, "data": None, "reason": str(e)}
            data = self.engine.bps.shadow(self.proc.tgid, address, data)    # 置いた BKPT ではなく元の命令を見せる
            return {"address": address, "length": length, "data": data.hex(), "reason": None}
        return self._call(go, halted=True)

    # ------------------------------------------------------------------ 1 行
    def _key(self, pc: int):
        w = self._where(pc)
        return w["function"], w["file"], w["line"]

    def step(self, tid: int, mode: str, max_instructions: int, timeout_s: float) -> dict:
        """スレッド tid を 1 行進める。上限は 2 つとも使う側が明示する: max_instructions（1 命令のステップの数）と
        timeout_s（PC の時計の秒。命令の間と、呼び出しの踏み越え・カーネルから戻るのを待つ run_to の中で確かめる）。
        命令の数を過ぎたら reached_new_line = false、時間を過ぎたら全コアを止めたまま timed_out = true（その時の位置）。"""
        def go():
            if mode not in STEP_MODES:
                raise EngineError("mode must be one of %s" % (STEP_MODES,))
            if max_instructions < 1:
                raise EngineError("max_instructions must be >= 1")
            if timeout_s < 0:
                raise EngineError("timeout_s must be >= 0")
            self._require_tid(tid)
            deadline = self._clock() + timeout_s
            start_pc = self.engine.get_regs(tid)[15]
            start = self._key(start_pc)
            n = calls = 0
            pc = start_pc

            def done(stop, reached):
                if stop.kind == "timeout":                     # run_to の待ちが上限を過ぎた（止めたまま）
                    return self._timed_out(tid, n, calls, start_pc)
                return self._step_result(stop, n, calls, reached, start_pc)
            while n < max_instructions:
                if n > 0 and self._clock() >= deadline:
                    return self._timed_out(tid, n, calls, start_pc)
                stop = self.engine.step(tid, deadline, self._clock)
                if stop.kind == "timeout":
                    return done(stop, False)
                n += 1
                if stop.kind != "trap":
                    return done(stop, False)
                pc = stop.addr
                link = self._link(pc)
                fn = self.symbols.function_at(link)
                if fn is None or fn.address == link:                     # 呼び出し（関数の先頭に入った・ELF の外へ出た）
                    if mode == "into" and fn is not None and self.symbols.line_entry(link) is not None:
                        target = (self.symbols.after_prologue(link) + self.load_bias) & MASK
                        if target != pc:
                            width = 2 if fn.address in self.image.thumb_functions else 4
                            stop, reached = self.engine.run_to(target, width, tid, deadline, self._clock)
                            if not reached:
                                return done(stop, False)
                            pc = target
                        return self._step_result(Stop("trap", tid, pc), n, calls, True, start_pc)
                    lr = self.engine.get_regs(tid)[14]
                    stop, reached = self.engine.run_to(lr & ~1, 2 if lr & 1 else 4, tid, deadline, self._clock)
                    calls += 1
                    if not reached:
                        return done(stop, False)
                    pc = lr & ~1
                    link = self._link(pc)
                entry = self.symbols.line_entry(link)
                if entry is not None and entry.address == link and self._key(pc) != start:
                    return self._step_result(Stop("trap", tid, pc), n, calls, True, start_pc)
            return self._step_result(Stop("trap", tid, pc), n, calls, False, start_pc)
        return self._call(go, halted=True)

    def _timed_out(self, tid: int, n: int, calls: int, start_pc: int) -> dict:
        """時間の上限で止めた 1 行のステップ: 全コアは止まっている。スレッドの今の位置。"""
        pc = self.engine.get_regs(tid)[15]
        out = self._step_result(Stop("trap", tid, pc), n, calls, False, start_pc)
        out["timed_out"] = True
        self.last_stop = out
        return out

    def _step_result(self, stop: Stop, n: int, calls: int, reached: bool, start_pc: int) -> dict:
        out = self._stop_dict(stop)
        if stop.kind == "trap":
            out["kind"] = "step"
        out.update(reached_new_line=reached, instructions=n, stepped_over_calls=calls, from_address=start_pc, timed_out=False,
                   **{"from_" + k: v for k, v in self._where(start_pc).items()})
        self.last_stop = out
        return out

    def step_instructions(self, tid: int, count: int, timeout_s: float) -> dict:
        """スレッド tid を 1 命令ずつ count 回進める（途中で 1 命令のステップでない止まり（当たり・終了）になれば打ち切る）。
        timeout_s（使う側が明示）は全体の上限（命令の間と、カーネルから戻るのを待つ run_to の中で確かめる。過ぎたら
        止めたまま timed_out = true）。止まった事実（kind = step / 途中の止まりの種類）と steps_done。"""
        def go():
            if count < 0:
                raise EngineError("count must be >= 0")
            if timeout_s < 0:
                raise EngineError("timeout_s must be >= 0")
            self._require_tid(tid)
            deadline = self._clock() + timeout_s
            stop, done, timed_out = Stop("trap", tid, self.engine.get_regs(tid)[15]), 0, False
            while done < count:
                if done > 0 and self._clock() >= deadline:
                    timed_out = True
                    break
                stop = self.engine.step(tid, deadline, self._clock)
                if stop.kind == "timeout":
                    stop, timed_out = Stop("trap", tid, self.engine.get_regs(tid)[15]), True
                    break
                if stop.kind != "trap":
                    break
                done += 1
            out = self._stop_dict(stop)
            if stop.kind == "trap":
                out["kind"] = "step"
            out["steps_done"] = done
            out["timed_out"] = timed_out
            self.last_stop = out
            return out
        return self._call(go, halted=True)

    def _width_at(self, address: int) -> int:
        """実行時の番地の命令の幅（その番地を含む関数が Thumb なら 2、ARM なら 4）。関数の外なら EngineError。"""
        fn = self.symbols.function_at(self._link(address))
        if fn is None:
            raise EngineError("0x%08x: no function of %s contains it, so its instruction set (ARM or Thumb) is not "
                              "known" % (address, self.image.path.name))
        return 2 if fn.address in self.image.thumb_functions else 4

    def _run_to_here(self, tid: int, address: int, timeout_s: float, start_pc: int) -> dict:
        """スレッド tid が address に着くまで全コアを走らせる（時間の上限は使う側が明示。過ぎたら止めたまま timed_out）。"""
        if timeout_s < 0:
            raise EngineError("timeout_s must be >= 0")
        stop, reached = self.engine.run_to(address, self._width_at(address), tid, self._clock() + timeout_s,
                                           self._clock)
        if stop.kind == "timeout":
            return self._timed_out(tid, 0, 0, start_pc)
        out = self._step_result(Stop("trap", tid, address) if reached else stop, 0, 0, reached, start_pc)
        out["reached"] = reached
        return out

    def step_out(self, tid: int, timeout_s: float) -> dict:
        """スレッド tid の今の関数から呼び出し元へ戻るまで走らせる（戻り先 = CFI で巻き戻したフレーム 1 の pc）。"""
        def go():
            self._require_tid(tid)
            regs = self.engine.get_regs(tid)
            insp = self._inspector(regs)
            if insp is None:
                raise EngineError("the ELF has no DWARF (no call frame information to find the return address)")
            try:
                caller = insp.frame(1)
            except IndexError as e:
                raise EngineError("tid %d: the caller is not known (%s)" % (tid, e)) from None
            return self._run_to_here(tid, caller.pc & ~1, timeout_s, regs[15])
        return self._call(go, halted=True)

    def run_to_location(self, tid: int, location: str, timeout_s: float) -> dict:
        """スレッド tid が場所（関数名・"ファイル:行"・番地）に着くまで走らせる（Run to Cursor。ほかのスレッドが先に
        そこを通っても、tid が着くまで走らせる）。"""
        def go():
            self._require_tid(tid)
            address = self.resolve(location)[0]
            return self._run_to_here(tid, address, timeout_s, self.engine.get_regs(tid)[15])
        return self._call(go, halted=True)

    def write_register(self, tid: int, name: str, value: int) -> None:
        """スレッド tid のレジスタ（r0〜r12・sp・lr・pc・cpsr）に書く（コアにいればコア、いなければ pt_regs）。"""
        def go():
            self._require_tid(tid)
            key = {"r13": "sp", "r14": "lr", "r15": "pc"}.get(name.lower(), name.lower())
            if key not in REG_NAMES:
                raise EngineError("register %s is not one of %s" % (name, REG_NAMES))
            regs = list(self.engine.get_regs(tid))
            regs[REG_NAMES.index(key)] = value & MASK
            self.engine.set_regs(tid, regs)
        self._call(go, halted=True)

    def write_expression(self, tid: int, expression: str, value, frame: int) -> None:
        """スレッド tid のフレーム frame で、C の式の指す値（メモリ上）に書く（プロセスの仮想メモリ）。"""
        def go():
            self._require_tid(tid)
            insp = self._inspector(self.engine.get_regs(tid))
            if insp is None:
                raise EngineError("the ELF has no DWARF (no types and variables)")
            insp.write(expression, value, frame)
        self._call(go, halted=True)

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
