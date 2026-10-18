"""SWD ターゲット（ADIv5 の SW-DP + MEM-AP + メモリ）のピンレベル振る舞いモデル（cocotb, テスト専用）。

## ピンとタイミング（ARM の CMSIS-DAP 参照実装と同じ）
- ターゲットは SWCLK の立ち上がりで SWDIO を取り込む。
- ターゲットが駆動するビットは立ち上がりの直後に出す（ホストは次の立ち上がりの直前に取り込む）。
- SWDIO は抵抗付きの共有線: テストのハーネスでホスト（DUT）の出力とモデルの出力（target_swdio、'Z' で解放）を
  解決し、どちらも駆動しなければプルアップで 1。両方が駆動すると 'X' になり、モデルはそれを検出して失敗させる。

## 振る舞い
- 1 が 50 個以上続くとラインリセット。それまでは何にも応答しない（JTAG 状態のまま）。
- multidrop（TARGETSEL の値 → DPIDR）を与えると、複数ドロップの SWD（DPv2）の DP の並び（RP2040 は core0・core1・
  救済の DP）を 1 本の線の上で模す。ラインリセットの後は DP が選ばれておらず、TARGETSEL の書き込み（DP の 0xC。
  ACK の 3 ビットを誰も駆動しない。要求の後 ターンアラウンド + 3 + ターンアラウンド 周期の後に 33 ビットのデータ）で
  一致した DP だけが以後に応答する。一致しなければどれも応答しない。
- 要求ヘッダ（start, APnDP, RnW, A[2:3], parity, stop, park）を検査し、不正なら応答しない。
- ACK: OK / WAIT（wait_next 回）/ FAULT（STICKYERR 中の AP アクセス）。データ相のパリティを検査する。
- DP: DPIDR / ABORT / CTRL/STAT（電源要求に即応答）/ SELECT / RDBUFF。
- MEM-AP: CSW（サイズ・自動インクリメント）/ TAR / DRW / IDR。AP の読み出しはポスティング（値は次の AP 読み出し
  か RDBUFF で返る）。fault_addresses へのアクセスは STICKYERR を立てる。
"""

import random
from dataclasses import dataclass, field

from cocotb.triggers import Edge, ReadOnly, RisingEdge, Timer

ACK_OK, ACK_WAIT, ACK_FAULT = 0b001, 0b010, 0b100

DPIDR = 0x2BA01477            # STM32F4（Cortex-M4 の SW-DP）
AP_IDR = 0x24770011           # AHB-AP

# CTRL/STAT
STICKYERR = 1 << 5
CDBGPWRUPREQ, CDBGPWRUPACK = 1 << 28, 1 << 29
CSYSPWRUPREQ, CSYSPWRUPACK = 1 << 30, 1 << 31
# ABORT
STKERRCLR = 1 << 2


