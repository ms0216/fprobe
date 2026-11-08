"""ロジアナの垂れ流し RLE のトークン形式（rle_format = "tokens"）: 偽のデバイスで流し、チャンクのつなぎ・VCD・統計・API の応答・
断る条件を確かめる。符号は fprobe.la_tokens（test_la_tokens.py）。仕様: docs/superpowers/specs/2026-11-07-la-rle2-design.md §7。"""

import random
from array import array
from types import SimpleNamespace

import pytest

from fprobe import generated as g
from fprobe import la_tokens as T
from fprobe.la import DeepAnalyzer, RawCapture, Transitions, write_vcd
from fprobe.la_tokens import CONTROL_RLE_FMT_BIT
from fprobe.caps import TokenInfo
from fprobe.mcp_server import Probe
from caps_fakes import caps_for
from test_la_stream import FakeStreamDevice
from test_la_tokens import clock, random_signal, runs_of


class TokenDevice(FakeStreamDevice):
    """トークン形式を申告する偽のデバイス（実デバイスの申告は段階 2）。"""

    def capabilities(self):
        return caps_for(mem_bytes=128 << 20, la_channels=16, la_sample_hz=100_000_000, la_rle_tokens=True,
                         la_rle_token_info=TokenInfo(T.TOKEN_FORMAT_VERSION, False, False, 16))


class NewerTokenDevice(TokenDevice):
    """このホストの知らない版のトークン形式を申告する偽のデバイス。"""

    def capabilities(self):
        return caps_for(mem_bytes=128 << 20, la_channels=16, la_sample_hz=100_000_000, la_rle_tokens=True,
                        la_rle_token_info=TokenInfo(T.TOKEN_FORMAT_VERSION + 1, False, False, 16))


class RepeatTokenDevice(TokenDevice):
    """トークン形式の REPEAT の回路（TLV LA_RLE_TOKEN_INFO の repeat）を申告する偽のデバイス。"""

    def capabilities(self):
        return caps_for(mem_bytes=128 << 20, la_channels=16, la_sample_hz=100_000_000, la_rle_tokens=True,
                        la_rle_token_info=TokenInfo(T.TOKEN_FORMAT_VERSION, True, False, 16))


@pytest.fixture(autouse=True)
def fast_reads(monkeypatch):
    monkeypatch.setattr("fprobe.la.LaStream.READ_MIN", 4096)
    monkeypatch.setattr("fprobe.la.LaStream.READ_MAX", 3000)            # 拍の途中で切れる長さ


def started(dev) -> int:
    """開始の書き（run = 1）の la_deep.control の値。"""
    return next(v for v in dev.control_writes if v & 1)


def token_source(kind, seed, n=60000, **kw):
    rng = random.Random(seed)
    samples = random_signal(rng, n, kind) if kind != "steady" else clock(n, 4)
    data = T.encode_tokens(samples, **kw)
    return rng, samples, data


@pytest.mark.parametrize("kind, repeat", [("hold", False), ("clock_slow", False), ("every", False), ("sparse", False),
                                          ("steady", True), ("hold", True)])
def test_chunks_chain_to_the_whole_transitions(kind, repeat):
    rng, samples, source = token_source(kind, 3, repeat=repeat)
    dev = TokenDevice(source, rng=rng, per_poll=40)
    stream = DeepAnalyzer(dev).stream(rle=True, rle_format="tokens", tick_us=0)
    chunks = list(stream.chunks(0.1))
    got = source[:dev.written * 16]
    assert dev.written >= 1 and dev.received == len(got)          # 出し切るまで受け取った
    if len(source) > 8000:
        assert len(chunks) > 2                                     # 何回かに分かれて届く
    start = started(dev)
    assert start >> CONTROL_RLE_FMT_BIT & 1 and start >> g.LA_DEEP_CONTROL_RLE[0] & 1
    assert all(c.rle_format == "tokens" and len(c.data) % 16 == 0 for c in chunks)
    values, starts = array("H"), array("Q")
    for c in chunks:
        v, s = c.transitions()
        values += v
        starts += s
    whole = T.decode_tokens(got)
    assert (list(values), list(starts)) == ([int(x) for x in whole.values], [int(x) for x in whole.starts])
    # チャンクの位置のつなぎ: first = 前までの時刻の進みの和
    assert all(b.first == a.first + a.length for a, b in zip(chunks, chunks[1:]))
    stats = stream.stats()
    assert (stats.rle_format, stats.wire_bytes, stats.bytes, stats.overrun) == ("tokens", len(got), len(got), False)
    assert stats.pad_bytes == whole.pad_bytes and stats.samples == whole.end_time + 1
    assert stats.changes is None and stats.bytes_per_change is None          # 変化点の数は VCD への書き出しで数える


