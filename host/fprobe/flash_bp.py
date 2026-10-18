"""Flash 上の無制限ブレークポイント（J-Link の Unlimited Flash Breakpoints、IAR の Flash ブレークポイントの相当）。

ハードの比較器（FPB。Cortex-M4 で 6 個）が尽きたら、Flash の命令を BKPT に書き換えてブレークポイントにする。
FlashBreakpoints は pyOCD のターゲットを包み、ブレークポイントとステップ・再開・読み出しの口だけを差し替える。
Debugger・行単位のステップ（stepping.Runner）・関数の呼び出し・GDB サーバは、包んだものをそのまま使えばよい。

Flash の書き換えを減らす工夫:
- 書くのは再開（resume・reset）の直前だけ。置く・外すは覚えるだけで、そのあいだの出し入れは書き換えにならない。
- まとめ書き: 再開の前に溜まった変更を 1 回の FlashMemory.patch に渡す（同じセクタは 1 回の消去、同じ中身のページは
  書かない）。
- 外したブレークポイントはすぐには戻さない（休眠）。同じセクタを書くついでに戻す。休眠中の BKPT で止まったら、黙って
  踏み越えて走らせ直し、dormant_limit 回当たったらそのセクタを戻す。同じ番地に置き直せば書き換えは 0 回。
- ブレークポイントの上からの再開・ステップは、BKPT の下の元の命令を Flash の外で実行する（fprobe.thumb_step: 模擬か
  借りた RAM での 1 命令）。できない命令だけ書き戻して進め、BKPT は次の再開で書き直す。
- ステップ中はハードを優先: 呼び手が「長く残るもの」（persistent。利用者のブレークポイント）を教えれば、ステップの踏み台
  （すぐ外す一時のもの）に比較器が足りないとき、利用者のものを 1 つ Flash へ移して比較器を空ける（踏み台を Flash に
  書くと、置くたびに書き換えが要る）。比較器が空けば、まだ書いていないものはハードに置く（書かずに済む）。
- ステップ（1 命令）では Flash を書かない。

元に戻す: close（切断）で書いた BKPT をすべて戻す。プロセスの終了時（atexit）も戻す。書き換えの記録（Journal）を
ファイルに先に書いておき（書いてから Flash を書く）、異常終了で残った BKPT は次の接続の recover で戻す。そのときは
BKPT の周りの 32 バイトが記録と同じ（元の中身か、記録した BKPT）であることを確かめ、別のファームウェアが書かれていれば
触らない。戻すときは、いまも BKPT が書いてある番地だけを戻す。

読み出しは BKPT を元の中身に見せる（逆アセンブル・検証・巻き戻しが元の命令を読む。pyOCD のソフトのブレークポイントと
同じ）。Flash を書き込む・消すときは fold・forget・forget_range・forget_all で記録を合わせる（fprobe.probe_flash）。
"""

import atexit
import json
import logging
import os
import weakref
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from pyocd.core.target import Target

from .flash import FlashMemory, Image, Segment
from .thumb_step import NeedsRewrite, OutOfLine, invalidate_icache

__all__ = ["BKPT_CODE", "FlashBreakpoints", "Journal"]

LOG = logging.getLogger(__name__)

BKPT_CODE = b"\x00\xbe"                  # BKPT #0（セミホスティングの 0xAB とは別）
WINDOW = 32                              # 記録で確かめる周りの大きさ
DFSR, DFSR_BKPT, DFSR_HALTED = 0xE000ED30, 0x2, 0x1
DORMANT_LIMIT = 3


@dataclass(frozen=True)
class _Written:
    """Flash に書いた BKPT: 元の中身と、書いたときの周りの元の中身（記録の照合用）。"""
    address: int
    original: bytes
    window: int
    window_original: bytes

    def as_dict(self) -> dict:
        return {"address": self.address, "original": self.original.hex(), "window": self.window,
                "window_original": self.window_original.hex()}

    @classmethod
    def from_dict(cls, d: dict) -> "_Written":
        return cls(d["address"], bytes.fromhex(d["original"]), d["window"], bytes.fromhex(d["window_original"]))


