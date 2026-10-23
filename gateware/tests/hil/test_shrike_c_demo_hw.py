"""Shrike-Lite の C のデモ（target_ip/fprobe_obs/examples/shrike_lite/firmware_c）を、動いているデーモン（PYNQ-Z2 の
fprobe）越しに書いて動かす実機の試験。--hil のときだけ。

前提: デーモンが動いている（FPROBE_DAEMON_URL、既定 ws://127.0.0.1:7820/api。トークンは FPROBE_TOKEN）、Shrike の SWD が
PYNQ の PMOD JA に配線されている（README）。書く前に、Shrike の USB ドライブ（MicroPython が残っていれば）を Windows で
取り出す（取り出さずに書くと FAT に汚れの印が付く）。変数の番地は ELF から引く（fprobe の汎用の操作だけで読む）。
"""

import os
import struct
import subprocess
import time
from pathlib import Path

import pytest
from elftools.elf.elffile import ELFFile

from fprobe.api.client import DaemonClient

pytestmark = pytest.mark.hil

DEMO = Path(__file__).resolve().parents[3] / "target_ip" / "fprobe_obs" / "examples" / "shrike_lite"
FPGA_ID = 0x5D01


def eject_shrike_drive() -> None:
    """Shrike（ディスクの名前 Vicharak Shrike）のボリュームがあれば Windows で取り出す。無ければ何もしない。"""
    script = ("$d = Get-Disk | Where-Object { $_.FriendlyName -match 'Shrike' }; if ($d) { "
              "$p = $d | Get-Partition | Where-Object DriveLetter; foreach ($x in $p) { "
              "(New-Object -ComObject Shell.Application).Namespace(17).ParseName(\"$($x.DriveLetter):\").InvokeVerb('Eject') } }")
    subprocess.run(["powershell.exe", "-NoProfile", "-Command", script], check=False, capture_output=True)
    time.sleep(2)


class Target:
    """デーモン越しの Shrike: ELF の変数を名前で読み書きする。"""

    def __init__(self, client: DaemonClient, elf: Path):
        self.c = client
        with open(elf, "rb") as f:
            self.symbols = {s.name: s["st_value"] for s in ELFFile(f).get_section_by_name(".symtab").iter_symbols()
                            if s.name.startswith("g_") or s.name == "_SEGGER_RTT"}

    def read(self, name: str) -> int:
        data = bytes.fromhex(self.c.call("target.memory.read", address=self.symbols[name], length=4))
        return struct.unpack("<I", data)[0]

    def write(self, name: str, value: int) -> None:
        self.c.call("target.memory.write", address=self.symbols[name], data_hex=struct.pack("<I", value).hex())

    def connect(self) -> None:
        self.c.call("target.connect", target="rp2040", connect_mode="attach", frequency_hz=1_000_000)


