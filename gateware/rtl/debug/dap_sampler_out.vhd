-- DAP サンプラの件の出力（dap_sampler から分けた部品）: 確認を待つ語を溜め（buf、分散 RAM）、確認が通ったら溜めた語を
-- 順に件（種類 7 と、要れば種類 17）と標本（sample_*）にして 1 周期 1 件で出す。誤りの区間（種類 18）は 1 件で、
-- 確認した語を出し終えてから出す。件の形は dap_sampler の先頭の説明のとおり。
--
-- 本体（dap_sampler の手順）との約束:
--   wr_*   : 確認を待つ語を buf に書く（1 周期。本体が値を受け取った周期に登録して出す）。
--   start_*: 本体が確認を通した周期の次の周期に 1 周期 1（本体が登録して出す）。その周期に buf の書き込みが終わるので、
--            次の周期から buf の 0 から start_count 語を出す（本体の確認から最初の件まで 2 周期。分ける前と同じ）。
--   gap_*  : 本体が誤りの区間を決めた周期の次の周期に 1 周期 1。確認した語を出していなければその周期に出し、出していれば
--            出し終えてから出す。出す前に次が来たら新しい方で上書きする（分ける前と同じ）。
--   draining・gap_pending は start・gap の入力の周期から 1 にする（本体からは、決めた次の周期から見える。分ける前と同じ）。
--   quiet: 最後に出した標本から 2 周期以上（その標本の値の一致が up_busy に届いた後: 一致は標本の 2 周期後に発火し、
--          同じ周期に up_busy に入る）。本体は quiet = '1' のときだけか所を始める（stop-precision の設計書 §3.2）。
-- 本体は確認した語を出し終える前に次の確認を出さない（1 転送は最短でも 160 周期 > 2 × CHECK_MAX + 1。本体の assert）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.event_pkg.all;
use work.dap_pkg.all;
use work.dap_sampler_pkg.all;

entity dap_sampler_out is
  generic (
    CHECK_MAX : positive range 1 to 64 := 1
  );
  port (
    clk          : in  std_logic;
    rst          : in  std_logic;
    -- 確認を待つ語の書き込み
    wr_valid     : in  std_logic;
    wr_index     : in  natural range 0 to CHECK_MAX - 1;
    wr_data      : in  std_logic_vector(ENTRY_BITS - 1 downto 0);   -- dap_sampler_pkg の to_bits
    -- 確認が通った: buf の 0 から start_count 語を出す
    start_valid  : in  std_logic;
    start_count  : in  natural range 1 to CHECK_MAX;
    -- 誤りの区間の件（種類 18 の [119:0]）
    gap_valid    : in  std_logic;
    gap_data     : in  std_logic_vector(119 downto 0);
    -- 状態
    draining     : out std_logic;                     -- 確認した語を出している（か start_valid）
    gap_pending  : out std_logic;                     -- 誤りの区間の件を出していない（か gap_valid）
    quiet        : out std_logic;                     -- 最後の標本から 2 周期以上
    -- 標本（sampler_match へ）と件（event_collector へ）。どちらも 1 周期のパルス
    sample_valid : out std_logic;
    sample_addr  : out std_logic_vector(31 downto 0);
    sample_data  : out std_logic_vector(31 downto 0);
    sample_ack   : out std_logic_vector(2 downto 0);
    sample_index : out unsigned(6 downto 0);
    sample_time  : out unsigned(47 downto 0);
    rec_valid    : out std_logic;
    rec_kind     : out std_logic_vector(7 downto 0);
    rec_word     : out std_logic_vector(119 downto 0)
  );
end entity;

