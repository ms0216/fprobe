-- テスト専用: control_plane + csr_decoder + 2 つのスレーブ。
--   窓 0（ワード 0..63）: RW メモリ。LFSR で 0〜3 周期の待ちを入れて応答する
--   窓 1（ワード 64..67）: 常にエラーを返す
--   それ以外は未割り当て（csr_decoder がエラー）
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;

entity control_plane_harness is
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    in_valid  : in  std_logic;
    in_ready  : out std_logic;
    in_data   : in  std_logic_vector(7 downto 0);
    in_last   : in  std_logic;
    out_valid : out std_logic;
    out_ready : in  std_logic;
    out_data  : out std_logic_vector(7 downto 0);
    out_last  : out std_logic
  );
end entity;

architecture rtl of control_plane_harness is
  constant WINDOWS : csr_windows_t := ((base => 0, size => 64), (base => 64, size => 4));
  signal bus_req, bus_we, bus_ack, bus_err : std_logic;
  signal bus_addr  : csr_addr_t;
  signal bus_wdata, bus_rdata : csr_word_t;
  signal s_req, s_ack, s_err : std_logic_vector(0 to 1) := (others => '0');
  signal s_rdata : csr_word_array_t(0 to 1) := (others => (others => '0'));
  signal memory  : csr_word_array_t(0 to 63) := (others => (others => '0'));
  signal lfsr    : unsigned(15 downto 0) := x"ACE1";
  signal waits   : unsigned(1 downto 0) := "00";
  signal busy    : std_logic := '0';
begin
  dut_i : entity work.control_plane
    port map (clk => clk, rst => rst, in_valid => in_valid, in_ready => in_ready, in_data => in_data,
              in_last => in_last, out_valid => out_valid, out_ready => out_ready, out_data => out_data,
              out_last => out_last, bus_req => bus_req, bus_we => bus_we, bus_addr => bus_addr,
              bus_wdata => bus_wdata, bus_ack => bus_ack, bus_err => bus_err, bus_rdata => bus_rdata);

  decoder_i : entity work.csr_decoder
    generic map (WINDOWS => WINDOWS)
    port map (clk => clk, rst => rst, bus_req => bus_req, bus_addr => bus_addr, bus_ack => bus_ack,
              bus_err => bus_err, bus_rdata => bus_rdata, s_req => s_req, s_ack => s_ack,
              s_err => s_err, s_rdata => s_rdata);

  process (clk)
    variable index : natural range 0 to 63;
  begin
    if rising_edge(clk) then
      lfsr     <= lfsr(14 downto 0) & (lfsr(15) xor lfsr(13) xor lfsr(12) xor lfsr(10));
      s_ack(0)   <= '0';
      s_rdata(0) <= (others => '0');            -- 応答の周期以外は 0
      index      := to_integer(bus_addr(5 downto 0));
      if s_req(0) = '1' and s_ack(0) = '0' then
        if busy = '0' then
          busy  <= '1';
          waits <= lfsr(1 downto 0);
        elsif waits /= 0 then
          waits <= waits - 1;
        else
          busy     <= '0';
          s_ack(0) <= '1';
          if bus_we = '1' then
            memory(index) <= bus_wdata;
          end if;
          s_rdata(0) <= memory(index);
        end if;
      end if;
      s_ack(1) <= s_req(1) and not s_ack(1);
    end if;
  end process;
  s_err <= '0' & s_ack(1);                 -- 窓 1 は常にエラー（応答の周期だけ 1。csr_bus_pkg の約束）
end architecture;
