"""通信の復号・SWO/ITM/DWT/ETM の解読の不具合の再現（検出専用。strict の xfail: 直ると XPASS で失敗して気づく）。
台帳: docs/reports/bugs/decode.md

手法: 試験の中に別実装のエンコーダ（正しいプロトコルの波形・バイト列を作る）を書いて製品のデコーダへ往復させ、
乱数のパラメータ・境界・壊れた入力・途中から始まる入力を当てた。UART（5〜9 ビット・パリティ 5 種・停止 1/1.5/2・
極性・ビット順・サンプルあたりの幅 2.5〜40 の小数・位相）・SPI（モード 0〜3・語長 1〜64・MSB/LSB・CS の有無）・
I2C（7/10 ビット・ACK/NACK・クロック引き延ばし・繰り返しスタート）・SWD（ターンアラウンド 1〜4・ORUNDETECT・
WAIT/FAULT・TARGETSEL）・ITM/DWT の全パケット（チャンク分割でも同じ結果）・TPIU のフレーム（HSYNC/FSYNC 混在）は
往復で一致した（不具合なし）。以下は往復では見つからず、境界・設計の読みで見つかったもの。
"""

from array import array

import pytest

from fprobe.decode import decode_i2c, decode_uart
from fprobe.itm import (Extension, ExceptionAction, ExceptionTrace, Instrumentation, LocalTimestamp, PortText,
                        TimestampRelation, Timestamper)
from fprobe.la_wave import Transitions
from fprobe.memview import BytesMemory
from fprobe.trace import Etmv4Config, decoder_for
from fprobe.trace import elements as el
from fprobe.trace.reconstruct import Reconstructor


def tr_from(samples, channels):
    values, starts, prev = array("H"), array("Q"), None
    for i, v in enumerate(samples):
        if v != prev:
            values.append(v)
            starts.append(i)
            prev = v
    return Transitions(values, starts, len(samples), 0, 0, 1e-6, channels)


def elem(cls, **kw):
    return cls(offset=0, raw=b"", **kw)


# --- 命令トレースの再構成 -----------------------------------------------------------------------

def _post_index_ldr_pc_memory():
    code = bytearray(0x200)
    code[0:4] = bytes.fromhex("5df804fb")            # ldr.w pc, [sp], #4（pop {pc} の 32 ビット形。間接の分岐）
    code[0x100:0x102] = bytes.fromhex("00bf")        # nop
    code[0x102:0x104] = bytes.fromhex("7047")        # bx lr
    return BytesMemory(0x08000000, bytes(code))


def test_reconstruct_ldr_pc_post_index_is_not_a_direct_branch():
    els = [elem(el.Address, address=0x08000000, isa="thumb"), elem(el.Atoms, pattern="E"),
           elem(el.Address, address=0x08000100, isa="thumb")]
    flow = Reconstructor(_post_index_ldr_pc_memory()).run(els)
    row = flow.instructions()[0]
    assert row["branch"] == "taken" and row.get("target") != 4          # 間接の分岐に行き先 0x4 を付けてはいけない


def test_reconstruct_atoms_after_ldr_pc_post_index_follow_the_address_packet():
    els = [elem(el.Address, address=0x08000000, isa="thumb"), elem(el.Atoms, pattern="EE"),
           elem(el.Address, address=0x08000100, isa="thumb")]
    flow = Reconstructor(_post_index_ldr_pc_memory()).run(els)
    assert flow.errors == [] and flow.addresses() == [0x08000000, 0x08000100, 0x08000102]


# --- ETMv4 の解読 --------------------------------------------------------------------------------

def test_etmv4_bytes_dropped_after_error_without_async_are_accounted():
    async_ = bytes(11) + b"\x80"
    data = async_ + bytes([0x01, 0x01, 0x00]) + b"\xB0" + bytes(range(1, 40))     # 0xB0 は M-profile に無いヘッダ
    elements = decoder_for("etmv4", Etmv4Config(True, 0, 0)).decode(data)
    accounted = sum(len(e.raw) + getattr(e, "skipped", 0) for e in elements)
    assert accounted == len(data)                                        # 全バイトが要素か skipped のどれかに載る


