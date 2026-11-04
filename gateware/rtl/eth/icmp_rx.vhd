-- 受信したフレームの ICMP 部（IP の中身）を読み、echo 応答に使う ICMP 部をバッファ（buf_*。番地 0 = 型）に書く。
--   sel     : IP のプロトコルが ICMP（ipv4_rx の proto。ICMP 部の位置では確定している）
--   hold    : 前の echo 応答を送り終えていない。新しい ICMP 部をバッファに書かない（受けた echo 要求には答えない）
--   ip_total: IP の全長（ipv4_rx の total）
--   echo    : 答えられる echo 要求（sel、型が echo 要求、ICMP 部が 8〜ICMP_MAX バイトで、その全部をバッファに書いた）。
--             フレームの最後のバイトの周期に見る
--   checksum: 要求の ICMP のチェックサム
-- valid の周期に data を pos（eth_rx_header）の位置のバイトとして受ける。各欄はその位置を受けた次の周期から読める。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.util_pkg.all;
use work.eth_pkg.all;

entity icmp_rx is
  generic (
    ICMP_MAX : positive := 128                       -- 答える ICMP 部（見出し 8 + データ）の最大バイト数（2 の冪）
  );
  port (
    clk       : in  std_logic;
    valid     : in  std_logic;
    data      : in  byte_t;
    pos       : in  frame_pos_t;
    sel       : in  std_logic;
    hold      : in  std_logic;
    ip_total  : in  unsigned(15 downto 0);
    echo      : out std_logic;
    checksum  : out std_logic_vector(15 downto 0);
    buf_we    : out std_logic;
    buf_waddr : out unsigned(clog2(ICMP_MAX) - 1 downto 0);
    buf_wdata : out byte_t
  );
end entity;

architecture rtl of icmp_rx is
  constant ICMP_BITS : natural := clog2(ICMP_MAX);
  signal icmp_type : byte_t := (others => '0');
  signal sum_r     : std_logic_vector(15 downto 0) := (others => '0');
  signal writing   : boolean := false;               -- このフレームの ICMP 部をバッファに書いている
  signal we_r      : std_logic := '0';
  signal wa_r      : unsigned(ICMP_BITS - 1 downto 0) := (others => '0');
  signal wd_r      : byte_t := (others => '0');
begin
  checksum  <= sum_r;
  buf_we    <= we_r;
  buf_waddr <= wa_r;
  buf_wdata <= wd_r;
  echo      <= '1' when sel = '1' and icmp_type = ICMP_ECHO_REQUEST and writing and hold = '0'
                        and to_integer(ip_total) <= IP_HDR_LEN + ICMP_MAX
                        and to_integer(ip_total) >= IP_HDR_LEN + 8 else '0';

  process (clk)
    variable n : natural range 0 to FRAME_POS_MAX;
  begin
    if rising_edge(clk) then
      we_r <= '0';
      if valid = '1' then
        n := to_integer(pos);
        if n = ICMP_TYPE_POS then
          icmp_type <= data;
          writing   <= hold = '0';                     -- 応答待ちの間はバッファを上書きしない
        elsif n = ICMP_CHECKSUM_POS then
          sum_r(15 downto 8) <= data;
        elsif n = ICMP_CHECKSUM_POS + 1 then
          sum_r(7 downto 0) <= data;
        end if;
        if n >= ICMP_TYPE_POS and n < ICMP_TYPE_POS + ICMP_MAX and sel = '1'
           and ((n = ICMP_TYPE_POS and hold = '0') or (n > ICMP_TYPE_POS and writing)) then
          we_r <= '1';
          wa_r <= to_unsigned(n - ICMP_TYPE_POS, ICMP_BITS);
          wd_r <= data;
        end if;
      end if;
    end if;
  end process;
end architecture;
