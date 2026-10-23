// パネルの一覧（View メニュー・ドッキングの既定の配置が使う。並びは View メニューの順）
import type { PanelDef } from "../../lib/dock";
import ActivityPanel from "./ActivityPanel.svelte";
import ApiPanel from "./ApiPanel.svelte";
import BreakpointsPanel from "./BreakpointsPanel.svelte";
import CallGraphPanel from "./CallGraphPanel.svelte";
import CallStackPanel from "./CallStackPanel.svelte";
import CpuLoadPanel from "./CpuLoadPanel.svelte";
import DashboardPanel from "./DashboardPanel.svelte";
import DataSamplingPanel from "./DataSamplingPanel.svelte";
import DisassemblyPanel from "./DisassemblyPanel.svelte";
import EventLogPanel from "./EventLogPanel.svelte";
import FaultPanel from "./FaultPanel.svelte";
import FavoritesPanel from "./FavoritesPanel.svelte";
import FlashPanel from "./FlashPanel.svelte";
import FpgaObsPanel from "./FpgaObsPanel.svelte";
import HeapPanel from "./HeapPanel.svelte";
import InterruptsPanel from "./InterruptsPanel.svelte";
import LocalsPanel from "./LocalsPanel.svelte";
import LogPanel from "./LogPanel.svelte";
import LinuxPanel from "./LinuxPanel.svelte";
import LogicAnalyzerPanel from "./LogicAnalyzerPanel.svelte";
import MemoryPanel from "./MemoryPanel.svelte";
import PeripheralsPanel from "./PeripheralsPanel.svelte";
import PowerPanel from "./PowerPanel.svelte";
import ProfilePanel from "./ProfilePanel.svelte";
import RecordingsPanel from "./RecordingsPanel.svelte";
import RegistersPanel from "./RegistersPanel.svelte";
import RtosObjectsPanel from "./RtosObjectsPanel.svelte";
import SourcePanel from "./SourcePanel.svelte";
import SystemPanel from "./SystemPanel.svelte";
import SwoTracePanel from "./SwoTracePanel.svelte";
import TerminalPanel from "./TerminalPanel.svelte";
import ThreadsPanel from "./ThreadsPanel.svelte";
import TimelinePanel from "./TimelinePanel.svelte";
import WatchPanel from "./WatchPanel.svelte";

// group: View メニューの区切りと、閉じていたパネルを開く場所（lib/dock.ts の PanelGroup）
export const PANELS: PanelDef[] = [
  // デバッガの窓
  { id: "source", title: "panel.source", component: SourcePanel, group: "debug" },
  { id: "disassembly", title: "panel.disassembly", component: DisassemblyPanel, group: "debug" },
  { id: "registers", title: "panel.registers", component: RegistersPanel, group: "debug" },
  { id: "watch", title: "panel.watch", component: WatchPanel, group: "debug" },
  { id: "locals", title: "panel.locals", component: LocalsPanel, group: "debug" },
  { id: "callStack", title: "panel.callStack", component: CallStackPanel, group: "debug" },
  { id: "breakpoints", title: "panel.breakpoints", component: BreakpointsPanel, group: "debug" },
  { id: "memory", title: "panel.memory", component: MemoryPanel, group: "debug" },
  { id: "peripherals", title: "panel.peripherals", component: PeripheralsPanel, group: "debug" },
  { id: "threads", title: "panel.threads", component: ThreadsPanel, group: "debug" },
  { id: "fault", title: "panel.fault", component: FaultPanel, group: "debug" },
  // 時間軸・端末・記録
  { id: "timeline", title: "panel.timeline", component: TimelinePanel, group: "trace" },
  { id: "terminal", title: "panel.terminal", component: TerminalPanel, group: "trace" },
  { id: "log", title: "panel.log", component: LogPanel, group: "trace" },
  { id: "linux", title: "panel.linux", component: LinuxPanel, group: "trace" },
  { id: "activity", title: "panel.activity", component: ActivityPanel, group: "trace" },
  { id: "swoTrace", title: "panel.swoTrace", component: SwoTracePanel, group: "trace" },
  { id: "eventLog", title: "panel.eventLog", component: EventLogPanel, group: "trace" },
  { id: "recordings", title: "panel.recordings", component: RecordingsPanel, group: "trace" },
  // 解析のビュー（システム解析 = MCU と FPGA を対等に並べる）
  { id: "system", title: "panel.system", component: SystemPanel, group: "analysis" },
  { id: "dataSampling", title: "panel.dataSampling", component: DataSamplingPanel, group: "analysis" },
  { id: "power", title: "panel.power", component: PowerPanel, group: "analysis" },
  { id: "cpuLoad", title: "panel.cpuLoad", component: CpuLoadPanel, group: "analysis" },
  { id: "rtosObjects", title: "panel.rtosObjects", component: RtosObjectsPanel, group: "analysis" },
  { id: "heap", title: "panel.heap", component: HeapPanel, group: "analysis" },
  { id: "profile", title: "panel.profile", component: ProfilePanel, group: "analysis" },
  { id: "interrupts", title: "panel.interrupts", component: InterruptsPanel, group: "analysis" },
  { id: "callGraph", title: "panel.callGraph", component: CallGraphPanel, group: "analysis" },
  { id: "logicAnalyzer", title: "panel.logicAnalyzer", component: LogicAnalyzerPanel, group: "analysis" },
  { id: "fpgaObs", title: "panel.fpgaObs", component: FpgaObsPanel, group: "analysis" },
  // 道具
  { id: "dashboard", title: "panel.dashboard", component: DashboardPanel, group: "tools" },
  { id: "flash", title: "panel.flash", component: FlashPanel, group: "tools" },
  { id: "api", title: "panel.api", component: ApiPanel, group: "tools" },
  { id: "favorites", title: "panel.favorites", component: FavoritesPanel, group: "tools" },
];
