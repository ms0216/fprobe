-- 受信したフレームの UDP の見出しを読み、宛先ポートが PORT_NUM のとき payload（ch | seq | data）を frame_mux の
-- 受信バッファ（buf_*。番地 0 = ch、1〜 = data。seq は捨てる）に書く。
--   sel      : IPv4 で、プロトコルが UDP（eth_rx_header の etype・ipv4_rx の proto。payload の位置では確定している）
--   ip_total : IP の全長（ipv4_rx の total）
--   ok       : sel、宛先ポートが PORT_NUM、UDP の長さが ch + seq 以上・MAX_PAYLOAD + ch + seq 以下で、IP の全長に収まる。
--              フレームの最後のバイトの周期に見る
--   src_port : 送信元のポート
--   data_len : バッファに書いた ch + data のバイト数（ok のときに意味を持つ）
-- valid の周期に data を pos（eth_rx_header）の位置のバイトとして受ける。各欄はその位置を受けた次の周期から読める。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.eth_pkg.all;

entity udp_rx is
  generic (
    PORT_NUM    : natural := 63920;
    MAX_PAYLOAD : positive := 512;                   -- data の最大バイト数
    RAM_BITS    : positive := 10                     -- frame_mux のバッファの番地幅
  );
  port (
    clk       : in  std_logic;
    valid     : in  std_logic;
    data      : in  byte_t;
    pos       : in  frame_pos_t;
    sel       : in  std_logic;
    ip_total  : in  unsigned(15 downto 0);
    ok        : out std_logic;
    src_port  : out std_logic_vector(15 downto 0);
    data_len  : out unsigned(15 downto 0);
    buf_we    : out std_logic;
    buf_waddr : out unsigned(RAM_BITS - 1 downto 0);
    buf_wdata : out byte_t
  );
end entity;

architecture rtl of udp_rx is
  constant PORT_V : std_logic_vector(15 downto 0) := std_logic_vector(to_unsigned(PORT_NUM, 16));
  constant CH_SEQ_LEN : natural := 2;                -- payload の先頭の ch と seq
  signal src_r : std_logic_vector(15 downto 0) := (others => '0');
  signal dst_r : std_logic_vector(15 downto 0) := (others => '0');
  signal len_r : unsigned(15 downto 0) := (others => '0');
  signal mine  : boolean;                            -- sel で、宛先ポートが PORT_NUM
  signal we_r  : std_logic := '0';
  signal wa_r  : unsigned(RAM_BITS - 1 downto 0) := (others => '0');
  signal wd_r  : byte_t := (others => '0');
begin
  mine      <= sel = '1' and dst_r = PORT_V;
  src_port  <= src_r;
  data_len  <= len_r - (UDP_HDR_LEN + 1);            -- ch + data（seq を除く）
  buf_we    <= we_r;
  buf_waddr <= wa_r;
  buf_wdata <= wd_r;
  ok        <= '1' when mine and to_integer(len_r) >= UDP_HDR_LEN + CH_SEQ_LEN
                        and to_integer(len_r) <= MAX_PAYLOAD + UDP_HDR_LEN + CH_SEQ_LEN
                        and to_integer(len_r) + IP_HDR_LEN <= to_integer(ip_total) else '0';

  process (clk)
    variable n  : natural range 0 to FRAME_POS_MAX;
    variable pl : natural range 0 to FRAME_POS_MAX;  -- payload（ch | seq | data）の中の位置
  begin
    if rising_edge(clk) then
      we_r <= '0';
      if valid = '1' then
        n := to_integer(pos);
        if n = UDP_SRC_POS then
          src_r(15 downto 8) <= data;
        elsif n = UDP_SRC_POS + 1 then
          src_r(7 downto 0) <= data;
        elsif n = UDP_DST_POS then
          dst_r(15 downto 8) <= data;
        elsif n = UDP_DST_POS + 1 then
          dst_r(7 downto 0) <= data;
        elsif n = UDP_LEN_POS then
          len_r(15 downto 8) <= unsigned(data);
        elsif n = UDP_LEN_POS + 1 then
          len_r(7 downto 0) <= unsigned(data);
        end if;
        -- payload の 0 = ch、1 = seq は捨てる、2〜 = data
        if n >= UDP_PAYLOAD_POS and mine then
          pl := n - UDP_PAYLOAD_POS;
          if pl + UDP_HDR_LEN < to_integer(len_r) and pl <= MAX_PAYLOAD + 1 and pl /= 1 then
            we_r <= '1';
            wa_r <= to_unsigned(pl - 1, RAM_BITS) when pl /= 0 else (others => '0');
            wd_r <= data;
          end if;
        end if;
      end if;
    end if;
  end process;
end architecture;
