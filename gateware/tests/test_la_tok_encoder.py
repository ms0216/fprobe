"""ロジアナの RLE のトークン形式の符号器（la_tok_encoder。docs/superpowers/specs/2026-11-07-la-rle2-design.md §5）。"""

from fprobe_gw.rtl import CORE_GENERATED, rtl
from fprobe_gw.toolchain.sim import run_cocotb

SOURCES = [CORE_GENERATED / "la_tok_pkg.vhd", *rtl("la/la_tok_encoder.vhd")]


def test_la_tok_encoder(tmp_path):
    run_cocotb(parallel=True, toplevel="la_tok_encoder", sources=SOURCES, test_module="tests.tb.la_tok_encoder_tb", build_dir=tmp_path)


def test_la_tok_encoder_small_limits(tmp_path, monkeypatch):
    """間隔の上限を小さくした構成（TOGGLE 7 + 2^9 − 1、VALUE 2^6 − 1、WAIT 2^8 − 1）で、WAIT による分割の規則を確かめる。"""
    monkeypatch.setenv("LTE_SMALL", "1")
    run_cocotb(parallel=True, toplevel="la_tok_encoder", sources=SOURCES, test_module="tests.tb.la_tok_encoder_tb",
               generics={"TOGGLE_DELTA_BITS": 9, "VALUE_DELTA_BITS": 6, "WAIT_BITS": 8}, build_dir=tmp_path)


def test_la_tok_encoder_repeat(tmp_path, monkeypatch):
    """REPEAT_EN = true・repeat = 1: 参照符号器 encode_tokens(repeat=True) とバイト単位で一致する（設計書 §3.7・§8 段階 2b）。"""
    monkeypatch.setenv("LTE_REPEAT", "1")
    run_cocotb(parallel=True, toplevel="la_tok_encoder", sources=SOURCES, test_module="tests.tb.la_tok_encoder_tb",
               generics={"REPEAT_EN": True}, build_dir=tmp_path)


def test_la_tok_encoder_repeat_built_but_off(tmp_path, monkeypatch):
    """REPEAT_EN = true の回路でも、repeat = 0（使う側が選ばない）なら REPEAT なしの参照符号器と一致する。"""
    monkeypatch.setenv("LTE_REPEAT", "0")
    run_cocotb(parallel=True, toplevel="la_tok_encoder", sources=SOURCES, test_module="tests.tb.la_tok_encoder_tb",
               generics={"REPEAT_EN": True}, build_dir=tmp_path)


def test_la_tok_encoder_repeat_small_limits(tmp_path, monkeypatch):
    """繰り返しの回数の上限（REPEAT_N_BITS = 3 → 8）と間隔の上限を小さくした構成で、REPEAT の閉じの規則を確かめる。"""
    monkeypatch.setenv("LTE_REPEAT", "1")
    monkeypatch.setenv("LTE_SMALL", "1")
    monkeypatch.setenv("LTE_NBITS", "3")
    run_cocotb(parallel=True, toplevel="la_tok_encoder", sources=SOURCES, test_module="tests.tb.la_tok_encoder_tb",
               generics={"REPEAT_EN": True, "REPEAT_N_BITS": 3, "TOGGLE_DELTA_BITS": 9, "VALUE_DELTA_BITS": 6, "WAIT_BITS": 8},
               build_dir=tmp_path)
