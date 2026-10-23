"""対象の FPGA の観測（docs/superpowers/specs/2026-10-20-fpga-observation-v2-design.md）: 対象の FPGA に入れた観測の
回路 v2（target_ip/fprobe_obs）を fprobe が対象の JTAG で読み続け（obs_jtag_engine）、記録をイベント 8、印をイベント 9 に
する。ここではその設定と、イベントから「fprobe の時刻の値の並び」を作り直すことを行う。

記録（32 ビット、31:30 が種類）。時刻のある記録は前の時刻のある記録からの対象の clk の周期数 delta を 29:16 に持つ:
  0 値 delta | value(15:0)（delta 0 = 鎖の始め）、1 刻み delta | 0・印 delta | 1 | 番号(14:7) | late(6:0)、
  2 溢れ lost(15:0)（鎖が切れる）、3 制御 種類(29:26): 0 組(7:0)・1 応答 番地(19:16) | 値(15:0)。
時刻は数えない: delta を足し合わせた「鎖の時刻」を、印（番号つき。fprobe がその Update-DR の時刻をイベント 9 にする）
で fprobe の時刻と結ぶ。鎖ごとに印の組を直線に合わせる（組が 1 つなら申告の CLOCK_HZ の比を傾きにする）。

標本型（obs_snap_core。極小の FPGA 向け）: 記録の語 = 0xA7 | 前の標本からの変化の回数(23:16) | 値(15:0)。時刻は
fprobe がその Capture-DR を出た瞬間（イベントの時刻そのもの）。標本の間の見えない変化は切れ目（失った数）にする。
"""

import time
from collections import deque
from dataclasses import dataclass, field

from . import generated as g
from .events import KIND_OBS_RECORD, KIND_OBS_SYNC

__all__ = ["CHAIN_FIELDS", "FpgaObs", "ObsDecoder", "ObsSample", "SyncSegment", "fit_line", "fit_sync", "samples"]

T_VALUE, T_MARK, T_OVERFLOW, T_CONTROL = 0, 1, 2, 3
C_GROUP, C_RESPONSE = 0, 1
OP_WRITE, OP_READ = 1, 2
REG_ID_LO, REG_ID_HI, REG_VERSION, REG_GROUPS, REG_FIFO_DEPTH, REG_CLOCK_LO, REG_CLOCK_HI = range(7)
REG_SYNC_DELAY, REG_SAMPLE_DELAY, REG_LOST_BITS = 7, 8, 9    # 申告の遅れ（半周期の単位・周期）と失った数の幅
REG_CONTROL, REG_SELECT = 14, 15
OBS_ID = 0x464F4253                               # "FOBS"
SNAP_ID = 0x534E4150                              # 標本型（エンジンが id に出す "SNAP"）
SNAP_MARK = 0xA7
LATE_UNKNOWN = 0x7F                               # 印の late の上限（それ以上待った印の瞬間は分からない）
WIDTH = 16                                        # 組の幅
CHAIN_FIELDS = {"ir_len": g.OBS_CHAIN_IR_LEN, "ir_before": g.OBS_CHAIN_IR_BEFORE, "ir_after": g.OBS_CHAIN_IR_AFTER,
                "dr_before": g.OBS_CHAIN_DR_BEFORE, "dr_after": g.OBS_CHAIN_DR_AFTER}


@dataclass(frozen=True)
class ObsSample:
    """観測した値。time_fprobe = fprobe のタイムベースの刻み（印が無く時刻が分からなければ None）。
    target_cycles = 鎖の時刻（鎖の始め・読み始めの印からの対象の clk の周期数。鎖が変わると 0 から）。
    kind: "value"（値が変わった）・"gap"（溢れの切れ目。value = None、lost = 失った値の変化の数（観測の回路の
    LOST_BITS の上限で止まる）、時刻は再開の時刻）・"group"（観測する組が group になった。value = None）・
    "hidden"（標本型: 前の標本からこの標本までに見えなかった値の変化。lost = その回数。値は失っていない）。
    received = その記録のイベントを fprobe が受けた時刻（起きた時刻の上限。time_fprobe が None のときの手がかり）。"""
    time_fprobe: float | None
    target_cycles: int
    value: int | None
    lost: int = 0
    group: int = 0
    kind: str = "value"
    received: int | None = field(default=None, compare=False)   # 手がかり（値の比べには入れない）


