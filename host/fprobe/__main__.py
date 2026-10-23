"""fprobe の CLI。

    fprobe info                                   ケイパビリティ（版を含む）・タイムベース・個体名
    fprobe name set NAME | list | remove NAME     個体名（~/.fprobe/probes.json）。以後 --name NAME で指定できる
    fprobe la out.vcd [--rate 25e6] [--pretrigger 1024] [--rise CH ...] [--fall CH ...] [--mask M --value V]
    fprobe la out.vcd --stream SECONDS [--rate 12.5e6] [--rle]      連続ストリーミング（トリガなし）
    fprobe run firmware.elf [--target stm32f401xe] [--load auto|ram|flash|none] [--timeout 60] [--junit out.xml]
                            [--set VAR=VALUE ...] [--rtt|--no-rtt] [--swo BAUD] [--pass REGEX] [--fail REGEX]
        書き込み → 実行 → 出力（RTT・セミホスティング・SWO）→ 終わり方で判定（CI 用。終了コードは fprobe.run）
"""

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from . import probe_names
from .device import Device
from .la import DeepAnalyzer, LogicAnalyzer, write_vcd


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="fprobe")
    parser.add_argument("--serial", help="USB シリアル番号（複数台あるとき）")
    parser.add_argument("--name", help="個体名（fprobe name set で付けた名前）。--serial / --port / --eth の代わり")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("info", help="ケイパビリティ（版を含む）・タイムベース・個体名")
    names = commands.add_parser("name", help="個体名を付ける・一覧・外す（つないだ機器のシリアル番号に結びつける）")
    names_sub = names.add_subparsers(dest="action", required=True)
    names_set = names_sub.add_parser("set", help="つないでいる機器（--serial / --port / --eth で選ぶ）に名前を付ける")
    names_set.add_argument("new_name", metavar="NAME")
    names_sub.add_parser("list", help="付けた名前の一覧")
    names_remove = names_sub.add_parser("remove", help="名前を外す")
    names_remove.add_argument("old_name", metavar="NAME")
    la = commands.add_parser("la", help="ロジックアナライザで捕捉して VCD に書く")
    la.add_argument("vcd")
    la.add_argument("--rate", type=float, help="サンプルレート [Hz]（省略時は最大）")
    la.add_argument("--pretrigger", type=int, default=1024)
    la.add_argument("--rise", type=int, nargs="*", default=[], metavar="CH")
    la.add_argument("--fall", type=int, nargs="*", default=[], metavar="CH")
    la.add_argument("--mask", type=lambda s: int(s, 0), default=0)
    la.add_argument("--value", type=lambda s: int(s, 0), default=0)
    la.add_argument("--timeout", type=float, default=10.0)
    la.add_argument("--stream", type=float, metavar="SECONDS", help="トリガを使わず SECONDS 秒流し続けて VCD に書く")
    la.add_argument("--rle", action="store_true", help="--stream を変化点圧縮で")
    _add_run(commands)
    merge = commands.add_parser("merge", help="2 つの記録を、子の記録の同期の組で親の時間軸 1 本に合成する")
    merge.add_argument("parent", help="親のボードの記録")
    merge.add_argument("child", help="子のボードの記録（同期の組 stream sync を含む）")
    merge.add_argument("--child-name", required=True, help="子の出来事の stream に付ける名前（<名前>.<stream>）")
    merge.add_argument("--out", required=True, help="合成した新しい記録")
    merge.add_argument("--cable-delay-ns", type=float, help="配線の遅れ（測った値を明示して渡す。省略時は足さない）")
    merge.add_argument("--gap-ms", type=float, help="組の間がこれを超えたら区間を分ける（省略時は分けない）")
    parser.add_argument("--port", help="UART のホストリンクの COM ポート（run のみ）")
    parser.add_argument("--eth", help="Ethernet のホストリンクの IP アドレス（run のみ）")
    args = parser.parse_args(argv)
    try:
        probe_names.apply_to_args(args)
    except probe_names.ProbeNameError as e:
        parser.error(str(e))

    if args.command == "run":
        raise SystemExit(_run(args))
    if args.command == "merge":
        raise SystemExit(_merge(args))
    if args.command == "name" and args.action != "set":
        raise SystemExit(_names(args))
    if args.command == "name":
        raise SystemExit(_name_set(args))

    with Device.open(args.serial) as device:
        if args.command == "info":
            caps = device.capabilities()
            info = {**vars(caps), "time": device.time(), "name": _usb_name(args.serial),
                    "version": caps.build.describe() if caps.build is not None else None}
            print(json.dumps(info, indent=2, ensure_ascii=False, default=_json_default))
        elif args.stream is not None:
            s = DeepAnalyzer(device).stream_to_vcd(args.vcd, args.stream, rate_hz=args.rate, rle=args.rle)
            print(f"{s.samples} サンプル（{s.duration_s:.3f} 秒分）を {s.elapsed_s:.2f} 秒で受信、{s.mb_per_s:.1f} MB/s"
                  f"{'、溢れあり（ホストが遅れた。それまでの分は正しい）' if s.overrun else ''} → {args.vcd}")
        else:
            cap = LogicAnalyzer(device).capture(
                timeout_s=args.timeout, rate_hz=args.rate, pretrigger=args.pretrigger, mask=args.mask,
                value=args.value, rise=sum(1 << c for c in args.rise), fall=sum(1 << c for c in args.fall))
            write_vcd(cap, args.vcd)
            print(f"{len(cap.samples)} サンプル（{cap.sample_period_s * 1e9:g} ns 間隔）、トリガ {cap.trigger} → {args.vcd}")


