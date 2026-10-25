"""Probe: 1 つの fprobe を開き、デバッガ・ロジックアナライザ・UART・CSR 等を API のメソッドとして出す本体（デーモンの中身）。

道具の実装は probe_*.py の mixin に分かれていて、ここは継承の並びと状態の初期化、機器の開閉（session・target・dbg・close）だけ。
デバッガとロジアナは同じ USB ハンドルを共有する（fprobe.session）ので、「捕捉を仕掛けてから実行・停止し、波形を見る」が
1 つの会話の中でできる。

依存の向き: デーモン（fprobe.api.daemon）がこの Probe を握る。MCP の入口（fprobe.mcp_client）はデーモンの薄いクライアントで、
Probe を import しない（import の輪を作らない）。入口の別名は fprobe.mcp_server。
"""

import threading

from serial import Serial

from .api import NotConnected
from .collector import Collector
from .debug import Debugger
from .la import LogicAnalyzer, Transitions, Waveform
from .probe_analysis import AnalysisTools
from .probe_breakpoints import BreakpointActions
from .probe_connect import ConnectTools
from .probe_cycles import CyclesTools
from .probe_debug import DebugTools
from .probe_decode import DecodeTools
from .probe_design import DesignTools
from .probe_device import DeviceTools
from .probe_elf_files import ElfFileTools
from .probe_elf_watch import ElfWatchTools
from .probe_etm import EtmTools
from .probe_events import EventTools
from .probe_execution import ExecutionTools
from .probe_flash import FlashTools
from .probe_flash_bp import FlashBreakpointTools
from .probe_formats import FormatTools
from .probe_fpga_obs import FpgaObsTools
from .probe_gdb import GdbTools
from .probe_gdb_trace import GdbTraceTools
from .probe_gpio import GpioTools
from .probe_heap import HeapTools
from .probe_hooks import HookTools
from .probe_inspect import SOURCE_LIMIT, InspectTools
from .probe_la import LogicAnalyzerTools
from .probe_linux import LinuxTools
from .probe_longrec import LongRecordingTools
from .probe_lowlevel import LowLevelTools
from .probe_match_trigger import MatchTriggerTools
from .probe_monitor import MonitorModeTools
from .probe_multicore import MulticoreTools
from .probe_observe import ObserveTools
from .probe_profile import ProfileTools
from .probe_profile_export import ProfileExportTools
from .probe_recording import RecordingTools
from .probe_recording_rtos import RecordingRtosTools
from .probe_rtos import RtosTools
from .probe_rtos_analysis import RtosAnalysisTools
from .probe_rules import RulesTools
from .probe_safety import SafetyTools
from .probe_snapshot import SnapshotTools
from .probe_stack import StackTools
from .probe_streams import StreamTools
from .probe_swo import SwoTools
from .probe_swo_rx import SwoReceiveTools
from .probe_system import SystemTools
from .probe_target import TargetTools
from .probe_terminal import TerminalTools
from .probe_testpoint import TestpointTools
from .probe_timeline import TimelineTools
from .probe_uart import UartTools
from .probe_ui_state import UiStateTools
from .probe_user import UserTools
from .probe_watch import WatchTools
from .probe_window import WindowTools
from .probe_xtrig import XtrigTools
from .probe_xtrig_out import XtrigOutputTools
from .recording import Recorder, Recording
from .sampler import Sampler
from .serial_link import DEFAULT_BAUD
from .session import Session
from .symbols import Symbols
from .voltage import VoltageHistory

__all__ = ["Probe", "SOURCE_LIMIT"]


