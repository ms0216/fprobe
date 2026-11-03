import sys, time
sys.path.insert(0, "<home>/Desktop/DebugProbe-with-FPGA/host")
from fprobe.la import DeepAnalyzer
from fprobe.session import Session
rate, dur = float(sys.argv[1]) * 1e6, float(sys.argv[2])
import time; _c=time.process_time(); _w=time.time()
with Session.open_eth("<家の LAN のアドレス>", link_up_wait_s=15.0) as s:
    la = DeepAnalyzer(s.device)
    lost0 = s.link.lost_frames
    stream = la.stream(rate_hz=rate, rle=False)
    n = 0
    for c in stream.chunks(duration_s=dur):
        n += 1
    st = stream.stats()
    print(f"RAW {rate/1e6:5.2f} MS/s {dur:.0f} s: 受信 {st.mb_per_s:6.2f} MB/s, 全 {st.bytes/1e6:.0f} MB, overrun={st.overrun}, lost_frames={s.link.lost_frames-lost0}, "
          f"欠けない {st.samples*st.sample_period_s:.2f}/{st.elapsed_s:.2f} s, チャンク {n}", flush=True)
print(f"PC python: cpu {time.process_time()-_c:.1f}s / wall {time.time()-_w:.1f}s")
