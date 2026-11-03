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
from .events import (ACK_OK, KIND_DAP_CONFIG, KIND_LA_GAP, KIND_OBS_RECORD, KIND_OBS_SYNC, KIND_SAMPLE, SAMPLER_KINDS,
                     WIRE_KINDS, EventLog, DapEvent, resolve)
from .timing import DapConfig
from .fpga_obs import ObsDecoder
from .la import Transitions
from .recording import Recorder

__all__ = ["Carry", "Collector", "add_capture", "add_events", "sample_clock", "unwrap48", "unwrap_events",
           "unwrap_obs_sample"]

MASK48 = (1 << 48) - 1
_STREAMS = {"ap_read": "dap", "ap_write": "dap", "drw_read": "dap", "drw_write": "dap", "inject": "dap", "dp_read": "dap",
            "dp_write": "dap", "fail": "dap",
            "uart_rx": "uart", "uart_tx": "uart", "swo": "swo", "aux": "aux", "sample": "sample",
            "sample_error": "sample", "dap_config": "dap", "sync_pair": "sync"}
_KINDS = {"uart_rx": "rx", "uart_tx": "tx", "swo": "byte", "aux": "mv", "sample": "word", "sample_error": "error",
          "dap_config": "config", "sync_pair": "pair"}


@dataclasses.dataclass(frozen=True)
class Carry:
    """かたまりをまたいで持ち越すもの: 値がまだ返っていない AP の読み（read）と、いま効いている PHY の設定（config。
    DAP の時刻が線上の点の回路の、イベント 16。範囲の式に使う）と、窓の後端（次のサンプラの件）がまだ来ていない最後の
    サンプラの語（sample。線上の点の回路だけ。次のかたまりで後端を決めてから書く。記録の終わりは flush_carry）。"""
    read: DapEvent | None = None
    config: DapConfig | None = None
    sample: DapEvent | None = None


def unwrap48(t48: int, reference: int) -> int:
    """48 ビットの時刻を、それより後に読んだタイムベース reference の上位で 64 ビットにする。"""
    return reference - ((reference - t48) & MASK48)


def unwrap_obs_sample(s, reference: int):
    """復号器の値（ObsSample。時刻はイベントの 48 ビットの時刻から伸ばした刻み）の時刻（time_fprobe・time_lo・time_hi）を、
    それより後に読んだタイムベース reference の上位で 64 ビットにした写し（_obs_rows と同じ: 丸めた時刻の下位 48 ビットから）。
    時刻の無い値はそのまま。"""
    if s.time_fprobe is None:
        return s
    rt = round(s.time_fprobe)
    shift = unwrap48(rt & MASK48, reference) - rt
    lo, hi = getattr(s, "time_lo", None), getattr(s, "time_hi", None)
    return dataclasses.replace(s, time_fprobe=s.time_fprobe + shift, time_lo=None if lo is None else lo + shift,
                               time_hi=None if hi is None else hi + shift)


def unwrap_events(events: list[DapEvent], reference: int) -> list[DapEvent]:
    """イベントの 48 ビットの時刻を、それより後に読んだタイムベース reference で 64 ビットにしたイベントの並び
    （48 ビットの折り返しをまたいでも時刻が戻らない）。"""
    return [dataclasses.replace(e, time=unwrap48(e.time, reference)) for e in events]


def add_capture(rec: Recorder, tr: Transitions) -> None:
    """捕捉を、トリガのサンプルがトリガ時刻（タイムベース）に来るように置く。"""
    ticks_per_sample = tr.sample_period_s * rec.timebase_hz
    rec.add_waveform(tr, start_ticks=tr.trigger_time - round(tr.trigger * ticks_per_sample))


