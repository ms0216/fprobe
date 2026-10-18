"""機器なしで画面を試すデモ（fake_daemon.py --demo）の一部: Event Log・Recordings（記録の検索・統計・印・書き出し）・Logic Analyzer（解読・トリガ・測定）のデモの値（G07・G09）。

DemoProbe（fake_demo.py）に混ぜる mixin。DemoProbe の偽の RAM（self._mem）・本物の ELF（self._elf）・デモの記録
（self._elf._view。tests/demo_recording.py）・実行の模擬（self.counter・self.state）を使う。値の形は本物の Probe の
同じ名前のメソッドと同じにする（画面はデモと本物を区別しない）。

- 記録の読み（find・timing.measure・書き出し・波形の測定・分割）は本物の Probe（self._elf）にそのまま渡す。
- 記録の一覧・開く・始める・止めるは、デモの記録と同じフォルダ（一時フォルダ）の中だけで本物と同じに動く
  （利用者の本物の記録 ~/.fprobe/recordings は見せない・書かない）。記録中は、ホストの時刻 → タイムベースの対応を
  デモの起動からの経過で作り、操作（デーモンの host の出来事）・印・捕捉と、変数のサンプルが入る。
- ロジアナの捕捉（FakeProbe.la_capture の ch0 = 1 MHz の時計 等）の ch2 に UART（1 Mbaud 8N1）の行を足し、
  解読（la.decode）・測定（la.measure）を本物で試せる。1 か所に枠の誤り（解読の誤りの赤）。
- クロストリガ（xtrig.*）は設定を覚える偽物。多段トリガは仕掛けてから 0.6 秒ごとに段を進め、最後の段で発火する。
"""

import re
import threading
import time
from array import array
from datetime import datetime
from pathlib import Path

from fprobe.la import Transitions

__all__ = ["SearchDemo"]

HZ = 10_000_000                                   # デモの記録のタイムベース（demo_recording.HZ）
UART_CHANNEL = 2
UART_BAUD = 1_000_000
UART_TEXT = b"fprobe: hello\r\nOK\r\n"
UART_BAD_BYTE = 16                                # この番目のバイトの止めビットを 0 にする（枠の誤り）
STAGE_S = 0.6                                     # 多段トリガの偽物が 1 段進む時間
DEMO_FEED_S = 0.1                                 # 記録中に変数のサンプルを足す間隔


