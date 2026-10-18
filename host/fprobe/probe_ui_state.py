"""Probe の道具: 画面の状態（人の画面・別の窓・AI が共有する「いま見ているところ」）。

Probe（fprobe.mcp_server）に混ぜて使う mixin。機器には触らない。変わるたびに通知 "ui.state" を送る（送り先は
デーモンが attach_publisher でつなぐ _publish）。誰が変えたかはデーモンの操作の記録（ui.state.set）に残る。

状態:
- view: 時間軸の視点 {"source": "capture" | "recording", "t0", "t1"}（[t0, t1)。単位は源の時刻 = 捕捉はサンプル番号、
  記録はタイムベース）。null = 画面に任せる（全体）。
- cursors: カーソル 3 種 {"selection", "mouse", "reference"}（同じ単位。null = 無い）。差は selection − reference。
- rows: 開いている段の id（"wave:<ch>" = ロジアナのチャンネル、ほかは記録の段の名前。画面の段の一覧の id。並びは表示の順）。
  null = 画面に任せる。
- event: 選んだ出来事 {"stream", "t", "t_end"?, "kind"?, "text"?, "row"?（段の id）}。null = 無い。
- frame: 呼び出し履歴で選んだフレーム（0 = 止まった関数）。
- sampling: データ サンプリングの式の一覧と周期 {"expressions": [{"expr", "plot", "axis"}], "rate_hz"}（別の窓・別の
  PC・AI が同じ一覧を見る。取っているかは watch.status）。null = まだ無い。
- dashboard: 操作パネル（G10）の部品の配置 {"version": 1, "widgets": [{"id", "kind", "expr", …}]}（別の窓・遠隔（--lan）の
  画面・AI が同じ配置を見る。部品の中身の検証と直しは画面がする。ここでは形と大きさだけ確かめる）。null = まだ無い。
- pointers: 窓・AI ごとのマウスのカーソル（cursors.mouse を渡した利用者ごと。人の画面がほかの窓・AI の指す時刻を
  時間軸に描く）。[{"key", "t", "source", "name", "role", "client", "time"}]。key は窓の印 origin（無ければ利用者の番号）、
  name・role・client（利用者の番号）はデーモンが渡す呼び出し元（caller）。mouse = null で消え、窓・利用者が切れたら
  デーモンが ui_state_client_left で消す。どちらも届かなかったもの（MCP 越しで切れ方が分からない等）は POINTER_TTL_S 秒で
  古いものから消える。

仕様: docs/superpowers/specs/2026-10-18-ui-framework-evaluation.md §4・§5 段取り 4
"""

import copy
import json
import threading
import time

__all__ = ["CURSORS", "UiStateTools"]

CURSORS = ("selection", "mouse", "reference")
SOURCES = ("capture", "recording")
KEYS = ("view", "cursors", "rows", "event", "frame", "sampling", "dashboard")
POINTER_TTL_S = 600.0
DASHBOARD_WIDGETS = 64                                         # 部品の数の上限（画面の MAX_WIDGETS と同じ）
DASHBOARD_BYTES = 65536                                        # 配置の JSON の大きさの上限


def _default() -> dict:
    return {"view": None, "cursors": dict.fromkeys(CURSORS), "rows": None, "event": None, "frame": 0, "sampling": None,
            "dashboard": None}


