"""Linux のプロセスの全コア停止デバッグの見せ方（LinuxDebugView に混ぜ込む部品）: ELF の状態とソースの一覧・読み出しと、
attach のときにソース・共有ライブラリを対象から取り寄せる関数（prefetch_sources / prefetch_libraries）。"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path, PurePosixPath

from .libsyms import library_bases, parse_maps
from ..symbols import Symbols, is_network_path

__all__ = ["SOURCE_LIMIT", "ViewSourcesMixin", "prefetch_libraries", "prefetch_sources"]

SOURCE_LIMIT = 2 << 20               # 読むソースの大きさの上限（probe_inspect.SOURCE_LIMIT と同じ）


class ViewSourcesMixin:
    """debug.elf.status・debug.source.list / read の形。"""

    def elf_status(self) -> dict:
        """debug.elf.status の形: attach の ELF（読み込みの見張りは無い。プロセスの像と ELF の一致は
        linux.halt.debug.status の image の事実）。"""
        path = self.d.image.path
        try:
            stat = path.stat()
            changed = (stat.st_size, stat.st_mtime_ns) != self._elf_stamp
        except OSError:
            changed = True
        return {"path": str(path), "loaded_at": self.loaded_at, "watching": False, "auto_reload": False, "interval_s": 0,
                "file_changed": changed, "target_image": "unknown", "changes": 0, "reloads": 0, "last": None,
                "error": None}

    def source_list(self) -> dict:
        symbols = self.d.symbols
        relocated = symbols.relocated()
        files = []
        for name, path in sorted(symbols.sources().items()):
            item = {"name": name, "path": str(path), "exists": not is_network_path(path) and path.is_file(),
                    **({"built_path": str(relocated[name]["built"])} if name in relocated else {})}
            got = self.sources.get(name)
            if got is not None:
                item["fetched" if "error" not in got else "fetch_error"] = dict(got)
            files.append(item)
        return {"files": files, "same_name": {n: [str(p) for p in ps]
                                              for n, ps in sorted(symbols.source_conflicts().items())}}

    def source_read(self, name: str) -> dict:
        path = self.d.symbols.sources().get(name)
        if path is None:
            raise KeyError(f"{name!r} はこの ELF のソースではない")
        if is_network_path(path):
            raise ValueError(f"外の共有のソースは開かない: {path}")
        lines = self.d.symbols.lines_of(name)
        if path.is_file():
            return {"name": name, "path": str(path), "text": _text(path), "lines": lines}
        got = self.sources.get(name)
        if got is None:
            raise FileNotFoundError(
                f"{name} がこの PC に無い（{path}）。止まっている間は対象の ssh が答えないので、取り寄せは attach のとき"
                "（コアを止める前）だけ: linux.halt.debug.attach に fetch_from_target（linux.connect のターゲットの名前）を渡す")
        if "error" in got:
            raise FileNotFoundError(f"{name} は attach のときに {got['target']} の {got['path']} から取り寄せられなかった: "
                                    f"{got['error']}")
        return {"name": name, "path": got["local_path"], "text": _text(Path(got["local_path"])), "lines": lines,
                "fetched": dict(got)}



def _built(symbols, name: str, path: Path) -> str:
    """行表の場所（ビルドした場所。ELF の隣の同じ名前に置き換えたなら元の場所）を posix の文字で。"""
    built = symbols.relocated().get(name, {}).get("built", path)
    return Path(built).as_posix()


def prefetch_sources(elf: str, target: str, fetch: Callable[[str, str, str], dict]) -> dict[str, dict]:
    """attach のとき（コアを止める前。止まった対象の ssh は答えない）に、ELF の行表が指すソースのうちこの PC に無いものを
    対象から取り寄せ、ELF と同じ所の <ELF の名前>.sources/<対象の上の道> に写す。名前 → {target, path, local_path, bytes,
    sha256}、取り寄せられなければ {target, path, error}（1 つの失敗で他をやめない）。"""
    symbols = Symbols(elf)
    root = Path(elf).parent / f"{Path(elf).name}.sources"
    out: dict[str, dict] = {}
    for name, path in sorted(symbols.sources().items()):
        if not is_network_path(path) and path.is_file():
            continue
        remote = _built(symbols, name, path)
        parts = [p for p in PurePosixPath(remote).parts if p not in ("/", "..", ".")]
        local = root.joinpath(*parts) if parts else root / name
        try:
            got = fetch(target, remote, str(local))
            out[name] = {"target": target, "path": remote, "local_path": got["local_path"], "bytes": got["bytes"],
                         "sha256": got["sha256"]}
        except Exception as e:  # noqa: BLE001 - 取り寄せられなかった事実を残し、他のファイルは続ける
            out[name] = {"target": target, "path": remote, "error": f"{type(e).__name__}: {e}"}
    return out


def prefetch_libraries(elf: str, target: str, pid: int, fetch: Callable[[str, str, str], dict]) -> list[dict]:
    """attach のとき（コアを止める前）に、対象の /proc/<pid>/maps が指す共有ライブラリを取り寄せ、ELF と同じ所の
    <ELF の名前>.libs/<対象の上の道> に写す。[{path, local_path, base（ファイルの先頭の対応の始まり）, bytes, sha256}]、
    取り寄せられなければ {path, base, error}（1 つの失敗で他をやめない）。maps が読めなければ、その事実の 1 件だけ。"""
    maps_local = Path(elf).parent / f"{Path(elf).name}.maps"
    try:
        got = fetch(target, f"/proc/{pid}/maps", str(maps_local))
        maps = parse_maps(Path(got["local_path"]).read_text(encoding="utf-8", errors="replace"))
    except Exception as e:  # noqa: BLE001 - 読めなかった事実を残す
        return [{"path": f"/proc/{pid}/maps", "base": None, "error": f"{type(e).__name__}: {e}"}]
    root = Path(elf).parent / f"{Path(elf).name}.libs"
    out: list[dict] = []
    for path, base in sorted(library_bases(maps).items()):          # 名前に .so を含むファイルだけ（実行ファイルは ELF として別に取り寄せ済み）
        parts = [p for p in PurePosixPath(path).parts if p not in ("/", "..", ".")]
        local = root.joinpath(*parts)
        try:
            got = fetch(target, path, str(local))
            out.append({"path": path, "local_path": got["local_path"], "base": base, "bytes": got["bytes"],
                        "sha256": got["sha256"]})
        except Exception as e:  # noqa: BLE001 - 取り寄せられなかった事実を残し、他は続ける
            out.append({"path": path, "base": base, "error": f"{type(e).__name__}: {e}"})
    return out


def _text(path: Path) -> str:
    if path.stat().st_size > SOURCE_LIMIT:
        raise ValueError(f"{path.name} は大きすぎる（{SOURCE_LIMIT // (1 << 20)} MB まで）")
    return path.read_text(encoding="utf-8", errors="replace")
