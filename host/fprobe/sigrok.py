"""sigrok のセッションファイル（.sr、srzip の第 2 版）の書き出しと読み込み。PulseView・sigrok-cli で開ける。
仕様: docs/superpowers/specs/2026-10-18-u20-decoders-design.md

.sr は zip: "version"（"2"）、"metadata"（INI。[device 1] に capturefile・total probes・samplerate・probeN・unitsize）、
ロジックのデータ "logic-1-1"、"logic-1-2" …（サンプルごとに unitsize バイト、リトルエンディアン、ビット k が probe k+1）。
変化点の表現（Transitions）から、毎サンプルの値を CHUNK サンプルずつに展開して書く（全体をメモリに持たない）。
"""

import configparser
import io
import re
import zipfile
from array import array
from pathlib import Path

from .decode.core import project
from .la import DEFAULT_NAMES, Capture, Transitions

__all__ = ["read_sr", "samplerate_string", "write_sr"]

CHUNK = 1 << 20                     # 1 つのデータのファイルのサンプル数
_UNITS = ((1_000_000_000, "GHz"), (1_000_000, "MHz"), (1_000, "kHz"))


def samplerate_string(hz: float) -> str:
    """libsigrok の sr_samplerate_string と同じ形（割り切れる最大の単位、無ければ Hz）。"""
    hz = round(hz)
    for unit, name in _UNITS:
        if hz >= unit and hz % unit == 0:
            return f"{hz // unit} {name}"
    return f"{hz} Hz"


def _parse_rate(text: str) -> float:
    m = re.fullmatch(r"\s*([0-9.]+)\s*([kKmMgG]?)\s*(?:Hz)?\s*", text)
    if not m:
        raise ValueError(f"samplerate が読めない: {text!r}")
    scale = {"": 1, "k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6, "g": 1e9, "G": 1e9}[m.group(2)]
    return float(m.group(1)) * scale


def write_sr(capture: "Capture | Transitions", path: str | Path, *, channels: list[int] | None = None,
             names: dict[int, str] | None = None) -> dict:
    """捕捉を .sr に書く。channels で書くチャンネルを選ぶ（既定は全部。順に probe1, probe2 …）。"""
    tr = capture if isinstance(capture, Transitions) else Transitions.from_capture(capture)
    channels = list(range(tr.channels)) if channels is None else list(channels)
    if not channels:
        raise ValueError("書くチャンネルが無い")
    for ch in channels:
        if not 0 <= ch < tr.channels:
            raise ValueError(f"チャンネル {ch} は捕捉に無い（0..{tr.channels - 1}）")
    starts, values = project(tr, channels)
    names = {**DEFAULT_NAMES, **(names or {})}
    unitsize = (len(channels) + 7) // 8
    rate = 1 / tr.sample_period_s
    meta = ["[global]", "sigrok version=0.5.2", "", "[device 1]", "capturefile=logic-1",
            f"total probes={len(channels)}", f"samplerate={samplerate_string(rate)}", "total analog=0"]
    meta += [f"probe{k + 1}={names.get(ch, f'ch{ch}')}" for k, ch in enumerate(channels)]
    meta += [f"unitsize={unitsize}", ""]
    files = 0
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("version", "2")
        z.writestr("metadata", "\n".join(meta))
        buf, filled = io.BytesIO(), 0
        for i in range(len(values)):
            run_end = starts[i + 1] if i + 1 < len(values) else tr.length
            n = run_end - starts[i]
            unit = values[i].to_bytes(unitsize, "little")
            while n:
                take = min(n, CHUNK - filled)
                buf.write(unit * take)
                filled += take
                n -= take
                if filled == CHUNK:
                    files += 1
                    z.writestr(f"logic-1-{files}", buf.getvalue())
                    buf, filled = io.BytesIO(), 0
        if filled:
            files += 1
            z.writestr(f"logic-1-{files}", buf.getvalue())
    return {"path": str(path), "samples": tr.length, "channels": channels, "samplerate_hz": rate,
            "unitsize": unitsize, "data_files": files}


def read_sr(path: str | Path) -> tuple[Transitions, dict[int, str]]:
    """.sr のロジックのデータを読む（変化点の表現と、チャンネルの名前）。トリガは先頭（0）とする。"""
    with zipfile.ZipFile(path) as z:
        if z.read("version").decode().strip() != "2":
            raise ValueError(".sr の版が 2 でない")
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(z.read("metadata").decode())
        dev = parser["device 1"]
        unitsize = int(dev["unitsize"])
        probes = int(dev["total probes"])
        rate = _parse_rate(dev["samplerate"])
        base = dev["capturefile"]
        names = {k - 1: dev[f"probe{k}"] for k in range(1, probes + 1) if f"probe{k}" in dev}
        listed = set(z.namelist())
        parts = sorted((int(n[len(base) + 1:]) for n in listed if re.fullmatch(re.escape(base) + r"-\d+", n)))
        data_names = [f"{base}-{k}" for k in parts] or ([base] if base in listed else [])
        values, starts, length, previous = array("H" if probes <= 16 else "L"), array("Q"), 0, None
        for name in data_names:
            data = z.read(name)
            for k in range(0, len(data) - unitsize + 1, unitsize):
                v = int.from_bytes(data[k:k + unitsize], "little")
                if v != previous:
                    values.append(v)
                    starts.append(length)
                    previous = v
                length += 1
    if not length:
        raise ValueError(".sr にロジックのデータが無い")
    return Transitions(values, starts, length, 0, 0, 1 / rate, probes), names
