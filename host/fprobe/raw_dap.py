"""DP / AP を直接読み書きする（U29）。pyOCD の DAPAccessCMSISDAP（CMSIS-DAP のコマンドの層）の上で、pyOCD の
ターゲットを作らずに動く: 接続できないターゲットを調べる・DP / AP のレジスタを直接触るため。

- 線の選択: SWD（JTAG→SWD の切り替え、休眠からの起こし、複数ドロップの TARGETSEL）と JTAG。
- DP: 番地の bit 7:4 が DPBANKSEL（例 0x24 = TARGETID、0x34 = DLPIDR）。SELECT は覚えておき、変わるときだけ書く。
- AP: ADIv5 は APSEL（0〜255）と AP のレジスタ番地（0x00〜0xFC）。ADIv6 は AP の番地（BASEPTR から）とレジスタ番地。
- MEM-AP の 32 ビット読み書き（CSW・TAR・DRW。ADIv6 は 0xD00 からの並び）。
- 応答の誤りは DapError（ack = wait / fault / no_ack / protocol_error）。pyOCD の例外を言葉の付いた種類に直す。

pyOCD のターゲットが接続中は使わない（同じ DP の SELECT を pyOCD が覚えているため。接続中は pyOCD の dp を使う）。
例外は複数ドロップの別の DP（fprobe.multidrop）: pyOCD の DP には DPIDR の読み出し（選び直しの後に必須）しかしない。
"""

import time
from enum import StrEnum

from pyocd.probe.pydapaccess.dap_access_api import DAPAccessIntf

__all__ = ["Ack", "DP_ABORT", "DP_CTRL_STAT", "DP_DPIDR", "DP_DLPIDR", "DP_NAMES", "DP_RDBUFF", "DP_SELECT",
           "DP_TARGETID", "DapError", "DpPort", "RawDap", "ack_of", "dp_address"]

DP_DPIDR, DP_ABORT, DP_CTRL_STAT, DP_SELECT, DP_RDBUFF = 0x0, 0x0, 0x4, 0x8, 0xC
DP_TARGETID, DP_DLPIDR, DP_EVENTSTAT = 0x24, 0x34, 0x44
DP_BASEPTR0 = 0x20                                     # ADIv6（DPv3）
DP_NAMES = {"DPIDR": DP_DPIDR, "ABORT": DP_ABORT, "CTRL/STAT": DP_CTRL_STAT, "CTRL_STAT": DP_CTRL_STAT,
            "SELECT": DP_SELECT, "RDBUFF": DP_RDBUFF, "TARGETID": DP_TARGETID, "DLPIDR": DP_DLPIDR,
            "EVENTSTAT": DP_EVENTSTAT, "BASEPTR0": DP_BASEPTR0}
_DP_REG = [DAPAccessIntf.REG.DP_0x0, DAPAccessIntf.REG.DP_0x4, DAPAccessIntf.REG.DP_0x8, DAPAccessIntf.REG.DP_0xC]
_AP_REG = [DAPAccessIntf.REG.AP_0x0, DAPAccessIntf.REG.AP_0x4, DAPAccessIntf.REG.AP_0x8, DAPAccessIntf.REG.AP_0xC]

CSYSPWRUPACK, CSYSPWRUPREQ, CDBGPWRUPACK, CDBGPWRUPREQ = 1 << 31, 1 << 30, 1 << 29, 1 << 28
STICKY_ERRORS = 0x1E                                   # ABORT: ORUNERRCLR | WDERRCLR | STKERRCLR | STKCMPCLR
_JTAG_STICKY = (1 << 1) | (1 << 4) | (1 << 5) | (1 << 7)   # JTAG-DP の CTRL/STAT（書いて 1 で消す）

# MEM-AP のレジスタ（ADIv5 の番地。ADIv6 は + 0xD00）
AP_CSW, AP_TAR, AP_DRW, AP_CFG, AP_BASE, AP_IDR = 0x00, 0x04, 0x0C, 0xF4, 0xF8, 0xFC
CSW_SIZE32 = 0x2
CSW_DEVICE_EN = 1 << 6
_CSW_KEEP = 0xFF000000 | (1 << 23)                     # Prot・SPIDEN 等はターゲットの既定を保つ


class Ack(StrEnum):
    OK = "ok"
    WAIT = "wait"
    FAULT = "fault"
    NO_ACK = "no_ack"
    PROTOCOL = "protocol_error"


