"""対象の FPGA の観測（fpga_obs_*）のサービス。FEATURES の FPGA_OBS がある構成だけ。値はイベント 8・9 で記録に入る。

Probe（fprobe.probe）が API の名前を委ねるサービス（fprobe.probe_service。ホストから使う名前は NEEDS）。Probe の _lock・session・_caps・_recorder・_collector・_view・_la_record・_events.read_events（EventService）・
_add_job / _remove_job・ui_state_get / ui_state_set を使う。観測の回路は fprobe.fpga_obs.FpgaObs。
"""

import math

from .collector import unwrap_events, unwrap_obs_sample
from .correlate import ConditionError
from .fpga_obs import FpgaObs, samples as obs_samples
from .fpga_signals import FpgaSignals
from .obs_condition import count_condition
from .probe_service import HostView

REOPEN_PERIOD_S = 1.0                  # fpga_obs_open の auto_reopen: 止まっていないか見る周期
DEFAULT_HALF_PERIOD = 10               # fpga_obs_open で tck_hz も half_period も渡さないときの半周期（今までの既定）
HALF_PERIOD_MIN, HALF_PERIOD_MAX = 2, 0xFFFF     # 観測の半周期の範囲（fprobe.fpga_obs.FpgaObs.open が確かめる範囲）


