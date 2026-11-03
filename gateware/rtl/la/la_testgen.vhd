-- 外部ピン入力の限界を測るための試験用の方形波（cap_clk の領域。PYNQ の fprobe_pynq_cap・fprobe_pynq_fast、sys で刻む fprobe_pynq_deep）。
-- 仕様: docs/superpowers/specs/2026-10-30-la-capture-clock-design.md の外部ピンの限界の測定（docs/hardware-wiring-request.md 3 章）
--
--   period: 周期（cap_clk の周期の数）。周波数 = cap_clk / period（cap_clk 300 MHz で period = 2 なら 150 MHz、3 なら 100 MHz）
--   high  : 1 の周期の数（デューティ = high / period）
--   edge_sel: 0 = cap_clk の立ち上がりで変える、1 = 立ち下がりで変える（cap_clk の半周期 = 300 MHz で 1.67 ns、400 MHz で 1.25 ns 遅れる）。
--             出力は取り込みと同じクロックの整数比なので、位相が固定される。2 通りで測って、位相の偏りによる見落としを減らす
--   enable  : 0 の間は out_oe = 0（ピンを駆動しない = Hi-Z）、out_q = 0
-- 範囲外の値は丸める（出力を固めない）: period < 2 は 2、4095 超は 4095（回路は 12 ビット）、high = 0 は 1、high >= period は period - 1。
-- 設定は sys の領域（clk）で書かれ、cdc_value で cap_clk の領域に渡す（変えている間は、前の値か新しい値のどちらかで動く）。
--
-- 300〜400 MHz（3.33〜2.5 ns）で動かすため、丸めと引き算はレジスタを挟んだ数段に分け（最初の版は 16 ビットの比較と引き算を 1 周期で行い、
-- cap_clk の setup が −3.4 ns で不合格だった）、出力のカウンタは比較でなく「ゼロまで数える下りカウンタ」を 1 の区間と 0 の区間で
-- 読み込み直す形にした。設定が出力に届くまで 6 周期かかる（enable も同じだけ遅らせて、古い値で始めない）。
-- 400 MHz（fprobe_pynq_fast）では「cnt = 0 なら読み込み、でなければ cnt − 1」の比較 → 引き算 → 選択が 1 周期に入らなかった
-- （C:\fw62 で −0.094 ns）。ゼロの判定 z を 1 周期前に求めてレジスタに持ち（z = 「いま cnt = 0」）、次の値を
-- 「(z ? 読み込む値 : cnt) + (z ? 0 : −1)」の 1 つの足し算にした（選択は足し算の入口の LUT に入り、キャリーの出力がそのまま D に行く）。
-- 足し算の入口に入るものはすべてレジスタにする: やり直し（restart = リセットか enable = 0。1 周期遅れ）、z、次に読み込む値 nload
-- （phase で選んだ lo_n / hi_n を 1 周期前に選んでおく）。それでも 12 ビットのキャリー（CARRY4 3 段）で −0.024 ns だったので、
-- カウンタを下 4 ビットと上 8 ビットに分け、上は「下 4 ビットがいま 0」（lo0。レジスタ）のときだけ 1 減らす（キャリーは最長 2 段）。
-- sys の rst は cap_clk に 2 段の同期化（sync_bit）で渡す（1 段の受け渡しは 2.5 ns の検査で −0.018 ns だった）。
-- 新しい設定が届くたびに、カウンタを最初からやり直す（古い設定の数え残しを持ち越さない。やり直しの 1 周期は oe = 0）。
-- リセット（rst。sys の同期・アクティブハイ）の間は、enable = 0 と同じ（oe = 0 = ピンを駆動しない）。解除後は、設定のまま再開する。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity la_testgen is
  port (
    clk      : in  std_logic;                       -- sys
    rst      : in  std_logic;
    cap_clk  : in  std_logic;
    enable   : in  std_logic;
    edge_sel : in  std_logic;
    period   : in  std_logic_vector(15 downto 0);
    high     : in  std_logic_vector(15 downto 0);
    out_q    : out std_logic;
    out_oe   : out std_logic
  );
