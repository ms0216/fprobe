-- CSR バスの振り分け。マスタの要求を、アドレスが当たる窓のスレーブへ渡し、その応答を返す。
-- どの窓にも当たらなければ自分で bus_ack + bus_err を返す。要求から応答までは最短 3 周期。
-- 応答（ack・err・rdata）はレジスタで返す（窓が増えても、窓の応答をまとめる経路がマスタの中まで伸びないように）。
-- 窓の応答は論理和でまとめる（スレーブは応答の周期以外 rdata・err を 0 にする約束。csr_bus_pkg）。要求は当たった
-- 窓にしか渡らないので、応答する窓は 1 つ。窓ごとの選択（多入力のマルチプレクサ）が要らない。
-- マスタは応答を受けた次の周期から次の要求を出せる（応答を返した周期の bus_req は前の要求の続きとして無視する）。
-- スレーブには bus_req を窓ごとに分けて渡す。番地・書き込み値・書き込みの印は、要求の最初の周期にレジスタに取った写し
-- （s_cmd）を全部の窓に配る（2026-10-29: マスタのレジスタから全部の窓へ直接配ると、チップ全体に広がる配線が
-- sys の周期を決めていた。写しは Vivado が負荷の近くに複製してよい）。マスタは要求の間これらを変えない約束なので、写しは
-- マスタの値と同じ。窓への要求（s_req）は、もともと最初の周期の次から出しているので、写しと揃う。
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
    bus_we    : in  std_logic := '0';
    bus_addr  : in  csr_addr_t;
    bus_wdata : in  csr_word_t := (others => '0');
    bus_ack   : out std_logic;
    bus_err   : out std_logic;
    bus_rdata : out csr_word_t;
    -- スレーブ側（窓ごと。s_cmd は全部の窓で共通）
    s_cmd     : out csr_cmd_t;
    s_req     : out std_logic_vector(WINDOWS'range);
    s_rsp     : in  csr_rsp_array_t(WINDOWS'range)
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
  signal we_r     : std_logic := '0';
  signal addr_r   : csr_addr_t := (others => '0');
  signal wdata_r  : csr_word_t := (others => '0');
  signal req_r    : std_logic_vector(WINDOWS'range) := (others => '0');
  attribute max_fanout : integer;
  attribute max_fanout of we_r, addr_r, wdata_r, req_r : signal is 16;
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
      ack   := ack or s_rsp(i).ack;
      err   := err or s_rsp(i).err;
      rdata := rdata or s_rsp(i).rdata;
    end loop;
    ack_c   <= ack;
    err_c   <= err;
    rdata_c <= rdata;
  end process;

  bus_ack   <= ack_r;
  bus_err   <= err_r;
  bus_rdata <= rdata_r;

  s_req   <= req_r;
  s_cmd.we    <= we_r;
  s_cmd.addr  <= addr_r;
  s_cmd.wdata <= wdata_r;

  process (clk)
  begin
    if rising_edge(clk) then
      miss_ack <= '0';
      ack_r    <= '0';
      if rst = '1' then
        active <= false;
        hits   <= (others => '0');
        req_r  <= (others => '0');
      elsif not active then
        if bus_req = '1' and ack_r = '0' then
          active <= true;
          for i in WINDOWS'range loop
            hits(i)  <= '1' when csr_hit(bus_addr, WINDOWS(i)) else '0';
            req_r(i) <= '1' when csr_hit(bus_addr, WINDOWS(i)) else '0';
          end loop;
          we_r    <= bus_we;
          addr_r  <= bus_addr;
          wdata_r <= bus_wdata;
        end if;
      elsif ack_c = '1' then
        active  <= false;                                 -- 応答した（次の周期にマスタへ返す）
        req_r   <= (others => '0');
        ack_r   <= '1';
        err_r   <= err_c;
        rdata_r <= rdata_c;
      elsif hits = (hits'range => '0') then
        miss_ack <= '1';                                  -- 未割り当て: 自分で応答する
      end if;
    end if;
  end process;
end architecture;
