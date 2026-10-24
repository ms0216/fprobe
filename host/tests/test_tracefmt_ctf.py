"""Zephyr の CTF の解読（fprobe.tracefmt.ctf）。記録は Zephyr の ctf_top.h と同じ並び（見出し + 欄を隙間なく）で作る。
実物の TSDL は tests/data/tracefmt/zephyr_v4.4_metadata（Zephyr v4.4.0）。"""

import struct
from pathlib import Path

import pytest

from fprobe.tracefmt.ctf import CtfDecoder, CtfError, builtin_metadata, decode_ctf, parse_tsdl

METADATA = Path(__file__).resolve().parent / "data" / "tracefmt" / "zephyr_v4.4_metadata"
T1, T2, IDLE = 0x2000_0100, 0x2000_0200, 0x2000_0300


def name20(s: str) -> bytes:
    return s.encode().ljust(20, b"\0")[:19] + b"\0"


def rec(ts: int, event_id: int, payload: bytes = b"", header: str = "ts32_id8") -> bytes:
    if header == "ts32_id8":
        return struct.pack("<IB", ts, event_id) + payload
    if header == "ts32_id16":
        return struct.pack("<IH", ts, event_id) + payload
    if header == "ts64_id16":
        return struct.pack("<QH", ts, event_id) + payload
    return struct.pack("<H", event_id) + payload


def thread(tid: int, name: str) -> bytes:
    return struct.pack("<I", tid) + name20(name)


def scenario(header: str = "ts32_id8", base: int = 1_000) -> bytes:
    return b"".join([
        rec(base, 0x13, thread(T1, "sensor"), header),
        rec(base + 1, 0x19, thread(T1, "sensor") + struct.pack("<II", 0x2000_1000, 1024), header),
        rec(base + 2, 0x12, thread(T1, "sensor") + struct.pack("<b", -2), header),
        rec(base + 10, 0x11, thread(IDLE, "idle"), header),
        rec(base + 20, 0x1B, b"", header),
        rec(base + 25, 0x1C, b"", header),
        rec(base + 30, 0x10, thread(IDLE, "idle"), header),
        rec(base + 30, 0x11, thread(T1, "sensor"), header),
        rec(base + 40, 0x62, name20("frame") + struct.pack("<II", 7, 9), header),
        rec(base + 45, 0x24, struct.pack("<II", 0x2000_4000, 100), header),
        rec(base + 50, 0x18, thread(T1, "sensor"), header),
        rec(base + 50, 0x10, thread(T1, "sensor"), header),
    ])


def test_builtin_table_decodes_threads_isrs_and_named_events():
    trace = decode_ctf(scenario(), header="ts32_id8")
    assert trace.ts_hz == 1e9
    assert {t.id: (t.name, t.priority, t.stack_size) for t in trace.tasks.values()} == {
        T1: ("sensor", -2, 1024), IDLE: ("idle", None, None)}
    assert not getattr(trace, "idle", set())       # 名前でアイドルを決めない（使う側が idle_tasks で渡す）
    assert [(e.kind, e.ts) for e in trace.events if e.kind in ("task_in", "task_out", "isr_enter", "isr_exit")] == [
        ("task_in", 1010), ("isr_enter", 1020), ("isr_exit", 1025), ("task_out", 1030), ("task_in", 1030),
        ("task_out", 1050)]
    [mark] = [e for e in trace.events if e.kind == "marker"]
    assert (mark.text, mark.args["arg0"], mark.args["arg1"]) == ("frame", 7, 9)
    [sem] = [e for e in trace.events if e.kind == "object"]
    assert sem.name == "semaphore_take_enter" and sem.args == {"id": 0x2000_4000, "timeout": 100}
    assert [e.kind for e in trace.events].count("task_block") == 1


@pytest.mark.parametrize("header", ["ts32_id16", "ts64_id16", "id16"])
def test_other_header_forms(header):
    trace = decode_ctf(scenario(header), header=header)
    assert sum(1 for e in trace.events if e.kind == "task_in") == 2
    if header == "id16":
        assert trace.ts_hz is None and [e.ts for e in trace.events[:3]] == [1, 2, 3]


def test_32_bit_nanoseconds_wrap():
    data = rec(0xFFFF_FFF0, 0x1B) + rec(0x10, 0x1C)
    trace = decode_ctf(data, header="ts32_id8")
    assert [e.ts for e in trace.events] == [0xFFFF_FFF0, 0x1_0000_0010]
    # 1 周未満の前提で戻した回数を結果に出す（前提が外れると時刻が 2^32 の倍数ずれる）
    assert trace.ts_wraps == 1 and trace.summary()["ts_wraps"] == 1


def test_real_metadata_file():
    meta = parse_tsdl(METADATA.read_text(encoding="utf-8"))
    assert len(meta.events) == 242 and [f.size for f in meta.header] == [32, 16]
    assert meta.events[0x12].fields[2].signed
    trace = decode_ctf(scenario("ts32_id16"), metadata=meta)
    assert sum(1 for e in trace.events if e.kind == "task_in") == 2


def test_unknown_ids_stop_decoding_with_a_reason():
    trace = decode_ctf(rec(1, 0x1B) + rec(2, 0xF7, b"\x01\x02") + rec(3, 0x1C), header="ts32_id8")
    assert len(trace.events) == 1 and trace.errors == 1 and "0xf7" in trace.notes[0]


def test_ram_backend_ends_at_zero_fill():
    data = scenario() + bytes(64)
    trace = decode_ctf(data, header="ts32_id8", ram_buffer=True)
    assert len(trace.events) == 12 and trace.errors == 0


def test_streaming_in_pieces():
    data = scenario()
    d = CtfDecoder(header="ts32_id8")
    for i in range(0, len(data), 5):
        d.feed(data[i:i + 5])
    assert len(d.trace.events) == 12


def test_bad_tsdl():
    with pytest.raises(CtfError):
        parse_tsdl("event { name = x; id = 1; fields := struct { weird_t a; }; };")
    with pytest.raises(CtfError):
        builtin_metadata("ts16")


def test_the_header_form_is_not_assumed():
    """見出しの形（版で違う）は metadata の event_header か、使う側の header で決める。4.3 までの形を既定にしない。"""
    with pytest.raises(CtfError, match="header"):
        decode_ctf(scenario())
    with pytest.raises(CtfError, match="header"):
        CtfDecoder()
    with pytest.raises(CtfError, match="event_header"):
        parse_tsdl("event { name = x; id = 1; };")
    assert [f.size for f in parse_tsdl("event { name = x; id = 1; };", header="ts64_id16").header] == [64, 16]


def test_object_events_are_zephyrs_names_not_a_name_prefix():
    """カーネルオブジェクトの操作（kind object）は Zephyr の metadata にある名前の表で決める。metadata の任意の名前を
    接頭辞（event_・work_ 等）で object にしない。"""
    meta = parse_tsdl("event { name = semaphore_give_enter; id = 1; fields := struct { uint32_t id; }; };"
                      "event { name = event_custom_counter; id = 2; fields := struct { uint32_t id; }; };",
                      header="ts32_id8")
    trace = decode_ctf(rec(1, 1, struct.pack("<I", 7)) + rec(2, 2, struct.pack("<I", 8)), metadata=meta)
    assert [(e.name, e.kind) for e in trace.events] == [("semaphore_give_enter", "object"),
                                                        ("event_custom_counter", "other")]
