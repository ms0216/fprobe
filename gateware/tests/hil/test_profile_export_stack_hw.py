"""プロファイルの集計・書き出し（U18）とメモリ・スタックの使用量（U26）の実機試験: Nucleo F401RE に RAM で走らせた
sram_demo（tick を回し続ける。割り込みなし）。

U18: pcsr の PC サンプリングを区間で切る・グループ・推移・gmon.out / callgrind / lcov / dot の書き出し。
U26: 接続中のメモリマップでの ELF の使用量、静的な最大のスタック、MSP の塗りと実測（実測 ≦ 静的な最大）。
ESP-03（gcov の .gcda をセミホスティングで書く）は、--coverage でビルドした試験用ファームが要る（未作成。手順は
test_gcda_by_semihosting の説明）。"""

import struct
import time
from pathlib import Path

import pytest

from fprobe.mcp_server import Probe
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
FIRMWARE = Path(__file__).resolve().parents[3] / "host" / "firmware"
SRAM_DEMO = FIRMWARE / "sram_demo" / "sram_demo.elf"


@pytest.fixture(scope="module")
def probe():
    program("fprobe")
    p = Probe(SERIAL)
    p.target_connect("stm32f401xe", frequency_hz=4_000_000)
    yield p
    p.target_reset()
    p.close()


def test_elf_size_with_the_target_memory_map(probe):
    probe.program_ram(str(SRAM_DEMO))
    out = probe.elf_size(top=5)
    assert out["regions_source"] == "target", out
    ram = next(r for r in out["regions"] if r["origin"] == 0x2000_0000)
    assert 0 < ram["used"] < ram["length"] and not out["outside"], out


def test_paint_then_measure_is_below_the_static_maximum(probe):
    probe.program_ram(str(SRAM_DEMO))                             # リセット直後の状態で止まる（SP = ベクタ表の初期値）
    static = probe.stack_static(su_paths=[])
    reset = next(r for r in static["roots"] if r["kind"] == "reset")
    assert reset["root"] == "reset_handler" and reset["bounded"], static
    painted = probe.stack_paint(nominal=False)                    # sram_demo には名目の大きさのシンボルが無い
    assert painted["bytes"] > 1024, painted
    probe.target_resume()
    time.sleep(0.5)
    running = probe.stack_usage()                                 # 走行中に読む（止めない）
    assert running["sp"] is None and not running["overflow"], running
    probe.target_halt()
    used = probe.stack_usage()
    assert 0 < used["used"] <= reset["max"], (used, reset)        # 実測は静的な最大を超えない
    assert used["current"] is not None and used["current"] <= used["used"], used


def test_profile_window_groups_timeline_and_exports(probe, tmp_path):
    probe.program_ram(str(SRAM_DEMO))
    probe.target_resume()
    probe.profile_start(method="pcsr", rate_hz=2000)
    time.sleep(1.0)
    probe.profile_stop()
    whole = probe.profile_aggregate()
    part = probe.profile_aggregate(t0_s=0.2, t1_s=0.6)
    assert 0 < part["total"] < whole["total"], (part["window"], whole["window"])
    assert abs(part["total"] / whole["total"] - 0.4) < 0.15, (part["total"], whole["total"])
    same = probe.profile_aggregate(t0=part["window"]["t0"], t1=part["window"]["t1"])   # 記録の時間軸でも同じ
    assert same["total"] == part["total"], same["window"]
    files = probe.profile_aggregate(by="file")
    assert files["rows"][0]["group"].endswith("sram_demo.c"), files["rows"]
    timeline = probe.profile_timeline(bucket_s=0.1)
    assert len(timeline["buckets"]) >= 8 and all(b["load"] > 0.9 for b in timeline["buckets"]), timeline
    gmon = probe.profile_export("gmon", str(tmp_path / "gmon.out"))
    data = (tmp_path / "gmon.out").read_bytes()
    assert data[:4] == b"gmon" and gmon["samples"] > 0, gmon
    rate = struct.unpack("<I", data[21 + 12:21 + 16])[0]
    assert 1000 < rate < 3000, rate                                # 実測の率（周期のゆらぎがあっても 2 kHz 前後）
    probe.profile_export("callgrind", str(tmp_path / "callgrind.out"))
    assert "fn=tick" in (tmp_path / "callgrind.out").read_text(encoding="utf-8")
    probe.profile_export("lcov", str(tmp_path / "pc.info"))
    assert "FNDA:" in (tmp_path / "pc.info").read_text(encoding="utf-8")
    probe.profile_export("dot", str(tmp_path / "pc.dot"))
    assert '"tick"' in (tmp_path / "pc.dot").read_text(encoding="utf-8")
    probe.target_halt()


@pytest.mark.skip(reason="--coverage でビルドした試験用ファーム（gcov の実行時ライブラリ + セミホスティングの "
                         "fopen / fwrite）が未作成")
def test_gcda_by_semihosting(probe, tmp_path):
    """ESP-03 の手順（実機で確かめる）:
    1. ファームを -fprofile-arcs -ftest-coverage（--coverage）でビルドし、newlib の rdimon（--specs=rdimon.specs）で
       リンクする。終わる所（試験の終わり）で __gcov_dump() を呼ぶ（.gcda を fopen / fwrite で書く）。
    2. probe.semihosting_configure(file_root=<ビルドのディレクトリ>, writable=True)（AI の道具からは file_root を
       指定できない: 人が許す）。.gcda はビルド時の .o と同じ道（-fprofile-dir / GCOV_PREFIX_STRIP で file_root の
       下に収める）。
    3. program_ram → run_until_halt（reason="exit" まで）。file_root の下に .gcda ができる。
    4. PC で gcov / lcov --capture --directory <ビルドのディレクトリ> → genhtml。fprobe の lcov（testpoint_export /
       profile_export）と lcov -a で合わせられる。"""
    raise NotImplementedError
