"""Shrike-Lite の C のデモ: 題材ごとに、fprobe の汎用の解析で原因に行き着けるかを実機で確かめ、数値を出す（README の元）。

    python target_ip/fprobe_obs/examples/shrike_lite/analyze.py --token <デーモンのトークン> [--scenario 1 2 3 4] [--latency]

前提: デーモン（PYNQ-Z2 の fprobe）が動いていて、ファームウェア（build.py --firmware）が書いてある。ターゲットの接続・
FPGA の観測・信号の定義・ELF の読み込みはこのスクリプトがする。試験の記録は claude-shrike-<題材> の名前で作る
（消すのは名前で 1 つずつ）。使うのは fprobe の汎用の操作だけ（題材の切り替えは変数の書き込み）。
"""

import argparse
import json
import struct
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ELF = HERE / "build" / "bug" / "fw" / "zephyr" / "zephyr.elf"
MAIN_C = HERE / "firmware_c" / "src" / "main.c"
DHCSR = 0xE000EDF0
SIGNALS = {"groups": {"0": [
    {"name": "state", "lsb": 0, "width": 2, "format": "enum", "enum": {"0": "IDLE", "1": "BUSY", "2": "DONE"}},
    {"name": "spi", "lsb": 2, "width": 1}, {"name": "restart", "lsb": 3, "width": 1},
    {"name": "write", "lsb": 4, "width": 1},
    {"name": "addr", "lsb": 5, "width": 3, "format": "enum",
     "enum": {"0": "ID", "1": "CTRL", "2": "A", "3": "B", "4": "RESULT", "6": "STATUS", "7": "LEFT"}},
    {"name": "left_hi", "lsb": 8, "width": 4, "format": "dec"}, {"name": "dropped", "lsb": 12, "width": 1},
    {"name": "stuck", "lsb": 13, "width": 1}, {"name": "mode", "lsb": 14, "width": 2}]}}
WATCH = ["g_requests", "g_errors", "g_retries", "g_last_a", "g_last_b"]


class Demo:
    def __init__(self, client, elf: Path = ELF):
        self.elf = elf
        from elftools.elf.elffile import ELFFile
        self.c = client
        with open(self.elf, "rb") as f:
            self.sym = {s.name: s["st_value"] for s in ELFFile(f).get_section_by_name(".symtab").iter_symbols()}

    def setup(self) -> None:
        c = self.c
        c.call("target.connect", target="rp2040", connect_mode="attach", frequency_hz=1_000_000)
        c.call("debug.elf.load", path=str(self.elf))
        c.call("fpga_obs.open", chain={"ir_len": 4}, user_ir=2, half_period=20, words=1, sync_period=0, auto_reopen=True)
        c.call("fpga_obs.signals_set", signals=SIGNALS)
        c.call("events.start")
        if c.call("target.status")["state"] == "HALTED":
            c.call("target.resume")

    def read(self, name: str) -> int:
        return struct.unpack("<I", bytes.fromhex(self.c.call("target.memory.read", address=self.sym[name], length=4)))[0]

    def write(self, name: str, value: int) -> None:
        self.c.call("target.memory.write", address=self.sym[name], data_hex=struct.pack("<I", value).hex())

    def scenario(self, s: int) -> None:
        """題材を切り替える。S4 の固まりも MODE の書き込みで解ける（g_reset = FPGA の送り直しは電源の切り入れを伴うので
        使わない）。"""
        self.write("g_scenario", s)
        time.sleep(1)

    def record(self, name: str, seconds: float, pins: bool = True) -> str:
        """MCU の変数・FPGA・（pins なら）ピンを seconds 秒記録する。記録の名前を返す。"""
        c = self.c
        c.call("watch.set", expressions=WATCH, names=WATCH, rate_hz=200 if pins else 1000)
        rec = c.call("recording.start", name=name)["recording"]
        if pins:
            c.call("recording.la_stream.start", rle=True)
        time.sleep(seconds)
        if pins:
            c.call("recording.la_stream.stop")
        c.call("recording.stop")
        c.call("watch.stop")
        c.call("recording.open", name=rec)
        return rec

    def halt_on_fpga(self, condition: str, timeout_s: float = 120, then=None) -> dict:
        """FPGA の条件で MCU をハードで止め、止まった場所・呼び出し履歴（変数つき）・FPGA の最新の値を返す。then: 条件を
        仕掛けた後にすること（題材の切り替え。先に切り替えると、条件の瞬間（例 stuck rose）を仕掛ける前に過ぎてしまう）。"""
        c = self.c
        c.call("fpga_obs.trigger_set", condition=condition, halt=True)
        if then:
            then()
        try:
            deadline = time.monotonic() + timeout_s
            while c.call("target.status")["state"] != "HALTED":
                if time.monotonic() > deadline:
                    return {"halted": False}
                time.sleep(0.5)
            st = c.call("target.status")
            bt = c.call("debug.backtrace", variables=True, max_frames=4)
            latest = c.call("fpga_obs.read", last=1)["items"]
            return {"halted": True, "where": st.get("where"), "halt_reason": st.get("halt_reason"),
                    "backtrace": bt, "fpga": latest[-1].get("fields") if latest else None,
                    "trigger": c.call("fpga_obs.trigger_status")}
        finally:
            c.call("fpga_obs.trigger_clear")
            if c.call("target.status")["state"] == "HALTED":
                c.call("target.resume")


