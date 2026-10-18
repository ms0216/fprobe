"""失敗の伝播を確かめるため、必ず失敗するテストベンチ。"""

import cocotb

from tests.tb.sim_smoke_tb import _count_four_cycles


@cocotb.test()
async def expects_wrong_value(dut):
    assert await _count_four_cycles(dut) == 0
