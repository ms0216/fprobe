"""記録を読んだ答え（波形の変化点・見え方・VCD・CSV・JSON）を、比べられる形（JSON にできる辞書）にする。
形式の新旧の同値の試験（test_recording_raw.py）と、古い記録の固定試料（tests/data/recording_legacy）が使う。"""

import hashlib
import json
from pathlib import Path

from fprobe.recording import Recording


def _digest(*blobs: bytes) -> str:
    h = hashlib.sha256()
    for b in blobs:
        h.update(len(b).to_bytes(8, "little"))
        h.update(b)
    return h.hexdigest()


def _normalized(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def answers(directory: Path, scratch: Path, channels: list[int] | None = None, width: int = 40) -> dict:
    """directory の記録を開いて、答えを集める（scratch は書き出しの置き場）。"""
    scratch.mkdir(parents=True, exist_ok=True)
    rec = Recording.open(directory)
    try:
        waves = []
        for t0, tps, tr in rec.waveforms():
            waves.append({"t0": t0, "tps": tps, "length": tr.length, "channels": tr.channels, "trigger": tr.trigger,
                          "period_s": tr.sample_period_s, "skew": [None if x is None else x for x in tr.channel_skew],
                          "values": _digest(bytes(memoryview(tr.values).cast("B")) if len(tr.values) else b""),
                          "starts": _digest(bytes(memoryview(tr.starts).cast("B")) if len(tr.starts) else b""),
                          "count": len(tr.values)})
        span = rec.span()
        out = {"waves": waves, "span": None if span is None else list(span), "channel_count": rec.channel_count()}
        if span is not None:
            out["view"] = rec.waveform_view(span[0], span[1] + 1, width)
            out["density"] = list(rec.density(span[0], span[1] + 1, width))
        rec.export_vcd(scratch / "a.vcd", channels)
        rec.export_csv(scratch / "a.csv")
        rec.export_json(scratch / "a.json")
        out["vcd"], out["csv"], out["json"] = (_normalized(scratch / n) for n in ("a.vcd", "a.csv", "a.json"))
        out["events"] = len(rec.events())
        return out
    finally:
        rec.close()


def dumps(obj: dict) -> str:
    return json.dumps(obj, indent=1, sort_keys=True)
