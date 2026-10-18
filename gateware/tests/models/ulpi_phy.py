"""USB3317 の ULPI 振る舞いモデル（cocotb, テスト専用）。

## クロックとサンプリングの約束

link（DUT）と PHY は同じクロック（ULPI CLKOUT）の立ち上がりで動く（ULPI 標準）。モデルは 1 周期ずつ:

1. 立ち上がりの直後に PHY の出力（DIR, NXT, DATA）をその周期の値に設定し、
2. 周期の終わり（次の立ち上がり）で PHY が取り込む link の出力（STP, DATA）を読む。
   link の出力は立ち上がりで変わるレジスタなので、立ち下がり後の値がそれである。

**NXT=1 にした周期の終わりに読んだ link のバイトが、受理されたバイト**である。link は次の立ち上がりで
NXT を見て、次のバイトを出力レジスタに載せる（モデルの次の周期の終わりに見える）。

根拠: docs/reference/usb2-hs-device-on-ulpi.md §1, §2。

DUT の信号名（ulpi_link のポート）: dir, nxt, data_i（PHY→link）, data_o, data_oe, stp（link→PHY）。
"""

import random
from collections import deque
from dataclasses import dataclass

import cocotb
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge

REG_VID_LOW, REG_VID_HIGH, REG_PID_LOW, REG_PID_HIGH = 0x00, 0x01, 0x02, 0x03
REG_FUNC_CTRL = 0x04
REG_IF_CTRL = 0x07
REG_OTG_CTRL = 0x0A
REG_SCRATCH = 0x16

# 書き込みアドレス → (実体レジスタ, 操作)。操作: "w" 書き込み / "s" セット / "c" クリア
_WRITABLE = {}
for _base in (REG_FUNC_CTRL, REG_IF_CTRL, REG_OTG_CTRL, REG_SCRATCH):
    _WRITABLE.update({_base: (_base, "w"), _base + 1: (_base, "s"), _base + 2: (_base, "c")})

LINE_SE0, LINE_J, LINE_K = 0b00, 0b01, 0b10      # FS 時。HS では 00=squelch, 01=非 squelch
VBUS_VALID = 0b11


@dataclass
class Transmission:
    """link が送出したもの。"""
    pid: int | None            # TX CMD の PID（NOPID なら None）
    data: bytes                # TX CMD の後に受理したバイト
    aborted: bool              # STP と同時に 0xFF、または DIR による中断
    start_cycle: int
    end_cycle: int

    @property
    def packet(self) -> bytes:
        """PID バイトを含む USB パケット（NOPID なら data のみ）。"""
        if self.pid is None:
            return self.data
        return bytes([((~self.pid & 0xF) << 4) | self.pid]) + self.data


@dataclass
class _Reception:
    packet: bytes
    error_at: int | None = None     # このバイト位置で RxError を出して打ち切る


