# M0+M1: 開発基盤とボード立ち上げ Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Amaranth で書いたゲートウェアを Gowin EDA でビルドし、PYNQ-Z2 を絶対に誤選択しない安全な書き込み経路で Primer 20K に載せ、PLL 100 MHz 系で動く UART エコーを実機自動テストで確認できる状態にする。

**Architecture:** `gateware/` に Python パッケージ `fprobe_gw` を置く。`toolchain/`（Gowin の所在・ビルド・タイミング判定・FTDI 列挙・安全な書き込み）と `platform/`（ボード層: Primer 20K の資源定義・PLL ソルバ）と `serial/`（再利用可能な UART コア）と `designs/`（ビルド単位のトップ）に責務分割する。ボードの JTAG 識別情報はボード層が持ち、書き込み器はそれに従うだけにする。

**Tech Stack:** Python 3.13, Amaranth 0.5.10（`amaranth.lib.wiring`, `amaranth.lib.stream`, `amaranth.lib.cdc`）, Gowin EDA Education 1.9.11.03（`gw_sh`, `programmer_cli`）, FTDI D2XX（ctypes）, pytest, pyserial（HIL）。

**Spec:** `docs/superpowers/specs/2026-10-15-fprobe-architecture-design.md`（§2 ハードウェア前提, §7 ボード層, §8 ピン割り当て, §10 開発・検証方式, §11 M0/M1）

## Global Constraints

- PYNQ-Z2（FTDI 説明 `TUL A`/`TUL B`, シリアル `1234-TUL`）には一切アクセスしない。JTAG 操作はボード層が宣言した FTDI 説明（Primer: `USB Debugger A`）と完全一致する**ちょうど 1 台**にのみ行い、さらに書き込み前に IDCODE（Primer: `0x0000081B`）を照合する。
- Gowin: `C:\Gowin\Gowin_V1.9.11.03_Education_x64`（環境変数 `FPROBE_GOWIN_HOME` で上書き可）。デバイス `GW2A-LV18PG256C8/I7`, ファミリ `GW2A-18C`。
- ビルド起動は `gw_sh` を絶対パスで直接実行する（このPCの cmd はカレントのバッチを探さないため Amaranth 標準の `build_*.bat` 実行は使わない）。
- 依存方向: `designs` → `platform`/`serial` → Amaranth。`toolchain` はゲートウェア本体に依存しない。
- コメント・ドキュメントは日本語、識別子は英語。
- テストは `pytest`。実機テストは `@pytest.mark.hil` を付け、既定では実行しない（`--hil` で有効化）。

## Review Focus

1. **Sipeed ボードが 0 台または 2 台以上接続 / PYNQ-Z2 が他プロセス使用中（説明文が空で列挙される）** → 曖昧なら必ず拒否し、空説明のデバイスは警告に列挙するだけで選択しない。（Task 4 のテスト）
2. **gw_sh の失敗・ビットストリーム未生成・タイミング違反** → 古い `top.fs` を書き込まないよう、ビルド前に成果物を削除し、TNS < 0 ならビルド失敗とする。（Task 5, 6 のテスト）
3. **ボード未接続・電源 OFF で書き込み** → IDCODE が読めない時点で明確なエラー。（Task 5 のテスト）
4. **UART 受信線のグリッチ・フレーミングエラー** → 半ビット未満のパルスは開始ビットとみなさず、ストップビット異常は `frame_error` を立てたうえで次のバイトから正常受信を再開する。（Task 9 のテスト）
5. **実現不能な PLL 周波数要求** → 許容誤差内の解が無ければ制約名を含む例外。（Task 7 のテスト）

---

## File Structure

```
.gitattributes                         改行を LF に固定
README.md                              セットアップ手順（日本語）
gateware/pyproject.toml                パッケージ fprobe-gateware
gateware/fprobe_gw/__init__.py
gateware/fprobe_gw/__main__.py         `python -m fprobe_gw` → cli.main()
gateware/fprobe_gw/cli.py              build / program / detect サブコマンド
gateware/fprobe_gw/toolchain/__init__.py
gateware/fprobe_gw/toolchain/gowin.py      GowinToolchain: 所在解決
gateware/fprobe_gw/toolchain/timing.py     TimingSummary: レポート解析
gateware/fprobe_gw/toolchain/build.py      GowinBuilder: prepare→extract→gw_sh→検証
gateware/fprobe_gw/toolchain/ftdi.py       FtdiDeviceInfo, list_ftdi_devices()
gateware/fprobe_gw/toolchain/programmer.py JtagIdentity, select_jtag_location(), GowinProgrammer
gateware/fprobe_gw/platform/__init__.py
gateware/fprobe_gw/platform/gowin_pll.py   PllConstraints, PllSolution, solve_pll(), GowinRPLL
gateware/fprobe_gw/platform/primer20k.py   TangPrimer20KDockPlatform, PrimerClockReset
gateware/fprobe_gw/serial/__init__.py
gateware/fprobe_gw/serial/uart.py          UartTx, UartRx
gateware/fprobe_gw/designs/__init__.py     DESIGNS レジストリ
gateware/fprobe_gw/designs/bringup.py      Bringup（LED 心拍 + UART エコー + 周波数申告）
gateware/tests/conftest.py                 --hil オプション
gateware/tests/test_*.py
gateware/tests/hil/test_bringup_hw.py
docs/boards/tang-primer-20k-dock.md        ピン表と注意点
```

---

### Task 1: リポジトリ足場とテスト基盤

**Files:**
- Create: `.gitattributes`, `README.md`, `gateware/pyproject.toml`, `gateware/fprobe_gw/__init__.py`, `gateware/tests/conftest.py`, `gateware/tests/test_package.py`

**Interfaces:**
- Produces: `fprobe_gw.__version__: str`; pytest オプション `--hil` とマーカー `hil`。

- [ ] **Step 1: 失敗するテストを書く**

```python
# gateware/tests/test_package.py
import fprobe_gw


def test_package_exposes_version():
    assert isinstance(fprobe_gw.__version__, str)
    assert fprobe_gw.__version__
```

- [ ] **Step 2: 失敗を確認** — Run: `.venv/Scripts/python -m pytest gateware/tests -q` → Expected: `ModuleNotFoundError: fprobe_gw`

- [ ] **Step 3: 実装**

`.gitattributes`:
```
* text=auto eol=lf
*.bat text eol=crlf
*.png binary
*.pdf binary
*.fs binary
```

`gateware/pyproject.toml`:
```toml
[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"

[project]
name = "fprobe-gateware"
version = "0.1.0"
description = "fprobe: FPGA マルチファンクション・デバッグプローブのゲートウェア"
requires-python = ">=3.11"
dependencies = [
    "amaranth~=0.5.10",
    "luna-usb~=0.2.3",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pyserial>=3.5"]

[tool.setuptools.packages.find]
include = ["fprobe_gw*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["hil: 実機（Primer 20K 等）が必要なテスト。--hil で有効化"]
```

`gateware/fprobe_gw/__init__.py`:
```python
"""fprobe ゲートウェア（Amaranth HDL）。"""

__version__ = "0.1.0"
```

`gateware/tests/conftest.py`:
```python
import pytest


def pytest_addoption(parser):
    parser.addoption("--hil", action="store_true", default=False,
                     help="実機が必要なテスト（hil マーカー）を実行する")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--hil"):
        return
    skip = pytest.mark.skip(reason="実機テストは --hil 指定時のみ実行")
    for item in items:
        if "hil" in item.keywords:
            item.add_marker(skip)
```

`README.md` にセットアップ手順（venv 作成、`pip install -e gateware[dev]`、`python -m pytest gateware/tests`）を書く。

- [ ] **Step 4: パス確認** — Run: `.venv/Scripts/python -m pip install -e "gateware[dev]" && .venv/Scripts/python -m pytest gateware/tests -q` → Expected: `1 passed`

- [ ] **Step 5: Commit** — `git add .gitattributes README.md gateware && git commit -m "chore: ゲートウェアパッケージの足場とテスト基盤"`

---

### Task 2: Gowin ツールチェーンの所在解決

**Files:**
- Create: `gateware/fprobe_gw/toolchain/__init__.py`, `gateware/fprobe_gw/toolchain/gowin.py`
- Test: `gateware/tests/test_toolchain_gowin.py`

**Interfaces:**
- Produces:
  - `class ToolchainNotFound(RuntimeError)`
  - `@dataclass(frozen=True) class GowinToolchain: home: Path` / properties `gw_sh: Path`, `programmer_cli: Path`
  - `GowinToolchain.locate(env: Mapping[str, str] = os.environ) -> GowinToolchain`
  - 定数 `DEFAULT_GOWIN_HOMES: tuple[Path, ...]`

- [ ] **Step 1: 失敗するテストを書く**

```python
# gateware/tests/test_toolchain_gowin.py
from pathlib import Path

import pytest

from fprobe_gw.toolchain.gowin import GowinToolchain, ToolchainNotFound


def _make_home(root: Path) -> Path:
    (root / "IDE" / "bin").mkdir(parents=True)
    (root / "Programmer" / "bin").mkdir(parents=True)
    (root / "IDE" / "bin" / "gw_sh.exe").write_bytes(b"")
    (root / "Programmer" / "bin" / "programmer_cli.exe").write_bytes(b"")
    return root


def test_locate_uses_env_override(tmp_path):
    home = _make_home(tmp_path / "gowin")
    tc = GowinToolchain.locate(env={"FPROBE_GOWIN_HOME": str(home)})
    assert tc.home == home
    assert tc.gw_sh == home / "IDE" / "bin" / "gw_sh.exe"
    assert tc.programmer_cli == home / "Programmer" / "bin" / "programmer_cli.exe"


def test_locate_rejects_incomplete_install(tmp_path):
    home = tmp_path / "empty"
    home.mkdir()
    with pytest.raises(ToolchainNotFound, match="gw_sh.exe"):
        GowinToolchain.locate(env={"FPROBE_GOWIN_HOME": str(home)})


def test_locate_falls_back_to_defaults(tmp_path, monkeypatch):
    home = _make_home(tmp_path / "default")
    monkeypatch.setattr("fprobe_gw.toolchain.gowin.DEFAULT_GOWIN_HOMES",
                        (tmp_path / "missing", home))
    assert GowinToolchain.locate(env={}).home == home


def test_locate_reports_all_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr("fprobe_gw.toolchain.gowin.DEFAULT_GOWIN_HOMES",
                        (tmp_path / "a", tmp_path / "b"))
    with pytest.raises(ToolchainNotFound) as e:
        GowinToolchain.locate(env={})
    assert "a" in str(e.value) and "b" in str(e.value)
```

- [ ] **Step 2: 失敗を確認** — `pytest gateware/tests/test_toolchain_gowin.py -q` → `ModuleNotFoundError`

