-- IN エンドポイントのバッファ（パケット化と、ACK までの保持・再送）。
--
-- ユーザー側: in_valid/in_ready/in_data/in_last のバイトストリーム。パケットは次で区切る:
--   * packet_size バイトに達した（現在の最大パケットサイズ。HS と FS で異なるので実行時に与える）
--   * in_last（転送の終わり）。ちょうどパケット境界で終わった転送には長さ 0 のパケット（ZLP）を続ける
--     （ZLP = false なら続けない: 1 メッセージ = 1 パケットで、ホストがパケット単位で読む CMSIS-DAP 等）
--   * 途中までのデータがあり、FLUSH_CYCLES の間 書き込み側にデータが無かった（ストリームの小休止）。
--     バッファ満杯で待たされている間（in_valid = '1'）は小休止ではないので数えない
-- プロトコル側:
--   pkt_ready: 先頭パケットが送出可能。pkt_empty: それが ZLP。
--   pl_valid/pl_ready/pl_data/pl_last: 先頭パケットのペイロード（usb_tx_packet へ）。
--   ack: 先頭パケットが届いた（解放して次へ）。retry: 届かなかった（同じパケットを最初から読み直す）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.util_pkg.all;

entity usb_ep_in is
  generic (
    DEPTH        : positive;     -- バイト数（2 の冪、MPS 以上）
    MPS          : positive;     -- 最大パケットサイズの上限
    FLUSH_CYCLES : positive;     -- 小休止とみなす無入力時間
    ZLP          : boolean := true
  );
  port (
    clk       : in  std_logic;
    rst         : in  std_logic;
    packet_size : in  natural range 1 to MPS;
    -- ユーザー側
    in_valid  : in  std_logic;
    in_ready  : out std_logic;
    in_data   : in  std_logic_vector(7 downto 0);
    in_last   : in  std_logic;
    -- プロトコル側
    pkt_ready : out std_logic;
    pkt_empty : out std_logic;
    pl_valid  : out std_logic;
    pl_ready  : in  std_logic;
    pl_data   : out std_logic_vector(7 downto 0);
    pl_last   : out std_logic;
    ack       : in  std_logic;
    retry     : in  std_logic
  );
end entity;

architecture rtl of usb_ep_in is
  constant ADDR_BITS : natural := clog2(DEPTH);
  constant LEN_QUEUE : positive := 8;              -- 閉じたパケットの長さを保持する数
  subtype ptr_t is unsigned(ADDR_BITS downto 0);
  subtype len_t is natural range 0 to MPS;

  type memory_t is array (0 to DEPTH - 1) of std_logic_vector(7 downto 0);
  signal memory : memory_t;
  type len_queue_t is array (0 to LEN_QUEUE - 1) of len_t;
  signal lengths : len_queue_t := (others => 0);

  -- 書き込み側
  signal wr_ptr      : ptr_t := (others => '0');
  signal cur_len     : len_t := 0;                  -- まだ閉じていないパケットの長さ
  signal idle        : natural range 0 to FLUSH_CYCLES := 0;
  signal zlp_pending : std_logic := '0';
  signal lq_wr       : natural range 0 to LEN_QUEUE - 1 := 0;
  -- 読み出し側
  signal base_ptr    : ptr_t := (others => '0');    -- 未 ACK の先頭パケットの開始位置
  signal rd_ptr      : ptr_t := (others => '0');
  signal lq_rd       : natural range 0 to LEN_QUEUE - 1 := 0;
  signal lq_count    : natural range 0 to LEN_QUEUE := 0;
  signal fetched     : len_t := 0;                  -- 先頭パケットのうち取り出したバイト数
  signal consumed    : len_t := 0;                  -- そのうち送り出したバイト数
  signal out_full    : std_logic := '0';
  signal out_reg     : std_logic_vector(7 downto 0) := (others => '0');

  signal head_len    : len_t;
  signal accept      : boolean;
  signal fills       : boolean;   -- 次のバイトでパケットが packet_size に達する
  signal closes      : boolean;   -- 受け付けたバイトでパケットを閉じる（満杯または転送の終わり）
  signal flush       : boolean;   -- 小休止で途中のパケットを閉じる
  signal push        : boolean;   -- 長さキューに 1 つ積む
  signal push_len    : len_t;
  signal pop_len     : boolean;
  signal pop         : boolean;
  signal fetch       : boolean;