class UlpiPhy:
    def __init__(self, dut, clock, *, rng: random.Random | None = None,
                 nxt_throttle: float = 0.0, start_cycles: int = 20,
                 vid: int = 0x0424, pid: int = 0x0006):
        self._dut = dut
        self._clock = clock
        self.rng = rng or random.Random(0)
        self.nxt_throttle = nxt_throttle          # NXT を出さない確率（PHY による間引き）
        self.cycle = 0
        self._dir_prev = 0
        self.regs = {REG_VID_LOW: vid & 0xFF, REG_VID_HIGH: vid >> 8,
                     REG_PID_LOW: pid & 0xFF, REG_PID_HIGH: pid >> 8,
                     REG_FUNC_CTRL: 0x41, REG_IF_CTRL: 0x00, REG_OTG_CTRL: 0x06,
                     REG_SCRATCH: 0x00}
        self.register_writes: list[tuple[int, int, int]] = []     # (cycle, addr, value)
        self.transmissions: list[Transmission] = []
        self.receptions_done = 0                   # link に渡し終えたパケット数
        self.transmitting = False                  # link からの送信を受けている最中
        self.last_reception_end = 0                # 最後に渡し終えたパケットの終了周期
        self.line_state = LINE_SE0
        self.vbus = VBUS_VALID
        self.ready = False                         # 起動シーケンス完了
        self._start_cycles = start_cycles
        self._receptions: deque[_Reception] = deque()
        self._rxcmd_pending = False
        self.preempt_next_command = False          # 次の link コマンドを受理せず DIR で割り込む
        self.abort_transmit_after: int | None = None   # 送信中、このバイト数受理後に DIR で中断
        self._set(dir=1, nxt=0, data=0)
        cocotb.start_soon(self._run())

    # --- テストからの操作 -------------------------------------------------------

    def set_line_state(self, line_state: int):
        if line_state != self.line_state:
            self.line_state = line_state
            self._rxcmd_pending = True

    def set_vbus(self, vbus: int):
        if vbus != self.vbus:
            self.vbus = vbus
            self._rxcmd_pending = True

    def receive(self, packet: bytes, *, error_at: int | None = None):
        """ホストからのパケットを、バスが空いたら link に渡す。"""
        self._receptions.append(_Reception(bytes(packet), error_at))

    @property
    def idle(self) -> bool:
        return not self._receptions and not self._rxcmd_pending

    @property
    def func_ctrl(self) -> int:
        return self.regs[REG_FUNC_CTRL]

    def rx_cmd(self, *, rx_active: bool = False, rx_error: bool = False) -> int:
        event = 0b11 if rx_error else (0b01 if rx_active else 0b00)
        return self.line_state | (self.vbus << 2) | (event << 4)

    # --- 1 周期 ------------------------------------------------------------------

    def _set(self, *, dir, nxt, data):
        self._dut.dir.value = dir
        self._dut.nxt.value = nxt
        self._dut.data_i.value = data

    async def _cycle(self, *, dir=0, nxt=0, data=0):
        """次の PHY 周期へ進んで出力を設定し、周期の終わりに取り込む link 出力 (stp, data or None) を返す。"""
        await RisingEdge(self._clock)
        self.cycle += 1
        # PHY がデータを駆動する周期（DIR = 1 の 2 周期目以降。1 周期目はターンアラウンド）の頭で、
        # link はバスを離していなければならない
        if dir and self._dir_prev:
            assert not int(self._dut.data_oe.value), f"周期 {self.cycle}: PHY と link が同時にデータを駆動"
        self._dir_prev = dir
        self._set(dir=dir, nxt=nxt, data=data)
        await FallingEdge(self._clock)
        await ReadOnly()
        stp = int(self._dut.stp.value)
        link_data = int(self._dut.data_o.value) if int(self._dut.data_oe.value) else None
        return stp, link_data

    def _will_accept(self) -> bool:
        return self.rng.random() >= self.nxt_throttle

    # --- 主ループ ----------------------------------------------------------------

    async def _run(self):
        # 起動: PLL ロックまで DIR=1。さらに link が STP を下げるまで DIR=1 を保つ
        stp, _ = await self._cycle(dir=1)
        remaining = self._start_cycles
        while remaining > 0 or stp:
            remaining = max(0, remaining - 1)
            stp, _ = await self._cycle(dir=1)
        await self._cycle(dir=0)                    # ターンアラウンド
        self.ready = True
        self._rxcmd_pending = True
        while True:
            if self._receptions:
                await self._reception(self._receptions.popleft())
            elif self._rxcmd_pending:
                await self._send_rx_cmd()
            else:
                _, link_data = await self._cycle(dir=0)
                if link_data:                       # 0x00 以外は link のコマンド
                    await self._command(link_data)

    async def _send_rx_cmd(self):
        self._rxcmd_pending = False
        await self._cycle(dir=1)                    # ターンアラウンド
        await self._cycle(dir=1, data=self.rx_cmd())
        await self._cycle(dir=0)                    # ターンアラウンド

    async def _reception(self, rec: _Reception):
        await self._cycle(dir=1, nxt=1)             # RxActive 開始（ターンアラウンド）
        for i, byte in enumerate(rec.packet):
            if rec.error_at is not None and i == rec.error_at:
                await self._cycle(dir=1, data=self.rx_cmd(rx_active=True, rx_error=True))
                break
            while not self._will_accept():
                await self._cycle(dir=1, data=self.rx_cmd(rx_active=True))
            await self._cycle(dir=1, nxt=1, data=byte)
        await self._cycle(dir=1, data=self.rx_cmd(rx_active=False))
        self.last_reception_end = self.cycle
        self.receptions_done += 1
        await self._cycle(dir=0)                    # ターンアラウンド

    async def _accept_one(self) -> tuple[int, int | None]:
        """NXT を 1 周期出し、その周期に読んだ link の値（受理したバイト）を返す。"""
        while not self._will_accept():
            await self._cycle(dir=0)
        return await self._cycle(dir=0, nxt=1)

    async def _command(self, command: int):
        if self.preempt_next_command:
            # コマンドを受理せず、受信（または RX CMD）で割り込む
            self.preempt_next_command = False
            if self._receptions:
                await self._reception(self._receptions.popleft())
            else:
                await self._send_rx_cmd()
            return
        kind = command >> 6
        if kind == 0b10:
            await self._register_write(command & 0x3F)
        elif kind == 0b11:
            await self._register_read(command & 0x3F)
        elif kind == 0b01:
            await self._transmit(command & 0x0F)
        else:
            raise AssertionError(f"unexpected link command {command:#04x}")

    async def _register_write(self, addr: int):
        _, command = await self._accept_one()
        assert command == 0x80 | addr, f"register write command changed: {command}"
        _, value = await self._accept_one()
        stp, data = await self._cycle(dir=0)
        assert stp == 1 and data == 0, "register write must end with STP and DATA=0"
        if addr not in _WRITABLE:
            raise AssertionError(f"write to read-only/unknown register {addr:#04x}")
        base, op = _WRITABLE[addr]
        if op == "w":
            self.regs[base] = value
        elif op == "s":
            self.regs[base] |= value
        else:
            self.regs[base] &= ~value & 0xFF
        if base == REG_FUNC_CTRL:
            self.regs[base] &= ~0x20 & 0xFF          # Reset ビットは自己クリア
        self.register_writes.append((self.cycle, addr, value))

    async def _register_read(self, addr: int):
        await self._accept_one()
        await self._cycle(dir=1)                    # ターンアラウンド
        await self._cycle(dir=1, data=self.regs.get(addr, 0))
        await self._cycle(dir=0)                    # ターンアラウンド

    async def _transmit(self, pid: int):
        start = self.cycle
        self.transmitting = True
        await self._accept_one()                    # TX CMD 受理
        data = bytearray()
        aborted = False
        while True:
            if self.abort_transmit_after is not None and len(data) >= self.abort_transmit_after:
                self.abort_transmit_after = None
                aborted = True
                await self._send_rx_cmd()           # DIR を上げて送信を打ち切る
                break
            accept = self._will_accept()
            stp, value = await self._cycle(dir=0, nxt=int(accept))
            if stp:
                aborted = value == 0xFF
                break
            if accept:
                data.append(value)
        self.transmitting = False
        self.transmissions.append(Transmission(
            pid=None if pid == 0 else pid, data=bytes(data), aborted=aborted,
            start_cycle=start, end_cycle=self.cycle))
        self._rxcmd_pending = True                  # 送信後は LineState を RX CMD で返す