def add_events(rec: Recorder, events: list[DapEvent], reference: int, carry: "Carry | DapEvent | None" = None,
               obs: ObsDecoder | None = None, line_time: bool = False,
               output_ticks: int | None = None, lost_before: bool = False) -> "Carry | None":
    """イベントのかたまりを記録へ。carry は前のかたまりから持ち越すもの（Carry。古い呼び手の DapEvent は値がまだ返って
    いない AP の読み）。次へ持ち越すものを返す（無ければ None）。line_time = 回路が DAP_LINE_TIME を申告する（ポストされた読み・
    サンプラの語の範囲を PHY の設定から出す。fprobe.events.resolve）。
    FPGA の観測（種類 8・9）は obs の復号器で値の並び（stream fpga: value・gap・group）にする（無ければ捨てる）。
    サンプラの語（stream sample・word）の本体は、line_time なら {t_lo, t_hi, waits}（対象が読んだ瞬間の範囲。t_hi はこの
    かたまりの中に次のサンプラの件が無ければ null）。誤りの区間は stream sample・error（a = 最初の番号、b = 読みの数）、
    PHY の設定は stream dap・config（b = half_period、本体 = 設定）。
    線上の点の回路では、かたまりの最後の語は窓の後端が次のかたまりにあるので書かずに持ち越す（Carry.sample。記録の
    終わりは flush_carry が t_hi null で書く）。lost_before = このかたまりの前に読めなかった件がある（リングが一周した・
    追い越された）: 持ち越した語の t_hi はこのかたまりの最初のサンプラの件で、間の件が欠けているので確かな上限のままだが
    値を返した転送の点とは限らない（本体に t_hi_after_loss: true）。"""
    if isinstance(carry, DapEvent):
        carry = Carry(read=carry)
    observed = [e for e in events if e.kind in (KIND_OBS_RECORD, KIND_OBS_SYNC)]
    # ロジアナの欠けの記録（種類 19）は、ロジアナの流れ（LaStream）が欠けの区間にして記録へ入れる（recording_stream）。
    # イベントの記録の読みにも同じ件が載るが、ここでは二重に入れない
    events = [e for e in events if e.kind not in (KIND_OBS_RECORD, KIND_OBS_SYNC, KIND_LA_GAP)]
    held = [e for e in (carry.read, carry.sample) if e is not None] if carry is not None else []
    held.sort(key=lambda e: (reference - e.time) & MASK48, reverse=True)      # 持ち越した 2 つは起きた順に
    batch = held + events
    config = carry.config if carry is not None else None
    # 線上の点の回路: 並びの最後のサンプラの件が語なら、その窓の後端は次のかたまりにある。書かずに持ち越す
    sampler = [e for e in batch if e.kind in SAMPLER_KINDS]
    hold = sampler[-1] if line_time and sampler and sampler[-1].kind == KIND_SAMPLE else None
    kept: DapEvent | None = None
    rows = []
    if obs is not None and observed:
        obs.reference = reference                                  # recent を 64 ビットの時刻にする基準（ObsDecoder.reference）
        rows += _obs_rows(obs.feed(observed), reference)

    def u(t: int | None) -> int | None:
        return None if t is None else unwrap48(t, reference)

    for a in resolve(batch, line_time=line_time, config=config, output_ticks=output_ticks):
        stream = _STREAMS[a.kind]
        kind = _KINDS.get(a.kind, a.kind)
        if a.kind == "dap_config":
            rows.append((stream, kind, u(a.time), 0, a.value, dict(a.detail)))
        elif a.kind == "sample_error":
            rows.append((stream, kind, u(a.time), a.address, a.value,
                         {"t_lo": u(a.time_lo), "t_hi": u(a.time_hi), "ack": a.detail["ack"],
                          "sticky": a.detail["sticky"], "dropped": a.detail["dropped"]}))
        elif a.kind == "sample" and line_time and a.ack == ACK_OK:
            if hold is not None and a.time_hi is None and a.time == hold.time and a.address == hold.address:
                kept = hold                                          # 持ち越す（次のかたまりで後端を決めて書く）
                continue
            body = {"t_lo": u(a.time_lo), "t_hi": u(a.time_hi), "waits": (a.detail or {}).get("waits")}
            held_sample = carry.sample if isinstance(carry, Carry) else None
            if lost_before and held_sample is not None and (a.time, a.address) == (held_sample.time, held_sample.address):
                body["t_hi_after_loss"] = True
            rows.append((stream, kind, u(a.time), a.address, a.value, body))
        elif kind == "inject":                                        # 割り込み書き込み: 転送数（旧い回路は null）
            rows.append((stream, kind, unwrap48(a.time, reference), a.address, a.value,
                         {"ack": a.ack, "transfers": a.transfers}))
        elif stream == "dap" and a.requested is not None and line_time:   # 線上の点の回路: 範囲は [t_lo, t_hi]（端子）
            rows.append((stream, kind, unwrap48(a.time, reference), a.address, a.value,
                         {"ack": a.ack, "requested": unwrap48(a.requested, reference), "t_lo": u(a.time_lo),
                          "t_hi": u(a.time_hi)}))
        elif stream == "dap" and a.requested is not None:           # ポストされた読み: 対象が読んだ瞬間の範囲は
            # [t_lo, t]（t = 値を返した転送。fprobe.timing.posted_read_window）。requested = 読みを頼んだ転送
            rows.append((stream, kind, unwrap48(a.time, reference), a.address, a.value,
                         {"ack": a.ack, "requested": unwrap48(a.requested, reference),
                          "t_lo": None if a.time_lo is None else unwrap48(a.time_lo, reference)}))
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
    for e in events:
        if e.kind == KIND_DAP_CONFIG:
            config = e.config
    read = _pending_read(batch)
    if read is None and config is None and kept is None:
        return None
    return Carry(read=read, config=config, sample=kept)


