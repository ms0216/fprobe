"""デバイスの出来事を共通の時間軸の記録（fprobe.recording）へ取り込む（F3a）。

- ロジアナの捕捉（`Transitions`）はトリガ時刻（タイムベース）で置く。
- イベント記録（DAP・UART・SWO・補助）は 48 ビットの時刻を、読んだときのタイムベースで 64 ビットに直して入れる。
  ポストされた AP の読みは、値が次のかたまりで返ることがあるので、未解決の読みを次へ持ち越す。
- `Collector` は記録中、時計の対応付けの点（clock）と、新しいイベントを定期的に取り込む。
  深いバッファの捕捉・流し続ける捕捉の間はイベント記録を読めない（読み出しの経路を共有する）ので、`pause_events` で止める。
  イベントも流す構成（ケイパビリティ la_stream_events。R1）では、流し続けの間の取り込みを流れに引き渡す
  （`hand_over` → 流れが続きの番号から取り込む → `take_back` で続きから再開。欠けも二重も出さない）。
"""

import contextlib
import threading
import time
from typing import Any

from .events import ACK_OK, EventLog, DapEvent, resolve
from .la import Transitions
from .recording import Recorder

__all__ = ["Collector", "add_capture", "add_events", "sample_clock", "unwrap48"]

MASK48 = (1 << 48) - 1
_STREAMS = {"ap_read": "dap", "ap_write": "dap", "dp_read": "dap", "dp_write": "dap", "fail": "dap",
            "uart_rx": "uart", "uart_tx": "uart", "swo": "swo", "aux": "aux", "sample": "sample"}
_KINDS = {"uart_rx": "rx", "uart_tx": "tx", "swo": "byte", "aux": "mv", "sample": "word"}


def unwrap48(t48: int, reference: int) -> int:
    """48 ビットの時刻を、それより後に読んだタイムベース reference の上位で 64 ビットにする。"""
    return reference - ((reference - t48) & MASK48)


def add_capture(rec: Recorder, tr: Transitions) -> None:
    """捕捉を、トリガのサンプルがトリガ時刻（タイムベース）に来るように置く。"""
    ticks_per_sample = tr.sample_period_s * rec.timebase_hz
    rec.add_waveform(tr, start_ticks=tr.trigger_time - round(tr.trigger * ticks_per_sample))


def add_events(rec: Recorder, events: list[DapEvent], reference: int, carry: DapEvent | None = None) -> DapEvent | None:
    """イベントのかたまりを記録へ。carry は前のかたまりで値がまだ返っていない AP の読み。次へ持ち越す読みを返す。"""
    batch = ([carry] if carry is not None else []) + events
    rows = []
    for a in resolve(batch):
        stream = _STREAMS[a.kind]
        kind = _KINDS.get(a.kind, a.kind)
        if stream == "dap":
            rows.append((stream, kind, unwrap48(a.time, reference), a.address, a.value, {"ack": a.ack}))
        elif stream == "sample" and a.ack != ACK_OK:                # 読めなかった語（値は意味が無い）: ACK を残す
            rows.append((stream, kind, unwrap48(a.time, reference), a.address, a.value, {"ack": a.ack}))
        elif stream in ("aux", "sample"):
            rows.append((stream, kind, unwrap48(a.time, reference), a.address, a.value))
        else:
            rows.append((stream, kind, unwrap48(a.time, reference), 0, a.value))
    rec.add_many(rows)
    return _pending_read(batch)


def _pending_read(events: list[DapEvent]) -> DapEvent | None:
    """値がまだ返っていない（後に AP の読み・RDBUFF・失敗が来ていない）最後の AP の読み。"""
    pending = None
    for e in events:
        if e.kind not in (1, 2):                                  # バイト・補助はポストの連鎖に関係しない
            continue
        if not e.ok:
            pending = None
        elif e.ap and e.read:
            pending = e
        elif not e.ap and e.read and e.register == 0xC:
            pending = None
    return pending