@pytest.fixture(scope="module")
def firmware():
    import importlib.util
    spec = importlib.util.spec_from_file_location("shrike_build", DEMO / "build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    fpga = build.build().image
    return build.build_firmware(fpga, Path(fpga).parent / "fw")


@pytest.fixture(scope="module")
def client():
    token = os.environ.get("FPROBE_TOKEN")
    if not token:
        pytest.skip("FPROBE_TOKEN（動いているデーモンのトークン）が無い")
    return DaemonClient(os.environ.get("FPROBE_DAEMON_URL", "ws://127.0.0.1:7820/api"), "shrike-c-hil", "script",
                        token=token)


@pytest.fixture(scope="module")
def target(client, firmware):
    t = Target(client, Path(firmware["elf"]))
    t.connect()
    return t


def test_flash_boots_and_runs(target, firmware):
    eject_shrike_drive()
    t0 = time.monotonic()
    r = target.c.call("target.flash", path=firmware["elf"], verify=True, reset=True)
    took = time.monotonic() - t0
    print(f"flash: {took:.1f} s {r}")
    target.connect()                                            # RP2040 のリセットで SWD がつなぎ直しになる
    time.sleep(3)                                               # FPGA の送り込み（約 0.8 s）と要求の開始
    assert target.read("g_fpga_id") == FPGA_ID
    first = target.read("g_requests")
    time.sleep(2)
    assert target.read("g_requests") > first
    assert target.read("g_errors") == 0


def rate(target: Target, seconds: float) -> tuple[int, int]:
    """seconds の間に増えた (要求, 誤り) の数。"""
    r0, e0 = target.read("g_requests"), target.read("g_errors")
    time.sleep(seconds)
    return target.read("g_requests") - r0, target.read("g_errors") - e0


def use(target: Target, scenario: int) -> None:
    """題材を切り替える（fprobe の汎用のメモリの書き込みだけ。フラッシュは書き直さない）。"""
    target.write("g_scenario", scenario)
    time.sleep(0.5)


def test_scenario_zero_has_no_errors(target):
    use(target, 0)
    requests, errors = rate(target, 60)
    print(f"S0: requests {requests} errors {errors}")
    assert requests > 1000 and errors == 0


# 題材の頻度の変数（仕様: 題材の変数で調整できる）。既定の S2（LFSR 10 ビット）・S3（99991 µs）は 30 秒で誤り数回で、
# 誤り 0 の回が確率で出る（S3 で約 13 %）。試験では起きやすくして確かめ、終わったら既定に戻す
FREQUENT = {2: ("g_s2_rate", 6, 10), 3: ("g_s3_period_us", 9973, 99991)}


@pytest.mark.parametrize("scenario", [1, 2, 3])
def test_each_scenario_produces_errors(target, scenario):
    name, often, default = FREQUENT.get(scenario, (None, None, None))
    if name:
        target.write(name, often)
    try:
        use(target, scenario)
        requests, errors = rate(target, 30)
        print(f"S{scenario}: requests {requests} errors {errors} retries {target.read('g_retries')}")
    finally:
        use(target, 0)
        if name:
            target.write(name, default)
    assert requests > 0 and errors > 0


def test_scenario_four_sticks_and_recovers(target):
    use(target, 4)
    deadline, stuck = time.monotonic() + 120, False
    while time.monotonic() < deadline and not stuck:
        before = target.read("g_requests")
        time.sleep(2)
        stuck = target.read("g_requests") == before               # 要求が進まない = FPGA が固まった
    print(f"S4: stuck {stuck} after {120 - (deadline - time.monotonic()):.0f} s, requests {target.read('g_requests')}")
    use(target, 0)
    target.write("g_reset", 1)
    time.sleep(3)
    assert stuck
    requests, errors = rate(target, 5)
    assert requests > 0 and target.read("g_reset") == 0


def test_switching_back_to_zero_clears_the_scenario(target):
    use(target, 1)
    time.sleep(5)
    use(target, 0)
    requests, errors = rate(target, 60)
    assert requests > 1000 and errors == 0


MAIN_C = DEMO / "firmware_c" / "src" / "main.c"
SIGNALS = {"groups": {"0": [
    {"name": "state", "lsb": 0, "width": 2, "format": "enum", "enum": {"0": "IDLE", "1": "BUSY", "2": "DONE"}},
    {"name": "spi", "lsb": 2, "width": 1}, {"name": "restart", "lsb": 3, "width": 1},
    {"name": "write", "lsb": 4, "width": 1},
    {"name": "addr", "lsb": 5, "width": 3, "format": "enum",
     "enum": {"0": "ID", "1": "CTRL", "2": "A", "3": "B", "4": "RESULT", "6": "STATUS", "7": "LEFT"}},
    {"name": "left_hi", "lsb": 8, "width": 4, "format": "dec"}, {"name": "dropped", "lsb": 12, "width": 1},
    {"name": "stuck", "lsb": 13, "width": 1}, {"name": "mode", "lsb": 14, "width": 2}]}}


def line_of(text: str) -> int:
    return next(i for i, line in enumerate(MAIN_C.read_text(encoding="utf-8").splitlines(), 1) if text in line)


def test_mcu_alone(target, firmware):
    # MCU だけ: ソース行のブレークポイント・変数を名前で・スレッドの一覧・RTT のログ
    c = target.c
    use(target, 0)
    c.call("debug.elf.load", path=firmware["elf"])
    line = line_of("bool ok = result ==")
    c.call("debug.breakpoint.set", location=f"main.c:{line}")
    try:
        r = c.call("debug.run_until_halt", timeout_s=10)
        print("halt", r)
        assert r["reason"] == "breakpoint" and f"main.c:{line}" in str(r["where"])
        a = c.call("debug.evaluate", expression="g_last_a")
        tasks = c.call("rtos.tasks")
        print("g_last_a", a, "tasks", [t.get("name") for t in tasks.get("tasks", [])])
        assert any(t.get("name") == "req" for t in tasks.get("tasks", []))
    finally:
        c.call("debug.breakpoint.clear", location=f"main.c:{line}")
        c.call("target.resume")
    c.call("rtt.start")
    data = c.call("rtt.read", channel=0, timeout_s=5)["data"]
    print("rtt", repr(data[:200]))
    assert data.startswith("req ") or "\nreq " in data
    assert all(ch.isprintable() or ch == "\n" for ch in data)   # 2026-10-21 に一度、開始直後に意味の無いバイトを読んだ


def test_soak_30min(target):
    # 題材なし（g_scenario = 0）で 30 分回して誤り 0（-k soak で個別に。長いので普段の実行では -k 'not soak'）
    use(target, 0)
    requests, errors = rate(target, 30 * 60)
    print(f"soak 30 min: requests {requests} errors {errors} retries {target.read('g_retries')}")
    assert requests > 30 * 60 * 10 and errors == 0                      # 動き続けた（毎秒 10 回より多く）・誤り 0


def test_mcu_survives_a_long_halt(target):
    # MicroPython は 150 秒止めると固まった。C のデモは 10 分止めても再開して動き続ける
    use(target, 0)
    target.c.call("target.halt")
    time.sleep(600)
    target.c.call("target.resume")
    requests, errors = rate(target, 5)
    assert requests > 0 and errors == 0


def test_fpga_alone(target):
    # FPGA だけ: 観測の回路で信号の定義どおりに状態が動き、要求ごとに BUSY に入る
    c = target.c
    use(target, 0)
    c.call("fpga_obs.open", chain={"ir_len": 4}, user_ir=2, half_period=20, words=1, sync_period=0, auto_reopen=True)
    c.call("fpga_obs.signals_set", signals=SIGNALS)
    time.sleep(2)
    items = c.call("fpga_obs.read", last=50_000)["items"]
    values = [(it["time_us"], it["fields"]["state"]) for it in items if it.get("fields")]
    busy = sum(1 for (_, p), (_, q) in zip(values, values[1:]) if q == "BUSY" and p != "BUSY")
    span_s = (values[-1][0] - values[0][0]) / 1e6
    print(f"fpga: {len(values)} values over {span_s:.2f} s, BUSY entries {busy} ({busy / span_s:.0f}/s)")
    assert {"BUSY", "DONE"} <= {v for _, v in values}
    assert 30 < busy / span_s < 300


def test_reset_during_s3_recovers(target):
    # 2026-10-21 の事故: S3 の割り込み（GPIO の出力を古い写しで書き戻す = CS を含む）が動いたまま FPGA に回路を送ると、
    # CS が乱れて FPGA が電源の入れ直しまで戻らなかった。送る前に題材の割り込みを止めること
    use(target, 3)
    time.sleep(2)
    target.write("g_fpga_id", 0)
    target.write("g_scenario", 0)
    target.write("g_reset", 1)
    time.sleep(4)
    assert target.read("g_fpga_id") == FPGA_ID
    requests, errors = rate(target, 3)
    assert requests > 0 and errors == 0
