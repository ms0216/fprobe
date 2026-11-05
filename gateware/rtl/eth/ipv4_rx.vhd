-- 受信したフレームの IPv4 の見出し（オプションなし）を読む。EtherType は見ない（使う側が ETHERTYPE_IPV4 と照らす）。
--   ok   : 版 4・IHL 5、分割なし（MF なし・位置 0）、宛先が自分の IP、見出しのチェックサムが正しく、pos のバイトまでで
--          全長（total）を受けている。フレームの最後のバイトの周期（pos = 最後の位置）に見る
--   total・proto・src: 全長・プロトコル・送信元の IP
-- valid の周期に data を pos（eth_rx_header）の位置のバイトとして受ける。各欄はその位置を受けた次の周期から読める。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.eth_pkg.all;

entity ipv4_rx is
  generic (
    IP : std_logic_vector(31 downto 0)
  );
  port (
    clk   : in  std_logic;
    valid : in  std_logic;
    data  : in  byte_t;
    pos   : in  frame_pos_t;
    ok    : out std_logic;
    total : out unsigned(15 downto 0);
    proto : out byte_t;
    src   : out std_logic_vector(31 downto 0)
  );
end entity;

architecture rtl of ipv4_rx is
  signal form_ok  : boolean := true;                 -- 版・長さ・分割の欄
  signal dst_me   : boolean := true;
  signal total_r  : unsigned(15 downto 0) := (others => '0');
  signal proto_r  : byte_t := (others => '0');
  signal src_r    : std_logic_vector(31 downto 0) := (others => '0');
  signal sum      : unsigned(19 downto 0) := (others => '0');   -- 見出しの 16 ビット語の和
  signal hi       : byte_t := (others => '0');                  -- 16 ビット語の上位（偶数番目のバイト）
begin
  total <= total_r;
  proto <= proto_r;
  src   <= src_r;
  ok    <= '1' when form_ok and dst_me and csum_fold(sum) = x"FFFF"
                    and to_integer(pos) + 1 >= ETH_PAYLOAD_POS + to_integer(total_r) else '0';

  process (clk)
    variable n : natural range 0 to FRAME_POS_MAX;
  begin
    if rising_edge(clk) then
      if valid = '1' then
        n := to_integer(pos);
        if n = IP_VER_IHL_POS then
          form_ok <= data = IP_VER_IHL_V4;                     -- 版 4・オプションなし
        elsif n = IP_TOTAL_LEN_POS then
          total_r(15 downto 8) <= unsigned(data);
        elsif n = IP_TOTAL_LEN_POS + 1 then
          total_r(7 downto 0) <= unsigned(data);
        elsif n = IP_FLAGS_POS then
          form_ok <= form_ok and data(5 downto 0) = "000000" and data(7) = '0';   -- MF なし・位置 0
        elsif n = IP_FLAGS_POS + 1 then
          form_ok <= form_ok and data = x"00";
        elsif n = IP_PROTO_POS then
          proto_r <= data;
        end if;
        if n >= IP_SRC_POS and n < IP_DST_POS then
          src_r <= src_r(23 downto 0) & data;
        end if;
        if n >= IP_DST_POS and n < IP_PAYLOAD_POS then
          if n = IP_DST_POS then
            dst_me <= data = ip_byte(IP, 0);
          else
            dst_me <= dst_me and data = ip_byte(IP, n - IP_DST_POS);
          end if;
        end if;
        if n >= ETH_PAYLOAD_POS and n < IP_PAYLOAD_POS then    -- 見出しのチェックサム（1 の補数の和。16 ビットずつ）
          if (n - ETH_PAYLOAD_POS) mod 2 = 0 then
            hi <= data;
          elsif n = ETH_PAYLOAD_POS + 1 then
            sum <= resize(unsigned(std_logic_vector'(hi & data)), 20);
          else
            sum <= sum + unsigned(std_logic_vector'(hi & data));
          end if;
        end if;
      end if;
    end if;
  end process;
end architecture;
