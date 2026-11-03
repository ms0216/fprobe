import pytest

from fprobe.linux.memap import JtagMemAp
from fprobe.raw_dap import DapError


class FakeDap:
    def __init__(self, mem, fail_at=None):
        self.mem, self.fail_at, self.calls = mem, fail_at, []

    def mem_read32_many(self, ap, addresses):
        self.calls.append((ap, list(addresses)))
        if self.fail_at in addresses:
            raise DapError("FAULT", "読み出し")
        return [self.mem[a] for a in addresses]


def test_reads_words_in_order_in_chunks_of_64():
    dap = FakeDap({0x1000 + 4 * i: i * 7 for i in range(150)})
    m = JtagMemAp(dap, ap=0, info={"via": "probe_jtag"})
    assert m.read_words(0x1000, 150) == [i * 7 for i in range(150)]
    assert [len(a) for _, a in dap.calls] == [64, 64, 22]
    assert all(ap == 0 for ap, _ in dap.calls)
    assert m.info["via"] == "probe_jtag"


def test_read_error_is_a_fact_not_a_zero():
    m = JtagMemAp(FakeDap({0x1000: 1, 0x1004: 2}, fail_at=0x1004), ap=0, info={})
    with pytest.raises(DapError):
        m.read_words(0x1000, 2)


def test_closed_port_is_refused():
    m = JtagMemAp(FakeDap({0x1000: 1}), ap=0, info={})
    m.close()
    with pytest.raises(RuntimeError, match="閉じ"):
        m.read_words(0x1000, 1)