def flush_carry(rec: Recorder, carry: "Carry | DapEvent | None", reference: int, line_time: bool = False,
                output_ticks: int | None = None) -> None:
    """記録の終わり・読み直しの前: 持ち越した最後のサンプラの語を、窓の後端が分からない（t_hi null）まま書く（捨てない）。
    設定は持ち越しの config（その語の前から効いていたもの）。"""
    if not isinstance(carry, Carry) or carry.sample is None:
        return
    for a in resolve([carry.sample], line_time=line_time, config=carry.config, output_ticks=output_ticks):
        if a.kind == "sample":
            u = (lambda t: None if t is None else unwrap48(t, reference))
            rec.add_many([("sample", "word", u(a.time), a.address, a.value,
                           {"t_lo": u(a.time_lo), "t_hi": None, "waits": (a.detail or {}).get("waits")})])


def _obs_rows(samples, reference: int) -> list[tuple]:
    """FPGA の観測の値の並び → stream fpga の行。印が来ないまま鎖が終わって時刻が決まらないものは捨てず、別の種類
    （value_untimed・gap_untimed・group_untimed）で、時刻を「受けた時」（起きた時刻の上限。起きたのはそれより前）にして
    入れる（R17。時刻の決まった value・gap・group と混ぜない: 状態の段・VCD・相関はそれらだけを使う）。
    時刻の決まった行の不確かさ（±）は、復号器が返した範囲 [time_lo, time_hi]（隣り合う印の窓を通る直線の範囲。段階 S3）を
    丸めた時刻から覆う値。範囲の無いもの（標本型の標本: 時刻は Capture-DR の端子の辺）は None。"""
    rows = []
    for s in samples:
        untimed = s.time_fprobe is None
        if untimed and s.received is None:
            continue
        rt = s.received if untimed else round(s.time_fprobe)
        t = unwrap48(rt & MASK48, reference)
        lo, hi = getattr(s, "time_lo", None), getattr(s, "time_hi", None)
        unc = None if untimed or lo is None else max(rt - lo, hi - rt)
        suffix = "_untimed" if untimed else ""
        if s.kind == "value":
            rows.append(("fpga", "value" + suffix, t, s.group, s.value, None, None, unc))
        elif s.kind in ("gap", "hidden"):
            rows.append(("fpga", s.kind + suffix, t, s.lost, 0, None, None, unc))
        else:
            rows.append(("fpga", "group" + suffix, t, s.group, 0, None, None, unc))
    return rows


def flush_obs(rec: Recorder, obs: ObsDecoder, reference: int) -> None:
    """記録を止めるとき: 復号器に残っている値（印の来ていない鎖）を出し切って記録へ。"""
    obs.reference = reference
    rows = _obs_rows(obs.flush(), reference)
    if rows:
        rec.add_many(rows)


def _pending_read(events: list[DapEvent]) -> DapEvent | None:
    """値がまだ返っていない（後に AP の読み・RDBUFF・失敗が来ていない）最後の AP の読み。次のかたまりで範囲の下限が
    分かるように、その前に線上で終わった転送の時刻を preceded_by に付ける（fprobe.events.resolve）。"""
    pending, last_wire = None, None
    for e in events:
        wire_before = last_wire
        if e.kind in WIRE_KINDS:
            last_wire = e.time
        if e.kind != 1:                                           # ホストの転送だけがポストの連鎖を作る
            continue
        if not e.ok:
            pending = None
        elif e.ap and e.read:
            pending = e if e.preceded_by is not None or wire_before is None else dataclasses.replace(
                e, preceded_by=wire_before)
        elif not e.ap and e.read and e.register == 0xC:
            pending = None
    return pending


