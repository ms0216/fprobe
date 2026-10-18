"""CSR 生成器: 定義の検査と、全種類を含むテスト用ブロックを生成してシミュレーションする。"""

import pytest

from fprobe_gw.csr.model import Block, CsrMap, Field, Register
from fprobe_gw.csr.vhdl import render_bank
from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb

TEST_BLOCK = Block("probe", 0x200, 0x20, doc="生成器の試験", registers=(
    Register("ctrl", 0x00, (Field("enable", 0, 1, "RW", 1), Field("mode", 4, 3, "RW", 5),
                            Field("ident", 16, 8, "CONST", 0xA5))),
    Register("status", 0x04, (Field("busy", 0, 1, "RO"), Field("count", 8, 16, "RO"))),
    Register.word("word", 0x0C, "RW", 0xDEADBEEF),
    Register.word("snap", 0x10, "RO", read_strobe=True),
    Register("go", 0x14, (Field("start", 0, 1, "PULSE"), Field("lanes", 4, 4, "PULSE"))),
    Register("pins", 0x18, (Field("pins", 0, 8, "RO"),), write_to="word"),   # 読むと入力、書くと word
))


def test_bank_simulation(tmp_path):
    source = tmp_path / "csr_probe.vhd"
    source.write_text(render_bank(TEST_BLOCK), encoding="utf-8")
    run_cocotb(toplevel="csr_probe", sources=[*rtl("core/csr_bus_pkg.vhd"), source],
               test_module="tests.tb.csr_bank_tb", build_dir=tmp_path / "sim")


@pytest.mark.parametrize("make", [
    lambda: Field("bad", 30, 4, "RW"),                                    # 32 ビットを超える
    lambda: Field("bad", 0, 2, "RW", 4),                                  # 値が幅を超える
    lambda: Field("Bad", 0, 1, "RW"),                                     # 名前
    lambda: Register("r", 2, (Field("a", 0, 1, "RW"),)),                  # ワード境界でない
    lambda: Register("r", 0, (Field("a", 0, 4, "RW"), Field("b", 3, 1, "RW"))),   # 重なり
    lambda: Block("b", 0x10, 0x20),                                       # base が size の倍数でない
    lambda: Block("b", 0, 0x30),                                          # 2 の冪でない
    lambda: Block("b", 0, 0x10, (Register.word("a", 0x10, "RW"),)),       # 窓の外
    lambda: CsrMap((Block("a", 0, 0x100), Block("b", 0x80, 0x80))),       # 窓の重なり
    lambda: Block("b", 0, 0x10, (Register.word("signal", 0, "RW"),)),     # VHDL 予約語
    lambda: Block("b", 0, 0x10, (Register.word("index", 0, "RO"),)),      # 生成コードの識別子
    lambda: Block("b", 0, 0x10, (Register.word("x", 0, "RW"), Register.word("x_r", 4, "RO"))),  # x_r が重複
    lambda: Block("b", 0, 0x10, (Register("a", 0, (Field("b_c", 0, 1, "RW"),)),
                                 Register("a_b", 4, (Field("c", 0, 1, "RW"),)))),        # a_b_c が重複
    lambda: Block("decoder", 0, 0x10),                                    # csr_decoder と衝突
    lambda: CsrMap((Block("b", 0, 0x10, (Register.word("base", 0, "RW"),)),)),   # B_BASE が重複
    lambda: Block("b", 0, 0x10, (Register.word("p", 0, "RO", write_to="q"),)),  # 書き先が無い
    lambda: Block("b", 0, 0x10, (Register.word("p", 0, "RO", write_to="q"),
                                 Register.word("q", 4, "RO"))),                # 書き先に書けるフィールドが無い
    lambda: Block("b", 0, 0x10, (Register.word("p", 0, "RW", write_to="q"),
                                 Register.word("q", 4, "RW"))),                # 自分にも書けるフィールドがある
])
def test_invalid_definitions_are_rejected(make):
    with pytest.raises(ValueError):
        make()