- [ ] **Step 3: 実装**

```python
# gateware/fprobe_gw/toolchain/gowin.py
"""Gowin EDA のインストール場所を解決する。"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

__all__ = ["DEFAULT_GOWIN_HOMES", "GowinToolchain", "ToolchainNotFound"]

ENV_VAR = "FPROBE_GOWIN_HOME"

DEFAULT_GOWIN_HOMES: tuple[Path, ...] = (
    Path(r"C:\Gowin\Gowin_V1.9.11.03_Education_x64"),
)


class ToolchainNotFound(RuntimeError):
    """必要な Gowin ツールが見つからない。"""


@dataclass(frozen=True)
class GowinToolchain:
    home: Path

    @property
    def gw_sh(self) -> Path:
        return self.home / "IDE" / "bin" / "gw_sh.exe"

    @property
    def programmer_cli(self) -> Path:
        return self.home / "Programmer" / "bin" / "programmer_cli.exe"

    def missing_tools(self) -> list[Path]:
        return [p for p in (self.gw_sh, self.programmer_cli) if not p.is_file()]

    @classmethod
    def locate(cls, env: Mapping[str, str] = os.environ) -> "GowinToolchain":
        if ENV_VAR in env:
            candidates = (Path(env[ENV_VAR]),)
        else:
            candidates = DEFAULT_GOWIN_HOMES
        problems = []
        for home in candidates:
            toolchain = cls(home)
            missing = toolchain.missing_tools()
            if not missing:
                return toolchain
            problems.append(f"{home}: " + ", ".join(p.name for p in missing) + " が無い")
        raise ToolchainNotFound(
            f"Gowin EDA が見つからない（{ENV_VAR} で指定可能）: " + "; ".join(problems))
```

`gateware/fprobe_gw/toolchain/__init__.py`: `"""開発用ツールチェーン操作（ゲートウェア本体には依存しない）。"""`

- [ ] **Step 4: パス確認** — `pytest gateware/tests/test_toolchain_gowin.py -q` → `4 passed`
- [ ] **Step 5: Commit** — `git commit -m "feat(toolchain): Gowin EDA の所在解決"`

---

### Task 3: タイミングレポート解析

**Files:**
- Create: `gateware/fprobe_gw/toolchain/timing.py`
- Test: `gateware/tests/test_toolchain_timing.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class ClockTiming: name: str; constraint_mhz: float; fmax_mhz: float; setup_tns: float; hold_tns: float`
  - `@dataclass(frozen=True) class TimingSummary: clocks: tuple[ClockTiming, ...]` / property `met: bool`, method `describe() -> str`
  - `parse_timing_report(html: str) -> TimingSummary`（`impl/pnr/project_tr_content.html` の内容）
  - `class TimingReportError(ValueError)`

- [ ] **Step 1: 失敗するテストを書く**（実物のレポート断片を使う）

```python
# gateware/tests/test_toolchain_timing.py
import pytest

from fprobe_gw.toolchain.timing import TimingReportError, parse_timing_report

MET = """
<h2>Max Frequency Summary:</h2><table><tr><th>NO.</th><th>Clock Name</th><th>Constraint</th>
<th>Actual Fmax</th><th>Logic Level</th><th>Entity</th></tr>
<tr><td>1</td><td>clk27_0__io</td><td>27.000(MHz)</td><td>435.211(MHz)</td><td>5</td><td>TOP</td></tr>
<tr><td>2</td><td>sync_clk</td><td>100.286(MHz)</td><td>180.100(MHz)</td><td>7</td><td>TOP</td></tr></table>
<h2>Total Negative Slack Summary:</h2><table><tr><th>Clock Name</th><th>Analysis Type</th>
<th>Endpoints TNS</th><th>Number of Endpoints</th></tr>
<tr><td>clk27_0__io</td><td>Setup</td><td>0.000</td><td>0</td></tr>
<tr><td>clk27_0__io</td><td>Hold</td><td>0.000</td><td>0</td></tr>
<tr><td>sync_clk</td><td>Setup</td><td>0.000</td><td>0</td></tr>
<tr><td>sync_clk</td><td>Hold</td><td>0.000</td><td>0</td></tr></table>
<h2>Timing Details</h2>
"""

VIOLATED = MET.replace(
    "<tr><td>sync_clk</td><td>Setup</td><td>0.000</td><td>0</td></tr>",
    "<tr><td>sync_clk</td><td>Setup</td><td>-1.250</td><td>3</td></tr>")


def test_parse_met_report():
    s = parse_timing_report(MET)
    assert s.met
    assert [c.name for c in s.clocks] == ["clk27_0__io", "sync_clk"]
    sync = s.clocks[1]
    assert sync.constraint_mhz == pytest.approx(100.286)
    assert sync.fmax_mhz == pytest.approx(180.1)
    assert sync.setup_tns == 0.0 and sync.hold_tns == 0.0


def test_parse_violated_report():
    s = parse_timing_report(VIOLATED)
    assert not s.met
    assert "sync_clk" in s.describe()
    assert "-1.250" in s.describe()


def test_unrecognised_report_is_an_error():
    with pytest.raises(TimingReportError):
        parse_timing_report("<html>nothing</html>")
```

- [ ] **Step 2: 失敗を確認** — `ModuleNotFoundError`
- [ ] **Step 3: 実装**

```python
# gateware/fprobe_gw/toolchain/timing.py
"""Gowin のタイミングレポート（project_tr_content.html）を解析する。"""

import html
import re
from dataclasses import dataclass

__all__ = ["ClockTiming", "TimingReportError", "TimingSummary", "parse_timing_report"]


class TimingReportError(ValueError):
    """レポートの書式が想定外。"""


@dataclass(frozen=True)
class ClockTiming:
    name: str
    constraint_mhz: float
    fmax_mhz: float
    setup_tns: float
    hold_tns: float

    @property
    def met(self) -> bool:
        return self.setup_tns >= 0.0 and self.hold_tns >= 0.0


@dataclass(frozen=True)
class TimingSummary:
    clocks: tuple[ClockTiming, ...]

    @property
    def met(self) -> bool:
        return all(c.met for c in self.clocks)

    def describe(self) -> str:
        lines = []
        for c in self.clocks:
            mark = "OK " if c.met else "NG "
            lines.append(f"{mark}{c.name}: 制約 {c.constraint_mhz:.3f} MHz, Fmax {c.fmax_mhz:.3f} MHz, "
                         f"Setup TNS {c.setup_tns:.3f}, Hold TNS {c.hold_tns:.3f}")
        return "\n".join(lines)


def _section_cells(text: str, title: str) -> list[list[str]]:
    """`title` 見出し直後の最初の table を行×セルで返す。"""
    m = re.search(re.escape(title) + r".*?<table>(.*?)</table>", text, re.S)
    if not m:
        raise TimingReportError(f"'{title}' 節が見つからない")
    rows = []
    for row in re.findall(r"<tr>(.*?)</tr>", m.group(1), re.S):
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if cells:
            rows.append(cells)
    return rows


def _mhz(cell: str) -> float:
    m = re.fullmatch(r"([\d.]+)\(MHz\)", cell)
    if not m:
        raise TimingReportError(f"周波数の書式が想定外: {cell!r}")
    return float(m.group(1))


def parse_timing_report(report_html: str) -> TimingSummary:
    fmax = {}
    order = []
    for cells in _section_cells(report_html, "Max Frequency Summary:"):
        _, name, constraint, actual, *_ = cells
        fmax[name] = (_mhz(constraint), _mhz(actual))
        order.append(name)
    tns: dict[tuple[str, str], float] = {}
    for cells in _section_cells(report_html, "Total Negative Slack Summary:"):
        name, kind, value, _ = cells
        tns[(name, kind)] = float(value)
    if not order:
        raise TimingReportError("クロックが 1 つも報告されていない")
    clocks = tuple(
        ClockTiming(name=n, constraint_mhz=fmax[n][0], fmax_mhz=fmax[n][1],
                    setup_tns=tns.get((n, "Setup"), 0.0), hold_tns=tns.get((n, "Hold"), 0.0))
        for n in order)
    return TimingSummary(clocks)
```

- [ ] **Step 4: パス確認** — `3 passed`。加えて、スパイクで得た実レポート（`scratchpad/spike_blinky/build/impl/pnr/project_tr_content.html`）を `parse_timing_report` に通し `met == True` を手動確認する。HTML 構造が上の想定と異なれば、実物に合わせてテスト断片と正規表現を修正する（テスト断片は実物から切り出すこと）。
- [ ] **Step 5: Commit** — `git commit -m "feat(toolchain): Gowin タイミングレポート解析"`

---

### Task 4: FTDI 列挙と安全な JTAG 選択

**Files:**
- Create: `gateware/fprobe_gw/toolchain/ftdi.py`, `gateware/fprobe_gw/toolchain/programmer.py`（本タスクでは選択ロジックのみ）
- Test: `gateware/tests/test_toolchain_select.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class FtdiDeviceInfo: index: int; flags: int; device_type: int; usb_id: int; location: int; serial: str; description: str` / property `busy: bool`（説明・シリアルとも空 = 他プロセスが開いている）
  - `list_ftdi_devices() -> list[FtdiDeviceInfo]`（D2XX, Windows のみ）
  - `@dataclass(frozen=True) class JtagIdentity: board: str; ftdi_description: str; gowin_cable: str; gowin_device: str; idcode: int`
  - `class UnsafeTargetError(RuntimeError)`
  - `select_jtag_location(devices: Sequence[FtdiDeviceInfo], identity: JtagIdentity) -> int`

- [ ] **Step 1: 失敗するテストを書く**

```python
# gateware/tests/test_toolchain_select.py
import pytest

from fprobe_gw.toolchain.ftdi import FtdiDeviceInfo
from fprobe_gw.toolchain.programmer import JtagIdentity, UnsafeTargetError, select_jtag_location

PRIMER = JtagIdentity(board="Tang Primer 20K Dock", ftdi_description="USB Debugger A",
                      gowin_cable="USB Debugger A", gowin_device="GW2A-18C", idcode=0x0000081B)


def dev(index, location, serial, description):
    return FtdiDeviceInfo(index=index, flags=0, device_type=6, usb_id=0x04036010,
                          location=location, serial=serial, description=description)


# 実機で観測した列挙（PYNQ-Z2 の A チャンネルは他プロセス使用中で空）
OBSERVED = [
    dev(0, 0, "", ""),
    dev(1, 546, "1234-tulB", "TUL B"),
    dev(2, 8481, "A", "USB Debugger A"),
    dev(3, 8482, "B", "USB Debugger B"),
]


def test_selects_the_single_matching_channel():
    assert select_jtag_location(OBSERVED, PRIMER) == 8481


def test_never_selects_pynq_even_when_it_is_the_only_device():
    with pytest.raises(UnsafeTargetError, match="見つからない"):
        select_jtag_location([dev(0, 545, "1234-tulA", "TUL A"),
                              dev(1, 546, "1234-tulB", "TUL B")], PRIMER)


def test_refuses_when_two_boards_match():
    two = OBSERVED + [dev(4, 9000, "A", "USB Debugger A")]
    with pytest.raises(UnsafeTargetError, match="2 台"):
        select_jtag_location(two, PRIMER)


def test_refuses_matching_device_without_location():
    with pytest.raises(UnsafeTargetError, match="USB 位置"):
        select_jtag_location([dev(0, 0, "A", "USB Debugger A")], PRIMER)


def test_busy_device_is_detected():
    assert OBSERVED[0].busy
    assert not OBSERVED[2].busy
```

