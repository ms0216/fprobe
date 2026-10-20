# PYNQ-Z2 の PS7 のブロック設計 ps7_hp_bd: ps7_bd に S_AXI_HP0（P3 の深いバッファ用）を足したもの。中身は ps7_bd.tcl。
set fprobe_bd_name ps7_hp_bd
set fprobe_hp0 1
source [file join [file dirname [info script]] ps7_bd.tcl]
