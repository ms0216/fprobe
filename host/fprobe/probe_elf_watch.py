"""Probe の道具: 読み込んだ ELF の更新の検出と自動の再読込（U46。SEGGER Ozone の再ビルドの検出 SEG-52 に相当）。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う mixin。Probe の _lock・_symbols・elf_load と、ObserveTools の _record_host、
FlashTools の _publish（デーモンの通知の送り先）を使う。

- elf_load のたびに、ファイルの印（更新時刻・大きさ）と、書き込む中身（PT_LOAD の区画）の要約（SHA-256）を覚える。
- 背景のスレッドが interval_s ごとにファイルの印を見る。変わった印が次の見回りでも同じ（＝リンカが書き終えた）なら
  更新とみなし、auto_reload なら読み込み直す（シンボル・行番号・DWARF）。壊れた途中のファイルは読めないので、
  前の ELF のまま誤りを残し、次に印が変わったときにやり直す。
- 更新は通知 "elf.changed"（画面）と共通の時間軸の記録（stream "host"、kind "elf.changed"）に出す。AI（MCP）へは
  MCP の側がこの通知を受けて、次の道具の結果に notices として添える（fprobe.mcp_server）。
- ターゲットに書いたもの（target_flash の ELF・program_ram）の中身の要約も覚え、読み込んだ ELF と比べて
  target_image = current（同じ）/ stale（書き直しが要る）/ unknown（このセッションでは書いていない）を返す。
"""

import hashlib
import os
import threading
import time
from datetime import datetime
from pathlib import Path

__all__ = ["ELF_TOPIC", "ElfWatchTools", "image_digest"]

ELF_TOPIC = "elf.changed"


def image_digest(symbols) -> str:
    """ELF の書き込む中身（番地と内容）の要約。リンクし直してもコードが同じなら同じ値になる。"""
    h = hashlib.sha256()
    for segment in sorted(symbols.segments, key=lambda s: s.address):
        h.update(segment.address.to_bytes(8, "little"))
        h.update(len(segment.data).to_bytes(8, "little"))
        h.update(segment.data)
    return h.hexdigest()


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None                                        # 書き直しの途中で一時的に消えている等
    return st.st_mtime_ns, st.st_size


