"""Probe: 1 つの fprobe を開き、デバッガ・ロジックアナライザ・UART・CSR 等を API のメソッドとして出す本体（デーモンの中身）。

道具の実装は領域ごとのサービス（probe_*.py の XxxService）にある。ここは核（機器の錠・機器の口・接続中のターゲット・
読み込んだ ELF 等の状態と、機器の開閉 session・target・dbg・close）と、サービスの組み立て・委譲だけ。
サービスはホスト（この Probe）を HostView 越しに使い、使う名前を NEEDS に並べる。ほかの領域に出す名前は SHARED、
API の名前は API に並べ、Probe はその名前をサービスに委ねる（fprobe.probe_service.expose。
docs/superpowers/specs/2026-11-03-probe-services-design.md）。
デバッガとロジアナは同じ USB ハンドルを共有する（fprobe.session）ので、「捕捉を仕掛けてから実行・停止し、波形を見る」が
1 つの会話の中でできる。

依存の向き: デーモン（fprobe.api.daemon）がこの Probe を握る。MCP の入口（fprobe.mcp_client）はデーモンの薄いクライアントで、
Probe を import しない（import の輪を作らない）。入口の別名は fprobe.mcp_server。
"""

import functools
import threading

from serial import Serial

from .api import NotConnected
from .caps import Capabilities
from .debug import Debugger
from .probe_analysis import AnalysisService
from .probe_breakpoints import BreakpointService
from .probe_connect import ConnectService
from .probe_cycles import CyclesService
from .probe_debug import DebugService
from .probe_decode import DecodeService
from .probe_design import DesignService
from .probe_device import DeviceService
from .probe_elf_files import ElfFileService
from .probe_elf_watch import ElfWatchService
from .probe_etm import EtmService
from .probe_events import EventService
from .probe_execution import ExecutionService
from .probe_flash import FlashService
from .probe_flash_bp import FlashBreakpointService
from .probe_formats import FormatService
from .probe_fpga_obs import FpgaObsService
from .probe_gdb import GdbService
from .probe_gdb_trace import GdbTraceService
from .probe_gpio import GpioService
from .probe_heap import HeapService
from .probe_hooks import HookService
from .probe_inspect import SOURCE_LIMIT, InspectService
from .probe_la import LogicAnalyzerService
from .probe_linux import LinuxService
from .probe_linux_halt import LinuxHaltService
from .probe_longrec import LongRecordingService
from .probe_lowlevel import LowLevelService
from .probe_match_trigger import MatchTriggerService
from .probe_monitor import MonitorModeService
from .probe_multicore import MulticoreService
from .probe_observe import ObserveService
from .probe_profile import ProfileService
from .probe_profile_export import ProfileExportService
from .probe_recording import RecordingService
from .probe_recording_rtos import RecordingRtosService
from .probe_rtt import RttService
from .probe_rtos import RtosService
from .probe_rtos_analysis import RtosAnalysisService
from .probe_rules import RulesService, inject_host
from .probe_safety import SafetyService
from .probe_semihosting import SemihostingService
from .probe_snapshot import SnapshotService
from .probe_stack import StackService
from .probe_streams import StreamService
from .probe_swo import SwoService
from .probe_swo_itm import SwoDecodeService
from .probe_swo_rx import SwoReceiveService
from .probe_system import SystemService
from .probe_target import TargetService
from .probe_terminal import TerminalService
from .probe_testpoint import TestpointService
from .probe_timeline import TimelineService
from .probe_trace import TraceService
from .probe_uart import UartService
from .probe_ui_state import UiStateService
from .probe_user import UserService
from .probe_watch import WatchService
from .probe_window import WindowService
from .probe_service import ReleaseSteps, attempt, expose
from .probe_xtrig import XtrigService
from .probe_xtrig_out import XtrigOutputService
from .sampler import Sampler
from .serial_link import DEFAULT_BAUD
from .session import Session
from .symbols import Symbols
from .voltage import VoltageHistory

__all__ = ["Probe", "SOURCE_LIMIT"]


