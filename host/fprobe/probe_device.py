"""プローブ本体（probe_info・probe_name_set・csr_read / csr_write・target_voltage・probe_voltage_history）のサービス。

Probe（fprobe.probe）が API の名前を委ねるサービス（fprobe.probe_service。ホストから使う名前は NEEDS）。Probe の _lock・session・_serial・_voltage_history を使う。
"""

import dataclasses

from . import probe_names
from .aux_mcu import Aux
from .probe_service import HostView


class DeviceService:
    """Probe が委ねるプローブ本体の情報・CSR・ターゲット電圧の道具。"""

    API = ("csr_read", "csr_write", "probe_info", "probe_name_set", "probe_voltage_history", "target_voltage")
    SHARED = ()
    # ホスト（Probe の核と、ほかの領域）から使う名前
    NEEDS = ("_debug", "_lock", "_voltage_history", "session")

    def __init__(self, host):
        self._host = HostView(host, self.NEEDS, type(self).__name__)

    def probe_info(self) -> dict:
        """プローブのケイパビリティ・タイムベースの現在値・ターゲット接続の有無・個体（device = つなぎ方と番地、
        name = 個体名、version = ビットストリームの版: 入力の要約・ビルドの日時・git）。link_open = 開いたときの識別の READ の
        事実（identify_wait_s = 開く側が明示した待つ秒数、identify_resends = 時間切れで送り直した回数。測れない機器では None）。"""
        with self._host._lock:
            dev = self._host.session.device
            caps = dev.capabilities()
            device = getattr(self._host.session, "address", None)
            wait = getattr(dev, "identify_wait_s", None)
            link_open = None if wait is None else {"identify_wait_s": float(wait),
                                                   "identify_resends": int(dev.identify_resends)}
            return {"capabilities": dataclasses.asdict(caps), "link_open": link_open,
                    "time": dev.time(), "target_connected": self._host._debug is not None,
                    "device": device, "name": probe_names.name_of(device),
                    "version": caps.build.describe() if caps.build is not None else None}

    def probe_name_set(self, name: str | None = None) -> dict:
        """今の機器（USB のシリアル番号・COM ポート・IP）に個体名を付ける（~/.fprobe/probes.json）。name を省くと名前を
        外す。以後 fprobe-daemon / fprobe-mcp を --name で起動でき、probe_info が名前を返す。"""
        with self._host._lock:
            device = getattr(self._host.session, "address", None)
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
        with self._host._lock:
            return self._host.session.device.read(address, count)

    def csr_write(self, address: int, words: list[int]) -> None:
        """CSR に 32 ビット語の列を書く。"""
        with self._host._lock:
            self._host.session.device.write(address, words)

    def target_voltage(self) -> dict:
        """ターゲットの電源電圧（補助マイコン Pico の ADC、V）。live = window_s 秒待つ間に計測値の数が増えた
        （samples_before → samples）。over_range が True なら測れる上限を超えている（volts は None。配線の分圧を確認）。"""
        with self._host._lock:
            reading = Aux(self._host.session.device).read().as_dict()
        self._host._voltage_history.add(reading)
        return reading

    def probe_voltage_history(self, since: float | None = None) -> dict:
        """デーモンが読んだ電圧（だれの probe_voltage でも）の履歴: since（UNIX 秒）より後の読み {time, volts,
        over_range, live} と統計（fprobe.voltage.VoltageHistory.read）。機器には触れない。"""
        return self._host._voltage_history.read(since)
