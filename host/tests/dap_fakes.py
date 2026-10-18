"""試験用: pyOCD の DAPAccessCMSISDAP（CMSIS-DAP のコマンドの層）の偽物。SW-DP（DPv1 / DPv2 の複数ドロップ /
ADIv6）・JTAG-DP・MEM-AP・ピンを模す。fprobe.raw_dap・fprobe.diagnose・Probe の低レベルの道具の試験に使う。

線の選択（JTAG→SWD・休眠からの起こし・TARGETSEL）・速度の上限・電源・WAIT の張り付きを設定で再現する。"""

from dataclasses import dataclass, field

from pyocd.probe.pydapaccess.dap_access_api import DAPAccessIntf

REG = DAPAccessIntf.REG
PORT = DAPAccessIntf.PORT


@dataclass
class FakeMemAp:
    idr: int = 0x24770011                          # AHB3 MEM-AP
    base: int = 0xE00FF003
    words: dict[int, int] = field(default_factory=dict)
    csw: int = 0x03000040                          # DeviceEn
    tar: int = 0
    readable: tuple[tuple[int, int], ...] = ((0xE0000000, 0xE0100000),)   # words に無くても 0 を返す範囲
    extra: dict[int, int] = field(default_factory=dict)   # AP のレジスタ番地（ADIv6 は窓の中の番地）→ 値（先に見る）
    on_write: object | None = None                  # (番地, 値) -> True なら既定の MEM-AP の扱いをしない

    def read(self, address: int) -> int:
        if address in self.words:
            return self.words[address]
        if any(lo <= address < hi for lo, hi in self.readable):
            return 0
        raise DAPAccessIntf.TransferFaultError("FAULT")


@dataclass
class FakeTarget:
    dpidr: int = 0x2BA01477                         # STM32F4 の SW-DP（DPv1）
    targetid: int | None = None                     # DPv2 の TARGETID（複数ドロップなら TARGETSEL に使う）
    multidrop: bool = False
    dormant_only: bool = False                      # JTAG→SWD（0xE79E）に応じない（休眠からの起こしだけ）
    swd: bool = True                                # SWD に応じる（False = JTAG だけのターゲット）
    max_hz: int = 50_000_000
    powered: bool = True
    power_ack: bool = True
    stuck_wait: bool = False
    parity_errors: bool = False
    adi_v6: bool = False
    baseptr: int = 0
    aps: dict[int, FakeMemAp] = field(default_factory=lambda: {0: FakeMemAp()})
    apb_words: dict[int, int] = field(default_factory=dict)   # ADIv6: AP 以外の番地（ROM テーブル等）
    jtag_chain: object | None = None               # JTAG の模型（jtag_fakes.FakeChain）
    jtag_dp_index: int | None = None               # JTAG で DP 転送に応じる装置の番号


