-- 多段トリガ（トリガシーケンサ、ボード非依存、clk のドメイン）。
-- 仕様: docs/superpowers/specs/2026-10-16-m9c-trigger-sequencer-design.md
--
-- 事象: ev(0) = パターン A、ev(1) = パターン B（ピンの値・エッジ。la_front と同じ意味。毎周期判定）、
--       ev(2) = match（DAP の番地一致）、ev(3) = host（ホストの指令）。どれも 1 周期のパルス（パターンは成立中ずっと）。
-- 段 k: ev and cond(k) /= 0 の周期を数え、count(k) 回（0 は 1 と同じ）で次の段へ。段 k > 0 に入ってから
--       within(k) 周期（0 = 制限なし）の間に進めなければ段 0 に戻る。段 stages - 1 を抜けたら fire を 1 周期出し、
--       fired を立てて止まる。enable = '0' で段 0 に戻して止める（0→1 で次の発火を待つ）。
-- 1 周期に進むのは高々 1 段（段を進めた事象は次の段に数えない）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity trig_seq is
  generic (
    WIDTH : positive := 16
  );
  port (
    clk        : in  std_logic;
    rst        : in  std_logic;
    probes     : in  std_logic_vector(WIDTH - 1 downto 0);   -- 非同期でよい（2 段で同期化）
    match      : in  std_logic;
    host       : in  std_logic;
    -- 設定（動作中は変えないこと）
    enable     : in  std_logic;
    stages     : in  unsigned(2 downto 0);                    -- 1〜4（0 は 1）
    a_mask, a_value, a_rise, a_fall : in std_logic_vector(WIDTH - 1 downto 0);
    b_mask, b_value, b_rise, b_fall : in std_logic_vector(WIDTH - 1 downto 0);
    cond0, cond1, cond2, cond3       : in std_logic_vector(3 downto 0);
    count0, count1, count2, count3   : in unsigned(15 downto 0);
    within1, within2, within3        : in unsigned(31 downto 0);
    -- 状態
    fire       : out std_logic;
    fired      : out std_logic;
    stage      : out unsigned(1 downto 0)
  );
end entity;

architecture rtl of trig_seq is
  signal p1, s, prev : std_logic_vector(WIDTH - 1 downto 0) := (others => '0');
  signal ev          : std_logic_vector(3 downto 0) := (others => '0');
  signal stage_r     : unsigned(1 downto 0) := (others => '0');
  signal left        : unsigned(15 downto 0) := (others => '0');   -- この段で残りの回数（0 は 1 と同じ）
  signal tleft       : unsigned(31 downto 0) := (others => '0');   -- この段の残り時間（周期）
  signal tlimit      : boolean := false;                            -- この段に制限時間がある
  signal fire_r, fired_r : std_logic := '0';

  -- 値一致かつエッジ条件（エッジ指定なしなら真）
  function hit(v, p, mask, value, rise, fall : std_logic_vector) return std_logic is
    variable edges : boolean;
  begin
    if (rise or fall) = (rise'range => '0') then
      edges := true;
    else
      edges := ((rise and v and not p) or (fall and p and not v)) /= (v'range => '0');
    end if;
    if (v and mask) = (value and mask) and edges then
      return '1';
    end if;
    return '0';
  end function;
begin
  fire  <= fire_r;
  fired <= fired_r;
  stage <= stage_r;

  -- 段に入るときに、その段の回数・制限時間を残り（left・tleft）として読み込み、以後は 0 と比べるだけにする
  -- （段ごとの設定を選ぶ処理を毎周期の判定から外す。sys 100 MHz のタイミングのため）
  process (clk)
    variable cond : std_logic_vector(3 downto 0);
    variable last : unsigned(1 downto 0);

    procedure enter(k : unsigned(1 downto 0)) is
      variable need   : unsigned(15 downto 0);
      variable within : unsigned(31 downto 0);
    begin
      case k is
        when "00"   => need := count0; within := (others => '0');
        when "01"   => need := count1; within := within1;
        when "10"   => need := count2; within := within2;
        when others => need := count3; within := within3;
      end case;
      stage_r <= k;
      left    <= need;                                       -- 0 は 1 と同じ（残り 1 以下で進む）
      tleft  <= within;
      tlimit <= within /= 0;
    end procedure;
  begin
    if rising_edge(clk) then
      p1   <= probes;
      s    <= p1;
      prev <= s;
      ev   <= host & match & hit(s, prev, b_mask, b_value, b_rise, b_fall)
                           & hit(s, prev, a_mask, a_value, a_rise, a_fall);
      fire_r <= '0';

      case stage_r is
        when "00"   => cond := cond0;
        when "01"   => cond := cond1;
        when "10"   => cond := cond2;
        when others => cond := cond3;
      end case;
      last := (others => '0');
      if stages > 1 then
        last := resize(stages - 1, 2);
      end if;

      if rst = '1' or enable = '0' then
        enter("00");
        fired_r <= '0';
      elsif fired_r = '0' then
        if tlimit then
          tleft <= tleft - 1;
        end if;
        if tlimit and tleft = 0 then
          enter("00");                                        -- 制限時間切れ: 最初から
        elsif (ev and cond) /= "0000" then
          if left <= 1 then
            if stage_r = last then
              fire_r  <= '1';
              fired_r <= '1';
              enter("00");
            else
              enter(stage_r + 1);
            end if;
          else
            left <= left - 1;
          end if;
        end if;
      end if;
    end if;
  end process;
end architecture;
