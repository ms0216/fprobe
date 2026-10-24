// ドッキング（dockview-core）: パネルの並び・分割・タブ・閉じる・配置の保存。各パネルは Svelte のコンポーネントを
// mount で入れる（context は App と同じ Map を渡す）。仕様: docs/superpowers/specs/2026-10-18-ui-layout-design.md §5
import { createDockview, themeDark, themeLight, type DockviewApi, type IContentRenderer } from "dockview-core";
import { mount, unmount, type Component } from "svelte";
import { t, i18n, type Key } from "./i18n";
import { SHARED, type Shared } from "./context";
import { load, save } from "./prefs";
import { sourcedApp, sourcedConn } from "./source";
import { headerActions, popoutTitle, tabMenu } from "./dockwindows";

/** パネルのまとまり（View メニューの区切りと、閉じていたパネルを開く場所）: debug = デバッガの窓（右の列）、
 *  trace = 時間軸・端末・記録（下の段）、analysis = 解析のビュー（下の段）、tools = 書き込み・操作パネル（下の段） */
export type PanelGroup = "debug" | "trace" | "analysis" | "tools";

export interface PanelDef {
  id: string;
  title: Key;
  component: Component<any>;
  group: PanelGroup;
}

const LAYOUT_KEY = "dock.layout.v2";            // v2: 既定が解析の配置に（2026-10-24）。前の保存は使わない

/** デバッグの配置（日常のデバッグ。表示メニューから選ぶ）: 中央 = Source、右 = Watch・Locals・Registers / Call Stack・
 *  Threads / Breakpoints・Peripherals・Memory、下 = Timeline・Terminal・Disassembly・Activity（Ozone・VS Code の型） */
function debugLayout(api: DockviewApi, add: (id: string, position?: object) => void): void {
  add("source");
  add("watch", { direction: "right", referencePanel: "source" });
  add("locals", { referencePanel: "watch" });
  add("registers", { referencePanel: "watch" });
  add("callStack", { direction: "below", referencePanel: "watch" });
  add("threads", { referencePanel: "callStack" });
  add("breakpoints", { direction: "below", referencePanel: "callStack" });
  add("peripherals", { referencePanel: "breakpoints" });
  add("memory", { referencePanel: "breakpoints" });
  add("timeline", { direction: "below", referencePanel: "source" });
  add("terminal", { referencePanel: "timeline" });
  add("disassembly", { referencePanel: "timeline" });
  add("activity", { referencePanel: "timeline" });
  api.getPanel("watch")?.api.setActive();
  api.getPanel("callStack")?.api.setActive();
  api.getPanel("breakpoints")?.api.setActive();
  api.getPanel("timeline")?.api.setActive();
  api.getPanel("source")?.api.setActive();
  // 大きさ: 右の列は狭く、下の段は低く
  api.getPanel("watch")?.group.api.setSize({ width: 460 });
  api.getPanel("timeline")?.group.api.setSize({ height: 300 });
}

/** システム解析の配置: 時間軸（MCU と FPGA が同じ時刻の上に並ぶ）を大きく、右の列はシステム解析（MCU と FPGA の今・
 *  条件・結果の直前に多い出来事）だけで高さを全部使う。下に FPGA 観測（接続の設定）・変数の観測・ロジアナ・端末・メモリ・操作
 *  （docs/superpowers/specs/2026-10-21-system-analysis-design.md） */
function systemLayout(api: DockviewApi, add: (id: string, position?: object) => void): void {
  add("timeline");
  add("system", { direction: "right", referencePanel: "timeline" });
  add("fpgaObs", { direction: "below", referencePanel: "timeline" });
  add("watch", { referencePanel: "fpgaObs" });
  add("logicAnalyzer", { referencePanel: "fpgaObs" });
  add("terminal", { referencePanel: "fpgaObs" });
  add("memory", { referencePanel: "fpgaObs" });
  add("eventLog", { referencePanel: "fpgaObs" });
  api.getPanel("system")?.api.setActive();
  api.getPanel("fpgaObs")?.api.setActive();
  api.getPanel("timeline")?.api.setActive();
  api.getPanel("system")?.group.api.setSize({ width: 560 });
  api.getPanel("fpgaObs")?.group.api.setSize({ height: 260 });
}

/** 解析の配置（既定。画面の作り直しの仕様 §3.3 の案 2。利用者の承認 2026-10-24）。
 *  根拠: docs/reports/2026-10-24-fprobe-value-analysis.md（価値が出る場面 = 条件を置いて捕まえ、前後を 1 本の時間軸で見る・
 *  FPGA と MCU の切り分け・時間を測る・試して確かめる・止めずに見る）と 2026-10-24-competitor-ui-research.md（中心の窓を
 *  固定し周りを出し入れ・時刻の連動は既定で全部の窓）。
 *  中央 = 時間軸（共通の時計。どの場面も最後はここ）、右上 = 条件と今の値（System: MCU と FPGA・条件・結果の直前の出来事）と
 *  クロストリガー（源 × 動作の経路を 1 か所で。価値の分析 §5.7-2・§6.2 の右の「条件の窓」。System のタブに並べる）、
 *  右下 = その時刻・その停止のコードの位置（Source・Call Stack・Locals）、
 *  下 = 捕まえる・測る・試す・止めずに見る（Logic Analyzer・Event Log・Data Sampling・Fault・Verify（故障注入・規則・フック）・
 *  Recordings・Activity。前に出すのは Logic Analyzer） */
