"""デーモンの許可の表: API から渡す PC のパスのうち、実行してよいファイル（exec）と書いてよい場所（write）。

利用者が <FPROBE_HOME>/permissions.json に書き、デーモンが起動時に読む（API からは変えられない）。仕様:
docs/superpowers/specs/2026-11-03-daemon-access-design.md §1。

    {"exec": ["C:/Xilinx/Vivado/2024.2/bin/xsdb.bat", "C:/Users/me/fprobe/decoders"],
     "write": ["C:/Users/me/fprobe/out"]}

項は絶対パス（~ は展開する）。ファイルならそのものだけ、フォルダならその下すべて（リンクと .. を解いてから比べる）。
ファイルが無ければ何も許さない（暗黙の許可は持たない）。デーモンの CLI は初めての起動（表のファイルが無いとき）に、
<FPROBE_HOME>/out だけに書いてよく実行は何も許さない表を作る（prepare_permissions。既にある表は書き換えない）。
"""

import json
from dataclasses import dataclass, replace
from pathlib import Path

__all__ = ["ACCESS", "Permissions", "PermissionsFileError", "load_permissions", "prepare_permissions"]

ACCESS = ("exec", "write")
FILE_NAME = "permissions.json"
OUT_DIR = "out"                         # 初めての表が書いてよいとする <FPROBE_HOME> の下のフォルダ


class PermissionsFileError(ValueError):
    """許可の表のファイルの形が違う（黙って捨てず、デーモンを起動しない）。"""


@dataclass(frozen=True)
class Permissions:
    file: Path | None                   # 読んだ（読もうとした）ファイル
    loaded: bool                        # ファイルがあって読んだ
    exec: tuple[Path, ...] = ()
    write: tuple[Path, ...] = ()
    created: bool = False               # この起動で初めての表として作った（prepare_permissions）

    def check(self, access: str, value: str) -> Path | None:
        """value（PC のパス）を解いた絶対パス。表の access の項のどれかと同じかその下なら返し、無ければ None。"""
        path = Path(value).expanduser().resolve()
        for entry in getattr(self, access):
            if path == entry or path.is_relative_to(entry):
                return path
        return None

    def as_dict(self) -> dict:
        return {"checked": True, "file": None if self.file is None else str(self.file), "loaded": self.loaded,
                "created": self.created, "exec": [str(p) for p in self.exec], "write": [str(p) for p in self.write]}


def load_permissions(path: Path) -> Permissions:
    """path の表を読む。無ければ空の表。形が違えば PermissionsFileError（どこが違うか）。"""
    if not path.is_file():
        return Permissions(path, False)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise PermissionsFileError(f"{path}: JSON として読めない: {e}") from None
    if not isinstance(data, dict):
        raise PermissionsFileError(f"{path}: 表は {{\"exec\": [...], \"write\": [...]}} のオブジェクト")
    unknown = set(data) - set(ACCESS)
    if unknown:
        raise PermissionsFileError(f"{path}: 知らない項目 {sorted(unknown)}（exec・write だけ）")
    lists = {}
    for access in ACCESS:
        entries = data.get(access, [])
        if not isinstance(entries, list):
            raise PermissionsFileError(f"{path}: {access} はパスの並び")
        resolved = []
        for entry in entries:
            if not isinstance(entry, str) or not entry:
                raise PermissionsFileError(f"{path}: {access} の項 {entry!r} は空でない文字列")
            p = Path(entry).expanduser()
            if not p.is_absolute():
                raise PermissionsFileError(f"{path}: {access} の項 {entry!r} は絶対パス（相対はデーモンの場所で意味が変わる）")
            resolved.append(p.resolve())
        lists[access] = tuple(resolved)
    return Permissions(path, True, **lists)


def prepare_permissions(path: Path, home: Path) -> Permissions:
    """デーモンの起動時の表: path があればそのまま読む（書き換えない。壊れていれば PermissionsFileError）。無ければ
    home/out だけに書いてよく、実行は何も許さない表を path に作り（home/out も作る）、created = True で返す。"""
    if not path.exists():
        out = (home / OUT_DIR).expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, "x", encoding="utf-8") as f:          # "x": 同時に作られていたら書かない（上書きしない）
                f.write(json.dumps({"exec": [], "write": [str(out)]}, ensure_ascii=False, indent=2) + "\n")
        except FileExistsError:
            return load_permissions(path)
        return replace(load_permissions(path), created=True)
    return load_permissions(path)
