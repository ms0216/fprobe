"""FPGA の観測の記録の復号: イベント（種類 8・9）から「fprobe の時刻の値の並び」を作り直す。
記録の形式と時刻の合わせ方は fpga_obs の説明と同じ（そちらは対象の回路の設定・操作）。

時刻の合わせ方（time-accuracy 設計書 §4.6・§13、段階 S3）: 印の組は窓（対象の鎖の時刻で [印の瞬間 − sync_delay_hi,
印の瞬間 − sync_delay_lo]、fprobe の時刻はイベント 9 の時刻 = Update-DR を出る TCK の端子の辺そのもの）で、値の時刻は、その値の
鎖の時刻に隣り合う 2 つの印の窓を通る直線の範囲（fprobe.clockmap。仮定は「隣り合う印の間で 2 つの時計の比が一定」だけ）。
印が 1 つの鎖は、使う側が選んだ比（申告の CLOCK_HZ の比、または borrow_ratio で前の鎖の比の範囲）を傾きにする。
最小二乗（段階 S3 の前の fit_line・fit_sync）は使わない（§6 の判断 4）。time_fprobe は範囲の中点（置き場所の決まり）で、
範囲そのものは time_lo・time_hi。
"""

import bisect
from collections import deque
from dataclasses import dataclass, field

from .clockmap import ClockMap, Pair, pair
from .events import KIND_OBS_RECORD, KIND_OBS_SYNC

