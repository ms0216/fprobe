"""SWO の受信（swo_start / swo_read / swo_stop）のサービス。DWT・ITM の設定とトレースは fprobe.probe_swo（SwoService）。

Probe（fprobe.probe）が API の名前を委ねるサービス（fprobe.probe_service。ホストから使う名前は NEEDS）。Probe の _lock・target・_debug・session・_swo_baud・_swo_mode と、SwoService の
_swo_reset / _swo_on、ObserveService の _collect / _observe / _logs を使う。
"""

from .api import NotConnected
from .probe_observe import ByteLog
from .session import SWO_MODES, swo_start as swo_start_mode
from .probe_service import HostView


class SwoReceiveService:
    """Probe が委ねる SWO の受信の道具。"""

    API = ("swo_read", "swo_start", "swo_stop")
    # ほかの領域（Probe の委譲）・試験に出す名前
    SHARED = ("_swo_baud", "_swo_mode")
    # ホスト（Probe の核と、ほかの領域）から使う名前
    NEEDS = ("_collect", "_debug", "_lock", "_logs", "_observe", "_swo_on", "_swo_reset", "session", "target")

    def __init__(self, host):
        self._host = HostView(host, self.NEEDS, type(self).__name__)
        self._swo_baud: int | None = None                 # swo_start の速度（時刻の分解能の見積もりに使う）
        self._swo_mode = "uart"                           # swo_start の方式（uart / manchester）

    def swo_start(self, baud: int = 2_000_000, mode: str = "uart") -> None:
        """SWO の受信を始める。mode: "uart"（NRZ。TPIU の SPPR = 2）/ "manchester"（SPPR = 1。ケイパビリティの
        swo_manchester がある構成だけ。ビットごとに同期し直すので、送り手との速さのずれに強い）。ターゲット側の
        TPIU（ACPR = TRACECLKIN / baud - 1、SPPR）と ITM はファームウェアかデバッガで設定しておくこと。
        受けられる速さ（sys 100 MHz）: UART は 25 Mbaud まで（速さが一致すること。divisor = 100 MHz / baud が
        小さいほどずれに弱い）、Manchester は 16.7〜20 Mbit/s までが実用。"""
        if mode not in SWO_MODES:
            raise ValueError(f"SWO の方式は {'/'.join(SWO_MODES)}: {mode!r}")
        with self._host._lock:
            self._host.target                                    # 未接続ならここで失敗する
            if mode == "manchester" and not self._host.session.device.capabilities().swo_manchester:
                raise RuntimeError("この構成（ゲートウェア）は SWO の Manchester 方式を受けられない（swo_manchester なし）")
            # 止めてから設定する: pyOCD の swo_start は設定の失敗を黙って捨てる。受信中に速さを変えると、Mode を
            # 受信中に受けない版のゲートウェア（2026-10-18 より前）では前の速さのまま受けていた
            self._host._debug.probe.swo_stop()
            if mode == "uart":
                self._host._debug.probe.swo_start(baud)
            else:
                swo_start_mode(self._host._debug.probe, baud, mode)
            self._swo_mode = mode
            self._host._swo_reset()                              # 解読（swo_decode / swo_text）は受信の最初から
            self._swo_baud = baud
            self._host._swo_on = True
            with self._host._observe:
                self._host._logs["swo"] = ByteLog()              # 前の受信の残りは読ませない

    def swo_read(self, timeout_s: float = 0.5) -> str:
        """timeout_s の間に届いた SWO のバイト列（ITM パケットのまま、16 進文字列）。背景で読んでいる（TCP の口・
        ファイルへ流している）なら、前回の読みからの分も含む。"""
        with self._host._lock:
            self._host.target                                    # 未接続ならここで失敗する
        return self._host._collect("swo", timeout_s).hex()

    def swo_stop(self) -> None:
        """SWO の受信を止める。"""
        with self._host._lock:
            if self._host._debug is None:                        # 未接続（スナップショットの読み手だけでは止める SWO が無い）
                raise NotConnected("ターゲット未接続（MCU なら target.connect を先に呼ぶ。MCP の道具名は target_connect。Linux のプロセスなら linux.halt.open → linux.halt.debug.attach）")
            self._host._swo_on = False
            self._host._debug.probe.swo_stop()
