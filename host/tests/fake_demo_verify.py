"""機器なしで画面を試すデモ（fake_daemon.py --demo）の一部: 試す窓（故障注入・規則・フック）が呼ぶ API。

DemoProbe（fake_demo.py）に混ぜる mixin。本物のコードをそのまま使い、機器の操作だけを偽物にする:
- 規則（rules.check・rules.states・rules.report）: 本物の Probe（self._elf）にデモの記録を開いた状態で渡す。
- 故障注入（inject.run）: 台本の検査と試行の進め方は本物（fprobe.inject.run_script）。機器の操作（InjectHost）だけ偽物
  （_DemoInjectHost）: 割り込み書き込みはデモの RAM に書き、注入の時刻はデモの記録の範囲の中の時刻にする。
- フック（hooks.*）: 本物の登録簿（fprobe.probe_hooks.HookTools）。デーモンが backend.hook_table を見て実行する。

偽の機器の成立の仕方（デモの約束。本物の機器の振る舞いではない）:
- host: 発火（fire）で成立。pattern・sequence: 仕掛けて 20 ms 後に成立（端子が動いたとみなす）。
- dap_match: 番地がデモの counter のとき 10 ms 後に成立（デモのファームウェアが counter を書くとみなす）。ほかの番地は
  成立しない（not_triggered を見せる）。
- 書き込みはデモの RAM の外なら swd_error（inject_error を見せる）。
"""

import time

from fprobe.inject import InjectScript, run_script
from fprobe.probe_hooks import HookTools
from fprobe.xtrig import InjectStatus

__all__ = ["VerifyDemo"]

DELAY_S = {"pattern": 0.02, "sequence": 0.02, "dap_match": 0.01}
STEP_S = 0.25                                   # 注入の時刻: 記録の始めから 0.25 秒ずつ（記録の長さで折り返す）


class _DemoInjectHost:
    """fprobe.inject.InjectHost のデモの実装。"""

    def __init__(self, probe):
        self.p = probe
        self._pending: tuple[int, int] | None = None
        self._armed: tuple[str, dict, float] | None = None
        self._fired = False

    # --- 番地・メモリ（デモの RAM） ---

    def resolve(self, address):
        if isinstance(address, int):
            return address
        text = address.strip()
        if text.lower().startswith("0x"):
            return int(text, 16)
        return self.p._elf._symbols.variable(text).address

    def read_memory(self, address, size):
        return int.from_bytes(self.p._mem.read(address, size), "little")

    def write_memory(self, address, value, size):
        self.p._mem.write(address, (value & (1 << 8 * size) - 1).to_bytes(size, "little"))

    # --- 割り込み書き込み（偽物） ---

    def set_inject(self, address, value, ap):
        self._pending = (address, value)

    def arm(self, when):
        kind, params = next(iter(when.items()))
        self._armed, self._fired = (kind, params, time.monotonic()), False

    def fire(self):
        self._fired = True

    def disarm(self):
        self._armed = None

    def restore_default(self):
        self._pending = None

    def _due(self) -> bool:
        if self._armed is None:
            return False
        kind, params, since = self._armed
        if kind == "host":
            return self._fired
        if kind == "dap_match":
            counter = self.p._elf._symbols.variable("counter").address
            if params.get("address") != counter:
                return False
        return time.monotonic() - since >= DELAY_S[kind]

    def inject_status(self):
        s = self.p._inject_status
        if self._due() and self._pending is not None:
            self._armed = None
            address, value = self._pending
            span = self.p._elf._view.span() or (0, 0)
            hz = self.timebase_hz()
            self.p._inject_n += 1
            length = max(1, span[1] - span[0])
            t = span[0] + round(self.p._inject_n * STEP_S * hz) % length
            try:
                self.write_memory(address, value, 4)
                result, ack = "ok", 1
            except PermissionError:                   # デモの RAM の外
                result, ack = "swd_error", 4
            s = InjectStatus(False, result, ack, s.count + 1, t)
            self.p._inject_status = s
        return s

    def timebase_hz(self):
        return float(self.p._elf._view.meta["timebase_hz"])

    # --- ターゲット（デモの実行） ---

    def state(self):
        return self.p.target_status()["state"].lower()

    def evaluate(self, expression):
        return self.p.evaluate(expression, live=self.p.state != "HALTED")["value"]

    def reset(self, kind):
        self.p.target_reset(halt=False)

    def resume(self):
        self.p.target_resume()

    def recording(self):
        return self.p._elf._view

    def note(self, stream, kind, t, t_end, body):
        rec = self.p._elf._recorder                 # 記録中だけ（本物と同じ）
        if rec is not None:
            rec.add(stream, kind, t, body=body, t_end=t_end)

    def sleep(self, seconds):
        time.sleep(seconds)

    def monotonic(self):
        return time.monotonic()


class VerifyDemo(HookTools):
    def _init_verify(self) -> None:
        """DemoProbe.__init__ から。"""
        self._init_hooks()
        self._inject_status = InjectStatus(False, "ok", 0, 0, 0)
        self._inject_n = 0

    # --- 規則（本物の Probe にデモの記録） ---

    def rules_check(self, rules=None, path=None, t0=None, t1=None, baseline=None, mark=True, limit=100, caller=None):
        return self._elf.rules_check(rules, path, t0, t1, baseline, mark, limit, caller)

    def rules_last(self):
        return self._elf.rules_last()

    def rules_states(self, event, field, labels=None, t0=None, t1=None, dot_path=None):
        return self._elf.rules_states(event, field, labels, t0, t1, dot_path)

    def rules_report(self, path, format="markdown"):
        return self._elf.rules_report(path, format)

    # --- 故障注入（本物の台本の検査と進め方、偽の機器） ---

    def inject_run(self, script=None, path=None, caller=None):
        if (script is None) == (path is None):
            raise ValueError("script か path のどちらか一方を渡す")
        parsed = InjectScript.parse(script) if script is not None else InjectScript.load(path)
        result = run_script(_DemoInjectHost(self), parsed)
        self._elf._last_inject = result               # 証跡（rules.report）と rules.last が読む
        self._elf._last_runs = {**self._elf._last_runs, "inject": {"at": time.time(), "caller": caller}}
        return result
