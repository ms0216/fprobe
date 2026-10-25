-- mem_port（rtl/mem/mem_test.vhd の説明: 1 コマンド = 128 ビットの 1 拍、読みの結果は発行順に rd_valid、止められない）を
-- AXI3 の 64 ビットのマスタにする（ボード非依存。Zynq の S_AXI_HP0 等）。仕様: docs/superpowers/specs/2026-10-19-pynq-z2-port-design.md §4.3
--
-- 1 拍 = 2 語の INCR のバースト（AxLEN = 1、AxSIZE = 3）。物理番地 = base + 拍の番号 × 16、下位 64 ビットが先の語。
-- base = 0、または 16 の倍数でない（下位 4 ビットが 0 でない）間はコマンドを受けない（ready = 0。PS の DDR の 0 番地を壊さない。
-- 16 バイトのバーストが 4 KB の境界をまたぐ AXI 違反も防ぐ）。
-- rst は PS 側の AXI を巻き戻せない: 書きの途中（AW だけ・W の一部だけ渡した後）や応答待ちの間に rst を入れると、相手に
-- 中途の取引が残る。メモリ側の rst は起動時の 1 回だけ（相手が idle の間）とする前提で、途中の rst は支援しない（台帳 BUG-RTL-AXI-1）。
-- 書きと読みの順: 書きの B がすべて返ってから読みを出す（AXI は書きと読みの順を保証しないため）。深いバッファを読み出し
-- ながら書く（ストリーム）間は、読みのたびに直前の書きの B の往復を待ち、その間は書きも止まる。読みの速さは PC へのリンクで
-- 抑えられる（数 MB/s）ので書きの余裕は大きいが、細かくするなら出しっぱなしの書きの番地との比較で待つ。読み・書きとも出しっぱなしは MAX_OUTSTANDING まで。
-- B・R は止めずに受ける（BREADY・RREADY = 1）。誤りの応答（SLVERR・DECERR）は errors に数える（読みの値はそのまま返す）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity axi_mem_port is
  generic (
    ADDR_BITS       : positive := 21;         -- 拍の番号のビット数（2**ADDR_BITS × 16 バイト）
    MAX_OUTSTANDING : positive := 8
  );
  port (
    clk           : in  std_logic;
    rst           : in  std_logic;
    base          : in  unsigned(31 downto 0);   -- 領域の物理番地（バイト）。0 または 16 の倍数でないなら使えない（ready = 0）
    ready         : out std_logic;
    -- mem_port
    cmd_valid     : in  std_logic;
    cmd_ready     : out std_logic;
    cmd_write     : in  std_logic;
    cmd_addr      : in  unsigned(ADDR_BITS - 1 downto 0);
    cmd_data      : in  std_logic_vector(127 downto 0);
    rd_valid      : out std_logic;
    rd_data       : out std_logic_vector(127 downto 0);
    errors        : out unsigned(15 downto 0);
    -- AXI3 のマスタ（ID・CACHE・PROT 等の固定の値はトップかブロック設計で与える）
    m_axi_awaddr  : out std_logic_vector(31 downto 0);
    m_axi_awlen   : out std_logic_vector(3 downto 0);
    m_axi_awsize  : out std_logic_vector(2 downto 0);
    m_axi_awburst : out std_logic_vector(1 downto 0);
    m_axi_awvalid : out std_logic;
    m_axi_awready : in  std_logic;
    m_axi_wdata   : out std_logic_vector(63 downto 0);
    m_axi_wstrb   : out std_logic_vector(7 downto 0);
    m_axi_wlast   : out std_logic;
    m_axi_wvalid  : out std_logic;
    m_axi_wready  : in  std_logic;
    m_axi_bresp   : in  std_logic_vector(1 downto 0);
    m_axi_bvalid  : in  std_logic;
    m_axi_bready  : out std_logic;
    m_axi_araddr  : out std_logic_vector(31 downto 0);
    m_axi_arlen   : out std_logic_vector(3 downto 0);
    m_axi_arsize  : out std_logic_vector(2 downto 0);
    m_axi_arburst : out std_logic_vector(1 downto 0);
    m_axi_arvalid : out std_logic;
    m_axi_arready : in  std_logic;
    m_axi_rdata   : in  std_logic_vector(63 downto 0);
    m_axi_rresp   : in  std_logic_vector(1 downto 0);
    m_axi_rlast   : in  std_logic;
    m_axi_rvalid  : in  std_logic;
    m_axi_rready  : out std_logic
  );
end entity;

