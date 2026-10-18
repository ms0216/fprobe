"""機器なしで画面を試すデモ（fake_daemon.py --demo）の一部: Terminal の源（RTT 0・1、SWO の ITM 0・1、UART、
セミホスティング）と、その行の時刻・SWO の開始・UART のボーレート（G04）。

DemoProbe（fake_demo.py）に混ぜる mixin。値の形は本物の Probe（probe_terminal.py・probe_observe.py・mcp_server.py）と同じ。

- 源ごとに増えるだけのバイト列（位置 since で読める）。読むたびに、経った時間の分の行を足す。書けば返事が来る。
- 受け取りごとの時刻（terminal_read の times）: 壁時計の秒と、記録の中の行ならタイムベースの時刻 t。UART の最初の行は
  デモの記録（tests/demo_recording.py）の UART の送信（TX）の行そのもので、t は記録の時刻（画面の行から時間軸のその
  UART の解読の箱へ飛べる）。デモの記録は「デモを始める直前の 2 秒」とみなし、壁時計もそれにそろえる。その後に流れる
  行は記録の外（t = null。本物でも記録していなければ時間軸の時刻は無い）。
- SWO は swo_start（または swo_configure の swo_baud）の後だけ源に出る（本物と同じ）。
"""

import threading
import time

__all__ = ["DemoStreams", "IoDemo"]


