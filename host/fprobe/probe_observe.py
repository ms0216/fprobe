"""Probe の道具: 止めずに見る道具の核（受信を貯める背景のスレッド・受け取り・周期の仕事）。

Probe（fprobe.probe）が API の名前を委ねるサービス（fprobe.probe_service。ホストから使う名前は NEEDS）。Probe の _lock・_debug・_recorder を使う。
機器に触るのは必ず _lock の中。この核の上に、次の領域が載る（それぞれ単独のサービス）:

- RTT（probe_rtt）: 制御ブロックの探索・読み書き・サンプラで起こす・速さの計測。
- SWO の ITM の解読（probe_swo_itm）: swo_decode・swo_text と、解読したパケットの受け口。
- セミホスティングと止まるまでの実行（probe_semihosting）: BKPT 0xAB の処理と run_until_halt。
- 捕捉の通信の解読（probe_decode の la_decode）。

核の持つもの:
- 背景のスレッド（_poll_loop）: 周期ごとに RTT の読み（_rtt_poll）・セミホスティングの見張り（_sh_poll）・周期の仕事
  （_add_job。ウォッチ等が足す）・生の流れ（SWO・UART。_tap が足すと背景で読む）を _lock の中で回す。やることが無ければ終わる。
- 貯め場（_logs: チャンネル・"semihosting"・"swo"・"uart" ごとの ByteLog）と、受け取り（_received）: 貯め、受け口（_tap）に渡し、
  記録中（_recorder）なら RTT・セミホスティングの行・データを共通の時間軸の記録に入れる（_record_received・_record_host）。

仕様: docs/superpowers/specs/2026-10-17-f1-rtt-itm-semihosting-design.md、計画: docs/superpowers/plans/2026-10-17-api-integration-a-d.md
"""

import threading
import time

from .observe_log import LOG_LIMIT, ByteLog, _incomplete_utf8_tail  # noqa: F401（旧名の再公開: 端末・SWO 受信・試験が読む）
from .probe_service import HostView
from .probe_swo_itm import _exception_name  # noqa: F401（旧名の再公開: 試験が probe_observe から取る）

__all__ = ["ByteLog", "ObserveService", "SEMIHOSTING"]

SEMIHOSTING = "semihosting"            # セミホスティングの出力の貯め場の名前（rtt_read の channel）
RAW_SOURCES = ("swo", "uart")          # 受けたバイトをそのまま貯める流れ（読む口: swo_read・swo_decode・uart_read）


class ObserveService:
    """止めずに見る道具の核: 受信を貯める背景のスレッドと受け取り（Probe のサービス）。"""

    API = ()
    # ほかの領域（Probe の委譲）・試験に出す名前
    SHARED = ("_add_job", "_closing", "_collect", "_ensure_poller", "_fetch", "_job_errors", "_jobs", "_logs", "_observe",
              "_partial", "_pump_errors", "_pumps", "_received", "_record_host", "_remove_job", "_stamp",
              "_stop_observing", "_swo_on", "_tap", "_untap", "poll_s")
    # ホスト（Probe の核と、ほかの領域）から使う名前
    NEEDS = ("_debug", "_lock", "_recorder", "_rtt", "_rtt_listeners", "_rtt_poll", "_rtt_text", "_sh_poll", "_sh_watch",
             "_uart_fetch")

    def __init__(self, host):
        self._host = HostView(host, self.NEEDS, type(self).__name__)
        self._observe = threading.Condition()                      # 貯め場・行の読み書き（機器の _lock とは別）
        self._logs: dict[int | str, ByteLog] = {}
        self._partial: dict[int | str, bytearray] = {}             # 記録の行に組み立て中のバイト
        self._poller: threading.Thread | None = None
        self._closing = threading.Event()
        self.poll_s = 0.01
        # 生の流れ（SWO・UART）: 背景で読む（_pumps）と、受けたバイトを渡す先（_taps。TCP の口・ファイル）
        self._taps: dict[int | str, list] = {}
        self._pumps: set[str] = set()
        self._pump_errors: dict[str, str] = {}
        # 周期の仕事（ほかの道具が足す。名前 → [周期, 次の時刻, 関数]）と、その最後の誤り
        self._jobs: dict[str, list] = {}
        self._job_errors: dict[str, str] = {}
        self._swo_on = False

    # --- 背景のスレッド ----------------------------------------------------------------------

    def _ensure_poller(self) -> None:
        with self._observe:
            if self._poller is None and not self._closing.is_set():
                self._poller = threading.Thread(target=self._poll_loop, name="fprobe-observe", daemon=True)
                self._poller.start()

    def _poll_loop(self) -> None:
        while not self._closing.wait(self.poll_s):
            with self._observe:
                if self._host._rtt is None and not self._host._sh_watch and not self._pumps and not self._jobs:
                    self._poller = None                            # やることが無い: 終わる（_ensure_poller で起こす）
                    return
            self._poll_once()

    def _poll_once(self) -> None:
        with self._host._lock:
            self._host._rtt_poll()                                 # RTT の読み（切断されていれば止める）
            self._host._sh_poll()                                  # セミホスティング・動作のブレークポイントの見張り
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
            self._host._uart_fetch()

    def _swo_fetch(self) -> None:
        if self._swo_on and self._host._debug is not None:
            chunk = self._host._debug.probe.swo_read()
            if chunk:
                self._received("swo", bytes(chunk), time.monotonic())

    def _collect(self, source: str, timeout_s: float) -> bytes:
        """timeout_s の間、source を読み、前回の続きから貯まった分を返す（背景で読んでいれば、その分も）。"""
        deadline = time.monotonic() + timeout_s
        while True:
            with self._host._lock:
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
        for listener in list(self._host._rtt_listeners):
            listener(key, data, host_s)
        if self._host._recorder is not None:
            self._record_received(key, data, host_s)

    def _stamp(self, host_s: float) -> tuple[float, int | None]:
        """受けた時刻（time.monotonic）→ (壁時計の秒, 記録中ならタイムベースの時刻)。機器には触らない。"""
        wall = time.time() - (time.monotonic() - host_s)
        rec = self._host._recorder
        if rec is None:
            return wall, None
        try:
            return wall, rec.clock.to_ticks(host_s)[0]
        except RuntimeError:
            return wall, None                                      # 時計の対応付けの前（記録を始めた直後）

    def _record_received(self, key: int | str, data: bytes, host_s: float) -> None:
        if key == SEMIHOSTING or key in self._host._rtt_text:
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
        """記録中なら出来事を入れ、記録のタイムベースに写した時刻を返す（記録していない・写せなければ None）。
        host_s は PC が対象の出来事を受けた・読んだ時刻（SWO の ITM パケット・RTT・セミホスティング・ヒープ・
        トレースポイント・ログのブレークポイント）なので、本体の印は time_source = "host_received"（対象の出来事は
        それより前。どれだけ前かは分からず、不確かさは PC の時計の写しの分だけ）。"""
        rec = self._host._recorder
        if rec is None:
            return None
        try:
            return rec.add_host(stream, kind, host_s, time_source="host_received", **fields)
        except RuntimeError:
            return None                                            # 時計の対応付けの前（記録を始めた直後）
