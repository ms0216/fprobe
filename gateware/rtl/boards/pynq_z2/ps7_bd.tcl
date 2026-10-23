# PYNQ-Z2 の PS7 のブロック設計 ps7_bd（Vivado のメモリ上のプロジェクトで source する。fprobe_gw/toolchain/vivado.py）。
#
# PS の M_AXI_GP0（AXI3）を、Xilinx の AXI のプロトコル変換で AXI4-Lite にして外に出す（口 m_axi_*）。
# 変換は 1 語ずつの読み書きにし、AXI3 の ID・バーストの応答を PL の回路が持たなくてよいようにする。
# ACLK は外から入れる sys（MMCM の 100 MHz）。FCLK・PS のリセットの出力は使わない（クロックまたぎを作らない）。
# 番地は 0x40000000 から 4 KB（zynq_frame_link のレジスタ。PS の中継が /dev/mem で写す）。
# 生成される包み ps7_bd_wrapper の口: aclk・aresetn・m_axi_*・pjtag_*（DDR と MIO は PS の専用の端子で、外に出さない）。
# PS の DAP の PJTAG を EMIO にして口 pjtag_tck・tms・tdi（入）・tdo（出）に出す（ボードの端子へ素通し。DAP がつながるのは
# independent の起動のときだけ。docs/superpowers/specs/2026-10-23-zynq-pjtag-design.md）。
#
# 呼ぶ前に fprobe_hp0 = 1 にすると（ps7_hp_bd.tcl）、S_AXI_HP0（AXI3、64 ビット、ACLK = aclk）も口 s_axi_hp0_* として出し、
# PL から PS の DDR（0〜512 MB）を読み書きできるようにする（P3 の深いバッファ。axi_mem_port がつながる）。

if {![info exists fprobe_bd_name]} { set fprobe_bd_name ps7_bd }
if {![info exists fprobe_hp0]} { set fprobe_hp0 0 }
create_bd_design $fprobe_bd_name

set ps [create_bd_cell -type ip -vlnv xilinx.com:ip:processing_system7 ps]
set_property -dict [list \
  CONFIG.PCW_USE_M_AXI_GP0 1 \
  CONFIG.PCW_EN_CLK0_PORT 0 \
  CONFIG.PCW_EN_RST0_PORT 0 \
  CONFIG.PCW_PJTAG_PERIPHERAL_ENABLE 1 \
  CONFIG.PCW_PJTAG_PJTAG_IO EMIO \
] $ps
foreach {port dir} {tck I tms I tdi I tdo O} {
  create_bd_port -dir $dir pjtag_$port
  connect_bd_net [get_bd_ports pjtag_$port] [get_bd_pins ps/PJTAG_[string toupper $port]]
}

set conv [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_protocol_converter conv]
set_property -dict [list CONFIG.SI_PROTOCOL AXI3 CONFIG.MI_PROTOCOL AXI4LITE CONFIG.TRANSLATION_MODE 2] $conv

create_bd_port -dir I -type clk -freq_hz 100000000 aclk
create_bd_port -dir I -type rst aresetn
set_property CONFIG.POLARITY ACTIVE_LOW [get_bd_ports aresetn]
set m [create_bd_intf_port -mode Master -vlnv xilinx.com:interface:aximm_rtl:1.0 m_axi]
set_property -dict [list CONFIG.PROTOCOL AXI4LITE CONFIG.DATA_WIDTH 32 CONFIG.ADDR_WIDTH 32 \
  CONFIG.FREQ_HZ 100000000] $m
set_property CONFIG.ASSOCIATED_BUSIF m_axi [get_bd_ports aclk]
set_property CONFIG.ASSOCIATED_RESET aresetn [get_bd_ports aclk]

connect_bd_net [get_bd_ports aclk] [get_bd_pins ps/M_AXI_GP0_ACLK] [get_bd_pins conv/aclk]
connect_bd_net [get_bd_ports aresetn] [get_bd_pins conv/aresetn]
connect_bd_intf_net [get_bd_intf_pins ps/M_AXI_GP0] [get_bd_intf_pins conv/S_AXI]
connect_bd_intf_net [get_bd_intf_pins conv/M_AXI] $m

assign_bd_address -offset 0x40000000 -range 4K -target_address_space [get_bd_addr_spaces ps/Data] \
  [get_bd_addr_segs m_axi/Reg]

if {$fprobe_hp0} {
  set_property -dict [list CONFIG.PCW_USE_S_AXI_HP0 1 CONFIG.PCW_S_AXI_HP0_DATA_WIDTH 64] $ps
  set hp [create_bd_intf_port -mode Slave -vlnv xilinx.com:interface:aximm_rtl:1.0 s_axi_hp0]
  set_property -dict [list CONFIG.PROTOCOL AXI3 CONFIG.DATA_WIDTH 64 CONFIG.ADDR_WIDTH 32 CONFIG.ID_WIDTH 6 \
    CONFIG.FREQ_HZ 100000000] $hp
  set_property CONFIG.ASSOCIATED_BUSIF {m_axi:s_axi_hp0} [get_bd_ports aclk]
  connect_bd_net [get_bd_ports aclk] [get_bd_pins ps/S_AXI_HP0_ACLK]
  connect_bd_intf_net $hp [get_bd_intf_pins ps/S_AXI_HP0]
  assign_bd_address -target_address_space [get_bd_addr_spaces s_axi_hp0] \
    [get_bd_addr_segs ps/S_AXI_HP0/HP0_DDR_LOWOCM]
}

validate_bd_design
save_bd_design
