-- サンプラの値の一致（R21。docs/superpowers/specs/2026-10-23-sampler-match-trigger-design.md）: DAP サンプラが返した語の
-- うち、ACK が OK で番地が match_addr の語（標本）を比べ、(値 xor match_value) and match_mask = 0 に「入った」瞬間
-- （前の標本が不一致、今の標本が一致）に fire を 1 周期出す（クロストリガの源 bit5）。enable の後の最初の標本では
-- 出さない（その前を見ていないので、入ったかどうかは分からない）。
-- 時刻は標本を受けた周期の time_count の下位 48 ビット（イベントの時刻と同じ幅）。fire_time = 発火した標本、prev_time = その直前の（不一致の）標本。条件に
-- 入ったのはその間のどこか（推定しない）。enable の 0→1 で数と状態を消す。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity sampler_match is
  port (
    clk          : in  std_logic;
    rst          : in  std_logic;
    time_count   : in  unsigned(63 downto 0);
    -- 設定（CSR sampler_match）
    enable       : in  std_logic;
    match_addr   : in  std_logic_vector(31 downto 0);
    match_mask   : in  std_logic_vector(31 downto 0);
    match_value  : in  std_logic_vector(31 downto 0);
    -- サンプラの出力（dap_sampler の sample_*）
    sample_valid : in  std_logic;
    sample_addr  : in  std_logic_vector(31 downto 0);
    sample_data  : in  std_logic_vector(31 downto 0);
    sample_ack   : in  std_logic_vector(2 downto 0);
    -- 結果
    fire         : out std_logic;
    matched      : out std_logic;                       -- 最後の標本が一致
    seen_any     : out std_logic;                       -- enable の後に標本があった
    seen         : out unsigned(31 downto 0);           -- 比べた標本の数
    count        : out unsigned(31 downto 0);           -- 発火の回数
    fire_time    : out unsigned(47 downto 0);
    prev_time    : out unsigned(47 downto 0)
  );
end entity;

architecture rtl of sampler_match is
  signal enable_prev : std_logic := '0';
  signal fire_r, matched_r, any_r : std_logic := '0';
  signal seen_r, count_r : unsigned(31 downto 0) := (others => '0');
  signal last_time, fire_t, prev_t : unsigned(47 downto 0) := (others => '0');
begin
  fire      <= fire_r;
  matched   <= matched_r;
  seen_any  <= any_r;
  seen      <= seen_r;
  count     <= count_r;
  fire_time <= fire_t;
  prev_time <= prev_t;

  process (clk)
    variable hit : boolean;
  begin
    if rising_edge(clk) then
      fire_r      <= '0';
      enable_prev <= enable;
      if rst = '1' or (enable = '1' and enable_prev = '0') then
        matched_r <= '0';
        any_r     <= '0';
        seen_r    <= (others => '0');
        count_r   <= (others => '0');
        last_time <= (others => '0');
        fire_t    <= (others => '0');
        prev_t    <= (others => '0');
      elsif enable = '1' and sample_valid = '1' and sample_ack = "001" and sample_addr = match_addr then
        hit := ((sample_data xor match_value) and match_mask) = (31 downto 0 => '0');
        if hit and any_r = '1' and matched_r = '0' then
          fire_r  <= '1';
          count_r <= count_r + 1;
          fire_t  <= time_count(47 downto 0);
          prev_t  <= last_time;
        end if;
        matched_r <= '1' when hit else '0';
        any_r     <= '1';
        seen_r    <= seen_r + 1;
        last_time <= time_count(47 downto 0);
      end if;
    end if;
  end process;
end architecture;
