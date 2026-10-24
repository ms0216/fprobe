"""Probe の道具の共通の部品: DAP サンプラの持ち主と、デバイスの時刻付きの出来事の「窓」。

Probe（fprobe.mcp_server）に混ぜて使う mixin。Probe の _lock・session・_recorder・_collector・_view を使う。

- DAP サンプラ（F4）は機器に 1 つ。使う道具（watch・RTOS の時間軸の方法 A・プロファイルの pcsr）は
  `_claim_sampler` で持ち主になり、終えたら `_release_sampler` で返す。ほかの道具が使っている間は、設定を
  黙って上書きせず誤りにする（観測していたものが知らないうちに消えないように）。
- 窓（`EventWindow`）: 始めた時刻から後の、デバイスの時刻付きの出来事（サンプラの語・SWO のバイト）。
  記録中に始めた窓は共通の時間軸の記録（stream "sample" / "swo"）から、記録していなければイベント記録（リング）
  から直接読む。どちらもデバイスのタイムベースの時刻なので、結果は記録の時刻とそのまま比べられる。
  サンプラが読めなかった語（ACK が OK でない）は使わない。
- `write_registers`: 設定値の一覧（fprobe.profile.dwt.RegisterWrite。mask が全ビットでなければ読み・変え・書き）を
  ターゲットに書き、書く前の値を返す（道具を止めるときに戻すため）。
"""

import time
from dataclasses import dataclass, field

from .collector import unwrap48
from .events import ACK_OK, EventLog, resolve
from .profile.dwt import ITM_LAR

__all__ = ["EventWindow", "WINDOW_EVENT_LIMIT", "WindowCursor", "WindowTools", "write_registers"]

WINDOW_EVENT_LIMIT = 1 << 18           # 記録していないとき、窓に読むイベント記録の件数の上限（16 バイト × 26 万 = 4 MB）
_RELEASE = {"watch": "watch.stop", "rtos": "rtos.timeline.stop", "profile": "profile.stop", "rtt": "rtt.stop"}
_PURPOSE = {"watch": "変数のライブ観測", "rtos": "タスクの時間軸", "profile": "PC のサンプリング", "rtt": "RTT の見張り",
            "clock": "CPU クロックの実測"}
_RECORDED_KINDS = {"sample": "word", "swo": "byte", "uart": "rx"}          # 記録の stream → kind
_RING_KINDS = {"sample": "sample", "swo": "swo", "uart": "uart_rx"}         # イベント記録の種類
_WRITE_ONLY = {ITM_LAR}


def write_registers(target, writes) -> dict[int, int]:
    """writes（RegisterWrite の列）を順に書く。番地ごとに、最初に書く前の値を返す（全ビットを書くものも読む）。
    書くだけのレジスタ（ITM_LAR）は読まない。"""
    before: dict[int, int] = {}
    for w in writes:
        full = w.mask == 0xFFFFFFFF
        if w.address not in before and not (full and w.address in _WRITE_ONLY):
            before[w.address] = target.read_memory(w.address)
        value = w.value if full else (target.read_memory(w.address) & ~w.mask) | (w.value & w.mask)
        target.write_memory(w.address, value & 0xFFFFFFFF)
    return before


@dataclass
class WindowCursor:
    """窓を少しずつ読み進める位置（背景のスレッドが周期的に新しい出来事だけを受け取る。長時間でも取りこぼさない）。
    記録からは前回の最後の時刻から後を、イベント記録からは前回までに読んだ数より後を読み、同じ時刻の出来事は
    渡した数で見分ける。"""
    window: "EventWindow"
    stream: str                        # sample / swo / uart
    last_t: int | None = None
    at_last: int = 0
    ring_seen: int = 0


@dataclass
class EventWindow:
    t0: int                            # 始めた時刻（タイムベース）
    recorded: bool                     # 記録中に始めた（記録から読む）
    seen: int = 0                      # 記録していないとき: 始めた時点のイベント記録の総数
    t1: int | None = None              # 閉じた時刻（開いている間は None）
    lost: int = 0                      # 読めなかった出来事の数（リングが一周した・上限を超えた）
    notes: list[str] = field(default_factory=list)

    def span(self) -> int | None:
        return None if self.t1 is None else self.t1 - self.t0


