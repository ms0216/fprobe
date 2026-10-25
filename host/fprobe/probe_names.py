"""プローブの個体名（ニックネーム。U49 / SEG-14）。

名前を機器のつなぎ方（device_of と同じ {"link": "usb" / "serial" / "eth", "address": USB シリアル番号 / COM ポート /
IP アドレス}）に結びつけて ~/.fprobe/probes.json（FPROBE_HOME）に保存する。デーモン・MCP の起動では --serial 等の
代わりに --name で指定でき、probe.info は今の機器の名前を返す。

注意: 今の fprobe のゲートウェアは USB シリアル番号が全台 "0001"（fprobe_gw/usb/fprobe_device.py の STRINGS）なので、
USB で複数台を名前で見分けるには、個体ごとのシリアル番号が要る（名前の仕組みはそのまま使える）。
"""

import json
import re
from pathlib import Path

from .home import fprobe_home

__all__ = ["ProbeNameError", "address_of", "apply_to_args", "load", "name_of", "names_path", "remove", "resolve",
           "set_name"]

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_LINKS = ("usb", "serial", "eth")


class ProbeNameError(ValueError):
    """名前が不正・見つからない。"""


def names_path() -> Path:
    return fprobe_home() / "probes.json"


def load() -> dict[str, dict]:
    """{名前: {"link", "address"}}。ファイルが無ければ空。"""
    path = names_path()
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {name: {"link": d["link"], "address": d["address"]} for name, d in data.get("probes", {}).items()}


def _save(names: dict[str, dict]) -> None:
    path = names_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"probes": dict(sorted(names.items()))}, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def set_name(name: str, device: dict) -> None:
    """device（{"link", "address"}）に名前を付ける。1 台に名前は 1 つ（前の名前は消える）。同じ名前の別の機器は上書き。"""
    if not _NAME.match(name):
        raise ProbeNameError(f"名前は英数字で始まり、英数字・_・.・- の 64 文字まで: {name!r}")
    if device.get("link") not in _LINKS or not device.get("address"):
        raise ProbeNameError(f"機器のつなぎ方が不正: {device}")
    entry = {"link": device["link"], "address": str(device["address"])}
    names = {n: d for n, d in load().items() if d != entry}
    names[name] = entry
    _save(names)


def remove(name: str) -> None:
    names = load()
    if name not in names:
        raise ProbeNameError(f"名前 {name!r} は無い（{', '.join(names) or 'なし'}）")
    del names[name]
    _save(names)


def resolve(name: str) -> dict:
    """名前 → {"link", "address"}。"""
    names = load()
    if name not in names:
        raise ProbeNameError(f"名前 {name!r} のプローブは登録されていない（{', '.join(names) or 'なし'}。"
                         "fprobe name set で付ける）")
    return names[name]


def name_of(device: dict | None) -> str | None:
    """機器（{"link", "address"}）の名前（無ければ None）。"""
    if not device:
        return None
    entry = {"link": device.get("link"), "address": str(device.get("address"))}
    return next((n for n, d in load().items() if d == entry), None)


def address_of(device: dict) -> dict:
    """{"serial" / "port" / "eth": 番地}（デーモン・Probe の引数の形）。"""
    key = {"usb": "serial", "serial": "port", "eth": "eth"}[device["link"]]
    return {key: device["address"]}


def apply_to_args(args) -> None:
    """argparse の結果に --name があれば、--serial / --port / --eth を名前から埋める（どれかと一緒には使えない）。"""
    name = getattr(args, "name", None)
    if not name:
        return
    given = [flag for flag in ("serial", "port", "eth") if getattr(args, flag, None)]
    if given:
        raise ProbeNameError(f"--name と --{given[0]} は一緒に使えない")
    for key, value in address_of(resolve(name)).items():
        setattr(args, key, value)