function analysisLayout(api: DockviewApi, add: (id: string, position?: object) => void): void {
  add("timeline");
  add("system", { direction: "right", referencePanel: "timeline" });
  add("crossTrigger", { referencePanel: "system" });
  add("source", { direction: "below", referencePanel: "system" });
  add("callStack", { referencePanel: "source" });
  add("locals", { referencePanel: "source" });
  add("logicAnalyzer", { direction: "below", referencePanel: "timeline" });
  add("eventLog", { referencePanel: "logicAnalyzer" });
  add("dataSampling", { referencePanel: "logicAnalyzer" });
  add("fault", { referencePanel: "logicAnalyzer" });
  add("verify", { referencePanel: "logicAnalyzer" });
  add("recordings", { referencePanel: "logicAnalyzer" });
  add("activity", { referencePanel: "logicAnalyzer" });
  api.getPanel("system")?.api.setActive();
  api.getPanel("source")?.api.setActive();
  api.getPanel("logicAnalyzer")?.api.setActive();
  api.getPanel("timeline")?.api.setActive();
  api.getPanel("system")?.group.api.setSize({ width: 600 });
  api.getPanel("source")?.group.api.setSize({ height: 430 });
  api.getPanel("logicAnalyzer")?.group.api.setSize({ height: 290 });
}

export type LayoutName = "analysis" | "debug" | "system";

export class Dock {
  readonly api: DockviewApi;
  private unsubscribe: () => void;

  constructor(element: HTMLElement, private panels: PanelDef[], context: Map<any, any>, dark: boolean) {
    const byId = new Map(panels.map((p) => [p.id, p]));
    this.api = createDockview(element, {
      theme: dark ? themeDark : themeLight,
      // 窓を外に出す（浮かせる・別のブラウザの窓へ）: タブの右クリックと見出しの右のボタン（lib/dockwindows.ts）
      getTabContextMenuItems: ({ panel }) => tabMenu(this.api, panel, t),
      createRightHeaderActionComponent: (group) => headerActions(this.api, group, t),
      createComponent: ({ name }): IContentRenderer => {
        const def = byId.get(name);
        const el = document.createElement("div");
        el.style.height = "100%";
        let instance: Record<string, any> | null = null;
        return {
          element: el,
          init: () => {
            if (def) instance = mount(def.component, { target: el, context: panelContext(context, def.id) });
          },
          dispose: () => {
            if (instance) void unmount(instance);
          },
        };
      },
    });
    if (!this.restore()) this.reset();
    this.api.onDidLayoutChange(() => save(LAYOUT_KEY, JSON.stringify(this.api.toJSON())));
    // 別の窓の題名を、前に出ている窓の名前に（OS のタスクバー・別のモニタで見分ける）
    this.api.onDidAddPopoutGroup(({ group, window }) => {
      const title = () => { window.document.title = popoutTitle(group.activePanel?.title); };
      title();
      group.api.onDidActivePanelChange(title);
    });
    // 言語を変えたらパネルの題を付け直す
    this.unsubscribe = i18n.subscribe(() => this.retitle());
  }

  private add = (id: string, position?: object) => {
    const def = this.panels.find((p) => p.id === id);
    if (!def || this.api.getPanel(id)) return;
    this.api.addPanel({ id, component: id, title: t(def.title), ...(position ? { position } : {}) } as any);
  };

  private restore(): boolean {
    try {
      const saved = load(LAYOUT_KEY, "");
      if (!saved) return false;
      this.api.fromJSON(JSON.parse(saved));
      return this.api.panels.length > 0;
    } catch {
      this.api.clear();
      return false;
    }
  }

  /** 既定の配置に戻す */
  reset(): void {
    this.layout("analysis");
  }

  /** 決まった配置にする（analysis = 解析・既定、debug = デバッガの型、system = システム解析） */
  layout(name: LayoutName): void {
    this.api.clear();
    (name === "system" ? systemLayout : name === "debug" ? debugLayout : analysisLayout)(this.api, this.add);
  }

  /** パネルを前に出す（閉じていたら既定の場所の近くに開く: デバッガの窓は右の列、ほかは下の段） */
  show(id: string): void {
    let panel = this.api.getPanel(id);
    if (!panel) {
      const def = this.panels.find((p) => p.id === id);
      const bottom = def?.group !== "debug" || id === "disassembly";
      const near = id === "source" ? undefined
        : (bottom ? this.api.getPanel("timeline") ?? this.api.getPanel("terminal") : this.api.getPanel("watch"))
          ?? this.api.activePanel;
      this.add(id, near ? { referencePanel: near.id } : undefined);
      panel = this.api.getPanel(id);
    }
    panel?.api.setActive();
  }

  setTheme(dark: boolean): void {
    this.api.updateOptions({ theme: dark ? themeDark : themeLight });
  }

  private retitle(): void {
    for (const p of this.panels) this.api.getPanel(p.id)?.api.setTitle(t(p.title));
  }

  dispose(): void {
    this.unsubscribe();
    this.api.dispose();
  }
}

/** 窓ごとの context: 要求に source "panel:<id>" を付ける（使われ方の記録。lib/source.ts） */
function panelContext(context: Map<any, any>, id: string): Map<any, any> {
  const shared = context.get(SHARED) as Shared | undefined;
  if (!shared) return context;
  const source = `panel:${id}`;
  return new Map([...context, [SHARED, { ...shared, conn: sourcedConn(shared.conn, source), app: sourcedApp(shared.app, source) }]]);
}