class WindowTools:
    def _init_window(self) -> None:
        self._sampler_user: str | None = None
        self._sampler_since: float | None = None
        self._sampler_client: dict | None = None

    # --- DAP サンプラの持ち主 -------------------------------------------------------------------

    def _claim_sampler(self, user: str, caller: dict | None = None) -> None:
        """DAP サンプラを user（watch / rtos / profile）が使う。ほかが使っていれば RuntimeError。caller（デーモンが渡す
        呼び出し元 {id, name, role}。デーモンを通さない呼び出しは None）は、使い始めたとき（since と組で）だけ覚える。"""
        holder = self._sampler_user
        if holder is not None and holder != user:
            release = _RELEASE.get(holder)
            how = f"（{release.replace('.', '_')} を先に）" if release else "（終わるのを待つ）"
            raise RuntimeError(f"DAP サンプラ（変数を止めずに読む回路。1 つだけ）は {holder}（{_PURPOSE.get(holder, holder)}）"
                               f"が使っている{how}")
        if self._sampler_user != user:
            self._sampler_since = time.time()
            self._sampler_client = dict(caller) if caller is not None else None
        self._sampler_user = user

    def _release_sampler(self, user: str) -> None:
        if self._sampler_user == user:
            self._sampler_user = None
            self._sampler_since = None
            self._sampler_client = None

    def sampler_status(self) -> dict:
        """DAP サンプラ（1 つ）をいま使っている道具（機器には触れない）: user（watch / rtos / profile / rtt / clock、
        空きなら None）、release（止める API。clock は自分で終わるので None）、since（使い始めた時刻、UNIX 秒）、
        client（使い始めた呼び出し元 {id, name, role}。デーモンが渡した事実。分からなければ None）。"""
        user = self._sampler_user
        return {"user": user, "release": _RELEASE.get(user) if user else None,
                "since": self._sampler_since if user else None,
                "client": self._sampler_client if user else None}

    # --- 出来事の窓 ---------------------------------------------------------------------------

    def _open_window(self) -> EventWindow:
        """いまから後の出来事を読む窓を開く（_lock の中で）。記録していなければイベント記録を動かす。"""
        device = self.session.device
        if self._recorder is not None:
            return EventWindow(device.time(), True)
        log = EventLog(device)                                    # イベント記録の無い構成なら RuntimeError
        log.ensure_started()
        return EventWindow(device.time(), False, log.counts()[0])

    def _close_window(self, window: EventWindow) -> None:
        """窓を閉じる（_lock の中で）。以後は閉じた時刻までを読む。"""
        if window.t1 is None:
            window.t1 = self.session.device.time()

    def _window_events(self, window: EventWindow, stream: str) -> list[tuple[int, int, int]]:
        """窓の中の出来事 (時刻, a, b): stream "sample" は (時刻, 番地, 値)、"swo" / "uart"（受けたバイト）は
        (時刻, 0, バイト)。
        _lock の外で呼ぶ（記録から読むときは、集める側がロックを取るため）。"""
        if window.recorded:
            return self._recorded_events(window, stream)
        with self._lock:
            return self._ring_events(window, stream)

    def _recorded_events(self, window: EventWindow, stream: str) -> list[tuple[int, int, int]]:
        collector = self._collector
        if collector is not None:
            collector.poll()                                      # 次の周期を待たずに、届いている分を記録へ
        view = self._view
        if view is None:
            raise RuntimeError("窓を開いた記録が読めない")
        t1 = window.t1
        if t1 is None:
            with self._lock:
                t1 = self.session.device.time()
        kinds = [_RECORDED_KINDS[stream]]
        return [(e.t, e.a, e.b) for e in view.events(window.t0, t1, streams=[stream], kinds=kinds)
                if e.body is None]                                # body に ACK があるのは読めなかった語

    def _ring_events(self, window: EventWindow, stream: str) -> list[tuple[int, int, int]]:
        device = self.session.device
        log = EventLog(device)
        written, _ = log.counts()
        if written < window.seen:                                 # 記録の開始等でリングが始め直された
            window.notes.append("イベント記録が途中で始め直された（記録の開始等）: それより前の出来事は読めない")
            window.seen = 0
        new = written - window.seen
        n = min(new, log.capacity, WINDOW_EVENT_LIMIT)
        if new > n:
            window.lost = max(window.lost, new - n)
        events = log.read(n, end=written) if n else []
        now = device.time()
        t1 = window.t1 if window.t1 is not None else now
        kind = _RING_KINDS[stream]
        out = []
        for a in resolve(events):
            if a.kind != kind or (kind == "sample" and a.ack != ACK_OK):      # 読めなかった語は使わない
                continue
            t = unwrap48(a.time, now)
            if window.t0 <= t < t1:
                out.append((t, a.address, a.value))
        return out

    def _window_cursor(self, window: EventWindow, stream: str) -> WindowCursor:
        return WindowCursor(window, stream, ring_seen=0 if window.recorded else window.seen)

    def _window_new(self, cursor: WindowCursor) -> list[tuple[int, int, int]]:
        """窓の出来事のうち、前回の呼び出しの後に届いたもの（_lock の外で。呼ぶのは 1 つのスレッドから）。"""
        window = cursor.window
        if window.recorded:
            part = EventWindow(cursor.last_t, True, t1=window.t1) if cursor.last_t is not None else window
            events = self._window_events(part, cursor.stream)
        else:
            with self._lock:
                written = EventLog(self.session.device).counts()[0]
            part = EventWindow(window.t0, False, cursor.ring_seen, window.t1)
            events = self._window_events(part, cursor.stream)
            cursor.ring_seen = written
            window.lost += part.lost
            window.notes += [n for n in part.notes if n not in window.notes]
        fresh, same = [], 0
        for e in events:
            t = e[0]
            if cursor.last_t is not None and t < cursor.last_t:
                continue
            if cursor.last_t is not None and t == cursor.last_t:
                same += 1
                if same <= cursor.at_last:
                    continue
            fresh.append(e)
        if fresh:
            last = fresh[-1][0]
            cursor.at_last = sum(1 for e in fresh if e[0] == last) + (cursor.at_last if last == cursor.last_t else 0)
            cursor.last_t = last
        return fresh

    def _window_info(self, window: EventWindow, hz: float) -> dict:
        """窓の範囲・出どころ・失った出来事（結果に付ける）。"""
        out = {"t0": window.t0, "t1": window.t1, "source": "recording" if window.recorded else "event_log",
               "timebase_hz": hz, "lost_events": window.lost}
        if window.t1 is not None:
            out["duration_s"] = (window.t1 - window.t0) / hz
        notes = list(window.notes)
        if window.lost:
            notes.append(f"イベント記録の {window.lost} 件を読めなかった（リングが一周した・上限）: 窓の始めの方が欠けている。"
                         "長い時間は recording_start で記録しながら使う")
        out["notes"] = notes
        return out
