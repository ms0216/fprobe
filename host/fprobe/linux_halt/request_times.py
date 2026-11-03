"""停止要求の書き込みの、FPGA が付けた時刻（イベント記録）。CTI の無い SoC で「こちらから全コアを止める」（`ArmACores.halt_all`
の順の停止）の時間差の上限を、PC の時計（USB / Ethernet の往復が入る）ではなく FPGA の時刻で出すために使う。
設計: docs/superpowers/specs/2026-11-01-stop-precision-design.md 3.3。式と各項の根拠（RTL の行）は docs/linux-halt.md。

FPGA はホストの DAP 転送を応答ごとに時刻つきで記録する（種類 1。DRW はホストが書いた CSW・TAR から求めたメモリの番地）。
各コアの DBGDRCR への停止要求の書き込みの、DP が受理した（ACK が OK の）転送の時刻を 1 件ずつ探し、最後 − 最初を返す。
  上限 = （最後の書き込みの時刻）−（最初の書き込みの時刻）+（書き込みごとの「対象に届く瞬間 → 時刻が付く瞬間」の差の違い）
最後の項は 0 周期: 届く瞬間（Update-DR の TCK の立ち下がり）から時刻が付くまでは、同じ設定（TCK の半周期・idle のクロック数・
TDO の取り込みの遅れ）の固定の手順で、待ちの入る調停が無い（docs/linux-halt.md に行番号）。各コアの中で要求を受けてから止まるまでの
遅れは含まない（コアごとに同じ、という仮定。使う側が判断できるよう、報告の根拠の文と StopReport.assumptions に書き、仮定を
使わない上限（PC の時計）を bound_without_assumptions_ns に並べる）。
DAP の時刻が線上の点の回路（ケイパビリティ dap_line_time。docs/superpowers/specs/2026-11-01-time-accuracy-design.md 段階 S2）
では、種類 1 の時刻はその書き込みの DR スキャンの Update-DR を出る TCK の立ち上がり（対象に届く点そのもの。端子の段
dap_output_ticks はどの書き込みにも同じだけ足されるので差に入らない）。最後の項は定義から 0 で、上の手順の議論は要らない。
差を取るだけなので、古い回路（完了 + 2 刻み）と新しい回路で式は同じ。

ここでは判定しない: 探した書き込みが無い・2 件ある・記録が欠けた・溢れた、はすべて理由つきの `NotTimed` にし、
使う側（`ArmACores`）がその理由を添えて PC の時計の上限に戻す。
"""
from __future__ import annotations

from dataclasses import dataclass

from fprobe.events import ACK_OK, KIND_HOST, KIND_HOST_UNTRACKED, DapEvent

__all__ = ["EventRequestTimes", "EventStopSpan", "NeedMoreEvents", "NotTimed", "RequestSpan", "event_stop_ticks",
           "write_span_ticks"]

MASK48 = (1 << 48) - 1
_DRW = 0xC


class NotTimed(Exception):
    """FPGA の時刻で上限を出せない（理由は str）。"""


