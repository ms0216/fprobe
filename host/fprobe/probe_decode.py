"""Probe の道具: 捕捉した波形の通信の解読（la_decode）、解読器の一覧と利用者の解読器の読み込み、捕捉のファイルへの
書き出し（sigrok の .sr・VCD）と読み込み。

Probe（fprobe.probe）が API の名前を委ねるサービス（fprobe.probe_service。ホストから使う名前は NEEDS）。Probe の _lock・_capture・_capture_info・_capture_names・_keep_capture・
_observe・_recorder を使う。機器には触らない（捕捉は la_capture 等が取ったもの）。

- 解読の結果は DECODE_CACHE 個まで覚え（同じ解読を offset で読み進める・積み重ねの下の解読を使い回す）、
  記録中（_recorder）なら注釈を共通の時間軸の記録に入れる。

仕様: docs/superpowers/specs/2026-10-17-f7-protocol-decode-design.md、docs/superpowers/specs/2026-10-18-u20-decoders-design.md
"""

import collections
import json
from pathlib import Path

from .api import NoData
from .decode import Decoding, decode_on, decoders, get_decoder, load_file
from .decode.registry import split_settings
from .la import write_vcd
from .sigrok import read_sr, write_sr
from .probe_service import HostView

__all__ = ["DecodeService"]

EXPORT_FORMATS = ("sr", "vcd")
DECODE_CACHE = 8                       # 解読の結果を覚えておく数（同じ解読を offset で読み進める・積み重ねの下の解読）


class DecodeService:
    """捕捉した波形の解読と、解読器・ロジアナのファイルの道具（Probe のサービス）。"""

    API = ("la_decode", "la_decoder_load", "la_decoders", "la_export", "la_load")
    SHARED = ("_decodings",)
    # ホスト（Probe の核と、ほかの領域）から使う名前
    NEEDS = ("_capture", "_capture_info", "_capture_names", "_keep_capture", "_lock", "_observe", "_recorder")

    def __init__(self, host):
        self._host = HostView(host, self.NEEDS, type(self).__name__)
        self._decodings: collections.OrderedDict = collections.OrderedDict()

    # --- 通信の解読 ---------------------------------------------------------------------------

    def la_decode(self, protocol: str, settings: dict | None = None, capture_id: int | None = None,
                  start: int | None = None, end: int | None = None, kinds: list[str] | None = None,
                  errors_only: bool = False, offset: int = 0, limit: int = 200) -> dict:
        """最後の捕捉の通信を解読する。protocol は登録された解読器の名前（la_decoders）。settings はその設定
        （チャンネル等）。積み重ねる解読器は下の解読の設定を下の名前のキーに入れる（例 modbus: {"uart": {...}}）。
        start / end（サンプル）・kinds・errors_only で絞り、offset から limit 件。kinds を省くと解読器の既定の種類
        （SWD は transfer、JTAG は ir・dr・reset 等。無ければ全部）。結果の kinds は絞った種類（null = 全部）、counts は
        絞る前の種類ごとの件数。"""
        with self._host._lock:
            tr, info = self._host._capture, self._host._capture_info
        if tr is None:
            raise NoData("捕捉が無い（la_capture / la_deep_capture を先に）")
        if capture_id is not None and capture_id != info["id"]:
            raise KeyError(f"捕捉 {capture_id} は残っていない（最後の捕捉は {info['id']}）")
        decoding = self._decoding(tr, info["id"], protocol, settings or {})
        items = decoding.annotations if start is None and end is None else \
            decoding.between(start or 0, tr.length if end is None else end)
        if kinds is None and get_decoder(protocol).default_kinds is not None:
            kinds = list(get_decoder(protocol).default_kinds)
        selected = [a for a in items if (kinds is None or a.kind in kinds) and (not errors_only or a.error)]
        return {"protocol": protocol, "params": dict(decoding.params), "capture": info, "total": len(decoding),
                "errors": len(decoding.errors()), "counts": dict(collections.Counter(a.kind for a in decoding)),
                # 絞り（kinds）の外の種類とその件数（事実。呼び出し側は引き算をしない）。kinds が null（全部）なら空
                "hidden_kinds": {} if kinds is None else dict(sorted(
                    collections.Counter(a.kind for a in decoding if a.kind not in kinds).items())),
                "kinds": kinds, "matched": len(selected), "offset": offset,
                "annotations": [decoding.annotation_dict(a) for a in selected[offset:offset + limit]]}

    def _decoding(self, tr, capture_id: int, protocol: str, settings: dict) -> Decoding:
        """解読（覚えた結果があればそれ）。積み重ねる解読器は下の解読もここを通す（下の結果も覚えて使い回す）。
        鍵に解読器そのもの（Decoder）を含めるので、利用者の解読器を読み直すと前の結果は使わない。"""
        info = get_decoder(protocol)
        key = (capture_id, protocol, info, json.dumps(settings, sort_keys=True, default=repr))
        with self._host._observe:
            cached = self._decodings.get(key)
        if cached is not None:
            return cached
        try:
            if info.stacked:
                name, lower_settings, own = split_settings(info, settings)
                info.check(own)                                    # 足りない設定を名前で（下の解読の前に）
                decoding = decode_on(protocol, self._decoding(tr, capture_id, name, lower_settings), **own)
            else:
                info.check(settings)
                decoding = info.function(tr, **settings)
        except TypeError as e:                                     # 知らない設定
            raise ValueError(f"{protocol} の設定が合わない: {e}") from None
        if not isinstance(decoding, Decoding):
            raise TypeError(f"解読器 {protocol} が Decoding を返さない")
        with self._host._observe:
            self._decodings[key] = decoding
            while len(self._decodings) > DECODE_CACHE:
                self._decodings.popitem(last=False)
        self._record_decoding(tr, decoding)
        return decoding

    def _record_decoding(self, tr, decoding: Decoding) -> None:
        """記録中なら注釈を共通の時間軸へ（捕捉のトリガのサンプルがトリガ時刻に来るように置く）。"""
        rec = self._host._recorder
        if rec is None or not decoding.annotations:
            return
        tps = tr.sample_period_s * rec.timebase_hz
        base = tr.trigger_time - round(tr.trigger * tps)
        rows = []
        for a in decoding:
            value = a.value if a.value is not None and -(1 << 63) <= a.value < 1 << 63 else 0
            body = {"protocol": decoding.protocol, "text": a.text, "value": a.value, "error": a.error,
                    "fields": dict(a.fields) if a.fields is not None else None}
            rows.append(("annotation", f"{decoding.protocol}.{a.kind}", base + round(a.start * tps), value, 0, body,
                         base + round(a.end * tps)))
        rec.add_many(rows)

    # --- 解読器・ファイル --------------------------------------------------------------------------

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
        with self._host._lock:
            tr, info = self._host._capture, self._host._capture_info
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
        names = {**self._host._capture_names, **{int(k): str(v) for k, v in (names or {}).items()}}
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
        with self._host._lock:
            self._host._keep_capture(tr, "file", names)
            info = dict(self._host._capture_info)
        return {"capture": info, "path": str(path), "names": {str(k): v for k, v in names.items()}}
