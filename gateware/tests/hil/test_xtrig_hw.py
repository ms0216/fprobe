"""Primer 20K + Nucleo で、クロストリガが実機で動くことを確かめる（M9）。

- 走っている Nucleo を、ロジアナのトリガ（プローブの UART 送信の立ち下がり）でハードウェアだけで止める
- pyOCD がこの番地に書いた瞬間でロジアナをトリガする
どちらも pyOCD のセッションを張ったまま行い、割り込みの後も pyOCD がそのまま使えることを確かめる。
"""

import time

import pytest

pytest.importorskip("pyocd")
from serial import Serial  # noqa: E402
from serial.tools import list_ports  # noqa: E402

from tests.hil.program import program  # noqa: E402
from fprobe import generated as g  # noqa: E402
from fprobe.la import LogicAnalyzer  # noqa: E402
from fprobe.session import Session  # noqa: E402
from fprobe.xtrig import CrossTrigger, Event, Pattern, Source, Stage, TriggerSequencer  # noqa: E402
from fprobe_gw.boards.primer20k import PRIMER20K  # noqa: E402
from fprobe_gw.usb.fprobe_device import STRINGS, STR_SERIAL  # noqa: E402

pytestmark = pytest.mark.hil

PINS = PRIMER20K.debug_connector
SERIAL = STRINGS[STR_SERIAL - 1]
SRAM = 0x20004000
NEVER = 1 << 15                       # ch15 は予約（常に 0）: ここが 1 になる値一致はクロストリガでしか成立しない


@pytest.fixture(scope="module")
def programmed():
    program("fprobe")


@pytest.fixture
def session(programmed):
    with Session.open(SERIAL) as s, s.debug(target_override="stm32f401xe", frequency=4_000_000,
                                            connect_mode="attach") as debug:
        xt = CrossTrigger(s.device)
        xt.set_inject()
        yield s, debug.target, xt
        xt.route()


def test_analyzer_trigger_halts_the_running_target(session):
    s, target, xt = session
    target.resume()
    assert target.get_state() == target.State.RUNNING
    before = xt.inject_status().count
    la = LogicAnalyzer(s.device)
    period = la.start(rate_hz=25e6, pretrigger=1024, fall=1 << PINS["uart_tx"])
    xt.route(to_inject=Source.LA_TRIGGER)
    port = next(p.device for p in list_ports.comports() if p.vid == g.USB_VID and p.pid == g.USB_PID)
    with Serial(port, 115200) as com:
        com.write(b"\x00")                                     # スタートビットの立ち下がりでトリガ
        com.flush()
    la.wait()
    cap = la.read(period)
    deadline = time.monotonic() + 1
    while xt.inject_status().count == before and time.monotonic() < deadline:
        time.sleep(0.01)
    status = xt.inject_status()
    assert (status.result, status.count) == ("ok", before + 1)
    assert target.get_state() == target.State.HALTED
    latency = (status.time - cap.trigger_time) / s.device.capabilities().timebase_hz
    # 成功基準「トリガ成立から SWD の停止要求の開始まで 1 µs 以内」: トリガ後に SWCLK が最初に動くまで
    swclk = cap.channel(PINS["swclk"])
    start = next(i for i in range(cap.trigger, len(swclk) - 1) if swclk[i] != swclk[i + 1]) - cap.trigger
    print(f"trigger -> SWD start: {start * period * 1e9:.0f} ns, -> halt write accepted: {latency * 1e6:.1f} us")
    assert start * period < 1e-6
    assert 0 < latency < 200e-6                                # ホストが静かなら SWD 転送 10 回分
    target.resume()                                            # pyOCD はそのまま使える
    assert target.get_state() == target.State.RUNNING


def test_debugger_write_to_an_address_triggers_the_analyzer(session):
    s, target, xt = session
    target.halt()
    xt.set_match(SRAM + 8)
    count_before, _ = xt.match_status()
    la = LogicAnalyzer(s.device)
    period = la.start(rate_hz=25e6, pretrigger=4096, mask=NEVER, value=NEVER)
    xt.route(to_la=Source.DAP_MATCH)
    target.write32(SRAM, 1)                                    # 別の番地では成立しない
    target.flush()                                             # pyOCD は書き込みを溜めるので送らせる
    assert not s.device.read32(g.LA_STATUS) >> g.LA_STATUS_TRIGGERED[0] & 1
    target.write32(SRAM + 8, 0x12345678)
    target.flush()
    la.wait(timeout_s=1)
    cap = la.read(period)
    count, match_time = xt.match_status()
    assert count == count_before + 1
    assert abs(match_time - cap.trigger_time) < 10            # 1〜2 周期で届く
    # トリガの直前にその書き込みの SWD 転送（SWCLK が動いている）がある
    swclk = cap.channel(PINS["swclk"])[cap.trigger - 2000:cap.trigger]
    assert sum(1 for a, b in zip(swclk, swclk[1:]) if a and not b) >= 40
    assert target.read32(SRAM + 8) == 0x12345678


