"""Probe の道具: Flash 上の無制限ブレークポイント（fprobe.flash_bp）。J-Link の Unlimited Flash Breakpoints、
IAR の Flash ブレークポイントの相当。

Probe（fprobe.mcp_server）に混ぜて使う mixin。Flash（書き込みアルゴリズムのある領域）を持つターゲットは、Probe.target が
FlashBreakpoints で包んだものを返す。ブレークポイントの道具（breakpoint_set・GDB の Z0・行単位のステップの踏み台・
関数の呼び出し）は何も変えずに、比較器が尽きたら Flash の BKPT を使う。

- 接続（target_connect）で、前のプロセスが異常終了して残した BKPT を記録（FPROBE_HOME/flash_breakpoints/）から戻す。
- 切断（target_disconnect・close）とプロセスの終了で、書いた BKPT をすべて戻す。
- Flash の書き込み・消去（probe_flash）は、書いた BKPT の記録を合わせる（_flash_bp_program・_flash_bp_erased）。
"""

import logging
import os
import re
from pathlib import Path

from pyocd.core.target import Target

from .flash import Image
from .flash_bp import DORMANT_LIMIT, FlashBreakpoints, Journal

__all__ = ["FlashBreakpointTools"]

LOG = logging.getLogger(__name__)


def _has_flash(target) -> bool:
    memory_map = getattr(target, "memory_map", None)
    if memory_map is None:
        return False
    try:
        return any(r.flash is not None for r in memory_map.iter_matching_regions(is_flash=True))
    except Exception:                                           # 偽物・特殊なターゲットのメモリマップ
        return False


