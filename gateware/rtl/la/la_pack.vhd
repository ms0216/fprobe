-- ロジアナのサンプルを、使うチャンネルだけ詰めて 16 ビットの語にする（深いバッファのストリーム用。la_encoder の手前。sys ドメイン）。
--
--   pack = 0: 詰めない（1 サンプル = 16 ビットの 1 語。入力をそのまま通す。待ちは 0 周期）
--   pack = 1: 下位 8 ビット（ch 0〜7）だけを使い、連続する 2 サンプルを 1 語に（時間の古いほうが下位）
--   pack = 2: 下位 4 ビット（ch 0〜3）だけを使い、連続する 4 サンプルを 1 語に
--   pack = 3: 下位 2 ビット（ch 0〜1）だけを使い、連続する 8 サンプルを 1 語に
-- 語が揃った周期の次に out_valid を 1 周期立てる（pack = 1〜3）。sample_valid の間が空いていても、揃うまで持つ。
-- run = 0 の間は作りかけの語を捨てる（次の run は、最初のサンプルから語を始める）。端数のサンプルは出さない。
-- out_hit は、語の中のどれかのサンプルが sample_hit のとき 1。
-- ストリームはトリガの位置の精度を使わないので、詰めるのはストリームだけ（pack は run の間、変えない）。
library ieee;
use ieee.std_logic_1164.all;

entity la_pack is
  port (
    clk          : in  std_logic;
    rst          : in  std_logic;
    run          : in  std_logic;
    pack         : in  std_logic_vector(1 downto 0);
    sample_valid : in  std_logic;
    sample       : in  std_logic_vector(15 downto 0);
    sample_hit   : in  std_logic;
    out_valid    : out std_logic;
    out_sample   : out std_logic_vector(15 downto 0);
    out_hit      : out std_logic
  );
end entity;

architecture rtl of la_pack is
  signal acc      : std_logic_vector(15 downto 0) := (others => '0');
  signal cnt      : integer range 0 to 7 := 0;
  signal hit_acc  : std_logic := '0';
  signal valid_r  : std_logic := '0';
  signal word_r   : std_logic_vector(15 downto 0) := (others => '0');
  signal hit_r    : std_logic := '0';
begin
  out_valid  <= sample_valid when pack = "00" else valid_r;
  out_sample <= sample       when pack = "00" else word_r;
  out_hit    <= sample_hit   when pack = "00" else hit_r;

  process (clk)
    variable w    : natural range 2 to 8;
    variable k    : natural range 2 to 8;
    variable word : std_logic_vector(15 downto 0);
  begin
    if rising_edge(clk) then
      valid_r <= '0';
      if rst = '1' or run = '0' then
        cnt <= 0;
        hit_acc <= '0';
        acc <= (others => '0');
      elsif sample_valid = '1' and pack /= "00" then
        case pack is
          when "01"   => w := 8; k := 2;
          when "10"   => w := 4; k := 4;
          when others => w := 2; k := 8;
        end case;
        word := acc;
        for j in 0 to 7 loop
          for b in 0 to 7 loop
            if j = cnt and b < w and w * j + b <= 15 then
              word(w * j + b) := sample(b);
            end if;
          end loop;
        end loop;
        if cnt = k - 1 then                                 -- 語が揃った
          word_r  <= word;
          hit_r   <= hit_acc or sample_hit;
          valid_r <= '1';
          cnt     <= 0;
          hit_acc <= '0';
          acc     <= (others => '0');
        else
          acc     <= word;
          hit_acc <= hit_acc or sample_hit;
          cnt     <= cnt + 1;
        end if;
      end if;
    end if;
  end process;
end architecture;
