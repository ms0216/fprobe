-- ARP・ICMP echo・UDP の最小のスタック（clk = sys のドメイン）。フレームの FCS は rmii_rx / rmii_tx が受け持つ。
-- 仕様: docs/superpowers/specs/2026-10-17-m11-ethernet-link-design.md §2
--
-- 受信（in_*: Ethernet のフレーム、最後のバイトに in_last と in_good）:
--   - ARP 要求（自分の IP 宛て）→ ARP 応答を送る。
--   - IPv4（オプションなし・分割なし、ヘッダのチェックサムが正しい、自分の IP 宛て）:
--     - ICMP echo 要求（ICMP 部が ICMP_MAX 以下）→ echo 応答を送る。
--     - UDP（宛先ポート PORT）→ payload の ch | seq | data を frame_mux の受信バッファへ（番地 0 = ch、1〜 = data）。
--       フレームが正しく終わったら commit し、送り主（MAC・IP・ポート）を以後の送信先にする。
--   frame_mux が前のフレームを流している間（rx_busy）は受信を止める（手前の FIFO が受け持つ）。
-- 送信（out_*: Ethernet のフレームの全バイト、続けて start_* に 1 件）: 優先順 ARP 応答 > echo 応答 > frame_mux の
--   フレーム（UDP、seq は通し番号）。送信先がまだ無ければ frame_mux のフレームは捨てる。
--   ヘッダはまとめて hdr に組んでから送り出す（UDP: Ethernet 14 + IP 20 + UDP 8 + ch + seq = 44 バイト、
--   ARP: 42、ICMP: 14 + 20 + ICMP の先頭 4）。続けて本体（frame_mux のバッファ / ICMP のバッファ）を送る。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity udp_stack is
  generic (
    MAC         : std_logic_vector(47 downto 0);
    IP          : std_logic_vector(31 downto 0);
    PORT_NUM    : natural := 63920;
    MAX_PAYLOAD : positive := 512;
    RAM_BITS    : positive := 10;                    -- frame_mux のバッファの番地幅
    ICMP_MAX    : positive := 128                    -- 答える ICMP 部（ヘッダ 8 + データ）の最大バイト数（2 の冪）
  );
  port (
    clk         : in  std_logic;
    rst         : in  std_logic;
    -- 受信したフレーム
    in_valid    : in  std_logic;
    in_ready    : out std_logic;
    in_data     : in  std_logic_vector(7 downto 0);
    in_last     : in  std_logic;
    in_good     : in  std_logic;
    -- 送るフレーム
    out_valid   : out std_logic;
    out_ready   : in  std_logic;
    out_data    : out std_logic_vector(7 downto 0);
    out_last    : out std_logic;
    start_valid : out std_logic;
    start_ready : in  std_logic;
    -- frame_mux
    rx_we       : out std_logic;
    rx_waddr    : out unsigned(RAM_BITS - 1 downto 0);
    rx_wdata    : out std_logic_vector(7 downto 0);
    rx_commit   : out std_logic;
    rx_len      : out natural range 0 to MAX_PAYLOAD + 1;
    rx_busy     : in  std_logic;
    tx_valid    : in  std_logic;
    tx_len      : in  natural range 1 to MAX_PAYLOAD + 1;
    tx_raddr    : out unsigned(RAM_BITS - 1 downto 0);
    tx_rdata    : in  std_logic_vector(7 downto 0);
    tx_done     : out std_logic;
    -- 状態
    peer_known  : out std_logic;                     -- 送信先（PC）が決まっている
    -- 状態（診断用）: [7:4] 送信の状態、[3] ARP 応答待ち、[2] echo 応答待ち、[1] 受信を止めている、[0] 受信の入力あり
    diag        : out std_logic_vector(7 downto 0)
  );
end entity;

