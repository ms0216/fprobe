"""DAP サンプラ（F4: 変数のライブ観測）: FPGA が自分で、最大 8 か所（各 1〜16 語）のメモリを周期的に読み、1 語ごとに
デバイスの時刻付きのイベント（種類 7）にする。ホストの DAP の命令の境界で線を借りるので、pyOCD 等のデバッグと同時に使える。

仕様: docs/superpowers/specs/2026-10-17-f4-live-sampler-design.md
読んだ値は イベント記録 → 共通の時間軸の記録（stream "sample"、a = 番地、b = 値）に入る。
"""

import math

from . import generated as g

__all__ = ["MAX_SLOTS", "MAX_WORDS", "Sampler", "plan_slots", "round_estimate", "round_transfers", "swclk_hz",
           "swd_hz_for_rate"]

MAX_SLOTS = 8
MAX_WORDS = 16
KB = 1024                                      # MEM-AP の番地の自動増加は 1 KB をまたげない（ADIv5）

# 速さの見積もり（B9・B17。docs/superpowers/specs/2026-10-18-b17-sampler-lazy-restore-design.md の 1・4 節）:
# 1 転送 = ヘッダ 8 + ターン 1 + ACK 3 + データ・パリティ 33 + ターン 1 = 46 SWCLK + idle 2（pyOCD）= 48 SWCLK。
# B17（遅延の戻し）: ホストが線を使っていない間は SELECT・CSW・TAR を戻さず線を持ったまま読むので、定常の 1 か所 n 語は
# TAR・DRW × n・RDBUFF・CTRL/STAT = 3 + n 転送。有効なか所が 1 つで 1 語なら CSW を自動増加なしにして TAR も書かない
# （DRW・RDBUFF・CTRL/STAT の 3 転送）。ホストの命令が来るたびに、戻し 3 転送と借り直し（SELECT・CSW・TAR）が加わる。
# B17 の前（毎回戻す）は 1 か所 8 + n 転送で、実機（SWD 3.857 MHz、1 か所 1 語）は見積もり 112 µs に対し約 120 µs
# （調停の分）だった。実際の速さは巡回の数で測る。
BITS_PER_TRANSFER = 48
TRANSFERS_PER_SLOT = 3                         # TAR・RDBUFF・CTRL/STAT（+ DRW × 語数）


def round_transfers(slots: list[tuple[int, int]]) -> int:
    """か所 [(番地, 語数)] の定常の 1 巡回の転送の数（線を持ったまま。ホストの命令が無い間）。"""
    if len(slots) == 1 and slots[0][1] == 1:
        return 3                                               # 同じ番地を読み続ける: TAR を書かない
    return sum(TRANSFERS_PER_SLOT + n for _, n in slots)
_HALF_MIN, _HALF_MAX = 2, 65535                # SWCLK の半周期（sys の周期数）の範囲（dap_cmd_config）


def _half_clock(sys_clock_hz: int) -> tuple[int, int]:
    """(SWCLK = 値 / 半周期 の値, 要求を 1% まで超えてよい分を除いた値)。dap_cmd_config.vhd の SWJ_Clock と同じ整数の計算。"""
    half_clock = sys_clock_hz // 2
    return half_clock, half_clock // 101 * 100


