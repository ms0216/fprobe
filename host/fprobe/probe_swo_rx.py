"""SWO の受信（swo_start / swo_read / swo_stop）の mixin。DWT・ITM の設定とトレースは fprobe.probe_swo（SwoTools）。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う。Probe の _lock・target・_debug・session・_swo_baud・_swo_mode と、SwoTools の
_swo_reset / _swo_on、ObserveTools の _collect / _observe / _logs を使う。
"""

from .api import NotConnected
from .probe_observe import ByteLog
from .session import SWO_MODES, swo_start as swo_start_mode


class SwoReceiveTools:
    """Probe に混ぜる SWO の受信の道具。"""

    def swo_start(self, baud: int = 2_000_000, mode: str = "uart") -> None:
        """SWO の受信を始める。mode: "uart"（NRZ。TPIU の SPPR = 2）/ "manchester"（SPPR = 1。ケイパビリティの
        swo_manchester がある構成だけ。ビットごとに同期し直すので、送り手との速さのずれに強い）。ターゲット側の
        TPIU（ACPR = TRACECLKIN / baud - 1、SPPR）と ITM はファームウェアかデバッガで設定しておくこと。
        受けられる速さ（sys 100 MHz）: UART は 25 Mbaud まで（速さが一致すること。divisor = 100 MHz / baud が
        小さいほどずれに弱い）、Manchester は 16.7〜20 Mbit/s までが実用。"""
        if mode not in SWO_MODES:
            raise ValueError(f"SWO の方式は {'/'.join(SWO_MODES)}: {mode!r}")
        with self._lock:
            self.target                                    # 未接続ならここで失敗する
            if mode == "manchester" and not getattr(self.session.device.capabilities(), "swo_manchester", False):
                raise RuntimeError("この構成（ゲートウェア）は SWO の Manchester 方式を受けられない（swo_manchester なし）")
            # 止めてから設定する: pyOCD の swo_start は設定の失敗を黙って捨てる。受信中に速さを変えると、Mode を
            # 受信中に受けない版のゲートウェア（2026-10-18 より前）では前の速さのまま受けていた
            self._debug.probe.swo_stop()
            if mode == "uart":
                self._debug.probe.swo_start(baud)
            else:
                swo_start_mode(self._debug.probe, baud, mode)
            self._swo_mode = mode
            self._swo_reset()                              # 解読（swo_decode / swo_text）は受信の最初から
            self._swo_baud = baud
            self._swo_on = True
            with self._observe:
                self._logs["swo"] = ByteLog()              # 前の受信の残りは読ませない

    def swo_read(self, timeout_s: float = 0.5) -> str:
        """timeout_s の間に届いた SWO のバイト列（ITM パケットのまま、16 進文字列）。背景で読んでいる（TCP の口・
        ファイルへ流している）なら、前回の読みからの分も含む。"""
        with self._lock:
            self.target                                    # 未接続ならここで失敗する
        return self._collect("swo", timeout_s).hex()

    def swo_stop(self) -> None:
        """SWO の受信を止める。"""
        with self._lock:
            if self._debug is None:                        # 未接続（スナップショットの読み手だけでは止める SWO が無い）
                raise NotConnected("ターゲット未接続（target_connect を先に呼ぶ）")
            self._swo_on = False
            self._debug.probe.swo_stop()