_ACK_TEXT = {Ack.WAIT: "WAIT が続いた（ターゲットが応答できない: 低消費電力・クロック停止・バスが塞がっている）",
             Ack.FAULT: "FAULT（前の誤りの印が残っている・アクセスが拒まれた）",
             Ack.NO_ACK: "応答が無い（配線・電源・速度・線のプロトコルの選択）",
             Ack.PROTOCOL: "パリティ誤り・プロトコル誤り（信号品質: 速度を下げる・GND・配線長）"}


def ack_of(error: BaseException) -> Ack:
    if isinstance(error, DAPAccessIntf.TransferFaultError):
        return Ack.FAULT
    if isinstance(error, DAPAccessIntf.TransferTimeoutError):
        return Ack.WAIT
    if isinstance(error, DAPAccessIntf.TransferProtocolError):
        return Ack.PROTOCOL
    return Ack.NO_ACK


class DapError(RuntimeError):
    def __init__(self, ack: Ack, what: str):
        super().__init__(f"{what}: {_ACK_TEXT.get(ack, ack)}")
        self.ack = ack
        self.what = what


def dp_address(register: int | str) -> int:
    """DP のレジスタを番地に（名前 "DPIDR" "CTRL/STAT" "TARGETID" 等か、番地。bit 7:4 は DPBANKSEL）。"""
    if isinstance(register, str):
        name = register.strip().upper()
        if name in DP_NAMES:
            return DP_NAMES[name]
        try:
            register = int(name, 0)
        except ValueError:
            raise ValueError(f"DP のレジスタ {register!r} を知らない（{', '.join(DP_NAMES)} か番地）") from None
    if register & 3 or not 0 <= register <= 0xFC:
        raise ValueError(f"DP の番地 0x{register:X} が不正（4 の倍数、0〜0xFC。bit 7:4 が DPBANKSEL）")
    return register