# --- SWO の組み立て ------------------------------------------------------------------------------

def test_value_watch_trace_does_not_claim_exact_address_when_window_is_wider():
    from fprobe.itm import DataTracePc, DataTraceValue
    from fprobe.swo_trace import (DataTraceAssembler, DwtInfo, TraceRequest, ValueWatch, plan_trace)
    dwt = DwtInfo(numcomp=4, arch="v7m", value_match=frozenset({1}), linkable=frozenset({1}), max_mask=15)
    # 4 バイトの変数が 0x20000002（8 バイトの窓 0x20000000〜7 が要る）
    req = TraceRequest(value_watches=(ValueWatch("v", value=5, size=4, address=0x20000002, action="trace"),))
    plan = plan_trace(req, dwt, swo_baud=2_000_000)
    link = next(c for c in plan.comparators if c.role == "link")
    assert (1 << link.mask) > 4                                           # 前提: 窓は変数より広い
    asm = DataTraceAssembler(plan.comparators)
    index = next(c.index for c in plan.comparators if c.role == "value_watch")
    (access,) = asm.feed([(1, None, DataTracePc(index, 0x08000100)), (2, None, DataTraceValue(index, True, 5, 4))])
    assert access.address is None                                         # 測っていない番地を事実として返さない


# --- ITM ---------------------------------------------------------------------------------------

def test_timestamper_keeps_timestamp_relation():
    ins = Instrumentation(1, 0x41, 1)
    exact = Timestamper().feed([ins, LocalTimestamp(10, TimestampRelation.SYNC)])
    delayed = Timestamper().feed([ins, LocalTimestamp(10, TimestampRelation.DATA_DELAYED)])
    assert exact != delayed                                               # 遅れて出た時刻と正確な時刻が区別できる


@pytest.mark.xfail(strict=True, reason="BUG-DEC-08: ITM の拡張パケット（ステイミュラス・ポートのページ）を PortText が無視する")
def test_port_text_honours_stimulus_port_page():
    lines = PortText().feed([Extension(1, 0), Instrumentation(3, ord("A"), 1), Instrumentation(3, ord("\n"), 1)])
    assert [(l.port, l.text) for l in lines] == [(35, "A")]               # ページ 1 のポート 3 = 通し 35


def test_exception_names_agree_across_modules():
    # 3 つの実装が一致する範囲（コアの例外 1〜6・11・12・14・15、IRQ）の確認
    from fprobe.probe_observe import _exception_name as observe
    from fprobe.profile.exceptions import exception_name as profile
    from fprobe.trace.elements import exception_name as element
    for n in (1, 2, 3, 4, 5, 6, 11, 12, 14, 15, 16, 17, 100):
        assert observe(n) == profile(n) == element(n)


def test_exception_names_agree_for_reserved_and_secure_fault():
    from fprobe.probe_observe import _exception_name as observe
    from fprobe.profile.exceptions import exception_name as profile
    from fprobe.trace.elements import exception_name as element
    for n in (0, 7, 8, 13):
        assert observe(n) == profile(n) == element(n), n


# --- 通信の解読 ----------------------------------------------------------------------------------

def test_i2c_stop_without_a_preceding_start_is_reported():
    # SCL=bit0, SDA=bit1。SCL 高のまま SDA が 0 → 1（STOP の条件）
    samples = [0b01] * 4 + [0b11] * 4
    d = decode_i2c(tr_from(samples, 2), scl=0, sda=1)
    assert "stop" in {a.kind for a in d.annotations}


def test_uart_line_held_low_for_whole_capture_is_reported():
    d = decode_uart(tr_from([0] * 200, 1), 0, baud=125_000, data_bits=8, parity="none", stop_bits=1,
                    invert=False, lsb_first=True)
    assert len(d.annotations) > 0


def test_uart_break_in_progress_at_capture_start_is_reported():
    d = decode_uart(tr_from([0] * 100 + [1] * 50, 1), 0, baud=125_000, data_bits=8, parity="none", stop_bits=1,
                    invert=False, lsb_first=True)
    assert len(d.annotations) > 0