def test_stream_to_vcd_counts_changes_and_matches_write_vcd(tmp_path):
    rng, samples, source = token_source("clock_slow", 5, n=80000, repeat=True)
    dev = TokenDevice(source, rng=rng, per_poll=100)
    stats = DeepAnalyzer(dev).stream_to_vcd(tmp_path / "s.vcd", 0.05, rle=True, rle_format="tokens", tick_us=0)
    got = source[:dev.written * 16]
    d = T.decode_tokens(got)
    assert stats.changes == len(d.values) and stats.wire_bytes == len(got)
    assert stats.bytes_per_change == pytest.approx(len(got) / len(d.values)) and stats.pad_bytes == d.pad_bytes
    write_vcd(Transitions(array("H", d.values), array("Q", d.starts), d.end_time + 1, 0, 0, stats.sample_period_s, 16),
              tmp_path / "w.vcd")
    changes = lambda path: [x for x in path.read_text().splitlines() if x.startswith("#") or x[:1] in "01"]   # noqa: E731
    trigger_id = chr(33 + 16)
    want = [x for x in changes(tmp_path / "w.vcd") if x[1:] != trigger_id]
    want = [x for i, x in enumerate(want) if not (x.startswith("#") and (i + 1 == len(want) or want[i + 1].startswith("#")))]
    assert changes(tmp_path / "s.vcd") == want


def test_raw_file_keeps_the_format_and_exports_the_same_vcd(tmp_path):
    rng, samples, source = token_source("hold", 9, n=40000)
    dev = TokenDevice(source, rng=rng, per_poll=100)
    stats = DeepAnalyzer(dev).stream_to_vcd(tmp_path / "s.vcd", 0.05, rle=True, rle_format="tokens", tick_us=0, raw_path=tmp_path / "s.fplaraw")
    raw = RawCapture(tmp_path / "s.fplaraw")
    assert raw.meta.rle and raw.meta.rle_format == "tokens" and raw.samples == stats.samples
    from fprobe.la import raw_to_vcd
    assert raw_to_vcd(raw, tmp_path / "again.vcd") == stats.changes
    assert (tmp_path / "again.vcd").read_bytes() == (tmp_path / "s.vcd").read_bytes()


def test_vcd_is_written_in_pieces_with_the_same_bytes_and_refuses_beyond_max_changes(tmp_path, monkeypatch):
    # REPEAT は小さな受け取りが大量の変化点になる。VCD は断片ごとに復号して書く（バイト列は同じ）。上限（使う側が明示）を超えるなら、
    # 上限ちょうどまでを書いて理由つきで断る
    from fprobe.la import raw_to_vcd
    from fprobe.la_wave import ChangeLimitExceeded
    rng, samples, source = token_source("clock_slow", 5, n=80000, repeat=True)
    dev = TokenDevice(source, rng=rng, per_poll=100)
    stats = DeepAnalyzer(dev).stream_to_vcd(tmp_path / "s.vcd", 0.05, rle=True, rle_format="tokens", tick_us=0, raw_path=tmp_path / "s.fplaraw")
    assert T.count_repeats(source[:dev.written * 16]) > 0
    monkeypatch.setattr("fprobe.la_wave.PIECE_CHANGES", 7)
    assert raw_to_vcd(RawCapture(tmp_path / "s.fplaraw"), tmp_path / "p.vcd") == stats.changes
    assert (tmp_path / "p.vcd").read_bytes() == (tmp_path / "s.vcd").read_bytes()
    assert raw_to_vcd(RawCapture(tmp_path / "s.fplaraw"), tmp_path / "ok.vcd", max_changes=stats.changes) == stats.changes   # ちょうどは通る
    with pytest.raises(ChangeLimitExceeded) as e:
        raw_to_vcd(RawCapture(tmp_path / "s.fplaraw"), tmp_path / "cut.vcd", max_changes=1000)
    assert e.value.limit == 1000 and e.value.written == 1000 and "max_changes = 1000" in str(e.value)
    lines = (tmp_path / "cut.vcd").read_text().splitlines()
    assert 0 < len([x for x in lines if x[:1] in "01"]) <= 1000 * 16        # 打ち切った所までの VCD（閉じてある）


