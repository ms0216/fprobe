"""GDB の接続の、トレースポイント（QT… / qT…）のパケットと、選んだトレースのフレームからの読み。

GdbSession（fprobe.gdb.session）に mixin される。実験の本体は Probe が持つ（fprobe.gdb.trace）。
"""

from .description import regnum


class _TraceMixin:
    """トレースポイントとフレーム。"""

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
                await self.host.run(lambda: self.probe.gdb_trace_start(self.owner, registers, self._trace_deny_halt),
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
