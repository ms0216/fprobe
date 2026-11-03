"""開くときの識別の READ（SYSTEM_MAGIC）の送り直し: 待つ時間は開く側が明示して渡す（暗黙の既定にしない）。送り直した回数は
事実として返す（Device.identify_resends・probe.info の link_open）。2026-11-03 のレビュー（docs/reports/2026-11-03-cloud-A.md）で、
Ethernet で開くときに 15 秒まで黙って送り直していた（使う側が明示しない送り直しで、数えもしなかった）のを直した。"""

import inspect

import pytest
from test_control import FakeDevice

from fprobe import Device
from fprobe.session import Session


class SilentAtFirst(FakeDevice):
    """最初の n 回の要求には応答しない（リンクの確立前）。"""

    def __init__(self, silent: int):
        super().__init__()
        self.silent = silent

    def send(self, message):
        if self.silent:
            self.silent -= 1
            self.requests.append(message)
            return
        super().send(message)


def test_without_a_wait_the_identify_read_is_not_resent():
    fake = SilentAtFirst(1)
    with pytest.raises(TimeoutError) as e:
        Device(fake)
    assert len(fake.requests) == 1
    assert "0 回" in "".join(getattr(e.value, "__notes__", []))


def test_the_resends_within_the_given_wait_are_counted():
    fake = SilentAtFirst(3)
    dev = Device(fake, identify_wait_s=5.0)
    assert dev.identify_resends == 3 and dev.identify_wait_s == 5.0
    assert Device(FakeDevice()).identify_resends == 0


def test_a_negative_wait_is_refused():
    with pytest.raises(ValueError):
        Device(FakeDevice(), identify_wait_s=-1.0)


def test_opening_over_ethernet_needs_the_wait_given_by_the_caller():
    # Ethernet で開く口は Session.open_eth だけ（使われない Device.open_eth は §B の C3 で消した）
    parameter = inspect.signature(Session.open_eth).parameters["link_up_wait_s"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY and parameter.default is inspect.Parameter.empty


def test_the_probe_over_ethernet_needs_the_wait_given_by_the_caller():
    from fprobe.probe import Probe
    with pytest.raises(ValueError, match="link_up_wait_s"):
        Probe(eth="192.0.2.1")


def test_probe_info_returns_the_wait_and_the_resends(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from probe_fakes import FakeDevice as ProbeFakeDevice

    from fprobe.probe import Probe
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    p = Probe("unused")
    device = ProbeFakeDevice()
    device.identify_wait_s, device.identify_resends = 15.0, 2
    p._session = SimpleNamespace(device=device, address={"link": "eth", "address": "192.0.2.1"})
    assert p.probe_info()["link_open"] == {"identify_wait_s": 15.0, "identify_resends": 2}


def test_the_entry_points_pass_their_own_wait():
    # CLI（fprobe）・デーモン・MCP（デーモンを起動する）は、それぞれの値を明示して渡す
    from fprobe import __main__ as cli
    from fprobe import mcp_client
    from fprobe.api import daemon
    assert cli.ETH_LINK_UP_WAIT_S > 0 and daemon.ETH_LINK_UP_WAIT_S > 0 and mcp_client.ETH_LINK_UP_WAIT_S > 0
    args = SimpleArgs(eth="192.0.2.1", eth_link_up_wait=7.5)
    command = mcp_client.daemon_command(args)
    i = command.index("--eth-link-up-wait")
    assert float(command[i + 1]) == 7.5
    assert "--eth-link-up-wait" not in mcp_client.daemon_command(SimpleArgs(serial="0001"))


class SimpleArgs:
    def __init__(self, **kw):
        self.listen, self.serial, self.port, self.eth, self.baud, self.bitstreams = 8765, None, None, None, 115200, None
        self.eth_link_up_wait = 15.0
        self.__dict__.update(kw)
