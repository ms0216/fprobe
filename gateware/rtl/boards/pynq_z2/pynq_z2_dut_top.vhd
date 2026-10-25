-- PYNQ-Z2 の DUT 回路（docs/superpowers/specs/2026-10-24-zynq-dut-environment-design.md）: Primer の fprobe が外から
-- デバッグする側。PS（Linux のアプリ）が AXI-Lite（0x40000000 から 4 KB）でサンプル生成 + FIFO を読み、注目信号 dbg を
-- PMOD JB に出す（fprobe のロジアナが見る）。PS の DAP の PJTAG（EMIO）は Raspberry Pi のヘッダの端子へ素通し
-- （independent の起動のときだけ DAP がつながる。fprobe の JTAG をジャンパ線で受ける）。
--
--   sys_clk_in（125 MHz）→ MMCM → sys_clk（100 MHz）。PS の M_AXI_GP0 の ACLK も sys_clk（ps7_bd）なのでクロックまたぎは無い。
--   LED（アクティブ High）: led(0) = sys の心拍、led(1) = MMCM のロック、led(2) = 生成の有効、led(3) = 消灯
--   PMOD JA など使わない端子はポートに持たない（Xilinx の既定で弱い引き下げ = ターゲットの線を動かさない）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.pynq_z2_pkg.all;

entity pynq_z2_dut_top is
  port (
    sys_clk_in   : in  std_logic;
    dut_dbg      : out std_logic_vector(5 downto 0);   -- PMOD JB
    rpi_pin      : inout std_logic_vector(15 downto 0); -- RPi ヘッダのピン診断（GPIO12〜27）
    ps_pjtag_tck : in  std_logic;
    ps_pjtag_tms : in  std_logic;
    ps_pjtag_tdi : in  std_logic;
    ps_pjtag_tdo : out std_logic;
    led          : out std_logic_vector(3 downto 0)
  );
end entity;

architecture rtl of pynq_z2_dut_top is
  -- Vivado のブロック設計 ps7_bd（ps7_bd.tcl）が生成する包み。GHDL では使わない
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

  signal sys_clk, sys_rst, mmcm_lock, aresetn : std_logic;
  signal araddr, awaddr, rdata, wdata : std_logic_vector(31 downto 0);
  signal arprot, awprot : std_logic_vector(2 downto 0);
  signal arready, arvalid, awready, awvalid, bready, bvalid, rready, rvalid, wready, wvalid : std_logic;
  signal bresp, rresp : std_logic_vector(1 downto 0);
  signal wstrb : std_logic_vector(3 downto 0);
  signal dbg : std_logic_vector(5 downto 0);
  signal heartbeat : unsigned(25 downto 0);
begin
  clock_i : entity work.xilinx_mmcm
    generic map (CLKIN_PERIOD_NS => MMCM_CLKIN_PERIOD_NS, MULT => MMCM_MULT, DIVCLK => MMCM_DIVCLK,
                 OUT_DIV => MMCM_OUT_DIV)
    port map (clkin => sys_clk_in, clkout => sys_clk, lock => mmcm_lock);
  sys_reset_i : entity work.reset_sync
    port map (clk => sys_clk, arst_in => not mmcm_lock, rst => sys_rst);
  aresetn <= not sys_rst;

  ps_i : ps7_bd_wrapper
    port map (aclk => sys_clk, aresetn => aresetn,
              pjtag_tck => ps_pjtag_tck, pjtag_tms => ps_pjtag_tms, pjtag_tdi => ps_pjtag_tdi,
              pjtag_tdo => ps_pjtag_tdo,
              m_axi_araddr => araddr, m_axi_arprot => arprot, m_axi_arready => arready, m_axi_arvalid => arvalid,
              m_axi_awaddr => awaddr, m_axi_awprot => awprot, m_axi_awready => awready, m_axi_awvalid => awvalid,
              m_axi_bready => bready, m_axi_bresp => bresp, m_axi_bvalid => bvalid,
              m_axi_rdata => rdata, m_axi_rready => rready, m_axi_rresp => rresp, m_axi_rvalid => rvalid,
              m_axi_wdata => wdata, m_axi_wready => wready, m_axi_wstrb => wstrb, m_axi_wvalid => wvalid);

  core_i : entity work.dut_axil_core
    port map (clk => sys_clk, rst => sys_rst,
              s_axi_awaddr => awaddr, s_axi_awvalid => awvalid, s_axi_awready => awready,
              s_axi_wdata => wdata, s_axi_wstrb => wstrb, s_axi_wvalid => wvalid, s_axi_wready => wready,
              s_axi_bresp => bresp, s_axi_bvalid => bvalid, s_axi_bready => bready,
              s_axi_araddr => araddr, s_axi_arvalid => arvalid, s_axi_arready => arready,
              s_axi_rdata => rdata, s_axi_rresp => rresp, s_axi_rvalid => rvalid, s_axi_rready => rready,
              dbg => dbg, pins => rpi_pin);

  dut_dbg <= dbg;

  process (sys_clk)
  begin
    if rising_edge(sys_clk) then
      if sys_rst = '1' then
        heartbeat <= (others => '0');
      else
        heartbeat <= heartbeat + 1;
      end if;
    end if;
  end process;

  led <= '0' & dbg(5) & mmcm_lock & heartbeat(heartbeat'high);
end architecture;
