"""Probe の解析の道具: 記録の SQL 問い合わせ・ロジアナの測定・記録の書き出し（Perfetto）。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う mixin。Probe の _lock・_capture・_capture_info・_reading（記録中のもの、または
開いたもの）を使う。どれも機器に触らない（ホストにある捕捉・記録を読むだけ）。
"""

from pathlib import Path

from . import voltage
from .api import NoData
from .code_at import code_at
from .la_measure import measure, measure_recording
from .profile.dwt import DWT_PCSR
from .recording_sql import DEFAULT_LIMIT, DEFAULT_TIMEOUT_S, query
from .symbols import Symbols

__all__ = ["AnalysisTools"]


class AnalysisTools:
    def _init_analysis(self) -> None:
        self._code_at_elf: tuple | None = None             # code_at に渡された ELF の読み込み（(印, Symbols)）

    # --- 記録の SQL 問い合わせ（U24） -------------------------------------------------------------

    def recording_query(self, sql: str, params: list | dict | None = None, limit: int = DEFAULT_LIMIT,
                        timeout_s: float = DEFAULT_TIMEOUT_S, t0: int | None = None, t1: int | None = None) -> dict:
        """記録（記録中のもの、または開いたもの）に読み取り専用の SQL を 1 文。表は events(t, t_end, stream, kind, a, b,
        body（JSON。json_extract で読む）, unc) と chunks（波形のかたまりの索引）、関数 timebase_hz()・seconds(t)。
        読み以外（書き・PRAGMA・ATTACH）は断る。limit 行・timeout_s 秒まで（超えたら truncated / 誤り）。
        長期の記録は t0 / t1 に重なる分割をつないで読む（省くと新しい分割から。segments・partial が返る）。"""
        view = self._reading()
        out = query(view.directory, sql, params, limit=limit, timeout_s=timeout_s, t0=t0, t1=t1)
        return {"recording": view.directory.name, **out}

    def recording_kinds(self, t0: int | None = None, t1: int | None = None) -> dict:
        """記録の範囲 [t0, t1)（省くと全体）の stream・kind ごとの件数・最初と最後の時刻・全体に対する割合（share）・
        1 秒あたり（per_second = 件数 ÷ 範囲の長さ。全体なら記録の最初から最後まで）。件数の多い順（同じなら stream・
        kind の順）。長期の記録はすべての分割を数える（行・分割の上限で欠けない）。"""
        view = self._reading()
        hz = view.meta["timebase_hz"]
        if t0 is None or t1 is None:
            span = view.span()
            lo = t0 if t0 is not None else (span[0] if span else None)
            hi = t1 if t1 is not None else (span[1] + 1 if span else None)
        else:
            lo, hi = t0, t1
        rows = view.kind_counts(t0, t1)
        total = sum(r[2] for r in rows)
        seconds = (hi - lo) / hz if lo is not None and hi is not None and hi > lo and hz else None
        kinds = [{"stream": s, "kind": k, "count": n, "first": a, "last": b, "share": n / total if total else 0.0,
                  "per_second": n / seconds if seconds else None} for s, k, n, a, b in rows]
        kinds.sort(key=lambda x: -x["count"])
        span_out = None if lo is None else [lo, hi if t1 is not None else hi - 1]
        return {"recording": view.directory.name, "timebase_hz": hz, "span": span_out, "seconds": seconds,
                "total": total, "kinds": kinds}

    def recording_voltage(self, below_v: float | None = None, t0: int | None = None, t1: int | None = None) -> dict:
        """記録の電圧（stream aux・kind mv。b = mV）の範囲 [t0, t1)（省くと全体）の統計（V）・いちばん低い点
        lowest {t, volts}（同じ値なら早い方）、below_v（使う人が渡す V）以下の連続する点の区間 dips（fprobe.voltage.dips。
        渡さなければ null）。すべての出来事を数える（件数の上限で欠けない）。"""
        query = {"stream": "aux", "kind": "mv", **({"t0": t0} if t0 is not None else {}), **({"t1": t1} if t1 is not None else {})}
        points = sorted((e.t, e.b / 1000) for e in self._reading().find(query))
        low = min(points, key=lambda p: p[1], default=None)
        return {"stats": voltage.stats([v for _, v in points]),
                "lowest": None if low is None else {"t": low[0], "volts": low[1]},
                "below_v": below_v, "dips": None if below_v is None else voltage.dips(points, below_v)}

    # --- 時刻からコードの位置へ（画面の作り直し §4） ---------------------------------------------

    def recording_code_at(self, t: int, elf: str | None = None) -> dict:
        """記録（記録中のもの、または開いたもの）の時刻 t（タイムベース）の前後に残っている事実（fprobe.code_at）。
        行は elf（省くと読み込んである ELF）で引く。使った ELF は elf、記録を始めたときの ELF は recorded_elf。"""
        view = self._reading()
        if elf is None:
            symbols = self._symbols
            path = str(symbols.path) if symbols is not None else None
        else:
            symbols, path = self._code_at_symbols(elf), elf
        return code_at(view, t, symbols, path)

    def _code_at_symbols(self, path: str) -> Symbols:
        """渡された ELF（カーソルを動かすたびに読み直さない。ファイルが替われば読み直す）。"""
        stamp = (path, Path(path).stat().st_mtime_ns)
        cached = self._code_at_elf
        if cached is None or cached[0] != stamp:
            cached = (stamp, Symbols(path))
            self._code_at_elf = cached
        return cached[1]

    # --- ロジアナの測定（U19） --------------------------------------------------------------------

    def la_measure(self, start: int = 0, end: int | None = None, channels: list[int] | None = None,
                   relations: list[dict] | None = None, capture_id: int | None = None) -> dict:
        """最後の捕捉の範囲 [start, end)（サンプル）を測る: チャンネルごとの周波数・周期・デューティ・high / low の幅の統計、
        relations でチャンネル間の時間差（delay・setup_hold）。位置（*_at）はサンプル番号。"""
        with self._lock:
            tr, info = self._capture, self._capture_info
        if tr is None:
            raise NoData("捕捉が無い（la_capture / la_deep_capture を先に）")
        if capture_id is not None and capture_id != info["id"]:
            raise KeyError(f"捕捉 {capture_id} は残っていない（最後の捕捉は {info['id']}）")
        return {"capture": info, **measure(tr, start, end, channels, relations)}

    def recording_waveform_measure(self, t0: int | None = None, t1: int | None = None,
                                   channels: list[int] | None = None, relations: list[dict] | None = None) -> dict:
        """記録の波形の時刻の範囲 [t0, t1)（タイムベース）を la_measure と同じく測る（捕捉ごとに測って合わせる）。
        位置はタイムベース。"""
        return measure_recording(self._reading(), t0, t1, channels, relations)

    # --- 記録の書き出し（U13）の部品 ---------------------------------------------------------------

    def _counter_names(self) -> dict[int, str]:
        """DAP サンプラの語の番地 → カウンタの名前（Perfetto への書き出し）。名前の付け方は _sample_name。分からない
        番地は書き出し側が番地で名付ける。"""
        view = self._view
        addresses = set() if view is None else set(view.distinct("a", streams=["sample"]))
        addresses |= {DWT_PCSR, *(word for w in self._watched for word in w.words)}
        run = self._rtos_run
        if run is not None and run.address is not None:
            addresses.add(run.address)
        names = {a: self._sample_name(a) for a in addresses}
        return {a: n for a, n in names.items() if n is not None}

    def _sample_name(self, address: int) -> str | None:
        """DAP サンプラの語の番地の名前: 観測中の式（watch_set。2 語目からは +オフセット）・タスクの時間軸の変数・
        PCSR を優先し、ほかは ELF のシンボル（変数 + オフセット）。分からなければ None。"""
        for w in self._watched:
            if address in w.words:
                return w.name if address == w.words[0] else f"{w.name}+{address - w.words[0]}"
        run = self._rtos_run
        if run is not None and run.address == address:
            return "実行中のタスク（TCB）"
        if address == DWT_PCSR:
            return "PC（DWT_PCSR）"
        named = ((self._view.meta if self._view is not None else None) or {}).get("sample.names") or {}
        if f"0x{address:08X}" in named:                                    # 記録中に watch_set の names で付けた名前
            return named[f"0x{address:08X}"]
        syms = self._symbols
        hit = syms.symbol_at(address) if syms is not None else None
        return None if hit is None else hit[0].name + (f"+{hit[1]}" if hit[1] else "")
