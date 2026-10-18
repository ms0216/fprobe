"""プラグイン: RP2350（Raspberry Pi Pico 2）のトレース FIFO と DMA で、ETM の命令トレースをターゲットの SRAM の
リングに溜め、SWD で読んで解読する（配線なし。段階 0b。docs/reports/2026-10-19-etm-investigation.md §4.2）。

本体の公開 API だけを使う（fprobe.plugins の約束）: CoreSight の標準の部品（ETM・ATB ファンネル・TPIU）は
`trace.etm.start` / `trace.etm.stop`、RP2350 固有のもの（ACCESSCTRL・トレース FIFO・DMA）は `target.memory.*`、
読み出しと解読は `trace.etm.decode`（リングの番地と最も古いバイトの位置）。

    from fprobe.plugins import ProbeCaller
    from fprobe.plugins.rp2350_trace import Rp2350TraceFifo
    fifo = Rp2350TraceFifo(client, buffer=0x2007_8000, size=32768, dma_channel=15)  # バッファはリンカで空けておく
    fifo.start()           # ターゲットを走らせる → 止める（ブレークポイント・故障）
    fifo.stop()            # 解読の要約。続けて trace.etm.history で止まる直前の命令

setup:
- "debugger"（既定）: デバッガが ACCESSCTRL.CORESIGHT_TRACE に DMA の許可を足し、トレース FIFO の FLUSH を外し、
  DMA のチャネルを ENDLESS・書き込み側のリングで回す。
- "target": ファームが自分で ACCESSCTRL・FIFO・DMA を設定する（デバッガから ACCESSCTRL を書けない場合の逃げ道）。
  プラグインは ETM・TPIU を設定し、止めたときに DMA の WRITE_ADDR を読んでリングを解読するだけ。

実機で未確認（RP2350 のデータシートの記述による。番地・ビットは Rp2350Addresses と定数で変えられる）:
- CoreSight の部品（ファンネル・TPIU）を CORESIGHT_PERIPH の窓（0x40140000〜）から AHB-AP で書けるか。
- ACCESSCTRL の書き込み（上位 16 ビットに 0xACCE）がデバッガから通るか（Secure・特権のバス主として扱われるか）。
- トレース FIFO が TPIU のどの出力を取り込むか（端子の幅 port_size の扱い）。既定は 4 ビット（端子と同じ）。
- ファンネルのどの入口がどのコアの ETM か（既定は全入口を有効にし、トレース ID で選ぶ）。
- ETM は接続しているコアの PPB（0xE0041000）で設定する。コア 1 の ETM はコア 1 につないで（trace_id を変えて）。
"""

from dataclasses import dataclass

from . import ApiCaller

__all__ = ["Rp2350Addresses", "Rp2350TraceFifo", "SETUPS"]

SETUPS = ("debugger", "target")

# DMA の CTRL（RP2350。RP2040 とはビットの位置が違う）
_EN = 1 << 0
_DATA_SIZE_WORD = 2 << 2
_INCR_WRITE = 1 << 6
_RING_SIZE_SHIFT = 8
_RING_SEL_WRITE = 1 << 12
_CHAIN_TO_SHIFT = 13
_TREQ_SEL_SHIFT = 17
_ENDLESS = 0xF << 28                 # TRANS_COUNT.MODE = ENDLESS（数を減らさず回り続ける）
# DMA のチャネルのレジスタ（チャネルの先頭から）
_READ_ADDR, _WRITE_ADDR, _TRANS_COUNT, _CTRL_TRIG, _AL1_CTRL = 0x00, 0x04, 0x08, 0x0C, 0x10
# ACCESSCTRL
_ACCESSCTRL_PASSWORD = 0xACCE_0000
_ACCESSCTRL_DMA = 1 << 6
# トレース FIFO の CTRL_STATUS
_FIFO_FLUSH = 1 << 0
_FIFO_OVERFLOW = 1 << 1


@dataclass(frozen=True)
class Rp2350Addresses:
    """RP2350 データシート（RP-008373-DS）§3.5・§3.5.7・ACCESSCTRL・DMA の番地。"""
    etm: int = 0xE004_1000                       # 接続しているコアの ETM-M33（PPB）
    funnel: int = 0x4014_7000                    # CORESIGHT_PERIPH_BASE + 0x7000（ATB ファンネル）
    tpiu: int = 0x4014_8000                      # CORESIGHT_TPIU_BASE
    trace_fifo: int = 0x5070_0000                # CORESIGHT_TRACE_BASE
    fifo_ctrl_status: int = 0x0                  # CTRL_STATUS（FLUSH・OVERFLOW）
    fifo_data: int = 0x4                         # TRACE_CAPTURE_FIFO
    accessctrl_coresight_trace: int = 0x4006_0058
    dma: int = 0x5000_0000
    dreq: int = 53                               # DREQ_CORESIGHT


