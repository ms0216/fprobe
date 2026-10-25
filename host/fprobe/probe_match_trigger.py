"""ハードの値の一致のトリガ（fpga_obs_trigger_*・sampler_trigger_*）の mixin。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う。成り立った瞬間にクロストリガの経路（源 obs_match / sampler_match）で MCU の停止
（halt）・ロジアナのトリガ（la）を起こす。Probe の _lock・session・_caps・fpga_obs_signals_get を使う。
仕様: docs/superpowers/specs/2026-10-21-system-analysis-design.md §4.1、2026-10-23-sampler-match-trigger-design.md
"""

import json
from pathlib import Path

from .home import fprobe_home
from . import generated as g
from .collector import unwrap48
from .fpga_obs import compile_match
from .fpga_signals import FpgaSignals
from .xtrig import Source


def _obs_trigger_path() -> Path:
    """仕掛けた FPGA の条件のトリガ（条件の文字・halt・la・レジスタの値）。デーモンの再起動の後に条件の文字で見せる。"""
    return fprobe_home() / "obs_trigger.json"


def _save_obs_trigger(trigger: dict | None) -> None:
    path = _obs_trigger_path()
    try:
        if trigger is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(trigger, ensure_ascii=False), encoding="utf-8")
    except OSError:                                         # 覚えられなくても、トリガそのものは働く
        pass


