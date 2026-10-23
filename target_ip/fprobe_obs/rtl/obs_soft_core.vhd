-- SPDX-License-Identifier: Apache-2.0
-- fprobe_obs: 利用者の JTAG を持たない FPGA 向けの観測の回路（obs_soft_tap + obs_core をつないだもの）。
-- 対象の回路は probes と clk・rst を渡し、4 本の JTAG のピン（tck・tms・tdi・tdo）を外に出すだけで使える。
-- tdo_oe は TDO を出す間 1（TAP が Shift-IR / Shift-DR。ほかは Hi-Z にして鎖の後ろを妨げない）。
--
-- ONE_CLOCK = true（既定）: TCK・TMS・TDI を clk で標本化し、全部を clk だけで動かす（小さい。TCK のクロックの網も
--   非同期 FIFO も要らない）。TCK は clk の 1/8 以下。false: TCK をクロックとして使う（TCK を速くできる。tck のピンを
--   クロックの網につなぐ）。
library ieee;
use ieee.std_logic_1164.all;

entity obs_soft_core is
    generic (
        IDCODE     : std_logic_vector(31 downto 0) := x"0F0B5001";
        GROUPS     : positive := 1;
        FIFO_DEPTH : positive := 512;
        CLOCK_HZ   : natural  := 0;
        LOST_BITS  : positive := 16;
        ONE_CLOCK  : boolean  := true
    );
    port (
        clk    : in  std_logic;
        rst    : in  std_logic;
        probes : in  std_logic_vector(16 * GROUPS - 1 downto 0);
        tck    : in  std_logic;
        tms    : in  std_logic;
        tdi    : in  std_logic;
        tdo    : out std_logic;
        tdo_oe : out std_logic
    );
end entity;

architecture rtl of obs_soft_core is
    signal u_tdi, u_tdo, u_sel, u_capture, u_shift, u_update, u_tlr : std_logic;
    signal rise, fall, tms_s, tdi_s : std_logic;
begin
    one_g : if ONE_CLOCK generate
        sampler : entity work.obs_tck_sampler
            port map (clk => clk, tck => tck, tms => tms, tdi => tdi, rise => rise, fall => fall,
                      tms_s => tms_s, tdi_s => tdi_s);
        tap : entity work.obs_soft_tap
            generic map (IDCODE => IDCODE, ONE_CLOCK => true)
            port map (tck => clk, tck_ce => rise, tck_fall_ce => fall, tms => tms_s, tdi => tdi_s, tdo => tdo,
                      tdo_oe => tdo_oe, user_tdi => u_tdi, user_tdo => u_tdo, user_sel => u_sel,
                      user_capture => u_capture, user_shift => u_shift, user_update => u_update, user_tlr => u_tlr);
        core : entity work.obs_core
            generic map (GROUPS => GROUPS, FIFO_DEPTH => FIFO_DEPTH, CLOCK_HZ => CLOCK_HZ, LOST_BITS => LOST_BITS,
                         ONE_CLOCK => true)
            port map (clk => clk, rst => rst, probes => probes, tck => clk, tck_ce => rise, tdi => u_tdi,
                      tdo => u_tdo, sel => u_sel, capture => u_capture, shift => u_shift, update => u_update,
                      tlr => u_tlr);
    end generate;

    two_g : if not ONE_CLOCK generate
        tap : entity work.obs_soft_tap
            generic map (IDCODE => IDCODE)
            port map (tck => tck, tms => tms, tdi => tdi, tdo => tdo, tdo_oe => tdo_oe,
                      user_tdi => u_tdi, user_tdo => u_tdo, user_sel => u_sel, user_capture => u_capture,
                      user_shift => u_shift, user_update => u_update, user_tlr => u_tlr);
        core : entity work.obs_core
            generic map (GROUPS => GROUPS, FIFO_DEPTH => FIFO_DEPTH, CLOCK_HZ => CLOCK_HZ, LOST_BITS => LOST_BITS)
            port map (clk => clk, rst => rst, probes => probes, tck => tck, tdi => u_tdi, tdo => u_tdo,
                      sel => u_sel, capture => u_capture, shift => u_shift, update => u_update, tlr => u_tlr);
        rise <= '0';
        fall <= '0';
        tms_s <= '0';
        tdi_s <= '0';
    end generate;
end architecture;
