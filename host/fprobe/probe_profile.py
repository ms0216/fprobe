"""Probe の道具: 性能解析（F5）。PC サンプリングのプロファイル・割り込みの時間・近似のカバレッジ・記録の 2 点間の時間。

Probe（fprobe.mcp_server）に混ぜて使う mixin。Probe の _lock・target・session・_symbols・_debug・swo_start・
_reading と、ObserveTools（SWO の受信 `_swo_receive` とパケットの受け取り `_swo_listen`）、InspectTools
（ELF の逆アセンブラ）、WindowTools（DAP サンプラの持ち主・出来事の窓・write_registers）を使う。
機器に触るのは必ず _lock の中。

- 方法 itm: DWT の PC サンプリング（と例外トレース・ローカルのタイムスタンプ）を設定し、SWO の受信を ObserveTools と
  共有して（同じ ItmDecoder のパケットを受け取る。二重に読まない）パケットをためる。止めたら DWT_CTRL を元に戻す
  （ITM・TPIU の設定は残す: ファームの printf 等も SWO を使うため）。
- 方法 pcsr: DAP サンプラで DWT_PCSR を周期的に読む（SWO も ITM も要らない。Cortex-M0+ でも使える）。値は窓
  （記録中なら記録の stream "sample"、そうでなければイベント記録）から読む。
- 計算はすべて fprobe.profile（Profiler・exception_profile・coverage・measure）。結果は as_dict() の形のまま返す。

仕様: docs/superpowers/specs/2026-10-17-f5-profile-design.md（8 節が組み込みの提案）
"""

from dataclasses import dataclass, field

from .api import NoData
from .itm import Packet, Timestamper
from .probe_window import EventWindow, write_registers
from .profile import (ExceptionProfile, PcSamples, Profile, Profiler, SamplingPlan, coverage, exception_profile,
                      measure, plan_itm_sampling, plan_pcsr_sampling)
from .profile.aliasing import aliasing_notes
from .profile.dwt import _EXCTRCENA, _PCSAMPLENA, DWT_CTRL, DWT_PCSR
from .recording import Event
from .sampler import Sampler

__all__ = ["PACKET_LIMIT", "ProfileTools"]

PACKET_LIMIT = 1 << 22                   # 方法 itm でためるパケットの上限（超えた分は数えて捨てる）
TIMING_LIMIT = 1000                      # timing_measure が返す生の組・外れ値の上限
_VECTOR_LIMIT = 16 + 240                 # ベクタ表の最大（ARMv7-M: 例外 16 + 割り込み 240）


@dataclass
class _ProfileRun:
    method: str                          # itm / pcsr
    plan: SamplingPlan
    core_clock_hz: float | None = None
    exception_trace: bool = False
    timestamps: bool = False
    restore: dict[int, int] = field(default_factory=dict)        # 止めるときに戻す（番地 → 元の値）
    packets: list[Packet] = field(default_factory=list)          # itm: 受け取ったパケット
    dropped: int = 0                                             # itm: PACKET_LIMIT を超えて捨てた数
    window: EventWindow | None = None                            # pcsr: DWT_PCSR の値を読む窓
    drained: int = 0                                             # itm: 始める前に捨てた前の受信の残り（バイト）
    jitter: int = 0                                              # pcsr: サンプラの周期のゆらぎ（2^jitter 周期の幅）
    built: "tuple[tuple, _ProfileResult] | None" = None          # 最後に組んだ結果と、そのときのサンプルの印

    def take(self, packets: list[Packet]) -> None:
        room = PACKET_LIMIT - len(self.packets)
        self.packets += packets[:max(room, 0)]
        self.dropped += max(len(packets) - max(room, 0), 0)


@dataclass
class _ProfileResult:
    run: _ProfileRun
    samples: PcSamples
    profile: Profile
    exceptions: ExceptionProfile | None
    reads: tuple[int, int, int] | None = None                     # pcsr: DWT_PCSR の読みの (数, 最初の時刻, 最後の時刻)