- [ ] **Step 2: 失敗を確認** — `ModuleNotFoundError`
- [ ] **Step 3: 実装**

```python
# gateware/fprobe_gw/toolchain/ftdi.py
"""FTDI D2XX による FT2232 系デバイスの列挙（読み取りのみ。デバイスは開かない）。"""

import ctypes
import sys
from dataclasses import dataclass

__all__ = ["FtdiDeviceInfo", "FtdiUnavailable", "list_ftdi_devices"]


class FtdiUnavailable(RuntimeError):
    """D2XX ドライバ（ftd2xx.dll）が使えない。"""


@dataclass(frozen=True)
class FtdiDeviceInfo:
    index: int
    flags: int
    device_type: int
    usb_id: int
    location: int
    serial: str
    description: str

    @property
    def busy(self) -> bool:
        """他プロセスが開いているデバイスは D2XX から説明もシリアルも読めない。"""
        return not self.serial and not self.description


def _load_d2xx():
    if sys.platform != "win32":
        raise FtdiUnavailable("D2XX 列挙は Windows でのみ対応")
    for name in ("ftd2xx64.dll", "ftd2xx.dll"):
        try:
            return ctypes.WinDLL(name)
        except OSError:
            continue
    raise FtdiUnavailable("ftd2xx.dll を読み込めない（FTDI ドライバ未導入）")


def list_ftdi_devices() -> list[FtdiDeviceInfo]:
    d2xx = _load_d2xx()
    count = ctypes.c_ulong()
    status = d2xx.FT_CreateDeviceInfoList(ctypes.byref(count))
    if status != 0:
        raise FtdiUnavailable(f"FT_CreateDeviceInfoList 失敗 (status={status})")
    devices = []
    for i in range(count.value):
        flags, dtype, usb_id, loc = (ctypes.c_ulong() for _ in range(4))
        handle = ctypes.c_void_p()
        serial = ctypes.create_string_buffer(64)
        desc = ctypes.create_string_buffer(64)
        status = d2xx.FT_GetDeviceInfoDetail(
            i, ctypes.byref(flags), ctypes.byref(dtype), ctypes.byref(usb_id),
            ctypes.byref(loc), serial, desc, ctypes.byref(handle))
        if status != 0:
            raise FtdiUnavailable(f"FT_GetDeviceInfoDetail({i}) 失敗 (status={status})")
        devices.append(FtdiDeviceInfo(
            index=i, flags=flags.value, device_type=dtype.value, usb_id=usb_id.value,
            location=loc.value, serial=serial.value.decode("ascii", "replace"),
            description=desc.value.decode("ascii", "replace")))
    return devices
```

```python
# gateware/fprobe_gw/toolchain/programmer.py（本タスク分）
"""FPGA への安全な書き込み。

対象は「ボード層が宣言した FTDI 説明と完全一致するちょうど 1 台」に限定し、
書き込み前に IDCODE を照合する。本プロジェクト対象外の機器（PYNQ-Z2 等）を
誤って操作しないための安全装置であり、条件を緩めてはならない。
"""

from collections.abc import Sequence
from dataclasses import dataclass

from .ftdi import FtdiDeviceInfo

__all__ = ["JtagIdentity", "UnsafeTargetError", "select_jtag_location"]


class UnsafeTargetError(RuntimeError):
    """書き込み対象を安全に一意特定できない。"""


@dataclass(frozen=True)
class JtagIdentity:
    board: str
    ftdi_description: str   # D2XX が返す説明（完全一致）
    gowin_cable: str        # programmer_cli の --cable 名
    gowin_device: str       # programmer_cli の --device 名
    idcode: int


def select_jtag_location(devices: Sequence[FtdiDeviceInfo], identity: JtagIdentity) -> int:
    matches = [d for d in devices if d.description == identity.ftdi_description]
    busy = [d for d in devices if d.busy]
    note = f"（他プロセス使用中で識別不能なデバイス {len(busy)} 台は対象外）" if busy else ""
    if not matches:
        raise UnsafeTargetError(
            f"{identity.board} の JTAG（説明 '{identity.ftdi_description}'）が見つからない{note}")
    if len(matches) > 1:
        locs = ", ".join(str(d.location) for d in matches)
        raise UnsafeTargetError(
            f"{identity.board} の候補が {len(matches)} 台あり一意に決められない（USB 位置 {locs}）")
    (target,) = matches
    if target.location == 0:
        raise UnsafeTargetError(f"{identity.board} の USB 位置が取得できない")
    return target.location
```

- [ ] **Step 4: パス確認** — `5 passed`。さらに `python -c "from fprobe_gw.toolchain.ftdi import list_ftdi_devices; print(list_ftdi_devices())"` で実機列挙が観測結果と一致することを確認。
- [ ] **Step 5: Commit** — `git commit -m "feat(toolchain): FTDI 列挙と JTAG 対象の安全な一意選択"`

---

### Task 5: GowinProgrammer（IDCODE 照合付き書き込み）

**Files:**
- Modify: `gateware/fprobe_gw/toolchain/programmer.py`
- Test: `gateware/tests/test_toolchain_programmer.py`

**Interfaces:**
- Consumes: Task 2 `GowinToolchain`, Task 4 `JtagIdentity`, `select_jtag_location`, `list_ftdi_devices`
- Produces:
  - `class ProgrammerError(RuntimeError)`
  - `class GowinProgrammer(toolchain: GowinToolchain, identity: JtagIdentity, *, list_devices=list_ftdi_devices, run=subprocess.run)`
    - `locate() -> int`
    - `read_idcode(location: int) -> int`
    - `program(bitstream: Path, *, persistent: bool = False) -> None`（False=SRAM, True=外部 Flash）
  - 定数 `OP_READ_IDCODE = 0`, `OP_SRAM_PROGRAM = 2`, `OP_FLASH_PROGRAM = 36`（外部 Flash は GAO-Bridge 経由。M1 実機で確認し、違えば修正）

- [ ] **Step 1: 失敗するテストを書く**

```python
# gateware/tests/test_toolchain_programmer.py
import subprocess
from pathlib import Path

import pytest

from fprobe_gw.toolchain.ftdi import FtdiDeviceInfo
from fprobe_gw.toolchain.gowin import GowinToolchain
from fprobe_gw.toolchain.programmer import (GowinProgrammer, JtagIdentity, ProgrammerError,
                                            UnsafeTargetError)

PRIMER = JtagIdentity(board="Tang Primer 20K Dock", ftdi_description="USB Debugger A",
                      gowin_cable="USB Debugger A", gowin_device="GW2A-18C", idcode=0x0000081B)
DEVICES = [FtdiDeviceInfo(2, 0, 6, 0x04036010, 8481, "A", "USB Debugger A")]
IDCODE_OK = " Target Cable: USB Debugger A/0/8481/null@2.5MHz\n ID Code is: 0x0000081B\n Finished.\n"
IDCODE_PYNQ = " ID Code is: 0x23727093\n Finished.\n"
PROGRAM_OK = " Operation \"SRAM Program\" for device#1...\n Finished.\n"


class FakeRun:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(map(str, args)))
        out = self.outputs.pop(0)
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")


@pytest.fixture
def toolchain(tmp_path):
    return GowinToolchain(tmp_path)


def test_program_sram_verifies_idcode_first(toolchain, tmp_path):
    fs = tmp_path / "top.fs"
    fs.write_bytes(b"x")
    run = FakeRun(IDCODE_OK, PROGRAM_OK)
    GowinProgrammer(toolchain, PRIMER, list_devices=lambda: DEVICES, run=run).program(fs)
    read, prog = run.calls
    assert read[read.index("--location") + 1] == "8481"
    assert read[read.index("--cable") + 1] == "USB Debugger A"
    assert read[read.index("--run") + 1] == "0"
    assert prog[prog.index("--run") + 1] == "2"
    assert prog[prog.index("--fsFile") + 1] == str(fs)
    assert prog[prog.index("--location") + 1] == "8481"


def test_refuses_on_idcode_mismatch(toolchain, tmp_path):
    fs = tmp_path / "top.fs"
    fs.write_bytes(b"x")
    run = FakeRun(IDCODE_PYNQ)
    with pytest.raises(UnsafeTargetError, match="0x23727093"):
        GowinProgrammer(toolchain, PRIMER, list_devices=lambda: DEVICES, run=run).program(fs)
    assert len(run.calls) == 1  # 書き込みコマンドは実行されない


def test_reports_unreadable_idcode(toolchain, tmp_path):
    fs = tmp_path / "top.fs"
    fs.write_bytes(b"x")
    run = FakeRun(" Error: cable open failed\n")
    with pytest.raises(ProgrammerError, match="IDCODE"):
        GowinProgrammer(toolchain, PRIMER, list_devices=lambda: DEVICES, run=run).program(fs)


def test_reports_failed_programming(toolchain, tmp_path):
    fs = tmp_path / "top.fs"
    fs.write_bytes(b"x")
    run = FakeRun(IDCODE_OK, " Error: verify failed\n")
    with pytest.raises(ProgrammerError, match="verify failed"):
        GowinProgrammer(toolchain, PRIMER, list_devices=lambda: DEVICES, run=run).program(fs)


def test_missing_bitstream_is_rejected_before_any_jtag_access(toolchain, tmp_path):
    run = FakeRun()
    with pytest.raises(FileNotFoundError):
        GowinProgrammer(toolchain, PRIMER, list_devices=lambda: DEVICES, run=run).program(
            tmp_path / "none.fs")
    assert run.calls == []
```

- [ ] **Step 2: 失敗を確認** — `ImportError: GowinProgrammer`
- [ ] **Step 3: 実装**（`programmer.py` に追記）

