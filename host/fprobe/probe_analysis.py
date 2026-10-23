"""Probe の解析の道具: 記録の SQL 問い合わせ・ロジアナの測定・記録の書き出し（Perfetto）。

Probe（fprobe.mcp_server）に混ぜて使う mixin。Probe の _lock・_capture・_capture_info・_reading（記録中のもの、または
開いたもの）を使う。どれも機器に触らない（ホストにある捕捉・記録を読むだけ）。
"""

from .la_measure import measure, measure_recording
from .profile.dwt import DWT_PCSR
from .recording_sql import DEFAULT_LIMIT, DEFAULT_TIMEOUT_S, query

__all__ = ["AnalysisTools"]


class AnalysisTools:
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

    # --- ロジアナの測定（U19） --------------------------------------------------------------------

    def la_measure(self, start: int = 0, end: int | None = None, channels: list[int] | None = None,
                   relations: list[dict] | None = None, capture_id: int | None = None) -> dict:
        """最後の捕捉の範囲 [start, end)（サンプル）を測る: チャンネルごとの周波数・周期・デューティ・high / low の幅の統計、
        relations でチャンネル間の時間差（delay・setup_hold）。位置（*_at）はサンプル番号。"""
        with self._lock:
            tr, info = self._capture, self._capture_info
        if tr is None:
            raise RuntimeError("捕捉が無い（la_capture / la_deep_capture を先に）")
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
        run = getattr(self, "_rtos_run", None)
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
        run = getattr(self, "_rtos_run", None)
        if run is not None and run.address == address:
            return "実行中のタスク（TCB）"
        if address == DWT_PCSR:
            return "PC（DWT_PCSR）"
        named = (getattr(getattr(self, "_view", None), "meta", None) or {}).get("sample.names") or {}
        if f"0x{address:08X}" in named:                                    # 記録中に watch_set の names で付けた名前
            return named[f"0x{address:08X}"]
        syms = self._symbols
        hit = syms.symbol_at(address) if syms is not None else None
        return None if hit is None else hit[0].name + (f"+{hit[1]}" if hit[1] else "")