class UiStateTools:
    """画面の状態（Probe の mixin）。"""

    def _init_ui_state(self) -> None:
        self._ui_lock = threading.Lock()
        self._ui_state = _default()
        self._ui_rev = 0
        self._ui_pointers: dict[str, dict] = {}

    def ui_state_get(self) -> dict:
        """画面の状態（rev は変わるたびに 1 増える番号）。"""
        with self._ui_lock:
            return {"rev": self._ui_rev, **copy.deepcopy(self._ui_state), "pointers": self._pointers()}

    def ui_state_set(self, view: dict | None = None, cursors: dict | None = None, rows: list[str] | None = None,
                     event: dict | None = None, frame: int | None = None, sampling: dict | None = None,
                     dashboard: dict | None = None, clear: list[str] | None = None,
                     origin: str | None = None, caller: dict | None = None) -> dict:
        """画面の状態を変える（渡したものだけ。cursors は渡したカーソルだけ、null で消す）。clear に挙げたもの
        （view / cursors / rows / event / frame / sampling / dashboard）は既定（null・すべて・0）に戻す。sampling はデータ サンプリングの
        式の一覧と周期。dashboard は操作パネルの
        部品の配置。origin は変えた窓の印（通知に
        そのまま載る。窓は自分の変更の通知を読み飛ばす）。変わった項目を changed に返し、通知 "ui.state" を送る。
        caller（デーモンが渡す呼び出し元 {id, name, role}）: cursors.mouse を渡したら、その窓・利用者のマウスの
        カーソル（pointers）も変える。"""
        for key in clear or []:
            if key not in KEYS:
                raise ValueError(f"clear の {key!r} は無い（{' / '.join(KEYS)}）")
        patch = {}
        if view is not None:
            patch["view"] = _view(view)
        if rows is not None:
            if not all(isinstance(r, str) and r for r in rows):
                raise ValueError("rows は段の名前（文字列）の並び")
            patch["rows"] = list(dict.fromkeys(rows))
        if event is not None:
            patch["event"] = _event(event)
        if frame is not None:
            if frame < 0:
                raise ValueError("frame は 0 以上")
            patch["frame"] = frame
        if sampling is not None:
            patch["sampling"] = _sampling(sampling)
        if dashboard is not None:
            patch["dashboard"] = _dashboard(dashboard)
        with self._ui_lock:
            before = copy.deepcopy(self._ui_state)
            state = self._ui_state
            default = _default()
            for key in clear or []:
                state[key] = default[key]
            state.update(patch)
            if cursors is not None:
                state["cursors"] = {**state["cursors"], **_cursors(cursors)}
            changed = [k for k in KEYS if state[k] != before[k]]
            if cursors is not None and "mouse" in cursors and self._point(origin, caller, cursors["mouse"], state):
                changed.append("pointers")
            if changed:
                self._ui_rev += 1
            out = {"rev": self._ui_rev, **copy.deepcopy(state), "pointers": self._pointers()}
        if changed:
            publish = getattr(self, "_publish", None)
            if publish is not None:
                publish("ui.state", {"rev": out["rev"], "origin": origin, "changed": changed,
                                     "state": {k: out[k] for k in KEYS}, "pointers": out["pointers"]})
        return {**out, "changed": changed}

    def _point(self, origin: str | None, caller: dict | None, t: int | None, state: dict) -> bool:
        """窓・利用者のマウスのカーソルを置く（t = None で消す）。変わったら True（_ui_lock の中で）。"""
        key = origin or (f"client:{caller['id']}" if caller and "id" in caller else "local")
        before = self._ui_pointers.get(key)
        if t is None:
            return self._ui_pointers.pop(key, None) is not None
        view = state.get("view")
        self._ui_pointers[key] = {"key": key, "t": t, "source": view["source"] if view else None,
                                  "name": (caller or {}).get("name"), "role": (caller or {}).get("role"),
                                  "client": (caller or {}).get("id"), "time": time.time()}
        return before is None or before["t"] != t or before["source"] != self._ui_pointers[key]["source"]

    def ui_state_client_left(self, client_id: int) -> bool:
        """利用者（窓・AI）が切れた: その利用者のマウスのカーソルを消し、消えたら通知 "ui.state" を送る（デーモンが呼ぶ。
        API のメソッドではない）。消したものがあれば True。"""
        with self._ui_lock:
            gone = [k for k, p in self._ui_pointers.items() if p.get("client") == client_id]
            for key in gone:
                del self._ui_pointers[key]
            if not gone:
                return False
            self._ui_rev += 1
            out = {"rev": self._ui_rev, **copy.deepcopy(self._ui_state), "pointers": self._pointers()}
        publish = getattr(self, "_publish", None)
        if publish is not None:
            publish("ui.state", {"rev": out["rev"], "origin": None, "changed": ["pointers"],
                                 "state": {k: out[k] for k in KEYS}, "pointers": out["pointers"]})
        return True

    def _pointers(self) -> list[dict]:
        """いまのマウスのカーソル（古いものは捨てる。_ui_lock の中で）。"""
        now = time.time()
        for key in [k for k, p in self._ui_pointers.items() if now - p["time"] > POINTER_TTL_S]:
            del self._ui_pointers[key]
        return [dict(p) for p in self._ui_pointers.values()]