def test_la_stream_tool_refuses_beyond_max_changes_and_keeps_the_raw_file(tmp_path, monkeypatch):
    from fprobe.la_wave import ChangeLimitExceeded
    monkeypatch.setattr("fprobe.la.time.sleep", lambda s: None)
    rng, samples, source = token_source("clock_slow", 1, n=50000, repeat=True)
    dev = TokenDevice(source, rng=rng, per_poll=100)
    p = _probe(dev, tmp_path, monkeypatch)
    with pytest.raises(ChangeLimitExceeded, match="max_changes = 500"):
        p.la_stream(str(tmp_path / "t.vcd"), duration_s=0.05, rle=True, rle_format="tokens", tick_us=0,
                    raw_path=str(tmp_path / "t.fplaraw"), max_changes=500)
    again = p.la_raw_to_vcd(str(tmp_path / "t.fplaraw"), str(tmp_path / "u.vcd"))     # 生の保存ファイルは残る。上限なしで書き直せる
    assert again["rle_format"] == "tokens" and again["samples"] > 0


def test_records_stream_reports_its_format_and_bytes_per_change(tmp_path):
    rng = random.Random(2)
    from test_la_stream import rle_source
    dev = FakeStreamDevice(rle_source(rng, 3000), rng=rng, per_poll=100)
    stats = DeepAnalyzer(dev).stream_to_vcd(tmp_path / "r.vcd", 0.05, rle=True)
    assert (stats.rle_format, stats.pad_bytes) == ("records", 0) and stats.wire_bytes == dev.received
    assert stats.bytes_per_change == pytest.approx(stats.wire_bytes / stats.changes)
    assert not started(dev) >> CONTROL_RLE_FMT_BIT & 1                       # 既定は今までどおり（レコード）
    raw_stats = DeepAnalyzer(FakeStreamDevice(b"\0" * 16 * 100, rng=rng)).stream_to_vcd(tmp_path / "w.vcd", 0.02, rle=False)
    assert raw_stats.rle_format == "raw"


def test_refuses_instead_of_silently_using_records(tmp_path):
    rng = random.Random(1)
    old = FakeStreamDevice(b"\0" * 16 * 100, rng=rng)                                # la_rle_tokens を申告しない
    with pytest.raises(RuntimeError, match="la_rle_tokens") as e:
        DeepAnalyzer(old).stream(rle=True, rle_format="tokens", tick_us=1000)
    assert "確かめ方" in str(e.value) and not any(v & 1 for v in old.control_writes)
    dev = TokenDevice(b"\0" * 16 * 100, rng=rng)
    with pytest.raises(ValueError, match="rle = True のときだけ"):
        DeepAnalyzer(dev).stream(rle=False, rle_format="tokens", tick_us=0)
    with pytest.raises(ValueError, match="rle_format は"):
        DeepAnalyzer(dev).stream(rle=True, rle_format="token")
    with pytest.raises(ValueError, match="tick_us を明示"):
        DeepAnalyzer(dev).stream(rle=True, rle_format="tokens")                      # tick_us の既定は無い
    with pytest.raises(ValueError, match="tokens）のときだけ"):
        DeepAnalyzer(dev).stream(rle=True, tick_us=1000)                              # records に tick_us は無い
    with pytest.raises(ValueError, match="詰める"):
        DeepAnalyzer(dev).stream(rle=True, rle_format="tokens", tick_us=0, pack_channels=8)
    with pytest.raises(ValueError, match="0 以上"):
        DeepAnalyzer(dev).stream(rle=True, rle_format="tokens", tick_us=-1)
    with pytest.raises(ValueError, match="より短い"):
        DeepAnalyzer(dev).stream(rle=True, rle_format="tokens", tick_us=0.001)       # 1 サンプル（10 ns）に満たない
    unknown = NewerTokenDevice(bytes(16 * 100), rng=rng)
    with pytest.raises(RuntimeError, match="版"):
        DeepAnalyzer(unknown).stream(rle=True, rle_format="tokens", tick_us=0)
    assert not any(v & 1 for v in dev.control_writes)
    DeepAnalyzer(old).stream(rle=True, rle_format="records")                         # records は明示してもよい
    assert any(v & 1 for v in old.control_writes)


