"""Probe の道具: 変数のライブ観測（F4。ターゲットを止めずに、FPGA の DAP サンプラが周期的に読む）と、その拡充（U43）。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う mixin。Probe の _lock・session・target・_symbols・_recorder と、DAP サンプラの
持ち主（WindowTools._claim_sampler）、背景の周期の仕事（ObserveTools._add_job）を使う。

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
"""

import collections
import dataclasses
import re
import time
from dataclasses import dataclass, field

from .collector import unwrap_events
from .debug import Debugger, PyocdAccess
from .dwarf.cexpr import Value, evaluate
from .dwarf.info import FrameView
from .dwarf.location import NotAvailable
from .dwarf.typesys import ArrayType, BaseType, CType, EnumType, PointerType, strip, type_name
from .dwarf.values import decode, decode_bits, encode, scalar
from .events import INJECT_KINDS, KIND_SAMPLE, EventCursor, EventLog, resolve
from .sampler import Sampler
from .watch_buffer import TargetRing
from .watch_trigger import Capture, Condition

__all__ = ["ValueStats", "WatchTools", "measure", "pair_latest", "rates", "xy_result"]

READ_LIMIT = 4096                      # 1 回に読むイベント記録の件数（最新から）
CHASE_LIMIT = 100_000                  # ホストが読んだ値を式ごとに残す数
BUFFER_LIMIT = 200_000                 # 吸い出した要素を残す数
PREFILL_LIMIT = 1 << 16                # プリトリガのために arm の時点でリングから読むイベントの上限
_RAW = re.compile(r"(0x[0-9a-fA-F]+)(?::(\d+))?")


def _counts(status: dict) -> dict:
    """サンプラの状態の数（巡回・誤り・追いつかなかった周期。32 ビットで回り込む）。"""
    return {k: status[k] for k in ("rounds", "errors", "overruns")}


def pair_latest(xs: list, ys: list, max_dt: float | None = None) -> tuple[list[tuple], int, int]:
    """XY の組: xs の各 (時刻, 値) に、その時刻以前で最新の ys の (時刻, 値) を組む（2 つは同時に測っていないので、
    両方の時刻を残す）。返り値 = ([(x の時刻, x, y の時刻, y)], それ以前に y が無い x の数, 時刻の差が max_dt を
    超えて外した数)。max_dt は使う側が渡すときだけ（無ければ外さない）。どちらも古い順。"""
    pairs, unpaired, excluded = [], 0, 0
    j = -1
    for tx, vx in xs:
        while j + 1 < len(ys) and ys[j + 1][0] <= tx:
            j += 1
        if j < 0:
            unpaired += 1
        elif max_dt is not None and tx - ys[j][0] > max_dt:
            excluded += 1
        else:
            pairs.append((tx, vx, ys[j][0], ys[j][1]))
    return pairs, unpaired, excluded


def xy_result(x: str, y: str, pairs: list[tuple], unpaired: int, excluded: int, max_dt_us: float | None,
              us: float, from_us: dict) -> dict:
    """watch_xy の応答（時刻は µs。us = 1 カウントの µs）。"""
    return {"x": x, "y": y,
            "points": [{"x_time_us": tx * us, "x": vx, "y_time_us": ty * us, "y": vy} for tx, vx, ty, vy in pairs],
            "dt_max_us": max(((tx - ty) * us for tx, _, ty, _ in pairs), default=None),
            "unpaired": unpaired, "excluded": excluded, "max_dt_us": max_dt_us, "from_us": from_us}


def _number(value: dict) -> int | float | None:
    """値の数（SCALAR_KINDS の value。真偽は 0・1）。数でない種類・浮動小数の NaN・無限大は None。"""
    v = scalar(value)
    return v if isinstance(v, (int, float)) else None


