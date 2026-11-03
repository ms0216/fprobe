"""実機: Primer の JTAG（PJTAG）で PYNQ-Z2 の 2 コアを CTI で止め、止めた時点の Linux のプロセスの一覧を作り、SSH の `ps` と突き合わせる。

FPROBE_ZYNQ_JTAG=1・FPROBE_ZYNQ_SSH・FPROBE_ZYNQ_KEY・FPROBE_ZYNQ_OFFSETS（pynq/offsets-pynq.json。カーネルの構造体の配置）が要る。
**Linux 全体が止まる**ので、読んだらすぐ再開する。起動は reserved-memory 方式（`mem=479M` なし）で、RAM は 0〜0x1E000000 が Linux
（0x1E000000 から上は fprobe の深いバッファ）。線形マップの上端は、この試験が決める値（0xDE000000）。
"""
import os
import subprocess

import pytest

ENV = ("FPROBE_ZYNQ_JTAG", "FPROBE_ZYNQ_SSH", "FPROBE_ZYNQ_KEY", "FPROBE_ZYNQ_OFFSETS")
pytestmark = pytest.mark.skipif(not all(os.environ.get(n) for n in ENV), reason="環境変数が足りない: " + ", ".join(ENV))
LIN = dict(page_offset=0xC0000000, phys_offset=0, linear_end=0xDE000000)


def test_the_processes_read_from_the_halted_kernel_match_ps():
    from fprobe.arm_a_target import arm_a_part
    from fprobe.linux_halt.backend_arma import ArmACores
    from fprobe.linux_halt.cli import list_processes
    from fprobe.linux_halt.cti import ApbIo
    from fprobe.linux_halt.kernel import Kernel, load_layout_json
    from fprobe.plugins import zynq_cti
    from fprobe.session import Session
    layout = load_layout_json(os.environ["FPROBE_ZYNQ_OFFSETS"], **LIN)
    session = Session.open(None)
    cores = ArmACores.open(session.dap_access(), arm_a_part("zynq7000"), frequency_hz=2_000_000, **LIN)
    rows = None
    try:
        cross = zynq_cti.cross_halt(zynq_cti.ZYNQ7000, ApbIo(cores._dap, cores.debug_ap), apb_offset=zynq_cti.ZYNQ7000.apb_offset_for(cores.discovery), ack_timeout_polls=100)
        cores.use_cross_halt(cross, request_skew_upper_bound_ns=1000, event_skew_upper_bound_ns=None, event_trigin=None,
                             hardware_halt_wait_s=0.05)
        report = cores.halt_all()
        assert report.cores_halted == (0, 1), report
        task = Kernel(cores.read_kernel, layout, max_tasks=100000, max_threads=4096).verify()
        print(f"\ninit_task: pid {task.pid} comm {task.comm!r}")
        rows = list_processes(Kernel(cores.read_kernel, layout, max_tasks=100000, max_threads=4096))
    finally:
        if any(cores.halted()):
            cores.resume_all()
        cores.close()
        session.close()
    mine = {r.tgid: r.comm for r in rows}
    out = subprocess.run(["ssh", "-i", os.environ["FPROBE_ZYNQ_KEY"], "-o", "BatchMode=yes", os.environ["FPROBE_ZYNQ_SSH"],
                          "ps -e -o pid=,comm="], capture_output=True, text=True, check=True).stdout
    theirs = {int(l.split(None, 1)[0]): l.split(None, 1)[1].strip() for l in out.splitlines() if l.strip()}
    # ps は kworker の名前に作業キュー名を足して表示する（kworker/0:1 → kworker/0:1-eve）ので、名前は前方一致で見る
    differ = {pid: (name, theirs[pid]) for pid, name in mine.items() if pid in theirs and not theirs[pid].startswith(name)}
    only_mine = sorted(pid for pid in mine if pid not in theirs)
    print(f"止めて読んだ: {len(mine)} 個、ps: {len(theirs)} 個。名前が違う: {differ}。止めて読んだ側にだけある pid: {only_mine}")
    print("ps 側にだけある（止めた後に起動した ssh・ps など）:", sorted((p, n) for p, n in theirs.items() if p not in mine))
    assert task.pid == 0 and task.comm.startswith("swapper")
    assert len(mine) > 20 and not differ
