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
import dataclasses
import threading
import time
from typing import Any

from . import generated as g
from .events import ACK_OK, KIND_OBS_RECORD, KIND_OBS_SYNC, EventLog, DapEvent, resolve
from .fpga_obs import ObsDecoder
from .la import Transitions
from .recording import Recorder

__all__ = ["Collector", "add_capture", "add_events", "sample_clock", "unwrap48", "unwrap_events"]

MASK48 = (1 << 48) - 1
_STREAMS = {"ap_read": "dap", "ap_write": "dap", "drw_read": "dap", "drw_write": "dap", "inject": "dap", "dp_read": "dap",
            "dp_write": "dap", "fail": "dap",
            "uart_rx": "uart", "uart_tx": "uart", "swo": "swo", "aux": "aux", "sample": "sample",
            "sync_pair": "sync"}
_KINDS = {"uart_rx": "rx", "uart_tx": "tx", "swo": "byte", "aux": "mv", "sample": "word", "sync_pair": "pair"}


def unwrap48(t48: int, reference: int) -> int:
    """48 ビットの時刻を、それより後に読んだタイムベース reference の上位で 64 ビットにする。"""
    return reference - ((reference - t48) & MASK48)


def unwrap_events(events: list[DapEvent], reference: int) -> list[DapEvent]:
    """イベントの 48 ビットの時刻を、それより後に読んだタイムベース reference で 64 ビットにしたイベントの並び
    （48 ビットの折り返しをまたいでも時刻が戻らない）。"""
    return [dataclasses.replace(e, time=unwrap48(e.time, reference)) for e in events]


def add_capture(rec: Recorder, tr: Transitions) -> None:
    """捕捉を、トリガのサンプルがトリガ時刻（タイムベース）に来るように置く。"""
    ticks_per_sample = tr.sample_period_s * rec.timebase_hz
    rec.add_waveform(tr, start_ticks=tr.trigger_time - round(tr.trigger * ticks_per_sample))


def add_events(rec: Recorder, events: list[DapEvent], reference: int, carry: DapEvent | None = None,
               obs: ObsDecoder | None = None) -> DapEvent | None:
    """イベントのかたまりを記録へ。carry は前のかたまりで値がまだ返っていない AP の読み。次へ持ち越す読みを返す。
    FPGA の観測（種類 8・9）は obs の復号器で値の並び（stream fpga: value・gap・group）にする（無ければ捨てる）。"""
    observed = [e for e in events if e.kind in (KIND_OBS_RECORD, KIND_OBS_SYNC)]
    events = [e for e in events if e.kind not in (KIND_OBS_RECORD, KIND_OBS_SYNC)]
    batch = ([carry] if carry is not None else []) + events
    rows = []
    if obs is not None and observed:
        rows += _obs_rows(obs.feed(observed), reference)
    for a in resolve(batch):
        stream = _STREAMS[a.kind]
        kind = _KINDS.get(a.kind, a.kind)
        if kind == "inject":                                        # 割り込み書き込み: 転送数（旧い回路は null）
            rows.append((stream, kind, unwrap48(a.time, reference), a.address, a.value,
                         {"ack": a.ack, "transfers": a.transfers}))
        elif stream == "dap":
            rows.append((stream, kind, unwrap48(a.time, reference), a.address, a.value, {"ack": a.ack}))
        elif stream == "sample" and a.ack != ACK_OK:                # 読めなかった語（値は意味が無い）: ACK を残す
            rows.append((stream, kind, unwrap48(a.time, reference), a.address, a.value, {"ack": a.ack}))
        elif stream == "sync":                                      # body = 親の 64 ビットの時刻（a・b は下位・上位）
            rows.append((stream, kind, unwrap48(a.time, reference), a.address, a.value,
                         {"parent": a.value << 32 | a.address}))
        elif stream in ("aux", "sample"):
            rows.append((stream, kind, unwrap48(a.time, reference), a.address, a.value))
        else:
            rows.append((stream, kind, unwrap48(a.time, reference), 0, a.value))
    rec.add_many(rows)
    return _pending_read(batch)


def _obs_rows(samples, reference: int) -> list[tuple]:
    """FPGA の観測の値の並び → stream fpga の行。印が来ないまま鎖が終わって時刻が決まらないものは捨てず、別の種類
    （value_untimed・gap_untimed・group_untimed）で、時刻を「受けた時」（起きた時刻の上限。起きたのはそれより前）にして
    入れる（R17。時刻の決まった value・gap・group と混ぜない: 状態の段・VCD・相関はそれらだけを使う）。"""
    rows = []
    for s in samples:
        untimed = s.time_fprobe is None
        if untimed and s.received is None:
            continue
        t = unwrap48((s.received if untimed else round(s.time_fprobe)) & MASK48, reference)
        suffix = "_untimed" if untimed else ""
        if s.kind == "value":
            rows.append(("fpga", "value" + suffix, t, s.group, s.value))
        elif s.kind in ("gap", "hidden"):
            rows.append(("fpga", s.kind + suffix, t, s.lost, 0))
        else:
            rows.append(("fpga", "group" + suffix, t, s.group, 0))
    return rows


