"""CMSIS-SVD（周辺レジスタの定義）の読み込みと、レジスタの値の解読・組み立て。

読み込み時に平らに展開する: derivedFrom を解決し、dim（配列・リスト）を個々のレジスタにし、クラスタは
"CLUSTER.REG" の点つなぎの名前に、番地は絶対番地にする。

    dev = load_device("STM32F401")                 # 置き場所から名前の同じファイルを探す（find_svd）
    reg = dev.register("RCC.CR")                   # Register（番地・大きさ・フィールド）
    reg.decode(0x83)                               # フィールドごとの値（列挙の名前つき）
    reg.encode(current, {"HSEON": 1})              # 読み・変更・書き込みの「変更」
    dev.find(0x40020014)                           # RegisterHit: GPIOA.ODR
    dev.read_register(TargetMemory(t), "GPIOA.MODER")   # レジスタの幅で 1 回読む

仕様: docs/superpowers/specs/2026-10-17-f2-disasm-svd-design.md
"""

import importlib.util
import os
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .svd_model import Device, EnumValue, Field, FieldValue, Peripheral, Register, RegisterHit, RegisterValue

__all__ = ["BUNDLED_DIR", "Device", "EnumValue", "Field", "FieldValue", "Peripheral", "Register", "RegisterHit",
           "RegisterValue", "SvdSource", "find_svd", "load_device"]

BUNDLED_DIR = Path(__file__).resolve().parent / "data" / "svd"


# --- SVD を探す -----------------------------------------------------------------------------

@dataclass(frozen=True)
class SvdSource:
    name: str                    # 合ったファイルの名前（拡張子なし）
    origin: str                  # "explicit" / "env" / "user" / "bundled" / "pyocd"
    path: Path                   # ファイル（pyocd なら zip）
    member: str | None = None    # zip の中の名前

    def read(self) -> bytes:
        if self.member is None:
            return self.path.read_bytes()
        with zipfile.ZipFile(self.path) as z:
            return z.read(self.member)


def _named(names: Iterable[str], device: str) -> str | None:
    """名前（拡張子 .svd / .xml を除く）が device と同じファイル（大文字小文字は区別しない）。部品名の先頭に合う・
    "x" を任意の 1 文字とみなす等の名前の慣習では選ばない。"""
    return next((n for n in names
                 if Path(n).suffix.lower() in (".svd", ".xml") and Path(n).stem.upper() == device.upper()), None)


def _pyocd_zip() -> Path | None:
    spec = importlib.util.find_spec("pyocd")
    if spec is None or not spec.submodule_search_locations:
        return None
    path = Path(next(iter(spec.submodule_search_locations))) / "debug" / "svd" / "svd_data.zip"
    return path if path.is_file() else None


def _search_dirs(paths: Sequence[str | Path]) -> list[tuple[str, Path]]:
    dirs = [("explicit", Path(p)) for p in paths]
    dirs += [("env", Path(p)) for p in os.environ.get("FPROBE_SVD_PATH", "").split(os.pathsep) if p]
    dirs += [("user", Path.home() / ".fprobe" / "svd"), ("bundled", BUNDLED_DIR)]
    return dirs


def find_svd(device: str, *, paths: Sequence[str | Path] = ()) -> SvdSource | None:
    """名前（拡張子を除く）が device（"STM32F401" など）の SVD のファイルを探す。探す順は data/svd/README.md。"""
    for origin, place in _search_dirs(paths):
        if origin == "explicit" and place.is_file():
            return SvdSource(place.stem, origin, place)
        if place.is_dir():
            best = _named((p.name for p in place.iterdir() if p.is_file()), device)
            if best is not None:
                return SvdSource(Path(best).stem, origin, place / best)
    archive = _pyocd_zip()
    if archive is not None:
        with zipfile.ZipFile(archive) as z:
            best = _named(z.namelist(), device)
        if best is not None:
            return SvdSource(Path(best).stem, "pyocd", archive, best)
    return None


def available_svds(paths: Sequence[str | Path] = ()) -> list[tuple[str, str, list[str] | None]]:
    """探す場所ごとの、そこにある SVD の名前（拡張子を除く。並びは名前順）。(origin, 場所, 名前の一覧)。場所が無ければ名前は None。
    pyOCD の内蔵は origin "pyocd"・場所 "pyOCD の内蔵"（pyocd が無ければ None）。find_svd が探す場所と同じ。"""
    out: list[tuple[str, str, list[str] | None]] = []
    for origin, place in _search_dirs(paths):
        if origin == "explicit" and place.is_file():
            out.append((origin, str(place), [place.stem]))
        elif place.is_dir():
            out.append((origin, str(place), sorted(Path(p.name).stem for p in place.iterdir()
                                                    if p.is_file() and p.suffix.lower() in (".svd", ".xml"))))
        else:
            out.append((origin, str(place), None))
    archive = _pyocd_zip()
    if archive is None:
        out.append(("pyocd", "pyOCD の内蔵", None))
    else:
        with zipfile.ZipFile(archive) as z:
            out.append(("pyocd", "pyOCD の内蔵", sorted(Path(n).stem for n in z.namelist()
                                                      if Path(n).suffix.lower() in (".svd", ".xml"))))
    return out


def load_device(device: str, *, paths: Sequence[str | Path] = ()) -> Device:
    source = find_svd(device, paths=paths)
    if source is None:
        lines = []
        for _, place, names in available_svds(paths):
            lines.append(f"  {place}（無い）" if names is None
                         else f"  {place}: {', '.join(names) if names else '（SVD は無い）'}" + (f"（{len(names)} 件）" if names else ""))
        raise FileNotFoundError(
            f"{device} の SVD が見つからない。選ぶのは、名前（拡張子を除く）が大文字小文字を区別せず完全に同じものだけ"
            "（部品名の先頭に合う等では選ばない）。探した場所と、そこにある SVD の名前:\n" + "\n".join(lines))
    return Device.parse(source.read())