def _load_obs_trigger() -> dict | None:
    try:
        return json.loads(_obs_trigger_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _need_action(halt: bool, la: bool) -> None:
    """成り立ったときの動作（halt = MCU の停止、la = ロジアナのトリガ）の少なくとも一方を使う側が選ぶ。"""
    if not halt and not la:
        raise ValueError("halt か la の少なくとも一方が要る（どちらも無ければ成立しても何も起きない）")


def _need_sampler_match(caps) -> None:
    if not getattr(caps, "sampler_match", False):
        raise RuntimeError("この構成にはサンプラの値の一致のトリガが無い（ケイパビリティ sampler_match）")


class MatchTriggerTools:
    """Probe に混ぜるハードの値の一致のトリガ。"""

    def _init_match_trigger(self) -> None:
        self._obs_trigger: dict | None = None              # 仕掛けた FPGA の条件のトリガ（condition・halt・la）

    def fpga_obs_trigger_set(self, condition: str, halt: bool = True, la: bool = False, group: int = 0) -> dict:
        """FPGA の条件（システム解析と同じ書き方。例 "restart changed and state=BUSY"・"state=BUSY"（入った瞬間）・
        "flag rose"。瞬間の項は 1 つまで）を fprobe の観測のエンジンで比べ、成り立ったらハードのクロストリガ
        （源 obs_match）: halt = 割り込み書き込み（既定は MCU の停止）、la = ロジアナのトリガ（クロストリガだけで
        捕捉するには la_arm / la_deep_arm の external_only=true（深いバッファは rle=False が速い。RLE は信号の変化が少ないとトリガ後の拍が埋まるのに十数秒かかる））。遅れは観測のスキャン 1 回分
        （スナップショット型・TCK 2.5 MHz で約 15 µs）+ SWD の書き込み（ホストの CSW・TAR の控えがあれば 4 転送 = 約 190
        SWCLK、無ければ読んで戻す転送が足される。2026-10-22 の Shrike-Lite の実測: SWD 4 MHz で 101 µs、1 MHz で
        206〜362 µs）。信号の名前は組 group の定義（fpga_obs_signals_get）から。
        docs/superpowers/specs/2026-10-21-system-analysis-design.md §4.1"""
        with self._lock:
            caps = self.session.device.capabilities()
            if not getattr(caps, "obs_match", False):
                raise RuntimeError("この構成にはハードの FPGA の条件のトリガが無い（ケイパビリティ obs_match）")
            self._need_inject(caps, halt)
            defs = self.fpga_obs_signals_get()
            _need_action(halt, la)
            mask, value, change = compile_match(condition, FpgaSignals.from_dict(defs["recording"] or defs["signals"]),
                                                group)
            self._obs_trigger = None                                     # 書く途中で失敗しても古い記憶を残さない
            dev = self.session.device
            dev.write32(g.OBS_MATCH_CHANGE, 0)                          # 変える間は止める（途中の組で発火しない）
            dev.write32(g.OBS_MATCH, mask | value << 16)
            dev.write32(g.OBS_MATCH_CHANGE, change | 1 << g.OBS_MATCH_CHANGE_ENABLE[0])
            self._obs_route(halt, la)
            self._obs_trigger = {"condition": condition, "halt": halt, "la": la}
            _save_obs_trigger({**self._obs_trigger, "mask": mask, "value": value, "change": change})
            return {"mask": mask, "value": value, "change": change, **self._obs_trigger}

    def fpga_obs_trigger_status(self) -> dict:
        """ハードの FPGA の条件のトリガ: 条件・halt・la・成り立った回数 count（無ければ condition = null）。
        条件の文字はデーモンにしか無いので、デーモンの再起動の前に仕掛けたもの（FPGA には残って MCU を止め続ける）は
        レジスタから orphan = true（mask・value・change・halt・la・count）で返す。fpga_obs_trigger_clear で外せる。"""
        with self._lock:
            trig = self._obs_trigger
            armed = self._obs_armed()
            if trig is not None and armed is None:                   # FPGA を書き直した・経路を外された: もう無い
                self._obs_trigger = trig = None
            if trig is not None:
                return {**trig, "count": self.session.device.read32(g.OBS_MATCH_COUNT)}
            if armed is None:
                return {"condition": None}
            dev = self.session.device
            match = dev.read32(g.OBS_MATCH)
            regs = {"mask": match & 0xFFFF, "value": match >> 16 & 0xFFFF, "change": dev.read32(g.OBS_MATCH_CHANGE) & 0xFFFF}
            saved = _load_obs_trigger()                        # デーモンの再起動の前に仕掛けた条件の文字
            if saved and all(saved.get(k) == v for k, v in regs.items()) and saved.get("halt") == armed["halt"]                     and saved.get("la") == armed["la"]:
                self._obs_trigger = {"condition": saved["condition"], "halt": armed["halt"], "la": armed["la"]}
                return {**self._obs_trigger, "restored": True, "count": dev.read32(g.OBS_MATCH_COUNT)}
            return {"condition": None, "orphan": True, **regs, **armed, "count": dev.read32(g.OBS_MATCH_COUNT)}

    def fpga_obs_trigger_clear(self) -> dict:
        """ハードの FPGA の条件のトリガをやめる（クロストリガの経路の obs_match を外す。ほかの源の経路は触らない）。
        デーモンの再起動の前に仕掛けた条件（orphan）も外す。"""
        with self._lock:
            if self._obs_trigger is not None or self._obs_armed() is not None:
                self.session.device.write32(g.OBS_MATCH_CHANGE, 0)
                self._obs_route(False, False)
            self._obs_trigger = None
            _save_obs_trigger(None)
            return {"condition": None}

    def _obs_armed(self) -> dict | None:
        """FPGA の条件のトリガがレジスタの上で生きていれば {halt, la}（_lock の中で）。無い・この構成に無ければ None。"""
        dev = self.session.device
        if not getattr(self._caps(), "obs_match", False):
            return None
        if not dev.read32(g.OBS_MATCH_CHANGE) >> g.OBS_MATCH_CHANGE_ENABLE[0] & 1:
            return None
        route, bit = dev.read32(g.XTRIG_ROUTE), int(Source.OBS_MATCH)
        halt = bool(route >> g.XTRIG_ROUTE_TO_INJECT[0] & bit)
        la = bool(route >> g.XTRIG_ROUTE_TO_LA[0] & bit)
        return {"halt": halt, "la": la} if halt or la else None

    def _obs_route(self, halt: bool, la: bool) -> None:
        """クロストリガの経路の obs_match のビットだけを変える（_lock の中で）。"""
        self._route_bit(Source.OBS_MATCH, halt, la)

    def sampler_trigger_set(self, address: int, mask: int, value: int, halt: bool, la: bool) -> dict:
        """DAP サンプラが読んだ address の語が (値 xor value) and mask = 0 に入った瞬間（前の標本が不一致・今の標本が一致）に
        ハードのクロストリガ（源 sampler_match）: halt = 割り込み書き込み（既定は MCU の停止）、la = ロジアナのトリガ。
        例: MCU が止まった = address 0xE000EDF0（DHCSR）・mask = value = 0x20000（S_HALT）。サンプラに address を読ませるのは
        別の道具（watch 等。この API はサンプラの持ち主にならない）。読んでいなければ sampler_trigger_status の seen が 0 のまま。
        有効にした後の最初の標本では発火しない（その前を見ていない）。"""
        _need_action(halt, la)
        for name, v in (("address", address), ("mask", mask), ("value", value)):
            if not 0 <= v <= 0xFFFFFFFF:
                raise ValueError(f"{name} は 32 ビット（{v:#x}）")
        with self._lock:
            _need_sampler_match(self._caps())
            self._need_inject(self._caps(), halt)
            dev = self.session.device
            dev.write32(g.SAMPLER_MATCH_CONTROL, 0)                    # 変える間は止める
            dev.write32(g.SAMPLER_MATCH_ADDR, address)
            dev.write32(g.SAMPLER_MATCH_MASK, mask)
            dev.write32(g.SAMPLER_MATCH_VALUE, value)
            dev.write32(g.SAMPLER_MATCH_CONTROL, 1 << g.SAMPLER_MATCH_CONTROL_ENABLE[0])
            self._route_bit(Source.SAMPLER_MATCH, halt, la)
            return {"address": address, "mask": mask, "value": value, "halt": halt, "la": la}

    def sampler_trigger_status(self) -> dict:
        """サンプラの値の一致のトリガのレジスタの事実: enabled・address・mask・value・halt・la（経路）・matched（最後の標本が
        一致）・seen_any・seen（有効にした後に比べた標本の数。0 ならサンプラがその番地を読んでいない）・count（発火の回数）・
        time（最後に発火した標本を受けた時刻）・prev_time（その直前の不一致の標本の時刻。条件に入ったのは prev_time と time の間の
        どこか）。回路の時刻は 48 ビット（イベントの時刻と同じ幅）なので、読んだ時のタイムベースの上位で 64 ビットに延ばして返す
        （発火が無い・標本が無いときは回路の値のまま）。"""
        with self._lock:
            _need_sampler_match(self._caps())
            dev = self.session.device
            # 1 回のまとめ読み（数・時刻を同じ時点に近づける。ハードにラッチは無い）
            address, mask, value, ctrl, seen, count, t_lo, t_hi, p_lo, p_hi = dev.read(g.SAMPLER_MATCH_ADDR, 10)
            route, bit = dev.read32(g.XTRIG_ROUTE), int(Source.SAMPLER_MATCH)
            now = dev.time()                                           # 48 ビットの時刻を延ばす基準（回路の値より後に読む）
            time_ = unwrap48(t_hi << 32 | t_lo, now) if count else t_hi << 32 | t_lo
            prev_time = unwrap48(p_hi << 32 | p_lo, now) if ctrl >> g.SAMPLER_MATCH_CONTROL_SEEN_ANY[0] & 1 else p_hi << 32 | p_lo
            return {"enabled": bool(ctrl >> g.SAMPLER_MATCH_CONTROL_ENABLE[0] & 1),
                    "address": address, "mask": mask, "value": value,
                    "halt": bool(route >> g.XTRIG_ROUTE_TO_INJECT[0] & bit), "la": bool(route >> g.XTRIG_ROUTE_TO_LA[0] & bit),
                    "matched": bool(ctrl >> g.SAMPLER_MATCH_CONTROL_MATCHED[0] & 1),
                    "seen_any": bool(ctrl >> g.SAMPLER_MATCH_CONTROL_SEEN_ANY[0] & 1),
                    "seen": seen, "count": count, "time": time_, "prev_time": prev_time}

    def sampler_trigger_clear(self) -> dict:
        """サンプラの値の一致のトリガをやめる（比べるのを止め、経路の sampler_match を外す。ほかの源の経路は触らない）。"""
        with self._lock:
            if getattr(self._caps(), "sampler_match", False):
                self.session.device.write32(g.SAMPLER_MATCH_CONTROL, 0)
                self._route_bit(Source.SAMPLER_MATCH, False, False)
            return {"enabled": False}