begin
  assert 2 ** ADDR_BITS = DEPTH and MPS <= DEPTH
    report "usb_ep_in: DEPTH must be a power of two and >= MPS" severity failure;

  head_len <= lengths(lq_rd);

  -- 書き込み: メモリに空きがあり、閉じたパケットの長さを入れる余地（ZLP 分を含め 2 つ）があること
  accept <= in_valid = '1' and zlp_pending = '0' and wr_ptr - base_ptr < DEPTH
            and lq_count <= LEN_QUEUE - 2;

  -- パケットを閉じる 3 つの契機（互いに排他。書き込み側の状態更新も同じ信号を使う）
  fills  <= cur_len + 1 >= packet_size;
  closes <= accept and (fills or in_last = '1');
  -- 小休止: 書き込み側にデータが無く（満杯で待たされているのではなく）、途中のパケットがある
  flush  <= in_valid = '0' and zlp_pending = '0' and cur_len /= 0 and idle = FLUSH_CYCLES;

  push     <= zlp_pending = '1' or closes or flush;      -- zlp_pending: 境界ちょうどで終わった転送の ZLP
  push_len <= cur_len + 1 when closes else cur_len when flush else 0;

  -- 読み出し
  pop_len <= ack = '1' and lq_count /= 0;
  pop     <= out_full = '1' and pl_ready = '1';
  fetch   <= lq_count /= 0 and fetched /= head_len and (out_full = '0' or pop)
             and ack = '0' and retry = '0';

  process (clk)
  begin
    if rising_edge(clk) then
      if accept then
        memory(to_integer(wr_ptr(ADDR_BITS - 1 downto 0))) <= in_data;
      end if;
      if fetch then
        out_reg <= memory(to_integer(rd_ptr(ADDR_BITS - 1 downto 0)));
      end if;
    end if;
  end process;

  process (clk)
    variable count : natural range 0 to LEN_QUEUE;
  begin
    if rising_edge(clk) then
      if rst = '1' then
        wr_ptr      <= (others => '0');
        cur_len     <= 0;
        idle        <= 0;
        zlp_pending <= '0';
        lq_wr       <= 0;
        base_ptr    <= (others => '0');
        rd_ptr      <= (others => '0');
        lq_rd       <= 0;
        lq_count    <= 0;
        fetched     <= 0;
        consumed    <= 0;
        out_full    <= '0';
      else
        -- 書き込み側
        if accept then
          wr_ptr <= wr_ptr + 1;
          if closes then
            cur_len <= 0;
          else
            cur_len <= cur_len + 1;
          end if;
          if ZLP and fills and in_last = '1' then
            zlp_pending <= '1';
          end if;
        elsif zlp_pending = '1' then
          zlp_pending <= '0';
        elsif flush then
          cur_len <= 0;
        end if;
        -- 小休止の計測: 途中のパケットがあり、書き込み側にデータが無い間だけ数える
        if in_valid = '1' or flush or cur_len = 0 then
          idle <= 0;
        elsif idle /= FLUSH_CYCLES then
          idle <= idle + 1;
        end if;

        count := lq_count;
        if push then
          lengths(lq_wr) <= push_len;
          lq_wr <= (lq_wr + 1) mod LEN_QUEUE;
          count := count + 1;
        end if;

        -- 読み出し側
        if pop_len then
          base_ptr <= base_ptr + head_len;
          rd_ptr   <= base_ptr + head_len;
          lq_rd    <= (lq_rd + 1) mod LEN_QUEUE;
          count    := count - 1;
          fetched  <= 0;
          consumed <= 0;
          out_full <= '0';
        elsif retry = '1' then
          rd_ptr   <= base_ptr;
          fetched  <= 0;
          consumed <= 0;
          out_full <= '0';
        else
          if fetch then
            rd_ptr   <= rd_ptr + 1;
            fetched  <= fetched + 1;
            out_full <= '1';
          elsif pop then
            out_full <= '0';
          end if;
          if pop then
            consumed <= consumed + 1;
          end if;
        end if;
        lq_count <= count;
      end if;
    end if;
  end process;

  in_ready  <= '1' when accept else '0';
  pkt_ready <= '1' when lq_count /= 0 else '0';
  pkt_empty <= '1' when head_len = 0 else '0';
  pl_valid  <= out_full;
  pl_data   <= out_reg;
  pl_last   <= '1' when consumed + 1 = head_len else '0';
end architecture;
