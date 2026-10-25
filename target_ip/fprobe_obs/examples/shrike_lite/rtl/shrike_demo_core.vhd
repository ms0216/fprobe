-- SPDX-License-Identifier: Apache-2.0
-- Shrike-Lite のデモの FPGA 側（ボードのピン以外のすべて）: RP2040 が SPI で使う演算器 + fprobe の観測回路。
--
--   RP2040（MCU）──SPI──▶ demo_spi_slave ──▶ demo_accel（A × B を加算の繰り返しで。演算中のリトライに不具合を仕込んである）
--   fprobe ──JTAG 4 本──▶ 観測回路（OBS: 1 = スナップショット型 obs_snap_core（既定。SLG47910 に収まる）、
--                          2 = ストリーム型 obs_soft_core（1 クロック版。大きい FPGA 向け）、0 = なし（規模の比較用））
--
-- 観測グループ 0（16 ビット）: 1:0 = 演算器の状態（0 IDLE・1 BUSY・2 DONE）、2 = SPI 転送中、3 = リトライのたびに
--   反転、4 = SPI の書き込みのたびに反転、7:5 = SPI の最後のコマンドのアドレス（下位 3 ビット）、11:8 = 残り回数の
--   上位 4 ビット、12 = S2 の取りこぼしのたびに反転（drop_tog）、13 = S4 で固まっている（stuck）、15:14 = 題材の仕込み
--   （mode の下位 2 ビット）。（1 クロックだけのイベントはトグルにして、値の変化として必ず記録に残す）
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity shrike_demo_core is
    generic (
        CLEAR_ON_RESTART : boolean  := false;     -- true で計算器の不具合を直した版
        OBS              : natural  := 1;         -- 観測の回路: 0 = なし、1 = 標本型、2 = 記録型
        CLOCK_HZ         : natural  := 25_000_000;
        FIFO_DEPTH       : positive := 4;
        LOST_BITS        : positive := 8;
        STEP_CYCLES      : positive := 256        -- 計算器の足し算 1 回の周期数
    );
    port (
        clk      : in  std_logic;
        nreset   : in  std_logic;                 -- 電源投入のリセット（0 でリセット。非同期）
        spi_sck  : in  std_logic;
        spi_cs_n : in  std_logic;
        spi_mosi : in  std_logic;
        spi_miso : out std_logic;
        tck      : in  std_logic;
        tms      : in  std_logic;
        tdi      : in  std_logic;
        tdo      : out std_logic;
        tdo_oe   : out std_logic
    );
end entity;

architecture rtl of shrike_demo_core is
    signal rst_s    : std_logic_vector(1 downto 0);
    signal rst      : std_logic;
    signal active   : std_logic;
    signal wr_valid : std_logic;
    signal wr_addr  : std_logic_vector(6 downto 0);
    signal wr_data  : std_logic_vector(15 downto 0);
    signal rd_addr  : std_logic_vector(6 downto 0);
    signal rd_data  : std_logic_vector(15 downto 0);
    signal state    : std_logic_vector(1 downto 0);
    signal restart  : std_logic;
    signal left     : std_logic_vector(7 downto 0);
    signal re_tog   : std_logic;
    signal wr_tog   : std_logic;
    signal dropped  : std_logic;
    signal stuck    : std_logic;
    signal mode     : std_logic_vector(2 downto 0);
    signal drop_tog : std_logic;
    signal probes   : std_logic_vector(15 downto 0);
begin
    -- リセットの同期化（入りは非同期・明けは clk に同期）
    process (clk, nreset)
    begin
        if nreset = '0' then
            rst_s <= (others => '1');
        elsif rising_edge(clk) then
            rst_s <= rst_s(0) & '0';
        end if;
    end process;
    rst <= rst_s(1);

    spi : entity work.demo_spi_slave
        port map (clk => clk, rst => rst, sck => spi_sck, cs_n => spi_cs_n, mosi => spi_mosi, miso => spi_miso,
                  active => active, wr_valid => wr_valid, wr_addr => wr_addr, wr_data => wr_data,
                  rd_addr => rd_addr, rd_data => rd_data);

    accel : entity work.demo_accel
        generic map (CLEAR_ON_RESTART => CLEAR_ON_RESTART, STEP_CYCLES => STEP_CYCLES)
        port map (clk => clk, rst => rst, wr_valid => wr_valid, wr_addr => wr_addr, wr_data => wr_data,
                  rd_addr => rd_addr, rd_data => rd_data, state => state, restart => restart, left => left,
                  dropped => dropped, stuck => stuck, mode => mode);

    process (clk)
    begin
        if rising_edge(clk) then
            if restart = '1' then
                re_tog <= not re_tog;
            end if;
            if wr_valid = '1' then
                wr_tog <= not wr_tog;
            end if;
            if dropped = '1' then                     -- S2 で捨てた: 反転（標本型でも回数を数え落とさない）
                drop_tog <= not drop_tog;
            end if;
            if rst = '1' then
                re_tog <= '0';
                wr_tog <= '0';
                drop_tog <= '0';
            end if;
        end if;
    end process;
    -- 観測の信号: 15:14 MODE の下位、13 固まり（S4）、12 捨てた反転（S2）、11:8 残りの上位、7:5 読みの番地、4 書きの反転、
    -- 3 計算中の開始の反転、2 SPI の転送中、1:0 状態
    probes <= mode(1 downto 0) & stuck & drop_tog & left(7 downto 4) & rd_addr(2 downto 0) & wr_tog & re_tog & active & state;

    snap_g : if OBS = 1 generate
        obs : entity work.obs_snap_core
            port map (clk => clk, rst => rst, probes => probes, tck => tck, tms => tms, tdi => tdi, tdo => tdo,
                      tdo_oe => tdo_oe);
    end generate;
    stream_g : if OBS = 2 generate
        obs : entity work.obs_soft_core
            generic map (GROUPS => 1, FIFO_DEPTH => FIFO_DEPTH, CLOCK_HZ => CLOCK_HZ, LOST_BITS => LOST_BITS)
            port map (clk => clk, rst => rst, probes => probes, tck => tck, tms => tms, tdi => tdi, tdo => tdo,
                      tdo_oe => tdo_oe);
    end generate;
    no_obs_g : if OBS /= 1 and OBS /= 2 generate    -- 0 = なし。範囲外の値も tdo を 0 に駆動する
        tdo <= '0';
        tdo_oe <= '0';
    end generate;
end architecture;
