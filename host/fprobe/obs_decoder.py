"""FPGA の観測の記録の復号: イベント（種類 8・9）から「fprobe の時刻の値の並び」を作り直す。
記録の形式と時刻の合わせ方は fpga_obs の説明と同じ（そちらは対象の回路の設定・操作）。
"""

from collections import deque
from dataclasses import dataclass, field

from .events import KIND_OBS_RECORD, KIND_OBS_SYNC

__all__ = ["ObsDecoder", "ObsSample", "SyncSegment", "fit_line", "fit_sync", "samples"]

T_VALUE, T_MARK, T_OVERFLOW, T_CONTROL = 0, 1, 2, 3
C_GROUP, C_RESPONSE = 0, 1
SNAP_MARK = 0xA7
LATE_UNKNOWN = 0x7F                               # 印の late の上限（それ以上待った印の瞬間は分からない）
WIDTH = 16                                        # 組の幅


@dataclass(frozen=True)
class ObsSample:
    """観測した値。time_fprobe = fprobe のタイムベースの刻み（印が無く時刻が分からなければ None）。
    target_cycles = 鎖の時刻（鎖の始め・読み始めの印からの対象の clk の周期数。鎖が変わると 0 から）。
    kind: "value"（値の記録。鎖の中では値が変わった印。**鎖の始め（開始・溢れ・リセット明けの再開の最初の値）は
    直前と同じ値でも出る** = 出発点で、変化ではない。直前の gap や group が目印）・"gap"（連続性の切れ目。溢れ、または対象のリセット。
    value = None、lost = 失った値の変化の数で、リセットの間の変化は数えられないので 0 でも欠けがある（観測の回路の
    LOST_BITS の上限で止まる）、時刻は再開の時刻）・"group"（観測する組が group になった。value = None）・
    "hidden"（標本型: 前の標本からこの標本までに見えなかった値の変化。lost = その回数。値は失っていない）。
    received = その記録のイベントを fprobe が受けた時刻（起きた時刻の上限。time_fprobe が None のときの手がかり）。
    time_basis = time_fprobe の決め方: "pairs"（その鎖の印の組 2 つ以上の直線。直近 SYNC_WINDOW 組）/
    "previous_ratio"（印 1 つと、前の鎖の印の組から求めた傾き）/ "nominal_ratio"（印 1 つと、申告の CLOCK_HZ の比）/
    None（時刻が無い）。"""
    time_fprobe: float | None
    target_cycles: int
    value: int | None
    lost: int = 0
    group: int = 0
    kind: str = "value"
    received: int | None = field(default=None, compare=False)   # 手がかり（値の比べには入れない）
    time_basis: str | None = field(default=None, compare=False)


_new, _set = object.__new__, object.__setattr__   # ObsSample の速い作り方（_sample）
TIME_WRAP = 1 << 48                                # fprobe のタイムベース（48 ビット）が折り返す大きさ
TIME_HALF_WRAP = TIME_WRAP >> 1


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
    denominator = n * sxx - sx * sx
    if denominator == 0:
        raise ValueError("対象の時刻が全部同じで、傾きが決まらない")
    ratio = (n * sxy - sx * sy) / denominator
    return t0, (sy - ratio * sx) / n, ratio


def fit_sync(pairs: list[tuple[float, float]], nominal_ratio: float | None = None) -> list[SyncSegment]:
    """(対象の時刻, fprobe の時刻) の組を直線に合わせる（同じ鎖の組。2 つ以上なら傾きも合わせ、1 つなら nominal_ratio）。"""
    if len(pairs) >= 2:
        try:
            return [SyncSegment(*fit_line(pairs))]
        except ValueError:                                   # 対象の時刻が全部同じ: 傾きは決まらない（申告の比があればそれ）
            pass
    if pairs and nominal_ratio:
        return [SyncSegment(pairs[0][0], pairs[0][1], nominal_ratio)]
    return []


