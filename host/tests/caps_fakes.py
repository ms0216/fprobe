"""試験の偽の機器を本物の型に照らす道具: ケイパビリティは本物の fprobe.caps.Capabilities で作り（caps_for）、偽の機器は
fprobe.device.Device と同じ口（FakeDeviceBase）にする。

SimpleNamespace で作ると、打ち間違えた欄・本物に無い欄が黙って通り、本物の Capabilities が欄を足し・名前を変えても
偽物は気づかない。caps_for は本物の dataclass を作るので、無い欄の名前は TypeError、渡さない欄は本物の既定の値
（その TLV が無い古いデバイスの意味。caps.py が 1 か所で決める）になる。
"""

import dataclasses
import inspect

from fprobe import generated as g
from fprobe.caps import Capabilities, TimingPoint
from fprobe.device import Device

__all__ = ["HZ", "FakeDeviceBase", "caps_for", "dap_timing", "with_caps"]

HZ = 100_000_000                       # 偽の機器の sys・タイムベースのクロック（probe_fakes.HZ と同じ）


def caps_for(design: str = "fprobe", **fields) -> Capabilities:
    """偽の機器のケイパビリティ: 名乗り（protocol_version・board・design）とクロック（sys・タイムベース = HZ）を本物の
    機器と同じく必ず持たせ、ほかは fields で渡した欄だけ（渡さない欄は TLV が無いときの意味）。"""
    base = {"protocol_version": g.PROTOCOL_VERSION, "board": "fake", "design": design,
            "sys_clock_hz": HZ, "timebase_hz": HZ}
    return Capabilities(**{**base, **fields})


def with_caps(caps: Capabilities, **fields) -> Capabilities:
    """caps の欄を一部だけ替えたもの（本物の dataclasses.replace。無い欄の名前は TypeError）。"""
    return dataclasses.replace(caps, **fields)


def dap_timing(output_ticks: int) -> dict[str, TimingPoint]:
    """DAP の線上の点から端子までの段（Capabilities.dap_output_ticks = output_ticks）を申告する TLV TIMING の情報源
    dap_swd・dap_jtag（delay = −段数。本物の回路と同じく 2 つとも申告する）。"""
    points = {"dap_swd": "swd_ack3_rise", "dap_jtag": "jtag_update_dr_exit"}
    return {name: TimingPoint(name, point, "tick", -output_ticks, 0, ()) for name, point in points.items()}


class FakeDeviceBase:
    """偽の機器の共通の土台: fprobe.device.Device の CSR 以外の口（protocol_version・capabilities・last_capabilities・
    known_capabilities）を本物と同じ意味で持つ。ケイパビリティは属性 caps（本物の Capabilities）か、派生の
    capabilities() が返す。

    派生のクラスを作るときに照らす（__init_subclass__）: Device と同じ名前のメソッドは Device と同じ引数の名前を持つこと
    （本物の口が変わったら偽物が黙ってずれない）、capabilities() は本物の Capabilities を返すこと（呼ぶたびに確かめ、
    last_capabilities に残す。本物の Device と同じ）。"""

    protocol_version = g.PROTOCOL_VERSION
    caps: Capabilities | None = None
    last_capabilities: Capabilities | None = None

    def capabilities(self) -> Capabilities:
        if self.caps is None:
            self.caps = caps_for()
        self.last_capabilities = self.caps
        return self.caps

    def known_capabilities(self) -> Capabilities:
        return self.last_capabilities if self.last_capabilities is not None else self.capabilities()

    def control_counters(self) -> dict[str, int]:
        """CONTROL の数え（Device.control_counters）。偽の機器の READ はリンクを通らないので、送り直しも遅れた応答も無い。"""
        return {"read_retries": 0, "stale_responses": 0}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        for name, fn in list(vars(cls).items()):
            if name.startswith("_") or not callable(fn) or not callable(getattr(Device, name, None)):
                continue
            _check_signature(cls, name, fn)
        if "capabilities" in vars(cls):
            cls.capabilities = _recorded(vars(cls)["capabilities"])

    def __setattr__(self, name, value):
        if name == "capabilities":
            raise TypeError("偽の機器の capabilities を差し替えない: 属性 caps に本物の Capabilities を置く（caps_for・with_caps）")
        if name == "caps" and value is not None and not isinstance(value, Capabilities):
            raise TypeError(f"caps は本物の Capabilities（caps_for で作る）: {type(value).__name__}")
        super().__setattr__(name, value)
        if name == "caps" and self.last_capabilities is not None:
            # 試験が構成を置き直した = この機器は初めからその構成（本物は申告が変わらない）: 前に読んだ値も今の構成にする
            super().__setattr__("last_capabilities", self.capabilities())


def _check_signature(cls, name: str, fn) -> None:
    want = [p.name for p in inspect.signature(getattr(Device, name)).parameters.values() if p.name != "self"]
    params = list(inspect.signature(fn).parameters.values())
    if any(p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD) for p in params):
        return
    got = [p.name for p in params if p.name != "self"]
    if got[:len(want)] != want or any(p.default is p.empty for p in params[len(want) + 1:]):
        raise TypeError(f"{cls.__qualname__}.{name}{tuple(got)} が本物の Device.{name}{tuple(want)} と合わない")


def _recorded(fn):
    def capabilities(self) -> Capabilities:
        caps = fn(self)
        if not isinstance(caps, Capabilities):
            raise TypeError(f"{type(self).__qualname__}.capabilities() は本物の Capabilities を返す（caps_for で作る）: "
                            f"{type(caps).__name__}")
        self.last_capabilities = caps
        return caps
    capabilities.__wrapped__ = fn
    return capabilities