def _json_default(value):
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    return sorted(value)


def _usb_name(serial: str | None) -> str | None:
    return probe_names.name_of({"link": "usb", "address": serial}) if serial else None


def _names(args) -> int:
    if args.action == "list":
        for name, device in probe_names.load().items():
            print(f"{name}\t{device['link']}\t{device['address']}")
        return 0
    try:
        probe_names.remove(args.old_name)
    except probe_names.ProbeNameError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    return 0


def _name_set(args) -> int:
    """つないでいる機器を開いて実際の番地（シリアル番号を省いたときも）を確かめてから名前を付ける。"""
    from .session import Session
    if args.eth:
        session = Session.open_eth(args.eth)
    elif args.port:
        session = Session.open_serial(args.port)
    else:
        session = Session.open(args.serial)
    try:
        device = session.address
    finally:
        session.close()
    try:
        probe_names.set_name(args.new_name, device)
    except probe_names.ProbeNameError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    print(f"{args.new_name} = {device['link']} {device['address']}")
    return 0


def _add_run(commands) -> None:
    run = commands.add_parser("run", help="ファームを書き込んで走らせ、出力を流し、終わり方で合否を返す（CI 用）",
                              description="終了コード: 0 合格 / ターゲットの SYS_EXIT の値 / 1 試験の失敗 / 3 プローブ・"
                                          "書き込みの失敗 / 124 時間切れ / 125 故障・思いがけない停止")
    run.add_argument("elf", type=Path)
    run.add_argument("--target", default="cortex_m", help="pyOCD のターゲット名（例 stm32f401xe）")
    run.add_argument("--frequency", type=float, default=4e6, help="SWD の周波数 [Hz]")
    run.add_argument("--connect-mode", default="halt", choices=["halt", "attach", "under-reset"])
    run.add_argument("--load", default="auto", choices=["auto", "ram", "flash", "none"],
                     help="auto: ELF がすべて RAM なら ram、そうでなければ flash。none: 書かない")
    run.add_argument("--no-reset", action="store_true", help="--load flash / none でもリセットしない")
    run.add_argument("--timeout", type=float, default=60.0, help="時間切れ [秒]")
    run.add_argument("--set", action="append", default=[], metavar="EXPR=VALUE",
                     help="走らせる前に書く変数（C の式。--load flash では起動のコードが .data を上書きすることに注意）")
    run.add_argument("--set-at", metavar="LOCATION",
                     help="--set を書く場所（関数名・ファイル:行。起動のコードの後。省略時は main があれば main）")
    run.add_argument("--no-catch-faults", action="store_true", help="故障の入口でコアを止めない（ベクタキャッチを使わない）")
    rtt = run.add_mutually_exclusive_group()
    rtt.add_argument("--rtt", dest="rtt", action="store_true", default=None, help="RTT を読む（既定: ELF に _SEGGER_RTT があれば）")
    rtt.add_argument("--no-rtt", dest="rtt", action="store_false")
    run.add_argument("--rtt-address", type=lambda s: int(s, 0), help="RTT の制御ブロックの番地")
    run.add_argument("--swo", type=int, metavar="BAUD", help="SWO を受ける（ITM の設定はファームで）")
    run.add_argument("--swo-port", type=int, default=0, help="SWO の ITM のポート")
    run.add_argument("--pass", dest="pass_pattern", metavar="REGEX", help="この行が出たら合格で終える")
    run.add_argument("--fail", dest="fail_pattern", metavar="REGEX", help="この行が出たら不合格で終える")
    run.add_argument("--wait-halt", action="store_true", help="試験のまとめの行が出ても、止まる・終わるまで待つ")
    run.add_argument("--junit", type=Path, help="JUnit XML の出力先")
    run.add_argument("--out", type=Path, default=Path("fprobe-run"),
                     help="result.json・output.log・故障時のスナップショット等の置き場")
    run.add_argument("--no-snapshot", action="store_true", help="故障・時間切れでスナップショットを残さない")