class ValueStats:
    """1 つの変数の、watch_set から読んだ値の統計（数・最小・最大・平均）。数でない値（構造体・配列等）は数えず
    not_numeric に。int・float・bool・enum（SCALAR_KINDS）の数を数える（真偽は 0・1、NaN・無限大は数えない）。"""

    def __init__(self):
        self.count = self.not_numeric = 0
        self.min = self.max = None
        self._sum = 0.0

    def add(self, value: object) -> None:
        value = _number(value)
        if value is None:
            self.not_numeric += 1
            return
        self.count += 1
        self._sum += value
        self.min = value if self.min is None or value < self.min else self.min
        self.max = value if self.max is None or value > self.max else self.max

    def as_dict(self) -> dict:
        return {"count": self.count, "min": self.min, "max": self.max,
                "mean": self._sum / self.count if self.count else None, "not_numeric": self.not_numeric}


def measure(series: dict[str, list[tuple[float, object]]], a_us: float, b_us: float) -> dict:
    """カーソル A・B の測定（series = 変数ごとの (時刻 µs, 値) の古い順）。a・b = その時刻以前で最後の値と、その値の
    時刻（測った時刻とは別。無ければ None）、delta = b − a（どちらも数のとき）、stats = A〜B（両端を含む）の値の
    ValueStats、first_us = 持っている最初の値の時刻（それより前は持っていない）。"""
    lo, hi = min(a_us, b_us), max(a_us, b_us)

    def at(points: list, t: float) -> dict | None:
        before = [p for p in points if p[0] <= t]
        return {"time_us": before[-1][0], "value": before[-1][1]} if before else None

    rows = []
    for name, points in series.items():
        a, b = at(points, a_us), at(points, b_us)
        st = ValueStats()
        for t, v in points:
            if lo <= t <= hi:
                st.add(v)
        na, nb = (None if x is None else _number(x["value"]) for x in (a, b))
        rows.append({"name": name, "a": a, "b": b, "delta": nb - na if na is not None and nb is not None else None,
                     "stats": st.as_dict(), "first_us": points[0][0] if points else None})
    return {"a_us": a_us, "b_us": b_us, "dt_us": b_us - a_us, "rows": rows}


def rates(status: dict) -> dict:
    """watch_status の数から: loss_ratio = 飛ばした周期 ÷ (巡回 + 飛ばした周期)、measured_hz = 巡回 ÷ 始めてからの秒。
    数が無い・0 なら None。"""
    rounds, overruns, since = status.get("rounds"), status.get("overruns"), status.get("since_start_s")
    total = (rounds or 0) + (overruns or 0)
    return {"loss_ratio": (overruns or 0) / total if rounds is not None and total else None,
            "measured_hz": rounds / since if rounds is not None and since else None}


class _NeedsMemory(Exception):
    """式の番地を決めるのにメモリの値が要る（ポインタをたどる式）。"""


def _no_memory(address: int, length: int) -> bytes:
    raise _NeedsMemory()


def _no_register(number: int) -> int:
    raise NotAvailable("走行中の観測ではレジスタ（ローカル変数）は使えない（大域・静的の式にする）")


def _frame(read_memory) -> FrameView:
    """大域だけを引くフレーム（レジスタは使えない）。"""
    return FrameView(None, read_memory, _no_register)


def _read_value(v: Value, read_memory) -> dict:
    size = (v.bit_offset + v.bit_size + 7) // 8 if v.bit_size is not None else v.type.size
    data = read_memory(v.address, size)
    return decode_bits(v.type, data, v.bit_offset, v.bit_size) if v.bit_size is not None else decode(v.type, data)


