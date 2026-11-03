# sync_bit の制約（Vivado。read_xdc -unmanaged -ref sync_bit で、このモジュールの全部の実体に効く。fprobe_gw.toolchain.vivado が
# ソースの隣の <entity>.xdc を読む）。
# 入力 d は非同期（別のクロックの領域、または関連クロックでも時刻を合わせない信号）。最初の段 stage(0) までの経路は時間を測らない
# （準安定は 2 段目までの 1 周期で収める。それが同期化の FF の役目）。関連クロック（同じ MMCM の整数比。sys 100 MHz → cap_clk 400 MHz）
# では、Vivado は既定でこの経路を受け側の 1 周期（2.5 ns）で検査し、配置しだいで不合格になる（ビルド C:/fw62 で la_testgen の rst の
# 受け渡しが -0.018 ns、C:/fw84 で cdc_value のトグルが -0.157 ns）。
# stage は generate（cross_g）の中で駆動され、名前は cross_g.stage_reg[0]（* で受ける。接頭辞なしの stage_reg[0] は何にも当たらず、
# C:\fw84 まで制約が効いていなかった）。SAME_CLOCK = true の実体には FF が無いので何もしない。
set first [get_cells -quiet {*stage_reg[0]}]
if {[llength $first] > 0} {
  set_false_path -to $first
}
