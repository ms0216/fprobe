-- ロジアナの拍と DAP のイベントを外部メモリの 2 つのリングに書く（mem ドメイン）。
-- 仕様: docs/superpowers/specs/2026-10-16-m8-ddr3-design.md §5、2026-10-16-m9b-dap-events-design.md §2
--
-- リング A（ロジアナ）: 番地 0 から LA_BEATS 拍（容量の 15/16）。start で先頭から書き始め（stop で中止。完了していれば
--   そのまま）、最初のトリガの拍（in_trig）を書いたら、その後 post_beats 拍を書いて止まる（done）。
--   written は書いた拍の総数（LA_BEATS で飽和）。stream = '1'（start の周期に取り込む）ならトリガで止まらず
--   stop まで書き続ける（ストリーミング。仕様: 2026-10-16-m8c-la-streaming-design.md）。total は書いた拍の総数
--   （32 ビット、折り返す。ストリームの読み出し側が未読を求める）。
-- リング B（イベント）: 番地 LA_BEATS から EV_BEATS 拍（容量の 1/16）。ev_enable の 0→1 で先頭から書き始め、
--   有効な間ずっと書く（折り返す）。ev_written は書いた総数（32 ビットで飽和）。
-- in_kind = '0' はロジアナの拍、'1' はイベント。書かない拍（止まっている側の拍）は読み捨てる（上流を詰まらせない）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity la_mem_writer is
  generic (
    ADDR_BITS : positive := 23
  );
  port (
    clk          : in  std_logic;
    rst          : in  std_logic;
    start        : in  std_logic;                            -- 1 周期
    stop         : in  std_logic;                            -- 1 周期（中止）
    post_beats   : in  unsigned(ADDR_BITS - 1 downto 0);     -- start の周期に取り込む
    stream       : in  std_logic;                            -- start の周期に取り込む
    ev_enable    : in  std_logic;
    -- 拍の入力
    in_valid     : in  std_logic;
    in_ready     : out std_logic;
    in_data      : in  std_logic_vector(127 downto 0);
    in_kind      : in  std_logic;
    in_trig      : in  std_logic;
    in_slot      : in  unsigned(2 downto 0);
    -- 状態
    running      : out std_logic;
    triggered    : out std_logic;
    done         : out std_logic;
    trigger_beat : out unsigned(ADDR_BITS - 1 downto 0);
    trigger_slot : out unsigned(2 downto 0);
    written      : out unsigned(ADDR_BITS downto 0);
    total        : out unsigned(31 downto 0);
    ev_written   : out unsigned(31 downto 0);
    ev_pos       : out unsigned(ADDR_BITS - 5 downto 0);     -- イベントの次の書き位置（リングの中。ストリームが追う）
    -- メモリ（mem_port、書き込みのみ）
    cmd_valid    : out std_logic;
    cmd_ready    : in  std_logic;
    cmd_write    : out std_logic;
    cmd_addr     : out unsigned(ADDR_BITS - 1 downto 0);
    cmd_data     : out std_logic_vector(127 downto 0)
  );
end entity;

architecture rtl of la_mem_writer is
  constant EV_BITS  : natural := ADDR_BITS - 4;
  constant LA_BEATS : unsigned(ADDR_BITS downto 0) :=
    shift_left(to_unsigned(1, ADDR_BITS + 1), ADDR_BITS) - shift_left(to_unsigned(1, ADDR_BITS + 1), EV_BITS);
  constant LA_LAST  : unsigned(ADDR_BITS - 1 downto 0) := resize(LA_BEATS - 1, ADDR_BITS);
  type state_t is (S_IDLE, S_RUN, S_POST, S_DONE);
  signal state     : state_t := S_IDLE;
  signal ptr       : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal count     : unsigned(ADDR_BITS downto 0) := (others => '0');
  signal total_r   : unsigned(31 downto 0) := (others => '0');
  signal streaming : std_logic := '0';
  signal post      : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal left      : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal trig_beat : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal trig_slot : unsigned(2 downto 0) := (others => '0');
  signal ev_prev   : std_logic := '0';
  signal ev_ptr    : unsigned(EV_BITS - 1 downto 0) := (others => '0');
  signal ev_count  : unsigned(31 downto 0) := (others => '0');
  signal writing   : boolean;                                -- ロジアナのリングを書いている
  signal take      : boolean;                                -- この拍をメモリに書く
  signal fire      : boolean;
begin
  writing   <= state = S_RUN or state = S_POST;
  take      <= (in_kind = '0' and writing) or (in_kind = '1' and ev_enable = '1' and ev_prev = '1');
  fire      <= take and in_valid = '1' and cmd_ready = '1';
  in_ready  <= cmd_ready when take else '1';                 -- 書かない拍は読み捨てる
  cmd_valid <= in_valid when take else '0';
  cmd_write <= '1';
  cmd_addr  <= ptr when in_kind = '0' else resize(LA_BEATS, ADDR_BITS) + resize(ev_ptr, ADDR_BITS);
  cmd_data  <= in_data;

  running      <= '1' when writing else '0';
  triggered    <= '1' when state = S_POST or state = S_DONE else '0';
  done         <= '1' when state = S_DONE else '0';
  trigger_beat <= trig_beat;
  trigger_slot <= trig_slot;
  written      <= count;
  total        <= total_r;
  ev_written   <= ev_count;
  ev_pos       <= ev_ptr;

  process (clk)
  begin
    if rising_edge(clk) then
      ev_prev <= ev_enable;
      if rst = '1' or (ev_enable = '1' and ev_prev = '0') then
        ev_ptr   <= (others => '0');                         -- イベントの記録を始める
        ev_count <= (others => '0');
      elsif fire and in_kind = '1' then
        ev_ptr <= ev_ptr + 1;
        if ev_count /= x"FFFFFFFF" then
          ev_count <= ev_count + 1;
        end if;
      end if;

      if rst = '1' then
        state <= S_IDLE;
      elsif stop = '1' then
        if state /= S_DONE then
          state <= S_IDLE;                                   -- 中止（完了した結果は残す）
        end if;
      elsif start = '1' then
        state     <= S_RUN;
        ptr       <= (others => '0');
        count     <= (others => '0');
        total_r   <= (others => '0');
        post      <= post_beats;
        streaming <= stream;
      elsif fire and in_kind = '0' then
        if ptr = LA_LAST then
          ptr <= (others => '0');
        else
          ptr <= ptr + 1;
        end if;
        if count /= LA_BEATS then
          count <= count + 1;
        end if;
        total_r <= total_r + 1;
        if state = S_RUN and in_trig = '1' and streaming = '0' then
          trig_beat <= ptr;
          trig_slot <= in_slot;
          left      <= post;
          if post = 0 then
            state <= S_DONE;
          else
            state <= S_POST;
          end if;
        elsif state = S_POST then
          left <= left - 1;
          if left = 1 then
            state <= S_DONE;
          end if;
        end if;
      end if;
    end if;
  end process;
end architecture;
