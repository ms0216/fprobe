"""変数のライブ観測の値の列（イベント記録とホストの読みから変数ごとの (時刻, 値) の列を作り、最新値・列・XY・カーソル測定にする）の部品（U43）。

WatchService（fprobe.probe_watch）に mixin される。状態（_watched・_chase_series・_watch_trigger・_host）は本体が持つ。
"""

from .collector import unwrap_events
from .events import EventLog, resolve
from .watch_stats import measure, pair_latest, xy_result
from .watch_value import _Assembler

READ_LIMIT = 4096                      # 1 回に読むイベント記録の件数（最新から）


def _window_us(spans: list | None, i: int, hz: float) -> dict:
    """値の列の i 番目の、値を読んだ瞬間の範囲（µs。線上の点の回路のサンプラの値だけ。無ければ空）。"""
    if not spans:
        return {}
    lo, hi = spans[i]
    return {"time_lo_us": None if lo is None else lo * 1e6 / hz, "time_hi_us": None if hi is None else hi * 1e6 / hz}


class WatchSeriesMixin:
    """値の列の読み出し。"""

    def _series(self, last: int, windows: dict | None = None) -> tuple[dict[str, list], float, dict[str, int | None]]:
        """イベント記録の最新の分（サンプラ）とホストの読み（ポインタをたどる式）から、変数ごとの (時刻, 値) の列と、
        変数ごとの読んだ範囲の始め（それより古い値があったが読まなかった・残っていなければその時刻、全部読めば None）。

        サンプラの値の時刻は回路で意味が違う: DAP の時刻が線上の点の回路（ケイパビリティ dap_line_time。time-accuracy
        S2）は読みを頼んだ転送の線上の点（窓の始め）、古い回路は値が返った周期。windows を渡すと、線上の点の回路では
        変数ごとに値の列と同じ並びの (t_lo, t_hi)（対象がその値を読んだ瞬間の範囲。複数の語の変数は語の範囲を合わせた
        もの。分からない端は None）を入れ、windows["_time_point"] に時刻の意味（read_request / value_returned）を入れる。"""
        if not self._watched:
            raise RuntimeError("観測していない（watch_set を先に）")
        out: dict[str, list] = {w.name: [] for w in self._watched}
        start: dict[str, int | None] = {w.name: None for w in self._watched}
        events: list = []
        older = False                                    # イベント記録に、読んだより古いイベントがあった
        with self._host._lock:
            device = self._host.session.device
            caps = device.capabilities()
            hz = caps.timebase_hz
            line_time = bool(caps.dap_line_time)
            output_ticks = caps.dap_output_ticks
            if any(not w.chase for w in self._watched):
                log = EventLog(device)
                written = log.counts()[0]
                events = log.read(min(last, READ_LIMIT), end=written)
                older = log.records < written
            reference = device.time()                    # 48 ビットのイベントの時刻を 64 ビットにする基準（読んだ後）
            for name, series in self._chase_series.items():
                kept = list(series)
                out[name] = kept[-last:]
                if out[name] and self._chase_total.get(name, 0) > len(out[name]):
                    start[name] = out[name][0][0]
        resolved = list(resolve(unwrap_events(events, reference), line_time=line_time, output_ticks=output_ticks))
        if older and resolved:
            for w in self._watched:
                if not w.chase:
                    start[w.name] = resolved[0].time
        assembler = _Assembler(self._watched)
        words = {w.name: w.words for w in self._watched if not w.chase}
        word_windows: dict[int, tuple[int | None, int | None]] = {}
        if windows is not None:
            windows["_time_point"] = "read_request" if line_time else "value_returned"
            if line_time:
                windows.update({name: [] for name in words})
        for a in resolved:
            if a.kind == "sample":
                word_windows[a.address] = (a.time_lo, a.time_hi)
                for name, value in assembler.feed(a.address, a.value):
                    out[name].append((a.time, value))
                    if windows is not None and line_time:
                        spans = [word_windows.get(x, (None, None)) for x in words[name]]
                        los, his = [s[0] for s in spans], [s[1] for s in spans]
                        windows[name].append((None if None in los else min(los), None if None in his else max(his)))
        return out, hz, start

    def watch_values(self) -> dict:
        """観測している変数の最新の値と、その時刻（タイムベースの µs）。time_point = 時刻の意味（_series）。線上の点の
        回路では値を読んだ瞬間の範囲 time_lo_us・time_hi_us も（分からない端は None）。"""
        windows: dict = {}
        series, hz, _ = self._series(READ_LIMIT, windows)
        return {"time_point": windows["_time_point"],
                "values": [{"name": name, "time_us": s[-1][0] * 1e6 / hz, "value": s[-1][1],
                            **_window_us(windows.get(name), -1, hz)} if s
                           else {"name": name, "time_us": None, "value": None} for name, s in series.items()]}

    def watch_series(self, name: str, last: int = 1000, after_us: float | None = None) -> dict:
        """変数 1 つの値の列（古い順。時刻はタイムベースの µs）。last はイベント記録から読む件数（最大 4096）。
        from_us = 読んだ範囲の始め（それより古い値があったが読まなかった・残っていない。全部読めば None）。
        after_us = 呼び出し側が持っている最後の値の時刻。それが from_us より前なら、その間の値は返らない（読まなかった・
        残っていない）ので gap = [after_us, from_us] で知らせる（無ければ None。呼び出し側は検出しない）。"""
        windows: dict = {}
        series, hz, start = self._series(last, windows)
        if name not in series:
            raise KeyError(f"{name!r} は観測していない")
        from_us = None if start[name] is None else start[name] * 1e6 / hz
        return {"name": name, "time_point": windows["_time_point"],
                "samples": [{"time_us": t * 1e6 / hz, "value": v, **_window_us(windows.get(name), i, hz)}
                            for i, (t, v) in enumerate(series[name])],
                "from_us": from_us,
                "gap": [after_us, from_us] if after_us is not None and from_us is not None and after_us < from_us else None}

    def watch_xy(self, x: str, y: str, last: int = 1000, max_dt_us: float | None = None) -> dict:
        """2 つの変数の XY の組（pair_latest: x の各値に、その時刻以前で最新の y。2 つは同時に測っていないので、
        点ごとに両方の時刻を出し、差の最大 dt_max_us も返す）。last・from_us は watch_series と同じ。"""
        series, hz, start = self._series(last)
        for name in (x, y):
            if name not in series:
                raise KeyError(f"{name!r} は観測していない")
        us = 1e6 / hz
        pairs, unpaired, excluded = pair_latest(series[x], series[y], None if max_dt_us is None else max_dt_us / us)
        return xy_result(x, y, pairs, unpaired, excluded, max_dt_us, us,
                         {n: None if start[n] is None else start[n] * us for n in (x, y)})

    def watch_measure(self, a_us: float, b_us: float, names: list[str] | None = None, source: str = "live") -> dict:
        """カーソル A・B の測定（measure）。source = "live": イベント記録の最新の分（watch_series と同じ）と
        ホストの読み、"capture": 条件つきの取得の結果。names を省くと観測しているすべて。"""
        if source == "live":
            series, hz, _ = self._series(READ_LIMIT)
            points = {n: [(t * 1e6 / hz, v) for t, v in s] for n, s in series.items()}
        elif source == "capture":
            trigger = self._watch_trigger
            if trigger is None:
                raise RuntimeError("条件つきの取得が無い（watch_trigger を先に）")
            points = {w.name: [] for w in self._watched}
            for t, name, value in sorted(trigger.capture.samples, key=lambda x: x[0]):
                points.setdefault(name, []).append((t * 1e6 / trigger.hz, value))
        else:
            raise ValueError("source は live か capture")
        for n in names or []:
            if n not in points:
                raise KeyError(f"{n!r} は観測していない")
        return measure({n: points[n] for n in (names or points)}, a_us, b_us)
