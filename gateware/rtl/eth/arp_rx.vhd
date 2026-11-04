-- 受信したフレームの ARP（IPv4 over Ethernet）の欄を読む。EtherType は見ない（使う側が ETHERTYPE_ARP と照らす）。
--   request: 固定の 8 バイト（HTYPE 1・PTYPE 0x0800・HLEN 6・PLEN 4・OPER 1）が要求の形
--   tpa_me : 目標の IP が自分
--   sha・spa: 送信元の MAC・IP（応答の宛先）
-- valid の周期に data を pos（eth_rx_header）の位置のバイトとして受ける。各欄はその位置を受けた次の周期から読める。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.eth_pkg.all;

entity arp_rx is
  generic (
    IP : std_logic_vector(31 downto 0)
  );
  port (
    clk     : in  std_logic;
    valid   : in  std_logic;
    data    : in  byte_t;
    pos     : in  frame_pos_t;
    request : out std_logic;
    tpa_me  : out std_logic;
    sha     : out std_logic_vector(47 downto 0);
    spa     : out std_logic_vector(31 downto 0)
  );
end entity;

architecture rtl of arp_rx is
  signal request_r : std_logic := '1';
  signal tpa_me_r  : std_logic := '1';
  signal sha_r     : std_logic_vector(47 downto 0) := (others => '0');
  signal spa_r     : std_logic_vector(31 downto 0) := (others => '0');
begin
  request <= request_r;
  tpa_me  <= tpa_me_r;
  sha     <= sha_r;
  spa     <= spa_r;

  process (clk)
    variable n : natural range 0 to FRAME_POS_MAX;
  begin
    if rising_edge(clk) then
      if valid = '1' then
        n := to_integer(pos);
        if n >= ARP_HEAD_POS and n < ARP_SHA_POS then
          if n = ARP_HEAD_POS then
            request_r <= '1' when data = ARP_REQUEST_HEAD(0) else '0';
          elsif data /= ARP_REQUEST_HEAD(n - ARP_HEAD_POS) then
            request_r <= '0';
          end if;
        end if;
        if n >= ARP_SHA_POS and n < ARP_SPA_POS then
          sha_r <= sha_r(39 downto 0) & data;
        end if;
        if n >= ARP_SPA_POS and n < ARP_THA_POS then
          spa_r <= spa_r(23 downto 0) & data;
        end if;
        if n >= ARP_TPA_POS and n < ARP_END_POS then
          if n = ARP_TPA_POS then
            tpa_me_r <= '1' when data = ip_byte(IP, 0) else '0';
          elsif data /= ip_byte(IP, n - ARP_TPA_POS) then
            tpa_me_r <= '0';
          end if;
        end if;
      end if;
    end if;
  end process;
end architecture;
