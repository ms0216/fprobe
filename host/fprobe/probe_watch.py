"""Probe の道具: 変数のライブ観測（F4。ターゲットを止めずに、FPGA の DAP サンプラが周期的に読む）と、その拡充（U43）。

Probe（fprobe.probe）が API の名前を委ねるサービス（fprobe.probe_service。ホストから使う名前は NEEDS）。Probe の _lock・session・target・_symbols・_recorder と、DAP サンプラの
持ち主（WindowService._claim_sampler）、背景の周期の仕事（ObserveService._add_job）を使う。

- watch_set で、変数の名前・C の式・番地を、ELF の DWARF で番地と大きさにし、サンプラのか所にする。値は DAP の
  イベント記録（種類 7）に入り、記録中なら stream "sample" にも入る。値は変数の全部の語がそろった時点で 1 つの値。
  誤った式（名前が無い・メモリ上に無い等）は rejected に印を付けて外し、残りを取る（全部が誤りなら始めない）。
  サンプラの巡回・誤り・追いつかなかった周期の数は FPGA の起動からの数なので、watch_set の時点を起点に引いて出す。
- ポインタをたどる式（"head->pos.x"・"*p"・"buf[i]" 等。番地がメモリの値で決まるもの）は、FPGA が決まった番地を
  読む方式では追えないので、ホストが chase_rate_hz ごとに式を評価し直して読む（method = host。周期ごとに番地を
  引き直す。速さはデバッグポートの往復で決まり、時刻はデバイスのタイムベースで付ける）。
- watch_trigger / watch_capture: 条件（変数の値・クロストリガ）で取得を始める・止める、プリトリガ・ポスト
  （fprobe.watch_trigger）。サンプラの語はイベント記録を前回の続きから読み（EventCursor）、プリトリガの分は arm した
  時点のリングから補う。
- watch_buffer_*: ターゲット側のバッファ（配列 + 書いた数）を吸い出す方式（fprobe.watch_buffer）。
- watch_write: 走行中の書き込み（止めない。大域・ポインタをたどる式・番地）。
時刻はすべてイベント記録と同じ 48 ビットのタイムベースのカウント（watch_series の time_us と同じ軸）。
仕様: docs/superpowers/specs/2026-10-17-f4-live-sampler-design.md

領域ごとの分け方: ここには watch_set / watch_stop / watch_status とポインタ追跡・統計の背景の仕事を置く。値の列と測定は
probe_watch_series.py、条件つきの取得は probe_watch_trigger.py、走行中の書き込みは probe_watch_write.py、ターゲット側のバッファは
probe_watch_buffer.py（いずれも WatchService に混ぜ込む部品）。
"""

import collections
import dataclasses
import time

from .debug import Debugger, PyocdAccess
from .dwarf.cexpr import evaluate
from .dwarf.typesys import type_name
from .events import KIND_SAMPLE, EventCursor, EventLog
from .probe_service import HostView
from .probe_watch_buffer import BUFFER_LIMIT, WatchBufferMixin, _Buffer  # noqa: F401  (旧名を保つ)
from .probe_watch_series import READ_LIMIT, WatchSeriesMixin, _window_us  # noqa: F401  (旧名を保つ)
from .probe_watch_trigger import PREFILL_LIMIT, WatchTriggerMixin, _Trigger  # noqa: F401  (旧名を保つ)
from .probe_watch_write import _RAW, WatchWriteMixin  # noqa: F401  (_RAW は試験が読む)
from .sampler import Sampler
from .watch_stats import ValueStats, expression_facts, measure, pair_latest, rates, xy_result  # noqa: F401  (再公開)
from .watch_value import _Assembler, _frame, _NeedsMemory, _no_memory, _read_value, _Watched  # noqa: F401  (_read_value は試験が読む)

__all__ = ["ValueStats", "WatchService", "measure", "pair_latest", "rates", "xy_result"]

CHASE_LIMIT = 100_000                  # ホストが読んだ値を式ごとに残す数


def _counts(status: dict) -> dict:
    """サンプラの状態の数（巡回・誤り・追いつかなかった周期。32 ビットで回り込む）。"""
    return {k: status[k] for k in ("rounds", "errors", "overruns")}


