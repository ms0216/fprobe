"""チェーンに期待の IDCODE が見えないとき、見えた値が 1 ビットずれの形かを事実として比べる（Pi 2B の 0x974008EF）。"""
from fprobe.arm_a_target import _one_bit_shift_of


def test_one_bit_shift_is_recognised_as_a_fact():
    assert _one_bit_shift_of(0x974008EF, 0x0BA00477)      # 実機で見た値（左へ 1 ビット）
    assert _one_bit_shift_of(0x0BA00477 >> 1, 0x0BA00477)
    assert not _one_bit_shift_of(0x0BA00477, 0x0BA00477)  # 同じ値はずれではない
    assert not _one_bit_shift_of(0x4BA00477, 0x0BA00477)  # 版だけ違う
    assert not _one_bit_shift_of(0x12345678, 0x0BA00477)
