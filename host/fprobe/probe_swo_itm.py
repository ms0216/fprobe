"""Probe の道具: SWO の ITM / DWT のパケットの解読（swo_decode・swo_text）と、解読したパケットを受け取る口。

Probe（fprobe.probe）が API の名前を委ねるサービス（fprobe.probe_service。ホストから使う名前は NEEDS）。SWO のバイトの
受信（swo_start・swo_read）は SwoReceiveService、バイトの貯め場は ObserveService の "swo" で、ここはそのバイトを
ITM のパケットに解読する層（fprobe.itm）。受信の口は 1 つのまま、swo_decode・swo_text・プロファイル等のどれが読んでも
全員が同じパケットを受け取る（_swo_listen）。記録中（_recorder）なら例外トレース・PC サンプル・スティミュラス・
データトレース・オーバーフローと printf の行を共通の時間軸の記録に入れる。

仕様: docs/superpowers/specs/2026-10-17-f1-rtt-itm-semihosting-design.md
"""

import collections
import dataclasses
import enum
import time

from .itm import (DataTraceAddress, DataTracePc, DataTraceValue, DecodeError, ExceptionTrace, Instrumentation,
                  ItmDecoder, Overflow, PcSample, PortText, Timestamper)
from .probe_service import HostView
from .profile.exceptions import exception_name as _exception_name_of

__all__ = ["SwoDecodeService"]

SWO_LINES_LIMIT = 10_000               # ポートごとに残す行数


def _jsonable_packet(packet) -> dict:
    out = {"type": type(packet).__name__}
    for f in dataclasses.fields(packet):
        if f.name == "offset":
            continue
        value = getattr(packet, f.name)
        out[f.name] = value.name.lower() if isinstance(value, enum.Enum) else \
            value.hex() if isinstance(value, (bytes, bytearray)) else value
    if isinstance(packet, ExceptionTrace):
        out["name"] = _exception_name(packet.number)
    return out


def _exception_name(number: int) -> str:
    return _exception_name_of(number)                 # 名前の表は profile/exceptions.py の 1 か所


