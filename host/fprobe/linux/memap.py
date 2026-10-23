"""道 B の止めずに読む口（memap）: ターゲットの DAP の、システムのメモリにつながる MEM-AP（Zynq は AHB-AP）で物理メモリを読む。

今の実装は Vivado の xsdb（hw_server）を子のプロセスで使い、ボードの USB の JTAG（Zynq-7000 の PYNQ-Z2 は FT2232）から読む
（R26 段階 2 の案 3。fprobe の FPGA の JTAG で読む口は別に作る）。xsdb の場所・JTAG のケーブルのシリアル・そのケーブルのチェーンに
あるはずの装置の IDCODE・1 つの命令の返事を待つ上限（timeout_s）は、使う側が明示する。CPU は止めない（xsdb の APU = DAP の
AHB-AP、mrd -force）。返事が上限までに来なければ xsdb を止め、以後は使えない（事実で断る）。"""
from __future__ import annotations

import queue
import re
import subprocess
import sys
import threading


class XsdbMemAp:
    def __init__(self, xsdb: str, cable_serial: str, chain_idcode: int, timeout_s: float):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", cable_serial):
            raise ValueError(f"ケーブルのシリアル {cable_serial!r} は英数字と _ . -")
        self.timeout_s = timeout_s
        argv = [sys.executable, xsdb] if xsdb.endswith(".py") else [xsdb]
        self._proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, encoding="utf-8", errors="replace", bufsize=1)
        self._lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._read_out, daemon=True, name="xsdb-out").start()
        self._lock = threading.Lock()
        self._dead: str | None = None                  # 止めた理由（以後は使えない）
        try:
            self._tcl("connect")
            devices = self._chain(self._tcl("jtag targets"), cable_serial)
            if devices is None:
                raise RuntimeError(f"JTAG のケーブル {cable_serial} が見えない")
            if chain_idcode not in devices:
                raise RuntimeError(f"ケーブル {cable_serial} のチェーンに IDCODE 0x{chain_idcode:08X} の装置が無い（違うボード？ "
                                   f"このケーブルの装置: {', '.join(f'0x{i:08X}' for i in devices)}）")
            self._tcl(f'targets -set -filter {{name =~ "APU" && jtag_cable_serial == "{cable_serial}"}}')
        except Exception:
            self.close()
            raise
        self.info = {"tool": "xsdb", "cable_serial": cable_serial, "chain_idcode": chain_idcode, "access": "AHB-AP (APU)",
                     "timeout_s": timeout_s}

    @staticmethod
    def _chain(text: str, cable_serial: str) -> list[int] | None:
        """jtag targets の出力（ケーブルの行の下に装置の行）から、そのケーブルの装置の IDCODE。ケーブルが無ければ None。"""
        devices, current = None, False
        for line in text.splitlines():
            m = re.search(r"\(idcode ([0-9a-fA-F]+)", line)
            if m:
                if current:
                    devices.append(int(m[1], 16))
            else:
                current = bool(re.search(rf"(^|\s){re.escape(cable_serial)}\s*$", line))
                if current:
                    devices = []
        return devices

    def _read_out(self) -> None:
        for line in self._proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _tcl(self, command: str) -> str:
        """1 つの Tcl の命令を走らせ、結果を返す（誤りは RuntimeError）。返事は「@@ <誤り> <値>」の 1 行（改行は \\n の文字）。"""
        with self._lock:
            if self._dead:
                raise RuntimeError(f"xsdb は止めた（{self._dead}）。linux.memap.connect し直す")
            line = f'set __r [catch {{{command}}} __v]; puts "@@ $__r [string map {{"\\n" "\\\\n"}} $__v]"\n'
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
            while True:
                try:
                    out = self._lines.get(timeout=self.timeout_s)
                except queue.Empty:
                    self._dead = f"{self.timeout_s} 秒で {command} に答えなかった"
                    self._proc.kill()
                    raise RuntimeError(f"xsdb が {self.timeout_s} 秒で {command} に答えなかった（xsdb を止めた）") from None
                if out is None:
                    self._dead = "xsdb が終わった"
                    raise RuntimeError(f"xsdb が終わった（{command}）")
                if out.startswith("@@ "):
                    code, _, value = out[3:].rstrip("\n").partition(" ")
                    value = value.replace("\\n", "\n")
                    if code != "0":
                        raise RuntimeError(f"xsdb: {command}: {value}")
                    return value

    def read_words(self, address: int, count: int) -> list[int]:
        """物理番地 address から 32 ビットの語を count 個（止めずに）。"""
        value = self._tcl(f"mrd -force -value 0x{address:08X} {count}")
        words = [int(v, 0) for v in value.split()]
        if len(words) != count:
            raise RuntimeError(f"xsdb が {count} 語のうち {len(words)} 語を返した")
        return words

    def close(self) -> None:
        got = self._lock.acquire(timeout=self.timeout_s)       # 読みの途中なら終わるのを待つ（上限まで）
        try:
            self._dead = self._dead or "閉じた"
            if self._proc.poll() is None:
                try:
                    if got:
                        self._proc.stdin.write("exit\n")
                        self._proc.stdin.flush()
                    self._proc.wait(10)
                except Exception:                            # noqa: BLE001  終わらなければ止める
                    self._proc.kill()
                    self._proc.wait()
        finally:
            if got:
                self._lock.release()
