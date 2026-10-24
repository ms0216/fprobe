"""トレースの形式の選び方: 流れ（RTT・SWO・UART で届くバイト）を解読するもの（`TraceStream`）と、一まとまりの
データ（ファイル・RAM から吸い出したもの）を解読する `decode_trace`、ターゲットの RAM から吸い出す `snapshot`。

| 形式 | 流れ | 一まとまり | RAM のスナップショット（ポストモーテム） |
|---|---|---|---|
| systemview | RTT（"SysView"。開始のコマンドが要る） | .SVDat・生のダンプ | RTT の上りのリング（RdOff〜WrOff） |
| ctf | RTT・UART・ITM（Zephyr の backend） | CTF のストリームのファイル | `ram_tracing`（RAM の backend） |
| tracealyzer | RTT（"TzData"。開始のコマンドが要る） | PSF のファイル | 読まない（形が違う） |
| tracex | — | TraceX のバッファのダンプ | `_tx_trace_header_ptr` の指すバッファ |
| eventrecorder | — | — | `EventRecorderInfo` の指すバッファ（止めずに読める） |
"""

from __future__ import annotations

import struct
from collections.abc import Callable

from ..rtt import Rtt, RttError
from .ctf import CtfDecoder, decode_ctf, parse_tsdl
from .eventrecorder import (INFO_SIZE, INFO_SYMBOL, RECORD_SIZE, STATUS_SIZE, decode_event_recorder, parse_info,
                            parse_status)
from .model import Trace
from .scvd import Scvd, ScvdContext
from .systemview import CHANNEL_NAME as SYSVIEW_CHANNEL, SystemViewDecoder, decode_systemview
from .tracealyzer import CHANNEL_NAME as TZ_CHANNEL, TracealyzerDecoder, decode_tracealyzer
from .tracex import HEADER_SIZE as TX_HEADER_SIZE, decode_tracex, header_info

__all__ = ["FORMATS", "STREAM_FORMATS", "SNAPSHOT_FORMATS", "TraceStream", "decode_trace", "rtt_ring", "snapshot"]

FORMATS = ("systemview", "ctf", "tracealyzer", "tracex", "eventrecorder")
STREAM_FORMATS = ("systemview", "ctf", "tracealyzer")
SNAPSHOT_FORMATS = ("systemview", "ctf", "tracex", "eventrecorder")
CHANNELS = {"systemview": SYSVIEW_CHANNEL, "tracealyzer": TZ_CHANNEL}
TX_HEADER_POINTER = "_tx_trace_header_ptr"
ZEPHYR_RAM_BUFFER = "ram_tracing"
SNAPSHOT_LIMIT = 4 << 20                  # RAM のバッファを読む上限の既定（snapshot の max_bytes。切ったら truncated）

Read = Callable[[int, int], bytes]


class TraceStream:
    """流れの解読: feed(バイト) → 新しい出来事。trace に全体。"""

    def __init__(self, format: str, decoder):
        self.format, self.decoder = format, decoder

    @classmethod
    def create(cls, format: str, description: str | None = None, metadata: str | None = None,
               ctf_header: str | None = None, strings=None) -> "TraceStream":
        if format == "systemview":
            return cls(format, SystemViewDecoder(synced=False, description=description, strings=strings))
        if format == "ctf":
            return cls(format, CtfDecoder(parse_tsdl(metadata, ctf_header) if metadata else None, ctf_header))
        if format == "tracealyzer":
            return cls(format, TracealyzerDecoder(strings))
        raise ValueError(f"流れで受けられる形式は {' / '.join(STREAM_FORMATS)}")

    @property
    def trace(self) -> Trace:
        return self.decoder.trace

    def feed(self, data: bytes):
        return self.decoder.feed(data)


def decode_trace(format: str, data: bytes, description: str | None = None, metadata: str | None = None,
                 ctf_header: str | None = None, strings=None, ts_hz: float | None = None,
                 ram_buffer: bool = False) -> Trace:
    """一まとまりのデータ → Trace。"""
    if format == "systemview":
        trace = decode_systemview(data, description, strings)
    elif format == "ctf":
        trace = decode_ctf(data, parse_tsdl(metadata, ctf_header) if metadata else None, ctf_header, ram_buffer)
    elif format == "tracealyzer":
        trace = decode_tracealyzer(data, strings)
    elif format == "tracex":
        trace = decode_tracex(data, ts_hz)
    else:
        raise ValueError(f"ファイルから読める形式は systemview / ctf / tracealyzer / tracex（{format} は RAM から）")
    if ts_hz and not trace.ts_hz:
        trace.ts_hz = ts_hz
    return trace


