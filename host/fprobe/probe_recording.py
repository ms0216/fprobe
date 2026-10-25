"""共通の時間軸の記録（recording_*・F3）の mixin: 記録の開始・停止・一覧・開く、イベント・相関・トリガ（記録を調べて動作）・
継続時間・取引・検索・密度・波形・印・書き出し・ホストのイベントの記録。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う。Probe の _lock・session・_caps・_recorder・_collector・_view・_la と、
_job / _add_job 等を使う。部品は fprobe.recording・fprobe.collector・fprobe.correlate・fprobe.export。
"""

from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import re
import threading
import time

from .home import fprobe_home
from .api import NoData
from .collector import Collector
from .correlate import ConditionWatcher, correlate, durations, transactions
from .export import write_trace
from .fpga_signals import FpgaSignals
from .la import channel_names
from .recording import Recording

TRIGGER_PERIOD_S = 0.5                 # recording_trigger: 記録を調べる周期


FIND_LIMIT = 1000                      # recording_find・recording_intervals が返す一覧の上限の既定（総数と切ったかも返す）


def _channel_list(caps) -> list[str]:
    """記録の情報 pins: ピンの既定の名前（ch の順。名前の無い ch は ch<N>）。後から開いた記録でもピンを名前で指せる
    （recording_correlate）。時間軸の段の名前（channel_names。利用者の名前）とは別（画面は既定の名前を訳して見せる）。"""
    names = channel_names(caps)
    count = max(int(getattr(caps, "la_channels", 0) or 0), max(names, default=-1) + 1)
    return [names.get(ch, f"ch{ch}") for ch in range(count)]