class Journal:
    """書き換えの記録（JSON。書き換えの前に書く）。key: どのターゲットのものか（ターゲットの名前等）。"""

    def __init__(self, path: str | Path, key: str):
        self.path = Path(path)
        self.key = key

    def save(self, entries) -> None:
        entries = sorted(entries, key=lambda e: e.address)
        if not entries:
            self.clear()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps({"version": 1, "target": self.key,
                                         "entries": [e.as_dict() for e in entries]}, indent=1), encoding="utf-8")
        os.replace(temporary, self.path)                       # 途中で落ちても前の記録か新しい記録のどちらか

    def load(self) -> list[_Written] | None:
        """このターゲットの記録。無い・別のターゲットのものなら None。"""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if data.get("target") != self.key:
            return None
        return [_Written.from_dict(e) for e in data.get("entries", [])]

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class FlashBreakpoints:
    """pyOCD のターゲット（CoreSightTarget か CortexM）を包み、Flash のブレークポイントを足す。包んだもの以外の
    属性はそのまま中のターゲットへ渡す。

    persistent(address) -> bool: 長く残るブレークポイント（利用者が置いたもの）か。ステップの踏み台に比較器を譲る判断に
    使う（省略時はすべて長く残るものとみなし、譲らない）。scratch: 元の命令を実行する RAM（8 バイト。省略時は Flash の
    書き込みアルゴリズムが使う RAM の先頭。止めている間だけ借りて戻す）。"""

    def __init__(self, target, journal: Journal | None = None, persistent=None, scratch: int | None = None,
                 enabled: bool = True, dormant_limit: int = DORMANT_LIMIT):
        self.target = target
        self.journal = journal
        self.persistent = persistent if persistent is not None else (lambda address: True)
        self.enabled = enabled
        self.dormant_limit = dormant_limit
        self._memory = FlashMemory(target)
        self._scratch = scratch
        self._hw: set[int] = set()                       # 比較器に置いた（このクラスが置いたもの）
        self._flash: set[int] = set()                    # Flash に置きたいもの（書いた・まだのどちらも）
        self._written: dict[int, _Written] = {}          # Flash に BKPT を書いてある番地
        self._hits: dict[int, int] = {}                  # 休眠中の BKPT に当たった回数
        self._watching = False                           # 再開した後の最初の停止をまだ見ていない
        self._stepped = False                            # 止まった後にステップで PC を動かした（再開の時点の PC は停止の場所でない）
        self.stats = {"commits": 0, "sector_writes": 0, "emulated": 0, "displaced": 0, "rewrites": 0,
                      "dormant_hits": 0, "restored": 0}
        self.last_rewrite: str | None = None
        ref = weakref.ref(self)
        self._exit_hook = lambda: (lambda s: s is not None and s._close_quietly())(ref())
        atexit.register(self._exit_hook)

    def __getattr__(self, name):
        return getattr(self.target, name)

    # --- 状態 -------------------------------------------------------------------------------------

    @property
    def dormant(self) -> list[int]:
        return sorted(a for a in self._written if a not in self._flash)

    def kind_of(self, address: int) -> str | None:
        """このクラスが置いたブレークポイントの種類: "hw"（比較器）/ "flash"（BKPT を書く）/ None。"""
        if address in self._hw:
            return "hw"
        if address in self._flash:
            return "flash"
        return None

    def status(self) -> dict:
        return {"enabled": self.enabled, "hardware": sorted(self._hw),
                "flash": [{"address": a, "state": "written" if a in self._written else "pending"}
                          for a in sorted(self._flash)],
                "dormant": self.dormant, "dormant_limit": self.dormant_limit, "scratch": self.scratch,
                "stats": dict(self.stats), "last_rewrite": self.last_rewrite,
                "journal": None if self.journal is None else str(self.journal.path)}

    @property
    def scratch(self) -> int | None:
        if self._scratch is None:
            try:
                spans = self._memory.algorithm_ram()
            except RuntimeError:                               # アルゴリズムの RAM が分からない: 書き戻して進める
                spans = []
            if spans:
                self._scratch = spans[0][0]
        return self._scratch

    @scratch.setter
    def scratch(self, address: int) -> None:
        self._scratch = address

    def _in_flash(self, address: int) -> bool:
        memory_map = getattr(self.target, "memory_map", None)
        region = memory_map.get_region_for_address(address) if memory_map is not None else None
        return region is not None and region.is_flash and region.flash is not None

    # --- ブレークポイント（pyOCD の口） ---------------------------------------------------------------

    def set_breakpoint(self, address: int, type=Target.BreakpointType.AUTO) -> bool:
        """AUTO: Flash の番地は比較器、尽きたら Flash の BKPT。SW: Flash の番地は比較器を使わずに BKPT。
        HW: 比較器だけ（GDB の Z1）。Flash の外（RAM）は中のターゲット（pyOCD）に任せる。"""
        if not self.enabled or type == Target.BreakpointType.HW or not self._in_flash(address):
            return self.target.set_breakpoint(address, type)
        if address & 1:
            raise ValueError(f"0x{address:08X} は命令の番地ではない（Thumb の命令は 2 バイト境界）")
        if address in self._hw or address in self._flash:
            return True
        if address in self._written:                          # 休眠中の BKPT: そのまま使う（書き換えは要らない）
            self._flash.add(address)
            self._hits.pop(address, None)
            return True
        if type == Target.BreakpointType.AUTO and self.target.set_breakpoint(address, Target.BreakpointType.HW):
            self._hw.add(address)
            return True
        self._flash.add(address)
        if self.target.get_state() != Target.State.HALTED:    # 走っている: いま書く（止めて書き、走らせ直す）
            self.commit()
        return True

    def remove_breakpoint(self, address: int) -> None:
        if address in self._hw:
            self.target.remove_breakpoint(address)
            self._hw.discard(address)
        elif address in self._flash:
            self._flash.discard(address)                      # 書いてあれば休眠（次に要るときまで戻さない）
        else:
            self.target.remove_breakpoint(address)

    def find_breakpoint(self, address: int):
        if address in self._flash:
            return SimpleNamespace(addr=address, type=Target.BreakpointType.SW)
        return self.target.find_breakpoint(address)

    # --- 実行 -------------------------------------------------------------------------------------

    def step(self, disable_interrupts: bool = True, start: int = 0, end: int = 0, hook_cb=None) -> None:
        """1 命令進める（Flash は書かない）。いまの PC に BKPT を書いてあれば、元の命令を Flash の外で実行する
        （割り込みは止めたまま）。start / end（範囲のステップ）は pyOCD と同じ: [start, end) の外に出るまで。"""
        self._stepped = True
        while True:
            pc = self.target.read_core_register("pc")
            if pc in self._written:
                self._step_original(pc)
            else:
                self.target.step(disable_interrupts, start, end, hook_cb)
            if start == end or (hook_cb is not None and hook_cb()):
                return
            pc = self.target.read_core_register("pc")
            if not start <= pc < end:
                return
            dfsr = self.target.read32(DFSR)
            if dfsr & ~DFSR_HALTED and not (dfsr == DFSR_BKPT and pc in self.dormant):
                return

    def resume(self) -> None:
        """再開する。溜まったブレークポイントをここで Flash に書く。いまの PC に休眠中の BKPT があれば先に踏み越える
        （置いているブレークポイントの上なら、ハードと同じくその場で止まり直す。踏み越えは Debugger.resume の役）。"""
        if self.target.get_state() == Target.State.HALTED:
            pc = self.target.read_core_register("pc")
            if pc in self._written and pc not in self._flash:
                # ステップで着いた（模擬した BL の飛び先等）なら当たりとして数える。その BKPT で止まった後の再開は数えない
                if self._stepped:
                    stepped = self._hit_dormant(pc)
                else:
                    self._step_original(pc)
                    stepped = True
                if stepped and self.target.read32(DFSR) & ~DFSR_HALTED:   # 踏み越えた命令でウォッチポイント等が成立した
                    return
            self.commit()
        self._stepped = False
        self.target.resume()
        self._watching = True

    def reset(self, reset_type=None) -> None:
        """リセットして走らせる（溜まったブレークポイントを先に書く）。"""
        self.commit()
        self.target.reset(reset_type)
        self._watching = True

    def halt(self) -> None:
        self._watching = False
        self.target.halt()

    def get_state(self):
        """コアの状態。休眠中の BKPT で止まっていたら、黙って踏み越えて走らせ直す（RUNNING を返す）。"""
        state = self.target.get_state()
        while state == Target.State.HALTED and self._watching:
            self._watching = False
            if not self._pass_dormant():
                break
            state = self.target.get_state()
        return state

    def _pass_dormant(self) -> bool:
        """休眠中の BKPT で止まっていれば踏み越えて走らせ直す（True）。dormant_limit 回目はそのセクタを戻す。"""
        if not self.dormant:
            return False
        pc = self.target.read_core_register("pc")
        if pc not in self._written or pc in self._flash or not self.target.read32(DFSR) & DFSR_BKPT:
            return False
        self._hit_dormant(pc)
        self.target.resume()
        self._watching = True
        return True

    def _hit_dormant(self, pc: int) -> bool:
        """休眠中の BKPT の上の pc を越える。dormant_limit 回目はそのセクタの休眠中のものを戻す（戻せば元の命令から
        そのまま走れる）。それまでは元の命令を Flash の外で 1 つ実行する。戻り値: 命令を実行した（戻したなら False）。"""
        self.stats["dormant_hits"] += 1
        self._hits[pc] = self._hits.get(pc, 0) + 1
        if self._hits[pc] >= self.dormant_limit:
            sector = self._memory.sector_at(pc)
            self._write(restore=[a for a in self.dormant if sector.address <= a < sector.end])
            return False
        self._step_original(pc)
        return True

    def _step_original(self, pc: int) -> None:
        """pc の BKPT の下の元の命令を 1 つ実行する。Flash の外でできなければ、書き戻して 1 命令進める。"""
        scratch = self.scratch
        try:
            if scratch is None:
                raise NeedsRewrite("命令を実行する RAM が分からない")
            how = OutOfLine(self.target, scratch, read=self._read).step(pc, self._read(pc, 4))
            self.stats[how] += 1
        except NeedsRewrite as e:
            self.last_rewrite = f"0x{pc:08X}: {e}"
            self.stats["rewrites"] += 1
            self._write(restore=[pc])                          # 置いたままなら次の再開で書き直す
            self.target.step(disable_interrupts=True)

    # --- Flash に書く --------------------------------------------------------------------------------

    def commit(self) -> None:
        """溜まった変更を Flash に書く（再開の直前に呼ばれる）。比較器が空いていれば書かずにハードへ、踏み台には
        利用者のものを移して比較器を譲る。書くセクタにある休眠中の BKPT はついでに戻す。"""
        pending = sorted((a for a in self._flash if a not in self._written), key=lambda a: (self.persistent(a), a))
        for address in pending:
            if self.target.set_breakpoint(address, Target.BreakpointType.HW):
                self._flash.discard(address)
                self._hw.add(address)
            elif not self.persistent(address):
                victim = self._victim(pending)
                if victim is not None:
                    self.target.remove_breakpoint(victim)
                    self._hw.discard(victim)
                    self._flash.add(victim)
                    if self.target.set_breakpoint(address, Target.BreakpointType.HW):
                        self._flash.discard(address)
                        self._hw.add(address)
        inserts = [a for a in self._flash if a not in self._written]
        if inserts:
            sectors = {self._memory.sector_at(a) for a in inserts}
            restore = [a for a in self.dormant if any(s.address <= a < s.end for s in sectors)]
            self._write(insert=inserts, restore=restore)

    def _victim(self, pending: list[int]) -> int | None:
        """比較器を譲る利用者のブレークポイント（どうせ書くセクタにあるものを優先）。"""
        candidates = sorted(a for a in self._hw if self.persistent(a))
        if not candidates:
            return None
        sectors = {self._memory.sector_at(a) for a in pending if self.persistent(a)}
        return next((a for a in candidates if self._memory.sector_at(a) in sectors), candidates[0])

    def _write(self, insert=(), restore=()) -> None:
        """BKPT を書く・元に戻す（1 回の patch）。記録を先に書き、Flash の後に書き直す。戻すのは、いまも BKPT が
        書いてある番地だけ（ほかの誰かが書き換えていれば触らない）。"""
        insert, restore = sorted(set(insert)), sorted(set(restore))
        added = {}
        for address in insert:
            original = bytes(self.target.read_memory_block8(address, len(BKPT_CODE)))
            window = address & ~(WINDOW - 1)
            added[address] = _Written(address, original, window, self._read(window, WINDOW))
        changes = [(a, BKPT_CODE) for a in insert]
        for address in restore:
            if bytes(self.target.read_memory_block8(address, len(BKPT_CODE))) == BKPT_CODE:
                changes.append((address, self._written[address].original))
        if self.journal is not None:
            self.journal.save([*self._written.values(), *added.values()])
        if changes:
            self._memory.patch(changes, preserve=True)
            invalidate_icache(self.target)
            self.stats["commits"] += 1
            self.stats["sector_writes"] += len({self._memory.sector_at(a) for a, _ in changes})
        self._written.update(added)
        for address in restore:
            del self._written[address]
            self._hits.pop(address, None)
            self.stats["restored"] += 1
        if self.journal is not None:
            self.journal.save(self._written.values())

    # --- 読む（BKPT を元の中身に見せる） ----------------------------------------------------------------

    def _read(self, address: int, length: int) -> bytes:
        data = bytes(self.target.read_memory_block8(address, length))
        if not self._written:
            return data
        out = bytearray(data)
        for w in self._written.values():
            for i, b in enumerate(w.original):
                if address <= w.address + i < address + length:
                    out[w.address + i - address] = b
        return bytes(out)

    def _overlaps(self, address: int, length: int) -> bool:
        return any(address < a + len(BKPT_CODE) and a < address + length for a in self._written)

    def read_memory_block8(self, address: int, length: int):
        if not self._overlaps(address, length):
            return self.target.read_memory_block8(address, length)
        return list(self._read(address, length))

    def read_memory_block32(self, address: int, count: int):
        if not self._overlaps(address, 4 * count):
            return self.target.read_memory_block32(address, count)
        data = self._read(address, 4 * count)
        return [int.from_bytes(data[i:i + 4], "little") for i in range(0, len(data), 4)]

    def read_memory(self, address: int, transfer_size: int = 32, now: bool = True):
        if not self._overlaps(address, transfer_size // 8):
            return self.target.read_memory(address, transfer_size, now)
        value = int.from_bytes(self._read(address, transfer_size // 8), "little")
        return value if now else (lambda: value)

    def read32(self, address: int, now: bool = True):
        return self.read_memory(address, 32, now) if self._overlaps(address, 4) else self.target.read32(address)

    def read16(self, address: int, now: bool = True):
        return self.read_memory(address, 16, now)

    def read8(self, address: int, now: bool = True):
        return self.read_memory(address, 8, now)

    # --- Flash の書き込み・消去との関係 ----------------------------------------------------------------

    def fold(self, image: Image) -> Image:
        """Flash に image を書く前に: image が書くセクタにある BKPT のうち、image が書かないバイトを元の中身にして
        足した image（セクタを消して書き直すとき、残す部分を Flash から読むと BKPT も残るため）。"""
        sectors = self._sectors_of(image)
        covered = lambda x: any(s.address <= x < s.end for s in image.segments)  # noqa: E731
        extra = [Segment(w.address + i, bytes([b])) for w in self._written.values()
                 if any(s.address <= w.address < s.end for s in sectors)
                 for i, b in enumerate(w.original) if not covered(w.address + i)]
        if not extra:
            return image
        return Image([*image.segments, *extra], image.format, image.source)

    def forget(self, image: Image) -> None:
        """image を書いた後に: そのセクタの BKPT の記録を捨てる（いまは元の中身か新しい中身）。置いたものは残り、
        次の再開で書き直す。"""
        sectors = self._sectors_of(image)
        self._drop(lambda a: any(s.address <= a < s.end for s in sectors))

    def forget_range(self, address: int, length: int) -> None:
        """[address, address + length) にかかるセクタを消した後に。"""
        sectors = self._memory.sectors(address, length)
        self._drop(lambda a: any(s.address <= a < s.end for s in sectors))

    def forget_all(self) -> None:
        """Flash をすべて消した後に。"""
        self._drop(lambda a: True)

    def _sectors_of(self, image: Image):
        return [b for s in image.segments if self._in_flash(s.address) for b in self._memory.sectors(s.address, len(s.data))]

    def _drop(self, matches) -> None:
        for address in [a for a in self._written if matches(a)]:
            del self._written[address]
            self._hits.pop(address, None)
        if self.journal is not None:
            self.journal.save(self._written.values())

    # --- 元に戻す -------------------------------------------------------------------------------------

    def restore_all(self) -> int:
        """書いた BKPT をすべて元に戻す（置いたものは残り、次の再開で書き直す）。戻した数。"""
        restore = list(self._written)
        if restore:
            self._write(restore=restore)
        return len(restore)

    def close(self) -> None:
        """切断の前に: Flash を元に戻し、比較器を外し、記録を消す。"""
        atexit.unregister(self._exit_hook)
        self.restore_all()
        for address in list(self._hw):
            self.target.remove_breakpoint(address)
        self._hw.clear()
        self._flash.clear()
        if self.journal is not None:
            self.journal.clear()

    def _close_quietly(self) -> None:
        try:
            self.close()
        except Exception as e:                                 # 戻せなければ記録が残り、次の接続で戻す
            LOG.warning("Flash のブレークポイントを戻せなかった（次の接続で戻す）: %s", e)

    def recover(self) -> dict:
        """前のプロセスが残した BKPT（記録）を戻す。周りの中身が記録と違う（別のファームウェアを書いた）番地は触らない。
        {"restored": [番地], "skipped": [番地]}。"""
        entries = self.journal.load() if self.journal is not None else None
        if entries is None:
            return {"restored": [], "skipped": []}
        bkpts = {w.address + i: b for w in entries for i, b in enumerate(BKPT_CODE)}
        changes, restored, skipped = [], [], []
        for window in sorted({w.window for w in entries}):
            members = [w for w in entries if w.window == window]
            current = bytes(self.target.read_memory_block8(window, WINDOW))
            original = members[0].window_original
            intact = all(c == o or bkpts.get(window + i) == c for i, (c, o) in enumerate(zip(current, original)))
            for w in members:
                at = w.address - window
                if intact and current[at:at + len(BKPT_CODE)] == BKPT_CODE:
                    changes.append((w.address, w.original))
                    restored.append(w.address)
                elif not intact:
                    skipped.append(w.address)
        if changes:
            self._memory.patch(changes, preserve=True)
            invalidate_icache(self.target)
        self.journal.clear()
        return {"restored": sorted(restored), "skipped": sorted(skipped)}