def rtt_ring(memory, address: int, channel: str | int) -> bytes:
    """RTT の上りのリングの中身を、消費せずに（RdOff を書かずに）RdOff から WrOff まで読む（ポストモーテム・
    シングルショットの吸い出し）。channel は名前か番号。"""
    rtt = Rtt(memory, address)
    cb = rtt.open()
    found = [c for c in cb.up if (c.name == channel if isinstance(channel, str) else c.index == channel)]
    if not found:
        names = ", ".join(f"{c.index}:{c.name}" for c in cb.up)
        raise RttError(f"RTT の上りに {channel!r} が無い（{names}）")
    c = found[0]
    wr, rd = struct.unpack("<II", memory.read_memory(c.descriptor_address + 12, 8))
    data = memory.read_memory(c.buffer_address, c.size)
    if wr >= rd:
        return bytes(data[rd:wr])
    return bytes(data[rd:]) + bytes(data[:wr])


def snapshot(format: str, read: Read, symbol: Callable[[str], tuple[int, int] | None], *,
             rtt_address: int | None = None, memory=None, address: int | None = None, size: int | None = None,
             description: str | None = None, metadata: str | None = None,
             ctf_header: str | None = None, strings=None, ts_hz: float | None = None, scvd: Scvd | None = None,
             scvd_context: ScvdContext | None = None, since: int | None = None,
             max_bytes: int = SNAPSHOT_LIMIT) -> tuple[Trace, dict]:
    """ターゲットの RAM から吸い出して解読する。(Trace, 吸い出しの情報) を返す（eventrecorder は next を含む）。
    read(番地, 長さ)・symbol(名前) → (番地, 大きさ)。max_bytes = ctf・tracex のバッファを読む上限: ctf は先頭
    max_bytes だけ読み、情報に size（領域の大きさ）・max_bytes・truncated（切ったか）。tracex は超えれば読まずに誤り。"""
    if max_bytes < 1:
        raise ValueError("max_bytes は 1 以上")
    if format == "systemview":
        if rtt_address is None or memory is None:
            raise ValueError("systemview のスナップショットには RTT の制御ブロックの番地が要る")
        data = rtt_ring(memory, rtt_address, SYSVIEW_CHANNEL)
        return decode_systemview(data, description, strings), {"bytes": len(data), "source": "rtt:SysView"}
    if format == "ctf":
        found = (address, size) if address is not None else symbol(ZEPHYR_RAM_BUFFER)
        if found is None:
            raise ValueError(f"シンボル {ZEPHYR_RAM_BUFFER} が無い（CONFIG_TRACING_BACKEND_RAM）")
        start, length = found
        if not length:
            raise ValueError("ram_tracing の大きさが分からない（size を渡す）")
        data = read(start, min(length, max_bytes))
        trace = decode_ctf(data, parse_tsdl(metadata, ctf_header) if metadata else None, ctf_header, ram_buffer=True)
        return trace, {"bytes": len(data), "size": length, "max_bytes": max_bytes, "truncated": length > max_bytes,
                       "address": start, "source": ZEPHYR_RAM_BUFFER}
    if format == "tracex":
        if address is None:
            found = symbol(TX_HEADER_POINTER)
            if found is None:
                raise ValueError(f"シンボル {TX_HEADER_POINTER} が無い（TX_ENABLE_EVENT_TRACE で作ったか）")
            address = struct.unpack("<I", read(found[0], 4))[0]
            if address == 0:
                raise ValueError("tx_trace_enable がまだ呼ばれていない（_tx_trace_header_ptr が 0）")
        head = read(address, TX_HEADER_SIZE)
        size = header_info(head)["size"]
        if size <= 0:
            raise ValueError(f"TraceX の見出しのバッファの大きさが {size}（正でない）")
        if size > max_bytes:
            raise ValueError(f"TraceX の見出しのバッファの大きさ {size} が読む上限 max_bytes {max_bytes} を超える"
                             "（読んでいない。max_bytes を上げて読める）")
        return decode_tracex(read(address, size), ts_hz), {"bytes": size, "address": address,
                                                            "source": "tracex"}
    if format == "eventrecorder":
        found = (address, INFO_SIZE) if address is not None else symbol(INFO_SYMBOL)
        if found is None:
            raise ValueError(f"シンボル {INFO_SYMBOL} が無い（Event Recorder を組み込んでいない）")
        info = parse_info(read(found[0], INFO_SIZE))
        status = parse_status(read(info.status, STATUS_SIZE))
        buffer = read(info.buffer, info.record_count * RECORD_SIZE)
        trace, nxt = decode_event_recorder(info, status, buffer, scvd, scvd_context, since)
        return trace, {"bytes": len(buffer), "address": info.buffer, "next": nxt, "source": INFO_SYMBOL,
                       "records": info.record_count}
    raise ValueError(f"RAM から吸い出せる形式は {' / '.join(SNAPSHOT_FORMATS)}")
