"""Primer の構成（ビットストリーム）の選択と書き替え（B22）。

Primer 20K に全部の機能は入らないので、USB の製品デザインは 3 構成ある（ゲートウェアの PROTOCOL_DESIGNS・
FEATURE_DESIGNS が正。`fprobe_gw gen` がホスト定数 DESIGN_FOR_SWD / DESIGN_FOR_JTAG / DESIGN_FOR_LA_INPUTS /
DESIGN_FOR_LA_STREAM_EVENTS に出す）:

- 標準（DESIGN_FOR_SWD = "fprobe"）: SWD・DAP サンプラ・故障注入・すべての解析。JTAG なし。
- JTAG（DESIGN_FOR_JTAG = "fprobe_jtag"）: JTAG と SWD。サンプラ・故障注入なし。
- ロジアナの専用の入力（DESIGN_FOR_LA_INPUTS = "fprobe_la"、U22）: SWD と J14 の専用の入力 6 本。サンプラ・故障注入・
  JTAG なし。機能のために選ぶ構成なので、SWD の接続では標準に戻さない（明示の probe_design_switch で選ぶ）。
  ロジアナの流し続けにイベントも載せる（DESIGN_FOR_LA_STREAM_EVENTS、R1）のもこの構成。

ターゲットが要る線（プロトコル）の構成でなければ、書き込み済みのビットストリームを Primer に書き込んでから続ける。
書き込みは既存の安全な道具 `python -m fprobe_gw program`（Primer の USB シリアル番号でポートを引き、IDCODE を
照合してから SRAM に書く）を子プロセスで呼ぶ。この組のデザインのときだけ書き替える（Ethernet 版等には触らない）。

設定:
- ビットストリームの置き場所: `<root>/<design>/top.fs`。root の既定は `<リポジトリ>/gateware/build`、環境変数
  FPROBE_BITSTREAMS かデーモン / MCP の --bitstreams で変える。
- 自動の書き替え: 既定で有効。環境変数 FPROBE_AUTO_SWITCH=0 で切る（要る構成でなければ誤りにする）。

仕様: docs/superpowers/specs/2026-10-18-b22-fpga-area-design.md §5
"""

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from . import generated as g

__all__ = ["BOARD", "DEFAULT_ROOT", "DesignSwitchError", "FEATURES", "PROTOCOLS", "SWITCHABLE", "auto_switch_enabled",
           "bitstream_path", "bitstream_root", "built_id", "design_for", "program", "protocol_for_target",
           "protocols_of", "wanted_design"]

PROTOCOLS = {"swd": g.DESIGN_FOR_SWD, "jtag": g.DESIGN_FOR_JTAG}     # 線 → その線に使う構成
FEATURES = {"la_inputs": g.DESIGN_FOR_LA_INPUTS,                      # 機能 → その機能のために選ぶ構成（U22）
            "la_stream_events": g.DESIGN_FOR_LA_STREAM_EVENTS}          # （R1）
SWITCHABLE = frozenset({*PROTOCOLS.values(), *FEATURES.values()})      # 書き替えてよいデザイン（Primer の USB 版）
BOARD = "primer20k"                                                    # fprobe_gw program --board
DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "gateware" / "build"
ENV_ROOT = "FPROBE_BITSTREAMS"
ENV_AUTO = "FPROBE_AUTO_SWITCH"
PROGRAM_TIMEOUT_S = 600                    # fprobe_gw program（programmer_cli の上限 300 秒 × IDCODE と書き込み）


class DesignSwitchError(RuntimeError):
    """要る構成に書き替えられない（設定で切ってある・組の外のデザイン・ビットストリームが無い・書き込みの失敗）。"""


def bitstream_root(root: str | Path | None = None) -> Path:
    """ビットストリームの置き場所（引数 → 環境変数 FPROBE_BITSTREAMS → 既定）。"""
    return Path(root or os.environ.get(ENV_ROOT) or DEFAULT_ROOT)


def bitstream_path(design: str, root: str | Path | None = None) -> Path:
    return bitstream_root(root) / design / "top.fs"


def built_id(design: str, root: str | Path | None = None) -> str | None:
    """ビルド済みのビットストリームの版（成功したビルドの印 inputs.sha256 の先頭 16 桁。ケイパビリティの build.id と
    同じ。U49）。無ければ None。"""
    stamp = bitstream_root(root) / design / "inputs.sha256"
    try:
        digest = stamp.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return digest[:16].lower() if len(digest) >= 16 else None


def auto_switch_enabled() -> bool:
    return os.environ.get(ENV_AUTO, "1").strip().lower() not in ("0", "false", "no", "off")


def design_for(protocol: str) -> str:
    try:
        return PROTOCOLS[protocol]
    except KeyError:
        raise ValueError(f"線は {sorted(PROTOCOLS)} のどれか: {protocol!r}") from None


def protocols_of(design: str) -> list[str]:
    """design を選ぶ線（機能の構成は SWD で使う。JTAG が要る機能の構成は無い）。"""
    lines = [p for p, d in PROTOCOLS.items() if d == design]
    return lines or (["swd"] if design in FEATURES.values() else [])


def protocol_for_target(target: str | None, jtag_chain=None) -> str:
    """target_connect が要る線: jtag_chain がある、または target が JTAG で直接つなぐ部品の表
    （fprobe.arm_a_target.ARM_A_PARTS。Cortex-A・ARM11）の名前と同じなら JTAG。名前の部分一致では推さない。"""
    from .arm_a_target import arm_a_part
    if jtag_chain is not None or arm_a_part(target) is not None:
        return "jtag"
    return "swd"


def wanted_design(caps, protocol: str) -> str | None:
    """protocol の線を使うために書き替える先のデザイン。書き替えが要らなければ None。
    JTAG は今の構成に JTAG があれば書き替えない。SWD はどの構成にもあるが、線のために選んだ構成（JTAG）からは標準
    （サンプラ・故障注入がある）に戻す。機能のために選んだ構成（FEATURES。専用の入力など）はそのまま使う。
    組の外のデザイン（Ethernet 版等）は書き替えない: その線が無ければ DesignSwitchError。"""
    design = design_for(protocol)
    if caps.design == design or (protocol == "jtag" and caps.jtag):
        return None
    if protocol == "swd" and caps.design in FEATURES.values():
        return None
    if caps.design not in SWITCHABLE:
        if protocol == "jtag" and not getattr(caps, "jtag", True):
            raise DesignSwitchError(f"デザイン {caps.design!r} に JTAG が無く、書き替えの対象でもない")
        return None
    return design


def program(bitstream: Path, *, run: Callable = subprocess.run) -> str:
    """Primer に bitstream を書き込む（SRAM）。fprobe_gw program と同じ安全な手順。出力を返す。"""
    command = [sys.executable, "-m", "fprobe_gw", "program", str(bitstream), "--board", BOARD]
    try:
        result = run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                     env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=PROGRAM_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        raise DesignSwitchError(f"書き込みが {PROGRAM_TIMEOUT_S} 秒で終わらない: {bitstream}") from e
    except OSError as e:
        raise DesignSwitchError(f"書き込みの道具（python -m fprobe_gw）を起動できない: {e}") from e
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        raise DesignSwitchError(f"書き込みに失敗（{bitstream}）: {output}")
    return output
