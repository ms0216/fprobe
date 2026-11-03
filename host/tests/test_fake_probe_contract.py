"""偽のデーモンの Probe（FakeProbe / DemoProbe）は、本物の Probe と同じ名前・同じ引数であること（caps_fakes.FakeDeviceBase と同じ考え方）。

偽物が本物の引数を受けないと、MCP や画面が本物の引数で呼んだとき TypeError になり、偽物の不足が「道具の失敗」に見える
（使いやすさの評価 2026-11-03: target_resume の core、peripheral_read の skip、svd_load の from_target、backtrace の
assume_leaf、disasm_* の data_ranges 等で、課題 4・7 の MCP の失敗の主因になった）。"""

import inspect

import pytest

import fake_demo
from fake_daemon import FakeProbe
from fake_probe_contract import NOT_IN_DEMO, NotInDemo, signature_problems
from fprobe.api.spec import load_spec
from fprobe.mcp_server import Probe


def _impls() -> set[str]:
    return {m.impl for m in load_spec().methods.values() if m.impl}


def test_every_method_the_demo_defines_takes_the_arguments_of_the_real_probe():
    assert signature_problems(fake_demo.DemoProbe) == []


def test_the_base_fake_takes_the_arguments_of_the_real_probe_too():
    # __init_subclass__ は派生だけを照らす。土台の FakeProbe 自身の定義も照らす
    assert signature_problems(FakeProbe) == []


def test_a_fake_whose_signature_differs_from_the_real_probe_cannot_be_created():
    with pytest.raises(TypeError, match=r"target_resume.*core"):
        class Bad(FakeProbe):
            def target_resume(self):                       # 本物は core を取る
                return None


def test_extra_arguments_of_a_fake_need_defaults():
    with pytest.raises(TypeError, match="extra"):
        class Bad(FakeProbe):
            def target_halt(self, core, extra):          # 既定の値が無い余分な引数は、本物の呼び出しで足りなくなる
                return None
    class Ok(FakeProbe):
        def target_halt(self, core=None, extra=None):
            return None


def test_every_api_method_is_implemented_by_the_demo_or_listed_as_not_in_demo():
    implemented = {n for n in _impls() if callable(getattr(fake_demo.DemoProbe, n, None))}
    assert _impls() - implemented == NOT_IN_DEMO, "API に足したメソッドは、偽の機器に実装するか NOT_IN_DEMO に書く"


def test_a_method_not_in_the_demo_says_so_instead_of_an_attribute_error():
    probe = fake_demo.DemoProbe()
    with pytest.raises(NotInDemo, match="log_read"):
        probe.log_read()


def test_the_methods_the_evaluation_hit_exist_in_the_demo():
    probe = fake_demo.DemoProbe()
    for name in ("run_until_halt", "disasm_text", "semihosting_status", "rtos_timeline_inversions", "pins_read"):
        assert name not in NOT_IN_DEMO
        assert callable(getattr(fake_demo.DemoProbe, name, None)), name


def test_not_in_demo_names_are_real_probe_methods():
    assert all(callable(getattr(Probe, n, None)) for n in NOT_IN_DEMO)
