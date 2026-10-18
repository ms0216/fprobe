-- 補助マイコン（Raspberry Pi Pico）からの計測値の受け口（clk のドメイン）。
-- 仕様: docs/superpowers/specs/2026-10-17-m11-aux-pico-design.md
--
-- UART（8N1、BAUD）で 3 バイトの組を受ける: b0 = 1 | ch(7)、b1 = 0 | value[13:7]、b2 = 0 | value[6:0]。
-- 先頭だけ最上位ビットが 1 なので、途中から受け始めても次の組で揃う。ch < CHANNELS の組ごとに sample_valid（1 周期）。
-- 組の途中で先頭が来たら、それまでを捨てて errors に数える（停止ビットの異常も数える）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity aux_link is
  generic (
    CLK_HZ   : positive;
    BAUD     : positive := 1_000_000;
    CHANNELS : positive := 4
  );
  port (
    clk          : in  std_logic;
    rst          : in  std_logic;
    rx           : in  std_logic;
    sample_valid : out std_logic;
    sample_ch    : out natural range 0 to CHANNELS - 1;
    sample_value : out unsigned(13 downto 0);
    errors       : out unsigned(31 downto 0)
  );
end entity;

architecture rtl of aux_link is
  constant DIVISOR_BITS : positive := 16;
  constant DIVISOR : unsigned(DIVISOR_BITS - 1 downto 0) := to_unsigned((CLK_HZ + BAUD / 2) / BAUD, DIVISOR_BITS);
  signal b_valid, frame_error : std_logic;
  signal b_data : std_logic_vector(7 downto 0);
  signal n      : natural range 0 to 2 := 0;           -- 組の中の位置（0 = 先頭待ち）
  signal ch_r   : natural range 0 to 127 := 0;
  signal hi     : std_logic_vector(6 downto 0) := (others => '0');
  signal err_r  : unsigned(31 downto 0) := (others => '0');
  signal sv     : std_logic := '0';
  signal sch    : natural range 0 to CHANNELS - 1 := 0;
  signal sval   : unsigned(13 downto 0) := (others => '0');
begin
  sample_valid <= sv;
  sample_ch    <= sch;
  sample_value <= sval;
  errors       <= err_r;

  rx_i : entity work.uart_rx
    generic map (DIVISOR_BITS => DIVISOR_BITS)
    port map (clk => clk, rst => rst, divisor => DIVISOR, rx => rx, out_valid => b_valid, out_ready => '1',
              out_data => b_data, frame_error => frame_error, overrun => open);

  process (clk)
    variable bad : boolean;
  begin
    if rising_edge(clk) then
      sv  <= '0';
      bad := frame_error = '1';
      if rst = '1' then
        n     <= 0;
        err_r <= (others => '0');
      elsif b_valid = '1' then
        if b_data(7) = '1' then                          -- 先頭
          bad  := bad or n /= 0;
          ch_r <= to_integer(unsigned(b_data(6 downto 0)));
          n    <= 1;
        elsif n = 1 then
          hi <= b_data(6 downto 0);
          n  <= 2;
        elsif n = 2 then
          n <= 0;
          if ch_r < CHANNELS then
            sv   <= '1';
            sch  <= ch_r;
            sval <= unsigned(hi & b_data(6 downto 0));
          end if;
        end if;                                          -- 先頭待ちのデータは捨てる（受け始め）
      end if;
      if bad and rst = '0' then
        err_r <= err_r + 1;
      end if;
    end if;
  end process;
end architecture;
