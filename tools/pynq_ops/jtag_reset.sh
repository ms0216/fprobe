#!/bin/sh
# PYNQ-Z2 が「Reboot failed -- System halted」で止まったとき、JTAG（PYNQ-Z2 自身の USB ケーブル）から PS のソフトリセットをかける
OO="/c/Users/M/AppData/Local/Microsoft/WinGet/Packages/xpack-dev-tools.openocd-xpack_Microsoft.Winget.Source_8wekyb3d8bbwe/xpack-openocd-0.12.0-7/bin/openocd"
cd "$(dirname "$0")"
timeout 60 "$OO" -f jtag_reset_full.cfg -c "init" -c "targets zynq.cpu0" -c "resume" -c "targets zynq.cpu1" -c "resume" \
  -c "zynq.ahb mww 0xF8000008 0xDF0D" -c "zynq.ahb mww 0xF8000200 0x1" -c "shutdown" 2>&1 | grep -E "Error: (JTAG|Invalid|Failed)" | head -3
