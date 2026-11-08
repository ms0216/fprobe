"""ロジアナの垂れ流し RLE の「トークン形式」: 符号の定義（定数）・参照符号器・復号器。

仕様: docs/superpowers/specs/2026-11-07-la-rle2-design.md §3。変化 1 回を（ふつうは）1 バイトにする。純粋・決定的で、デバイスにも
時間にも触れない。符号器はサンプル列だけで決まる（データから閾値を推定しない。k ≥ 4 で VALUE にする境も符号の定義の定数）。
RTL（段階 2 以降）はこの符号器とバイト単位で一致させる。定数の置き場: 段階 0〜1 はこのモジュールが正。RTL と共有するときは
`fprobe_gw/csr/fprobe_map.py` に移して `gen` で出す（設計書 §8 段階 2）。

形式（1 拍 = 16 バイト。**トークンも、同じサンプルの出来事の束（グループ）も拍をまたがない**。入らなければ残りを NOP で埋める）:
  頭   0 cccc ddd   最上位ビット 0。ch（cccc）+ 間隔の欄（ddd）
  尾   1 xxxxxxx    最上位ビット 1。頭の後ろに 0 個以上
  TOGGLE       ddd = 0〜6              ch cccc が ddd サンプル後に反転（1 バイト）
  TOGGLE_LONG  ddd = 7 + 尾 T1（bit6 = 0）  ch cccc が 7 + P サンプル後に反転
  NOP          ddd = 7 で尾が無い       何もしない（拍の端の埋め）
  WAIT         ddd = 7, cccc = 0, T1 の bit6 = 1   時刻だけ P サンプル進める
  VALUE        ddd = 7, cccc = 1, T1 の bit6 = 1   値を P[15:0] にする。P >> 16 サンプル後
  REPEAT       ddd = 7, cccc = 2, T1 の bit6 = 1   直前の B 個（P の bit0 + 1 = 1 か 2）のトークンを N 回（N = P >> 1 + 1）繰り返す。
                    対象は同じ拍の中の、間隔が 0 でない TOGGLE・WAIT（VALUE・REPEAT・束の途中は不可）
  P = T1[5:0] + Σ T_k[6:0] << (6 + 7 (k − 2))（k ≥ 2）。尾は最大 6 個。
流れの最初の出来事は VALUE（初期値）。サンプル s の値 = 時刻が s 以下のすべての出来事を適用した値。同じ時刻の出来事は束ねて、
束ねた後の値が前と違えば変化点 1 つ。

チャンクの状態: 復号は (first, previous) から続きを復号できる。first = 直前の出来事の時刻（最初は 0）、previous = 直前の値
（最初は None）。復号した全サンプル数は「最後の出来事の時刻 + 1」（それ以降は次の出来事か tick の WAIT が届くまで分からない）。
"""

from __future__ import annotations

import ctypes
import os
import threading
from array import array
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Iterable, Iterator
from typing import NamedTuple

try:                                                       # numpy は無くてもよい（純 Python の復号に落ちる）
    import numpy as _np
except ImportError:                                        # pragma: no cover - numpy の無い環境
    _np = None

__all__ = ["BEAT_BYTES", "CONTROL_RLE_FMT_BIT", "DELTA_DIRECT_MAX", "KIND_REPEAT", "KIND_VALUE", "KIND_WAIT", "NOP",
           "TokenDecode", "TokenError", "default_threads", "VALUE_MIN_CHANGES", "WAIT_MAX", "decode_tokens", "encode_tokens",
           "VALUE_MIN_TAILS", "REPEAT_GROUP_MAX", "REPEAT_N_MAX", "TOKEN_FORMAT_VERSION", "c_available", "count_repeats",
           "expand_samples", "token_extent", "expanded_tokens", "iter_decode_tokens"]

# --- 符号の定義として固定した定数（設計書 §3、§7.4） ----------------------------------------------------------

BEAT_BYTES = 16
NOP = 0x07                              # 頭 0 0000 111 で尾が無い
TAIL_FLAG = 0x80                        # 尾の最上位ビット
SPECIAL_FLAG = 0x40                     # 尾 T1 の bit6: 1 = 特殊（WAIT / VALUE / REPEAT）、0 = TOGGLE_LONG
DELTA_EXT = 7                           # 間隔の欄が 7 = 拡張
DELTA_DIRECT_MAX = DELTA_EXT - 1        # 1 バイトで書ける間隔の最大（6）
KIND_WAIT, KIND_VALUE, KIND_REPEAT = 0, 1, 2   # 特殊トークンの種類（頭の cccc）
T1_BITS, TAIL_BITS, MAX_TAILS = 6, 7, 6        # 尾 T1 の P の幅、2 個目以降の尾の P の幅、尾の最大数
PAYLOAD_BITS = T1_BITS + TAIL_BITS * (MAX_TAILS - 1)   # P の最大幅（41）
VALUE_MIN_CHANGES = 4                   # 1 サンプルで変わる ch がこの数以上なら VALUE（TOGGLE は 3 個まで）
WAIT_MAX = (1 << 40) - 1                # 符号器が 1 つの WAIT に入れる時間の最大（回路の数えの幅 40 ビット）
VALUE_MIN_TAILS = 3                     # VALUE の尾の最小数（値の 16 ビットが入る幅。間隔が小さくても 4 バイト。設計書 §3.2 の表）
VALUE_DELTA_BITS = PAYLOAD_BITS - 16    # VALUE の間隔の幅（P の上位）
REPEAT_GROUP_MAX = 3                    # REPEAT の対象にする束の最大バイト数（1 つの TOGGLE で 3 バイト以下 = 間隔 8198 以下。対象 2 つ + REPEAT が 1 拍に必ず入る）
REPEAT_N_MAX = 1 << 40                  # 1 つの REPEAT にまとめる繰り返しの回数の最大（P の幅 41 ビット。超えたら閉じて新しい REPEAT にする）
TOKEN_FORMAT_VERSION = 1                # 形式の版（TLV LA_RLE_TOKEN_INFO。fprobe_gw/csr/fprobe_map.py の LA_TOK_FORMAT_VERSION と一致）
CONTROL_RLE_FMT_BIT = 17                # CSR la_deep.control の rle_fmt（設計書 §7.2。段階 2 で fprobe_map.py に移る。0 = レコード、1 = トークン）


class TokenError(ValueError):
    """トークン列が形式に合わない（黙って補わず、位置つきで報告する）。"""


# --- 参照符号器 ----------------------------------------------------------------------------------------

def _tails(payload: int, special: bool, at_least: int = 1) -> bytes:
    """P の尾（T1 + 2 個目以降）。special なら T1 の bit6 を立てる。尾の数は P が収まる最小（at_least 個以上。上の桁は 0 で埋める）。"""
    if payload >> PAYLOAD_BITS:
        raise ValueError(f"P が {PAYLOAD_BITS} ビットに収まらない: {payload}")
    out = bytearray([TAIL_FLAG | (SPECIAL_FLAG if special else 0) | payload & (1 << T1_BITS) - 1])
    payload >>= T1_BITS
    while payload or len(out) < at_least:
        out.append(TAIL_FLAG | payload & (1 << TAIL_BITS) - 1)
        payload >>= TAIL_BITS
    return bytes(out)


def _toggle(ch: int, delta: int) -> bytes:
    if delta <= DELTA_DIRECT_MAX:
        return bytes([ch << 3 | delta])
    return bytes([ch << 3 | DELTA_EXT]) + _tails(delta - DELTA_EXT, False)


def _wait(delta: int) -> bytes:
    return bytes([KIND_WAIT << 3 | DELTA_EXT]) + _tails(delta, True)


def _value(value: int, delta: int) -> bytes:
    return bytes([KIND_VALUE << 3 | DELTA_EXT]) + _tails(delta << 16 | value, True, VALUE_MIN_TAILS)


