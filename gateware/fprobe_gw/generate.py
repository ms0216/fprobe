"""コミットする生成物の一覧（単一の定義元から作る VHDL・ホスト Python・文書）。

`python -m fprobe_gw gen` で書き出し、tests/test_generated.py が一致を検査する。
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import build_info, caps, dap_info
from .csr import fprobe_map
from .csr.fprobe_map import FPROBE_MAP, MEM_BEAT_BYTES
from .csr.host import render_host_module
from .csr.markdown import render_markdown
from .csr.vhdl import bank_entity_name, render_bank, render_map_package
from .designs import DESIGNS, FEATURE_DESIGNS, PROTOCOL_DESIGNS, caps_package_path
from .rtl import CORE_GENERATED, RTL_ROOT
from .usb import fprobe_device as usb_device
from .usb.fprobe_device import FPROBE_DEVICE
from .usb.vhdl_rom import COMMITTED_PACKAGE as USB_PACKAGE
from .usb.vhdl_rom import render_package as render_usb_package

__all__ = ["Artifact", "REPO_ROOT", "artifacts", "stale", "write_all"]

REPO_ROOT = RTL_ROOT.parent.parent


@dataclass(frozen=True)
class Artifact:
    path: Path
    render: Callable[[], str]


def artifacts() -> list[Artifact]:
    banks = [Artifact(CORE_GENERATED / f"{bank_entity_name(b)}.vhd", lambda b=b: render_bank(b))
             for b in FPROBE_MAP.blocks if not b.external]
    return [
        Artifact(USB_PACKAGE, lambda: render_usb_package(FPROBE_DEVICE)),
        Artifact(CORE_GENERATED / "csr_map_pkg.vhd", lambda: render_map_package(FPROBE_MAP)),
        *banks,
        Artifact(REPO_ROOT / "host" / "fprobe" / "generated.py", lambda: render_host_module(FPROBE_MAP, {
            "USB（fprobe 拡張インターフェース）": {
                "USB_VID": usb_device.VID, "USB_PID": usb_device.PID,
                "USB_EP_CONTROL": usb_device.EP_CONTROL, "USB_EP_STREAM": usb_device.EP_STREAM,
                "USB_CONTROL_INTERFACE": usb_device.STRINGS[usb_device.STR_CONTROL - 1]},
            "ケイパビリティ（TLV の種類・値）": caps.HOST_CONSTANTS,
            "外部メモリ": {"MEM_BEAT_BYTES": MEM_BEAT_BYTES},
            "ストリームの見出し（ロジアナとイベントを 1 本に流す構成）":
                {n: getattr(fprobe_map, n) for n in ("STREAM_SEG_MAGIC", "STREAM_SEG_LA", "STREAM_SEG_EVENTS",
                                                     "STREAM_SEG_MAX_BEATS")},
            "Primer の構成（線・機能 → デザイン。ホストが書き替えに使う）":
                {f"DESIGN_FOR_{p.upper()}": d for p, d in {**PROTOCOL_DESIGNS, **FEATURE_DESIGNS}.items()}})),
        Artifact(REPO_ROOT / "docs" / "registers.md", lambda: render_markdown(FPROBE_MAP)),
        Artifact(RTL_ROOT / "debug" / "generated" / "dap_info_pkg.vhd", dap_info.render_package),
        Artifact(build_info.DEFAULT_PACKAGE, lambda: build_info.render_package(None)),
        *[Artifact(caps_package_path(d.name),
                   lambda d=d: caps.render_package("design_caps_pkg", d.capabilities))
          for d in DESIGNS.values() if d.capabilities is not None],
    ]


def _current(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.is_file() else None


def stale() -> list[Path]:
    """生成器の出力と一致しないファイル。"""
    return [a.path for a in artifacts() if _current(a.path) != a.render()]


def write_all() -> list[Path]:
    """変わったものだけ書き出し、書いたパスを返す。"""
    written = []
    for a in artifacts():
        text = a.render()
        if _current(a.path) != text:
            a.path.parent.mkdir(parents=True, exist_ok=True)
            a.path.write_text(text, encoding="utf-8", newline="\n")
            written.append(a.path)
    return written
