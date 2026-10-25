-- SPDX-License-Identifier: Apache-2.0
-- fprobe_obs の例: Xilinx 7 系（Zynq-7000・Artix-7・Kintex-7 等）で、BSCANE2 のユーザーの DR に obs_core をつなぐ（2 クロック版）。
-- 観測する値（GROUPS = 2）: 組 0 = 32 ビットのカウンタの 31〜16 ビット、組 1 = 15〜0 ビット。実際の設計では probes に
-- 状態機械の状態・フラグ・カウンタ・レジスタを束ねる（16 ビット × GROUPS。SELECT で 1 組ずつ読む）。
--
-- JTAG: FPGA の標準の JTAG の鎖（USB-JTAG 等）にそのまま載る。fprobe の JTAG の口をその鎖につなぎ、fpga_obs.open の chain に
-- ir_len（7 系は 6 ビット。鎖に他の装置があれば ir_before・ir_after・dr_before・dr_after）と、USER の命令を渡す
-- （BSCANE2 の JTAG_CHAIN = 1 は USER1 = IR 0x02）。ベンダーの ILA（同じ BSCANE2 の USER 命令を使う）と共存するときは
-- JTAG_CHAIN を別にする（2、3、4）。
--
-- タイミングの制約（Vivado）: TCK と clk は非同期なので、別のグループにする。
--   set_clock_groups -asynchronous -group [get_clocks -of_objects [get_pins bscan_i/TCK]] -group [get_clocks clk]
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
library unisim;
use unisim.vcomponents.all;

entity xilinx_obs_counter_top is
  port (
    clk : in  std_logic;                     -- 対象の回路のクロック（例 100 MHz）
    led : out std_logic_vector(3 downto 0)   -- 動いている印（カウンタの上位）
  );
end entity;

architecture rtl of xilinx_obs_counter_top is
  signal por     : unsigned(3 downto 0) := (others => '0');   -- 構成の直後のリセット
  signal rst     : std_logic;
  signal counter : unsigned(31 downto 0) := (others => '0');
  signal probes  : std_logic_vector(31 downto 0);
  signal tck_raw, tck, tdi, tdo, sel, capture, shift, update, tlr : std_logic;
begin
  process (clk)
  begin
    if rising_edge(clk) then
      if por /= 15 then
        por <= por + 1;
      end if;
      counter <= counter + 1;
    end if;
  end process;
  rst <= '1' when por /= 15 else '0';
  probes <= std_logic_vector(counter);
  led <= std_logic_vector(counter(31 downto 28));

  bscan_i : BSCANE2
    generic map (JTAG_CHAIN => 1)
    port map (TCK => tck_raw, TDI => tdi, TDO => tdo, SEL => sel, CAPTURE => capture, SHIFT => shift,
              UPDATE => update, RESET => tlr, DRCK => open, RUNTEST => open, TMS => open);
  tck_buf : BUFG port map (I => tck_raw, O => tck);

  obs_i : entity work.obs_core
    generic map (GROUPS => 2, FIFO_DEPTH => 512, CLOCK_HZ => 100_000_000, ONE_CLOCK => false)
    port map (clk => clk, rst => rst, probes => probes, tck => tck, tdi => tdi, tdo => tdo,
              sel => sel, capture => capture, shift => shift, update => update, tlr => tlr);
end architecture;