def sample_clock(device: Any, rec: Recorder) -> bool:
    """時計の対応付けの点を 1 つ取り、記録にも残す（clock ストリーム: a = タイムベース、本体に往復時間）。タイムベースが
    前の読みより小さければ（0 に戻った。time-accuracy §14.1）記録は終わり、点は入れずに False。"""
    before = time.monotonic()
    ticks = device.time()
    after = time.monotonic()
    if not observe_reference(rec, ticks, (before + after) / 2):
        return False
    rec.clock.add((before + after) / 2, ticks, after - before)
    rec.add("clock", "point", ticks, body={"host_s": (before + after) / 2, "round_trip_s": after - before})
    return True


def observe_reference(rec: Any, ticks: int, host_s: float | None = None) -> bool:
    """記録に時刻の基準として読んだタイムベースを渡す（observe_time の無い書き手は確かめない）。"""
    observe = getattr(rec, "observe_time", None)
    return True if observe is None else observe(ticks, host_s)


RECENT_OBS = 20_000                        # 復号器が覚えておく最近の値（fpga_obs_read の窓の上限）
# 復号器が時刻を決める印を待って持つ値の数の上限（ObsDecoder.max_pending）。根拠: 記録しながらの取り込みは止まらずに続く（日単位）
# ので、印の来ない鎖が続いても PC のメモリを使い尽くさない上限が要る。1 件は約 200 バイト（タプルと値）で、上限で約 200 MB。
# 超えた値は時刻を決めずに出す（time_basis = "hold_limit"。値は正しい。何件かは untimed_by_limit の事実）
OBS_PENDING_MAX = 1 << 20


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
        self._carry: Carry | None = None
        self.obs = ObsDecoder(recent=RECENT_OBS, max_pending=OBS_PENDING_MAX)   # FPGA の観測の復号器（流し続けの間は流れと共有する）
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread: threading.Thread | None = None
        self.lost = 0
        self.restarts = 0                          # イベント記録が始め直された（数が戻った）回数
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
            self._flush_carry()                                    # 持ち越した最後のサンプラの語（後端は分からない）
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
        if not self.device.known_capabilities().sync:
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

    def take_back(self, seen: int | None, carry: "Carry | DapEvent | None") -> None:
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
        try:
            self._flush_carry()                                    # 持ち越した語は書いてから捨てる（後端は分からない）
        except Exception:                                          # noqa: BLE001
            pass
        self._seen, self._carry = written, None

    def _flush_carry(self) -> None:
        if self.events is None:
            return
        flush_carry(self.rec, self._carry, self.device.time(), line_time=getattr(self.events, "line_time", False),
                    output_ticks=getattr(self.events, "output_ticks", None))
        if isinstance(self._carry, Carry):
            self._carry = dataclasses.replace(self._carry, sample=None)

    def _poll_events(self) -> None:
        if self.events is None or self._paused.is_set():
            return
        self._read_new()

    def _read_new(self) -> None:
        if getattr(self.rec, "ended", None) is not None:           # 記録は終わった（タイムベースが 0 に戻った等）
            return
        written, _ = self.events.counts()
        if written < self._seen:
            # イベント記録が始め直された（数が戻った）事実: 黙って待たず、数えて 0 から読む（EventCursor と同じ）
            self.restarts += 1
            self.rec.add("annotation", "events_restarted", self.device.time(), body={"written": written, "seen": self._seen})
            self._seen = 0
        new = written - self._seen
        if new <= 0:
            return
        lost_before = new > self.events.capacity
        if new > self.events.capacity:                             # 読む前にリングが一周した
            self.lost += new - self.events.capacity
            self.rec.add("annotation", "events_lost", self.device.time(), a=new - self.events.capacity)
            self.obs.events_lost()
            new = self.events.capacity
        batch = self.events.read(last=new, end=written)
        overwritten = getattr(self.events, "overwritten", 0)       # 読む間に書き手に追い越された先頭（読めなかった）
        lost_before = lost_before or bool(overwritten)
        if overwritten:
            self.lost += overwritten
            self.rec.add("annotation", "events_lost", self.device.time(), a=overwritten)
            self.obs.events_lost()
        reference = self.device.time()
        if not observe_reference(self.rec, reference):                      # 0 に戻った: このかたまりは前の時代と混ぜない
            return
        self._carry = add_events(self.rec, batch, reference, carry=self._carry, obs=self.obs,
                                 line_time=getattr(self.events, "line_time", False),
                                 output_ticks=getattr(self.events, "output_ticks", None), lost_before=lost_before)
        self._seen = written


def _optional_event_log(device: Any) -> EventLog | None:
    try:
        return EventLog(device)
    except RuntimeError:                                           # イベント記録の無い構成
        return None