```python
import re
import subprocess
from pathlib import Path

from .ftdi import list_ftdi_devices
from .gowin import GowinToolchain

OP_READ_IDCODE = 0
OP_SRAM_PROGRAM = 2
OP_FLASH_PROGRAM = 36   # exFlash Erase,Program thru GAO-Bridge（Primer 20K の外部 Flash）


class ProgrammerError(RuntimeError):
    """programmer_cli が失敗した。"""


class GowinProgrammer:
    def __init__(self, toolchain: GowinToolchain, identity: JtagIdentity, *,
                 list_devices=list_ftdi_devices, run=subprocess.run):
        self._toolchain = toolchain
        self._identity = identity
        self._list_devices = list_devices
        self._run = run

    def locate(self) -> int:
        return select_jtag_location(self._list_devices(), self._identity)

    def _invoke(self, location: int, operation: int, *extra: str) -> str:
        args = [str(self._toolchain.programmer_cli),
                "--device", self._identity.gowin_device,
                "--cable", self._identity.gowin_cable,
                "--location", str(location),
                "--run", str(operation), *extra]
        result = self._run(args, capture_output=True, text=True, timeout=300)
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0 or re.search(r"\bError\b", output, re.I) \
                or "Finished." not in output:
            raise ProgrammerError(f"programmer_cli 失敗 (op={operation}):\n{output.strip()}")
        return output

    def read_idcode(self, location: int) -> int:
        try:
            output = self._invoke(location, OP_READ_IDCODE)
        except ProgrammerError as e:
            raise ProgrammerError(f"IDCODE を読めない（接続・電源を確認）: {e}") from e
        m = re.search(r"ID Code is:\s*0x([0-9A-Fa-f]+)", output)
        if not m:
            raise ProgrammerError(f"IDCODE を読めない:\n{output.strip()}")
        return int(m.group(1), 16)

    def program(self, bitstream: Path, *, persistent: bool = False) -> None:
        bitstream = Path(bitstream)
        if not bitstream.is_file():
            raise FileNotFoundError(bitstream)
        location = self.locate()
        idcode = self.read_idcode(location)
        if idcode != self._identity.idcode:
            raise UnsafeTargetError(
                f"USB 位置 {location} の IDCODE 0x{idcode:08X} が {self._identity.board} の "
                f"0x{self._identity.idcode:08X} と一致しないため中止")
        operation = OP_FLASH_PROGRAM if persistent else OP_SRAM_PROGRAM
        self._invoke(location, operation, "--fsFile", str(bitstream))
```

（`__all__` に `GowinProgrammer`, `ProgrammerError` を追加）

- [ ] **Step 4: パス確認** — `pytest gateware/tests/test_toolchain_programmer.py -q` → `5 passed`、全テストもパス。
- [ ] **Step 5: Commit** — `git commit -m "feat(toolchain): IDCODE 照合付きの安全な Gowin 書き込み器"`

---

### Task 6: GowinBuilder（ビルド実行と成果物検証）

**Files:**
- Create: `gateware/fprobe_gw/toolchain/build.py`
- Test: `gateware/tests/test_toolchain_build.py`

**Interfaces:**
- Consumes: Task 2 `GowinToolchain`, Task 3 `parse_timing_report`, `TimingSummary`
- Produces:
  - `class BuildError(RuntimeError)`
  - `@dataclass(frozen=True) class BuildResult: bitstream: Path; log: Path; timing: TimingSummary`
  - `class GowinBuilder(toolchain: GowinToolchain, *, run=subprocess.run)` / `build(platform, top, build_dir: Path, *, name: str = "top", **kwargs) -> BuildResult`

挙動: `platform.prepare(top, name=name, **kwargs)` → `plan.extract(build_dir)` → `build_dir/name.fs` と `impl/` を**事前削除** → `gw_sh name.tcl` を `cwd=build_dir` で実行し stdout+stderr を `build_dir/gw_sh.log` に保存 → 非ゼロ終了・`name.fs` 不在・タイミング未達はすべて `BuildError`（メッセージにログパスとタイミング要約を含める）。

- [ ] **Step 1: 失敗するテストを書く**

```python
# gateware/tests/test_toolchain_build.py
import subprocess
from pathlib import Path

import pytest

from fprobe_gw.toolchain.build import BuildError, GowinBuilder
from fprobe_gw.toolchain.gowin import GowinToolchain

REPORT_OK = ("<h2>Max Frequency Summary:</h2><table><tr><td>1</td><td>clk</td><td>27.000(MHz)</td>"
             "<td>300.000(MHz)</td><td>3</td><td>TOP</td></tr></table>"
             "<h2>Total Negative Slack Summary:</h2><table>"
             "<tr><td>clk</td><td>Setup</td><td>0.000</td><td>0</td></tr>"
             "<tr><td>clk</td><td>Hold</td><td>0.000</td><td>0</td></tr></table>")


class FakePlan:
    def extract(self, root):
        Path(root).mkdir(parents=True, exist_ok=True)
        (Path(root) / "top.tcl").write_text("run all")


class FakePlatform:
    def prepare(self, top, *, name, **kwargs):
        assert name == "top"
        return FakePlan()


def fake_gw_sh(report=REPORT_OK, rc=0, produce=True):
    def run(args, *, cwd, **kwargs):
        cwd = Path(cwd)
        if produce:
            pnr = cwd / "impl" / "pnr"
            pnr.mkdir(parents=True)
            (pnr / "project_tr_content.html").write_text(report)
            (cwd / "top.fs").write_bytes(b"bitstream")
        return subprocess.CompletedProcess(args, rc, stdout="gw_sh output", stderr="")
    return run


def test_successful_build(tmp_path):
    result = GowinBuilder(GowinToolchain(tmp_path / "gw"), run=fake_gw_sh()).build(
        FakePlatform(), object(), tmp_path / "b")
    assert result.bitstream == tmp_path / "b" / "top.fs"
    assert result.timing.met
    assert result.log.read_text() == "gw_sh output"


def test_stale_bitstream_is_never_reused(tmp_path):
    build_dir = tmp_path / "b"
    build_dir.mkdir()
    (build_dir / "top.fs").write_bytes(b"stale")
    with pytest.raises(BuildError, match="ビットストリーム"):
        GowinBuilder(GowinToolchain(tmp_path / "gw"), run=fake_gw_sh(produce=False)).build(
            FakePlatform(), object(), build_dir)
    assert not (build_dir / "top.fs").exists()


def test_nonzero_exit_is_an_error(tmp_path):
    with pytest.raises(BuildError, match="gw_sh.log"):
        GowinBuilder(GowinToolchain(tmp_path / "gw"), run=fake_gw_sh(rc=1)).build(
            FakePlatform(), object(), tmp_path / "b")


def test_timing_violation_is_an_error(tmp_path):
    bad = REPORT_OK.replace("<td>Setup</td><td>0.000</td>", "<td>Setup</td><td>-0.500</td>")
    with pytest.raises(BuildError, match="タイミング"):
        GowinBuilder(GowinToolchain(tmp_path / "gw"), run=fake_gw_sh(report=bad)).build(
            FakePlatform(), object(), tmp_path / "b")
```

- [ ] **Step 2: 失敗を確認**
- [ ] **Step 3: 実装**

```python
# gateware/fprobe_gw/toolchain/build.py
"""Amaranth 設計を Gowin EDA（gw_sh）でビルドし、成果物とタイミングを検証する。"""

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .gowin import GowinToolchain
from .timing import TimingReportError, TimingSummary, parse_timing_report

__all__ = ["BuildError", "BuildResult", "GowinBuilder"]


class BuildError(RuntimeError):
    """ビルドが失敗した、または成果物が信頼できない。"""


@dataclass(frozen=True)
class BuildResult:
    bitstream: Path
    log: Path
    timing: TimingSummary


class GowinBuilder:
    def __init__(self, toolchain: GowinToolchain, *, run=subprocess.run):
        self._toolchain = toolchain
        self._run = run

    def build(self, platform, top, build_dir: Path, *, name: str = "top", **kwargs) -> BuildResult:
        build_dir = Path(build_dir)
        bitstream = build_dir / f"{name}.fs"
        # 前回の成果物が残っていると、失敗したビルドで古いビットストリームを書き込みかねない
        bitstream.unlink(missing_ok=True)
        shutil.rmtree(build_dir / "impl", ignore_errors=True)

        plan = platform.prepare(top, name=name, **kwargs)
        plan.extract(build_dir)

        log = build_dir / "gw_sh.log"
        result = self._run([str(self._toolchain.gw_sh), f"{name}.tcl"], cwd=build_dir,
                           capture_output=True, text=True)
        log.write_text((result.stdout or "") + (result.stderr or ""), encoding="utf-8")
        if result.returncode != 0:
            raise BuildError(f"gw_sh が終了コード {result.returncode} で失敗（{log}）")
        if not bitstream.is_file():
            raise BuildError(f"ビットストリーム {bitstream} が生成されていない（{log}）")

        report = build_dir / "impl" / "pnr" / "project_tr_content.html"
        try:
            timing = parse_timing_report(report.read_text(encoding="utf-8", errors="replace"))
        except (OSError, TimingReportError) as e:
            raise BuildError(f"タイミングレポートを解析できない: {e}") from e
        if not timing.met:
            raise BuildError("タイミング制約未達:\n" + timing.describe())
        return BuildResult(bitstream=bitstream, log=log, timing=timing)
```

- [ ] **Step 4: パス確認** — `4 passed`
- [ ] **Step 5: Commit** — `git commit -m "feat(toolchain): 成果物とタイミングを検証する Gowin ビルダ"`

---

### Task 7: Gowin rPLL ソルバとプリミティブ

**Files:**
- Create: `gateware/fprobe_gw/platform/__init__.py`, `gateware/fprobe_gw/platform/gowin_pll.py`
- Test: `gateware/tests/test_platform_gowin_pll.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class PllConstraints: pfd_mhz: tuple[float, float]; vco_mhz: tuple[float, float]; out_mhz: tuple[float, float]; idiv_max: int = 64; fbdiv_max: int = 64; odiv_values: tuple[int, ...] = (2, 4, 8, 16, 32, 48, 64, 80, 96, 112, 128)`
  - `PLL_CONSTRAINTS: dict[str, PllConstraints]`（キーはファミリ名。`"GW2A-18C"` は PFD 3–400, VCO 500–1250, OUT 3.90625–625）
  - `@dataclass(frozen=True) class PllSolution: fin_mhz: float; idiv: int; fbdiv: int; odiv: int` / properties `fout_mhz`, `vco_mhz`, `pfd_mhz`（`idiv`/`fbdiv` は実際の分周比 1..64。rPLL パラメータは `IDIV_SEL = idiv-1` 等）
  - `class PllSolveError(ValueError)`
  - `solve_pll(fin_mhz: float, fout_mhz: float, constraints: PllConstraints, *, tolerance: float = 0.005) -> PllSolution`（誤差最小、同率なら PFD 最大）
  - `class GowinRPLL(Elaboratable)`: `__init__(self, solution: PllSolution, *, device: str, clkin: Value)`, 出力信号 `clkout: Signal`, `lock: Signal`

