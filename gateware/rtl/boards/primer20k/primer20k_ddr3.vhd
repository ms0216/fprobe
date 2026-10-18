-- Tang Primer 20K の DDR3（1 Gb x16 = 128 MB、DDR3-800）。Gowin の DDR3 IP（DDR3MI。Gowin IDE 同梱の生成済み
-- 暗号化ネットリスト。fprobe_gw/boards/primer20k.py の説明を参照）を包み、ボード非依存のメモリ I/F（mem_port、rtl/mem/mem_test.vhd の説明を参照）だけを出す。
-- 仕様: docs/superpowers/specs/2026-10-16-m8-ddr3-design.md §2
--
-- mem_port の 1 拍 = 128 ビット = BL8 1 回（ユーザー側 1:4）。番地（拍の番号、23 ビット）は IP の番地（16 ビット語
-- 単位、{rank(1), bank(3), row(14), column(10)}）へ {0, bank, 0 & row(13), column の上位 7 ビット & 000} と並べる
-- （チップの行は 13 ビット。IP は 14 ビットで生成されているので最上位を 0 にする）。
-- 書き込みはコマンドとデータを同じ周期に渡す。mem_clk は IP のユーザー側クロック（memory_clk / 4）。
-- mem_port のコマンドはレジスタスライスを通して IP へ渡す（こちらの論理と IP の中の論理を 1 周期でつながない）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity primer20k_ddr3 is
  port (
    clk27       : in    std_logic;
    memory_clk  : in    std_logic;                            -- メモリクロック（トップのメモリ用 PLL）
    pll_lock    : in    std_logic;
    rst         : in    std_logic;                            -- 非同期でよい
    -- mem_port（mem_clk のドメイン）
    mem_clk     : out   std_logic;
    mem_rst     : out   std_logic;
    mem_ready   : out   std_logic;                            -- 初期化・較正が済んだ
    cmd_valid   : in    std_logic;
    cmd_ready   : out   std_logic;
    cmd_write   : in    std_logic;
    cmd_addr    : in    unsigned(22 downto 0);
    cmd_data    : in    std_logic_vector(127 downto 0);
    rd_valid    : out   std_logic;
    rd_data     : out   std_logic_vector(127 downto 0);
    -- DDR3
    ddr_addr    : out   std_logic_vector(13 downto 0);
    ddr_ba      : out   std_logic_vector(2 downto 0);
    ddr_cs_n    : out   std_logic;
    ddr_ras_n   : out   std_logic;
    ddr_cas_n   : out   std_logic;
    ddr_we_n    : out   std_logic;
    ddr_ck      : out   std_logic;
    ddr_ck_n    : out   std_logic;
    ddr_cke     : out   std_logic;
    ddr_odt     : out   std_logic;
    ddr_reset_n : out   std_logic;
    ddr_dm      : out   std_logic_vector(1 downto 0);
    ddr_dq      : inout std_logic_vector(15 downto 0);
    ddr_dqs     : inout std_logic_vector(1 downto 0);
    ddr_dqs_n   : inout std_logic_vector(1 downto 0)
  );
end entity;