_VALUE_DELTA_MAX = (1 << VALUE_DELTA_BITS) - 1
_TOGGLE_DELTA_MAX = DELTA_EXT + (1 << PAYLOAD_BITS) - 1


class _Packer:
    """拍への詰め: 束（グループ）は拍をまたがない。入らなければ残りを NOP で埋めて拍を出す。"""

    def __init__(self) -> None:
        self.beats: list[bytes] = []
        self.cur = bytearray()
        self.pad = 0
        self.last_flush = 0                  # 最後に拍を出した時刻（サンプル番号）
        self.events_since_flush = False

    def flush(self, at: int) -> None:
        if self.cur:
            self.pad += BEAT_BYTES - len(self.cur)
            self.beats.append(bytes(self.cur) + bytes([NOP]) * (BEAT_BYTES - len(self.cur)))
            self.cur = bytearray()
        self.last_flush = at
        self.events_since_flush = False

    def put(self, group: bytes, at: int) -> None:
        if len(group) > BEAT_BYTES:
            raise ValueError("1 つの束が 1 拍に入らない")
        if len(self.cur) + len(group) > BEAT_BYTES:
            self.flush(at)
        self.cur += group
        self.events_since_flush = True
        if len(self.cur) == BEAT_BYTES:
            self.beats.append(bytes(self.cur))
            self.cur = bytearray()
            self.last_flush = at
            self.events_since_flush = False


def _repeat(pattern: int, count: int) -> bytes:
    """REPEAT: 直前の pattern（1 か 2）個のトークンを count 回（>= 1）繰り返す。P = (pattern − 1) + 2 (count − 1)。"""
    return bytes([KIND_REPEAT << 3 | DELTA_EXT]) + _tails((pattern - 1) + 2 * (count - 1), True)


class _Encoder:
    """参照符号器の本体。出来事の束（グループ）を作って拍に詰める。repeat のとき、1 つのトークンだけの束（1 ch の反転）が
    同じもの（同じ ch・同じ間隔）で続くのを REPEAT にまとめる（B = 1: 同じ束の繰り返し。B = 2: 2 つの束の繰り返し）。

    REPEAT は「直前の B 個のトークンを N 回」で、対象のトークンと REPEAT を**同じ拍に**置く（復号が拍の中で閉じる。対象は
    REPEAT_GROUP_MAX バイト以下の束だけなので、対象 2 つ + REPEAT は必ず 1 拍に入る）。比べるのは完全一致だけ（推定ではない）。
    REPEAT にまとめて短くなるときだけ使う（N 回分のバイト数 > REPEAT のバイト数）。決まりは定数（B ≤ 2、持つ束は直近の 2 つまで、N は REPEAT_N_MAX まで）。"""

    def __init__(self, repeat: bool) -> None:
        self.pk = _Packer()
        self.repeat = repeat
        self.held: list[tuple[bytes, tuple | None]] = []     # まだ拍に置いていない、繰り返しになりうる束（直近の 2 つまで）
        self.pattern: list[tuple[bytes, tuple]] = []         # 繰り返し中の B 個の束
        self.n = 0                                           # 繰り返しの回数（完了した分）
        self.partial: list[tuple[bytes, tuple]] = []         # 繰り返しの途中まで一致した束（B = 2 のとき 0〜1 個）
        self.repeats = 0                                     # 出した REPEAT の数

    # 束を出す口: sig = (ch, 間隔)（1 つのトークンだけの束）か None（繰り返しにしない束）
    def submit(self, group: bytes, sig: tuple | None, at: int) -> None:
        if self.pattern and self.n >= REPEAT_N_MAX:          # 回数が上限に達していたら、次の束の前に閉じる
            self.close(at)
        if not self.repeat or sig is None:
            self.settle(at)
            self.pk.put(group, at)
            return
        if self.pattern:                                     # 繰り返し中
            want = self.pattern[len(self.partial)][1]
            if sig == want:
                self.partial.append((group, sig))
                if len(self.partial) == len(self.pattern):
                    self.n += 1
                    self.partial = []
                return
            self.close(at)                                   # 一致しない: 閉じて、この束は最初から
        held = self.held
        if held and held[-1][1] == sig:                      # B = 1
            for g, _ in held[:-1]:
                self.pk.put(g, at)
            self.pattern, self.n, self.partial, self.held = [held[-1]], 1, [], []
        elif len(held) == 2 and held[0][1] == sig:           # B = 2（held[0] != held[1]）
            self.pattern, self.n, self.partial, self.held = list(held), 0, [(group, sig)], []
        else:
            held.append((group, sig))
            if len(held) > 2:
                self.pk.put(held.pop(0)[0], at)

    def close(self, at: int) -> None:
        """繰り返しを閉じる: まとめて短くなれば REPEAT、そうでなければ並べる。途中まで一致した束はそのまま続ける。"""
        pattern, n, partial = self.pattern, self.n, self.partial
        self.pattern, self.n, self.partial = [], 0, []
        body = b"".join(g for g, _ in pattern)
        if n:
            rep = _repeat(len(pattern), n)
            if len(rep) < n * len(body):                     # 対象 + REPEAT は REPEAT_GROUP_MAX により必ず 1 拍に入る
                self.pk.put(body + rep, at)
                self.repeats += 1
            else:
                for _ in range(n + 1):
                    for g, _sig in pattern:
                        self.pk.put(g, at)
        else:
            for g, _sig in pattern:
                self.pk.put(g, at)
        for g, _ in partial:
            self.pk.put(g, at)

    def settle(self, at: int) -> None:
        """持っている束・繰り返しを全部拍に置く（繰り返しにならない束・tick・停止の前）。"""
        if self.pattern:
            self.close(at)
        for g, _ in self.held:
            self.pk.put(g, at)
        self.held = []


def encode_tokens(samples: Iterable[int], *, width: int = 16, tick_samples: int | None = None,
                  resync_at: Iterable[int] = (), repeat: bool = False) -> bytes:
    """サンプル列（各要素は width ビットの値。1 要素 = 1 サンプル）→ トークン形式の拍の列（16 バイトの倍数）。

    - 最初のサンプルは VALUE（Δ = 0）。値が変わらない間は何も出さない。1 サンプルで変わる ch が k 個: k ≤ 3 なら TOGGLE を k 個
      （ch の小さい順。最初に間隔、残りは 0）、k ≥ 4 なら VALUE を 1 つ。間隔が大きいと尾のバイトが付く。
    - 束は拍をまたがない。入らなければ残りを NOP で埋める。最後の端数の拍も NOP で埋めて出す（停止）。
    - tick_samples（None = 無し）: 最後に拍を出してから tick_samples サンプル経ったら、作りかけの拍を NOP で埋めて出す。その間に出来事が
      1 つも無ければ、先に WAIT（時刻が進んだことを届ける）を出す。
    - resync_at: これらのサンプル番号で、新しい拍の先頭に VALUE（そのサンプルの値。変化でなくても）を置く（欠けの後の再提示）。
    - repeat（既定 False = 段階 2 の最初の RTL の対象）: 同じ束の繰り返しを REPEAT にまとめる（_Encoder の説明）。
    - 間隔が 1 つのトークンに入らないほど長いときは、WAIT で分ける。"""
    if tick_samples is not None and tick_samples < 1:
        raise ValueError(f"tick_samples は 1 以上: {tick_samples}")
    resync = frozenset(resync_at)
    mask = (1 << width) - 1
    enc = _Encoder(repeat)
    pk = enc.pk
    previous = None                          # 直前のサンプルの値
    last_event = None                        # 直前の出来事の時刻
    any_event = False                        # 最後に拍を出してから出来事があった（置いていない束も数える）
    flushed = 0                              # pk.last_flush の見え方（拍が出たら出来事の印を下げる）
    s = -1
    for s, raw in enumerate(samples):
        value = int(raw)
        if value & ~mask:
            raise ValueError(f"サンプル {s} の値 {value:#x} が {width} ビットに収まらない")
        forced = previous is None or s in resync
        if forced or value != previous:
            x = value ^ (previous or 0)
            bits = [ch for ch in range(width) if x >> ch & 1]
            use_value = forced or len(bits) >= VALUE_MIN_CHANGES
            delta = s if last_event is None else s - last_event
            if s in resync:
                enc.settle(s)
                pk.flush(s)                  # 新しい拍の先頭から始める
            limit = _VALUE_DELTA_MAX if use_value else _TOGGLE_DELTA_MAX
            while delta > limit:             # 長い間隔は WAIT で分ける（残りの間隔は 1 以上: 束が WAIT と同じ時刻にならない）
                step = min(delta - 1, WAIT_MAX)
                enc.submit(_wait(step), None, s)
                delta -= step
            if use_value:
                enc.submit(_value(value, delta), None, s)
            else:
                group = b"".join(_toggle(ch, delta if i == 0 else 0) for i, ch in enumerate(bits))
                enc.submit(group, (bits[0], delta) if len(bits) == 1 and len(group) <= REPEAT_GROUP_MAX else None, s)
            last_event = s
            any_event = True
        previous = value
        if pk.last_flush != flushed:         # 拍が満ちて出た
            flushed = pk.last_flush
            any_event = bool(enc.held or enc.pattern or pk.cur)
        if tick_samples is not None and s - pk.last_flush >= tick_samples:
            if not any_event:
                enc.submit(_wait(s - last_event), None, s)
                last_event = s
            enc.settle(s)
            pk.flush(s)
            flushed, any_event = pk.last_flush, False
    enc.settle(s)
    pk.flush(s)
    return b"".join(pk.beats)


