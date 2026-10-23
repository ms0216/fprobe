// API の仕様（host/api/fprobe_api.json）から、画面が呼べるメソッドの一覧（MCP の道具と同じ範囲）。build 時に読み込む
import spec from "../../../api/fprobe_api.json";
import { apiMethods, type ApiSpec } from "./apiform";

export const METHODS = apiMethods(spec as unknown as ApiSpec);
const NAMES = new Set(METHODS.map((m) => m.name));

/** API パネルで開けるメソッドか（session.* 等は開けない） */
export const inApiPanel = (method: string): boolean => NAMES.has(method);
