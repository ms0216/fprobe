#!/usr/bin/env python3
# 測定の道具（PYNQ で動かす。1.7.6 の b2_top.py と同じ）: top.py 待つ秒 窓の秒 -> 窓の間のスレッドごとの CPU 時間（utime+stime）の上位
import os, sys, time
def snap():
    d = {}
    for p in os.listdir('/proc'):
        if not p.isdigit(): continue
        try:
            for t in os.listdir(f'/proc/{p}/task'):
                f = open(f'/proc/{p}/task/{t}/stat').read()
                name = f[f.index('(')+1:f.rindex(')')]; x = f[f.rindex(')')+2:].split()
                d[(p, t, name)] = (int(x[11]) + int(x[12]), int(x[36]))
        except OSError: pass
    return d
time.sleep(float(sys.argv[1])); a = snap(); w = float(sys.argv[2]); time.sleep(w); b = snap()
hz = os.sysconf('SC_CLK_TCK')
r = sorted(((b[k][0] - a[k][0], k, b[k][1]) for k in b if k in a), reverse=True)[:6]
for dlt, k, cpu in r: print('thread', k, 'cpu%%=%.1f' % (100.0 * dlt / hz / w), 'last_cpu', cpu)
