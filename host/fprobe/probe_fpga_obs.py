"""対象の FPGA の観測（fpga_obs_*）の mixin。FEATURES の FPGA_OBS がある構成だけ。値はイベント 8・9 で記録に入る。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う。Probe の _lock・session・_caps・_recorder・_collector・_view・_la_record・_read_events・
_add_job / _remove_job・ui_state_get / ui_state_set を使う。観測の回路は fprobe.fpga_obs.FpgaObs。
"""

from .collector import unwrap_events
from .correlate import ConditionError
from .fpga_obs import FpgaObs, samples as obs_samples
from .fpga_signals import FpgaSignals
from .obs_condition import count_condition

REOPEN_PERIOD_S = 1.0                  # fpga_obs_open の auto_reopen: 止まっていないか見る周期


class FpgaObsTools:
    """Probe に混ぜる対象の FPGA の観測の道具。"""

    def _init_fpga_obs(self) -> None:
        self._fpga_obs: FpgaObs | None = None              # 開いた観測（_obs が機器に合わせて作る）
        self._fpga_obs_opened: dict | None = None          # 開いた設定と申告（config・declared・auto_reopen・reopened）
        self._fpga_latest: dict = {}                       # グループごとの最後に分かった値（system_status が見せる）
        self._obs_delays: dict = {}                        # 申告の遅れ（取り込みの復号器・fpga_obs_read に渡す）

    def fpga_obs_open(self, chain: dict | None = None, user_ir: int = 2, half_period: int = 10, words: int = 64,
                      sync_period: int = 100, auto_reopen: bool = False, id_scans: int = 0) -> dict:
        """対象の FPGA の JTAG で観測の回路 v2（target_ip/fprobe_obs）を読み始め、その申告（id・version・width・groups・
        fifo_depth・clock_hz・sync_delay・sample_delay・lost_bits）を返す。chain = 鎖での対象の位置 {ir_len（既定 4）,
        ir_before, ir_after, dr_before, dr_after}、user_ir = 対象の USER の命令、half_period = TCK の半周期（sys の周期数。
        obs_soft_core の 1 クロック版は TCK ≤ 対象の clk / 8）、words = 1 スキャンの記録の語数、sync_period = 印（時刻
        合わせ）の間隔（スキャンの数）、id_scans = 始めに ID の応答を待つスキャンの上限（0 = 上限なし。上限まで来なければ
        止まり、status の stuck・id_timeout）。値を記録に残すには events_start も要る。auto_reopen = 観測の回路が答えなくなって
        止まったら（stuck。対象の FPGA を書き直した・電源を入れ直した等）、1 秒ごとに同じ設定で開き直す（記録中なら印を
        残す。回数は fpga_obs_status の reopened）。止まった回路の線をまた動かすので、利用者が選んだときだけ。"""
        config = {"chain": chain, "user_ir": user_ir, "half_period": half_period, "words": words,
                  "sync_period": sync_period, "id_scans": id_scans}
        with self._lock:
            info = self._open_obs(config)
            self._fpga_obs_opened.update(auto_reopen=auto_reopen, reopened=0)
            if auto_reopen:
                self._add_job("fpga_obs.reopen", REOPEN_PERIOD_S, self._fpga_obs_reopen)
            else:
                self._remove_job("fpga_obs.reopen")
            return info

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
        if self._recorder is not None:
            self._recorder.add("annotation", "mark", self.session.device.time(),
                               body={"text": "FPGA の観測を開き直した（観測の回路が答えなくなっていた）"})

    def _open_obs(self, config: dict) -> dict:
        """観測を開き、設定と申告を覚え、遅れの申告を取り込みに渡す（_lock の中で）。"""
        with self._lock:
            self._fpga_latest = {}                           # 別の回路・設定かもしれない: 前の最後の値は捨てる
            self._fpga_obs = FpgaObs(self.session.device)
            info = self._fpga_obs.open(config["chain"], config["user_ir"], config["half_period"], config["words"],
                                       config["sync_period"], id_scans=config.get("id_scans", 0))
            # 開いた設定と申告を覚える（別の窓・開き直した画面が見る。対象を書き直した後に同じ設定で開き直す）
            previous = self._fpga_obs_opened or {}
            self._fpga_obs_opened = {**previous, "config": config, "declared": info}
            hz = self.session.device.capabilities().timebase_hz
            self._obs_delays = {"sync_delay": info["sync_delay"], "sample_delay": info["sample_delay"],
                                "nominal_ratio": hz / info["clock_hz"] if info["clock_hz"] else None}
            collector = self._collector
            if collector is not None:                              # 記録中: 取り込みの復号器も同じ遅れで（前の回路の傾き・印は捨てる）
                collector.obs.reset_clock(**self._obs_delays)
            return info

    def _obs(self) -> FpgaObs:
        """開いた観測（open で読んだ型・遅れの申告を持つ）。開いていなければ新しく（状態を見る・閉じるため）。"""
        obs = self._fpga_obs
        if obs is None or obs.dev is not self.session.device:
            obs = self._fpga_obs = FpgaObs(self.session.device)
        return obs

    def fpga_obs_close(self) -> dict:
        """観測をやめる（対象の TAP を Test-Logic-Reset に戻し、線を手放す。auto_reopen も止める）。"""
        with self._lock:
            self._remove_job("fpga_obs.reopen")
            if self._fpga_obs_opened:
                self._fpga_obs_opened["auto_reopen"] = False
            self._obs().close()
            self._fpga_latest = {}
            return {}

    def fpga_obs_status(self) -> dict:
        """観測の状態（running・id_ok・stuck = 観測の回路が答えないので止めた・cmd_pending・overflow_seen・
        cmd_dropped・scans・id・mode = stream（記録型）/ snapshot（標本型））と、最後に開いた設定 config（fpga_obs_open の
        引数。開いていなければ null）・申告 declared（fpga_obs_open の戻り）・auto_reopen・reopened（自動で開き直した回数）。"""
        with self._lock:
            opened = self._fpga_obs_opened or {"config": None, "declared": None, "auto_reopen": False,
                                                                  "reopened": 0}
            return {**self._obs().status(), "mode": self._obs().mode, **opened}

    def fpga_obs_register_read(self, addr: int) -> dict:
        """観測の回路のレジスタ（16 ビット）を読む（0・1 ID の下位・上位、2 VERSION、3 GROUPS、4 FIFO_DEPTH、
        5・6 CLOCK_HZ の下位・上位、7 SYNC_DELAY、8 SAMPLE_DELAY、9 LOST_BITS、14 CONTROL、15 SELECT）。標本型には無い。"""
        with self._lock:
            return {"value": self._obs().register_read(addr)}

    def fpga_obs_register_write(self, addr: int, value: int) -> dict:
        """観測の回路のレジスタに書く（14 CONTROL の bit0 = 記録する、15 SELECT = 観測する 16 ビットの組。値は 0〜255）。"""
        with self._lock:
            self._obs().register_write(addr, value)
            return {}

    def fpga_obs_select(self, group: int) -> dict:
        """観測する 16 ビットの組を選ぶ（0〜groups − 1）。"""
        with self._lock:
            self._obs().select(group)
            return {}

    def fpga_obs_start(self) -> dict:
        """観測の回路が値の変化を記録し始める（始め直すと絶対の時刻と今の値から）。"""
        with self._lock:
            self._obs().start()
            return {}

    def fpga_obs_stop(self) -> dict:
        """観測の回路の記録を止める（読み出しは続く）。"""
        with self._lock:
            self._obs().stop()
            return {}

    def fpga_obs_signals_get(self) -> dict:
        """FPGA の観測の組に付ける信号の定義: signals = 画面の状態（記録していないときの既定）、recording = 開いている
        記録の定義（無ければ null）。"""
        meta = getattr(self._view, "meta", None) or {}
        return {"signals": self.ui_state_get()["fpga_signals"], "recording": meta.get("fpga.signals")}

    def fpga_obs_signals_set(self, signals: dict) -> dict:
        """信号の定義（{"groups": {"<組>": [{"name", "lsb", "width", "format": hex/dec/bin/enum, "enum": {"<値>": "<名前>"}}]}}）
        を置く。画面の状態（別の窓・AI と共有）と、記録中・開いている記録の情報に（後から変えても過去の記録の段の名前が
        変わる。記録の生の値は変わらない）。名前の重複・ビットの重なり・16 ビットの外は断る。"""
        d = FpgaSignals.from_dict(signals).to_dict()
        self.ui_state_set(fpga_signals=d)
        wrote = False
        with self._lock:
            if self._recorder is not None:
                self._recorder.update_meta(**{"fpga.signals": d})
                wrote = True
            view = self._view
            if view is not None and hasattr(view, "update_meta"):
                view.update_meta(**{"fpga.signals": d})
                wrote = True
        return {"signals": d, "recording": wrote}

    def fpga_obs_read(self, last: int = 2000, since: int | None = None) -> dict:
        """イベントの記録から観測した値を fprobe の時刻で返す（窓は events_read と同じ。続けて読むなら since）。
        items: time_us（fprobe のタイムベース。印が無く決まらなければ null）・target_cycles（鎖の中の対象の clk の周期。
        溢れ・読み始めで 0 から）・value（null は溢れの切れ目、lost = 失った変化の数）。窓の中の最初の鎖の始め・印より
        前の値は時刻が決まらないので出ない。"""
        hz = self._caps().timebase_hz
        defs = self.fpga_obs_signals_get()
        extra_out: dict = {}
        try:
            signals = FpgaSignals.from_dict(defs["recording"] or defs["signals"])
        except (ValueError, KeyError, TypeError) as e:          # 壊れた定義でも生の値は返す（定義の誤りを添える）
            signals = None
            extra_out["signals_error"] = str(e)

        def item(s) -> dict:
            fields = signals is not None and s.kind == "value"
            return {"time_us": None if s.time_fprobe is None else s.time_fprobe * 1e6 / hz,
                    "time_basis": getattr(s, "time_basis", None),
                    "target_cycles": s.target_cycles, "kind": s.kind, "group": s.group, "value": s.value,
                    "lost": s.lost, **({"fields": signals.fields(s.group, s.value)} if fields else {})}

        la_record, collector = self._la_record, self._collector      # 別のスレッドが替えるので一度だけ読む
        if la_record is not None and la_record.running and collector is not None:
            # ロジアナを記録へ流している間はリングを流れが読む: 記録へ取り込んだ値（共有の復号器が覚えている分）から。
            # 機器に触らないので機器のロックを取らない（取ると流れの受け取りと奪い合い、画面の毎秒の読みで 1 秒待ち、
            # 受け取りも遅れて値が数分古くなった。2026-10-21 実機）
            if since is not None:
                raise RuntimeError("ロジアナを記録へ流している間は since で続けて読めない（last だけ。続きは "
                                   "recording_events の stream fpga）")
            recent = list(collector.obs.recent)[-last:] if last > 0 else []
            return {"source": "recording", "items": [item(s) for s in recent], **extra_out}
        with self._lock:
            written, lost, first, events, extra = self._read_events(last, since)
            events = unwrap_events(events, self.session.device.time())     # 48 ビットの時刻をタイムベース（64 ビット）に
            items = [item(s) for s in obs_samples(events, **self._obs_delays)]
            return {"written": written, "lost": lost, "first": first, "next": first + len(events), **extra,
                    "items": items, **extra_out}

    def fpga_obs_count(self, conditions: list[str], last: int) -> dict:
        """観測した値の最後の last 件（fpga_obs_read の窓。使う側が明示する。既定の値は無い）で、条件ごとに成り立った
        回数と、数えた範囲の事実（数えた値の数・最初と最後の値の時刻 t0_us / t1_us・その間の秒 span_s・切れ目の数 gaps・
        失った変化の数 lost）を返す（fprobe.obs_condition）。読めない条件は error に理由を返す（ほかの条件は数える）。"""
        items = self.fpga_obs_read(last=last)["items"]
        defs = self.fpga_obs_signals_get()
        try:
            signals = FpgaSignals.from_dict(defs["recording"] or defs["signals"])
        except (ValueError, KeyError, TypeError):
            signals = None          # 壊れた定義: 列挙の名前を数に直せない（fpga_obs_read が signals_error を返す）
        out = []
        for text in conditions:
            try:
                out.append(count_condition(items, text, signals))
            except ConditionError as e:
                out.append({"condition": text, "error": str(e)})
        return {"last": last, "conditions": out}
