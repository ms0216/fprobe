-- 受信したフレームの Ethernet の見出し（宛先 MAC・送信元 MAC・EtherType）を読み、各バイトのフレーム内の位置を数える。
-- 受信の各層（arp_rx・ipv4_rx・icmp_rx・udp_rx）は、この pos で自分の欄を選ぶ。
--   valid の周期に data を pos の位置のバイトとして受ける（pos は 0 から数え、FRAME_POS_MAX で止まる）。last のバイトの
--   次は 0 に戻る。各欄はその位置のバイトを受けた次の周期から読める（次のフレームの同じ位置まで保つ）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.eth_pkg.all;

entity eth_rx_header is
  generic (
    MAC : std_logic_vector(47 downto 0)
  );
  port (
    clk     : in  std_logic;
    rst     : in  std_logic;
    valid   : in  std_logic;                         -- data を受けた
    data    : in  byte_t;
    last    : in  std_logic;                         -- フレームの最後のバイト
    pos     : out frame_pos_t;                       -- data のフレーム内の位置
    dst_me  : out std_logic;                         -- 宛先 MAC が自分
    dst_bc  : out std_logic;                         -- 宛先 MAC がブロードキャスト
    src_mac : out std_logic_vector(47 downto 0);
    etype   : out std_logic_vector(15 downto 0)
  );
end entity;

architecture rtl of eth_rx_header is
  signal i      : natural range 0 to FRAME_POS_MAX := 0;
  signal me, bc : std_logic := '1';
  signal src_r  : std_logic_vector(47 downto 0) := (others => '0');
  signal type_r : std_logic_vector(15 downto 0) := (others => '0');
begin
  pos     <= to_unsigned(i, FRAME_POS_BITS);
  dst_me  <= me;
  dst_bc  <= bc;
  src_mac <= src_r;
  etype   <= type_r;

  process (clk)
    variable n : natural range 0 to FRAME_POS_MAX;
  begin
    if rising_edge(clk) then
      if rst = '1' then
        i <= 0;
      elsif valid = '1' then
        n := i;
        if last = '1' then
          i <= 0;
        elsif i < FRAME_POS_MAX then
          i <= i + 1;
        end if;
        if n < ETH_SRC_POS then
          if n = ETH_DST_POS then
            me <= '1' when data = mac_byte(MAC, 0) else '0';
            bc <= '1' when data = x"FF" else '0';
          else
            if data /= mac_byte(MAC, n - ETH_DST_POS) then
              me <= '0';
            end if;
            if data /= x"FF" then
              bc <= '0';
            end if;
          end if;
        elsif n < ETH_TYPE_POS then
          src_r <= src_r(39 downto 0) & data;
        elsif n < ETH_PAYLOAD_POS then
          type_r <= type_r(7 downto 0) & data;
        end if;
      end if;
    end if;
  end process;
end architecture;
