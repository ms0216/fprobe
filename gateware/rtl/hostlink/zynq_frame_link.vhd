-- PS（ARM の Linux の中継 fprobe-relay）と frame_mux の受け渡しのバッファをつなぐ AXI4-Lite のスレーブ
-- （ボード非依存。clk = AXI の ACLK = sys）。仕様: docs/superpowers/specs/2026-10-19-pynq-z2-port-design.md §4.2
--
-- 「フレームの橋渡し」のリンク: プロセッサがフレーム（ch(1) | payload）をそのまま運ぶ。PC との線上の形式（UDP の
-- ch | seq | payload）は中継が受け持ち、ここはフレームを 4 バイトずつ受け渡すだけ。
--
-- レジスタ（番地はバイト、12 ビット）:
--   0x000 ID        読み: 0x46504C4B（"FPLK"）。中継はこれを確かめてから受け渡しを始める
--   0x004 VERSION   読み: 1
--   0x008 RX_STATUS 読み: bit0 = busy（frame_mux が前のフレームを流し中。RX_BUF に書かない・COMMIT しない）
--   0x00C RX_COMMIT 書き: [9:0] = 長さ（ch を含むバイト数、1〜MAX_PAYLOAD+1）。書くと RX_BUF の 1 フレームを渡す
--   0x010 TX_HEAD   読み: [9:0] = 送るフレームの長さ（ch を含む。無ければ 0）
--   0x014 TX_DONE   書き: 何を書いても、今の送るフレームを読み終えた
--   0x018 DROPS     読み: 捨てた COMMIT の数（busy の間・長さが範囲外）。busy の間の RX_BUF の書きは無視する
--   0x01C MEM_BASE  読み書き: 深いバッファに使う DDR の領域の物理番地（PS の中継が書く。リセットの後は 0 = 使えない）。
--                   mem_base に出す（深いバッファの無い構成ではつながない）
--   0x020 MEM_BYTES 読み: PL が MEM_BASE から使う大きさ（generic。深いバッファの無い構成は 0）。中継はこの範囲が Linux の
--                   メモリと重ならないことを確かめてから MEM_BASE を書く
--   0x024 MEM_ERRORS 読み: 深いバッファの AXI の誤りの応答の数（mem_errors。DDR の外の番地など）
--   0x400〜0x7FF RX_BUF 書き: 4 バイトずつ（リトルエンディアン）。バイト番地 0 = ch、1〜 = payload
--   0x800〜0xBFF TX_BUF 読み: 4 バイトずつ（TX_HEAD の長さより後ろのバイトは 0）
--   その他      読みは 0、書きは無視。どの番地にも必ず OKAY を返す（返さないと ARM が固まる）
--
-- 1 度に 1 つの取引だけを扱う（書きの AW と W はどちらの順でも受ける）。書きを読みより先に片付ける。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity zynq_frame_link is
  generic (
    MAX_PAYLOAD : positive := 512;
    RAM_BITS    : positive := 10;
    MEM_BYTES   : natural := 0             -- 深いバッファが MEM_BASE から使う大きさ（0 = 深いバッファ無し）
  );
  port (
    clk           : in  std_logic;
    rst           : in  std_logic;
    -- AXI4-Lite のスレーブ
    s_axi_awaddr  : in  std_logic_vector(11 downto 0);
    s_axi_awvalid : in  std_logic;
    s_axi_awready : out std_logic;
    s_axi_wdata   : in  std_logic_vector(31 downto 0);
    s_axi_wstrb   : in  std_logic_vector(3 downto 0);
    s_axi_wvalid  : in  std_logic;
    s_axi_wready  : out std_logic;
    s_axi_bresp   : out std_logic_vector(1 downto 0);
    s_axi_bvalid  : out std_logic;
    s_axi_bready  : in  std_logic;
    s_axi_araddr  : in  std_logic_vector(11 downto 0);
    s_axi_arvalid : in  std_logic;
    s_axi_arready : out std_logic;
    s_axi_rdata   : out std_logic_vector(31 downto 0);
    s_axi_rresp   : out std_logic_vector(1 downto 0);
    s_axi_rvalid  : out std_logic;
    s_axi_rready  : in  std_logic;
    -- frame_mux のリンク側
    rx_we         : out std_logic;
    rx_waddr      : out unsigned(RAM_BITS - 1 downto 0);
    rx_wdata      : out std_logic_vector(7 downto 0);
    rx_commit     : out std_logic;
    rx_len        : out natural range 0 to MAX_PAYLOAD + 1;
    rx_busy       : in  std_logic;
    tx_valid      : in  std_logic;
    tx_len        : in  natural range 1 to MAX_PAYLOAD + 1;
    tx_raddr      : out unsigned(RAM_BITS - 1 downto 0);
    tx_rdata      : in  std_logic_vector(7 downto 0);
    tx_done       : out std_logic;
    mem_base      : out unsigned(31 downto 0);
    mem_errors    : in  unsigned(15 downto 0) := (others => '0')
  );