# 領域ごとのサービスの API・SHARED の名前を委ねる（fprobe.probe_service.expose）
@expose("_svc_xtrig", XtrigService)
@expose("_svc_xtrig_out", XtrigOutputService)
@expose("_svc_match_trigger", MatchTriggerService)
@expose("_svc_gpio", GpioService)
@expose("_svc_events", EventService)
@expose("_svc_linux_halt", LinuxHaltService)
@expose("_svc_la", LogicAnalyzerService)
@expose("_svc_longrec", LongRecordingService)
@expose("_svc_decode", DecodeService)
@expose("_svc_timeline", TimelineService)
@expose("_svc_recording", RecordingService)
@expose("_svc_recording_rtos", RecordingRtosService)
@expose("_svc_rules", RulesService)
@expose("_svc_rtos_analysis", RtosAnalysisService)
@expose("_svc_analysis", AnalysisService)
@expose("_svc_window", WindowService)
@expose("_svc_fpga_obs", FpgaObsService)
@expose("_svc_ui_state", UiStateService)
@expose("_svc_user", UserService)
@expose("_svc_hooks", HookService)
@expose("_svc_terminal", TerminalService)
@expose("_svc_streams", StreamService)
@expose("_svc_formats", FormatService)
@expose("_svc_trace", TraceService)
@expose("_svc_elf_watch", ElfWatchService)
@expose("_svc_elf_files", ElfFileService)
@expose("_svc_device", DeviceService)
@expose("_svc_system", SystemService)
@expose("_svc_uart", UartService)
@expose("_svc_design", DesignService)
@expose("_svc_swo", SwoService)
@expose("_svc_swo_rx", SwoReceiveService)
@expose("_svc_testpoint", TestpointService)
@expose("_svc_profile", ProfileService)
@expose("_svc_profile_export", ProfileExportService)
@expose("_svc_etm", EtmService)
@expose("_svc_cycles", CyclesService)
@expose("_svc_linux", LinuxService)
@expose("_svc_heap", HeapService)
@expose("_svc_stack", StackService)
@expose("_svc_rtos", RtosService)
@expose("_svc_safety", SafetyService)
@expose("_svc_watch", WatchService)
@expose("_svc_gdb", GdbService)
@expose("_svc_gdb_trace", GdbTraceService)
@expose("_svc_execution", ExecutionService)
@expose("_svc_inspect", InspectService)
@expose("_svc_observe", ObserveService)
@expose("_svc_rtt", RttService)
@expose("_svc_semihosting", SemihostingService)
@expose("_svc_swo_itm", SwoDecodeService)
@expose("_svc_breakpoints", BreakpointService)
@expose("_svc_flash", FlashService)
@expose("_svc_snapshot", SnapshotService)
@expose("_svc_flash_bp", FlashBreakpointService)
@expose("_svc_lowlevel", LowLevelService)
@expose("_svc_connect", ConnectService)
@expose("_svc_monitor", MonitorModeService)
@expose("_svc_multicore", MulticoreService)
@expose("_svc_target", TargetService)
@expose("_svc_debug", DebugService)
class Probe:

    def __init__(self, serial: str | None = None, port: str | None = None, baud: int = DEFAULT_BAUD,
                 eth: str | None = None, bitstreams: str | None = None, *, link_up_wait_s: float | None = None):
        """eth（IP アドレス）で開くなら link_up_wait_s（リンクの確立を待つ秒数。Session.open_eth。使う側が明示する）も渡す。"""
        if eth and link_up_wait_s is None:
            raise ValueError("eth で開くときは link_up_wait_s（リンクの確立を待つ秒数）を明示する")
        self._serial = serial
        self._port, self._baud = port, baud               # port を指定すると SerialLink で開く
        self._eth = eth                                   # IP アドレスを指定すると EthLink で開く
        self._link_up_wait_s = link_up_wait_s
        self._lock = threading.RLock()
        self._voltage_history = VoltageHistory()
        self._session: Session | None = None
        self._debug = None
        self._symbols: Symbols | None = None                # elf_load / program_ram / flash（ELF）で読み込む
        self._dbg: Debugger | None = None
        self._uart: Serial | None = None
        self._target_type: str | None = None              # target_connect のターゲット名（SVD を探す）
        self._link: dict | None = None                    # つないでいる先（target・frequency_hz・wire。target_status に出す）
        # サービス（ホストは Probe 自身。作る順は、前の領域の状態を使う初期化の順）
        self._svc_debug = DebugService(self)
        self._svc_multicore = MulticoreService(self)
        self._svc_connect = ConnectService(self)
        self._svc_execution = ExecutionService(self)
        self._svc_cycles = CyclesService(self)
        self._svc_profile_export = ProfileExportService(self)
        self._svc_swo_rx = SwoReceiveService(self)
        self._svc_uart = UartService(self)
        self._svc_system = SystemService(self)
        self._svc_device = DeviceService(self)
        self._svc_elf_files = ElfFileService(self)
        self._svc_rtos_analysis = RtosAnalysisService(self)
        self._svc_rules = RulesService(self, lambda: inject_host(self))
        self._svc_recording_rtos = RecordingRtosService(self)
        self._svc_timeline = TimelineService(self)
        self._svc_decode = DecodeService(self)
        self._svc_xtrig = XtrigService(self)
        self._svc_xtrig_out = XtrigOutputService(self)
        self._svc_match_trigger = MatchTriggerService(self)
        self._svc_gpio = GpioService(self)
        self._svc_events = EventService(self)
        self._svc_linux_halt = LinuxHaltService(self)
        self._svc_inspect = InspectService(self)
        self._svc_breakpoints = BreakpointService(self)
        self._svc_flash = FlashService(self)
        self._svc_flash_bp = FlashBreakpointService(self)
        self._svc_gdb = GdbService(self)
        self._svc_gdb_trace = GdbTraceService(self)
        self._svc_hooks = HookService(self)
        self._svc_observe = ObserveService(self)
        self._svc_rtt = RttService(self)
        self._svc_semihosting = SemihostingService(self)
        self._svc_swo_itm = SwoDecodeService(self)
        self._svc_profile = ProfileService(self)
        self._svc_rtos = RtosService(self)
        self._svc_snapshot = SnapshotService(self)
        self._svc_swo = SwoService(self)
        self._svc_testpoint = TestpointService(self)
        self._svc_watch = WatchService(self)
        self._svc_window = WindowService(self)
        self._svc_user = UserService(self)
        self._svc_longrec = LongRecordingService(self)
        self._svc_heap = HeapService(self)
        self._svc_lowlevel = LowLevelService(self)
        self._svc_elf_watch = ElfWatchService(self)
        self._svc_streams = StreamService(self)
        self._svc_terminal = TerminalService(self)
        self._svc_ui_state = UiStateService(self)
        self._svc_formats = FormatService(self)
        self._svc_trace = TraceService(self)
        self._svc_stack = StackService(self)
        self._svc_design = DesignService(self, bitstreams)                     # Primer の構成の書き替え（ビットストリームの置き場所）
        self._svc_monitor = MonitorModeService(self)
        self._svc_safety = SafetyService(self)
        self._svc_etm = EtmService(self)
        self._svc_linux = LinuxService(self)
        self._svc_analysis = AnalysisService(self)
        self._svc_fpga_obs = FpgaObsService(self)
        self._svc_la = LogicAnalyzerService(self)
        self._svc_recording = RecordingService(self)
        self._svc_target = TargetService(self)
        # 機器を手放すとき（_release_device）に、機器の口を閉じる前に閉じる段（登録の順に閉じる）。gdb の後始末（BKPT を外し、
        # 全コアを走らせる）を待つ linux_halt は最後で、錠の外
        self._release_steps = ReleaseSteps()
        # （段は呼ぶ時に名前で引く: 試験・プラグインが差し替えたものを使う）
        for name, method in (("la_record", "_stop_la_record"), ("streams", "_close_streams"),
                             ("formats", "_close_formats"), ("observing", "_stop_observing"),
                             ("testpoint", "_close_testpoint"), ("linux_halt", "_close_linux_halt")):
            self._release_steps.add(name, functools.partial(_call, self, method))

    # --- 核（サービスはホスト越しに使う） ---------------------------------------------------------------

    @property
    def session(self) -> Session:
        if self._session is None:
            if self._eth:
                self._session = Session.open_eth(self._eth, link_up_wait_s=self._link_up_wait_s)
            elif self._port:
                self._session = Session.open_serial(self._port, self._baud)
            else:
                self._session = Session.open(self._serial)
            self._stop_stale_sampler()
        return self._session

    def _stop_stale_sampler(self) -> None:
        """開いたばかりの機器で、前の持ち主（落ちた・起動し直したデーモン等）が動かしたままの DAP サンプラを止める（B30）。
        機器を同時に使えるホストのプロセスは 1 つだけ（リンクの事実: USB は libusb が機器を排他的に握る・COM ポートは排他・
        UDP は FPGA が最後に要求を送った相手にだけ応答を返すので、2 つ目のプロセスが開いた時点で前の相手には届かない）。だから
        開いた時点で動いているサンプラの持ち主は、このプロセスの中にはおらず、ほかのプロセスが使い続けることもできない（C8: 推定ではなく
        リンクの性質）。止めずにおくと線を借り続けて接続を邪魔する。"""
        if self._session.device.capabilities().sampler:
            Sampler(self._session.device).stop()

    @property
    def target(self):
        """接続中のターゲット。未接続でスナップショットを開いていれば、その読み手（読むだけ）。"""
        if self._debug is None:
            offline = self._offline_target()
            if offline is not None:
                return offline
            raise NotConnected("ターゲット未接続（MCU なら target.connect を先に呼ぶ。MCP の道具名は target_connect。Linux のプロセスなら linux.halt.open → linux.halt.debug.attach）")
        # Flash を持てば Flash のブレークポイントで包み、モニタ方式ならモニタの包み（fprobe.monitor）で包む
        return self._monitor_wrap(self._flash_bp_wrap(self._debug.target))

    @property
    def dbg(self) -> Debugger:
        if self._dbg is None or self._dbg.target is not self.target:
            self._dbg = Debugger(self.target, self._symbols)
        return self._dbg

    def close(self) -> list[dict]:
        """全部を閉じる。閉じるのに失敗した段の事実（_release_device と同じ形）を返す（無ければ空）。"""
        failures = self._release_device()
        for name, step in (("linux", self._close_linux),   # Linux のターゲットは機器と無関係（USB の開き直しでは閉じない）
                           ("elf_watch", self._stop_elf_watch)):
            attempt(failures, name, step)
        return failures

    def _release_device(self) -> list[dict]:
        """機器とターゲットにつながるものを閉じる（USB の開き直し・構成の書き替えでも使う）。ELF の更新の見張りは
        機器と無関係なので止めない（止めると、開き直しの後に ELF を作り直しても読み込み直さなくなった。2026-10-18）。

        どの段も必ず試す（1 つの段の失敗で後ろの段を閉じ残さない。切れた機器への後始末は失敗しうる）。失敗した段は
        [{"step": 段の名前, "error": "型: 文"}] で返す（無ければ空）。機器の口（session・uart）は閉じるのに失敗しても手放す
        （開き直しは新しい口で行う。残ったものは linux.halt.rescue・recover で）。"""
        failures = self._release_steps.run()
        with self._lock:
            attempt(failures, "target", self.target_disconnect)
            attempt(failures, "lowlevel", self._close_lowlevel)
            uart, self._uart = self._uart, None
            if uart is not None:
                attempt(failures, "uart", uart.close)
            session, self._session = self._session, None
            if session is not None:
                attempt(failures, "session", session.close)
        return failures

    def _caps(self) -> Capabilities:
        """ケイパビリティ（最後に読んだもの = Device.last_capabilities。無ければ読む）。capabilities() は呼ぶたびに機器から
        読み直す（UDP のリンクで何往復もする）ので、毎秒の読み（system_status・fpga_obs_read 等）はこちらで。回路が替わる
        構成の切り替え・つなぎ直しは Device を開き直す（新しい Device の last_capabilities は None から）。"""
        device = self.session.device
        if device.last_capabilities is not None:
            return device.last_capabilities
        with self._lock:                                      # 読み直すときは機器を使う（ほかのスレッドと混ぜない）
            return device.capabilities()


def _call(obj, method: str) -> None:
    getattr(obj, method)()
