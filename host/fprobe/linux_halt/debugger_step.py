"""`HaltDebugger` の部品: ステップと「そこまで走る」（`step`・`step_instructions`・`step_out`・`run_to_location`）。

上限（命令の数・時間）は使う側が明示する。時間を過ぎたら全コアを止めたまま `timed_out`。
"""
from __future__ import annotations

from .debugger_common import MASK, STEP_MODES, call_made
from .engine import EngineError, Stop
from .mmu import Unmapped


class DebuggerStepMixin:
    """ステップ（`HaltDebugger` の状態を使う）。"""

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
                prev_pc = pc                                             # 実行する命令の pc（呼び出しか戻りかの判定に使う）
                stop = self.engine.step(tid, deadline, self._clock)
                if stop.kind == "timeout":
                    return done(stop, False)
                n += 1
                if stop.kind != "trap":
                    return done(stop, False)
                pc = stop.addr
                link = self._link(pc)
                fn = self.symbols.function_at(link)
                if fn is None and not self._was_call(prev_pc, tid):
                    # ELF の外へ戻った（main が libc へ戻る等）。呼び出しではないので LR は戻り番地でない: そこで止める
                    return self._step_result(Stop("trap", tid, pc), n, calls, True, start_pc)
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

    def _insn_width(self, pc: int, thumb: bool) -> int:
        """pc の命令の幅（ARM は 4。Thumb は先頭の半語が 0b11101 / 0b11110 / 0b11111 で始まれば 4（Thumb-2 の 32 ビット）、他は 2）。"""
        if not thumb:
            return 4
        first = int.from_bytes(self.engine.bps.shadow(self.proc.tgid, pc & ~1, self.proc.mem.read(pc & ~1, 2)), "little")
        return 4 if (first >> 11) in (0b11101, 0b11110, 0b11111) else 2

    def _was_call(self, prev_pc: int, tid: int) -> bool:
        """直前に実行した命令（prev_pc）が呼び出しだったか。LR が「prev_pc の命令の次」になっていれば呼び出し（call_made）。命令の幅は、
        prev_pc を含む関数の命令セット（ELF のシンボル・取り寄せたライブラリのシンボル）から決め、分からなければ ARM / Thumb のどちらの幅でも
        LR と合うかで見る。ステップごとの JTAG の読みを増やさないよう、レジスタ（LR）は ELF の外へ出たときだけ読む。"""
        lr = self.engine.get_regs(tid)[14]
        try:
            thumb = self._width_at(prev_pc) == 2
            return call_made(prev_pc, self._insn_width(prev_pc, thumb), lr)
        except (EngineError, Unmapped):
            return any(call_made(prev_pc, width, lr) for width in (2, 4))

    def _width_at(self, address: int, return_thumb: bool | None = None) -> int:
        """実行時の番地の命令の幅（その番地を含む関数が Thumb なら 2、ARM なら 4）。関数の外なら、共有ライブラリのシンボルの
        bit 0、無ければ return_thumb（戻り番地として来た番地の bit 0。呼び出し元が Thumb で戻るか）。どれも無ければ EngineError。"""
        fn = self.symbols.function_at(self._link(address))
        if fn is None:
            thumb = self.libs.thumb_at(address)                     # 共有ライブラリの関数（取り寄せたシンボルの bit 0）
            if thumb is None:
                thumb = return_thumb                                # シンボルが無い所（stripped の libc の内部）への戻り
            if thumb is None:
                raise EngineError("0x%08x: no function of %s or of a loaded shared library contains it, so its instruction "
                                  "set (ARM or Thumb) is not known" % (address, self.image.path.name))
            return 2 if thumb else 4
        return 2 if fn.address in self.image.thumb_functions else 4

    def _run_to_here(self, tid: int, address: int, timeout_s: float, start_pc: int,
                     return_thumb: bool | None = None) -> dict:
        """スレッド tid が address に着くまで全コアを走らせる（時間の上限は使う側が明示。過ぎたら止めたまま timed_out）。
        return_thumb: address が戻り番地なら、その bit 0（シンボルの無い所への戻りの命令セット）。"""
        if timeout_s < 0:
            raise EngineError("timeout_s must be >= 0")
        stop, reached = self.engine.run_to(address, self._width_at(address, return_thumb), tid,
                                           self._clock() + timeout_s, self._clock)
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
                last = insp.frames()[-1].stop                  # 巻き戻しが止まった理由（unwind の事実。無ければ None）
                raise EngineError("tid %d: the caller is not known (%s; unwinding stopped: %s)"
                                  % (tid, e, last if last else "no reason recorded")) from None
            return self._run_to_here(tid, caller.pc & ~1, timeout_s, regs[15], return_thumb=caller.thumb)
        return self._call(go, halted=True)

    def run_to_location(self, tid: int, location: str, timeout_s: float) -> dict:
        """スレッド tid が場所（関数名・"ファイル:行"・番地）に着くまで走らせる（Run to Cursor。ほかのスレッドが先に
        そこを通っても、tid が着くまで走らせる）。"""
        def go():
            self._require_tid(tid)
            address = self.resolve(location)[0]
            return self._run_to_here(tid, address, timeout_s, self.engine.get_regs(tid)[15])
        return self._call(go, halted=True)
