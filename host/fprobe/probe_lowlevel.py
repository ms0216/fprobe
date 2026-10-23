"""Probe の道具: 接続の診断と低レベル操作（U29）。J-Link Commander の ReadDP / WriteAP / JTAG の IR・DR / ピン /
TestRSpeed、EmuDiag、BMP の swdp_scan に当たる。

Probe（fprobe.mcp_server）に混ぜて使う mixin。Probe の _lock・session・target・_debug と target_voltage を使う。
機器に触るのは必ず _lock の中。

二つの経路:
- ターゲットに接続中（target_connect の後）: DP / AP は pyOCD の dp を通す（pyOCD が覚えている SELECT と食い違わない）。
  ピンは読むだけ。線を直接動かす道具（JTAG・ピンの書き込み・診断）は断る（pyOCD の接続を壊すため）。
- 未接続: Session.dap_access で CMSIS-DAP の層だけを開き（RawDap）、線を直接動かす。target_connect の前に閉じる。
  DP / AP は、診断の後ならその線と速度、そうでなければ SWD 1 MHz で DP を起こして使う。

fprobe の DAP_SWJ_Pins が返すのは SWCLK/TCK・SWDIO/TMS・nRESET だけ（TDI・TDO・nTRST は線が無いか返さない）。
"""

import time

from pyocd.core.target import Target
from pyocd.probe.cmsis_dap_probe import CMSISDAPProbe
from pyocd.probe.debug_probe import DebugProbe

from . import diagnose as diag
from . import generated as g
from .aux_mcu import Aux
from .jtag_chain import bits_from_text, clock, dap_link_wire, reset_tap, scan_chain, shift, shift_device
from .raw_dap import DapError, RawDap, dp_address
from .stm32_option_bytes import protection_status

__all__ = ["LowLevelTools", "PIN_BITS", "SPEED_TEST_LIMIT"]

PIN_BITS = {"swclk": 0, "swdio": 1, "nreset": 7}              # CMSIS-DAP の DAP_SWJ_Pins の並び
NOT_REPORTED = ["tdi", "tdo", "ntrst"]
SPEED_TEST_LIMIT = 16 << 20
DEFAULT_HZ = 1_000_000


