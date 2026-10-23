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
- fpga_signals: FPGA の観測の組に付ける信号の定義（fprobe.fpga_signals の形）。記録していないときの既定で、
  記録を始めるとその記録の付帯情報に写す。null = まだ無い。
- system: システム解析の MCU のメモリの表と FPGA の条件 {"watches": [{"name", "address"}], "conditions": [{"name", "expr"}]}
  （別の窓・別の PC・AI が同じものを見る。名前は記録の段・条件で使うので重ならない）。null = まだ無い。

仕様: docs/superpowers/specs/2026-10-18-ui-framework-evaluation.md §4・§5 段取り 4
"""

import copy
import json
import threading
import time
from pathlib import Path

from .fpga_signals import FpgaSignals

__all__ = ["CURSORS", "UiStateTools"]

CURSORS = ("selection", "mouse", "reference")
SOURCES = ("capture", "recording")
KEYS = ("view", "cursors", "rows", "event", "frame", "sampling", "dashboard", "fpga_signals", "system")
LASTING = ("sampling", "dashboard", "fpga_signals", "system")   # デーモンの再起動を越えて残す（ui_state_persist）
SYSTEM_ITEMS = 64                                              # システム解析の表・条件の数の上限
POINTER_TTL_S = 600.0
DASHBOARD_WIDGETS = 64                                         # 部品の数の上限（画面の MAX_WIDGETS と同じ）
DASHBOARD_BYTES = 65536                                        # 配置の JSON の大きさの上限


def _default() -> dict:
    return {"view": None, "cursors": dict.fromkeys(CURSORS), "rows": None, "event": None, "frame": 0, "sampling": None,
            "dashboard": None, "fpga_signals": None, "system": None}


class UiStateTools:
    """画面の状態（Probe の mixin）。"""

    def _init_ui_state(self) -> None:
        self._ui_lock = threading.Lock()
        self._ui_state = _default()
        self._ui_rev = 0
        self._ui_pointers: dict[str, dict] = {}
        self._ui_path: Path | None = None
        self._ui_rejected: dict = {}                               # 読み戻せなかった保存の項目（そのまま書き戻す）

    def ui_state_persist(self, path: Path) -> None:
        """長く持つ項目（LASTING: データ サンプリング・操作パネル・FPGA の信号の定義・システム解析の表）を path に
        保存し、いまある内容を読み戻す（デーモンが起動時に呼ぶ。API のメソッドではない）。壊れたファイルは無視する。"""
        self._ui_path = Path(path)
        try:
            saved = json.loads(self._ui_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return                                                 # 無い・壊れた保存: 既定で始める
        if not isinstance(saved, dict):
            return
        for key in LASTING:                                        # 項目ごとに: 1 つが今の検査に通らなくても残りは戻す
            if saved.get(key) is None:
                continue
            try:
                self.ui_state_set(**{key: saved[key]})
            except (ValueError, TypeError, AttributeError):
                self._ui_rejected[key] = saved[key]                # 戻せなかった項目もファイルからは消さない

    def _ui_save(self, state: dict) -> None:
        """長く持つ項目をファイルへ（書きかけを残さないよう別名に書いて置き換える）。"""
        if self._ui_path is None:
            return
        try:
            self._ui_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._ui_path.with_suffix(".tmp")
            kept = {k: state[k] if state[k] is not None or k not in self._ui_rejected else self._ui_rejected[k]
                    for k in LASTING}
            tmp.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._ui_path)
        except OSError:
            pass                                                   # 書けない: 画面の状態はそのまま使える

    def ui_state_get(self) -> dict:
        """画面の状態（rev は変わるたびに 1 増える番号）。"""
        with self._ui_lock:
            return {"rev": self._ui_rev, **copy.deepcopy(self._ui_state), "pointers": self._pointers()}

    def ui_state_set(self, view: dict | None = None, cursors: dict | None = None, rows: list[str] | None = None,
                     event: dict | None = None, frame: int | None = None, sampling: dict | None = None,
                     dashboard: dict | None = None, fpga_signals: dict | None = None, system: dict | None = None,
                     clear: list[str] | None = None,
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
        if fpga_signals is not None:                       # 検査して既定を埋めた形で（fprobe.fpga_signals）
            patch["fpga_signals"] = FpgaSignals.from_dict(fpga_signals).to_dict()
        if system is not None:
            patch["system"] = _system(system)
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
            if any(k in LASTING for k in changed):
                self._ui_save(state)
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


def _system(system: dict) -> dict:
    """システム解析の表と条件: 名前の前後の空白を除く。番地は 32 ビットの語（4 の倍数）、名前は空でなく重ならない。
    条件は式が空でなく、名前が空なら式。"""
    watches, conditions = system.get("watches", []), system.get("conditions", [])
    if not isinstance(watches, list) or not isinstance(conditions, list) or \
            len(watches) > SYSTEM_ITEMS or len(conditions) > SYSTEM_ITEMS:
        raise ValueError(f"system.watches・conditions は {SYSTEM_ITEMS} 個までの並び")
    out_w, names = [], set()
    for w in watches:
        name = str(w.get("name", "")).strip() if isinstance(w, dict) else ""
        address = w.get("address") if isinstance(w, dict) else None
        if not name or not isinstance(address, int) or not 0 <= address <= 0xFFFF_FFFF or address % 4:
            raise ValueError(f"system.watches の {w!r}: 名前と 32 ビットの語の番地（4 の倍数）")
        if name in names:
            raise ValueError(f"system.watches の名前 {name!r} が重なる")
        names.add(name)
        out_w.append({"name": name, "address": address})
    out_c = []
    for c in conditions:
        expr = str(c.get("expr", "")).strip() if isinstance(c, dict) else ""
        if not expr:
            raise ValueError(f"system.conditions の {c!r}: 式が空")
        out_c.append({"name": str(c.get("name", "")).strip() or expr, "expr": expr})
    return {"watches": out_w, "conditions": out_c}


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