class SwoDecodeService:
    """SWO の ITM の解読（Probe のサービス）。"""

    API = ("swo_decode", "swo_text")
    # ほかの領域（Probe の委譲）・試験に出す名前
    SHARED = ("_itm", "_swo_drain", "_swo_listen", "_swo_listeners", "_swo_receive", "_swo_reset", "_swo_unlisten")
    # ホスト（Probe の核と、ほかの領域）から使う名前
    NEEDS = ("_collect", "_lock", "_observe", "_record_host", "_recorder", "target")

    def __init__(self, host):
        self._host = HostView(host, self.NEEDS, type(self).__name__)
        self._swo_reset()
        self._swo_listeners: list = []                             # 解読したパケットを受け取る道具（プロファイル等）

    def _swo_reset(self) -> None:
        self._itm = ItmDecoder()
        self._timestamper = Timestamper()
        self._port_text = PortText()
        self._swo_lines: dict[int, collections.deque] = {}
        self._swo_overflows = 0
        self._swo_errors = 0

    def _swo_receive(self, timeout_s: float) -> tuple[int, list[dict]]:
        """timeout_s の間に届いた SWO を解読する。(バイト数, パケット)。行はポートごとに貯める。"""
        with self._host._lock:
            self._host.target                                            # 未接続ならここで失敗する
        data = self._host._collect("swo", timeout_s)
        now = time.monotonic()
        packets = self._itm.feed(bytes(data))
        for listener in list(self._swo_listeners):
            listener(packets)
        timed = self._timestamper.feed(packets) + self._timestamper.flush()
        lines = self._port_text.feed(packets)
        with self._host._observe:
            for line in lines:
                self._swo_lines.setdefault(line.port, collections.deque(maxlen=SWO_LINES_LIMIT)).append(line.text)
        out = []
        for t in timed:
            d = _jsonable_packet(t.packet)
            d.update(local_time=t.local_time, global_time=t.global_time)
            out.append(d)
            self._swo_overflows += isinstance(t.packet, Overflow)
            self._swo_errors += isinstance(t.packet, DecodeError)
            self._record_packet(t.packet, now)
        for line in lines:
            self._host._record_host("swo", "line", now, a=line.port, body={"port": line.port, "text": line.text})
        return len(data), out

    def _swo_drain(self, quiet_s: float = 0.03, limit_s: float = 0.5) -> int:
        """SWO の受信に残っている古いバイトを捨てる（quiet_s の間なにも来なくなるまで、最長 limit_s）。捨てたバイト数。
        受信を始めた直後は、前の受信の残り（機器の FIFO）が先に届き、新しい流れとの継ぎ目で解読がずれる
        （2026-10-18 に実機で）。ターゲットが出力を止めている間に呼ぶ。"""
        dropped, last, deadline = 0, time.monotonic(), time.monotonic() + limit_s
        while time.monotonic() < deadline:
            chunk = self._host._collect("swo", 0)
            now = time.monotonic()
            if chunk:
                dropped, last = dropped + len(chunk), now
            elif now - last >= quiet_s:
                break
            time.sleep(0.005)
        self._swo_reset()
        return dropped

    def _swo_listen(self, listener) -> None:
        """SWO の受信を解読したパケット（fprobe.itm.Packet の列）を listener にも渡す。受信の口は 1 つのまま
        （swo_decode・swo_text・プロファイルのどれが読んでも、全員が同じパケットを受け取る）。"""
        self._swo_listeners.append(listener)

    def _swo_unlisten(self, listener) -> None:
        if listener in self._swo_listeners:
            self._swo_listeners.remove(listener)

    def _record_packet(self, p, host_s: float) -> None:
        if self._host._recorder is None:
            return
        record = self._host._record_host
        if isinstance(p, ExceptionTrace):
            record("swo", "exception", host_s, a=p.number,
                   body={"action": p.action.name.lower(), "name": _exception_name(p.number)})
        elif isinstance(p, PcSample):
            record("swo", "pc_sample", host_s, a=p.pc or 0, body={"sleep": p.pc is None})
        elif isinstance(p, Instrumentation):
            record("swo", "stimulus", host_s, a=p.port, b=p.value, body={"size": p.size})
        elif isinstance(p, (DataTracePc, DataTraceAddress, DataTraceValue)):
            record("swo", "data_trace", host_s, body=_jsonable_packet(p))
        elif isinstance(p, Overflow):
            record("swo", "overflow", host_s)

    def _take_lines(self, port: int | None) -> dict[int, list[str]]:
        with self._host._observe:
            ports = list(self._swo_lines) if port is None else [port]
            out = {p: list(self._swo_lines.get(p, ())) for p in ports}
            for p in ports:
                self._swo_lines.pop(p, None)
            return out

    def swo_decode(self, timeout_s: float = 0.5, limit: int = 500) -> dict:
        """timeout_s の間に届いた SWO を ITM / DWT のパケットに解読する（swo_start の後。swo_read と同じ受信を使うので
        どちらか一方で読む）。packets は先頭 limit 件（例外トレース・PC サンプル・スティミュラス等）、text はポートごとの
        printf の行（呼び出しをまたいで行を組み立てる）。"""
        received, packets = self._swo_receive(timeout_s)
        return {"bytes": received, "synced": self._itm.synced, "unsynced_bytes": self._itm.unsynced_bytes,
                "packets": packets[:limit],
                "dropped": max(0, len(packets) - limit), "text": self._take_lines(None),
                "overflows": self._swo_overflows, "errors": self._swo_errors}

    def swo_text(self, port: int = 0, timeout_s: float = 0.5) -> dict:
        """timeout_s の間に届いた SWO から、ITM のポート port に printf された行（pending は改行の来ていない途中）。"""
        self._swo_receive(timeout_s)
        return {"port": port, "lines": self._take_lines(port)[port], "pending": self._port_text.pending(port)}