end entity;

architecture rtl of zynq_frame_link is
  constant LINK_ID      : std_logic_vector(31 downto 0) := x"46504C4B";
  constant LINK_VERSION : std_logic_vector(31 downto 0) := x"00000001";

  type state_t is (S_IDLE, S_WRITE, S_WBYTES, S_BRESP, S_READ, S_RBYTES, S_RRESP);
  signal state : state_t;

  signal aw_got, w_got : std_logic;
  signal waddr : std_logic_vector(11 downto 0);
  signal wdata : std_logic_vector(31 downto 0);
  signal wstrb : std_logic_vector(3 downto 0);
  signal raddr : std_logic_vector(11 downto 0);
  signal rdata : std_logic_vector(31 downto 0);
  signal lane  : unsigned(1 downto 0);       -- 今のバイト（4 バイトの何番目）
  signal wait_data : std_logic;              -- TX_BUF の読み: 番地を出した次の周期でデータを取る
  signal drops : unsigned(31 downto 0);
  signal mem_base_r : unsigned(31 downto 0);

  signal awready_i, wready_i, arready_i : std_logic;

  -- 窓の中のバイト番地（RX_BUF・TX_BUF は 1 KB。番地の [9:2] が語、lane がバイト）
  function byte_addr(a : std_logic_vector(11 downto 0); l : unsigned(1 downto 0)) return unsigned is
    variable full : unsigned(9 downto 0);
  begin
    full := unsigned(a(9 downto 2)) & l;
    return resize(full, RAM_BITS);
  end function;
