"""SWO の線レベルモデル（テスト専用）: Manchester 方式（Arm の TPIU）。UART（NRZ）方式は tests.models.uart。"""


def manchester_halves(data: bytes, idle_bits: int = 1) -> list[int]:
    """1 フレーム（開始ビット 1、バイトの LSB から、休止 Low）の半ビットごとの線レベル。
    1 = 前半 High・後半 Low、0 = 前半 Low・後半 High。"""
    bits = [1] + [(byte >> i) & 1 for byte in data for i in range(8)]
    halves = [h for b in bits for h in ((1, 0) if b else (0, 1))]
    return halves + [0] * (2 * idle_bits)


def segments(halves: list[int], half_ns: float, skew_ns: float = 0.0) -> list[tuple[int, float]]:
    """半ビットの列を (レベル, 長さ ns) の区間にまとめる。skew_ns: High の区間を長く（負なら短く）する線のひずみ
    （立ち上がりと立ち下がりの遅れの差。区間の始まりの時刻は保ち、High の終わりだけをずらす）。"""
    runs: list[list] = []
    for h in halves:
        if runs and runs[-1][0] == h:
            runs[-1][1] += half_ns
        else:
            runs.append([h, half_ns])
    out = []
    carry = 0.0
    for level, length in runs:
        length += carry
        carry = 0.0
        if level == 1:
            length += skew_ns
            carry = -skew_ns
        out.append((level, length))
    return out
