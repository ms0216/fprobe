-- ロジアナのサンプル列を 128 ビットの拍に詰める（深いバッファ・ストリーミング用。sys ドメイン）。
-- 仕様: docs/superpowers/specs/2026-10-16-m8-ddr3-design.md §5
--
--   RAW: 1 拍 = 8 サンプル（16 ビット × 8、下位から時間順）
--   RLE（変化点）: 1 拍 = 4 レコード（32 ビット: 上位 16 = 繰り返し count、下位 16 = 値。値が count + 1 サンプル続いた）。
--        値が変わるか count が 65535 に達するとレコードを閉じる。トリガのサンプルでは必ず新しいレコードを始める。
-- run の 0→1 で始め（RLE の最初のレコードは最初のサンプルから）、1→0 で止める（作りかけの拍は捨てる）。
-- 拍に最初のトリガが入っていれば out_trig = '1'、out_slot = その拍の中の番号（RAW はサンプル、RLE はレコード）。
-- 出力は止められない（サンプルは毎周期来うる）。拍が揃ったときに out_ready = '0' ならその拍を捨て、overrun を立てる
-- （run の 0→1 で消える）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity la_encoder is
  port (
    clk          : in  std_logic;
    rst          : in  std_logic;
    run          : in  std_logic;
    rle          : in  std_logic;                        -- '1' = 変化点、'0' = RAW（run の 0→1 で取り込む）
    sample_valid : in  std_logic;
    sample       : in  std_logic_vector(15 downto 0);
    sample_hit   : in  std_logic;                        -- このサンプルでトリガ（最初の 1 回だけ使う）
    out_valid    : out std_logic;
    out_ready    : in  std_logic;
    out_data     : out std_logic_vector(127 downto 0);
    out_trig     : out std_logic;
    out_slot     : out unsigned(2 downto 0);
    triggered    : out std_logic;                        -- トリガを見た
    overrun      : out std_logic
  );
end entity;

architecture rtl of la_encoder is
  signal run_prev  : std_logic := '0';
  signal rle_r     : std_logic := '0';
  signal armed     : boolean := false;                   -- まだトリガを見ていない
  signal beat      : std_logic_vector(127 downto 0) := (others => '0');
  signal slot      : unsigned(2 downto 0) := (others => '0');
  signal beat_trig : std_logic := '0';
  signal beat_slot : unsigned(2 downto 0) := (others => '0');
  -- RLE: 作りかけのレコード
  signal open_rec  : boolean := false;
  signal value     : std_logic_vector(15 downto 0) := (others => '0');
  signal count     : unsigned(15 downto 0) := (others => '0');
  signal rec_trig  : std_logic := '0';                   -- 作りかけのレコードはトリガのサンプルから始まった
  signal valid_r   : std_logic := '0';
  signal data_r    : std_logic_vector(127 downto 0) := (others => '0');
  signal trig_r    : std_logic := '0';
  signal slot_r    : unsigned(2 downto 0) := (others => '0');
  signal overrun_r : std_logic := '0';
begin
  out_valid <= valid_r;
  out_data  <= data_r;
  out_trig  <= trig_r;
  out_slot  <= slot_r;
  triggered <= '0' when armed else '1';
  overrun   <= overrun_r;

  process (clk)
    -- 拍に 1 項目（RAW は 16 ビット、RLE は 32 ビット）を足す。揃ったら出す
    procedure push(item : std_logic_vector(31 downto 0); is_trig : std_logic; last_slot : natural) is
      variable b : std_logic_vector(127 downto 0);
      variable t : std_logic;
      variable s : unsigned(2 downto 0);
    begin
      b := beat;
      if rle_r = '1' then
        for k in 0 to 3 loop
          if k = to_integer(slot) then
            b(32 * k + 31 downto 32 * k) := item;
          end if;
        end loop;
      else
        for k in 0 to 7 loop
          if k = to_integer(slot) then
            b(16 * k + 15 downto 16 * k) := item(15 downto 0);
          end if;
        end loop;
      end if;
      t := beat_trig;
      s := beat_slot;
      if is_trig = '1' then
        t := '1';
        s := slot;
      end if;
      if slot = last_slot then
        if valid_r = '1' and out_ready = '0' then
          overrun_r <= '1';                                   -- 前の拍がまだ出ていない: この拍を捨てる
        else
          valid_r <= '1';
          data_r  <= b;
          trig_r  <= t;
          slot_r  <= s;
        end if;
        beat      <= (others => '0');
        slot      <= (others => '0');
        beat_trig <= '0';
      else
        beat      <= b;
        slot      <= slot + 1;
        beat_trig <= t;
        beat_slot <= s;
      end if;
    end procedure;

    variable hit : std_logic;
  begin
    if rising_edge(clk) then
      run_prev <= run;
      if valid_r = '1' and out_ready = '1' then
        valid_r <= '0';
      end if;

      if rst = '1' then
        valid_r   <= '0';
        armed     <= true;
        open_rec  <= false;
        overrun_r <= '0';
      elsif run = '0' then
        armed    <= true;                                         -- 止まっている（出しかけの拍は出し切る）
        open_rec <= false;
      elsif run_prev = '0' then
        -- 開始
        rle_r     <= rle;
        armed     <= true;
        open_rec  <= false;
        beat      <= (others => '0');
        slot      <= (others => '0');
        beat_trig <= '0';
        overrun_r <= '0';
      elsif sample_valid = '1' then
        hit := '0';
        if armed and sample_hit = '1' then
          hit   := '1';
          armed <= false;
        end if;
        if rle_r = '0' then
          push(x"0000" & sample, hit, 7);
        elsif not open_rec then
          open_rec <= true;
          value    <= sample;
          count    <= (others => '0');
          rec_trig <= hit;
        elsif sample = value and count /= x"FFFF" and hit = '0' then
          count <= count + 1;
        else
          push(std_logic_vector(count) & value, rec_trig, 3);    -- 作りかけのレコードを閉じて出す
          value    <= sample;
          count    <= (others => '0');
          rec_trig <= hit;
        end if;
      end if;
    end if;
  end process;
end architecture;