begin
  awready_i <= '1' when state = S_IDLE and aw_got = '0' else '0';
  wready_i  <= '1' when state = S_IDLE and w_got = '0' else '0';
  -- 書きを受け始めていたら読みは待たせる（書きを先に片付ける）
  arready_i <= '1' when state = S_IDLE and aw_got = '0' and w_got = '0' and s_axi_awvalid = '0'
                        and s_axi_wvalid = '0' else '0';
  s_axi_awready <= awready_i;
  s_axi_wready  <= wready_i;
  s_axi_arready <= arready_i;
  s_axi_bresp   <= "00";
  s_axi_rresp   <= "00";
  s_axi_bvalid  <= '1' when state = S_BRESP else '0';
  s_axi_rvalid  <= '1' when state = S_RRESP else '0';
  s_axi_rdata   <= rdata;
  mem_base      <= mem_base_r;

  process (clk)
    variable len : natural range 0 to 1023;
  begin
    if rising_edge(clk) then
      rx_we <= '0';
      rx_commit <= '0';
      tx_done <= '0';
      if rst = '1' then
        state <= S_IDLE;
        aw_got <= '0';
        w_got <= '0';
        waddr <= (others => '0');
        wdata <= (others => '0');
        wstrb <= (others => '0');
        raddr <= (others => '0');
        rdata <= (others => '0');
        lane <= (others => '0');
        wait_data <= '0';
        drops <= (others => '0');
        mem_base_r <= (others => '0');
        rx_waddr <= (others => '0');
        rx_wdata <= (others => '0');
        rx_len <= 0;
        tx_raddr <= (others => '0');
      else
        case state is
          when S_IDLE =>
            if s_axi_awvalid = '1' and awready_i = '1' then
              waddr <= s_axi_awaddr;
              aw_got <= '1';
            end if;
            if s_axi_wvalid = '1' and wready_i = '1' then
              wdata <= s_axi_wdata;
              wstrb <= s_axi_wstrb;
              w_got <= '1';
            end if;
            if (aw_got = '1' or (s_axi_awvalid = '1' and awready_i = '1')) and
               (w_got = '1' or (s_axi_wvalid = '1' and wready_i = '1')) then
              state <= S_WRITE;
            elsif s_axi_arvalid = '1' and arready_i = '1' then
              raddr <= s_axi_araddr;
              state <= S_READ;
            end if;

          when S_WRITE =>
            aw_got <= '0';
            w_got <= '0';
            lane <= (others => '0');
            state <= S_BRESP;
            case waddr(11 downto 10) is
              when "01" =>                                   -- RX_BUF
                if rx_busy = '0' then
                  state <= S_WBYTES;
                end if;
              when "00" =>
                if waddr(9 downto 2) = x"03" then            -- RX_COMMIT
                  len := to_integer(unsigned(wdata(9 downto 0)));
                  if rx_busy = '0' and len >= 1 and len <= MAX_PAYLOAD + 1 then
                    rx_len <= len;
                    rx_commit <= '1';
                  else
                    drops <= drops + 1;
                  end if;
                elsif waddr(9 downto 2) = x"05" then         -- TX_DONE
                  tx_done <= tx_valid;
                elsif waddr(9 downto 2) = x"07" then         -- MEM_BASE
                  mem_base_r <= unsigned(wdata);
                end if;
              when others =>
                null;
            end case;

          when S_WBYTES =>
            rx_we <= wstrb(to_integer(lane));
            rx_waddr <= byte_addr(waddr, lane);
            rx_wdata <= wdata(8 * to_integer(lane) + 7 downto 8 * to_integer(lane));
            lane <= lane + 1;
            if lane = 3 then
              state <= S_BRESP;
            end if;

          when S_BRESP =>
            if s_axi_bready = '1' then
              state <= S_IDLE;
            end if;

          when S_READ =>
            rdata <= (others => '0');
            lane <= (others => '0');
            wait_data <= '0';
            state <= S_RRESP;
            case raddr(11 downto 10) is
              when "10" =>                                   -- TX_BUF
                tx_raddr <= byte_addr(raddr, "00");
                state <= S_RBYTES;
              when "00" =>
                case raddr(9 downto 2) is
                  when x"00" => rdata <= LINK_ID;
                  when x"01" => rdata <= LINK_VERSION;
                  when x"02" => rdata <= (0 => rx_busy, others => '0');
                  when x"04" =>
                    if tx_valid = '1' then
                      rdata <= std_logic_vector(to_unsigned(tx_len, 32));
                    end if;
                  when x"06" => rdata <= std_logic_vector(drops);
                  when x"07" => rdata <= std_logic_vector(mem_base_r);
                  when x"08" => rdata <= std_logic_vector(to_unsigned(MEM_BYTES, 32));
                  when x"09" => rdata <= std_logic_vector(resize(mem_errors, 32));
                  when others => null;
                end case;
              when others =>
                null;
            end case;

          when S_RBYTES =>
            -- 番地を出した次の周期に tx_rdata が出る（frame_mux の送りのバッファは 1 周期の同期読み）
            wait_data <= not wait_data;
            if wait_data = '1' then
              -- フレームの長さより後ろは 0（送りのバッファに残った前のフレームを出さない）
              if to_integer(byte_addr(raddr, lane)) < tx_len then
                rdata(8 * to_integer(lane) + 7 downto 8 * to_integer(lane)) <= tx_rdata;
              end if;
              lane <= lane + 1;
              tx_raddr <= byte_addr(raddr, lane + 1);
              if lane = 3 then
                state <= S_RRESP;
              end if;
            end if;

          when S_RRESP =>
            if s_axi_rready = '1' then
              state <= S_IDLE;
            end if;
        end case;
      end if;
    end if;
  end process;
end architecture;
