"""Probe の道具: 止めずに見る（RTT・SWO の ITM の解読・セミホスティング）と、捕捉した波形の通信の解読。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う mixin。Probe の _lock・target・dbg・_symbols・_debug・_recorder・_capture・
_capture_info を使う。機器に触るのは必ず _lock の中。

- RTT: 背景のスレッドが周期的に poll し、受信をチャンネルごとの ByteLog に貯める（GUI と AI が同時に読める）。
- セミホスティング: 止まった理由が BKPT 0xAB なら処理して再開する（run_until_halt の中と、target_resume の後の
  背景の見張り）。出力は ByteLog "semihosting" に入り、rtt_read(channel="semihosting") で読む。ファイル操作は
  file_root を API で明示したときだけ（仕様の x-mcp: false で MCP には出さない）。
- 記録中（_recorder）なら、RTT の行・データ、ITM の解読結果、通信の解読の注釈を共通の時間軸の記録に入れる。

仕様: docs/superpowers/specs/2026-10-17-f1-rtt-itm-semihosting-design.md、
docs/superpowers/specs/2026-10-17-f7-protocol-decode-design.md、計画: docs/superpowers/plans/2026-10-17-api-integration-a-d.md
"""

import bisect
import collections
import dataclasses
import enum
import json
import threading
import time

from pyocd.core.target import Target

from .api import NoData
from .debug import PyocdAccess, Stop
from .decode import Decoding, decode_on, get_decoder
from .decode.registry import split_settings
from .itm import (DataTraceAddress, DataTracePc, DataTraceValue, DecodeError, ExceptionTrace, Instrumentation,
                  ItmDecoder, Overflow, PcSample, PortText, Timestamper)
from .events import ACK_OK, EventCursor, EventLog, resolve
from .profile.exceptions import exception_name as _exception_name_of
from .rtt import Rtt, find_control_block, rtt_address_from_elf
from .sampler import Sampler
from .semihosting import FileAccess, SemihostingHost, SemihostingResult

__all__ = ["ByteLog", "ObserveTools", "SEMIHOSTING"]

RTT_WAKES = ("poll", "sampler")
RTT_RESYNC_S = 1.0                     # サンプラで起こすときも、この間隔で記述子を読み直す（安全網・位置の取り直し）

SEMIHOSTING = "semihosting"            # セミホスティングの出力の貯め場の名前（rtt_read の channel）
RAW_SOURCES = ("swo", "uart")          # 受けたバイトをそのまま貯める流れ（読む口: swo_read・swo_decode・uart_read）
LOG_LIMIT = 1 << 20                    # 1 つの貯め場に残すバイト数（超えたら古いものから捨てる）
MARKS_LIMIT = 100_000                  # 1 つの貯め場に残す受け取りの時刻の数
SWO_LINES_LIMIT = 10_000               # ポートごとに残す行数
DECODE_CACHE = 8                       # 解読の結果を覚えておく数（同じ解読を offset で読み進める・積み重ねの下の解読）


class ByteLog:
    """受け取ったバイトの貯め場。位置は受け取りの最初からの通し番号。既定の読み手の位置（cursor。読むと進む）と、
    明示の位置（since。画面が自分で持つ。cursor は動かさない）の 2 通りで読める。受け取りごとの時刻（marks）も持つ
    （端末の行の時刻・時間軸へ飛ぶ。G04）。"""

    def __init__(self, limit: int = LOG_LIMIT):
        self.limit = limit
        self._data = bytearray()
        self.start = 0                 # _data[0] の通し番号
        self.cursor = 0
        # 受け取りごとの (位置, 壁時計の秒, タイムベースの時刻 | None)。位置の昇順。読めなくなった分は捨てる（読める
        # 最初のバイトを含む受け取りの印は 1 つ残す）
        self.marks: list[tuple[int, float, int | None]] = []

    @property
    def end(self) -> int:
        return self.start + len(self._data)

    def append(self, data: bytes, wall: float | None = None, t: int | None = None) -> None:
        """data を足す。wall（受けた壁時計の秒）を渡すと、その受け取りの時刻の印も残す（t は記録中のタイムベース）。"""
        if wall is not None and data:
            self.marks.append((self.end, wall, t))
        self._data += data
        excess = len(self._data) - self.limit
        if excess > 0:
            del self._data[:excess]
            self.start += excess
            first = bisect.bisect_right(self.marks, self.start, key=lambda m: m[0]) - 1
            if first > 0:
                del self.marks[:first]
        if len(self.marks) > MARKS_LIMIT:
            del self.marks[:len(self.marks) - MARKS_LIMIT]

    def times(self, start: int, length: int) -> list[tuple[int, float, int | None]]:
        """位置 [start, start + length) のバイトの受け取りの時刻: start を含む受け取りの印（位置は start に詰める）と、
        その後に始まる受け取りの印。"""
        i = bisect.bisect_right(self.marks, start, key=lambda m: m[0])     # 後に始まる最初の印
        out = [(start, *self.marks[i - 1][1:])] if i > 0 else []
        for pos, wall, t in self.marks[i:]:
            if pos >= start + length:
                break
            out.append((pos, wall, t))
        return out

    def read(self, since: int | None = None, max_bytes: int = 65536) -> tuple[bytes, int, int]:
        """(データ, 読み始めた位置, 捨てられて読めなかったバイト数)。"""
        pos = self.cursor if since is None else since
        lost = max(0, self.start - pos)
        pos = min(max(pos, self.start), self.end)
        data = bytes(self._data[pos - self.start:pos - self.start + max_bytes])
        if since is None:
            self.cursor = pos + len(data)
        return data, pos, lost

    def unread(self, count: int) -> None:
        """既定の読み手の位置を count 戻す（途中で切れた UTF-8 の文字を次の読みに回す）。"""
        self.cursor -= count


