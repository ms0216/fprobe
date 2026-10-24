// 窓を外に出す: 画面の中で浮かせる（dockview の floating group）と、別のブラウザの窓に出す（popout。別のモニタに置ける）。
// 画面は考えない層で状態はデーモンが持つので、別の窓に出しても時間軸のカーソル・止まった場所は同じものを見る。
// 入口はタブの右クリックのメニュー（tabMenu）と、窓の見出しの右のボタン（headerActions）
import type { ContextMenuItem, DockviewApi, DockviewGroupPanel, IDockviewPanel, IGroupHeaderProps,
  IHeaderActionsRenderer } from "dockview-core";

/** 別の窓の中身を置くページ（同じ origin。public/popout.html） */
export const POPOUT_URL = "/popout.html";

type Api = Pick<DockviewApi, "addFloatingGroup" | "addPopoutGroup">;
type Translate = (key: any) => string;

export function floatOut(api: Api, item: IDockviewPanel | DockviewGroupPanel): void {
  api.addFloatingGroup(item);
}

export function popOut(api: Api, item: IDockviewPanel | DockviewGroupPanel): Promise<boolean> {
  return api.addPopoutGroup(item, { popoutUrl: POPOUT_URL });
}

/** タブの右クリックのメニュー */
export function tabMenu(api: Api, panel: IDockviewPanel, t: Translate): ContextMenuItem[] {
  return [
    { label: t("dock.float"), action: () => floatOut(api, panel) },
    { label: t("dock.popout"), action: () => void popOut(api, panel) },
    "separator",
    { label: t("dock.close"), action: () => panel.api.close() },
  ];
}

export type GroupLocation = "grid" | "floating" | "popout";

/** 窓の場所ごとに出すボタン（浮いている窓はもう浮いている、別の窓の中では両方とも意味が無い） */
export function actionsFor(location: GroupLocation): { float: boolean; popout: boolean } {
  return { float: location === "grid", popout: location !== "popout" };
}

/** 別の窓の題名（前に出ている窓の名前。OS のタスクバーで見分ける） */
export function popoutTitle(panelTitle: string | undefined): string {
  return panelTitle ? `${panelTitle} — fprobe` : "fprobe";
}

/** 窓の見出しの右のボタン（浮かせる・別の窓に出す。どちらも窓のまとまりごと。場所が変われば出すボタンも変える） */
export function headerActions(api: Api, group: DockviewGroupPanel, t: Translate): IHeaderActionsRenderer {
  const element = document.createElement("div");
  element.className = "dock-actions";
  const button = (glyph: string, key: string, run: () => void) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = glyph;
    b.title = t(key);
    b.setAttribute("aria-label", t(key));
    b.addEventListener("click", (e) => { e.stopPropagation(); run(); });
    element.appendChild(b);
    return b;
  };
  const float = button("⧉", "dock.float", () => floatOut(api, group));
  const popout = button("⇱", "dock.popout", () => void popOut(api, group));
  const show = (location: GroupLocation) => {
    const a = actionsFor(location);
    float.hidden = !a.float;
    popout.hidden = !a.popout;
  };
  let off: { dispose(): void } | null = null;
  return {
    element,
    init: (params: IGroupHeaderProps) => {
      show(params.api.location.type as GroupLocation);
      off = params.api.onDidLocationChange((e) => show(e.location.type as GroupLocation));
    },
    dispose: () => { off?.dispose(); element.remove(); },
  };
}
