"""`HaltDebugger` の部品: 場所の解決とブレークポイントの置き外し（`resolve`・`break_insert`・`break_remove`）。
"""
from __future__ import annotations

from .bp import NoFreeSlot
from .debugger_common import MASK
from .engine import EngineError
from .mmu import Unmapped


class DebuggerBreakMixin:
    """ブレークポイント（`HaltDebugger` の状態を使う）。"""

    def resolve(self, location: str) -> tuple[int, int, bool | None]:
        """場所 → (実行時の番地, リンク時の番地, prologue_end)。番地（"0x…"・10 進。実行時）・"ファイル:行"（その行の最初の
        番地）・関数名（前置きの後。prologue_end = 行表に prologue_end があったか。ほかは None）。解けなければ EngineError。"""
        text = location.strip()
        prologue_end = None
        try:
            if text.lower().startswith("0x") or text.isdigit():
                address = int(text, 0)
                return address, self._link(address), None
            link = self.symbols.resolve(text)
            if not (":" in text and text.rpartition(":")[2].isdigit()):     # 関数名: 前置きの後
                start = link
                link = self.symbols.after_prologue(start)
                prologue_end = self.symbols.has_prologue_end(start)
            return (link + self.load_bias) & MASK, link, prologue_end
        except (KeyError, ValueError) as e:
            raise EngineError("location %r: %s" % (location, e.args[0] if e.args else e)) from None

    def break_insert(self, location: str, kind: str) -> dict:
        def run():
            if kind not in ("sw", "hw"):
                raise EngineError("kind must be 'sw' or 'hw' (got %r)" % (kind,))
            address, link, prologue_end = self.resolve(location)
            function = self.symbols.function_at(link)
            if function is None:
                # 実行ファイルの外（共有ライブラリの中の番地。例 "0x76dc02ec"）: 取り寄せたライブラリのシンボルの bit 0 で ARM / Thumb を知る
                try:
                    width = self._width_at(address)
                except EngineError as e:
                    raise EngineError("location %r: %s. Breakpoints by name or file:line use the attached ELF only; "
                                      "a code address inside a shared library works when its symbols were fetched" % (location, e)) from None
            else:
                width = 2 if function.address in self.image.thumb_functions else 4
            if self._bp_at(address) is not None:
                raise EngineError("location %r (0x%08x): there is already breakpoint id %d"
                                  % (location, address, self._bp_at(address)))
            try:
                self.engine.bps.insert(self.proc.tgid, address, kind=kind, width=width)
            except Unmapped as e:
                raise EngineError("location %r (0x%08x): the page is not mapped (%s); bp_unmapped=%s"
                                  % (location, address, e, self.engine.bps.unmapped)) from None
            except NoFreeSlot as e:
                message = "location %r (0x%08x): no free hardware breakpoint (%s)" % (location, address, e)
                raise EngineError(message) from None
            placed = self.engine.bps.lookup(self.proc.tgid, address)
            bp = {"id": self._next_id, "location": location, "address": address, "link_address": link,
                  "kind": "hw" if placed.hw else "sw", "requested_kind": kind, "width": width,
                  "prologue_end": prologue_end, **self._where(address)}
            self.breakpoints[self._next_id] = bp
            self._next_id += 1
            return dict(bp)
        return self._call(run, halted="auto")

    def break_remove(self, bp_id: int) -> dict:
        def run():
            bp = self.breakpoints.get(bp_id)
            if bp is None:
                raise EngineError("no breakpoint id %d (ids: %s)" % (bp_id, sorted(self.breakpoints)))
            self.engine.bps.remove(self.proc.tgid, bp["address"], kind=bp["kind"])
            del self.breakpoints[bp_id]
            return {"removed": dict(bp)}
        return self._call(run, halted="auto")