architecture rtl of dap_sampler_out is
  type buf_t is array (0 to CHECK_MAX - 1) of std_logic_vector(ENTRY_BITS - 1 downto 0);
  signal buf        : buf_t;
  signal busy_r     : boolean := false;               -- 確認した語を出している
  signal idx        : natural range 0 to CHECK_MAX - 1 := 0;
  signal cnt        : natural range 1 to CHECK_MAX := 1;
  signal second     : boolean := false;               -- 今の語の件を出した。次はその種類 17
  signal gap_pend   : boolean := false;
  signal gap_word   : std_logic_vector(119 downto 0) := (others => '0');
  signal since_emit : natural range 0 to 2 := 2;      -- 最後に出した標本からの周期（2 で止める）
  signal r_valid    : std_logic := '0';
  signal r_kind     : std_logic_vector(7 downto 0) := (others => '0');
  signal r_word     : std_logic_vector(119 downto 0) := (others => '0');
  signal s_valid    : std_logic := '0';
  signal s_addr     : std_logic_vector(31 downto 0) := (others => '0');
  signal s_data     : std_logic_vector(31 downto 0) := (others => '0');
  signal s_ack      : std_logic_vector(2 downto 0) := (others => '0');
  signal s_index    : unsigned(6 downto 0) := (others => '0');
  signal s_time     : unsigned(47 downto 0) := (others => '0');
begin

  -- 確認を待つ語（分散 RAM に推論させるため、リセットの無い別のプロセス）
  buf_i : process (clk)
  begin
    if rising_edge(clk) then
      if wr_valid = '1' then
        buf(wr_index) <= wr_data;
      end if;
    end if;
  end process;

  process (clk)
    variable e : entry_t;
  begin
    if rising_edge(clk) then
      s_valid <= '0';
      r_valid <= '0';
      if since_emit /= 2 then
        since_emit <= since_emit + 1;
      end if;
      if start_valid = '1' then                             -- この周期に buf の書き込みが終わる: 次の周期から出す
        busy_r <= true;
        idx    <= 0;
        cnt    <= start_count;
        second <= false;
      elsif busy_r then
        e := to_entry(buf(idx));
        r_valid <= '1';
        if not second then
          r_kind     <= EVENT_KIND_SAMPLE;
          r_word     <= e.waits & ACK_OK & e.value & e.addr & e.stamp;
          s_valid    <= '1';
          s_addr     <= e.addr;
          s_data     <= e.value;
          s_ack      <= ACK_OK;
          s_index    <= unsigned(e.index);
          s_time     <= unsigned(e.stamp);
          since_emit <= 0;
        else
          r_kind <= EVENT_KIND_SAMPLE_BOUND;
          r_word <= x"0" & e.req & x"00000000" & e.addr & e.rsp_time;
        end if;
        if not second and e.bound = '1' then
          second <= true;
        else
          second <= false;
          if idx + 1 = cnt then
            busy_r <= false;
          else
            idx <= idx + 1;
          end if;
        end if;
      elsif gap_valid = '1' then                            -- 来た周期に出す
        r_valid  <= '1';
        r_kind   <= EVENT_KIND_SAMPLE_GAP;
        r_word   <= gap_data;
        gap_pend <= false;
      elsif gap_pend then
        r_valid  <= '1';
        r_kind   <= EVENT_KIND_SAMPLE_GAP;
        r_word   <= gap_word;
        gap_pend <= false;
      end if;
      -- 今出せない誤りの区間は控える（出したなら上の分岐で済んでいる）
      if gap_valid = '1' and (start_valid = '1' or busy_r) then
        gap_pend <= true;
        gap_word <= gap_data;
      end if;
      if rst = '1' then
        busy_r   <= false;
        gap_pend <= false;
        r_valid  <= '0';
        s_valid  <= '0';
      end if;
    end if;
  end process;

  draining     <= '1' when busy_r or start_valid = '1' else '0';
  gap_pending  <= '1' when gap_pend or gap_valid = '1' else '0';
  quiet        <= '1' when since_emit = 2 else '0';
  sample_valid <= s_valid;
  sample_addr  <= s_addr;
  sample_data  <= s_data;
  sample_ack   <= s_ack;
  sample_index <= s_index;
  sample_time  <= s_time;
  rec_valid    <= r_valid;
  rec_kind     <= r_kind;
  rec_word     <= r_word;
end architecture;