architecture rtl of axi_mem_port is
  signal aw_pending, ar_pending : std_logic;
  signal w_left : unsigned(1 downto 0);               -- 送る残りの W の語（0〜2）
  signal wbuf : std_logic_vector(127 downto 0);
  signal waddr, raddr : unsigned(31 downto 0);
  signal wr_out, rd_out : natural range 0 to MAX_OUTSTANDING;   -- B・R の返っていない数
  signal r_lo : std_logic_vector(63 downto 0);
  signal r_second : std_logic;                        -- 次の R は 2 語目
  signal r_err : std_logic;                           -- この拍の 1 語目が誤りだった
  signal err_count : unsigned(15 downto 0);
  signal can_write, can_read, enabled : std_logic;
  signal b_done, r_done : std_logic;
begin
  enabled <= '1' when base /= 0 and base(3 downto 0) = "0000" else '0';
  ready <= enabled;
  can_write <= '1' when enabled = '1' and aw_pending = '0' and w_left = 0 and wr_out < MAX_OUTSTANDING else '0';
  can_read  <= '1' when enabled = '1' and ar_pending = '0' and aw_pending = '0' and w_left = 0 and wr_out = 0
                        and rd_out < MAX_OUTSTANDING else '0';
  cmd_ready <= can_write when cmd_write = '1' else can_read;

  m_axi_awaddr  <= std_logic_vector(waddr);
  m_axi_awlen   <= "0001";
  m_axi_awsize  <= "011";
  m_axi_awburst <= "01";
  m_axi_awvalid <= aw_pending;
  m_axi_wdata   <= wbuf(63 downto 0) when w_left = 2 else wbuf(127 downto 64);
  m_axi_wstrb   <= (others => '1');
  m_axi_wlast   <= '1' when w_left = 1 else '0';
  m_axi_wvalid  <= '1' when w_left /= 0 else '0';
  m_axi_bready  <= '1';
  m_axi_araddr  <= std_logic_vector(raddr);
  m_axi_arlen   <= "0001";
  m_axi_arsize  <= "011";
  m_axi_arburst <= "01";
  m_axi_arvalid <= ar_pending;
  m_axi_rready  <= '1';
  errors <= err_count;

  b_done <= m_axi_bvalid;                              -- BREADY = 1
  r_done <= m_axi_rvalid and m_axi_rlast;              -- RREADY = 1

  process (clk)
    variable wr_next, rd_next : natural range 0 to MAX_OUTSTANDING;
    variable errs : unsigned(15 downto 0);
  begin
    if rising_edge(clk) then
      rd_valid <= '0';
      if rst = '1' then
        aw_pending <= '0';
        ar_pending <= '0';
        w_left <= (others => '0');
        wbuf <= (others => '0');
        waddr <= (others => '0');
        raddr <= (others => '0');
        wr_out <= 0;
        rd_out <= 0;
        r_lo <= (others => '0');
        r_second <= '0';
        r_err <= '0';
        err_count <= (others => '0');
        rd_data <= (others => '0');
      else
        wr_next := wr_out;
        rd_next := rd_out;
        errs := err_count;
        -- コマンドを受ける
        if cmd_valid = '1' and cmd_write = '1' and can_write = '1' then
          waddr <= base + shift_left(resize(cmd_addr, 32), 4);
          wbuf <= cmd_data;
          aw_pending <= '1';
          w_left <= "10";
          wr_next := wr_next + 1;
        elsif cmd_valid = '1' and cmd_write = '0' and can_read = '1' then
          raddr <= base + shift_left(resize(cmd_addr, 32), 4);
          ar_pending <= '1';
          rd_next := rd_next + 1;
        end if;
        -- AXI の受け渡し
        if aw_pending = '1' and m_axi_awready = '1' then
          aw_pending <= '0';
        end if;
        if w_left /= 0 and m_axi_wready = '1' then
          w_left <= w_left - 1;
        end if;
        if ar_pending = '1' and m_axi_arready = '1' then
          ar_pending <= '0';
        end if;
        if b_done = '1' then
          if wr_next /= 0 then                          -- rst の後に届く古い応答で数を巻き戻さない
            wr_next := wr_next - 1;
          end if;
          if m_axi_bresp /= "00" then
            errs := errs + 1;
          end if;
        end if;
        if m_axi_rvalid = '1' then
          if r_second = '0' then
            r_lo <= m_axi_rdata;
          end if;
          r_second <= not m_axi_rlast;
          if m_axi_rlast = '1' then
            r_err <= '0';
            if m_axi_rresp /= "00" or r_err = '1' then
              errs := errs + 1;                         -- 1 拍に 1 回（どちらかの語が誤りなら）
            end if;
          elsif m_axi_rresp /= "00" then
            r_err <= '1';
          end if;
          if r_done = '1' then
            rd_valid <= '1';
            rd_data <= m_axi_rdata & r_lo;
            if rd_next /= 0 then
              rd_next := rd_next - 1;
            end if;
          end if;
        end if;
        wr_out <= wr_next;
        rd_out <= rd_next;
        err_count <= errs;
      end if;
    end if;
  end process;
end architecture;