class Probe(InspectTools, ObserveTools, WatchTools, RtosTools, ProfileTools, SwoTools, CyclesTools, WindowTools,
            BreakpointActions, AnalysisTools, GdbTools, GdbTraceTools, FlashTools, TestpointTools, SnapshotTools,
            ExecutionTools, RtosAnalysisTools, RulesTools, HookTools, FlashBreakpointTools, DecodeTools, UserTools,
            LongRecordingTools, HeapTools, LowLevelTools, ConnectTools, ElfWatchTools, StreamTools, FormatTools,
            ProfileExportTools, StackTools, TimelineTools, ElfFileTools, TerminalTools, UiStateTools, DesignTools,
            MonitorModeTools, XtrigOutputTools, GpioTools, SafetyTools, MulticoreTools, EtmTools, LinuxTools,
            RecordingRtosTools, FpgaObsTools, MatchTriggerTools, UartTools, SystemTools, EventTools,
            LogicAnalyzerTools, XtrigTools, RecordingTools, SwoReceiveTools, TargetTools, DebugTools, DeviceTools):

    def __init__(self, serial: str | None = None, port: str | None = None, baud: int = DEFAULT_BAUD,
                 eth: str | None = None, bitstreams: str | None = None):
        self._serial = serial
        self._port, self._baud = port, baud               # port を指定すると SerialLink で開く
        self._eth = eth                                   # IP アドレスを指定すると EthLink で開く
        self._lock = threading.RLock()
        self._voltage_history = VoltageHistory()
        self._session: Session | None = None
        self._debug = None
        self._symbols: Symbols | None = None                # elf_load / program_ram / flash（ELF）で読み込む
        self._dbg: Debugger | None = None
        self._capture_id = 0                                    # 最後の捕捉（画面の波形 la_view 用）
        self._capture: Transitions | None = None
        self._capture_lock = threading.Lock()             # 捕捉・情報・変化点の表をそろえて読み書きする（読みのスレッド）
        self._capture_info: dict | None = None
        self._capture_names: dict[int, str] = {}                # 最後の捕捉の ch の既定の名前（書き出し用）
        self._wave: Waveform | None = None                     # 変化点の表は画面が最初に求めたときに作る
        self._recorder: Recorder | None = None                 # 記録中の共通の時間軸の記録（F3）
        self._collector: Collector | None = None
        self._view: Recording | None = None                    # 読む対象の記録（記録中のもの、または開いたもの）
        self._view_lock = threading.Lock()                     # _view の差し替えと前の記録を閉じるのを 1 つに
        self._uart: Serial | None = None
        self._la: LogicAnalyzer | None = None
        self._la_period = 0.0
        self._target_type: str | None = None              # target_connect のターゲット名（SVD を探す）
        self._link: dict | None = None                    # つないでいる先（target・frequency_hz・wire。target_status に出す）
        self._swo_baud: int | None = None                 # swo_start の速度（時刻の分解能の見積もりに使う）
        self._swo_mode = "uart"                           # swo_start の方式（uart / manchester）
        self._init_inspect()
        self._init_breakpoints()
        self._init_flash()
        self._init_flash_bp()
        self._init_gdb()
        self._init_gdb_trace()
        self._init_hooks()
        self._init_observe()
        self._init_profile()
        self._init_rtos()
        self._init_snapshot()
        self._init_swo()
        self._init_testpoint()
        self._init_watch()
        self._init_window()
        self._init_user()
        self._init_longrec()
        self._init_heap()
        self._init_lowlevel()
        self._init_elf_watch()
        self._init_streams()
        self._init_terminal()
        self._init_ui_state()
        self._init_formats()
        self._init_stack()
        self._init_design(bitstreams)                     # Primer の構成の書き替え（ビットストリームの置き場所）
        self._init_monitor()
        self._init_safety()
        self._init_xtrig_out()
        self._init_etm()
        self._init_linux()
        self._init_analysis()
        self._init_fpga_obs()
        self._init_la()
        self._init_match_trigger()
        self._init_recording()
        self._init_target()

    # --- 共通 ------------------------------------------------------------------------

    @property
    def session(self) -> Session:
        if self._session is None:
            if self._eth:
                self._session = Session.open_eth(self._eth)
            elif self._port:
                self._session = Session.open_serial(self._port, self._baud)
            else:
                self._session = Session.open(self._serial)
            self._stop_stale_sampler()
        return self._session

    def _stop_stale_sampler(self) -> None:
        """開いたばかりの機器で、前の持ち主（落ちた・起動し直したデーモン等）が動かしたままの DAP サンプラを止める（B30）。
        持ち主はプロセスの中にしかいないので、開いた時点で動いているものは誰の物でもなく、線を借り続けて接続を邪魔する。"""
        if getattr(self._session.device.capabilities(), "sampler", False):
            Sampler(self._session.device).stop()

    @property
    def target(self):
        """接続中のターゲット。未接続でスナップショットを開いていれば、その読み手（読むだけ）。"""
        if self._debug is None:
            offline = self._offline_target()
            if offline is not None:
                return offline
            raise NotConnected("ターゲット未接続（target_connect を先に呼ぶ）")
        # Flash を持てば Flash のブレークポイントで包み、モニタ方式ならモニタの包み（fprobe.monitor）で包む
        return self._monitor_wrap(self._flash_bp_wrap(self._debug.target))

    @property
    def dbg(self) -> Debugger:
        if self._dbg is None or self._dbg.target is not self.target:
            self._dbg = Debugger(self.target, self._symbols)
        return self._dbg

    def close(self) -> None:
        self._release_device()
        self._close_linux()                               # Linux のターゲットは機器と無関係（USB の開き直しでは閉じない）
        self._stop_elf_watch()

    def _release_device(self) -> None:
        """機器とターゲットにつながるものを閉じる（USB の開き直し・構成の書き替えでも使う）。ELF の更新の見張りは
        機器と無関係なので止めない（止めると、開き直しの後に ELF を作り直しても読み込み直さなくなった。2026-10-18）。"""
        self._stop_la_record()
        self._close_streams()
        self._close_formats()
        self._stop_observing()
        self._close_testpoint()
        with self._lock:
            self.target_disconnect()
            self._close_lowlevel()
            if self._uart is not None:
                self._uart.close()
                self._uart = None
            if self._session is not None:
                self._session.close()
                self._session = None

    def _caps(self):
        """ケイパビリティ（最後に読んだもの。無ければ読む）。capabilities() は呼ぶたびに機器から読み直す（UDP の
        リンクで何往復もする）ので、毎秒の読み（system_status・fpga_obs_read 等）はこちらで。構成の切り替え・
        つなぎ直しでは capabilities() が読み直して last_capabilities も替わる。"""
        device = self.session.device
        cached = getattr(device, "last_capabilities", None)
        if cached is not None:
            return cached
        with self._lock:                                      # 読み直すときは機器を使う（ほかのスレッドと混ぜない）
            return device.capabilities()

