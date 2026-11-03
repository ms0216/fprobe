-- サンプラの値の一致（R21。docs/superpowers/specs/2026-10-23-sampler-match-trigger-design.md）: DAP サンプラが返した語の
-- うち、ACK が OK で番地が match_addr に一致する語（標本）を比べ、(値 xor match_value) and match_mask = 0 に「入った」瞬間
-- （同じ番号の前の標本が不一致、今の標本が一致）に fire を 1 周期出す（クロストリガの源 bit5）。
-- 番地は (sample_addr xor match_addr) and match_addr_mask = 0 で比べる（全部 1 なら 1 番地。0 のビットは見ない:
-- 例 4 コアの DBGDSCR をまとめて見る。docs/superpowers/specs/2026-10-29-halt-watch-design.md §3.1）。
-- 「入った」の判定の状態（前の標本が一致したか・標本を見たか・その時刻）は、標本の番号（sample_index = サンプラの
-- か所 × 16 + 語）ごとに持つ。交互に来る別の番地の標本が、判定に混ざらない。enable の後、その番号の最初の標本では
-- 出さない（その前を見ていないので、入ったかどうかは分からない）。
-- 時刻は標本の時刻 sample_time（dap_sampler の、その語を読みに行った DRW の転送の線上の点。イベント 7 の時刻と同じ。
-- docs/superpowers/specs/2026-11-01-time-accuracy-design.md R6）。fire_time = 発火した標本、prev_time = 同じ番号の直前の
-- （不一致の）標本、fire_index = 発火した標本の番号。条件に入ったのは、prev_time の読みの後〜fire_time の読みの値が返る
-- までのどこか（推定しない）。2026-11-01 の前は「標本を受けた周期」（値が返った後）の時刻だった。
-- enable の 0→1 で数と状態を消す。
-- 2 段（2026-10-29、sys を上げるため）: 1 段目で番地・値を比べて結果をレジスタに置き、2 段目で状態を更新して発火する。
-- fire は標本の 2 周期後に出る（標本の周期の立ち上がりで 1 段目が比べ、次の立ち上がりで 2 段目が fire をレジスタに置く）。
-- once = 1: enable の後に最初に入った 1 回だけ発火し、その後は発火しない（数・時刻・番号を上書きしない）。停止の見張りで、
-- FPGA が止めた残りのコアの標本が「入った」として発火し直し、最初に止まったコアの記録を消すのを防ぐ（2026-10-29 のレビュー）。
-- unmatched_seen: 番号ごとに「enable の後に不一致の標本を見た」（ビット i = 番号 i。enable の 0→1 でだけ消える。once で発火を
-- 止めた後も記録する）。発火と同じ 2 段目で更新する: 番号 i が後で「入った」ときに発火するのは、この印が立った後だけ。
-- 停止の見張りのホストは、止まっていると読んだコアの印がその後も 0 なら、そのコアの停止では発火しない（見逃した）と確定できる
-- （docs/superpowers/specs/2026-10-29-halt-watch-design.md §3.6。推定でなく、FPGA が見た標本の事実）。
-- 門と最初の標本（docs/superpowers/specs/2026-11-03-halt-watch-fpga-restart-design.md §3.1）:
--   after_start = 1: start（1 周期）を受けるまで標本を比べない（数えず、番号ごとの状態・unmatched も変えない）。start を受けた
--   次の周期の標本から比べる。started = start を受けた（enable の 0→1 で消える）。停止の見張りでは、start は FPGA が全コアの
--   再開の要求を書き終えた周期: 門の後の標本は、どれも再開の要求の後の読み。
--   first_sample = 1: enable の後（after_start なら門の後）のその番号の最初の標本が一致なら発火する（0 なら今までどおり出さない）。
--   そのとき fire_first = 1、prev_time = start_time（start を受けていなければ 0）。どちらにするかは使う側が明示して選ぶ。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity sampler_match is
  port (
    clk             : in  std_logic;
    rst             : in  std_logic;
    -- 設定（CSR sampler_match）
    enable          : in  std_logic;
    match_addr      : in  std_logic_vector(31 downto 0);
    match_addr_mask : in  std_logic_vector(31 downto 0);   -- 比べる番地のビット
    match_mask      : in  std_logic_vector(31 downto 0);
    match_value     : in  std_logic_vector(31 downto 0);
    once            : in  std_logic;                       -- 1: enable の後の最初の 1 回だけ発火する
    after_start     : in  std_logic;                       -- 1: start を受けるまで標本を比べない
    first_sample    : in  std_logic;                       -- 1: 番号の最初の標本の一致でも発火する
    start           : in  std_logic;                       -- 1 周期: 門を開ける
    start_time      : in  unsigned(47 downto 0);           -- start の周期: 門を開けた事実の時刻（最初の標本の発火の prev_time）
    -- サンプラの出力（dap_sampler の sample_*）
    sample_valid    : in  std_logic;
    sample_addr     : in  std_logic_vector(31 downto 0);
    sample_data     : in  std_logic_vector(31 downto 0);
    sample_ack      : in  std_logic_vector(2 downto 0);
    sample_index    : in  unsigned(6 downto 0);            -- か所 × 16 + 語
    sample_time     : in  unsigned(47 downto 0);           -- 標本の時刻（読みに行った転送の線上の点）
    -- 結果
    fire            : out std_logic;
    matched         : out std_logic;                       -- 最後の標本が一致
    seen_any        : out std_logic;                       -- enable の後に標本があった
    seen            : out unsigned(31 downto 0);           -- 比べた標本の数
    count           : out unsigned(31 downto 0);           -- 発火の回数
    fire_time       : out unsigned(47 downto 0);
    prev_time       : out unsigned(47 downto 0);
    fire_index      : out unsigned(6 downto 0);
    fire_first      : out std_logic;                       -- 最後の発火は、その番号の最初の標本（prev_time = start_time）
    started         : out std_logic;                       -- enable の後に start を受けた
    unmatched_seen  : out std_logic_vector(127 downto 0)  -- 番号ごと: enable の後に不一致の標本を見た
  );