class FpgaObsService:
    """Probe が委ねる対象の FPGA の観測の道具。"""

    API = ("fpga_obs_close", "fpga_obs_count", "fpga_obs_open", "fpga_obs_read", "fpga_obs_register_read",
           "fpga_obs_register_write", "fpga_obs_select", "fpga_obs_signals_get", "fpga_obs_signals_set",
           "fpga_obs_start", "fpga_obs_status", "fpga_obs_stop")
    # ほかの領域（Probe の委譲）・試験に出す名前
    SHARED = ("_fpga_latest", "_fpga_obs", "_fpga_obs_opened", "_fpga_obs_reopen", "_obs", "_obs_delays", "_open_obs")
    # ホスト（Probe の核と、ほかの領域）から使う名前
    NEEDS = ("_add_job", "_caps", "_collector", "_la_record", "_lock", "_read_events", "_recorder", "_remove_job",
             "_view", "session", "ui_state_get", "ui_state_set")

    def __init__(self, host):
        self._host = HostView(host, self.NEEDS, type(self).__name__)
        self._fpga_obs: FpgaObs | None = None              # 開いた観測（_obs が機器に合わせて作る）
        self._fpga_obs_opened: dict | None = None          # 開いた設定と申告（config・declared・auto_reopen・reopened）
        self._fpga_latest: dict = {}                       # グループごとの最後に分かった値（system_status が見せる）
        self._obs_delays: dict = {}                        # 申告の遅れ（取り込みの復号器・fpga_obs_read に渡す）

    def fpga_obs_open(self, chain: dict | None = None, user_ir: int = 2, half_period: int | None = None, words: int = 64,
                      sync_period: int = 100, auto_reopen: bool = False, id_scans: int = 0,
                      borrow_ratio: bool = False, tck_hz: float | None = None) -> dict:
        """対象の FPGA の JTAG で観測の回路 v2（target_ip/fprobe_obs）を読み始め、その申告（id・version・width・groups・
        fifo_depth・clock_hz・sync_delay・sample_delay・lost_bits）を返す。chain = 鎖での対象の位置 {ir_len（既定 4）,
        ir_before, ir_after, dr_before, dr_after}、user_ir = 対象の USER の命令、half_period = TCK の半周期（sys の周期数。
        obs_soft_core の 1 クロック版は TCK ≤ 対象の clk / 8）、words = 1 スキャンの記録の語数、sync_period = 印（時刻
        合わせ）の間隔（スキャンの数）、id_scans = 始めに ID の応答を待つスキャンの上限（0 = 上限なし。上限まで来なければ
        止まり、status の stuck・id_timeout）。値を記録に残すには events_start も要る。auto_reopen = 観測の回路が答えなくなって
        止まったら（stuck。対象の FPGA を書き直した・電源を入れ直した等）、1 秒ごとに同じ設定で開き直す（記録中なら印を
        残す。回数は fpga_obs_status の reopened）。止まった回路の線をまた動かすので、利用者が選んだときだけ。
        borrow_ratio = 印が 1 つしか無い鎖の時刻に、前の鎖の印の組から求めた傾きを使う（前の鎖と時計の比が同じという
        仮定。使う側が明示したときだけ。既定は申告の CLOCK_HZ の比、無ければ時刻なし。値の time_basis が決め方）。
        tck_hz = 求める TCK（Hz）: それを超えない最も速い半周期を選ぶ（観測の回路も jtag_phy なので TCK = sys /
        (2 × 半周期 + Capabilities.jtag_bit_extra_cycles)。half_period と同時には渡せない。どちらも無ければ半周期 10）。
        実際の TCK は fpga_obs_status の tck_hz（システムのクロックの申告が無ければ null）。config は半周期で覚える
        （そのまま fpga_obs_open に渡せば同じ設定で開き直せる）。"""
        with self._host._lock:
            caps = self._host.session.device.capabilities()
            half_period = self._half_period(caps, half_period, tck_hz)
            config = {"chain": chain, "user_ir": user_ir, "half_period": half_period, "words": words,
                      "sync_period": sync_period, "id_scans": id_scans, "borrow_ratio": borrow_ratio}
            info = self._open_obs(config)
            actual = caps.sys_clock_hz / (2 * half_period + caps.jtag_bit_extra_cycles) if caps.sys_clock_hz else None
            self._fpga_obs_opened.update(auto_reopen=auto_reopen, reopened=0, tck_hz=actual)
            if auto_reopen:
                self._host._add_job("fpga_obs.reopen", REOPEN_PERIOD_S, self._fpga_obs_reopen)
            else:
                self._host._remove_job("fpga_obs.reopen")
            return info

    @staticmethod
    def _half_period(caps, half_period: int | None, tck_hz: float | None) -> int:
        """fpga_obs_open の半周期: tck_hz（求める TCK）なら、超えない最も速い半周期（HALF_PERIOD_MIN〜MAX に収める）。"""
        if tck_hz is None:
            return DEFAULT_HALF_PERIOD if half_period is None else half_period
        if half_period is not None:
            raise ValueError("tck_hz と half_period は同時に渡せない（どちらか 1 つ）")
        if not tck_hz > 0:
            raise ValueError(f"tck_hz は正（{tck_hz}）")
        if not caps.sys_clock_hz:
            raise ValueError("プローブがシステムのクロック（sys_clock_hz）を申告しない: tck_hz を半周期に換算できない（half_period で渡す）")
        half = math.ceil((caps.sys_clock_hz / tck_hz - caps.jtag_bit_extra_cycles) / 2 - 1e-9)   # 誤差で 1 つ遅くしない
        return min(HALF_PERIOD_MAX, max(HALF_PERIOD_MIN, half))

    def _fpga_obs_reopen(self) -> None:
        """auto_reopen の周期の仕事（_lock の中で）: 止まっていたら同じ設定で開き直す。まだ答えなければ次の周期で。"""
        opened = self._fpga_obs_opened
        if not opened or not self._obs().status().get("stuck"):
            return
        try:
            self._open_obs(opened["config"])
        except Exception:                                          # まだ答えない（書き込みの途中等）
            return
        self._fpga_obs_opened["reopened"] = opened.get("reopened", 0) + 1     # _open_obs が覚え直した方に
        if self._host._recorder is not None:
            self._host._recorder.add("annotation", "mark", self._host.session.device.time(),
                               body={"text": "FPGA の観測を開き直した（観測の回路が答えなくなっていた）"})

    def _open_obs(self, config: dict) -> dict:
        """観測を開き、設定と申告を覚え、遅れの申告を取り込みに渡す（_lock の中で）。"""
        with self._host._lock:
            self._fpga_latest = {}                           # 別の回路・設定かもしれない: 前の最後の値は捨てる
            self._fpga_obs = FpgaObs(self._host.session.device)
            info = self._fpga_obs.open(config["chain"], config["user_ir"], config["half_period"], config["words"],
                                       config["sync_period"], id_scans=config.get("id_scans", 0))
            # 開いた設定と申告を覚える（別の窓・開き直した画面が見る。対象を書き直した後に同じ設定で開き直す）
            previous = self._fpga_obs_opened or {}
            self._fpga_obs_opened = {**previous, "config": config, "declared": info}
            hz = self._host.session.device.capabilities().timebase_hz
            # 申告の遅れの範囲（段階 S3。古い回路は FpgaObs.open が ±0.5 周期にしたもの、標本型は None）も渡す: 値の時刻の範囲
            self._obs_delays = {"sync_delay": info["sync_delay"], "sample_delay": info["sample_delay"],
                                **{k: info.get(k) for k in ("sync_delay_lo", "sync_delay_hi", "sample_delay_lo",
                                                            "sample_delay_hi")},
                                "nominal_ratio": hz / info["clock_hz"] if info["clock_hz"] else None,
                                "borrow_ratio": bool(config.get("borrow_ratio", False))}
            collector = self._host._collector
            if collector is not None:                              # 記録中: 取り込みの復号器も同じ遅れで（前の回路の傾き・印は捨てる）
                collector.obs.reset_clock(**self._obs_delays)
            return info

    def _obs(self) -> FpgaObs:
        """開いた観測（open で読んだ型・遅れの申告を持つ）。開いていなければ新しく（状態を見る・閉じるため）。"""
        obs = self._fpga_obs
        if obs is None or obs.dev is not self._host.session.device:
            obs = self._fpga_obs = FpgaObs(self._host.session.device)
        return obs

    def fpga_obs_close(self) -> dict:
        """観測をやめる（対象の TAP を Test-Logic-Reset に戻し、線を手放す。auto_reopen も止める）。"""
        with self._host._lock:
            self._host._remove_job("fpga_obs.reopen")
            if self._fpga_obs_opened:
                self._fpga_obs_opened["auto_reopen"] = False
            self._obs().close()
            self._fpga_latest = {}
            return {}

    def fpga_obs_status(self) -> dict:
        """観測の状態（running・id_ok・stuck = 観測の回路が答えないので止めた・cmd_pending・overflow_seen・
        cmd_dropped・scans・id・mode = stream（記録型）/ snapshot（標本型））と、最後に開いた設定 config（fpga_obs_open の
        引数。開いていなければ null）・申告 declared（fpga_obs_open の戻り）・auto_reopen・reopened（自動で開き直した回数）・
        tck_hz（開いた設定の実際の TCK = sys / (2 × half_period + extra)。開いていない・システムのクロックの申告が無ければ null）。"""
        with self._host._lock:
            opened = self._fpga_obs_opened or {"config": None, "declared": None, "auto_reopen": False,
                                                                  "reopened": 0, "tck_hz": None}
            return {**self._obs().status(), "mode": self._obs().mode, **opened}

    def fpga_obs_register_read(self, addr: int) -> dict:
        """観測の回路のレジスタ（16 ビット）を読む（0・1 ID の下位・上位、2 VERSION、3 GROUPS、4 FIFO_DEPTH、
        5・6 CLOCK_HZ の下位・上位、7 SYNC_DELAY、8 SAMPLE_DELAY、9 LOST_BITS、14 CONTROL、15 SELECT）。標本型には無い。"""
        with self._host._lock:
            return {"value": self._obs().register_read(addr)}

    def fpga_obs_register_write(self, addr: int, value: int) -> dict:
        """観測の回路のレジスタに書く（14 CONTROL の bit0 = 記録する、15 SELECT = 観測する 16 ビットの組。値は 0〜255）。"""
        with self._host._lock:
            self._obs().register_write(addr, value)
            return {}

    def fpga_obs_select(self, group: int) -> dict:
        """観測する 16 ビットの組を選ぶ（0〜groups − 1）。"""
        with self._host._lock:
            self._obs().select(group)
            return {}

    def fpga_obs_start(self) -> dict:
        """観測の回路が値の変化を記録し始める（始め直すと絶対の時刻と今の値から）。"""
        with self._host._lock:
            self._obs().start()
            return {}

    def fpga_obs_stop(self) -> dict:
        """観測の回路の記録を止める（読み出しは続く）。"""
        with self._host._lock:
            self._obs().stop()
            return {}

    def fpga_obs_signals_get(self) -> dict:
        """FPGA の観測の組に付ける信号の定義: signals = 画面の状態（記録していないときの既定）、recording = 開いている
        記録の定義（fpga_obs_signals_set と同じ検査を通したもの。無ければ null）、recording_error = 記録の情報に置かれた
        定義が壊れていて読めなかった理由（手で書き換えた等。そのとき recording は null で、fpga_obs_read も signals_error を返す。
        壊れていなければ null）。"""
        meta = getattr(self._host._view, "meta", None) or {}
        raw, recording, error = meta.get("fpga.signals"), None, None
        if raw is not None:
            try:
                recording = FpgaSignals.from_dict(raw).to_dict()
            except (ValueError, KeyError, TypeError) as e:
                error = str(e)
        return {"signals": self._host.ui_state_get()["fpga_signals"], "recording": recording, "recording_error": error}

    def _signal_defs(self) -> tuple[FpgaSignals | None, str | None]:
        """観測の値に付ける信号の定義（開いている記録のもの、無ければ画面の状態のもの）と、読めなかった理由。記録の定義が
        壊れていれば画面の定義に替えない（記録の値を別の定義で読まない）: (None, 理由)。"""
        defs = self.fpga_obs_signals_get()
        if defs["recording_error"] is not None:
            return None, defs["recording_error"]
        try:
            return FpgaSignals.from_dict(defs["recording"] or defs["signals"]), None
        except (ValueError, KeyError, TypeError) as e:
            return None, str(e)

    def fpga_obs_signals_set(self, signals: dict) -> dict:
        """信号の定義（{"groups": {"<組>": [{"name", "lsb", "width", "format": hex/dec/bin/enum, "enum": {"<値>": "<名前>"}}]}}）
        を置く。画面の状態（別の窓・AI と共有）と、記録中・開いている記録の情報に（後から変えても過去の記録の段の名前が
        変わる。記録の生の値は変わらない）。名前の重複・ビットの重なり・16 ビットの外は断る。"""
        d = FpgaSignals.from_dict(signals).to_dict()
        self._host.ui_state_set(fpga_signals=d)
        wrote = False
        with self._host._lock:
            if self._host._recorder is not None:
                self._host._recorder.update_meta(**{"fpga.signals": d})
                wrote = True
            view = self._host._view
            if view is not None and hasattr(view, "update_meta"):
                view.update_meta(**{"fpga.signals": d})
                wrote = True
        return {"signals": d, "recording": wrote}

    def fpga_obs_read(self, last: int = 2000, since: int | None = None) -> dict:
        """イベントの記録から観測した値を fprobe の時刻で返す（窓は events_read と同じ。続けて読むなら since）。
        items: time_us（fprobe のタイムベース。印が無く決まらなければ null）・target_cycles（鎖の中の対象の clk の周期。
        溢れ・読み始めで 0 から）・value（null は溢れの切れ目、lost = 失った変化の数）。窓の中の最初の鎖の始め・印より
        前の値は時刻が決まらないので出ない。"""
        hz = self._host._caps().timebase_hz
        signals, error = self._signal_defs()
        extra_out: dict = {} if error is None else {"signals_error": error}   # 壊れた定義でも生の値は返す（定義の誤りを添える）

        def item(s) -> dict:
            fields = signals is not None and s.kind == "value"
            lo, hi = getattr(s, "time_lo", None), getattr(s, "time_hi", None)
            return {"time_us": None if s.time_fprobe is None else s.time_fprobe * 1e6 / hz,
                    "time_lo_us": None if lo is None else lo * 1e6 / hz,      # 範囲（段階 S3。time_us はその中点）
                    "time_hi_us": None if hi is None else hi * 1e6 / hz,
                    "copy": getattr(s, "copy", False),
                    "time_basis": getattr(s, "time_basis", None),
                    "target_cycles": s.target_cycles, "kind": s.kind, "group": s.group, "value": s.value,
                    "lost": s.lost, **({"fields": signals.fields(s.group, s.value)} if fields else {})}

        la_record, collector = self._host._la_record, self._host._collector      # 別のスレッドが替えるので一度だけ読む
        if la_record is not None and la_record.running and collector is not None:
            # ロジアナを記録へ流している間はリングを流れが読む: 記録へ取り込んだ値（共有の復号器が覚えている分）から。
            # 機器に触らないので機器のロックを取らない（取ると流れの受け取りと奪い合い、画面の毎秒の読みで 1 秒待ち、
            # 受け取りも遅れて値が数分古くなった。2026-10-21 実機）
            if since is not None:
                raise RuntimeError("ロジアナを記録へ流している間は since で続けて読めない（last だけ。続きは "
                                   "recording_events の stream fpga）")
            recent = list(collector.obs.recent)[-last:] if last > 0 else []
            # 復号器の時刻はイベントの 48 ビットの時刻から伸ばした刻み: 取り込みが最後に読んだタイムベースで 64 ビットにする
            # （他の道の time_us・system.status の age_us と同じ時刻の領域に。前は 2^48 刻みを越えるとずれた。レビュー 2026-11-03）
            reference = collector.obs.reference
            if reference is not None:
                recent = [unwrap_obs_sample(s, reference) for s in recent]
            # untimed_by_limit: 印を待って持つ上限（collector.OBS_PENDING_MAX）に達して時刻を決めずに出した値の数（事実）
            return {"source": "recording", "items": [item(s) for s in recent],
                    "untimed_by_limit": collector.obs.untimed_by_limit, **extra_out}
        with self._host._lock:
            written, lost, first, records, events, extra = self._host._read_events(last, since)
            events = unwrap_events(events, self._host.session.device.time())     # 48 ビットの時刻をタイムベース（64 ビット）に
            items = [item(s) for s in obs_samples(events, **self._obs_delays)]
            return {"written": written, "lost": lost, "first": first, "next": first + records, **extra,
                    "items": items, **extra_out}

    def fpga_obs_count(self, conditions: list[str], last: int) -> dict:
        """観測した値の最後の last 件（fpga_obs_read の窓。使う側が明示する。既定の値は無い）で、条件ごとに成り立った
        回数と、数えた範囲の事実（数えた値の数・最初と最後の値の時刻 t0_us / t1_us・その間の秒 span_s・切れ目の数 gaps・
        失った変化の数 lost）を返す（fprobe.obs_condition）。読めない条件は error に理由を返す（ほかの条件は数える）。"""
        items = self.fpga_obs_read(last=last)["items"]
        signals, _ = self._signal_defs()   # 壊れた定義: 列挙の名前を数に直せない（fpga_obs_read が signals_error を返す）
        out = []
        for text in conditions:
            try:
                out.append(count_condition(items, text, signals))
            except ConditionError as e:
                out.append({"condition": text, "error": str(e)})
        return {"last": last, "conditions": out}