end entity;

architecture rtl of la_testgen is
  signal cfg_src  : std_logic_vector(33 downto 0);
  signal cfg_last : std_logic_vector(33 downto 0) := (others => '0');
  signal load     : std_logic := '0';
  signal cfg_dst  : std_logic_vector(33 downto 0);
  signal cfg_r    : std_logic_vector(33 downto 0) := (others => '0');
  signal chg, chg1 : std_logic := '0';                             -- 新しい設定が届いた（cap_clk で 1 周期）
  signal rst_c    : std_logic;                                     -- sys の rst を cap_clk に同期化したもの（sync_bit。2 周期遅れる）
  -- 設定の整形（cap_clk）。段ごとにレジスタ
  signal p1, h1, p2, h2, p3, h3, h4, pm1, d : unsigned(11 downto 0) := (others => '0');
  signal ge, hz   : std_logic := '0';
  signal en_sr    : std_logic_vector(5 downto 1) := (others => '0');
  signal edge_sr  : std_logic_vector(5 downto 1) := (others => '0');
  signal hi_n, lo_n : unsigned(11 downto 0) := (others => '0');
  signal hi_z, lo_z : std_logic := '0';                            -- hi_n = 0・lo_n = 0（1 周期だけの区間）
  signal z        : std_logic := '0';                              -- いま cnt = 0（区間の最後の周期）
  signal nload    : unsigned(11 downto 0) := (others => '0');      -- 次の z で読み込む値（次の区間の長さ − 1）
  signal nload_z  : std_logic := '0';                              -- nload = 0
  signal restart  : std_logic := '1';                              -- リセットか enable = 0（駆動しない・やり直し）
  signal lo0      : std_logic := '0';                              -- cnt の下 4 ビットがいま 0
  signal dec      : std_logic;                                     -- 数えている（restart でも z でもない）
  signal a_lo     : unsigned(3 downto 0);
  signal a_hi     : unsigned(7 downto 0);
  -- 出力
  signal cnt      : unsigned(11 downto 0) := (others => '0');
  signal phase    : std_logic := '1';
  signal q_r      : std_logic := '0';
  signal q_f      : std_logic := '0';
  signal oe_r     : std_logic := '0';
