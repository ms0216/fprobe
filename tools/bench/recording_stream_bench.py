"""記録の受け取り（LaRecordingStream の受け取りのスレッドの本体 _run）の速さを、変化点で保存する道（形式 1。変更前の道）と、
生の拍で保存する道（形式 2。記録の形式の版 3）で測る（試験の計測用。本体ではない）。

ネットワークも機器も使わない: la_vcd_bench の合成の流れ（拍の列）を StreamChunk に切り、偽のストリームとして _run に渡す。記録は
一時ディレクトリの実ファイル（Recorder。ディスクへの書き込みを含む）。測るのは _run が流れを消化し終えるまでの時間（受け取りの
スレッドが流れに追いつける速さ）。記録を閉じる時間・読む時間は含まない。出す数: 入力（線の拍）の MB/s・実時間比（800 MS/s として。
1 以上なら流しに追いつく）。

    python tools/bench/recording_stream_bench.py [--mb 64] [--chunk-mb 4] [--blocks 4] [--only 名前,...] [--repeat 2]
"""

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, os.environ.get("FPROBE_HOST") or str(Path(__file__).resolve().parents[2] / "host"))

import la_vcd_bench as base                                    # noqa: E402
from fprobe.recording import Recorder                          # noqa: E402
from fprobe.recording_stream import LaRecordingStream          # noqa: E402

HZ = 1e9


class FakeStream:
    def __init__(self, chunks, rle, pack, channels):
        self._chunks = chunks
        self.rle, self.pack, self.channels, self.sample_period_s = rle, pack, channels, base.PERIOD_S
        self.carries_events = False

    def chunks(self, _duration):
        yield from self._chunks

    def stats(self):
        return SimpleNamespace(samples=0, overrun=False, gaps=(), gap_samples=0, unplaced_beats=0, link_buffer_dict=None)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mb", type=float, default=64)
    ap.add_argument("--chunk-mb", type=float, default=4)
    ap.add_argument("--blocks", type=int, default=4, help="記録のかたまり（chunk_s）の数の目安")
    ap.add_argument("--only", default="")
    ap.add_argument("--repeat", type=int, default=2)
    args = ap.parse_args()
    has_raw = hasattr(Recorder, "add_raw_waveform")
    original = Recorder.__dict__.get("waveform_format")
    total = int(args.mb * 1e6) // 16 * 16
    chunk = int(args.chunk_mb * 1e6) // 16 * 16
    wanted = set(filter(None, args.only.split(",")))
    print(f"{'場面':<18} {'道':<12} {'入力 MB/s':>10} {'実時間比':>9} {'秒':>7} {'かたまり':>8}")
    with tempfile.TemporaryDirectory() as tmp:
        for name, data, rle, pack, channels in base.scenarios(total):
            if wanted and name not in wanted:
                continue
            chunks = base.chunks_of(data, rle, pack, chunk)
            samples = sum(c.length for c in chunks)
            per_block_s = samples / args.blocks * base.PERIOD_S / HZ * HZ     # サンプル数 / blocks 個ぶんの秒
            # 変更前の版（git archive の版。FPROBE_HOST）には生の拍の道が無く、変化点の道だけを測る
            for label, legacy in (("transitions", True), ("raw", False)):
                if not legacy and not has_raw:
                    continue
                best = None
                if has_raw:
                    Recorder.waveform_format = property(lambda self: 1) if legacy else original
                for k in range(args.repeat):
                    result = _run(Path(tmp) / f"{name}-{label}-{k}", chunks, rle, pack, channels, per_block_s)
                    best = min(best, result) if best else result
                dt, blocks = best
                real = samples * base.PERIOD_S
                print(f"{name:<18} {label:<12} {len(data) / 1e6 / dt:>10.1f} {real / dt:>9.2f} {dt:>7.3f} {blocks:>8}")
            sys.stdout.flush()


def _run(directory, chunks, rle, pack, channels, per_block_s):
    rec = Recorder(directory, HZ)
    s = LaRecordingStream(SimpleNamespace(), rec, rle=rle, chunk_s=per_block_s)
    s.stream = FakeStream(chunks, rle, pack, channels)
    s.anchor = SimpleNamespace(ticks=0, uncertainty=0, method="circuit", t_lo=0, t_hi=0, basis=None)
    s._skew = ()
    t0 = time.perf_counter()
    s._run()
    dt = time.perf_counter() - t0
    assert s.error is None, s.error
    rec.close()
    import shutil
    shutil.rmtree(directory, ignore_errors=True)
    return dt, s.blocks


if __name__ == "__main__":
    main()
