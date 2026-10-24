"""la_collect のチャンネルごとの要約（fprobe.mcp_server._channel_summary）: 切ったこと・省いたことを結果に出す。"""

from types import SimpleNamespace

from fprobe.mcp_server import _channel_summary


def capture(*channels: str, trigger: int = 0):
    bits = [[int(c) for c in ch] for ch in channels]
    return SimpleNamespace(sample_period_s=1e-8, channels=len(bits), trigger=trigger, channel=lambda ch: bits[ch])


def test_first_edges_say_when_they_are_cut_and_quiet_channels_are_listed():
    busy = "01" * 20                                             # 39 回の変化
    out = _channel_summary(capture(busy, "0" * 40, "1" * 40))
    ch0 = out["channels"][0]
    assert ch0["edges"] == 39 and len(ch0["first_edges_ns"]) == 16 and ch0["first_edges_truncated"]
    assert out["first_edges_limit"] == 16
    assert 1 not in out["channels"] and out["constant_low"] == [1]             # 常に 0 は省き、どれを省いたかを返す
    assert out["channels"][2]["edges"] == 0 and not out["channels"][2]["first_edges_truncated"]
