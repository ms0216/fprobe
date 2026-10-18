-- メモリ試験（ボード非依存、メモリのクロックドメイン）。仕様: docs/superpowers/specs/2026-10-16-m8-ddr3-design.md §2
--
-- start（1 周期）で base から count 拍（1 拍 = DATA_BITS ビット）に、番地と seed から決まるパターンを書き、
-- 読み戻して比べる。誤りの数・最初の誤りの番地・書き込み / 読み出しにかかった周期数を出す（帯域の測定にも使う）。
-- パターンは番地ごとに異なるので、番地線の断線・短絡（容量より先が折り返す）も誤りとして現れる。
--
-- メモリ I/F（mem_port）: 1 コマンド = 1 拍。cmd_valid / cmd_ready で受け渡し、cmd_write = '1' なら cmd_data を
-- 書く。読み出しの結果は発行順に rd_valid（1 周期）+ rd_data で返る（受け側は止められない）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity mem_test is
  generic (
    ADDR_BITS : positive := 24;
    DATA_BITS : positive := 128               -- 32 の倍数
  );
  port (
    clk         : in  std_logic;
    rst         : in  std_logic;
    -- 制御（start の周期に base / count / seed を取り込む）
    start       : in  std_logic;
    base        : in  unsigned(ADDR_BITS - 1 downto 0);
    count       : in  unsigned(ADDR_BITS downto 0);         -- 1 以上
    seed        : in  std_logic_vector(31 downto 0);
    -- 結果（done から次の start まで有効）
    busy        : out std_logic;
    done        : out std_logic;
    errors      : out unsigned(31 downto 0);
    first_error : out unsigned(ADDR_BITS - 1 downto 0);
    write_cycles : out unsigned(31 downto 0);
    read_cycles : out unsigned(31 downto 0);
    -- メモリ
    cmd_valid   : out std_logic;
    cmd_ready   : in  std_logic;
    cmd_write   : out std_logic;
    cmd_addr    : out unsigned(ADDR_BITS - 1 downto 0);
    cmd_data    : out std_logic_vector(DATA_BITS - 1 downto 0);
    rd_valid    : in  std_logic;
    rd_data     : in  std_logic_vector(DATA_BITS - 1 downto 0)
  );
end entity;

architecture rtl of mem_test is
  type state_t is (S_IDLE, S_WRITE, S_READ, S_LAST, S_DONE);
  signal state     : state_t := S_IDLE;
  signal seed_r    : std_logic_vector(31 downto 0) := (others => '0');
  signal base_r    : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal count_r   : unsigned(ADDR_BITS downto 0) := (others => '0');
  signal issued    : unsigned(ADDR_BITS downto 0) := (others => '0');   -- 発行したコマンド数
  signal received  : unsigned(ADDR_BITS downto 0) := (others => '0');   -- 受け取った読み出し数
  signal addr      : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal rd_addr   : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal valid_r   : std_logic := '0';
  signal errors_r  : unsigned(31 downto 0) := (others => '0');
  signal first_r   : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal wcycles, rcycles : unsigned(31 downto 0) := (others => '0');
  signal check     : boolean := false;                                -- 読み値を比べる（1 段遅らせる）
  signal got       : std_logic_vector(DATA_BITS - 1 downto 0) := (others => '0');
  signal got_addr  : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal differ    : std_logic_vector(DATA_BITS / 32 - 1 downto 0) := (others => '0');   -- 32 ビットごとの不一致
  signal judge     : boolean := false;                                -- differ を数える
  signal judge_addr : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');

  -- 番地 a のパターン: 32 ビット語 i = seed を i * 8 ビット回したもの xor 番地（語ごとにずらす）
  function pattern(a : unsigned; s : std_logic_vector(31 downto 0)) return std_logic_vector is
    variable r : std_logic_vector(DATA_BITS - 1 downto 0);
    variable w : unsigned(31 downto 0);
    variable a32 : unsigned(31 downto 0);
  begin
    a32 := resize(a, 32);
    for i in 0 to DATA_BITS / 32 - 1 loop
      w := rotate_left(unsigned(s), 8 * i) xor rotate_left(a32, 7 * i);
      r(32 * i + 31 downto 32 * i) := std_logic_vector(w);
    end loop;
    return r;
  end function;
begin
  busy         <= '1' when state = S_WRITE or state = S_READ or state = S_LAST else '0';
  done         <= '1' when state = S_DONE else '0';
  errors       <= errors_r;
  first_error  <= first_r;
  write_cycles <= wcycles;
  read_cycles  <= rcycles;
  cmd_valid    <= valid_r;
  cmd_write    <= '1' when state = S_WRITE else '0';
  cmd_addr     <= addr;
  cmd_data     <= pattern(addr, seed_r);

  process (clk)
    variable expect : std_logic_vector(DATA_BITS - 1 downto 0);
  begin
    if rising_edge(clk) then
      check <= false;
      if rst = '1' then
        state   <= S_IDLE;
        valid_r <= '0';
      else
        -- 読み値の比較: 受け取った次の周期に 32 ビットごとの不一致を求め、その次の周期に数える。
        -- 期待値は番地と seed の xor（回転は配線）なのでレジスタに持たず、比較と同じ段で作る
        judge      <= check;
        judge_addr <= got_addr;
        expect := pattern(got_addr, seed_r);
        for i in differ'range loop
          differ(i) <= '1' when got(32 * i + 31 downto 32 * i) /= expect(32 * i + 31 downto 32 * i) else '0';
        end loop;
        if judge and differ /= (differ'range => '0') then
          if errors_r = 0 then
            first_r <= judge_addr;
          end if;
          if errors_r /= x"FFFFFFFF" then
            errors_r <= errors_r + 1;
          end if;
        end if;

        case state is
          when S_IDLE | S_DONE =>
            if start = '1' then
              seed_r   <= seed;
              base_r   <= base;
              count_r  <= count;
              addr     <= base;
              rd_addr  <= base;
              issued   <= (others => '0');
              received <= (others => '0');
              errors_r <= (others => '0');
              first_r  <= (others => '0');
              wcycles  <= (others => '0');
              rcycles  <= (others => '0');
              if count = 0 then
                state <= S_DONE;                                     -- 何もしない
              else
                valid_r <= '1';
                state   <= S_WRITE;
              end if;
            end if;

          when S_WRITE =>
            wcycles <= wcycles + 1;
            if valid_r = '1' and cmd_ready = '1' then
              addr   <= addr + 1;
              issued <= issued + 1;
              if issued + 1 = count_r then
                addr   <= base_r;
                issued <= (others => '0');
                state  <= S_READ;                                     -- 続けて読み出しを出す
              end if;
            end if;

          when S_READ =>
            rcycles <= rcycles + 1;
            if valid_r = '1' and cmd_ready = '1' then
              addr   <= addr + 1;
              issued <= issued + 1;
              if issued + 1 = count_r then
                valid_r <= '0';
              end if;
            end if;
            if rd_valid = '1' then
              got      <= rd_data;
              got_addr <= rd_addr;
              check    <= true;
              rd_addr  <= rd_addr + 1;
              received <= received + 1;
              if received + 1 = count_r then
                state <= S_LAST;
              end if;
            end if;

          when S_LAST =>
            if not check and not judge then
              state <= S_DONE;                                        -- 最後の比較が終わるのを待つ
            end if;
        end case;
      end if;
    end if;
  end process;
end architecture;
