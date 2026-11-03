"""Probe を領域ごとのサービスに分ける基盤（docs/superpowers/specs/2026-11-03-probe-services-design.md）。"""

import inspect

import pytest

from fprobe.api.spec import load_spec
from fprobe.probe import Probe
from fprobe.probe_service import ReleaseSteps, delegate, expose


class _Counter:
    """試験用のサービス。"""

    API = ("bump", "value")

    def __init__(self, start: int):
        self._n = start

    def bump(self, by: int = 1) -> int:
        """数を by だけ増やす。"""
        self._n += by
        return self._n

    def value(self) -> int:
        return self._n

    def _hidden(self) -> None:
        pass


# --- 委譲 ------------------------------------------------------------------------------------------------

def test_delegate_from_the_class_gives_the_service_function_with_its_signature_and_doc():
    @expose("_counter", _Counter)
    class Host:
        def __init__(self):
            self._counter = _Counter(10)

    assert Host.bump is _Counter.bump
    assert Host.bump.__doc__ == "数を by だけ増やす。"
    assert list(inspect.signature(Host.bump).parameters) == ["self", "by"]


def test_delegate_from_an_instance_calls_that_instances_service():
    @expose("_counter", _Counter)
    class Host:
        def __init__(self, start):
            self._counter = _Counter(start)

    a, b = Host(1), Host(100)
    assert a.bump(2) == 3 and b.bump() == 101
    assert a.value() == 3 and b.value() == 101
    assert list(inspect.signature(a.bump).parameters) == ["by"]


def test_only_the_names_the_service_lists_are_exposed():
    @expose("_counter", _Counter)
    class Host:
        pass

    assert not hasattr(Host, "_hidden")


def test_expose_refuses_a_name_the_class_already_has():
    class Base:
        def value(self):
            return "base"

    with pytest.raises(TypeError, match="value"):
        @expose("_counter", _Counter)
        class Host(Base):
            pass


def test_expose_refuses_a_name_listed_but_not_defined():
    class Broken:
        API = ("missing",)

    with pytest.raises(TypeError, match="missing"):
        @expose("_broken", Broken)
        class Host:
            pass


def test_delegate_is_a_descriptor_that_names_its_service():
    d = delegate("_counter", _Counter, "bump")
    assert (d.attr, d.service, d.name) == ("_counter", _Counter, "bump")


# --- 後始末の登録 ------------------------------------------------------------------------------------------

def test_release_steps_run_in_registered_order_and_collect_failures():
    calls = []
    steps = ReleaseSteps()
    steps.add("a", lambda: calls.append("a"))
    steps.add("b", lambda: (calls.append("b"), 1 / 0))
    steps.add("c", lambda: calls.append("c"))
    assert steps.run() == [{"step": "b", "error": "ZeroDivisionError: division by zero"}]
    assert calls == ["a", "b", "c"]
    assert steps.names() == ["a", "b", "c"]


def test_release_steps_refuse_a_duplicate_name():
    steps = ReleaseSteps()
    steps.add("a", lambda: None)
    with pytest.raises(ValueError, match="a"):
        steps.add("a", lambda: None)


# --- Probe ----------------------------------------------------------------------------------------------

def test_every_api_impl_resolves_to_a_callable_on_probe():
    probe = Probe("unused")
    for m in load_spec().methods.values():
        if m.impl:
            assert callable(getattr(probe, m.impl)), m.name


def test_no_two_tools_define_the_same_name():
    """mixin どうし・mixin とサービスの委譲の名前が重ならない（MRO で黙って片方が隠れない）。"""
    seen: dict[str, str] = {}
    for base in Probe.__mro__[1:]:
        if base is object:
            continue
        for name in vars(base):
            if name.startswith("__"):
                continue
            assert name not in seen, (name, seen.get(name), base.__name__)
            seen[name] = base.__name__
    for name, value in vars(Probe).items():
        if isinstance(value, delegate):
            assert name not in seen, (name, seen[name])


def test_release_device_runs_the_registered_steps_in_order():
    probe = Probe("unused")
    assert probe._release_steps.names()[:6] == ["la_record", "streams", "formats", "observing", "testpoint",
                                                "linux_halt"]


# --- 委譲の書き込み・共有の名前・ホストの見え方 ------------------------------------------------------------

class _Shared:
    API = ("value",)
    SHARED = ("_state", "_bump")

    def __init__(self):
        self._state = 1

    def value(self):
        return self._state

    def _bump(self):
        self._state += 1


def test_shared_names_are_delegated_for_reading_and_writing():
    @expose("_svc", _Shared)
    class Host:
        def __init__(self):
            self._svc = _Shared()

    h = Host()
    assert h._state == 1
    h._state = 5                                   # 書くとサービスの状態が変わる（ほかの領域が書く状態）
    assert h._svc._state == 5 and h.value() == 5
    h._bump()
    assert h.value() == 6


def test_replacing_a_delegated_method_on_the_host_replaces_it_in_the_service():
    """試験・プラグインが Probe のメソッドを差し替えると、サービスの中からの呼び出しにも効く（mixin のときと同じ）。"""
    @expose("_svc", _Shared)
    class Host:
        def __init__(self):
            self._svc = _Shared()

    h = Host()
    h.value = lambda: "patched"
    assert h._svc.value() == "patched"
    del h.value
    assert h.value() == 1


def test_a_host_view_lets_a_service_reach_only_what_it_declares():
    from types import SimpleNamespace

    from fprobe.probe_service import HostView
    host = SimpleNamespace(a=1, b=2)
    view = HostView(host, ("a",), "Svc")
    assert view.a == 1
    view.a = 3
    assert host.a == 3
    host.a = 4                                     # 引くたびにホストを見る（差し替えが効く）
    assert view.a == 4
    with pytest.raises(AttributeError, match="Svc.*b"):
        view.b
    with pytest.raises(AttributeError, match="Svc.*b"):
        view.b = 1


# --- Probe の全サービス: 依存の宣言と、委ねた名前の実在 -------------------------------------------------------

def _services():
    probe = Probe("unused")
    out = {}
    for name, value in vars(Probe).items():
        if isinstance(value, delegate):
            out[value.attr] = (value.service, getattr(probe, value.attr))
    return probe, out


def test_every_host_name_a_service_uses_is_declared_in_its_needs():
    """サービスの中の self._host.<名前> は全部 NEEDS にある（試験で通らない道も、静的に確かめる）。"""
    import ast
    import inspect
    _, services = _services()
    for cls, _svc in services.values():
        if not hasattr(cls, "NEEDS"):
            continue
        tree = ast.parse(inspect.getsource(inspect.getmodule(cls)))
        node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls.__name__)
        used = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Attribute)
                and n.value.attr == "_host" and isinstance(n.value.value, ast.Name) and n.value.value.id == "self"}
        assert used <= set(cls.NEEDS), (cls.__name__, sorted(used - set(cls.NEEDS)))
        assert set(cls.NEEDS) <= used, (cls.__name__, "使わない名前が NEEDS にある", sorted(set(cls.NEEDS) - used))


def test_every_needed_name_is_on_the_probe_and_every_shared_name_is_on_its_service():
    probe, services = _services()
    on_probe = set(dir(Probe)) | set(vars(probe))
    for cls, svc in services.values():
        for name in getattr(cls, "NEEDS", ()):
            assert name in on_probe, (cls.__name__, name)
        for name in getattr(cls, "SHARED", ()):
            assert name in dir(svc), (cls.__name__, name)