@dataclass
class SwdTarget:
    dut: object
    turnaround: int = 1
    memory: dict[int, int] = field(default_factory=dict)      # ワードアドレス（4 の倍数）→ 値
    fault_addresses: set[int] = field(default_factory=set)
    rng: random.Random = field(default_factory=lambda: random.Random(1))
    output_delay_ns: float = 0.0      # 立ち上がりから SWDIO を変えるまで（配線の往復を含む応答の遅れ）
    multidrop: dict[int, int] | None = None   # 複数ドロップ: TARGETSEL の値 → その DP の DPIDR

    def __post_init__(self):
        self.ctrl_stat = 0
        self.select = 0
        self.read_buffer = 0          # 直前の AP 読み出しの結果（RDBUFF）
        self.csw = 0x23000002         # 32 ビット、インクリメントなし
        self.tar = 0
        self.wait_next = 0            # 次の n 回の AP アクセスに WAIT を返す
        self.corrupt_next_parity = False
        self.line_reset = False
        self.selected: int | None = None        # 複数ドロップ: 選ばれた DP の TARGETSEL の値（None = どれも応答しない）
        self.transactions: list[tuple] = []     # (APnDP, RnW, addr, ack, value)
        self.dut.target_swdio.value = "Z"

    # --- ピン ------------------------------------------------------------------

    async def _bit(self) -> int:
        """次の立ち上がりで取り込んだ SWDIO。"""
        await RisingEdge(self.dut.swclk)
        await ReadOnly()
        value = str(self.dut.swdio.value)
        assert value in ("0", "1"), f"SWDIO が {value!r}（ホストとターゲットが同時に駆動している）"
        return int(value)

    async def _drive_after_edge(self, bit: int | None):
        """立ち上がりを待ち、その直後から bit を出す（None で解放）。"""
        await RisingEdge(self.dut.swclk)
        if self.output_delay_ns:
            await Timer(self.output_delay_ns, "ns")
        self.dut.target_swdio.value = "Z" if bit is None else str(bit)

    # --- 主ループ ------------------------------------------------------------------

    async def run(self):
        ones = 0
        while True:
            bit = await self._bit()
            ones = ones + 1 if bit else 0
            if ones >= 50:
                self.line_reset = True
                self.selected = None                        # 複数ドロップ: TARGETSEL で選び直す
            if not self.line_reset or bit == 0 or ones > 1:
                continue
            # start ビット（直前が 0 の 1）。ラインリセット中の 1 の連続は数えない
            header = [1] + [await self._bit() for _ in range(7)]
            ones = 0 if header[-1] == 0 else 1
            ap, rnw, a2, a3, parity, stop, park = header[1:]
            if stop != 0 or park != 1 or (ap ^ rnw ^ a2 ^ a3) != parity:
                continue                                    # 不正なヘッダには応答しない
            addr = (a3 << 3) | (a2 << 2)
            if self.multidrop is not None:
                if not ap and not rnw and addr == 0xC:
                    await self._targetsel()
                    ones = 0
                    continue
                if self.selected is None:
                    continue                                # 選ばれていない DP は応答しない
            await self._transaction(ap, rnw, addr)
            ones = 0

    async def _transaction(self, ap: int, rnw: int, addr: int):
        # ターンアラウンド: park を取り込んだ立ち上がりから turnaround 周期後に ACK の 1 ビット目を出す
        for _ in range(self.turnaround - 1):
            await RisingEdge(self.dut.swclk)
        ack = self._ack(ap)
        for i in range(3):
            await self._drive_after_edge(ack >> i & 1)
        if ack != ACK_OK:
            await self._drive_after_edge(None)
            await self._skip_turnaround()
            self.transactions.append((ap, rnw, addr, ack, None))
            return
        if rnw:
            value = self._read(ap, addr)
            parity = bin(value).count("1") & 1
            if self.corrupt_next_parity:
                self.corrupt_next_parity = False
                parity ^= 1
            for i in range(32):
                await self._drive_after_edge(value >> i & 1)
            await self._drive_after_edge(parity)
            await self._drive_after_edge(None)
            await self._skip_turnaround()
            self.transactions.append((ap, rnw, addr, ack, value))
        else:
            # ACK の最後のビットの周期が終わったら解放し、ターンアラウンド周期の後からホストのデータを取り込む
            await self._drive_after_edge(None)
            await self._skip_turnaround()
            bits = [await self._bit() for _ in range(33)]
            value = sum(b << i for i, b in enumerate(bits[:32]))
            if (bin(value).count("1") & 1) != bits[32]:
                self.ctrl_stat |= 1 << 7                    # WDATAERR
                self.transactions.append((ap, rnw, addr, ack, "parity"))
                return
            self._write(ap, addr, value)
            self.transactions.append((ap, rnw, addr, ack, value))

    async def _targetsel(self):
        """TARGETSEL の書き込み: ACK を返さない（ターンアラウンド・ACK の 3 ビット・ターンアラウンドを誰も駆動しない）。
        続く 33 ビット（値とパリティ）が DP の一つと一致すればそれを選ぶ。"""
        for _ in range(2 * self.turnaround + 3):
            await RisingEdge(self.dut.swclk)
        bits = [await self._bit() for _ in range(33)]
        value = sum(b << i for i, b in enumerate(bits[:32]))
        ok = (bin(value).count("1") & 1) == bits[32]
        self.selected = value if ok and value in self.multidrop else None
        self.transactions.append((0, 0, 0xC, None, value if ok else "parity"))

    async def _skip_turnaround(self):
        """ターゲットが解放した後のターンアラウンド周期（誰も駆動しない）を読み飛ばす。"""
        for _ in range(self.turnaround):
            await RisingEdge(self.dut.swclk)

    def _ack(self, ap: int) -> int:
        if not ap:
            return ACK_OK
        if self.ctrl_stat & STICKYERR:
            return ACK_FAULT
        if self.wait_next:
            self.wait_next -= 1
            return ACK_WAIT
        return ACK_OK

    # --- レジスタ ------------------------------------------------------------------

    def _read(self, ap: int, addr: int) -> int:
        if not ap:
            if addr == 0x0:
                return DPIDR if self.multidrop is None else self.multidrop[self.selected]
            if addr == 0x4:
                return self.ctrl_stat
            if addr == 0xC:
                return self.read_buffer
            return 0
        previous = self.read_buffer
        self.read_buffer = self._ap_read((self.select >> 4 & 0xF) << 4 | addr)
        return previous

    def _write(self, ap: int, addr: int, value: int):
        if not ap:
            if addr == 0x0:
                if value & STKERRCLR:
                    self.ctrl_stat &= ~STICKYERR
            elif addr == 0x4:
                self.ctrl_stat = (value & (CDBGPWRUPREQ | CSYSPWRUPREQ)) | (self.ctrl_stat & STICKYERR)
                if value & CDBGPWRUPREQ:
                    self.ctrl_stat |= CDBGPWRUPACK
                if value & CSYSPWRUPREQ:
                    self.ctrl_stat |= CSYSPWRUPACK
            elif addr == 0x8:
                self.select = value
            return
        self._ap_write((self.select >> 4 & 0xF) << 4 | addr, value)

    def _size_bytes(self) -> int:
        return 1 << (self.csw & 0x7)

    def _advance(self):
        if (self.csw >> 4) & 0x3 == 0b01:
            self.tar = (self.tar + self._size_bytes()) & 0xFFFFFFFF

    def _ap_read(self, reg: int) -> int:
        if reg == 0x00:
            return self.csw
        if reg == 0x04:
            return self.tar
        if reg == 0xFC:
            return AP_IDR
        if reg == 0x0C:
            word = self.tar & ~3
            if word in self.fault_addresses:
                self.ctrl_stat |= STICKYERR
                return 0
            value = self.memory.get(word, 0)
            self._advance()
            return value
        return 0

    def _ap_write(self, reg: int, value: int):
        if reg == 0x00:
            self.csw = (self.csw & ~0x37) | (value & 0x37)
        elif reg == 0x04:
            self.tar = value
        elif reg == 0x0C:
            word = self.tar & ~3
            if word in self.fault_addresses:
                self.ctrl_stat |= STICKYERR
                return
            size = self._size_bytes()
            lane = (self.tar & 3) * 8
            mask = ((1 << (8 * size)) - 1) << lane if size < 4 else 0xFFFFFFFF
            self.memory[word] = (self.memory.get(word, 0) & ~mask) | (value & mask)
            self._advance()
