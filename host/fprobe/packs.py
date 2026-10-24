"""CMSIS-Pack（デバイスの定義・Flash の書き込みアルゴリズム・SVD）の導入と選択。pyOCD の pack 機能と同じ仕組み。

- find: 索引（cmsis-pack-manager の Cache。Keil の公開索引の写し）から部品番号で探す。索引が無ければ取ってくる。
- install: 見つかった部品の Pack を落として入れる。入れた Pack のターゲットは、pyOCD が target_connect の
  ターゲット名から自動で使う（pyOCD の board が ManagedPacks から引く）。
- installed: 入っている Pack。
- load: 手元の .pack ファイル（索引に無い Pack、社内の Pack、版を固定したいとき）のデバイスを、この実行の間だけ
  ターゲットとして登録する。

索引の取得とダウンロードはネットに出る（keil.com 等）。ponytail: 進捗は出さない（索引は数十秒かかることがある）。
"""

import fnmatch
import os
import re
from pathlib import Path
from typing import Callable

__all__ = ["Packs", "glob_matcher"]

INSTALL_LIMIT = 8                  # 1 回で入れる Pack の数の上限（"*" で索引の全部を落とさないように）
FIND_LIMIT = 200                   # find が返す部品の数の既定の上限（総数と切ったかは結果に）


def glob_matcher(pattern: str) -> Callable[[str], bool]:
    """大文字小文字を区別しないグロブ。* ? [ を含まなければ部分一致。"""
    if not any(c in pattern for c in "*?["):
        pattern = f"*{pattern}*"
    regex = re.compile(fnmatch.translate(pattern), re.IGNORECASE)
    return lambda name: regex.match(name) is not None


def _default_cache():
    import cmsis_pack_manager
    return cmsis_pack_manager.Cache(True, False)            # silent、no_timeouts でない


class Packs:
    def __init__(self, cache_factory: Callable[[], object] | None = None):
        self._factory = cache_factory or _default_cache
        self._cache = None

    @property
    def cache(self):
        if self._cache is None:
            self._cache = self._factory()
        return self._cache

    def _index(self, update: bool) -> dict:
        cache = self.cache
        if update or not cache.index:
            cache.cache_descriptors()
        return cache.index

    def _installed_path(self, ref) -> str:
        return os.path.join(self.cache.data_path, ref.get_pack_name())

    def _matches(self, pattern: str, update: bool) -> list[dict]:
        match = glob_matcher(pattern)
        index = self._index(update)
        return [index[name] for name in sorted(index) if match(name)]

    def find(self, pattern: str, update: bool = False, limit: int = FIND_LIMIT) -> dict:
        """部品番号（グロブ。ワイルドカードが無ければ部分一致）で索引を探す。名前の順に limit 個まで: parts と、合った
        部品の総数 total・切ったか truncated・使った上限 limit。"""
        if limit < 1:
            raise ValueError("limit は 1 以上")
        matches = self._matches(pattern, update)
        out = []
        for info in matches[:limit]:
            ref, = self.cache.packs_for_devices([info])
            out.append({"part": info["name"], "vendor": info["vendor"].split(":")[0], "pack": f"{ref.vendor}.{ref.pack}",
                        "version": ref.version, "installed": os.path.isfile(self._installed_path(ref))})
        return {"parts": out, "total": len(matches), "truncated": len(matches) > limit, "limit": limit}

    def install(self, pattern: str, update: bool = False, limit: int = INSTALL_LIMIT) -> dict:
        """部品番号に合う部品の Pack を落として入れる（入れ済みも含めて最新の版を落とし直さない: Cache が判断）。"""
        devices = self._matches(pattern, update)
        if not devices:
            raise ValueError(f"{pattern!r} に合う部品が索引に無い（target_pack_find で探す）")
        packs = self.cache.packs_for_devices(devices)
        if len(packs) > limit:
            raise ValueError(f"{pattern!r} は Pack が多すぎる（{len(packs)} 個。部品番号を絞る）")
        self.cache.download_pack_list(packs)
        return {"packs": [{"pack": f"{r.vendor}.{r.pack}", "version": r.version} for r in packs],
                "parts": [d["name"] for d in devices]}

    def installed(self) -> list[dict]:
        cache = self.cache
        refs = cache.packs_for_devices(list(cache.index.values())) if cache.index else []
        return [{"pack": f"{r.vendor}.{r.pack}", "version": r.version, "path": self._installed_path(r)}
                for r in refs if os.path.isfile(self._installed_path(r))]

    def load(self, path: str | Path) -> dict:
        """手元の .pack のデバイスをターゲットとして登録する（この実行の間。target_connect のターゲット名に使える名前を返す）。"""
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"{path} が無い")
        from pyocd.target import normalise_target_type_name
        targets = sorted({normalise_target_type_name(d.part_number) for d in _pack_devices(str(path))})
        _register_pack(str(path))
        return {"path": str(path), "targets": targets}


def _pack_devices(path: str) -> list:
    from pyocd.target.pack.cmsis_pack import CmsisPack
    return list(CmsisPack(path).devices)


def _register_pack(path: str) -> None:
    from pyocd.target.pack.pack_target import PackTargets
    PackTargets.populate_targets_from_pack([path])
