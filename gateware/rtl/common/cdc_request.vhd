-- 要求（1 周期の req とその時点の値の組）のクロックドメイン乗せ換え。cdc_value の上に、要求ごとの通し番号を載せる。
--
-- 送り側: req（1 周期）で value を写し、通し番号 src_seq を 1 進める。番号と値の組を流し続ける（cdc_value の src_load = '1'）。
-- 受け側: 届いた番号 dst_seq が前に届いた番号と違えば、その組の値 dst_value と dst_new（1 周期）を出す。
--   受け渡しの間に要求が重なると、間の要求は飛ばして最新の要求だけが届く（値は最新の要求の時点のもの）。
--   送り側は src_seq と、受け側が受けた番号（呼ぶ側が受け側から返す）を比べれば、要求が受けられたかが分かる。
-- リセット（同期・アクティブハイ）: 両方を同時にかける。番号は 0 から始まり、受け側は最初に届いた番号 0 を要求とみなさない。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity cdc_request is
  generic (
    WIDTH      : positive;
    SEQ_BITS   : positive := 8;
    SAME_CLOCK : boolean := false
  );
  port (
    src_clk   : in  std_logic;
    src_rst   : in  std_logic;
    req       : in  std_logic;
    value     : in  std_logic_vector(WIDTH - 1 downto 0);
    src_seq   : out unsigned(SEQ_BITS - 1 downto 0);
    dst_clk   : in  std_logic;
    dst_rst   : in  std_logic;
    dst_seq   : out unsigned(SEQ_BITS - 1 downto 0);       -- 最後に受けた要求の番号
    dst_new   : out std_logic;                             -- 1 周期: dst_value が新しい要求の値になった
    dst_value : out std_logic_vector(WIDTH - 1 downto 0)
  );
end entity;

architecture rtl of cdc_request is
  signal seq_r : unsigned(SEQ_BITS - 1 downto 0) := (others => '0');
  signal value_r : std_logic_vector(WIDTH - 1 downto 0) := (others => '0');
  signal x_src, x_dst : std_logic_vector(SEQ_BITS + WIDTH - 1 downto 0);
  signal x_new : std_logic;
  signal d_seq : unsigned(SEQ_BITS - 1 downto 0) := (others => '0');
  signal d_new : std_logic := '0';
  signal d_value : std_logic_vector(WIDTH - 1 downto 0) := (others => '0');
begin
  process (src_clk)
  begin
    if rising_edge(src_clk) then
      if src_rst = '1' then
        seq_r   <= (others => '0');
        value_r <= (others => '0');
      elsif req = '1' then
        seq_r   <= seq_r + 1;
        value_r <= value;
      end if;
    end if;
  end process;
  src_seq <= seq_r;

  x_src <= std_logic_vector(seq_r) & value_r;
  x_i : entity work.cdc_value
    generic map (WIDTH => SEQ_BITS + WIDTH, SAME_CLOCK => SAME_CLOCK)
    port map (src_clk => src_clk, src_rst => src_rst, src_load => '1', src_value => x_src, src_ready => open,
              dst_clk => dst_clk, dst_rst => dst_rst, dst_value => x_dst, dst_changed => x_new);

  process (dst_clk)
    variable rx : unsigned(SEQ_BITS - 1 downto 0);
  begin
    if rising_edge(dst_clk) then
      rx := unsigned(x_dst(SEQ_BITS + WIDTH - 1 downto WIDTH));
      d_new <= '0';
      if dst_rst = '1' then
        d_seq   <= (others => '0');
        d_value <= (others => '0');
      elsif x_new = '1' and rx /= d_seq then
        d_seq   <= rx;
        d_new   <= '1';
        d_value <= x_dst(WIDTH - 1 downto 0);
      end if;
    end if;
  end process;
  dst_seq   <= d_seq;
  dst_new   <= d_new;
  dst_value <= d_value;
end architecture;
