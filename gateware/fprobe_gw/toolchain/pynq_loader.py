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
import re
import subprocess
from pathlib import Path

from .vivado import VivadoToolchain

__all__ = ["DEFAULT_HOST", "PynqError", "PynqLoader", "render_jtag_tcl", "run_checked"]

DEFAULT_HOST = "192.168.77.2"       # PC の USB の LAN に直結（PYNQ の eth0:2）
DEFAULT_USER = "xilinx"
DEFAULT_KEY = Path.home() / ".ssh" / "fprobe_pynq"
PL_HELPER = "/usr/local/bin/fprobe-pl"
ZYNQ7020_IDCODE = "3727093"         # IDCODE の版の桁を除いた部分
VIVADO_TIMEOUT_S = 600
_SAFE_USER = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")            # sudoers・SSH のコマンドにそのまま入るので語を限る
_SAFE_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:-]*")           # 先頭が - だと ssh のオプションに読まれる
_DESIGN_ID = re.compile(r"[0-9a-f]+")                            # fprobe-pl after <id> と同じ（遠隔のシェルに渡る）


class PynqError(RuntimeError):
    """PYNQ への書き込み・操作が失敗した。"""


def run_checked(run, args, *, what: str, timeout: float, **kw) -> subprocess.CompletedProcess:
    """外部コマンドを走らせる。時間切れ・起動できない（コマンドが無い）は PynqError にする（終了コードは呼び手が見る）。"""
    try:
        return run(args, timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        raise PynqError(f"{what}が時間内（{timeout:g} 秒）に終わらなかった") from None
    except OSError as e:
        raise PynqError(f"{what}を起動できない（{args[0]}）: {e}") from e


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
        if not _SAFE_USER.fullmatch(self.user) or not _SAFE_HOST.fullmatch(self.host):
            raise PynqError(f"PYNQ のユーザー名・ホスト名に使えない文字がある（{self.user}@{self.host}）")

    def ssh(self, command: str, *, timeout: float = 60) -> str:
        args = ["ssh", "-i", str(self.key), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                f"{self.user}@{self.host}", command]
        r = run_checked(self._run, args, what=f"PYNQ への SSH のコマンド（{command}）", timeout=timeout,
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise PynqError(f"PYNQ への SSH のコマンドが失敗（{command}、終了コード {r.returncode}）: "
                            f"{(r.stderr or r.stdout or '').strip()}")
        return r.stdout or ""

    def program(self, bitstream: Path, *, relay: bool, design_id: str = "", shifter: bool = False) -> None:
        """bitstream を JTAG で書く。relay は zynq_frame_link を持つ回路か（持つなら印 design_id を置き中継を起動する）。
        shifter は、中継を持たないが PS が AXI で触る回路（Zynq DUT）か（書いた後に PS と PL の間のレベルシフタを有効にする。
        書き終えたとき（PS7 を持つ回路が載ったとき）だけ。失敗したら有効にしない）。"""
        if relay and not _DESIGN_ID.fullmatch(design_id):
            # 遠隔のシェルに渡る語なので、PYNQ に触れる前に断る（空は JTAG で書いた後に補助が拒み、中継が止まったままになる）
            raise PynqError(f"中継の印の id は 16 進（小文字）でなければならない: {design_id!r}")
        bitstream = Path(bitstream).resolve()          # Vivado は置き場所を cwd にして起動する
        if not bitstream.is_file():
            raise FileNotFoundError(bitstream)
        self.ssh(f"sudo -n {PL_HELPER} before")
        tcl = bitstream.parent / "jtag_program.tcl"
        tcl.write_text(render_jtag_tcl(bitstream), encoding="utf-8")
        # 時間切れでも中継は止まり、シフタは無効のまま（安全な側）。書き直すまで PL の状態は不明
        r = run_checked(self._run, [str(self._toolchain.vivado), "-mode", "batch", "-nojournal", "-nolog", "-notrace",
                                    "-source", str(tcl)], what="Vivado の JTAG の書き込み", timeout=VIVADO_TIMEOUT_S,
                        cwd=bitstream.parent, capture_output=True, text=True, errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0 or "FPROBE-ERROR" in out:
            lines = [l for l in out.splitlines() if "FPROBE-ERROR" in l or l.startswith("ERROR")]
            raise PynqError(f"JTAG での書き込みが失敗（終了コード {r.returncode}）: " + " / ".join(lines))
        if relay:
            self.ssh(f"sudo -n {PL_HELPER} after {design_id}")
        elif shifter:
            self.ssh(f"sudo -n {PL_HELPER} enable")