begin
  cfg_src <= enable & edge_sel & period & high;

  process (clk)
  begin
    if rising_edge(clk) then
      load     <= '1' when cfg_src /= cfg_last else '0';
      cfg_last <= cfg_src;
    end if;
  end process;

  rst_sync_i : entity work.sync_bit
    generic map (INIT => '1')
    port map (clk => cap_clk, d => rst, q => rst_c);

  cdc_i : entity work.cdc_value
    generic map (WIDTH => 34)
    port map (src_clk => clk, src_load => load, src_value => cfg_src, dst_clk => cap_clk, dst_value => cfg_dst,
              dst_changed => chg);

  -- 設定の整形: 段 0〜5（enable・edge は同じ段数だけ遅らせる）
  process (cap_clk)
  begin
    if rising_edge(cap_clk) then
      cfg_r <= cfg_dst;                                              -- 段 0: 受け渡しの出口を 1 段置く
      chg1  <= chg;
      -- 段 1: 12 ビットに丸める
      if cfg_r(31 downto 28) /= "0000" then
        p1 <= (others => '1');                                       -- 4095 超は 4095
      elsif cfg_r(27 downto 17) = "00000000000" then
        p1 <= to_unsigned(2, 12);                                    -- 2 未満は 2
      else
        p1 <= unsigned(cfg_r(27 downto 16));
      end if;
      if cfg_r(15 downto 12) /= "0000" then
        h1 <= (others => '1');                                       -- 大きすぎる high は、あとで period - 1 に丸まる
      else
        h1 <= unsigned(cfg_r(11 downto 0));
      end if;
      en_sr(1)   <= cfg_r(33) and not chg1;                         -- 設定が届いた周期は enable を落とし、カウンタをやり直す
      edge_sr(1) <= cfg_r(32);
      -- 段 2: 比較と period - 1
      pm1 <= p1 - 1;
      ge  <= '1' when h1 >= p1 else '0';
      hz  <= '1' when h1 = 0 else '0';
      h2  <= h1;
      p2  <= p1;
      en_sr(2)   <= en_sr(1);
      edge_sr(2) <= edge_sr(1);
      -- 段 3: high を 1〜period - 1 に丸める
      if hz = '1' then
        h3 <= to_unsigned(1, 12);
      elsif ge = '1' then
        h3 <= pm1;
      else
        h3 <= h2;
      end if;
      p3 <= p2;
      en_sr(3)   <= en_sr(2);
      edge_sr(3) <= edge_sr(2);
      -- 段 4: 0 の区間の長さ
      d  <= p3 - h3;
      h4 <= h3;
      en_sr(4)   <= en_sr(3);
      edge_sr(4) <= edge_sr(3);
      -- 段 5: 下りカウンタに読み込む値（長さ - 1）
      hi_n <= h4 - 1;
      lo_n <= d - 1;
      hi_z <= '1' when h4 = 1 else '0';
      lo_z <= '1' when d = 1 else '0';
      en_sr(5)   <= en_sr(4);
      edge_sr(5) <= edge_sr(4);
      if rst_c = '1' then                                            -- リセット: enable の段を空にする（ピンを駆動しない）
        en_sr <= (others => '0');
      end if;
    end if;
  end process;

  -- 1 の区間（hi_n + 1 周期）と 0 の区間（lo_n + 1 周期）を、下りカウンタが交互に数える。
  -- 区間の最後（z = 1、cnt = 0）で phase を反転し、次の区間の長さ − 1（nload）を読み込む（phase = 1 の区間の後は 0 の区間 = lo_n）
  dec  <= not (restart or z);
  a_lo <= hi_n(3 downto 0) when restart = '1' else nload(3 downto 0) when z = '1' else cnt(3 downto 0);
  a_hi <= hi_n(11 downto 4) when restart = '1' else nload(11 downto 4) when z = '1' else cnt(11 downto 4);
  process (cap_clk)
    variable phase_next : std_logic;
  begin
    if rising_edge(cap_clk) then
      restart <= rst_c or not en_sr(4);                              -- リセットは、enable = 0 と同じ（レジスタの 1 段で en_sr(5) と同じ時期）
      cnt(3 downto 0)  <= a_lo + unsigned'(3 downto 0 => dec);                -- 数えている間は −1
      cnt(11 downto 4) <= a_hi + unsigned'(7 downto 0 => dec and lo0);        -- 下 4 ビットが 0 から 15 に戻るときだけ −1
      if restart = '1' then
        lo0 <= '1' when hi_n(3 downto 0) = 0 else '0';
      elsif z = '1' then
        lo0 <= '1' when nload(3 downto 0) = 0 else '0';
      else
        lo0 <= '1' when cnt(3 downto 0) = 1 else '0';
      end if;
      phase_next := phase;
      if restart = '1' then
        phase_next := '1';
        z     <= hi_z;
        q_r   <= '0';
        oe_r  <= '0';
      else
        oe_r <= '1';
        q_r  <= phase;
        if z = '1' then
          phase_next := not phase;
          z <= nload_z;
        else
          z <= '1' when cnt = 1 else '0';
        end if;
      end if;
      phase <= phase_next;
      if phase_next = '1' then                                       -- 次に z になったときに読み込む区間の長さ
        nload   <= lo_n;
        nload_z <= lo_z;
      else
        nload   <= hi_n;
        nload_z <= hi_z;
      end if;
    end if;
  end process;

  process (cap_clk)
  begin
    if falling_edge(cap_clk) then
      q_f <= q_r;
    end if;
  end process;

  out_q  <= q_f when edge_sr(5) = '1' else q_r;
  out_oe <= oe_r;
end architecture;
