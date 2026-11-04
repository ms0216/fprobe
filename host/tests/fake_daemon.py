"""偽の機器で fprobe-daemon を起動する（画面の結合試験と、機器なしで画面を試すため）。

python host/tests/fake_daemon.py [--port N] [--ui-dir DIR]
待ち受けたら 1 行目に "PORT <n>" を出す。記録は一時ディレクトリに書く。
"""

import argparse
import asyncio
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fprobe.api import NotConnected  # noqa: E402
from fprobe.api.daemon import UI_DIR, create_app  # noqa: E402
from fprobe.mcp_server import Probe  # noqa: E402
from fprobe.probe_service import expose  # noqa: E402
from fprobe.probe_ui_state import UiStateService  # noqa: E402
from fake_probe_contract import NOT_IN_DEMO, not_in_demo, signature_problems, unsupported_arg  # noqa: E402


@expose("_svc_ui_state", UiStateService)
class FakeProbe:
    """Probe と同じ名前・引数のメソッドを持つ偽物（画面が使うもの）。画面の状態（ui.state）は本物（Probe と同じサービス）。"""

    def __init_subclass__(cls, **kwargs):
        """本物の Probe と同じ名前のメソッドは同じ引数を受ける（tests/fake_probe_contract.py）。ずれていればクラスを作れない。"""
        super().__init_subclass__(**kwargs)
        problems = signature_problems(cls)
        if problems:
            raise TypeError(f"{cls.__qualname__}: " + "; ".join(problems))

    @property
    def state(self) -> str:
        return self._state

    @state.setter
    def state(self, value: str) -> None:
        # 走っている時間を数える（止まっている間は進まない。端末の出力はこの時計で増える）
        now = time.monotonic()
        if getattr(self, "_state", None) == "RUNNING":
            self._run_acc = getattr(self, "_run_acc", 0.0) + (now - self._run_since)
        self._state, self._run_since = value, now

    def running_seconds(self) -> float:
        """これまでにターゲットが走っていた時間（秒。止まっている間は進まない）。"""
        acc = getattr(self, "_run_acc", 0.0)
        return acc + (time.monotonic() - self._run_since if self._state == "RUNNING" else 0.0)

    def __getattr__(self, name):
        # 偽の機器が実装しない API のメソッド: AttributeError ではなく「偽物の不足」と分かる誤りにする
        if name in NOT_IN_DEMO:
            return not_in_demo(name)
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

    def __init__(self):
        self._publish = None
        self._svc_ui_state = UiStateService(self)
        self.connected = False
        self.link: dict = {}                     # つないでいる先（本物の target_status と同じく状態に出す）
        self.state = "RUNNING"
        self.pc = 0x08000100
        self.counter = 0
        self.breakpoints: dict[int, str] = {}
        self.bp_disabled = {}                    # 無効にしたブレークポイント（本物の breakpoint_enable）
        self.watchpoints: dict[int, dict] = {}
        self._elf = Probe("unused")              # ELF・ソースは本物（機器は開かない）

    def attach_publisher(self, publish):
        """デーモンの通知の送り先（ui.state の通知）。"""
        self._publish = publish

    def probe_info(self):
        return {"capabilities": {"board": "fake", "design": "fprobe", "protocol_version": 1, "la_channels": 10,
                                 "la_depth": 8192, "la_sample_hz": 100_000_000, "mem_bytes": 0, "aux": True,
                                 "channels": [0, 1, 2]},
                "time": 0, "target_connected": self.connected}

    def target_voltage(self):
        return {"volts": 3.3, "over_range": False, "live": True, "samples": 100, "samples_before": 90, "window_s": 0.01,
                "errors": 0}

    def target_connect(self, target, frequency_hz, connect_mode="halt", jtag_chain=None,
                       script=None, core=None):
        for arg, value in (("script", script), ("core", core)):
            if value is not None:
                raise unsupported_arg("target_connect", arg, value)
        self.connected, self.state = True, "HALTED"
        self.link = {"target": target, "frequency_hz": frequency_hz, "wire": "jtag" if jtag_chain else "swd"}
        return {"target": target, "frequency_hz": frequency_hz}

    def target_disconnect(self):
        self.connected = False

    def _need(self):
        if not self.connected:
            raise NotConnected("ターゲット未接続（target_connect を先に呼ぶ）")

    def target_status(self):
        self._need()
        if self.state != "HALTED":
            return {"state": self.state, **self.link}
        return {"state": "HALTED", **self.link, "pc": self.pc, **self._elf._place_of(self.pc)}

    # --- デバッグ（ELF は本物、実行は偽物: 再開するとブレークポイントがあれば 0.3 秒後にそこで止まる） ------

    def elf_load(self, path):
        return self._elf.elf_load(path)

    def elf_load_demo(self):
        return self._elf.elf_load_demo()

    def elf_upload(self, name, data_base64):
        return self._elf.elf_upload(name, data_base64)

    def elf_status(self):
        return self._elf.elf_status()

    def halt_risk(self):
        return self._elf.halt_risk()

    def halt_risk_set(self, names=None, patterns=None):
        return self._elf.halt_risk_set(names, patterns)

    def liveness_set(self, functions=None, patterns=None, pc_samples=None, tick_variable=None):
        return self._elf.liveness_set(functions, patterns, pc_samples, tick_variable)

    def liveness_get(self):
        return self._elf.liveness_get()

    def sampler_status(self):
        return {"user": None, "release": None, "since": None, "client": None}

    def symbol_info(self, location):
        return self._elf.symbol_info(location)

    def source_list(self):
        return self._elf.source_list()

    def source_read(self, name):
        return self._elf.source_read(name)

    def program_ram(self, path, vector_table=None):
        self._need()
        self.elf_load(path)
        entry = self._elf._symbols.entry
        self.state, self.pc = "HALTED", entry
        start = {"from": "entry" if vector_table is None else "vector_table", "entry": entry, "vector_table": vector_table}
        return {"reason": "user", "pc": self.pc, **self._elf._place_of(self.pc), "elapsed_s": 0.0, "start": start}

    def _address(self, location):
        return int(location, 16) if location.startswith("0x") else self._elf._symbols.resolve(location)

    def breakpoint_set(self, location, condition=None, ignore_count=0, log=None, message=None):
        for arg, value in (("condition", condition), ("ignore_count", ignore_count or None), ("log", log),
                           ("message", message)):
            if value is not None:
                raise unsupported_arg("breakpoint_set", arg, value)
        address = self._address(location)
        self.bp_disabled.pop(address, None)
        self.breakpoints[address] = location
        return {"address": address, **self._elf._place_of(address)}

    def breakpoint_clear(self, location=None):
        if location is None:
            self.breakpoints.clear()
            self.bp_disabled.clear()
            self.watchpoints.clear()
        else:
            self.breakpoints.pop(self._address(location), None)
            self.bp_disabled.pop(self._address(location), None)
        return self.breakpoint_list()

    def breakpoint_enable(self, location, enabled):
        address = self._address(location)
        if enabled and address in self.bp_disabled:
            self.breakpoints[address] = self.bp_disabled.pop(address)
        elif not enabled and address in self.breakpoints:
            self.bp_disabled[address] = self.breakpoints.pop(address)
        elif address not in self.breakpoints and address not in self.bp_disabled:
            raise KeyError(f"{location!r} にブレークポイントが無い")
        return self.breakpoint_list()

    def breakpoint_list(self):
        self._need()                             # 本物と同じく、ターゲット未接続では誤り
        rows = [{"address": a, "location": n, "enabled": True, **self._elf._place_of(a)} for a, n in self.breakpoints.items()]
        rows += [{"address": a, "location": n, "enabled": False, **self._elf._place_of(a)} for a, n in self.bp_disabled.items()]
        return {"breakpoints": sorted(rows, key=lambda b: b["address"]),
                "watchpoints": list(self.watchpoints.values())}

    def watchpoint_set(self, location, kind="write", size=None):
        s = self._elf._symbols.variable(location)
        self.watchpoints[s.address] = {"address": s.address, "size": size or s.size, "kind": kind}
        return self.watchpoints[s.address]

    def watchpoint_clear(self, location):
        address = int(location, 16) if location.startswith("0x") else self._elf._symbols.variable(location).address
        self.watchpoints.pop(address, None)
        return self.breakpoint_list()

    def read_registers(self, names=None):
        names = names or [f"r{i}" for i in range(13)] + ["sp", "lr", "pc", "xpsr"]
        return {n: (self.pc if n == "pc" else 0x20008000 if n == "sp" else i) for i, n in enumerate(names)}

    def variable_read(self, name, index=None):
        self._elf._symbols.variable(name)
        return {"name": name, "value": self.counter if name in ("counter", "milestone") else 0}

    def terminal_sources(self):
        return {"sources": [{"id": "uart", "kind": "uart", "name": "UART", "writable": True, "received": 0},
                            {"id": "semihosting", "kind": "semihosting", "name": "Semihosting", "writable": True,
                             "received": 0}], "rtt": False, "swo": False}

    def terminal_read(self, source, since=0, max_bytes=65536, timeout_s=0.0, times=False, terminals=False):
        return {"source": source, "data": "", "start": since, "next": since, "lost": 0,
                **({"times": [], "timebase_hz": None} if times else {}),
                **({"terminals": []} if terminals and source.startswith("rtt") else {})}

    def evaluate(self, expression, frame=0, max_elements=64, live=False):
        return {"expression": expression, "type": "uint32_t", "value": self.variable_read(expression)["value"]}

    def expression_check(self, expression):
        return self._elf.expression_check(expression)                  # ELF だけ（本物）

    def backtrace(self, variables=False, max_frames=64, max_elements=64, assume_leaf=False):
        self._need()
        for arg, value in (("variables", variables or None), ("assume_leaf", assume_leaf or None)):
            if value is not None:
                raise unsupported_arg("backtrace", arg, value)
        return [{"pc": self.pc, "where": self._elf._where(self.pc), "note": ""},
                {"pc": self.pc + 0x20, "where": self._elf._where(self._elf._symbols.symbol("tick").address),
                 "note": ""}][:max_frames]

    # --- ロジアナ（偽物の波形: ch0 = 1 MHz の時計、ch1 = 時計の 4 回に 1 回、ch8 = 前半だけ駆動、ch9 = 中ほどで処理中） ----

    def la_capture(self, rate_hz=None, pretrigger=1024, mask=0, value=0, rise=0, fall=0, timeout_s=5.0, vcd_path=None):
        from array import array
        from fprobe.la import Transitions
        n, period = 8192, 1 / (rate_hz or 25e6)
        values, starts, previous = array("H"), array("Q"), None
        for i in range(0, n, 12):                                    # 25 MHz で 12 サンプル ≒ 半周期 0.5 µs
            k = i // 12
            v = (k & 1) | ((k >> 3) & 1) << 1 | (i < n // 2) << 8 | (3000 <= i < 5000) << 9
            if v != previous:
                values.append(v)
                starts.append(i)
                previous = v
        self._elf._keep_capture(Transitions(values, starts, n, pretrigger, 0, period, 10), "la.capture")
        return {"trigger_index": pretrigger, "samples": n, "sample_period_ns": period * 1e9}

    def la_view(self, start=0, end=None, width=1000):
        return self._elf.la_view(start, end, width)

    def timeline_tiles(self, **params):
        return self._elf.timeline_tiles(**params)

    def recording_status(self):
        return self._elf.recording_status()

    def open_demo_recording(self, directory: Path) -> None:
        """画面を機器なしで試すための記録（共通タイムラインのすべての段の型。tests/demo_recording.py）を作って開く。"""
        from demo_recording import write_demo_recording
        from fprobe.recording import Recording
        write_demo_recording(directory)
        self._elf._view = Recording(directory)

    def target_step(self, count=1):
        self._need()
        self.pc += 2 * count
        self.state = "HALTED"
        return self.target_status()

    def target_halt(self, core=None):
        if core is not None:
            raise unsupported_arg("target_halt", "core", core)
        self._need()
        time.sleep(0.01)
        self.state = "HALTED"
        return self.target_status()

    def target_resume(self, core=None):
        if core is not None:
            raise unsupported_arg("target_resume", "core", core)
        self._need()
        self.state = "RUNNING"
        if self.breakpoints:
            def hit():
                self.counter += 256
                self.state, self.pc = "HALTED", next(iter(self.breakpoints))
            threading.Timer(0.3, hit).start()

    def target_reset(self, halt=False, kind="default"):
        if kind != "default":
            raise unsupported_arg("target_reset", "kind", kind)
        self._need()
        self.state = "HALTED" if halt else "RUNNING"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--ui-dir", type=Path, default=UI_DIR)
    parser.add_argument("--demo", action="store_true",
                        help="デモ（デモの ELF を読んで接続・停止した状態、各パネルの偽の値、Timeline の捕捉と記録）")
    parser.add_argument("--demo-recording", action="store_true", help="--demo と同じ（前の名前）")
    parser.add_argument("--fatal", type=float, default=None, metavar="S",
                        help="致命的エラーのデモ（--demo と。止めると壊れる手がかりのある ELF として走り出し、S 秒後に故障の"
                             "処理の無限ループを回り続ける）")
    args = parser.parse_args()
    if args.demo or args.demo_recording or args.fatal is not None:
        import fake_demo                        # fake_demo は FakeProbe を使う（循環しないようここで）
        probe = fake_demo.DemoProbe(fatal_after_s=args.fatal)
        fake_demo.start(probe)
    else:
        probe = FakeProbe()
    app = create_app(probe, token=None, activity_dir=Path(tempfile.mkdtemp(prefix="fprobe-fake-")),
                     ui_dir=args.ui_dir, watch_s=0.1)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning"))

    async def run():
        task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.01)
        print(f"PORT {server.servers[0].sockets[0].getsockname()[1]}", flush=True)
        await task
    asyncio.run(run())


if __name__ == "__main__":
    main()
