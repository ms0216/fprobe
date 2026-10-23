"""Primer 20K + Nucleo で、DAP のイベント記録が実機で動くことを確かめる（M9b）。

- pyOCD の書き込み・読み出しが、ポストされた読みを対応付けた後の正しい番地と値で記録される
- クロストリガの割り込み書き込みが種類 INJECT として記録される
- ロジアナの深いバッファ（RLE）と同時に動かしても、どちらも取りこぼさない
"""

import time

import pytest

pytest.importorskip("pyocd")
from tests.hil.program import program  # noqa: E402
from fprobe import generated as g  # noqa: E402
from fprobe.events import INJECT_KINDS, EventLog, resolve  # noqa: E402
from fprobe.la import DeepAnalyzer  # noqa: E402
from fprobe.session import Session  # noqa: E402
from fprobe.xtrig import DHCSR, CrossTrigger, Source  # noqa: E402
from fprobe_gw.usb.fprobe_device import STRINGS, STR_SERIAL  # noqa: E402

pytestmark = pytest.mark.hil

SERIAL = STRINGS[STR_SERIAL - 1]
SRAM = 0x20004000


@pytest.fixture(scope="module")
def programmed():
    program("fprobe")


def test_host_accesses_and_injection_are_recorded(programmed):
    with Session.open(SERIAL) as s, s.debug(target_override="stm32f401xe", frequency=4_000_000,
                                            connect_mode="attach") as debug:
        target = debug.target
        log, la, xt = EventLog(s.device), DeepAnalyzer(s.device), CrossTrigger(s.device)
        la.start(rle=True, mask=1 << 15, value=1 << 15, post_beats=0)        # ch15 は常に 0: 止まらず記録し続ける
        log.start()
        target.write32(SRAM, 0x5A5AC3C3)
        target.flush()
        assert target.read32(SRAM) == 0x5A5AC3C3
        target.resume()
        xt.set_inject()
        xt.route(to_inject=Source.HOST)
        xt.fire()                                                              # ハードウェアで DHCSR に停止を書く
        time.sleep(0.05)
        xt.route()
        assert target.get_state() == target.State.HALTED
        log.stop()
        written, lost = log.counts()
        events = log.read()
        assert written == len(events) > 0 and lost == 0
        accesses = resolve(events)
        assert any(a.kind == "ap_write" and a.address == SRAM and a.value == 0x5A5AC3C3 for a in accesses)
        assert any(a.kind == "ap_read" and a.address == SRAM and a.value == 0x5A5AC3C3 for a in accesses)
        injected = [e for e in events if e.kind in INJECT_KINDS]
        assert len(injected) == 1 and injected[0].address == DHCSR and injected[0].ok
        print("割り込み書き込みの転送数", injected[0].transfers)          # R20: 種類 11 の回路なら数、旧い種類 2 は None
        assert [e.time for e in events] == sorted(e.time for e in events)
        assert not la.status() >> g.LA_DEEP_STATUS_OVERRUN[0] & 1                # ロジアナも取りこぼしていない
        s.device.write32(g.LA_DEEP_CONTROL, 0)
        target.resume()


def test_uart_bytes_are_recorded_on_the_same_timebase(programmed):
    # 時刻相関: PC → ターゲットの UART のバイトが、DAP の転送と同じ時間軸のイベント（種類 4）として記録される
    from serial import Serial
    from serial.tools import list_ports

    from fprobe.events import KIND_UART_TX
    port = next(p.device for p in list_ports.comports() if p.vid == g.USB_VID and p.pid == g.USB_PID)
    with Session.open(SERIAL) as s, Serial(port, 115200) as com:
        log = EventLog(s.device)
        log.start()
        before = s.device.time()
        com.write(b"AB")
        com.flush()
        time.sleep(0.05)
        after = s.device.time()
        log.stop()
        sent = [e for e in log.read() if e.kind == KIND_UART_TX]
        assert [e.data & 0xFF for e in sent] == [0x41, 0x42]
        assert before < sent[0].time <= sent[1].time < after
        assert log.counts()[1] == 0
