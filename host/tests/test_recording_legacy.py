"""古い記録（記録の形式の版 3 より前。user_version 2・波形は変化点のファイル）を、書き換えずにそのまま読める。
固定試料 tests/data/recording_legacy は変更前のコードで作ったもの（expected.json はそのときの答え）。"""

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from recording_answers import answers

DATA = Path(__file__).parent / "data" / "recording_legacy"
EXPECTED = json.loads((DATA / "expected.json").read_text(encoding="utf-8"))


def _tree(directory: Path) -> dict[str, bytes]:
    return {str(p.relative_to(directory)): p.read_bytes() for p in sorted(directory.rglob("*"))
            if p.is_file() and not p.name.endswith(("-wal", "-shm"))}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_a_legacy_recording_is_read_with_the_same_answers(tmp_path, name):
    shutil.copytree(DATA / name, tmp_path / name)
    assert answers(tmp_path / name, tmp_path / "out") == EXPECTED[name]


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_reading_a_legacy_recording_does_not_rewrite_it(tmp_path, name):
    shutil.copytree(DATA / name, tmp_path / name)
    before = _tree(tmp_path / name)
    answers(tmp_path / name, tmp_path / "out")
    assert _tree(tmp_path / name) == before


def test_the_fixture_is_really_a_legacy_recording():
    db = sqlite3.connect(DATA / "plain" / "recording.sqlite")
    assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert not any(b'"format"' in p.read_bytes().split(b"\n", 1)[0] for p in (DATA / "plain" / "waveform").iterdir())