- [ ] **Step 1: 失敗するテストを書く**

```python
# gateware/tests/test_platform_gowin_pll.py
import pytest
from amaranth import Signal
from amaranth.back import rtlil

from fprobe_gw.platform.gowin_pll import (PLL_CONSTRAINTS, GowinRPLL, PllSolveError,
                                          PllSolution, solve_pll)

GW2A = PLL_CONSTRAINTS["GW2A-18C"]


def test_reproduces_known_good_sipeed_setting():
    # Sipeed 公式例: 27 MHz → 120 MHz は IDIV_SEL=8, FBDIV_SEL=39, ODIV_SEL=8
    s = solve_pll(27.0, 120.0, GW2A)
    assert s.fout_mhz == pytest.approx(120.0)
    assert (s.idiv, s.fbdiv, s.odiv) == (9, 40, 8)


def test_100mhz_within_tolerance_and_constraints():
    s = solve_pll(27.0, 100.0, GW2A)
    assert abs(s.fout_mhz - 100.0) / 100.0 <= 0.005
    assert GW2A.pfd_mhz[0] <= s.pfd_mhz <= GW2A.pfd_mhz[1]
    assert GW2A.vco_mhz[0] <= s.vco_mhz <= GW2A.vco_mhz[1]


def test_infeasible_request_names_the_reason():
    with pytest.raises(PllSolveError, match="27.0 MHz"):
        solve_pll(27.0, 2000.0, GW2A)


def test_rpll_instance_parameters():
    s = PllSolution(fin_mhz=27.0, idiv=9, fbdiv=40, odiv=8)
    pll = GowinRPLL(s, device="GW2A-18C", clkin=Signal())
    text = rtlil.convert(pll, ports=[pll.clkout, pll.lock])
    assert "IDIV_SEL" in text and "FBDIV_SEL" in text and "ODIV_SEL" in text
```

- [ ] **Step 2: 失敗を確認**
- [ ] **Step 3: 実装**

```python
# gateware/fprobe_gw/platform/gowin_pll.py
"""Gowin rPLL の分周比ソルバとプリミティブ。

fout = fin * FBDIV / IDIV, fvco = fout * ODIV, fpfd = fin / IDIV
（IDIV/FBDIV は実分周比。rPLL のパラメータ値はそれぞれ -1 したもの）
"""

from dataclasses import dataclass

from amaranth import Elaboratable, Instance, Module, Signal, Value

__all__ = ["PLL_CONSTRAINTS", "GowinRPLL", "PllConstraints", "PllSolution", "PllSolveError",
           "solve_pll"]


@dataclass(frozen=True)
class PllConstraints:
    pfd_mhz: tuple[float, float]
    vco_mhz: tuple[float, float]
    out_mhz: tuple[float, float]
    idiv_max: int = 64
    fbdiv_max: int = 64
    odiv_values: tuple[int, ...] = (2, 4, 8, 16, 32, 48, 64, 80, 96, 112, 128)


# 出典: Gowin UG286（Clock User Guide）/ DS102（GW2A データシート）
PLL_CONSTRAINTS: dict[str, PllConstraints] = {
    "GW2A-18C": PllConstraints(pfd_mhz=(3.0, 400.0), vco_mhz=(500.0, 1250.0),
                               out_mhz=(3.90625, 625.0)),
}


class PllSolveError(ValueError):
    """制約内で要求周波数を実現できない。"""


@dataclass(frozen=True)
class PllSolution:
    fin_mhz: float
    idiv: int
    fbdiv: int
    odiv: int

    @property
    def pfd_mhz(self) -> float:
        return self.fin_mhz / self.idiv

    @property
    def fout_mhz(self) -> float:
        return self.fin_mhz * self.fbdiv / self.idiv

    @property
    def vco_mhz(self) -> float:
        return self.fout_mhz * self.odiv


def solve_pll(fin_mhz: float, fout_mhz: float, constraints: PllConstraints, *,
              tolerance: float = 0.005) -> PllSolution:
    def within(value, bounds):
        return bounds[0] <= value <= bounds[1]

    best = None
    for idiv in range(1, constraints.idiv_max + 1):
        if not within(fin_mhz / idiv, constraints.pfd_mhz):
            continue
        for fbdiv in range(1, constraints.fbdiv_max + 1):
            for odiv in constraints.odiv_values:
                s = PllSolution(fin_mhz, idiv, fbdiv, odiv)
                if not (within(s.fout_mhz, constraints.out_mhz)
                        and within(s.vco_mhz, constraints.vco_mhz)):
                    continue
                key = (abs(s.fout_mhz - fout_mhz), -s.pfd_mhz)
                if best is None or key < best[0]:
                    best = (key, s)
    if best is None or best[0][0] / fout_mhz > tolerance:
        found = f"（最良 {best[1].fout_mhz:.4f} MHz）" if best else ""
        raise PllSolveError(
            f"{fin_mhz} MHz から {fout_mhz} MHz を誤差 {tolerance:.2%} 以内で作れない{found}")
    return best[1]


class GowinRPLL(Elaboratable):
    def __init__(self, solution: PllSolution, *, device: str, clkin: Value):
        self.solution = solution
        self.device = device
        self.clkin = clkin
        self.clkout = Signal()
        self.lock = Signal()

    def elaborate(self, platform):
        m = Module()
        s = self.solution
        m.submodules.rpll = Instance(
            "rPLL",
            p_FCLKIN=f"{s.fin_mhz:g}",
            p_DYN_IDIV_SEL="false", p_IDIV_SEL=s.idiv - 1,
            p_DYN_FBDIV_SEL="false", p_FBDIV_SEL=s.fbdiv - 1,
            p_DYN_ODIV_SEL="false", p_ODIV_SEL=s.odiv,
            p_PSDA_SEL="0000", p_DYN_DA_EN="true", p_DUTYDA_SEL="1000",
            p_CLKOUT_FT_DIR=1, p_CLKOUTP_FT_DIR=1, p_CLKOUT_DLY_STEP=0, p_CLKOUTP_DLY_STEP=0,
            p_CLKFB_SEL="internal", p_CLKOUT_BYPASS="false", p_CLKOUTP_BYPASS="false",
            p_CLKOUTD_BYPASS="false", p_DYN_SDIV_SEL=2, p_CLKOUTD_SRC="CLKOUT",
            p_CLKOUTD3_SRC="CLKOUT", p_DEVICE=self.device,
            i_CLKIN=self.clkin, i_CLKFB=0, i_RESET=0, i_RESET_P=0,
            i_FBDSEL=0, i_IDSEL=0, i_ODSEL=0, i_PSDA=0, i_DUTYDA=0, i_FDLY=0,
            o_CLKOUT=self.clkout, o_LOCK=self.lock,
            o_CLKOUTP=Signal(), o_CLKOUTD=Signal(), o_CLKOUTD3=Signal())
        return m
```

- [ ] **Step 4: パス確認** — `4 passed`
- [ ] **Step 5: Commit** — `git commit -m "feat(platform): Gowin rPLL ソルバとプリミティブ"`

---

### Task 8: Tang Primer 20K Dock ボード層

**Files:**
- Create: `gateware/fprobe_gw/platform/primer20k.py`, `docs/boards/tang-primer-20k-dock.md`
- Test: `gateware/tests/test_platform_primer20k.py`

**Interfaces:**
- Consumes: Task 4 `JtagIdentity`, Task 7 `solve_pll`, `GowinRPLL`, `PLL_CONSTRAINTS`
- Produces:
  - `class TangPrimer20KDockPlatform(GowinPlatform)`: `part`, `family`, `default_clk = "clk27"`, `resources`, `connectors`, クラス属性 `jtag_identity: JtagIdentity`, `sync_mhz_requested = 100.0`, メソッド `sync_solution() -> PllSolution`, `__init__(self, *, revision: int = 3714)`
  - リソース名: `clk27`, `led` 0–5（`PinsN`）, `button` 0–3（`PinsN`, Key_0..Key_3。Key_4/SW0 は rev 差・RECONFIG ピンのため**定義しない**）, `uart` 0（BL702: `tx`=M11, `rx`=T13）, `ulpi` 0（M2 用）
  - コネクタ `pmod` 0–3（ピン 1..8 = ヘッダの 5..12 番）
  - `class PrimerClockReset(Elaboratable)`: `sync` ドメインを PLL から作り、PLL ロックまでリセット保持。属性 `sync_hz: int`

ピン表（回路図 rev 3714 p1 より。`docs/boards/tang-primer-20k-dock.md` にも同表を記載）:

| コネクタ | silk | ヘッダ 5,6,7,8,9,10,11,12 番 |
|---|---|---|
| pmod0 | J14 (MIC) | P9, T9(WS2812 共用), T8, P8, R8, T7, T6, P6 |
| pmod1 | J12 (DVP) | J14, J16, M14, M15, T12, R11, T11, P11 |
| pmod2 | J6 (LCD B) | B12, C12, B13, A14, B14, A15, D14, E15 |
| pmod3 | J5 (LCD R/G) | L9, N8, N9, N7, N6, D11, A11, B11 |

LED: C13(DONE), A13(READY), N16, N14, L14, L16（アクティブロー, LVCMOS33。C13/A13 は `-use_done_as_gpio 1 -use_ready_as_gpio 1` が必要）。
Button: Key_0=T10(3.3V), Key_1=T3, Key_2=T2, Key_3=D7（1.5V バンク → `LVCMOS15`, アクティブロー）。
ULPI: clk=T15, dir=K12, nxt=K13, stp=K11, rst=F10（Ethernet PHY と共用）, data[0..7]=G11,H12,J12,H13,T14,R13,P13,R12。

- [ ] **Step 1: 失敗するテストを書く**

```python
# gateware/tests/test_platform_primer20k.py
from amaranth import Elaboratable, Module

from fprobe_gw.platform.primer20k import PrimerClockReset, TangPrimer20KDockPlatform


class _Top(Elaboratable):
    def elaborate(self, platform):
        m = Module()
        m.submodules.crg = PrimerClockReset()
        led = platform.request("led", 0)
        m.d.sync += led.o.eq(~led.o)
        return m


def _files(platform):
    plan = platform.prepare(_Top(), name="top")
    return plan.files


def test_constraints_contain_expected_pins():
    files = _files(TangPrimer20KDockPlatform())
    cst = files["top.cst"]
    assert 'IO_LOC "clk27_0__io" H11;' in cst
    assert 'IO_LOC "led_0__io" C13;' in cst


def test_tcl_enables_done_and_ready_as_gpio():
    tcl = _files(TangPrimer20KDockPlatform())["top.tcl"]
    assert "-use_done_as_gpio 1" in tcl and "-use_ready_as_gpio 1" in tcl


def test_pmod_connector_pins():
    p = TangPrimer20KDockPlatform()
    assert p.connectors["pmod", 0].mapping["1"] == "P9"
    assert p.connectors["pmod", 3].mapping["8"] == "B11"


def test_sync_frequency_is_reported_from_pll_solution():
    p = TangPrimer20KDockPlatform()
    assert abs(p.sync_solution().fout_mhz - 100.0) < 0.5


def test_jtag_identity_is_primer():
    ident = TangPrimer20KDockPlatform.jtag_identity
    assert ident.ftdi_description == "USB Debugger A"
    assert ident.idcode == 0x0000081B
```

