"""システム（MCU と FPGA を並べて）の mixin: system_status。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う。Probe の _lock・session・target・_caps・target_status と、FpgaObsTools の
fpga_obs_status / fpga_obs_read / fpga_obs_signals_get を使う。
"""

from .api import NotConnected
from .fpga_signals import FpgaSignals


class SystemTools:
    """Probe に混ぜるシステム全体の見回し。"""

    def system_status(self, addresses: list[int] | None = None, fpga_last: int = 2000) -> dict:
        """MCU と FPGA の両方の今を 1 回で返す（システム解析の画面・AI の見回り用。どちらかが無くても他方は返す）。
        mcu: target_status と、addresses の 32 ビットの語（止めずに読む。例: ファームウェアのメールボックス）・connect
        （最後に成功した target_connect の引数。対象のリセットで SWD がはずれたら同じ引数でつなぎ直す）。
        fpga: 観測の状態（型・スキャンの数・溢れ）と、最近の値（fpga_obs_read の最後の fpga_last 件のうち、各組の最新の
        値と信号。窓から消えても最後に分かった値を返す。time_us で古さがわかる）。
        値を読むにはイベントの記録（events_start）が要る。記録型は窓の中に印が無いと値の時刻が決まらず
        出ないので、窓は広め（既定 2000 件）。"""
        with self._lock:
            device = self.session.device
            out: dict = {"time_us": device.time() * 1e6 / self._caps().timebase_hz}
            try:
                mcu = {"connected": True, **self.target_status()}
                mcu["memory"] = {f"0x{a:08X}": self.target.read32(a) for a in addresses or []}
            except NotConnected:
                mcu = {"connected": False}
            except Exception as e:                           # 読めない番地等: 理由を返す
                mcu = {"connected": True, "error": str(e)}
            mcu["connect"] = self._last_connect
            out["mcu"] = mcu
            if not getattr(self._caps(), "fpga_obs", False):
                out["fpga"] = {"available": False}
                return out
            fpga: dict = {"available": True, **self.fpga_obs_status()}
            try:
                items = self.fpga_obs_read(last=fpga_last)["items"]
                # 最後に分かった値を覚えておく（値が変わらないと窓から消える。MCU を止めた瞬間の FPGA こそ見たい）
                latest = self._fpga_latest
                for it in items:
                    if it["kind"] == "value":
                        latest[it["group"]] = it
                defs = self.fpga_obs_signals_get()                # 名前は今の定義で（定義を変えた後も古い名前を出さない）
                signals = FpgaSignals.from_dict(defs["recording"] or defs["signals"])
                fpga["latest"] = [{**latest[g], "fields": signals.fields(g, latest[g]["value"])} for g in sorted(latest)]
                fpga["gaps"] = sum(1 for it in items if it["kind"] == "gap")
                fpga["window"] = {"last": fpga_last, "items": len(items)}     # 見た窓（latest のうち窓の外のものもある）
            except Exception as e:                           # イベントの記録を始めていない等
                fpga["latest_error"] = str(e)
            out["fpga"] = fpga
            return out
