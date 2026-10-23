# 試験の偽の xsdb: XsdbMemAp が送る行（catch で包んだ Tcl）を部分の文字で解き、@@ <誤り> <値> で答える。
# メモリは FPROBE_FAKE_XSDB_MEM（16 進の番地 → 16 進の語の JSON）、チェーンは FPROBE_FAKE_XSDB_JTAG（jtag targets の出力）。
import json
import os
import re
import sys

mem = {int(k, 16): int(v, 16) for k, v in json.loads(os.environ.get("FPROBE_FAKE_XSDB_MEM", "{}")).items()}
jtag = os.environ.get("FPROBE_FAKE_XSDB_JTAG", "  1  Xilinx TUL 1234-tulA\\n     2  arm_dap (idcode 4ba00477 irlen 4)\\n"
                                              "     3  xc7z020 (idcode 23727093 irlen 6 fpga)")   # 改行は \n の 2 文字のまま（本物の返事の包み方と同じ）
log = os.environ.get("FPROBE_FAKE_XSDB_LOG")
for line in sys.stdin:
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(line)
    if "exit" == line.strip():
        break
    m = re.search(r"mrd -force -value (0x[0-9a-fA-F]+) (\d+)", line)
    if m and os.environ.get("FPROBE_FAKE_XSDB_HANG"):
        import time
        time.sleep(60)                                # 返事をしない（hw_server が止まった・ケーブルが抜けた）
    if m:
        a, n = int(m[1], 16), int(m[2])
        if any(a + 4 * i not in mem for i in range(n)):
            print("@@ 1 Memory read error at 0x%X. AHB AP transaction error, DAP status 0x30000021" % a, flush=True)
        else:
            print("@@ 0 " + " ".join(str(mem[a + 4 * i]) for i in range(n)), flush=True)
    elif "jtag targets" in line:
        print("@@ 0 " + jtag, flush=True)
    else:
        print("@@ 0 ", flush=True)
