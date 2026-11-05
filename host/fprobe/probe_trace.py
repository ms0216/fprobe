"""Probe の道具: トレース形式の取り込み（U14）と、SCVD（Component Viewer）。

Probe（fprobe.probe）が API の名前を委ねるサービス（fprobe.probe_service。ホストから使う名前は NEEDS）。Probe の _lock・session・target・_symbols・_recorder・_debug と、
FormatService（受け口 `_feed_start`・`_feed_stop`）、RtosService（時間軸の結果 `_rtos_last` = 解析の道具が読むもの・`_rtos_run`）、
WindowService（`_window_info`）を使う。機器に触るのは必ず _lock の中。

- トレース（fprobe.tracefmt）: SystemView・Zephyr の CTF・Tracealyzer を流れから、Event Recorder を RAM から周期的に
  （止めずに）、SystemView・CTF・TraceX・Event Recorder を RAM から一度に（スナップショット・ポストモーテム）、
  ファイルから。結果は出来事（stream "trace"）と、タスクの出入りがあれば時間軸（stream "rtos" の run / switch /
  gap。rtos_timeline_load 等の解析がそのまま使える。作り方は fprobe.trace_timeline）。
- SCVD（Component Viewer）: scvd_load → scvd_view（ターゲットのメモリを読んで表示の木）。Event Recorder の書式にも。

時刻: ターゲットの時刻を持つ形式（SystemView・CTF・Event Recorder 等）は、最初に解読した出来事をその受けた時刻に合わせ、
以後はターゲットの時刻の速さで写す（ずれは補正しない）。RAM のスナップショットは最後の出来事を読んだ時刻に合わせる（近似）。

仕様: docs/superpowers/specs/2026-10-18-u14-u15-trace-log-formats-design.md
"""

from __future__ import annotations

import dataclasses
import threading
import time
from pathlib import Path

from .debug import PyocdAccess
from .probe_formats_feed import EVENTS_LIMIT, PULL_S, SOURCES, _Feed, timebase_hz, with_error_and_window
from .probe_service import HostView
from .trace_timeline import publish_timeline
from .tracefmt.model import TargetClock, Trace, TraceEvent, write_trace
from .tracefmt.scvd import LIST_LIMIT, STRING_LIMIT, Scvd, ScvdContext
from .tracefmt.stream import SNAPSHOT_FORMATS, SNAPSHOT_LIMIT, STREAM_FORMATS, TraceStream, decode_trace, snapshot

__all__ = ["TraceService"]