def _incomplete_utf8_tail(data: bytes) -> int:
    """末尾の、まだ続きが来ていない UTF-8 の文字のバイト数（0〜3）。"""
    for back in range(1, min(4, len(data)) + 1):
        b = data[-back]
        if b & 0xC0 != 0x80:                                 # 先頭のバイト（または ASCII）
            need = 2 if b & 0xE0 == 0xC0 else 3 if b & 0xF0 == 0xE0 else 4 if b & 0xF8 == 0xF0 else 1
            return back if need > back else 0
    return 0


def _jsonable_packet(packet) -> dict:
    out = {"type": type(packet).__name__}
    for f in dataclasses.fields(packet):
        if f.name == "offset":
            continue
        value = getattr(packet, f.name)
        out[f.name] = value.name.lower() if isinstance(value, enum.Enum) else \
            value.hex() if isinstance(value, (bytes, bytearray)) else value
    if isinstance(packet, ExceptionTrace):
        out["name"] = _exception_name(packet.number)
    return out


def _exception_name(number: int) -> str:
    return _exception_name_of(number)                 # 名前の表は profile/exceptions.py の 1 か所


class _PyocdSemihosting:
    """pyOCD のターゲットをセミホスティングの読み手（SemihostingTarget）に合わせる。"""

    def __init__(self, target):
        self.target = target
        self.access = PyocdAccess(target)

    def read_core_register(self, name: str) -> int:
        return self.target.read_core_register(name)

    def write_core_register(self, name: str, value: int) -> None:
        self.target.write_core_register(name, value)

    def read_memory(self, address: int, length: int) -> bytes:
        return self.access.read_memory(address, length)

    def write_memory(self, address: int, data: bytes) -> None:
        self.access.write_memory(address, data)

    def resume(self) -> None:
        self.target.resume()


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


