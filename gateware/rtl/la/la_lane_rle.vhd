-- ロジアナの変化点（RLE）の N 並列の符号器（ボード非依存、clk = sys のドメイン）: 1 周期に LANES サンプル（レーン）の語を受け、
-- 値が変わった位置ごとにレコードを閉じ、4 レコードずつ 128 ビットの拍にする（深いバッファ・垂れ流し用）。
-- 仕様: docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §2
--
--   レコード（32 ビット、la_encoder の RLE と同じ形）: 上位 16 = 繰り返しの数 − 1、下位 16 = 値。値が count + 1 細かいサンプル続いた。
--   1 周期に閉じるレコードは最大 LANES 個（語のすべてのレーンで値が変わったとき）。語のレーン k で値が変われば、その前の続き
--   （前の語から続くなら、その長さを足す）を閉じる。続きの長さが 65536 − LANES を超えた語の終わりでも閉じる（数の 16 ビットに
--   必ず収まる。変化の無い区間は 65536 − LANES 以上・65536 以下のサンプルごとに 1 レコード）。
--   閉じたレコードは時間の順に詰めて BUF 個の溜めに入れ、4 個たまるごとに拍にして出す（1 周期に 1 拍まで = 4 レコード）。
--   受け手が受けられない（beat_ready = '0'）間は溜めに残す。溜めに入りきらないレコードが出た周期に overflow を 1 周期立て、
--   そのレコードとそれ以後のレコードは入れない（start まで。溢れる前に溜めに入ったレコードは出す = 出た拍は欠けの無い
--   先頭の部分。受け手は溢れとして記録する）。
-- start（1 周期）で溜め・作りかけのレコードを捨てて始め直す（最初のレコードは次の語のレーン 0 から）。
-- run を止めたとき作りかけのレコードと 4 個に満たない溜めは出さない（la_encoder と同じ: 端数は捨てる）。
-- パイプライン: 入力（隣のレーンとの比較）→ 閉じるレコード → 詰め → 溜めと拍 の 4 段。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.la_pkg.all;

entity la_lane_rle is
  generic (
    WIDTH : positive := 16;                 -- <= 16（レコードの値の欄）
    LANES : positive := 8;                  -- 1〜8
    BUF   : positive := 16                  -- 溜めのレコードの数（>= 4 + LANES）
  );
  port (
    clk        : in  std_logic;
    rst        : in  std_logic;
    start      : in  std_logic;
    in_valid   : in  std_logic;
    lane_data      : in  std_logic_vector(LANES * WIDTH - 1 downto 0);
    beat_valid : out std_logic;
    beat_ready : in  std_logic;
    beat_data  : out std_logic_vector(127 downto 0);
    overflow   : out std_logic
  );
end entity;

architecture rtl of la_lane_rle is
  subtype val_t is std_logic_vector(WIDTH - 1 downto 0);
  subtype rec_t is std_logic_vector(31 downto 0);
  type rec_array_t is array (natural range <>) of rec_t;
  constant RUN_MAX : natural := 65536;                      -- 1 レコードの最大の長さ（数の欄 16 ビット + 1）
  constant CLOSE_AT : natural := RUN_MAX - LANES;           -- 語の終わりの続きがこれを超えたら閉じる
  -- 段 A（入力）
  signal va : std_logic := '0';
  signal wa : std_logic_vector(LANES * WIDTH - 1 downto 0) := (others => '0');
  signal last_in : val_t := (others => '0');                -- 前に受けた語の最も新しいレーン
  signal chg_a : std_logic_vector(LANES - 1 downto 0) := (others => '0');   -- レーン k が 1 つ前のサンプルと違う
  signal prev_a : val_t := (others => '0');                 -- wa の 1 つ前のサンプル（前の語の最も新しいレーン）
  -- 段 B（閉じるレコード）の状態: 作りかけの続きの長さ（0 = 続きが無い: 次の語のレーン 0 から始まる）
  signal run_len : unsigned(16 downto 0) := (others => '0');
  signal vb : std_logic := '0';
  signal rv_b : std_logic_vector(LANES - 1 downto 0) := (others => '0');
  signal rec_b : rec_array_t(0 to LANES - 1) := (others => (others => '0'));
  -- 段 C（詰め）
  signal kc : natural range 0 to LANES := 0;
  signal rec_c : rec_array_t(0 to LANES - 1) := (others => (others => '0'));
  -- 段 D（溜め）
  signal fill : natural range 0 to BUF := 0;
  signal buf_r : rec_array_t(0 to BUF - 1) := (others => (others => '0'));
  signal bv : std_logic := '0';
  signal bd : std_logic_vector(127 downto 0) := (others => '0');
  signal ovf : std_logic := '0';
  signal broken : boolean := false;                         -- 溢れた後（以後のレコードは溜めに入れない。start で戻る）

  function lane(w : std_logic_vector; k : natural) return val_t is
  begin
    return lane_slice(w, WIDTH, k);
  end function;

  function rec(len : unsigned; v : val_t) return rec_t is
  begin
    return rle_record(resize(len - 1, 16), v);
  end function;