class WatchService(WatchSeriesMixin, WatchTriggerMixin, WatchWriteMixin, WatchBufferMixin):
    API = ("watch_buffer_read", "watch_buffer_start", "watch_buffer_stop", "watch_capture", "watch_measure",
           "watch_series", "watch_set", "watch_status", "watch_stop", "watch_trigger", "watch_values", "watch_write",
           "watch_xy")
    # ほかの領域（Probe の委譲）・試験に出す名前
    SHARED = ("_capture_dict", "_chase_rate", "_describe", "_stats_once", "_watch_buffer", "_watch_client",
              "_watch_rate", "_watch_rejected", "_watch_running", "_watch_started", "_watch_target", "_watch_targets",
              "_watched", "_write_bits")
    # ホスト（Probe の核と、ほかの領域）から使う名前
    NEEDS = ("_add_job", "_claim_sampler", "_job_errors", "_linux_view", "_lock", "_name_watches", "_recorder",
             "_release_sampler", "_remove_job", "_sampler_user", "_symbols", "session", "target")

    def __init__(self, host):
        self._host = HostView(host, self.NEEDS, type(self).__name__)
        self._watched: list[_Watched] = []
        self._watch_running = False                        # watch_set で動き、watch_stop で false（一覧は残る）
        self._watch_rejected: list[dict] = []
        self._watch_base: dict | None = None               # watch_set の時点のサンプラの数（起点）
        self._watch_started = 0.0
        self._watch_client: dict | None = None             # 最後に watch_set した呼び出し元（デーモンが渡す事実）
        self._watch_rate = 0.0
        self._chase_rate = 0.0
        self._chase_series: dict[str, collections.deque] = {}
        self._chase_total: dict[str, int] = {}             # 式ごとに足した値の数（deque の上限で捨てた分が分かる）
        self._chase_errors: dict[str, str] = {}
        self._watch_trigger: _Trigger | None = None
        self._watch_buffer: _Buffer | None = None
        self._watch_stats: dict[str, ValueStats] = {}       # watch_set からの値の統計（watch_status の stats）
        self._stats_cursor: EventCursor | None = None       # 統計のためにイベント記録を続きから読む
        self._stats_assembler: _Assembler | None = None

    def _watch_info(self):
        return Debugger(None, self._host._symbols).debug_info() if self._host._symbols else None

    def _watch_target(self, expression: str) -> _Watched:
        raw = _RAW.fullmatch(expression.strip())
        if raw:
            return _Watched(expression, int(raw[1], 16), int(raw[2] or 4))   # 大きさが無ければサンプラの読みの単位（1 語）
        info = self._watch_info()
        if info is None:
            if self._host._symbols is None:
                raise RuntimeError("ELF が読み込まれていない（elf_load を先に。番地なら \"0x20000000:4\" の形で）")
            s = self._host._symbols.variable(expression)                        # DWARF が無い: シンボルの番地と大きさ
            if not s.size:
                raise ValueError(f"{expression} のシンボルに大きさが無い（\"0x{s.address:08X}:4\" の形で大きさを付ける）")
            return _Watched(expression, s.address, s.size)
        try:
            v = evaluate(expression, info, _frame(_no_memory))           # 大域だけ（メモリは読まない）
        except _NeedsMemory:
            with self._host._lock:                                              # ポインタをたどる: いまの値で形を確かめる
                v = evaluate(expression, info, _frame(PyocdAccess(self._host.target).read_memory))
            chase = True
        else:
            chase = False
        if v.address is None or v.type.size is None:
            raise ValueError(f"{expression!r} はメモリ上の値ではない（レジスタ・定数・不完全型）")
        return _Watched(expression, None if chase else v.address, v.type.size, v.type, v.bit_offset, v.bit_size,
                        chase)

    def watch_set(self, expressions: list[str], rate_hz: float = 1000.0, ap: int = 0, chase_rate_hz: float = 100.0,
                  jitter: int = 0, names: list[str] | None = None, caller: dict | None = None) -> dict:
        """変数を止めずに周期的に読む（FPGA の DAP サンプラ）。expressions: 変数名・C の式・番地（"0x20000000" か
        "0x20000000:バイト数"）。決まった番地の式はサンプラが rate_hz で読む（合わせて 8 か所・各 16 語まで）。ポインタを
        たどる式はホストが chase_rate_hz ごとに番地を引き直して読む（method = host）。jitter（0〜15）: サンプラの周期に
        ゆらぎを足し、周期的な処理との噛み合い（エイリアシング）を防ぐ（fprobe.sampler.Sampler.start）。
        names: 式ごとの名前（値・時間軸の段の名前。番地の式に「誤り」等。記録中なら記録の情報にも残す）。
        caller: デーモンが渡す呼び出し元 {id, name, role}（引数の仕様には出ない）。watch_status・sampler_status の
        client に事実として残す。"""
        watched, rejected = self._watch_labelled(expressions, names, chase_rate_hz)
        sampled = [w for w in watched if not w.chase]
        slots = []
        with self._host._lock:
            device = self._host.session.device
            if sampled:
                self._host._claim_sampler("watch", caller)
                slots = Sampler(device).configure([(w.address, w.size) for w in sampled], 1.0 / rate_hz, ap)
                if self._host._recorder is None:
                    EventLog(device).ensure_started()                     # 記録中は Collector が始めている
                cursor = EventCursor(EventLog(device))                     # 統計: 始めてからの値をすべて
                Sampler(device).start(jitter)
                self._watch_base = _counts(Sampler(device).status())
            elif self._host._sampler_user == "watch":                           # 前の watch_set のサンプラ
                Sampler(device).stop()
                self._host._release_sampler("watch")
            self._end_watch_jobs()
            self._watched, self._watch_rate, self._chase_rate = watched, rate_hz, chase_rate_hz
            self._watch_running = bool(watched)
            if names is not None:                        # 記録の段の名前（観測を止めた後・後から開いた記録でも）
                self._host._name_watches({w.address: w.name for w in sampled if w.address is not None})
            self._watch_rejected, self._watch_started = rejected, time.monotonic()
            self._watch_client = dict(caller) if caller is not None else None
            if not sampled:
                self._watch_base = None
            self._start_watch_readers(watched, sampled, cursor if sampled else None, chase_rate_hz)
        return {"watches": [self._describe(w) for w in watched], "rate_hz": rate_hz, "chase_rate_hz": chase_rate_hz,
                "slots": [{"address": f"0x{a:08X}", "words": n} for a, n in slots], "rejected": rejected}

    def _watch_labelled(self, expressions: list[str], names: list[str] | None,
                        chase_rate_hz: float) -> tuple[list[_Watched], list[dict]]:
        """watch_set の引数を確かめ、式を読む対象にする（names があれば名前を付け替える）。"""
        if not 0 < chase_rate_hz <= 10_000:
            raise ValueError("chase_rate_hz は 0 より大きく 10000 以下")
        if names is not None and (len(names) != len(expressions) or len(set(names)) != len(names)
                                  or not all(isinstance(n, str) and n.strip() for n in names)):
            raise ValueError("names は式と同じ数の、重ならない空でない名前")
        watched, rejected = self._watch_targets(expressions)
        if names is not None:
            label = dict(zip(expressions, (n.strip() for n in names)))
            watched = [dataclasses.replace(w, name=label.get(w.name, w.name)) for w in watched]
        return watched, rejected

    def _start_watch_readers(self, watched: list[_Watched], sampled: list[_Watched], cursor,
                             chase_rate_hz: float) -> None:
        """統計とポインタ追跡の状態を作り、読む仕事を足す（_lock の中で）。cursor は sampled があるときの統計の読み手。"""
        self._chase_series = {w.name: collections.deque(maxlen=CHASE_LIMIT) for w in watched if w.chase}
        self._chase_total = {name: 0 for name in self._chase_series}
        self._chase_errors = {}
        self._watch_stats = {w.name: ValueStats() for w in watched}
        self._stats_cursor = cursor
        self._stats_assembler = _Assembler(watched) if sampled else None
        if sampled:
            self._host._add_job("watch.stats", 0.0, self._stats_once)
        if self._chase_series:
            self._host._add_job("watch.chase", 1.0 / chase_rate_hz, self._chase_once)

    def _watch_targets(self, expressions: list[str]) -> tuple[list[_Watched], list[dict]]:
        """式ごとにか所にする。誤った式は rejected（{"expression", "error"}）に外す。全部が誤りなら誤り（1 つなら
        その誤りのまま、複数なら式ごとの誤りを並べた ValueError）。"""
        watched, rejected, errors = [], [], []
        for e in expressions:
            try:
                watched.append(self._watch_target(e))
            except Exception as error:                                    # 名前が無い・メモリ上に無い・ELF が無い
                errors.append(error)
                rejected.append({"expression": e, "error": str(error.args[0]) if error.args else type(error).__name__})
        if expressions and not watched:
            if len(errors) == 1:
                raise errors[0]
            raise ValueError("どの式も読めない: " + "; ".join(f"{r['expression']}: {r['error']}" for r in rejected))
        return watched, rejected

    @staticmethod
    def _describe(w: _Watched) -> dict:
        return {"name": w.name, "address": None if w.address is None else f"0x{w.address:08X}", "size": w.size,
                "type": type_name(w.type) if w.type is not None else None, "method": "host" if w.chase else "sampler"}

    def _end_watch_jobs(self) -> None:
        """ホストの読みと条件つきの取得を終える（_lock の中で）。"""
        self._host._remove_job("watch.chase")
        self._host._remove_job("watch.drain")
        self._host._remove_job("watch.stats")
        trigger = self._watch_trigger
        if trigger is not None and not trigger.capture.done:
            c = trigger.capture
            c.cancel(c.samples[-1][0] if c.samples else c.t_start or 0)

    def watch_stop(self) -> dict:
        """周期的な読みを止める（読んだ値はイベント記録・記録に残る。条件つきの取得は打ち切る）。"""
        with self._host._lock:
            self._end_watch_jobs()
            self._watch_running = False                         # 一覧は残す（値を読む）が、動いてはいない
            if self._host._sampler_user not in (None, "watch"):
                return {"stopped": True}                       # サンプラはほかの道具のもの: 触らない（止めてあるものは止まっている）
            if self._host._sampler_user == "watch" or not any(w.chase for w in self._watched):
                sampler = Sampler(self._host.session.device)
                sampler.stop()
                self._host._release_sampler("watch")
                return sampler.status()
            return {"stopped": True}

    def watch_status(self, expressions: list[str] | None = None) -> dict:
        """観測している変数・周期と、サンプラの状態（巡回の数・誤り・飛ばした周期 = watch_set からの数。FPGA の起動からの
        数は device。最後の ACK。held = 線を持っている〔B17: ホストの次の転送の前に SELECT・CSW・TAR を戻す〕）、
        watch_set で外した誤った式（rejected）、始めてからの秒（since_start_s）、ホストの読みの誤り、条件つきの取得の状態、
        client（最後に watch_set した呼び出し元 {id, name, role}。デーモンが渡した事実。分からなければ null）、
        その数からの loss_ratio・measured_hz（rates）。stats = 変数ごとの watch_set から読んだ値の統計（ValueStats。
        サンプラの値はイベント記録を続きから読んで数え、読めずに一周された・上限を超えたイベントの数は
        stats_lost_events。ホストの読みの値は読むたびに数える）。"""
        with self._host._lock:
            status = Sampler(self._host.session.device).status() if any(not w.chase for w in self._watched) else {}
        if status and self._watch_base is not None:                       # 起動からの数 → watch_set からの数
            device = _counts(status)
            status.update({k: (v - self._watch_base[k]) & 0xFFFFFFFF for k, v in device.items()}, device=device)
        trigger = self._watch_trigger
        cursor = self._stats_cursor
        out = {"watches": [self._describe(w) for w in self._watched], "rejected": list(self._watch_rejected),
               "stats": {name: st.as_dict() for name, st in self._watch_stats.items()},
               "stats_lost_events": None if cursor is None else cursor.lost,
                "running": self._watch_running,         # watch_stop の後は false（一覧は残る）
                "since_start_s": time.monotonic() - self._watch_started if self._watched else None,
                "rate_hz": self._watch_rate, "client": self._watch_client,
                "chase_rate_hz": self._chase_rate, "chase_errors": dict(self._chase_errors),
                "capture": None if trigger is None else {"state": trigger.capture.state,
                                                         "reason": trigger.capture.reason},
                **status}
        return {**out, **rates(out), "expressions": expression_facts(expressions, out["watches"], out["rejected"])}

    # --- ポインタをたどる式（ホストの読み） -------------------------------------------------------

    def _chase_once(self) -> None:
        """背景の仕事（_lock の中で）: ポインタをたどる式を評価し直して読む。"""
        info = self._watch_info()
        access = PyocdAccess(self._host.target)
        frame = _frame(access.read_memory)
        t = self._host.session.device.time()
        trigger = self._watch_trigger
        for w in self._watched:
            if not w.chase:
                continue
            try:
                value = _read_value(evaluate(w.name, info, frame), access.read_memory)
            except Exception as e:                                         # NULL を指した等: その周期は値なし
                self._chase_errors[w.name] = f"{type(e).__name__}: {e}"
                continue
            self._chase_errors.pop(w.name, None)
            self._chase_series[w.name].append((t, value))
            self._watch_stats[w.name].add(value)
            self._chase_total[w.name] = self._chase_total.get(w.name, 0) + 1
            if trigger is not None and trigger.capture.state != "done":
                trigger.capture.feed(t, w.name, value)

    def _stats_once(self) -> None:
        """背景の仕事（_lock の中で）: イベント記録の続きを読み、サンプラの値を統計に足す（読めなかった分は
        cursor.lost。watch_status の stats_lost_events）。"""
        for e in self._stats_cursor.take():
            if e.kind == KIND_SAMPLE and e.ok:
                for name, value in self._stats_assembler.feed(e.address, e.data):
                    self._watch_stats[name].add(value)