class TraceService:
    """トレース形式の取り込みと SCVD（Probe のサービス）。"""

    API = ("scvd_load", "scvd_view", "trace_events", "trace_import", "trace_snapshot", "trace_start", "trace_status",
           "trace_stop")
    # ほかの領域（Probe の委譲）・試験に出す名前
    SHARED = ("_trace_feed",)
    # ホスト（Probe の核と、ほかの領域）から使う名前
    NEEDS = ("_debug", "_feed_start", "_feed_stop", "_lock", "_recorder", "_rtos_last", "_rtos_run", "_rtt", "_symbols",
             "_window_info", "session", "target")

    def __init__(self, host):
        self._host = HostView(host, self.NEEDS, type(self).__name__)
        self._trace_feed: _Feed | None = None
        self._trace_last: _Feed | None = None
        self._scvd: Scvd | None = None
        self._scvd_files: list[str] = []

    def _elf_strings(self):
        if self._host._symbols is None:
            return None
        from .logfmt.compact import ElfStrings
        return ElfStrings(self._host._symbols.path).get

    @staticmethod
    def _text_file(path: str | None) -> str | None:
        return Path(path).read_text(encoding="utf-8") if path else None

    def trace_start(self, format: str, source: str = "rtt", channel: int | str | None = None, port: int = 1,
                    description: str | None = None, metadata: str | None = None, ctf_header: str | None = None,
                    send_start: bool = True, period_s: float = PULL_S, address: int | None = None,
                    swo_baud: int | None = None,
                    core_clock_hz: float | None = None, string_limit: int = STRING_LIMIT,
                    ts_hz: float | None = None) -> dict:
        """トレースの流れの取り込みを始める（すぐ戻る）。format: systemview（RTT の "SysView"。send_start なら
        開始のコマンドを書く）/ ctf（Zephyr。metadata = ビルドの TSDL のファイル、無ければ組み込みの表と ctf_header
        ts32_id8 / ts32_id16 / ts64_id16 / id16。時刻の速さは TSDL の clock か ts_hz で明示する。どちらも無ければ
        時刻をタイムベースに写さない）/ tracealyzer（RTT の "TzData"、開始のコマンドは "TzCtrl"）/
        eventrecorder（source memory: RAM を period_s ごとに止めずに読む。EventRecorderInfo か address。書式は
        scvd_load の SCVD）。source: rtt /
        swo（ITM の port）/ uart / memory。description は SystemView の記述ファイル（SYSVIEW_<OS>.txt）。記録中なら
        出来事を stream "trace" に。trace_stop で時間軸（stream "rtos"・rtos_timeline_* の解析）にもする。"""
        if self._trace_feed is not None:
            raise RuntimeError("トレースの取り込みが動いている（trace_stop を先に）")
        if format == "eventrecorder":
            if source not in ("memory", "rtt"):
                raise ValueError("eventrecorder は source memory（RAM を周期的に読む）")
            return self._er_start(period_s, address, string_limit)
        if format not in STREAM_FORMATS:
            raise ValueError(f"流れで受けられる形式は {' / '.join(STREAM_FORMATS)} と eventrecorder（memory）")
        if source not in SOURCES:
            raise ValueError(f"source は {' / '.join(SOURCES)}")
        stream = TraceStream.create(format, description=self._text_file(description),
                                    metadata=self._text_file(metadata), ctf_header=ctf_header,
                                    strings=self._elf_strings(), ts_hz=ts_hz)
        feed = _Feed("trace", format, source, {"channel": channel, "port": port, "send_start": send_start,
                                               "description": description, "metadata": metadata,
                                               "ctf_header": ctf_header, "ts_hz": ts_hz}, stream, time.monotonic())
        feed.trace = stream.trace
        feed.deliver = lambda events, t, unc, _source, feed=feed: self._trace_received(feed, events, t, unc)
        self._host._feed_start(feed, channel, port, swo_baud, core_clock_hz)
        self._trace_feed = feed
        return self._trace_state(feed)

    def _publish_timeline(self, feed: _Feed, idle_tasks: list[str] | None) -> dict:
        """取り込んだトレースを時間軸にして RtosService の最後の結果にする（作り方は fprobe.trace_timeline）。"""
        return publish_timeline(feed, idle_tasks, rtos_running=self._host._rtos_run is not None,
                                recorder=self._host._recorder, set_last=self._set_rtos_last)

    def _set_rtos_last(self, result) -> None:
        self._host._rtos_last = result

    def _trace_received(self, feed: _Feed, events: list[TraceEvent], t: int | None, unc: float | None) -> None:
        if not events:
            return
        trace = feed.trace
        if len(trace.events) > EVENTS_LIMIT:
            del trace.events[:len(trace.events) - EVENTS_LIMIT]
            feed.warnings = [w for w in feed.warnings if not w.startswith("出来事が多い")] + [
                f"出来事が多い: 最近の {EVENTS_LIMIT} 個だけ持っている（全部は記録の stream \"trace\"）"]
        self._anchor(feed, events[-1], t, unc)                    # 受けた時刻 = 上限
        self._write_trace(feed, events)

    @staticmethod
    def _anchor(feed: _Feed, last: TraceEvent, t: int | None, unc: float | None = None) -> None:
        """最後の出来事を受けた・読んだ時刻（上限）を、その時刻の区切りの対応点にする（区切りごとに最初の 1 つ）。
        時刻の無い形・速さの分からない形は写さない。"""
        trace = feed.trace
        if not (trace.timed and trace.ts_hz and t is not None and feed.timebase_hz):
            return
        if feed.clock is None:
            feed.clock = TargetClock(trace.ts_hz, feed.timebase_hz, last.ts, t, unc, segment=last.segment)
        else:
            feed.clock.add_anchor(last.segment, last.ts, t, unc)

    def _write_trace(self, feed: _Feed, events: list[TraceEvent] | None = None) -> None:
        rec = self._host._recorder
        if rec is None or feed.clock is None:
            return
        events = feed.trace.events if events is None else events
        feed.written += write_trace(rec, feed.trace, feed.clock, events)

    def trace_stop(self, timeline: bool = True, idle_tasks: list[str] | None = None) -> dict:
        """トレースの取り込みを止める。要約（出来事の種類ごとの数・タスク・失った数・時刻の速さ）を返し、timeline なら
        タスクの出入りを時間軸にして rtos_timeline_get / rtos_timeline_load 等で解析できるようにする（記録中なら
        stream "rtos" にも）。idle_tasks: アイドルとして扱うタスクの名前（完全一致。fprobe は名前でアイドルを決めない。
        結果の timeline.idle_tasks に合った名前・合わなかった名前）。"""
        feed = self._trace_feed
        if feed is None:
            raise RuntimeError("トレースを取り込んでいない（trace_start）")
        self._host._feed_stop(feed)
        self._trace_feed, self._trace_last = None, feed
        out = self._trace_state(feed)
        if timeline:
            out["timeline"] = self._publish_timeline(feed, idle_tasks)
        return out

    def _trace_state(self, feed: _Feed) -> dict:
        trace = feed.trace
        out = {"running": feed.running, "format": feed.format, "source": feed.source,
               "settings": {k: v for k, v in feed.settings.items() if v is not None}, "bytes": feed.bytes,
               **trace.summary(), "recorded_events": feed.written, "warnings": list(feed.warnings),
               "elapsed_s": (feed.stopped or time.monotonic()) - feed.started}
        out["notes"] = list(trace.notes)
        if feed.clock is not None:
            out["clock"] = {**feed.clock.describe(), "timebase_hz": feed.timebase_hz,
                            "ts_hz": feed.trace.ts_hz, "basis": "anchor_per_segment_declared_rate"}
            out["unplaced_events"] = sum(1 for e in list(feed.trace.events) if e.segment not in feed.clock.anchors)
            out["notes"].append("時刻は時刻の区切りごとに 1 つの対応点（anchor: その区切りの最後の出来事を受けた・読んだ"
                                "時刻。出来事はそれより前なので上限）とトレースが申告した速さ（ts_hz）で写した: 出来事までの"
                                "遅れと速さの誤差は分からず、不確かさに入れていない（clock.unknown）。対応点の無い区切りの"
                                "出来事は写さない（unplaced_events）")
        elif trace.events:
            out["notes"].append("時刻の対応が無い（ターゲットの時刻の速さか受けた時刻が分からない）: 記録には書かない")
        return with_error_and_window(feed, out, self._host._window_info)

    def trace_status(self) -> dict:
        """トレースの取り込みの様子（形式・出来事の数・タスク・失った数・時刻の対応）。"""
        feed = self._trace_feed or self._trace_last
        if feed is None:
            return {"running": False}
        return self._trace_state(feed)

    def trace_events(self, offset: int = 0, limit: int = 200, kinds: list[str] | None = None,
                     task: int | None = None) -> dict:
        """取り込んだトレース（取り込み中・最後の結果・スナップショット・ファイル）の出来事を offset から limit 件。
        kinds（task_in / isr_enter / print / marker / object 等）・task（タスクの ID・番地）で絞る。t は
        タイムベース（時刻の対応があるとき）。"""
        feed = self._trace_feed or self._trace_last
        if feed is None:
            raise RuntimeError("トレースが無い（trace_start / trace_snapshot / trace_import）")
        events = [e for e in list(feed.trace.events)
                  if (kinds is None or e.kind in kinds) and (task is None or e.task == task)]
        names = feed.trace.names()
        out = []
        for e in events[offset:offset + limit]:
            item = e.to_dict()
            t = feed.clock.place(e) if feed.clock is not None else None
            if t is not None:                                # 対応点の無い区切りは時刻が分からない（t なし）
                item["t"] = t
            if e.task is not None and e.task in names:
                item["task_name"] = names[e.task]
            out.append(item)
        return {"format": feed.format, "total": len(events), "offset": offset, "events": out}


    # --- スナップショット・ファイル ---------------------------------------------------------------

    def _symbol_lookup(self, name: str) -> tuple[int, int] | None:
        if self._host._symbols is None:
            return None
        try:
            s = self._host._symbols.variable(name)
        except KeyError:
            try:
                s = self._host._symbols.symbol(name)
            except KeyError:
                return None
        return s.address, s.size

    def trace_snapshot(self, format: str, address: int | None = None, size: int | None = None,
                       description: str | None = None,
                       metadata: str | None = None, ctf_header: str | None = None, ts_hz: float | None = None,
                       timeline: bool = True, rtt_address: int | None = None,
                       max_bytes: int = SNAPSHOT_LIMIT, idle_tasks: list[str] | None = None,
                       string_limit: int = STRING_LIMIT) -> dict:
        """ターゲットの RAM のトレースのバッファを吸い出して解読する（ポストモーテム・スナップショット。止めなくてよいが、
        止めた方が書きかけが無い）。format: systemview（RTT の "SysView" のリングを消費せずに。rtt_address 省略時は
        rtt_start の制御ブロック）/ ctf（Zephyr の ram_tracing。address なら size も）/ tracex（_tx_trace_header_ptr か address。ts_hz =
        時刻の速さ）/ eventrecorder（EventRecorderInfo か address。書式は scvd_load の SCVD）。スナップショットを
        開いていれば（snapshot_open）その RAM から。最後の出来事を読んだ時刻に合わせて記録・時間軸にする（近似）。
        max_bytes = ctf・tracex のバッファを読む上限（ctf は切って read に size・max_bytes・truncated、tracex は誤り）。
        idle_tasks は trace_stop と同じ。"""
        if format not in SNAPSHOT_FORMATS:
            raise ValueError(f"RAM から吸い出せる形式は {' / '.join(SNAPSHOT_FORMATS)}")
        with self._host._lock:
            memory = PyocdAccess(self._host.target)
            if format == "systemview" and rtt_address is None:
                if self._host._rtt is not None:
                    rtt_address = self._host._rtt.address
                else:
                    found = self._symbol_lookup("_SEGGER_RTT")
                    rtt_address = found[0] if found else None
            trace, info = snapshot(format, memory.read_memory, self._symbol_lookup, rtt_address=rtt_address,
                                   memory=memory, address=address, size=size, description=self._text_file(description),
                                   metadata=self._text_file(metadata), ctf_header=ctf_header,
                                   strings=self._elf_strings(), ts_hz=ts_hz, scvd=self._scvd,
                                   scvd_context=(self._scvd_context(memory, string_limit=string_limit)
                                                 if format == "eventrecorder" else None),
                                   max_bytes=max_bytes)
            hz = timebase_hz(self._host) if self._host._debug is not None else None
            now = self._host.session.device.time() if hz else None
        feed = _Feed("trace", format, "memory", {"address": info.get("address"), "snapshot": True,
                                                  "string_limit": string_limit if format == "eventrecorder" else None}, None,
                     time.monotonic(), timebase_hz=hz, trace=trace)
        feed.stopped = feed.started
        if trace.events:
            self._anchor(feed, trace.events[-1], now)      # 読んだ後の時刻 = 上限（幅は分からない）
        if feed.clock is not None:
            feed.warnings.append("時刻の対応は、最後の出来事をそれを読んだ後の時刻に置いた上限（実際は読むまでの分だけ前。どれだけ前かは分からない）")
            self._write_trace(feed)
        self._trace_last = feed
        out = {**self._trace_state(feed), "read": info}
        if timeline:
            out["timeline"] = self._publish_timeline(feed, idle_tasks)
        return out

    def trace_import(self, format: str, path: str, description: str | None = None, metadata: str | None = None,
                     ctf_header: str | None = None, ts_hz: float | None = None, ram_buffer: bool = False,
                     timeline: bool = True, idle_tasks: list[str] | None = None) -> dict:
        """トレースのファイル（SystemView の .SVDat・CTF のストリーム・Tracealyzer の PSF・TraceX のバッファのダンプ）を
        解読する（機器に触らない）。時刻はターゲットの時刻のまま（記録には書かない）。timeline なら時間軸の解析にも。
        idle_tasks は trace_stop と同じ。"""
        data = Path(path).read_bytes()
        trace = decode_trace(format, data, description=self._text_file(description),
                             metadata=self._text_file(metadata), ctf_header=ctf_header, strings=self._elf_strings(),
                             ts_hz=ts_hz, ram_buffer=ram_buffer)
        feed = _Feed("trace", format, "file", {"path": str(path)}, None, time.monotonic(), trace=trace)
        feed.stopped = feed.started
        self._trace_last = feed
        out = {**self._trace_state(feed), "bytes": len(data)}
        if timeline:
            out["timeline"] = self._publish_timeline(feed, idle_tasks)
        return out

    # --- Event Recorder を止めずに読む ---------------------------------------------------------------

    def _er_start(self, period_s: float, address: int | None, string_limit: int = STRING_LIMIT) -> dict:
        if period_s <= 0:
            raise ValueError("period_s は正")
        if string_limit < 1:
            raise ValueError("string_limit は 1 以上")
        feed = _Feed("trace", "eventrecorder", "memory",
                     {"period_s": period_s, "address": address, "string_limit": string_limit}, None,
                     time.monotonic(),
                     trace=Trace("eventrecorder"))
        feed.poll = self._er_poll
        with self._host._lock:
            feed.timebase_hz = timebase_hz(self._host)
        self._er_poll(feed)                                        # 初めの読み（無ければここで誤り）
        feed.pump = threading.Thread(target=self._er_pump, args=(feed, period_s), name="fprobe-eventrecorder",
                                     daemon=True)
        feed.pump.start()
        self._trace_feed = feed
        return self._trace_state(feed)

    def _er_pump(self, feed: _Feed, period_s: float) -> None:
        while not feed.halt.wait(period_s):
            try:
                with feed.lock:
                    self._er_poll(feed)
            except Exception as e:
                feed.error = f"{type(e).__name__}: {e}"
                return

    def _er_poll(self, feed: _Feed) -> None:
        with self._host._lock:
            memory = PyocdAccess(self._host.target)
            part, info = snapshot("eventrecorder", memory.read_memory, self._symbol_lookup,
                                  address=feed.settings.get("address"), scvd=self._scvd,
                                  scvd_context=self._scvd_context(memory, string_limit=feed.settings["string_limit"]),
                                  since=feed.since)
            now = self._host.session.device.time() if feed.timebase_hz else None
        trace = feed.trace
        trace.ts_hz = trace.ts_hz or part.ts_hz
        trace.system = part.system
        trace.lost += part.lost
        trace.notes += [n for n in part.notes if n not in trace.notes]
        for task in part.tasks.values():
            info_t = trace.task(task.id)
            info_t.name = task.name or info_t.name
        # 時刻はターゲットの ts_overflow から作った 64 ビットの値（読みをまたいで続く。decode_event_recorder）
        trace.ts_wraps += part.ts_wraps
        if part.ts_wrap_min_back is not None:
            trace.ts_wrap_min_back = min(x for x in (part.ts_wrap_min_back, trace.ts_wrap_min_back) if x is not None)
        init_count = part.system.get("init_count")
        if feed.er_init_count not in (None, init_count):
            # 記録が初期化し直された（init_count が変わった）: ts_overflow と時刻は 0 から数え直し、前とつながらない
            trace.new_segment("recorder_reinit", init_count=init_count)
        feed.er_init_count = init_count
        if part.events and trace.events and part.events[0].ts < trace.events[-1].ts:
            # 同じ初期化の中で時刻が前の読みより戻った: ts_overflow の数え方と食い違う（事実として区切る）
            trace.new_segment("ts_went_back", previous_ts=trace.events[-1].ts, ts=part.events[0].ts)
        part.events = [dataclasses.replace(e, segment=trace.segment) for e in part.events]
        feed.since = info["next"]
        had_clock = feed.clock is not None
        trace.events.extend(part.events)
        if part.events:
            self._anchor(feed, part.events[-1], now)
        if not had_clock and feed.clock is not None:
            feed.warnings.append("時刻の対応は、最初に読んだ最後の出来事をそれを読んだ後の時刻に置いた上限（実際は読むまでの分だけ前。どれだけ前かは分からない）")
        if part.events:
            self._write_trace(feed, part.events)

    # --- SCVD（Component Viewer） ----------------------------------------------------------------

    def scvd_load(self, paths: list[str]) -> dict:
        """SCVD のファイル（RTX5.scvd・EventRecorder.scvd・ミドルウェアのもの）を読む。scvd_view の表示と、
        Event Recorder の出来事の名前・書式に使う（前に読んだものに足す。同じ ID は後のものが勝つ）。"""
        scvd = self._scvd or Scvd()
        for p in paths:
            scvd.add(Path(p).read_text(encoding="utf-8"), str(p))
            self._scvd_files.append(str(p))
        self._scvd = scvd
        return {"files": list(self._scvd_files), "components": scvd.names, "typedefs": len(scvd.typedefs),
                "events": len(scvd.events), "objects": [o.get("name") for o in scvd.objects]}

    def _scvd_context(self, memory, list_limit: int = LIST_LIMIT, string_limit: int = STRING_LIMIT) -> ScvdContext:
        symbols = self._host._symbols

        def symbol_name(address: int) -> str | None:
            if symbols is None:
                return None
            found = symbols.symbol_at(address & ~1)
            return found[0].name if found is not None and found[1] == 0 else None

        def register(name: str) -> int | None:
            try:
                return self._host.target.read_core_register(name.lower())
            except Exception:
                return None
        running = False
        try:
            running = not self._host.target.is_halted() if hasattr(self._host.target, "is_halted") else False
        except Exception:
            pass
        return ScvdContext(read=memory.read_memory, symbol=self._symbol_lookup, symbol_name=symbol_name,
                           string=self._elf_strings(), register=register, running=running, list_limit=list_limit,
                           string_limit=string_limit)

    def scvd_view(self, list_limit: int = LIST_LIMIT, string_limit: int = STRING_LIMIT) -> dict:
        """SCVD の objects（Component Viewer）を評価して、ターゲットのメモリから表示の木（property・value・children）を
        作る（止めずに読める。一貫させたいなら止めて）。読めない・式の誤り・readlist / list を上限 list_limit や輪で
        止めたこと・番地から読む文字列を上限 string_limit バイトで切ったこと（値の中にも印）は notes。"""
        if self._scvd is None:
            raise RuntimeError("SCVD が読み込まれていない（scvd_load）")
        if list_limit < 1:
            raise ValueError("list_limit は 1 以上")
        if string_limit < 1:
            raise ValueError("string_limit は 1 以上")
        with self._host._lock:
            memory = PyocdAccess(self._host.target)
            view = self._scvd.view(self._scvd_context(memory, list_limit, string_limit))
        return {"files": list(self._scvd_files), "objects": view, "list_limit": list_limit,
                "string_limit": string_limit}
