"""道 B のリングと、止めずに読む口（memap）: linux.ring.*・linux.memap.*。

LinuxService（fprobe.probe_linux）に混ぜ込む部品（LinuxRingMixin）。エージェントが物理メモリに書くリングを、止めて読む CPU
（Cortex-A のコア）か、止めずに読む口（xsdb か fprobe の JTAG の MEM-AP）で読む。読んだ記録は LinuxService の _linux_keep で
出来事に入れる。
"""
from __future__ import annotations

import threading
import time

from .armv7a_debug import decode_ap_idr
from .jtag_chain import scan_chain
from .linux.memap import JtagMemAp, XsdbMemAp
from .linux.ring_cpu import ArmACpu, MemapReader, count_ring_read, new_ring_stats
from .linux.ring import HEADER, RECORD, RingError, RingReader, parse_header
from .linux.ssh import TargetHalted
from .linux.target import Target as _Target


class LinuxRingMixin:
    def linux_ring_open(self, target: str, phys: int, size: int, cached: bool, linger: bool) -> dict:
        """エージェントに、物理番地 phys から size バイトをリングとして開かせる（/dev/mem。cached = false なら O_SYNC）。
        linger = true なら、SSH が切れても ptrace しているプロセスが終わるまで、ptrace とリングへの書き込みを続ける。"""
        t = self._lt(target)
        reply = self._agent(t).request("ring_open", phys=phys, size=size, cached=cached, linger=linger)
        with t.lock:
            t.ring = {"phys": phys, "size": size, "cached": cached, "linger": linger, "capacity": reply["capacity"],
                      "first": reply["first"]}
            t.ring_reader = None
            t.ring_stats = new_ring_stats()
        return t.ring

    def linux_memap_connect(self, target: str, xsdb: str, cable_serial: str, chain_idcode: int, timeout_s: float) -> dict:
        """止めずに読む口（memap）を開く: Vivado の xsdb（hw_server）で、ボードの USB の JTAG から DAP の AHB-AP で物理メモリを読む。
        xsdb の場所・JTAG のケーブルのシリアル・チェーンにあるはずの装置の IDCODE（違うボードを読まない）を明示する。"""
        t = self._lt(target)
        if t.memap is not None:
            t.memap.close()
            t.memap = None
        t.memap = XsdbMemAp(xsdb, cable_serial=cable_serial, chain_idcode=chain_idcode, timeout_s=timeout_s)
        return t.memap.info

    def linux_memap_connect_jtag(self, target: str, chain_idcode: int, ap: int, frequency_hz: int) -> dict:
        """止めずに読む口（memap）を、fprobe の JTAG の口につないだ DAP の MEM-AP で開く（xsdb 不要）。
        chain_idcode（チェーンにあるはずの DP の IDCODE。違うターゲットを読まない）・ap（MEM-AP の番号。Zynq の AHB-AP は 0）・
        frequency_hz は使う側が明示する。開いている間、プローブの生の DAP をこの口が使う（jtag.scan 等とは同時に使えない）。"""
        t = self._lt(target)
        self._host._require_disconnected("止めずに読む口（JTAG）")
        if t.memap is not None:
            t.memap.close()
            t.memap = None
        self._host._ensure_design("jtag")
        with self._host._lock:
            scan = scan_chain(self._host._jtag_wire(frequency_hz))
            want = chain_idcode & 0x0FFFFFFF
            index = next((d.index for d in scan.devices if d.idcode is not None and d.idcode & 0x0FFFFFFF == want), None)
            if index is None:
                seen = ", ".join(f"0x{d.idcode:08X}" if d.idcode is not None else "BYPASS" for d in scan.devices) or "なし"
                raise RuntimeError(f"チェーンに IDCODE 0x{chain_idcode:08X} の装置が見えない（見えた装置: {seen}）")
            if scan.chain is None or len(scan.chain) != len(scan.devices):
                raise RuntimeError(f"チェーンの IR 長が分からない: {scan.as_dict()}")
            dap = self._host._raw()
            dap.start("jtag", frequency_hz, scan.chain, dap_index=index)
            dap.read_dpidr()                                    # JTAG の初期化に要る（無いと SELECT の書き込みが応答しない）
            if not dap.power_up()["debug_power"]:
                raise RuntimeError("DP のデバッグ電源が上がらない")
            dap.clear_errors()                                  # 接続の前から残っていた誤りの印（STICKYERR 等）を消す。以後の読みの誤りは事実で出る
            idr = decode_ap_idr(dap.read_ap(ap, 0xFC))
            if idr["kind"] != "mem-ap":
                raise RuntimeError(f"AP {ap} は MEM-AP でない: {idr}")
            info = {"via": "probe_jtag", "chain_idcode": chain_idcode, "device_index": index, "ap": ap,
                    "ap_idr": idr["value"], "frequency_hz": frequency_hz}
            t.memap = JtagMemAp(dap, ap, info)
            return info

    def linux_memap_disconnect(self, target: str) -> dict:
        t = self._lt(target)
        if t.memap is not None:
            t.memap.close()
            t.memap = None
        return {"memap": None}

    def _linux_ring_cpu(self):
        """止めて読む CPU（target.connect で Cortex-A のコアにつないだもの）"""
        target = getattr(self._host._debug, "target", None) if self._host._debug is not None else None
        if target is None or not hasattr(target, "core") or not hasattr(target.core, "read_memory_fast"):
            raise RuntimeError("Cortex-A のコアに target.connect していない（道 B の access=halt はそのコアに読ませる）")
        return ArmACpu(target)

    def _linux_ring_reader(self, t: _Target, access: str):
        """access に合う読み手（halt = 止めて読む CPU、memap = 止めずに読む口）。リングのキャッシュの開き方と合わなければ誤り。"""
        if access == "halt":
            if t.ring is not None and not t.ring["cached"]:
                raise ValueError("cached=false で開いたリングは access=halt で読まない: エージェントは O_SYNC（キャッシュしない）で書き、"
                                 "止めたコアはキャッシュする線形の写しで読むので、古い値を読みうる（cached=true で開き直す）")
            return self._linux_ring_cpu()
        if access == "memap":
            if t.memap is None:
                raise RuntimeError("止めずに読む口が無い（linux.memap.connect）")
            if t.ring is not None and t.ring["cached"]:
                raise ValueError("cached=true で開いたリングは access=memap で読まない: memap は CPU のキャッシュを通らずに物理メモリを"
                                 "読むので、キャッシュに残った書き込みが見えない（cached=false で開き直す）")
            return MemapReader(t.memap)
        raise ValueError(f"access は halt / memap（{access!r}）")

    def linux_ring_read(self, target: str, address: int, access: str, max_records: int) -> dict:
        """リングの新しい記録を、古い方から max_records 個まで読む。address はリングの頭の、読み手から見た番地。
        access = "halt": target.connect でつないだコアを止めて、そのコアに読ませ（address は仮想番地）、すぐ再開する。
        access = "memap": linux.memap.connect の口で、止めずに読む（address は物理番地）。"""
        t = self._lt(target)
        cpu = self._linux_ring_reader(t, access)
        words = 0

        def read(address: int, count: int) -> bytes:
            nonlocal words
            words += count
            return b"".join(w.to_bytes(4, "little") for w in cpu.read_words(address, count))

        out = {"records": [], "lost": [], "torn": [], "corrupt": [], "head": None, "left": 0}
        header_error = None
        with self._host._lock:
            was = access == "halt" and cpu.state() == "halted"
            t0 = time.perf_counter()
            failed = False
            try:
                if access == "halt" and not was:
                    cpu.halt()                               # 止める要求の後の失敗でも、下の finally で必ず再開する
                try:
                    head = parse_header(read(address, HEADER // 4))
                    if t.ring is not None and head["capacity"] != t.ring["capacity"]:
                        raise RingError(f"頭の capacity {head['capacity']} が ring_open の {t.ring['capacity']} と違う")
                except RingError as e:
                    header_error = str(e)                    # 事実で返す（この読みの頭は使わない）
                else:
                    if t.ring_reader is None or (t.ring_reader.capacity, t.ring_reader.first) != (head["capacity"], head["first"]):
                        t.ring_reader = RingReader(head["capacity"], head["first"])
                    out = t.ring_reader.take(head["head"], lambda f, n: read(address + HEADER + f * RECORD, n * RECORD // 4),
                                             limit=max_records)
            except BaseException:
                failed = True
                raise
            finally:
                if access == "halt" and not was:
                    try:
                        cpu.resume()
                    except Exception:                        # noqa: BLE001  元の誤りがあればそちらを返す
                        if not failed:
                            raise
                read_ms = (time.perf_counter() - t0) * 1e3
                halted_ms = read_ms if access == "halt" else 0.0       # memap は止めない
        records = [self._linux_ring_record(target, t, r) for r in out["records"]]
        with t.lock:
            count_ring_read(t.ring_stats, out, records, halted_ms, header_error is not None)
        return {"records": records, "lost": out["lost"], "torn": out["torn"], "corrupt": out["corrupt"],
                "head": out["head"], "left": out["left"], "header_error": header_error, "halted_ms": halted_ms,
                "read_ms": read_ms, "words": words, "core": cpu.index, "access": access,
                "kept_halted": was if access == "halt" else None}

    def _linux_ring_record(self, target: str, t: _Target, r: dict) -> dict:
        regs = dict(zip(("r0", "r1", "r2", "r3", "lr"), r["regs"]))
        body = {"method": "ptrace", "probe": t.by_bp.get(r["bp"], f"bp{r['bp']}"), "tid": r["tid"], "target_ns": r["t_ns"],
                "regs": regs, "clock": "CLOCK_MONOTONIC_RAW", "seq": r["seq"], "paths": ["b"]}
        kept = self._linux_keep(target, t, body)
        return {"agent_seq": r["seq"], "probe": body["probe"], "duplicate": not kept}

    def linux_ring_watch(self, target: str, interval_s: float, address: int, access: str, max_records: int) -> dict:
        """interval_s ごとに linux.ring.read を続ける（止めた回数と時間は linux.agent.status の ring_stats）。"""
        t = self._lt(target)
        self.linux_ring_unwatch(target=target)
        t.ring_stop = threading.Event()
        t.ring_stats["error"] = None
        stop = t.ring_stop

        def loop():
            while not stop.wait(interval_s):
                try:
                    self.linux_ring_read(target=target, address=address, access=access, max_records=max_records)
                except TargetHalted:
                    continue                                 # 止まっている間は休み、走り出したら続ける
                except Exception as e:                       # noqa: BLE001  止めて事実を残す（調停を止めない）
                    t.ring_stats["error"] = f"{type(e).__name__}: {e}"
                    return
        threading.Thread(target=loop, daemon=True, name=f"linux-ring-{target}").start()   # 頭の誤りは事実として数え、続ける
        return {"watching": True}

    def linux_ring_unwatch(self, target: str) -> dict:
        t = self._lt(target)
        if t.ring_stop is not None:
            t.ring_stop.set()
            t.ring_stop = None
        return {"watching": False, "ring_stats": dict(t.ring_stats)}
