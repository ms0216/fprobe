-- CSR バスの振り分け。マスタの要求を、アドレスが当たる窓のスレーブへ渡し、その応答を返す。
-- どの窓にも当たらなければ自分で bus_ack + bus_err を返す。要求から応答までは最短 3 周期。
-- 応答（ack・err・rdata）はレジスタで返す（窓が増えても、窓の応答をまとめる経路がマスタの中まで伸びないように）。
-- 窓の応答は論理和でまとめる（スレーブは応答の周期以外 rdata・err を 0 にする約束。csr_bus_pkg）。要求は当たった
-- 窓にしか渡らないので、応答する窓は 1 つ。窓ごとの選択（多入力のマルチプレクサ）が要らない。
-- マスタは応答を受けた次の周期から次の要求を出せる（応答を返した周期の bus_req は前の要求の続きとして無視する）。
-- スレーブには bus_req だけを窓ごとに分けて渡す（bus_we / bus_addr / bus_wdata はマスタのものを直接使う）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;

entity csr_decoder is
  generic (
    WINDOWS : csr_windows_t     -- 重ならないこと
  );
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    -- マスタ側
    bus_req   : in  std_logic;
    bus_addr  : in  csr_addr_t;
    bus_ack   : out std_logic;
    bus_err   : out std_logic;
    bus_rdata : out csr_word_t;
    -- スレーブ側（窓ごと）
    s_req     : out std_logic_vector(WINDOWS'range);
    s_ack     : in  std_logic_vector(WINDOWS'range);
    s_err     : in  std_logic_vector(WINDOWS'range);
    s_rdata   : in  csr_word_array_t(WINDOWS'range)
  );
end entity;

architecture rtl of csr_decoder is
  -- 要求の最初の周期に当たる窓を覚え（hits）、次の周期からその窓へ要求を渡す。番地のデコードを要求の経路から
  -- 外すため。代わりに 1 アクセスあたり 1 周期遅れる。
  signal hits     : std_logic_vector(WINDOWS'range) := (others => '0');
  signal active   : boolean := false;
  signal miss_ack : std_logic := '0';
  signal ack_c, err_c : std_logic;                       -- 窓の応答の論理和（組み合わせ）
  signal rdata_c  : csr_word_t;
  signal ack_r, err_r : std_logic := '0';
  signal rdata_r  : csr_word_t := (others => '0');
begin
  check : for i in WINDOWS'range generate
    assert csr_window_ok(WINDOWS(i))
      report "csr_decoder: window size must be a power of two and base aligned to it" severity failure;
  end generate;

  process (all)
    variable ack, err : std_logic;
    variable rdata    : csr_word_t;
  begin
    ack   := miss_ack;
    err   := miss_ack;
    rdata := (others => '0');
    for i in WINDOWS'range loop
      ack   := ack or s_ack(i);
      err   := err or s_err(i);
      rdata := rdata or s_rdata(i);
    end loop;
    ack_c   <= ack;
    err_c   <= err;
    rdata_c <= rdata;
  end process;

  bus_ack   <= ack_r;
  bus_err   <= err_r;
  bus_rdata <= rdata_r;

  s_req <= hits when active and bus_req = '1' else (others => '0');

  process (clk)
  begin
    if rising_edge(clk) then
      miss_ack <= '0';
      ack_r    <= '0';
      if rst = '1' then
        active <= false;
        hits   <= (others => '0');
      elsif not active then
        if bus_req = '1' and ack_r = '0' then
          active <= true;
          for i in WINDOWS'range loop
            hits(i) <= '1' when csr_hit(bus_addr, WINDOWS(i)) else '0';
          end loop;
        end if;
      elsif ack_c = '1' then
        active  <= false;                                 -- 応答した（次の周期にマスタへ返す）
        ack_r   <= '1';
        err_r   <= err_c;
        rdata_r <= rdata_c;
      elsif hits = (hits'range => '0') then
        miss_ack <= '1';                                  -- 未割り当て: 自分で応答する
      end if;
    end if;
  end process;
end architecture;