def _run(args) -> int:
    from . import run as run_mod
    values = {}
    for item in args.set:
        name, sep, value = item.partition("=")
        if not sep:
            raise SystemExit(f"--set は EXPR=VALUE（{item!r}）")
        values[name.strip()] = value.strip()
    options = run_mod.RunOptions(
        elf=args.elf, out_dir=args.out, target=args.target, frequency_hz=int(args.frequency),
        connect_mode=args.connect_mode, load=args.load, reset=not args.no_reset, set_values=values,
        set_at=args.set_at, catch_faults=not args.no_catch_faults,
        timeout_s=args.timeout, rtt=args.rtt, rtt_address=args.rtt_address, swo_baud=args.swo, swo_port=args.swo_port,
        pass_pattern=args.pass_pattern, fail_pattern=args.fail_pattern, stop_on_summary=not args.wait_halt,
        junit=args.junit, snapshot=not args.no_snapshot)

    def echo(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()
    result = run_mod.run_firmware(options, {"serial": args.serial, "port": args.port, "eth": args.eth}, echo)
    summary = getattr(result, "reason", "")
    if summary:
        print(f"\n[fprobe run] {result.outcome}: {summary}（終了コード {result.exit_code}）", file=sys.stderr)
    return result.exit_code


def _merge(args) -> int:
    from .merge import merge
    from .recording import Recording
    def ticks(value: float | None, unit: float, recording: str) -> int | None:
        """value × unit 秒を、その記録のタイムベースの刻みに（配線の遅れは親の時刻に足す、区間は子の時刻で比べる）。"""
        if value is None:
            return None
        r = Recording.open(Path(recording))
        try:
            return round(value * unit * r.meta["timebase_hz"])
        finally:
            r.close()

    result = merge(Path(args.parent), Path(args.child), Path(args.out), child_name=args.child_name,
                   cable_delay_ticks=ticks(args.cable_delay_ns, 1e-9, args.parent),
                   gap_ticks=ticks(args.gap_ms, 1e-3, args.child))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    main()
