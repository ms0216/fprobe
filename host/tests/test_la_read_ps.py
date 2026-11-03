"""トリガ取り込みの読み出し（深いバッファ全量）を、PS が DDR から直接読んで TCP で送る形で頼む判断（偽のデバイス。ハードウェア不要）。
仕様: docs/superpowers/specs/2026-10-30-la-stream-ps-direct-design.md。書き込みが止まったロジアナの取り込みだけ（イベントは書き込み中のことがあり、
応答の来ていない書きを PS が読む恐れがあるので、今まで通り）。"""

from fprobe import generated as g
from fprobe.la import DeepAnalyzer
from test_la_time import FakeDevice

READ = 1 << g.LA_DEEP_COMMAND_READ[0]
READ_PS = 1 << g.LA_DEEP_COMMAND_READ_PS[0]


class Dev(FakeDevice):
    def __init__(self, *, tcp, **caps):
        super().__init__(mem_bytes=1 << 20, **caps)
        if tcp is not None:
            self.stream_tcp_open = tcp
        self.commands = []

    def write32(self, address, value):
        if address == g.LA_DEEP_COMMAND:
            self.commands.append(value)


def command_used(*, la_stream_ps, tcp):
    dev = Dev(tcp=tcp, la_stream_ps=la_stream_ps, la_stream_events=la_stream_ps)
    DeepAnalyzer(dev).read()
    assert len(dev.commands) == 1
    return dev.commands[0]


def test_the_ps_reads_the_capture_when_the_device_can_and_the_tcp_stream_is_open():
    assert command_used(la_stream_ps=True, tcp=True) == READ_PS


def test_without_the_tcp_stream_the_window_path_is_used():
    # TCP が無いと、PS が UDP で一気に送る形になり、欠けても気づけない（31 MB を毎秒 1 万数千個のパケットで）。窓経由のまま
    assert command_used(la_stream_ps=True, tcp=False) == READ
    assert command_used(la_stream_ps=True, tcp=None) == READ                      # TCP の項目が無い Device


def test_a_device_without_the_capability_uses_the_window_path():
    assert command_used(la_stream_ps=False, tcp=True) == READ
