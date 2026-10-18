"""解読器の登録簿と積み重ね。仕様: docs/superpowers/specs/2026-10-18-u20-decoders-design.md

解読器は 2 種類:
- 波形を読む解読器: `function(tr: Transitions, **settings) -> Decoding`（lower が空）。
- 積み重ねる解読器: `function(lower: Decoding, **settings) -> Decoding`（lower = 受け付ける下の解読の名前）。
  名前で呼ぶとき（`decode`）は、設定の中の「下の解読の名前」のキーに下の設定を入れる。例:
  `decode("spiflash", tr, spi={"sck": 0, "mosi": 1, "miso": 2, "cs": 3}, address_bytes=3)`。何段でも重ねられる。

利用者の解読器は `register` / `@decoder` で登録する。`load_file` は Python のファイルを読み込み、その中の登録を
そのファイルのもの（origin）として持つ（読み直すと前の登録を置き換える。失敗したら登録簿は元のまま）。
組み込みの名前は上書きできない。
"""

import contextlib
import dataclasses
import hashlib
import inspect
import sys
import threading
import types
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from .core import Decoding

__all__ = ["BUILTIN", "Decoder", "decode", "decode_on", "decoder", "decoders", "get_decoder", "load_file",
           "register", "unregister"]

BUILTIN = "builtin"

_lock = threading.RLock()
_registry: dict[str, "Decoder"] = {}
_origin = threading.local()                         # load_file の中で登録の出どころを示す


@dataclass(frozen=True)
class Decoder:
    """登録された解読器。"""
    name: str
    function: Callable[..., Decoding]
    lower: tuple[str, ...] = ()                     # 空: 波形を読む。それ以外: 受け付ける下の解読の名前
    summary: str = ""
    default_kinds: tuple[str, ...] | None = None    # la.decode で kinds を省いたときに返す種類（None なら全部）
    origin: str = BUILTIN                           # "builtin"、Python から登録なら "python"、ファイルならその絶対パス

    @property
    def stacked(self) -> bool:
        return bool(self.lower)

    def parameters(self) -> list[dict]:
        """設定の一覧（関数の引数から。最初の引数＝波形か下の解読は除く）。"""
        try:
            sig = inspect.signature(self.function)
        except (TypeError, ValueError):
            return []
        out = []
        for p in list(sig.parameters.values())[1:]:
            if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                continue
            required = p.default is p.empty
            out.append({"name": p.name, "required": required,
                        **({} if required else {"default": _jsonable(p.default)})})
        return out

    def describe(self) -> dict:
        doc = inspect.getdoc(self.function) or ""
        return {"name": self.name, "lower": list(self.lower), "summary": self.summary or doc.split("\n")[0],
                "params": self.parameters(),
                "default_kinds": list(self.default_kinds) if self.default_kinds is not None else None,
                "origin": self.origin}


def _jsonable(value):
    return value if value is None or isinstance(value, (bool, int, float, str)) else repr(value)


def register(name: str, function: Callable[..., Decoding], *, lower: tuple[str, ...] | list[str] = (),
             summary: str = "", default_kinds: tuple[str, ...] | None = None, origin: str | None = None) -> Decoder:
    """解読器を登録する。同じ名前は同じ出どころ（origin）のものだけ置き換えられる（組み込みは置き換えられない）。"""
    if not name or not isinstance(name, str):
        raise ValueError(f"解読器の名前が空（{name!r}）")
    if not callable(function):
        raise TypeError(f"解読器 {name} が関数でない")
    origin = origin or getattr(_origin, "value", None) or "python"
    info = Decoder(name, function, tuple(lower), summary, tuple(default_kinds) if default_kinds else None, origin)
    with _lock:
        old = _registry.get(name)
        if old is not None and old.origin != origin:
            raise ValueError(f"解読器 {name} はすでにある（{old.origin}）")
        _registry[name] = info
    return info


def decoder(name: str, *, lower: tuple[str, ...] | list[str] = (), summary: str = "",
            default_kinds: tuple[str, ...] | None = None) -> Callable:
    """関数に付けて登録する飾り。"""
    def wrap(function):
        register(name, function, lower=lower, summary=summary, default_kinds=default_kinds)
        return function
    return wrap