- [ ] **Step 2: 失敗を確認**
- [ ] **Step 3: 実装**

```python
# gateware/fprobe_gw/platform/primer20k.py
"""Sipeed Tang Primer 20K Dock（rev 3713/3714）のボード層。

ピン出典: Tang_Primer_20K_Dock_3714_Schematics.pdf。詳細は docs/boards/tang-primer-20k-dock.md。
"""

from amaranth import ClockDomain, ClockSignal, Elaboratable, Module, ResetSignal, Signal
from amaranth.build import Attrs, Clock, Connector, Pins, PinsN, Resource, Subsignal
from amaranth.lib.cdc import ResetSynchronizer
from amaranth.vendor import GowinPlatform

from ..toolchain.programmer import JtagIdentity
from .gowin_pll import PLL_CONSTRAINTS, GowinRPLL, PllSolution, solve_pll

__all__ = ["PrimerClockReset", "TangPrimer20KDockPlatform"]

_LV33 = Attrs(IO_TYPE="LVCMOS33")


class TangPrimer20KDockPlatform(GowinPlatform):
    part = "GW2A-LV18PG256C8/I7"
    family = "GW2A-18C"
    default_clk = "clk27"
    default_rst = None

    jtag_identity = JtagIdentity(
        board="Tang Primer 20K Dock", ftdi_description="USB Debugger A",
        gowin_cable="USB Debugger A", gowin_device="GW2A-18C", idcode=0x0000081B)

    clk27_mhz = 27.0
    sync_mhz_requested = 100.0

    resources = [
        Resource("clk27", 0, Pins("H11", dir="i"), Clock(27e6), _LV33),
        *[Resource("led", i, PinsN(pin, dir="o"), _LV33)
          for i, pin in enumerate(["C13", "A13", "N16", "N14", "L14", "L16"])],
        Resource("button", 0, PinsN("T10", dir="i"), _LV33),
        *[Resource("button", i, PinsN(pin, dir="i"), Attrs(IO_TYPE="LVCMOS15"))
          for i, pin in [(1, "T3"), (2, "T2"), (3, "D7")]],
        # BL702（USB-JTAG 側の仮想 COM）。方向は FPGA から見たもの
        Resource("uart", 0,
                 Subsignal("tx", Pins("M11", dir="o")),
                 Subsignal("rx", Pins("T13", dir="i")),
                 _LV33),
        Resource("ulpi", 0,
                 Subsignal("clk", Pins("T15", dir="i"), Clock(60e6)),
                 Subsignal("data", Pins("G11 H12 J12 H13 T14 R13 P13 R12", dir="io")),
                 Subsignal("dir", Pins("K12", dir="i")),
                 Subsignal("nxt", Pins("K13", dir="i")),
                 Subsignal("stp", Pins("K11", dir="o")),
                 Subsignal("rst", Pins("F10", dir="o")),  # RTL8201F RESET と共用
                 _LV33),
    ]

    # 各ヘッダの 5..12 番ピンを コネクタピン 1..8 とする（1..4 番は電源）
    connectors = [
        Connector("pmod", 0, "P9 T9 T8 P8 R8 T7 T6 P6"),        # J14 (MIC)。T9 は WS2812 と共用
        Connector("pmod", 1, "J14 J16 M14 M15 T12 R11 T11 P11"),  # J12 (DVP)
        Connector("pmod", 2, "B12 C12 B13 A14 B14 A15 D14 E15"),  # J6 (LCD B)
        Connector("pmod", 3, "L9 N8 N9 N7 N6 D11 A11 B11"),       # J5 (LCD R/G)
    ]

    def __init__(self, *, revision: int = 3714, toolchain: str = "Gowin"):
        if revision not in (3713, 3714):
            raise ValueError(f"未対応の基板リビジョン: {revision}")
        self.revision = revision
        super().__init__(toolchain=toolchain)

    def sync_solution(self) -> PllSolution:
        return solve_pll(self.clk27_mhz, self.sync_mhz_requested, PLL_CONSTRAINTS[self.family])

    def toolchain_prepare(self, fragment, name, **kwargs):
        options = kwargs.pop("add_options", "")
        options = "set_option -use_done_as_gpio 1 -use_ready_as_gpio 1\n" + options
        return super().toolchain_prepare(fragment, name, add_options=options, **kwargs)


class PrimerClockReset(Elaboratable):
    """27 MHz → PLL → `sync` ドメイン。PLL ロックまで sync をリセット保持する。"""

    def __init__(self):
        self.sync_hz = None

    def elaborate(self, platform):
        m = Module()
        clk27 = platform.request("clk27", 0)
        solution = platform.sync_solution()
        self.sync_hz = round(solution.fout_mhz * 1e6)
        m.submodules.pll = pll = GowinRPLL(solution, device=platform.family, clkin=clk27.i)
        m.domains.sync = ClockDomain("sync")
        m.d.comb += ClockSignal("sync").eq(pll.clkout)
        m.submodules.rst_sync = ResetSynchronizer(~pll.lock, domain="sync")
        platform.add_clock_constraint(pll.clkout, solution.fout_mhz * 1e6)
        return m
```

注意: `sync_hz` を設計側が elaboration 前に必要とする場合（UART 分周比など）は `platform.sync_solution()` から直接求める。`PrimerClockReset` は単に `sync` を作る責務に限定する。`toolchain_prepare` のオーバーライドで `add_options` を合成できない場合（Amaranth の引数受け渡しが異なる場合）は、`build` 呼び出し側で `add_options` を渡す方式に切り替え、本テストをそれに合わせて修正する。

`docs/boards/tang-primer-20k-dock.md`: 上記ピン表、LED/ボタン極性、バンク電圧、F10 共用、T9 共用、rev 3714 の SW0/KEY4 入れ替え（未使用の理由）、USB ポートの役割と VBUS 直結、BL702 UART を記載。

- [ ] **Step 4: パス確認** — `5 passed`
- [ ] **Step 5: Commit** — `git commit -m "feat(platform): Tang Primer 20K Dock ボード層と PLL クロック生成"`

---

### Task 9: UART コア（UartTx / UartRx）

**Files:**
- Create: `gateware/fprobe_gw/serial/__init__.py`, `gateware/fprobe_gw/serial/uart.py`
- Test: `gateware/tests/test_serial_uart.py`

**Interfaces:**
- Produces:
  - `class UartTx(wiring.Component)`: `__init__(self, *, divisor_bits: int = 16)`; ポート `data: In(stream.Signature(8))`, `divisor: In(divisor_bits)`（1 ビットのクロック数, ≥ 2）, `tx: Out(1, init=1)`, `busy: Out(1)`
  - `class UartRx(wiring.Component)`: ポート `data: Out(stream.Signature(8))`, `divisor: In(divisor_bits)`, `rx: In(1)`, `frame_error: Out(1)`（1 クロックパルス）, `overrun: Out(1)`（1 クロックパルス: 受信完了時に前バイト未消費）
  - 8N1 固定。RX は `FFSynchronizer`（2 段）経由。開始ビット検出後、半ビット時点で再確認し low でなければ破棄（グリッチ耐性）。
  - ヘルパ（テスト専用, `tests/models/uart.py`）: `uart_bits(byte) -> list[int]`

- [ ] **Step 1: 失敗するテストを書く**

```python
# gateware/tests/models/__init__.py は空
# gateware/tests/models/uart.py
def uart_bits(byte: int) -> list[int]:
    """8N1 の線レベル列（開始ビット, LSB first データ, 停止ビット）。"""
    return [0] + [(byte >> i) & 1 for i in range(8)] + [1]
```

```python
# gateware/tests/test_serial_uart.py
from amaranth.sim import Simulator

from fprobe_gw.serial.uart import UartRx, UartTx
from tests.models.uart import uart_bits

DIV = 8


async def drive_line(ctx, rx, bits, divisor=DIV):
    for b in bits:
        ctx.set(rx, b)
        await ctx.tick().repeat(divisor)


def run(dut, testbench):
    sim = Simulator(dut)
    sim.add_clock(1e-8)
    sim.add_testbench(testbench)
    sim.run()


def test_tx_emits_8n1_frame():
    dut = UartTx()

    async def tb(ctx):
        ctx.set(dut.divisor, DIV)
        ctx.set(dut.data.payload, 0xA5)
        ctx.set(dut.data.valid, 1)
        await ctx.tick().until(dut.data.ready)
        ctx.set(dut.data.valid, 0)
        # 開始ビットの立ち下がりを待ち、各ビットの中央でサンプル
        await ctx.tick().until(~dut.tx)
        await ctx.tick().repeat(DIV // 2)
        seen = []
        for _ in range(10):
            seen.append(ctx.get(dut.tx))
            await ctx.tick().repeat(DIV)
        assert seen == uart_bits(0xA5)
        assert ctx.get(dut.tx) == 1

    run(dut, tb)


def test_rx_receives_bytes_back_to_back():
    dut = UartRx()

    async def tb(ctx):
        ctx.set(dut.divisor, DIV)
        ctx.set(dut.rx, 1)
        ctx.set(dut.data.ready, 1)
        await ctx.tick().repeat(5)
        received = []

        async def collect():
            pass

        for byte in (0x00, 0xFF, 0x3C):
            await drive_line(ctx, dut.rx, uart_bits(byte))
        # 最後のバイトの取り込み余裕
        await ctx.tick().repeat(DIV * 2)

    # 受信はモニタ用 testbench で収集する
    sim = Simulator(dut)
    sim.add_clock(1e-8)
    got = []

    async def monitor(ctx):
        async for _, _, valid, payload in ctx.tick().sample(dut.data.valid, dut.data.payload):
            if valid:
                got.append(payload)

    sim.add_process(monitor)
    sim.add_testbench(tb)
    sim.run()
    assert got == [0x00, 0xFF, 0x3C]


def test_rx_ignores_short_glitch_and_flags_frame_error():
    dut = UartRx()
    events = {"frame_error": 0, "bytes": []}

    async def tb(ctx):
        ctx.set(dut.divisor, DIV)
        ctx.set(dut.rx, 1)
        ctx.set(dut.data.ready, 1)
        await ctx.tick().repeat(5)
        # 半ビット未満のグリッチ → 無視されるべき
        ctx.set(dut.rx, 0)
        await ctx.tick().repeat(DIV // 2 - 2)
        ctx.set(dut.rx, 1)
        await ctx.tick().repeat(DIV * 12)
        # 停止ビットが 0 のフレーム → frame_error
        await drive_line(ctx, dut.rx, uart_bits(0x55)[:-1] + [0])
        ctx.set(dut.rx, 1)
        await ctx.tick().repeat(DIV * 3)
        # その後の正常なバイトは受信できる
        await drive_line(ctx, dut.rx, uart_bits(0x81))
        await ctx.tick().repeat(DIV * 2)

    async def monitor(ctx):
        async for _, _, fe, valid, payload in ctx.tick().sample(
                dut.frame_error, dut.data.valid, dut.data.payload):
            events["frame_error"] += fe
            if valid:
                events["bytes"].append(payload)

    sim = Simulator(dut)
    sim.add_clock(1e-8)
    sim.add_process(monitor)
    sim.add_testbench(tb)
    sim.run()
    assert events["frame_error"] == 1
    assert events["bytes"] == [0x81]


def test_tx_rx_loopback():
    from amaranth import Module
    m = Module()
    m.submodules.tx = tx = UartTx()
    m.submodules.rx = rx = UartRx()
    m.d.comb += rx.rx.eq(tx.tx)
    got = []

    async def tb(ctx):
        ctx.set(tx.divisor, DIV)
        ctx.set(rx.divisor, DIV)
        ctx.set(rx.data.ready, 1)
        for byte in b"fprobe":
            ctx.set(tx.data.payload, byte)
            ctx.set(tx.data.valid, 1)
            await ctx.tick().until(tx.data.ready)
        ctx.set(tx.data.valid, 0)
        await ctx.tick().repeat(DIV * 12)

    async def monitor(ctx):
        async for _, _, valid, payload in ctx.tick().sample(rx.data.valid, rx.data.payload):
            if valid:
                got.append(payload)

    sim = Simulator(m)
    sim.add_clock(1e-8)
    sim.add_process(monitor)
    sim.add_testbench(tb)
    sim.run()
    assert bytes(got) == b"fprobe"
```

