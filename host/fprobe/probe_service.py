"""Probe を領域ごとのサービスに分ける基盤: 委譲（expose・delegate）、ホストの見え方（HostView）、後始末の登録（ReleaseSteps）。

仕様: docs/superpowers/specs/2026-11-03-probe-services-design.md

サービスは 1 つの領域の状態と操作を持つクラス（probe_*.py の XxxService）。ホスト（Probe: 核の状態と、ほかの領域）は
HostView 越しに使い、使う名前をクラスの NEEDS に全部並べる。API・MCP・デーモンは Probe の属性を impl 名で引くので、
サービスが API（公開の道具）と SHARED（ほかの領域・試験に出す名前）に並べた名前を、Probe に委譲の記述子として出す
（expose）。import の輪を作らないよう、probe_* を import しない。
"""

from collections.abc import Callable
from typing import Any

__all__ = ["HostView", "ReleaseSteps", "delegate", "expose"]


class delegate:  # noqa: N801 - 記述子（property と同じく小文字で使う）
    """Probe の属性 name を、そのインスタンスの self.<attr>（サービス）の同じ名前の属性に委ねる記述子（読み・書き・消し）。

    クラスから引くとサービスのクラスの属性（メソッドなら関数。署名・説明をそのまま見せる）、インスタンスから引くとサービスの
    属性（メソッドならサービスに束ねたもの）。書く・消すとサービスの属性を書く・消す: 試験・プラグインが Probe のメソッドを
    差し替えると、サービスの中からの呼び出しにも効く（mixin のときと同じ）。"""

    def __init__(self, attr: str, service: type, name: str):
        self.attr, self.service, self.name = attr, service, name
        self.__doc__ = getattr(getattr(service, name, None), "__doc__", None)

    def __get__(self, obj, objtype=None):
        if obj is None:
            return getattr(self.service, self.name)
        return getattr(getattr(obj, self.attr), self.name)

    def __set__(self, obj, value) -> None:
        setattr(getattr(obj, self.attr), self.name, value)

    def __delete__(self, obj) -> None:
        delattr(getattr(obj, self.attr), self.name)

    def __set_name__(self, owner, name: str) -> None:
        if name != self.name:
            raise TypeError(f"委譲の名前 {name} とサービスのメソッドの名前 {self.name} が違う")


def expose(attr: str, service: type) -> Callable[[type], type]:
    """クラスの飾り: service.API（公開の道具。呼べるもの）と service.SHARED（ほかの領域に出す名前。メソッド・状態）を、
    self.<attr> への委譲としてクラスに足す。既にある名前（継承したものを含む）・API の名前が service に無いものは TypeError
    （黙って隠さない・打ち違えを残さない）。SHARED の状態の名前はサービスの生成時にできるので、ここでは確かめない
    （Probe を作った後の試験 test_probe_services が確かめる）。"""
    def decorate(cls: type) -> type:
        for name in service.API:
            if not callable(getattr(service, name, None)) and not isinstance(getattr(service, name, None), property):
                raise TypeError(f"{service.__name__}.API の {name} が {service.__name__} に無い")
        for name in (*service.API, *getattr(service, "SHARED", ())):
            if hasattr(cls, name):
                owner = next(c.__name__ for c in cls.__mro__ if name in vars(c))
                raise TypeError(f"{name} は {owner} に既にある（{service.__name__} の委譲と重なる）")
            setattr(cls, name, delegate(attr, service, name))
        return cls
    return decorate


class HostView:
    """サービスから見たホスト（Probe: 核と、ほかの領域）。サービスが NEEDS に並べた名前だけを読める・書ける（並べていない
    名前は AttributeError: 領域の間の依存を、サービスの NEEDS に全部出す）。引くたびにホストを見る（差し替えが効く）。"""

    __slots__ = ("_host", "_needs", "_owner")

    def __init__(self, host: Any, needs: tuple[str, ...], owner: str):
        object.__setattr__(self, "_host", host)
        object.__setattr__(self, "_needs", frozenset(needs))
        object.__setattr__(self, "_owner", owner)

    def __getattr__(self, name: str) -> Any:
        if name in self._needs:
            return getattr(self._host, name)
        raise AttributeError(f"{self._owner} は {name} を NEEDS に並べていない（ホストの {name} は使えない）")

    def __setattr__(self, name: str, value: Any) -> None:
        if name not in self._needs:
            raise AttributeError(f"{self._owner} は {name} を NEEDS に並べていない（ホストの {name} は書けない）")
        setattr(self._host, name, value)


class ReleaseSteps:
    """機器を手放すときに閉じる段の登録（名前付き。登録の順に閉じる）。どの段も必ず試し（1 つの段の失敗で後ろを閉じ残さない）、
    失敗した段は [{"step": 名前, "error": "型: 文"}] で返す（例外にしない。Probe.close の約束）。"""

    def __init__(self):
        self._steps: list[tuple[str, Callable[[], Any]]] = []

    def add(self, name: str, step: Callable[[], Any]) -> None:
        if name in self.names():
            raise ValueError(f"後始末の段 {name} は登録済み")
        self._steps.append((name, step))

    def names(self) -> list[str]:
        return [name for name, _ in self._steps]

    def run(self) -> list[dict]:
        failures: list[dict] = []
        for name, step in self._steps:
            attempt(failures, name, step)
        return failures


def attempt(failures: list[dict], name: str, step: Callable[[], Any]) -> None:
    """後始末の 1 段を試し、失敗したら failures に事実として足す（止めない）。"""
    try:
        step()
    except Exception as e:  # noqa: BLE001 - 後始末はどの段も試す。失敗は集めて返す
        failures.append({"step": name, "error": f"{type(e).__name__}: {e}"})
