"""Linux のプロセスの全コア停止デバッグの見せ方（LinuxDebugView に混ぜ込む部品）: ブレークポイントの一覧・設定・無効化・解除。"""
from __future__ import annotations

from ..symbols import Location

__all__ = ["ViewBreakpointsMixin"]


class ViewBreakpointsMixin:
    """debug.breakpoint.* の形のブレークポイント（self.d の HaltDebugger に置き、無効にしたものは self._disabled に持つ）。"""

    def _row(self, bp: dict, enabled: bool) -> dict:
        loc = self.d.symbols.describe(self.d._link(bp["address"]))
        row = {"address": bp["address"], "location": bp["location"], "enabled": enabled,
               "where": str(Location(bp["address"], loc.function, loc.offset, loc.file, loc.line))
               if loc.function is not None or loc.file else f"0x{bp['address']:08X}"}
        for key in ("function", "file", "line"):
            if bp.get(key) is not None:
                row[key] = bp[key]
        at = self.d.symbols.line_place(bp["location"])
        if at is not None:
            row["at"] = at
        if enabled:
            row["kind"] = bp["kind"]
        return row

    def breakpoint_list(self) -> dict:
        def go():
            rows = [self._row(b, True) for b in self.d.breakpoints.values()]
            rows += [self._row(b, False) for b in self._disabled.values()]
            return {"breakpoints": sorted(rows, key=lambda b: b["address"]), "watchpoints": []}
        return self.d.use(go, halted=False)

    def breakpoint_enable(self, location: str, enabled: bool) -> dict:
        """無効 = 外して場所と種類を覚える、有効 = 同じ場所・種類で置き直す（マイコンと同じ）。"""
        def go():
            if enabled:
                bp = self._disabled.get(location) or next(
                    (b for b in self._disabled.values() if b["address"] == self.d.resolve(location)[0]), None)
                if bp is None:
                    self._find(location)                         # 置いてあれば何もしない（無ければ KeyError）
                    return self.breakpoint_list()
                self.d.break_insert(bp["location"], bp["kind"])
                del self._disabled[bp["location"]]
            else:
                bp = self._find(location)
                self.d.break_remove(bp["id"])
                self._disabled[bp["location"]] = bp
            return self.breakpoint_list()
        return self.d.use(go, halted="auto")                   # 走行中なら短く止めて置き外し、また走らせる（一般のデバッガと同じ）

    def breakpoint_set(self, location: str, condition: str | None = None, ignore_count: int = 0,
                       log: list[str] | None = None, message: str | None = None) -> dict:
        if condition is not None or ignore_count or log is not None or message is not None:
            raise ValueError("Linux のプロセスのブレークポイントには condition・ignore_count・log・message は使えない")
        if self.breakpoint_kind is None:
            raise RuntimeError("linux.halt.debug.attach に breakpoint_kind（sw / hw）を渡していない: 種類を渡さない置き方"
                               "（画面の行のクリック等）には使う種類が要る（linux.halt.debug.break.insert なら kind を渡す）")
        bp = self.d.break_insert(location, self.breakpoint_kind)
        row = self._row(bp, True)
        return {k: row[k] for k in ("address", "where", "function", "file", "line") if k in row}

    def _find(self, location: str) -> dict:
        """置いたときの location の文字列か、同じ番地に解ける場所のブレークポイント。"""
        for b in self.d.breakpoints.values():
            if b["location"] == location:
                return b
        address = self.d.resolve(location)[0]
        for b in self.d.breakpoints.values():
            if b["address"] == address:
                return b
        raise ValueError(f"{location} にブレークポイントが無い")

    def breakpoint_clear(self, location: str | None = None) -> dict:
        def go():
            if location is None:
                self._disabled.clear()
                ids = list(self.d.breakpoints)
            elif location in self._disabled:
                del self._disabled[location]
                ids = []
            else:
                ids = [self._find(location)["id"]]
            for bp_id in ids:
                self.d.break_remove(bp_id)
            return self.breakpoint_list()
        return self.d.use(go, halted="auto")                   # 走行中なら短く止めて外し、また走らせる
