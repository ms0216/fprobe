"""辿る上限に達したことと、列が壊れていること（輪・NULL・数より長い）を分けて書く（ヒューリスティックの総点検 T1〜T8）。
上限に達しただけでは壊れているとは言えない（正当な長い列もある）。"""
import re
from pathlib import Path

import fprobe.rtos.base as base
from fprobe.rtos.base import KernelAwareness, stopped_note

RTOS_DIR = Path(base.__file__).parent


class _Access:
    def __init__(self, words: dict[int, int]):
        self.words = words

    def read_memory(self, address: int, size: int) -> bytes:
        return self.words.get(address, 0).to_bytes(4, "little")[:size]


def _aware(words):
    return KernelAwareness(info=None, access=_Access(words))


def test_stopped_note_says_what_was_seen():
    assert "輪" in stopped_note("x", 3, "cycle") and "壊れ" not in stopped_note("x", 3, "limit", 64)
    assert "上限 64" in stopped_note("x", 64, "limit", 64)
    assert "NULL" in stopped_note("x", 2, "null")
    assert "数（5）" in stopped_note("x", 5, "over_count", 5)


def test_chain_reports_a_cycle_as_a_cycle(monkeypatch):
    k = _aware({0x100: 0x200, 0x200: 0x100})                      # 0x100 → 0x200 → 0x100（輪）
    assert k.chain(0x100, 0, "list") == [0x100, 0x200]
    assert "輪" in k.warnings[0]


def test_chain_reports_the_limit_as_the_limit_not_as_corruption(monkeypatch):
    monkeypatch.setattr(base, "MAX_CHAIN", 2)
    k = _aware({0x100: 0x200, 0x200: 0x300, 0x300: 0})            # 正当な 3 個の列を上限 2 で止める
    assert k.chain(0x100, 0, "list") == [0x100, 0x200]
    assert "上限 2" in k.warnings[0] and "壊れ" not in k.warnings[0]


def test_no_walker_calls_reaching_its_limit_corruption():
    # 元の 8 か所の文言（「〜が壊れている（N 個で止めた）」「大きすぎる」）が残っていない
    old = re.compile(r"壊れている（\{|大きすぎる（\{")
    hits = [f"{p.name}:{i}" for p in RTOS_DIR.glob("*.py")
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if old.search(line)]
    assert hits == []


def test_trace_readers_do_not_guess_the_cause():
    # T10・T11: 上限・範囲を超えたことは事実として書き、原因（像が違う・版が違う）は推測しない
    root = RTOS_DIR.parent
    texts = {p: p.read_text(encoding="utf-8") for p in (root / "trace" / "reconstruct.py", root / "tracefmt" / "tracealyzer.py")}
    assert not [p.name for p, t in texts.items() if "像が違う" in t or "版が違う" in t]
