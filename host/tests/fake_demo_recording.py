"""機器なしで画面を試すデモ（fake_daemon.py --demo）の一部: ピン（ロジアナ）の垂れ流しの記録（System の Record Pins）の状態。

DemoProbe（fake_demo.py）に混ぜる mixin。値の形は本物の Probe（probe_recording.py・probe_longrec.py・recording_stream.py）と同じ:
recording_status の la_stream は LaRecordingStream.status と同じ欄。

- 記録の開始・停止・状態は SearchDemo（fake_demo_search.py）の記録のまま。ここは recording.status の la_stream（ピンの流れの状態）だけを足す。
  記録の中に波形は入れない。
- ピンの流れの数（wire_bytes・changes・pad_bytes 等）は偽の数: 経った時間に比例して増やす（形式の差を見せるため。records は
  1 変化 4 バイト、tokens は 1 変化がふつう 1 バイトで 3 % が NOP の埋め）。本物の機器の性能を表すものではない。
- 形式の断りは本物と同じ文（DeepAnalyzer._check_rle_format）。デモの機器は既定でトークン形式を出せる（tokens_capable = False で
  出せない構成にできる）。
"""

import time
from types import SimpleNamespace

from fprobe.la_deep import DeepAnalyzer

__all__ = ["RecordingDemo"]

CHANGES_PER_S = 2_500_000                    # 偽の流れの変化点の数（毎秒）
RECORDS_BYTES, TOKENS_BYTES = 4.0, 1.0       # 1 変化の拍のバイト数（records は 4 B、tokens はふつう 1 B）
TOKENS_PAD = 0.03                            # tokens の NOP の埋めの割合（偽）


class _FakePinStream:
    def __init__(self, rle: bool, rle_format: str, started: float):
        self.rle, self.rle_format, self.started = rle, rle_format, started
        self.stopped: float | None = None

    def status(self) -> dict:
        elapsed = (self.stopped if self.stopped is not None else time.monotonic()) - self.started
        changes = int(elapsed * CHANGES_PER_S)
        per = TOKENS_BYTES if self.rle_format == "tokens" else RECORDS_BYTES
        wire = int(changes * per) // 16 * 16
        out = {"running": self.stopped is None, "blocks": int(elapsed), "error": None, "events": None,
               "samples": int(elapsed * 100e6), "duration_s": elapsed, "mb_per_s": wire / elapsed / 1e6 if elapsed else 0.0,
               "overrun": False, "sample_period_ns": 10.0, "link_buffer": None, "gaps": 0, "gap_samples": 0, "unplaced_beats": 0,
               "rle_format": self.rle_format if self.rle else "raw", "wire_bytes": wire,
               "pad_bytes": int(wire * TOKENS_PAD) if self.rle_format == "tokens" else 0,
               # records は生の拍で保存するので復号して数えない（本物と同じ）。tokens は受け取りで復号するので数える
               "changes": changes if self.rle_format == "tokens" else None}
        out["bytes_per_change"] = wire / changes if out["changes"] else None
        return out


class RecordingDemo:
    tokens_capable = True

    def _init_recording(self) -> None:
        self._pins: _FakePinStream | None = None

    def probe_info(self):
        info = super().probe_info()
        info["capabilities"]["la_rle_tokens"] = self.tokens_capable
        return info

    def recording_stop(self):
        self._stop_pins()                                          # 本物と同じ: 流し続けの残りを入れてから閉じる
        return super().recording_stop()

    def _stop_pins(self) -> None:
        if self._pins is not None and self._pins.stopped is None:
            self._pins.stopped = time.monotonic()
        self._pins = None
        self._elf._la_record = None                                # recording.status の la_stream（本物の Probe が読む）

    def recording_la_stream_start(self, rate_hz=None, rle=True, chunk_s=1.0, pack_channels=None, gaps=False,
                                  gap_hold_bytes=None, control_read_retries=0, rle_format=None, tick_us=None, rle_repeat=None, max_changes=None):
        if self._elf._recorder is None:
            raise RuntimeError("記録していない（recording_start を先に）")
        if self._pins is not None:
            raise RuntimeError("ロジアナを記録へ流している（recording_la_stream_stop を先に）")
        from fprobe import la_tokens as T
        from fprobe.caps import TokenInfo
        caps = SimpleNamespace(la_rle_tokens=self.tokens_capable, la_rle_token_info=TokenInfo(T.TOKEN_FORMAT_VERSION, False, False, 16))
        fmt = DeepAnalyzer._check_rle_format(SimpleNamespace(caps=caps), rle, rle_format, pack_channels, tick_us, rle_repeat)   # 本物と同じ断り
        self._pins = _FakePinStream(rle, fmt, time.monotonic())
        self._elf._la_record = self._pins
        return self._pins.status()

    def recording_la_stream_stop(self):
        if self._pins is None:
            raise RuntimeError("流していない（recording_la_stream_start を先に）")
        self._pins.stopped = time.monotonic()
        status = self._pins.status()
        self._pins = None
        self._elf._la_record = None
        return status
