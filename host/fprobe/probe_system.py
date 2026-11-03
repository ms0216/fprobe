"""システム（MCU と FPGA を並べて）のサービス: system_status。

Probe（fprobe.probe）が API の名前を委ねるサービス（fprobe.probe_service。ホストから使う名前は NEEDS）。Probe の _lock・session・target・_caps・target_status と、FpgaObsService の
fpga_obs_status / fpga_obs_read / fpga_obs_signals_get を使う。
"""

from .api import NotConnected
from .fpga_signals import FpgaSignals
from .probe_service import HostView


class SystemService:
    """Probe が委ねるシステム全体の見回し。"""

    API = ("system_status",)
    SHARED = ()
    # ホスト（Probe の核と、ほかの領域）から使う名前
    NEEDS = ("_caps", "_fpga_latest", "_last_connect", "_linux_attaching", "_linux_view", "_lock", "fpga_obs_read",
             "fpga_obs_signals_get",
             "fpga_obs_status", "session", "target", "target_status")

    def __init__(self, host):
        self._host = HostView(host, self.NEEDS, type(self).__name__)

    def system_status(self, addresses: list[int] | None = None, fpga_last: int = 2000) -> dict:
        """MCU と FPGA の両方の今を 1 回で返す（システム解析の画面・AI の見回り用。どちらかが無くても他方は返す）。
        mcu: target_status と、addresses の 32 ビットの語（止めずに読む。例: ファームウェアのメールボックス）・connect
        （最後に成功した target_connect の引数。対象のリセットで SWD がはずれたら同じ引数でつなぎ直す）。
        fpga: 観測の状態（型・スキャンの数・溢れ）と、最近の値（fpga_obs_read の最後の fpga_last 件のうち、各組の最新の
        値と信号。窓から消えても最後に分かった値を返す。time_us で古さがわかる）。
        値を読むにはイベントの記録（events_start）が要る。記録型は窓の中に印が無いと値の時刻が決まらず
        出ないので、窓は広め（既定 2000 件）。"""
        with self._host._lock:
            device = self._host.session.device
            out: dict = {"time_us": device.time() * 1e6 / self._host._caps().timebase_hz}
            try:
                view = self._host._linux_view()
                mcu = {"connected": True, **self._host.target_status()}
                mcu["memory"] = (view.read_words(addresses or []) if view is not None   # Linux: プロセスの仮想メモリ
                                 else {f"0x{a:08X}": self._host.target.read32(a) for a in addresses or []})
            except NotConnected:
                mcu = {"connected": False}
                attaching = self._host._linux_attaching()
                if attaching is not None:                    # Linux のプロセスへの attach が進行中（まだ対象が無い）
                    mcu["linux_attaching"] = attaching
            except Exception as e:                           # 読めない番地等: 理由を返す
                mcu = {"connected": True, "error": str(e)}
            mcu["connect"] = self._host._last_connect
            out["mcu"] = mcu
            if not self._host._caps().fpga_obs:
                out["fpga"] = {"available": False}
                return out
            fpga: dict = {"available": True, **self._host.fpga_obs_status()}
            try:
                items = self._host.fpga_obs_read(last=fpga_last)["items"]
                # 最後に分かった値を覚えておく（値が変わらないと窓から消える。MCU を止めた瞬間の FPGA こそ見たい）
                latest = self._host._fpga_latest
                for it in items:
                    if it["kind"] == "value":
                        latest[it["group"]] = it
                defs = self._host.fpga_obs_signals_get()                # 名前は今の定義で（定義を変えた後も古い名前を出さない）
                signals = FpgaSignals.from_dict(defs["recording"] or defs["signals"])
                # 経過は値を読み終えた後の時刻から（画面で引き算しない。読む前の時刻を使うと新しい値で負になる）
                now_us = device.time() * 1e6 / self._host._caps().timebase_hz
                fpga["latest"] = [{**latest[g], "fields": signals.fields(g, latest[g]["value"]),
                                   "age_us": None if latest[g]["time_us"] is None else now_us - latest[g]["time_us"]}
                                  for g in sorted(latest)]
                fpga["gaps"] = sum(1 for it in items if it["kind"] == "gap")
                fpga["window"] = {"last": fpga_last, "items": len(items)}     # 見た窓（latest のうち窓の外のものもある）
            except Exception as e:                           # イベントの記録を始めていない等
                fpga["latest_error"] = str(e)
            out["fpga"] = fpga
            return out