end entity;

architecture rtl of sampler_match is
  type time_ram_t is array (0 to 127) of unsigned(47 downto 0);
  signal last_time : time_ram_t;                           -- 番号ごとの前の標本の時刻（idx_seen が 1 の番号だけ意味を持つ）
  signal idx_seen, idx_matched : std_logic_vector(127 downto 0) := (others => '0');
  signal idx_unmatched : std_logic_vector(127 downto 0) := (others => '0');   -- 不一致の標本を見た番号
  signal enable_prev : std_logic := '0';
  signal fire_r, matched_r, any_r : std_logic := '0';
  signal seen_r, count_r : unsigned(31 downto 0) := (others => '0');
  signal fire_t, prev_t : unsigned(47 downto 0) := (others => '0');
  signal fire_i : unsigned(6 downto 0) := (others => '0');
  signal first_r, started_r : std_logic := '0';
  signal start_t : unsigned(47 downto 0) := (others => '0');
  -- 1 段目（比べた結果）
  signal v1, hit1 : boolean := false;
  signal fired    : boolean := false;               -- enable の後に発火した（once の判定。count_r /= 0 を毎回比べない）
  signal idx1     : unsigned(6 downto 0) := (others => '0');
  signal time1    : unsigned(47 downto 0) := (others => '0');
begin
  fire       <= fire_r;
  matched    <= matched_r;
  seen_any   <= any_r;
  seen       <= seen_r;
  count      <= count_r;
  fire_time  <= fire_t;
  prev_time  <= prev_t;
  fire_index <= fire_i;
  fire_first <= first_r;
  started    <= started_r;
  unmatched_seen <= idx_unmatched;

  process (clk)
    variable i : natural range 0 to 127;
  begin
    if rising_edge(clk) then
      fire_r      <= '0';
      enable_prev <= enable;
      -- 1 段目: 比べる
      -- 門: start を受けた後の周期の標本だけ（start と同じ周期の標本は、門の前の読み）
      v1    <= enable = '1' and sample_valid = '1' and sample_ack = "001" and
               ((sample_addr xor match_addr) and match_addr_mask) = (31 downto 0 => '0') and
               (after_start = '0' or started_r = '1');
      hit1  <= ((sample_data xor match_value) and match_mask) = (31 downto 0 => '0');
      idx1  <= sample_index;
      time1 <= sample_time;
      -- 2 段目: 状態と発火
      if rst = '1' or (enable = '1' and enable_prev = '0') then
        v1        <= false;
        fired     <= false;
        idx_seen  <= (others => '0');
        idx_unmatched <= (others => '0');
        matched_r <= '0';
        any_r     <= '0';
        seen_r    <= (others => '0');
        count_r   <= (others => '0');
        fire_t    <= (others => '0');
        prev_t    <= (others => '0');
        fire_i    <= (others => '0');
        first_r   <= '0';
        started_r <= '0';
        start_t   <= (others => '0');
      else
        if start = '1' and enable = '1' then
          started_r <= '1';
          start_t   <= start_time;
        end if;
        if v1 and enable = '1' then
          i := to_integer(idx1);
          -- 入った: 前の標本が不一致、または（first_sample のとき）この番号の最初の標本
          if hit1 and ((idx_seen(i) = '1' and idx_matched(i) = '0') or (idx_seen(i) = '0' and first_sample = '1'))
             and not (once = '1' and fired) then
            fire_r  <= '1';
            fired   <= true;
            count_r <= count_r + 1;
            fire_t  <= time1;
            fire_i  <= idx1;
            if idx_seen(i) = '1' then
              prev_t  <= last_time(i);
              first_r <= '0';
            else
              prev_t  <= start_t;
              first_r <= '1';
            end if;
          end if;
          idx_matched(i) <= '1' when hit1 else '0';
          idx_seen(i)    <= '1';
          if not hit1 then
            idx_unmatched(i) <= '1';
          end if;
          last_time(i)   <= time1;
          matched_r      <= '1' when hit1 else '0';
          any_r          <= '1';
          seen_r         <= seen_r + 1;
        end if;
      end if;
    end if;
  end process;
end architecture;
