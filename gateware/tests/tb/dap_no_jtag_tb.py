"""JTAG を外した構成（dap_harness の HAS_JTAG = false。B22 の標準構成）の dap_processor のテストベンチ。

DAP_Info の Capabilities に JTAG が無く、DAP_Connect の JTAG は失敗で答え、SWD はそのまま使える。
"""

import cocotb

from fprobe_gw import dap_info
from tests.models.swd_target import DPIDR
from tests.tb.dap_processor_tb import DP_IDCODE_R, OK, setup


@cocotb.test()
async def jtag_is_neither_declared_nor_accepted(dut):
    dap, _ = await setup(dut)
    assert await dap.command(bytes([0x00, 0xF0])) == bytes([0x00, 1, dap_info.capabilities(jtag=False)])
    assert await dap.command(bytes([0x02, 2])) == bytes([0x02, 0])              # JTAG: 失敗
    for info_id, text in dap_info.STRINGS.items():                              # 文字列は同じ（ROM）
        r = await dap.command(bytes([0x00, info_id]))
        assert r[2:2 + r[1]].rstrip(b"\0").decode() == text
    await dap.connect()                                                         # SWD は使える
    assert await dap.transfer((DP_IDCODE_R, None)) == (1, OK, [DPIDR])
