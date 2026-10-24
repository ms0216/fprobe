"""fprobe.trace.etmv4: ETMv4（M-profile の命令トレース）のパケットの解読。

バイト列は Arm IHI 0064（ETMv4 Architecture Specification）のパケットの形から手で組んだもの（etm_encoder）と、
直接書いた 16 進。"""

import pytest

import etm_encoder as enc
from fprobe.trace import elements as el
from fprobe.trace.etmv4 import Etmv4Config, Etmv4Decoder


M_PROFILE = {"commit_opt1": True, "vmid_bytes": 1, "cid_bytes": 4}


def decode(data: bytes, **config) -> list:
    return Etmv4Decoder(Etmv4Config(**{**M_PROFILE, **config})).decode(data)


def test_the_config_has_no_implicit_values():
    # 本来 TRCIDRn から決まる値（COMMOPT・VMID / CID の大きさ）を暗黙の既定で補わない
    with pytest.raises(TypeError):
        Etmv4Config()
    with pytest.raises(TypeError):
        Etmv4Decoder()


def kinds(items) -> list[str]:
    return [type(e).__name__ for e in items]


def test_nothing_is_decoded_before_the_first_async():
    items = decode(bytes([0xF7, 0x9B, 0x00]) + enc.async_() + enc.trace_info())
    assert kinds(items) == ["Async", "TraceInfo"]
    assert items[0].offset == 3 and items[0].skipped == 3


def test_async_with_a_longer_run_of_zeros():
    items = decode(bytes(20) + b"\x80" + enc.trace_on())
    assert kinds(items) == ["Async", "TraceOn"]


def test_trace_info_fields():
    items = decode(enc.async_() + enc.trace_info(cc=True, cc_threshold=300))
    info = items[1]
    assert info.cycle_counting and info.cc_threshold == 300
    assert info.raw == enc.trace_info(cc=True, cc_threshold=300)


def test_long_address_is1_and_is0():
    items = decode(enc.async_() + enc.long_address_is1(0x1000_0124) + enc.long_address_is0(0x0800_0A48))
    a, b = items[1], items[2]
    assert (a.address, a.isa, a.form) == (0x1000_0124, "thumb", "long")
    assert (b.address, b.isa) == (0x0800_0A48, "arm")


def test_a_hand_written_long_address():
    # 0x9B, A[7:1] = 0x12（0x24 >> 1）, A[15:8] = 0x01, A[23:16] = 0x00, A[31:24] = 0x10  →  0x1000_0124
    items = decode(enc.async_() + bytes.fromhex("9b12010010"))
    assert items[1].address == 0x1000_0124


def test_short_address_keeps_the_upper_bits_of_the_last_address():
    data = (enc.async_() + enc.long_address_is1(0x1000_4000)
            + enc.short_address_is1(0x1000_4066)                      # 1 バイト: A[7:1]
            + enc.short_address_is1(0x1000_5A02, two_bytes=True))     # 2 バイト: A[15:1]
    items = decode(data)
    assert [i.address for i in items[1:]] == [0x1000_4000, 0x1000_4066, 0x1000_5A02]
    assert items[2].form == "short"


def test_exact_match_refers_to_the_address_history():
    data = (enc.async_() + enc.long_address_is1(0x1000_0100) + enc.long_address_is1(0x1000_0200)
            + enc.exact_match(1)                                       # 1 つ前 = 0x100
            + enc.exact_match(1))                                      # 履歴に積まれたので、1 つ前 = 0x200
    items = decode(data)
    assert [i.address for i in items[1:]] == [0x1000_0100, 0x1000_0200, 0x1000_0100, 0x1000_0200]
    assert items[3].form == "exact"


def test_address_with_context():
    items = decode(enc.async_() + enc.address_context_is1(0x1000_0040, context=0x20))
    a, c = items[1], items[2]
    assert isinstance(a, el.Address) and a.address == 0x1000_0040
    assert isinstance(c, el.Context) and c.non_secure


def test_context_packet_without_payload():
    items = decode(enc.async_() + b"\x80")
    assert kinds(items) == ["Async", "Context"] and items[1].unchanged


@pytest.mark.parametrize("data, pattern", [
    (b"\xF7", "E"), (b"\xF6", "N"),                                     # 形式 1
    (b"\xD9", "EN"), (b"\xDA", "NE"), (b"\xDB", "EE"),                  # 形式 2（ビット 0 が古い方）
    (b"\xF8", "NNN"), (b"\xFD", "ENE"),                                 # 形式 3
    (b"\xDC", "NEEE"), (b"\xDD", "NNNN"), (b"\xDE", "NENE"), (b"\xDF", "ENEN"),   # 形式 4
    (b"\xF5", "NEEEE"), (b"\xD5", "NNNNN"), (b"\xD6", "NENEN"), (b"\xD7", "ENENE"),  # 形式 5
    (b"\xC0", "EEEE"), (b"\xE0", "EEEN"), (b"\xC2", "EEEEEE"), (b"\xD4", "E" * 24),  # 形式 6
])
def test_atom_formats(data, pattern):
    items = decode(enc.async_() + data)
    assert isinstance(items[1], el.Atoms) and items[1].pattern == pattern