@dataclass(frozen=True)
class SyncSegment:
    """印の組を合わせた直線: fprobe = fprobe0 + ratio × (対象 − target0)。"""
    target0: float
    fprobe0: float
    ratio: float

    def to_fprobe(self, target: float) -> float:
        return self.fprobe0 + self.ratio * (target - self.target0)


def fit_line(pairs: list[tuple[float, float]]) -> tuple[float, float, float]:
    """最小二乗の直線（最初の組からの差で和を取る）。(target0, fprobe0, ratio)、target0 = 最初の組の対象の時刻。"""
    t0 = pairs[0][0]
    n = len(pairs)
    sx = sum(t - t0 for t, _ in pairs)
    sy = sum(f for _, f in pairs)
    sxx = sum((t - t0) ** 2 for t, _ in pairs)
    sxy = sum((t - t0) * f for t, f in pairs)
    ratio = (n * sxy - sx * sy) / (n * sxx - sx * sx)
    return t0, (sy - ratio * sx) / n, ratio


def fit_sync(pairs: list[tuple[float, float]], nominal_ratio: float | None = None) -> list[SyncSegment]:
    """(対象の時刻, fprobe の時刻) の組を直線に合わせる（同じ鎖の組。2 つ以上なら傾きも合わせ、1 つなら nominal_ratio）。"""
    if len(pairs) >= 2:
        return [SyncSegment(*fit_line(pairs))]
    if pairs and nominal_ratio:
        return [SyncSegment(pairs[0][0], pairs[0][1], nominal_ratio)]
    return []


@dataclass
class _Chain:
    """1 本の鎖（delta を続けて足せる区間）と、その印の組。"""
    pairs: list[tuple[float, float]] = field(default_factory=list)
    segment: SyncSegment | None = None


@dataclass
class _Pending:
    chain: _Chain
    t: int                       # 鎖の時刻
    value: int | None
    lost: int
    group: int
    kind: str
    received: int | None = None      # その記録のイベントを受けた fprobe の時刻


