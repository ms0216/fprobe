"""RLE のトークン形式の符号の定数: 参照符号器（host/fprobe/la_tokens.py）の値が、定義元（fprobe_map.LA_TOK_CONSTANTS）と、
そこから生成した回路のパッケージ（rtl/core/generated/la_tok_pkg.vhd）・ホストの生成定数（fprobe/generated.py）と一致する。"""

import re

from fprobe import generated as g
from fprobe import la_tokens as T
from fprobe_gw.csr.fprobe_map import FPROBE_MAP, LA_TOK_CONSTANTS
from fprobe_gw.rtl import CORE_GENERATED

# 定義元の名前 → 参照符号器の値
REFERENCE = {
    "LA_TOK_FORMAT_VERSION": T.TOKEN_FORMAT_VERSION,
    "LA_TOK_BEAT_BYTES": T.BEAT_BYTES,
    "LA_TOK_NOP": T.NOP,
    "LA_TOK_TAIL_FLAG": T.TAIL_FLAG,
    "LA_TOK_SPECIAL_FLAG": T.SPECIAL_FLAG,
    "LA_TOK_DELTA_EXT": T.DELTA_EXT,
    "LA_TOK_DELTA_DIRECT_MAX": T.DELTA_DIRECT_MAX,
    "LA_TOK_KIND_WAIT": T.KIND_WAIT,
    "LA_TOK_KIND_VALUE": T.KIND_VALUE,
    "LA_TOK_KIND_REPEAT": T.KIND_REPEAT,
    "LA_TOK_T1_BITS": T.T1_BITS,
    "LA_TOK_TAIL_BITS": T.TAIL_BITS,
    "LA_TOK_MAX_TAILS": T.MAX_TAILS,
    "LA_TOK_PAYLOAD_BITS": T.PAYLOAD_BITS,
    "LA_TOK_VALUE_MIN_CHANGES": T.VALUE_MIN_CHANGES,
    "LA_TOK_VALUE_MIN_TAILS": T.VALUE_MIN_TAILS,
    "LA_TOK_WAIT_BITS": T.WAIT_MAX.bit_length(),
    "LA_TOK_CONTROL_RLE_FMT_BIT": T.CONTROL_RLE_FMT_BIT,
}


def test_every_constant_has_a_reference_value_and_they_agree():
    assert set(REFERENCE) == set(LA_TOK_CONSTANTS), "定数を足したら参照符号器との対応もここに足す"
    assert T.WAIT_MAX == (1 << LA_TOK_CONSTANTS["LA_TOK_WAIT_BITS"][0]) - 1
    for name, value in REFERENCE.items():
        assert LA_TOK_CONSTANTS[name][0] == value, name


def test_host_generated_constants_equal_the_definition():
    for name, (value, _) in LA_TOK_CONSTANTS.items():
        assert getattr(g, name) == value, name


def test_the_circuit_package_equals_the_definition():
    text = (CORE_GENERATED / "la_tok_pkg.vhd").read_text(encoding="utf-8")
    found = {k: int(v) for k, v in re.findall(r"constant (\w+)\s*: natural := (\d+);", text)}
    assert found == {name: value for name, (value, _) in LA_TOK_CONSTANTS.items()}


def test_the_control_bit_is_the_csr_field():
    assert g.LA_DEEP_CONTROL_RLE_FMT == (T.CONTROL_RLE_FMT_BIT, 1)
    assert g.LA_DEEP_TICK == g.LA_DEEP_BASE + 0x54 and g.LA_DEEP_PAD_BYTES == g.LA_DEEP_BASE + 0x58
    assert g.LA_DEEP_KEYFRAMES == g.LA_DEEP_BASE + 0x5C
    assert FPROBE_MAP is not None
