"""`HaltDebugger` の部品: 番地の読み替え（実行時 ↔ ELF）・停止の dict・今のスレッド・事実（`facts`・`snapshot`）。

どれも鍵（`_cv`）の中で呼ぶか、写し（`_facts`）を返す。コアを動かさない。
"""
from __future__ import annotations

from dataclasses import asdict

from ..dwarf import AccessError, Inspector
from ..dwarf.cexpr import ExpressionError
from ..dwarf.location import NotAvailable
from .debugger_common import MASK, _ThreadAccess, stop_report
from .engine import EngineError, Stop


class DebuggerFactsMixin:
    """事実の読みと、番地・停止の dict の組み立て（`HaltDebugger` の状態を使う）。"""

    def _link(self, address: int) -> int:
        return (address - self.load_bias) & MASK

    def _where(self, address: int) -> dict:
        """実行時の番地 → 関数・ファイル・行（ELF のリンク時の番地 address − load_bias で引いた事実。外なら None）。"""
        loc = self.symbols.describe(self._link(address))
        out = {"function": loc.function, "file": loc.file, "line": loc.line}
        if loc.function is None and (lib := self.libs.lookup(address)) is not None:     # 共有ライブラリの中（関数の名前だけ）
            out["function"] = lib.function
        return out

    def module_of(self, address: int) -> tuple[str, int] | None:
        """番地が共有ライブラリの中なら (ファイル名, ライブラリの中の番地)。シンボルが無くても分かる事実。"""
        return self.libs.module_of(address)

    def lib_location(self, address: int):
        """番地が共有ライブラリの関数の中なら、その場所（モジュール名・関数・先頭からのバイト数・Thumb か）。外なら None。"""
        return self.libs.lookup(address)

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
                    "auto_resumed": [dict(a) for a in self.auto_resumed], "brief_halts": dict(self.brief_halts),
                    "library_symbols": self.libs.facts(),
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