@dataclass(frozen=True)
class RequestSpan:
    """最初の停止要求の書き込みから最後のものまで（FPGA のタイムベース）。"""
    ticks: int
    timebase_hz: int

    @property
    def ns(self) -> int:
        """ns（切り上げ。上限なので小さく丸めない）。"""
        return -(-self.ticks * 1_000_000_000 // self.timebase_hz)


def _drw_write(e: DapEvent) -> bool:
    return e.ap and not e.read and e.register == _DRW


def write_span_ticks(events: list[DapEvent], writes: list[tuple[int, int]]) -> int:
    """events（記録の順）の中の、writes の各 (番地, 値) の DRW の書き込みで DP が受理したもの（ACK が OK）を 1 件ずつ探し、
    最後 − 最初（刻み。48 ビットの時刻の折り返しをまたいでよい）。WAIT の転送は受理されていない（JTAG-DP は要求を捨てる）ので数えない。
    番地を追えなかった DRW の書き込み（種類 12）があれば、並びの順で当てはめず NotTimed。"""
    if not writes:
        raise NotTimed("no halt request was written")
    if any(e.kind == KIND_HOST_UNTRACKED and _drw_write(e) for e in events):
        raise NotTimed("the FPGA could not track the memory address of a DRW write (event kind 12): which core each "
                       "write went to is not recorded")
    found: dict[int, list[int]] = {}
    for i, e in enumerate(events):
        if e.kind == KIND_HOST and _drw_write(e) and e.ok:
            found.setdefault(e.address, []).append(i)
    picked = []
    for address, value in writes:
        hits = [i for i in found.get(address, []) if events[i].data == value & 0xFFFFFFFF]
        if len(hits) != 1:
            raise NotTimed("%d accepted writes of 0x%08X to 0x%08X were recorded (one expected)" % (len(hits), value, address))
        picked.append(hits[0])
    first = events[min(picked)].time
    return max((events[i].time - first) & MASK48 for i in picked)


class EventRequestTimes:
    """イベント記録（`fprobe.events.EventLog` の口: running / counts / read / overwritten / capacity）から、停止要求の書き込みの
    時刻を読む。log が None は「この構成にイベント記録が無い」。timebase_hz: タイムベースの周波数（ケイパビリティ。既定なし）。"""

    def __init__(self, log, *, timebase_hz: int, line_time: bool = False):
        """line_time: 回路が DAP の時刻を線上の点で付ける（ケイパビリティ dap_line_time）。コアが自分で止まった停止の下限の取り方が
        変わる（event_stop_ticks）。停止要求の書き込みの間の差（span）には関係しない。"""
        if not timebase_hz or timebase_hz <= 0:
            raise ValueError("timebase_hz must be the device's timebase frequency (capabilities): %r" % (timebase_hz,))
        self.log, self.timebase_hz, self.line_time = log, timebase_hz, line_time

    @classmethod
    def open(cls, device) -> "EventRequestTimes":
        """device のイベント記録を使う（記録していなければ始める。記録中なら続ける）。イベント記録の無い構成は log = None。"""
        from fprobe.events import EventLog
        caps = device.capabilities()
        try:
            log = EventLog(device)
        except RuntimeError:                                       # この構成にイベント記録が無い
            return cls(None, timebase_hz=caps.timebase_hz, line_time=bool(caps.dap_line_time))
        log.ensure_started()
        return cls(log, timebase_hz=caps.timebase_hz, line_time=bool(caps.dap_line_time))

    def mark(self):
        """書き込みの前に呼ぶ: 今の記録の位置（書いた総数と、記録できなかった数）。"""
        return None if self.log is None else self.log.counts()

    def span(self, mark, writes: list[tuple[int, int]]) -> RequestSpan:
        """mark の後に記録された writes の書き込みの、最初から最後まで。使えなければ NotTimed（理由つき）。"""
        if self.log is None or mark is None:
            raise NotTimed("this fprobe build has no event log")
        if not self.log.running():
            raise NotTimed("the event log was not recording")
        written0, lost0 = mark
        written, lost = self.log.counts()
        if written < written0:
            raise NotTimed("the event log was restarted during the stop")
        if lost != lost0:
            raise NotTimed("the event log lost %d events during the stop" % (lost - lost0))
        n = written - written0
        if n > self.log.capacity:
            raise NotTimed("the event ring overwrote %d events of the stop before they were read" % (n - self.log.capacity))
        events = self.log.read(n, end=written)
        if getattr(self.log, "overwritten", 0) or self.log.records != n:      # records = 返した記録の件数
            raise NotTimed("events of the stop were overwritten while they were read")
        return RequestSpan(write_span_ticks(events, writes), self.timebase_hz)

    FIRST_READ = 512                       # 末尾から最初に読む記録の数（足りなければ倍にして読み直す。結果には影響しない）

    def event_stop_span(self, mark, **kw) -> EventStopSpan:
        """コアが自分で止まった停止の上限（event_stop_ticks。kw はその引数から events・line_time・timebase_hz を除いたもの）。
        mark は再開の前の `mark()`: その後の記録の末尾を、要る転送が見つかるまで広げて読む。使えなければ NotTimed（理由つき）。"""
        if self.log is None or mark is None:
            raise NotTimed("this fprobe build has no event log" if self.log is None else
                           "the event log position was not taken at the resume")
        if not self.log.running():
            raise NotTimed("the event log was not recording")
        written0, lost0 = mark
        written, lost = self.log.counts()
        if written < written0:
            raise NotTimed("the event log was restarted since the resume")
        if lost != lost0:
            raise NotTimed("the event log lost %d events since the resume" % (lost - lost0))
        since = written - written0
        n = min(self.FIRST_READ, since)
        while True:
            limit = min(since, self.log.capacity)
            n = min(n, limit)
            events = self.log.read(n, end=written)
            if getattr(self.log, "overwritten", 0) or self.log.records != n:
                raise NotTimed("events of the stop were overwritten while they were read")
            try:
                return event_stop_ticks(events, line_time=self.line_time, timebase_hz=self.timebase_hz, **kw)
            except NeedMoreEvents as e:
                if n >= limit:
                    if since > self.log.capacity:
                        raise NotTimed("%s (%d events were written since the resume; the ring holds %d)"
                                       % (e, since, self.log.capacity)) from None
                    raise NotTimed("%s (all %d events since the resume were read)" % (e, since)) from None
                n *= 2


# --- コアが自分で止まった停止（ブレークポイント等）の上限を、FPGA の時刻で（2026-11-03、実機の評価の指摘）------------------
#
# ホストが待つ間に読んだ DSCR で止まったコア（hit）を見つけ、他のコア（requested）に停止要求を書いた停止。イベント記録の
# ホストの転送（種類 1）から、次の 2 つを事実として取る:
#   下限: どのコアも、この時刻より前には止まっていない = 各コアについて、境（hit: 止まったのを読んだ読み、requested: 停止要求の
#         書き込み）より前の「そのコアの DSCR の読みで HALTED = 0 だったもの」の読みを頼んだ転送か、「そのコアの再開の要求の
#         書き込み」の、いちばん後のもの（間の HALTED = 1 の読みは同じ停止を先に見たもの）。その最小。
#   上限: hit は止まったのを読んだ読みの値が返った転送より前に止まっている。requested は (仮定つき) 停止要求の書き込みが届いた
#         瞬間に止まる / (仮定なし) 書き込みの後で HALTED = 1 を読んだ読みの値が返った転送より前に止まっている。
# 転送の時刻の意味（回路）: 線上の点の回路（dap_line_time）は、読みを頼んだ転送の点で AP の読みが始まり、書き込みの点で対象に
# 届く（docs/superpowers/specs/2026-11-01-time-accuracy-design.md）ので、点をそのまま下限に使う。古い回路の時刻は転送の完了 + 2
# （ACK・データ相の後）で下限にならないので、その転送の前に線上で終わった転送の時刻を下限に使う（fprobe.timing.posted_read_window
# と同じ規則）。値が返った転送の時刻は、どちらの回路でも、対象が読んだ瞬間より後（上限）。

class NeedMoreEvents(NotTimed):
    """読んだ記録の範囲に、要る転送がまだ無い（もっと前から読む）。"""


@dataclass(frozen=True)
class EventStopSpan:
    """コアが自分で止まった停止の、FPGA の時刻での上限（刻み）。"""
    assumed_ticks: int                 # 下限 → 最後の停止要求の書き込み（requested は書き込みが届いた瞬間に止まる、の仮定）
    measured_ticks: int | None         # 下限 → 各コアが止まっているのを読んだ読みの値が返った転送（仮定なし。読みが無ければ None）
    timebase_hz: int
    lower_core: int                    # 下限を決めたコア
    lower_from: str                    # "read"（HALTED = 0 を読んだ読み）/ "restart"（再開の要求の書き込み）

    @staticmethod
    def _ns(ticks: int, hz: int) -> int:
        return -(-ticks * 1_000_000_000 // hz)

    @property
    def assumed_ns(self) -> int:
        return self._ns(self.assumed_ticks, self.timebase_hz)

    @property
    def measured_ns(self) -> int | None:
        return None if self.measured_ticks is None else self._ns(self.measured_ticks, self.timebase_hz)


def _transfers(events: list[DapEvent]):
    """ホストの転送を、読み（値を返した転送と対応付けたもの）と書き込みの並びにする。
    -> [("r"|"w", 番地, 値, 頼んだ転送の位置, 値を返した転送の位置 or 書き込みの位置)]。失敗した転送でポストの連鎖は途切れる。"""
    out, pending = [], None
    for i, e in enumerate(events):
        if e.kind == KIND_HOST_UNTRACKED and e.ap and e.register == _DRW:
            raise NotTimed("the FPGA could not track the memory address of a DRW transfer (event kind 12): which core "
                           "it went to is not recorded")
        if e.kind != KIND_HOST:
            continue
        if not e.ok:
            pending = None
            continue
        if e.ap and e.read:
            if pending is not None:
                out.append(("r", events[pending].address, e.data, pending, i))
            pending = i if e.register == _DRW else None
        elif not e.ap and e.read and e.register == 0xC:                # DP RDBUFF: ポストされた読みの値
            if pending is not None:
                out.append(("r", events[pending].address, e.data, pending, i))
                pending = None
        elif e.ap and not e.read and e.register == _DRW:
            out.append(("w", e.address, e.data, i, i))
    return out


def event_stop_ticks(events: list[DapEvent], *, hits: list[int], requested: list[int], dscr: dict[int, int],
                     drcr: dict[int, int], hrq: int, rrq: int, halted: int, line_time: bool, timebase_hz: int
                     ) -> EventStopSpan:
    """コアが自分で止まった停止の上限（上の説明）。events は記録の順（停止要求の書き込みと、その後の読みまでを含む末尾）。
    要る転送が範囲に無ければ NeedMoreEvents（呼び出し側がもっと前から読む）、使えなければ NotTimed（理由つき）。"""
    t = _transfers(events)

    def lower_at(i: int) -> int:
        if line_time:
            return events[i].time
        before = [j for j in range(i - 1, -1, -1) if events[j].kind in (KIND_HOST, KIND_HOST_UNTRACKED)]
        if not before:
            raise NeedMoreEvents("the transfer before position %d is not in the events read" % i)
        return events[before[0]].time

    writes = {}
    for c in requested:
        ws = [x for x in t if x[0] == "w" and x[1] == drcr[c] and x[2] == hrq]
        if not ws:
            raise NeedMoreEvents("no accepted halt-request write to core %d (0x%08X) in the events read" % (c, drcr[c]))
        writes[c] = ws[-1]
    first_write = min((w[3] for w in writes.values()), default=len(events))
    boundary, ends_assumed, ends_measured = {}, [], []
    for h in hits:
        ds = [x for x in t if x[0] == "r" and x[1] == dscr[h] and x[2] & halted and x[3] < first_write]
        if not ds:
            raise NeedMoreEvents("no read of core %d's DBGDSCR with HALTED = 1 before the halt requests" % h)
        boundary[h] = ds[-1][3]
        ends_assumed.append(events[ds[-1][4]].time)
        ends_measured.append(events[ds[-1][4]].time)
    for c, w in writes.items():
        boundary[c] = w[3]
        ends_assumed.append(events[w[3]].time)
        seen = [x for x in t if x[0] == "r" and x[1] == dscr[c] and x[2] & halted and x[3] > w[3]]
        ends_measured.append(events[seen[0][4]].time if seen else None)
    lowers = []
    for c, b in boundary.items():
        # そのコアが止まっていなかった証拠（HALTED = 0 の読み・再開の要求の書き込み）の、境より前でいちばん後のもの。間の
        # HALTED = 1 の読みは同じ停止を先に見たもの（コアは再開まで止まったまま）なので飛ばす
        before = [x for x in t if x[3] < b and ((x[0] == "r" and x[1] == dscr[c] and not x[2] & halted)
                                                or (x[0] == "w" and x[1] == drcr[c] and x[2] & rrq))]
        if not before:
            raise NeedMoreEvents("no earlier read of core %d's DBGDSCR with HALTED = 0 nor restart request in the "
                                 "events read" % c)
        last = before[-1]
        lowers.append((lower_at(last[3]), c, "read" if last[0] == "r" else "restart"))
    lo, core, src = min(lowers, key=lambda x: x[0])
    assumed = max((e - lo) & MASK48 for e in ends_assumed)
    measured = None if any(e is None for e in ends_measured) else max((e - lo) & MASK48 for e in ends_measured)
    return EventStopSpan(assumed, measured, timebase_hz, core, src)