@dataclass
class _Chain:
    """1 本の鎖（delta を続けて足せる区間）と、その印の組。"""
    pairs: list[tuple[float, float]] = field(default_factory=list)
    segment: SyncSegment | None = None
    basis: str | None = None                       # 時刻の決め方（ObsSample.time_basis）


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
        self._pending: deque[tuple] = deque()      # 時刻の待ち: (鎖, 鎖の時刻, 値, lost, 組, 種類, 受けた時刻)
        self._ratio: float | None = None           # 最後に合わせた傾き（印が 1 つの鎖に使う）
        self._snap_last: int | None = None         # 標本型: 前の標本の値
        self._f_last: float | None = None          # 最後の印の fprobe の時刻（48 ビットの折り返しを展開した値。鎖をまたぐ）
        self._snap_time: float | None = None       # 標本型: 前の標本の fprobe の時刻（同じく展開した値）

    @staticmethod
    def _extend(t: int, last: float | None) -> float:
        """48 ビットの fprobe の時刻を、前に見た時刻 last から続く値にする（半周以上戻ったら 1 周進んだとする。時刻の円環の性質）。"""
        f = float(t)
        if last is not None:
            while f < last - TIME_HALF_WRAP:
                f += TIME_WRAP
        return f

    def events_lost(self) -> None:
        """イベントが欠けた（記録のリングの追い越し等）: 鎖が切れる。次の印から数え直し、組の記録まで値を出さない。"""
        self._end_chain()
        self._t = None
        self._group = None
        self._snap_last = None
        self._shown_group = None                    # 欠けた区間の変化は分からない: 組と値を読み直す（標本型と同じ）
        self._last = None
        self._lost = None

    def reset_clock(self, nominal_ratio: float | None, sync_delay: float, sample_delay: float) -> None:
        """観測を開き直した（FPGA を書き直した等で、時計の源・申告の遅れが変わりうる）: 前の鎖の傾き・覚えている印を捨てる。"""
        self.nominal_ratio, self.sync_delay, self.sample_delay = nominal_ratio, sync_delay, sample_delay
        self._ratio = None
        self._f_last = self._snap_time = None
        self._marks.clear()
        self._events.clear()
        self.events_lost()

    def feed(self, events) -> list[ObsSample]:
        out: list[ObsSample] = []
        pending = self._pending
        for e in events:
            kind = e.kind
            if kind == KIND_OBS_RECORD:
                data = e.data
                if data >> 24 == SNAP_MARK:
                    if pending:
                        self._release(out)
                    out += self._snapshot(e)
                    continue
                self._record(data, e.time, out)
            elif kind == KIND_OBS_SYNC:
                seq = e.data & 0xFF
                self._events[seq] = e.time
                self._forget(self._events)
                self._pair(seq)
            if pending:
                self._release(out)
        self.recent.extend(out)
        return out

    def _snapshot(self, e) -> list[ObsSample]:
        """標本型の標本: 値が変わっていれば値、見えない変化があれば先に切れ目（失った数 = 回数 − 見えた 1 回）。"""
        v, n, at = e.data & 0xFFFF, e.data >> 16 & 0xFF, self._extend(e.time, self._snap_time)
        self._snap_time = at
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
        out = [_sample(*p, None) for p in self._pending]
        self._pending.clear()
        self.recent.extend(out)
        return out

    # --- 記録 -------------------------------------------------------------------------------------------------
    def _record(self, word: int, received: int, out: list[ObsSample]) -> None:
        kind, payload = word >> 30, word & (1 << 30) - 1
        delta = payload >> 16 & 0x3FFF
        if kind == T_VALUE:
            if delta == 0:                                   # 鎖の始め
                chain = self._chain = _Chain()
                t = self._t = 0
            elif self._t is None:
                return
            else:
                chain = self._chain
                t = self._t = self._t + delta
            group = self._group
            if group is None:
                return
            pending = self._pending
            if self._lost is not None:                       # 溢れ → 組の切り替えの順（起きた順）
                pending.append((chain, t, None, self._lost, group, "gap", received))
                self._lost = None
                self._last = None
            if group != self._shown_group:
                self._shown_group = group
                pending.append((chain, t, None, 0, group, "group", received))
                self._last = None
            v = payload & 0xFFFF
            if delta != 0 and v == self._last:               # 印の後の書き直し: 変化ではない（鎖の中の値の記録は変化）
                return
            self._last = v
            seg = chain.segment
            if seg is not None and not pending:              # 待つものが無く時刻も決まっている: そのまま出す
                out.append(_sample(chain, t, v, 0, group, "value", received, seg, self.sample_delay))
            else:
                pending.append((chain, t, v, 0, group, "value", received))
        elif kind == T_MARK:
            is_mark = payload >> 15 & 1
            if self._t is None:
                if not is_mark:
                    return
                self._chain, self._t = _Chain(), 0           # 読み始め・欠けの後: この印から数える
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

    # --- 印 ---------------------------------------------------------------------------------------------------
    def _pair(self, seq: int) -> None:
        if seq in self._marks and seq in self._events:
            chain, instant = self._marks.pop(seq)
            f = self._f_last = self._extend(self._events.pop(seq), self._f_last)      # 48 ビットの時刻が折り返した: 前の時刻から 2^47 以上戻ったら 2^48 を足す
            chain.pairs = (chain.pairs + [(instant - self.sync_delay, f)])[-self.SYNC_WINDOW:]
            chain.basis = "pairs" if len(chain.pairs) >= 2 else "previous_ratio" if self._ratio else "nominal_ratio"
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
    def _release(self, out: list[ObsSample]) -> None:
        """記録の順に、時刻の決まったもの（と、印の来ないまま終わった鎖のもの）を out へ出す。"""
        pending, current, delay = self._pending, self._chain, self.sample_delay
        while pending:
            p = pending[0]
            seg = p[0].segment
            if seg is not None:
                out.append(_sample(*p, seg, delay))
            elif p[0] is not current:
                out.append(_sample(*p, None))
            else:
                break
            pending.popleft()


def _sample(chain: _Chain, t: int, value: int | None, lost: int, group: int, kind: str, received: int | None,
            seg: SyncSegment | None, sample_delay: float = 0.0) -> ObsSample:
    """待っていた記録（鎖・鎖の時刻・…）から ObsSample を作る。seg が無ければ時刻は None。"""
    if seg is None:
        at = basis = None
    else:
        at, basis = seg.fprobe0 + seg.ratio * (t - sample_delay - seg.target0), chain.basis
    s = _new(ObsSample)
    _set(s, "__dict__", {"time_fprobe": at, "target_cycles": t, "value": value, "lost": lost, "group": group,
                         "kind": kind, "received": received, "time_basis": basis})   # frozen の __init__ より速い
    return s


def samples(events, nominal_ratio: float | None = None, sync_delay: float = 0.0,
            sample_delay: float = 0.0) -> list[ObsSample]:
    """イベント（種類 8・9。他は無視）から値の並びを作り直す（ObsDecoder に一度に与えて flush）。"""
    d = ObsDecoder(sync_delay, sample_delay, nominal_ratio)
    return d.feed(events) + d.flush()