class ObsDecoder:
    """観測の記録（イベント 8）と印（イベント 9）を、受け取った順に少しずつ値の並びにする（記録しながらの取り込み用）。

    鎖の時刻は鎖の始め（delta 0 の値）か、読み始め・欠けの後の最初の印から数える。値の fprobe の時刻はその鎖の印が
    来てから決まるので、それまで持っておき（後ろ向きに決める）、記録の順に出す。印の来ないまま鎖が終われば時刻は
    None で出す。sync_delay・sample_delay は観測の回路が申告する遅れ（周期）: 印の瞬間は Update-DR より sync_delay 遅く、
    値の記録は変化より sample_delay 遅い。time_fprobe はそれを引いた本当の瞬間。nominal_ratio = タイムベース / 対象の
    clk（申告の CLOCK_HZ から。印が 1 つの間の傾き）。一度にすべてを与えても、少しずつ与えても（最後に flush）同じ結果。"""

    SYNC_WINDOW = 256
    MARK_MEMORY = 64                               # 組にならない印・イベントを覚えておく数（番号は 8 ビットで一周）

    def __init__(self, sync_delay: float = 0.0, sample_delay: float = 0.0, nominal_ratio: float | None = None,
                 recent: int = 0):
        self.sync_delay, self.sample_delay, self.nominal_ratio = sync_delay, sample_delay, nominal_ratio
        # 最近に出した値の並び（recent 件。記録へ取り込みながら、イベントのリングを読めない間の fpga_obs_read 用）
        self.recent: deque[ObsSample] = deque(maxlen=recent)
        self._received: int | None = None             # 今読んでいる記録のイベントを受けた時刻
        self._chain: _Chain | None = None
        self._t: int | None = None                 # 鎖の時刻（鎖の始め・印を見るまで None）
        self._group: int | None = None             # 今の組（組の記録で知る）
        self._shown_group: int | None = None       # 最後に出した組の印
        self._lost: int | None = None              # 溢れ（次の値で切れ目にする）
        self._last: int | None = None              # 最後に出した値
        self._marks: dict[int, tuple[_Chain, float]] = {}    # 番号 → (鎖, 印の瞬間の鎖の時刻)
        self._events: dict[int, int] = {}                    # 番号 → fprobe の時刻
        self._pending: list[_Pending] = []
        self._ratio: float | None = None           # 最後に合わせた傾き（印が 1 つの鎖に使う）
        self._snap_last: int | None = None         # 標本型: 前の標本の値

    def events_lost(self) -> None:
        """イベントが欠けた（記録のリングの追い越し等）: 鎖が切れる。次の印から数え直し、組の記録まで値を出さない。"""
        self._end_chain()
        self._t = None
        self._group = None
        self._snap_last = None

    def feed(self, events) -> list[ObsSample]:
        out: list[ObsSample] = []
        for e in events:
            if e.kind == KIND_OBS_SYNC:
                seq = e.data & 0xFF
                self._events[seq] = e.time
                self._forget(self._events)
                self._pair(seq)
            elif e.kind == KIND_OBS_RECORD and e.data >> 24 == SNAP_MARK:
                out += self._release() + self._snapshot(e)
                continue
            elif e.kind == KIND_OBS_RECORD:
                self._record(e.data, e.time)
            out += self._release()
        self.recent.extend(out)
        return out

    def _snapshot(self, e) -> list[ObsSample]:
        """標本型の標本: 値が変わっていれば値、見えない変化があれば先に切れ目（失った数 = 回数 − 見えた 1 回）。"""
        v, n, at = e.data & 0xFFFF, e.data >> 16 & 0xFF, float(e.time)
        last, self._snap_last = self._snap_last, v
        if last is None:
            return [ObsSample(at, 0, None, 0, 0, "group"), ObsSample(at, 0, v)]
        out = []
        lost = n - (1 if v != last else 0)
        if lost > 0:
            out.append(ObsSample(at, 0, None, lost, 0, "hidden"))
        if v != last:
            out.append(ObsSample(at, 0, v))
        return out

    def flush(self) -> list[ObsSample]:
        """持っている値を出す（印の来ていない鎖の値は時刻 None で）。"""
        out = [self._sample(p, None) for p in self._pending]
        self._pending = []
        self.recent.extend(out)
        return out

    # --- 記録 -------------------------------------------------------------------------------------------------
    def _record(self, word: int, received: int | None = None) -> None:
        self._received = received
        kind, payload = word >> 30, word & (1 << 30) - 1
        delta = payload >> 16 & 0x3FFF
        if kind == T_VALUE:
            if delta == 0:                                   # 鎖の始め
                self._end_chain()
                self._chain, self._t = _Chain(), 0
            elif self._t is None:
                return
            else:
                self._t += delta
            self._value(payload & 0xFFFF, fresh=delta == 0)
        elif kind == T_MARK:
            is_mark = payload >> 15 & 1
            if self._t is None:
                if not is_mark:
                    return
                self._end_chain()                            # 読み始め・欠けの後: この印から数える
                self._chain, self._t = _Chain(), 0
            else:
                self._t += delta
            if is_mark:
                seq, late = payload >> 7 & 0xFF, payload & 0x7F
                if late != LATE_UNKNOWN:                   # 127 = 長く待って瞬間が分からない（時刻合わせに使わない）
                    self._marks[seq] = (self._chain, self._t - late)
                    self._forget(self._marks)
                    self._pair(seq)
        elif kind == T_OVERFLOW:
            self._lost = payload & 0xFFFF
            self._end_chain()
            self._t = None
        elif payload >> 26 & 0xF == C_GROUP:
            self._group = payload & 0xFF

    def _value(self, v: int, fresh: bool) -> None:
        """fresh = 鎖の始め。鎖の中の値の記録は変化なので前の値と違う。同じなら印の後の書き直し（変化ではない）。"""
        if self._group is None:
            return
        if self._lost is not None:                           # 溢れ → 組の切り替えの順（起きた順）
            self._pending.append(_Pending(self._chain, self._t, None, self._lost, self._group, "gap", self._received))
            self._lost = None
            self._last = None
        if self._group != self._shown_group:
            self._shown_group = self._group
            self._pending.append(_Pending(self._chain, self._t, None, 0, self._group, "group", self._received))
            self._last = None
        if not fresh and v == self._last:                    # 印の後の書き直し: 変化ではない
            return
        self._last = v
        self._pending.append(_Pending(self._chain, self._t, v, 0, self._group, "value", self._received))

    # --- 印 ---------------------------------------------------------------------------------------------------
    def _pair(self, seq: int) -> None:
        if seq in self._marks and seq in self._events:
            chain, instant = self._marks.pop(seq)
            f = self._events.pop(seq)
            chain.pairs = (chain.pairs + [(instant - self.sync_delay, float(f))])[-self.SYNC_WINDOW:]
            segs = fit_sync(chain.pairs, self._ratio or self.nominal_ratio)
            chain.segment = segs[0] if segs else None
            if chain.segment is not None and len(chain.pairs) >= 2:
                self._ratio = chain.segment.ratio

    def _forget(self, d: dict) -> None:
        while len(d) > self.MARK_MEMORY:
            d.pop(next(iter(d)))

    def _end_chain(self) -> None:
        self._chain = None

    # --- 出す ---------------------------------------------------------------------------------------------------
    def _release(self) -> list[ObsSample]:
        """記録の順に、時刻の決まったもの（と、印の来ないまま終わった鎖のもの）を出す。"""
        out = []
        while self._pending:
            p = self._pending[0]
            if p.chain.segment is not None:
                out.append(self._sample(p, p.chain.segment))
            elif p.chain is not self._chain:
                out.append(self._sample(p, None))
            else:
                break
            self._pending.pop(0)
        return out

    def _sample(self, p: _Pending, seg: SyncSegment | None) -> ObsSample:
        at = seg.to_fprobe(p.t - self.sample_delay) if seg is not None else None
        return ObsSample(at, p.t, p.value, p.lost, p.group, p.kind, p.received)


