"""停止の見張り: FPGA が全コアの停止を見張り、1 つが止まったら残りを止める（CTI の無い Cortex-A 等）。

仕様: docs/superpowers/specs/2026-10-29-halt-watch-design.md
既存の部品をつなぐだけで、専用の回路は無い:
  DAP サンプラ（watch の各番地を 1 か所 1 語で読み続ける）→ サンプラの値の一致（番地のマスクで全部をまとめ、
  (値 and halted_mask) = halted_value に入った瞬間）→ クロストリガ（源 SAMPLER_MATCH → 割り込み書き込み）→
  割り込み書き込み（halt_writes の語の列を順に書き、最後に CTRL/STAT で誤りを確かめる）。
番地と値はすべて呼び出し側が渡す（ボード固有の値をここに持たない）。サンプラ・値の一致・割り込み書き込みの中身を
占有する（同時に他の用途には使えない）。0 にはならない: 時間差は目標の側で測って報告する。

CSR の往復（1 回 = USB・Ethernet の往復 0.3〜1 ms）: 見張りの間に読むのは `poll` の 1 回（値の一致の count から unmatched3 までの
11 語）。発火した後の `status` は、それに割り込み書き込みの状態（xtrig の inject_status から inject の first_time までの 19 語）の
1 回を足す。`arm`・`disarm` は 2 回ずつ。クロストリガの経路は `configure` で足して `release` で外す（値の一致を有効にしていなければ
発火しないので、有効の間だけ経路を足す必要はない）。
見逃しの確定（§3.6）: `HaltWatchPoll.seen_running` は、有効にした後に FPGA がその位置の語を「止まっていない値」で読んだか。
読んだ後に止まれば、そのとき発火する（once で他の位置が先に発火していなければ）。ホストが止まっているのを読んだ位置が、その読みの
後の poll でも False なら、その位置の停止では発火しない（推定ではなく、FPGA が読んだ標本の事実）。

再開を FPGA が行う（docs/superpowers/specs/2026-11-03-halt-watch-fpga-restart-design.md。ケイパビリティ watch_start = `can_restart`）:
`configure(restart_writes=...)` で表の後ろに再開の要求を入れ、`arm_and_restart` が値の一致を門つき（after_start・first_sample）で
有効にし、割り込み書き込みの始動の列で再開を書かせる。門は始動の列が成功した後に開き、その後の標本だけを比べるので、
「止まった」の条件に再開の後だけ 1 になるビット（ARMv7 の DBGDSCR.RESTARTED）を入れれば、再開の後のどの停止も発火する
（最初の標本でも）。見逃しの確定（seen_running）は要らない。
"""

from dataclasses import dataclass

from . import generated as g
from .collector import unwrap48
from .device import Device
from .xtrig import Source

__all__ = ["HaltWatch", "HaltWatchError", "HaltWatchPoll", "HaltWatchRestart", "HaltWatchStatus"]

SLOTS = 8                       # サンプラのか所
WORDS = 8                       # 割り込み書き込みの語（語 0 + 表の 7）
BUSY_READS = 100                # 表を書き換える前に、書き込みが終わるのを待つ（CSR を読む回数。1 回の書き込みは µs、1 回の読みは USB の往復）


class HaltWatchError(RuntimeError):
    """FPGA の見張りの設定ができない（割り込み書き込みが終わらない等）。"""
RESULTS = ("ok", "swd_error", "not_connected", "absent")


@dataclass(frozen=True)
class HaltWatchPoll:
    """見張りの間に読む状態（1 回の読み）。"""
    matched: bool               # 値の一致が発火した（割り込み書き込みはまだ書いている途中のことがある）
    index: int                  # 発火した標本の番号（か所 × 16 + 語）。matched のときだけ意味を持つ
    t_fire48: int               # 発火した標本の時刻（48 ビット。タイムベースの下位）
    t_prev48: int               # 同じ番号の直前の（止まっていない）標本の時刻（48 ビット）
    seen_running: tuple[bool, ...]   # watch の位置ごと: 有効にした後に FPGA が止まっていない値を読んだ
    fire_first: bool = False    # 発火はその番号の門の後の最初の標本（t_prev48 = 始動の列の最初の語の時刻）。matched のときだけ


