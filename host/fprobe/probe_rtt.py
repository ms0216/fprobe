"""Probe の道具: RTT（ターゲットを止めずに、背景で周期的に読む）。

Probe（fprobe.probe）が API の名前を委ねるサービス（fprobe.probe_service。ホストから使う名前は NEEDS）。受信を貯める
背景の核（ObserveService: 貯め場 _logs・背景のスレッド・受け取り _received）の上に載り、RTT の制御ブロックの探索・
読み・書き・サンプラによる起こし方・速さの計測を持つ。機器に触るのは必ず _lock の中。

- 背景のスレッドが周期的に poll し（ObserveService._poll_once が _rtt_poll を呼ぶ）、受信をチャンネルごとの ByteLog に貯める
  （GUI と AI が同時に読める）。セミホスティングの出力も同じ貯め場（channel "semihosting"）で、rtt_read で読む。
- wake = sampler なら FPGA の DAP サンプラが上りの WrOff を見張り、変わったチャンネルだけをホストが読む。

仕様: docs/superpowers/specs/2026-10-17-f1-rtt-itm-semihosting-design.md
"""

import dataclasses
import time

from .observe_log import ByteLog, _incomplete_utf8_tail as incomplete_utf8_tail
from .debug import PyocdAccess
from .events import ACK_OK, EventCursor, EventLog, resolve
from .probe_observe import SEMIHOSTING
from .probe_service import HostView
from .rtt import Rtt, find_control_block, rtt_address_from_elf
from .sampler import Sampler

__all__ = ["RttService"]

RTT_WAKES = ("poll", "sampler")
RTT_RESYNC_S = 1.0                     # サンプラで起こすときも、この間隔で記述子を読み直す（安全網・位置の取り直し）


@dataclasses.dataclass
class _SamplerWake:
    """RTT の読みを FPGA の DAP サンプラで起こす（U50）: サンプラが上りの WrOff を周期的に読み、デバイスの時刻付きの
    イベントにする。ホストはイベント記録（FPGA の中。ターゲットの線を使わない）だけを見て、WrOff が変わった
    チャンネルだけをデバッグポートで読む。何も来ていない間、ターゲットへの読みは RTT_RESYNC_S ごとの読み直しだけ。"""
    cursor: EventCursor
    channels: dict[int, int]                          # WrOff の番地 → チャンネル
    rate_hz: float
    last_full: float
    samples: int = 0                                  # 受けた WrOff の値の数
    wakes: int = 0                                    # 変化を見てデバッグポートで読んだ回数
    idle: int = 0                                     # 変化が無く、ターゲットに触れなかった周期
    full: int = 0                                     # 記述子を読み直した回数（安全網・取りこぼし）
    lost_seen: int = 0                                # 読み直しで扱い済みの、取りこぼしたイベントの数

    def describe(self) -> dict:
        return {"mode": "sampler", "rate_hz": self.rate_hz, "samples": self.samples, "wakes": self.wakes,
                "idle": self.idle, "full_polls": self.full, "lost_events": self.cursor.lost}