def count_repeats(data) -> int:
    """拍の列にある REPEAT トークンの数（試験・統計用）。"""
    return decode_tokens(data).repeat_tokens


# --- 復号 ----------------------------------------------------------------------------------------------

class TokenDecode(NamedTuple):
    """復号の結果。values / starts は変化点（値と開始時刻 = first 以降の通しの時刻）。numpy があれば numpy の配列
    （値 uint16・開始 uint64）、無ければ array（"H"・"Q"）。"""
    values: object
    starts: object
    end_time: int                            # 最後の出来事の時刻（次のチャンクの first）。出来事が無ければ first
    last_value: int | None                   # 最後の値（次のチャンクの previous）。出来事が無ければ previous
    pad_bytes: int                           # NOP の数
    tokens: int                              # NOP・REPEAT を除くトークンの数（REPEAT で繰り返した分も数える）
    repeat_tokens: int = 0                   # REPEAT トークンの数


def _check_beats(data) -> int:
    n = len(data)
    if n % BEAT_BYTES:
        raise TokenError(f"拍（16 バイト）の倍数でない: {n} バイト")
    return n


_NO_VALUE_FIRST = "流れの最初の出来事が VALUE でない"
_STRADDLE = "先頭の出来事の間隔が 0（同じサンプルの束が拍をまたいでいる。形式は束を拍の中に収める）"


def _bad_repeat(at: int, why: str) -> TokenError:
    return TokenError(f"バイト {at}: REPEAT の対象が不正（{why}）")


# --- スレッドで分けて復号する（設計書 §6.3）-------------------------------------------------------------------
#
# トークンは拍をまたがず、拍の最初は必ず頭なので、拍の境で塊を切れば各部分を独立に parse できる（numpy は GIL を放す）。
# 状態を持ち越すのは「時刻の進み」と「最後の値」だけで、部分ごとの要約（_summary）から順に求める（安い）。
# 求めた (first, previous) を各部分の変化点の計算に渡せば、直列で復号したのと要素単位で同じ結果になる。誤りのある塊は
# 直列の道でもう一度復号して、同じ TokenError（同じ文面）を出す。

PARALLEL_MIN_BYTES = 512 * 1024         # この大きさ未満の塊は分けない（スレッドの費用のほうが大きい）
PIECE_MIN_BYTES = 128 * 1024            # 1 つの部分の最小の大きさ
MAX_THREADS = 4                         # threads の既定の上限

_POOL_SIZE = max(MAX_THREADS, os.cpu_count() or 1)
_pool_lock = threading.Lock()
_pool: ThreadPoolExecutor | None = None


def default_threads() -> int:
    """threads の既定（この PC の論理コア数、上限 MAX_THREADS）。"""
    return max(1, min(os.cpu_count() or 1, MAX_THREADS))


def _executor() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=_POOL_SIZE, thread_name_prefix="la-tokens")
        return _pool


def _bounds(n: int, threads: int | None) -> list[int] | None:
    """拍の境の切れ目 [0, ..., n]。分けないなら None。"""
    t = default_threads() if threads is None else threads
    if t <= 1 or n < PARALLEL_MIN_BYTES:
        return None
    k = min(t, n // PIECE_MIN_BYTES, _POOL_SIZE)
    if k < 2:
        return None
    step = n // k // BEAT_BYTES * BEAT_BYTES
    return [i * step for i in range(k)] + [n]


def _parse_pieces(data, width: int, bounds: list[int]):
    b = _np.frombuffer(data, dtype=_np.uint8)

    def work(i: int):
        p = _parse(b[bounds[i]:bounds[i + 1]], width)
        return p, (_summary(p) if len(p.delta) else None)

    return list(_executor().map(work, range(len(bounds) - 1)))


def _chain(parsed, first: int, previous: int | None):
    """部分ごとの (first, previous) を前から順に求める。誤りは TokenError（直列の道が同じ誤りを出す）。"""
    starts, started = [], False
    for p, s in parsed:
        starts.append((first, previous))
        if s is None:
            continue
        if not started:                                       # 先頭の出来事の確認は塊の最初のトークンだけ（直列と同じ）
            _check_start(p, previous)
            started = True
        dsum, has_value, v = s
        first += dsum
        previous = v if has_value else previous ^ v
    return starts, first, previous


def _decode_parallel(data, first: int, previous: int | None, width: int, bounds: list[int]) -> TokenDecode:
    parsed = _parse_pieces(data, width, bounds)
    states, end, last = _chain(parsed, first, previous)
    live = [i for i, (p, s) in enumerate(parsed) if s is not None]
    pad = sum(p.pad for p, _ in parsed)
    repeats = sum(len(p.rep) for p, _ in parsed)
    if not live:
        return TokenDecode(_np.empty(0, _np.uint16), _np.empty(0, _np.uint64), first, previous, pad, 0, repeats)
    pool = _executor()
    expanded = list(pool.map(lambda i: _expand(parsed[i][0]), live))
    tokens = [len(p.delta) for p in expanded]
    at = [sum(tokens[:k]) for k in range(len(live))]
    big_v, big_t = _np.empty(sum(tokens), _np.uint16), _np.empty(sum(tokens), _np.uint64)   # 間引かなければそのまま結果になる

    def changes(k: int) -> TokenDecode:
        i, lo = live[k], at[k]
        return _changes(expanded[k], states[i][0], states[i][1], 0, (big_v[lo:lo + tokens[k]], big_t[lo:lo + tokens[k]]))

    parts = list(pool.map(changes, range(len(live))))
    sizes = [len(d.values) for d in parts]
    if sizes == tokens:                                       # どの部分も間引いていない: 作った配列がそのまま結果
        values, starts = big_v, big_t
    else:
        total = sum(sizes)
        values, starts = _np.empty(total, _np.uint16), _np.empty(total, _np.uint64)
        offsets = [sum(sizes[:k]) for k in range(len(parts))]

        def copy(k: int) -> None:
            values[offsets[k]:offsets[k] + sizes[k]] = parts[k].values
            starts[offsets[k]:offsets[k] + sizes[k]] = parts[k].starts

        list(pool.map(copy, range(len(parts))))
    return TokenDecode(values, starts, end, last, pad, sum(tokens), repeats)


# --- C 版（任意の高速版。無くても numpy の版が同じ結果で動く） ------------------------------------------------------
#
# 尾の混ざる列は numpy では呼び出しの数が支配して（約 30 回/塊、GIL の取り合い）1 億トークン/s に届かないので、同じ復号を
# C で書いた共有ライブラリ（_la_tokens_c.c、tools/build_la_tokens_c.py で作る）があれば ctypes（GIL を放す）で呼ぶ。
# 形式の違反・未対応の範囲は C が 1 を返し、numpy の版が同じ文面の TokenError を出す（判定を二重に持たない）。
# 無い・読めない・FPROBE_LA_TOKENS_C=0 のときは使わない。結果は numpy の版と要素単位で一致する（試験）。

_C_ABI = 1
_C_MAX_CAP = 1 << 28                    # REPEAT の展開でこれを超える変化点が要りうるときは numpy の版に任せる


class _CRes(ctypes.Structure):
    _fields_ = [("pad", ctypes.c_uint64), ("tokens", ctypes.c_uint64), ("repeats", ctypes.c_uint64),
                ("dsum", ctypes.c_uint64), ("nout", ctypes.c_uint64), ("last", ctypes.c_uint32),
                ("has_value", ctypes.c_int32), ("first_is_value", ctypes.c_int32), ("first_delta_zero", ctypes.c_int32)]


def _load_c():
    """C 版を読む。使えなければ None。"""
    if _np is None or os.environ.get("FPROBE_LA_TOKENS_C") == "0":
        return None
    here = Path(__file__).resolve().parent
    for ext in (".dll", ".so", ".dylib"):
        path = here / f"_la_tokens_c{ext}"
        if not path.exists():
            continue
        try:
            lib = ctypes.CDLL(str(path))
            lib.la_tok_abi.restype = ctypes.c_int
            if lib.la_tok_abi() != _C_ABI:
                return None
            fn = lib.la_tok_run
            fn.restype = ctypes.c_int
            fn.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_uint64, ctypes.c_int, ctypes.c_uint32,
                           ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64, ctypes.POINTER(_CRes)]
            return fn
        except (OSError, AttributeError):
            return None
    return None