class FakeDapLink:
    """DAPAccessCMSISDAP の口のうち fprobe が使うもの。"""

    def __init__(self, target: FakeTarget | None = None, pins: int = 0b1000_0011):
        self.t = target or FakeTarget()
        self.port = None
        self.clock = 1_000_000
        self.swd_active = False
        self.selected = not self.t.multidrop
        self.after_alert = False
        self.select = 0
        self.ctrl_stat = 0
        self.sticky = False
        self.pins = pins
        self.pin_writes: list[tuple[int, int]] = []
        self.opened = False
        self.closed = False
        self.jtag_irlens: list[int] | None = None
        self.log: list[str] = []

    # 開閉
    def open(self):
        self.opened = True

    def close(self):
        self.closed = True

    def connect(self, port=PORT.DEFAULT):
        self.port = PORT.SWD if port == PORT.DEFAULT else port
        self.log.append(f"connect {self.port.name}")

    def disconnect(self):
        self.port = None

    def get_swj_mode(self):
        return self.port

    def set_clock(self, hz):
        self.clock = hz

    def configure_jtag(self, irlens=None):
        self.jtag_irlens = irlens

    def flush(self):
        pass

    # 線のシーケンス
    def swj_sequence(self, length, bits):
        if length == 51 and bits == (1 << 51) - 1:
            if self.t.multidrop:
                self.selected = False
            return
        if length == 16 and bits == 0xE79E and self.t.swd and not self.t.dormant_only:
            self.swd_active = True
        elif length == 136:
            self.after_alert = True
        elif length == 12 and bits == 0x01A0 and self.after_alert and self.t.swd:
            self.swd_active = True
        elif length == 16 and bits == 0xE73C:
            self.swd_active = False

    def swd_sequence(self, sequences):
        if sequences and sequences[0] == (8, 0x99):
            value = sequences[2][1] & 0xFFFFFFFF
            if self.t.multidrop:                                          # 複数ドロップでない DP は無視する
                self.selected = value == self.t.targetid
            self.log.append(f"targetsel 0x{value:08X}")
        return 0, []

    def jtag_sequence(self, cycles, tms, read_tdo, tdi):
        if self.t.jtag_chain is None:
            out = (1 << cycles) - 1                                   # 何もつながっていない: TDO はプルアップ
        else:
            out = self.t.jtag_chain.sequence(cycles, tms, tdi, True)
        return out.to_bytes((cycles + 7) // 8, "little") if read_tdo else None

    def pin_access(self, mask, value):
        if mask:
            self.pin_writes.append((mask, value))
            self.pins = (self.pins & ~mask) | (value & mask)
        return self.pins

    # 転送
    def _check(self, dap_index):
        t = self.t
        if self.port == PORT.JTAG:
            if t.jtag_dp_index is None or dap_index != t.jtag_dp_index or not t.powered:
                raise DAPAccessIntf.TransferError("No ACK")
            return
        if (not t.powered or not self.swd_active or not self.selected or self.clock > t.max_hz):
            raise DAPAccessIntf.TransferError("No ACK")
        if t.parity_errors:
            raise DAPAccessIntf.TransferProtocolError("SWD protocol error")

    def _ap(self):
        t = self.t
        if t.adi_v6:
            address = self.select & 0xFFFFFFF0
            for base, ap in t.aps.items():
                if base <= address < base + 0x1000:
                    return ap, base, address - base
            return None, address, 0
        return t.aps.get(self.select >> 24), self.select >> 24, self.select & 0xF0

    def _ap_register(self, ap, offset):
        return offset - (0xD00 if self.t.adi_v6 else 0)

    def read_reg(self, reg_id, dap_index=0, now=True):
        self._check(dap_index)
        t = self.t
        if reg_id.value < 4:
            reg = reg_id.value * 4
            bank = self.select & 0xF
            if reg == 0x0:
                if t.adi_v6 and bank == 2:
                    return t.baseptr
                return t.dpidr
            if reg == 0x4:
                if bank == 0:
                    return self.ctrl_stat | (0x20 if self.sticky else 0)
                if bank == 2 and t.targetid is not None:
                    return t.targetid
                if bank == 3:
                    return 0x00000001
                return 0
            if reg == 0x8:
                return 0                                                  # RESEND
            return 0                                                      # RDBUFF
        if t.stuck_wait:
            raise DAPAccessIntf.TransferTimeoutError("WAIT")
        if self.sticky:
            raise DAPAccessIntf.TransferFaultError("FAULT")
        if not self.ctrl_stat & (1 << 29):
            raise DAPAccessIntf.TransferFaultError("FAULT")                # デバッグ電源が無い
        ap, key, offset = self._ap()
        offset += (reg_id.value - 4) * 4
        if ap is None:
            if t.adi_v6:
                return t.apb_words.get(key + (reg_id.value - 4) * 4, 0)
            return 0                                                      # 無い AP の IDR は 0
        if offset in ap.extra:
            return ap.extra[offset]
        register = self._ap_register(ap, offset)
        if register == 0x00:
            return ap.csw
        if register == 0x04:
            return ap.tar
        if register == 0x0C:
            try:
                return ap.read(ap.tar)
            except DAPAccessIntf.TransferFaultError:
                self.sticky = True
                raise
        if register == 0xF8:
            return ap.base
        if register == 0xFC:
            return ap.idr
        return 0

    def write_reg(self, reg_id, value, dap_index=0):
        if reg_id == REG.DP_0x0:                                          # ABORT（JTAG では使わない）
            self._check(dap_index)
            if value & 0x4:
                self.sticky = False
            return
        self._check(dap_index)
        if reg_id == REG.DP_0x4:
            if self.select & 0xF == 0:
                if self.port == PORT.JTAG and value & (1 << 5):
                    self.sticky = False
                req = value & ((1 << 30) | (1 << 28))
                self.ctrl_stat = req | ((req << 1) if self.t.power_ack else 0)
            return
        if reg_id == REG.DP_0x8:
            self.select = value
            return
        if reg_id.value >= 4:
            if self.sticky:
                raise DAPAccessIntf.TransferFaultError("FAULT")
            ap, key, offset = self._ap()
            if ap is None:
                return
            if ap.on_write is not None and ap.on_write(offset + (reg_id.value - 4) * 4, value):
                return
            register = self._ap_register(ap, offset + (reg_id.value - 4) * 4)
            if register == 0x00:
                ap.csw = (value & ~0x40) | (ap.csw & 0x40)                   # DeviceEn は読むだけ
            elif register == 0x04:
                ap.tar = value
            elif register == 0x0C:
                ap.words[ap.tar] = value


class FakeMultidropBus:
    """複数ドロップの SWD の線（RP2040 等）: 1 本の線に DP（FakeDapLink。FakeTarget(multidrop=True, targetid=…)）が並ぶ。
    線のリセット・TARGETSEL はすべての DP に届き、転送は選ばれた 1 つの DP だけが受ける（どれも選ばれていなければ無応答）。"""

    def __init__(self, dps: list[FakeDapLink]):
        self.dps = dps
        self.log: list[str] = []

    def flush(self):
        pass

    def swj_sequence(self, length, bits):
        self.log.append(f"swj {length}")
        for dp in self.dps:
            dp.swj_sequence(length, bits)

    def swd_sequence(self, sequences):
        for dp in self.dps:
            dp.swd_sequence(sequences)
        if sequences and sequences[0] == (8, 0x99):
            self.log.append(f"targetsel 0x{sequences[2][1] & 0xFFFFFFFF:08X}")
        return 0, []

    def selected(self) -> FakeDapLink:
        chosen = [dp for dp in self.dps if dp.selected]
        if len(chosen) != 1:
            raise DAPAccessIntf.TransferError("No ACK")
        return chosen[0]

    def read_reg(self, reg_id, dap_index=0, now=True):
        return self.selected().read_reg(reg_id, dap_index, now)

    def write_reg(self, reg_id, value, dap_index=0):
        self.selected().write_reg(reg_id, value, dap_index)
