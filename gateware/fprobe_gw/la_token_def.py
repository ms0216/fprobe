"""ロジアナの RLE のトークン形式の符号の定数を VHDL のパッケージ（rtl/core/generated/la_tok_pkg.vhd）にする。

定義元は csr/fprobe_map.py の LA_TOK_CONSTANTS。ホスト側の同じ値は host/fprobe/generated.py の LA_TOK_*
（fprobe.la_tokens の参照符号器の定数との一致は host/tests/test_la_tok_constants.py）。
"""

from .csr.fprobe_map import LA_TOK_CONSTANTS

__all__ = ["render_vhdl_package"]


def render_vhdl_package() -> str:
    width = max(len(n) for n in LA_TOK_CONSTANTS)
    lines = [
        "-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。",
        "-- 生成元: gateware/fprobe_gw/csr/fprobe_map.py の LA_TOK_CONSTANTS（ロジアナの RLE のトークン形式の符号の定数）",
        "library ieee;",
        "use ieee.std_logic_1164.all;",
        "",
        "package la_tok_pkg is",
        *(f"  constant {n:<{width}} : natural := {v};   -- {doc}" for n, (v, doc) in LA_TOK_CONSTANTS.items()),
        "end package;",
        "",
    ]
    return "\n".join(lines)