def samples(events, nominal_ratio: float | None = None, sync_delay: float = 0.0,
            sample_delay: float = 0.0) -> list[ObsSample]:
    """イベント（種類 8・9。他は無視）から値の並びを作り直す（ObsDecoder に一度に与えて flush）。"""
    d = ObsDecoder(sync_delay, sample_delay, nominal_ratio)
    return d.feed(events) + d.flush()


class FpgaObs:
    """fprobe の FPGA の観測の口（ケイパビリティ FPGA_OBS がある構成だけ）。"""

    def __init__(self, device):
        caps = device.capabilities()
        if not getattr(caps, "fpga_obs", False):
            raise RuntimeError("この構成には FPGA の観測が無い（ケイパビリティ FEATURES の FPGA_OBS。PYNQ 等の大きい構成だけ）")
        self.dev = device
        self.sync_delay = 0.0                          # 観測の回路の申告（open で読む。周期）
        self.sample_delay = 0.0
        self.clock_hz = 0
        self.mode = "stream"                           # open で決まる: "stream"（記録型）・"snapshot"（標本型）
        self.sys_clock_hz = caps.sys_clock_hz or 100_000_000
        self.scan_s = 0.0                              # 1 スキャンの時間の見積もり（open で。待ちの長さに使う）

    def samples(self, events, nominal_ratio: float | None = None) -> list[ObsSample]:
        """samples（モジュールの関数）を、open で読んだ観測の回路の遅れで。"""
        return samples(events, nominal_ratio, sync_delay=self.sync_delay, sample_delay=self.sample_delay)

    def open(self, chain: dict | None = None, user_ir: int = 2, half_period: int = 10, words: int = 64,
             sync_period: int = 100, timeout_s: float = 1.0) -> dict:
        """対象の JTAG の鎖での位置（chain: CHAIN_FIELDS のうち要るもの。ir_len の既定は 4）・USER の命令・TCK の半周期
        （sys の周期数。1 クロック版の観測の回路は TCK ≤ 対象の clk / 8）・1 スキャンの記録の語数・印の間隔（スキャンの
        数）を設定して始め、観測の回路の申告を返す。"""
        unknown = set(chain or {}) - set(CHAIN_FIELDS)
        if unknown:
            raise ValueError(f"chain に知らない項目: {sorted(unknown)}（使えるのは {sorted(CHAIN_FIELDS)}）")
        if not 2 <= half_period <= 0xFFFF or not 1 <= words <= 256 or not 0 <= sync_period <= 0xFFFF:
            raise ValueError("half_period は 2〜65535、words は 1〜256、sync_period は 0〜65535")
        fields = {"ir_len": 4, **(chain or {})}
        packed = 0
        for name, value in fields.items():
            lsb, width = CHAIN_FIELDS[name]
            if not 0 <= value < 1 << width:
                raise ValueError(f"chain の {name} = {value} は {width} ビットに入らない")
            packed |= value << lsb
        self.close()
        # 1 スキャン = 見出し 1 語 + 記録 words 語（と鎖・状態の移りの分）の TCK。命令の間の分を見込んで 1.5 倍
        self.scan_s = (1 + words) * 32 * 2 * half_period / self.sys_clock_hz * 1.5 + 1e-4
        timeout_s = max(timeout_s, 40 * self.scan_s)
        self.dev.write32(g.OBS_HALF_PERIOD, half_period)
        self.dev.write32(g.OBS_CHAIN, packed)
        self.dev.write32(g.OBS_USER_IR, user_ir)
        self.dev.write32(g.OBS_WORDS, words)
        self.dev.write32(g.OBS_SYNC_PERIOD, sync_period)
        self.dev.write32(g.OBS_CONTROL, 1)
        self._wait(lambda s: s["id_ok"] or s["stuck"], timeout_s, "ID を読めない")
        status = self.status()
        if not status["id_ok"]:
            self.close()
            raise RuntimeError(f"観測の回路が答えない（ID {status['id']:#010x}。鎖の位置・USER の命令・配線・TCK の速さ"
                               "（1 クロック版は対象の clk の 1/8 以下）を確かめる）")
        if status["id"] == SNAP_ID:
            # 標本型: レジスタが無い（組 1・幅 16。時刻は fprobe の Capture-DR の瞬間）
            self.mode = "snapshot"
            self.sync_delay = self.sample_delay = 0.0
            self.clock_hz = 0
            return {"id": status["id"], "mode": "snapshot", "version": 2, "width": WIDTH, "groups": 1,
                    "fifo_depth": 0, "clock_hz": 0, "sync_delay": 0.0, "sample_delay": 0.0, "lost_bits": 8}
        self.mode = "stream"
        self.sync_delay = self.register_read(REG_SYNC_DELAY) / 2
        self.sample_delay = float(self.register_read(REG_SAMPLE_DELAY))
        self.clock_hz = self.register_read(REG_CLOCK_LO) | self.register_read(REG_CLOCK_HI) << 16
        return {"id": status["id"], "mode": "stream", "version": self.register_read(REG_VERSION), "width": WIDTH,
                "groups": self.register_read(REG_GROUPS), "fifo_depth": self.register_read(REG_FIFO_DEPTH),
                "clock_hz": self.clock_hz, "sync_delay": self.sync_delay, "sample_delay": self.sample_delay,
                "lost_bits": self.register_read(REG_LOST_BITS)}

    def close(self, timeout_s: float = 1.0) -> None:
        """止める（今のスキャンを終え、TAP を Test-Logic-Reset に戻して線を手放す）。手放すまで待つ（その後なら別の
        道具を対象の JTAG につないでよい）。"""
        self.dev.write32(g.OBS_CONTROL, 0)
        deadline = time.monotonic() + max(timeout_s, 20 * self.scan_s)
        while self.dev.read32(g.OBS_STATUS) >> g.OBS_STATUS_DRIVING[0] & 1:
            if time.monotonic() > deadline:
                raise TimeoutError("観測の線を手放さない（エンジンが止まらない）")

    def status(self) -> dict:
        s = self.dev.read32(g.OBS_STATUS)
        out = {name.lower(): bool(s >> getattr(g, f"OBS_STATUS_{name}")[0] & 1)
               for name in ("RUNNING", "ID_OK", "STUCK", "CMD_PENDING", "OVERFLOW_SEEN", "CMD_DROPPED", "TARGET_RUNNING",
                            "DRIVING")}
        out["scans"] = self.dev.read32(g.OBS_SCANS)
        out["id"] = self.dev.read32(g.OBS_ID)
        return out

    def register_read(self, addr: int, timeout_s: float | None = None) -> int:
        """観測の回路のレジスタ（16 ビット）を読む。応答は記録の列で来て、エンジンが read_value（番号 | 番地 | 値）に
        写す。番号が変わり番地が合うまで待つ。"""
        self._need_registers()
        before = self.dev.read32(g.OBS_READ_VALUE) >> 24
        self._command(OP_READ, addr, 0, timeout_s)
        deadline = time.monotonic() + self._timeout(timeout_s)
        while True:
            v = self.dev.read32(g.OBS_READ_VALUE)
            if v >> 24 != before and v >> 16 & 0xF == addr:
                return v & 0xFFFF
            if self.status()["stuck"]:
                raise RuntimeError("観測の回路が答えなくなった（レジスタの読み）")
            if time.monotonic() > deadline:
                raise TimeoutError(f"レジスタ {addr} の応答が来ない")

    def register_write(self, addr: int, value: int, timeout_s: float | None = None) -> None:
        self._need_registers()
        self._command(OP_WRITE, addr, value, timeout_s)
        start = self.dev.read32(g.OBS_SCANS)
        self._wait(lambda s: (s["scans"] - start) % (1 << 32) >= 1, self._timeout(timeout_s), "スキャンが進まない")

    def select(self, group: int) -> None:
        """観測する 16 ビットの組（0〜GROUPS − 1。範囲外は観測の回路が無視する。標本型は 0 だけ）。記録中は、観測の回路が
        変化の無い周期（か溢れの立て直し）に組の記録を書くまで前の組を見続ける（記録の組の印が切り替えの瞬間）。"""
        if self.mode == "snapshot" and group == 0:
            return
        self.register_write(REG_SELECT, group)

    def start(self) -> None:
        """観測の回路が記録を始める（始め直すと溢れの印も消える。標本型はいつも動いているので何もしない）。"""
        if self.mode != "snapshot":
            self.register_write(REG_CONTROL, 1)

    def stop(self) -> None:
        if self.mode != "snapshot":
            self.register_write(REG_CONTROL, 0)

    def _need_registers(self) -> None:
        if self.mode == "snapshot":
            raise RuntimeError("標本型の観測の回路（obs_snap_core）にはレジスタが無い")

    def _timeout(self, timeout_s: float | None) -> float:
        return max(1.0, 40 * self.scan_s) if timeout_s is None else timeout_s

    def _command(self, op: int, addr: int, data: int, timeout_s: float | None) -> None:
        """命令（op(3:0) | 番地(7:4) | データ(15:8)）を待ち行列に積み、スキャンに載るまで待つ。"""
        if not 0 <= addr <= 15 or not 0 <= data <= 0xFF:
            raise ValueError("番地は 0〜15、データは 0〜255")
        self.dev.write32(g.OBS_CMD, op | addr << 4 | data << 8)
        self.dev.write32(g.OBS_CMD_PUSH, 1)
        if self.status()["cmd_dropped"]:
            raise RuntimeError("命令の待ち行列が一杯で捨てられた（観測が止まっていないか確かめる）")
        self._wait(lambda s: not s["cmd_pending"], self._timeout(timeout_s), "命令が出ない")

    def _wait(self, done, timeout_s: float, what: str) -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            s = self.status()
            if done(s):
                return
            if s["stuck"]:
                raise RuntimeError(f"観測の回路が答えなくなった（{what}）")
            if time.monotonic() > deadline:
                raise TimeoutError(f"{what}（{timeout_s} 秒）")