class SearchDemo:
    def _init_search(self) -> None:
        """DemoProbe.__init__ から（ELF を読み、接続・停止した後）。"""
        self._search_t0 = time.monotonic()        # デモのタイムベースの 0（記録中のホストの時刻の写し）
        self._search_root: Path | None = None
        self._feeder: threading.Event | None = None
        self._xtrig = {"route": {"to_la": [], "to_inject": []}, "inject": {"address": 0xE000EDF0, "value": 0xA05F0003,
                                                                            "ap": 0},
                       "match": None, "match_count": 0, "match_time": 0, "sequence": None, "armed_at": None,
                       "fired_at": None}

    # --- ロジアナ: 偽の捕捉に UART の行を足す ---------------------------------------------------------

    def la_capture(self, rate_hz=None, pretrigger=1024, mask=0, value=0, rise=0, fall=0, timeout_s=5.0, vcd_path=None):
        e = self._elf
        rec, e._recorder = e._recorder, None                      # 記録へは UART を足した捕捉だけを入れる
        try:
            out = super().la_capture(rate_hz, pretrigger, mask, value, rise, fall, timeout_s, vcd_path)
        finally:
            e._recorder = rec
        tr = e._capture
        with_uart = _with_uart(tr, UART_CHANNEL, UART_BAUD, UART_TEXT, self._ticks() if e._recorder else 0)
        e._capture_id -= 1                                        # 同じ捕捉を置き換える（番号を進めない）
        e._keep_capture(with_uart, "la.capture")
        return out

    def la_decoders(self):
        return self._elf.la_decoders()

    def la_decode(self, protocol, settings=None, capture_id=None, start=None, end=None, kinds=None, errors_only=False,
                  offset=0, limit=200):
        return self._elf.la_decode(protocol, settings, capture_id, start, end, kinds, errors_only, offset, limit)

    def la_measure(self, start=0, end=None, channels=None, relations=None, capture_id=None):
        return self._elf.la_measure(start, end, channels, relations, capture_id)

    # --- 記録の読み（本物） -------------------------------------------------------------------------

    def recording_events(self, t0=None, t1=None, streams=None, kinds=None, limit=1000):
        return self._elf.recording_events(t0, t1, streams, kinds, limit)

    def recording_find(self, query):
        return self._elf.recording_find(query)

    def recording_intervals(self, start, end):
        return self._elf.recording_intervals(start, end)

    def timing_measure(self, start, end, bins=20, outliers="iqr", limit=1000):
        return self._elf.timing_measure(start, end, bins, outliers, limit)

    def recording_waveform_measure(self, t0=None, t1=None, channels=None, relations=None):
        return self._elf.recording_waveform_measure(t0, t1, channels, relations)

    def recording_export(self, format, path, t0=None, t1=None, streams=None, waveform=True, channels=None):
        return self._elf.recording_export(format, path, t0, t1, streams, waveform, channels)

    def recording_query(self, sql, params=None, limit=1000, timeout_s=5.0, t0=None, t1=None):
        return self._elf.recording_query(sql, params, limit, timeout_s, t0, t1)

    def recording_segments(self):
        return self._elf.recording_segments()

    def recording_mark(self, text, t=None):
        return self._elf.recording_mark(text, t)

    def record_host_event(self, stream, kind, body):
        self._elf.record_host_event(stream, kind, body)          # デーモンの操作の記録（記録中だけ書かれる）

    # --- 記録の一覧・開く・始める・止める（デモのフォルダの中で本物と同じに） ---------------------------

    def _recordings(self) -> Path:
        """デモの記録のフォルダ（デモの記録を開いた一時フォルダ）。本物の Probe の保存先もここにする。"""
        if self._search_root is None:
            view = self._elf._view
            if view is None:
                raise RuntimeError("デモの記録が無い")
            self._search_root = Path(view.directory).parent
            root = self._search_root
            self._elf._recordings_dir = lambda: root
        return self._search_root

    def recording_list(self):
        self._recordings()
        return self._elf.recording_list()

    def recording_open(self, name):
        self._recordings()
        return self._elf.recording_open(name)

    def recording_start(self, name="", events=True, segment_s=None, segment_bytes=None, max_bytes=None,
                        decimate_after_s=None, decimate_streams=None):
        from fprobe.recording import Recording
        e = self._elf
        if e._recorder is not None:
            raise RuntimeError("記録中（recording_stop を先に）")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^\w\-]+", "_", name).strip("_")
        directory = self._recordings() / (f"{stamp}-{safe}" if safe else stamp)
        rec = e._make_recorder(directory, HZ, {"name": name, "board": "fake", "design": "fprobe", "elf": None},
                               segment_s, segment_bytes, max_bytes, decimate_after_s, decimate_streams)
        rec.clock.add(self._search_t0, 0, 1e-4)                   # ホストの時刻 → デモのタイムベース
        e._recorder = rec
        e._view = Recording.open(directory)
        stop = self._feeder = threading.Event()
        threading.Thread(target=self._feed_recording, args=(rec, stop), daemon=True).start()
        return {"recording": directory.name, "path": str(directory)}

    def recording_stop(self):
        e = self._elf
        rec = e._recorder
        if rec is None:
            raise RuntimeError("記録していない")
        if self._feeder is not None:
            self._feeder.set()
            self._feeder = None
        rec.close()
        e._recorder = None
        return {"recording": rec.directory.name, "events": e._view.count() if e._view else 0}

    def _ticks(self) -> int:
        return round((time.monotonic() - self._search_t0) * HZ)

    def _feed_recording(self, rec, stop: threading.Event) -> None:
        """記録中: 変数のサンプル（counter）と電圧を足す（本物の集める側の代わり）。"""
        from fake_demo import COUNTER
        while not stop.wait(DEMO_FEED_S):
            t = self._ticks()
            try:
                rec.add("sample", "word", t, COUNTER, self.counter & 0xFFFF_FFFF)
                rec.add("aux", "mv", t, 0, 3300)
                rec.flush()
            except Exception:                                     # 止めた直後（閉じた記録）
                return

    # --- クロストリガ（設定を覚える偽物） -----------------------------------------------------------

    def xtrig_route(self, to_la=None, to_inject=None):
        self._xtrig["route"] = {"to_la": list(to_la or []), "to_inject": list(to_inject or [])}
        return dict(self._xtrig["route"])

    def xtrig_set_inject(self, address=0xE000EDF0, value=0xA05F0003, ap=0):
        self._xtrig["inject"] = {"address": address, "value": value, "ap": ap}

    def xtrig_set_match(self, address, address_mask=0xFFFFFFFF, data=0, data_mask=0, on_write=True, on_read=False):
        self._xtrig["match"] = {"address": address, "address_mask": address_mask, "data": data, "data_mask": data_mask,
                                "on_write": on_write, "on_read": on_read}
        self._xtrig["match_count"], self._xtrig["match_time"] = 3, self._ticks()     # 偽: 一致を数回見た

    def xtrig_set_sequence(self, stages, pattern_a=None, pattern_b=None, arm=True):
        names = {"pattern_a", "pattern_b", "dap_match", "host"}
        if not 1 <= len(stages) <= 4:
            raise ValueError("段は 1〜4 個")
        for st in stages:
            unknown = set(st["when"]) - names
            if unknown:
                raise KeyError(f"知らない事象 {sorted(unknown)}")
        self._xtrig.update(sequence={"stages": stages, "pattern_a": pattern_a, "pattern_b": pattern_b},
                           armed_at=time.monotonic() if arm else None, fired_at=None)
        stage, fired, _ = self._sequence_state()
        return {"stages": len(stages), "armed": arm, "stage": stage, "fired": fired}

    def _sequence_state(self) -> tuple[int, bool, int]:
        x = self._xtrig
        seq, armed = x["sequence"], x["armed_at"]
        if seq is None or armed is None:
            return 0, False, 0
        n = len(seq["stages"])
        stage = int((time.monotonic() - armed) / STAGE_S)
        if stage >= n:
            if x["fired_at"] is None:
                x["fired_at"] = round((armed + n * STAGE_S - self._search_t0) * HZ)
            return n - 1, True, x["fired_at"]
        return stage, False, 0

    def xtrig_sequence_status(self):
        stage, fired, t = self._sequence_state()
        return {"stage": stage, "fired": fired, "time": t}

    def xtrig_fire(self):
        pass

    def xtrig_status(self):
        x = self._xtrig
        return {"inject": {"busy": False, "result": "ok", "ack": 1, "count": 0, "time": 0},
                "match": {"count": x["match_count"], "time": x["match_time"]}}