class RawDap:
    """CMSIS-DAP の層（link = DAPAccessCMSISDAP、open 済み）の上の DP / AP。start で線を選ぶ。"""

    def __init__(self, link):
        self.link = link
        self.protocol: str | None = None
        self.dap_index = 0
        self.adi_v6 = False
        self._select: int | None = None                 # 最後に書いた SELECT（不明なら None）
        self._csw_set: set[int] = set()                 # CSW を 32 ビット・自動増加なしにした AP
        self.frequency_hz: int | None = None

    @classmethod
    def attached(cls, link, protocol: str, frequency_hz: int | None = None) -> "RawDap":
        """既に DAP_Connect 済みの link（pyOCD が接続中の接続口）に乗る。線の選択・速度は変えない。"""
        dap = cls(link)
        dap.protocol, dap.frequency_hz = protocol, frequency_hz
        return dap

    # --- 線 --------------------------------------------------------------------------------------

    def start(self, protocol: str, frequency_hz: int, jtag_chain: list[int] | None = None, dap_index: int = 0) -> None:
        """CMSIS-DAP の DAP_Connect（swd / jtag）と速度。jtag なら IR 長の列（TDO 側から）と DP の位置 dap_index。"""
        if self.protocol is not None:
            self.link.disconnect()
        port = {"swd": DAPAccessIntf.PORT.SWD, "jtag": DAPAccessIntf.PORT.JTAG}[protocol]
        self.link.connect(port)
        self.link.set_clock(frequency_hz)
        if protocol == "jtag":
            self.link.configure_jtag(list(jtag_chain or [4]))
        self.protocol, self.frequency_hz, self.dap_index = protocol, frequency_hz, dap_index
        self._forget()

    def set_clock(self, frequency_hz: int) -> None:
        self.link.set_clock(frequency_hz)
        self.frequency_hz = frequency_hz

    def stop(self) -> None:
        if self.protocol is not None:
            self.link.disconnect()
            self.protocol = None

    def line_reset(self) -> None:
        self.link.swj_sequence(51, (1 << 51) - 1)
        self._forget()

    def switch_to_swd(self, dormant: bool = False) -> None:
        """SWD を選ぶ: 線のリセット → JTAG→SWD（0xE79E）か、休眠（dormant）を経由した起こし → 線のリセット → 休み。"""
        self.line_reset()
        if dormant:
            self.link.swj_sequence(39, 0x33BBBBBA)                                   # JTAG → 休眠
            self.link.swj_sequence(136, 0x19BC0EA2E3DDAFE986852D956209F392FF)        # 選択の合図
            self.link.swj_sequence(12, 0x01A0)                                       # SWD の起動コード
        else:
            self.link.swj_sequence(16, 0xE79E)
        self.line_reset()
        self.link.swj_sequence(8, 0)

    def switch_to_jtag(self) -> None:
        """SWD→JTAG（0xE73C）と TAP のリセット（TMS = 1 を 8 回）。"""
        self.line_reset()
        self.link.swj_sequence(16, 0xE73C)
        self.link.swj_sequence(8, 0xFF)

    def targetsel(self, value: int) -> None:
        """複数ドロップの SWD（DPv2）で DP を選ぶ: 線のリセットの後の TARGETSEL の書き込み（応答は無い）。"""
        parity = bin(value & 0xFFFFFFFF).count("1") & 1
        self.link.swd_sequence([(8, 0x99), (5,), (33, (value & 0xFFFFFFFF) | parity << 32), (2, 0)])
        self._forget()

    def select_drop(self, targetsel: int) -> int:
        """複数ドロップの SWD で DP を選び直す: 線のリセット → 休み 2 周期 → TARGETSEL → DPIDR（必須）。DPIDR を返す。
        選ばれていた DP のレジスタ（SELECT・CTRL/STAT）は DP ごとに保たれる。"""
        self.line_reset()
        self.link.swj_sequence(2, 0)
        self.targetsel(targetsel)
        return self.read_dpidr()

    # --- DP ---------------------------------------------------------------------------------------

    def _xfer(self, what: str, fn):
        try:
            value = fn()
            self.link.flush()
            return value
        except DAPAccessIntf.TransferError as e:
            raise DapError(ack_of(e), what) from e

    def _dp_bank(self, address: int) -> int:
        bank, reg = (address >> 4) & 0xF, address & 0xC
        banked = reg == 0x4 or (self.adi_v6 and reg == 0x0)
        if bank and not banked:
            raise ValueError(f"DP の番地 0x{address:X}: DPBANKSEL があるのは 0x4（ADIv6 は 0x0 も）だけ")
        if banked and (self._select is None or self._select & 0xF != bank):
            self._write_select(((self._select or 0) & ~0xF) | bank)
        return reg

    def _forget(self) -> None:
        self._select = None
        self._csw_set.clear()

    def _write_select(self, value: int) -> None:
        self._xfer("SELECT の書き込み", lambda: self.link.write_reg(_DP_REG[2], value, dap_index=self.dap_index))
        self._select = value

    def read_dpidr(self) -> int:
        """DPIDR を読み（線のリセットの後に必須）、ADIv6（DPv3）かを覚える。"""
        value = self._xfer("DPIDR の読み出し", lambda: self.link.read_reg(_DP_REG[0], dap_index=self.dap_index))
        self.adi_v6 = (value >> 12) & 0xF >= 3
        return value

    def read_dp(self, register: int | str) -> int:
        address = dp_address(register)
        if address == DP_DPIDR:
            return self.read_dpidr()
        reg = self._dp_bank(address)
        return self._xfer(f"DP 0x{address:02X} の読み出し",
                          lambda: self.link.read_reg(_DP_REG[reg >> 2], dap_index=self.dap_index))

    def write_dp(self, register: int | str, value: int) -> None:
        address = dp_address(register)
        if address == DP_SELECT:
            self._write_select(value)
            return
        reg = address if address == DP_ABORT else self._dp_bank(address)
        self._xfer(f"DP 0x{address:02X} の書き込み",
                   lambda: self.link.write_reg(_DP_REG[reg >> 2], value & 0xFFFFFFFF, dap_index=self.dap_index))

    def clear_errors(self) -> None:
        """誤りの印（STICKYERR 等）を消す。SWD は ABORT、JTAG は CTRL/STAT に 1 を書く。"""
        if self.protocol == "jtag":
            ctrl = self.read_dp(DP_CTRL_STAT)
            self.write_dp(DP_CTRL_STAT, ctrl | _JTAG_STICKY)
        else:
            self.write_dp(DP_ABORT, STICKY_ERRORS)

    def power_up(self, timeout_s: float = 0.1) -> dict:
        """CTRL/STAT でデバッグとシステムの電源を求め、応答（ACK）を待つ。"""
        self.write_dp(DP_CTRL_STAT, CSYSPWRUPREQ | CDBGPWRUPREQ)
        deadline = time.monotonic() + timeout_s
        while True:
            ctrl = self.read_dp(DP_CTRL_STAT)
            ok = ctrl & (CSYSPWRUPACK | CDBGPWRUPACK) == CSYSPWRUPACK | CDBGPWRUPACK
            if ok or time.monotonic() >= deadline:
                return {"ctrl_stat": ctrl, "debug_power": bool(ctrl & CDBGPWRUPACK),
                        "system_power": bool(ctrl & CSYSPWRUPACK)}

    # --- AP ---------------------------------------------------------------------------------------

    def _select_ap(self, ap: int, register: int) -> int:
        if register & 3 or register < 0:
            raise ValueError(f"AP のレジスタ番地 0x{register:X} は 4 の倍数")
        if self.adi_v6:
            if register > 0xFFC:
                raise ValueError("ADIv6 の AP のレジスタ番地は 0〜0xFFC")
            select = ((ap + register) & 0xFFFFFFF0) | ((self._select or 0) & 0xF)
        else:
            if not 0 <= ap <= 0xFF or register > 0xFC:
                raise ValueError("ADIv5 の AP は APSEL 0〜255、レジスタ番地 0〜0xFC")
            select = ap << 24 | (register & 0xF0) | ((self._select or 0) & 0xF)
        if select != self._select:
            self._write_select(select)
        return (register & 0xC) >> 2

    def read_ap(self, ap: int, register: int) -> int:
        reg = self._select_ap(ap, register)
        return self._xfer(f"AP 0x{ap:X} の 0x{register:02X} の読み出し",
                          lambda: self.link.read_reg(_AP_REG[reg], dap_index=self.dap_index))

    def write_ap(self, ap: int, register: int, value: int) -> None:
        reg = self._select_ap(ap, register)
        self._xfer(f"AP 0x{ap:X} の 0x{register:02X} の書き込み",
                   lambda: self.link.write_reg(_AP_REG[reg], value & 0xFFFFFFFF, dap_index=self.dap_index))

    # --- MEM-AP -----------------------------------------------------------------------------------

    def mem_register(self, register: int) -> int:
        """MEM-AP のレジスタ番地（ADIv5 の番地を ADIv6 の並びに直す）。"""
        return register + 0xD00 if self.adi_v6 else register

    def _prepare(self, ap: int, address: int) -> None:
        if ap not in self._csw_set:                              # 32 ビット・番地の自動増加なし（Prot 等は保つ）
            csw = self.read_ap(ap, self.mem_register(AP_CSW))
            self.write_ap(ap, self.mem_register(AP_CSW), (csw & _CSW_KEEP) | CSW_SIZE32)
            self._csw_set.add(ap)
        self.write_ap(ap, self.mem_register(AP_TAR), address)

    def mem_read32(self, ap: int, address: int) -> int:
        self._prepare(ap, address)
        return self.read_ap(ap, self.mem_register(AP_DRW))

    def mem_write32(self, ap: int, address: int, value: int) -> None:
        self._prepare(ap, address)
        self.write_ap(ap, self.mem_register(AP_DRW), value)


class DpPort:
    """pyOCD の DebugPort の口のうち Probe の dp_read / ap_read が使うもの（RawDap の上。ADIv6 の AP の番地）。
    pyOCD を使わない接続（RISC-V・Cortex-A）で、接続中の DP / AP の読み書きに使う。"""

    def __init__(self, dap: RawDap):
        from pyocd.coresight.dap import ADIVersion
        self.dap = dap
        self.adi_version = ADIVersion.ADIv6 if dap.adi_v6 else ADIVersion.ADIv5

    def read_dp(self, address: int) -> int:
        return self.dap.read_dp(address)

    def write_dp(self, address: int, value: int) -> None:
        self.dap.write_dp(address, value)

    def _split(self, address: int) -> tuple[int, int]:
        return (address & ~0xFFF, address & 0xFFF) if self.dap.adi_v6 else (address >> 24, address & 0xFC)

    def read_ap(self, address: int) -> int:
        return self.dap.read_ap(*self._split(address))

    def write_ap(self, address: int, value: int) -> None:
        self.dap.write_ap(*self._split(address), value)

    def flush(self) -> None:
        pass