# PA5（LD2）をトグルし続ける Thumb のループ（SRAM に置いて走らせる）:
#   ldr r0, =GPIOA_ODR / movs r1, #0x20 / loop: ldr r2,[r0]; eors r2,r1; str r2,[r0];
#   movs r3, #255; d: subs r3, #1; bne d; b loop
TOGGLE_PA5 = [0x4804, 0x2120, 0x6802, 0x404A, 0x6002, 0x23FF, 0x3B01, 0xD1FD, 0xE7F8, 0xBF00]
GPIOA_ODR = 0x40020014


def test_target_gpio_edge_halts_the_cpu(session):
    # ターゲット自身の動作（PA5 = J12 の 4 番）でロジアナをトリガし、ハードウェアだけで CPU を止める
    s, target, xt = session
    target.reset_and_halt()
    target.write32(0x40023830, target.read32(0x40023830) | 1)                     # GPIOA のクロック
    target.write32(0x40020000, target.read32(0x40020000) & ~(3 << 10) | 1 << 10)  # PA5 = 出力
    target.write32(GPIOA_ODR, target.read32(GPIOA_ODR) & ~(1 << 5))
    code = b"".join(h.to_bytes(2, "little") for h in TOGGLE_PA5) + GPIOA_ODR.to_bytes(4, "little")
    target.write_memory_block8(SRAM, code)
    target.write_core_register("pc", SRAM | 1)
    target.write_core_register("xpsr", 1 << 24)
    target.flush()
    before = xt.inject_status().count
    la = LogicAnalyzer(s.device)
    period = la.start(rate_hz=25e6, pretrigger=1024, rise=1 << PINS["spare"])
    xt.route(to_inject=Source.LA_TRIGGER)
    target.resume()
    la.wait(timeout_s=1)
    cap = la.read(period)
    deadline = time.monotonic() + 1
    while xt.inject_status().count == before and time.monotonic() < deadline:
        time.sleep(0.01)
    status = xt.inject_status()
    assert (status.result, status.count) == ("ok", before + 1)
    assert target.get_state() == target.State.HALTED
    assert SRAM <= target.read_core_register("pc") < SRAM + len(code)              # ループの中で止まった
    pa5 = cap.channel(PINS["spare"])
    assert pa5[cap.trigger - 1] == 0 and pa5[cap.trigger] == 1
    latency = (status.time - cap.trigger_time) / s.device.capabilities().timebase_hz
    print(f"PA5 rise -> halt write: {latency * 1e6:.1f} us")
    assert 0 < latency < 200e-6
    target.reset()                                                                # 元のプログラムに戻す


def test_sequence_triggers_on_the_second_edge_within_a_time_limit(session):
    # 多段トリガ: UART 送信の立ち下がり（スタートビット）の後、1 ms 以内にもう一度立ち下がったらロジアナをトリガ
    s, _, xt = session
    seq = TriggerSequencer(s.device)
    fall = Pattern(fall=1 << PINS["uart_tx"])
    seq.configure([Stage(Event.PATTERN_A), Stage(Event.PATTERN_A, within_s=1e-3)], a=fall)
    port = next(p.device for p in list_ports.comports() if p.vid == g.USB_VID and p.pid == g.USB_PID)
    with Serial(port, 115200) as com:
        # 間隔が制限時間より長い 2 バイトでは成立しない（段 1 で時間切れ → 段 0 に戻る）
        seq.arm()
        for _ in range(2):
            com.write(b"\x00")
            com.flush()
            time.sleep(0.01)
        stage, fired, _ = seq.status()
        assert not fired and stage == 0                        # どちらの後も時間切れで段 0 に戻った
        la = LogicAnalyzer(s.device)
        period = la.start(rate_hz=25e6, pretrigger=4096, mask=NEVER, value=NEVER)   # 1 バイト目（87 µs 前）も入る
        xt.route(to_la=Source.SEQUENCE)
        seq.arm()
        com.write(b"\x00\x00")                                 # 続けて 2 バイト（間隔約 87 µs）
        com.flush()
        la.wait(timeout_s=1)
    cap = la.read(period)
    stage, fired, fire_time = seq.status()
    assert fired
    tx = cap.channel(PINS["uart_tx"])
    falls = [i for i in range(1, len(tx)) if tx[i - 1] and not tx[i]]
    before = [i for i in falls if i <= cap.trigger]
    # トリガは 2 回目の立ち下がりの直後（同期化と判定で数周期 = 0.1〜0.4 µs）
    assert len(before) == 2 and 0 <= (cap.trigger - before[-1]) * period < 1e-6, (before, cap.trigger)
    assert abs(fire_time - cap.trigger_time) < 10
