-- OUT エンドポイントのバッファ（パケット単位で確定/破棄できる FIFO）。
--
-- プロトコル側:
--   wr_valid/wr_data で現在のパケットのペイロードを書き、パケット末で commit（受理）か discard（破棄）。
--   破棄されたパケットは痕跡を残さない。長さ 0 のパケットは commit しても何も出さない。
--   space_for_packet は packet_size バイト以上の空きがあるか（ACK/NAK/NYET の判断に使う）。
--   packet_size は現在の最大パケットサイズ（HS と FS で異なるので実行時に与える。MPS が上限）。
--   空きを超える書き込みは捨て、そのパケットは commit されても破棄扱いにする（防御）。
-- ユーザー側:
--   確定したバイトを out_valid/out_ready/out_data で出し、各 USB パケットの最終バイトで out_last。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.util_pkg.all;

entity usb_ep_out is
  generic (
    DEPTH : positive;      -- バイト数（2 の冪、MPS 以上）
    MPS   : positive       -- 最大パケットサイズの上限
  );
  port (
    clk              : in  std_logic;
    rst              : in  std_logic;
    packet_size      : in  natural range 1 to MPS;
    -- プロトコル側
    wr_valid         : in  std_logic;
    wr_data          : in  std_logic_vector(7 downto 0);
    commit           : in  std_logic;
    discard          : in  std_logic;
    space_for_packet : out std_logic;
    -- ユーザー側
    out_valid        : out std_logic;
    out_ready        : in  std_logic;
    out_data         : out std_logic_vector(7 downto 0);
    out_last         : out std_logic
  );
end entity;

architecture rtl of usb_ep_out is
  constant ADDR_BITS : natural := clog2(DEPTH);
  subtype ptr_t is unsigned(ADDR_BITS downto 0);   -- 1 ビット余分に持ち満杯と空を区別

  type memory_t is array (0 to DEPTH - 1) of std_logic_vector(8 downto 0);  -- last & data
  signal memory : memory_t;

  signal wr_ptr     : ptr_t := (others => '0');   -- 書き込み中パケットの末尾
  signal commit_ptr : ptr_t := (others => '0');   -- 確定済みの末尾（読み出しの上限）
  signal rd_ptr     : ptr_t := (others => '0');   -- 次にメモリから取り出す位置
  signal last_byte  : std_logic_vector(7 downto 0) := (others => '0');
  signal overflowed : std_logic := '0';

  signal out_full   : std_logic := '0';
  signal out_reg    : std_logic_vector(8 downto 0) := (others => '0');

  signal used       : ptr_t;
  signal full       : boolean;
  signal pop        : boolean;
  signal fetch      : boolean;
  signal write_en   : boolean;
  signal fix_last   : boolean;
begin
  assert 2 ** ADDR_BITS = DEPTH and MPS <= DEPTH
    report "usb_ep_out: DEPTH must be a power of two and >= MPS" severity failure;

  used     <= wr_ptr - rd_ptr;
  full     <= used = DEPTH;
  pop      <= out_full = '1' and out_ready = '1';
  fetch    <= rd_ptr /= commit_ptr and (out_full = '0' or pop);
  write_en <= wr_valid = '1' and not full;
  -- commit 時に、パケットの最終バイトの位置へ last を書き直す
  fix_last <= commit = '1' and wr_ptr /= commit_ptr and overflowed = '0';

  process (clk)
  begin
    if rising_edge(clk) then
      if write_en then
        memory(to_integer(wr_ptr(ADDR_BITS - 1 downto 0))) <= '0' & wr_data;
      elsif fix_last then
        memory(to_integer((wr_ptr(ADDR_BITS - 1 downto 0)) - 1)) <= '1' & last_byte;
      end if;
      if fetch then
        out_reg <= memory(to_integer(rd_ptr(ADDR_BITS - 1 downto 0)));
      end if;
    end if;
  end process;

  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        wr_ptr     <= (others => '0');
        commit_ptr <= (others => '0');
        rd_ptr     <= (others => '0');
        out_full   <= '0';
        overflowed <= '0';
      else
        if write_en then
          wr_ptr    <= wr_ptr + 1;
          last_byte <= wr_data;
        elsif wr_valid = '1' then
          overflowed <= '1';
        end if;
        if commit = '1' then
          if overflowed = '1' then
            wr_ptr <= commit_ptr;
          else
            commit_ptr <= wr_ptr;
          end if;
          overflowed <= '0';
        elsif discard = '1' then
          wr_ptr     <= commit_ptr;
          overflowed <= '0';
        end if;
        if fetch then
          rd_ptr   <= rd_ptr + 1;
          out_full <= '1';
        elsif pop then
          out_full <= '0';
        end if;
      end if;
    end if;
  end process;

  space_for_packet <= '1' when DEPTH - to_integer(used) >= packet_size else '0';
  out_valid        <= out_full;
  out_data         <= out_reg(7 downto 0);
  out_last         <= out_reg(8);
end architecture;
