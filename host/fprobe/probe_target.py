"""ターゲット（target_*・read_registers・read_memory 等）の mixin: 接続（自動判別・線・マルチドロップ・RISC-V・ARM-A）・
切断・状態・停止 / 再開 / ステップ / リセット・レジスタとメモリの読み書き。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う。Probe の _lock・_serial 等の接続の設定・session・target・dbg・_debug・_link・_symbols と、
他の mixin の _close_* / _init_* を使う。機器に触るのは必ず _lock の中。
"""

from . import fault
from .arm_a_target import arm_a_part, open_arm_a
from .bitstreams import auto_switch_enabled, design_for, protocol_for_target
from .multidrop import resolve_target
from .riscv_target import open_riscv, riscv_part


def _cortex_m(link: dict | None) -> bool:
    """接続中のターゲットが Cortex-M か（DFSR 等の M のレジスタを触ってよいか）。Cortex-A・ARM11・RISC-V は違う。"""
    name = (link or {}).get("target") or ""
    return riscv_part(name) is None and arm_a_part(name) is None


class TargetTools:
    """Probe に混ぜるターゲットの接続・実行制御・レジスタとメモリの道具。"""

    def _init_target(self) -> None:
        self._last_connect: dict | None = None             # 最後の target_connect の頼み（つなぎ直し・system_status）

    def target_connect(self, target: str = "cortex_m", frequency_hz: int = 4_000_000,
                       connect_mode: str = "halt", jtag_chain: list[int] | None = None,
                       script: str | None = None, core: int | None = None) -> dict:
        """ターゲットに接続する。既定は SWD。jtag_chain（JTAG チェーンの IR 長、TDO 側から。例: STM32 は [4, 5]）を
        指定すると JTAG。connect_mode: halt / attach / under-reset。target="auto" なら先に部品を判別して
        pyOCD のターゲット名を選ぶ（判別の結果は auto に。JTAG だけのターゲットなら jtag_chain も）。
        script: 接続・リセット等の手順を上書きする pyOCD のユーザスクリプト（"builtin:<名前>" かファイル。script_check）。
        core: コアごとに DP を持つ複数ドロップの部品（RP2040）のコアの番号（target="rp2040" と組む。fprobe.multidrop）。
        RISC-V（target="rp2350_riscv"。fprobe.riscv_target）ではハートの番号（connect_mode は halt / attach）。
        Cortex-A・ARM11（target="bcm2836" / "cortex_a" / "zynq7000" / "bcm2835" / "arm11"。fprobe.arm_a_target）ではコアの番号
        （JTAG で直接。connect_mode は halt / attach）。
        Primer の構成（probe_design_get）が要る線のものでなければ、書き替えてから接続する（結果の design_switch）。"""
        asked = {"target": target, "frequency_hz": frequency_hz, "connect_mode": connect_mode, "jtag_chain": jtag_chain,
                 "script": script, "core": core}
        switches = []
        auto = None
        if target == "auto":
            target, auto = self._resolve_auto_target(frequency_hz, switches)
            frequency_hz = auto["frequency_hz"]               # 判別で DP が応答した速さ（頼まれた速さ）
            if jtag_chain is None:
                jtag_chain = auto["jtag_chain"]
        if riscv_part(target) is None and arm_a_part(target) is None:
            target, _, core = resolve_target(target, core)
        switch = self._ensure_design(protocol_for_target(target, jtag_chain))
        if switch is not None:
            switches.append(switch)
        status = self._connect_target(target, frequency_hz, connect_mode, jtag_chain, script, core)
        if status.get("state") != "HALTED":
            with self._lock:
                self._clear_halt_reason()          # 前のツール・前の停止が残した DFSR で、次の停止の理由を誤らない
        self._last_connect = asked                 # 同じ設定でつなぎ直す（対象のリセットで SWD がはずれた後。system_status）
        if auto is not None:
            status["auto"] = {**auto, "target": target}
        if switches:
            status["design_switch"] = switches[-1] if len(switches) == 1 else switches
        return status

    def _resolve_auto_target(self, frequency_hz: int, switches: list) -> tuple[str, dict]:
        """target="auto" の判別。今の構成でつながらず、JTAG が無い構成なら、JTAG の構成に書き替えて判別し直す。"""
        with self._lock:
            self.target_disconnect()
            self._close_snapshot()
            caps = self.session.device.capabilities()
            try:
                return self._auto_target(frequency_hz)
            except RuntimeError:
                if caps.jtag or not self._switchable(caps) or not auto_switch_enabled():
                    raise
        switches.append(self._switch_design(caps.design, design_for("jtag")))
        with self._lock:
            return self._auto_target(frequency_hz)

    def _connect_target(self, target: str, frequency_hz: int, connect_mode: str, jtag_chain: list[int] | None,
                        script: str | None, core: int | None = None) -> dict:
        with self._lock:
            self.target_disconnect()
            self._close_snapshot()                         # 実物に繋ぐ: 開いていたスナップショットは閉じる
            options = {}
            script_info = None
            riscv, arm_a = riscv_part(target), arm_a_part(target)
            if riscv is not None and (script is not None or jtag_chain is not None):
                raise ValueError("RISC-V の接続（pyOCD を使わない）に script・jtag_chain は使えない")
            if arm_a is not None and script is not None:
                raise ValueError("Cortex-A・ARM11 の接続（pyOCD を使わない）に script は使えない")
            if script is not None:
                options["user_script"], script_info = self._connect_script(script)
            self._close_lowlevel()                         # 低レベルの道具が開いた DAP の接続口を pyOCD に渡す
            if riscv is not None:                          # pyOCD に RISC-V は無い: DM を直接使う（fprobe.riscv_target）
                debug = open_riscv(self.session.dap_access(), riscv, core or 0, frequency_hz, connect_mode)
            elif arm_a is not None:                        # pyOCD に Cortex-A・ARM11 は無い: JTAG で直接（fprobe.arm_a_target）
                debug = open_arm_a(self.session.dap_access(), arm_a, core or 0, frequency_hz, connect_mode,
                                   jtag_chain)
            else:
                debug = self.session.debug(target_override=target, frequency=frequency_hz, connect_mode=connect_mode,
                                           jtag_chain=jtag_chain, **options)
                debug.open()
            self._debug = debug
            self._target_type = target
            self._link = {"target": target, "frequency_hz": frequency_hz, "wire": protocol_for_target(target, jtag_chain)}
            if riscv is not None:
                self._link.update(debug.info())            # arch・hart・debug_module
                core = debug.target.dm.hart
            elif arm_a is not None:
                self._link.update(debug.info())            # arch・core・cores・debug_base 等
                core = debug.target.index
            if core is not None:
                self._link["core"] = core                  # 複数ドロップの部品のコア・RISC-V のハート（target_status）
            recovered = self._flash_bp_attach()           # 前のプロセスが異常終了して残した Flash の BKPT を戻す
            status = self.target_status()
            if recovered is not None:
                status["flash_breakpoints_recovered"] = recovered
            if script_info is not None:
                status["script"] = script_info
            return status

    def target_disconnect(self) -> None:
        """ターゲットから切断する。"""
        with self._lock:
            self._rtt = None
            self._watch_stops(False)
            if self._debug is not None:
                try:
                    self._monitor_detach()                 # モニタ方式で止まっていれば再開して離す
                    self._flash_bp_detach()                # Flash に書いた BKPT を戻す
                finally:                                   # 片付けが失敗しても接続は必ず閉じる（失敗は呼び手に伝わる）
                    debug, self._debug = self._debug, None
                    self._link = None
                    debug.close()

    def target_status(self) -> dict:
        """コアの状態（RUNNING / HALTED 等）と、停止中なら PC と止まった理由 halt_reason（request = 停止要求・ステップ・
        クロストリガ / breakpoint / bkpt_instruction = ファームウェア自身の BKPT（panic・assert 等）/ watchpoint /
        vector_catch / external。fprobe.fault.halt_reason）と、DFSR の立っていたビット全部 halt_bits（HALTED / BKPT /
        DWTTRAP / VCATCH / EXTERNAL。複数立っていても halt_reason は 1 語）。つないでいる先（target・frequency_hz・wire）も。
        走っているのに致命的エラーで回り続けていれば fatal（fprobe.probe_safety）。複数ドロップの部品（RP2040）なら
        core（接続中のコア）と cores（各コアの状態。fprobe.probe_multicore）。"""
        with self._lock:
            state = self.target.get_state().name
            status = {"state": state, **(self._link or {})}
            if state == "HALTED":
                pc = self.target.read_core_register("pc")
                status["pc"] = pc
                status.update(self._place_of(pc))
                status.update(self._halt_reason(pc))
            if self._monitor is not None:
                status["debug_mode"] = "monitor"              # 止まっているのは DebugMon の中（優先度の高い割り込みは動く）
            fatal = self._fatal_state(state)
            if fatal is not None:
                status["fatal"] = fatal
            return self._multicore_status(status)

    def _halt_reason(self, pc: int) -> dict:
        """DFSR（と BKPT なら PC の命令）から止まった理由（_lock の中で）: halt_reason（1 語）と、DFSR の立っていた
        ビット全部の halt_bits。Cortex-M 以外・読めなければ空。"""
        if not _cortex_m(self._link):
            return {}
        try:
            dfsr = self.target.read32(fault.DFSR)
            instruction = self.target.read16(pc & ~1) if dfsr & 0x02 else None
        except Exception:                                   # noqa: BLE001  理由は状態の添え物。読めなくても状態は返す
            return {}
        dbg = self._dbg                                     # 置いたブレークポイント（無ければまだ置いていない）
        reason = fault.halt_reason(dfsr, instruction, breakpoint=dbg is not None and pc in dbg.breakpoints)
        return {**({"halt_reason": reason} if reason is not None else {}), "halt_bits": fault.dfsr_bits(dfsr)}

    def _clear_halt_reason(self) -> None:
        """DFSR をクリアする（W1C。pyOCD は自分の再開・ステップでだけクリアする）。Cortex-M 以外は何もしない。"""
        if not _cortex_m(self._link):
            return
        try:
            self.target.write32(fault.DFSR, 0x1F)
        except Exception:                                   # noqa: BLE001  理由の表示のための片付け。失敗しても操作は続ける
            pass

    def target_halt(self, core: int | None = None) -> dict:
        """コアを停止する。core: 複数ドロップの部品（RP2040）で接続していないコアを止める（その状態を返す）。"""
        with self._lock:
            if core is not None:
                other = self._other_core(core, "halt")
                if other is not None:
                    return other
            if self.target.get_state().name != "HALTED":
                self._clear_halt_reason()                   # 前の停止の理由（リセットでは消えない）を混ぜない
            self.target.halt()
            self._watch_stops(False)
            return self.target_status()

    def target_resume(self, core: int | None = None) -> dict:
        """コアの実行を再開する（いまの PC にブレークポイントがあれば 1 命令踏み越えてから。同じ場所で止まり直さない）。
        core: 複数ドロップの部品（RP2040）で接続していないコアを再開する（その状態を返す）。"""
        with self._lock:
            if core is not None:
                other = self._other_core(core, "resume")
                if other is not None:
                    return other
            self.dbg.resume()
            self._watch_stops(True)                  # BKPT 0xAB（セミホスティング）で止まったら背景で処理して再開
            return self.target_status()

    def target_step(self, count: int = 1) -> dict:
        """1 命令ずつ count 回ステップ実行する（Cortex-M は、途中でベクタキャッチ・ウォッチポイント等で止まればそこで
        打ち切り、実行した回数を steps_done で返す。止まった理由は halt_reason / halt_bits）。"""
        with self._lock:
            self._watch_stops(False)
            if _cortex_m(self._link):
                done = self.dbg.step_instructions(count)
            else:                                           # DFSR を持たないコア（RISC-V・Cortex-A）は 1 命令ずつ進めるだけ
                for _ in range(count):
                    self.target.step()
                done = count
            return {**self.target_status(), "steps_done": done}

    def target_reset(self, halt: bool = False, kind: str = "default") -> dict:
        """ターゲットをリセットする（halt=True ならリセット直後で停止）。kind: default / system（SYSRESETREQ。
        周辺も）/ core（VECTRESET。コアだけ、ARMv7-M だけ）/ hardware（nRESET の線）/ emulated / rescue（RP2040 の
        救済の DP でチップ全体。つなぎ直して起動させる。halt は使わない。fprobe.multidrop）。"""
        with self._lock:
            if kind == "rescue":
                self._rescue()
                self._watch_stops(True)
                return {**self.target_status(), "kind": kind}
            self._clear_halt_reason()                       # DFSR はリセットで消えない（電源投入だけ）
            self.dbg.reset(kind, halt)
            self._watch_stops(not halt)
            return {**self.target_status(), "kind": kind}

    def read_registers(self, names: list[str] | None = None) -> dict[str, int]:
        """コアレジスタを読む（省略時 r0..r12, sp, lr, pc, xpsr。RISC-V は pc と x1〜x31 の ABI の名前、
        Cortex-A・ARM11 は xpsr の代わりに cpsr）。"""
        with self._lock:
            names = names or getattr(self.target, "default_register_names", None) \
                or [f"r{i}" for i in range(13)] + ["sp", "lr", "pc", "xpsr"]
            return dict(zip(names, self.target.read_core_registers_raw(names)))

    def write_register(self, name: str, value: int) -> None:
        """コアレジスタに書く（停止中）。"""
        with self._lock:
            self.target.write_core_register_raw(name, value)
            self.target.flush()

    def read_memory(self, address: int, length: int) -> str:
        """ターゲットのメモリを読み、16 進文字列（アドレス順のバイト）で返す。"""
        with self._lock:
            return bytes(self.target.read_memory_block8(address, length)).hex()

    def write_memory(self, address: int, data_hex: str) -> None:
        """ターゲットのメモリ（RAM・周辺）に 16 進文字列のバイト列を書く。Flash は flash を使う。"""
        with self._lock:
            self.target.write_memory_block8(address, bytes.fromhex(data_hex))
            self.target.flush()                            # pyOCD は書き込みを溜める。戻る時点で書き終えておく