def swclk_hz(sys_clock_hz: int, requested_hz: float) -> float:
    """要求した SWD の速さ（DAP_SWJ_Clock）で FPGA が実際に出す SWCLK。半周期 = ceil(sys/2 / (1.01 × 要求))、2〜65535。"""
    half_clock, tolerated = _half_clock(sys_clock_hz)
    requested = max(1, int(requested_hz))
    half = min(max(-(-tolerated // requested), _HALF_MIN), _HALF_MAX)
    return half_clock / half


def round_estimate(slots: list[tuple[int, int]], sys_clock_hz: int, swd_hz: float) -> dict:
    """か所 [(番地, 語数)] の定常の 1 巡回の見積もり: swd_hz（要求）、swclk_hz（実際）、transfers、round_us、max_rate_hz
    （巡回が続けて回ったときの速さ。実機は調停の分と、ホストの命令ごとの戻し・借り直しの分だけ遅い）。"""
    transfers = round_transfers(slots)
    swclk = swclk_hz(sys_clock_hz, swd_hz)
    round_s = transfers * BITS_PER_TRANSFER / swclk
    return {"swd_hz": swd_hz, "swclk_hz": swclk, "transfers": transfers, "round_us": round_s * 1e6,
            "max_rate_hz": 1 / round_s}


def swd_hz_for_rate(slots: list[tuple[int, int]], rate_hz: float, sys_clock_hz: int) -> int | None:
    """rate_hz の巡回に要る、最も遅い SWD の速さの要求（target_connect の frequency_hz に渡す値）。FPGA の最も速い
    SWCLK でも届かなければ None。"""
    transfers = round_transfers(slots)
    needed = transfers * BITS_PER_TRANSFER * rate_hz             # 要る SWCLK
    half_clock, tolerated = _half_clock(sys_clock_hz)
    half = min(math.floor(half_clock / needed), _HALF_MAX)
    if half < _HALF_MIN:
        return None
    return -(-tolerated // half)                                 # この半周期を選ばせる最も遅い要求


def plan_slots(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """(番地, バイト数) の範囲を、語の境界の (先頭の番地, 語数) のか所にする。重なる・隣り合う範囲はまとめ、
    1 か所 16 語まで・1 KB をまたがないように分ける。8 か所に収まらなければ ValueError。"""
    words = sorted({w for address, size in ranges for w in range(address // 4, (address + max(size, 1) + 3) // 4)})
    slots: list[tuple[int, int]] = []
    for w in words:
        if slots:
            first, n = slots[-1]
            if w == first // 4 + n and n < MAX_WORDS and (w * 4) % KB != 0:
                slots[-1] = (first, n + 1)
                continue
        slots.append((w * 4, 1))
    if len(slots) > MAX_SLOTS:
        raise ValueError(f"観測する範囲が {len(slots)} か所になり、{MAX_SLOTS} か所に収まらない（範囲を減らす）")
    return slots


class Sampler:
    def __init__(self, device):
        caps = device.capabilities()
        if not getattr(caps, "sampler", False):
            raise RuntimeError("このデバイスには DAP サンプラが無い（ケイパビリティ FEATURES）")
        self.dev = device
        self.sys_clock_hz = caps.sys_clock_hz
        self.ap = 0

    def configure(self, ranges: list[tuple[int, int]], period_s: float, ap: int = 0) -> list[tuple[int, int]]:
        """観測する範囲（(番地, バイト数)）と周期を設定する（止めてから）。設定したか所を返す。"""
        slots = plan_slots(ranges)
        period = round(period_s * self.sys_clock_hz)
        if not 1 <= period < 1 << 32:
            raise ValueError(f"周期 {period_s} 秒は設定できない")
        self.stop()
        for k in range(MAX_SLOTS):                             # か所の表（分散 RAM）に 1 つずつ: 位置・番地・設定・書く
            address, n = slots[k] if k < len(slots) else (0, 1)
            self.dev.write(g.SAMPLER_SLOT_INDEX, [k, address, ((n - 1) << 8) | (k < len(slots)), 1])
        self.dev.write32(g.SAMPLER_PERIOD, period)
        self.ap = ap
        return slots

    def start(self, jitter: int = 0) -> None:
        """始める。jitter = k（0〜15）: 周期に 0〜2^k − 1 の乱数を足し 2^(k−1) を引く（平均は変わらない）。周期的な処理と
        読みの周期が噛み合う偏り（エイリアシング）を防ぐ。2^k が周期以上なら FPGA はゆらぎを入れない。"""
        if not 0 <= jitter <= 15:
            raise ValueError("jitter は 0〜15")
        self.dev.write32(g.SAMPLER_CONTROL, (jitter << 16) | (self.ap << 8) | 1)

    def stop(self) -> None:
        self.dev.write32(g.SAMPLER_CONTROL, self.ap << 8)

    def status(self) -> dict:
        """巡回の数・誤り・追いつかなかった周期・最後の ACK・読んでいるか、held（線を持っている。ホストの次の転送の
        前に SELECT・CSW・TAR を戻す）。"""
        rounds, errors, overruns, status = self.dev.read(g.SAMPLER_ROUNDS, 4)
        return {"rounds": rounds, "errors": errors, "overruns": overruns, "last_ack": status & 0x7,
                "busy": bool(status >> g.SAMPLER_STATUS_BUSY[0] & 1),
                "held": bool(status >> g.SAMPLER_STATUS_HELD[0] & 1)}