def s1(d: Demo) -> dict:
    """S1（MCU → FPGA）: 結果の直前に多い出来事と、区間ごとの集計（結果あり・なし）。"""
    d.scenario(1)
    d.record("claude-shrike-s1", 6, pins=False)
    d.scenario(0)
    cor = d.c.call("recording.correlate", effect="g_errors changed", causes=None, window_s=0.01, limit=5)
    tx = d.c.call("recording.transactions", start="state changed and state=BUSY", counts=["restart changed"],
                  effect="g_errors changed", rows=0)
    return {"effect": cor["effect"], "top": [{k: c[k] for k in ("condition", "preceded", "coverage", "judged")}
                                             for c in cor["causes"][:3]],
            "warnings": cor["warnings"], "by_effect": tx["by_effect"], "effect_offset_s": tx["effect_offset_s"]}


def s2(d: Demo) -> dict:
    """S2（FPGA → MCU）: 取りこぼし（dropped の反転）の後に誤りが来るか。FPGA の取りこぼしで MCU を止めて中を見る。"""
    d.scenario(2)
    d.record("claude-shrike-s2", 20, pins=False)
    cor = d.c.call("recording.correlate", effect="g_errors changed",
                   causes=["dropped changed", "restart changed", "state changed and state=BUSY"], window_s=0.02)
    halt = d.halt_on_fpga("dropped changed")
    d.scenario(0)
    return {"effect": cor["effect"], "causes": [{k: c[k] for k in ("condition", "preceded", "coverage", "judged")}
                                                for c in cor["causes"]], "halt": halt}


def s3(d: Demo) -> dict:
    """S3（タイミング）: 割り込みの印（ch14）が SPI の転送中（spi = 1）に変わった回の後に誤りが来るか。"""
    d.scenario(3)
    d.record("claude-shrike-s3", 30, pins=True)              # 割り込みは 100 ms ごと: 6 秒では誤りが出ないことがある
    d.scenario(0)
    cor = d.c.call("recording.correlate", effect="g_errors changed",
                   causes=["ch14 changed and spi=1", "ch14 changed", "spi rose"], window_s=0.012)
    return {"effect": cor["effect"], "causes": [{k: c[k] for k in ("condition", "preceded", "coverage", "judged")}
                                                for c in cor["causes"]], "warnings": cor["warnings"]}


def s4(d: Demo) -> dict:
    """S4（1 回きり）: 固まった瞬間（stuck rose）に MCU を止め、そのときの要求の A と処理の場所を見る。"""
    halt = d.halt_on_fpga("stuck rose", timeout_s=180, then=lambda: d.scenario(4))
    requests = d.read("g_requests")
    d.scenario(0)
    return {"halt": halt, "requests_at_halt": requests}


def latency(d: Demo) -> dict:
    """連動の遅れ: FPGA の条件（演算の開始）→ MCU の停止の要求（DHCSR の書き込み）を記録の時刻で測る。"""
    c = d.c
    d.scenario(0)
    c.call("watch.set", expressions=WATCH, names=WATCH, rate_hz=200)
    rec = c.call("recording.start", name="claude-shrike-latency")["recording"]
    c.call("fpga_obs.trigger_set", condition="state=BUSY", halt=True)
    deadline = time.monotonic() + 10
    while c.call("target.status")["state"] != "HALTED" and time.monotonic() < deadline:
        time.sleep(0.2)
    c.call("fpga_obs.trigger_clear")
    c.call("target.resume")
    time.sleep(0.5)
    c.call("recording.stop")
    c.call("watch.stop")
    c.call("recording.open", name=rec)
    ev = c.call("recording.events", streams=["dap", "fpga"], limit=100_000)["events"]
    # DAP の記録: TAR の書き込みは a = 0x4・b = 番地、DRW の読み書きは a = 番地・b = 値。停止 = DHCSR に DBGKEY | C_HALT
    # 停止はクロストリガの割り込み書き込み（kind "inject"。R20 の前の記録では ap_write）。本体に停止の書き込みまでの転送数
    halt_writes = [e for e in ev if e["stream"] == "dap" and e["kind"] in ("ap_write", "inject") and e.get("a") == DHCSR
                   and (e.get("b") or 0) >> 16 == 0xA05F and e["b"] & 0x2]
    halts = [e["t"] for e in halt_writes]
    values = [(e["t"], e["b"]) for e in ev if e["stream"] == "fpga" and e["kind"] == "value"]
    if not halts:
        return {"error": "DHCSR の停止の書き込みが記録に無い"}
    rises = [t for (_, b0), (t, b) in zip(values, values[1:]) if b0 & 0x3 != 1 and b & 0x3 == 1 and t <= halts[0]]
    hz = c.call("recording.status")["timebase_hz"]
    return {"busy_rise_to_halt_write_us": (halts[0] - rises[-1]) / hz * 1e6 if rises else None, "halts": len(halts),
            "transfers": [(e.get("body") or {}).get("transfers") for e in halt_writes],
            "swd_hz": c.call("target.status").get("frequency_hz")}


def main(argv=None) -> int:
    from fprobe.api.client import DaemonClient
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--token", required=True)
    p.add_argument("--url", default="ws://127.0.0.1:7820/api")
    p.add_argument("--scenario", type=int, nargs="*", default=[1, 2, 3, 4])
    p.add_argument("--latency", action="store_true")
    p.add_argument("--elf", type=Path, default=ELF, help="書いたファームウェアの ELF（--fixed で作ったら build/fixed/fw/...）")
    a = p.parse_args(argv)
    d = Demo(DaemonClient(a.url, "shrike-analyze", "script", token=a.token), a.elf)
    d.setup()
    out = {}
    for s in a.scenario:
        out[f"S{s}"] = {1: s1, 2: s2, 3: s3, 4: s4}[s](d)
        print(json.dumps({f"S{s}": out[f"S{s}"]}, ensure_ascii=False, default=str)[:4000], flush=True)
    if a.latency:
        out["latency"] = latency(d)
        print(json.dumps({"latency": out["latency"]}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