class ElfWatchTools:
    """ELF の更新の見張り（Probe の mixin）。"""

    def _init_elf_watch(self) -> None:
        self._elf_watching = True                          # 既定で見張る（elf_watch で切り替え）
        self._elf_auto = True
        self._elf_interval = 1.0
        self._elf_stamp: tuple[int, int] | None = None     # 読み込んだときの印
        self._elf_digest: str | None = None                # 読み込んだ ELF の中身の要約
        self._elf_loaded_at: str | None = None
        self._elf_seen: tuple[int, int] | None = None      # 前の見回りで見た、読み込んだものと違う印（書き終わり待ち）
        self._elf_failed: tuple[int, int] | None = None    # 読めなかった印（同じ印ではやり直さない）
        self._elf_changes = 0
        self._elf_reloads = 0
        self._elf_last: dict | None = None
        self._elf_error: str | None = None
        self._written_digest: str | None = None            # ターゲットに書いた ELF の中身の要約
        self._written_other = False                        # ELF でない（HEX / BIN）ものを書いた: 比べられない
        self._elf_thread: threading.Thread | None = None
        self._elf_wake = threading.Event()
        self._elf_closing = threading.Event()

    # --- 読み込み・書き込みの知らせ（elf_load・target_flash・program_ram から） -----------------

    def _elf_loaded(self) -> None:
        """ELF を読み込んだ（_lock の中で）: 印と要約を覚え、見張りを動かす。"""
        path = self._symbols.path
        self._elf_stamp = _stamp(path)
        self._elf_digest = image_digest(self._symbols)
        self._elf_loaded_at = datetime.now().isoformat(timespec="milliseconds")
        self._elf_seen = self._elf_failed = None
        self._elf_error = None
        self._ensure_elf_watcher()

    def _image_written(self, elf: bool) -> None:
        """ターゲットに書いた（_lock の中で）。elf なら、いま読み込んでいる ELF の中身を書いた。"""
        self._written_digest = self._elf_digest if elf else None
        self._written_other = not elf

    def _target_image(self) -> str:
        if self._written_digest is None:
            return "unknown"
        return "current" if self._written_digest == self._elf_digest else "stale"

    # --- 見張り ---------------------------------------------------------------------------------

    def _ensure_elf_watcher(self) -> None:
        if self._elf_watching and self._elf_thread is None and not self._elf_closing.is_set():
            self._elf_thread = threading.Thread(target=self._elf_loop, name="fprobe-elf-watch", daemon=True)
            self._elf_thread.start()

    def _elf_loop(self) -> None:
        while not self._elf_closing.is_set():
            self._elf_wake.wait(self._elf_interval)
            self._elf_wake.clear()
            if self._elf_closing.is_set() or not self._elf_watching:
                break
            try:
                self._elf_check()
            except Exception as e:                         # 見張りは止めない
                self._elf_error = f"{type(e).__name__}: {e}"
        self._elf_thread = None

    def _stop_elf_watch(self) -> None:
        """Probe.close から。"""
        self._elf_closing.set()
        self._elf_wake.set()
        thread = self._elf_thread
        if thread is not None:
            thread.join(timeout=2)

    def _elf_check(self) -> dict | None:
        """1 回の見回り。更新を扱ったらその知らせ（通知と同じ辞書）を返す。"""
        symbols = self._symbols
        if symbols is None:
            return None
        stamp = _stamp(symbols.path)
        if stamp is None or stamp == self._elf_stamp or stamp == self._elf_failed:
            self._elf_seen = None
            return None
        if stamp != self._elf_seen:                        # 初めて見た印: 書き終わるのを 1 回待つ
            self._elf_seen = stamp
            return None
        self._elf_seen = None
        return self._elf_changed(symbols.path, stamp)

    def _elf_changed(self, path: Path, stamp: tuple[int, int]) -> dict:
        from .symbols import Symbols
        self._elf_changes += 1
        before = self._elf_digest
        notice = {"path": str(path), "time": datetime.now().isoformat(timespec="milliseconds"), "reloaded": False,
                  "error": None}
        try:
            if self._elf_auto:
                with self._lock:
                    if self._symbols is None or self._symbols.path != path:
                        return {**notice, "error": "読み込んでいる ELF が替わった"}
                    self.elf_load(str(path))
                self._elf_reloads += 1
                notice["reloaded"] = True
                notice["image_changed"] = self._elf_digest != before
            else:
                notice["image_changed"] = image_digest(Symbols(path)) != before
        except Exception as e:                             # 書きかけ・壊れた ELF: 前のまま
            self._elf_failed = stamp
            self._elf_error = notice["error"] = f"{type(e).__name__}: {e}"
        notice["target_image"] = self._target_image() if notice["reloaded"] else \
            ("stale" if notice.get("image_changed") and self._written_digest is not None else self._target_image())
        notice["message"] = self._elf_message(notice)
        self._elf_last = notice
        publish = self._publish
        if publish is not None:
            publish(ELF_TOPIC, notice)
        self._record_host("host", ELF_TOPIC, time.monotonic(), body=notice)
        return notice

    @staticmethod
    def _elf_message(n: dict) -> str:
        name = Path(n["path"]).name
        if n["error"]:
            return f"{name} が更新されたが読めない（{n['error']}）。前の ELF のまま"
        head = f"{name} が更新された" + ("。読み込み直した" if n["reloaded"] else "（読み込み直していない: debug_elf_load）")
        if not n.get("image_changed"):
            return head + "。書き込む中身は同じ"
        if n["target_image"] == "stale":
            return head + "。ターゲットの中身は古い: target_flash で書き直す"
        return head + "。書き込む中身が変わった（ターゲットに書いたものと合っているかは不明）"

    # --- 道具 -----------------------------------------------------------------------------------

    def elf_watch(self, enabled: bool = True, auto_reload: bool = True, interval_s: float = 1.0) -> dict:
        """読み込んだ ELF の更新の見張りの設定（既定で有効）。auto_reload: 更新を見つけたら読み込み直す（False なら
        知らせるだけ）。interval_s: 見回りの間隔（書き終わりを待つため、検出は最長でその 2 倍かかる）。"""
        if not 0.05 <= interval_s <= 60:
            raise ValueError("interval_s は 0.05〜60 秒")
        self._elf_watching, self._elf_auto, self._elf_interval = enabled, auto_reload, interval_s
        self._elf_wake.set()                               # 間隔を変えた・止めた: 今の待ちを切り上げる
        if enabled and self._symbols is not None:
            thread = self._elf_thread
            if thread is not None and not thread.is_alive():
                self._elf_thread = None
            self._ensure_elf_watcher()
        return self.elf_status()

    def elf_status(self) -> dict:
        """読み込んだ ELF と更新の見張りの様子: path・loaded_at・file_changed（読み込んだ後にファイルが変わったか）・
        target_image（current / stale = 書き直しが要る / unknown）・changes・reloads・last（最後の更新の知らせ）・error。"""
        symbols = self._symbols
        path = symbols.path if symbols is not None else None
        stamp = _stamp(path) if path is not None else None
        return {"path": str(path) if path else None, "loaded_at": self._elf_loaded_at,
                "watching": self._elf_watching, "auto_reload": self._elf_auto, "interval_s": self._elf_interval,
                "file_changed": path is not None and stamp != self._elf_stamp,
                "target_image": self._target_image() if not self._written_other else "unknown",
                "changes": self._elf_changes, "reloads": self._elf_reloads, "last": self._elf_last,
                "error": self._elf_error}
