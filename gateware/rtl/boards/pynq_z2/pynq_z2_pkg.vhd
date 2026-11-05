-- PYNQ-Z2 のボード定数。
-- ツール側の定義（gateware/fprobe_gw/boards/pynq_z2.py）と一致していることを
-- tests/test_boards_pynq_z2.py が検査する。値を変える場合は両方を変えること。

library ieee;
use ieee.std_logic_1164.all;

package pynq_z2_pkg is

  constant CLK125_HZ : natural := 125_000_000;

  -- MMCME2: 125 MHz * 48 / 5 = 1200 MHz（VCO。-1 の上限）、/ 12 = 100 MHz（sys）。
  -- 取り込みのクロック cap_clk（2 本目の出力）は構成の CAP_RATIO で sys × CAP_RATIO = VCO / mmcm_cap_div(CAP_RATIO)
  -- （比 3 = 300 MHz、比 4 = 400 MHz）
  constant MMCM_CLKIN_PERIOD_NS : real := 8.0;
  constant MMCM_MULT : real := 48.0;
  constant MMCM_DIVCLK : positive := 5;
  constant MMCM_OUT_DIV : real := 12.0;

  constant SYS_CLK_HZ : natural := 100_000_000;

  -- cap_clk = sys × ratio の 2 本目の出力の分周（ratio <= 1 なら 0 = 2 本目の出力を使わない）。割り切れない比は合成で止める
  function mmcm_cap_div(ratio : natural) return natural;

  -- デバッグの線（PMOD JA、pmod1）の割り当て。Primer の J12 と同じ並び
  constant DBG_SWCLK   : natural := 0;
  constant DBG_TDI     : natural := 1;
  constant DBG_NRESET  : natural := 2;
  constant DBG_SWDIO   : natural := 3;
  constant DBG_SPARE   : natural := 4;
  constant DBG_UART_RX : natural := 5;
  constant DBG_SWO     : natural := 6;
  constant DBG_UART_TX : natural := 7;

  -- ロジアナの専用の入力の数（PMOD JB の 6 本。fprobe_gw/boards/pynq_z2.py の資源 la_in と一致）
  constant LA_INPUT_PINS : natural := 6;

  -- クロストリガの出力の端子の候補 0〜3（pmod1 の番号。fprobe_gw/boards/pynq_z2.py の output_pins と一致）
  type xout_pin_array_t is array (0 to 3) of natural range 0 to 7;
  constant XOUT_PIN_CHOICES : xout_pin_array_t := (4, 5, 6, 7);   -- 予備、UART RX、SWO、UART TX

  -- UART の RTS/CTS の端子の候補 0・1（uart_flow_pins と一致）
  type uart_flow_pin_array_t is array (0 to 1) of natural range 0 to 7;
  constant UART_FLOW_PIN_CHOICES : uart_flow_pin_array_t := (4, 1);   -- 予備、TDI

  -- xilinx_la_iserdes（ISERDESE2、NETWORKING・DDR）の語の最も新しいレーンが、その語の出た CLKDIV の立ち上がりと同時の CLK の辺
  -- より何辺前のサンプルか（Vivado 2025.2 の UNISIM を xsim で動かした記録 tests/data/iserdes_ref_w*.txt。lanes = 6・8）
  function iserdes_newest_age(lanes : positive) return natural;

  -- Vivado のブロック設計 ps7_bd（ps7_bd.tcl）・ps7_hp_bd（ps7_hp_bd.tcl）が生成する包み。GHDL では使わない
  -- （ps7_bd_wrapper は pynq_z2_fprobe_top・pynq_z2_dut_top が、ps7_hp_bd_wrapper は深いバッファの構成の fprobe_top が使う）
  component ps7_bd_wrapper
    port (
      aclk          : in  std_logic;
      aresetn       : in  std_logic;
      pjtag_tck     : in  std_logic;
      pjtag_tms     : in  std_logic;
      pjtag_tdi     : in  std_logic;
      pjtag_tdo     : out std_logic;
      m_axi_araddr  : out std_logic_vector(31 downto 0);
      m_axi_arprot  : out std_logic_vector(2 downto 0);
      m_axi_arready : in  std_logic;
      m_axi_arvalid : out std_logic;
      m_axi_awaddr  : out std_logic_vector(31 downto 0);
      m_axi_awprot  : out std_logic_vector(2 downto 0);
      m_axi_awready : in  std_logic;
      m_axi_awvalid : out std_logic;
      m_axi_bready  : out std_logic;
      m_axi_bresp   : in  std_logic_vector(1 downto 0);
      m_axi_bvalid  : in  std_logic;
      m_axi_rdata   : in  std_logic_vector(31 downto 0);
      m_axi_rready  : out std_logic;
      m_axi_rresp   : in  std_logic_vector(1 downto 0);
      m_axi_rvalid  : in  std_logic;
      m_axi_wdata   : out std_logic_vector(31 downto 0);
      m_axi_wready  : in  std_logic;
      m_axi_wstrb   : out std_logic_vector(3 downto 0);
      m_axi_wvalid  : out std_logic
    );
  end component;

  component ps7_hp_bd_wrapper
    port (
      aclk          : in  std_logic;
      aresetn       : in  std_logic;
      pjtag_tck     : in  std_logic;
      pjtag_tms     : in  std_logic;
      pjtag_tdi     : in  std_logic;
      pjtag_tdo     : out std_logic;
      m_axi_araddr  : out std_logic_vector(31 downto 0);
      m_axi_arprot  : out std_logic_vector(2 downto 0);
      m_axi_arready : in  std_logic;
      m_axi_arvalid : out std_logic;
      m_axi_awaddr  : out std_logic_vector(31 downto 0);
      m_axi_awprot  : out std_logic_vector(2 downto 0);
      m_axi_awready : in  std_logic;
      m_axi_awvalid : out std_logic;
      m_axi_bready  : out std_logic;
      m_axi_bresp   : in  std_logic_vector(1 downto 0);
      m_axi_bvalid  : in  std_logic;
      m_axi_rdata   : in  std_logic_vector(31 downto 0);
      m_axi_rready  : out std_logic;
      m_axi_rresp   : in  std_logic_vector(1 downto 0);
      m_axi_rvalid  : in  std_logic;
      m_axi_wdata   : out std_logic_vector(31 downto 0);
      m_axi_wready  : in  std_logic;
      m_axi_wstrb   : out std_logic_vector(3 downto 0);
      m_axi_wvalid  : out std_logic;
      s_axi_hp0_araddr   : in  std_logic_vector(31 downto 0);
      s_axi_hp0_arburst  : in  std_logic_vector(1 downto 0);
      s_axi_hp0_arcache  : in  std_logic_vector(3 downto 0);
      s_axi_hp0_arid     : in  std_logic_vector(5 downto 0);
      s_axi_hp0_arlen    : in  std_logic_vector(3 downto 0);
      s_axi_hp0_arlock   : in  std_logic_vector(1 downto 0);
      s_axi_hp0_arprot   : in  std_logic_vector(2 downto 0);
      s_axi_hp0_arqos    : in  std_logic_vector(3 downto 0);
      s_axi_hp0_arready  : out std_logic;
      s_axi_hp0_arsize   : in  std_logic_vector(2 downto 0);
      s_axi_hp0_arvalid  : in  std_logic;
      s_axi_hp0_awaddr   : in  std_logic_vector(31 downto 0);
      s_axi_hp0_awburst  : in  std_logic_vector(1 downto 0);
      s_axi_hp0_awcache  : in  std_logic_vector(3 downto 0);
      s_axi_hp0_awid     : in  std_logic_vector(5 downto 0);
      s_axi_hp0_awlen    : in  std_logic_vector(3 downto 0);
      s_axi_hp0_awlock   : in  std_logic_vector(1 downto 0);
      s_axi_hp0_awprot   : in  std_logic_vector(2 downto 0);
      s_axi_hp0_awqos    : in  std_logic_vector(3 downto 0);
      s_axi_hp0_awready  : out std_logic;
      s_axi_hp0_awsize   : in  std_logic_vector(2 downto 0);
      s_axi_hp0_awvalid  : in  std_logic;
      s_axi_hp0_bid      : out std_logic_vector(5 downto 0);
      s_axi_hp0_bready   : in  std_logic;
      s_axi_hp0_bresp    : out std_logic_vector(1 downto 0);
      s_axi_hp0_bvalid   : out std_logic;
      s_axi_hp0_rdata    : out std_logic_vector(63 downto 0);
      s_axi_hp0_rid      : out std_logic_vector(5 downto 0);
      s_axi_hp0_rlast    : out std_logic;
      s_axi_hp0_rready   : in  std_logic;
      s_axi_hp0_rresp    : out std_logic_vector(1 downto 0);
      s_axi_hp0_rvalid   : out std_logic;
      s_axi_hp0_wdata    : in  std_logic_vector(63 downto 0);
      s_axi_hp0_wid      : in  std_logic_vector(5 downto 0);
      s_axi_hp0_wlast    : in  std_logic;
      s_axi_hp0_wready   : out std_logic;
      s_axi_hp0_wstrb    : in  std_logic_vector(7 downto 0);
      s_axi_hp0_wvalid   : in  std_logic
    );
  end component;

end package;

package body pynq_z2_pkg is
  function mmcm_cap_div(ratio : natural) return natural is
    constant OUT_DIV : natural := integer(MMCM_OUT_DIV);
  begin
    if ratio <= 1 then
      return 0;
    end if;
    assert OUT_DIV mod ratio = 0 report "CAP_RATIO does not divide the MMCM's sys divider" severity failure;
    return OUT_DIV / ratio;
  end function;

  function iserdes_newest_age(lanes : positive) return natural is
  begin
    return lanes - 3;
  end function;
end package body;