architecture rtl of udp_stack is
  constant PORT_V : std_logic_vector(15 downto 0) := std_logic_vector(to_unsigned(PORT_NUM, 16));
  constant ICMP_BITS : natural := 7;
  subtype byte_t is std_logic_vector(7 downto 0);
  type bytes_t is array (natural range <>) of byte_t;

  function mac_byte(m : std_logic_vector(47 downto 0); i : natural) return byte_t is
  begin
    return m(47 - 8 * i downto 40 - 8 * i);
  end function;

  function ip_byte(a : std_logic_vector(31 downto 0); i : natural) return byte_t is
  begin
    return a(31 - 8 * i downto 24 - 8 * i);
  end function;

  -- 16 ビットの 1 の補数の和の折り返し
  function fold(s : unsigned(19 downto 0)) return unsigned is
    variable t : unsigned(19 downto 0) := resize(s(15 downto 0), 20) + resize(s(19 downto 16), 20);
  begin
    t := resize(t(15 downto 0), 20) + resize(t(19 downto 16), 20);
    return t(15 downto 0);
  end function;

  ---------------------------------------------------------------------------------------------
  -- 受信
  ---------------------------------------------------------------------------------------------
  signal i         : natural range 0 to 2047 := 0;   -- フレーム内の位置
  signal dst_me, dst_bc : boolean := true;
  signal etype     : std_logic_vector(15 downto 0) := (others => '0');
  signal src_mac   : std_logic_vector(47 downto 0) := (others => '0');
  -- ARP
  signal arp_ok    : boolean := true;                -- 固定の欄が要求の形か
  signal arp_spa   : std_logic_vector(31 downto 0) := (others => '0');
  signal arp_sha   : std_logic_vector(47 downto 0) := (others => '0');
  signal tpa_me    : boolean := true;
  -- IPv4
  signal ip_ok     : boolean := true;                -- バージョン・長さ・分割の欄
  signal ip_total  : unsigned(15 downto 0) := (others => '0');
  signal ip_proto  : byte_t := (others => '0');
  signal ip_src    : std_logic_vector(31 downto 0) := (others => '0');
  signal ip_dst_me : boolean := true;
  signal ip_sum    : unsigned(19 downto 0) := (others => '0');
  signal ip_hi     : byte_t := (others => '0');      -- 16 ビット語の上位（偶数番目のバイト）
  -- UDP
  signal udp_src   : std_logic_vector(15 downto 0) := (others => '0');
  signal udp_dst   : std_logic_vector(15 downto 0) := (others => '0');
  signal udp_len   : unsigned(15 downto 0) := (others => '0');
  -- ICMP
  signal icmp_type : byte_t := (others => '0');
  signal icmp_w    : boolean := false;               -- ICMP のバッファに書いてよい（応答を送り終えている）
  signal icmp_we   : std_logic := '0';
  signal icmp_wa   : unsigned(ICMP_BITS - 1 downto 0) := (others => '0');
  signal icmp_wd   : byte_t := (others => '0');
  signal icmp_ra   : unsigned(ICMP_BITS - 1 downto 0) := (others => '0');
  signal icmp_q    : byte_t;
  signal rx_we_r   : std_logic := '0';
  signal rx_wa_r   : unsigned(RAM_BITS - 1 downto 0) := (others => '0');
  signal rx_wd_r   : byte_t := (others => '0');
  signal commit_r  : std_logic := '0';
  signal len_r     : natural range 0 to MAX_PAYLOAD + 1 := 0;
  signal take_in   : std_logic;

  -- 応答の依頼（受信 → 送信）
  signal arp_due, icmp_due : boolean := false;
  signal arp_mac_r : std_logic_vector(47 downto 0) := (others => '0');   -- ARP 応答の宛先
  signal arp_ip_r  : std_logic_vector(31 downto 0) := (others => '0');
  signal icmp_mac_r : std_logic_vector(47 downto 0) := (others => '0');  -- echo 応答の宛先
  signal icmp_ip_r : std_logic_vector(31 downto 0) := (others => '0');
  signal icmp_len_r : natural range 0 to ICMP_MAX := 0;
  signal icmp_sum_r : std_logic_vector(15 downto 0) := (others => '0');  -- 要求の ICMP チェックサム
  signal arp_clr, icmp_clr : std_logic := '0';
  -- 送信先（最後に受けた正しい UDP の送り主）
  signal peer_ok   : boolean := false;
  signal peer_mac  : std_logic_vector(47 downto 0) := (others => '0');
  signal peer_ip   : std_logic_vector(31 downto 0) := (others => '0');
  signal peer_port : std_logic_vector(15 downto 0) := (others => '0');

  ---------------------------------------------------------------------------------------------
  -- 送信
  ---------------------------------------------------------------------------------------------
  type tx_state_t is (T_IDLE, T_CH_WAIT, T_CH, T_SUM, T_HDR, T_BODY_ADDR, T_BODY_WAIT, T_BODY, T_START, T_DONE);
  type kind_t is (K_ARP, K_ICMP, K_UDP);
  constant HDR_MAX : positive := 44;
  signal tx_state  : tx_state_t := T_IDLE;
  signal kind      : kind_t := K_UDP;
  signal hdr       : bytes_t(0 to HDR_MAX - 1) := (others => (others => '0'));
  signal hdr_n     : natural range 0 to HDR_MAX := 0;  -- 残りのヘッダのバイト数
  signal body_i    : natural range 0 to MAX_PAYLOAD + 1 := 0;
  signal body_end  : natural range 0 to MAX_PAYLOAD + 1 := 0;
  signal dst_mac_t : std_logic_vector(47 downto 0) := (others => '0');
  signal dst_ip_t  : std_logic_vector(31 downto 0) := (others => '0');
  signal ip_len_t  : unsigned(15 downto 0) := (others => '0');
  signal proto_t   : byte_t := (others => '0');
  signal sum_t     : unsigned(19 downto 0) := (others => '0');
  signal ch_t      : byte_t := (others => '0');
  signal seq       : unsigned(7 downto 0) := (others => '0');
  signal ov        : std_logic := '0';
  signal od        : byte_t := (others => '0');
  signal ol        : std_logic := '0';
  signal sv        : std_logic := '0';
  signal done_r    : std_logic := '0';
  signal ra        : unsigned(RAM_BITS - 1 downto 0) := (others => '0');
