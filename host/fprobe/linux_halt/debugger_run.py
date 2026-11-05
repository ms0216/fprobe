"""`HaltDebugger` の部品: 全コアを走らせる・待つ・止める（`run`・`request_halt`・`poll`・`halt`）。

待つ間は `RUN_SLICE_S` ごとに鍵を放し、状態の読み・見張りの poll を通す。
"""
from __future__ import annotations

from .debugger_common import RUN_SLICE_S
from .engine import EngineError, Stop


class DebuggerRunMixin:
    """走行の制御（`HaltDebugger` の状態を使う）。"""

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