def sample_clock(device: Any, rec: Recorder) -> None:
    """時計の対応付けの点を 1 つ取り、記録にも残す（clock ストリーム: a = タイムベース、本体に往復時間）。"""
    before = time.monotonic()
    ticks = device.time()
    after = time.monotonic()
    rec.clock.add((before + after) / 2, ticks, after - before)
    rec.add("clock", "point", ticks, body={"host_s": (before + after) / 2, "round_trip_s": after - before})


class Collector:
    """記録中、時計の対応付けと新しいイベントを定期的に取り込む（別スレッド）。"""

    def __init__(self, device: Any, rec: Recorder, events: Any = None, poll_s: float = 0.2, clock_s: float = 1.0,
                 lock: Any = None):
        self.device, self.rec = device, rec
        # 機器の読みは、ほかの操作（Probe の道具）と同じロックの中で（USB のやりとりを混ぜない）
        self._lock = lock if lock is not None else contextlib.nullcontext()
        self.events = events if events is not None else _optional_event_log(device)
        self.poll_s, self.clock_s = poll_s, clock_s
        self._seen = 0
        self._carry: DapEvent | None = None
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread: threading.Thread | None = None
        self.lost = 0

    def start(self) -> None:
        with self._lock:
            if self.events is not None:
                begin = getattr(self.events, "start", None)
                if begin is not None:
                    begin()                                        # デバイスのイベント記録をリングの先頭から始める
                self._seen = self.events.counts()[0]               # 始める前の出来事は入れない
            sample_clock(self.device, self.rec)
        self._thread = threading.Thread(target=self._run, name="fprobe-collector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.poll()

    def poll(self) -> None:
        """いまイベント記録に届いている出来事を、次の周期を待たずに取り込む（記録の範囲を読む直前に）。"""
        with self._lock:
            self._poll_events()
        self.rec.flush()

    def pause_events(self, paused: bool) -> None:
        """深いバッファの捕捉中など、イベント記録を読めない間は止める。"""
        if paused:
            self._paused.set()
        else:
            self._paused.clear()

    def hand_over(self) -> tuple[int | None, DapEvent | None]:
        """取り込みを流し続け（イベントも流す構成）に引き渡す: いまの出来事まで取り込んでから止め、続きの番号
        （events.written で数える。イベント記録が無ければ None）とポストされた読みの持ち越しを返す。"""
        with self._lock:
            if self.events is not None:
                self._read_new()
            self._paused.set()
            return (self._seen if self.events is not None else None), self._carry

    def take_back(self, seen: int | None, carry: DapEvent | None) -> None:
        """流し続けが取り込んだ続き（次の番号 seen と持ち越し）から再開する。"""
        with self._lock:
            if seen is not None:
                self._seen = seen
            self._carry = carry
            self._paused.clear()

    def _run(self) -> None:
        last_clock = time.monotonic()
        while not self._stop.wait(self.poll_s):
            if time.monotonic() - last_clock >= self.clock_s:
                with self._lock:
                    sample_clock(self.device, self.rec)
                last_clock = time.monotonic()
            with self._lock:
                self._poll_events()
            self.rec.flush()

    def _poll_events(self) -> None:
        if self.events is None or self._paused.is_set():
            return
        self._read_new()

    def _read_new(self) -> None:
        written, _ = self.events.counts()
        new = written - self._seen
        if new <= 0:
            return
        if new > self.events.capacity:                             # 読む前にリングが一周した
            self.lost += new - self.events.capacity
            self.rec.add("annotation", "events_lost", self.device.time(), a=new - self.events.capacity)
            new = self.events.capacity
        batch = self.events.read(last=new, end=written)
        self._carry = add_events(self.rec, batch, self.device.time(), carry=self._carry)
        self._seen = written


def _optional_event_log(device: Any) -> EventLog | None:
    try:
        return EventLog(device)
    except RuntimeError:                                           # イベント記録の無い構成
        return None