def test_exception_takes_the_return_address_from_the_next_address_packet():
    data = (enc.async_() + enc.long_address_is1(0x1000_0000) + enc.exception(15)    # SysTick
            + enc.long_address_is1(0x1000_0006))
    items = decode(data)
    ex = items[2]
    assert isinstance(ex, el.ExceptionEntry)
    assert (ex.number, ex.name, ex.return_address) == (15, "SysTick", 0x1000_0006)
    assert len(items) == 3                                             # 戻り先のアドレスは例外の中に入る


def test_irq_exception_numbers_use_the_second_byte():
    items = decode(enc.async_() + enc.exception(16 + 26) + enc.short_address_is1(0x0000_0010))
    assert items[1].number == 42 and items[1].name == "IRQ26"


def test_exception_return_and_function_return_are_markers():
    items = decode(enc.async_() + enc.exception_return() + b"\x05")
    assert kinds(items) == ["Async", "ExceptionReturn", "FunctionReturn"]


def test_timestamp_updates_the_low_bits():
    data = enc.async_() + enc.timestamp(0x12345, bits=21) + enc.timestamp(0x7F, bits=7)
    items = decode(data)
    assert items[1].value == 0x12345 and items[2].value == 0x1237F


def test_timestamp_with_cycle_count():
    items = decode(enc.async_() + enc.timestamp(1000, bits=14, cycles=77))
    assert (items[1].value, items[1].cycles) == (1000, 77)


def test_cycle_count_formats():
    data = (enc.async_() + enc.trace_info(cc=True, cc_threshold=10)
            + b"\x0E\x85\x01"            # 形式 1（コミットなし）: 続きの印付きで 0x85 0x01 = 5 + 128 = 133
            + b"\x0F"                    # 形式 1、U = 1（数は不明）
            + b"\x0C\x13"                # 形式 2: 下位 4 ビット 3 → 閾値 + 3
            + b"\x12")                   # 形式 3: 下位 2 ビット 2 → 閾値 + 2
    items = decode(data)
    counts = [i.count for i in items if isinstance(i, el.CycleCount)]
    assert counts == [133, None, 13, 12]


def test_overflow_and_discard_and_events():
    items = decode(enc.async_() + enc.overflow() + b"\x00\x03" + b"\x75" + b"\x70")
    assert kinds(items) == ["Async", "Overflow", "Discard", "Event", "Ignore"]
    assert items[3].events == 0x5


def test_unknown_header_reports_and_resyncs():
    data = enc.async_() + b"\xF7" + b"\x08" + b"\xF7" + enc.async_() + b"\xF6"
    items = decode(data)
    assert kinds(items) == ["Async", "Atoms", "DecodeError", "Async", "Atoms"]
    assert items[2].offset == 13
    assert items[4].pattern == "N"


def test_truncated_packet_at_the_end_is_reported():
    items = decode(enc.async_() + enc.long_address_is1(0x1000_0000)[:3])
    assert kinds(items) == ["Async", "Truncated"]


def test_every_item_keeps_its_bytes_and_offset():
    data = enc.async_() + enc.trace_info() + enc.long_address_is1(0x2000_0000) + enc.atoms("EEN")
    items = decode(data)
    assert b"".join(i.raw for i in items) == data
    assert [i.offset for i in items] == [0, 12, 15, 20]
    assert items[-1].as_dict()["kind"] == "atoms"


def test_context_with_a_vmid_the_etm_says_it_does_not_have_is_an_error():
    """TRCIDR2 が VMID 0 バイト（無い）なのに Context に VMID の印があれば誤り（1 バイトと仮定して読まない）。"""
    items = decode(enc.async_() + enc.address_context_is1(0x1000_0040, context=0x40) + b"\x11", vmid_bytes=0)
    assert any(isinstance(i, el.DecodeError) and "VMID" in i.message for i in items)


def test_decoder_config_keeps_the_sizes_the_id_registers_say():
    from fprobe.trace.coresight import Etmv4Info
    info = Etmv4Info(4, 2, True, True, False, False, 0, vmid_bytes=0, cid_bytes=0)
    config = info.decoder_config()
    assert (config.vmid_bytes, config.cid_bytes) == (0, 0)