（`test_rx_receives_bytes_back_to_back` 内の未使用 `received`/`collect` は書かない。上は意図説明用。実装時は削除した形で書く。）

`gateware/tests/__init__.py` を空で作り、`tests.models` を import 可能にする（pytest の `rootdir` は `gateware`、`pythonpath = ["."]` を `pyproject.toml` の `[tool.pytest.ini_options]` に追加）。

- [ ] **Step 2: 失敗を確認**
- [ ] **Step 3: 実装**

```python
# gateware/fprobe_gw/serial/uart.py
"""8N1 UART 送受信コア。ボーレートは実行時の分周比（1 ビットあたりのクロック数）で指定する。"""

from amaranth import Module, Signal
from amaranth.lib import stream, wiring
from amaranth.lib.cdc import FFSynchronizer
from amaranth.lib.wiring import In, Out

__all__ = ["UartRx", "UartTx"]


class UartTx(wiring.Component):
    def __init__(self, *, divisor_bits: int = 16):
        super().__init__({
            "data": In(stream.Signature(8)),
            "divisor": In(divisor_bits),
            "tx": Out(1, init=1),
            "busy": Out(1),
        })
        self._divisor_bits = divisor_bits

    def elaborate(self, platform):
        m = Module()
        shreg = Signal(10)        # [停止 1][データ 8][開始 0] を LSB から送出
        bits_left = Signal(range(11))
        timer = Signal(self._divisor_bits)

        m.d.comb += [
            self.busy.eq(bits_left != 0),
            self.data.ready.eq(bits_left == 0),
        ]
        with m.If(bits_left == 0):
            with m.If(self.data.valid):
                m.d.sync += [
                    shreg.eq(Cat(0, self.data.payload, 1)),
                    bits_left.eq(10),
                    timer.eq(0),
                ]
        with m.Else():
            m.d.sync += self.tx.eq(shreg[0])
            with m.If(timer == self.divisor - 1):
                m.d.sync += [
                    timer.eq(0),
                    shreg.eq(shreg >> 1),
                    bits_left.eq(bits_left - 1),
                ]
            with m.Else():
                m.d.sync += timer.eq(timer + 1)
        with m.If(bits_left == 0):
            m.d.sync += self.tx.eq(1)
        return m


class UartRx(wiring.Component):
    def __init__(self, *, divisor_bits: int = 16):
        super().__init__({
            "data": Out(stream.Signature(8)),
            "divisor": In(divisor_bits),
            "rx": In(1),
            "frame_error": Out(1),
            "overrun": Out(1),
        })
        self._divisor_bits = divisor_bits

    def elaborate(self, platform):
        m = Module()
        rx = Signal(init=1)
        m.submodules.sync_rx = FFSynchronizer(self.rx, rx, init=1)

        timer = Signal(self._divisor_bits)
        bit_index = Signal(range(9))
        shreg = Signal(8)

        m.d.sync += [self.frame_error.eq(0), self.overrun.eq(0)]
        with m.If(self.data.ready):
            m.d.sync += self.data.valid.eq(0)

        with m.FSM():
            with m.State("IDLE"):
                with m.If(~rx):
                    m.d.sync += timer.eq(0)
                    m.next = "START"
            with m.State("START"):
                # 開始ビットの中央で再確認（それ未満のグリッチは捨てる）
                with m.If(timer == (self.divisor >> 1) - 1):
                    m.d.sync += [timer.eq(0), bit_index.eq(0)]
                    with m.If(rx):
                        m.next = "IDLE"
                    with m.Else():
                        m.next = "DATA"
                with m.Else():
                    m.d.sync += timer.eq(timer + 1)
            with m.State("DATA"):
                with m.If(timer == self.divisor - 1):
                    m.d.sync += [timer.eq(0), shreg.eq(Cat(shreg[1:], rx)),
                                 bit_index.eq(bit_index + 1)]
                    with m.If(bit_index == 7):
                        m.next = "STOP"
                with m.Else():
                    m.d.sync += timer.eq(timer + 1)
            with m.State("STOP"):
                with m.If(timer == self.divisor - 1):
                    m.d.sync += timer.eq(0)
                    with m.If(rx):
                        with m.If(self.data.valid & ~self.data.ready):
                            m.d.sync += self.overrun.eq(1)
                        m.d.sync += [self.data.payload.eq(shreg), self.data.valid.eq(1)]
                        m.next = "IDLE"
                    with m.Else():
                        m.d.sync += self.frame_error.eq(1)
                        m.next = "BREAK"
                with m.Else():
                    m.d.sync += timer.eq(timer + 1)
            with m.State("BREAK"):
                # 線が high に戻るまで次の開始ビットを探さない
                with m.If(rx):
                    m.next = "IDLE"
        return m
```

（`Cat` を `from amaranth import Cat, Module, Signal` で import すること。）

- [ ] **Step 4: パス確認** — `pytest gateware/tests/test_serial_uart.py -q` → `4 passed`
- [ ] **Step 5: Commit** — `git commit -m "feat(serial): 実行時分周比指定の 8N1 UART 送受信コア"`

---

### Task 10: Bringup デザインと CLI

**Files:**
- Create: `gateware/fprobe_gw/designs/__init__.py`, `gateware/fprobe_gw/designs/bringup.py`, `gateware/fprobe_gw/cli.py`, `gateware/fprobe_gw/__main__.py`
- Test: `gateware/tests/test_designs_bringup.py`, `gateware/tests/test_cli.py`

**Interfaces:**
- Consumes: Task 8 `TangPrimer20KDockPlatform`, `PrimerClockReset`; Task 9 `UartTx`, `UartRx`; Task 6 `GowinBuilder`; Task 5 `GowinProgrammer`; Task 2 `GowinToolchain`
- Produces:
  - `class BringupCore(wiring.Component)`（ボード非依存・シミュレーション可能な本体）: `__init__(self, *, sync_hz: int, baud: int = 115200)`; ポート `rx: In(1)`, `tx: Out(1)`, `heartbeat: Out(1)`（約 1 Hz）, `activity: Out(1)`（受信毎トグル）。
    - 振る舞い: `?` を受信すると `fprobe-bringup sync_hz=<10進> rev=<rev>\r\n` を返す。他のバイトはそのままエコー。
  - `class Bringup(Elaboratable)`: ボードに `BringupCore` を結線（uart 0, led 0..2）。
  - `DESIGNS: dict[str, Callable[[platform], Elaboratable]]`（`"bringup"` を登録）
  - CLI: `python -m fprobe_gw build bringup [--program] [--persistent] [--build-dir build/bringup]`, `python -m fprobe_gw program <file.fs> [--persistent]`, `python -m fprobe_gw detect`（FTDI 列挙と選択結果の表示。JTAG アクセスなし）

- [ ] **Step 1: 失敗するテストを書く**

```python
# gateware/tests/test_designs_bringup.py
from amaranth.sim import Simulator

from fprobe_gw.designs.bringup import BringupCore
from tests.models.uart import uart_bits

SYNC_HZ = 1_000_000
BAUD = 125_000          # 分周比 8
DIV = SYNC_HZ // BAUD


def simulate(send: bytes, cycles_after: int):
    dut = BringupCore(sync_hz=SYNC_HZ, baud=BAUD, revision=3714)
    line = []

    async def tb(ctx):
        ctx.set(dut.rx, 1)
        await ctx.tick().repeat(10)
        for byte in send:
            for b in uart_bits(byte):
                ctx.set(dut.rx, b)
                await ctx.tick().repeat(DIV)
        await ctx.tick().repeat(cycles_after)

    async def monitor(ctx):
        async for _, _, tx in ctx.tick().sample(dut.tx):
            line.append(tx)

    sim = Simulator(dut)
    sim.add_clock(1 / SYNC_HZ)
    sim.add_process(monitor)
    sim.add_testbench(tb)
    sim.run()
    return decode(line, DIV)


def decode(line, div):
    out, i = [], 0
    while i < len(line):
        if line[i] == 0:
            mid = i + div // 2
            bits = [line[mid + div * (k + 1)] for k in range(8)]
            out.append(sum(b << k for k, b in enumerate(bits)))
            i = mid + div * 9
        else:
            i += 1
    return bytes(out)


def test_echoes_bytes():
    assert simulate(b"hi", cycles_after=DIV * 30) == b"hi"


def test_question_mark_reports_frequency():
    reply = simulate(b"?", cycles_after=DIV * 10 * 60)
    assert reply == b"fprobe-bringup sync_hz=1000000 rev=3714\r\n"
```

