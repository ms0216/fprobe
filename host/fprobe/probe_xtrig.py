"""クロストリガ（xtrig_*）の mixin: 源 × 動作の経路・割り込み書き込み・値の一致・多段トリガ（シーケンサ）。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う。Probe の _lock・session・_caps を使う。経路のビットを変える _route_bit と
_need_inject は MatchTriggerTools（FPGA の条件・サンプラの一致のトリガ）も使う。部品は fprobe.xtrig。
"""

from . import generated as g
from .xtrig import CrossTrigger, Event, Pattern, Source, Stage, TriggerSequencer


class XtrigTools:
    """Probe に混ぜるクロストリガの道具。"""

    def xtrig_route(self, to_la: list[str] | None = None, to_inject: list[str] | None = None) -> dict:
        """源（"la_trigger" / "dap_match" / "host" / "sequence" = 多段トリガ / "obs_match" = FPGA の観測の値の一致（fpga_obs_trigger_set） / "sampler_match" = サンプラの値の一致（sampler_trigger_set））を動作につなぐ。to_la: ロジアナをトリガする源、
        to_inject: 割り込み書き込み（既定はターゲット停止）をする源。指定しない動作は切る。"""
        def flags(names):
            total = Source(0)
            for n in names or []:
                try:
                    total |= Source[str(n).upper()]
                except KeyError:
                    raise ValueError(f"源 {n!r} は無い（la_trigger / dap_match / host / sequence / obs_match / "
                                     "sampler_match）") from None
            return total
        with self._lock:
            caps = self.session.device.capabilities()
            for name in {*(to_la or []), *(to_inject or [])} & {"obs_match", "sampler_match"}:
                if not getattr(caps, name, False):
                    raise RuntimeError(f"この構成には源 {name} が無い（ケイパビリティ {name}）")
            if to_inject and not getattr(self.session.device.capabilities(), "inject", True):
                raise RuntimeError("この構成に故障注入（割り込み書き込み）が無い: probe_design_switch(protocol=\"swd\") で"
                                   "標準の構成に")
            CrossTrigger(self.session.device).route(to_la=flags(to_la), to_inject=flags(to_inject))
            return {"to_la": to_la or [], "to_inject": to_inject or []}

    def xtrig_set_inject(self, address: int = 0xE000EDF0, value: int = 0xA05F0003, ap: int = 0) -> None:
        """割り込み書き込みの番地と値（既定は DHCSR ← C_HALT | C_DEBUGEN = ターゲット停止）。
        ホストの DAP コマンドの境界で行い、ホストの SELECT・CSW・TAR は元に戻す。"""
        with self._lock:
            CrossTrigger(self.session.device).set_inject(address, value, ap)

    def xtrig_set_match(self, address: int, address_mask: int = 0xFFFFFFFF, data: int = 0, data_mask: int = 0,
                        on_write: bool = True, on_read: bool = False) -> None:
        """番地一致（源 dap_match）の条件: デバッガ（ホスト）がこの番地にアクセスしたら成立。data_mask = 0 なら値を見ない。"""
        with self._lock:
            CrossTrigger(self.session.device).set_match(address, address_mask=address_mask, data=data,
                                                        data_mask=data_mask, on_write=on_write, on_read=on_read)

    def xtrig_set_sequence(self, stages: list[dict], pattern_a: dict | None = None,
                           pattern_b: dict | None = None, arm: bool = True) -> dict:
        """多段トリガ（源 "sequence"）。stages は 1〜4 個の {"when": ["pattern_a" / "pattern_b" / "dap_match" / "host"],
        "count": 回数（既定 1）, "within_us": 段に入ってからの制限時間（省略で無制限、段 0 では無視）}。
        pattern_a / pattern_b は {"mask", "value", "rise", "fall"}（ロジアナのチャンネルのビット）。
        例: 「ch3 の立ち上がりの後 10 µs 以内に ch5 の立ち下がり」= stages=[{"when": ["pattern_a"]},
        {"when": ["pattern_b"], "within_us": 10}], pattern_a={"rise": 8}, pattern_b={"fall": 32}。
        xtrig_route の to_la / to_inject に "sequence" を入れてつなぐ。単発（再び使うときは arm）。"""
        names = {"pattern_a": Event.PATTERN_A, "pattern_b": Event.PATTERN_B, "dap_match": Event.DAP_MATCH,
                 "host": Event.HOST}
        seq_stages = []
        for st in stages:
            when = Event(0)
            for name in st["when"]:
                when |= names[name]
            within = st.get("within_us")
            seq_stages.append(Stage(when, int(st.get("count", 1)), None if within is None else within * 1e-6))
        with self._lock:
            seq = TriggerSequencer(self.session.device)
            seq.configure(seq_stages, Pattern(**(pattern_a or {})), Pattern(**(pattern_b or {})))
            if arm:
                seq.arm()
            stage, fired, _ = seq.status()
            return {"stages": len(seq_stages), "armed": arm, "stage": stage, "fired": fired}

    def xtrig_sequence_status(self) -> dict:
        """多段トリガの状態（レジスタの事実）: 今の段、発火したか、発火した時刻（タイムベースのカウント）、仕掛けているか
        （armed）、設定した段の数（stages）と、段ごとの状態 stage_states（done = 済んだ・current = 待っている段・
        waiting = まだ・idle = 仕掛けていない。発火したら全部 done）。config は段とピンの条件のレジスタの読み戻し
        （xtrig_set_sequence の stages・pattern_a・pattern_b と同じ形。within_us は刻みに丸めた値、制限なしは None）。"""
        with self._lock:
            seq = TriggerSequencer(self.session.device)
            stage, fired, time_ = seq.status()
            armed, stages = seq.control()
            configured, a, b = seq.config(stages)
        states = ["done" if fired else "idle" if not armed else
                  "done" if k < stage else "current" if k == stage else "waiting" for k in range(stages)]
        names = {Event.PATTERN_A: "pattern_a", Event.PATTERN_B: "pattern_b", Event.DAP_MATCH: "dap_match",
                 Event.HOST: "host"}
        config = {"stages": [{"when": [n for e, n in names.items() if st.when & e], "count": st.count,
                              "within_us": None if st.within_s is None else st.within_s * 1e6} for st in configured],
                  "pattern_a": vars(a), "pattern_b": vars(b)}
        return {"stage": stage, "fired": fired, "time": time_, "armed": armed, "stages": stages,
                "stage_states": states, "config": config}

    def xtrig_fire(self) -> None:
        """源 host を 1 回発火する。"""
        with self._lock:
            CrossTrigger(self.session.device).fire()

    def xtrig_status(self) -> dict:
        """割り込み書き込みの状態（結果・回数・時刻）と中身（ap・address・value。レジスタの読み戻し）、番地一致の回数・
        時刻（タイムベースのカウント）と条件（address・address_mask・data・data_mask・on_write・on_read。読み戻し）、いまの経路 route
        （レジスタから読んだ to_la・to_inject の源の名前。xtrig_route 以外の道具（fpga_obs.trigger_set・sampler.trigger_set・
        ルール）が書いたビットも含む）。"""
        with self._lock:
            xt = CrossTrigger(self.session.device)
            count, time_ = xt.match_status()
            match = xt.match_config()
            ap, address, value = xt.inject_config()
            route = self.session.device.read32(g.XTRIG_ROUTE)

            def names(field) -> list[str]:
                bits = route >> field[0] & (1 << field[1]) - 1
                return [s.name.lower() for s in Source if bits & int(s)]
            return {"inject": {**vars(xt.inject_status()), "ap": ap, "address": address, "value": value},
                    "match": {"count": count, "time": time_, **match},
                    "route": {"to_la": names(g.XTRIG_ROUTE_TO_LA), "to_inject": names(g.XTRIG_ROUTE_TO_INJECT)}}

    @staticmethod
    def _need_inject(caps, halt: bool) -> None:
        """halt（割り込み書き込みの経路）を使うなら、故障注入の回路が要る（無い構成では経路を立てても何も起きない）。"""
        if halt and not getattr(caps, "inject", True):
            raise RuntimeError("この構成に故障注入（割り込み書き込み。ケイパビリティ inject）が無いので halt はできない"
                               "（la だけなら使える）")

    def _route_bit(self, source: Source, halt: bool, la: bool) -> None:
        """クロストリガの経路の source のビットだけを変える（_lock の中で）。"""
        dev = self.session.device
        route = dev.read32(g.XTRIG_ROUTE)
        bit = int(source)
        for field, on in ((g.XTRIG_ROUTE_TO_INJECT, halt), (g.XTRIG_ROUTE_TO_LA, la)):
            route = route | bit << field[0] if on else route & ~(bit << field[0])
        dev.write32(g.XTRIG_ROUTE, route)