def jtag_half_period(sys_clock_hz: int, requested_hz: float) -> int:
    """要求した速さ（DAP_SWJ_Clock）で jtag_phy が使う半周期（sys の周期数）。dap_cmd_config.vhd と同じ整数の計算
    （fprobe.sampler.swclk_hz と同じ）。JTAG の 1 ビットは 2 × 半周期 + 1 周期（High 相の後に 1 周期の区切り）。"""
    tolerated = sys_clock_hz // 2 // 101 * 100
    return min(max(-(-tolerated // max(1, int(requested_hz))), 2), 65535)


class NotDisconnected(RuntimeError):
    pass


class LowLevelTools:
    """接続の診断と低レベル操作（Probe の mixin）。"""

    def _init_lowlevel(self) -> None:
        self._raw_link = None                       # Session.dap_access（未接続のときだけ）
        self._raw_dap: RawDap | None = None
        self._raw_dp_ready = False                  # DPIDR を読み、デバッグ電源を上げた
        self._jtag_scan = None                      # 最後の jtag_scan（ChainScan）

    def _close_lowlevel(self) -> None:
        if self._raw_link is not None:
            link, self._raw_link, self._raw_dap = self._raw_link, None, None
            self._raw_dp_ready = False
            try:
                if link.get_swj_mode() is not None:
                    link.disconnect()
            finally:
                link.close()

    def _require_disconnected(self, what: str) -> None:
        if self._debug is not None:
            raise NotDisconnected(f"{what}はターゲットに接続中は使えない（pyOCD の接続を壊す）: target_disconnect してから")

    def _raw(self) -> RawDap:
        """未接続の CMSIS-DAP の層（開いていなければ開く）。"""
        self._require_disconnected("線を直接動かす操作")
        if self._raw_dap is None:
            self._raw_link = self.session.dap_access()
            self._raw_dap = RawDap(self._raw_link)
        return self._raw_dap

    def _raw_started(self, protocol: str = "swd", frequency_hz: int = DEFAULT_HZ) -> RawDap:
        dap = self._raw()
        if dap.protocol is None:
            dap.start(protocol, frequency_hz)
        return dap

    def _raw_dp(self) -> RawDap:
        """DP を使える状態の RawDap（SWD 1 MHz で起こす。診断の後はその線と速度のまま）。"""
        dap = self._raw()
        if not self._raw_dp_ready:
            if dap.protocol != "swd":
                dap.start("swd", DEFAULT_HZ)
            dap.switch_to_swd()
            try:
                dap.read_dpidr()
            except DapError:
                dap.switch_to_swd(dormant=True)
                dap.read_dpidr()
            dap.power_up()
            self._raw_dp_ready = True
        return dap

    # --- ピン --------------------------------------------------------------------------------------

    def pins_read(self) -> dict:
        """デバッグコネクタのピンの状態（SWCLK/TCK・SWDIO/TMS・nRESET）。変えない。"""
        with self._lock:
            if self._debug is not None:                                 # 接続中: pyOCD のプローブの口で
                pins = self._debug.probe.read_pins(DebugProbe.PinGroup.PROTOCOL_PINS, DebugProbe.ProtocolPin.ALL_PINS)
                value = CMSISDAPProbe.to_cmsis_dap_pins(pins)
            else:
                value = self._raw_started().link.pin_access(0, 0)
            return self._pins_dict(value)

    @staticmethod
    def _pins_dict(value: int) -> dict:
        return {**{name: (value >> bit) & 1 for name, bit in PIN_BITS.items()}, "not_reported": NOT_REPORTED}

    def pins_write(self, swclk: int | None = None, swdio: int | None = None, nreset: int | None = None,
                   hold_us: int = 0) -> dict:
        """ピンを個別に High（1）/ Low（0）にする（None は変えない）。未接続のときだけ。hold_us があればその後で
        元の値に戻す（例 nreset=0, hold_us=1000 で 1 ms のリセットの脈）。書いた後の状態を返す。"""
        wanted = {"swclk": swclk, "swdio": swdio, "nreset": nreset}
        if any(v not in (None, 0, 1) for v in wanted.values()):
            raise ValueError("ピンの値は 0 か 1（変えないなら省く）")
        mask = sum(1 << PIN_BITS[n] for n, v in wanted.items() if v is not None)
        if not mask:
            raise ValueError("変えるピンが無い（swclk / swdio / nreset）")
        value = sum(v << PIN_BITS[n] for n, v in wanted.items() if v)
        with self._lock:
            link = self._raw_started().link
            before = link.pin_access(0, 0)
            link.pin_access(mask, value)
            settle_us = None
            if hold_us:
                time.sleep(hold_us / 1e6)
                link.pin_access(mask, before & mask)
                # 戻した線が戻るまで待つ: nRESET はオープンドレインで、ターゲットのコンデンサ（Nucleo の NRST は
                # 100 nF）があると立ち上がりに時間がかかる（実機で、戻した直後に読むとまだ 0 だった。2026-10-18）
                started = time.perf_counter()
                while (link.pin_access(0, 0) ^ before) & mask and time.perf_counter() - started < 0.1:
                    time.sleep(0.0005)
                settle_us = round((time.perf_counter() - started) * 1e6)
            out = self._pins_dict(link.pin_access(0, 0))
            if settle_us is not None:
                out["settle_us"] = settle_us             # 元の値に戻るまでの時間（0.1 秒で打ち切り）
            return out

    # --- DP / AP ----------------------------------------------------------------------------------

    def dp_read(self, register: int | str) -> dict:
        """DP のレジスタを読む（名前 DPIDR / CTRL/STAT / SELECT / RDBUFF / TARGETID / DLPIDR / EVENTSTAT / BASEPTR0、
        または番地。bit 7:4 が DPBANKSEL、例 0x24 = TARGETID）。"""
        address = dp_address(register)
        with self._lock:
            if self._debug is not None:
                value = self._debug.target.dp.read_dp(address)
            else:
                value = self._raw_dp().read_dp(address)
            return {"register": address, "value": value, "path": self._path()}

    def dp_write(self, register: int | str, value: int) -> dict:
        """DP のレジスタに書く（ABORT = 0x0 への書き込みで誤りの印を消す、等）。"""
        address = dp_address(register)
        with self._lock:
            if self._debug is not None:
                self._debug.target.dp.write_dp(address, value)
            else:
                self._raw_dp().write_dp(address, value)
            return {"register": address, "value": value, "path": self._path()}

    def ap_read(self, ap: int, register: int) -> dict:
        """AP のレジスタを読む。ap は ADIv5 なら APSEL（0〜255）、ADIv6 なら AP の番地。register は AP の中の番地
        （例 0xFC = IDR、ADIv6 の MEM-AP は 0xDFC）。"""
        with self._lock:
            if self._debug is not None:
                value = self._debug.target.dp.read_ap(self._pyocd_ap_address(ap, register))
            else:
                value = self._raw_dp().read_ap(ap, register)
            return {"ap": ap, "register": register, "value": value, "path": self._path()}

    def ap_write(self, ap: int, register: int, value: int) -> dict:
        """AP のレジスタに書く（ap・register は ap_read と同じ）。"""
        with self._lock:
            if self._debug is not None:
                self._debug.target.dp.write_ap(self._pyocd_ap_address(ap, register), value)
                self._debug.target.dp.flush()
            else:
                self._raw_dp().write_ap(ap, register, value)
            return {"ap": ap, "register": register, "value": value, "path": self._path()}

    def _pyocd_ap_address(self, ap: int, register: int) -> int:
        from pyocd.coresight.dap import ADIVersion
        if register & 3:
            raise ValueError("AP のレジスタ番地は 4 の倍数")
        if self._debug.target.dp.adi_version == ADIVersion.ADIv6:
            return ap + register
        if not 0 <= ap <= 0xFF or not 0 <= register <= 0xFC:
            raise ValueError("ADIv5 の AP は APSEL 0〜255、レジスタ番地 0〜0xFC")
        return ap << 24 | register

    def _path(self) -> str:
        if self._debug is not None:
            return "pyocd"
        return f"raw {self._raw_dap.protocol} {self._raw_dap.frequency_hz} Hz"

    # --- JTAG -------------------------------------------------------------------------------------

    def _jtag_wire(self, frequency_hz: int | None = None):
        dap = self._raw()
        if dap.protocol != "jtag" or (frequency_hz and frequency_hz != dap.frequency_hz):
            dap.start("jtag", frequency_hz or dap.frequency_hz or DEFAULT_HZ)
            dap.switch_to_jtag()
            self._raw_dp_ready = False
        return dap_link_wire(dap.link)

    def jtag_scan(self, frequency_hz: int = DEFAULT_HZ, max_devices: int = 16) -> dict:
        """JTAG のチェーンを走査する（未接続のときだけ）: 装置の数・IDCODE・IR 長（表・Capture-IR から。一つに決まらなければ ambiguous と候補）。
        返る jtag_chain は target_connect にそのまま渡せる。TAP はリセットして IDCODE を選んだ状態で終える。
        Primer の構成に JTAG が無ければ書き替えてから（probe_design_get）。"""
        self._ensure_design("jtag")
        with self._lock:
            scan = scan_chain(self._jtag_wire(frequency_hz), max_devices=max_devices)
            self._jtag_scan = scan
            return scan.as_dict()

    # --- TDO の取り込みの遅れ（R16。docs/superpowers/specs/2026-10-23-jtag-tdo-sample-design.md）------------------

    def _write_jtag_delay(self, cycles: int) -> int:
        """CSR debug.sample_delay の jtag の欄だけを書き、読み返した値を返す（_lock の中で。古い回路は欄が無く 0 が返る）。"""
        dev = self.session.device
        shift_, width = g.DEBUG_SAMPLE_DELAY_JTAG
        mask = ((1 << width) - 1) << shift_
        dev.write32(g.DEBUG_SAMPLE_DELAY, dev.read32(g.DEBUG_SAMPLE_DELAY) & ~mask | cycles << shift_)
        return (dev.read32(g.DEBUG_SAMPLE_DELAY) & mask) >> shift_

    def jtag_sample_delay_set(self, cycles: int) -> dict:
        """JTAG の TDO の取り込みを、次の TCK の立ち下がりの直前から cycles 周期（sys、0〜7）遅らせる。
        TDO が立ち下がりから 2 × half_period 周期（sys）より遅れて届くとき（Pi 2B の 14.3 MHz 以上で見た。1 ビットずれて読める）に使う。
        どの値がよいかは jtag_sample_delay_scan の事実で決める。readback = 読み返した値（古い回路は欄が無く 0）。
        Primer の構成に JTAG が無ければ書き替えてから書く。値は構成を書き替える（FPGA を書き直す）と 0 に戻る。"""
        if not 0 <= cycles <= 7:
            raise ValueError(f"cycles は 0〜7（{cycles}）")
        self._ensure_design("jtag")                     # 先に JTAG の構成へ（後で書き替えると 0 に戻る）
        with self._lock:
            return {"cycles": cycles, "readback": self._write_jtag_delay(cycles)}

    def jtag_sample_delay_scan(self, frequency_hz: int, expected_idcodes: list[int], reads: int) -> dict:
        """TCK frequency_hz で、取り込みの遅れ 0〜7 のそれぞれについて、チェーンの IDCODE（TAP をリセットして DR を
        32 × len(expected_idcodes) ビット）を reads 回読み、expected_idcodes（TDO 側から。呼び手が明示）と一致した回数
        matches と、見えた値の並び observed（値の組と回数）を返す。applied = 遅れの欄を書いて読み返せた。
        どれを使うかは決めない（終わったら元の遅れに戻す。使うには jtag_sample_delay_set）。未接続のときだけ。
        実際の TCK: half_period（SWJ_Clock と同じ整数の計算）と tck_hz = sys / (2 × half_period + 1)。"""
        if not expected_idcodes or reads < 1:
            raise ValueError("expected_idcodes は 1 つ以上、reads は 1 以上")
        self._ensure_design("jtag")
        n = len(expected_idcodes)
        expected = [v & 0xFFFFFFFF for v in expected_idcodes]
        with self._lock:
            self._require_disconnected("TDO の取り込みの遅れの走査")      # 遅れの欄を書く前に（接続中の転送を乱さない）
            original = (self.session.device.read32(g.DEBUG_SAMPLE_DELAY) >> g.DEBUG_SAMPLE_DELAY_JTAG[0]
                        & (1 << g.DEBUG_SAMPLE_DELAY_JTAG[1]) - 1)
            rows = []
            try:
                for delay in range(8):
                    applied = self._write_jtag_delay(delay) == delay
                    wire = self._jtag_wire(frequency_hz)
                    seen: dict[tuple[int, ...], int] = {}
                    for _ in range(reads):
                        reset_tap(wire)
                        value = shift(wire, ir=False, value=(1 << 32 * n) - 1, length=32 * n)
                        words = tuple(value >> 32 * k & 0xFFFFFFFF for k in range(n))
                        seen[words] = seen.get(words, 0) + 1
                    rows.append({"delay": delay, "applied": applied, "reads": reads,
                                 "matches": seen.get(tuple(expected), 0),
                                 "observed": [{"idcodes": list(k), "count": c} for k, c in seen.items()]})
            finally:
                self._write_jtag_delay(original)
            sys_hz = self.session.device.capabilities().sys_clock_hz
            half = jtag_half_period(sys_hz, frequency_hz)
            return {"frequency_hz": frequency_hz, "half_period": half, "tck_hz": sys_hz / (2 * half + 1),
                    "expected_idcodes": expected, "delays": rows, "restored_delay": original}

    def jtag_reset(self) -> dict:
        """すべての TAP を Test-Logic-Reset に（TMS = 1 を 6 回）して Run-Test/Idle へ。"""
        self._ensure_design("jtag")
        with self._lock:
            reset_tap(self._jtag_wire())
            return {"state": "run-test/idle"}

    def jtag_shift(self, device: int = 0, ir: int | None = None, dr_value: int = 0, dr_length: int = 0,
                   chain: list[int] | None = None) -> dict:
        """チェーンの device 番目に IR を入れ（ほかは BYPASS）、DR を dr_length ビット シフトする（ほかは 1 ビット）。
        chain は IR 長の列（TDO 側から。省略時は最後の jtag_scan）。取り出した IR・DR を返す。"""
        self._ensure_design("jtag")
        with self._lock:
            if chain is None:
                if self._jtag_scan is None or self._jtag_scan.chain is None:
                    raise ValueError("chain（IR 長の列）が要る: 先に jtag_scan するか chain を与える")
                chain = self._jtag_scan.chain
            out = shift_device(self._jtag_wire(), list(chain), device, ir, dr_value, dr_length)
            return {"device": device, "chain": list(chain), **out}

    def jtag_clock(self, tms: str, tdi: str | None = None) -> dict:
        """生のビット列を TCK ごとに出す（時間順の "0" / "1" の文字列。tdi 省略時は 0）。TDO を同じ形で返す。
        TAP の状態は呼び手が管理する（ほかの JTAG の道具は Run-Test/Idle を前提にする）。"""
        tms_bits = bits_from_text(tms)
        tdi_bits = bits_from_text(tdi) if tdi is not None else [0] * len(tms_bits)
        self._ensure_design("jtag")
        with self._lock:
            tdo = clock(self._jtag_wire(), tms_bits, tdi_bits)
            return {"tdo": "".join(map(str, tdo)), "clocks": len(tdo)}

    # --- 速度 -------------------------------------------------------------------------------------

    def target_speed_test(self, address: int, length: int = 65536, write: bool = False) -> dict:
        """ターゲットのメモリの読み（と write なら書き）の速度を測る。書きは RAM の番地で、コアを止めてから
        （試験の模様を書いて読み比べ、元の中身を書き戻す）。"""
        if address % 4 or length % 4 or not 0 < length <= SPEED_TEST_LIMIT:
            raise ValueError(f"address と length は 4 の倍数、length は 4〜{SPEED_TEST_LIMIT}")
        words = length // 4
        with self._lock:
            target = self.target
            t0 = time.perf_counter()
            original = target.read_memory_block32(address, words)
            out = {"address": address, "bytes": length, "read": _rate(length, time.perf_counter() - t0),
                   "frequency_hz": self._swd_frequency()}
            if write:
                if target.get_state() != Target.State.HALTED:
                    raise RuntimeError("書き込みの速度試験はコアを止めてから（RAM を一時的に書き換える）: target_halt")
                pattern = [(i * 0x9E3779B1 ^ 0xA5A5A5A5) & 0xFFFFFFFF for i in range(words)]
                t0 = time.perf_counter()
                target.write_memory_block32(address, pattern)
                target.flush()
                elapsed = time.perf_counter() - t0
                verified = list(target.read_memory_block32(address, words)) == pattern
                target.write_memory_block32(address, list(original))
                target.flush()
                out["write"] = {**_rate(length, elapsed), "verified": verified,
                                "restored": list(target.read_memory_block32(address, words)) == list(original)}
            return out

    def _swd_frequency(self) -> int | None:
        try:
            return int(self._debug.probe.session.options.get("frequency"))
        except Exception:
            return None

    # --- 診断 -------------------------------------------------------------------------------------

    def target_diagnose(self, frequency_hz: int = DEFAULT_HZ, max_aps: int = 8, jtag: bool = True) -> dict:
        """ターゲットにつながらない理由を順に調べる（未接続のときだけ）: 電圧 → ピン → SWD（切り替え・休眠・
        複数ドロップ・速度を下げて）→ JTAG のチェーン → DP（電源の要求）→ AP・ROM テーブル → コア → 部品の判別 → 保護。
        verdict（結論）・段ごとの status と message・suggested_connect（target_connect の引数）を返す。"""
        with self._lock:
            d = self._diagnose(frequency_hz, max_aps, jtag)
            return d.as_dict()

    def _diagnose(self, frequency_hz: int, max_aps: int, jtag: bool, measure: bool = True) -> diag.Diagnosis:
        self._require_disconnected("接続の診断")
        jtag = jtag and self.session.device.capabilities().jtag      # JTAG の無い構成（標準）では JTAG を調べない
        dap = self._raw()
        self._raw_dp_ready = False
        voltage = self._voltage_reading if measure else None
        pins = (lambda: self._raw_started().link.pin_access(0, 0)) if measure else None
        d = diag.diagnose(dap, frequency_hz, voltage=voltage, pins=pins, protection=protection_status,
                          max_aps=max_aps, jtag=jtag)
        self._raw_dp_ready = d.connect is not None             # 以後の dp_read 等はこの線と速度で
        if d.jtag is not None:
            self._jtag_scan = d.jtag
        return d

    def _voltage_reading(self) -> dict:
        r = Aux(self.session.device).read()
        return {"volts": r.target_volts, "over_range": r.over_range, "live": r.live, "samples": r.samples,
                "errors": r.errors}

    def _auto_target(self, frequency_hz: int) -> tuple[str, dict]:
        """target_connect の target="auto": 診断（電圧・ピンを除く）で部品を判別し、pyOCD のターゲット名を選ぶ。"""
        d = self._diagnose(frequency_hz, 8, True, measure=False)
        self._close_lowlevel()
        if d.connect is None:
            raise RuntimeError(f"ターゲットを判別できない: {d.verdict()}（target_diagnose で詳しく）")
        info = {"verdict": d.verdict(), "part": d.part.as_dict() if d.part else None,
                "core": d.core["core"] if d.core else None, "suggested_connect": d.connect}
        return d.connect["target"], info


def _rate(length: int, seconds: float) -> dict:
    return {"seconds": seconds, "bytes_per_s": length / seconds if seconds > 0 else None}
