"""記録（Recording）の書き出し: VCD（波形と FPGA の観測の信号）・CSV・JSON。読みながら書く（長い記録でも全部をメモリに載せない）。"""

import csv
import heapq
import itertools
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .la import vcd_blocks_skewed
from .la_vcd import vcd_comments, vcd_unit
from .la_wave import skew_unknown
from .recording_model import Chunk

if TYPE_CHECKING:
    from .recording import Recording

__all__ = ["export_csv", "export_json", "export_vcd", "vcd_id"]


def vcd_id(i: int) -> str:
    """VCD の識別子（印字できる ASCII 94 字の並び）。"""
    out = chr(33 + i % 94)
    while i >= 94:
        i = i // 94 - 1
        out = chr(33 + i % 94) + out
    return out



def export_vcd(rec: "Recording", path: Path, channels: list[int] | None = None) -> None:
    """波形と FPGA の観測の信号を VCD に（時刻は ns か、丸めずに書ける最も粗い ps の単位 = vcd_unit。タイムベースは
    10 の冪とは限らないため。800 MS/s の波形は 10 ps）。FPGA の信号は記録の信号の定義（fpga.signals）で信号ごとに
    `fpga_<組>_<名前>`、定義の無い組は 16 ビットの `fpga_<組>`。ずれが分からずずらさずに置いた ch は $comment に書く。
    変化は波形と FPGA の出来事を時刻順に流しながら合わせて書く（長い記録でも全部をメモリに載せない）。
    numpy があれば波形の変化点はかたまりごとにまとめて行にする（fprobe.recording_vcd。同じバイト列）。"""
    from . import recording_vcd
    from .timeline_fpga import _lanes, signals_of           # timeline_fpga はこのモジュールを読む

    hz = rec.meta["timebase_hz"]
    chunks = rec._chunks()
    count = max((c.channels for c in chunks), default=0)
    wanted = channels if channels is not None else list(range(count))
    ids = {ch: vcd_id(i) for i, ch in enumerate(wanted)}
    tick_ns = 1e9 / hz
    skews = [rec.chunk_skew(c) for c in chunks]
    unit, (unit_count, unit_name), exact = vcd_unit(
        [tick_ns, *(c.ticks_per_sample * tick_ns for c in chunks),
         *(v * tick_ns for skew in skews for v in skew if v is not None)])
    scale = tick_ns / unit                                   # タイムベースの刻み → VCD の単位
    unknown = sorted({ch for skew in skews for ch in skew_unknown(skew, len(skew)) if ch in ids})
    lines = [f"$timescale {unit_count} {unit_name} $end", *vcd_comments(exact, unknown),
             "$scope module fprobe $end"]
    lines += [f"$var wire 1 {ids[ch]} ch{ch} $end" for ch in wanted]
    groups = rec._distinct_a("fpga", "value")
    plan: dict[int, list[tuple[str, int, Any]]] = {}
    if groups:
        signals = signals_of(rec)
        used_names = {f"ch{ch}" for ch in wanted}
        for g in groups:
            plan[g] = []
            for _kind, row, _title, sig in _lanes(signals, g):
                # VCD は ASCII: 英数字と _ だけにし、置き換えて同じになった名前は _2・_3… で分ける
                base = name = "fpga_" + re.sub(r"[^A-Za-z0-9_]", "_", row.replace(".", "_"))
                k = 2
                while name in used_names:
                    name, k = f"{base}_{k}", k + 1
                used_names.add(name)
                ident = vcd_id(len(ids) + sum(map(len, plan.values())))
                width = sig.width if sig is not None else 16
                lines.append(f"$var wire {width} {ident} {name} $end")
                plan[g].append((ident, width, sig))
    lines += ["$upscope $end", "$enddefinitions $end"]

    def wave() -> Iterator[tuple[int, str]]:
        # ch ごとの時刻のずれ（channel_skew）で、かたまりの終わりの変化が次のかたまりの始まりより後になりうるので、
        # 次のかたまりの最も早い変化より前の分だけを出し、残りは次と合わせて並べる
        previous, held, seq = None, [], 0
        wave_ids = {ch: ids[ch] for ch in wanted}
        trs = [rec._transitions(c) for c in chunks[:1]]
        for k, c in enumerate(chunks):
            tr = trs[0]
            trs = [rec._transitions(chunks[k + 1])] if k + 1 < len(chunks) else []
            blocks, previous = vcd_blocks_skewed(tr.values, tr.starts, wave_ids, previous,
                                                 tr.skew_samples(c.ticks_per_sample))
            for start, text in blocks:
                heapq.heappush(held, (round((c.t0 + start * c.ticks_per_sample) * scale), seq, text))
                seq += 1
            if trs:
                earliest = chunks[k + 1].t0 + min([0.0, *((v or 0.0) for v in trs[0].channel_skew)])
                limit = round(earliest * scale)
            else:
                limit = None
            while held and (limit is None or held[0][0] < limit):
                t, _, text = heapq.heappop(held)
                yield t, text

    def fpga() -> Iterator[tuple[int, str]]:
        if not plan:
            return
        last: dict[str, int] = {}
        for t, _rowid, _stream, kind, a, b in rec._rows(None, None, ["fpga"], None, columns="a, b"):
            untimed = kind.endswith("_untimed")                # 時刻の決まらない記録の間も不明
            if kind in ("group", "gap") or untimed:            # 観測していない信号は不明（x）
                for g, items in plan.items():
                    for ident, width, _sig in items:
                        if (kind == "gap" or untimed or g != a) and ident in last:
                            del last[ident]
                            yield round(t * scale), (f"x{ident}" if width == 1 else f"bx {ident}")
                continue
            if kind != "value":
                continue
            for ident, width, sig in plan[a]:
                v = sig.extract(b) if sig is not None else b
                if last.get(ident) == v:
                    continue
                last[ident] = v
                yield round(t * scale), (f"{v}{ident}" if width == 1 else f"b{v:0{width}b} {ident}")

    if recording_vcd.fast_ok(wanted, ids):
        def read(c: Chunk) -> tuple:
            tr = rec._transitions_np(c)
            return tr.values, tr.starts, tr.skew_samples(c.ticks_per_sample), tr.channel_skew
        recording_vcd.write_fast(path, lines, chunks, read, wanted, ids, scale, fpga())
        return
    with Path(path).open("w", encoding="ascii") as f:     # 改行は OS の既定（前の write_text と同じ）
        f.write("\n".join(lines) + "\n")
        current = None
        # 同じ時刻では波形の変化が先、FPGA の変化が後（heapq.merge は同じ鍵で前の並びを先に出す）
        for t, text in heapq.merge(wave(), fpga(), key=lambda x: x[0]):
            if t != current:
                f.write(f"#{t}\n")
                current = t
            f.write(text + "\n")