class RttService:
    """RTT の受信・送信・計測（Probe のサービス）。"""

    API = ("rtt_measure", "rtt_read", "rtt_start", "rtt_status", "rtt_stop", "rtt_write")
    # ほかの領域（Probe の委譲）・試験に出す名前
    SHARED = ("_rtt", "_rtt_listen", "_rtt_listeners", "_rtt_poll", "_rtt_text", "_rtt_unlisten", "_rtt_wake")
    # ホスト（Probe の核と、ほかの領域）から使う名前
    NEEDS = ("_claim_sampler", "_closing", "_debug", "_ensure_poller", "_lock", "_logs", "_observe", "_partial",
             "_received", "_recorder", "_release_sampler", "_stdin_write", "_symbols", "poll_s", "session", "target")

    def __init__(self, host):
        self._host = HostView(host, self.NEEDS, type(self).__name__)
        self._rtt: Rtt | None = None
        self._rtt_text: set[int] = {0}
        self._rtt_error: str | None = None
        self._rtt_polls = 0
        self._rtt_wake: _SamplerWake | None = None                 # None: ホストが毎周期読む
        self._rtt_listeners: list = []                             # 受信を受け取る道具（テストポイント等）

    # --- 背景の読み --------------------------------------------------------------------------

    def _rtt_poll(self) -> None:
        """背景のスレッドの 1 周期の RTT の読み（ObserveService._poll_once から、_lock の中で）。切断されていれば止める。"""
        if self._host._debug is None and self._rtt is not None:   # 切断された
            self._rtt_release_wake()
            self._rtt = None
        if self._rtt is None:
            return
        try:
            received = self._rtt.poll() if self._rtt_wake is None else self._rtt_wake_poll(self._rtt_wake)
            self._rtt_polls += 1
            self._rtt_error = None
        except Exception as e:                                     # ターゲットのリセット中等: 次の周期にやり直す
            self._rtt_error = f"{type(e).__name__}: {e}"
            received = {}
        now = time.monotonic()
        for channel, data in received.items():
            self._host._received(channel, data, now)

    # --- 始める・止める ----------------------------------------------------------------------

    def rtt_start(self, address: int | None = None, ranges: list[list[int]] | None = None,
                  text_channels: list[int] | None = None, wake: str = "poll", interval_s: float | None = None,
                  sampler_rate_hz: float = 1000.0, caller: dict | None = None) -> dict:
        """RTT の受信を始める（ターゲットを止めずに、背景で周期的に読む）。制御ブロックの番地は address、無ければ ELF の
        _SEGGER_RTT、それも無ければ RAM（ranges = [[番地, 長さ], …]、省略時はターゲットのメモリマップの RAM）を走査。
        text_channels（既定 [0]）は記録で行に区切るチャンネル（ほかはバイト列）。
        wake: poll（ホストが interval_s ごとに記述子を読む）/ sampler（FPGA の DAP サンプラが sampler_rate_hz で WrOff を
        読み、変わったチャンネルだけをホストが読む。何も来ない間はターゲットの線を使わない。サンプラは 1 つなので
        watch_set 等とは同時に使えない）。interval_s: 背景の読みの周期（既定 0.01 秒）。
        caller: デーモンが渡す呼び出し元 {id, name, role}（引数の仕様には出ない。sampler_status の client に残す）。"""
        if wake not in RTT_WAKES:
            raise ValueError(f"wake は {' / '.join(RTT_WAKES)}（{wake!r}）")
        if interval_s is not None and not 0.0005 <= interval_s <= 1.0:
            raise ValueError("interval_s は 0.0005〜1 秒")
        with self._host._lock:
            target = self._host.target
            memory = PyocdAccess(target)
            if address is None:
                address = self._rtt_address(memory, target, ranges)
            # 制御ブロックが置かれた領域の終わり（メモリマップの事実。分からなければ 32 ビットの番地の終わり）
            region = next((r for r in getattr(target, "memory_map", None) or []
                           if r.start <= address < r.start + r.length), None)
            rtt = Rtt(memory, address, end=region.start + region.length if region is not None else 1 << 32)
            cb = rtt.open()
            if wake == "sampler":
                self._host._claim_sampler("rtt", caller)                 # ほかの道具が使っていれば、ここで誤り
            self._rtt_release_wake()                               # 前の rtt_start のサンプラ
            if wake == "sampler":
                self._host._claim_sampler("rtt", caller)
                self._rtt_wake = self._rtt_sampler_wake(rtt, sampler_rate_hz)
            if interval_s is not None:
                self._host.poll_s = interval_s
            with self._host._observe:
                self._rtt, self._rtt_error, self._rtt_polls = rtt, None, 0
                self._rtt_text = set(text_channels if text_channels is not None else [0])
                for c in cb.up:
                    self._host._logs[c.index] = ByteLog()
                    self._host._partial.pop(c.index, None)
        self._host._ensure_poller()
        with self._host._observe:
            return {"address": address, "wake": self._rtt_wake_info(), **self._rtt_channels(rtt)}

    def rtt_stop(self) -> dict:
        """RTT の受信を止める（貯めたものは rtt_read で読める）。"""
        with self._host._lock, self._host._observe:
            was, self._rtt = self._rtt is not None, None
            self._rtt_release_wake()
        return {"stopped": was}

    def _rtt_address(self, memory, target, ranges) -> int:
        if ranges is None and self._host._symbols is not None:
            try:
                return rtt_address_from_elf(self._host._symbols.path)
            except KeyError:
                # 読み込んだ ELF に制御ブロックが無い = ファームは RTT を組み込んでいない。RAM の走査（数秒〜十数秒）を
                # せずにすぐ知らせる（ELF が書いたものと違うなら ranges を渡せば探す）
                raise RuntimeError(f"ELF（{self._host._symbols.path.name}）に _SEGGER_RTT が無い: ファームに RTT が組み込まれて"
                                   "いない（書いたファームと ELF が違うなら ranges か address を渡すと RAM を探す）") from None
        if ranges is None:
            memory_map = getattr(target, "memory_map", None) or []
            ranges = [[r.start, r.length] for r in memory_map if getattr(r, "is_ram", False)]
            if not ranges:
                raise RuntimeError("RAM の範囲が分からない（ranges か address を指定する）")
        return find_control_block(memory, [tuple(r) for r in ranges])

    # --- サンプラで起こす --------------------------------------------------------------------

    def _rtt_sampler_wake(self, rtt: Rtt, rate_hz: float) -> _SamplerWake:
        """サンプラに上りの WrOff を見張らせる（_lock の中で。持ち主は claim 済み）。"""
        addresses = rtt.wroff_addresses()
        if not addresses:
            self._host._release_sampler("rtt")
            raise RuntimeError("使われている上りのチャンネルが無い")
        device = self._host.session.device
        try:
            sampler = Sampler(device)
            sampler.configure([(a, 4) for a in addresses.values()], 1.0 / rate_hz)
            log = EventLog(device)
            if self._host._recorder is None:
                log.ensure_started()                               # 記録中は Collector が始めている
            cursor = log.cursor()
            sampler.start()
        except BaseException:
            self._host._release_sampler("rtt")
            raise
        return _SamplerWake(cursor, {a: ch for ch, a in addresses.items()}, rate_hz, time.monotonic())

    def _rtt_release_wake(self) -> None:
        """サンプラで起こしていたら止めて返す（_lock の中で）。"""
        if self._rtt_wake is None:
            return
        self._rtt_wake = None
        try:
            Sampler(self._host.session.device).stop()
        except Exception:                                          # 機器が切れた等: 持ち主だけは返す
            pass
        self._host._release_sampler("rtt")

    def _rtt_wake_poll(self, wake: _SamplerWake) -> dict[int, bytes]:
        """サンプラの WrOff の値を見て、変わったチャンネルだけを読む（_lock の中で）。イベントを取りこぼしたとき・
        RTT_RESYNC_S ごとには記述子から読み直す。"""
        latest: dict[int, int] = {}
        for a in resolve(wake.cursor.take()):
            if a.kind == "sample" and a.ack == ACK_OK and a.address in wake.channels:
                latest[wake.channels[a.address]] = a.value
                wake.samples += 1
        now = time.monotonic()
        if wake.cursor.lost != wake.lost_seen or now - wake.last_full >= RTT_RESYNC_S:
            wake.lost_seen = wake.cursor.lost
            wake.full += 1
            wake.last_full = now
            return self._rtt.poll()
        up = self._rtt.control_block.up
        changed = {ch: wr for ch, wr in latest.items() if wr != up[ch].offset}
        if not changed:
            wake.idle += 1
            return {}
        wake.wakes += 1
        return self._rtt.poll(changed, wr_hint=changed)

    def _rtt_wake_info(self) -> dict:
        wake = self._rtt_wake
        return {"mode": "poll", "interval_s": self._host.poll_s} if wake is None else \
            {**wake.describe(), "interval_s": self._host.poll_s}

    # --- 読む・書く --------------------------------------------------------------------------

    def _rtt_channels(self, rtt: Rtt) -> dict:
        """チャンネルの一覧と受信の量（_observe の中で）。"""
        cb = rtt.control_block

        def channel(c, up: bool) -> dict:
            out = {"index": c.index, "name": c.name, "size": c.size, "mode": int(c.mode), "resyncs": c.resyncs}
            if up:
                log = self._host._logs.get(c.index)
                out.update(received=log.end if log else 0, unread=log.end - log.cursor if log else 0)
            return out
        return {"up": [channel(c, True) for c in cb.up], "down": [channel(c, False) for c in cb.down]}

    def rtt_read(self, channel: int | str = 0, since: int | None = None, max_bytes: int = 65536,
                 encoding: str = "text", timeout_s: float = 0.0) -> dict:
        """受信を読む。channel は RTT の上りの番号か "semihosting"（セミホスティングの出力）。since を省くと前回の
        続きから（読むと進む）。since（前回の next）を渡すと、そこから読む（既定の位置は動かさない。画面用）。
        timeout_s までデータを待つ。encoding: text（UTF-8）/ hex。lost は貯め場からあふれて読めなかったバイト数。"""
        if encoding not in ("text", "hex"):
            raise ValueError("encoding は text か hex")
        deadline = time.monotonic() + timeout_s
        with self._host._observe:
            while True:
                log = self._host._logs.get(channel)
                waiting = log is None or (since if since is not None else log.cursor) >= log.end
                remaining = deadline - time.monotonic()
                if not waiting or not remaining > 0:           # NaN の期限でも待たない（NaN は <= 0 が偽）
                    break
                self._host._observe.wait(remaining)
            if log is None:
                return {"channel": channel, "data": "", "start": 0, "next": 0, "lost": 0}
            data, start, lost = log.read(since, max_bytes)
            if encoding == "text" and (tail := incomplete_utf8_tail(data)):
                data = data[:-tail]
                if since is None:
                    log.unread(tail)
            text = data.decode("utf-8", errors="replace") if encoding == "text" else data.hex()
            return {"channel": channel, "data": text, "start": start, "next": start + len(data), "lost": lost}

    def rtt_write(self, channel: int | str = 0, text: str | None = None, data_hex: str | None = None,
                  timeout_s: float = 1.0) -> dict:
        """ターゲットへ送る（RTT の下りの番号。"semihosting" ならセミホスティングの標準入力）。text（UTF-8）か data_hex。
        ターゲットが読んで空くまで timeout_s 待つ。written は書けたバイト数。"""
        if (text is None) == (data_hex is None):
            raise ValueError("text か data_hex のどちらか 1 つを指定する")
        data = text.encode("utf-8") if text is not None else bytes.fromhex(data_hex)
        if channel == SEMIHOSTING:
            self._host._stdin_write(data)
            return {"written": len(data)}
        deadline, done = time.monotonic() + timeout_s, 0
        while True:
            with self._host._lock:
                if self._rtt is None:
                    raise RuntimeError("RTT を始めていない（rtt_start）")
                done += self._rtt.write(channel, data[done:])
            if done == len(data) or time.monotonic() >= deadline:
                return {"written": done}
            time.sleep(self._host.poll_s)

    def _rtt_listen(self, listener) -> None:
        """RTT の受信（チャンネル, バイト列, ホストの時刻）を listener にも渡す（背景のスレッドから、_lock の中で）。
        貯め場（rtt_read の読む口）とは別に、取りこぼさずに受け取れる。"""
        self._rtt_listeners.append(listener)

    def _rtt_unlisten(self, listener) -> None:
        if listener in self._rtt_listeners:
            self._rtt_listeners.remove(listener)

    # --- 様子・計測 --------------------------------------------------------------------------

    def rtt_status(self) -> dict:
        """RTT の受信の様子（制御ブロックの番地・チャンネル・受信したバイト数・未読・最後の誤り）とセミホスティングの出力の量。"""
        with self._host._observe:
            rtt = self._rtt                                       # 切断で別のスレッドが None にしても、この読みは一貫させる
            out = {"running": rtt is not None, "error": self._rtt_error, "polls": self._rtt_polls}
            if rtt is not None:
                out.update(address=rtt.address, wake=self._rtt_wake_info(), stats=rtt.stats(),
                           **self._rtt_channels(rtt))
            log = self._host._logs.get(SEMIHOSTING)
            out[SEMIHOSTING] = {"received": log.end if log else 0, "unread": log.end - log.cursor if log else 0}
            return out

    def _rtt_counters(self) -> tuple[float, dict, dict, dict | None]:
        rtt = self._rtt
        if rtt is None:
            raise RuntimeError("RTT を始めていない（rtt_start）")
        with self._host._observe:
            logs = self._host._logs
            received = {c.index: logs[c.index].end if c.index in logs else 0
                        for c in rtt.control_block.up if c.in_use}
            wake = self._rtt_wake
            return time.monotonic(), rtt.stats(), received, None if wake is None else wake.describe()

    def rtt_measure(self, duration_s: float = 1.0) -> dict:
        """RTT の受信の速さを duration_s の間はかる（機器には触れない。背景の読みの数え上げを比べる）。ターゲットが
        出し続けている間に呼ぶ。bytes_per_s（チャンネルごとと合計）、polls_per_s、empty_poll_ratio（何も来なかった読み
        の割合）、reads_per_s・read_bytes_per_s（デバッグポートの読み）、efficiency（受けたバイト ÷ 読んだバイト）、
        wake（サンプラで起こすなら、ターゲットに触れなかった周期 idle 等）。"""
        t0, s0, r0, w0 = self._rtt_counters()
        self._host._closing.wait(duration_s)
        t1, s1, r1, w1 = self._rtt_counters()
        dt = max(t1 - t0, 1e-9)
        d = {k: s1[k] - s0[k] for k in s1}
        up = {ch: {"bytes": r1[ch] - r0.get(ch, 0), "bytes_per_s": round((r1[ch] - r0.get(ch, 0)) / dt, 1)}
              for ch in r1}
        total = sum(v["bytes"] for v in up.values())
        out = {"duration_s": round(dt, 4), "up": up, "bytes": total, "bytes_per_s": round(total / dt, 1),
               "polls": d["polls"], "polls_per_s": round(d["polls"] / dt, 1),
               "empty_poll_ratio": round(d["empty_polls"] / d["polls"], 4) if d["polls"] else None,
               "reads": d["reads"], "reads_per_s": round(d["reads"] / dt, 1),
               "read_bytes_per_s": round(d["read_bytes"] / dt, 1), "writes_per_s": round(d["writes"] / dt, 1),
               "efficiency": round(d["up_bytes"] / d["read_bytes"], 4) if d["read_bytes"] else None,
               "wake": {"mode": "poll", "interval_s": self._host.poll_s}}
        if w0 is not None and w1 is not None:
            out["wake"] = {"mode": "sampler", "rate_hz": w1["rate_hz"], "interval_s": self._host.poll_s,
                           **{k: w1[k] - w0[k] for k in ("samples", "wakes", "idle", "full_polls", "lost_events")}}
        return out
