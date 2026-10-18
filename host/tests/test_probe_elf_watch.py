"""ELF の更新の検出と自動の再読込（fprobe.probe_elf_watch、U46）と、AI への知らせ（MCP の notices）。
機器は使わない（firmware の ELF を一時フォルダに写して書き換える）。"""

import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from fprobe.api.spec import load_spec
from fprobe.mcp_server import Notices, Probe, tool_function
from fprobe.probe_elf_watch import image_digest
from fprobe.symbols import Symbols

FIRMWARE = Path(__file__).resolve().parents[1] / "firmware"
OLD = FIRMWARE / "dwarf_demo" / "dwarf_demo_O0.elf"
NEW = FIRMWARE / "dwarf_demo" / "dwarf_demo_O1.elf"


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    p = Probe("unused")
    p._session = SimpleNamespace(device=None)
    published = []
    p.attach_publisher(lambda topic, data: published.append((topic, data)))
    p.published = published
    p._elf_interval = 60                                   # 背景の見回りは使わず、_elf_check を直接呼ぶ
    yield p
    p._stop_elf_watch()


def rebuild(path: Path, source: Path, bump_ns: int = 10_000_000) -> None:
    """ELF を書き換えたことにする（中身を差し替え、更新時刻を進める）。"""
    st = os.stat(path)
    shutil.copyfile(source, path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + bump_ns))


def test_a_rebuilt_elf_is_reloaded_after_it_settles_and_announced(probe, tmp_path):
    elf = tmp_path / "app.elf"
    shutil.copyfile(OLD, elf)
    probe.elf_load(str(elf))
    assert probe._elf_check() is None and not probe.elf_status()["file_changed"]
    rebuild(elf, NEW)
    assert probe.elf_status()["file_changed"]
    assert probe._elf_check() is None                      # 初めて見た印: 書き終わるのを待つ
    notice = probe._elf_check()
    assert notice["reloaded"] and notice["image_changed"] and notice["error"] is None
    assert probe._elf_digest == image_digest(Symbols(NEW))
    assert probe.published == [("elf.changed", notice)] and "読み込み直した" in notice["message"]
    status = probe.elf_status()
    assert status["reloads"] == 1 and status["changes"] == 1 and not status["file_changed"]


def test_target_image_is_stale_after_a_rebuild_of_what_was_written(probe, tmp_path):
    elf = tmp_path / "app.elf"
    shutil.copyfile(OLD, elf)
    probe.elf_load(str(elf))
    assert probe.elf_status()["target_image"] == "unknown"
    probe._image_written(elf=True)                         # target_flash で ELF を書いた
    assert probe.elf_status()["target_image"] == "current"
    rebuild(elf, NEW)
    probe._elf_check()
    notice = probe._elf_check()
    assert notice["target_image"] == "stale" and "target_flash" in notice["message"]
    assert probe.elf_status()["target_image"] == "stale"


def test_a_relink_with_the_same_contents_keeps_the_target_current(probe, tmp_path):
    elf = tmp_path / "app.elf"
    shutil.copyfile(OLD, elf)
    probe.elf_load(str(elf))
    probe._image_written(elf=True)
    rebuild(elf, OLD)
    probe._elf_check()
    notice = probe._elf_check()
    assert not notice["image_changed"] and notice["target_image"] == "current" and "同じ" in notice["message"]


def test_a_half_written_elf_keeps_the_old_one_and_is_not_retried(probe, tmp_path):
    elf = tmp_path / "app.elf"
    shutil.copyfile(OLD, elf)
    probe.elf_load(str(elf))
    st = os.stat(elf)
    elf.write_bytes(OLD.read_bytes()[:100])               # リンカが書いている途中
    os.utime(elf, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    probe._elf_check()
    notice = probe._elf_check()
    assert not notice["reloaded"] and notice["error"] and probe._symbols.path == elf
    assert probe._elf_digest == image_digest(Symbols(OLD))
    assert probe._elf_check() is None and probe._elf_check() is None   # 同じ印ではやり直さない
    rebuild(elf, NEW, bump_ns=20_000_000)                  # 書き終わった
    probe._elf_check()
    assert probe._elf_check()["reloaded"]


def test_without_auto_reload_the_change_is_only_announced(probe, tmp_path):
    elf = tmp_path / "app.elf"
    shutil.copyfile(OLD, elf)
    probe.elf_load(str(elf))
    probe.elf_watch(auto_reload=False, interval_s=60)
    rebuild(elf, NEW)
    probe._elf_check()
    notice = probe._elf_check()
    assert not notice["reloaded"] and notice["image_changed"] and "debug_elf_load" in notice["message"]
    assert probe._elf_digest == image_digest(Symbols(OLD))
    with pytest.raises(ValueError):
        probe.elf_watch(interval_s=0)


def test_the_background_watcher_reloads_by_itself(probe, tmp_path):
    import time
    elf = tmp_path / "app.elf"
    shutil.copyfile(OLD, elf)
    probe.elf_load(str(elf))
    probe.elf_watch(interval_s=0.05)
    rebuild(elf, NEW)
    deadline = time.monotonic() + 5
    while probe.elf_status()["reloads"] == 0:
        assert time.monotonic() < deadline, "読み込み直さない"
        time.sleep(0.02)
    probe.elf_watch(enabled=False)
    assert not probe.elf_status()["watching"]


def test_notices_are_attached_to_the_next_dict_result_of_a_tool():
    class Client:
        def call(self, method, **params):
            return [1] if method == "debug.backtrace" else {"ok": True}
    notices = Notices()
    spec = load_spec()
    status = tool_function(Client(), spec.methods["target.status"], notices)
    backtrace = tool_function(Client(), spec.methods["debug.backtrace"], notices)
    assert status() == {"ok": True}
    notices.add("elf.changed", {"message": "app.elf が更新された"})
    assert backtrace() == [1]                              # 辞書でない結果は形を変えない
    assert status() == {"ok": True, "notices": [{"topic": "elf.changed", "message": "app.elf が更新された"}]}
    assert status() == {"ok": True}                        # 添えたら消える
