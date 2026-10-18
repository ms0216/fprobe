"""Probe の道具: 実行の制御（ソース行単位のステップ・ベクタキャッチ・ターゲットの関数の呼び出し）。

Probe（fprobe.mcp_server）に混ぜて使う mixin。Probe の _lock・target・dbg・_stop_dict・_service・_break_action・
_with_fault を使う。行単位のステップと関数の呼び出しは、run_until_halt と同じく待つ間は機器のロックを放す
（背景の RTT 等を止めない）。仕様: docs/reports/2026-10-18-gap-analysis.md の U01・U02・U53。

- debug.step: GDB の step / next / finish / until、Ozone・IAR の Step Into / Over / Out / Run to Cursor。
  止まったら関数名・行。故障で止まれば target.fault の要約、out なら戻り値。
- target.vector_catch / target.vector_catch.get: DEMCR の VC_*（J-Link の VCatch、IAR の Vector catch）。例外の入口で
  止める。設定と読みを分ける（読みは状態を変えない読みとして操作の記録から外せる）。
- debug.call: 止まったコアで関数を引数付きで実行し戻り値を得る（GDB の print f(x)、winIDEA testIDEA の土台）。
"""

__all__ = ["ExecutionTools"]


class ExecutionTools:
    """実行の制御の道具（Probe の mixin）。"""

    def step_line(self, mode: str = "over", location: str | None = None, timeout_s: float = 5.0,
                  mask_interrupts: bool = True) -> dict:
        """ソース行単位のステップ。mode: into（呼び出しに入る。前置きの後で止まる）/ over（呼び出しは踏み越える）/
        out（いまの関数から出る。戻り値も返す）/ until（over と同じだが後ろへ戻るループでは止まらない。location
        （関数名・"ファイル:行"・"0x..."）を指定するとそこまで。いまの関数から戻ればそこでも止まる）。
        mask_interrupts: 1 命令ずつ進める間は割り込みを止める（入らない）。False なら割り込みに入っても踏み越える。
        reason は step（終えた）か、途中で止まった理由（breakpoint / watchpoint / vector_catch / fault / timeout）。"""
        with self._lock:
            dbg = self.dbg
            self._watch_stops(False)                              # ここで処理する（背景の見張りと取り合わない）
        exited: dict = {}
        target = location if location is None or not location.lower().startswith("0x") else int(location, 16)
        stop = dbg.step_line(mode, target, timeout_s, mask_interrupts, lock=self._lock,
                             on_breakpoint=lambda: self._continue_after_break(exited))
        out = self._stop_dict(stop)
        if stop.value is not None:
            out["return_value"] = stop.value
        if exited:
            out.update(reason="exit", exit_code=exited["code"])
        elif stop.reason not in ("step", "timeout"):
            self._with_fault(out)
        return out

    def _continue_after_break(self, exited: dict) -> bool:
        """BKPT・ブレークポイントで止まった（ロックの中）: セミホスティングの要求・止まらない動作なら処理して True。"""
        result = self._service()
        if result is not None:
            if result.exited:
                exited["code"] = result.exit_code
                return False
            return True
        return self._break_action()

    def vector_catch(self, catch: list[str] | None = None) -> dict:
        """ベクタキャッチ（例外の入口でコアを止める。DEMCR の VC_*）。catch を渡すとその集合にする（"all" ですべて、
        [] で無し）。省略すると読むだけ。名前: core_reset / mem_manage / no_coprocessor / check_error / state_error /
        bus_fault / interrupt_error / hard_fault / secure_fault。ARMv6-M は hard_fault と core_reset だけ
        （unsupported に入る）。止まると debug.run_until_halt の reason が vector_catch になり、故障なら要約が付く。"""
        with self._lock:
            return self.dbg.vector_catch(catch)

    def vector_catch_get(self) -> dict:
        """いまのベクタキャッチを読む（enabled・available）。状態を変えない。"""
        return self.vector_catch(None)

    def call_function(self, function: str, arguments: list | None = None, timeout_s: float = 2.0,
                      disable_interrupts: bool = False) -> dict:
        """止まったコアで関数を引数付きで実行し、戻り値を得る（終われば、時間切れ・故障で打ち切っても、レジスタと
        スタックの下のメモリを元に戻す）。function: 関数名か "0x..."。arguments: 数、C の式（文字列。例 "&sensors[1]"、
        "MODE_RUN"）、{"string": "文字"}・{"bytes": "16 進"}（メモリに置いてその番地を渡す。呼んだ後の中身を
        buffers に返す）。呼んだ関数の中のブレークポイントでは止まらない。disable_interrupts: 呼ぶ間は割り込みを止める。"""
        with self._lock:
            dbg = self.dbg
            self._watch_stops(False)
        exited: dict = {}
        location = int(function, 16) if function.lower().startswith("0x") else function
        out = dbg.call(location, arguments or [], timeout_s, disable_interrupts, lock=self._lock,
                       on_breakpoint=lambda: self._continue_after_break(exited))
        if exited:
            out["exit_code"] = exited["code"]
        return out

