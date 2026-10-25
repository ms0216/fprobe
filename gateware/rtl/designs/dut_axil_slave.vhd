-- AXI4-Lite のスレーブ → dut_stream_core のレジスタ口。32 ビットの書きだけ（wstrb は見ない）、語番地は addr(5 downto 2)、
-- 応答は常に OKAY。1 回に 1 つの操作を直列に処理する（書きが先。AW と W が同時に揃ったときだけ受ける）。
-- DATA の読みの拍 reg_rd_en は AR の受理の次の周期に 1 クロックだけ立つ（FIFO の取り出しが読みごとにちょうど 1 回）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity dut_axil_slave is
  port (
    clk           : in  std_logic;
    rst           : in  std_logic;
    s_axi_awaddr  : in  std_logic_vector(31 downto 0);
    s_axi_awvalid : in  std_logic;
    s_axi_awready : out std_logic;
    s_axi_wdata   : in  std_logic_vector(31 downto 0);
    s_axi_wstrb   : in  std_logic_vector(3 downto 0);
    s_axi_wvalid  : in  std_logic;
    s_axi_wready  : out std_logic;
    s_axi_bresp   : out std_logic_vector(1 downto 0);
    s_axi_bvalid  : out std_logic;
    s_axi_bready  : in  std_logic;
    s_axi_araddr  : in  std_logic_vector(31 downto 0);
    s_axi_arvalid : in  std_logic;
    s_axi_arready : out std_logic;
    s_axi_rdata   : out std_logic_vector(31 downto 0);
    s_axi_rresp   : out std_logic_vector(1 downto 0);
    s_axi_rvalid  : out std_logic;
    s_axi_rready  : in  std_logic;
    reg_wr_en     : out std_logic;
    reg_addr      : out unsigned(3 downto 0);
    reg_wdata     : out std_logic_vector(31 downto 0);
    reg_rd_en     : out std_logic;
    reg_rdata     : in  std_logic_vector(31 downto 0)
  );
end entity;

architecture rtl of dut_axil_slave is
  type state_t is (S_IDLE, S_WR, S_B, S_RD, S_R);
  signal state   : state_t;
  signal addr_r  : unsigned(3 downto 0);
  signal wdata_r : std_logic_vector(31 downto 0);
  signal rdata_r : std_logic_vector(31 downto 0);
  signal take_wr : std_logic;
  signal take_rd : std_logic;
begin
  take_wr <= '1' when state = S_IDLE and s_axi_awvalid = '1' and s_axi_wvalid = '1' else '0';
  take_rd <= '1' when state = S_IDLE and s_axi_arvalid = '1' and take_wr = '0' else '0';

  s_axi_awready <= take_wr;
  s_axi_wready  <= take_wr;
  s_axi_arready <= take_rd;
  s_axi_bresp   <= "00";
  s_axi_rresp   <= "00";
  s_axi_bvalid  <= '1' when state = S_B else '0';
  s_axi_rvalid  <= '1' when state = S_R else '0';
  s_axi_rdata   <= rdata_r;

  reg_wr_en <= '1' when state = S_WR else '0';
  reg_rd_en <= '1' when state = S_RD else '0';
  reg_addr  <= addr_r;
  reg_wdata <= wdata_r;

  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        state   <= S_IDLE;
        addr_r  <= (others => '0');
        wdata_r <= (others => '0');
        rdata_r <= (others => '0');
      else
        case state is
          when S_IDLE =>
            if take_wr = '1' then
              addr_r  <= unsigned(s_axi_awaddr(5 downto 2));
              wdata_r <= s_axi_wdata;
              state   <= S_WR;
            elsif take_rd = '1' then
              addr_r <= unsigned(s_axi_araddr(5 downto 2));
              state  <= S_RD;
            end if;
          when S_WR =>
            state <= S_B;
          when S_B =>
            if s_axi_bready = '1' then
              state <= S_IDLE;
            end if;
          when S_RD =>
            rdata_r <= reg_rdata;
            state   <= S_R;
          when S_R =>
            if s_axi_rready = '1' then
              state <= S_IDLE;
            end if;
        end case;
      end if;
    end if;
  end process;
end architecture;
