"""Probe の道具: 止める前の手がかりと、走らせたままの見張り（止まらずに回り続けている状態）。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う mixin。Probe の _lock・target・_symbols・_monitor を使う。

- halt_risk: ELF だけで、使う側が halt_risk_set で明示した名前・正規表現に合うシンボルがあるか（fprobe.halt_risk）と、
  ELF に止めずにデバッグするモニタ（target.debug_mode の monitor）があるか。機器には触らない。既定の名前の表は
  持たない（慣習の表はプラグイン fprobe.plugins.heuristics）。
- 走らせたままの見張り: target_status が走っているコアを見るたびに、止めずに読める材料（DHCSR のロックアップ、
  DWT_PCSR の PC の見本を liveness_set の pc_samples 回、liveness_set の tick_variable で名前を渡されたティックの変数）を fprobe.liveness に渡し、事実（ロックアップ・
  liveness_set で明示した関数の中・ティックが変わっていない）があれば状態に fatal を付ける（デーモンの見張りが
  1 秒ごとに読むので、通知 target.state で画面・AI に届く）。PCSR は DEMCR.TRCENA が要るので、無ければ立てる
  （デバッグの登録簿だけ。ターゲットの動きは変えない）。
"""

import time

from .halt_risk import compile_patterns, scan
from .liveness import Liveness
from .profile.dwt import DEMCR, DEMCR_TRCENA, DWT_PCSR

__all__ = ["DHCSR", "SafetyTools"]

DHCSR = 0xE000EDF0
S_LOCKUP = 1 << 19
NOT_CORTEX_M = ("riscv", "armv7-a", "armv6")   # pyOCD を使わない接続の debug_architecture（DHCSR・DWT が無い）


class SafetyTools:
    def _init_safety(self) -> None:
        self._halt_names: list[str] = []                                  # halt_risk_set で渡された条件
        self._halt_patterns: list[str] = []
        self._liveness_settings = {"functions": [], "patterns": [], "pc_samples": None,   # 条件が無ければ PC を読まない
                                   "tick_variable": None}
        self._liveness = Liveness(self._function_name)
        self._tick_symbols: tuple[object, str, int | None] | None = None   # (ELF, 名前, ティックの変数の番地)

    # --- 止める前の手がかり ------------------------------------------------------------------------

    def halt_risk_set(self, names: list[str] | None = None, patterns: list[str] | None = None) -> dict:
        """halt_risk で探す名前（完全一致）・正規表現（re.search）を置き換える（渡さなければ空 = 何も探さない）。"""
        names, patterns = list(names or []), list(patterns or [])
        compile_patterns(patterns)
        self._halt_names, self._halt_patterns = names, patterns
        return {"names": list(names), "patterns": list(patterns)}

    def halt_risk_get(self) -> dict:
        """halt_risk で探す今の条件（names・patterns）。ELF に依らない。"""
        return {"names": list(self._halt_names), "patterns": list(self._halt_patterns)}

    def halt_risk(self) -> dict:
        """止める前の手がかり（ELF だけ。機器には触らない。判定はしない）: names・patterns（halt_risk_set で渡された
        条件）、matched（どれかに合った）、matches（合った条件ごと: query、by = name / pattern、count、symbols =
        [{name, kind, address}] を全部）、monitor_in_elf（止めずにデバッグするモニタがあるか。あれば
        target_debug_mode_set の mode="monitor" へ）。"""
        symbols = self._symbols
        if symbols is None:
            raise RuntimeError("ELF が読み込まれていない（debug_elf_load を先に）")
        return {"elf": str(symbols.path), **scan(symbols.names(), self._halt_names, self._halt_patterns)}

    # --- 走らせたままの見張りの条件 ------------------------------------------------------------------

    def liveness_set(self, functions: list[str] | None = None, patterns: list[str] | None = None,
                     pc_samples: int | None = None, tick_variable: str | None = None) -> dict:
        """走らせたままの見張りの条件を置き換える: PC の見本がその中にあれば fatal（kind = pc_in）にする関数の名前
        （完全一致）・正規表現（渡さなければ空 = PC の場所では知らせず、PC も読まない）、ティックの変数の名前
        tick_variable（前の見張りから変わっていなければ kind = tick_stopped。名前の慣習で探さないので、使う側が渡す。
        渡さなければティックは読まない）、見張りの 1 回に読む PC の見本の数（条件を渡すときは必須。既定は無い）。"""
        functions, patterns = list(functions or []), list(patterns or [])
        if functions or patterns or tick_variable is not None:
            if isinstance(pc_samples, bool) or not isinstance(pc_samples, int) or pc_samples < 1:
                raise ValueError(f"pc_samples（1 以上の整数。見張りの 1 回に読む PC の見本の数）が要る（{pc_samples!r}）")
        else:
            pc_samples = None
        if tick_variable is not None and self._symbols is not None:
            self._symbols.variable(tick_variable)                               # ELF に無ければ KeyError
        live = Liveness(self._function_name, functions, patterns)               # 正規表現が読めなければ ValueError
        with self._lock:
            self._liveness = live
            self._liveness_settings = {"functions": functions, "patterns": patterns, "pc_samples": pc_samples,
                                       "tick_variable": tick_variable}
        return self.liveness_get()

    def liveness_get(self) -> dict:
        """走らせたままの見張りの条件（functions・patterns・pc_samples・tick_variable）。"""
        s = self._liveness_settings
        return {"functions": list(s["functions"]), "patterns": list(s["patterns"]), "pc_samples": s["pc_samples"],
                "tick_variable": s["tick_variable"]}

    # --- 致命的エラーの見張り -----------------------------------------------------------------------

    def _function_name(self, pc: int) -> str | None:
        f = self._symbols.function_at(pc) if self._symbols is not None else None
        return f.name if f is not None else None

    def _tick_address(self) -> int | None:
        """使う側が liveness_set の tick_variable で渡したティックの変数の番地（渡されていない・ELF に無ければ None。
        ELF と名前ごとに 1 回引く）。"""
        symbols, name = self._symbols, self._liveness_settings["tick_variable"]
        if symbols is None or name is None:
            return None
        if self._tick_symbols is None or self._tick_symbols[0] is not symbols or self._tick_symbols[1] != name:
            try:
                address = symbols.variable(name).address
            except KeyError:
                address = None
            self._tick_symbols = (symbols, name, address)
        return self._tick_symbols[2]

    def _fatal_state(self, state: str) -> dict | None:
        """走っているコアが致命的エラーで回り続けていれば、その様子（_lock の中で。target_status から）。読めなければ
        None（見張りは状態の読みを妨げない）。"""
        if state not in ("RUNNING", "LOCKUP") or self._monitor is not None \
                or getattr(self.target, "debug_architecture", None) in NOT_CORTEX_M:     # DHCSR・DWT は Cortex-M だけ
            self._liveness.reset()
            return None
        try:
            target = self.target
            lockup = state == "LOCKUP" or bool(target.read_memory(DHCSR) & S_LOCKUP)
            pcs: list[int | None] = []
            if not lockup and self._liveness_settings["pc_samples"]:
                demcr = target.read_memory(DEMCR)
                if not demcr & DEMCR_TRCENA:
                    target.write_memory(DEMCR, demcr | DEMCR_TRCENA)
                pcs = [target.read_memory(DWT_PCSR) for _ in range(self._liveness_settings["pc_samples"])]
            tick_address = self._tick_address()
            tick = target.read_memory(tick_address) if tick_address is not None and not lockup else None
        except Exception:                                                  # 読めない（切れかけ等）: 決めない
            return None
        return self._liveness.observe(lockup, pcs, tick, time.time())
