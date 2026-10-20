-- 外部メモリの無い構成のイベント記録: 内蔵 RAM の 2**DEPTH_BITS 件のリング（ボード非依存、clk のドメイン）。
-- 仕様: docs/superpowers/specs/2026-10-20-bram-event-ring-design.md
--
-- 口と意味は la_deep_buffer のイベントと読み出しの部分と同じ（PC は同じ手順で読む）:
--   書き: ev_enable の間の ev_valid を 1 周期に 1 件書く（失わない: ev_lost = 0）。古いものから上書きする。
--         ev_enable の立ち上がりで、書いた数を 0 に戻して先頭から書く。
--   読み: read（1 周期）で、read_region = '1' ならリングの read_first 件目から read_count 件（折り返す）を、1 件 16 バイト・
--         下位のバイトから out_* に出す。読む間も書ける。read_region = '0'（ロジアナのリング）はこの構成に無いので何も出さない。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity event_ring is
  generic (
    DEPTH_BITS : positive := 10;             -- 件数 = 2**DEPTH_BITS（1 件 128 ビット）
    ADDR_BITS  : positive := 10              -- read_first・read_count の幅（DEPTH_BITS 以上。余りは件数で折り返す）
  );
  port (
    clk         : in  std_logic;
    rst         : in  std_logic;
    ev_enable   : in  std_logic;
    ev_valid    : in  std_logic;
    ev_data     : in  std_logic_vector(127 downto 0);
    ev_written  : out unsigned(31 downto 0);
    ev_lost     : out unsigned(31 downto 0);
    ev_beats    : out unsigned(31 downto 0);
    read        : in  std_logic;
    read_region : in  std_logic;
    read_first  : in  unsigned(ADDR_BITS - 1 downto 0);
    read_count  : in  unsigned(ADDR_BITS downto 0);
    reading     : out std_logic;
    out_valid   : out std_logic;
    out_ready   : in  std_logic;
    out_data    : out std_logic_vector(7 downto 0)
  );
end entity;

architecture rtl of event_ring is
  constant DEPTH : positive := 2 ** DEPTH_BITS;

  type state_t is (S_IDLE, S_ADDR, S_WAIT, S_SEND);
  signal state : state_t;

  signal we, enable_prev : std_logic;
  signal wptr, raddr, waddr : unsigned(DEPTH_BITS - 1 downto 0);
  signal written : unsigned(31 downto 0);
  signal rdata, beat : std_logic_vector(127 downto 0);
  signal remaining : unsigned(ADDR_BITS downto 0);
  signal byte_index : unsigned(3 downto 0);
begin
  ram_i : entity work.sdp_ram
    generic map (WIDTH => 128, DEPTH => DEPTH)
    port map (clk => clk, we => we, waddr => waddr, wdata => ev_data, raddr => raddr, rdata => rdata);

  we <= ev_valid and ev_enable;
  -- 記録を始める周期のイベントは 0 番へ（wptr はまだ前の記録の続きを指している）
  waddr <= (others => '0') when ev_enable = '1' and enable_prev = '0' else wptr;
  ev_written <= written;
  ev_lost <= (others => '0');
  ev_beats <= to_unsigned(DEPTH, 32);
  reading <= '0' when state = S_IDLE else '1';
  out_valid <= '1' when state = S_SEND else '0';
  out_data <= beat(7 downto 0);

  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        enable_prev <= '0';
        wptr <= (others => '0');
        written <= (others => '0');
        state <= S_IDLE;
        raddr <= (others => '0');
        beat <= (others => '0');
        remaining <= (others => '0');
        byte_index <= (others => '0');
      else
        enable_prev <= ev_enable;
        -- 書き（立ち上がりの周期は先頭から）
        if ev_enable = '1' and enable_prev = '0' then
          if ev_valid = '1' then
            wptr <= to_unsigned(1, DEPTH_BITS);
            written <= to_unsigned(1, 32);
          else
            wptr <= (others => '0');
            written <= (others => '0');
          end if;
        elsif we = '1' then
          wptr <= wptr + 1;
          written <= written + 1;
        end if;

        -- 読み: 番地を出す → RAM の 1 周期の遅れ → 16 バイトを順に出す
        case state is
          when S_IDLE =>
            if read = '1' and read_region = '1' and read_count /= 0 then
              raddr <= resize(read_first, DEPTH_BITS);
              remaining <= read_count;
              state <= S_ADDR;
            end if;
          when S_ADDR =>
            state <= S_WAIT;                       -- raddr が RAM に入る周期
          when S_WAIT =>
            beat <= rdata;
            byte_index <= (others => '0');
            state <= S_SEND;
          when S_SEND =>
            if out_ready = '1' then
              beat <= x"00" & beat(127 downto 8);
              byte_index <= byte_index + 1;
              if byte_index = 15 then
                remaining <= remaining - 1;
                raddr <= raddr + 1;
                if remaining = 1 then
                  state <= S_IDLE;
                else
                  state <= S_ADDR;
                end if;
              end if;
            end if;
        end case;
      end if;
    end if;
  end process;
end architecture;