class ProfileTools:
    """性能解析（Probe の mixin）。"""

    def _init_profile(self) -> None:
        self._profile_run: _ProfileRun | None = None
        self._profile_last: _ProfileResult | None = None

    # --- 取る ----------------------------------------------------------------------------------

    def profile_start(self, method: str = "itm", rate_hz: float = 10_000.0, core_clock_hz: float | None = None,
                      swo_baud: int = 2_000_000, exception_trace: bool = True, exception_rate_hz: float = 1000.0,
                      timestamps: bool | None = None, ap: int = 0) -> dict:
        """PC サンプリングを始める。method: itm（DWT の PC サンプリング → SWO。core_clock_hz が要る。例外トレース・
        ローカルのタイムスタンプで割り込みの時間も。TPIU も設定するが、SWO のピンの設定はチップ固有なのでファームか
        target_peripheral_write で）/ pcsr（DAP サンプラで DWT_PCSR を読む。SWO 不要・Cortex-M0+ も可・遅い）。
        timestamps は省略で exception_trace と同じ（割り込みの時間に要る）。計画（実際のサンプル率・SWO の使用率・警告）を返す。"""
        if method not in ("itm", "pcsr"):
            raise ValueError("method は itm か pcsr")
        if method == "itm" and core_clock_hz is None:
            raise ValueError("method itm には core_clock_hz（コアのクロック。DWT の間隔と SWO の分周に使う）が要る")
        with self._lock:
            if self._profile_run is not None:
                raise RuntimeError("プロファイルを取っている（profile_stop を先に）")
            swo = getattr(self, "_swo_run", None)
            if method == "itm" and swo is not None and swo.running:
                raise RuntimeError("swo_configure が DWT・ITM を設定している（swo_unconfigure を先に）")
            if self._symbols is None:
                raise RuntimeError("ELF が読み込まれていない（debug_elf_load）: PC を関数・行に割り当てられない")
            if method == "itm":
                run = self._start_itm_sampling(rate_hz, core_clock_hz, swo_baud, exception_trace, exception_rate_hz,
                                               exception_trace if timestamps is None else timestamps)
            else:
                run = self._start_pcsr_sampling(rate_hz, ap)
            self._profile_run, self._profile_last = run, None
            return {"running": True, "method": method, "plan": run.plan.as_dict()}

    def _start_itm_sampling(self, rate_hz: float, core_clock_hz: float, swo_baud: int, exception_trace: bool,
                            exception_rate_hz: float, timestamps: bool) -> _ProfileRun:
        plan = plan_itm_sampling(core_clock_hz, rate_hz, swo_baud, exception_trace=exception_trace,
                                 exception_rate_hz=exception_rate_hz, timestamps=timestamps)
        run = _ProfileRun("itm", plan, core_clock_hz, exception_trace, timestamps)
        # 順序（2026-10-18 に実機で）: DWT の出力（PC サンプル・例外トレース）を止める → 受信を始めて前の受信の残りを
        # 捨てる → 設定 → DWT を最後に有効にする。逆だと受信がパケットの途中や古いバイトの継ぎ目から始まり、同期の
        # パケット（SYNCTAP。16 MHz で十数秒に 1 回）まで解読がずれたままになる
        original = self.target.read_memory(DWT_CTRL)
        self.target.write_memory(DWT_CTRL, original & ~(_PCSAMPLENA | _EXCTRCENA))
        self.swo_start(round(plan.swo_baud))
        run.drained = self._swo_drain()
        write_registers(self.target, [w for w in plan.writes if w.address != DWT_CTRL])
        self._swo_listen(run.take)
        write_registers(self.target, [w for w in plan.writes if w.address == DWT_CTRL])
        run.restore = {DWT_CTRL: original}
        return run

    def _start_pcsr_sampling(self, rate_hz: float, ap: int) -> _ProfileRun:
        sampler = Sampler(self.session.device)
        link = self._link or {}
        swd_hz = link.get("frequency_hz") if link.get("wire") == "swd" else None
        plan = plan_pcsr_sampling(rate_hz, sampler.sys_clock_hz, swd_hz)
        run = _ProfileRun("pcsr", plan)
        self._claim_sampler("profile")
        try:
            write_registers(self.target, plan.writes)
            sampler.configure(plan.sampler_ranges, plan.period_s, ap)
            run.window = self._open_window()
            # 周期にゆらぎを入れる（周期の半分まで）: 割り込み等の周期と噛み合う偏り（エイリアシング）を防ぐ
            # （実機で SysTick の割合が 0% か 0.8% に偏った。真は 0.1%）
            period_cycles = plan.period_s * sampler.sys_clock_hz
            run.jitter = max(0, min(15, int(period_cycles).bit_length() - 2))   # 2^k ≦ 周期の半分。古い FPGA は無視する
            sampler.start(jitter=run.jitter)
        except Exception:
            self._release_sampler("profile")
            raise
        return run


    def profile_stop(self, top: int = 10) -> dict:
        """PC サンプリングを止めて、ためたサンプルをプロファイルにする（DWT_CTRL を元に戻す）。要約（分類・上位 top の
        関数・割り込みごとの回数）を返す。詳しくは profile_result / profile_coverage / profile_exceptions。"""
        with self._lock:
            run = self._profile_run
            if run is None:
                raise RuntimeError("プロファイルを取っていない（profile_start）")
            try:
                if run.method == "itm":
                    if self._debug is not None:
                        self._swo_receive(0)                      # 届いている分を受け取ってから
                        for address, value in run.restore.items():
                            self.target.write_memory(address, value)
                else:
                    Sampler(self.session.device).stop()
                    self._close_window(run.window)
            finally:
                self._swo_unlisten(run.take)
                self._release_sampler("profile")
                self._profile_run = None
        result = self._build_profile(run)
        self._profile_last = result
        return self._profile_summary(result, "function", top, None)

    # --- 読む ----------------------------------------------------------------------------------

    def _current_profile(self) -> _ProfileResult:
        """取っている最中ならいままでの分（サンプルが増えていなければ前に組んだもの）、止めた後なら最後の結果。"""
        run = self._profile_run
        if run is not None:
            if run.method == "itm" and self._debug is not None:
                self._swo_receive(0)
            events = None if run.method == "itm" else self._window_events(run.window, "sample")
            # サンプルが増えたかの印。同じなら前に組んだものを使う（画面は 1 回の読みで result・coverage 等を続けて呼ぶ）
            mark = (len(run.packets), run.dropped) if events is None else (len(events), events[-1][0] if events else 0)
            if run.built is None or run.built[0] != mark:
                run.built = (mark, self._build_profile(run, events))
            return run.built[1]
        if self._profile_last is None:
            raise NoData("プロファイルが無い（profile_start）")
        return self._profile_last

    def profile_result(self, by: str = "function", top: int = 20, function: str | None = None) -> dict:
        """プロファイルの上位 top（取っている最中ならいままでの分）。by: function / line / instruction / exception。
        line・instruction は function で絞れる。割合には信頼区間（ci）が付く（サンプル数による誤差だけ）。"""
        return self._profile_summary(self._current_profile(), by, top, function)

    def profile_coverage(self, functions: list[str] | None = None, all_functions: bool = False) -> dict:
        """PC サンプルが当たった命令・行からの、関数ごとの実行された行の割合（近似: 当たらなかった行が実行されなかった
        とは限らない）。functions を省くと当たった関数（all_functions なら ELF の全関数）。"""
        result = self._current_profile()
        with self._lock:
            disassembler, _ = self._disassembler(from_elf=True)
        return coverage(result.profile, disassembler, functions=functions, all_functions=all_functions).as_dict()

    def profile_exceptions(self) -> dict:
        """割り込みごとの回数・時間（含む／自分）・最大の回の時刻（ITM のローカルの時刻）。method itm と例外トレースが要る。"""
        result = self._current_profile()
        if result.exceptions is None:
            raise RuntimeError("例外トレースが無い（profile_start の method itm・exception_trace）")
        out = result.exceptions.as_dict()
        if not result.run.timestamps:
            out["notes"] = ["ローカルのタイムスタンプが無い（timestamps=false）ので時間は測れない（回数だけ）"]
        return out

    def timing_measure(self, start: dict, end: dict, bins: int = 20, outliers: str = "iqr",
                       limit: int = TIMING_LIMIT) -> dict:
        """記録（記録中のもの、または開いたもの）の 2 点間の時間の統計（最小・最大・平均・σ・分位・分布・外れ値）。
        start / end は recording_find と同じ条件。外れ値の t は記録・タイムラインのその時刻へ跳ぶ鍵。outliers: iqr / zscore。"""
        out = measure(self._reading(), start, end, bins=bins, outliers=outliers).as_dict()
        for key in ("intervals", "outliers"):
            if key in out and len(out[key]) > limit:
                out[f"{key}_total"] = len(out[key])
                out[key] = out[key][:limit]
        return out

    # --- 計算 ----------------------------------------------------------------------------------

    def _build_profile(self, run: _ProfileRun, events: list[tuple[int, int, int]] | None = None) -> _ProfileResult:
        """events: pcsr の窓の出来事（読んであれば。省くと窓から読む）。"""
        names = self._vector_names()
        exceptions = None
        reads = None
        if run.method == "itm":
            stamper = Timestamper()
            timed = stamper.feed(run.packets) + stamper.flush()
            samples = PcSamples.from_itm(timed)
            if run.exception_trace:
                exceptions = exception_profile(timed, clock_hz=run.core_clock_hz, names=names)
        else:
            if events is None:
                events = self._window_events(run.window, "sample")
            pcsr = [(t, a, v) for t, a, v in events if a == DWT_PCSR]
            samples = PcSamples.from_pcsr(Event(t, "sample", "word", a, v) for t, a, v in pcsr)
            reads = (len(pcsr), pcsr[0][0], pcsr[-1][0]) if pcsr else (0, 0, 0)
        with self._lock:
            disassembler, _ = self._disassembler(from_elf=True)
        profile = Profiler(self._symbols, disassembler).profile(samples, names=names)
        return _ProfileResult(run, samples, profile, exceptions, reads)

    def _vector_names(self) -> dict[int, str]:
        """ELF のベクタ表から、割り込み（例外 16 以上）の処理の関数名。ベクタ表は最初の区画の先頭にある変数
        （大きさで表の長さが分かるもの）。無ければ名前を付けない（コードを表と取り違えない）。"""
        syms = self._symbols
        if syms is None or not syms.segments:
            return {}
        segment = min(syms.segments, key=lambda s: s.address)
        hit = syms.symbol_at(segment.address)
        if hit is None or hit[1] != 0 or hit[0].kind != "object" or not hit[0].size:
            return {}
        count = min(hit[0].size, len(segment.data)) // 4
        names = {}
        for n in range(16, min(count, _VECTOR_LIMIT)):
            handler = int.from_bytes(segment.data[4 * n:4 * n + 4], "little")
            if not handler & 1:                                   # Thumb の関数の番地でない（空き・予約）
                continue
            where = syms.describe(handler & ~1)
            if where.function is not None and where.offset == 0:
                names[n] = f"IRQ{n - 16} ({where.function})"
        return names

    def _profile_summary(self, result: _ProfileResult, by: str, top: int, function: str | None) -> dict:
        run, prof = result.run, result.profile
        plan = run.plan
        out = {"running": run is self._profile_run, "method": run.method, "rate_hz": plan.rate_hz,
               "total": prof.total, "active": prof.active, "unavailable": prof.unavailable,
               "overflows": prof.overflows, "confidence": prof.confidence, "categories": prof.category_table(),
               "by": by, "rows": prof.top(top, by, function), "notes": prof.notes() + list(plan.warnings)}
        if run.method == "itm":
            out.update(swo_utilization=plan.utilization, dropped_packets=run.dropped)
            if result.exceptions is not None:
                traced = result.exceptions.exceptions()
                out["exceptions"] = [{k: e[k] for k in ("exception", "name", "count", "total_self", "load")}
                                     for e in traced[:top]]
                # 例外トレースの本当の負荷とサンプルの割合が食い違えば、エイリアシングを知らせる
                out["notes"] += aliasing_notes(prof.top(1000, "exception"), traced, prof.total)
        else:
            with self._lock:
                hz = self.session.device.capabilities().timebase_hz
            out["window"] = self._window_info(run.window, hz)
            out["sampling"] = sampling = self._pcsr_speed(result, hz)
            achieved = sampling["achieved_hz"]
            if achieved is not None and achieved < 0.9 * sampling["requested_hz"]:
                swd = plan.swd or {}
                need = swd.get("swd_hz_for_rate")
                fix = (f"SWD を {need / 1e6:.2f} MHz 以上にする（target_connect の frequency_hz）か rate_hz を下げる"
                       if need else "rate_hz を下げる")
                out["notes"].append(f"サンプラが追いつかない: 実際 約 {achieved:.0f} Hz（求めた {sampling['requested_hz']:g} Hz）。{fix}")
        return out

    @staticmethod
    def _pcsr_speed(result: _ProfileResult, hz: float) -> dict:
        """方法 pcsr の速さ（B9）: 求めた・見込み（SWD の速さからの見積もり）・実際（サンプルの時刻の間隔）。"""
        plan = result.run.plan
        swd = plan.swd or {}
        count, first, last = result.reads or (0, 0, 0)            # 止まっていた・眠りの読み（PCSR が無効）も 1 回
        achieved = (count - 1) / ((last - first) / hz) if count >= 2 and last > first else None
        return {"requested_hz": plan.requested_rate_hz, "expected_hz": plan.rate_hz, "achieved_hz": achieved,
                "samples": count, "swd_hz": swd.get("swd_hz"), "swclk_hz": swd.get("swclk_hz"),
                "round_us": swd.get("round_us"), "swd_hz_for_rate": swd.get("swd_hz_for_rate")}
