"""U52: UART の RTS/CTS（CSR uart.flow・uart.lines）と、U51: SWO の受信の方式（session.swo_start）。機器には触れない。"""

from types import SimpleNamespace

import pytest

from fprobe import generated as g
from fprobe.session import swo_start
from fprobe.uart_flow import UartFlow


class FakeDevice:
    def __init__(self, uart_flow=(4, 1)):
        self.regs = {g.UART_FLOW: 0, g.UART_LINES: 0}
        self._caps = SimpleNamespace(uart_flow=uart_flow)

    def capabilities(self):
        return self._caps

    def read32(self, addr):
        return self.regs[addr]

    def write32(self, addr, value):
        self.regs[addr] = value


def test_configure_maps_channels_to_candidate_indexes():
    dev = FakeDevice()
    flow = UartFlow(dev)
    flow.configure(auto=True, rts_channel=4, cts_channel=1)
    word = dev.regs[g.UART_FLOW]
    assert word == 1 | 1 << 4 | 0 << 8 | 1 << 12 | 1 << 16
    assert flow.config() == {"auto": True, "rts_channel": 4, "cts_channel": 1}
    flow.configure(auto=False, rts_channel=None, cts_channel=None)
    assert dev.regs[g.UART_FLOW] == 0
    assert flow.config() == {"auto": False, "rts_channel": None, "cts_channel": None}


def test_configure_rejects_unknown_pins_and_designs_without_the_circuit():
    flow = UartFlow(FakeDevice())
    with pytest.raises(ValueError, match="RTS"):
        flow.configure(False, rts_channel=5, cts_channel=None)             # 5 = UART RX は候補でない
    with pytest.raises(ValueError, match="同じ端子"):
        flow.configure(False, rts_channel=4, cts_channel=4)
    none = UartFlow(FakeDevice(uart_flow=()))
    with pytest.raises(RuntimeError, match="回路が無い"):
        none.configure(True, rts_channel=None, cts_channel=None)
    none.configure(False, rts_channel=None, cts_channel=None)               # 何も使わないのはよい


def test_lines():
    dev = FakeDevice()
    dev.regs[g.UART_LINES] = 0b1011
    lines = UartFlow(dev).lines()
    assert (lines.cts, lines.rts, lines.host_rts, lines.host_break) == (True, True, False, True)


class FakeProtocol:
    def __init__(self, refuse_mode=None):
        self.sent = []
        self.refuse_mode = refuse_mode

    def swo_mode(self, mode):
        self.sent.append(("mode", mode))
        return 0xFF if mode == self.refuse_mode else 0

    def swo_transport(self, transport):
        self.sent.append(("transport", transport))
        return 0

    def swo_baudrate(self, baud):
        self.sent.append(("baud", baud))
        return baud

    def swo_control(self, start):
        self.sent.append(("control", start))
        return 0


def fake_probe(protocol):
    interface = SimpleNamespace(started=False)
    interface.start_swo = lambda: setattr(interface, "started", True)
    link = SimpleNamespace(_protocol=protocol, _interface=interface, _swo_status=None, flush=lambda: None)
    return SimpleNamespace(_link=link)


def test_swo_start_sends_the_mode_in_pyocd_order():
    protocol = FakeProtocol()
    probe = fake_probe(protocol)
    assert swo_start(probe, 12_500_000, "manchester") == 12_500_000
    assert protocol.sent == [("mode", 0), ("transport", 0), ("transport", 2), ("mode", 2), ("baud", 12_500_000),
                             ("control", 1)]
    assert probe._link._interface.started


def test_swo_start_raises_when_the_probe_refuses_the_mode():
    with pytest.raises(RuntimeError, match="manchester"):
        swo_start(fake_probe(FakeProtocol(refuse_mode=2)), 2_000_000, "manchester")
    with pytest.raises(ValueError):
        swo_start(fake_probe(FakeProtocol()), 2_000_000, "parallel")
