-- テスト専用: swd_phy と SWDIO の共有線（ホストとターゲットモデルの出力を解決し、プルアップ）。
-- now: リセットから数える周期のカウンタ（タイムベースの代わり。rsp_time の試験が SWCLK の辺と比べる）。
-- pad_stages: ボードの出力の段（IOB の FF。PYNQ-Z2 は 1）の模型。SWCLK・SWDIO の出力と出力の許可を pad_stages 周期遅らせて
--   端子に出す（入力は遅らせない）。往復の遅れが段の分だけ長くなる（docs/boards/pynq-z2.md）。
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
    sample_delay     : in  natural range 0 to 7;
    pad_stages       : in  natural range 0 to 2 := 0;
    cmd_valid        : in  std_logic;
    cmd_ready        : out std_logic;
    cmd_cond         : in  std_logic := '0';
    cmd_op           : in  std_logic_vector(1 downto 0);
    cmd_count        : in  natural range 1 to 64;
    cmd_request      : in  std_logic_vector(3 downto 0);
    cmd_data         : in  std_logic_vector(63 downto 0);
    rsp_valid        : out std_logic;
    rsp_ack          : out std_logic_vector(2 downto 0);
    rsp_data         : out std_logic_vector(63 downto 0);
    rsp_parity_error : out std_logic;
    rsp_time         : out unsigned(63 downto 0);
    now              : out unsigned(63 downto 0);
    -- ターゲットモデル側
    swclk            : out std_logic;
    swdio            : out std_logic;           -- 共有線の値（'X' は衝突）
    target_swdio     : in  std_logic            -- モデルの出力（'Z' で解放）
  );
end entity;

architecture rtl of swd_phy_harness is
  signal sclk, sclk_oe, sdo, sdo_oe : std_logic;
  signal line, sdi : std_logic;
  signal count : unsigned(63 downto 0) := (others => '0');
  type pads_t is array (0 to 2) of std_logic_vector(3 downto 0);   -- sclk, sclk_oe, sdo, sdo_oe
  signal pads : pads_t := (others => "1111");
  signal pad  : std_logic_vector(3 downto 0);
begin
  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        count <= (others => '0');
      else
        count <= count + 1;
      end if;
      pads(1) <= pads(0);
      pads(2) <= pads(1);
    end if;
  end process;
  now <= count;
  pads(0) <= sclk & sclk_oe & sdo & sdo_oe;
  pad <= pads(pad_stages);

  phy_i : entity work.swd_phy
    port map (clk => clk, rst => rst, enable => enable, half_period => half_period,
              turnaround => turnaround, data_phase => data_phase, idle_cycles => idle_cycles,
              sample_delay => sample_delay, now => count, cmd_valid => cmd_valid, cmd_ready => cmd_ready,
              cmd_cond => cmd_cond, cmd_op => cmd_op, cmd_count => cmd_count,
              cmd_request => cmd_request, cmd_data => cmd_data, rsp_valid => rsp_valid,
              rsp_ack => rsp_ack, rsp_data => rsp_data, rsp_parity_error => rsp_parity_error, rsp_time => rsp_time,
              swclk => sclk, swclk_oe => sclk_oe, swdio_o => sdo, swdio_oe => sdo_oe,
              swdio_i => sdi);

  line <= pad(1) when pad(0) = '1' else 'Z';
  line <= target_swdio;
  line <= 'H';
  sdi   <= to_x01(line);
  swdio <= sdi;
  swclk <= pad(3) when pad(2) = '1' else '1';        -- 解放時はプルアップ
end architecture;