__all__ = ["ObsDecoder", "ObsSample", "SyncSegment", "samples"]

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
    直前と同じ値でも出る** = 出発点で、変化ではない。copy が目印）・"gap"（連続性の切れ目。溢れ、または対象のリセット。
    value = None、lost = 失った値の変化の数で、リセットの間の変化は数えられないので 0 でも欠けがある（観測の回路の
    LOST_BITS の上限で止まる）、時刻は再開の時刻）・"group"（観測する組が group になった。value = None）・
    "hidden"（標本型: 前の標本からこの標本までに見えなかった値の変化。lost = その回数。値は失っていない）。
    received = その記録のイベントを fprobe が受けた時刻（起きた時刻の上限。time_fprobe が None のときの手がかり）。
    time_basis = time_fprobe の決め方: "pairs"（その鎖の隣り合う 2 つの印の窓を通る直線の範囲。値が印の外なら端の 2 つ）/
    "previous_ratio"（印 1 つと、前の鎖の比の範囲。使う側が borrow_ratio で明示したときだけ: 前の鎖と
    時計の比が同じという仮定）/ "nominal_ratio"（印 1 つと、観測の回路が申告した CLOCK_HZ の比）/ "assumption_broken"
    （窓を通る直線が無い: 時刻は None）/ "hold_limit"（時刻を決める印が届く前に、印を待って持つ数の上限 max_pending
    （使う側が明示）に達したので、時刻を決めずに出した: 時刻は None）/ None（時刻が無い）。
    time_lo・time_hi = 値が変わった（写しなら、その値だった）瞬間の fprobe の時刻の範囲（両端を含む。time_fprobe はその中点）。
    印の窓（申告の sync_delay の範囲）・値の記録の遅れの範囲（sample_delay）・比の不確かさを含む。
    copy（kind = "value" のときだけ意味を持つ）= この値は写し: その時刻にその値だったことだけが確かで、値がその時刻に
    変わったとは限らない（前の値が分からないか、別の組・別の区間のもの）。鎖の始め（delta 0: 開始・溢れの後・リセット明けの
    再開）、溢れ・組の記録・イベントの欠けの後の最初の値、標本型の標本。False = 鎖の中の値の記録で、time_fprobe はその値に
    変わった瞬間（観測の回路の sample_delay を引いた時刻）。"""
    time_fprobe: float | None
    target_cycles: int
    value: int | None
    lost: int = 0
    group: int = 0
    kind: str = "value"
    received: int | None = field(default=None, compare=False)   # 手がかり（値の比べには入れない）
    time_basis: str | None = field(default=None, compare=False)
    copy: bool = field(default=False, compare=False)
    time_lo: float | None = field(default=None, compare=False)
    time_hi: float | None = field(default=None, compare=False)


_new, _set = object.__new__, object.__setattr__   # ObsSample の速い作り方（_sample）
TIME_WRAP = 1 << 48                                # fprobe のタイムベース（48 ビット）が折り返す大きさ
TIME_HALF_WRAP = TIME_WRAP >> 1


class SyncSegment:
    """1 本の鎖の印の窓の組（対象の鎖の時刻 x の窓 → fprobe の時刻 y）と、印が 1 つのときの比の範囲 slope（使う側が選んだもの）。
    window(x_lo, x_hi) = 対象の時刻の窓に対する fprobe の時刻の範囲（隣り合う 2 つの印の窓を通る直線の範囲。値が印の外なら
    端の 2 つの直線を延ばす）。範囲が出なければ None。"""

    def __init__(self, pairs: list[Pair], slope: tuple[float, float] | None = None):
        self.pairs = pairs
        self.slope = slope
        self._xs = [p.x_lo for p in pairs]
        self._maps: dict[int, tuple[ClockMap, list[tuple[float, float]] | None, float]] = {}

    def _map(self, x: float):
        if len(self.pairs) >= 2:
            i = min(max(bisect.bisect_right(self._xs, x) - 1, 0), len(self.pairs) - 2)
            key, ps, slope = i, self.pairs[i:i + 2], None
        else:
            key, ps, slope = 0, self.pairs, self.slope
        got = self._maps.get(key)
        if got is None:
            m = ClockMap(ps, slope=slope)
            v = m._vertices()
            got = self._maps[key] = (m, None if v is None else [(float(a), float(b)) for a, b in v], float(m.x0))
        return got

    def window(self, x_lo: float, x_hi: float) -> tuple[float, float] | None:
        _, v, x0 = self._map((x_lo + x_hi) / 2)
        if v is None:
            return None
        return min(a + b * (x_lo - x0) for a, b in v), max(a + b * (x_hi - x0) for a, b in v)

    def slope_range(self) -> tuple[float, float] | None:
        """最後の 2 つの印（印が 1 つなら与えた比）の直線の傾きの範囲。"""
        m = self._map(self._xs[-1] if self._xs else 0.0)[0]
        r = m.slope_range()
        return None if r is None else (float(r[0]), float(r[1]))

    @property
    def feasible(self) -> bool:
        return all(self._map(x)[1] is not None for x in self._xs)


@dataclass
class _Chain:
    """1 本の鎖（delta を続けて足せる区間）と、その印の組。"""
    pairs: list[Pair] = field(default_factory=list)
    segment: SyncSegment | None = None
    basis: str | None = None                       # 時刻の決め方（ObsSample.time_basis）


class ObsDecoder:
    """観測の記録（イベント 8）と印（イベント 9）を、受け取った順に少しずつ値の並びにする（記録しながらの取り込み用）。

    鎖の時刻は鎖の始め（delta 0 の値）か、読み始め・欠けの後の最初の印から数える。値の fprobe の時刻はその鎖の印が
    来てから決まるので、それまで持っておき（後ろ向きに決める）、記録の順に出す。印の来ないまま鎖が終われば時刻は
    None で出す。sync_delay・sample_delay は観測の回路が申告する遅れ（周期）: 印の瞬間は Update-DR より sync_delay 遅く、
    値の記録は変化より sample_delay 遅い。範囲（sync_delay_lo / hi・sample_delay_lo / hi。FpgaObs.open）があれば範囲を、
    無ければ 1 点（sync_delay・sample_delay）を窓に使う。time_lo・time_hi はそれを引いた本当の瞬間の範囲。nominal_ratio =
    タイムベース / 対象の clk（申告の CLOCK_HZ から。印が 1 つの間の傾き）。borrow_ratio = 印が 1 つの鎖に前の鎖の比の範囲を
    使うか（前の鎖と時計の比が同じという仮定。使う側が明示したときだけ。既定は申告の比、それも無ければ時刻なし）。
    fprobe の時刻（48 ビット）が前の時刻より戻り、それが一周（2^47 以上戻る）でなければ、タイムベースが 0 に戻った事実
    （時代の切れ目。time-accuracy §14.1）: 前の時代の印・鎖を捨てて数え直し、restarts に数える（前後の時代を 1 本の直線に混ぜない）。
    一度にすべてを与えても、少しずつ与えても（最後に flush）同じ結果（max_pending に達しない限り）。

    max_pending（必ず明示する。既定は持たない）: 時刻を決める印を待って持つ値の数の上限。超えたら古いものから、時刻を決めずに
    （time_fprobe = None、time_basis = "hold_limit"）出し、untimed_by_limit に数える。値と鎖の時刻は正しいまま（推定しない）。
    止めずに出す理由: 値どうしは独立で、時刻が無いのはその値だけ（後の値の時刻は、印が届けば決まる）。None = 上限を置かない
    （一度に全部を与える使い方: 入力がもうメモリにあり、持つ数は入力の数を超えない）。"""

    SYNC_WINDOW = 256
    MARK_MEMORY = 64                               # 組にならない印・イベントを覚えておく数（番号は 8 ビットで一周）

    def __init__(self, sync_delay: float = 0.0, sample_delay: float = 0.0, nominal_ratio: float | None = None,
                 recent: int = 0, borrow_ratio: bool = False, sync_delay_lo: float | None = None,
                 sync_delay_hi: float | None = None, sample_delay_lo: float | None = None,
                 sample_delay_hi: float | None = None, *, max_pending: int | None):
        if max_pending is not None and max_pending < 1:
            raise ValueError(f"max_pending（印を待って持つ値の数の上限）が不正: {max_pending}")
        self.max_pending = max_pending
        self.untimed_by_limit = 0                  # max_pending に達して時刻を決めずに出した値の数
        self.sync_delay, self.sample_delay, self.nominal_ratio = sync_delay, sample_delay, nominal_ratio
        self.borrow_ratio = borrow_ratio
        self._set_delay_ranges(sync_delay_lo, sync_delay_hi, sample_delay_lo, sample_delay_hi)
        # 最近に出した値の並び（recent 件。記録へ取り込みながら、イベントのリングを読めない間の fpga_obs_read 用）
        self.recent: deque[ObsSample] = deque(maxlen=recent)
        # recent の時刻（イベントの 48 ビットの時刻から伸ばした刻み。タイムベースの上位とは揃っていない）を 64 ビットの
        # タイムベースにするための基準: 食わせる側（collector.add_events・flush_obs）が、そのイベントの後に読んだタイムベースを置く。
        # None = まだ置かれていない
        self.reference: int | None = None
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
        self._ratio: tuple[float, float] | None = None   # 最後の鎖の比の範囲（borrow_ratio で印が 1 つの鎖に使う）
        self.restarts = 0                          # タイムベースが 0 に戻った（時代の切れ目）のを見た回数
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

    def _restart_if_back(self, f: float, last: float | None) -> bool:
        """展開した時刻 f が前の時刻 last より前（一周ではない戻り）= タイムベースが 0 に戻った: 時代を切る。"""
        if last is None or f >= last:
            return False
        self.restarts += 1
        self._ratio = None
        self._f_last = self._snap_time = None
        self._marks.clear()
        self.events_lost()
        return True

    def events_lost(self) -> None:
        """イベントが欠けた（記録のリングの追い越し等）: 鎖が切れる。次の印から数え直し、組の記録まで値を出さない。"""
        self._end_chain()
        self._t = None
        self._group = None
        self._snap_last = None
        self._shown_group = None                    # 欠けた区間の変化は分からない: 組と値を読み直す（標本型と同じ）
        self._last = None
        self._lost = None

    def _set_delay_ranges(self, sync_lo, sync_hi, sample_lo, sample_hi) -> None:
        """申告の遅れの範囲（周期。FpgaObs.open の sync_delay_lo/hi・sample_delay_lo/hi）。印の瞬間は Update-DR を出る
        TCK の立ち上がり（イベント 9 の時刻）より [sync_delay_lo, sync_delay_hi] 周期遅く、値の記録は変化より
        [sample_delay_lo, sample_delay_hi] 周期遅い（どちらも端を含む）。渡されなければ None（分からない）。
        範囲があれば時刻の窓に使い（段階 S3）、無ければ 1 点の sync_delay・sample_delay を使う。"""
        self.sync_delay_lo, self.sync_delay_hi = sync_lo, sync_hi
        self.sample_delay_lo, self.sample_delay_hi = sample_lo, sample_hi

    def reset_clock(self, nominal_ratio: float | None, sync_delay: float, sample_delay: float,
                    borrow_ratio: bool = False, sync_delay_lo: float | None = None,
                    sync_delay_hi: float | None = None, sample_delay_lo: float | None = None,
                    sample_delay_hi: float | None = None) -> None:
        """観測を開き直した（FPGA を書き直した等で、時計の源・申告の遅れが変わりうる）: 前の鎖の傾き・覚えている印を捨てる。"""
        self.nominal_ratio, self.sync_delay, self.sample_delay = nominal_ratio, sync_delay, sample_delay
        self.borrow_ratio = borrow_ratio
        self._set_delay_ranges(sync_delay_lo, sync_delay_hi, sample_delay_lo, sample_delay_hi)
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
                if self.max_pending is not None and len(pending) > self.max_pending:
                    self._release_over_limit(out)
        self.recent.extend(out)
        return out

    def _release_over_limit(self, out: list[ObsSample]) -> None:
        """持つ数が max_pending を超えた分を、古いものから時刻を決めずに出す（time_basis = "hold_limit"）。"""
        pending = self._pending
        while len(pending) > self.max_pending:
            s = _sample(*pending.popleft(), None, None)
            _set(s, "time_basis", "hold_limit")
            out.append(s)
            self.untimed_by_limit += 1

    def _snapshot(self, e) -> list[ObsSample]:
        """標本型の標本: 値が変わっていれば値、見えない変化があれば先に切れ目（失った数 = 回数 − 見えた 1 回）。"""
        v, n, at = e.data & 0xFFFF, e.data >> 16 & 0xFF, self._extend(e.time, self._snap_time)
        self._restart_if_back(at, self._snap_time)
        self._snap_time = at
        last, self._snap_last = self._snap_last, v
        # 標本の値はどれも写し（その瞬間の値。前の標本からいつ変わったかは分からない）
        if last is None:
            return [ObsSample(at, 0, None, 0, 0, "group"), ObsSample(at, 0, v, copy=True)]
        out = []
        lost = n - (1 if v != last else 0)
        if lost > 0:
            out.append(ObsSample(at, 0, None, lost, 0, "hidden"))
        if v != last:
            out.append(ObsSample(at, 0, v, copy=True))
        return out

    def flush(self) -> list[ObsSample]:
        """持っている値を出す（印の来ていない鎖の値は時刻 None で）。"""
        out = [_sample(*p, None, None) for p in self._pending]
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
                pending.append((chain, t, None, self._lost, group, "gap", received, False))
                self._lost = None
                self._last = None
            if group != self._shown_group:
                self._shown_group = group
                pending.append((chain, t, None, 0, group, "group", received, False))
                self._last = None
            v = payload & 0xFFFF
            if delta != 0 and v == self._last:               # 印の後の書き直し: 変化ではない（鎖の中の値の記録は変化）
                return
            # 写し: 鎖の始め（観測の回路が今の値を書いた）か、前の値が分からない・別の組（溢れ・組の記録・欠けの後）。
            # その時刻の値ではあるが、その時刻に変わったとは限らない
            copy = delta == 0 or self._last is None
            self._last = v
            seg = chain.segment
            if seg is not None and not pending:              # 待つものが無く時刻も決まっている: そのまま出す
                out.append(_sample(chain, t, v, 0, group, "value", received, copy, seg, self._sample_range()))
            else:
                pending.append((chain, t, v, 0, group, "value", received, copy))
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
            # 値の前に溢れの記録が続けて来ることがある（観測の回路が溢れの記録を書いた次の周期に値が変わった）: 間に失った
            # 数はその和
            self._lost = (self._lost or 0) + (payload & 0xFFFF)
            self._end_chain()
            self._t = None
        elif payload >> 26 & 0xF == C_GROUP:
            self._group = payload & 0xFF

    # --- 印 ---------------------------------------------------------------------------------------------------
    def _pair(self, seq: int) -> None:
        if seq in self._marks and seq in self._events:
            chain, instant = self._marks.pop(seq)
            f = self._extend(self._events.pop(seq), self._f_last)   # 48 ビットの時刻が折り返した: 2^47 以上戻ったら 2^48 を足す
            if self._restart_if_back(f, self._f_last):
                return                                   # 前の時代の印と組にしない（この印は鎖の外になった）
            self._f_last = f
            lo, hi = self._sync_range()
            chain.pairs = (chain.pairs + [pair((instant - hi, instant - lo), f)])[-self.SYNC_WINDOW:]
            borrowed = self._ratio if self.borrow_ratio else None
            nominal = (self.nominal_ratio, self.nominal_ratio) if self.nominal_ratio else None
            if len(chain.pairs) >= 2:
                chain.basis, chain.segment = "pairs", SyncSegment(chain.pairs)
                r = chain.segment.slope_range()
                if r is None:
                    chain.basis = "assumption_broken"
                elif r[1] > 0:                           # 傾き 0 だけの範囲は比として借りられない
                    self._ratio = r
            elif borrowed or nominal:
                chain.basis = "previous_ratio" if borrowed else "nominal_ratio"
                chain.segment = SyncSegment(chain.pairs, borrowed or nominal)
            else:
                chain.basis, chain.segment = None, None

    def _sync_range(self) -> tuple[float, float]:
        if self.sync_delay_lo is not None and self.sync_delay_hi is not None:
            return self.sync_delay_lo, self.sync_delay_hi
        return self.sync_delay, self.sync_delay

    def _sample_range(self) -> tuple[float, float]:
        if self.sample_delay_lo is not None and self.sample_delay_hi is not None:
            return self.sample_delay_lo, self.sample_delay_hi
        return self.sample_delay, self.sample_delay

    def _forget(self, d: dict) -> None:
        while len(d) > self.MARK_MEMORY:
            d.pop(next(iter(d)))

    def _end_chain(self) -> None:
        self._chain = None

    # --- 出す ---------------------------------------------------------------------------------------------------
    def _release(self, out: list[ObsSample]) -> None:
        """記録の順に、時刻の決まったもの（と、印の来ないまま終わった鎖のもの）を out へ出す。"""
        pending, current, delay = self._pending, self._chain, self._sample_range()
        while pending:
            p = pending[0]
            seg = p[0].segment
            if seg is not None:
                out.append(_sample(*p, seg, delay))
            elif p[0] is not current:
                out.append(_sample(*p, None, None))
            else:
                break
            pending.popleft()


def _sample(chain: _Chain, t: int, value: int | None, lost: int, group: int, kind: str, received: int | None,
            copy: bool, seg: SyncSegment | None, sample_delay: tuple[float, float] | None) -> ObsSample:
    """待っていた記録（鎖・鎖の時刻・…・写しか）から ObsSample を作る。seg が無ければ時刻は None。sample_delay = 値の記録が
    変化より遅い周期の範囲（lo, hi）。"""
    at = lo = hi = basis = None
    if seg is not None:
        d_lo, d_hi = sample_delay or (0.0, 0.0)
        w = seg.window(t - d_hi, t - d_lo)
        if w is None:
            basis = "assumption_broken"
        else:
            lo, hi = w
            at, basis = (lo + hi) / 2, chain.basis
    s = _new(ObsSample)
    _set(s, "__dict__", {"time_fprobe": at, "target_cycles": t, "value": value, "lost": lost, "group": group,
                         "kind": kind, "received": received, "time_basis": basis,
                         "copy": copy, "time_lo": lo, "time_hi": hi})   # frozen の __init__ より速い
    return s


def samples(events, nominal_ratio: float | None = None, sync_delay: float = 0.0,
            sample_delay: float = 0.0, borrow_ratio: bool = False, **delay_ranges) -> list[ObsSample]:
    """イベント（種類 8・9。他は無視）から値の並びを作り直す（ObsDecoder に一度に与えて flush）。delay_ranges =
    ObsDecoder の sync_delay_lo・sync_delay_hi・sample_delay_lo・sample_delay_hi。"""
    d = ObsDecoder(sync_delay, sample_delay, nominal_ratio, borrow_ratio=borrow_ratio, max_pending=None, **delay_ranges)
    return d.feed(events) + d.flush()
