-- ロジックアナライザの捕捉（ボード非依存、clk のドメイン）。
-- 仕様: docs/superpowers/specs/2026-10-16-m7-logic-analyzer-design.md §2
--
-- 前段（同期化・サンプル間隔・トリガ判定）は la_front、記録（状態・BRAM）は la_record。サンプルは rate + 1 周期ごと。
-- run の 0→1 で開始: プリトリガ分（pretrigger サンプル）を溜め、トリガを待ち（その間はリングで上書き）、
-- トリガ後に残り（DEPTH - pretrigger サンプル。トリガのサンプルを含む）を溜めて完了。run の 1→0 で中止。
-- トリガ = 値一致 ((s and mask) = (value and mask)) かつ エッジ条件（rise / fall で指定したチャンネルの
-- どれかが変化。指定なしなら真）、または ext_trigger（1 周期のパルスでよい。トリガ待ちの間に来たものを
-- 次のサンプルで使う）。判定は 1 段のパイプライン: サンプル n の判定は次の周期に、1 周期遅らせたサンプル列と揃えて使う。
-- 完了後、バッファは start_index から DEPTH サンプルが時間順（rd_addr で同期読み出し、1 周期遅れ。RD_REG なら 2 周期遅れ）。
-- 速いクロック（300 MHz 級）のため、幅の広い比較・加減算を毎周期の経路に置かない: pretrigger は DEPTH − 1 以下（下位 ADDR_BITS ビットだけ使う）。
-- FAST = true（取り込み領域）: トリガの時刻・位相・start_index はトリガを受けた周期の 1 周期後に確定する（done の 1 周期後。読み出しは
-- done を見てから始めるので影響しない）。前段 la_front も FAST にする。FAST = false は元の回路と周期まで同じ。
-- 外部トリガは EXT_DELAY 周期遅らせ、外部トリガが来た周期のピンのサンプルに付ける（la_front の注。時刻の正しさ S1b の N3）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.util_pkg.all;
use work.timing_pkg.all;

entity la_capture is
  generic (
    WIDTH : positive := 16;
    DEPTH : positive := 8192;                   -- 2 の冪
    RD_REG : boolean := false;                  -- 読み出しに出力レジスタを 1 段足す（BRAM の出力のレジスタに入り、速いクロックで間に合う）
    FAST   : boolean := false;
    EXT_DELAY : natural := LA_FRONT_EXT_ALIGN   -- 外部トリガの揃え（la_front の注。probes が前に通った段数を呼び手が足す）
  );
  port (
    clk           : in  std_logic;
    rst           : in  std_logic;
    probes        : in  std_logic_vector(WIDTH - 1 downto 0);
    time          : in  unsigned(63 downto 0);
    phase         : in  unsigned(3 downto 0) := (others => '0');   -- トリガの時刻の細かい位置（la_cap_domain: sys の周期の中の cap の周期の番号）
    ext_trigger   : in  std_logic;
    -- 設定（捕捉中は変えないこと）
    run           : in  std_logic;
    rate          : in  unsigned(15 downto 0);
    pretrigger    : in  unsigned(31 downto 0);   -- DEPTH - 1 まで
    trig_mask     : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_value    : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_rise     : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_fall     : in  std_logic_vector(WIDTH - 1 downto 0);
    pw_enable     : in  std_logic := '0';      -- パルス幅のトリガ（la_front）
    pw_event      : in  std_logic := '0';
    -- 状態
    capturing     : out std_logic;
    triggered     : out std_logic;
    done          : out std_logic;
    start_index   : out unsigned(31 downto 0);
    trigger_index : out unsigned(31 downto 0);
    trigger_time  : out unsigned(63 downto 0);
    trigger_phase : out unsigned(3 downto 0);
    -- 読み出し
    rd_addr       : in  unsigned(clog2(DEPTH) - 1 downto 0);
    rd_data       : out std_logic_vector(WIDTH - 1 downto 0)
  );
end entity;

architecture rtl of la_capture is
  signal arm, valid, hit : std_logic;
  signal data : std_logic_vector(WIDTH - 1 downto 0);
begin
  front_i : entity work.la_front
    generic map (WIDTH => WIDTH, FAST => FAST, EXT_DELAY => EXT_DELAY)
    port map (clk => clk, rst => rst, probes => probes, ext_trigger => ext_trigger, run => run, arm => arm,
              rate => rate, trig_mask => trig_mask, trig_value => trig_value, trig_rise => trig_rise,
              trig_fall => trig_fall, pw_enable => pw_enable, pw_event => pw_event, sample_valid => valid, sample => data, sample_hit => hit);

  record_i : entity work.la_record
    generic map (WIDTH => WIDTH, DEPTH => DEPTH, RD_REG => RD_REG, FAST => FAST)
    port map (clk => clk, rst => rst, run => run, pretrigger => pretrigger, valid => valid, data => data, hit => hit,
              time => time, phase => phase, arm => arm, capturing => capturing, triggered => triggered, done => done,
              start_index => start_index, trigger_index => trigger_index, trigger_time => trigger_time,
              trigger_phase => trigger_phase, rd_addr => rd_addr, rd_data => rd_data);
end architecture;
