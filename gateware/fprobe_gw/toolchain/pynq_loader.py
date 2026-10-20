"""PYNQ-Z2 の PL を書く。

PYNQ の Linux 経由（pynq.Bitstream・fpga_manager）で書くと Linux が固まった（2026-10-19。原因は未特定）ので、
JTAG（Vivado の hw_server）で書く。口はシリアル 1234-TUL だけを選び、IDCODE が Zynq-7020 であることを照合してから書く
（Primer の FT2232 と取り違えない）。

PL を書き直す間に PS が PL の番地を読むと ARM が固まる。また、PS と PL の間のレベルシフタが有効なまま PL を書き直した・
PS7 を持たない回路でシフタを有効にしたとき、Linux が固まった（2026-10-20。PL から PS への信号が定まらないため）。そこで、
書く前に PYNQ の上の補助 /usr/local/bin/fprobe-pl で中継を止めて「fprobe の回路が載っている」印を消し、シフタを無効にし、fprobe の回路（zynq_frame_link を持つ）を書いたときだけ印を置いて中継を
起動し直す。中継は印が無ければ PL を読まない。PS と PL の間のレベルシフタは中継が起動時に有効にする（JTAG で書いた
後は無効のままで、PL の信号が PS に届かない。2026-10-20）。補助は `python -m fprobe_gw pynq install` が据え付ける。
"""

import os
import subprocess
from pathlib import Path

from .vivado import VivadoToolchain

__all__ = ["DEFAULT_HOST", "PynqError", "PynqLoader", "render_jtag_tcl"]

DEFAULT_HOST = "192.168.77.2"       # PC の USB の LAN に直結（PYNQ の eth0:2）
DEFAULT_USER = "xilinx"
DEFAULT_KEY = Path.home() / ".ssh" / "fprobe_pynq"
PL_HELPER = "/usr/local/bin/fprobe-pl"
ZYNQ7020_IDCODE = "3727093"         # IDCODE の版の桁を除いた部分


class PynqError(RuntimeError):
    """PYNQ への書き込み・操作が失敗した。"""


def render_jtag_tcl(bitstream: Path) -> str:
    bit = Path(bitstream).resolve().as_posix()
    return f"""open_hw_manager
connect_hw_server -allow_non_jtag
set mine [lsearch -all -inline -nocase [get_hw_targets] *1234-TUL*]
if {{[llength $mine] != 1}} {{ puts "FPROBE-ERROR: 1234-TUL cable count is not 1: $mine"; exit 2 }}
current_hw_target [lindex $mine 0]
open_hw_target
set pl [lsearch -inline [get_hw_devices] xc7z020*]
if {{$pl eq ""}} {{ puts "FPROBE-ERROR: xc7z020 not found"; exit 3 }}
if {{![string match -nocase "*{ZYNQ7020_IDCODE}" [get_property IDCODE_HEX $pl]]}} {{ puts "FPROBE-ERROR: IDCODE is not Zynq-7020"; exit 4 }}
current_hw_device $pl
set_property PROGRAM.FILE {{{bit}}} $pl
program_hw_devices $pl
close_hw_target
exit 0
"""


class PynqLoader:
    def __init__(self, toolchain: VivadoToolchain, *, host: str | None = None, user: str | None = None,
                 key: Path | None = None, run=subprocess.run):
        self._toolchain = toolchain
        self.host = host or os.environ.get("FPROBE_PYNQ_HOST", DEFAULT_HOST)
        self.user = user or os.environ.get("FPROBE_PYNQ_USER", DEFAULT_USER)
        self.key = Path(key or os.environ.get("FPROBE_PYNQ_KEY", DEFAULT_KEY))
        self._run = run

    def ssh(self, command: str, *, timeout: float = 60) -> str:
        args = ["ssh", "-i", str(self.key), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                f"{self.user}@{self.host}", command]
        r = self._run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        if r.returncode != 0:
            raise PynqError(f"PYNQ への SSH のコマンドが失敗（{command}、終了コード {r.returncode}）: "
                            f"{(r.stderr or r.stdout or '').strip()}")
        return r.stdout or ""

    def program(self, bitstream: Path, *, relay: bool, design_id: str = "") -> None:
        """bitstream を JTAG で書く。relay は zynq_frame_link を持つ回路か（持つなら印 design_id を置き中継を起動する）。"""
        bitstream = Path(bitstream).resolve()          # Vivado は置き場所を cwd にして起動する
        if not bitstream.is_file():
            raise FileNotFoundError(bitstream)
        self.ssh(f"sudo -n {PL_HELPER} before")
        tcl = bitstream.parent / "jtag_program.tcl"
        tcl.write_text(render_jtag_tcl(bitstream), encoding="utf-8")
        r = self._run([str(self._toolchain.vivado), "-mode", "batch", "-nojournal", "-nolog", "-notrace",
                       "-source", str(tcl)], cwd=bitstream.parent, capture_output=True, text=True, errors="replace", timeout=600)
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0 or "FPROBE-ERROR" in out:
            lines = [l for l in out.splitlines() if "FPROBE-ERROR" in l or l.startswith("ERROR")]
            raise PynqError(f"JTAG での書き込みが失敗（終了コード {r.returncode}）: " + " / ".join(lines))
        if relay:
            self.ssh(f"sudo -n {PL_HELPER} after {design_id}")
