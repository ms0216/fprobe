"""応答の型 Basis（fprobe.timing の各関数の basis）は形を決める: 関数ごとの欄を書き、知らない欄は断る。

前は「何でもよい値」（$defs の Json の表）で、欄の打ち違い・欄を足したのに仕様を直していないことが照合で見つからなかった。
"""

import json

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from fprobe import timing
from fprobe.api.spec import SPEC_PATH

SPEC = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
REGISTRY = Registry().with_resource("urn:spec", Resource.from_contents({**SPEC, "$schema":
                                                                        "https://json-schema.org/draft/2020-12/schema"}))
BASIS = Draft202012Validator({"$ref": "urn:spec#/$defs/Basis"}, registry=REGISTRY)


def _cfg(jtag: bool) -> timing.DapConfig:
    return timing.DapConfig(time=0, half_period=2, idle_cycles=0, turnaround=1, data_phase=False, swd_sample_delay=0,
                            jtag_sample_delay=0, jtag=jtag, enable=True, dr_before=0, dr_after=0, ir_before=0,
                            ir_after=0, ir_len=4)


CASES = {
    "stream_start_recorded": timing.stream_start_recorded(1000, 3, 1, 990, 1010)["basis"],
    "stream_start_recorded_without_reads": timing.stream_start_recorded(1000, 3, 1)["basis"],
    "stream_start_lanes": timing.stream_start_lanes(1000, 4, 7, 990, 1010)["basis"],
    "stream_start_lanes_in_anchor": {**timing.stream_start_lanes(1000, 4, 7, 990, 1010)["basis"], "fine_sample": 3993},
    "stream_start_window": timing.stream_start_window(100, 200, None, 3, 1)["basis"],
    "stream_start_window_trigger": timing.stream_start_window(100, 200, 150, 3, 1)["basis"],
    "stream_start_window_in_anchor": {**timing.stream_start_window(100, 200, None, 3, 1)["basis"], "start_valid": False},
    "posted_read_window": timing.posted_read_window(10, 20, 5)["basis"],
    "posted_read_window_without_before": timing.posted_read_window(10, 20, None)["basis"],
    "sampler_window_swd": timing.sampler_window(100, 200, _cfg(False), 2, 1)["basis"],
    "sampler_window_jtag": timing.sampler_window(100, 200, _cfg(True), 0, 1)["basis"],
    "sampler_window_unknown": timing.sampler_window(100, None, None, None, None)["basis"],
    "posted_read_window_line": timing.posted_read_window_line(100, 200, _cfg(False), 50, 1)["basis"],
    "posted_read_window_line_unknown": timing.posted_read_window_line(100, 200, None, None, None)["basis"],
}


@pytest.mark.parametrize("name", CASES)
def test_every_basis_the_timing_functions_return_fits_the_schema(name):
    errors = list(BASIS.iter_errors(CASES[name]))
    assert not errors, [e.message for e in errors]


@pytest.mark.parametrize("bad", [
    {**CASES["posted_read_window"], "typo": 1},                         # 知らない欄
    {**CASES["stream_start_window"], "bounds": {"t_lo": "before", "t_hi": "elsewhere"}},   # 決まった名前の外
    {**CASES["sampler_window_swd"], "waits": "2"},                      # 型が違う
    {},
])
def test_the_schema_refuses_a_shape_no_function_returns(bad):
    assert list(BASIS.iter_errors(bad))


def test_a_data_gap_error_is_the_collectors_text():
    """DataGap.error（reason events_error）は Collector.last_error の文（"型: 文"）。古い記録で無ければ null。"""
    gap = Draft202012Validator({"$ref": "urn:spec#/$defs/DataGap"}, registry=REGISTRY)
    base = {"reason": "events_error", "t0": 1, "t1": 1}
    assert not list(gap.iter_errors({**base, "error": "TimeoutError: 応答が届かない"}))
    assert not list(gap.iter_errors({**base, "error": None}))
    assert list(gap.iter_errors({**base, "error": {"nested": 1}}))
