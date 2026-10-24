"""Probe の端末の口（fprobe.probe_terminal、G04）: RTT の上り・SWO の ITM のポート・UART・セミホスティングを
位置 since から読む（ほかの読み手の位置は動かさない）。機器は使わない（rtt_demo の ELF を置いた偽のメモリ、偽の SWO・UART）。"""

from types import SimpleNamespace

import pytest
from test_probe_observe import RTT_DEMO, FakeCpu, FakeSwo, connect, target_writes, wait_for
from test_probe_recording import FakeDevice
from test_probe_streams import FakeLink

from fprobe.mcp_server import Probe


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    p = Probe("unused")
    p._session = SimpleNamespace(device=FakeDevice(), link=FakeLink())
    yield p
    p._close_streams()
    p._stop_observing()


def itm(port: int, text: bytes) -> bytes:
    """ITM のスティミュラス・ポート port への 1 バイトずつの書き込み（ヘッダ = port << 3 | 1）。"""
    return b"".join(bytes([port << 3 | 1, b]) for b in text)


def test_sources_list_rtt_channels_uart_and_semihosting(probe):
    assert [s["id"] for s in probe.terminal_sources()["sources"]] == ["uart", "semihosting"]
    cpu = FakeCpu(image=RTT_DEMO)
    connect(probe, cpu)
    probe.elf_load(str(RTT_DEMO))
    probe.rtt_start()
    sources = probe.terminal_sources()["sources"]
    assert sources[0]["id"] == "rtt0" and sources[0]["writable"] and sources[0]["kind"] == "rtt"


def test_rtt_is_read_from_a_position_without_moving_the_tool_cursor(probe):
    cpu = FakeCpu(image=RTT_DEMO)
    connect(probe, cpu)
    probe.elf_load(str(RTT_DEMO))
    probe.rtt_start()
    target_writes(cpu, 0, "héllo\n".encode())
    first = probe.terminal_read("rtt0", timeout_s=2)
    assert first["data"] == "héllo\n" and first["start"] == 0 and first["next"] == 7
    assert probe.terminal_read("rtt0", since=first["next"])["data"] == ""
    assert probe.rtt_read(timeout_s=1)["data"] == "héllo\n"                  # AI の読み（cursor）は別


def test_swo_ports_are_split(probe):
    swo = FakeSwo([itm(0, b"boot\n") + itm(1, b"dbg 1\n"), itm(0, b"tick\n")])
    connect(probe, FakeCpu(), swo)
    probe.swo_start(baud=1_000_000)
    probe.terminal_read("swo0")                                              # 初めての読みで背景の受信を始める
    wait_for(lambda: probe.terminal_read("swo0")["data"] == "boot\ntick\n")
    assert probe.terminal_read("swo1")["data"] == "dbg 1\n"
    assert [s["id"] for s in probe.terminal_sources()["sources"] if s["kind"] == "swo"] == ["swo0", "swo1"]
    with pytest.raises(ValueError):
        probe.terminal_read("swo32")


def test_uart_is_received_in_the_background_and_still_readable_by_tools(probe):
    link = probe.session.link
    assert probe.terminal_read("uart")["data"] == ""
    link.rx += b"boot ok\r\n"
    wait_for(lambda: probe.terminal_read("uart")["data"] == "boot ok\r\n")
    assert probe.uart_read(timeout_s=0) == {"data": "boot ok\r\n", "baud": link.uart_baud}   # 使った速さも返す


def test_semihosting_and_bad_names(probe):
    probe._received("semihosting", b"exit 0\n", 0.0)
    assert probe.terminal_read("semihosting", since=2)["data"] == "it 0\n"
    with pytest.raises(ValueError):
        probe.terminal_read("itm")