def test_rle_repeat_is_chosen_by_the_caller_and_refused_when_unsupported():
    """REPEAT は使う側が明示する（既定・None・False は使わない）。回路が申告しない・トークン形式でないときは断る（黙って使わない）。"""
    rng = random.Random(4)
    rep = RepeatTokenDevice(bytes(16 * 100), rng=rng)
    for arg in (None, False):
        DeepAnalyzer(rep).stream(rle=True, rle_format="tokens", tick_us=0, rle_repeat=arg)
    assert not any(v & 1 and v >> g.LA_DEEP_CONTROL_RLE_REPEAT[0] & 1 for v in rep.control_writes)
    rep = RepeatTokenDevice(bytes(16 * 100), rng=rng)
    DeepAnalyzer(rep).stream(rle=True, rle_format="tokens", tick_us=0, rle_repeat=True)
    start = started(rep)
    assert start >> g.LA_DEEP_CONTROL_RLE_REPEAT[0] & 1 and start >> CONTROL_RLE_FMT_BIT & 1
    plain = TokenDevice(bytes(16 * 100), rng=rng)                                  # repeat を申告しない構成
    with pytest.raises(RuntimeError, match="REPEAT"):
        DeepAnalyzer(plain).stream(rle=True, rle_format="tokens", tick_us=0, rle_repeat=True)
    assert not any(v & 1 for v in plain.control_writes)
    DeepAnalyzer(plain).stream(rle=True, rle_format="tokens", tick_us=0, rle_repeat=False)   # False は明示してよい
    with pytest.raises(ValueError, match="rle_repeat はトークン形式"):
        DeepAnalyzer(rep).stream(rle=True, rle_repeat=True)                          # records に rle_repeat は無い
    with pytest.raises(ValueError, match="rle_repeat はトークン形式"):
        DeepAnalyzer(rep).stream(rle=True, rle_format="records", rle_repeat=False)


def _probe(dev, tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    p = Probe("unused")
    p._session = SimpleNamespace(device=dev)
    return p


def test_la_stream_tool_returns_the_format_facts(tmp_path, monkeypatch):
    """Probe の la_stream（API la.stream。応答は tests/api_result_check.py が仕様の result と照合する）。"""
    monkeypatch.setattr("fprobe.la.time.sleep", lambda s: None)
    rng, samples, source = token_source("clock_slow", 1, n=50000, repeat=True)
    dev = TokenDevice(source, rng=rng, per_poll=100)
    p = _probe(dev, tmp_path, monkeypatch)
    got = p.la_stream(str(tmp_path / "t.vcd"), duration_s=0.05, rle=True, rle_format="tokens", tick_us=0, raw_path=str(tmp_path / "t.fplaraw"))
    assert got["rle_format"] == "tokens" and got["changes"] > 0 and got["wire_bytes"] == dev.received
    assert got["bytes_per_change"] == pytest.approx(got["wire_bytes"] / got["changes"])
    assert got["pad_bytes"] >= 0 and got["samples"] > 0
    again = p.la_raw_to_vcd(str(tmp_path / "t.fplaraw"), str(tmp_path / "u.vcd"))
    assert again["rle_format"] == "tokens" and again["samples"] == got["samples"]
    with pytest.raises(RuntimeError, match="la_rle_tokens"):
        _probe(FakeStreamDevice(b"\0" * 16 * 100, rng=rng), tmp_path, monkeypatch).la_stream(
            str(tmp_path / "x.vcd"), duration_s=0.05, rle_format="tokens", tick_us=0)


def test_decode_helpers_accept_the_format():
    samples = clock(5000, 4)
    data = T.encode_tokens(samples, repeat=True)
    from fprobe.la_wave import decode_beats, decode_transitions
    values, starts, length = decode_transitions(data, True, rle_format="tokens")
    assert (list(values), list(starts)) == runs_of(samples) and length == starts[-1] + 1
    assert list(decode_beats(data, True, "tokens")) == samples[:length]


def test_tick_us_is_written_in_samples_before_the_run_starts():
    """tick_us（マイクロ秒）→ CSR la_deep.tick（サンプル数 = tick_us / サンプルの周期）。開始（control の run = 1）より前に書く。"""
    rng = random.Random(2)
    dev = TokenDevice(b"\0" * 16 * 100, rng=rng)
    writes = []
    orig = dev.write32
    dev.write32 = lambda addr, value: (writes.append((addr, value)), orig(addr, value))[1]
    DeepAnalyzer(dev).stream(rle=True, rle_format="tokens", tick_us=1000)
    addrs = [a for a, _ in writes]
    assert (g.LA_DEEP_TICK, 100_000) in writes                                       # 1000 us / 10 ns
    assert addrs.index(g.LA_DEEP_TICK) < max(i for i, a in enumerate(addrs) if a == g.LA_DEEP_CONTROL)
    writes.clear()
    DeepAnalyzer(dev).stream(rle=True, rle_format="tokens", tick_us=0)
    assert (g.LA_DEEP_TICK, 0) in writes
    writes.clear()
    DeepAnalyzer(dev).stream(rle=True)                                                # レコード形式は tick を書かない
    assert g.LA_DEEP_TICK not in [a for a, _ in writes]