def flush_obs(rec: Recorder, obs: ObsDecoder, reference: int) -> None:
    """記録を止めるとき: 復号器に残っている値（印の来ていない鎖）を出し切って記録へ。"""
    rows = _obs_rows(obs.flush(), reference)
    if rows:
        rec.add_many(rows)


def _pending_read(events: list[DapEvent]) -> DapEvent | None:
    """値がまだ返っていない（後に AP の読み・RDBUFF・失敗が来ていない）最後の AP の読み。"""
    pending = None
    for e in events:
        if e.kind != 1:                                           # ホストの転送だけがポストの連鎖を作る
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


RECENT_OBS = 20_000                        # 復号器が覚えておく最近の値（fpga_obs_read の窓の上限）


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
        self.obs = ObsDecoder(recent=RECENT_OBS)   # FPGA の観測の復号器（流し続けの間は流れと共有する）
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread: threading.Thread | None = None
        self.lost = 0
        self.errors = 0                            # 読み出しに失敗した回数（失敗しても取り込みは続ける）
        self.last_error: str | None = None

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
        try:
            self.poll()                                            # 最後の取り込み（失敗しても記録は閉じられるように）
        except Exception as e:                                     # noqa: BLE001
            self._note(e)
        try:
            flush_obs(self.rec, self.obs, self.device.time())      # 印の来ていない鎖の値も捨てない（R17）
        except Exception as e:                                     # noqa: BLE001
            self._note(e)
        try:
            self._keep_sync_counts()
        except Exception as e:                                     # noqa: BLE001
            self._note(e)

    def _keep_sync_counts(self) -> None:
        """ボードの間の時刻の同期の数（CSR sync.status）を meta に残す。CRC が合わず捨てた組はイベントにならず、
        ここにしか無い。pairs は 16 ビットで一周する数のまま（受けた組そのものは stream sync にある）。"""
        capabilities = getattr(self.device, "capabilities", None)
        if capabilities is None or not getattr(capabilities(), "sync", False):
            return
        with self._lock:
            status = self.device.read32(g.SYNC_STATUS)
        self.rec.update_meta(sync={"pairs_mod_65536": status & 0xFFFF, "crc_errors": status >> 16})

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
            # どの失敗でもスレッドは止めない（止まると「記録中」のまま何も入らない）
            if time.monotonic() - last_clock >= self.clock_s:
                with self._lock:
                    try:
                        sample_clock(self.device, self.rec)
                    except Exception as e:                         # noqa: BLE001  時計の点は次の周期でまた取る
                        self._note(e)
                last_clock = time.monotonic()
            with self._lock:
                try:
                    self._poll_events()
                except Exception as e:                             # noqa: BLE001  1 回の失敗で記録を黙って止めない
                    try:
                        self._recover(e)
                    except Exception as again:                     # noqa: BLE001  立て直しも失敗: 次の周期で
                        self._note(again)
            try:
                self.rec.flush()
            except Exception as e:                                 # noqa: BLE001
                self._note(e)

    def _note(self, error: Exception) -> None:
        self.errors += 1
        self.last_error = f"{type(error).__name__}: {error}"

    def _recover(self, error: Exception) -> None:
        """読み出しの失敗（ストリームが途中で止まった等）の後: 記録に印を残し、受け取り口に残った読みかけを捨て、
        今の総数から読み直す（読めなかった分は欠落として数える）。_lock の中で。"""
        self._note(error)
        try:
            self.rec.add("annotation", "events_error", self.device.time(), body={"error": self.last_error})
        except Exception:                                          # noqa: BLE001  印が付けられなくても読み直しは続ける
            pass
        drain = getattr(self.device, "read_stream_some", None)
        deadline = time.monotonic() + 1.0
        while drain is not None and time.monotonic() < deadline:
            try:
                drain(1 << 16, timeout_ms=50)
            except Exception:                                      # noqa: BLE001  何も届かない = 出し切った
                break
        try:
            written, _ = self.events.counts()
        except Exception:                                          # noqa: BLE001  数も読めない: 次の周期で読み直す
            return
        if written > self._seen:
            self.lost += written - self._seen
            self.obs.events_lost()
        self._seen, self._carry = written, None

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
            self.obs.events_lost()
            new = self.events.capacity
        batch = self.events.read(last=new, end=written)
        overwritten = getattr(self.events, "overwritten", 0)       # 読む間に書き手に追い越された先頭（読めなかった）
        if overwritten:
            self.lost += overwritten
            self.rec.add("annotation", "events_lost", self.device.time(), a=overwritten)
            self.obs.events_lost()
        self._carry = add_events(self.rec, batch, self.device.time(), carry=self._carry, obs=self.obs)
        self._seen = written


def _optional_event_log(device: Any) -> EventLog | None:
    try:
        return EventLog(device)
    except RuntimeError:                                           # イベント記録の無い構成
        return None
