"""コマンドライン: `python -m fprobe.linux_halt {attach, ps, threads, halt, resume, status, recover, rescue}`（プロトタイプの `lxdbg` に相当）。

中身は `fprobe.linux_halt.service`（デーモンの API `linux.halt.*` と同じもの。二重の実装をしない）。各コマンドは
「開く（API の linux.halt.open）→ 操作 1 つ → 閉じる（linux.halt.close）」で、attach は gdb.serve して gdb が去るまで待つ。
**設定値に既定は無い**（配線・基板・カーネルの性質は呼び出し側が言う）。バックエンドは 2 つ:
  * `--backend fprobe`: fprobe 自身の経路（USB の fprobe → JTAG。`ArmACores`）。デーモンを通らず、プロセス内で USB を開く
    （デーモンが同じ fprobe を握っている間は、API の linux.halt.* を使う）。
  * `--backend openocd`: OpenOCD の Tcl ポート経由（Pi 2B の基準線）。
attach はどの終わり方でも、ターゲットをきれいに戻す（BKPT を外し、HW の bp/wp を外し、全コアを走らせ、停止デバッグを切る）。
"""
from __future__ import annotations

import argparse
import signal
import sys
from dataclasses import dataclass

from .bp import UNMAPPED_POLICIES
from .engine import EngineError
from .kernel import Kernel, KernelLayoutError, ProcessNotFound
from .process import ProcessExited
from .service import UNMEASURED, Busy, FileLog, HaltOptions, HaltSession, OptionsError, process_rows


@dataclass(frozen=True)
class ProcRow:
    tgid: int
    comm: str
    has_mm: bool




def list_processes(kernel: Kernel) -> list[ProcRow]:
    """スレッドグループのリーダー（idle は除く）。mm が無いものはカーネルスレッド（service.HaltSession.processes と同じ）。"""
    return [ProcRow(**row) for row in process_rows(kernel)]


def _int(text: str) -> int:
    return int(text, 0)


_NOT_GIVEN = object()


def _bound(text: str):
    """--fpga-halt-watch-bound-ns・--cti-event-bound-ns: ns の整数か unmeasured（明示の「測っていない」。既定値ではない）。"""
    return UNMEASURED if text == UNMEASURED else int(text)