class Rp2350TraceFifo:
    def __init__(self, api: ApiCaller, buffer: int, size: int = 32768, dma_channel: int = 15, setup: str = "debugger",
                 trace_id: int = 0x10, port_size: int = 4, funnel_ports: int = 0xFF, sync_period: int = 256,
                 cycle_counting: bool = False, timestamps: bool = False,
                 addresses: Rp2350Addresses = Rp2350Addresses()):
        """buffer: リングの先頭（size に揃った番地。ファームのリンカで空けておく）。size: 2 のべき、32 KB まで
        （DMA の RING_SIZE）。sync_period: ETM の同期の間隔（リングが周回した後も解読を始められるよう小さく）。"""
        if setup not in SETUPS:
            raise ValueError(f"setup は {', '.join(SETUPS)}: {setup!r}")
        if size & (size - 1) or not 64 <= size <= 32768:
            raise ValueError(f"size は 64〜32768 の 2 のべき（DMA のリング）: {size}")
        if buffer % size:
            raise ValueError(f"buffer（0x{buffer:08X}）は size（{size}）に揃える（DMA のリング）")
        if not 0 <= dma_channel <= 15:
            raise ValueError(f"DMA のチャネルは 0〜15: {dma_channel}")
        self.api, self.buffer, self.size, self.channel, self.setup = api, buffer, size, dma_channel, setup
        self.trace_id, self.port_size, self.funnel_ports = trace_id, port_size, funnel_ports
        self.sync_period, self.cycle_counting, self.timestamps = sync_period, cycle_counting, timestamps
        self.a = addresses

    # --- メモリ（公開 API の target.memory.*） ----------------------------------------------

    def _read32(self, address: int) -> int:
        return int.from_bytes(bytes.fromhex(self.api.call("target.memory.read", address=address, length=4)), "little")

    def _write32(self, address: int, value: int) -> None:
        self.api.call("target.memory.write", address=address, data_hex=value.to_bytes(4, "little").hex())

    def _ch(self, offset: int) -> int:
        return self.a.dma + 0x40 * self.channel + offset

    def _ctrl(self, enable: bool) -> int:
        return ((_EN if enable else 0) | _DATA_SIZE_WORD | _INCR_WRITE | (self.size.bit_length() - 1) << _RING_SIZE_SHIFT
                | _RING_SEL_WRITE | self.channel << _CHAIN_TO_SHIFT | self.a.dreq << _TREQ_SEL_SHIFT)

    # --- 始める・止める ---------------------------------------------------------------------

    def start(self) -> dict:
        """FIFO と DMA（setup = "debugger" のとき）を用意してから ETM・TPIU を始める。"""
        if self.setup == "debugger":
            self._write32(self._ch(_AL1_CTRL), self._ctrl(enable=False))           # 前の転送を止める
            self.api.call("target.memory.write", address=self.buffer, data_hex=bytes(self.size).hex())
            access = self._read32(self.a.accessctrl_coresight_trace)
            self._write32(self.a.accessctrl_coresight_trace, _ACCESSCTRL_PASSWORD | (access & 0xFF) | _ACCESSCTRL_DMA)
            fifo_ctrl = self.a.trace_fifo + self.a.fifo_ctrl_status
            self._write32(fifo_ctrl, _FIFO_FLUSH)                                  # 一度空にしてから
            self._write32(fifo_ctrl, 0)                                            # FLUSH を外して取り込む
            self._write32(self._ch(_READ_ADDR), self.a.trace_fifo + self.a.fifo_data)
            self._write32(self._ch(_WRITE_ADDR), self.buffer)
            self._write32(self._ch(_TRANS_COUNT), _ENDLESS | 1)
            self._write32(self._ch(_CTRL_TRIG), self._ctrl(enable=True))
        out = self.api.call("trace.etm.start", etm_base=self.a.etm, trace_id=self.trace_id, tpiu_base=self.a.tpiu,
                            port_size=self.port_size, formatter=True, funnel_base=self.a.funnel,
                            funnel_ports=self.funnel_ports, cycle_counting=self.cycle_counting,
                            timestamps=self.timestamps, sync_period=self.sync_period)
        return {"setup": self.setup, "buffer": self.buffer, "size": self.size, "dma_channel": self.channel, **out}

    def stop(self, decode: bool = True) -> dict:
        """ETM を止めて TPIU を出し切り、DMA を止め、リングを書き込み位置（最も古いバイト）から読んで解読する。"""
        self.api.call("trace.etm.stop", etm_base=self.a.etm, tpiu_base=self.a.tpiu)
        write_addr = self._read32(self._ch(_WRITE_ADDR))
        status = self._read32(self.a.trace_fifo + self.a.fifo_ctrl_status)
        if self.setup == "debugger":
            self._write32(self._ch(_AL1_CTRL), self._ctrl(enable=False))
            self._write32(self.a.trace_fifo + self.a.fifo_ctrl_status, _FIFO_FLUSH)    # 取り込みを止める
        if not self.buffer <= write_addr < self.buffer + self.size:
            raise RuntimeError(f"DMA の WRITE_ADDR 0x{write_addr:08X} がリングの外（チャネル {self.channel}）")
        ring = {"address": self.buffer, "size": self.size, "oldest": write_addr - self.buffer,
                "overflow": bool(status & _FIFO_OVERFLOW)}
        if not decode:
            return {"ring": ring}
        out = self.api.call("trace.etm.decode", address=self.buffer, length=self.size, oldest=ring["oldest"],
                            trace_id=self.trace_id)
        return {"ring": ring, **out}