begin
  assert WIDTH <= 16 and LANES <= 8 and BUF >= 4 + LANES
    report "la_lane_rle: WIDTH <= 16, LANES <= 8, BUF >= 4 + LANES" severity failure;

  process (clk)
    variable c : std_logic_vector(LANES - 1 downto 0);
    variable first : boolean;
    variable prev_k : integer;
    variable len : unsigned(16 downto 0);
    variable rv : std_logic_vector(LANES - 1 downto 0);
    variable rb : rec_array_t(0 to LANES - 1);
    variable n : natural range 0 to LANES;
    variable rc : rec_array_t(0 to LANES - 1);
    variable f : natural range 0 to BUF;
    variable nb : rec_array_t(0 to BUF - 1);
    variable can_emit : boolean;
  begin
    if rising_edge(clk) then
      -- 段 A: 隣のサンプルとの比較（語の境はレーン 0 と前の語の最も新しいレーン）
      va <= in_valid;
      if in_valid = '1' then
        wa <= lane_data;
        prev_a <= last_in;
        last_in <= lane(lane_data, LANES - 1);
        for k in 0 to LANES - 1 loop
          if k = 0 then
            chg_a(k) <= '1' when lane(lane_data, 0) /= last_in else '0';
          else
            chg_a(k) <= '1' when lane(lane_data, k) /= lane(lane_data, k - 1) else '0';
          end if;
        end loop;
      end if;

      -- 段 B: この語で閉じるレコード（続きが無ければレーン 0 では閉じない）
      vb <= va;
      if va = '1' then
        c := chg_a;
        if run_len = 0 then
          c(0) := '0';
        end if;
        rv := (others => '0');
        rb := (others => (others => '0'));
        first := true;
        prev_k := 0;
        for k in 0 to LANES - 1 loop
          if c(k) = '1' then
            if first then
              len := run_len + k;                           -- 前の語から続いた分 + この語のレーン 0〜k − 1
            else
              len := to_unsigned(k - prev_k, 17);
            end if;
            rv(k) := '1';
            if k = 0 then
              rb(k) := rec(len, prev_a);
            else
              rb(k) := rec(len, lane(wa, k - 1));
            end if;
            first := false;
            prev_k := k;
          end if;
        end loop;
        if first then                                       -- この語に変化が無い
          len := run_len + LANES;
          if len > CLOSE_AT then                            -- 数の欄に収まるうちに閉じる（次の語は新しいレコード）
            rv(0) := '1';
            rb(0) := rec(len, lane(wa, LANES - 1));
            run_len <= (others => '0');
          else
            run_len <= len;
          end if;
        else
          run_len <= to_unsigned(LANES - prev_k, 17);       -- 最後の変化からの続き
        end if;
        rv_b <= rv;
        rec_b <= rb;
      end if;

      -- 段 C: 閉じたレコードを時間の順に詰める
      n := 0;
      rc := (others => (others => '0'));
      if vb = '1' then
        for k in 0 to LANES - 1 loop
          if rv_b(k) = '1' then
            rc(n) := rec_b(k);
            n := n + 1;
          end if;
        end loop;
      end if;
      kc <= n;
      rec_c <= rc;

      -- 段 D: 溜めから 4 個出し、詰めたレコードを後ろに足す
      if bv = '1' and beat_ready = '1' then
        bv <= '0';
      end if;
      ovf <= '0';
      f := fill;
      nb := buf_r;
      can_emit := fill >= 4 and (bv = '0' or beat_ready = '1');
      if can_emit then
        bv <= '1';
        for r in 0 to 3 loop
          bd(32 * r + 31 downto 32 * r) <= buf_r(r);
        end loop;
        for i in 0 to BUF - 5 loop
          nb(i) := buf_r(i + 4);
        end loop;
        f := fill - 4;
      end if;
      if kc > 0 and not broken then
        if f + kc > BUF then
          ovf <= '1';                                       -- 入りきらない: 入れずに溢れを知らせる
          broken <= true;
        else
          for i in 0 to BUF - 1 loop
            for j in 0 to LANES - 1 loop
              if i = f + j and j < kc then
                nb(i) := rec_c(j);
              end if;
            end loop;
          end loop;
          f := f + kc;
        end if;
      end if;
      fill <= f;
      buf_r <= nb;

      if rst = '1' or start = '1' then
        va <= '0';
        vb <= '0';
        kc <= 0;
        run_len <= (others => '0');
        fill <= 0;
        bv <= '0';
        ovf <= '0';
        broken <= false;
      end if;
    end if;
  end process;

  beat_valid <= bv;
  beat_data  <= bd;
  overflow   <= ovf;
end architecture;