@dataclass(frozen=True)
class HaltWatchStatus:
    fired: bool                 # 値の一致が発火し、割り込み書き込みが終わった
    core: int | None            # 止まったのを見つけた watch の位置（fired のとき）
    result: str                 # 割り込み書き込みの結果（RESULTS）
    written: int                # 受理された語の数（失敗なら、最後に受理された語が誤りのことがある）
    t_prev: int                 # その位置の直前の（止まっていない）標本の時刻（タイムベース）
    # 止まった標本の時刻。DAP_LINE_TIME の回路（time-accuracy S2）では標本の時刻 = その読みを頼んだ転送の線上の点（窓の始め）で、
    # 止まったのは t_prev の読みの窓の始めより後、t_fire の読みの窓の後端（イベント 7 の次のサンプラの件の時刻。CSR には無い）より前。
    # 古い回路は値を受け取った周期で、止まったのは t_prev と t_fire の間
    t_fire: int
    t_first_write: int          # 最初の語の書き込みの時刻（DAP_LINE_TIME: その DRW の線上の点。古い回路: 受理された周期）
    t_last_write: int           # 最後の語の書き込みが受理された時刻
    seen_running: tuple[bool, ...] = ()   # この状態を作った poll の HaltWatchPoll.seen_running
    fire_first: bool = False    # HaltWatchPoll.fire_first（t_prev = 始動の列の最初の語の時刻）


@dataclass(frozen=True)
class HaltWatchRestart:
    """始動の列（FPGA が書く再開の要求）の状態（1 回の読み）。"""
    pending: bool               # 要求してから終わるまで
    result: str                 # 最後の始動の列の結果（RESULTS の先頭 3 つ）
    written: int                # 受理された語の数（失敗なら、最後に受理された語が誤りのことがある）
    count: int                  # 終わった始動の列の数
    t_first: int                # 最初に受理された語の DRW の線上の点（タイムベース。written > 0 のときだけ意味を持つ）
    t_last: int                 # 最後に受理された語の同じ点


def _field(word: int, field: tuple[int, int]) -> int:
    lsb, width = field
    return word >> lsb & (1 << width) - 1


# 1 回の読みにまとめる CSR の並び。xtrig と inject の窓は隣り合う（1 回の読みが窓をまたいでも、語ごとに番地で振り分けられる）
_POLL_AT = g.SAMPLER_MATCH_COUNT
_POLL_WORDS = (g.SAMPLER_MATCH_UNMATCHED3 - _POLL_AT) // 4 + 1
_INJ_AT = g.XTRIG_INJECT_STATUS
_INJ_WORDS = (g.INJECT_FIRST_TIME_HI - _INJ_AT) // 4 + 1
assert g.XTRIG_BASE + g.XTRIG_SIZE == g.INJECT_BASE, "xtrig と inject の窓が隣り合っていない（status を 1 回で読めない）"