_C = _load_c()


def c_available() -> bool:
    """C 版が使える（読めている）か。"""
    return _C is not None


def _c_args_ok(first: int, previous: int | None, width: int) -> bool:
    return 0 <= first < (1 << 62) and (previous is None or 0 <= previous <= 0xFFFF) and 1 <= width <= 16


def _c_input(data):
    b = data if isinstance(data, _np.ndarray) else _np.frombuffer(data, dtype=_np.uint8)
    if b.dtype != _np.uint8 or b.ndim != 1 or not b.flags.c_contiguous:
        return None
    return b


def _c_call(base: int, nbytes: int, mode: int, first: int, previous: int | None, width: int, ov: int, os_: int, cap: int):
    res = _CRes()
    rc = _C(base, nbytes, mode, first, 0 if previous is None else 1, 0 if previous is None else previous, width,
            ov, os_, cap, ctypes.byref(res))
    return rc, res


def _c_start_ok(res: _CRes, previous: int | None) -> bool:
    """_check_start と同じ条件（流れの先頭は VALUE から、続きの先頭の間隔は 0 でない）。"""
    return bool(res.first_is_value) if previous is None else not res.first_delta_zero


def _c_summaries(base: int, bounds: list[int], width: int):
    def work(i: int):
        return _c_call(base + bounds[i], bounds[i + 1] - bounds[i], 0, 0, None, width, 0, 0, 0)

    out = list(_executor().map(work, range(len(bounds) - 1)))
    return None if any(rc for rc, _ in out) else [r for _, r in out]


def _c_chain(summaries, first: int, previous: int | None):
    """部分ごとの (first, previous) を前から順に求める。開始の確認に通らない・先頭以外の部分の先頭の間隔が 0 のときは None
    （numpy の版に任せる）。"""
    states, started = [], False
    for r in summaries:
        states.append((first, previous))
        if r.tokens == 0:
            continue
        if not started:
            if not _c_start_ok(r, previous):
                return None
            started = True
        elif r.first_delta_zero:
            return None
        first += r.dsum
        previous = r.last if r.has_value else previous ^ r.last
    return states, first, previous


def _trim(a, m: int):
    return a[:m].copy() if m * 2 < len(a) else a[:m]


def _c_decode(data, n: int, first: int, previous: int | None, width: int, threads: int | None) -> TokenDecode | None:
    if _C is None or not _c_args_ok(first, previous, width):
        return None
    b = _c_input(data)
    if b is None:
        return None
    base = b.ctypes.data
    bounds = _bounds(n, threads)
    empty = TokenDecode(_np.empty(0, _np.uint16), _np.empty(0, _np.uint64), first, previous, 0, 0)
    if bounds is None:
        cap = n
        while True:
            ov, os_ = _np.empty(cap, _np.uint16), _np.empty(cap, _np.uint64)
            rc, res = _c_call(base, n, 1, first, previous, width, ov.ctypes.data, os_.ctypes.data, cap)
            if rc == 2 and res.tokens > cap and res.tokens <= _C_MAX_CAP:
                cap = res.tokens                                   # REPEAT の展開で足りなかった: 必要な数で取り直す
                continue
            break
        if rc:
            return None
        if res.tokens == 0:
            return empty._replace(pad_bytes=res.pad, repeat_tokens=res.repeats)
        if not _c_start_ok(res, previous):
            return None
        m = res.nout
        return TokenDecode(_trim(ov, m), _trim(os_, m), first + res.dsum, res.last, res.pad, res.tokens, res.repeats)
    sums = _c_summaries(base, bounds, width)
    if sums is None:
        return None
    chained = _c_chain(sums, first, previous)
    if chained is None:
        return None
    states, end, last = chained
    pad, repeats = sum(r.pad for r in sums), sum(r.repeats for r in sums)
    live = [i for i, r in enumerate(sums) if r.tokens]
    if not live:
        return empty._replace(pad_bytes=pad, repeat_tokens=repeats)
    total = sum(sums[i].tokens for i in live)
    if total > _C_MAX_CAP:
        return None
    offsets, acc = {}, 0
    for i in live:
        offsets[i] = acc
        acc += sums[i].tokens
    ov, os_ = _np.empty(total, _np.uint16), _np.empty(total, _np.uint64)
    ov_base, os_base = ov.ctypes.data, os_.ctypes.data

    def work(i: int):
        off, f, p = offsets[i], *states[i]
        return _c_call(base + bounds[i], bounds[i + 1] - bounds[i], 1, f, p, width, ov_base + off * 2, os_base + off * 8,
                       sums[i].tokens)

    out = dict(zip(live, _executor().map(work, live)))
    if any(rc for rc, _ in out.values()):
        return None
    pos = 0
    for i in live:                                                 # 部分ごとの結果を前に詰める（前から順なので重ならない向き）
        m, off = out[i][1].nout, offsets[i]
        if off != pos and m:
            ctypes.memmove(ov_base + pos * 2, ov_base + off * 2, m * 2)
            ctypes.memmove(os_base + pos * 8, os_base + off * 8, m * 8)
        pos += m
    return TokenDecode(_trim(ov, pos), _trim(os_, pos), end, last, pad, total, repeats)


def _c_extent(data, n: int, first: int, previous: int | None, width: int, threads: int | None):
    if _C is None or not _c_args_ok(first, previous, width):
        return None
    b = _c_input(data)
    if b is None:
        return None
    base = b.ctypes.data
    bounds = _bounds(n, threads)
    if bounds is None:
        rc, r = _c_call(base, n, 0, 0, None, width, 0, 0, 0)
        if rc:
            return None
        sums = [r]
    else:
        sums = _c_summaries(base, bounds, width)
        if sums is None:
            return None
    chained = _c_chain(sums, first, previous)
    if chained is None:
        return None
    _, end, last = chained
    return end - first, last, sum(r.pad for r in sums)


