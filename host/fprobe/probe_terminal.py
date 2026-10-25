"""Probe の道具: 画面の端末（G04）の口。RTT の上りのチャンネル・SWO の ITM のスティミュラス・ポート・UART・
セミホスティングの出力を、同じ形（位置 since から読む。ほかの読み手の位置は動かさない）で読む。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う mixin。ObserveTools の貯め場（_logs・_observe）と受信の口（_tap）を使う。

- 源の名前: "rtt0"〜（RTT の上り）、"swo0"〜"swo31"（ITM のポート。printf の出力）、"uart"、"semihosting"。
- RTT・セミホスティングは ObserveTools が背景で貯めている。UART・SWO は、初めて読んだときから背景で受け始める
  （stream_serve と同じ口。UART は uart_read の読みにも残る）。SWO はポートごとに分けて貯める（swo_start は別に呼ぶ）。
- 書き込みは既存の rtt_write（RTT の下り・"semihosting" の標準入力）と uart_write。UART の速さは uart_configure。
- 受け取りごとの時刻（times）: 壁時計の秒と、記録中ならタイムベースの時刻（画面は行の時刻を出し、時間軸のその時刻へ
  飛ぶ）。

仕様: docs/superpowers/specs/2026-10-18-ui-layout-design.md（Terminal）、docs/reports/2026-10-18-gap-analysis.md G04
"""

import re
import time

from .itm import Instrumentation, ItmDecoder
from .probe_observe import SEMIHOSTING, ByteLog, _incomplete_utf8_tail

__all__ = ["TerminalTools"]

_SOURCE = re.compile(r"(rtt|swo)(\d{1,2})|uart|semihosting")
_TERMINAL_MARK = b"\xff"                                      # SEGGER_RTT_SetTerminal: 0xFF の後に番号の文字
_TERMINAL_IDS = b"0123456789ABCDEF"


def _split_terminals(data: bytes) -> tuple[bytes, list[tuple[int, int]], list[int]]:
    """SEGGER の仮想端末の切り替え（0xFF と '0'〜'F'）を除く。(除いた後のバイト列, [(除いた後の位置, 端末の番号)],
    除いたバイトの元の位置) を返す。番号でない文字が続く 0xFF は文字のまま残す。"""
    out, switches, removed = bytearray(), [], []
    i = 0
    while i < len(data):
        b = data[i]
        if b == 0xFF and i + 1 < len(data) and data[i + 1] in _TERMINAL_IDS:
            switches.append((len(out), _TERMINAL_IDS.index(data[i + 1])))
            removed += [i, i + 1]
            i += 2
            continue
        out.append(b)
        i += 1
    return bytes(out), switches, removed