def _export_rows(rec: "Recording", t0: int | None = None, t1: int | None = None, streams: list[str] | None = None,
                 kinds: list[str] | None = None, limit: int | None = None) -> Iterator[tuple]:
    """events と同じ絞り込み・並びの (t, rowid, stream, kind, a, b, body の JSON の文字列, t_end, unc) を読みながら。本体は
    記録に書いた JSON の文字列のまま（json.dumps(…, ensure_ascii=False) の文字列は、読んで書き直しても同じ文字列）。"""
    rows = rec._rows(t0, t1, streams, kinds)
    return rows if limit is None else itertools.islice(rows, max(0, int(limit)))


def export_csv(rec: "Recording", path: Path, **filters: Any) -> None:
    """出来事を CSV に（読みながら書く。本体の欄は JSON）。"""
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["t", "t_end", "stream", "kind", "a", "b", "body", "uncertainty"])
        w.writerows([t, "" if te is None else te, s, k, a, b, "" if body is None else body, "" if unc is None else unc]
                    for t, _rowid, s, k, a, b, body, te, unc in _export_rows(rec, **filters))


def export_json(rec: "Recording", path: Path, **filters: Any) -> None:
    """出来事を JSON の並びに（json.dumps([asdict(出来事), …], ensure_ascii=False) と同じ文字列を、読みながら書く）。"""
    enc = json.JSONEncoder(ensure_ascii=False).encode
    with Path(path).open("w", encoding="utf-8") as f:
        f.write("[")
        first = True
        for t, _rowid, s, k, a, b, body, te, unc in _export_rows(rec, **filters):
            f.write(('{"t": %s, "stream": %s, "kind": %s, "a": %s, "b": %s, "body": %s, "t_end": %s, "uncertainty": %s}'
                     if first else
                     ', {"t": %s, "stream": %s, "kind": %s, "a": %s, "b": %s, "body": %s, "t_end": %s, "uncertainty": %s}')
                    % (enc(t), enc(s), enc(k), enc(a), enc(b), "null" if body is None else body, enc(te), enc(unc)))
            first = False
        f.write("]")
