"""Probe の道具: 解読器の一覧と利用者の解読器の読み込み、捕捉のファイルへの書き出し（sigrok の .sr・VCD）と読み込み。

Probe（fprobe.mcp_server）に混ぜて使う mixin。Probe の _lock・_capture・_capture_info・_capture_names・_keep_capture を使う。
機器には触らない（捕捉は la_capture 等が取ったもの）。解読そのもの（la_decode）は probe_observe にある。

仕様: docs/superpowers/specs/2026-10-18-u20-decoders-design.md
"""

from pathlib import Path

from .api import NoData
from .decode import decoders, load_file
from .la import write_vcd
from .sigrok import read_sr, write_sr

__all__ = ["DecodeTools"]

EXPORT_FORMATS = ("sr", "vcd")


class DecodeTools:
    """解読器とロジアナのファイルの道具（Probe の mixin）。"""

    def la_decoders(self) -> dict:
        """la_decode に使える解読器の一覧（名前・下の解読（積み重ねる解読器）・設定・既定の種類・出どころ）。"""
        return {"decoders": [d.describe() for d in decoders()]}

    def la_decoder_load(self, path: str) -> dict:
        """利用者の解読器のファイル（Python。@decoder か register で登録する）を読み込む。同じファイルを読み直すと
        そのファイルの前の登録を置き換える。失敗したら登録は元のまま。"""
        names = load_file(path)
        return {"loaded": names, "origin": str(Path(path).resolve()),
                "decoders": [d.describe() for d in decoders() if d.name in names]}

    def _last_capture(self):
        with self._lock:
            tr, info = self._capture, self._capture_info
        if tr is None:
            raise NoData("捕捉が無い（la_capture / la_deep_capture / la_load を先に）")
        return tr, info

    def la_export(self, path: str, format: str = "sr", channels: list[int] | None = None,
                  names: dict | None = None) -> dict:
        """最後の捕捉をファイルに書く。format: sr（sigrok。PulseView・sigrok-cli で開く）/ vcd。channels で書く
        チャンネルを選ぶ（sr のみ。既定は全部）、names はチャンネルの名前（{"5": "rx"}）。"""
        if format not in EXPORT_FORMATS:
            raise ValueError(f"形式は {EXPORT_FORMATS} のどれか（{format!r}）")
        tr, info = self._last_capture()
        names = {**self._capture_names, **{int(k): str(v) for k, v in (names or {}).items()}}
        if format == "vcd":
            if channels is not None:
                raise ValueError("vcd は全チャンネルを書く（channels は sr だけ）")
            write_vcd(tr, Path(path), names)
            return {"path": str(path), "format": "vcd", "capture": info, "samples": tr.length,
                    "channels": list(range(tr.channels))}
        out = write_sr(tr, Path(path), channels=channels, names=names)
        return {**out, "format": "sr", "capture": info}

    def la_load(self, path: str) -> dict:
        """sigrok の .sr を最後の捕捉として読み込む（他のロジアナで取った波形も la_decode・la_measure・la_view で扱う）。
        トリガは先頭のサンプルとする。"""
        tr, names = read_sr(Path(path))
        with self._lock:
            self._keep_capture(tr, "file", names)
            info = dict(self._capture_info)
        return {"capture": info, "path": str(path), "names": {str(k): v for k, v in names.items()}}