def decode_tokens(data, first: int = 0, previous: int | None = None, *, width: int = 16,
                  threads: int | None = None) -> TokenDecode:
    """拍の列を変化点にする。first = 直前の出来事の時刻、previous = 直前の値（None = 流れの先頭。最初の出来事は VALUE でなければ
    ならず、先頭の値は必ず変化点になる）。誤り（尾で始まる拍・未知の種類・ch が width 以上・束が拍をまたぐ・REPEAT の対象が
    同じ拍に無い等）は TokenError。

    threads: 大きな塊（PARALLEL_MIN_BYTES 以上）を拍の境で分けて復号するスレッドの数（None = default_threads()、1 = 分けない）。
    結果は threads に依らず同じ（要素単位で一致）。"""
    n = _check_beats(data)
    if _np is None:
        return _decode_py(bytes(data), first, previous, width)
    if n == 0:
        return TokenDecode(_np.empty(0, _np.uint16), _np.empty(0, _np.uint64), first, previous, 0, 0)
    got = _c_decode(data, n, first, previous, width, threads)
    if got is not None:
        return got
    bounds = _bounds(n, threads)
    if bounds is not None:
        try:
            return _decode_parallel(data, first, previous, width, bounds)
        except TokenError:
            pass                                              # 直列の道が同じ誤りを同じ文面で出す
    p = _parse(data, width)
    if len(p.delta) == 0:
        return TokenDecode(_np.empty(0, _np.uint16), _np.empty(0, _np.uint64), first, previous, p.pad, 0, len(p.rep))
    _check_start(p, previous)
    return _changes(_expand(p), first, previous, len(p.rep))


def expanded_tokens(data, width: int = 16, *, threads: int | None = None) -> int:
    """REPEAT を展開した後のトークンの数（NOP・REPEAT 自身を除く）。変化点の数の上限（束の途中のトークンや値の変わらない束は
    変化点にならない）。変化点を作らずに求める（REPEAT は回数を掛けるだけ）ので、展開が巨大でもメモリは要らない。"""
    n = _check_beats(data)
    if n == 0:
        return 0
    if _np is None:
        return _decode_py(bytes(data), 0, None, width).tokens          # numpy が無い道は全部をループで復号する（遅い）
    got = _c_tokens(data, n, width, threads)
    if got is not None:
        return got
    p = _parse(data, width)
    return _tokens_of(p)


def _tokens_of(p: "_Parsed") -> int:
    return len(p.delta) - len(p.rep) + (int((p.rep_b.astype(_np.int64) * p.rep_n).sum()) if len(p.rep) else 0)


def _c_tokens(data, n: int, width: int, threads: int | None) -> int | None:
    """C 版の要約の道で、展開後のトークンの数。C 版が使えない・誤りがあれば None（numpy の版が誤りを報告する）。"""
    if _C is None or not _c_args_ok(0, None, width):
        return None
    b = _c_input(data)
    if b is None:
        return None
    base = b.ctypes.data
    bounds = _bounds(n, threads)
    if bounds is None:
        rc, r = _c_call(base, n, 0, 0, None, width, 0, 0, 0)
        return None if rc else int(r.tokens)
    sums = _c_summaries(base, bounds, width)
    return None if sums is None else sum(int(r.tokens) for r in sums)


def token_extent(data, first: int = 0, previous: int | None = None, *, width: int = 16,
                 threads: int | None = None) -> tuple[int, int | None, int]:
    """(時刻の進み = 出来事の間隔の和, 最後の値, NOP の数)。変化点を作らずに求める（受け取りのループで位置を進めるため。
    REPEAT は展開せず、回数を掛けて求める）。threads は decode_tokens と同じ。"""
    n = _check_beats(data)
    if _np is None:
        d = _decode_py(bytes(data), first, previous, width)
        return d.end_time - first, d.last_value, d.pad_bytes
    if n == 0:
        return 0, previous, 0
    got = _c_extent(data, n, first, previous, width, threads)
    if got is not None:
        return got
    bounds = _bounds(n, threads)
    if bounds is not None:
        try:
            parsed = _parse_pieces(data, width, bounds)
            _, end, last = _chain(parsed, first, previous)
            return end - first, last, sum(p.pad for p, _ in parsed)
        except TokenError:
            pass
    p = _parse(data, width)
    if len(p.delta) == 0:
        return 0, previous, p.pad
    _check_start(p, previous)
    dsum, has_value, v = _summary(p)
    return dsum, (v if has_value else previous ^ v), p.pad


class _Parsed(NamedTuple):
    """NOP を除いたトークンの列（REPEAT は 1 つのトークンとして残す。_expand で展開する）。"""
    delta: object                            # トークンごとの間隔（uint8 か uint64。REPEAT は 0）
    xor: object                              # トークンごとの反転する ch のビット（uint16。TOGGLE 以外は 0）
    vpos: object                             # VALUE のトークンの位置（NOP を除いた番号）
    vval: object                             # その値（uint16）
    pad: int                                 # NOP の数
    rep: object                              # REPEAT のトークンの位置
    rep_b: object                            # 対象のトークンの数 B（1 か 2）
    rep_n: object                            # 繰り返しの回数 N


_NO_INDEX = _np.empty(0, _np.intp) if _np is not None else None


