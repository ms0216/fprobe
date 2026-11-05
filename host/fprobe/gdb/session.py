"""1 つの GDB の接続の RSP の処理（all-stop）。

機器には直接触らない: 操作はすべて `GdbHost.run(fn, control=…)` に渡す関数（Probe の GdbService を呼ぶ）で行い、
デーモンではそれが操作の調停（1 本の作業スレッド・人の一時停止）と記録を通る。API のメソッド（monitor）は
`GdbHost.call` で、他の利用者と同じ検証・調停・記録を通る。

トレースポイント（QT… / qT…）の実験は Probe が持つ（GDB がつなぎ直しても続く）。フレームを選んでいる間
（QTFrame）は g・p・m をそのフレームの集めた値で答える。

分け方: ここには接続・パケットの振り分け・attach・記述（qXfer）を置く。スレッド・レジスタ・メモリ・実行は session_target.py、
トレースポイントは session_trace.py、Flash と monitor は session_monitor.py（いずれも GdbSession に混ぜ込む部品）。

仕様: docs/superpowers/specs/2026-10-18-u04-gdb-server-design.md
"""

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from . import rsp
from .description import Region, fill_gaps, layout, memory_map_xml, target_xml, threads_xml
from .session_monitor import HELP, _json, _MonitorMixin, _params  # noqa: F401  (旧名を保つ)
from .session_target import _BREAK_KINDS, _WATCH_STOPS, PACKET_SIZE, SIGINT, SIGSEGV, SIGTRAP, SINGLE, _Closed, _TargetMixin  # noqa: F401
from .session_trace import _TraceMixin

__all__ = ["GdbHost", "GdbSession", "Options", "PACKET_SIZE"]


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


class GdbSession(_TargetMixin, _TraceMixin, _MonitorMixin):
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
        self._trace_deny_halt = False             # monitor trace deny-halt（使う側が明示したときだけ断る）
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