class FlashBreakpointTools:
    def _init_flash_bp(self) -> None:
        self._flash_bp: FlashBreakpoints | None = None
        self._flash_bp_settings = {"enabled": True, "dormant_limit": DORMANT_LIMIT, "scratch": None}

    # --- 包む・接続・切断 ----------------------------------------------------------------------------

    def _flash_bp_wrap(self, raw):
        """Probe.target から: Flash を持つターゲットなら包んだもの（同じターゲットには同じもの）、なければそのまま。"""
        fbp = self._flash_bp
        if fbp is not None and fbp.target is raw:
            return fbp
        if not _has_flash(raw):
            return raw
        settings = self._flash_bp_settings
        self._flash_bp = FlashBreakpoints(raw, journal=self._flash_bp_journal(), persistent=self._flash_bp_persistent,
                                          scratch=settings["scratch"], enabled=settings["enabled"],
                                          dormant_limit=settings["dormant_limit"], guards=self._flash_guards())
        return self._flash_bp

    def _flash_bp_persistent(self, address: int) -> bool:
        """長く残るブレークポイント（利用者・GDB が置いたもの）か。ステップの踏み台は違う。"""
        return self._dbg is not None and address in self._dbg.breakpoints

    def _flash_bp_journal(self) -> Journal:
        # プローブとターゲットの組ごと（同じ種類のターゲットを 2 台のプローブで同時に使っても記録が混ざらない）
        probe_id = self._serial or self._port or self._eth or "default"
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{self._target_type or 'target'}-{probe_id}")
        # FPROBE_HOME はデーモン（fprobe.api.daemon.fprobe_home）と同じ
        home = Path(os.environ.get("FPROBE_HOME", Path.home() / ".fprobe"))
        return Journal(home / "flash_breakpoints" / f"{name}.json", name)

    def _flash_bp_attach(self) -> dict | None:
        """接続した直後に: 前のプロセスが残した BKPT を戻す。何かあれば {"restored", "skipped"}。"""
        target = self.target
        if not isinstance(target, FlashBreakpoints):
            return None
        out = target.recover()
        return out if out["restored"] or out["skipped"] else None

    def _flash_bp_detach(self) -> None:
        """切断の前に: 書いた BKPT を戻す。戻せなければ記録が残り、次の接続で戻す。"""
        fbp, self._flash_bp = self._flash_bp, None
        if fbp is None:
            return
        try:
            fbp.close()
        except Exception as e:
            LOG.warning("Flash のブレークポイントを戻せなかった（記録 %s が残り、次の接続で戻す）: %s",
                        fbp.journal.path if fbp.journal else "-", e)

    def _flash_bp_active(self) -> FlashBreakpoints | None:
        fbp = self._flash_bp
        return fbp if fbp is not None and self._debug is not None and fbp.target is self._debug.target else None

    # --- Flash の書き込み・消去（probe_flash から） -------------------------------------------------

    def _flash_bp_program(self, memory, image: Image, **options):
        """image を Flash に書く。書くセクタにある BKPT は元の中身にして一緒に書き、記録を捨てる（置いたものは次の
        再開で書き直す）。"""
        fbp = self._flash_bp_active()
        if fbp is None:
            return memory.program(image, **options)
        folded = fbp.fold(image)
        report = memory.program(folded, **options)
        if report.erase_method == "chip":
            fbp.forget_all()
        else:
            fbp.forget(folded)
        return report

    def _flash_bp_erased(self, mode: str, address: int | None = None, length: int | None = None) -> None:
        fbp = self._flash_bp_active()
        if fbp is None:
            return
        if mode == "range":
            fbp.forget_range(address, length)
        else:
            fbp.forget_all()

    def _breakpoint_kind(self, address: int) -> dict:
        """breakpoint_list の各項の種類: hw（比較器）/ flash（Flash の BKPT）/ sw（RAM の BKPT）。未接続なら付けない
        （機器には触らない。どれもホストが覚えている状態）。"""
        if self._debug is None:
            return {}
        target = self.target
        if isinstance(target, FlashBreakpoints):
            kind = target.kind_of(address)
            if kind is not None:
                return {"kind": kind}
        find = getattr(target, "find_breakpoint", None)
        bp = find(address) if find is not None else None
        if bp is None:
            return {}
        return {"kind": "sw" if bp.type == Target.BreakpointType.SW else "hw"}

    # --- 道具 ----------------------------------------------------------------------------------------

    def flash_breakpoints(self, enabled: bool | None = None, dormant_limit: int | None = None,
                          scratch: int | None = None) -> dict:
        """Flash のブレークポイントの様子と設定。enabled: 比較器が尽きたら Flash の BKPT を使う（既定 True）。
        dormant_limit: 外した BKPT に何回当たったら Flash を戻すか（それまでは黙って踏み越える）。scratch: 元の命令を
        実行する RAM の番地（8 バイト。省略時は Flash の書き込みアルゴリズムの RAM の先頭）。"""
        if dormant_limit is not None and dormant_limit < 1:
            raise ValueError("dormant_limit は 1 以上")
        with self._lock:
            target = self.target if self._debug is not None else None
            fbp = target if isinstance(target, FlashBreakpoints) else None
            if enabled is False and fbp is not None and fbp.status()["flash"]:
                raise ValueError("Flash のブレークポイントが置いてある（先に debug_breakpoint_clear で外す）")
            for key, value in (("enabled", enabled), ("dormant_limit", dormant_limit), ("scratch", scratch)):
                if value is not None:
                    self._flash_bp_settings[key] = value
            if fbp is None:
                return {**self._flash_bp_settings, "available": False}
            if enabled is not None:
                fbp.enabled = enabled
                if not enabled:
                    fbp.restore_all()                          # 休眠中の BKPT も戻す
            if dormant_limit is not None:
                fbp.dormant_limit = dormant_limit
            if scratch is not None:
                fbp.scratch = scratch
            status = fbp.status()
            status["flash"] = [{**e, **self._place_of(e["address"])} for e in status["flash"]]
            return {**status, "available": True}

    def flash_breakpoints_restore(self) -> dict:
        """いま Flash に書いてある BKPT をすべて元に戻す（ほかのツールに渡す前等）。置いたブレークポイントは残り、次の
        再開で書き直す。"""
        with self._lock:
            fbp = self.target
            if not isinstance(fbp, FlashBreakpoints):
                return {"restored": 0}
            return {"restored": fbp.restore_all()}
