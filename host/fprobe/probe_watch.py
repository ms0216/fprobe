"""Probe の道具: 変数のライブ観測（F4。ターゲットを止めずに、FPGA の DAP サンプラが周期的に読む）と、その拡充（U43）。

Probe（fprobe.mcp_server）に混ぜて使う mixin。Probe の _lock・session・target・_symbols・_recorder と、DAP サンプラの
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
import re
import time
from dataclasses import dataclass, field

from .collector import MASK48
from .debug import Debugger, PyocdAccess
from .dwarf.cexpr import Value, evaluate
from .dwarf.info import FrameView
from .dwarf.location import NotAvailable
from .dwarf.typesys import ArrayType, BaseType, CType, EnumType, PointerType, strip, type_name
from .dwarf.values import decode, decode_bits, encode
from .events import KIND_INJECT, KIND_SAMPLE, EventCursor, EventLog, resolve
from .sampler import Sampler
from .watch_buffer import TargetRing
from .watch_trigger import Capture, Condition

__all__ = ["WatchTools"]

READ_LIMIT = 4096                      # 1 回に読むイベント記録の件数（最新から）
CHASE_LIMIT = 100_000                  # ホストが読んだ値を式ごとに残す数
BUFFER_LIMIT = 200_000                 # 吸い出した要素を残す数
PREFILL_LIMIT = 1 << 16                # プリトリガのために arm の時点でリングから読むイベントの上限
_RAW = re.compile(r"(0x[0-9a-fA-F]+)(?::(\d+))?")


def _counts(status: dict) -> dict:
    """サンプラの状態の数（巡回・誤り・追いつかなかった周期。32 ビットで回り込む）。"""
    return {k: status[k] for k in ("rounds", "errors", "overruns")}


class _NeedsMemory(Exception):
    """式の番地を決めるのにメモリの値が要る（ポインタをたどる式）。"""


def _no_memory(address: int, length: int) -> bytes:
    raise _NeedsMemory()


def _no_register(number: int) -> int:
    raise NotAvailable("走行中の観測ではレジスタ（ローカル変数）は使えない（大域・静的の式にする）")


def _frame(read_memory) -> FrameView:
    """大域だけを引くフレーム（レジスタは使えない）。"""
    return FrameView(None, read_memory, _no_register)


def _read_value(v: Value, read_memory) -> object:
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

    def value(self, words: dict[int, int]) -> object:
        data = b"".join(words[w].to_bytes(4, "little") for w in self.words)
        data = data[self.address % 4:self.address % 4 + self.size]
        if self.type is None:
            return int.from_bytes(data, "little")
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

    def feed(self, address: int, value: int) -> list[tuple[str, object]]:
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
        self._watch_rejected: list[dict] = []
        self._watch_base: dict | None = None               # watch_set の時点のサンプラの数（起点）
        self._watch_started = 0.0
        self._watch_rate = 0.0
        self._chase_rate = 0.0
        self._chase_series: dict[str, collections.deque] = {}
        self._chase_errors: dict[str, str] = {}
        self._watch_trigger: _Trigger | None = None
        self._watch_buffer: _Buffer | None = None

    def _watch_info(self):
        return Debugger(None, self._symbols).debug_info() if self._symbols else None

    def _watch_target(self, expression: str) -> _Watched:
        raw = _RAW.fullmatch(expression.strip())
        if raw:
            return _Watched(expression, int(raw[1], 16), int(raw[2] or 4))
        info = self._watch_info()
        if info is None:
            if self._symbols is None:
                raise RuntimeError("ELF が読み込まれていない（elf_load を先に。番地なら \"0x20000000:4\" の形で）")
            s = self._symbols.variable(expression)                        # DWARF が無い: シンボルの番地と大きさ
            return _Watched(expression, s.address, s.size or 4)
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
                  jitter: int = 0) -> dict:
        """変数を止めずに周期的に読む（FPGA の DAP サンプラ）。expressions: 変数名・C の式・番地（"0x20000000" か
        "0x20000000:バイト数"）。決まった番地の式はサンプラが rate_hz で読む（合わせて 8 か所・各 16 語まで）。ポインタを
        たどる式はホストが chase_rate_hz ごとに番地を引き直して読む（method = host）。jitter（0〜15）: サンプラの周期に
        ゆらぎを足し、周期的な処理との噛み合い（エイリアシング）を防ぐ（fprobe.sampler.Sampler.start）。"""
        if not 0 < chase_rate_hz <= 10_000:
            raise ValueError("chase_rate_hz は 0 より大きく 10000 以下")
        watched, rejected = self._watch_targets(expressions)
        sampled = [w for w in watched if not w.chase]
        slots = []
        with self._lock:
            device = self.session.device
            if sampled:
                self._claim_sampler("watch")
                slots = Sampler(device).configure([(w.address, w.size) for w in sampled], 1.0 / rate_hz, ap)
                if self._recorder is None:
                    EventLog(device).ensure_started()                     # 記録中は Collector が始めている
                Sampler(device).start(jitter)
                self._watch_base = _counts(Sampler(device).status())
            elif self._sampler_user == "watch":                           # 前の watch_set のサンプラ
                Sampler(device).stop()
                self._release_sampler("watch")
            self._end_watch_jobs()
            self._watched, self._watch_rate, self._chase_rate = watched, rate_hz, chase_rate_hz
            self._watch_rejected, self._watch_started = rejected, time.monotonic()
            if not sampled:
                self._watch_base = None
            self._chase_series = {w.name: collections.deque(maxlen=CHASE_LIMIT) for w in watched if w.chase}
            self._chase_errors = {}
            if self._chase_series:
                self._add_job("watch.chase", 1.0 / chase_rate_hz, self._chase_once)
        return {"watches": [self._describe(w) for w in watched], "rate_hz": rate_hz, "chase_rate_hz": chase_rate_hz,
                "slots": [{"address": f"0x{a:08X}", "words": n} for a, n in slots], "rejected": rejected}

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
        trigger = self._watch_trigger
        if trigger is not None and not trigger.capture.done:
            c = trigger.capture
            c.cancel(c.samples[-1][0] if c.samples else c.t_start or 0)

    def watch_stop(self) -> dict:
        """周期的な読みを止める（読んだ値はイベント記録・記録に残る。条件つきの取得は打ち切る）。"""
        with self._lock:
            self._end_watch_jobs()
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
        watch_set で外した誤った式（rejected）、始めてからの秒（since_start_s）、ホストの読みの誤り、条件つきの取得の状態。"""
        with self._lock:
            status = Sampler(self.session.device).status() if any(not w.chase for w in self._watched) else {}
        if status and self._watch_base is not None:                       # 起動からの数 → watch_set からの数
            device = _counts(status)
            status.update({k: (v - self._watch_base[k]) & 0xFFFFFFFF for k, v in device.items()}, device=device)
        trigger = self._watch_trigger
        return {"watches": [self._describe(w) for w in self._watched], "rejected": list(self._watch_rejected),
                "since_start_s": time.monotonic() - self._watch_started if self._watched else None,
                "rate_hz": self._watch_rate,
                "chase_rate_hz": self._chase_rate, "chase_errors": dict(self._chase_errors),
                "capture": None if trigger is None else {"state": trigger.capture.state,
                                                         "reason": trigger.capture.reason},
                **status}

    # --- 値の列 ---------------------------------------------------------------------------------

    def _series(self, last: int) -> tuple[dict[str, list], float]:
        """イベント記録の最新の分（サンプラ）とホストの読み（ポインタをたどる式）から、変数ごとの (時刻, 値) の列。"""
        if not self._watched:
            raise RuntimeError("観測していない（watch_set を先に）")
        out: dict[str, list] = {w.name: [] for w in self._watched}
        with self._lock:
            device = self.session.device
            hz = device.capabilities().timebase_hz
            events = EventLog(device).read(min(last, READ_LIMIT)) if any(not w.chase for w in self._watched) else []
            for name, series in self._chase_series.items():
                out[name] = list(series)[-last:]
        assembler = _Assembler(self._watched)
        for a in resolve(events):
            if a.kind == "sample":
                for name, value in assembler.feed(a.address, a.value):
                    out[name].append((a.time, value))
        return out, hz

    def watch_values(self) -> dict:
        """観測している変数の最新の値と、その時刻（タイムベースの µs）。"""
        series, hz = self._series(READ_LIMIT)
        return {"values": [{"name": name, "time_us": s[-1][0] * 1e6 / hz, "value": s[-1][1]} if s
                           else {"name": name, "time_us": None, "value": None} for name, s in series.items()]}

    def watch_series(self, name: str, last: int = 1000) -> dict:
        """変数 1 つの値の列（古い順。時刻はタイムベースの µs）。last はイベント記録から読む件数（最大 4096）。"""
        series, hz = self._series(last)
        if name not in series:
            raise KeyError(f"{name!r} は観測していない")
        return {"name": name, "samples": [{"time_us": t * 1e6 / hz, "value": v} for t, v in series[name]]}

    # --- ポインタをたどる式（ホストの読み） -------------------------------------------------------

    def _chase_once(self) -> None:
        """背景の仕事（_lock の中で）: ポインタをたどる式を評価し直して読む。"""
        info = self._watch_info()
        access = PyocdAccess(self.target)
        frame = _frame(access.read_memory)
        t = self.session.device.time() & MASK48
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
            if trigger is not None and trigger.capture.state != "done":
                trigger.capture.feed(t, w.name, value)

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
                    prefill = log.read(min(log.capacity, PREFILL_LIMIT))
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
        for e in trigger.cursor.take():
            if e.time <= trigger.skip_until:
                continue
            latest = e.time
            if e.kind == KIND_SAMPLE and e.ok:
                for name, value in trigger.assembler.feed(e.address, e.data):
                    trigger.capture.feed(e.time, name, value)
            elif e.kind == KIND_INJECT:
                trigger.capture.event(e.time, "xtrig")
        trigger.capture.tick(self.session.device.time() & MASK48 if latest is None else latest)

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
                return {"expression": expression, "address": f"0x{address:08X}", "type": None, "value": after,
                        "atomic": True}
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
            t = self.session.device.time() & MASK48
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
        読めなかった要素の数、next は次の since。"""
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
            return {"samples": [{"seq": seq, "time_us": t * 1e6 / hz, "value": v} for seq, t, v in items],
                    "next": nxt, **self._buffer_info()}

    def watch_buffer_stop(self) -> dict:
        """吸い出しを止める（吸い出した要素は watch_buffer_read で読める）。"""
        with self._lock:
            self._remove_job("watch.buffer")
            return self._buffer_info()