class RecordingTools:
    """Probe に混ぜる共通の時間軸の記録の道具。"""

    def _init_recording(self) -> None:
        self._trigger: dict | None = None                  # recording_trigger の設定（無ければ None）

    @staticmethod
    def _recordings_dir() -> Path:
        return fprobe_home() / "recordings"

    def recording_start(self, name: str = "", events: bool = True, segment_s: float | None = None,
                        segment_bytes: int | None = None, max_bytes: int | None = None,
                        decimate_after_s: float | None = None, decimate_streams: list[str] | None = None) -> dict:
        """共通の時間軸の記録を始める。記録中は、ロジアナの捕捉・DAP / UART / SWO / 電圧のイベント・時計の対応付け・
        印・（デーモン経由なら）すべての操作とターゲットの状態の変化が、1 つの時刻の上に保存される。
        長期の記録（日単位）: segment_s（分割の時間）・segment_bytes・max_bytes（容量の上限。古い分割から消す）・
        decimate_after_s（閉じてからこの秒数で decimate_streams を間引く）のどれかを指定すると分割した記録になる。"""
        with self._lock:
            if self._recorder is not None:
                raise RuntimeError("記録中（recording_stop を先に）")
            device = self.session.device
            caps = device.capabilities()
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            safe = re.sub(r"[^\w\-]+", "_", name).strip("_")
            directory = self._recordings_dir() / (f"{stamp}-{safe}" if safe else stamp)
            self._recorder = self._make_recorder(
                directory, caps.timebase_hz,
                {"name": name, "board": caps.board, "design": caps.design,
                 "elf": str(self._symbols.path) if self._symbols else None,
                 "fpga.signals": self.ui_state_get()["fpga_signals"], "pins": _channel_list(caps)},
                segment_s, segment_bytes, max_bytes, decimate_after_s, decimate_streams)
            try:
                self._collector = Collector(device, self._recorder, lock=self._lock)
                for k, v in self._obs_delays.items():    # FPGA の観測を先に開いていれば同じ遅れで
                    setattr(self._collector.obs, k, v)
                if not events:
                    self._collector.events = None                     # イベント記録を取り込まない
                self._collector.start()
                self._set_view(Recording.open(directory))
            except BaseException:                                     # 途中で失敗: 記録中の状態を残さない
                collector, recorder = self._collector, self._recorder
                self._collector = self._recorder = None
                if collector is not None and collector._thread is not None:
                    collector._stop.set()
                recorder.close()
                raise
            # 前から動いている watch の名前も記録の変数の名前に（watch_set を記録の後に呼び直さなくても）
            if self._watch_running:            # 止めた watch の名前は写さない（値が入らない）
                self._name_watches({w.address: w.name for w in self._watched
                                    if getattr(w, "address", None) is not None and w.name})
            return {"recording": directory.name, "path": str(directory)}

    def _name_watches(self, names: dict[int, str]) -> None:
        """記録中の記録の変数の名前（sample.names）に足す（_lock の中で）。読む記録が記録中の記録のときだけ、その情報も
        合わせる（記録中に別の記録を開いていれば、その古い記録の情報は書き換えない）。"""
        recorder = self._recorder
        if recorder is None or not names:
            return
        named = dict((getattr(recorder, "_meta", None) or {}).get("sample.names") or {})
        named.update({f"0x{a:08X}": n for a, n in names.items()})
        recorder.update_meta(**{"sample.names": named})
        view = self._view
        if view is not None and getattr(view, "directory", None) == recorder.directory and hasattr(view, "update_meta"):
            view.update_meta(**{"sample.names": named})

    def recording_stop(self) -> dict:
        """記録を止める（保存した記録は recording_list / recording_open で読める）。"""
        collector, recorder = self._collector, self._recorder
        if recorder is None:
            raise RuntimeError("記録していない")
        self._stop_la_record()                                    # 流し続けの残りを入れてから閉じる
        self._linux_before_recording_stop()                       # Linux の出来事の、組を待っている分を入れる
        collector.stop()                                          # ロックの外で（集める側がロックを取るため）
        with self._lock:
            recorder.close()
            self._recorder = self._collector = None
            view = self._view                                  # 記録中に別の記録を開いていれば、その件数ではない
            same = view is not None and view.directory == recorder.directory
            return {"recording": recorder.directory.name, "events": view.count() if same else None}

    def recording_status(self) -> dict:
        """記録中か、記録の名前・件数・時刻の範囲・ホストの出来事の時刻の不確かさ。記録中は watch_running（MCU のメモリの
        読みが動いているか。false なら記録にメモリが入らない）。"""
        rec, view = self._recorder, self._view
        out = {"recording": rec is not None, "name": view.directory.name if view else None}
        if view is not None:
            if rec is not None:
                rec.flush()
            out.update(events=view.count(), span=view.span(), timebase_hz=view.meta["timebase_hz"])
        if rec is not None:
            # MCU のメモリの読み（watch）が動いているか: 止まると記録に入らない（watch_set で再開。それまでは欠け）
            out["watch_running"] = self._watch_running
            try:
                out["clock_uncertainty_ticks"] = rec.clock.to_ticks(time.monotonic())[1]
            except RuntimeError:
                out["clock_uncertainty_ticks"] = None
        if view is not None:
            out["segmented"] = view.meta.get("layout") == "segmented"
        collector = self._collector                     # イベントの読み出しの失敗（取り込みは続く。欠落は lost）
        if collector is not None and collector.errors:
            out["events_errors"] = {"count": collector.errors, "last": collector.last_error, "lost": collector.lost}
        la_record = self._la_record                    # 別のスレッド（条件で止める等）が None にするので一度だけ読む
        if la_record is not None:
            out["la_stream"] = la_record.status()
        return out

    def recording_list(self) -> dict:
        """保存した記録の一覧（新しい順）。"""
        root = self._recordings_dir()
        items = []
        for d in sorted(root.glob("*/meta.json"), reverse=True):
            meta = json.loads(d.read_text(encoding="utf-8"))
            items.append({"name": d.parent.name, "title": meta.get("name"), "board": meta.get("board")})
        return {"recordings": items}

    def recording_open(self, name: str) -> dict:
        """保存した記録を開く（以後の recording_events 等の対象）。name は recording_list の name。"""
        directory = self._recordings_dir() / name
        if directory.parent != self._recordings_dir() or not (directory / "meta.json").is_file():
            raise KeyError(f"記録 {name!r} が無い")
        view = Recording.open(directory)
        self._set_view(view)
        return {"name": name, "events": view.count(), "span": view.span()}

    def _set_view(self, view: Recording) -> None:
        """読む記録を替え、前の記録を閉じる（開いたままだと Windows でフォルダを消せない。参照の循環で GC は遅い）。
        ponytail: 別のスレッドで読んでいる最中の前の記録の問い合わせは 1 回だけ失敗する（画面は次の要求で読み直す）。"""
        with self._view_lock:
            old, self._view = self._view, view
            if old is not None and old is not view:
                old.close()

    def _reading(self) -> Recording:
        view, recorder = self._view, self._recorder     # 別のスレッドが替える・None にするので一度だけ読む
        if view is None:
            raise NoData("読む記録が無い（recording_start か recording_open を先に）")
        if recorder is not None:
            recorder.flush()
        return view

    def recording_events(self, t0: int | None = None, t1: int | None = None, streams: list[str] | None = None,
                         kinds: list[str] | None = None, limit: int = 1000) -> dict:
        """時刻（タイムベース）の範囲の出来事を時刻順に。streams の例: waveform 以外の dap / uart / swo / rtt / aux /
        host / target / annotation / sample（a 番地・b 値・name 記録の変数の名前）/ fpga（kind value の a 組・b 生の値。fields に記録の信号の
        定義で名前ごとの値。fpga の value_untimed・gap_untimed・group_untimed は、時刻合わせの印が来ないまま鎖が終わって時刻の決まらない記録（t は受けた時 = 起きた時刻の上限））/ clock。
        先頭 limit 件（total = 範囲の総数・truncated = 切ったか・limit = 使った上限）。"""
        view = self._reading()
        signals = FpgaSignals.from_dict(view.meta.get("fpga.signals"))
        names = {int(a, 16): n for a, n in (view.meta.get("sample.names") or {}).items()}
        out = []
        for e in view.events(t0, t1, streams, kinds, limit):
            item = asdict(e)
            if e.stream == "sample" and e.a in names:
                item["name"] = names[e.a]
            if e.stream == "fpga" and e.kind == "value":
                item["fields"] = signals.fields(e.a, e.b)
            out.append(item)
        total = view.count_events(t0, t1, streams, kinds)
        return {"events": out, "total": total, "truncated": total > len(out), "limit": limit}

    def recording_correlate(self, effect: str, causes: list[str] | None = None, window_s: float | None = None,
                            t0: int | None = None, t1: int | None = None, examples: int = 5, limit: int = 20,
                            gap_s: float | None = None, max_coverage: float | None = None) -> dict:
        """結果の直前に多い出来事: 結果の条件 effect（例 "誤り changed"）の各時刻の直前（window_s 秒の中）に、causes の各条件
        （例 "restart changed and state=BUSY"・"in0 rose"）が来ていた割合（preceded）・記録のうち候補の後 window_s 以内に
        当たる時間の割合（coverage）・後に結果が来た候補の割合（followed）・遅れ・例の時刻。測った事実だけを返す（判定・
        経験則の順位づけをしない）。並びは preceded の大きい順、同じなら条件の文字の順。変数は MCU のメモリの名前
        （watch_set の names）・番地、FPGA の信号の名前、ピンの名前・ch<N>（同じ名前は mcu. / fpga. / pin. で区別）。
        項: <変数> changed / rose / fell、<変数>=<値>（列挙の名前も）を and でつなぐ。sampling = 変数の読みの間隔（秒。
        遅れの分解能）、fpga_hidden = 標本型で標本の間に見えなかった変化の数。causes を省くと記録の変数から候補を作って
        調べ（auto。信号の変化・列挙の値に入った瞬間・変化 かつ 列挙 = 値の組・ピンの rose / fell・ほかのメモリの変化）、
        上位 limit 個を返す（切る前の数 causes_total・切ったか truncated・使った limit。max_coverage を渡せば coverage が
        それを超える候補を除く）。gap_s を渡せば、それより長い
        MCU のメモリの読みの空白を欠けにする（既定は欠けにしない。読みの事実は reads）。gaps = 使った変数の
        データの欠け（時刻・reason。warnings にも合計）。judged = 先行率の分母（窓が候補の変数の欠けにかかる結果と、結果の変数の欠けの直後の読みで見えた結果は除く）。
        fpga_hidden = 標本型の FPGA の観測で、標本の間に変わって見えなかった変化の数（信号を問わない合計。多ければ
        FPGA の 1 回きりの変化を見落としうる。記録型なら 0）。sampling の null = 決まった読みの間隔が無い変数
        （FPGA の信号は変化を記録する）。effect.delta = 結果の MCU のメモリの範囲の最初と最後の値の差（count と違えば、1 回の
        読みで何回分も動いている）。"""
        return correlate(self._reading(), effect, causes, window_s, t0, t1, examples, limit, gap_s, max_coverage)

    def recording_trigger_set(self, condition: str, action: str = "mark", post_s: float = 1.0,
                              lag_s: float = 2.0) -> dict:
        """記録中に、条件（recording_correlate と同じ書き方。MCU のメモリ・FPGA の信号・ピンをまたげる）が成り立つたびに
        記録に印を付ける（stream annotation の mark）。action = stop なら、最初に成り立った時刻から post_s 秒が記録に
        入ったら記録を止める（めったに起きない不具合を前後つきで残す）。判定は PC で、今から lag_s 秒前までを調べる
        （流れ・取り込みが記録に届くのを待つ）。調べ終えた時刻より前に後から届いた出来事は調べず、その数を late に返す。
        記録の始めから調べる。様子は recording_trigger_status、やめるのは recording_trigger_clear。"""
        if action not in ("mark", "stop"):
            raise ValueError("action は mark / stop")
        if not post_s >= 0:
            raise ValueError("post_s は 0 以上")
        if not lag_s >= 0:
            raise ValueError("lag_s は 0 以上")
        with self._lock:
            if self._recorder is None:
                raise RuntimeError("記録していない（recording_start を先に）")
            watcher = ConditionWatcher(condition)                      # 条件の形はここで確かめる
            self._clear_trigger()
            # 記録中の記録を自分で開いて読む（画面・AI が別の記録を開いて読む記録 _view を替えても、見張りは替わらない）
            self._trigger = {"condition": watcher.condition.text, "action": action, "post_s": post_s, "lag_s": lag_s,
                             "watcher": watcher, "view": Recording.open(self._recorder.directory), "checked": 0, "hits": 0, "first_t": None, "last_t": None,
                             "state": "armed", "error": None, "late": 0, "_checked_count": 0}
            self._add_job("recording.trigger", TRIGGER_PERIOD_S, self._trigger_poll)
            return self.recording_trigger_status()

    def recording_trigger_status(self) -> dict:
        """条件・action・lag_s・回数 hits・最初と最後に成り立った時刻（タイムベース）・調べ終えた時刻 checked・
        調べ終えた後に届いて調べなかった出来事の数 late・状態（off / armed = まだ / fired = 成り立った / stopped = 記録を
        止めた / error）。"""
        trig = self._trigger
        if trig is None:
            return {"state": "off"}
        return {k: v for k, v in trig.items() if k not in ("watcher", "view") and not k.startswith("_")}

    def recording_trigger_clear(self) -> dict:
        """条件の見張りをやめる。"""
        with self._lock:
            self._clear_trigger()
            return {"state": "off"}

    def _clear_trigger(self) -> None:
        self._remove_job("recording.trigger")
        trig = self._trigger
        if trig is not None:
            trig["view"].close()
        self._trigger = None

    def _trigger_poll(self, now: int | None = None) -> None:
        """recording_trigger の周期の仕事（_lock の中で）: 前に調べた所から now（既定: 今 − lag_s）までを調べる。"""
        trig = self._trigger
        if trig is None or trig["state"] in ("stopped", "error"):
            return
        if self._recorder is None:                                      # 記録が止まった: 見張りも終わる
            self._remove_job("recording.trigger")
            trig["view"].close()
            trig["state"] = "stopped" if trig["state"] == "fired" else "off"
            return
        hz = self._recorder.timebase_hz
        if now is None:
            now = self.session.device.time() - int(trig["lag_s"] * hz)
        try:
            self._recorder.flush()
            view = trig["view"]
            if trig["checked"]:                                         # 調べ終えた所より前に後から届いた出来事
                trig["late"] += view.count_events(t1=trig["checked"]) - trig["_checked_count"]
            hits = trig["watcher"].scan(view, trig["checked"], now)
        except Exception as e:                                          # 条件の変数が記録に無い等
            trig.update(state="error", error=f"{type(e).__name__}: {e}")
            self._remove_job("recording.trigger")
            trig["view"].close()
            return
        trig["checked"] = max(trig["checked"], now)
        for t in hits:
            self._recorder.add("annotation", "mark", t, body={"text": f"条件が成り立った: {trig['condition']}"})
        self._recorder.flush()                                          # 自分の印も数えた後で、届いた分の数を覚える
        trig["_checked_count"] = trig["view"].count_events(t1=trig["checked"])
        if hits:
            trig["hits"] += len(hits)
            trig["first_t"] = trig["first_t"] if trig["first_t"] is not None else hits[0]
            trig["last_t"] = hits[-1]
            trig["state"] = "fired"
        if trig["action"] == "stop" and trig["first_t"] is not None and now >= trig["first_t"] + trig["post_s"] * hz:
            trig["state"] = "stopped"
            self._remove_job("recording.trigger")
            trig["view"].close()
            # 止めるのはロックの外で（ロジアナの流れの受け取りがロックを取る。recording_la_stream_stop）
            threading.Thread(target=self._trigger_stop, args=(trig["condition"],), name="fprobe-trigger-stop",
                             daemon=True).start()

    def _trigger_stop(self, condition: str) -> None:
        """条件で記録を止め、誰が・なぜ止めたかを通知 recording で知らせる（デーモンを通らない止め方なので）。"""
        result = self.recording_stop()
        publish = self._publish
        if publish is not None:
            publish("recording", {"action": "stop", "name": result.get("recording"), "reason": f"条件: {condition}",
                                  "client": {"id": None, "name": "trigger", "role": "script"}})

    def recording_durations(self, variable: str, t0: int | None = None, t1: int | None = None, limit: int = 20,
                            gap_s: float | None = None) -> dict:
        """変数（recording_correlate と同じ指し方）の値ごとの回数・続いた時間（両端の見えている区間。秒の最小・中央値・
        90 % 点・最大）・その値だった時間の割合。続いた時間の合計の大きい順に limit 個（値の総数 values_total・切ったか
        truncated・使った limit）。例: FPGA の state が BUSY のまま続く時間を MCU の待ち時間と比べる。データの欠け
        にかかる回は数えず excluded（欠けは gaps）。"""
        return durations(self._reading(), variable, t0, t1, limit, gap_s)

    def recording_transactions(self, start: str, by: str | None = None, counts: list[str] | None = None,
                               effect: str | None = None, t0: int | None = None, t1: int | None = None,
                               rows: int = 20, by_at: str = "start", effect_lag_s: float = 0.0,
                               gap_s: float | None = None) -> dict:
        """記録を区間（リクエスト等の 1 回の取引）に切って数える（条件・変数は recording_correlate と同じ書き方）。区間 =
        start（瞬間の項のある条件）が成り立ってから次に成り立つまで。区間ごとに by の開始時の値・counts の各条件の回数・
        effect が起きたか（by_at "end" なら by は区間の終わりの値。例 start "requests changed"・by "last_b"・by_at "end" で
        要求ごとの B）。groups = by の値ごとの区間の数・effect の割合（effect_rate）・counts の平均、rows = 最初の rows
        区間（入れうる区間の数 rows_total・切ったか rows_truncated・使った rows は rows_limit）。例: start "state changed and state=BUSY"、by "left_hi"、counts ["restart changed and state=BUSY"]、
        effect "errors changed" → どの入力の要求が再試行され、再試行された要求が誤るか。データの欠けにかかる区間は groups・rows に
        入れず excluded（変数ごとの内訳は excluded_by、欠けは gaps）。length_s = 区間の長さ、effect_offset_s = 区間の始め
        から結果までの時間（秒の分位。結果が始めの直後に集まるなら、前の区間の結果が次に入っている可能性: 区間の切り方を
        見直すか、effect_lag_s でその秒数だけ前の時刻の区間に割り当てる）。effect があれば by_effect = 結果が起きた区間（with）と起きなかった区間（without）の
        数・counts の平均（失敗した回と普段の回の違い）。"""
        return transactions(self._reading(), start, by, counts, effect, t0, t1, rows, by_at, effect_lag_s, gap_s)

    def recording_find(self, query: dict, limit: int = FIND_LIMIT) -> dict:
        """条件に一致する出来事の一覧。query: stream / kind / t0 / t1 / a / b（値か {"eq"|"lt"|"ge"…: 値}）/
        body（{欄: 値か {"regex": …}}）。events は時刻の順に先頭 limit 件（count = 総数、truncated = 切ったか、
        limit = 使った上限）。"""
        if limit < 0:
            raise ValueError("limit は 0 以上")
        found = self._reading().find(query)
        return {"count": len(found), "events": [asdict(e) for e in found[:limit]], "truncated": len(found) > limit,
                "limit": limit}

    def recording_intervals(self, start: dict, end: dict, limit: int = FIND_LIMIT) -> dict:
        """開始の条件から次の終了の条件までの時間（タイムベース）の一覧と統計（最小・最大・平均・標準偏差。統計は
        全部の組から）。intervals は先頭 limit 組（count = 総数、intervals_truncated = 切ったか、limit = 使った上限）。"""
        if limit < 0:
            raise ValueError("limit は 0 以上")
        stats = self._reading().intervals(start, end)
        stats["intervals_truncated"] = len(stats["intervals"]) > limit
        stats["intervals"] = stats["intervals"][:limit]
        stats["limit"] = limit
        return stats

    def recording_density(self, t0: int, t1: int, width: int = 1000, streams: list[str] | None = None) -> dict:
        """画素ごとの出来事の件数（縮図・タイムラインの要約）。長期の記録で間引いた・消した部分にかかれば coverage と、
        端がそこにかかって数えられない画素の inexact（[{pixel, max}]。counts はその画素では残っている出来事だけの下限、
        max は上限。消した分割なら None。按分で推定しない）。"""
        view = self._reading()
        bounds = view.density_bounds(t0, t1, width, streams)
        out = {"counts": bounds["counts"]}
        if bounds["inexact"]:
            out["inexact"] = bounds["inexact"]
        coverage = view.coverage(t0, t1)
        if not coverage["complete"]:
            out["coverage"] = coverage
        return out

    def recording_waveform(self, t0: int, t1: int, width: int = 1000) -> dict:
        """記録の波形を画素ごとに '0' / '1' / 'x'（変化）/ '-'（波形が無い）。"""
        return {"levels": self._reading().waveform_view(t0, t1, width)}

    def recording_mark(self, text: str, t: int | None = None, caller: dict | None = None) -> dict:
        """印とメモを残す（調査の経緯）。t（タイムベース）を省くと記録中の記録のいまの時刻、指定するとその時刻
        （記録中の記録か、開いた記録。時間軸で選んだ時刻に後から印を置く）。caller（デーモンが渡す呼び出し元
        {id, name, role}。引数の仕様には出ない）は置いた者として印の本文の client に残す（デーモンを通さなければ無し）。"""
        body: dict = {"text": text, **({"client": dict(caller)} if caller is not None else {})}
        rec = self._recorder
        if rec is not None:
            if t is None:
                return {"t": rec.add_host("annotation", "mark", time.monotonic(), body=body)}
            rec.add("annotation", "mark", t, body=body)
            return {"t": t}
        if t is None:
            raise RuntimeError("記録していない（recording_start を先に。開いた記録には t（タイムベース）を指定して印を足す）")
        self._reading().add("annotation", "mark", t, body=body)
        return {"t": t}

    def recording_export(self, format: str, path: str, t0: int | None = None, t1: int | None = None,
                         streams: list[str] | None = None, waveform: bool = True,
                         channels: list[int] | None = None) -> dict:
        """記録を書き出す。format: vcd（波形）/ csv / json（出来事）/ perfetto（Chrome JSON のトレースイベント。
        Perfetto UI・chrome://tracing で開く）。t0 / t1 / streams は csv・json・perfetto の範囲と種類、waveform・channels は
        perfetto の波形（channels は vcd も）。"""
        r = self._reading()
        if format == "perfetto":
            return write_trace(r, Path(path), t0=t0, t1=t1, streams=streams, waveform=waveform, channels=channels,
                               names=self._counter_names())
        if format == "vcd":
            if t0 is not None or t1 is not None or streams is not None:
                raise ValueError("vcd は範囲・streams で絞れない（波形の全体。channels だけ）")
            r.export_vcd(Path(path), channels)
        else:
            {"csv": r.export_csv, "json": r.export_json}[format](Path(path), t0=t0, t1=t1, streams=streams)
        return {"path": path}

    def record_host_event(self, stream: str, kind: str, body: dict) -> None:
        """デーモンが、操作の記録（activity）やターゲットの状態の変化を記録中の記録へ入れる口。"""
        rec = self._recorder
        if rec is not None:
            try:
                rec.add_host(stream, kind, time.monotonic(), body=body)
            except RuntimeError:
                pass                                              # 時計の対応付けの前（始めた直後）
