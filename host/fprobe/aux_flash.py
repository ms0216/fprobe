"""補助マイコン（Pico、MicroPython 入り）に計測のプログラム（host/firmware/aux_pico/main.py）を書き込む。

    python -m fprobe.aux_flash <Pico の USB シリアル番号>

COM ポートは USB シリアル番号で選ぶ（他の Pico・機器には触れない）。MicroPython 本体は BOOTSEL で出る
RPI-RP2 ドライブに UF2 を置いて先に入れておく。要 mpremote（pip install -e host[aux]）。
"""

import argparse
import subprocess
import sys
from pathlib import Path

from serial.tools import list_ports

PICO_VID = 0x2E8A
MAIN = Path(__file__).resolve().parents[1] / "firmware" / "aux_pico" / "main.py"


def port_of(serial_number: str) -> str:
    ports = [p.device for p in list_ports.comports() if p.vid == PICO_VID and p.serial_number == serial_number]
    if len(ports) != 1:
        raise SystemExit(f"シリアル番号 {serial_number} の Pico の COM ポートが特定できない: {ports}")
    return ports[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("serial", help="Pico の USB シリアル番号")
    args = parser.parse_args()
    port = port_of(args.serial)
    subprocess.run([sys.executable, "-m", "mpremote", "connect", port, "cp", str(MAIN), ":main.py",
                    "+", "reset"], check=True)
    print(f"{port} に {MAIN.name} を書き込んだ（リセットで動き始める）")


if __name__ == "__main__":
    main()