def _ocd_boards() -> dict:
    from fprobe.plugins.openocd_boards import BOARDS
    return BOARDS


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--backend", required=True, choices=["fprobe", "openocd"])
    common.add_argument("--jtag-khz", required=True, type=int, help="JTAG クロック（kHz）。配線の性質なので既定なし")
    common.add_argument("--offsets", required=True, help="カーネルのオフセットの JSON（tools/gen_offsets.py の出力）")
    common.add_argument("--page-offset", required=True, type=_int, help="カーネルの線形マップの仮想アドレスの始まり")
    common.add_argument("--phys-offset", required=True, type=_int, help="線形マップが対応する物理アドレスの始まり")
    common.add_argument("--linear-map-end", required=True, type=_int,
                        help="線形マップの上端の仮想アドレス（カーネルの high_memory）。これ以上は vmalloc・highmem で、"
                             "VMAP_STACK のスタックもここにある: オフセットだけでは読めないので断る")
    common.add_argument("--max-tasks", required=True, type=int, help="タスク一覧が何個まで続いてよいか（超えたら破損とみなす）")
    common.add_argument("--max-threads", required=True, type=int, help="1 プロセスのスレッド一覧が何個まで続いてよいか")
    common.add_argument("--mmu-format", required=True, choices=["short", "lpae"], help="ページテーブルの形式")
    common.add_argument("--journal", required=True, help="ブレークポイントのジャーナルのファイル（クラッシュからの回復用）")
    common.add_argument("--part", help="[fprobe] fprobe の部品の名前（arm_a_part。例 bcm2836）")
    common.add_argument("--probe-serial", help="[fprobe] fprobe の USB のシリアル番号（省略は唯一の機器）")
    common.add_argument("--probe-eth", help="[fprobe] プローブを Ethernet の中継のボード（PYNQ-Z2 の fprobe）にする: その IP アドレス")
    common.add_argument("--probe-eth-link-up-wait", type=float, metavar="SECONDS",
                        help="[fprobe] --probe-eth で開くとき、リンクの確立を待つ秒数（識別の READ をこの間だけ送り直す。"
                             "--probe-eth と一緒に必ず渡す。0 = 送り直さない）")
    common.add_argument("--fpga-halt-watch-bound-ns", type=_bound, default=_NOT_GIVEN,
                        help="[fprobe] FPGA の停止の見張りを使う（CTI の無い SoC。fprobe.halt_watch）: 1 つのコアが止まってから最後のコアが"
                             "止まるまでの上限（ns。目標の側で測った値）。測っていなければ unmeasured と書く")
    common.add_argument("--fpga-halt-watch-poll-s", type=float,
                        help="[fprobe] 見張りの間、FPGA の状態を読む間隔（秒。--fpga-halt-watch-bound-ns と一緒に）")
    common.add_argument("--fpga-request-times", action="store_true",
                        help="[fprobe] CTI の無い SoC で、こちらから全コアを止めたときの時間差の上限を、FPGA が停止要求の書き込みに付けた"
                             "時刻（イベント記録）で出す（docs/linux-halt.md。コアが要求を受けてから止まるまでの遅れはコアごとに同じ、"
                             "という仮定を報告に書く）。使えない停止は PC の時計の上限で、理由を報告に書く。付けなければ PC の時計だけ")
    common.add_argument("--cti-board", choices=["zynq7000"],
                        help="[fprobe] CTI で全コアを同時に止める: ボード（CTI の番号と番地。fprobe.plugins.zynq_cti）。下の --cti-* を全部一緒に")
    common.add_argument("--cti-request-bound-ns", type=int, help="[fprobe] こちらから止めるときの時間差の上限（ns。目標の側で測った値）")
    common.add_argument("--cti-event-bound-ns", type=_bound, default=_NOT_GIVEN,
                        help="[fprobe] コアが自分で止まって CTI が他を止めるときの時間差の上限（ns）。測っていなければ unmeasured")
    common.add_argument("--cti-hw-wait-s", type=float, help="[fprobe] ハードウェアが全コアを止めるのを待つ秒数（過ぎたらソフトの要求で止める）")
    common.add_argument("--cti-ack-polls", type=int, help="[fprobe] 停止要求の確認応答が消えるのを何回まで見るか")
    common.add_argument("--ocd-board", choices=sorted(_ocd_boards()),
                        help="[openocd] OpenOCD の設定のボード（ターゲット名・DAP・APB-AP・コアの数。fprobe.plugins.openocd_boards）")
    common.add_argument("--ocd-port", type=int, help="[openocd] OpenOCD の Tcl ポート")
    common.add_argument("--ocd-timeout-s", type=float, help="[openocd] OpenOCD の応答を待つ秒数")
    common.add_argument("--hw-slots", type=int, help="[openocd] コアごとのハードウェアブレークポイントの数")
    common.add_argument("--watch-slots", type=int, help="[openocd] コアごとのウォッチポイントの数")

    ap = argparse.ArgumentParser(prog="fprobe.linux_halt", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("attach", parents=[common], help="プロセスにつないで gdb に応える（API の linux.halt.gdb.serve）")
    a.add_argument("--pid", required=True, type=int)
    a.add_argument("--port", required=True, type=int, help="gdb が来る TCP ポート（127.0.0.1 のみ）")
    a.add_argument("--skew-limit-ns", required=True, type=int, help="「同時」とみなす上限（ns）。停止ごとの判定に使う")
    a.add_argument("--thread-id", required=True, choices=["stack", "contextidr"])
    a.add_argument("--kernel-isa", required=True, choices=["arm", "thumb"], help="カーネルのコードの命令セット")
    a.add_argument("--max-step-retries", required=True, type=int)
    a.add_argument("--bp-unmapped", required=True, choices=UNMAPPED_POLICIES,
                   help="未マップのページへの break: error = 断る / hw = ハードウェアブレークポイントにする")
    a.add_argument("-v", "--verbose", action="store_true")
    a.add_argument("--log", help="パケットのログのファイル")
    a.set_defaults(fn=cmd_attach)
    sub.add_parser("ps", parents=[common], help="プロセスの一覧（API の linux.halt.processes）").set_defaults(fn=cmd_ps)
    t = sub.add_parser("threads", parents=[common], help="プロセスのスレッドの一覧（API の linux.halt.threads）")
    t.add_argument("--pid", required=True, type=int)
    t.set_defaults(fn=cmd_threads)
    sub.add_parser("halt", parents=[common], help="全コアを止めて停止の報告を出す（止めたまま。API の linux.halt.halt_all）"
                   ).set_defaults(fn=cmd_halt)
    sub.add_parser("resume", parents=[common], help="全コアを走らせる（API の linux.halt.resume_all）").set_defaults(fn=cmd_resume)
    sub.add_parser("recover", parents=[common], help="死んだデバッガが残した BKPT を戻す（API の linux.halt.recover。コアが止まったままなら止めずに戻す。rescue の前に行う）"
                   ).set_defaults(fn=cmd_recover)
    sub.add_parser("rescue", parents=[common], help="強制終了されたデバッガの後始末: bp/wp を消し、停止デバッグを切り、止まったコアを再開"
                                                     "（API の linux.halt.rescue。BKPT が残っていそうなら、先に recover）").set_defaults(fn=cmd_rescue)
    sub.add_parser("status", parents=[common], help="読み取りだけ: 各コアが走っているか、残っている HW bp/wp（API の linux.halt.status）"
                   ).set_defaults(fn=cmd_status)
    return ap


def options_of(args) -> HaltOptions:
    """コマンドラインの共通の引数 → service.HaltOptions（API の linux.halt.open の引数と同じ名前）。"""
    def given(v):
        return None if v is _NOT_GIVEN else v
    return HaltOptions(
        backend=args.backend, jtag_khz=args.jtag_khz, offsets=args.offsets, page_offset=args.page_offset,
        phys_offset=args.phys_offset, linear_map_end=args.linear_map_end, max_tasks=args.max_tasks,
        max_threads=args.max_threads, mmu_format=args.mmu_format, journal=args.journal,
        fpga_request_times=args.fpga_request_times, part=args.part,
        fpga_halt_watch_bound_ns=given(args.fpga_halt_watch_bound_ns), fpga_halt_watch_poll_s=args.fpga_halt_watch_poll_s,
        cti_board=args.cti_board, cti_request_bound_ns=args.cti_request_bound_ns,
        cti_event_bound_ns=given(args.cti_event_bound_ns), cti_hw_wait_s=args.cti_hw_wait_s, cti_ack_polls=args.cti_ack_polls,
        ocd_board=args.ocd_board, ocd_port=args.ocd_port, ocd_timeout_s=args.ocd_timeout_s, hw_slots=args.hw_slots,
        watch_slots=args.watch_slots)


def parse(argv=None) -> argparse.Namespace:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.probe_eth is not None and args.probe_serial is not None:
        ap.error("--probe-eth and --probe-serial name two different probes")
    if (args.probe_eth is None) != (args.probe_eth_link_up_wait is None):
        ap.error("--probe-eth needs --probe-eth-link-up-wait (seconds to wait for the link; no default)")
    try:
        args.options = options_of(args).check()
    except OptionsError as e:
        ap.error(str(e))
    args.fpga_halt_watch, args.cti = args.options.fpga_halt_watch, args.options.cti
    for name in ("cti_event_bound_ns", "fpga_halt_watch_bound_ns"):     # バックエンドの「分からない」は None
        if getattr(args, name) in (_NOT_GIVEN, UNMEASURED):
            setattr(args, name, None)
    return args


def open_session(args) -> tuple[HaltSession, object]:
    """-> (session, close)。fprobe なら USB（か Ethernet）の fprobe をこのプロセスで開く。"""
    if args.backend == "openocd":
        session = HaltSession.open(args.options)
        _print_opened(session.info)
        return session, session.close
    from fprobe.session import Session
    probe = (Session.open_eth(args.probe_eth, link_up_wait_s=args.probe_eth_link_up_wait) if args.probe_eth is not None
             else Session.open(args.probe_serial))
    if args.probe_eth is not None:                        # 送り直した回数は事実として出す（待つ時間は使う側が渡した値）
        print("probe identify read: resent %d times (wait %g s)" % (probe.device.identify_resends,
                                                                    probe.device.identify_wait_s))
    try:
        session = HaltSession.open(args.options, dap_link=probe.dap_access, device=probe.device)
    except BaseException:
        probe.close()
        raise
    _print_opened(session.info)

    def close():
        try:
            return session.close()
        finally:
            probe.close()
    return session, close


def _print_opened(info: dict) -> None:
    if info.get("actual_jtag_khz") is not None:
        print("jtag clock: %d kHz" % info["actual_jtag_khz"])
    times = info.get("fpga_request_times")
    if times is not None and not times["event_log"]:
        print("fpga request times: this fprobe build has no event log; every bound will be host-timed (and say so)")


def _print_stop(stop: dict | None, label: str = "stop") -> None:
    if stop is None:
        return
    bound = "unknown (not a measurement of zero)" if stop["skew_upper_bound_ns"] is None else "%d ns" % stop["skew_upper_bound_ns"]
    print("%s: %s; cores halted %s; skew upper bound %s" % (label, stop["method"], stop["cores_halted"], bound))
    print("  basis: %s" % stop["basis"])


def _one(args, op):
    session, close = open_session(args)
    try:
        return op(session)
    finally:
        close()


def cmd_ps(args):
    r = _one(args, lambda s: s.processes())
    print("%6s  %-16s %s" % ("PID", "COMM", "USER-SPACE"))
    for p in r["processes"]:
        print("%6d  %-16s %s" % (p["tgid"], p["comm"], "yes" if p["has_mm"] else "(kernel thread)"))


def cmd_threads(args):
    r = _one(args, lambda s: s.threads(args.pid))
    print("pid %d %s (pgd %08x)" % (r["pid"], r["comm"], r["pgd"]))
    for t in r["threads"]:
        print("%6d  %-16s task=%08x" % (t["tid"], t["comm"], t["task"]))


def cmd_halt(args):
    _print_stop(_one(args, lambda s: s.halt_all())["stop"])
    print("the cores stay halted (resume runs them)")


def cmd_resume(args):
    print("resumed cores %s" % _one(args, lambda s: s.resume_all())["resumed"])


def cmd_recover(args):
    r = _one(args, lambda s: s.recover())
    print("restored %d instruction(s); %d not restored because the memory no longer held our BKPT" % (r["restored"], r["stale"]))


def cmd_status(args):
    """読み取りだけ（コアを止めない）。試験の「きれいに戻した」確認に使う。"""
    r = _one(args, lambda s: s.status())
    if r["cores"] is None:
        raise EngineError(r["cores_unread"])
    for c in r["cores"]:
        print("cpu%d %s bp=%s wp=%s" % (c["core"], "running" if c["running"] else "halted", c["breakpoints"], c["watchpoints"]))
    j = r["journal"]
    if j["exists"]:
        print("journal %s: %s entr(ies) (recover restores them)" % (j["path"], "?" if j["entries"] is None else len(j["entries"])))


def cmd_rescue(args):
    """強制終了されたデバッガが、コアを止めたまま残したとき: ターゲットのメモリは触らず、コアを動ける状態に戻す。"""
    r = _one(args, lambda s: s.rescue())
    print("rescued: resumed cores %s; breakpoints and watchpoints cleared; halting debug off" % (r["resumed"],))
    print("BKPT instructions left in the target's memory (if any) are restored by `recover`")


def _make_logger(args):
    if args.log:
        return FileLog(args.log)
    return (lambda m: print(m, file=sys.stderr, flush=True)) if args.verbose else (lambda m: None)


def _graceful_exit(signum, frame):
    raise KeyboardInterrupt        # gdb のサーバを止めて後始末を通る


def cmd_attach(args):
    log = _make_logger(args)
    for name in ("SIGBREAK", "SIGTERM"):             # SIGBREAK = Windows の Ctrl-Break
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _graceful_exit)
    session, close = open_session(args)
    try:
        r = session.gdb_serve(pid=args.pid, port=args.port, skew_limit_ns=args.skew_limit_ns, thread_id=args.thread_id,
                              kernel_isa=args.kernel_isa, max_step_retries=args.max_step_retries,
                              bp_unmapped=args.bp_unmapped, log=log)
        rec = r["recovered"]
        if rec["restored"] or rec["stale"]:
            print("restored %d instruction(s) left by a previous session (%d not restored: the memory no longer held "
                  "our BKPT)" % (rec["restored"], rec["stale"]))
        print("attached: %s (pid %d, %d thread(s), pgd %08x)" % (r["comm"], r["pid"], r["threads"], r["pgd"]))
        print("gdb: target remote :%d" % args.port, flush=True)
        try:
            while not session.wait_gdb(0.25):          # 短く待つ: Python はシグナルのハンドラを待ちの合間にしか走らせない
                pass
        except KeyboardInterrupt:
            pass
        g = session.gdb_stop()
        for e in g["cleanup_errors"]:
            print("cleanup:", e, file=sys.stderr)
        if g["cleanup_reconnected"]:
            print("cleanup: reconnected the DP and cleaned up again", file=sys.stderr)
        for e in g["cleanup_errors_after_reconnect"]:
            print("cleanup after reconnect:", e, file=sys.stderr)
        if g["ended_by"] == "error":
            print("error:", g["error"], file=sys.stderr)
            return 1
    except (ProcessNotFound, ProcessExited, EngineError, Busy) as e:
        print("error:", e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    finally:
        close()
    return 0


def main(argv=None):
    args = parse(argv)
    for stream in (sys.stdout, sys.stderr):                  # 全部のバイトを出せないコンソールで落ちない
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    try:
        return args.fn(args) or 0
    except (KernelLayoutError, ProcessNotFound, ProcessExited, EngineError, Busy) as e:
        print("error:", e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