def _with_uart(tr: Transitions, channel: int, baud: int, text: bytes, trigger_time: int) -> Transitions:
    """捕捉の channel に UART（8N1、アイドル High）の行を足した捕捉。行は捕捉の 1/8 から始める。"""
    n = tr.length
    bits = []                                                     # 線の値をビットの時間ごとに
    for k, byte in enumerate(text):
        bad = k == UART_BAD_BYTE
        bits += [0] + [(byte >> b) & 1 for b in range(8)] + [0 if bad else 1] + ([1, 1] if bad else [])
    level = bytearray(b"\x01") * n                                # channel の各サンプルの値（アイドル High）
    per_bit = 1 / (baud * tr.sample_period_s)
    first = n / 8
    for b, v in enumerate(bits):
        for i in range(round(first + b * per_bit), min(n, round(first + (b + 1) * per_bit))):
            level[i] = v
    values, starts = array("H"), array("Q")
    j = 0
    current = tr.values[0]
    previous = None
    bit = 1 << channel
    for i in range(n):
        while j + 1 < len(tr.starts) and tr.starts[j + 1] <= i:
            j += 1
            current = tr.values[j]
        v = (current & ~bit) | (bit if level[i] else 0)
        if v != previous:
            values.append(v)
            starts.append(i)
            previous = v
    return Transitions(values, starts, n, tr.trigger, trigger_time, tr.sample_period_s, tr.channels)
