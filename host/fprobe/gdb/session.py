"""1 つの GDB の接続の RSP の処理（all-stop）。

機器には直接触らない: 操作はすべて `GdbHost.run(fn, control=…)` に渡す関数（Probe の GdbTools を呼ぶ）で行い、
デーモンではそれが操作の調停（1 本の作業スレッド・人の一時停止）と記録を通る。API のメソッド（monitor）は
`GdbHost.call` で、他の利用者と同じ検証・調停・記録を通る。

トレースポイント（QT… / qT…）の実験は Probe が持つ（GDB がつなぎ直しても続く）。フレームを選んでいる間
（QTFrame）は g・p・m をそのフレームの集めた値で答える。

仕様: docs/superpowers/specs/2026-10-18-u04-gdb-server-design.md
"""

import asyncio
import json
import shlex
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from . import rsp
from .description import Region, fill_gaps, layout, memory_map_xml, regnum, target_xml, threads_xml

__all__ = ["GdbHost", "GdbSession", "Options", "PACKET_SIZE"]

PACKET_SIZE = 0x4000                  # GDB に申告するパケットの最大（m の応答は 16 進で 2 倍になるので 8 KB まで読む）
SIGINT, SIGTRAP, SIGSEGV = 2, 5, 11
SINGLE = 1                            # RTOS のスレッドが無いときの唯一のスレッドの ID
_BREAK_KINDS = {"0": "break", "1": "hbreak", "2": "write", "3": "read", "4": "access"}
_WATCH_STOPS = {"write": "watch", "read": "rwatch", "access": "awatch"}
HELP = """fprobe の GDB サーバ（monitor のコマンド）
  monitor help                      この案内
  monitor reset [halt|init|run]     ターゲットのリセット（省略は run。halt / init は止める）
  monitor elf <path>                ELF を fprobe に読ませる（RTOS のタスクが GDB のスレッドになる・ソースの言葉の API）
  monitor threads [on|off]          RTOS のタスクを GDB のスレッドとして見せるか
  monitor pause | resume            人の一時停止（AI 等の操作を保留する）/ 再開
  monitor trace [allow-halt|deny-halt]
                                    トレースポイントの実験の状態。止めると壊れうるターゲットで当たるたびに
                                    止めるのを許す / 許さない（既定は許さない。この接続だけ）
  monitor status                    デーモンの状態（利用者・一時停止・機器）
  monitor <API のメソッド> [JSON | key=value ...]
                                    fprobe の API を呼ぶ（例: monitor la.capture {"mask": 1, "value": 1}、
                                    monitor recording.start name=crash、monitor rtos.tasks）
"""


class GdbHost(Protocol):
    """GDB の接続が機器と API を使う窓口（デーモンが利用者ごとに作る）。"""

    async def run(self, fn: Callable[[], Any], *, control: bool, record: tuple[str, dict] | None = None,
                  moves: bool = False) -> Any:
        """fn を機器の作業スレッドで実行する。control: 状態を変える操作か（人の一時停止で保留される）。record:
        操作の記録に残す (名前, 引数)。moves: コアを動かし得る（画面の状態の見張りを起こす）。"""

    async def call(self, method: str, params: dict) -> Any:
        """API のメソッドを呼ぶ（検証・調停・記録つき）。"""


@dataclass
class Options:
    target: str = "cortex_m"          # GDB がつながったときにターゲットが未接続なら、この pyOCD のターゲット名で接続
    frequency_hz: int = 4_000_000
    poll_s: float = 0.02              # 実行中に止まったかを見る周期


class _Closed(Exception):
    """接続が切れた（実行の待ちの途中）。"""