class IoDemo:
    def _init_io(self) -> None:
        """DemoProbe.__init__ から（ELF を読み、接続・停止した後）。"""
        self._streams = DemoStreams(lambda: self.counter)
        self._uart_baud = 115_200

    def open_demo_recording(self, directory) -> None:
        """デモの記録を開いたら、その UART の送信の行を UART の源の最初の行にする（時刻つき）。"""
        super().open_demo_recording(directory)
        view = self._elf._view
        tx = view.events(None, None, ["uart"], ["tx"], 1_000_000)
        self._streams.seed("uart", [(e.t, e.b) for e in tx], view.meta["timebase_hz"], view.span()[1] - view.span()[0])

    # --- RTT・UART・SWO --------------------------------------------------------------------------

    def rtt_start(self, **kwargs):
        return {"running": True, "address": 0x20000100, "up": 2, "down": 1}

    def rtt_status(self):
        return {"running": True, "address": 0x20000100,
                "up": [{"index": 0, "name": "Terminal", "received": self._streams.size("rtt0")},
                       {"index": 1, "name": "Log", "received": self._streams.size("rtt1")}],
                "down": [{"index": 0, "name": "Terminal"}],
                "semihosting": {"received": self._streams.size("semihosting")}}

    def rtt_read(self, channel=0, since=None, max_bytes=65536, encoding="text", timeout_s=0.0):
        source = "semihosting" if channel == "semihosting" else f"rtt{channel}"
        out = self._streams.read(source, since or 0, max_bytes)
        return {"channel": channel, "data": out["data"], "start": out["start"], "next": out["next"], "lost": 0}

    def rtt_write(self, channel=0, text=None, data_hex=None, timeout_s=1.0):
        data = text if text is not None else bytes.fromhex(data_hex or "").decode(errors="replace")
        self._streams.echo("semihosting" if channel == "semihosting" else f"rtt{channel}", data)
        return {"written": len(data.encode())}

    def uart_write(self, data, baud=None):
        if baud:
            self._uart_baud = baud
        self._streams.echo("uart", data)
        return len(data)

    def uart_read(self, timeout_s=0.5, baud=None):
        if baud:
            self._uart_baud = baud
        return self._streams.read("uart", 0, 1 << 20)["data"]

    def uart_configure(self, baud):
        self._uart_baud = baud
        return {"baud": baud}

    def swo_start(self, baud=2_000_000):
        self._need()
        self._streams.start_swo()

    def swo_stop(self):
        self._streams.stop_swo()

    def swo_configure(self, ports=None, timestamps=False, timestamp_prescale=1, exception_trace=False, counters=None,
                      data_trace=None, value_watch=None, swo_baud=None, core_clock_hz=None, trace_pin=True):
        """ITM のポートを有効にして（swo_baud なら）SWO を受け始める。結果の形は本物（probe_swo.py）と同じ。"""
        self._need()
        if swo_baud:
            self._streams.start_swo()
        comparators = [{"index": i, "expression": d["expression"], "access": d.get("access", "write"),
                        "address": self._elf._symbols.variable(d["expression"]).address, "size": 4,
                        "emits": [k for k in ("pc", "value", "offset") if d.get(k, k != "offset")]}
                       for i, d in enumerate(data_trace or [])]
        self._swo_request = {"exception_trace": exception_trace, "data_trace": comparators, "running": True}
        writes = [{"address": 0xE0000E00, "value": sum(1 << p for p in ports or []), "mask": 0xFFFFFFFF,
                   "note": "ITM_TER（ポートを有効に）"}]
        return {"running": True, "core": {"name": "Cortex-M4", "arch": "ARMv7E-M", "cpuid": 0x410FC241},
                "plan": {"comparators": comparators, "warnings": [], "bytes_per_access": {}, "swo_bytes_per_s_max": 0,
                         "writes": writes},
                "swo_baud": swo_baud, "synced": True, "trace_pin": trace_pin, "t0": None, "notes": []}

    def swo_trace(self, offset=0, limit=500, name=None):
        """swo_configure の結果。データトレースのアクセスは、デモの記録の変数のサンプル（counter）の時刻と値から作る
        （時刻はタイムベース = 時間軸の記録と同じ。PC は tick() の counter++ の行）。例外の回数は記録の割り込みから。"""
        req = getattr(self, "_swo_request", None)
        if req is None:
            raise RuntimeError("swo_configure で設定していない")
        view = self._elf._view
        accesses = []
        if req["data_trace"] and view is not None:
            pc = self._line_address(17)
            place = self._elf._place_of(pc)
            for c in req["data_trace"]:
                for e in view.events(None, None, ["sample"], ["word"], 1_000_000):
                    if e.a != 0x20000010:
                        continue
                    accesses.append({"t": e.t, "time_us": e.t / 10, "name": c["expression"], "role": "value",
                                     "comparator": c["index"], "write": True, "address": c["address"], "size": 4,
                                     "raw": e.b, "value": e.b, "complete": True, "type": "uint32_t", "pc": pc, **place})
        accesses = [a for a in accesses if name is None or a["name"] == name]
        counts: dict[str, int] = {}
        if req["exception_trace"] and view is not None:
            for e in view.events(None, None, ["rtos"], ["isr"], 1_000_000):
                n = (e.body or {}).get("name", f"IRQ{e.a - 16}")
                counts[n] = counts.get(n, 0) + 1
        return {"running": req["running"], "core": "Cortex-M4", "total": len(accesses),
                "accesses": accesses[offset:offset + limit], "counters": {},
                "exceptions": {"counts": counts, "events": sum(counts.values())}, "lost": 0, "outside": 0, "unknown": 0,
                "overflows": 0, "decode_errors": 0, "dropped": 0, "comparators": req["data_trace"], "warnings": []}

    def swo_unconfigure(self):
        out = self.swo_trace()
        self._swo_request["running"] = False
        return {**out, "running": False}

    # --- 端末の口 ----------------------------------------------------------------------------------

    def terminal_sources(self):
        sources = self._streams.sources()
        for s in sources:
            if s["id"] == "uart":
                s["baud"] = self._uart_baud
        return {"sources": sources, "rtt": True, "swo": self._streams.swo_on}

    def terminal_read(self, source, since=0, max_bytes=65536, timeout_s=0.0, times=False):
        return self._streams.read(source, since, max_bytes, times)