class ObserveTools:
    """止めずに見る道具と通信の解読（Probe の mixin）。"""

    def _init_observe(self) -> None:
        self._observe = threading.Condition()                      # 貯め場・行の読み書き（機器の _lock とは別）
        self._logs: dict[int | str, ByteLog] = {}
        self._partial: dict[int | str, bytearray] = {}             # 記録の行に組み立て中のバイト
        self._poller: threading.Thread | None = None
        self._closing = threading.Event()
        self.poll_s = 0.01
        # RTT
        self._rtt: Rtt | None = None
        self._rtt_text: set[int] = {0}
        self._rtt_error: str | None = None
        self._rtt_polls = 0
        self._rtt_wake: _SamplerWake | None = None                 # None: ホストが毎周期読む
        self._rtt_listeners: list = []                             # 受信を受け取る道具（テストポイント等）
        # 生の流れ（SWO・UART）: 背景で読む（_pumps）と、受けたバイトを渡す先（_taps。TCP の口・ファイル）
        self._taps: dict[int | str, list] = {}
        self._pumps: set[str] = set()
        self._pump_errors: dict[str, str] = {}
        # 周期の仕事（ほかの道具が足す。名前 → [周期, 次の時刻, 関数]）と、その最後の誤り
        self._jobs: dict[str, list] = {}
        self._job_errors: dict[str, str] = {}
        self._swo_on = False
        # SWO（ITM）
        self._swo_reset()
        self._swo_listeners: list = []                             # 解読したパケットを受け取る道具（プロファイル等）
        # セミホスティング
        self._sh_enabled = True
        self._sh_files = FileAccess()
        self._sh_cmdline = ""
        self._sh_host: SemihostingHost | None = None
        self._sh_watch = False                                     # 再開した後、背景で止まるのを見張る（BKPT 0xAB・動作のあるブレークポイント）
        self._sh_requests = 0
        self._sh_exit: dict | None = None
        self._sh_stdin = bytearray()
        # 通信の解読
        self._decodings: collections.OrderedDict = collections.OrderedDict()

    # --- 背景のスレッド ----------------------------------------------------------------------

    def _ensure_poller(self) -> None:
        with self._observe:
            if self._poller is None and not self._closing.is_set():
                self._poller = threading.Thread(target=self._poll_loop, name="fprobe-observe", daemon=True)
                self._poller.start()

    def _poll_loop(self) -> None:
        while not self._closing.wait(self.poll_s):
            with self._observe:
                if self._rtt is None and not self._sh_watch and not self._pumps and not self._jobs:
                    self._poller = None                            # やることが無い: 終わる（_ensure_poller で起こす）
                    return
            self._poll_once()

    def _poll_once(self) -> None:
        with self._lock:
            if self._debug is None and (self._rtt is not None or self._sh_watch):   # 切断された
                self._rtt_release_wake()
                self._rtt, self._sh_watch = None, False
            if self._rtt is not None:
                try:
                    received = self._rtt.poll() if self._rtt_wake is None else self._rtt_wake_poll(self._rtt_wake)
                    self._rtt_polls += 1
                    self._rtt_error = None
                except Exception as e:                             # ターゲットのリセット中等: 次の周期にやり直す
                    self._rtt_error = f"{type(e).__name__}: {e}"
                    received = {}
                now = time.monotonic()
                for channel, data in received.items():
                    self._received(channel, data, now)
            if self._sh_watch:
                self._service_in_background()
            now = time.monotonic()
            for name, job in list(self._jobs.items()):
                period, due, fn = job
                if now < due:
                    continue
                job[1] = max(due + period, now)                    # 遅れても詰めて走らせない
                self._run_retrying(self._job_errors, name, fn)     # 走行中の読みの失敗等
            for source in sorted(self._pumps):
                self._run_retrying(self._pump_errors, source, lambda source=source: self._fetch(source))   # COM ポートが無い等

    @staticmethod
    def _run_retrying(errors: dict, key, fn) -> None:
        """fn() を呼ぶ。失敗は errors[key] に残して次の周期にやり直す（成功すれば消す）。"""
        try:
            fn()
            errors.pop(key, None)
        except Exception as e:
            errors[key] = f"{type(e).__name__}: {e}"

    def _fetch(self, source: str) -> None:
        """生の流れ source の届いた分を読んで貯める（_lock の中で）。"""
        if source == "swo":
            self._swo_fetch()
        elif source == "uart":
            self._uart_fetch()

    def _swo_fetch(self) -> None:
        if self._swo_on and self._debug is not None:
            chunk = self._debug.probe.swo_read()
            if chunk:
                self._received("swo", bytes(chunk), time.monotonic())

    def _collect(self, source: str, timeout_s: float) -> bytes:
        """timeout_s の間、source を読み、前回の続きから貯まった分を返す（背景で読んでいれば、その分も）。"""
        deadline = time.monotonic() + timeout_s
        while True:
            with self._lock:
                self._fetch(source)
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        with self._observe:
            log = self._logs.get(source)
            return log.read(max_bytes=LOG_LIMIT)[0] if log is not None else b""

    def _add_job(self, name: str, period_s: float, fn) -> None:
        """背景で period_s ごとに fn() を呼ぶ（_lock の中で。周期の細かさは poll_s まで）。同じ名前は置き換える。"""
        with self._observe:
            self._jobs[name] = [period_s, time.monotonic(), fn]
            self._job_errors.pop(name, None)
        self._ensure_poller()

    def _remove_job(self, name: str) -> None:
        with self._observe:
            self._jobs.pop(name, None)

    def _tap(self, key: int | str, fn) -> None:
        """key（RTT の上りの番号・"semihosting"・"swo"・"uart"）の受けたバイトを fn(data) にも渡す（背景のスレッドから、
        _lock の中で。待たないこと）。生の流れなら背景で読み始める。"""
        with self._observe:
            self._taps.setdefault(key, []).append(fn)
            if key in RAW_SOURCES:
                self._pumps.add(key)
        self._ensure_poller()

    def _untap(self, key: int | str, fn) -> None:
        with self._observe:
            fns = self._taps.get(key, [])
            if fn in fns:
                fns.remove(fn)
            if not fns:
                self._taps.pop(key, None)
                self._pumps.discard(key)

    def _stop_observing(self) -> None:
        """Probe.close から: 背景のスレッドを止める。"""
        self._closing.set()
        thread = self._poller
        if thread is not None:
            thread.join(timeout=2)

    def _received(self, key: int | str, data: bytes, host_s: float) -> None:
        wall, t = self._stamp(host_s)
        with self._observe:
            self._logs.setdefault(key, ByteLog()).append(data, wall, t)
            self._observe.notify_all()
            taps = list(self._taps.get(key, ()))
        for tap in taps:
            tap(data)
        if key in RAW_SOURCES:
            return                                                 # SWO・UART は記録・RTT の受け手の対象でない
        for listener in list(self._rtt_listeners):
            listener(key, data, host_s)
        if self._recorder is not None:
            self._record_received(key, data, host_s)

    def _stamp(self, host_s: float) -> tuple[float, int | None]:
        """受けた時刻（time.monotonic）→ (壁時計の秒, 記録中ならタイムベースの時刻)。機器には触らない。"""
        wall = time.time() - (time.monotonic() - host_s)
        rec = self._recorder
        if rec is None:
            return wall, None
        try:
            return wall, rec.clock.to_ticks(host_s)[0]
        except RuntimeError:
            return wall, None                                      # 時計の対応付けの前（記録を始めた直後）

    def _record_received(self, key: int | str, data: bytes, host_s: float) -> None:
        if key == SEMIHOSTING or key in self._rtt_text:
            buf = self._partial.setdefault(key, bytearray())
            buf += data
            *lines, rest = bytes(buf).split(b"\n")
            self._partial[key] = bytearray(rest)
            for line in lines:
                text = line.rstrip(b"\r").decode("utf-8", errors="replace")
                if key == SEMIHOSTING:
                    self._record_host(SEMIHOSTING, "line", host_s, body={"text": text})
                else:
                    self._record_host("rtt", "line", host_s, a=key, body={"channel": key, "text": text})
        else:
            self._record_host("rtt", "data", host_s, a=key, body={"channel": key, "hex": data.hex()})

    def _record_host(self, stream: str, kind: str, host_s: float, **fields) -> int | None:
        """記録中なら出来事を入れ、記録のタイムベースに写した時刻を返す（記録していない・写せなければ None）。"""
        rec = self._recorder
        if rec is None:
            return None
        try:
            return rec.add_host(stream, kind, host_s, **fields)
        except RuntimeError:
            return None                                            # 時計の対応付けの前（記録を始めた直後）

    # --- RTT ---------------------------------------------------------------------------------

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
        with self._lock:
            target = self.target
            memory = PyocdAccess(target)
            if address is None:
                address = self._rtt_address(memory, target, ranges)
            # 制御ブロックが置かれた領域の終わり（メモリマップの事実。分からなければ 32 ビットの番地の終わり）
            region = next((r for r in getattr(target, "memory_map", None) or []
                           if r.start <= address < r.start + r.length), None)
            rtt = Rtt(memory, address, end=region.start + region.length if region is not None else 1 << 32)
            cb = rtt.open()
            if wake == "sampler":
                self._claim_sampler("rtt", caller)                 # ほかの道具が使っていれば、ここで誤り
            self._rtt_release_wake()                               # 前の rtt_start のサンプラ
            if wake == "sampler":
                self._claim_sampler("rtt", caller)
                self._rtt_wake = self._rtt_sampler_wake(rtt, sampler_rate_hz)
            if interval_s is not None:
                self.poll_s = interval_s
            with self._observe:
                self._rtt, self._rtt_error, self._rtt_polls = rtt, None, 0
                self._rtt_text = set(text_channels if text_channels is not None else [0])
                for c in cb.up:
                    self._logs[c.index] = ByteLog()
                    self._partial.pop(c.index, None)
        self._ensure_poller()
        with self._observe:
            return {"address": address, "wake": self._rtt_wake_info(), **self._rtt_channels(rtt)}

    def _rtt_sampler_wake(self, rtt: Rtt, rate_hz: float) -> _SamplerWake:
        """サンプラに上りの WrOff を見張らせる（_lock の中で。持ち主は claim 済み）。"""
        addresses = rtt.wroff_addresses()
        if not addresses:
            self._release_sampler("rtt")
            raise RuntimeError("使われている上りのチャンネルが無い")
        device = self.session.device
        try:
            sampler = Sampler(device)
            sampler.configure([(a, 4) for a in addresses.values()], 1.0 / rate_hz)
            log = EventLog(device)
            if self._recorder is None:
                log.ensure_started()                               # 記録中は Collector が始めている
            cursor = log.cursor()
            sampler.start()
        except BaseException:
            self._release_sampler("rtt")
            raise
        return _SamplerWake(cursor, {a: ch for ch, a in addresses.items()}, rate_hz, time.monotonic())

    def _rtt_release_wake(self) -> None:
        """サンプラで起こしていたら止めて返す（_lock の中で）。"""
        if self._rtt_wake is None:
            return
        self._rtt_wake = None
        try:
            Sampler(self.session.device).stop()
        except Exception:                                          # 機器が切れた等: 持ち主だけは返す
            pass
        self._release_sampler("rtt")

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
        return {"mode": "poll", "interval_s": self.poll_s} if wake is None else \
            {**wake.describe(), "interval_s": self.poll_s}

    def _rtt_address(self, memory, target, ranges) -> int:
        if ranges is None and self._symbols is not None:
            try:
                return rtt_address_from_elf(self._symbols.path)
            except KeyError:
                # 読み込んだ ELF に制御ブロックが無い = ファームは RTT を組み込んでいない。RAM の走査（数秒〜十数秒）を
                # せずにすぐ知らせる（ELF が書いたものと違うなら ranges を渡せば探す）
                raise RuntimeError(f"ELF（{self._symbols.path.name}）に _SEGGER_RTT が無い: ファームに RTT が組み込まれて"
                                   "いない（書いたファームと ELF が違うなら ranges か address を渡すと RAM を探す）") from None
        if ranges is None:
            memory_map = getattr(target, "memory_map", None) or []
            ranges = [[r.start, r.length] for r in memory_map if getattr(r, "is_ram", False)]
            if not ranges:
                raise RuntimeError("RAM の範囲が分からない（ranges か address を指定する）")
        return find_control_block(memory, [tuple(r) for r in ranges])

    def _rtt_channels(self, rtt: Rtt) -> dict:
        """チャンネルの一覧と受信の量（_observe の中で）。"""
        cb = rtt.control_block

        def channel(c, up: bool) -> dict:
            out = {"index": c.index, "name": c.name, "size": c.size, "mode": int(c.mode), "resyncs": c.resyncs}
            if up:
                log = self._logs.get(c.index)
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
        with self._observe:
            while True:
                log = self._logs.get(channel)
                waiting = log is None or (since if since is not None else log.cursor) >= log.end
                remaining = deadline - time.monotonic()
                if not waiting or not remaining > 0:           # NaN の期限でも待たない（NaN は <= 0 が偽）
                    break
                self._observe.wait(remaining)
            if log is None:
                return {"channel": channel, "data": "", "start": 0, "next": 0, "lost": 0}
            data, start, lost = log.read(since, max_bytes)
            if encoding == "text" and (tail := _incomplete_utf8_tail(data)):
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
            with self._observe:
                self._sh_stdin += data
            return {"written": len(data)}
        deadline, done = time.monotonic() + timeout_s, 0
        while True:
            with self._lock:
                if self._rtt is None:
                    raise RuntimeError("RTT を始めていない（rtt_start）")
                done += self._rtt.write(channel, data[done:])
            if done == len(data) or time.monotonic() >= deadline:
                return {"written": done}
            time.sleep(self.poll_s)

    def _rtt_listen(self, listener) -> None:
        """RTT の受信（チャンネル, バイト列, ホストの時刻）を listener にも渡す（背景のスレッドから、_lock の中で）。
        貯め場（rtt_read の読む口）とは別に、取りこぼさずに受け取れる。"""
        self._rtt_listeners.append(listener)

    def _rtt_unlisten(self, listener) -> None:
        if listener in self._rtt_listeners:
            self._rtt_listeners.remove(listener)

    def rtt_stop(self) -> dict:
        """RTT の受信を止める（貯めたものは rtt_read で読める）。"""
        with self._lock, self._observe:
            was, self._rtt = self._rtt is not None, None
            self._rtt_release_wake()
        return {"stopped": was}

    def rtt_status(self) -> dict:
        """RTT の受信の様子（制御ブロックの番地・チャンネル・受信したバイト数・未読・最後の誤り）とセミホスティングの出力の量。"""
        with self._observe:
            rtt = self._rtt                                       # 切断で別のスレッドが None にしても、この読みは一貫させる
            out = {"running": rtt is not None, "error": self._rtt_error, "polls": self._rtt_polls}
            if rtt is not None:
                out.update(address=rtt.address, wake=self._rtt_wake_info(), stats=rtt.stats(),
                           **self._rtt_channels(rtt))
            log = self._logs.get(SEMIHOSTING)
            out[SEMIHOSTING] = {"received": log.end if log else 0, "unread": log.end - log.cursor if log else 0}
            return out

    def _rtt_counters(self) -> tuple[float, dict, dict, dict | None]:
        rtt = self._rtt
        if rtt is None:
            raise RuntimeError("RTT を始めていない（rtt_start）")
        with self._observe:
            received = {c.index: self._logs[c.index].end if c.index in self._logs else 0
                        for c in rtt.control_block.up if c.in_use}
            wake = self._rtt_wake
            return time.monotonic(), rtt.stats(), received, None if wake is None else wake.describe()

    def rtt_measure(self, duration_s: float = 1.0) -> dict:
        """RTT の受信の速さを duration_s の間はかる（機器には触れない。背景の読みの数え上げを比べる）。ターゲットが
        出し続けている間に呼ぶ。bytes_per_s（チャンネルごとと合計）、polls_per_s、empty_poll_ratio（何も来なかった読み
        の割合）、reads_per_s・read_bytes_per_s（デバッグポートの読み）、efficiency（受けたバイト ÷ 読んだバイト）、
        wake（サンプラで起こすなら、ターゲットに触れなかった周期 idle 等）。"""
        t0, s0, r0, w0 = self._rtt_counters()
        self._closing.wait(duration_s)
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
               "wake": {"mode": "poll", "interval_s": self.poll_s}}
        if w0 is not None and w1 is not None:
            out["wake"] = {"mode": "sampler", "rate_hz": w1["rate_hz"], "interval_s": self.poll_s,
                           **{k: w1[k] - w0[k] for k in ("samples", "wakes", "idle", "full_polls", "lost_events")}}
        return out

    # --- SWO（ITM の解読） -------------------------------------------------------------------

    def _swo_reset(self) -> None:
        self._itm = ItmDecoder()
        self._timestamper = Timestamper()
        self._port_text = PortText()
        self._swo_lines: dict[int, collections.deque] = {}
        self._swo_overflows = 0
        self._swo_errors = 0

    def _swo_receive(self, timeout_s: float) -> tuple[int, list[dict]]:
        """timeout_s の間に届いた SWO を解読する。(バイト数, パケット)。行はポートごとに貯める。"""
        with self._lock:
            self.target                                            # 未接続ならここで失敗する
        data = self._collect("swo", timeout_s)
        now = time.monotonic()
        packets = self._itm.feed(bytes(data))
        for listener in list(self._swo_listeners):
            listener(packets)
        timed = self._timestamper.feed(packets) + self._timestamper.flush()
        lines = self._port_text.feed(packets)
        with self._observe:
            for line in lines:
                self._swo_lines.setdefault(line.port, collections.deque(maxlen=SWO_LINES_LIMIT)).append(line.text)
        out = []
        for t in timed:
            d = _jsonable_packet(t.packet)
            d.update(local_time=t.local_time, global_time=t.global_time)
            out.append(d)
            self._swo_overflows += isinstance(t.packet, Overflow)
            self._swo_errors += isinstance(t.packet, DecodeError)
            self._record_packet(t.packet, now)
        for line in lines:
            self._record_host("swo", "line", now, a=line.port, body={"port": line.port, "text": line.text})
        return len(data), out

    def _swo_drain(self, quiet_s: float = 0.03, limit_s: float = 0.5) -> int:
        """SWO の受信に残っている古いバイトを捨てる（quiet_s の間なにも来なくなるまで、最長 limit_s）。捨てたバイト数。
        受信を始めた直後は、前の受信の残り（機器の FIFO）が先に届き、新しい流れとの継ぎ目で解読がずれる
        （2026-10-18 に実機で）。ターゲットが出力を止めている間に呼ぶ。"""
        dropped, last, deadline = 0, time.monotonic(), time.monotonic() + limit_s
        while time.monotonic() < deadline:
            chunk = self._collect("swo", 0)
            now = time.monotonic()
            if chunk:
                dropped, last = dropped + len(chunk), now
            elif now - last >= quiet_s:
                break
            time.sleep(0.005)
        self._swo_reset()
        return dropped

    def _swo_listen(self, listener) -> None:
        """SWO の受信を解読したパケット（fprobe.itm.Packet の列）を listener にも渡す。受信の口は 1 つのまま
        （swo_decode・swo_text・プロファイルのどれが読んでも、全員が同じパケットを受け取る）。"""
        self._swo_listeners.append(listener)

    def _swo_unlisten(self, listener) -> None:
        if listener in self._swo_listeners:
            self._swo_listeners.remove(listener)

    def _record_packet(self, p, host_s: float) -> None:
        if self._recorder is None:
            return
        if isinstance(p, ExceptionTrace):
            self._record_host("swo", "exception", host_s, a=p.number,
                              body={"action": p.action.name.lower(), "name": _exception_name(p.number)})
        elif isinstance(p, PcSample):
            self._record_host("swo", "pc_sample", host_s, a=p.pc or 0, body={"sleep": p.pc is None})
        elif isinstance(p, Instrumentation):
            self._record_host("swo", "stimulus", host_s, a=p.port, b=p.value, body={"size": p.size})
        elif isinstance(p, (DataTracePc, DataTraceAddress, DataTraceValue)):
            self._record_host("swo", "data_trace", host_s, body=_jsonable_packet(p))
        elif isinstance(p, Overflow):
            self._record_host("swo", "overflow", host_s)

    def _take_lines(self, port: int | None) -> dict[int, list[str]]:
        with self._observe:
            ports = list(self._swo_lines) if port is None else [port]
            out = {p: list(self._swo_lines.get(p, ())) for p in ports}
            for p in ports:
                self._swo_lines.pop(p, None)
            return out

    def swo_decode(self, timeout_s: float = 0.5, limit: int = 500) -> dict:
        """timeout_s の間に届いた SWO を ITM / DWT のパケットに解読する（swo_start の後。swo_read と同じ受信を使うので
        どちらか一方で読む）。packets は先頭 limit 件（例外トレース・PC サンプル・スティミュラス等）、text はポートごとの
        printf の行（呼び出しをまたいで行を組み立てる）。"""
        received, packets = self._swo_receive(timeout_s)
        return {"bytes": received, "synced": self._itm.synced, "unsynced_bytes": self._itm.unsynced_bytes,
                "packets": packets[:limit],
                "dropped": max(0, len(packets) - limit), "text": self._take_lines(None),
                "overflows": self._swo_overflows, "errors": self._swo_errors}

    def swo_text(self, port: int = 0, timeout_s: float = 0.5) -> dict:
        """timeout_s の間に届いた SWO から、ITM のポート port に printf された行（pending は改行の来ていない途中）。"""
        self._swo_receive(timeout_s)
        return {"port": port, "lines": self._take_lines(port)[port], "pending": self._port_text.pending(port)}

    # --- セミホスティング -----------------------------------------------------------------------

    def semihosting_configure(self, enabled: bool = True, file_root: str | None = None, writable: bool = False,
                              cmdline: str = "") -> dict:
        """セミホスティング（BKPT 0xAB）の処理の設定。既定で有効（出力は rtt_read の channel "semihosting"）。
        ファイル操作は file_root を指定したときだけ、その下に限って許す（writable で書き込みも）。file_root を省いて
        呼ぶとファイル操作は無効に戻る（AI の道具からは file_root を指定できないので、AI が設定し直すと無効になる）。"""
        with self._lock:
            self._sh_enabled = enabled
            self._sh_files = FileAccess(file_root, writable)
            self._sh_cmdline = cmdline
            if self._sh_host is not None:
                self._sh_host.close()
                self._sh_host = None
            return self.semihosting_status()

    def semihosting_status(self) -> dict:
        """セミホスティングの設定・処理した要求の数・最後の終了（SYS_EXIT の終了コード）。"""
        root = self._sh_files.root
        return {"enabled": self._sh_enabled, "file_root": str(root) if root else None,
                "writable": self._sh_files.writable, "requests": self._sh_requests, "exit": self._sh_exit,
                "watching": self._sh_watch}

    def _semihosting(self) -> SemihostingHost:
        if self._sh_host is None:
            self._sh_host = SemihostingHost(console=self._console, stdin=self._stdin, files=self._sh_files,
                                            cmdline=self._sh_cmdline)
        return self._sh_host

    def _console(self, stream: str, data: bytes) -> None:
        self._received(SEMIHOSTING, data, time.monotonic())

    def _stdin(self, n: int) -> bytes:
        with self._observe:
            data = bytes(self._sh_stdin[:n])
            del self._sh_stdin[:n]
            return data

    def _service(self) -> SemihostingResult | None:
        """止まった位置が BKPT 0xAB なら要求を処理する（_lock の中で）。処理しなかったら None。"""
        if not self._sh_enabled:
            return None
        result = self._semihosting().handle(_PyocdSemihosting(self.target))
        if not result.handled:
            return None
        self._sh_requests += 1
        if result.exited:
            self._sh_exit = {"code": result.exit_code, "reason": result.exit_reason}
            self._record_host(SEMIHOSTING, "exit", time.monotonic(), a=result.exit_code or 0)
        return result

    def _service_in_background(self) -> None:
        """再開した後の見張り（_lock の中で）: BKPT 0xAB（セミホスティング）か、止まらない動作のブレークポイント
        （条件が偽・無視する回数・ログ）で止まっていたら処理して再開する。ほかの理由の停止なら見張りを終える。"""
        target = self.target
        if target.get_state() != Target.State.HALTED:
            return
        if target.get_halt_reason() == Target.HaltReason.BREAKPOINT:
            result = self._service()
            if result is not None and not result.exited:
                target.resume()                                    # BKPT 0xAB は PC を進めてある
                return
            if result is None and self._break_action():
                self.dbg.resume()                                  # ブレークポイントを 1 命令踏み越えて続ける
                return
        self._sh_watch = False

    def _watch_stops(self, on: bool) -> None:
        """target_resume 等の後に on、止める操作で off。"""
        self._sh_watch = on and (self._sh_enabled or bool(self._actions) or bool(self._break_hooks))
        if self._sh_watch:
            self._ensure_poller()

    def _run_until_halt(self, timeout_s: float) -> dict:
        """実行して止まるまで待つ。BKPT 0xAB（セミホスティング）なら処理して続ける。SYS_EXIT なら reason = "exit"。
        待つ間は機器のロックを放す（背景の RTT が読み続ける）。"""
        started = time.monotonic()
        deadline = started + timeout_s
        with self._lock:
            dbg = self.dbg
            self._sh_watch = False                                 # ここで処理する（背景と取り合わない）
        while True:
            stop = dbg.run_until_halt(max(0.0, deadline - time.monotonic()), lock=self._lock)
            with self._lock:
                result = self._service() if stop.reason == "breakpoint" else None
                if result is None and stop.reason == "breakpoint" and self._break_action():
                    continue                                       # 止まらない動作: 踏み越えて続ける（次の run で）
            if result is None or result.exited:
                stop = Stop(stop.reason, stop.pc, stop.where, round(time.monotonic() - started, 4))
                out = self._stop_dict(stop)
                if result is not None:
                    out.update(reason="exit", exit_code=result.exit_code)
                elif stop.reason != "timeout":
                    self._with_fault(out)
                return out

    def _with_fault(self, out: dict) -> dict:
        """故障のハンドラ（・ベクタキャッチ）で止まっていれば、原因の要約（target.fault の）を out に添える。"""
        try:
            with self._lock:
                report = self.target_fault()
        except Exception:                                          # 故障レジスタが読めない: 要約を付けないだけ
            return out
        if report["faulted"]:
            out["fault"] = {"causes": [c["bit"] for c in report["causes"]],
                            "at": (report.get("frame") or {}).get("pc")}
        return out

    # --- 通信の解読 ---------------------------------------------------------------------------

    def la_decode(self, protocol: str, settings: dict | None = None, capture_id: int | None = None,
                  start: int | None = None, end: int | None = None, kinds: list[str] | None = None,
                  errors_only: bool = False, offset: int = 0, limit: int = 200) -> dict:
        """最後の捕捉の通信を解読する。protocol は登録された解読器の名前（la_decoders）。settings はその設定
        （チャンネル等）。積み重ねる解読器は下の解読の設定を下の名前のキーに入れる（例 modbus: {"uart": {...}}）。
        start / end（サンプル）・kinds・errors_only で絞り、offset から limit 件。kinds を省くと解読器の既定の種類
        （SWD は transfer、JTAG は ir・dr・reset 等。無ければ全部）。結果の kinds は絞った種類（null = 全部）、counts は
        絞る前の種類ごとの件数。"""
        with self._lock:
            tr, info = self._capture, self._capture_info
        if tr is None:
            raise NoData("捕捉が無い（la_capture / la_deep_capture を先に）")
        if capture_id is not None and capture_id != info["id"]:
            raise KeyError(f"捕捉 {capture_id} は残っていない（最後の捕捉は {info['id']}）")
        decoding = self._decoding(tr, info["id"], protocol, settings or {})
        items = decoding.annotations if start is None and end is None else \
            decoding.between(start or 0, tr.length if end is None else end)
        if kinds is None and get_decoder(protocol).default_kinds is not None:
            kinds = list(get_decoder(protocol).default_kinds)
        selected = [a for a in items if (kinds is None or a.kind in kinds) and (not errors_only or a.error)]
        return {"protocol": protocol, "params": dict(decoding.params), "capture": info, "total": len(decoding),
                "errors": len(decoding.errors()), "counts": dict(collections.Counter(a.kind for a in decoding)),
                "kinds": kinds, "matched": len(selected), "offset": offset,
                "annotations": [decoding.annotation_dict(a) for a in selected[offset:offset + limit]]}

    def _decoding(self, tr, capture_id: int, protocol: str, settings: dict) -> Decoding:
        """解読（覚えた結果があればそれ）。積み重ねる解読器は下の解読もここを通す（下の結果も覚えて使い回す）。
        鍵に解読器そのもの（Decoder）を含めるので、利用者の解読器を読み直すと前の結果は使わない。"""
        info = get_decoder(protocol)
        key = (capture_id, protocol, info, json.dumps(settings, sort_keys=True, default=repr))
        with self._observe:
            cached = self._decodings.get(key)
        if cached is not None:
            return cached
        try:
            if info.stacked:
                name, lower_settings, own = split_settings(info, settings)
                info.check(own)                                    # 足りない設定を名前で（下の解読の前に）
                decoding = decode_on(protocol, self._decoding(tr, capture_id, name, lower_settings), **own)
            else:
                info.check(settings)
                decoding = info.function(tr, **settings)
        except TypeError as e:                                     # 知らない設定
            raise ValueError(f"{protocol} の設定が合わない: {e}") from None
        if not isinstance(decoding, Decoding):
            raise TypeError(f"解読器 {protocol} が Decoding を返さない")
        with self._observe:
            self._decodings[key] = decoding
            while len(self._decodings) > DECODE_CACHE:
                self._decodings.popitem(last=False)
        self._record_decoding(tr, decoding)
        return decoding

    def _record_decoding(self, tr, decoding: Decoding) -> None:
        """記録中なら注釈を共通の時間軸へ（捕捉のトリガのサンプルがトリガ時刻に来るように置く）。"""
        rec = self._recorder
        if rec is None or not decoding.annotations:
            return
        tps = tr.sample_period_s * rec.timebase_hz
        base = tr.trigger_time - round(tr.trigger * tps)
        rows = []
        for a in decoding:
            value = a.value if a.value is not None and -(1 << 63) <= a.value < 1 << 63 else 0
            body = {"protocol": decoding.protocol, "text": a.text, "value": a.value, "error": a.error,
                    "fields": dict(a.fields) if a.fields is not None else None}
            rows.append(("annotation", f"{decoding.protocol}.{a.kind}", base + round(a.start * tps), value, 0, body,
                         base + round(a.end * tps)))
        rec.add_many(rows)
