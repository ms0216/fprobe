"""デーモン経由で試験用の方形波を切り替える。使い方: tg.py <period> <high> <edge>  /  tg.py off"""
import sys
sys.path.insert(0, "<home>/Desktop/DebugProbe-with-FPGA/host")
from fprobe.api.client import DaemonClient
from fprobe import generated as g
with DaemonClient("ws://127.0.0.1:7820/api", "testgen", "script") as c:
    if sys.argv[1] == "off":
        c.call("probe.csr.write", address=g.LA_TESTGEN_CONTROL, words=[0])
        print("方形波: 停止（Hi-Z）")
    else:
        period, high, edge = (int(x) for x in sys.argv[1:4])
        c.call("probe.csr.write", address=g.LA_TESTGEN_PERIOD, words=[period])
        c.call("probe.csr.write", address=g.LA_TESTGEN_HIGH, words=[high])
        c.call("probe.csr.write", address=g.LA_TESTGEN_CONTROL, words=[1 | edge << 1])
        print(f"方形波: 周期 {period}、high {high}、edge {edge}  → {300 / period:.2f} MHz、1 の幅 {high * 10 / 3:.1f} ns")