def unregister(name: str) -> None:
    with _lock:
        info = _registry.get(name)
        if info is None:
            raise ValueError(f"解読器 {name} は無い")
        if info.origin == BUILTIN:
            raise ValueError(f"組み込みの解読器 {name} は外せない")
        del _registry[name]


def get_decoder(name: str) -> Decoder:
    with _lock:
        info = _registry.get(name)
        if info is None:
            raise ValueError(f"解読できるのは {sorted(_registry)}（{name!r}）")
        return info


def decoders() -> list[Decoder]:
    with _lock:
        return sorted(_registry.values(), key=lambda d: (d.stacked, d.name))


def split_settings(info: Decoder, params: Mapping[str, object]) -> tuple[str, dict, dict]:
    """積み重ねる解読器の設定を (下の名前, 下の設定, 自分の設定) に分ける。"""
    keys = [k for k in info.lower if k in params]
    if len(keys) != 1:
        raise ValueError(f"{info.name} は下の解読の設定を {list(info.lower)} のどれか 1 つのキーに入れる"
                         f"（例 {{{info.lower[0]!r}: {{…}}}}）")
    key = keys[0]
    lower_params = params[key]
    if not isinstance(lower_params, Mapping):
        raise ValueError(f"{info.name} の {key} は下の解読の設定（辞書）")
    return key, dict(lower_params), {k: v for k, v in params.items() if k != key}


def decode(protocol: str, tr, **params) -> Decoding:
    """名前で解読器を選んで解読する（API・MCP からの入口）。積み重ねる解読器は下から順に解読する。"""
    info = get_decoder(protocol)
    if not info.stacked:
        return _checked(info, info.function(tr, **params))
    name, lower_params, own = split_settings(info, params)
    return decode_on(protocol, decode(name, tr, **lower_params), **own)


def decode_on(protocol: str, lower: Decoding, **params) -> Decoding:
    """解読の結果（lower）の上に、積み重ねる解読器を走らせる。結果の params に下の設定を下の名前で入れる。"""
    info = get_decoder(protocol)
    if not info.stacked:
        raise ValueError(f"{protocol} は波形を読む解読器（下の解読の上には重ねられない）")
    if lower.protocol not in info.lower:
        raise ValueError(f"{protocol} が受け付ける下の解読は {list(info.lower)}（{lower.protocol!r}）")
    if lower.protocol not in _registry:
        raise ValueError(f"下の解読器 {lower.protocol} が登録されていない")
    result = _checked(info, info.function(lower, **params))
    return dataclasses.replace(result, params={**result.params, lower.protocol: dict(lower.params)})


def _checked(info: Decoder, result) -> Decoding:
    if not isinstance(result, Decoding):
        raise TypeError(f"解読器 {info.name} が Decoding を返さない（{type(result).__name__}）")
    return result


@contextlib.contextmanager
def _loading(origin: str) -> Iterator[None]:
    previous = getattr(_origin, "value", None)
    _origin.value = origin
    try:
        yield
    finally:
        _origin.value = previous


def load_file(path: str | Path) -> list[str]:
    """利用者の解読器のファイル（Python）を読み込む。登録された名前の一覧を返す。
    同じファイルを読み直すと、そのファイルの前の登録をすべて置き換える。失敗したら登録簿は元のまま。"""
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"解読器のファイルが無い: {path}")
    origin = str(path)
    module_name = "fprobe_user_decoder_" + hashlib.sha1(origin.encode()).hexdigest()[:12]
    with _lock:
        saved = dict(_registry)
        for name in [n for n, d in _registry.items() if d.origin == origin]:
            del _registry[name]
        try:
            # バイトコードの写し（__pycache__）は使わない: 同じ秒・同じ大きさの書き換えでも読み直しが古くならない
            module = types.ModuleType(module_name)
            module.__file__ = origin
            code = compile(path.read_bytes(), origin, "exec")
            with _loading(origin):
                exec(code, module.__dict__)
            names = sorted(n for n, d in _registry.items() if d.origin == origin)
            if not names:
                raise ValueError(f"{path} は解読器を 1 つも登録しない（@decoder か register を使う）")
        except BaseException:
            _registry.clear()
            _registry.update(saved)
            raise
        sys.modules[module_name] = module
        return names
