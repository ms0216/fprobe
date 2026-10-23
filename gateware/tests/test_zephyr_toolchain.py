"""fprobe_gw.toolchain.zephyr: Zephyr の作業場所・SDK・west の場所と west build の呼び方（機器・道具は使わない）。"""

import subprocess
from pathlib import Path

import pytest

from fprobe_gw.toolchain.gowin import ToolchainNotFound
from fprobe_gw.toolchain.zephyr import ZephyrToolchain


def fake_tools(tmp_path: Path) -> dict[str, str]:
    ws, sdk = tmp_path / "ws", tmp_path / "sdk"
    (ws / "zephyr").mkdir(parents=True)
    (ws / "zephyr" / "VERSION").write_text("VERSION_MAJOR = 4\nVERSION_MINOR = 1\nPATCHLEVEL = 0\n", encoding="utf-8")
    sdk.mkdir()
    west = tmp_path / "west.exe"
    west.write_bytes(b"")
    return {"FPROBE_ZEPHYR_WS": str(ws), "FPROBE_ZEPHYR_SDK": str(sdk), "FPROBE_WEST": str(west)}


def test_locate_uses_the_environment(tmp_path):
    env = fake_tools(tmp_path)
    tc = ZephyrToolchain.locate(env)
    assert (tc.ws, tc.sdk, tc.west) == (Path(env["FPROBE_ZEPHYR_WS"]), Path(env["FPROBE_ZEPHYR_SDK"]), Path(env["FPROBE_WEST"]))


def test_locate_reports_what_is_missing(tmp_path):
    with pytest.raises(ToolchainNotFound) as e:
        ZephyrToolchain.locate({"FPROBE_ZEPHYR_WS": str(tmp_path / "none"), "FPROBE_ZEPHYR_SDK": str(tmp_path),
                                "FPROBE_WEST": str(tmp_path / "west.exe")})
    assert all(name in str(e.value) for name in ("FPROBE_ZEPHYR_WS", "FPROBE_ZEPHYR_SDK", "FPROBE_WEST"))


def test_version_reads_the_zephyr_version_file(tmp_path):
    assert ZephyrToolchain.locate(fake_tools(tmp_path)).version() == "4.1.0"


def test_build_runs_west_without_touching_the_workspace(tmp_path, monkeypatch):
    env = fake_tools(tmp_path)
    tc = ZephyrToolchain.locate(env)
    calls = []

    def run(args, **kw):
        calls.append((args, kw))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    app, out = tmp_path / "app", tmp_path / "out"
    elf = tc.build(app, "rpi_pico", out, ["-DEXTRA=1"])
    (args, kw), = calls
    assert elf == out / "zephyr" / "zephyr.elf"
    assert args[:2] == [str(tc.west), "build"]
    assert args[args.index("-s") + 1] == str(app) and args[args.index("-d") + 1] == str(out)
    assert args[args.index("-b") + 1] == "rpi_pico" and args[-2:] == ["--", "-DEXTRA=1"]
    assert Path(kw["cwd"]) == tc.ws
    assert kw["env"]["ZEPHYR_SDK_INSTALL_DIR"] == str(tc.sdk)
    assert kw["env"]["ZEPHYR_TOOLCHAIN_VARIANT"] == "zephyr" and kw["env"]["PYTHONUTF8"] == "1"


def test_relative_paths_are_made_absolute_and_zephyr_base_is_the_workspace(tmp_path, monkeypatch):
    # west は作業場所（~/zmk/ws）を cwd に走るので、相対の out はその中にできてしまう（書き換えない約束に反する）。
    # 呼び手のカレントから見た絶対パスにして渡す。ZEPHYR_BASE も作業場所の Zephyr に（シェルの別の値を使わない）
    env = fake_tools(tmp_path)
    tc = ZephyrToolchain.locate(env)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: calls.append((args, kw)) or subprocess.CompletedProcess(args, 0, "", ""))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ZEPHYR_BASE", str(tmp_path / "elsewhere"))
    elf = tc.build(Path("app"), "rpi_pico", Path("out"))
    (args, kw), = calls
    assert args[args.index("-d") + 1] == str(tmp_path / "out") and args[args.index("-s") + 1] == str(tmp_path / "app")
    assert elf == tmp_path / "out" / "zephyr" / "zephyr.elf"
    assert kw["env"]["ZEPHYR_BASE"] == str(tc.ws / "zephyr")
