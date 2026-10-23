-- obs_soft_tap + obs_core（対象側の観測の回路をふつうの端子の JTAG で受ける形）。
library ieee;
use ieee.std_logic_1164.all;

entity obs_tap_harness is
    port (
        clk    : in  std_logic;
        rst    : in  std_logic;
        probes : in  std_logic_vector(15 downto 0);
        tck    : in  std_logic;
        tms    : in  std_logic;
        tdi    : in  std_logic;
        tdo    : out std_logic;
        tdo_oe : out std_logic
    );
end entity;

architecture sim of obs_tap_harness is
    signal u_tdi, u_tdo, u_sel, u_capture, u_shift, u_update, u_tlr : std_logic;
begin
    tap : entity work.obs_soft_tap
        port map (tck => tck, tms => tms, tdi => tdi, tdo => tdo, tdo_oe => tdo_oe,
                  user_tdi => u_tdi, user_tdo => u_tdo, user_sel => u_sel, user_capture => u_capture,
                  user_shift => u_shift, user_update => u_update, user_tlr => u_tlr);

    core : entity work.obs_core
        generic map (GROUPS => 1, FIFO_DEPTH => 16)
        port map (clk => clk, rst => rst, probes => probes, tck => tck, tdi => u_tdi, tdo => u_tdo,
                  sel => u_sel, capture => u_capture, shift => u_shift, update => u_update, tlr => u_tlr);
end architecture;
