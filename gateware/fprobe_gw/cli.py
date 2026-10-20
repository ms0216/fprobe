"""fprobe ゲートウェアの開発用 CLI（`python -m fprobe_gw`）。"""

import argparse
import sys
from pathlib import Path

from .boards.primer20k import PRIMER20K
from .boards.pynq_z2 import PYNQ_Z2
from .designs import DESIGNS
from .toolchain.build import INPUTS_STAMP, BuildError, GowinBuilder
from .toolchain.ftdi import FtdiUnavailable, list_ftdi_devices, usb_port_chain
from .toolchain.gowin import GowinToolchain, ToolchainNotFound
from .toolchain.vivado import VivadoToolchain
from .toolchain.vivado_build import VivadoBuilder
from .toolchain.pynq_loader import PynqError, PynqLoader
from .toolchain.programmer import (GowinProgrammer, ProgrammerError, UnsafeTargetError,
                                   select_jtag_location)
from .generate import write_all
from . import pynq_setup

# ビルドの既定の置き場所は gateware/build（カレントに依らない: 別の場所から実行すると作り直しになっていた）
BUILD_ROOT = Path(__file__).resolve().parents[1] / "build"

__all__ = ["BOARDS", "main", "make_parser"]

BOARDS = {"primer20k": PRIMER20K, "pynq_z2": PYNQ_Z2}

_EXPECTED_ERRORS = (BuildError, PynqError, FtdiUnavailable, ProgrammerError, ToolchainNotFound,
                    UnsafeTargetError, FileNotFoundError)


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m fprobe_gw")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="デザインをビルドする（対象ボードはデザインが決める）")
    b.add_argument("design", choices=sorted(DESIGNS))
    b.add_argument("--build-dir", help="既定: build/<design>")
    b.add_argument("--single", action="store_true",
                   help="配置を変えた版を並列にビルドせず、デザインの設定で 1 回だけビルドする")
    b.add_argument("--program", action="store_true", help="ビルド後に書き込む")
    b.add_argument("--persistent", action="store_true", help="SRAM ではなく外部 Flash に書き込む")

    g = sub.add_parser("program", help="ビットストリームを書き込む")
    g.add_argument("bitstream")
    g.add_argument("--board", choices=sorted(BOARDS), default="primer20k")
    g.add_argument("--persistent", action="store_true", help="SRAM ではなく外部 Flash に書き込む")
    g.add_argument("--relay", action="store_true",
                   help="PYNQ-Z2: zynq_frame_link を持つ fprobe の回路（書いた後に PS の中継を起動する）")
    g.add_argument("--unverified", action="store_true",
                   help="成功したビルドの印（隣の inputs.sha256）が無いビットストリームも書く（外で作ったもの等）")

    d = sub.add_parser("detect", help="FTDI 機器の列挙と書き込み対象の判定（JTAG アクセスなし）")
    d.add_argument("--board", choices=sorted(BOARDS), default="primer20k")

    q = sub.add_parser("pynq", help="PYNQ-Z2 の上の中継（fprobe-relay）の据え付け・状態")
    q.add_argument("action", choices=("install", "status", "reserve", "unreserve"),
                   help="reserve / unreserve: P3 の深いバッファの DDR の予約を置く / 外して、PYNQ を再起動する")

    sub.add_parser("gen", help="生成物（USB ディスクリプタ ROM・CSR・ホスト定数・レジスタ表）を再生成する")
    return p


def _detect(board) -> None:
    if board.jtag_identity is None:
        raise BuildError(f"{board.name} は Gowin の書き込み器の自動判定の対象でない（JTAG は Vivado の hw_server で"
                         "シリアル 1234-TUL の口を選ぶ）")
    devices = list_ftdi_devices()
    for d in devices:
        state = "（他プロセス使用中）" if d.busy else ""
        print(f"[{d.index}] loc={d.location} serial={d.serial!r} desc={d.description!r}{state}")
    identity = board.jtag_identity
    chain = usb_port_chain(identity.usb_id, identity.usb_serial)
    print(f"{identity.usb_serial} の USB ポート経路: {'-'.join(map(str, chain))}")
    location = select_jtag_location(devices, identity, chain)
    print(f"書き込み対象: {board.name} USB 位置 {location}")


