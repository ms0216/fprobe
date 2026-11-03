"""ISERDESE2 の本物の模型（Vivado の UNISIM）を xsim で動かし、記録を tests/data/iserdes_ref_w<W>.txt に書く。

一度だけ（模型を変えたとき・Vivado の版を変えたとき）手で実行する。試験（tests/test_xilinx_la_iserdes.py）は記録だけを使う
（Vivado が無くても走る）。使い方: python gateware/tests/xsim/run_iserdes_ref.py [Vivado の bin]
"""

import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
DEFAULT_BIN = Path(r"C:\AMDDesignTools\2025.2\Vivado\bin")


def run(width: int, vivado_bin: Path) -> str:
    glbl = vivado_bin.parent / "data" / "verilog" / "src" / "glbl.v"
    with tempfile.TemporaryDirectory() as tmp:
        def tool(name, *args):
            done = subprocess.run([str(vivado_bin / f"{name}.bat"), *args], cwd=tmp, capture_output=True, text=True)
            if done.returncode:
                raise RuntimeError(f"{name} が失敗した:\n{done.stdout}\n{done.stderr}")
        # 幅は包みの module で渡す（xelab.bat の -generic_top W=6 は cmd が = で引数を切る）
        (Path(tmp) / "top.v").write_text(f"module top; iserdes_ref_tb #(.W({width})) t(); endmodule\n")
        tool("xvlog", str(HERE / "iserdes_ref_tb.v"), "top.v", str(glbl))
        tool("xelab", "-L", "unisims_ver", "-L", "secureip", "top", "glbl", "-s", "sim")
        out = subprocess.run([str(vivado_bin / "xsim.bat"), "sim", "-R"], cwd=tmp, check=True, capture_output=True,
                             text=True).stdout
    lines = [ln for ln in out.splitlines() if ln.startswith(("B ", "Q "))]
    return "\n".join(lines) + "\n"


def main() -> None:
    vivado_bin = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_BIN
    for width in (6, 8):
        text = run(width, vivado_bin)
        (DATA / f"iserdes_ref_w{width}.txt").write_text(
            f"# ISERDESE2 NETWORKING DDR DATA_WIDTH={width}（Vivado の UNISIM を xsim で。gateware/tests/xsim/run_iserdes_ref.py）\n"
            + text, encoding="utf-8")


if __name__ == "__main__":
    main()
