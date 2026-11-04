-- Ethernet の共通の定義（rmii_rx・rmii_tx・udp_stack と受信の各層）: FCS（CRC-32、反射の多項式 0xEDB88320）を RMII の
-- 1 ジビットずつ進める関数、フレームの各欄の位置（フレームの先頭からのバイトの番号。プリアンブル・SFD・FCS を除く）、
-- アドレスのバイトの取り出しと、IP のチェックサム（16 ビットの 1 の補数の和）の折り返し。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

package eth_pkg is

  subtype byte_t is std_logic_vector(7 downto 0);
  type bytes_t is array (natural range <>) of byte_t;

  -- Ethernet II
  constant ETH_DST_POS       : natural := 0;                -- 宛先 MAC（6）
  constant ETH_SRC_POS       : natural := 6;                -- 送信元 MAC（6）
  constant ETH_TYPE_POS      : natural := 12;               -- EtherType（2）
  constant ETH_PAYLOAD_POS   : natural := 14;
  constant ETHERTYPE_IPV4 : std_logic_vector(15 downto 0) := x"0800";
  constant ETHERTYPE_ARP  : std_logic_vector(15 downto 0) := x"0806";
  -- ARP（IPv4 over Ethernet）: 固定の 8 バイト（HTYPE 1・PTYPE 0x0800・HLEN 6・PLEN 4・OPER）の後に SHA・SPA・THA・TPA
  constant ARP_HEAD_POS      : natural := ETH_PAYLOAD_POS;
  constant ARP_REQUEST_HEAD : bytes_t(0 to 7) := (x"00", x"01", x"08", x"00", x"06", x"04", x"00", x"01");
  constant ARP_REPLY_HEAD   : bytes_t(0 to 7) := (x"00", x"01", x"08", x"00", x"06", x"04", x"00", x"02");
  constant ARP_SHA_POS       : natural := ARP_HEAD_POS + 8;     -- 送信元 MAC（6）
  constant ARP_SPA_POS       : natural := ARP_SHA_POS + 6;      -- 送信元 IP（4）
  constant ARP_THA_POS       : natural := ARP_SPA_POS + 4;      -- 目標 MAC（6）
  constant ARP_TPA_POS       : natural := ARP_THA_POS + 6;      -- 目標 IP（4）
  constant ARP_END_POS       : natural := ARP_TPA_POS + 4;
  -- IPv4（オプションなし、IHL = 5）
  constant IP_VER_IHL_POS    : natural := ETH_PAYLOAD_POS;      -- 0x45
  constant IP_TOTAL_LEN_POS  : natural := ETH_PAYLOAD_POS + 2;
  constant IP_FLAGS_POS      : natural := ETH_PAYLOAD_POS + 6;  -- フラグ・断片の位置（2）
  constant IP_TTL_POS        : natural := ETH_PAYLOAD_POS + 8;
  constant IP_PROTO_POS      : natural := ETH_PAYLOAD_POS + 9;
  constant IP_CHECKSUM_POS   : natural := ETH_PAYLOAD_POS + 10;
  constant IP_SRC_POS        : natural := ETH_PAYLOAD_POS + 12;
  constant IP_DST_POS        : natural := ETH_PAYLOAD_POS + 16;
  constant IP_HDR_LEN    : natural := 20;
  constant IP_PAYLOAD_POS    : natural := ETH_PAYLOAD_POS + IP_HDR_LEN;
  constant IP_PROTO_ICMP : byte_t := x"01";
  constant IP_PROTO_UDP  : byte_t := x"11";
  -- UDP・ICMP（IP の中身の先頭から）
  constant UDP_SRC_POS       : natural := IP_PAYLOAD_POS;
  constant UDP_DST_POS       : natural := IP_PAYLOAD_POS + 2;
  constant UDP_LEN_POS       : natural := IP_PAYLOAD_POS + 4;
  constant UDP_CHECKSUM_POS  : natural := IP_PAYLOAD_POS + 6;
  constant UDP_HDR_LEN   : natural := 8;
  constant UDP_PAYLOAD_POS   : natural := IP_PAYLOAD_POS + UDP_HDR_LEN;
  constant ICMP_TYPE_POS     : natural := IP_PAYLOAD_POS;
  constant ICMP_CHECKSUM_POS : natural := IP_PAYLOAD_POS + 2;
  constant ICMP_ECHO_REQUEST : byte_t := x"08";

  -- 受信したフレームの中のバイトの位置（eth_rx_header が数える。FRAME_POS_MAX で止まる）
  constant FRAME_POS_BITS : positive := 11;
  constant FRAME_POS_MAX  : natural := 2 ** FRAME_POS_BITS - 1;
  subtype frame_pos_t is unsigned(FRAME_POS_BITS - 1 downto 0);

  -- CRC c に、ジビット d（d(0) が先に線に出るビット）を通した値
  function crc_dibit(c : std_logic_vector(31 downto 0); d : std_logic_vector(1 downto 0)) return std_logic_vector;

  -- MAC アドレス m の i 番目（0 = 線に先に出る上位）のバイト
  function mac_byte(m : std_logic_vector(47 downto 0); i : natural) return byte_t;
  -- IPv4 アドレス a の i 番目（0 = 上位）のバイト
  function ip_byte(a : std_logic_vector(31 downto 0); i : natural) return byte_t;
  -- 16 ビット語の和 s（桁上がりを 4 ビット持つ）を、1 の補数の和の 16 ビットに折り返す
  function csum_fold(s : unsigned(19 downto 0)) return unsigned;

end package;

package body eth_pkg is

  function crc_dibit(c : std_logic_vector(31 downto 0); d : std_logic_vector(1 downto 0)) return std_logic_vector is
    variable x : std_logic_vector(31 downto 0) := c;
  begin
    for i in 0 to 1 loop
      if (x(0) xor d(i)) = '1' then
        x := ('0' & x(31 downto 1)) xor x"EDB88320";
      else
        x := '0' & x(31 downto 1);
      end if;
    end loop;
    return x;
  end function;

  function mac_byte(m : std_logic_vector(47 downto 0); i : natural) return byte_t is
  begin
    return m(47 - 8 * i downto 40 - 8 * i);
  end function;

  function ip_byte(a : std_logic_vector(31 downto 0); i : natural) return byte_t is
  begin
    return a(31 - 8 * i downto 24 - 8 * i);
  end function;

  function csum_fold(s : unsigned(19 downto 0)) return unsigned is
    variable t : unsigned(19 downto 0) := resize(s(15 downto 0), 20) + resize(s(19 downto 16), 20);
  begin
    t := resize(t(15 downto 0), 20) + resize(t(19 downto 16), 20);
    return t(15 downto 0);
  end function;

end package body;
