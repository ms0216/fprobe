"""caps_rom（ハーネス経由）のテストベンチ。期待値は tests/test_caps.py の TEST_CAPS の符号化。"""

import cocotb

from fprobe_gw import caps
from tests.tb.common import start_clock_and_reset
from tests.tb.csr_decoder_tb import CsrMaster
from tests.test_caps import TEST_CAPS


@cocotb.test()
async def reads_content_then_zeros_and_ignores_writes(dut):
    await start_clock_and_reset(dut)
    bus = CsrMaster(dut)
    expected = caps.encode(TEST_CAPS)
    words = len(expected) // 4
    data = bytearray()
    for i in range(words + 3):
        value, err, _ = await bus.access(i)
        assert not err
        data += value.to_bytes(4, "little")
    assert bytes(data) == expected + bytes(12)
    await bus.access(0, write=0)
    assert (await bus.access(0))[0] == int.from_bytes(b"FCAP", "little")
    assert (await bus.access(1023))[0] == 0
