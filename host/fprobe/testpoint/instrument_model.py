"""テストポイントの自動挿入の設定と結果（Options・WatchVar・FileResult・InstrumentResult）と、ターゲット側ヘッダの場所。

挿入の本体は instrument.py（ファイル・ツリー）と instrument_function.py（1 つの関数）。
"""

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

from .cparse import Skipped
from .probes import ProbeTable

__all__ = ["FileResult", "InstrumentResult", "Options", "WatchVar", "header_path"]


def header_path() -> Path:
    """ターゲット側のヘッダ（写しの出力先にも置く）。"""
    return Path(__file__).with_name("fprobe_tp.h")


@dataclass(frozen=True)
class WatchVar:
    name: str
    value: bool = True              # False: 値を出さない（構造体など整数にできない変数）

    @classmethod
    def parse(cls, spec: str) -> "WatchVar":
        """"name" または "name:novalue"。"""
        name, _, flag = spec.partition(":")
        if not name.isidentifier() or flag not in ("", "novalue"):
            raise ValueError(f"変数の指定が読めない: {spec!r}（name または name:novalue）")
        return cls(name, flag != "novalue")


@dataclass(frozen=True)
class Options:
    entry_exit: bool = True
    branches: bool = True
    variables: tuple[WatchVar, ...] = ()
    include: tuple[str, ...] = ()        # 関数名の glob（空 = すべて）
    exclude: tuple[str, ...] = ()
    only_changed: bool = False           # 前回（baseline）から変わった・新しい関数にだけ入れる
    macro: str = "FPROBE_TP"
    value_macro: str = "FPROBE_TP_VALUE"
    # 戻り値の型の中の、型でない語（記憶域・属性のマクロ等。例 STATIC_INLINE）。出口の前に戻り値を置く一時変数の型から
    # 除く。語がマクロかは写しの側から分からないので、使う側が明示する（名前の形では決めない）
    non_type_words: tuple[str, ...] = ()

    def signature(self) -> str:
        """指紋に混ぜる（挿入の形を変える設定が変われば入れ直す）。"""
        return repr((self.entry_exit, self.branches, sorted((v.name, v.value) for v in self.variables), self.macro,
                     self.value_macro) + ((sorted(self.non_type_words),) if self.non_type_words else ()))

    def selects(self, name: str) -> bool:
        if self.include and not any(fnmatch.fnmatchcase(name, p) for p in self.include):
            return False
        return not any(fnmatch.fnmatchcase(name, p) for p in self.exclude)


@dataclass
class FileResult:
    path: str
    instrumented: list[str] = field(default_factory=list)      # 挿入した関数
    reused: list[str] = field(default_factory=list)            # 前回の ID をそのまま使った関数
    changed: list[str] = field(default_factory=list)           # 前回（baseline）から変わった・新しい関数
    skipped: list[Skipped] = field(default_factory=list)       # 挿入しなかった関数と理由
    warnings: list[str] = field(default_factory=list)
    error: str | None = None                                   # ファイル全体が読めない（写しは元のまま）
    probes: int = 0


@dataclass
class InstrumentResult:
    table: ProbeTable
    files: list[FileResult]
    removed: list[str]                  # 前回あって今回無い関数（"ファイル:名前"）
    written: list[Path]                 # 中身が変わって書いたファイル

    @property
    def probe_count(self) -> int:
        return len(self.table)