```python
# gateware/tests/test_cli.py
from fprobe_gw import cli
from fprobe_gw.designs import DESIGNS


def test_bringup_is_registered():
    assert "bringup" in DESIGNS


def test_parser_accepts_build_and_program():
    p = cli.make_parser()
    a = p.parse_args(["build", "bringup", "--program"])
    assert a.design == "bringup" and a.program and not a.persistent
    a = p.parse_args(["program", "x.fs", "--persistent"])
    assert a.bitstream == "x.fs" and a.persistent
```

- [ ] **Step 2: 失敗を確認**
- [ ] **Step 3: 実装**

```python
# gateware/fprobe_gw/designs/bringup.py
"""ボード立ち上げ確認用デザイン: LED 心拍・UART エコー・クロック周波数の申告。"""

from amaranth import Array, Const, Elaboratable, Module, Signal
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out

from ..platform.primer20k import PrimerClockReset
from ..serial.uart import UartRx, UartTx

__all__ = ["Bringup", "BringupCore"]


class BringupCore(wiring.Component):
    def __init__(self, *, sync_hz: int, baud: int = 115200, revision: int = 0):
        self.sync_hz = sync_hz
        self.divisor = round(sync_hz / baud)
        self.banner = f"fprobe-bringup sync_hz={sync_hz} rev={revision}\r\n".encode()
        super().__init__({"rx": In(1, init=1), "tx": Out(1, init=1),
                          "heartbeat": Out(1), "activity": Out(1)})

    def elaborate(self, platform):
        m = Module()
        m.submodules.urx = urx = UartRx()
        m.submodules.utx = utx = UartTx()
        m.d.comb += [urx.divisor.eq(self.divisor), utx.divisor.eq(self.divisor),
                     urx.rx.eq(self.rx), self.tx.eq(utx.tx)]

        half_second = Signal(range(self.sync_hz // 2))
        with m.If(half_second == self.sync_hz // 2 - 1):
            m.d.sync += [half_second.eq(0), self.heartbeat.eq(~self.heartbeat)]
        with m.Else():
            m.d.sync += half_second.eq(half_second + 1)

        banner = Array(Const(b, 8) for b in self.banner)
        index = Signal(range(len(self.banner) + 1))
        with m.FSM():
            with m.State("IDLE"):
                m.d.comb += urx.data.ready.eq(1)
                with m.If(urx.data.valid):
                    m.d.sync += self.activity.eq(~self.activity)
                    with m.If(urx.data.payload == ord("?")):
                        m.d.sync += index.eq(0)
                        m.next = "BANNER"
                    with m.Else():
                        m.d.sync += utx.data.payload.eq(urx.data.payload)
                        m.next = "ECHO"
            with m.State("ECHO"):
                m.d.comb += utx.data.valid.eq(1)
                with m.If(utx.data.ready):
                    m.next = "IDLE"
            with m.State("BANNER"):
                m.d.comb += [utx.data.valid.eq(1), utx.data.payload.eq(banner[index])]
                with m.If(utx.data.ready):
                    m.d.sync += index.eq(index + 1)
                    with m.If(index == len(self.banner) - 1):
                        m.next = "IDLE"
        return m


class Bringup(Elaboratable):
    def elaborate(self, platform):
        m = Module()
        m.submodules.crg = PrimerClockReset()
        sync_hz = round(platform.sync_solution().fout_mhz * 1e6)
        m.submodules.core = core = BringupCore(sync_hz=sync_hz, revision=platform.revision)
        uart = platform.request("uart", 0)
        leds = [platform.request("led", i) for i in range(3)]
        m.d.comb += [
            core.rx.eq(uart.rx.i), uart.tx.o.eq(core.tx),
            leds[0].o.eq(core.heartbeat), leds[1].o.eq(1), leds[2].o.eq(core.activity),
        ]
        return m
```

注意: ECHO 状態で `utx.data.payload` は `sync` で保持した値をそのまま使うため、`utx.data.payload` の駆動を `m.d.sync`（ECHO 用）と `m.d.comb`（BANNER 用）で混在させてはならない。実装では専用の `tx_byte = Signal(8)` を設け、`m.d.comb += utx.data.payload.eq(Mux(banner_state, banner[index], tx_byte))` の形に統一する（上記コードはこの点を修正して書くこと）。

```python
# gateware/fprobe_gw/designs/__init__.py
"""ビルド可能なトップレベルデザインのレジストリ。"""

from .bringup import Bringup

DESIGNS = {
    "bringup": Bringup,
}
```

```python
# gateware/fprobe_gw/cli.py
"""fprobe ゲートウェアの開発用 CLI。"""

import argparse
import sys
from pathlib import Path

from .designs import DESIGNS
from .platform.primer20k import TangPrimer20KDockPlatform
from .toolchain.build import BuildError, GowinBuilder
from .toolchain.ftdi import list_ftdi_devices
from .toolchain.gowin import GowinToolchain
from .toolchain.programmer import (GowinProgrammer, ProgrammerError, UnsafeTargetError,
                                   select_jtag_location)

PLATFORMS = {"primer20k": TangPrimer20KDockPlatform}


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m fprobe_gw")
    p.add_argument("--board", choices=sorted(PLATFORMS), default="primer20k")
    sub = p.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="デザインをビルドする")
    b.add_argument("design", choices=sorted(DESIGNS))
    b.add_argument("--build-dir")
    b.add_argument("--program", action="store_true", help="ビルド後に書き込む")
    b.add_argument("--persistent", action="store_true", help="外部 Flash に書き込む")
    g = sub.add_parser("program", help="ビットストリームを書き込む")
    g.add_argument("bitstream")
    g.add_argument("--persistent", action="store_true")
    sub.add_parser("detect", help="FTDI 機器の列挙と書き込み対象の判定（JTAG アクセスなし）")
    return p


def main(argv=None) -> int:
    args = make_parser().parse_args(argv)
    platform_cls = PLATFORMS[args.board]
    try:
        if args.command == "detect":
            devices = list_ftdi_devices()
            for d in devices:
                print(f"[{d.index}] loc={d.location} serial={d.serial!r} desc={d.description!r}"
                      + (" (使用中)" if d.busy else ""))
            print("書き込み対象 USB 位置:", select_jtag_location(devices, platform_cls.jtag_identity))
            return 0
        toolchain = GowinToolchain.locate()
        if args.command == "build":
            build_dir = Path(args.build_dir or f"build/{args.board}/{args.design}")
            result = GowinBuilder(toolchain).build(platform_cls(), DESIGNS[args.design](), build_dir)
            print(result.timing.describe())
            print("ビットストリーム:", result.bitstream)
            bitstream = result.bitstream if args.program else None
        else:
            bitstream = Path(args.bitstream)
        if bitstream is not None:
            GowinProgrammer(toolchain, platform_cls.jtag_identity).program(
                bitstream, persistent=args.persistent)
            print("書き込み完了:", "外部 Flash" if args.persistent else "SRAM")
        return 0
    except (BuildError, ProgrammerError, UnsafeTargetError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
```

```python
# gateware/fprobe_gw/__main__.py
import sys

from .cli import main

sys.exit(main())
```

- [ ] **Step 4: パス確認** — `pytest gateware/tests -q` → 全テストパス
- [ ] **Step 5: Commit** — `git commit -m "feat: bringup デザインと開発用 CLI"`

---

### Task 11: 実機確認（HIL）

**Files:**
- Create: `gateware/tests/hil/__init__.py`, `gateware/tests/hil/test_bringup_hw.py`

**Interfaces:**
- Consumes: CLI（Task 10）、`TangPrimer20KDockPlatform.sync_solution()`
- Produces: `find_primer_console() -> str`（COM ポート名。pyserial の `list_ports` で `serial_number` または `hwid` に `FACTORYAIOT_PRO` を含み、かつインターフェース B のもの）

- [ ] **Step 1: テストを書く**

```python
# gateware/tests/hil/test_bringup_hw.py
"""Primer 20K 実機での bringup 確認。`pytest --hil gateware/tests/hil` で実行。"""

import subprocess
import sys
import time

import pytest
import serial
from serial.tools import list_ports

from fprobe_gw.platform.primer20k import TangPrimer20KDockPlatform

pytestmark = pytest.mark.hil


def find_primer_console() -> str:
    ports = [p for p in list_ports.comports() if "FACTORYAIOT_PRO" in (p.hwid or "").upper()]
    if len(ports) != 1:
        pytest.fail(f"Primer の COM ポートを一意に特定できない: {[p.device for p in ports]}")
    return ports[0].device


@pytest.fixture(scope="module")
def console():
    subprocess.run([sys.executable, "-m", "fprobe_gw", "build", "bringup", "--program"],
                   check=True)
    time.sleep(0.5)
    with serial.Serial(find_primer_console(), 115200, timeout=1) as port:
        port.reset_input_buffer()
        yield port


def test_echo(console):
    console.write(b"fprobe!")
    assert console.read(7) == b"fprobe!"


def test_reported_frequency_matches_pll_solution(console):
    console.reset_input_buffer()
    console.write(b"?")
    line = console.readline().decode()
    expected = round(TangPrimer20KDockPlatform().sync_solution().fout_mhz * 1e6)
    assert line == f"fprobe-bringup sync_hz={expected} rev=3714\r\n"
```

- [ ] **Step 2: 実行** — `.venv/Scripts/python -m pytest --hil gateware/tests/hil -v`。Expected: `2 passed`。
  - 失敗時の切り分け: (a) COM ポートが見つからない → `hwid` を表示して検索条件を実物に合わせる (b) エコーが来ない → UART の TX/RX ピン入れ替えを疑い、回路図 p1/p2 で再確認 (c) 周波数文字列は来るが文字化け → PLL 出力周波数の誤り（ソルバ制約）を疑う。
- [ ] **Step 3: 外部 Flash 書き込み確認（任意）** — `python -m fprobe_gw build bringup --program --persistent` 後、Primer の電源を入れ直して `?` 応答が返ることを確認。`OP_FLASH_PROGRAM` が誤っていれば正しい操作番号に修正しテストを更新。
- [ ] **Step 4: Commit** — `git commit -m "test(hil): Primer 20K 実機での bringup 確認"`

---

## Self-Review 結果

- Spec 対応: §10 安全装置 → Task 4/5、§7 ボード層 → Task 7/8、§8 PMOD → Task 8、§11 M0 → Task 1–6、M1 → Task 7–11。§12「基板リビジョン判定」は、Key_4/SW0 を使わない設計とし、`revision` パラメータ（既定 3714）で吸収（ボタン判定は不要になった）。
- 型の一貫性: `JtagIdentity`（Task 4）を Task 5/8/10 で同名使用。`PllSolution.idiv/fbdiv/odiv` は実分周比で統一し、rPLL パラメータ変換は `GowinRPLL` 内のみ。
- 計画中の既知の修正指示: Task 9 の `Cat` import、Task 10 の `utx.data.payload` 駆動の一本化は、実装時に必ず反映する。
