import time, sys
sys.path.insert(0, "host/tests")  # リポジトリの根から実行する
from test_fpga_obs import group_record, mark, sync, value
from fprobe.fpga_obs import ObsDecoder
import random
N = 300_000
ev = [group_record(0)]
t = 0
for i in range(N):
    ev.append(value(0 if i == 0 else 4096, i & 0xFFFF))
    if i % 1000 == 500:
        ev.append(mark(10, (i // 1000) & 0xFF)); ev.append(sync((i // 1000) & 0xFF, 1000 + i * 4096))
d = ObsDecoder(nominal_ratio=1.0)
t0 = time.perf_counter()
out = []
for k in range(0, len(ev), 2000):
    out += d.feed(ev[k:k + 2000])
out += d.flush()
dt = time.perf_counter() - t0
print(f"{len(ev)} イベント → {len(out)} 件、{dt:.2f} s、{len(ev)/dt/1e3:.0f} k イベント/秒")
import cProfile, pstats
d = ObsDecoder(nominal_ratio=1.0)
pr = cProfile.Profile(); pr.enable()
for k in range(0, len(ev), 2000):
    d.feed(ev[k:k + 2000])
d.flush(); pr.disable()
pstats.Stats(pr).sort_stats("tottime").print_stats(8)