def _parse(data, width: int) -> _Parsed:
    """バイト列（bytes か uint8 の配列）をトークンの列にする（先頭の出来事の確認 _check_start は呼び側）。

    速さのための組み立て: (1) NOP（頭で ddd = 7、次のバイトが尾でない）を先にバイト列から外し、以降は NOP を考えない。
    (2) 尾の無い塊（TOGGLE だけ）は頭の位置を求めず、バイト単位の演算だけで済ませる。(3) 型は小さく保つ（間隔は長いトークンが
    あるときだけ uint64）。誤りの報告のバイト位置は元のバイト列の位置（NOP を外す前）。"""
    b = data if isinstance(data, _np.ndarray) else _np.frombuffer(data, dtype=_np.uint8)
    tail = b >= TAIL_FLAG
    bad = _np.flatnonzero(tail[::BEAT_BYTES])
    if len(bad):
        raise TokenError(f"拍 {int(bad[0])} の先頭が尾（頭でなければならない）")
    nopb = (b & 0x87) == DELTA_EXT                            # 頭で ddd = 7
    nopb[:-1] &= ~tail[1:]                                    # 次が尾でなければ NOP（最後のバイトは次が無いので NOP）
    npad = int(_np.count_nonzero(nopb))
    # 外した後の位置 -> 元の位置（誤りの報告と REPEAT の拍の確認にだけ使う。必要なときに求める）
    if npad:
        keep = ~nopb
        b = b[keep]
        tail = b >= TAIL_FLAG

        def orig() -> "_np.ndarray":
            return _np.flatnonzero(keep)

    def at(i: int) -> int:
        return int(orig()[i]) if npad else i

    if not tail.any():                                        # 尾が 1 つも無い（TOGGLE だけ）: 速い道（頭の位置を求めない）
        ch = b >> 3
        _check_channels(ch, width)
        return _Parsed(b & 7, _np.left_shift(_np.uint16(1), ch, dtype=_np.uint16), _NO_INDEX, _np.empty(0, _np.uint16),
                       npad, _NO_INDEX, _NO_INDEX, _NO_INDEX)
    heads = _np.flatnonzero(b < TAIL_FLAG)
    nhead = len(heads)
    gap = _np.empty(nhead, dtype=_np.intp)                    # 次の頭までのバイト数 = 尾の数 + 1
    _np.subtract(heads[1:], heads[:-1], out=gap[:-1])
    gap[-1] = len(b) - heads[-1]
    hb = b[heads]
    dd = hb & 7
    ext = dd == DELTA_EXT
    lng = _np.flatnonzero(ext)                                # 拡張の頭（NOP は外してあるので、尾が 1 個以上ある）
    if int(_np.count_nonzero(gap != 1)) != len(lng):          # 尾のある頭は拡張だけのはず
        wrong = _np.flatnonzero((gap != 1) & ~ext)
        raise TokenError(f"バイト {at(int(heads[wrong[0]]))}: 間隔の欄が 7 でない頭の後ろに尾がある")
    if int(gap.max()) > MAX_TAILS + 1:
        wrong = _np.flatnonzero(gap > MAX_TAILS + 1)
        raise TokenError(f"バイト {at(int(heads[wrong[0]]))}: 尾が {MAX_TAILS} 個を超える")
    ch = hb >> 3
    delta = dd                                                # TOGGLE（間隔の欄 0〜6）は直接。拡張（ddd = 7）は下で尾から求める
    xor = _np.left_shift(_np.uint16(1), ch, dtype=_np.uint16)
    if width < 16:
        _check_channels(ch[~ext], width)
    vpos, vval, rep, rep_pay = _NO_INDEX, _np.empty(0, _np.uint16), _NO_INDEX, _np.empty(0, _np.uint64)
    if len(lng):
        delta = delta.astype(_np.uint64)
        hp = heads[lng]
        tn = gap[lng] - 1
        t1 = b[hp + 1]
        pay = (t1 & ((1 << T1_BITS) - 1)).astype(_np.uint64)
        sel = _np.flatnonzero(tn >= 2)
        for j in range(2, int(tn.max()) + 1):
            if j > 2:
                sel = sel[tn[sel] >= j]
            pay[sel] |= ((b[hp[sel] + j] & ((1 << TAIL_BITS) - 1)).astype(_np.uint64)
                         << _np.uint64(T1_BITS + TAIL_BITS * (j - 2)))
        special = (t1 & SPECIAL_FLAG) != 0
        c = ch[lng]
        sp = _np.flatnonzero(special)                         # 特殊（WAIT・VALUE・REPEAT）は少ないので、長い TOGGLE を全部に置いてから直す
        if len(sp):
            reserved = _np.flatnonzero(c[sp] > KIND_REPEAT)
            if len(reserved):
                k = int(sp[reserved[0]])
                raise TokenError(f"バイト {at(int(hp[k]))}: 予約の種類 {int(c[k])}")
        if width < 16:
            _check_channels(c[~special], width)
        delta[lng] = pay + _np.uint64(DELTA_EXT)
        xor[lng] = _np.left_shift(_np.uint16(1), c, dtype=_np.uint16)
        if len(sp):
            kind, at_sp, pay_sp = c[sp], lng[sp], pay[sp]
            xor[at_sp] = 0
            d = pay_sp.copy()                                 # WAIT: 間隔 = P
            val = kind == KIND_VALUE
            d[val] = pay_sp[val] >> _np.uint64(16)
            isrep = kind == KIND_REPEAT
            d[isrep] = 0
            delta[at_sp] = d
            vpos = at_sp[val]
            vval = (pay_sp[val] & _np.uint64(0xFFFF)).astype(_np.uint16)
            rep = at_sp[isrep]
            rep_pay = pay_sp[isrep]
            if width < 16:
                over = _np.flatnonzero(vval >> _np.uint16(width))
                if len(over):
                    raise TokenError(f"バイト {at(int(hp[sp[_np.flatnonzero(val)[over[0]]]]))}: "
                                     f"VALUE の値 {int(vval[over[0]]):#x} が {width} ビットに収まらない")
    rep_b = (rep_pay & _np.uint64(1)).astype(_np.intp) + 1
    rep_n = (rep_pay >> _np.uint64(1)).astype(_np.int64) + 1
    if len(rep):
        where = orig()[heads] if npad else heads
        _check_repeats(where, delta, vpos, rep, rep_b)
    return _Parsed(delta, xor, vpos, vval, npad, rep, rep_b, rep_n)


