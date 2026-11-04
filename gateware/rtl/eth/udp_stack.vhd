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
--   欄の読み取りは層ごとの entity（eth_rx_header: 見出しと位置、arp_rx、ipv4_rx、icmp_rx: ICMP のバッファへ、
--   udp_rx: frame_mux のバッファへ）が受け持ち、ここはフレームの最後のバイトでの判定（応答の依頼・commit・送信先）だけ。
-- 送信（out_*: Ethernet のフレームの全バイト、続けて start_* に 1 件）: 優先順 ARP 応答 > echo 応答 > frame_mux の
--   フレーム（UDP、seq は通し番号）。送信先がまだ無ければ frame_mux のフレームは捨てる。
--   ヘッダはまとめて hdr に組んでから送り出す（UDP: Ethernet 14 + IP 20 + UDP 8 + ch + seq = 44 バイト、
--   ARP: 42、ICMP: 14 + 20 + ICMP の先頭 4）。続けて本体（frame_mux のバッファ / ICMP のバッファ）を送る。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.util_pkg.all;
use work.eth_pkg.all;

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
  constant ICMP_BITS : natural := clog2(ICMP_MAX);

  ---------------------------------------------------------------------------------------------
  -- 受信: 層ごとの読み取り（eth_rx_header・arp_rx・ipv4_rx・icmp_rx・udp_rx）と、フレームの終わりでの判定
  ---------------------------------------------------------------------------------------------
  signal take_in   : std_logic;
  signal rx_take   : std_logic;                      -- このバイトを各層が読む（リセット中は読まない）
  signal pos       : frame_pos_t;                    -- in_data のフレーム内の位置
  -- Ethernet
  signal dst_me, dst_bc : std_logic;
  signal src_mac   : std_logic_vector(47 downto 0);
  signal etype     : std_logic_vector(15 downto 0);
  -- ARP
  signal arp_request, arp_tpa_me : std_logic;
  signal arp_sha   : std_logic_vector(47 downto 0);
  signal arp_spa   : std_logic_vector(31 downto 0);
  -- IPv4
  signal ip_ok     : std_logic;
  signal ip_total  : unsigned(15 downto 0);
  signal ip_proto  : byte_t;
  signal ip_src    : std_logic_vector(31 downto 0);
  -- ICMP
  signal icmp_sel, icmp_hold, icmp_echo : std_logic;
  signal icmp_sum_r : std_logic_vector(15 downto 0);  -- 要求の ICMP チェックサム
  signal icmp_we   : std_logic;
  signal icmp_wa   : unsigned(ICMP_BITS - 1 downto 0);
  signal icmp_wd   : byte_t;
  signal icmp_ra   : unsigned(ICMP_BITS - 1 downto 0) := (others => '0');
  signal icmp_q    : byte_t;
  -- UDP
  signal udp_sel, udp_ok : std_logic;
  signal udp_src   : std_logic_vector(15 downto 0);
  signal udp_data_len : unsigned(15 downto 0);
  signal commit_r  : std_logic := '0';
  signal len_r     : natural range 0 to MAX_PAYLOAD + 1 := 0;

  -- 応答の依頼（受信 → 送信）
  signal arp_due, icmp_due : boolean := false;
  signal arp_mac_r : std_logic_vector(47 downto 0) := (others => '0');   -- ARP 応答の宛先
  signal arp_ip_r  : std_logic_vector(31 downto 0) := (others => '0');
  signal icmp_mac_r : std_logic_vector(47 downto 0) := (others => '0');  -- echo 応答の宛先
  signal icmp_ip_r : std_logic_vector(31 downto 0) := (others => '0');
  signal icmp_len_r : natural range 0 to ICMP_MAX := 0;
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

  ---------------------------------------------------------------------------------------------
  -- 受信
  ---------------------------------------------------------------------------------------------
  take_in  <= in_valid and not rx_busy and not commit_r;
  in_ready <= take_in;
  rx_take  <= take_in and not rst;

  eth_i : entity work.eth_rx_header
    generic map (MAC => MAC)
    port map (clk => clk, rst => rst, valid => take_in, data => in_data, last => in_last, pos => pos,
              dst_me => dst_me, dst_bc => dst_bc, src_mac => src_mac, etype => etype);

  arp_i : entity work.arp_rx
    generic map (IP => IP)
    port map (clk => clk, valid => rx_take, data => in_data, pos => pos,
              request => arp_request, tpa_me => arp_tpa_me, sha => arp_sha, spa => arp_spa);

  ipv4_i : entity work.ipv4_rx
    generic map (IP => IP)
    port map (clk => clk, valid => rx_take, data => in_data, pos => pos,
              ok => ip_ok, total => ip_total, proto => ip_proto, src => ip_src);

  icmp_sel  <= '1' when ip_proto = IP_PROTO_ICMP else '0';
  icmp_hold <= '1' when icmp_due else '0';
  icmp_i : entity work.icmp_rx
    generic map (ICMP_MAX => ICMP_MAX)
    port map (clk => clk, valid => rx_take, data => in_data, pos => pos, sel => icmp_sel, hold => icmp_hold,
              ip_total => ip_total, echo => icmp_echo, checksum => icmp_sum_r,
              buf_we => icmp_we, buf_waddr => icmp_wa, buf_wdata => icmp_wd);

  udp_sel <= '1' when etype = ETHERTYPE_IPV4 and ip_proto = IP_PROTO_UDP else '0';
  udp_i : entity work.udp_rx
    generic map (PORT_NUM => PORT_NUM, MAX_PAYLOAD => MAX_PAYLOAD, RAM_BITS => RAM_BITS)
    port map (clk => clk, valid => rx_take, data => in_data, pos => pos, sel => udp_sel, ip_total => ip_total,
              ok => udp_ok, src_port => udp_src, data_len => udp_data_len,
              buf_we => rx_we, buf_waddr => rx_waddr, buf_wdata => rx_wdata);

  -- フレームの最後のバイトで、正しく受けたフレームなら応答の依頼・commit・送信先の更新をする
  process (clk)
  begin
    if rising_edge(clk) then
      commit_r <= '0';
      if arp_clr = '1' then
        arp_due <= false;
      end if;
      if icmp_clr = '1' then
        icmp_due <= false;
      end if;
      if rst = '1' then
        arp_due  <= false;
        icmp_due <= false;
        peer_ok  <= false;
      elsif take_in = '1' and in_last = '1' and in_good = '1' and (dst_me = '1' or dst_bc = '1') then
        if etype = ETHERTYPE_ARP and arp_request = '1' and arp_tpa_me = '1' and to_integer(pos) >= ARP_END_POS - 1 then
          if not arp_due then
            arp_due   <= true;
            arp_mac_r <= arp_sha;
            arp_ip_r  <= arp_spa;
          end if;
        elsif etype = ETHERTYPE_IPV4 and ip_ok = '1' and dst_me = '1' then
          if icmp_echo = '1' then
            icmp_due   <= true;
            icmp_mac_r <= src_mac;
            icmp_ip_r  <= ip_src;
            icmp_len_r <= to_integer(ip_total) - IP_HDR_LEN;
          elsif udp_ok = '1' then
            commit_r  <= '1';
            len_r     <= to_integer(udp_data_len);
            peer_ok   <= true;
            peer_mac  <= src_mac;
            peer_ip   <= ip_src;
            peer_port <= udp_src;
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
              ip_len_t  <= to_unsigned(IP_HDR_LEN + icmp_len_r, 16);
              proto_t   <= IP_PROTO_ICMP;
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
                ip_len_t  <= to_unsigned(IP_HDR_LEN + UDP_HDR_LEN + 1 + tx_len, 16);   -- + seq
                proto_t   <= IP_PROTO_UDP;
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
              s := not csum_fold(sum_t);
              h := (others => x"00");
              for k in 0 to 5 loop
                h(ETH_DST_POS + k) := mac_byte(dst_mac_t, k);
                h(ETH_SRC_POS + k) := mac_byte(MAC, k);
              end loop;
              if kind = K_ARP then
                h(ETH_TYPE_POS)     := ETHERTYPE_ARP(15 downto 8);
                h(ETH_TYPE_POS + 1) := ETHERTYPE_ARP(7 downto 0);
                h(ARP_HEAD_POS to ARP_SHA_POS - 1) := ARP_REPLY_HEAD;
                for k in 0 to 5 loop
                  h(ARP_SHA_POS + k) := mac_byte(MAC, k);
                  h(ARP_THA_POS + k) := mac_byte(arp_mac_r, k);
                end loop;
                for k in 0 to 3 loop
                  h(ARP_SPA_POS + k) := ip_byte(IP, k);
                  h(ARP_TPA_POS + k) := ip_byte(arp_ip_r, k);
                end loop;
                hdr_n <= ARP_END_POS;
              else
                h(ETH_TYPE_POS)     := ETHERTYPE_IPV4(15 downto 8);
                h(ETH_TYPE_POS + 1) := ETHERTYPE_IPV4(7 downto 0);
                h(IP_VER_IHL_POS)   := x"45";
                h(IP_TOTAL_LEN_POS)     := std_logic_vector(ip_len_t(15 downto 8));
                h(IP_TOTAL_LEN_POS + 1) := std_logic_vector(ip_len_t(7 downto 0));
                h(IP_FLAGS_POS)     := x"40";                -- DF
                h(IP_TTL_POS)       := x"40";                -- TTL 64
                h(IP_PROTO_POS)     := proto_t;
                h(IP_CHECKSUM_POS)     := std_logic_vector(s(15 downto 8));
                h(IP_CHECKSUM_POS + 1) := std_logic_vector(s(7 downto 0));
                for k in 0 to 3 loop
                  h(IP_SRC_POS + k) := ip_byte(IP, k);
                  h(IP_DST_POS + k) := ip_byte(dst_ip_t, k);
                end loop;
                if kind = K_ICMP then
                  -- 型 8 → 0（エコー応答）: チェックサムに 0x0800 を足す（1 の補数の和）。ICMP の残りは本体から
                  c := resize(unsigned(icmp_sum_r), 17) + to_unsigned(16#0800#, 17);
                  c := resize(c(15 downto 0), 17) + resize(c(16 downto 16), 17);
                  h(ICMP_CHECKSUM_POS)     := std_logic_vector(c(15 downto 8));
                  h(ICMP_CHECKSUM_POS + 1) := std_logic_vector(c(7 downto 0));
                  hdr_n <= ICMP_CHECKSUM_POS + 2;
                else
                  udp_l := ip_len_t - IP_HDR_LEN;
                  h(UDP_SRC_POS)     := PORT_V(15 downto 8);
                  h(UDP_SRC_POS + 1) := PORT_V(7 downto 0);
                  h(UDP_DST_POS)     := peer_port(15 downto 8);
                  h(UDP_DST_POS + 1) := peer_port(7 downto 0);
                  h(UDP_LEN_POS)     := std_logic_vector(udp_l(15 downto 8));
                  h(UDP_LEN_POS + 1) := std_logic_vector(udp_l(7 downto 0));
                  -- チェックサムなし（UDP_CHECKSUM_POS = 0）
                  h(UDP_PAYLOAD_POS)     := ch_t;
                  h(UDP_PAYLOAD_POS + 1) := std_logic_vector(seq);
                  seq   <= seq + 1;
                  hdr_n <= UDP_PAYLOAD_POS + 2;
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