def _word(words: list[int], base: int, address: int) -> int:
    return words[(address - base) // 4]


def _pair(words: list[int], base: int, lo: int) -> int:
    return _word(words, base, lo + 4) << 32 | _word(words, base, lo)


class HaltWatch:
    def __init__(self, dev: Device):
        self.dev = dev
        self._ap = 0
        self._slots = 0
        self.restarts = False       # configure で再開の要求を表に入れた（arm_and_restart が使える）
        self._start = 0             # inject.start の first・last

    @property
    def can_restart(self) -> bool:
        """この回路が、再開を FPGA が書いて門の後だけ比べる経路を持つ（ケイパビリティ watch_start）。"""
        return bool(self.dev.capabilities().watch_start)

    def configure(self, *, ap: int, watch: list[int], halted_mask: int, halted_value: int,
                  halt_writes: list[tuple[int, int]], restart_writes: list[tuple[int, int]] | None = None) -> None:
        """watch: 見張る語の番地（例 各コアの DBGDSCR）。halted_mask / halted_value: 止まった判定。halt_writes: 1 つが止まったら
        書く (番地, 値) の列（例 全コアの DBGDRCR ← HRQ。止まったコアに書いても無害なので全部に書く）。動かすのは arm。
        最後にクロストリガの自分の経路（源 SAMPLER_MATCH → 割り込み書き込み。他の経路は変えない）を足し、`release` まで残す。
        restart_writes: FPGA が書く再開の要求の (番地, 値) の列（例 全コアの DBGDRCR ← RRQ | CSE）。`can_restart` の回路だけ
        （無ければ ValueError）。表の halt_writes の後ろに入れ、`arm_and_restart` が始動の列として書かせる。halted_mask・
        halted_value は、再開の後だけ成り立つ値（例 HALTED と RESTARTED が両方 1）を呼び出し側が渡す。"""
        if not watch or len(watch) > SLOTS:
            raise ValueError("watch: 1〜%d 語（サンプラのか所の数）が要る: %d" % (SLOTS, len(watch)))
        if not halt_writes or len(halt_writes) > WORDS:
            raise ValueError("halt_writes: 1〜%d 語（割り込み書き込みの語の数）が要る: %d" % (WORDS, len(halt_writes)))
        restart_writes = list(restart_writes or [])
        if len(halt_writes) + len(restart_writes) > WORDS:
            raise ValueError("halt_writes と restart_writes: 合わせて %d 語まで（割り込み書き込みの表）: %d + %d"
                             % (WORDS, len(halt_writes), len(restart_writes)))
        if restart_writes and not self.can_restart:
            raise ValueError("restart_writes: this device has no FPGA restart path (capability watch_start)")
        if any(a & 3 for a in watch) or any(a & 3 for a, _ in [*halt_writes, *restart_writes]):
            raise ValueError("番地は語の境界（4 の倍数）でなければならない（aligned）")
        self.disarm()
        for _ in range(BUSY_READS):                                   # 書き込みの最中に表を書き換えない（止めてから変える）
            if not _field(self.dev.read32(g.XTRIG_INJECT_STATUS), g.XTRIG_INJECT_STATUS_BUSY):
                break
        else:
            raise HaltWatchError("the injection is still busy after %d status reads: the list was not changed" % BUSY_READS)
        self._ap, self._slots = ap, len(watch)
        enable = 1 << g.SAMPLER_SLOT_CONFIG_ENABLE[0]
        for k in range(SLOTS):
            self.dev.write(g.SAMPLER_SLOT_INDEX, [k, watch[k], enable, 1] if k < len(watch) else [k, 0, 0, 1])
        self.dev.write32(g.SAMPLER_PERIOD, 1)                         # できるだけ速く（前の巡回が終わり次第、次）
        caps = self.dev.capabilities()
        if caps.dap_line_time:
            # JTAG の誤りの確認は語ごと（K = 1）。まとめると止まったコアに気づくのが遅れる（stop-precision §3.6）。
            # 前の使い手が K > 1 にしていても、見張りは必ず 1 にする
            self.dev.write32(g.SAMPLER_CHECK, 1 << g.SAMPLER_CHECK_WORDS[0])
        differ = 0
        for a in watch:
            differ |= a ^ watch[0]
        self.dev.write32(g.SAMPLER_MATCH_ADDR_MASK, 0xFFFFFFFF & ~differ)
        self.dev.write(g.SAMPLER_MATCH_ADDR, [watch[0], halted_mask, halted_value])
        (a0, v0), rest = halt_writes[0], halt_writes[1:]
        self.dev.write(g.XTRIG_INJECT_AP, [ap, a0, v0])
        for i, (a, v) in enumerate(rest, start=1):
            self.dev.write(g.INJECT_ENTRY, [i, a, v, 1])
        self.dev.write32(g.INJECT_CONTROL, len(rest))
        first = len(halt_writes)
        for i, (a, v) in enumerate(restart_writes, start=first):
            self.dev.write(g.INJECT_ENTRY, [i, a, v, 1])
        self.restarts = bool(restart_writes)
        self._start = (first << g.INJECT_START_FIRST[0] | (first + len(restart_writes) - 1) << g.INJECT_START_LAST[0]
                       if restart_writes else 0)
        route = self.dev.read32(g.XTRIG_ROUTE)                        # 値の一致はまだ無効（disarm 済み）: 足しても発火しない
        self.dev.write32(g.XTRIG_ROUTE, route | int(Source.SAMPLER_MATCH) << g.XTRIG_ROUTE_TO_INJECT[0])

    def arm(self) -> None:
        """見張りを始める: 値の一致を 0→1（数と状態を消す）、once（最初の 1 回だけ発火: FPGA が止めた残りのコアで発火し直して
        最初のコアの記録を上書きしない）を 1 回の往復（EXCHANGE）で → サンプラを動かす。経路は configure で足してある。
        **始めた時にすでに止まっていたコアでは、走っているのを読むまで発火しない**（その前を見ていない）。全コアが止まっている
        間に始めれば、再開の後に走っている値を読んでから止まったコアで発火し、読む前に止まったコアは `seen_running` が False の
        まま残る（呼び出し側が確かめる）。"""
        self.dev.exchange(g.SAMPLER_MATCH_CONTROL, [0, 1 | 1 << g.SAMPLER_MATCH_CONTROL_ONCE[0]])
        self.dev.write32(g.SAMPLER_CONTROL, 1 | self._ap << g.SAMPLER_CONTROL_AP[0])

    def arm_and_restart(self) -> None:
        """見張りを始め、FPGA に再開を書かせる（`restarts` のときだけ。全コアが止まっている間に呼ぶ）: 値の一致を 0→1（once・
        after_start・first_sample）→ サンプラを動かす → 始動の列を要求（3 回の往復）。値の一致は、始動の列が成功した後に始めた
        サンプラの読みの標本だけを比べる。終わったかと結果は `restart_status`（失敗なら門は開かず、発火しない）。"""
        if not self.restarts:
            raise HaltWatchError("arm_and_restart: no restart writes were configured")
        mode = (1 | 1 << g.SAMPLER_MATCH_CONTROL_ONCE[0] | 1 << g.SAMPLER_MATCH_CONTROL_AFTER_START[0]
                | 1 << g.SAMPLER_MATCH_CONTROL_FIRST_SAMPLE[0])
        self.dev.exchange(g.SAMPLER_MATCH_CONTROL, [0, mode])
        self.dev.write32(g.SAMPLER_CONTROL, 1 | self._ap << g.SAMPLER_CONTROL_AP[0])
        self.dev.write32(g.INJECT_START, self._start | 1 << g.INJECT_START_GO[0])

    def restart_status(self) -> HaltWatchRestart:
        """始動の列の状態（1 回の読み）。"""
        st, f_lo, f_hi, l_lo, l_hi = self.dev.read(g.INJECT_START_STATUS, 5)
        return HaltWatchRestart(pending=bool(_field(st, g.INJECT_START_STATUS_PENDING)),
                                result=RESULTS[_field(st, g.INJECT_START_STATUS_RESULT)],
                                written=_field(st, g.INJECT_START_STATUS_WRITTEN_WORDS),
                                count=_field(st, g.INJECT_START_STATUS_COUNT),
                                t_first=f_hi << 32 | f_lo, t_last=l_hi << 32 | l_lo)

    def disarm(self) -> None:
        """見張りをやめる（ホストがコアを個別に止める・ステップする前に必ず）。値の一致を先に止める: その後は発火しない。それより
        前の発火は、経路が残っているので必ず割り込み書き込みに届き、count に残る（外した後の `poll` で、外す前に発火したかが
        分かる）。その後にサンプラを止める（ホストの DAP の転送と線を取り合わない）。"""
        self.dev.write32(g.SAMPLER_MATCH_CONTROL, 0)
        self.dev.write32(g.SAMPLER_CONTROL, self._ap << g.SAMPLER_CONTROL_AP[0])

    def release(self) -> None:
        """見張りをやめ、クロストリガの自分の経路を外す（他の経路は変えない）。使い終わる（接続を閉じる）ときに。"""
        self.disarm()
        route = self.dev.read32(g.XTRIG_ROUTE)
        self.dev.write32(g.XTRIG_ROUTE, route & ~(int(Source.SAMPLER_MATCH) << g.XTRIG_ROUTE_TO_INJECT[0]))

    def poll(self) -> HaltWatchPoll:
        """値の一致の状態を 1 回の読みで（JTAG は使わない）。"""
        w = self.dev.read(_POLL_AT, _POLL_WORDS)
        unmatched = 0
        for k in range(4):
            unmatched |= _word(w, _POLL_AT, g.SAMPLER_MATCH_UNMATCHED0 + 4 * k) << 32 * k
        fire = _word(w, _POLL_AT, g.SAMPLER_MATCH_FIRE)
        return HaltWatchPoll(matched=_word(w, _POLL_AT, g.SAMPLER_MATCH_COUNT) > 0,
                             index=_field(fire, g.SAMPLER_MATCH_FIRE_INDEX),
                             fire_first=bool(_field(fire, g.SAMPLER_MATCH_FIRE_FIRST)),
                             t_fire48=_pair(w, _POLL_AT, g.SAMPLER_MATCH_TIME_LO),
                             t_prev48=_pair(w, _POLL_AT, g.SAMPLER_MATCH_PREV_LO),
                             seen_running=tuple(bool(unmatched >> 16 * k & 1) for k in range(self._slots)))

    def status(self, poll: HaltWatchPoll | None = None) -> HaltWatchStatus:
        """発火と割り込み書き込みの状態。poll（直前に読んだもの）を渡せばその読みを使う。発火していなければ読みは poll の 1 回、
        発火していれば割り込み書き込みの状態の 1 回を足す（受理された語が無いときだけ、時刻の折り返しを外すためのタイムベースの
        読みをもう 1 回）。"""
        p = self.poll() if poll is None else poll
        if not p.matched:
            return HaltWatchStatus(False, None, "ok", 0, p.t_prev48, p.t_fire48, 0, 0, p.seen_running)
        w = self.dev.read(_INJ_AT, _INJ_WORDS)
        inject = _word(w, _INJ_AT, g.XTRIG_INJECT_STATUS)
        busy = bool(_field(inject, g.XTRIG_INJECT_STATUS_BUSY))
        written = _field(_word(w, _INJ_AT, g.INJECT_STATUS), g.INJECT_STATUS_WRITTEN_WORDS)
        t_last = _pair(w, _INJ_AT, g.XTRIG_INJECT_TIME_LO)
        # 値の一致の時刻は 48 ビット（イベントと同じ幅）: それより後の 64 ビットのタイムベースの値で折り返しを外す。書き終えた
        # 要求で語が受理されていれば、最後の書き込みの時刻（発火の後）がその値になる（読みを足さない）。無ければ今の時刻を読む
        reference = t_last if written and not busy else self.dev.time()
        return HaltWatchStatus(fired=not busy, core=p.index // 16 if not busy else None,
                               result=RESULTS[_field(inject, g.XTRIG_INJECT_STATUS_RESULT)], written=written,
                               t_prev=unwrap48(p.t_prev48, reference), t_fire=unwrap48(p.t_fire48, reference),
                               t_first_write=_pair(w, _INJ_AT, g.INJECT_FIRST_TIME_LO), t_last_write=t_last,
                               seen_running=p.seen_running, fire_first=p.fire_first)