class TerminalTools:
    """端末の口（Probe の mixin）。"""

    def _init_terminal(self) -> None:
        self._term_itm: ItmDecoder | None = None             # SWO をポートごとに分ける解読器（受信の口 _tap に付ける）
        self._term_ports: dict[int, ByteLog] = {}
        self._term_uart = False                               # UART を背景で受けている

    def terminal_sources(self) -> dict:
        """端末に出せる源の一覧（機器には触れない）: id・kind（rtt / swo / uart / semihosting）・name・writable
        （書ける: RTT は同じ番号の下りがあるとき、UART・セミホスティングの標準入力）・received（貯めた量）。"""
        out = []
        rtt = self._rtt
        with self._observe:
            if rtt is not None:
                cb = rtt.control_block
                downs = {c.index for c in cb.down}
                for c in cb.up:
                    log = self._logs.get(c.index)
                    out.append({"id": f"rtt{c.index}", "kind": "rtt", "channel": c.index, "name": c.name or f"RTT {c.index}",
                                "writable": c.index in downs, "received": log.end if log else 0})
            if self._swo_on or self._term_ports:
                for port in sorted(set(self._term_ports) | {0}):
                    log = self._term_ports.get(port)
                    out.append({"id": f"swo{port}", "kind": "swo", "port": port, "name": f"ITM {port}", "writable": False,
                                "received": log.end if log else 0})
            uart = self._logs.get("uart")
            out.append({"id": "uart", "kind": "uart", "name": "UART", "writable": True, "received": uart.end if uart else 0,
                        "baud": self._uart_baud_now()})
            sh = self._logs.get(SEMIHOSTING)
            out.append({"id": SEMIHOSTING, "kind": SEMIHOSTING, "name": "Semihosting", "writable": True,
                        "received": sh.end if sh else 0})
        return {"sources": out, "rtt": rtt is not None, "swo": self._swo_on}

    def terminal_read(self, source: str, since: int = 0, max_bytes: int = 65536, timeout_s: float = 0.0,
                      times: bool = False, terminals: bool = False) -> dict:
        """源 source の受信を位置 since から読む（UTF-8 の文字列。ほかの読み手の位置は動かさない）。timeout_s まで
        データを待つ。next を次の since に渡す。lost は貯め場からあふれて読めなかったバイト数。times なら受け取りごとの
        時刻 times = [{at（data の中の文字の位置）, wall（壁時計の秒）, t（記録中に受けたものはタイムベースの時刻、
        ほかは null）}] と timebase_hz（記録中なら）も。terminals なら（RTT の源だけ）SEGGER の仮想端末の切り替え
        （0xFF と番号 '0'〜'F'）を文字から除き、terminals = [{at（そこから後の文字の位置）, id（0〜15）}] に返す。"""
        log_of = self._terminal_log(source)
        terminals = terminals and source.startswith("rtt")
        deadline = time.monotonic() + timeout_s
        with self._observe:
            while True:
                log = log_of()
                if (log is not None and since < log.end) or time.monotonic() >= deadline:
                    break
                self._observe.wait(deadline - time.monotonic())
            if log is None:
                return {"source": source, "data": "", "start": since, "next": since, "lost": 0,
                        **({"times": [], "timebase_hz": None} if times else {}),
                        **({"terminals": []} if terminals else {})}
            data, start, lost = log.read(since, max_bytes)
            data = data[:len(data) - _incomplete_utf8_tail(data)]
            if terminals and data.endswith(_TERMINAL_MARK):
                data = data[:-1]                              # 番号がまだ来ていない切り替え（次の読みで）
            marks = log.times(start, len(data)) if times else []
        end = start + len(data)
        text, switches, removed = _split_terminals(data) if terminals else (data, [], [])

        def chars(pos: int) -> int:                           # バイトの位置 → 文字の位置
            return len(text[:pos].decode("utf-8", errors="replace"))

        def moved(pos: int) -> int:                           # 元のバイトの位置 → 切り替えを除いた後の位置
            return pos - sum(1 for r in removed if r < pos)

        out = {"source": source, "data": text.decode("utf-8", errors="replace"), "start": start, "next": end, "lost": lost}
        if times:
            rec = self._recorder
            out["times"] = [{"at": chars(moved(pos - start)), "wall": round(wall, 6), "t": t} for pos, wall, t in marks]
            out["timebase_hz"] = rec.timebase_hz if rec is not None else None
        if terminals:
            out["terminals"] = [{"at": chars(pos), "id": number} for pos, number in switches]
        return out

    def _terminal_log(self, source: str):
        """源 → その貯め場を返す関数（_observe の中で呼ぶ）。UART・SWO は背景の受信を始める。"""
        m = _SOURCE.fullmatch(source)
        if m is None:
            raise ValueError(f"源 {source!r} は無い（rtt0〜 / swo0〜swo31 / uart / semihosting）")
        if source == SEMIHOSTING:
            return lambda: self._logs.get(SEMIHOSTING)
        if source == "uart":
            if not self._term_uart:
                self._term_uart = True
                self._tap("uart", lambda data: None)          # 背景で受ける（貯め場 _logs["uart"] に入る）
            return lambda: self._logs.get("uart")
        number = int(m[2])
        if m[1] == "rtt":
            return lambda: self._logs.get(number)
        if number > 31:
            raise ValueError("ITM のポートは 0〜31")
        if self._term_itm is None:
            self._term_itm = ItmDecoder()
            self._tap("swo", self._terminal_swo)
        return lambda: self._term_ports.get(number)

    def _terminal_swo(self, data: bytes) -> None:
        """受けた SWO を ITM のパケットにし、スティミュラス・ポートの書き込みをポートごとに貯める（背景のスレッド）。"""
        packets = self._term_itm.feed(data)
        wall, t = self._stamp(time.monotonic())                # 受けた直後（背景のスレッドの tap の中）
        with self._observe:
            for p in packets:
                if isinstance(p, Instrumentation):
                    self._term_ports.setdefault(p.port, ByteLog()).append(p.value.to_bytes(p.size, "little"), wall, t)
            self._observe.notify_all()