architecture rtl of primer20k_ddr3 is
  component DDR3MI is                                       -- 生成済みネットリスト（Verilog）
    port (
      clk                 : in    std_logic;
      memory_clk          : in    std_logic;
      pll_lock            : in    std_logic;
      rst_n               : in    std_logic;
      app_burst_number    : in    std_logic_vector(5 downto 0);
      clk_out             : out   std_logic;
      ddr_rst             : out   std_logic;
      init_calib_complete : out   std_logic;
      cmd_ready           : out   std_logic;
      cmd                 : in    std_logic_vector(2 downto 0);
      cmd_en              : in    std_logic;
      addr                : in    std_logic_vector(27 downto 0);
      wr_data_rdy         : out   std_logic;
      wr_data             : in    std_logic_vector(127 downto 0);
      wr_data_en          : in    std_logic;
      wr_data_end         : in    std_logic;
      wr_data_mask        : in    std_logic_vector(15 downto 0);
      rd_data             : out   std_logic_vector(127 downto 0);
      rd_data_valid       : out   std_logic;
      rd_data_end         : out   std_logic;
      sr_req              : in    std_logic;
      ref_req             : in    std_logic;
      sr_ack              : out   std_logic;
      ref_ack             : out   std_logic;
      burst               : in    std_logic;
      O_ddr_addr          : out   std_logic_vector(13 downto 0);
      O_ddr_ba            : out   std_logic_vector(2 downto 0);
      O_ddr_cs_n          : out   std_logic;
      O_ddr_ras_n         : out   std_logic;
      O_ddr_cas_n         : out   std_logic;
      O_ddr_we_n          : out   std_logic;
      O_ddr_clk           : out   std_logic;
      O_ddr_clk_n         : out   std_logic;
      O_ddr_cke           : out   std_logic;
      O_ddr_odt           : out   std_logic;
      O_ddr_reset_n       : out   std_logic;
      O_ddr_dqm           : out   std_logic_vector(1 downto 0);
      IO_ddr_dq           : inout std_logic_vector(15 downto 0);
      IO_ddr_dqs          : inout std_logic_vector(1 downto 0);
      IO_ddr_dqs_n        : inout std_logic_vector(1 downto 0)
    );
  end component;

  signal rst_n, clk_out, ddr_rst, calib, mem_rst_i : std_logic;
  signal ctl_cmd_ready, wr_data_rdy, fire, write_fire : std_logic;
  signal ready_i : std_logic;
  signal ip_addr : std_logic_vector(27 downto 0);
  signal ip_cmd  : std_logic_vector(2 downto 0);
  signal s_valid, s_ready, s_write : std_logic;
  signal s_addr : unsigned(22 downto 0);
  signal s_data : std_logic_vector(127 downto 0);
  signal s_in, s_out : std_logic_vector(151 downto 0);
  constant NO_MASK : std_logic_vector(15 downto 0) := (others => '0');
begin
  rst_n <= not rst;

  s_in <= cmd_write & std_logic_vector(cmd_addr) & cmd_data;
  slice_i : entity work.stream_slice
    generic map (WIDTH => 152)
    port map (clk => clk_out, rst => mem_rst_i, in_valid => cmd_valid, in_ready => cmd_ready, in_data => s_in,
              out_valid => s_valid, out_ready => s_ready, out_data => s_out);
  s_write <= s_out(151);
  s_addr  <= unsigned(s_out(150 downto 128));
  s_data  <= s_out(127 downto 0);

  ip_addr <= '0' & std_logic_vector(s_addr(22 downto 20)) & '0' & std_logic_vector(s_addr(19 downto 0)) & "000";

  ip_cmd     <= "00" & not s_write;                        -- 0 = 書き込み、1 = 読み出し
  ready_i    <= ctl_cmd_ready and (wr_data_rdy or not s_write) and calib;
  s_ready    <= ready_i;
  fire       <= s_valid and ready_i;
  write_fire <= fire and s_write;

  controller_i : component DDR3MI
    port map (
      clk => clk27, memory_clk => memory_clk, pll_lock => pll_lock, rst_n => rst_n, app_burst_number => "000000",           -- 1 コマンド = BL8 1 回
      clk_out => clk_out, ddr_rst => ddr_rst, init_calib_complete => calib,
      cmd_ready => ctl_cmd_ready, cmd => ip_cmd, cmd_en => fire, addr => ip_addr,
      wr_data_rdy => wr_data_rdy, wr_data => s_data, wr_data_en => write_fire, wr_data_end => write_fire,
      wr_data_mask => NO_MASK,
      rd_data => rd_data, rd_data_valid => rd_valid, rd_data_end => open,
      sr_req => '0', ref_req => '0', sr_ack => open, ref_ack => open, burst => '1',
      O_ddr_addr => ddr_addr, O_ddr_ba => ddr_ba, O_ddr_cs_n => ddr_cs_n, O_ddr_ras_n => ddr_ras_n,
      O_ddr_cas_n => ddr_cas_n, O_ddr_we_n => ddr_we_n, O_ddr_clk => ddr_ck, O_ddr_clk_n => ddr_ck_n,
      O_ddr_cke => ddr_cke, O_ddr_odt => ddr_odt, O_ddr_reset_n => ddr_reset_n, O_ddr_dqm => ddr_dm,
      IO_ddr_dq => ddr_dq, IO_ddr_dqs => ddr_dqs, IO_ddr_dqs_n => ddr_dqs_n);

  mem_clk   <= clk_out;
  -- IP のリセット出力は入力クロック側で作られるので、mem_clk に同期化して使う
  mem_reset_i : entity work.reset_sync
    port map (clk => clk_out, arst_in => ddr_rst, rst => mem_rst_i);
  mem_rst   <= mem_rst_i;
  mem_ready <= calib;
end architecture;
