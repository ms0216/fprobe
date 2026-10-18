-- テスト専用: swd_phy と SWDIO の共有線（ホストとターゲットモデルの出力を解決し、プルアップ）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity swd_phy_harness is
  port (
    clk              : in  std_logic;
    rst              : in  std_logic;
    enable           : in  std_logic;
    half_period      : in  unsigned(15 downto 0);
    turnaround       : in  natural range 1 to 4;
    data_phase       : in  std_logic;
    idle_cycles      : in  natural range 0 to 255;
    sample_delay     : in  natural range 0 to 3;
    cmd_valid        : in  std_logic;
    cmd_ready        : out std_logic;
    cmd_op           : in  std_logic_vector(1 downto 0);
    cmd_count        : in  natural range 1 to 64;
    cmd_request      : in  std_logic_vector(3 downto 0);
    cmd_data         : in  std_logic_vector(63 downto 0);
    rsp_valid        : out std_logic;
    rsp_ack          : out std_logic_vector(2 downto 0);
    rsp_data         : out std_logic_vector(63 downto 0);
    rsp_parity_error : out std_logic;
    -- ターゲットモデル側
    swclk            : out std_logic;
    swdio            : out std_logic;           -- 共有線の値（'X' は衝突）
    target_swdio     : in  std_logic            -- モデルの出力（'Z' で解放）
  );
end entity;

architecture rtl of swd_phy_harness is
  signal sclk, sclk_oe, sdo, sdo_oe : std_logic;
  signal line, sdi : std_logic;
begin
  phy_i : entity work.swd_phy
    port map (clk => clk, rst => rst, enable => enable, half_period => half_period,
              turnaround => turnaround, data_phase => data_phase, idle_cycles => idle_cycles,
              sample_delay => sample_delay, cmd_valid => cmd_valid, cmd_ready => cmd_ready, cmd_op => cmd_op, cmd_count => cmd_count,
              cmd_request => cmd_request, cmd_data => cmd_data, rsp_valid => rsp_valid,
              rsp_ack => rsp_ack, rsp_data => rsp_data, rsp_parity_error => rsp_parity_error,
              swclk => sclk, swclk_oe => sclk_oe, swdio_o => sdo, swdio_oe => sdo_oe,
              swdio_i => sdi);

  line <= sdo when sdo_oe = '1' else 'Z';
  line <= target_swdio;
  line <= 'H';
  sdi   <= to_x01(line);
  swdio <= sdi;
  swclk <= sclk when sclk_oe = '1' else '1';        -- 解放時はプルアップ
end architecture;