def _view(view: dict) -> dict:
    source = view.get("source", "capture")
    if source not in SOURCES:
        raise ValueError(f"view.source は {' / '.join(SOURCES)}")
    t0, t1 = view.get("t0"), view.get("t1")
    if not isinstance(t0, int) or not isinstance(t1, int) or t1 <= t0:
        raise ValueError("view は整数の t0 < t1")
    return {"source": source, "t0": t0, "t1": t1}


def _cursors(cursors: dict) -> dict:
    out = {}
    for name, t in cursors.items():
        if name not in CURSORS:
            raise ValueError(f"カーソル {name!r} は無い（{' / '.join(CURSORS)}）")
        if t is not None and not isinstance(t, int):
            raise ValueError(f"カーソル {name} は整数か null")
        out[name] = t
    return out


def _sampling(sampling: dict) -> dict:
    """データ サンプリングの一覧: 式の前後の空白を除き、空・重なりを除く。plot の既定は真、axis は left / right。"""
    items = sampling.get("expressions", [])
    if not isinstance(items, list):
        raise ValueError("sampling.expressions は式の並び")
    out, seen = [], set()
    for item in items:
        expr = item.get("expr") if isinstance(item, dict) else None
        if not isinstance(expr, str) or not expr.strip():
            raise ValueError("sampling.expressions の各項目は空でない expr が要る")
        axis = item.get("axis", "left")
        if axis not in ("left", "right"):
            raise ValueError("sampling.expressions の axis は left / right")
        expr = expr.strip()
        if expr not in seen:
            seen.add(expr)
            out.append({"expr": expr, "plot": bool(item.get("plot", True)), "axis": axis})
    rate = sampling.get("rate_hz")
    if rate is not None and (not isinstance(rate, (int, float)) or isinstance(rate, bool) or rate <= 0):
        raise ValueError("sampling.rate_hz は正の数か null")
    return {"expressions": out, "rate_hz": None if rate is None else float(rate)}


def _dashboard(layout: dict) -> dict:
    """操作パネルの配置の形と大きさ（version 1、部品は id・kind・expr が文字列の辞書、64 個まで、JSON で 64 KiB まで）。"""
    if not isinstance(layout, dict) or layout.get("version") != 1:
        raise ValueError("dashboard は version 1 の配置")
    widgets = layout.get("widgets")
    if not isinstance(widgets, list) or len(widgets) > DASHBOARD_WIDGETS:
        raise ValueError(f"dashboard.widgets は部品の並び（{DASHBOARD_WIDGETS} 個まで）")
    for w in widgets:
        if not isinstance(w, dict) or not all(isinstance(w.get(k), str) for k in ("id", "kind", "expr")):
            raise ValueError("dashboard.widgets の各部品は文字列の id・kind・expr が要る")
    if len(json.dumps(layout, ensure_ascii=False).encode()) > DASHBOARD_BYTES:
        raise ValueError(f"dashboard は JSON で {DASHBOARD_BYTES} バイトまで")
    return copy.deepcopy(layout)


def _event(event: dict) -> dict:
    if not isinstance(event.get("stream"), str) or not isinstance(event.get("t"), int):
        raise ValueError("event は stream（文字列）と t（整数）が要る")
    keep = ("stream", "t", "t_end", "kind", "text", "seq", "row")
    return {k: event[k] for k in keep if event.get(k) is not None}