def _check_repeats(where, delta, vpos, rep, rep_b) -> None:
    """対象は直前の B 個: 同じ拍の中、VALUE・REPEAT でなく、間隔が 0 でない。最初に不正な REPEAT を、確認の順（拍・種類・間隔）で報告する。
    where = トークンごとの元のバイト位置。"""
    special = _np.zeros(len(delta), dtype=bool)               # VALUE・REPEAT
    special[vpos] = True
    special[rep] = True
    b2 = rep_b == 2
    p1 = _np.maximum(rep - 1, 0)
    p2 = _np.maximum(rep - 2, 0)
    low = _np.maximum(rep - rep_b, 0)
    c1 = (rep < rep_b) | (where[low] // BEAT_BYTES != where[rep] // BEAT_BYTES)
    c2 = special[p1] | (b2 & special[p2])
    c3 = (delta[p1] == 0) | (b2 & (delta[p2] == 0))
    bad = c1 | c2 | c3
    if bad.any():
        k = int(_np.argmax(bad))
        why = ("同じ拍に直前の対象のトークンが B 個ない" if c1[k]
               else "VALUE・REPEAT は繰り返せない" if c2[k] else "間隔 0 のトークン（束の途中）は繰り返せない")
        raise _bad_repeat(int(where[rep[k]]), why)


def _rep_terms(p: _Parsed):
    """REPEAT ごとの、対象 B 個の間隔の和（uint64）と、N が奇数のときの反転のビット（uint16）。"""
    r, b2 = p.rep, p.rep_b == 2
    r2 = _np.maximum(r - 2, 0)
    unit = p.delta[r - 1].astype(_np.uint64) + _np.where(b2, p.delta[r2], 0).astype(_np.uint64)
    odd = (p.rep_n & 1) == 1
    flip = _np.where(odd, p.xor[r - 1] ^ _np.where(b2, p.xor[r2], 0).astype(_np.uint16), 0).astype(_np.uint16)
    return unit, flip


def _summary(p: _Parsed) -> tuple[int, bool, int]:
    """REPEAT を展開せずに求める要約: (間隔の和, VALUE があるか, 値)。VALUE があれば値 = 最後の値、無ければ値 = 前の値に
    重ねる反転の累積（XOR）。"""
    dsum = int(p.delta.sum(dtype=_np.uint64))
    xr = int(_np.bitwise_xor.reduce(p.xor))
    has_value = len(p.vpos) > 0
    if has_value:
        j = int(p.vpos[-1])
        xr = int(_np.bitwise_xor.reduce(p.xor[j:]))
    if len(p.rep):
        unit, flip = _rep_terms(p)
        times = p.rep_n.astype(_np.uint64)
        if float(unit.astype(_np.float64) @ times.astype(_np.float64)) + float(dsum) >= 2.0 ** 63:
            raise TokenError("REPEAT で時刻が 64 ビットに収まらない")
        dsum += int((unit * times).sum(dtype=_np.uint64))
        after = p.rep > p.vpos[-1] if has_value else slice(None)
        xr ^= int(_np.bitwise_xor.reduce(flip[after]))
    if has_value:
        return dsum, True, int(p.vval[-1]) ^ xr
    return dsum, False, xr


def _expand(p: _Parsed) -> _Parsed:
    """REPEAT を、対象のトークンを N 回並べたものに展開する（変化点を作るために必要。変化点の数だけの大きさ）。
    対象は元の位置に 1 回あり、その後ろに N 回続く。"""
    if len(p.rep) == 0:
        return p
    nt = len(p.delta)
    r, b, n = p.rep, p.rep_b, p.rep_n
    cnt = b * n                                               # REPEAT ごとの、並べるトークンの数
    step = _np.zeros(nt, dtype=_np.int64)
    step[r] = cnt - 1                                         # REPEAT 自身は消え、cnt 個が入る
    shift = _np.cumsum(step)                                  # 各トークンの手前までに増えた数（REPEAT 自身を含む）
    idx = _np.empty(nt + int(shift[-1]), dtype=_np.intp)      # 展開後の各位置がどの元のトークンか
    plain = _np.ones(nt, dtype=bool)
    plain[r] = False
    keep = _np.flatnonzero(plain)
    idx[keep + shift[keep]] = keep
    total = int(cnt.sum())
    ends = _np.cumsum(cnt)
    within = _np.arange(total) - _np.repeat(ends - cnt, cnt)   # 並べた列の中の位置
    at = r + shift[r] - (cnt - 1)                              # 並べた列の展開後の先頭
    idx[_np.repeat(at, cnt) + within] = _np.repeat(r - b, cnt) + (within & _np.repeat(b - 1, cnt))
    vpos = p.vpos + shift[p.vpos]                              # VALUE は繰り返されない
    return p._replace(delta=p.delta[idx], xor=p.xor[idx], vpos=vpos, rep=p.rep[:0], rep_b=p.rep_b[:0], rep_n=p.rep_n[:0])


def _check_channels(ch, width: int) -> None:
    if width < 16 and len(ch) and int(ch.max()) >= width:
        raise TokenError(f"TOGGLE の ch {int(ch.max())} がチャンネル数 {width} 以上")


def _check_start(p: _Parsed, previous: int | None) -> None:
    """流れの先頭は VALUE から。続きのチャンクの先頭の出来事の間隔が 0 なのは、束が前の拍からつながっている（形式は束を拍の中に収める）。"""
    if len(p.delta) == 0:
        return
    if previous is None:
        if len(p.vpos) == 0 or p.vpos[0] != 0:
            raise TokenError(_NO_VALUE_FIRST)
    elif p.delta[0] == 0:
        raise TokenError(_STRADDLE)


def _values_after(p: _Parsed, previous: int | None, out=None):
    """トークンごとの、そのトークンまで適用した値（uint16）。累積の XOR を 1 回だけ取る。VALUE は「その手前の値との差」を
    反転として置き換えれば、累積が自然に値を置き直す形になる（手前の値は、直前の VALUE（または previous）に区間の反転の和を
    重ねたもの）。"""
    x = p.xor
    if len(p.vpos) == 0:
        acc = _np.bitwise_xor.accumulate(x, out=out)
        acc ^= _np.uint16(previous)
        return acc
    vpos = p.vpos
    sums = _np.bitwise_xor.reduceat(x, vpos)                   # i 番目の VALUE から次の VALUE の手前までの反転の和
    before = _np.empty(len(vpos), dtype=_np.uint16)            # 各 VALUE の手前の値
    before[0] = 0 if vpos[0] == 0 else int(previous) ^ int(_np.bitwise_xor.reduce(x[:vpos[0]]))
    before[1:] = p.vval[:-1] ^ sums[:-1]
    if out is None:
        x = x.copy()
    else:
        out[...] = x
        x = out
    if vpos[0] > 0:
        x[0] ^= _np.uint16(previous)                           # previous を累積の初めに入れる（先頭が VALUE なら不要）
    x[vpos] = p.vval ^ before
    _np.bitwise_xor.accumulate(x, out=x)
    return x


def _changes(p: _Parsed, first: int, previous: int | None, repeats: int, out=None) -> TokenDecode:
    v = _values_after(p, previous, None if out is None else out[0])
    delta = p.delta
    nonzero = _np.count_nonzero(delta) == len(v)              # 間隔が 0 のトークン（束の途中）が無ければ、トークン = 束
    if not nonzero:
        end = _np.empty(len(v), dtype=bool)                   # 束（同じ時刻の出来事）の最後のトークン
        end[:-1] = delta[1:] > 0
        end[-1] = True
    time = _np.empty(len(delta), dtype=_np.uint64) if out is None else out[1]
    if delta.dtype == _np.uint64:
        _np.add.accumulate(delta, out=time)
    else:                                                     # 先に型をそろえる（型変換つきの累積は GIL を放さない）
        time[...] = delta
        _np.add.accumulate(time, out=time)
    span = int(time[-1])
    ve, te = (v, time) if nonzero else (v[end], time[end])
    changed = _np.empty(len(ve), dtype=bool)
    changed[0] = True if previous is None else int(ve[0]) != previous
    _np.not_equal(ve[1:], ve[:-1], out=changed[1:])
    if not changed.all():
        ve, te = ve[changed], te[changed]
    if first:
        te += _np.uint64(first)                               # te は上で作った配列（time か、その抜粋）
    return TokenDecode(ve, te, first + span, int(v[-1]), p.pad, len(p.delta), repeats)


def _decode_py(data: bytes, first: int, previous: int | None, width: int) -> TokenDecode:
    """decode_tokens の numpy の無い道（トークンごとのループ。結果は同じ）。"""
    values, starts = array("H"), array("Q")
    state = {"t": first, "v": previous, "committed": previous, "open": False, "tokens": 0}

    def step(delta: int, toggle: int | None, new_value: int | None) -> None:
        if state["tokens"] == 0:
            if previous is None and new_value is None:
                raise TokenError(_NO_VALUE_FIRST)
            if previous is not None and delta == 0:
                raise TokenError(_STRADDLE)
        if delta > 0 and state["open"]:                       # 束が終わった: 前の束と違えば変化点
            if state["v"] != state["committed"]:
                values.append(state["v"])
                starts.append(state["t"])
            state["committed"] = state["v"]
            state["open"] = False
        state["t"] += delta
        if new_value is not None:
            state["v"] = new_value
        elif toggle is not None:
            state["v"] ^= 1 << toggle
        state["open"] = True
        state["tokens"] += 1

    pad = repeats = 0
    for beat in range(0, len(data), BEAT_BYTES):
        end = beat + BEAT_BYTES
        if data[beat] & TAIL_FLAG:
            raise TokenError(f"拍 {beat // BEAT_BYTES} の先頭が尾（頭でなければならない）")
        i = beat
        recent: list[tuple[int, int | None]] = []             # この拍の（間隔, 反転する ch）。VALUE・REPEAT は None で区切る
        while i < end:
            head = data[i]
            ddd, c = head & 7, head >> 3
            j = i + 1
            while j < end and data[j] & TAIL_FLAG:
                j += 1
            ntail = j - i - 1
            if ddd != DELTA_EXT and ntail:
                raise TokenError(f"バイト {i}: 間隔の欄が 7 でない頭の後ろに尾がある")
            if ntail > MAX_TAILS:
                raise TokenError(f"バイト {i}: 尾が {MAX_TAILS} 個を超える")
            delta, toggle, new_value = ddd, None, None
            if ddd == DELTA_EXT:
                if ntail == 0:
                    pad += 1
                    i = j
                    continue
                pay = data[i + 1] & ((1 << T1_BITS) - 1)
                for k in range(2, ntail + 1):
                    pay |= (data[i + k] & ((1 << TAIL_BITS) - 1)) << (T1_BITS + TAIL_BITS * (k - 2))
                if not data[i + 1] & SPECIAL_FLAG:
                    delta, toggle = DELTA_EXT + pay, c
                elif c == KIND_WAIT:
                    delta = pay
                elif c == KIND_VALUE:
                    delta, new_value = pay >> 16, pay & 0xFFFF
                    if new_value >> width:
                        raise TokenError(f"バイト {i}: VALUE の値 {new_value:#x} が {width} ビットに収まらない")
                elif c == KIND_REPEAT:
                    b, k = (pay & 1) + 1, (pay >> 1) + 1
                    body = recent[-b:]
                    if len(body) < b:
                        raise _bad_repeat(i, "同じ拍に直前の対象のトークンが B 個ない")
                    if any(x is None or x[0] == 0 for x in body):
                        raise _bad_repeat(i, "VALUE・REPEAT・間隔 0 のトークンは繰り返せない")
                    for _ in range(k):
                        for d, tg in body:
                            step(d, tg, None)
                    repeats += 1
                    recent.append(None)
                    i = j
                    continue
                else:
                    raise TokenError(f"バイト {i}: 予約の種類 {c}")
            else:
                toggle = c
            if toggle is not None and toggle >= width:
                raise TokenError(f"TOGGLE の ch {toggle} がチャンネル数 {width} 以上")
            step(delta, toggle, new_value)
            recent.append(None if new_value is not None else (delta, toggle))
            i = j
    if state["open"] and state["v"] != state["committed"]:
        values.append(state["v"])
        starts.append(state["t"])
    return TokenDecode(values, starts, state["t"], state["v"], pad, state["tokens"], repeats)


def expand_samples(data, width: int = 16) -> "array[int]":
    """拍の列を毎サンプルの値にする（試験・短い記録用。長さ = 最後の出来事の時刻 + 1）。"""
    d = decode_tokens(data, width=width)
    out = array("H")
    if len(d.values) == 0:
        return out
    starts = [int(x) for x in d.starts] + [d.end_time + 1]
    for k, value in enumerate(d.values):
        out.extend(array("H", [int(value)]) * (starts[k + 1] - starts[k]))
    return out


# --- 有界の復号（展開の大きさに依らないメモリ）-----------------------------------------------------------------
#
# REPEAT は「対象を N 回」で、N は 2^40 まで書ける（100 MS/s の方形波は 10 秒で約 240 KB に縮み、展開すると変化点が 10 億を超える）。
# decode_tokens は全部を 1 つの配列に展開するので、変化点の数に比例するメモリが要る。iter_decode_tokens は同じ変化点を、
# 1 つが max_changes 個以下の断片に分けて順に返す。メモリは max_changes と入力の大きさで決まり、展開の大きさに依らない。

def iter_decode_tokens(data, first: int = 0, previous: int | None = None, *, width: int = 16, max_changes: int,
                       threads: int | None = None) -> Iterator[tuple[object, object]]:
    """decode_tokens と同じ変化点を、(値, 開始) の断片（それぞれ numpy の配列か array。1 つが max_changes 個以下、空は返さない）に
    分けて順に返す。断片を順につなぐと decode_tokens の values / starts と要素単位で一致する（誤りも同じ TokenError）。
    max_changes は使う側が決める（既定は無い）。末尾の状態（最後の出来事の時刻・最後の値・NOP の数）は token_extent で求める。

    numpy が無いときは断片に分けても全部を先に復号する（メモリは展開に比例する。numpy の道だけが有界）。"""
    if max_changes < 1:
        raise ValueError("max_changes は 1 以上")
    n = _check_beats(data)
    if _np is None:
        d = _decode_py(bytes(data), first, previous, width)
        yield from _slices(d.values, d.starts, max_changes)
        return
    if n == 0:
        return
    count = _c_tokens(data, n, width, threads)
    if count is not None and count <= max_changes:             # 展開が小さい: 従来の道（C 版・スレッド）をそのまま使う
        d = decode_tokens(data, first, previous, width=width, threads=threads)
        yield from _slices(d.values, d.starts, max_changes)
        return
    p = _parse(data, width)
    if len(p.delta) == 0:
        return
    _check_start(p, previous)
    _summary(p)                                                # 時刻が 64 ビットに収まらないときの誤りを、断片を返す前に出す
    if len(p.rep) == 0 or _tokens_of(p) <= max_changes:
        d = _changes(_expand(p), first, previous, len(p.rep))
        yield from _slices(d.values, d.starts, max_changes)
        return
    yield from _stream_changes(p, first, previous, max_changes)


def _slices(values, starts, size: int) -> Iterator[tuple[object, object]]:
    for i in range(0, len(values), size):
        yield values[i:i + size], starts[i:i + size]


def _stream_changes(p: "_Parsed", first: int, previous: int | None, limit: int) -> Iterator[tuple[object, object]]:
    """REPEAT を含む列を、トークンの塊（limit 個以下）ごとに復号して断片にする。塊をまたぐ状態は、時刻・値・まだ閉じていない束
    （次のトークンの間隔が 0 でなければ閉じる）・直前の束の値（変化点かの比較）。"""
    t, v, committed = first, previous, previous
    pend: tuple[int, int] | None = None                       # まだ閉じていない束（値, 時刻）
    out_v: list = []
    out_s: list = []
    held = 0

    def take(block: "_Parsed"):
        """塊を順に処理して、閉じた束の変化点を (値, 開始) で返す（無ければ None）。"""
        nonlocal t, v, committed, pend
        vv = _values_after(block, v)
        tt = _np.add.accumulate(block.delta, dtype=_np.uint64)
        tt += _np.uint64(t)
        ve, te, dn = vv, tt, block.delta[1:]
        if pend is not None:
            ve = _np.concatenate([_np.array([pend[0]], dtype=_np.uint16), vv])
            te = _np.concatenate([_np.array([pend[1]], dtype=_np.uint64), tt])
            dn = block.delta
        end = dn > 0                                          # 次のトークンの間隔が 0 でないなら、その手前で束が閉じる
        ev, et = ve[:-1][end], te[:-1][end]
        pend = (int(ve[-1]), int(te[-1]))
        t, v = int(tt[-1]), int(vv[-1])
        if len(ev) == 0:
            return None
        changed = _np.empty(len(ev), dtype=bool)
        changed[0] = True if committed is None else int(ev[0]) != committed
        _np.not_equal(ev[1:], ev[:-1], out=changed[1:])
        committed = int(ev[-1])
        return ev[changed], et[changed]

    def feed(got) -> None:
        nonlocal held
        if got is None or len(got[0]) == 0:
            return
        out_v.append(got[0])
        out_s.append(got[1])
        held += len(got[0])

    def drain(everything: bool):
        nonlocal out_v, out_s, held
        if held == 0 or (held < limit and not everything):
            return
        av, as_ = (out_v[0], out_s[0]) if len(out_v) == 1 else (_np.concatenate(out_v), _np.concatenate(out_s))
        keep = 0 if everything else held % limit
        cut = held - keep
        rest = (av[cut:], as_[cut:]) if keep else None
        out_v, out_s, held = ([rest[0]], [rest[1]], keep) if rest else ([], [], 0)
        for i in range(0, cut, limit):
            yield av[i:i + limit], as_[i:i + limit]

    def plain(a: int, b: int):
        for lo in range(a, b, limit):
            hi = min(lo + limit, b)
            i, j = int(_np.searchsorted(p.vpos, lo)), int(_np.searchsorted(p.vpos, hi))
            yield p._replace(delta=p.delta[lo:hi], xor=p.xor[lo:hi], vpos=p.vpos[i:j] - lo, vval=p.vval[i:j],
                             rep=_NO_INDEX, rep_b=_NO_INDEX, rep_n=_NO_INDEX)

    start = 0
    for r, b, n in zip(p.rep.tolist(), p.rep_b.tolist(), p.rep_n.tolist()):
        for block in plain(start, r):                          # REPEAT の手前まで（対象は 1 回、ここに含まれる）
            feed(take(block))
            yield from drain(False)
        pattern_d, pattern_x = p.delta[r - b:r], p.xor[r - b:r]
        per = max(1, limit // b)                               # 1 つの塊に並べる繰り返しの回数
        left = n
        while left:
            k = min(per, left)
            block = p._replace(delta=_np.tile(pattern_d, k), xor=_np.tile(pattern_x, k), vpos=_NO_INDEX,
                               vval=p.vval[:0], rep=_NO_INDEX, rep_b=_NO_INDEX, rep_n=_NO_INDEX)
            feed(take(block))
            yield from drain(False)
            left -= k
        start = r + 1
    for block in plain(start, len(p.delta)):
        feed(take(block))
        yield from drain(False)
    if pend is not None and (committed is None or pend[0] != committed):   # 最後の束
        feed((_np.array([pend[0]], dtype=_np.uint16), _np.array([pend[1]], dtype=_np.uint64)))
    yield from drain(True)