def compile_match(condition: str, signals: "FpgaSignals", group: int = 0) -> tuple[int, int, int]:
    """システム解析の条件（例 "restart changed and state=BUSY"）を、観測のエンジンの値の一致（CSR obs.match・
    match_change）の (mask, value, change) に。瞬間の項（changed / rose / fell）は 1 つまで（ハードは変化のマスクの
    どれかを見る）、無ければ「一致に入った瞬間」。rose / fell は 1 ビットの信号だけ。信号は組 group の定義から。
    docs/superpowers/specs/2026-10-21-system-analysis-design.md §4.1"""
    from .correlate import ConditionError, parse_condition
    try:
        cond = parse_condition(condition, need_edge=False)
    except ConditionError as e:
        raise ValueError(str(e)) from None
    by_name = {s.name: s for s in signals.signals(group)}
    edges = [t for t in cond.terms if t.op != "="]
    if len(edges) > 1:
        raise ValueError("FPGA で比べる条件の瞬間の項（changed / rose / fell）は 1 つまで")
    mask = value = change = 0
    for t in cond.terms:
        name = t.name[5:] if t.name.startswith("fpga.") else t.name
        sig = by_name.get(name)
        if sig is None:
            raise ValueError(f"組 {group} に信号 {name!r} が無い（FPGA の信号だけで書く）")
        bits = sig.mask << sig.lsb
        if t.op == "=":
            names = {v: k for k, v in sig.enum.items()}
            text = t.value or ""
            try:
                v = names[text] if text in names else int(text, 0)
            except ValueError:
                raise ValueError(f"{name} の値 {text!r} が数でも列挙の名前でもない") from None
            if not 0 <= v <= sig.mask:
                raise ValueError(f"{name} の値 {v} は {sig.width} ビットに入らない")
            if mask & bits and (value & bits) != v << sig.lsb:
                raise ValueError(f"{name} に二つの値を求めている")
            mask |= bits
            value |= v << sig.lsb
        else:
            change |= bits
            if t.op in ("rose", "fell"):
                if sig.width != 1:
                    raise ValueError(f"rose / fell は 1 ビットの信号だけ（{name} は {sig.width} ビット）")
                mask |= bits
                value |= bits if t.op == "rose" else 0
    return mask, value, change
