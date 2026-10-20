"""DAP トランザクションのイベント記録の復号と、ポストされた読みの対応付け。

仕様: docs/superpowers/specs/2026-10-16-m9b-dap-events-design.md
イベント 1 件 = 16 バイト: [47:0] 時刻、[79:48] 番地、[111:80] データ、[114:112] ACK、[118:115] 要求
（APnDP, RnW, A[3:2]）、[119] パリティ異常、[127:120] 種類。
種類 3 / 4 / 5（UART 受信・UART 送信・SWO）はデータの下位 8 ビットがバイト（波形・DAP と同じ時間軸で並べられる）。
"""

from dataclasses import dataclass

from . import generated as g
from .device import Device

__all__ = ["Access", "DapEvent", "EventCursor", "EventLog", "KIND_AUX", "KIND_HOST", "KIND_INJECT", "KIND_SAMPLE", "KIND_SWO", "KIND_UART_RX", "KIND_UART_TX",
           "decode_events", "resolve"]

EVENT_BYTES = 16
KIND_HOST = 1            # ホスト（pyOCD 等）の転送
KIND_INJECT = 2          # クロストリガの割り込み書き込み
KIND_UART_RX = 3         # UART の受信バイト（ターゲット → PC）
KIND_UART_TX = 4         # UART の送信バイト（PC → ターゲット）
KIND_SWO = 5             # SWO のバイト
KIND_AUX = 6             # 補助マイコンの計測値（data = ch << 16 | mV）
KIND_SAMPLE = 7          # DAP サンプラ（F4）の 1 語（番地・値。FPGA が自分で周期的に読んだもの）
BYTE_KINDS = {KIND_UART_RX: "uart_rx", KIND_UART_TX: "uart_tx", KIND_SWO: "swo"}
ACK_OK = 0b001
DP_RDBUFF = 0xC


@dataclass(frozen=True)
class DapEvent:
    """線上の 1 転送。AP の読みの data はポストされた値（1 つ前の AP 読みの結果）のまま。"""
    time: int
    kind: int
    ap: bool
    read: bool
    register: int            # A[3:2] << 2（0x0 / 0x4 / 0x8 / 0xC）
    ack: int
    parity_error: bool
    address: int             # DRW ならメモリの番地（TAR から）、それ以外はレジスタの番地
    data: int

    @property
    def ok(self) -> bool:
        return self.ack == ACK_OK and not self.parity_error


@dataclass(frozen=True)
class Access:
    """意味づけした 1 アクセス（ポストされた読みを対応付けた後）。time は値が確定した転送の時刻。"""
    time: int
    kind: str                # "ap_read" / "ap_write" / "dp_read" / "dp_write" / "fail" / "uart_rx" / "uart_tx" / "swo"
                             # / "aux"（address = チャンネル、value = mV）/ "sample"（サンプラが読んだ番地と値）
    address: int
    value: int
    ack: int


def decode_events(data: bytes) -> list[DapEvent]:
    if len(data) % EVENT_BYTES:
        raise ValueError("イベント（16 バイト）の倍数でない")
    out = []
    for i in range(0, len(data), EVENT_BYTES):
        v = int.from_bytes(data[i:i + EVENT_BYTES], "little")
        request = v >> 115 & 0xF
        out.append(DapEvent(time=v & (1 << 48) - 1, kind=v >> 120 & 0xFF, ap=bool(request & 1),
                            read=bool(request & 2), register=(request >> 2) << 2, ack=v >> 112 & 7,
                            parity_error=bool(v >> 119 & 1), address=v >> 48 & 0xFFFFFFFF,
                            data=v >> 80 & 0xFFFFFFFF))
    return out


def resolve(events: list[DapEvent]) -> list[Access]:
    """ポストされた AP の読みを、値が返った転送（次の AP 読みか DP の RDBUFF 読み）と対応付ける（DAP.c と同じ規則）。"""
    out: list[Access] = []
    pending: DapEvent | None = None          # 値がまだ返っていない AP の読み
    for e in events:
        if e.kind in BYTE_KINDS:                 # バイトはそのまま（DAP のポストの連鎖には関係しない）
            out.append(Access(e.time, BYTE_KINDS[e.kind], 0, e.data & 0xFF, 0))
            continue
        if e.kind == KIND_AUX:
            out.append(Access(e.time, "aux", e.data >> 16, e.data & 0x3FFF, 0))
            continue
        if e.kind == KIND_SAMPLE:                # サンプラの読みは値が確定済み（ホストのポストの連鎖とは別）
            out.append(Access(e.time, "sample", e.address, e.data, e.ack))
            continue
        if not e.ok:
            out.append(Access(e.time, "fail", e.address, e.data, e.ack))
            pending = None                   # 失敗でポストの連鎖は途切れる
            continue
        if e.ap and e.read:
            if pending is not None:
                out.append(Access(e.time, "ap_read", pending.address, e.data, e.ack))
            pending = e
        elif not e.ap and e.read and e.register == DP_RDBUFF:
            if pending is not None:
                out.append(Access(e.time, "ap_read", pending.address, e.data, e.ack))
                pending = None
            else:
                out.append(Access(e.time, "dp_read", e.register, e.data, e.ack))
        elif e.read:
            out.append(Access(e.time, "dp_read", e.register, e.data, e.ack))
        else:
            out.append(Access(e.time, "ap_write" if e.ap else "dp_write", e.address, e.data, e.ack))
    return out


