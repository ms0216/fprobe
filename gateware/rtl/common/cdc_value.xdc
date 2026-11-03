# cdc_value の制約（Vivado。read_xdc -unmanaged -ref cdc_value で、このモジュールの全部の実体に効く。fprobe_gw.toolchain.vivado が
# ソースの隣の <entity>.xdc を読む）。
# 保持した値 held は、トグルが受け側で 2 段の同期化（sync_bit）と t_prev の比べを通り、受け側の返事（t_prev）が送り側で 2 段の
# 同期化を通って戻るまで変わらない（握手。cdc_value.vhd の注）。value_r はトグルの変化を見た周期にだけ held を取る。よって
# held → value_r の経路は受け側の 2 周期の中に着けばよい（設計で決まる多周期の経路）。関連クロック（同じ MMCM の整数比。
# 例: sys 100 MHz → cap_clk 400 MHz）では、Vivado は既定でこの経路を受け側の 1 周期（2.5 ns）で検査し、配置しだいで不合格に
# なる（C:\fw62 で −0.068 ns）。
# held と value_r はどちらも generate（cross_g）の中で作られ、名前は cross_g.held_reg・cross_g.value_r_reg（* で受ける。
# 接頭辞なしの value_r_reg[*] は何にも当たらず、C:\fw83・fw84 まで制約が効いていなかった）。
# SAME_CLOCK = true の実体には held が無く、同じクロックの 1 周期の経路なので何もしない。held がある実体で value_r が
# 見つからなければ get_cells が誤りにする（-quiet を付けない。名前が変わったら黙って効かなくならない）。
set held [get_cells -quiet {*held_reg[*]}]
if {[llength $held] > 0} {
  set value_r [get_cells {*value_r_reg[*]}]
  set_multicycle_path -setup -end 2 -from $held -to $value_r
  set_multicycle_path -hold -end 1 -from $held -to $value_r
}
