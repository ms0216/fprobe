"""プラグイン: OpenOCD 経由のバックエンド（`fprobe.linux_halt.backend_openocd.OpenOcdCores`）に渡すボードの名前。

本体はボード固有の値を持たない。値は、それぞれの OpenOCD の設定ファイル（コアごとのターゲット、SMP でない）に合わせる。

  * RPI2B: Raspberry Pi 2B（BCM2836、Cortex-A7 × 4）。プロトタイプの rpi2.cfg（ターゲット rpi2.cpu0〜3、DAP rpi2.dap、
    コアのデバッグレジスタは AP 0 = APB-AP）。
  * ZYNQ7000: PYNQ-Z2（Zynq-7020、Cortex-A9 × 2）。プロトタイプの tools/openocd/pynq_z2.cfg（ターゲット zynq.cpu0・1、
    DAP zynq.dap。OpenOCD の target/zynq_7000.cfg と同じ名前）。コアのデバッグレジスタは AP 1 = APB-AP（AP 0 は AHB-AP）。
"""
from ..linux_halt.backend_openocd import OcdBoard

__all__ = ["BOARDS", "RPI2B", "ZYNQ7000"]

RPI2B = OcdBoard(name="rpi2b", target_prefix="rpi2.cpu", dap="rpi2.dap", apb_ap=0, n_cores=4)
ZYNQ7000 = OcdBoard(name="zynq7000", target_prefix="zynq.cpu", dap="zynq.dap", apb_ap=1, n_cores=2)
BOARDS = {b.name: b for b in (RPI2B, ZYNQ7000)}