class EventLog:
    """DAP のイベント記録（外部メモリのリング）。ロジアナの深いバッファと同時に使える（読み出しは捕捉していないときに）。"""

    def __init__(self, dev: Device):
        self.dev = dev
        self.capacity = dev.read32(g.EVENTS_CAPACITY)
        if not self.capacity:
            raise RuntimeError("このデバイスにはイベント記録が無い")
        self.overwritten = 0          # 直前の read で、読む間に書き手に上書きされて捨てた件数

    def start(self) -> None:
        """記録を始める（リングの先頭から）。"""
        self.dev.write32(g.EVENTS_CONTROL, 0)
        self.dev.write32(g.EVENTS_CONTROL, 1)

    def running(self) -> bool:
        return bool(self.dev.read32(g.EVENTS_CONTROL) & 1)

    def ensure_started(self) -> None:
        """記録していなければ始める。記録中なら続ける（ほかの道具が読んでいる途中のリングを捨てない）。"""
        if not self.running():
            self.start()

    def stop(self) -> None:
        self.dev.write32(g.EVENTS_CONTROL, 0)

    def counts(self) -> tuple[int, int]:
        """(書いた総数, 記録できなかった数)。"""
        written, lost = self.dev.read(g.EVENTS_WRITTEN, 2)
        return written, lost

    def cursor(self) -> "EventCursor":
        """いまから後に書かれるイベントを順に読む読み手。"""
        return EventCursor(self)

    def read(self, last: int | None = None, end: int | None = None) -> list[DapEvent]:
        """end 件目（書いた総数で数える。既定: 読んだ時点の総数）で終わる last 件（既定: 残っている全部）を古い順に。
        先に counts で数を読んだ呼び手は、その数を end に渡す（間に書かれた分で範囲がずれ、欠けと二重が出る。
        2026-10-18 に実機で、記録中の SWO のバイトがそうなった）。"""
        written = self.counts()[0] if end is None else end
        available = min(written, self.capacity)
        n = available if last is None else min(last, available)
        if n == 0:
            return []
        self.dev.write32(g.LA_DEEP_READ_REGION, 1)
        self.dev.write(g.LA_DEEP_READ_FIRST, [(written - n) % self.capacity, n])
        self.dev.write32(g.LA_DEEP_COMMAND, 1 << g.LA_DEEP_COMMAND_READ[0])
        events = decode_events(self.dev.read_stream(n * EVENT_BYTES, timeout_ms=5000))
        # 読む間に書き手が読み手を追い越した分は、要求より新しいイベントにすり替わっている（リングが小さい構成・遅いリンクで
        # 起きる）。読み終えた時点の総数から、まだ残っていたはずの番号より前の先頭を捨てて数える
        after = self.counts()[0]
        drop = n if after < written else min(n, max(0, after - self.capacity - (written - n)))
        self.overwritten = drop
        return events[drop:]


class EventCursor:
    """イベント記録を前回の続きから読む（読んでもリングは消えない。ほかの読み手と同時に使える）。読む前にリングが
    一周して上書きされた分・limit を超えた分は lost に数える（古い方を捨て、最新を読む）。"""

    def __init__(self, log: EventLog):
        self.log = log
        self.seen = log.counts()[0]
        self.lost = 0
        self.restarts = 0

    def take(self, limit: int = 1 << 16) -> list[DapEvent]:
        written, _ = self.log.counts()
        if written < self.seen:                    # 記録が始め直された（記録の開始等）
            self.seen, self.restarts = 0, self.restarts + 1
        new = written - self.seen
        n = min(new, self.log.capacity, limit)
        self.lost += new - n
        self.seen = written
        if not n:
            return []
        events = self.log.read(n, end=written)
        self.lost += self.log.overwritten
        return events