class DemoStreams:
    """端末の源のデモ: 源ごとに増えるだけのバイト列（位置 since で読める）と受け取りの時刻の印。読むたびに、経った
    時間の分の行を足す。"""

    SOURCES = [("rtt0", "rtt", "Terminal", True), ("rtt1", "rtt", "Log", False), ("swo0", "swo", "ITM 0", False),
               ("swo1", "swo", "ITM 1", False), ("uart", "uart", "UART", True),
               ("semihosting", "semihosting", "Semihosting", True)]
    PERIOD_S = {"rtt0": 0.5, "rtt1": 1.5, "swo0": 0.7, "swo1": 2.0, "uart": 1.0, "semihosting": 4.0}
    SWO = ("swo0", "swo1")

    def __init__(self, counter):
        self._counter = counter
        self._started = time.monotonic()
        self._wall0 = time.time()
        self._lock = threading.Lock()
        self._data = {
            "rtt0": bytearray(b"SEGGER RTT demo (fprobe fake)\r\nboot: clock 84 MHz, RAM image\r\n"),
            "rtt1": bytearray(b"\x1b[32mI\x1b[0m log: level=info\r\n"),
            "swo0": bytearray(b""),
            "swo1": bytearray(b""),
            "uart": bytearray(b"\r\nU-Boot-like demo loader\r\n> "),
            "semihosting": bytearray(b"semihosting: hello from the target\n"),
        }
        # 源ごとの受け取りの印 (位置, 壁時計の秒, タイムベースの時刻 | None)
        self._marks: dict[str, list[tuple[int, float, int | None]]] = {
            s: ([(0, self._wall0, None)] if data else []) for s, data in self._data.items()}
        self._steps = dict.fromkeys(self._data, 0)
        self.swo_on = False
        self._swo_started = 0.0
        self._hz: float | None = None

    def seed(self, source: str, items: list[tuple[int, int]], hz: float, span: int) -> None:
        """記録のバイト（時刻 t・値）を源の最初の行にする。行の始まりごとに印（t と、記録をデモの直前の span とみなした
        壁時計）。"""
        with self._lock:
            data, marks = bytearray(), []
            line_start = True
            for t, byte in items:
                if line_start:
                    marks.append((len(data), self._wall0 - (span - t) / hz, t))
                data.append(byte)
                line_start = byte == 0x0A
            rest = len(data)
            marks += [(rest + pos, wall, t) for pos, wall, t in self._marks[source]]
            self._data[source] = data + self._data[source]
            self._marks[source] = marks
            self._hz = hz

    def start_swo(self) -> None:
        with self._lock:
            if not self.swo_on:
                self.swo_on, self._swo_started = True, time.monotonic()
                for s in self.SWO:
                    self._steps[s] = 0
                self._add("swo0", b"ITM printf on port 0\n", time.time())

    def stop_swo(self) -> None:
        with self._lock:
            self.swo_on = False

    def _line(self, source: str, k: int) -> str:
        t = k * self.PERIOD_S[source]
        c = self._counter()
        return {"rtt0": f"[{t:7.1f}] tick counter={c} sensor={(k * 37) % 1000}\r\n",
                "rtt1": f"\x1b[33mW\x1b[0m [{t:7.1f}] queue depth {k % 7}/8\r\n",
                "swo0": f"itm0: t={t:.1f}s adc={(k * 211) % 4096}\n",
                "swo1": f"itm1: task switch #{k}\n",
                "uart": f"status {k}: ok\r\n> ",
                "semihosting": f"semihosting: checkpoint {k}\n"}[source]

    def _add(self, source: str, data: bytes, wall: float, t: int | None = None) -> None:
        if data:
            self._marks[source].append((len(self._data[source]), wall, t))
            self._data[source] += data

    def _feed(self) -> None:
        now = time.monotonic()
        for source, period in self.PERIOD_S.items():
            swo = source in self.SWO
            if swo and not self.swo_on:
                continue
            origin = self._swo_started if swo else self._started
            due = int((now - origin) / period)
            while self._steps[source] < due:
                self._steps[source] += 1
                k = self._steps[source]
                wall = self._wall0 + (origin - self._started) + k * period
                self._add(source, self._line(source, k).encode(), wall)

    def size(self, source: str) -> int:
        with self._lock:
            self._feed()
            return len(self._data[source])

    def read(self, source: str, since: int, max_bytes: int, times: bool = False) -> dict:
        if source not in self._data:
            raise ValueError(f"源 {source!r} は無い")
        with self._lock:
            self._feed()
            data = self._data[source]
            start = min(since, len(data))
            part = bytes(data[start:start + max_bytes])
            marks = self._marks[source]
            before = [m for m in marks if m[0] <= start]
            inside = [m for m in marks if start < m[0] < start + len(part)]
        out = {"source": source, "data": part.decode(errors="replace"), "start": start, "next": start + len(part),
               "lost": 0}
        if times:
            picked = ([(start, *before[-1][1:])] if before else []) + inside
            out["times"] = [{"at": len(part[:pos - start].decode(errors="replace")), "wall": round(wall, 6), "t": t}
                            for pos, wall, t in picked]
            out["timebase_hz"] = self._hz
        return out

    def echo(self, source: str, text: str) -> None:
        reply = {"uart": f"{text.rstrip()}\r\nok\r\n> ", "semihosting": f"stdin: {text}"}.get(source, f">> {text}")
        with self._lock:
            self._feed()
            self._add(source, reply.encode(), time.time())

    def sources(self) -> list[dict]:
        return [{"id": i, "kind": k, "name": n, "writable": w, "received": self.size(i),
                 **({"channel": int(i[3:])} if k == "rtt" else {"port": int(i[3:])} if k == "swo" else {})}
                for i, k, n, w in self.SOURCES if k != "swo" or self.swo_on]
