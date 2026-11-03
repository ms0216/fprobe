"""la_deep_buffer の試験用の、窓の PS 側の位置のレジスタ（rtl/la/la_ps_port.vhd）の模型。

la_ps_port と同じ約束: 位置（ps_la_consumed・ps_ev_consumed・ps_out_beats）は、流れの番号（dut.ps_epoch）が変わった次の周期に
0 に戻り、その番号を ps_ret_epoch に出す。PS の書きは、添えた番号（POS_TAG）が今の番号のときだけ受ける。"""

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge


class PsWindow:
    def __init__(self, dut, *, ev_mask=None, la_start=0):
        """la_start: 流れの開始のロジアナの位置（試験の TOTAL_START_BELOW_4G に合わせる。実機の la_ps_port は 0）。"""
        self.dut = dut
        self.la_start = la_start
        self.epoch = None
        self.stale_writes = 0
        self.ev_mask = ev_mask
        self.task = cocotb.start_soon(self._run())

    async def _run(self):
        dut = self.dut
        while True:
            await ReadOnly()
            e = int(dut.ps_epoch.value)
            await RisingEdge(dut.clk)
            if e != self.epoch:
                self.epoch = e
                dut.ps_la_consumed.value = self.la_start
                dut.ps_ev_consumed.value = 0
                dut.ps_out_beats.value = 0
                dut.ps_ret_epoch.value = e

    def write(self, tag, *, la=None, ev=None, beats=None):
        """PS の位置の書き（POS_TAG = tag）。受けたら True。"""
        if tag != self.epoch:
            self.stale_writes += 1
            return False
        dut = self.dut
        if la is not None:
            dut.ps_la_consumed.value = la & 0xFFFFFFFF
        if ev is not None:
            dut.ps_ev_consumed.value = ev if self.ev_mask is None else ev & self.ev_mask
        if beats is not None:
            dut.ps_out_beats.value = beats & 0xFFFFFFFF
        return True

    def stop(self):
        self.task.cancel()
