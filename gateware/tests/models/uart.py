"""UART の線レベルモデル（テスト専用）。"""


def uart_bits(byte: int) -> list[int]:
    """8N1 の線レベル列（開始ビット, LSB first のデータ 8 ビット, 停止ビット）。"""
    return [0] + [(byte >> i) & 1 for i in range(8)] + [1]


def decode_uart_line(levels: list[int], divisor: int) -> bytes:
    """1 クロック毎にサンプルした線レベル列から 8N1 フレームを復号する。"""
    out, i = [], 0
    while i < len(levels):
        if levels[i] == 0:
            middle = i + divisor // 2
            if middle + divisor * 9 >= len(levels):
                break
            bits = [levels[middle + divisor * (k + 1)] for k in range(8)]
            out.append(sum(b << k for k, b in enumerate(bits)))
            i = middle + divisor * 9
        else:
            i += 1
    return bytes(out)


def rx_divisor(sys_clk_hz: int, baud: int) -> int:
    """受信側の分周比: 公称ボーレートに最も近い値（bringup_core と同じ式）。"""
    return (sys_clk_hz + baud // 2) // baud


def tx_divisor(sys_clk_hz: int, baud: int, margin_ppm: int = 5000) -> int:
    """送信側の分周比: 公称より margin_ppm 以上速い値（bringup_core と同じ式）。"""
    margin_hz = max(1, baud * (margin_ppm // 100) // 10_000)
    return sys_clk_hz // (baud + margin_hz)
