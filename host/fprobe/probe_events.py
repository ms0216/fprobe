"""DAP のイベント記録（events_start / events_stop / events_read）の mixin。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う。Probe の _lock・session と _la_idle（ロジアナの捕捉を止める）を使う。
"""

from .collector import unwrap_events
from .events import EventLog, INJECT_KINDS, resolve


class EventTools:
    """Probe に混ぜる DAP のイベント記録の道具。"""

    def events_start(self) -> dict:
        """DAP 転送のイベント記録を始める（外部メモリのリング。古いものから上書き）。"""
        with self._lock:
            self._la_idle()
            log = EventLog(self.session.device)
            log.start()
            return {"capacity": log.capacity}

    def events_stop(self) -> dict:
        """イベント記録を止める（記録は残り、events_read で読める）。"""
        with self._lock:
            self._la_idle()
            log = EventLog(self.session.device)
            log.stop()
            written, lost = log.counts()
            return {"written": written, "lost": lost}

    def events_read(self, last: int = 200, raw: bool = False, since: int | None = None) -> dict:
        """イベントを古い順に返す。since が無ければ最新の last 件。since（前回の結果の next）があれば、その番号から
        last 件まで（続けて読めば取りこぼさない。読む前に上書きされた数は skipped）。first = 返した最初の番号、
        next = 次に since に渡す番号（番号は記録を始めてから書いた数で数える）。raw = False ならアクセス（ポストされた
        読みを対応付けた後。クロストリガの割り込み書き込みは kind "inject" と、停止の書き込みまでの転送数 transfers（旧い回路は
        null））、True なら線上の転送そのもの。time_us は記録開始とは無関係なタイムベースの時刻（µs）。"""
        with self._lock:
            written, lost, first, events, extra = self._read_events(last, since)
            hz = self.session.device.capabilities().timebase_hz
            events = unwrap_events(events, self.session.device.time())     # 48 ビットの時刻を 64 ビットに（折り返しをまたいでも戻らない）
            if raw:
                items = [{"time_us": e.time * 1e6 / hz, "kind": e.kind, "ap": e.ap, "read": e.read,
                          "register": e.register, "ack": e.ack, "parity_error": e.parity_error,
                          "address": f"0x{e.address:08X}", "data": f"0x{e.data:08X}",
                          **({"transfers": e.transfers} if e.kind in INJECT_KINDS else {})} for e in events]
            else:
                items = [{"time_us": a.time * 1e6 / hz, "kind": a.kind, "address": f"0x{a.address:08X}",
                          "value": f"0x{a.value:08X}", "ack": a.ack,
                          **({"transfers": a.transfers} if a.kind == "inject" else {})} for a in resolve(events)]
            return {"written": written, "lost": lost, "first": first, "next": first + len(events), **extra,
                    "items": items}

    def _read_events(self, last: int, since: int | None) -> tuple[int, int, int, list, dict]:
        """イベントの記録の窓（events_read の説明）: (written, lost, first, events, extra)。ロックの中で呼ぶ。"""
        self._la_idle()
        log = EventLog(self.session.device)
        written, lost = log.counts()
        extra = {}
        if since is None:
            events = log.read(last, end=written)
            first = written - len(events)
        else:
            since = since if 0 <= since <= written else 0      # 範囲の外 = 記録が始め直された: 最初から
            first = max(since, written - log.capacity)
            extra["skipped"] = first - since
            n = min(last, written - first)
            events = log.read(n, end=first + n) if n > 0 else []
            if n > 0 and log.overwritten:           # 読む間に上書きされた先頭: 返した最初の番号は後ろにずれ、読み飛ばしに数える
                first += log.overwritten
                extra["skipped"] += log.overwritten
        return written, lost, first, events, extra
