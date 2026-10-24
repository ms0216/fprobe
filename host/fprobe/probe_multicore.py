"""Probe の道具: マルチコア（U31）。コアごとに DP を持つ複数ドロップの SWD の部品（RP2040）で、コアを選んで接続し、
両方のコアの状態を出し、接続していないコアを止める・再開する。

Probe（fprobe.mcp_server）に混ぜて使う mixin。Probe の _lock・_debug・_link・session・target_connect と _place_of を使う。

- コアの選択: target_connect の core（pyOCD のターゲット名を fprobe.multidrop の表で選ぶ）。target_core_select は
  同じ速度で connect_mode="attach"（止めずに）別のコアにつなぎ直す。ブレークポイント・レジスタ等の道具はすべて
  接続中のコアに効く。
- もう一方のコア: fprobe.multidrop.CoreProbe で DP を一時的に選び直して DHCSR を読み書きする（target_status の
  cores、target_halt / target_resume の core）。同じ線の DAP サンプラ（watch 等）が動いている間は、サンプラの読みが
  選び直した DP に向かうので覗かない。
"""

import time

from . import generated as g
from .multidrop import CoreProbe, MultidropPart, part_of

__all__ = ["MulticoreTools"]

_SAMPLER_BUSY = ("DAP サンプラ（watch 等）が同じ線で動いている間は、もう一方のコアを覗かない（DP を選び直すと"
                 "サンプラの読みがそちらへ向かう）: サンプラを止めてから")


class MulticoreTools:
    """コアの選択と、接続していないコアの状態・停止・再開（Probe の mixin）。"""

    def _multidrop(self) -> tuple[MultidropPart, int] | None:
        """接続中なら (部品, 接続中のコア)。コアごとに DP を持つ部品でなければ None。"""
        link = self._link or {}
        if link.get("core") is None:
            return None
        found = part_of(link.get("target"))
        return (found[0], link["core"]) if found else None

    def _sampler_running(self) -> bool:
        device = self.session.device
        return bool(getattr(device.capabilities(), "sampler", False) and device.read32(g.SAMPLER_CONTROL) & 1)

    def _core_probe(self) -> CoreProbe:
        part, home = self._multidrop()
        if self._sampler_running():
            raise RuntimeError(_SAMPLER_BUSY)
        probe = self._debug.probe
        probe.flush()                                         # pyOCD が溜めた転送を、DP を選び直す前に出す
        # pyOCD の CMSISDAPProbe は CMSIS-DAP の層（DAPAccessCMSISDAP）を _link に持つ。RawDap はその層の上で動く
        return CoreProbe(probe._link, part, home, (self._link or {}).get("frequency_hz"))

    def _core_entry(self, index: int, values: dict, selected: bool) -> dict:
        entry = {"core": index, "selected": selected, "state": values["state"]}
        if "pc" in values:
            entry["pc"] = values["pc"]
            entry.update(self._place_of(values["pc"]))
        return entry

    def _multicore_status(self, status: dict) -> dict:
        """target_status に足す: core（接続中のコア）と cores（各コアの状態。読めなければ error）、部品の表
        （fprobe.multidrop.PARTS）に救済の DP があれば rescue_dp（その TARGETSEL。target_reset の kind=rescue が使える）。"""
        found = self._multidrop()
        if found is None:
            return status
        part, home = found
        status["core"] = home
        if part.rescue is not None:
            status["rescue_dp"] = part.rescue
        cores = []
        for core in part.cores:
            if core.index == home:
                cores.append(self._core_entry(home, status, True))
                continue
            try:
                values = self._core_probe().visit(core.index, lambda c: c.state())
                cores.append(self._core_entry(core.index, values, False))
            except Exception as e:                          # 覗けなくても接続中のコアの状態は返す
                cores.append({"core": core.index, "selected": False, "error": str(e)})
        status["cores"] = cores
        return status

    def _rescue(self) -> None:
        """救済の DP でチップ全体（USB 等の周辺も）をリセットし、最後の接続と同じ設定で attach でつなぎ直して、普通の
        リセット（SYSRESETREQ）で Flash から起動させる（_lock の中で）。ファームウェアが固まり、コアだけのリセットでは
        周辺が戻らないとき。"""
        found = self._multidrop()
        if found is None or found[0].rescue is None:
            raise ValueError("rescue のリセットは救済の DP を持つ部品（RP2040）に接続中だけ")
        connect = dict(getattr(self, "_last_connect", None) or {"target": self._link["target"]})
        probe = self._core_probe()
        self._monitor_detach()                              # チップが生きている間に: Flash に書いた BKPT を戻す等
        self._flash_bp_detach()
        probe.rescue()
        try:
            self.target_disconnect()
        except Exception:                                   # noqa: BLE001  チップはリセット済み: 片付けに失敗しても捨てる
            self._debug, self._link = None, None
        time.sleep(0.05)                                    # リセットからブート ROM が止まるまで
        self.target_connect(**{**connect, "connect_mode": "attach"})
        self._clear_halt_reason()                           # 前の停止の理由（DFSR）を残さない
        self.dbg.reset("system", False)

    def _other_core(self, core: int, action: str) -> dict | None:
        """core が接続中のコアでなければ、そのコアを action（halt / resume）して状態を返す。接続中のコアなら None。"""
        found = self._multidrop()
        if found is None:
            raise ValueError("core を選べるのはコアごとに DP を持つ複数ドロップの部品（RP2040 等）に接続中だけ")
        part, home = found
        part.core(core)
        if core == home:
            return None
        values = self._core_probe().visit(core, lambda c: getattr(c, action)())
        return self._core_entry(core, values, False)

    def target_core_select(self, core: int, connect_mode: str = "attach") -> dict:
        """接続するコアを替える（同じ部品・同じ速度で、既定は止めずに attach でつなぎ直す）。以後の道具はそのコアに効く。"""
        with self._lock:
            found = self._multidrop()
            if found is None:
                raise ValueError("コアを替えられるのはコアごとに DP を持つ複数ドロップの部品（RP2040 等）に接続中だけ")
            part, _ = found
            frequency_hz = self._link["frequency_hz"]
        return self.target_connect(part.name, frequency_hz=frequency_hz, connect_mode=connect_mode, core=core)
