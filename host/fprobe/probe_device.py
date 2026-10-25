"""プローブ本体（probe_info・probe_name_set・csr_read / csr_write・target_voltage・probe_voltage_history）の mixin。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う。Probe の _lock・session・_serial・_voltage_history を使う。
"""

from . import probe_names
from .aux_mcu import Aux


class DeviceTools:
    """Probe に混ぜるプローブ本体の情報・CSR・ターゲット電圧の道具。"""

    def probe_info(self) -> dict:
        """プローブのケイパビリティ・タイムベースの現在値・ターゲット接続の有無・個体（device = つなぎ方と番地、
        name = 個体名、version = ビットストリームの版: 入力の要約・ビルドの日時・git）。"""
        with self._lock:
            caps = self.session.device.capabilities()
            device = getattr(self.session, "address", None)
            return {"capabilities": {k: v for k, v in vars(caps).items() if not k.startswith("_")},
                    "time": self.session.device.time(), "target_connected": self._debug is not None,
                    "device": device, "name": probe_names.name_of(device),
                    "version": caps.build.describe() if getattr(caps, "build", None) is not None else None}

    def probe_name_set(self, name: str | None = None) -> dict:
        """今の機器（USB のシリアル番号・COM ポート・IP）に個体名を付ける（~/.fprobe/probes.json）。name を省くと名前を
        外す。以後 fprobe-daemon / fprobe-mcp を --name で起動でき、probe_info が名前を返す。"""
        with self._lock:
            device = getattr(self.session, "address", None)
            if not device:
                raise RuntimeError("この機器のつなぎ方が分からない")
            if name:
                probe_names.set_name(name, device)
            else:
                old = probe_names.name_of(device)
                if old is not None:
                    probe_names.remove(old)
            return {"device": device, "name": probe_names.name_of(device)}

    def csr_read(self, address: int, count: int = 1) -> list[int]:
        """CSR を読む（バイトアドレス、32 ビット語 count 個）。レジスタ表は docs/registers.md。"""
        with self._lock:
            return self.session.device.read(address, count)

    def csr_write(self, address: int, words: list[int]) -> None:
        """CSR に 32 ビット語の列を書く。"""
        with self._lock:
            self.session.device.write(address, words)

    def target_voltage(self) -> dict:
        """ターゲットの電源電圧（補助マイコン Pico の ADC、V）。live = window_s 秒待つ間に計測値の数が増えた
        （samples_before → samples）。over_range が True なら測れる上限を超えている（volts は None。配線の分圧を確認）。"""
        with self._lock:
            reading = Aux(self.session.device).read().as_dict()
        self._voltage_history.add(reading)
        return reading

    def probe_voltage_history(self, since: float | None = None) -> dict:
        """デーモンが読んだ電圧（だれの probe_voltage でも）の履歴: since（UNIX 秒）より後の読み {time, volts,
        over_range, live} と統計（fprobe.voltage.VoltageHistory.read）。機器には触れない。"""
        return self._voltage_history.read(since)