begin
  rx_we      <= rx_we_r;
  rx_waddr   <= rx_wa_r;
  rx_wdata   <= rx_wd_r;
  rx_commit  <= commit_r;
  rx_len     <= len_r;
  out_valid  <= ov;
  out_data   <= od;
  out_last   <= ol;
  start_valid <= sv;
  tx_done    <= done_r;
  tx_raddr   <= ra;
  peer_known <= '1' when peer_ok else '0';
  process (all)
  begin
    diag <= std_logic_vector(to_unsigned(tx_state_t'pos(tx_state), 4)) & "00" & (rx_busy or commit_r) & in_valid;
    if arp_due then
      diag(3) <= '1';
    end if;
    if icmp_due then
      diag(2) <= '1';
    end if;
  end process;

  take_in  <= in_valid and not rx_busy and not commit_r;
  in_ready <= take_in;

  process (clk)
    variable b  : byte_t;
    variable n  : natural range 0 to 2047;
    variable pl : natural range 0 to 2047;            -- UDP の payload（ch | seq | data）の位置
    variable fl : natural;
  begin
    if rising_edge(clk) then
      rx_we_r  <= '0';
      commit_r <= '0';
      icmp_we  <= '0';
      if arp_clr = '1' then
        arp_due <= false;
      end if;
      if icmp_clr = '1' then
        icmp_due <= false;
      end if;
      if rst = '1' then
        i        <= 0;
        arp_due  <= false;
        icmp_due <= false;
        peer_ok  <= false;
      elsif take_in = '1' then
        b := in_data;
        n := i;
        if i < 2047 then
          i <= i + 1;
        end if;
        -- Ethernet
        if n <= 5 then
          if n = 0 then
            dst_me <= b = mac_byte(MAC, 0);
            dst_bc <= b = x"FF";
          else
            dst_me <= dst_me and b = mac_byte(MAC, n);
            dst_bc <= dst_bc and b = x"FF";
          end if;
        elsif n <= 11 then
          src_mac <= src_mac(39 downto 0) & b;
        elsif n <= 13 then
          etype <= etype(7 downto 0) & b;
        end if;
        -- ARP（14〜41）
        case n is
          when 14 => arp_ok <= b = x"00";
          when 15 => arp_ok <= arp_ok and b = x"01";
          when 16 => arp_ok <= arp_ok and b = x"08";
          when 17 => arp_ok <= arp_ok and b = x"00";
          when 18 => arp_ok <= arp_ok and b = x"06";
          when 19 => arp_ok <= arp_ok and b = x"04";
          when 20 => arp_ok <= arp_ok and b = x"00";
          when 21 => arp_ok <= arp_ok and b = x"01";       -- 要求
          when 38 => tpa_me <= b = ip_byte(IP, 0);
          when 39 | 40 | 41 => tpa_me <= tpa_me and b = ip_byte(IP, n - 38);
          when others => null;
        end case;
        if n >= 22 and n <= 27 then
          arp_sha <= arp_sha(39 downto 0) & b;
        end if;
        if n >= 28 and n <= 31 then
          arp_spa <= arp_spa(23 downto 0) & b;
        end if;
        -- IPv4（14〜33）
        case n is
          when 14 => ip_ok <= b = x"45";
          when 16 => ip_total(15 downto 8) <= unsigned(b);
          when 17 => ip_total(7 downto 0) <= unsigned(b);
          when 20 => ip_ok <= ip_ok and b(5 downto 0) = "000000" and b(7) = '0';   -- MF なし・位置 0
          when 21 => ip_ok <= ip_ok and b = x"00";
          when 23 => ip_proto <= b;
          when 30 => ip_dst_me <= b = ip_byte(IP, 0);
          when 31 | 32 | 33 => ip_dst_me <= ip_dst_me and b = ip_byte(IP, n - 30);
          when others => null;
        end case;
        if n >= 26 and n <= 29 then
          ip_src <= ip_src(23 downto 0) & b;
        end if;
        if n >= 14 and n <= 33 then                    -- ヘッダのチェックサム（1 の補数の和）
          if n mod 2 = 0 then
            ip_hi <= b;
          elsif n = 15 then
            ip_sum <= resize(unsigned(std_logic_vector'(ip_hi & b)), 20);
          else
            ip_sum <= ip_sum + unsigned(std_logic_vector'(ip_hi & b));
          end if;
        end if;
        -- UDP（34〜41）/ ICMP（34〜）
        case n is
          when 34 => udp_src(15 downto 8) <= b; icmp_type <= b;
          when 35 => udp_src(7 downto 0) <= b;
          when 36 => udp_dst(15 downto 8) <= b; icmp_sum_r(15 downto 8) <= b;
          when 37 => udp_dst(7 downto 0) <= b; icmp_sum_r(7 downto 0) <= b;
          when 38 => udp_len(15 downto 8) <= unsigned(b);
          when 39 => udp_len(7 downto 0) <= unsigned(b);
          when others => null;
        end case;
        if n = 34 then
          icmp_w <= not icmp_due;                      -- 応答待ちの間はバッファを上書きしない
        end if;
        if n >= 34 and n < 34 + ICMP_MAX and ip_proto = x"01" and ((n = 34 and not icmp_due) or (n > 34 and icmp_w)) then
          icmp_we <= '1';
          icmp_wa <= to_unsigned(n - 34, ICMP_BITS);
          icmp_wd <= b;
        end if;
        -- UDP の payload を frame_mux のバッファへ（42 = ch、43 = seq は捨てる、44〜 = data）
        if n >= 42 and etype = x"0800" and ip_proto = x"11" and udp_dst = PORT_V then
          pl := n - 42;
          if pl + 8 < to_integer(udp_len) and pl <= MAX_PAYLOAD + 1 and pl /= 1 then
            rx_we_r <= '1';
            rx_wa_r <= to_unsigned(pl - 1, RAM_BITS) when pl /= 0 else (others => '0');
            rx_wd_r <= b;
          end if;
        end if;

        if in_last = '1' then
          i <= 0;
          if in_good = '1' and (dst_me or dst_bc) then
            if etype = x"0806" and arp_ok and tpa_me and n >= 41 then
              if not arp_due then
                arp_due   <= true;
                arp_mac_r <= arp_sha;
                arp_ip_r  <= arp_spa;
              end if;
            elsif etype = x"0800" and ip_ok and ip_dst_me and fold(ip_sum) = x"FFFF" and n + 1 >= 14 + to_integer(ip_total)
                  and dst_me then
              if ip_proto = x"01" and icmp_type = x"08" and icmp_w and not icmp_due
                 and to_integer(ip_total) - 20 <= ICMP_MAX and to_integer(ip_total) >= 28 then
                icmp_due   <= true;
                icmp_mac_r <= src_mac;
                icmp_ip_r  <= ip_src;
                icmp_len_r <= to_integer(ip_total) - 20;
              elsif ip_proto = x"11" and udp_dst = PORT_V and to_integer(udp_len) >= 10
                    and to_integer(udp_len) <= MAX_PAYLOAD + 10 and to_integer(udp_len) + 20 <= to_integer(ip_total) then
                fl := to_integer(udp_len) - 9;               -- ch + data
                commit_r  <= '1';
                len_r     <= fl;
                peer_ok   <= true;
                peer_mac  <= src_mac;
                peer_ip   <= ip_src;
                peer_port <= udp_src;
              end if;
            end if;
          end if;
        end if;
      end if;
    end if;
  end process;

  icmp_ram_i : entity work.sdp_ram
    generic map (WIDTH => 8, DEPTH => ICMP_MAX)
    port map (clk => clk, we => icmp_we, waddr => icmp_wa, wdata => icmp_wd, raddr => icmp_ra, rdata => icmp_q);

  ---------------------------------------------------------------------------------------------
  -- 送信
  ---------------------------------------------------------------------------------------------
  process (clk)
    variable h : bytes_t(0 to HDR_MAX - 1);
    variable s : unsigned(15 downto 0);
    variable c : unsigned(16 downto 0);
    variable udp_l : unsigned(15 downto 0);
  begin
    if rising_edge(clk) then
      arp_clr  <= '0';
      icmp_clr <= '0';
      done_r   <= '0';
      if ov = '1' and out_ready = '1' then
        ov <= '0';
      end if;
      if sv = '1' and start_ready = '1' then
        sv <= '0';
      end if;
      if rst = '1' then
        tx_state <= T_IDLE;
        ov       <= '0';
        sv       <= '0';
        seq      <= (others => '0');
      else
        case tx_state is
          when T_IDLE =>
            ra      <= (others => '0');
            icmp_ra <= to_unsigned(4, ICMP_BITS);
            if arp_due and arp_clr = '0' then
              kind      <= K_ARP;
              dst_mac_t <= arp_mac_r;
              tx_state  <= T_SUM;
            elsif icmp_due and icmp_clr = '0' then
              kind      <= K_ICMP;
              dst_mac_t <= icmp_mac_r;
              dst_ip_t  <= icmp_ip_r;
              ip_len_t  <= to_unsigned(20 + icmp_len_r, 16);
              proto_t   <= x"01";
              body_i    <= 4;
              body_end  <= icmp_len_r;
              tx_state  <= T_SUM;
            elsif tx_valid = '1' and done_r = '0' then
              if not peer_ok then
                done_r <= '1';                         -- 送信先がまだ無い: 捨てる
              else
                kind      <= K_UDP;
                dst_mac_t <= peer_mac;
                dst_ip_t  <= peer_ip;
                ip_len_t  <= to_unsigned(20 + 8 + 1 + tx_len, 16);
                proto_t   <= x"11";
                body_i    <= 1;
                body_end  <= tx_len;
                tx_state  <= T_CH_WAIT;
              end if;
            end if;

          when T_CH_WAIT =>                            -- frame_mux のバッファ(0) = ch が出るのを待つ
            tx_state <= T_CH;

          when T_CH =>
            ch_t     <= tx_rdata;
            tx_state <= T_SUM;

          when T_SUM =>                                -- IP ヘッダのチェックサム（宛先と長さ以外は定数）
            sum_t <= resize(unsigned'(x"4500"), 20) + resize(ip_len_t, 20) + resize(unsigned'(x"4000"), 20)
                     + resize(unsigned(std_logic_vector'(x"40" & proto_t)), 20)
                     + resize(unsigned(IP(31 downto 16)), 20) + resize(unsigned(IP(15 downto 0)), 20)
                     + resize(unsigned(dst_ip_t(31 downto 16)), 20) + resize(unsigned(dst_ip_t(15 downto 0)), 20);
            tx_state <= T_HDR;
            hdr_n    <= 0;

          when T_HDR =>
            if hdr_n = 0 then                          -- ヘッダを組む（1 周期）
              s := not fold(sum_t);
              for k in 0 to 5 loop
                h(k)     := mac_byte(dst_mac_t, k);
                h(6 + k) := mac_byte(MAC, k);
              end loop;
              h(12) := x"08";
              if kind = K_ARP then
                h(13) := x"06";
                h(14 to 21) := (x"00", x"01", x"08", x"00", x"06", x"04", x"00", x"02");   -- 応答
                for k in 0 to 5 loop
                  h(22 + k) := mac_byte(MAC, k);
                  h(32 + k) := mac_byte(arp_mac_r, k);
                end loop;
                for k in 0 to 3 loop
                  h(28 + k) := ip_byte(IP, k);
                  h(38 + k) := ip_byte(arp_ip_r, k);
                end loop;
                h(42) := x"00";
                h(43) := x"00";
                hdr_n <= 42;
              else
                h(13) := x"00";
                h(14) := x"45";
                h(15) := x"00";
                h(16) := std_logic_vector(ip_len_t(15 downto 8));
                h(17) := std_logic_vector(ip_len_t(7 downto 0));
                h(18) := x"00";
                h(19) := x"00";
                h(20) := x"40";                        -- DF
                h(21) := x"00";
                h(22) := x"40";                        -- TTL 64
                h(23) := proto_t;
                h(24) := std_logic_vector(s(15 downto 8));
                h(25) := std_logic_vector(s(7 downto 0));
                for k in 0 to 3 loop
                  h(26 + k) := ip_byte(IP, k);
                  h(30 + k) := ip_byte(dst_ip_t, k);
                end loop;
                if kind = K_ICMP then
                  -- 型 8 → 0: チェックサムに 0x0800 を足す（1 の補数の和）
                  c := resize(unsigned(icmp_sum_r), 17) + to_unsigned(16#0800#, 17);
                  c := resize(c(15 downto 0), 17) + resize(c(16 downto 16), 17);
                  h(34) := x"00";
                  h(35) := x"00";
                  h(36) := std_logic_vector(c(15 downto 8));
                  h(37) := std_logic_vector(c(7 downto 0));
                  h(38 to 43) := (others => x"00");
                  hdr_n <= 38;
                else
                  udp_l := ip_len_t - 20;
                  h(34) := PORT_V(15 downto 8);
                  h(35) := PORT_V(7 downto 0);
                  h(36) := peer_port(15 downto 8);
                  h(37) := peer_port(7 downto 0);
                  h(38) := std_logic_vector(udp_l(15 downto 8));
                  h(39) := std_logic_vector(udp_l(7 downto 0));
                  h(40) := x"00";                      -- チェックサムなし
                  h(41) := x"00";
                  h(42) := ch_t;
                  h(43) := std_logic_vector(seq);
                  seq   <= seq + 1;
                  hdr_n <= 44;
                end if;
              end if;
              hdr <= h;
            elsif ov = '0' or out_ready = '1' then     -- 1 バイトずつ出す
              ov    <= '1';
              od    <= hdr(0);
              hdr   <= hdr(1 to HDR_MAX - 1) & x"00";
              hdr_n <= hdr_n - 1;
              if hdr_n = 1 then
                if kind = K_ARP or body_i = body_end then
                  ol       <= '1';
                  tx_state <= T_START;
                else
                  ol       <= '0';
                  tx_state <= T_BODY_ADDR;
                end if;
              else
                ol <= '0';
              end if;
            end if;

          when T_BODY_ADDR =>                          -- 本体: 番地 → 待ち → 出す
            if kind = K_UDP then
              ra <= to_unsigned(body_i, RAM_BITS);
            else
              icmp_ra <= to_unsigned(body_i, ICMP_BITS);
            end if;
            tx_state <= T_BODY_WAIT;

          when T_BODY_WAIT =>
            tx_state <= T_BODY;

          when T_BODY =>
            if ov = '0' or out_ready = '1' then
              ov <= '1';
              od <= tx_rdata when kind = K_UDP else icmp_q;
              if body_i + 1 = body_end then
                ol       <= '1';
                tx_state <= T_START;
              else
                ol       <= '0';
                body_i   <= body_i + 1;
                tx_state <= T_BODY_ADDR;
              end if;
            end if;

          when T_START =>                              -- 最後のバイトが受け取られたら送り出しの 1 件
            if ov = '0' or out_ready = '1' then
              sv       <= '1';
              tx_state <= T_DONE;
            end if;

          when T_DONE =>
            if sv = '0' or start_ready = '1' then
              case kind is
                when K_ARP  => arp_clr <= '1';
                when K_ICMP => icmp_clr <= '1';
                when K_UDP  => done_r <= '1';
              end case;
              tx_state <= T_IDLE;
            end if;
        end case;
      end if;
    end if;
  end process;
end architecture;