class GdbSession:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, host: GdbHost, probe: Any,
                 owner: str, options: Options | None = None):
        self.reader, self.writer, self.host, self.probe, self.owner = reader, writer, host, probe, owner
        self.options = options or Options()
        self._events: asyncio.Queue = asyncio.Queue()
        self._ack = True
        self._last = b""                          # 最後に送ったパケット（'-' で送り直す）
        self._features: set[str] = set()          # GDB が qSupported で申告した機能
        self._attached = False
        self._regs = []                           # GDB に見せるレジスタの並び
        self._regions: list[Region] = []
        self._threads_on = True
        self._threads: list[dict] | None = None   # 停止ごとのタスクの一覧の写し（None = 読み直す）
        self._thread_note: str | None = None
        self._g_thread: int | None = None         # Hg で選んだスレッド（None = いまのスレッド）
        self._stop: dict | None = None
        self._flash: list[tuple[int, bytearray]] = []
        self._console = 0                         # セミホスティングの出力の読んだ位置
        self._closing = False
        self._tframe: int | None = None           # 見ているトレースのフレーム（None = 実物）
        self._trace_allow_halt = False            # monitor trace allow-halt
        self._uploads: list[str] = []             # qTfP / qTfV の残り

    # --- 接続 -----------------------------------------------------------------------------------

    async def serve(self) -> None:
        pump = asyncio.create_task(self._pump())
        try:
            while not self._closing:
                event = await self._events.get()
                if event is None:
                    break
                await self._on_event(event)
        except (_Closed, ConnectionError):
            pass
        finally:
            pump.cancel()
            if self._attached:
                try:
                    # 自分の置いたものを外す後片付けは、人の一時停止でも保留しない（read）。切断で取り消されないように
                    await self.host.run(lambda: self.probe.gdb_release(self.owner), control=False)
                except Exception:
                    pass                                     # ターゲットが切断された等: 外すものが無い
            self.writer.close()

    async def _pump(self) -> None:
        reader = rsp.PacketReader()
        try:
            while data := await self.reader.read(65536):
                for event in reader.feed(data):
                    self._events.put_nowait(event)
        except ConnectionError:
            pass
        self._events.put_nowait(None)

    async def _on_event(self, event) -> None:
        if event is rsp.NAK:
            await self._write(self._last)
        elif event is rsp.CORRUPT:
            if self._ack:
                await self._write(b"-")
        elif isinstance(event, rsp.Packet):
            if self._ack:
                await self._write(b"+")
            reply = await self._reply(event.payload)
            if reply is not None:
                await self.send(reply)
        # ACK・止まっているときの ^C は何もしない

    async def _write(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def send(self, payload: str | bytes) -> None:
        data = rsp.frame(payload.encode("utf-8") if isinstance(payload, str) else payload)
        self._last = data
        await self._write(data)

    async def console(self, text: str | bytes) -> None:
        """GDB のコンソールに出す（O パケット。実行中と monitor の応答の前だけ）。"""
        data = text.encode("utf-8") if isinstance(text, str) else text
        for i in range(0, len(data), PACKET_SIZE // 2 - 1):
            await self.send("O" + data[i:i + PACKET_SIZE // 2 - 1].hex())

    # --- パケット ---------------------------------------------------------------------------------

    async def _reply(self, payload: bytes) -> str | bytes | None:
        text = payload.decode("latin-1")
        try:
            return await self._dispatch(text, payload)
        except _Closed:
            raise
        except Exception as e:
            return self._error(e)

    def _error(self, e: Exception) -> str:
        if "error-message" in self._features:
            return f"E.{type(e).__name__}: {e}"
        return "E01"

    async def _dispatch(self, text: str, payload: bytes) -> str | bytes | None:
        head = text[:1]
        if text.startswith("qSupported"):
            self._features = {f[:-1] for f in text.partition(":")[2].split(";") if f.endswith("+")}
            return (f"PacketSize={PACKET_SIZE:x};QStartNoAckMode+;qXfer:features:read+;qXfer:memory-map:read+;"
                    "qXfer:threads:read+;vContSupported+;swbreak+;hwbreak+;ConditionalTracepoints+;"
                    "TracepointSource+;EnableDisableTracepoints+;DisconnectedTracing+;InstallInTrace+;tracenz+;"
                    "qXfer:traceframe-info:read+")
        if text == "QStartNoAckMode":
            await self.send("OK")                                 # この OK までは ack がつく
            self._ack = False
            return None
        if text.startswith("qXfer:"):
            return await self._xfer(text)
        if text == "?":
            await self._attach()
            return self._stop_reply(self._stop)
        if text.startswith("qRcmd,"):
            return await self._monitor(bytes.fromhex(text[6:]).decode("utf-8", errors="replace"))
        if text in ("qAttached",) or text.startswith("qAttached:"):
            return "1"
        if text.startswith("qSymbol"):
            return "OK"
        if text == "qC":
            return f"QC{await self._current_thread():x}"
        if text == "qfThreadInfo":
            return "m" + ",".join(f"{t['id']:x}" for t in await self._thread_list())
        if text == "qsThreadInfo":
            return "l"
        if text.startswith("qThreadExtraInfo,"):
            thread = await self._find_thread(int(text.split(",")[1], 16))
            return self._extra(thread).encode("utf-8").hex()
        if head == "H":
            tid = self._tid(text[2:])
            if tid is not None:
                await self._find_thread(tid)
            if text[1] == "g":
                self._g_thread = tid
            return "OK"
        if head == "T":
            await self._find_thread(self._tid(text[1:]))
            return "OK"
        if text == "g":
            return await self._read_all_registers()
        if head == "G":
            return await self._write_all_registers(text[1:])
        if head == "p":
            return await self._read_register(int(text[1:], 16))
        if head == "P":
            n, value = text[1:].split("=")
            return await self._write_register(int(n, 16), value)
        if head == "m":
            address, length = (int(x, 16) for x in text[1:].split(","))
            return await self._read_memory(address, length)
        if head in ("M", "X"):
            return await self._write_memory(payload)
        if head in ("Z", "z"):
            return await self._breakpoint(text)
        if head in ("c", "s"):                                    # c [addr]
            return await self._resume(head == "s", int(text[1:], 16) if text[1:] else None)
        if head in ("C", "S"):                                    # C sig[;addr]（シグナルは渡せないので無視）
            address = text[1:].partition(";")[2]
            return await self._resume(head == "S", int(address, 16) if address else None)
        if text == "vCont?":
            return "vCont;c;C;s;S"
        if text.startswith("vCont;"):
            actions = [a.split(":")[0][:1] for a in text[6:].split(";")]
            return await self._resume(any(a in ("s", "S") for a in actions), None)
        if text.startswith("vFlash"):
            return await self._flash_packet(text, payload)
        if text.startswith(("QT", "qT")):
            return await self._trace_packet(text)
        if head == "D":
            await self._detach(resume=True)
            await self.send("OK")
            self._closing = True
            return None
        if text == "k" or text.startswith("vKill"):
            await self._detach(resume=False)
            self._closing = True
            return "OK" if head == "v" else None
        return ""                                                 # 対応しないパケット（vMustReplyEmpty 等も）

    # --- 接続・切断 ---------------------------------------------------------------------------------

    async def _attach(self) -> None:
        if self._attached:
            return
        opts = self.options
        info = await self.host.run(lambda: self.probe.gdb_attach(opts.target, opts.frequency_hz), control=True,
                                   record=("gdb.attach", {"target": opts.target}), moves=True)
        self._regs = layout(set(info["registers"]))
        self._regions = list(info["regions"])
        self._attached = True
        self._stop = await self.host.run(self.probe.gdb_stop, control=False)
        self._g_thread = None

    async def _detach(self, resume: bool) -> None:
        if not self._attached:
            return
        await self.host.run(lambda: self.probe.gdb_release(self.owner), control=False)
        if resume:
            await self.host.run(self.probe.gdb_resume, control=True, record=("gdb.detach", {}), moves=True)

    # --- 記述（qXfer） -------------------------------------------------------------------------------

    async def _xfer(self, text: str) -> bytes | str:
        _, obj, op, annex, span = text.split(":", 4)
        if op != "read":
            return ""
        offset, length = (int(x, 16) for x in span.split(","))
        await self._attach()
        if obj == "features" and annex == "target.xml":
            data = target_xml(self._regs)
        elif obj == "memory-map":
            data = memory_map_xml(fill_gaps(self._regions))
        elif obj == "traceframe-info":
            exp, frame = await self._selected_frame()
            data = exp.frame_info_xml(frame)
        elif obj == "threads":
            data = threads_xml([{"id": t["id"], "name": t["name"], "extra": self._extra(t)}
                                for t in await self._thread_list()])
        else:
            return "E00"
        blob = data.encode("utf-8")
        chunk = blob[offset:offset + length]
        return (b"l" if offset + length >= len(blob) else b"m") + chunk

    # --- スレッド ---------------------------------------------------------------------------------

    async def _thread_list(self) -> list[dict]:
        await self._attach()
        if self._threads is None:
            threads: list[dict] = []
            self._thread_note = None
            if self._threads_on:
                listing = await self.host.run(self.probe.gdb_threads, control=False)
                threads, self._thread_note = listing["threads"], listing["note"]
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
        return next((t["id"] for t in threads if t["current"]), threads[0]["id"])

    async def _context(self) -> int | None:
        """Hg で選んだスレッドが実行中のタスクでなければ、その TCB の番地（保存された文脈で読む）。"""
        if self._g_thread is None:
            return None
        thread = await self._find_thread(self._g_thread)
        return None if thread["current"] or thread["id"] == SINGLE else thread["id"]

    @staticmethod
    def _extra(thread: dict) -> str:
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

    # --- トレースポイント ---------------------------------------------------------------------------

    def _live(self, what: str) -> None:
        if self._tframe is not None:
            raise RuntimeError(f"トレースのフレームを見ている間は{what}ことができない（tfind none で実物に戻る）")

    async def _experiment(self):
        return await self.host.run(self.probe.gdb_trace_experiment, control=False)

    async def _selected_frame(self):
        exp = await self._experiment()
        frame = exp.frame(self._tframe) if self._tframe is not None else None
        if frame is None:
            raise LookupError("トレースのフレームを選んでいない（QTFrame）")
        return exp, frame

    async def _frame_registers(self, numbers) -> list[int | None]:
        """選んだフレームのレジスタ（集めていなければ None。pc は集めていなければトレースポイントの番地）。"""
        _, frame = await self._selected_frame()
        pc = regnum(self._regs, "pc")
        return [frame.registers.get(n, frame.address if n == pc else None) for n in numbers]

    async def _frame_memory(self, address: int, length: int) -> bytes:
        """選んだフレームの集めたメモリ。無ければ、読むだけの区画（QTro）なら実物を読む。どちらでもなければ誤り
        （GDB は <unavailable> と出す）。"""
        exp, frame = await self._selected_frame()
        data = frame.read(address, length)
        if data is not None:
            return data
        if exp.readonly_covers(address, length):
            return await self.host.run(lambda: self.probe.gdb_read_memory(address, length), control=False)
        raise LookupError(f"0x{address:08x} はこのフレームで集めていない")

    async def _trace_packet(self, text: str) -> str:
        exp = await self._experiment()
        head = text.partition(":")[0]
        if text == "QTinit":
            await self.host.run(lambda: self.probe.gdb_trace_init(self.owner), control=True,
                                record=("gdb.trace.init", {}))
            self._tframe = None
            return "OK"
        if head == "QTDP":
            if exp.define(text) and exp.running:                   # 実験の途中で足した（InstallInTrace）
                await self.host.run(self.probe.gdb_trace_install, control=True)
            return "OK"
        simple = {"QTDPsrc": exp.source, "QTDV": exp.variable, "QTro": exp.set_readonly, "QTBuffer": exp.buffer,
                  "QTNotes": exp.notes, "QTDisconnected": exp.set_disconnected}
        if head in simple:
            simple[head](text)
            return "OK"
        if text == "QTStart":
            await self._attach()
            registers = [(r.name, r.bits) for r in self._regs]
            try:
                await self.host.run(lambda: self.probe.gdb_trace_start(self.owner, registers, self._trace_allow_halt),
                                    control=True, record=("gdb.trace.start", {"tracepoints": len(exp.tracepoints)}))
            except RuntimeError as e:
                await self.console(f"fprobe: {e}\n")
                raise
            self._tframe = None
            return "OK"
        if text == "QTStop":
            await self.host.run(self.probe.gdb_trace_stop, control=True, record=("gdb.trace.stop", {}))
            return "OK"
        if head in ("QTEnable", "QTDisable"):
            exp.set_enabled(text)
            if exp.running:
                await self.host.run(self.probe.gdb_trace_install, control=True)
            return "OK"
        if text == "qTStatus":
            return exp.status_reply()
        if head == "qTP":
            _, number, address = text.split(":")
            return exp.tracepoint_status(int(number, 16), int(address, 16))
        if head == "qTV":
            return exp.variable_reply(int(text.split(":")[1], 16))
        if text in ("qTfP", "qTfV"):
            self._uploads = exp.upload_tracepoints() if text == "qTfP" else exp.upload_variables()
            return self._uploads.pop(0) if self._uploads else "l"
        if text in ("qTsP", "qTsV"):
            return self._uploads.pop(0) if self._uploads else "l"
        if head == "QTFrame":
            self._tframe, reply = exp.find(text, self._tframe)
            self._threads = None
            return reply
        return ""                                                 # qTMinFTPILen・qTBuffer・QTSave・静的なマーカ等

    # --- Flash ----------------------------------------------------------------------------------

    async def _flash_packet(self, text: str, payload: bytes) -> str:
        await self._attach()
        if text.startswith("vFlashErase:"):
            return "OK"                    # 消去は書くときに区画（セクタ）ごと（gdb_flash）。GDB は書く区画だけを消す
        if text.startswith("vFlashWrite:"):
            header, _, data = payload[len("vFlashWrite:"):].partition(b":")
            address = int(header, 16)
            if self._flash and self._flash[-1][0] + len(self._flash[-1][1]) == address:
                self._flash[-1][1].extend(data)
            else:
                self._flash.append((address, bytearray(data)))
            return "OK"
        if text == "vFlashDone":
            blocks, self._flash = [(a, bytes(d)) for a, d in self._flash], []
            if blocks:
                result = await self.host.run(lambda: self.probe.gdb_flash(blocks), control=True, moves=True,
                                             record=("gdb.flash", {"blocks": [[a, len(d)] for a, d in blocks]}))
                await self.console(f"fprobe: wrote {result['bytes']} bytes in {result['seconds']} s\n")
            self._threads = None
            return "OK"
        return ""

    # --- monitor ---------------------------------------------------------------------------------

    async def _monitor(self, text: str) -> str:
        command, _, rest = text.strip().partition(" ")
        rest = rest.strip()
        if command in ("", "help"):
            await self.console(HELP)
            return "OK"
        if command == "reset":
            if rest not in ("", "run", "halt", "init"):
                raise ValueError("reset [halt|init|run]")
            await self._attach()
            halt = rest in ("halt", "init")
            result = await self.host.run(lambda: self.probe.gdb_reset(halt), control=True,
                                         record=("gdb.reset", {"halt": halt}), moves=True)
            self._threads = None
            await self.console(_json(result) + "GDB の写しは古い: maintenance flush register-cache で読み直す\n")
            return "OK"
        if command == "threads":
            if rest in ("on", "off"):
                self._threads_on = rest == "on"
                self._threads = None
            threads = await self._thread_list()
            state = "on" if self._threads_on else "off"
            note = f"（{self._thread_note}）" if self._thread_note else ""
            await self.console(f"threads {state}: {len(threads)} {note}\n")
            return "OK"
        if command == "trace":
            if rest in ("allow-halt", "deny-halt"):
                self._trace_allow_halt = rest == "allow-halt"
            elif rest:
                raise ValueError("trace [allow-halt|deny-halt]")
            summary = await self.host.run(self.probe.gdb_trace_summary, control=False)
            await self.console(_json({"allow_halt": self._trace_allow_halt, **summary}))
            return "OK"
        if command == "elf":
            result = await self.host.call("debug.elf.load", {"path": rest})
        elif command in ("pause", "resume"):
            result = await self.host.call(f"session.control.{command}", {})
        elif command == "status":
            result = await self.host.call("session.status", {})
        else:
            result = await self.host.call(command, _params(rest))
        self._threads = None
        await self.console(_json(result))
        return "OK"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1) + "\n"


def _params(text: str) -> dict:
    """monitor の引数: JSON のオブジェクト、または key=value の並び（値は JSON として読めなければ文字列）。"""
    if not text:
        return {}
    if text.startswith("{"):
        params = json.loads(text)
        if not isinstance(params, dict):
            raise ValueError("引数は JSON のオブジェクト")
        return params
    params = {}
    for word in shlex.split(text):
        key, eq, value = word.partition("=")
        if not eq:
            raise ValueError(f"引数 {word!r} は key=value の形で")
        try:
            params[key] = json.loads(value)
        except ValueError:
            params[key] = value
    return params