def _build_vivado(args) -> int:
    design = DESIGNS[args.design]
    if args.persistent:
        raise BuildError("PYNQ-Z2 の起動時の回路の置き換え（--persistent）は未対応（JTAG で SRAM に書くだけ）")
    build_dir = Path(args.build_dir or BUILD_ROOT / design.name)
    builder = VivadoBuilder(VivadoToolchain.locate())
    result = (builder.build(design.project(), build_dir) if args.single
              else builder.build_best(design.project(), build_dir))
    print(result.timing.describe())
    print(f"ビットストリーム: {result.bitstream}")
    if args.program:
        _program_pynq(result.bitstream, relay=design.capabilities is not None)
    return 0


def _program_pynq(bitstream: Path, *, relay: bool) -> None:
    stamp = bitstream.parent / INPUTS_STAMP
    if relay and not stamp.is_file():
        # 中継の印の id はビルドの印から取る。無いと書いた後に fprobe-pl が拒むので、書く前に断る
        raise BuildError(f"中継を起動するには {bitstream} の隣にビルドの印 {INPUTS_STAMP} が要る")
    PynqLoader(VivadoToolchain.locate()).program(
        bitstream, relay=relay, design_id=stamp.read_text(encoding="utf-8")[:16] if stamp.is_file() else "")
    print(f"書き込み完了（PYNQ-Z2、JTAG。中継: {'起動' if relay else '止めたまま'}）")


def main(argv=None) -> int:
    args = make_parser().parse_args(argv)
    try:
        if args.command == "detect":
            _detect(BOARDS[args.board])
            return 0
        if args.command == "pynq":
            loader = PynqLoader(VivadoToolchain.locate())
            if args.action == "install":
                pynq_setup.install(loader)
                print("据え付け完了（fprobe-relay・fprobe-pl・サービス）")
            elif args.action in ("reserve", "unreserve"):
                pynq_setup.reserve(loader, undo=args.action == "unreserve")
                print("PYNQ を再起動した。起動の後、PL は未構成: fprobe の回路を書き直すこと")
            else:
                text = pynq_setup.status(loader)       # PYNQ の日本語のログ（cp932 で出せない文字は置き換える）
                print(text.encode(sys.stdout.encoding or "utf-8", "replace").decode(sys.stdout.encoding or "utf-8"))
            return 0
        if args.command == "gen":
            written = write_all()
            for path in written:
                print(f"生成: {path}")
            if not written:
                print("生成物は最新")
            return 0

        if args.command == "build" and DESIGNS[args.design].board.toolchain == "vivado":
            return _build_vivado(args)
        toolchain = GowinToolchain.locate() if BOARDS.get(getattr(args, "board", ""), PRIMER20K).toolchain == "gowin" else None
        if args.command == "build":
            design = DESIGNS[args.design]
            build_dir = Path(args.build_dir or BUILD_ROOT / design.name)
            builder = GowinBuilder(toolchain)
            result = (builder.build(design.project(), build_dir) if args.single
                      else builder.build_best(design.project(), build_dir))
            print(result.timing.describe())
            print(f"ビットストリーム: {result.bitstream}")
            if not args.program:
                return 0
            board, bitstream = design.board, result.bitstream
        else:
            board, bitstream = BOARDS[args.board], Path(args.bitstream)
            if not args.unverified and not (bitstream.parent / INPUTS_STAMP).is_file():
                raise BuildError(f"{bitstream} の隣に成功したビルドの印 {INPUTS_STAMP} が無い（タイミング未達・"
                                 "駆動されない配線などで採用されなかった版かもしれない）。外で作ったものなら --unverified")

        if board.toolchain == "vivado":
            if args.persistent:
                raise BuildError("PYNQ-Z2 の起動時の回路の置き換えは未対応（JTAG で SRAM に書くだけ）")
            _program_pynq(bitstream, relay=args.relay)
            return 0
        if args.persistent:
            # B34: 外部 Flash への書き込みの間、書き込み器が FPGA を別の回路で動かし、J12 の端子を駆動する
            # （2026-10-19 に XIAO の行の線が押され、キーが 573 文字届いた。SRAM への書き込みでは起きない）
            print("注意: Flash への書き込みの間は J12 の端子が駆動されることがある。ターゲットの入力につないだ線"
                  "（キーの行など）は外してから書く", file=sys.stderr)
        GowinProgrammer(toolchain, board.jtag_identity).program(
            bitstream, persistent=args.persistent)
        print(f"書き込み完了（{board.name}, {'外部 Flash' if args.persistent else 'SRAM'}）")
        return 0
    except _EXPECTED_ERRORS as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