@dataclass(frozen=True)
class _Watched:
    name: str
    address: int | None                # chase なら None（周期ごとに式から引く）
    size: int
    type: CType | None = None
    bit_offset: int | None = None
    bit_size: int | None = None
    chase: bool = False

    @property
    def words(self) -> range:
        return range(self.address // 4 * 4, self.address + self.size, 4)

    def value(self, words: dict[int, int]) -> dict:
        data = b"".join(words[w].to_bytes(4, "little") for w in self.words)
        data = data[self.address % 4:self.address % 4 + self.size]
        if self.type is None:                          # 番地の式（型が無い）: 符号なし整数（fprobe.dwarf.values の int の形）
            return {"kind": "int", "value": int.from_bytes(data, "little")}
        if self.bit_size is not None:
            return decode_bits(self.type, data, self.bit_offset, self.bit_size)
        return decode(self.type, data)


class _Assembler:
    """サンプラの語（番地, 値）から変数の値を組み立てる。語は巡回の順に読むので、変数の最後の語が来た時点で
    同じ巡回の値がそろう。"""

    def __init__(self, watched: list[_Watched]):
        self.watched = [w for w in watched if not w.chase]
        self.words: dict[int, int] = {}
        self.last = {w.name: w.words[-1] for w in self.watched}

    def feed(self, address: int, value: int) -> list[tuple[str, dict]]:
        self.words[address] = value
        return [(w.name, w.value(self.words)) for w in self.watched
                if address == self.last[w.name] and all(x in self.words for x in w.words)]


@dataclass
class _Trigger:
    capture: Capture
    cursor: EventCursor | None
    assembler: _Assembler
    hz: float
    skip_until: int = -1                                  # プリトリガで補った最後の時刻（カーソルの重複を捨てる）
    started: float = field(default_factory=time.monotonic)


@dataclass
class _Buffer:
    ring: TargetRing
    array: str
    head: str
    rate_hz: float
    samples: collections.deque = field(default_factory=lambda: collections.deque(maxlen=BUFFER_LIMIT))
    cursor: int = 0                                       # 既定の読み手の次の通し番号


class WatchTools:
    def _init_watch(self) -> None:
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
        return Debugger(None, self._symbols).debug_info() if self._symbols else None

    def _watch_target(self, expression: str) -> _Watched:
        raw = _RAW.fullmatch(expression.strip())
        if raw:
            return _Watched(expression, int(raw[1], 16), int(raw[2] or 4))   # 大きさが無ければサンプラの読みの単位（1 語）
        info = self._watch_info()
        if info is None:
            if self._symbols is None:
                raise RuntimeError("ELF が読み込まれていない（elf_load を先に。番地なら \"0x20000000:4\" の形で）")
            s = self._symbols.variable(expression)                        # DWARF が無い: シンボルの番地と大きさ
            if not s.size:
                raise ValueError(f"{expression} のシンボルに大きさが無い（\"0x{s.address:08X}:4\" の形で大きさを付ける）")
            return _Watched(expression, s.address, s.size)
        try:
            v = evaluate(expression, info, _frame(_no_memory))           # 大域だけ（メモリは読まない）
        except _NeedsMemory:
            with self._lock:                                              # ポインタをたどる: いまの値で形を確かめる
                v = evaluate(expression, info, _frame(PyocdAccess(self.target).read_memory))
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
        with self._lock:
            device = self.session.device
            if sampled:
                self._claim_sampler("watch", caller)
                slots = Sampler(device).configure([(w.address, w.size) for w in sampled], 1.0 / rate_hz, ap)
                if self._recorder is None:
                    EventLog(device).ensure_started()                     # 記録中は Collector が始めている
                cursor = EventCursor(EventLog(device))                     # 統計: 始めてからの値をすべて
                Sampler(device).start(jitter)
                self._watch_base = _counts(Sampler(device).status())
            elif self._sampler_user == "watch":                           # 前の watch_set のサンプラ
                Sampler(device).stop()
                self._release_sampler("watch")
            self._end_watch_jobs()
            self._watched, self._watch_rate, self._chase_rate = watched, rate_hz, chase_rate_hz
            self._watch_running = bool(watched)
            if names is not None:                        # 記録の段の名前（観測を止めた後・後から開いた記録でも）
                self._name_watches({w.address: w.name for w in sampled if w.address is not None})
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
            self._add_job("watch.stats", 0.0, self._stats_once)
        if self._chase_series:
            self._add_job("watch.chase", 1.0 / chase_rate_hz, self._chase_once)

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
        self._remove_job("watch.chase")
        self._remove_job("watch.drain")
        self._remove_job("watch.stats")
        trigger = self._watch_trigger
        if trigger is not None and not trigger.capture.done:
            c = trigger.capture
            c.cancel(c.samples[-1][0] if c.samples else c.t_start or 0)

    def watch_stop(self) -> dict:
        """周期的な読みを止める（読んだ値はイベント記録・記録に残る。条件つきの取得は打ち切る）。"""
        with self._lock:
            self._end_watch_jobs()
            self._watch_running = False                         # 一覧は残す（値を読む）が、動いてはいない
            if self._sampler_user not in (None, "watch"):
                return {"stopped": True}                       # サンプラはほかの道具のもの: 触らない（止めてあるものは止まっている）
            if self._sampler_user == "watch" or not any(w.chase for w in self._watched):
                sampler = Sampler(self.session.device)
                sampler.stop()
                self._release_sampler("watch")
                return sampler.status()
            return {"stopped": True}

    def watch_status(self) -> dict:
        """観測している変数・周期と、サンプラの状態（巡回の数・誤り・飛ばした周期 = watch_set からの数。FPGA の起動からの
        数は device。最後の ACK。held = 線を持っている〔B17: ホストの次の転送の前に SELECT・CSW・TAR を戻す〕）、
        watch_set で外した誤った式（rejected）、始めてからの秒（since_start_s）、ホストの読みの誤り、条件つきの取得の状態、
        client（最後に watch_set した呼び出し元 {id, name, role}。デーモンが渡した事実。分からなければ null）、
        その数からの loss_ratio・measured_hz（rates）。stats = 変数ごとの watch_set から読んだ値の統計（ValueStats。
        サンプラの値はイベント記録を続きから読んで数え、読めずに一周された・上限を超えたイベントの数は
        stats_lost_events。ホストの読みの値は読むたびに数える）。"""
        with self._lock:
            status = Sampler(self.session.device).status() if any(not w.chase for w in self._watched) else {}
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
        return {**out, **rates(out)}

    # --- 値の列 ---------------------------------------------------------------------------------

    def _series(self, last: int) -> tuple[dict[str, list], float, dict[str, int | None]]:
        """イベント記録の最新の分（サンプラ）とホストの読み（ポインタをたどる式）から、変数ごとの (時刻, 値) の列と、
        変数ごとの読んだ範囲の始め（それより古い値があったが読まなかった・残っていなければその時刻、全部読めば None）。"""
        if not self._watched:
            raise RuntimeError("観測していない（watch_set を先に）")
        out: dict[str, list] = {w.name: [] for w in self._watched}
        start: dict[str, int | None] = {w.name: None for w in self._watched}
        events: list = []
        older = False                                    # イベント記録に、読んだより古いイベントがあった
        with self._lock:
            device = self.session.device
            hz = device.capabilities().timebase_hz
            if any(not w.chase for w in self._watched):
                log = EventLog(device)
                written = log.counts()[0]
                events = log.read(min(last, READ_LIMIT), end=written)
                older = len(events) < written
            reference = device.time()                    # 48 ビットのイベントの時刻を 64 ビットにする基準（読んだ後）
            for name, series in self._chase_series.items():
                kept = list(series)
                out[name] = kept[-last:]
                if out[name] and self._chase_total.get(name, 0) > len(out[name]):
                    start[name] = out[name][0][0]
        resolved = list(resolve(unwrap_events(events, reference)))
        if older and resolved:
            for w in self._watched:
                if not w.chase:
                    start[w.name] = resolved[0].time
        assembler = _Assembler(self._watched)
        for a in resolved:
            if a.kind == "sample":
                for name, value in assembler.feed(a.address, a.value):
                    out[name].append((a.time, value))
        return out, hz, start

    def watch_values(self) -> dict:
        """観測している変数の最新の値と、その時刻（タイムベースの µs）。"""
        series, hz, _ = self._series(READ_LIMIT)
        return {"values": [{"name": name, "time_us": s[-1][0] * 1e6 / hz, "value": s[-1][1]} if s
                           else {"name": name, "time_us": None, "value": None} for name, s in series.items()]}

    def watch_series(self, name: str, last: int = 1000) -> dict:
        """変数 1 つの値の列（古い順。時刻はタイムベースの µs）。last はイベント記録から読む件数（最大 4096）。
        from_us = 読んだ範囲の始め（それより古い値があったが読まなかった・残っていない。全部読めば None）。"""
        series, hz, start = self._series(last)
        if name not in series:
            raise KeyError(f"{name!r} は観測していない")
        return {"name": name, "samples": [{"time_us": t * 1e6 / hz, "value": v} for t, v in series[name]],
                "from_us": None if start[name] is None else start[name] * 1e6 / hz}

    def watch_xy(self, x: str, y: str, last: int = 1000, max_dt_us: float | None = None) -> dict:
        """2 つの変数の XY の組（pair_latest: x の各値に、その時刻以前で最新の y。2 つは同時に測っていないので、
        点ごとに両方の時刻を出し、差の最大 dt_max_us も返す）。last・from_us は watch_series と同じ。"""
        series, hz, start = self._series(last)
        for name in (x, y):
            if name not in series:
                raise KeyError(f"{name!r} は観測していない")
        us = 1e6 / hz
        pairs, unpaired, excluded = pair_latest(series[x], series[y], None if max_dt_us is None else max_dt_us / us)
        return xy_result(x, y, pairs, unpaired, excluded, max_dt_us, us,
                         {n: None if start[n] is None else start[n] * us for n in (x, y)})

    def watch_measure(self, a_us: float, b_us: float, names: list[str] | None = None, source: str = "live") -> dict:
        """カーソル A・B の測定（measure）。source = "live": イベント記録の最新の分（watch_series と同じ）と
        ホストの読み、"capture": 条件つきの取得の結果。names を省くと観測しているすべて。"""
        if source == "live":
            series, hz, _ = self._series(READ_LIMIT)
            points = {n: [(t * 1e6 / hz, v) for t, v in s] for n, s in series.items()}
        elif source == "capture":
            trigger = self._watch_trigger
            if trigger is None:
                raise RuntimeError("条件つきの取得が無い（watch_trigger を先に）")
            points = {w.name: [] for w in self._watched}
            for t, name, value in sorted(trigger.capture.samples, key=lambda x: x[0]):
                points.setdefault(name, []).append((t * 1e6 / trigger.hz, value))
        else:
            raise ValueError("source は live か capture")
        for n in names or []:
            if n not in points:
                raise KeyError(f"{n!r} は観測していない")
        return measure({n: points[n] for n in (names or points)}, a_us, b_us)

    # --- ポインタをたどる式（ホストの読み） -------------------------------------------------------

    def _chase_once(self) -> None:
        """背景の仕事（_lock の中で）: ポインタをたどる式を評価し直して読む。"""
        info = self._watch_info()
        access = PyocdAccess(self.target)
        frame = _frame(access.read_memory)
        t = self.session.device.time()
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

    # --- 条件つきの取得 -----------------------------------------------------------------------------

    def watch_trigger(self, start: dict | None = None, stop: dict | None = None, pre_s: float = 0.0,
                      post_s: float | None = None, max_samples: int = 100_000) -> dict:
        """観測している変数の取得を条件で始める・止める（watch_set の後）。start / stop: {"variable", "op"（> >= < <= ==
        !=）, "value", "edge"} か {"event": "xtrig"}（クロストリガの割り込み書き込み。xtrig_route の to_inject に
        ロジアナのトリガ・DAP の一致・多段の条件をつなぐ）。start を省くといまから。pre_s: 始まりより前の分（プリ
        トリガ）、post_s: 始まりから取る長さ、max_samples: 件数の上限。結果は watch_capture。"""
        if not self._watched:
            raise RuntimeError("観測していない（watch_set を先に）")
        names = {w.name for w in self._watched}
        start_c = Condition.parse(start, names) if start is not None else None
        stop_c = Condition.parse(stop, names) if stop is not None else None
        if pre_s < 0 or (post_s is not None and post_s <= 0) or max_samples < 1:
            raise ValueError("pre_s ≥ 0、post_s > 0、max_samples ≥ 1")
        with self._lock:
            device = self.session.device
            hz = device.capabilities().timebase_hz
            self._end_watch_jobs()
            capture = Capture(start_c, stop_c, round(pre_s * hz), None if post_s is None else round(post_s * hz),
                              max_samples)
            sampled = any(not w.chase for w in self._watched)
            needs_events = sampled or any(c is not None and c.event for c in (start_c, stop_c))
            cursor, prefill = None, []
            if needs_events:
                log = EventLog(device)
                log.ensure_started()
                cursor = log.cursor()
                if capture.pre_ticks:
                    prefill = unwrap_events(log.read(min(log.capacity, PREFILL_LIMIT)), device.time())
            trigger = _Trigger(capture, cursor, _Assembler(self._watched), hz)
            self._prefill(trigger, prefill)
            self._watch_trigger = trigger
            if needs_events:
                self._add_job("watch.drain", 0.0, self._drain_once)
            if self._chase_series:
                self._add_job("watch.chase", 1.0 / self._chase_rate, self._chase_once)
        return self._capture_dict(trigger, include_samples=False)

    def _prefill(self, trigger: _Trigger, events: list) -> None:
        """プリトリガ: arm の時点でリングに残っている語と、ホストが読んだ値の直近を、時刻順に流し込む。"""
        updates = []
        for e in events:
            if e.kind == KIND_SAMPLE and e.ok:
                updates += [(e.time, name, value) for name, value in trigger.assembler.feed(e.address, e.data)]
            trigger.skip_until = max(trigger.skip_until, e.time)
        if updates or self._chase_series:
            latest = max([u[0] for u in updates] + [s[-1][0] for s in self._chase_series.values() if s] or [0])
            for name, series in self._chase_series.items():
                updates += [(t, name, v) for t, v in series if t >= latest - trigger.capture.pre_ticks]
        for t, name, value in sorted(updates, key=lambda u: u[0]):
            trigger.capture.feed(t, name, value)

    def _drain_once(self) -> None:
        """背景の仕事（_lock の中で）: イベント記録の続きを読み、条件つきの取得に流す。"""
        trigger = self._watch_trigger
        if trigger is None or trigger.capture.done:
            self._remove_job("watch.drain")
            return
        latest = None
        device = self.session.device
        for e in unwrap_events(trigger.cursor.take(), device.time()):
            if e.time <= trigger.skip_until:
                continue
            latest = e.time
            if e.kind == KIND_SAMPLE and e.ok:
                for name, value in trigger.assembler.feed(e.address, e.data):
                    trigger.capture.feed(e.time, name, value)
            elif e.kind in INJECT_KINDS:
                trigger.capture.event(e.time, "xtrig")
        trigger.capture.tick(device.time() if latest is None else latest)

    def _capture_dict(self, trigger: _Trigger, include_samples: bool = True) -> dict:
        c, hz = trigger.capture, trigger.hz
        us = (lambda t: None if t is None else t * 1e6 / hz)
        out = {"state": c.state, "reason": c.reason, "start": None if c.start is None else c.start.describe(),
               "stop": None if c.stop is None else c.stop.describe(), "pre_s": c.pre_ticks / hz,
               "post_s": None if c.post_ticks is None else c.post_ticks / hz, "max_samples": c.max_samples,
               "t_start_us": us(c.t_start), "t_end_us": us(c.t_end), "count": len(c.samples), "dropped": c.dropped,
               "lost_events": trigger.cursor.lost if trigger.cursor is not None else 0}
        if include_samples:
            samples: dict[str, list] = {w.name: [] for w in self._watched}
            for t, name, value in sorted(c.samples, key=lambda s: s[0]):
                samples.setdefault(name, []).append({"time_us": t * 1e6 / hz, "value": value})
            out["samples"] = samples
        return out

    def watch_capture(self, wait_s: float = 0.0) -> dict:
        """条件つきの取得（watch_trigger）の状態と結果（機器には触れない）。終わるまで wait_s 待つ。state: armed（条件を
        待つ）/ capturing / done（reason: stop / post / max_samples / cancelled）。samples は変数ごとの値の列（時刻は
        watch_series と同じ軸の µs）。"""
        trigger = self._watch_trigger
        if trigger is None:
            raise RuntimeError("条件つきの取得を仕掛けていない（watch_trigger を先に）")
        deadline = time.monotonic() + wait_s
        while not trigger.capture.done and time.monotonic() < deadline:
            time.sleep(0.01)
        with self._lock:
            return self._capture_dict(trigger)

    # --- 走行中の書き込み ---------------------------------------------------------------------------

    def watch_write(self, expression: str, value: int | float | str) -> dict:
        """走行中の変数に書く（止めない。CubeMonitor・µC/Probe の実行中の変更）。expression: 大域・静的の C の式
        （ポインタをたどってもよい）か番地（"0x20000000:4" は整数を書く）。value: 数・列挙の名前・真偽。書いた後に
        読み直した値を返す。ビットフィールドは読み・変え・書きで、ターゲットが同じ語を同時に書くと失われうる（atomic）。"""
        raw = _RAW.fullmatch(expression.strip())
        with self._lock:
            access = PyocdAccess(self.target)
            if raw:
                address, size = int(raw[1], 16), int(raw[2] or 4)
                if isinstance(value, (str, float)) or size not in (1, 2, 4, 8):
                    raise ValueError("番地には 1 / 2 / 4 / 8 バイトの整数を書く")
                access.write_memory(address, (int(value) & ((1 << 8 * size) - 1)).to_bytes(size, "little"))
                after = int.from_bytes(access.read_memory(address, size), "little")
                return {"expression": expression, "address": f"0x{address:08X}", "type": None,
                        "value": {"kind": "int", "value": after}, "atomic": True}
            info = self._watch_info()
            if info is None:
                raise RuntimeError("ELF に DWARF が無い（番地なら \"0x20000000:4\" の形で）")
            v = evaluate(expression, info, _frame(access.read_memory))
            if v.address is None:
                raise ValueError(f"{expression!r} はメモリ上に無い（レジスタ・定数には書かない）")
            atomic = v.bit_size is None
            if atomic:
                access.write_memory(v.address, encode(v.type, value))
            else:
                self._write_bits(access, v, value)
            return {"expression": expression, "address": f"0x{v.address:08X}", "type": type_name(v.type),
                    "value": _read_value(v, access.read_memory), "atomic": atomic}

    @staticmethod
    def _write_bits(access, v: Value, value: object) -> None:
        s = strip(v.type)
        number = s.value_of(value) if isinstance(s, EnumType) and isinstance(value, str) else value
        if isinstance(number, bool):
            number = int(number)
        if not isinstance(number, int):
            raise TypeError(f"ビットフィールドには整数を書く（{value!r}）")
        n = v.bit_size
        lo, hi = (-(1 << (n - 1)), 1 << (n - 1)) if getattr(s, "signed", False) else (0, 1 << n)
        if not lo <= int(number) < hi:
            raise ValueError(f"{number} は {n} ビットのビットフィールドに入らない")
        size = (v.bit_offset + n + 7) // 8
        data = int.from_bytes(access.read_memory(v.address, size), "little")
        mask = ((1 << n) - 1) << v.bit_offset
        data = (data & ~mask) | ((int(number) << v.bit_offset) & mask)
        access.write_memory(v.address, data.to_bytes(size, "little"))

    # --- ターゲット側のバッファ -----------------------------------------------------------------------

    def watch_buffer_start(self, array: str, head: str, counter: bool = True, rate_hz: float = 100.0) -> dict:
        """ターゲット側のバッファを吸い出す（STM32CubeMonitor の Snapshot 方式）。ファームが配列 array に値（構造体でも）
        を書き、head を進める（counter = True: 書いた総数。False: 次に書く位置）。PC は rate_hz ごとに head を読み、増えた
        要素だけを読んで DWARF の型で解釈する（速い変化を取りこぼさず、ターゲットの周期に同期する）。読みは
        watch_buffer_read。1 つだけ（呼び直すと前のを止める）。"""
        if not 0 < rate_hz <= 10_000:
            raise ValueError("rate_hz は 0 より大きく 10000 以下")
        info = self._watch_info()
        if info is None:
            raise RuntimeError("ELF に DWARF が無い（watch_buffer は配列の型を使う）")
        with self._lock:
            access = PyocdAccess(self.target)
            frame = _frame(access.read_memory)
            va, vh = evaluate(array, info, frame), evaluate(head, info, frame)
            s = strip(va.type)
            if not isinstance(s, ArrayType) or va.address is None or s.length is None or not s.element.size:
                raise ValueError(f"{array!r} は長さの分かるメモリ上の配列ではない（{type_name(va.type)}）")
            hs = strip(vh.type)
            if vh.address is None or vh.bit_size is not None or not isinstance(hs, (BaseType, EnumType, PointerType)) \
                    or hs.size not in (1, 2, 4, 8):
                raise ValueError(f"{head!r} は整数の変数ではない（{type_name(vh.type)}）")
            ring = TargetRing(s.element, va.address, s.length, vh.address, hs.size, counter)
            ring.poll(access.read_memory)                                  # いまの位置に合わせる
            self._watch_buffer = _Buffer(ring, array, head, rate_hz)
            self._add_job("watch.buffer", 1.0 / rate_hz, self._buffer_once)
        return self._buffer_info()

    def _buffer_once(self) -> None:
        """背景の仕事（_lock の中で）: 増えた要素を吸い出す。"""
        buf = self._watch_buffer
        if buf is None:
            self._remove_job("watch.buffer")
            return
        got = buf.ring.poll(PyocdAccess(self.target).read_memory)
        if got:
            t = self.session.device.time()
            buf.samples.extend((seq, t, value) for seq, value in got)

    def _buffer_info(self) -> dict:
        buf = self._watch_buffer
        if buf is None:
            raise RuntimeError("吸い出していない（watch_buffer_start を先に）")
        r = buf.ring
        return {"array": buf.array, "head": buf.head, "counter": r.counter, "rate_hz": buf.rate_hz,
                "address": f"0x{r.base:08X}", "length": r.length, "element": type_name(r.element),
                "total": r.seq, "lost": r.lost, "error": self._job_errors.get("watch.buffer")}

    def watch_buffer_read(self, since: int | None = None, limit: int = 1000) -> dict:
        """吸い出した要素（機器には触れない）: samples = [{"seq"（通し番号）, "time_us"（PC が読んだ時刻）, "value"}]。
        since を省くと前回の続きから（読むと進む）。since を渡すとそこから（画面用。進まない）。lost は一周されて
        読めなかった要素の数、evicted はホストで持つ数の上限（kept_limit = BUFFER_LIMIT）を超えて捨てた、since より後の
        要素の数、next は次の since。"""
        with self._lock:                                                   # 背景の吸い出しと取り合わない
            buf = self._watch_buffer
            if buf is None:
                raise RuntimeError("吸い出していない（watch_buffer_start を先に）")
            first = buf.cursor if since is None else since
            items = [s for s in buf.samples if s[0] >= first][:limit]
            nxt = items[-1][0] + 1 if items else max(first, buf.samples[0][0] if buf.samples else first)
            if since is None:
                buf.cursor = nxt
            hz = self.session.device.capabilities().timebase_hz
            evicted = max(0, buf.samples[0][0] - first) if buf.samples else 0
            return {"samples": [{"seq": seq, "time_us": t * 1e6 / hz, "value": v} for seq, t, v in items],
                    "next": nxt, "evicted": evicted, "kept_limit": BUFFER_LIMIT, **self._buffer_info()}

    def watch_buffer_stop(self) -> dict:
        """吸い出しを止める（吸い出した要素は watch_buffer_read で読める）。"""
        with self._lock:
            self._remove_job("watch.buffer")
            return self._buffer_info()
