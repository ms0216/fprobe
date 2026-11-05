"""GDB の接続の、ターゲットの状態を読み書きするパケット（スレッド・レジスタ・メモリ・ブレークポイント・実行と停止の待ち）の部品。

GdbSession（fprobe.gdb.session）に mixin される。接続・パケットの振り分け・attach は本体が持つ。
"""

import asyncio

from . import rsp
from .description import regnum

PACKET_SIZE = 0x4000                  # GDB に申告するパケットの最大（m の応答は 16 進で 2 倍になるので 8 KB まで読む）
SIGINT, SIGTRAP, SIGSEGV = 2, 5, 11
SINGLE = 1                            # RTOS のスレッドが無いときの唯一のスレッドの ID
_BREAK_KINDS = {"0": "break", "1": "hbreak", "2": "write", "3": "read", "4": "access"}
_WATCH_STOPS = {"write": "watch", "read": "rwatch", "access": "awatch"}


class _Closed(Exception):
    """接続が切れた（実行の待ちの途中）。"""


class _TargetMixin:
    """スレッド・レジスタ・メモリ・実行。"""

    async def _thread_list(self) -> list[dict]:
        await self._attach()
        if self._threads is None:
            threads: list[dict] = []
            self._thread_note = None
            if self._threads_on:
                listing = await self.host.run(self.probe.gdb_threads, control=False)
                threads, self._thread_note = listing["threads"], listing["note"]
            if threads and not any(t["current"] for t in threads):
                # 実行中のタスクが無い（スケジューラの前等）: 先頭のタスクを「いま」とせず、コアそのもののスレッドを足す
                threads = [*threads, {"id": SINGLE, "name": "core", "state": "running", "current": True,
                                      "no_task": True}]
            self._threads = threads or [{"id": SINGLE, "name": "main", "state": "running", "current": True}]
        return self._threads

    @staticmethod
    def _tid(text: str) -> int | None:
        """スレッド ID の指定（-1 = すべて、0 = どれでも）→ 番号。いまのスレッドの意味なら None。"""
        tid = int(text, 16) if text and text != "-1" else 0
        return tid or None

    async def _find_thread(self, tid: int | None) -> dict:
        threads = await self._thread_list()
        for t in threads:
            if t["id"] == tid or (tid is None and t["current"]):
                return t
        raise KeyError(f"スレッド {tid:x} は無い" if tid is not None else "いまのスレッドが無い")

    async def _current_thread(self) -> int:
        threads = await self._thread_list()
        return next(t["id"] for t in threads if t["current"])           # _thread_list がいまのスレッドを必ず含める

    async def _context(self) -> int | None:
        """Hg で選んだスレッドが実行中のタスクでなければ、その TCB の番地（保存された文脈で読む）。"""
        if self._g_thread is None:
            return None
        thread = await self._find_thread(self._g_thread)
        return None if thread["current"] or thread["id"] == SINGLE else thread["id"]

    @staticmethod
    def _extra(thread: dict) -> str:
        if thread.get("no_task"):
            return "no current task"
        if thread["id"] == SINGLE and "priority" not in thread:
            return "no RTOS"
        return f"{thread['state']}, priority {thread['priority']}"

    # --- レジスタ・メモリ ---------------------------------------------------------------------------

    async def _read_all_registers(self) -> str:
        await self._attach()
        if self._tframe is not None:
            values = await self._frame_registers(range(len(self._regs)))
            return "".join(rsp.le_hex(v, r.bits) for v, r in zip(values, self._regs))
        context = await self._context()
        names = [r.name for r in self._regs]
        values = await self.host.run(lambda: self.probe.gdb_read_registers(names, context), control=False)
        return "".join(rsp.le_hex(v, r.bits) for v, r in zip(values, self._regs))

    async def _write_all_registers(self, data: str) -> str:
        await self._attach()
        values, at = {}, 0
        for r in self._regs:
            chunk = data[at:at + r.bits // 4]
            at += r.bits // 4
            if len(chunk) == r.bits // 4 and "x" not in chunk:
                values[r.name] = rsp.from_le_hex(chunk)
        return await self._write_registers(values)

    async def _read_register(self, n: int) -> str:
        await self._attach()
        if n >= len(self._regs):
            raise IndexError(f"レジスタ {n} は無い")
        r = self._regs[n]
        if self._tframe is not None:
            value, = await self._frame_registers([n])
            return rsp.le_hex(value, r.bits)
        context = await self._context()
        value, = await self.host.run(lambda: self.probe.gdb_read_registers([r.name], context), control=False)
        return rsp.le_hex(value, r.bits)

    async def _write_register(self, n: int, value: str) -> str:
        await self._attach()
        if n >= len(self._regs):
            raise IndexError(f"レジスタ {n} は無い")
        return await self._write_registers({self._regs[n].name: rsp.from_le_hex(value)})

    async def _write_registers(self, values: dict[str, int]) -> str:
        self._live("レジスタを書く")
        context = await self._context()
        await self.host.run(lambda: self.probe.gdb_write_registers(values, context), control=True,
                            record=("gdb.registers.write", {"values": values}))
        self._threads = None
        return "OK"

    async def _read_memory(self, address: int, length: int) -> str:
        await self._attach()
        length = min(length, PACKET_SIZE // 2)                   # 短く返してよい（GDB が続きを読む）
        if self._tframe is not None:
            return (await self._frame_memory(address, length)).hex()
        data = await self.host.run(lambda: self.probe.gdb_read_memory(address, length), control=False)
        return data.hex()

    async def _write_memory(self, payload: bytes) -> str:
        await self._attach()
        header, _, body = payload[1:].partition(b":")
        address, length = (int(x, 16) for x in header.decode("latin-1").split(","))
        data = bytes.fromhex(body.decode("latin-1")) if payload[:1] == b"M" else body
        if len(data) != length:
            raise ValueError(f"長さ {length} とデータ {len(data)} バイトが合わない")
        self._live("メモリを書く")
        if length:
            await self.host.run(lambda: self.probe.gdb_write_memory(address, data), control=True,
                                record=("gdb.memory.write", {"address": address, "length": length}))
            self._threads = None
        return "OK"

    async def _breakpoint(self, text: str) -> str:
        await self._attach()
        kind_code, address, length = text[1:].split(";")[0].split(",")
        kind = _BREAK_KINDS.get(kind_code)
        if kind is None:
            return ""
        insert = text[0] == "Z"
        address_i, length_i = int(address, 16), int(length, 16)
        # GDB は止まるたびにブレークポイントを全部出し入れする: 記録には残さない（debug.breakpoint.list で見える）
        await self.host.run(lambda: self.probe.gdb_breakpoint(self.owner, insert, kind, address_i, length_i),
                            control=True)
        return "OK"

    # --- 実行 ------------------------------------------------------------------------------------

    async def _resume(self, step: bool, address: int | None) -> str:
        await self._attach()
        self._live("再開する")
        if address is not None:
            await self._write_registers({"pc": address})
        self._threads = None
        if step:
            stop = await self.host.run(self.probe.gdb_step, control=True, record=("gdb.step", {}), moves=True)
            return self._stop_reply(stop)
        await self.host.run(self.probe.gdb_resume, control=True, record=("gdb.continue", {}), moves=True)
        return await self._wait_stop()

    async def _wait_stop(self) -> str:
        """実行中: ^C を待ちつつ周期的に止まったかを見る。止まったら停止の応答。"""
        while True:
            try:
                event = await asyncio.wait_for(self._events.get(), self.options.poll_s)
            except TimeoutError:
                event = False
            if event is None:
                raise _Closed()                                   # 切断（ターゲットは動いたまま）
            if event is rsp.INTERRUPT:
                await self.host.run(self.probe.gdb_halt, control=True, record=("gdb.interrupt", {}), moves=True)
            elif event is rsp.NAK:
                await self._write(self._last)
            elif isinstance(event, rsp.Packet) and self._ack:
                await self._write(b"+")                           # all-stop の実行中のパケットは受けるだけ
            out = await self.host.run(lambda: self.probe.gdb_poll(self._console), control=False)
            if out["console"]:
                await self.console(out["console"])
            self._console = out["console_next"]
            stop = out["stop"]
            if stop is not None:
                if "fault" in stop:
                    fault = stop["fault"]
                    at = f" at 0x{fault['at']:08x}" if fault.get("at") is not None else ""
                    await self.console(f"fprobe: fault {', '.join(fault['causes'])}{at} (monitor target.fault)\n")
                return self._stop_reply(stop)

    def _stop_reply(self, stop: dict) -> str:
        self._stop = stop
        self._threads = None
        self._g_thread = None
        if "exit" in stop:
            return f"W{stop['exit'] & 0xFF:02x}"
        reason = stop["reason"]
        signal = SIGSEGV if "fault" in stop else SIGINT if reason in ("user", "external") else SIGTRAP
        parts = [f"T{signal:02x}",
                 f"{regnum(self._regs, 'pc'):02x}:{rsp.le_hex(stop['pc'], 32)};",
                 f"{regnum(self._regs, 'sp'):02x}:{rsp.le_hex(stop['sp'], 32)};"]
        thread = stop.get("thread") if self._threads_on else None
        parts.append(f"thread:{thread or SINGLE:x};")
        watch = stop.get("watch")
        if watch is not None:
            parts.append(f"{_WATCH_STOPS[watch['kind']]}:{watch['address']:x};")
        elif stop.get("breakpoint") == "sw" and "swbreak" in self._features:
            parts.append("swbreak:;")
        elif stop.get("breakpoint") == "hw" and "hwbreak" in self._features:
            parts.append("hwbreak:;")
        return "".join(parts)
