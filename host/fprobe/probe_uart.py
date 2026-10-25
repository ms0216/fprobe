"""UART（uart_*）の mixin。USB では CDC-ACM、フレームのリンクでは ch 3 = UART0。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う。Probe の _lock・_uart・_serial・_session・session と、
ObserveTools の _collect / _received を使う。
"""

import time

from serial import Serial
from serial.tools import list_ports

from . import generated as g
from .frame_link import FrameLink
from .uart_flow import UartFlow

UART_DEFAULT_BAUD = 115_200            # UART を頼まれた速さ無しで開くときの速さ（使った速さは結果の baud に出す）


class UartTools:
    """Probe に混ぜる UART の道具。"""

    def _uart_link(self, baud: int | None) -> FrameLink | None:
        """フレームのリンクで開いているならそのリンク（ボーレートを LINE_CODING で合わせる。頼まれず、まだ決まって
        いなければ UART_DEFAULT_BAUD。使った速さは結果の baud に出す）。USB なら None。"""
        link = self.session.link
        if link is not None:
            want = baud or link.uart_baud or UART_DEFAULT_BAUD
            if link.uart_baud != want:
                link.set_uart_baud(want)
        return link

    def _uart_port(self, baud: int | None) -> Serial:
        if self._uart is None:
            serial_number = self._serial
            ports = [p.device for p in list_ports.comports() if p.vid == g.USB_VID and p.pid == g.USB_PID
                     and (serial_number is None or p.serial_number == serial_number)]
            if len(ports) != 1:
                raise RuntimeError(f"fprobe の COM ポートが特定できない: {ports}")
            self._uart = Serial(ports[0], baud or UART_DEFAULT_BAUD, timeout=0)
        elif baud and self._uart.baudrate != baud:
            self._uart.baudrate = baud                     # ボーレートはライン設定でプローブに伝わる
        return self._uart

    def uart_write(self, data: str, baud: int | None = None) -> dict:
        """ターゲットの UART に送る（latin-1）。baud を指定するとボーレートも変える。written（送ったバイト数）と、
        使ったボーレート baud（頼まれず、まだ開いていなければ UART_DEFAULT_BAUD）。"""
        with self._lock:
            if link := self._uart_link(baud):
                return {"written": link.uart_write(data.encode("latin-1")), "baud": link.uart_baud}
            port = self._uart_port(baud)
            n = port.write(data.encode("latin-1"))
            port.flush()
            return {"written": n, "baud": port.baudrate}

    def _uart_fetch(self, baud: int | None = None) -> None:
        """UART の届いた分を読んで貯める（_lock の中で）。"""
        link = self._uart_link(baud)
        data = link.uart_read(4096) if link else self._uart_port(baud).read(4096)
        if data:
            self._received("uart", bytes(data), time.monotonic())

    def uart_read(self, timeout_s: float = 0.5, baud: int | None = None) -> dict:
        """timeout_s の間に届いたもの data（latin-1 で文字列化）と、受けたボーレート baud（頼まれず、まだ開いて
        いなければ UART_DEFAULT_BAUD）。背景で読んでいる（TCP の口・ファイルへ流している）なら、前回の読みからの分も含む。"""
        if baud is not None:
            with self._lock:
                self._uart_fetch(baud)                     # ボーレートを先に合わせる
        data = self._collect("uart", timeout_s).decode("latin-1")
        with self._lock:
            return {"data": data, "baud": self._uart_baud_now()}

    def uart_configure(self, baud: int) -> dict:
        """ターゲットの UART のボーレートを変える（送らない・読まない。端末の画面の設定）。変えた後の baud を返す。"""
        with self._lock:
            if link := self._uart_link(baud):
                return {"baud": link.uart_baud}
            return {"baud": self._uart_port(baud).baudrate}

    def uart_flow(self, auto: bool = False, rts_channel: int | None = None, cts_channel: int | None = None) -> dict:
        """UART のハードウェアのフロー制御（U52）。rts_channel / cts_channel: RTS を出す / CTS を読む端子（ロジアナの
        ch。候補はケイパビリティ uart_flow = 結果の pins。Primer は 4 予備・1 TDI。選んだ端子の普段の機能の出力は止める。
        None で使わない）。auto: 自動の RTS/CTS（CTS が出ている間だけ送り、受信の下流が満杯なら RTS を下げる。CTS を
        取らなければ CTS は出ているとみなす）。auto = False なら RTS は COM ポートの RTS どおり。線はアクティブ Low。
        設定と制御線の状態を返す。"""
        with self._lock:
            flow = UartFlow(self.session.device)
            flow.configure(auto, rts_channel, cts_channel)
            return self._uart_flow_state(flow)

    def uart_lines(self) -> dict:
        """UART の制御線の状態（U52）: cts（CTS が出ている）・rts（RTS を出している）・host_rts（COM ポートの RTS）・
        host_break（ブレークを送っている）と、フロー制御の設定（uart_flow）。"""
        with self._lock:
            return self._uart_flow_state(UartFlow(self.session.device))

    @staticmethod
    def _uart_flow_state(flow: UartFlow) -> dict:
        lines = flow.lines()
        return {**flow.config(), "pins": list(flow.pins), "cts": lines.cts, "rts": lines.rts,
                "host_rts": lines.host_rts, "host_break": lines.host_break}

    def _uart_baud_now(self) -> int | None:
        """いまの UART のボーレート（まだ開いていなければ None）。機器には触らない。"""
        link = getattr(self._session, "link", None)
        if link is not None:
            return getattr(link, "uart_baud", None)
        return self._uart.baudrate if self._uart is not None else None
