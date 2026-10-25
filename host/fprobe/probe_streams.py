"""Probe の道具: 生のバイトの流れの TCP での公開とファイルへの記録（U05）。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う mixin。ObserveTools の受信の口（_tap / _untap。RTT の上り・セミホスティング・
SWO・UART）と書き込み（rtt_write・uart_write）を使う。

流れの名前: "rtt0"〜（RTT の上りの番号。書き込みは同じ番号の下りへ）、"swo"（読むだけ）、"uart"、"semihosting"
（書き込みは標準入力へ）。
- stream_serve: 流れを 127.0.0.1 のポートで公開する（SEGGER の RTT Telnet サーバ・OpenOCD の RTT の TCP ポート・
  orbuculum に相当）。何人つながっても全員に同じバイトを配る。待ち受けは常に 127.0.0.1 だけ（認証が無いので、
  デーモンの --lan でも LAN には開かない）。流れの出どころ（rtt_start・swo_start）は別に始める。
- stream_log_start: 流れをファイルに書く（SEGGER の RTT Logger に相当。全チャンネル・SWO・UART に使える）。
- 相手からの書き込みは、デーモンの中なら調停（人の一時停止）と操作の記録を通る（attach_stream_host）。
"""

import re
from pathlib import Path

from .streams import PROTOCOLS, StreamLogger, StreamServer

__all__ = ["StreamTools", "stream_key"]

_RTT = re.compile(r"rtt(\d{1,2})")
_NAMED = ("swo", "uart", "semihosting")


def stream_key(name: str) -> int | str:
    """流れの名前 → 受信の口の鍵（RTT の上りの番号・"swo"・"uart"・"semihosting"）。"""
    m = _RTT.fullmatch(name)
    if m:
        return int(m[1])
    if name in _NAMED:
        return name
    raise ValueError(f"流れ {name!r} は無い（rtt0〜rtt63 / swo / uart / semihosting）")


class StreamTools:
    """流れの公開と記録（Probe の mixin）。"""

    def _init_streams(self) -> None:
        self._servers: dict[str, StreamServer] = {}
        self._stream_loggers: dict[str, StreamLogger] = {}
        self._stream_host = None                              # デーモン（書き込みを調停に通す）
        self._stream_clients: dict[str, object] = {}

    def attach_stream_host(self, host) -> None:
        """書き込みを通す先をつなぐ（デーモンが起動時に呼ぶ）。host.join(名前) → 利用者、host.call(利用者, メソッド,
        引数)、host.leave(利用者)。"""
        self._stream_host = host

    def _close_streams(self) -> None:
        """Probe.close から。"""
        for name in list(self._servers):
            self.stream_close(name)
        for name in list(self._stream_loggers):
            self.stream_log_stop(name)

    # --- 書き込み -------------------------------------------------------------------------------

    @staticmethod
    def _write_call(name: str, key: int | str, data: bytes) -> tuple[str, str, dict]:
        """(API のメソッド, 実装, 引数)。"""
        if key == "uart":
            return "uart.write", "uart_write", {"data": data.decode("latin-1")}
        return "rtt.write", "rtt_write", {"channel": key, "data_hex": data.hex()}

    def _stream_input(self, name: str, key: int | str, data: bytes) -> None:
        method, impl, params = self._write_call(name, key, data)
        host, client = self._stream_host, self._stream_clients.get(name)
        if host is not None and client is not None:
            result = host.call(client, method, params)
        else:
            result = getattr(self, impl)(**params)
        written = result.get("written", len(data)) if isinstance(result, dict) else result
        if written < len(data):                                # ターゲットが読まず、下りが空かなかった
            raise RuntimeError(f"{name}: {len(data)} バイトのうち {written} バイトしか書けなかった")

    # --- 道具 -----------------------------------------------------------------------------------

    def stream_serve(self, stream: str, port: int = 0, protocol: str = "raw", writable: bool = True) -> dict:
        """流れ stream（rtt0… / swo / uart / semihosting）を TCP の 127.0.0.1:port（0 なら空いているポート）で公開する。
        つながった全員が同じバイトを受け取り、送ったバイトはターゲットへ書く（rttN は下り N、uart は UART、
        semihosting は標準入力。swo と writable = False は読むだけ）。protocol: raw / telnet（0xFF の扱い・交渉の命令を
        捨てる）。流れの出どころ（rtt_start・swo_start）は別に始める。"""
        key = stream_key(stream)
        if protocol not in PROTOCOLS:
            raise ValueError(f"protocol は {' / '.join(PROTOCOLS)}（{protocol!r}）")
        if stream in self._servers:
            raise RuntimeError(f"{stream} は公開中（ポート {self._servers[stream].port}。stream_close を先に）")
        writable = writable and key != "swo"
        on_input = (lambda data: self._stream_input(stream, key, data)) if writable else None
        server = StreamServer(stream, port, protocol=protocol, on_input=on_input)   # ポートが使えなければ OSError
        self._servers[stream] = server
        if writable and self._stream_host is not None:
            self._stream_clients[stream] = self._stream_host.join(f"tcp:{stream}")
        self._tap(key, server.publish)
        return {"stream": stream, **server.info()}

    def stream_close(self, stream: str) -> dict:
        """公開をやめる（つながっている相手は切れる）。"""
        server = self._servers.pop(stream, None)
        if server is None:
            raise KeyError(f"{stream} は公開していない")
        self._untap(stream_key(stream), server.publish)
        server.close()
        client = self._stream_clients.pop(stream, None)
        if client is not None:
            self._stream_host.leave(client)
        return {"stream": stream, **server.info()}

    def stream_log_start(self, stream: str, path: str, timestamps: bool = False, append: bool = True) -> dict:
        """流れ stream をファイル path に書き始める（append = False なら書き直す）。timestamps: 行の頭にこの PC の時刻
        を付ける（文字の流れ用。バイナリの流れには付けない）。"""
        key = stream_key(stream)
        if stream in self._stream_loggers:
            raise RuntimeError(f"{stream} は記録中（{self._stream_loggers[stream].path}。stream_log_stop を先に）")
        logger = StreamLogger(Path(path), timestamps=timestamps, append=append)
        self._stream_loggers[stream] = logger
        self._tap(key, logger.write)
        return {"stream": stream, **logger.info()}

    def stream_log_stop(self, stream: str) -> dict:
        """ファイルへの記録をやめる。"""
        logger = self._stream_loggers.pop(stream, None)
        if logger is None:
            raise KeyError(f"{stream} は記録していない")
        self._untap(stream_key(stream), logger.write)
        logger.close()
        return {"stream": stream, **logger.info()}

    def stream_list(self) -> dict:
        """公開中の流れ（ポート・相手ごとの送ったバイトと捨てたバイト・書き込みの誤り）とファイルへの記録、
        流れの出どころの様子（RTT の上りのチャンネル・SWO の受信中か・背景の読みの誤り）。"""
        rtt = self._rtt
        sources = {"rtt": [f"rtt{c.index}" for c in rtt.control_block.up if c.in_use] if rtt is not None else [],
                   "swo": self._swo_on, "errors": dict(self._pump_errors)}
        return {"served": [{"stream": name, **s.info()} for name, s in list(self._servers.items())],
                "logs": [{"stream": name, **lg.info()} for name, lg in list(self._stream_loggers.items())],
                "sources": sources}