def test_times_of_each_received_chunk(probe):
    # 行の時刻（G04）: 受け取りごとの壁時計の秒。記録していなければ時間軸の時刻は無い
    probe._received("semihosting", "héllo\n".encode(), 100.0)
    probe._received("semihosting", b"two\n", 101.5)
    r = probe.terminal_read("semihosting", times=True)
    assert r["data"] == "héllo\ntwo\n"
    assert [m["at"] for m in r["times"]] == [0, 6]                         # 文字の位置（é は 2 バイト）
    assert r["times"][1]["wall"] - r["times"][0]["wall"] == pytest.approx(1.5)
    assert [m["t"] for m in r["times"]] == [None, None] and r["timebase_hz"] is None
    tail = probe.terminal_read("semihosting", since=3, times=True)       # 受け取りの途中から読んでも始めの時刻が付く
    assert [m["at"] for m in tail["times"]] == [0, 4] and tail["times"][0]["wall"] == r["times"][0]["wall"]
    assert "times" not in probe.terminal_read("semihosting")


def test_times_carry_the_timebase_while_recording(probe):
    from types import SimpleNamespace
    probe._recorder = SimpleNamespace(clock=SimpleNamespace(to_ticks=lambda h: (round(h * 1e6), 0.0)), timebase_hz=1e6,
                                      add_host=lambda *a, **k: None)
    try:
        probe._received("semihosting", b"boot\n", 2.5)
        r = probe.terminal_read("semihosting", times=True)
        assert r["times"][0]["t"] == 2_500_000 and r["timebase_hz"] == 1e6
    finally:
        probe._recorder = None


def test_byte_log_keeps_the_mark_of_the_first_readable_byte():
    from fprobe.probe_observe import ByteLog
    log = ByteLog(limit=8)
    log.append(b"aaaa", 1.0)
    log.append(b"bbbb", 2.0)
    log.append(b"cccc", 3.0)                                              # 先頭の 4 バイトはあふれて捨てる
    assert log.start == 4 and [m[0] for m in log.marks] == [4, 8]
    assert log.times(6, 6) == [(6, 2.0, None), (8, 3.0, None)]


def test_uart_baud_is_configured_and_listed(probe):
    probe.session.link.uart_baud = None
    probe.session.link.set_uart_baud = lambda baud: setattr(probe.session.link, "uart_baud", baud)
    assert probe.uart_configure(baud=9600) == {"baud": 9600}
    uart = next(s for s in probe.terminal_sources()["sources"] if s["id"] == "uart")
    assert uart["baud"] == 9600


def test_rtt_virtual_terminals_are_split_out_of_the_text(probe):
    # SEGGER の仮想端末（SEGGER_RTT_SetTerminal / TerminalOut）: 0xFF と端末の番号 '0'〜'F' は文字から除き、その文字の
    # 位置と番号を terminals に返す（画面は端末ごとのタブに分ける）。受け取りの時刻の位置もずらす
    probe._received(0, b"boot\n\xff1err\n", 1.0)
    probe._received(0, b"\xff0ok\n", 2.0)
    r = probe.terminal_read("rtt0", times=True, terminals=True)
    assert r["data"] == "boot\nerr\nok\n" and r["next"] == 16
    assert r["terminals"] == [{"at": 5, "id": 1}, {"at": 9, "id": 0}]
    assert [m["at"] for m in r["times"]] == [0, 9]
    raw = probe.terminal_read("rtt0")                                       # 頼まなければ今までどおり（バイトのまま）
    assert "terminals" not in raw and raw["data"].count("�") == 2


def test_rtt_virtual_terminal_marker_split_across_reads_waits_for_its_number(probe):
    probe._received(0, "é".encode() + b"\xff", 1.0)
    r = probe.terminal_read("rtt0", terminals=True)
    assert r["data"] == "é" and r["next"] == 2 and r["terminals"] == []      # 番号がまだ来ていない 0xFF は次の読みへ
    probe._received(0, b"Ax", 2.0)
    r = probe.terminal_read("rtt0", since=r["next"], terminals=True)
    assert r["data"] == "x" and r["terminals"] == [{"at": 0, "id": 10}] and r["next"] == 5


def test_rtt_virtual_terminal_bad_number_is_left_as_text(probe):
    probe._received(0, b"a\xffzb", 1.0)
    r = probe.terminal_read("rtt0", terminals=True)
    assert r["data"] == "a�zb" and r["terminals"] == []
    assert "terminals" not in probe.terminal_read("uart", terminals=True)   # RTT の源だけ
