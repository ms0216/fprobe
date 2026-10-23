-- SPDX-License-Identifier: Apache-2.0
-- Shrike-Lite のデモ: RP2040 から FPGA のレジスタを読み書きする SPI モード 0 のスレーブ（clk で標本化する）。
--
-- 1 回の転送 = CS を下げて 24 ビット（MSB から）: 命令 8 ビット（bit7 = 1 で読み、6:0 = 番地）+ データ 16 ビット。
--   書き: CS を上げたときに 24 ビットそろっていれば wr_valid を 1 周期（途中で終わった転送は何もしない）。
--   読み: 命令の 8 ビット目で rd_addr を出し、その 2 周期後の rd_data（読み出しを登録してよい）を 9 ビット目から MISO に
--   出す（SCK の立ち下がりで変える）。
-- SCK は clk の 1/8 以下（同期化 2 段 + 辺の検出）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity demo_spi_slave is
    port (
        clk      : in  std_logic;
        rst      : in  std_logic;
        sck      : in  std_logic;
        cs_n     : in  std_logic;
        mosi     : in  std_logic;
        miso     : out std_logic;
        active   : out std_logic;                        -- CS が下がっている（同期化の後）
        wr_valid : out std_logic;
        wr_addr  : out std_logic_vector(6 downto 0);
        wr_data  : out std_logic_vector(15 downto 0);
        rd_addr  : out std_logic_vector(6 downto 0);
        rd_data  : in  std_logic_vector(15 downto 0)
    );
end entity;

architecture rtl of demo_spi_slave is
    signal sck_s   : std_logic_vector(2 downto 0);
    signal cs_s    : std_logic_vector(2 downto 0);
    signal mosi_s  : std_logic_vector(1 downto 0);
    signal count   : unsigned(4 downto 0);             -- 受けたビット数（31 で止める）
    signal shift_i : std_logic_vector(23 downto 0);
    signal shift_o : std_logic_vector(15 downto 0);
    signal is_read : std_logic;
    signal load    : std_logic_vector(1 downto 0);      -- 2 周期後に rd_data を送り出しのレジスタへ
    signal miso_r  : std_logic;
    signal addr_r  : std_logic_vector(6 downto 0);
begin
    miso <= miso_r;
    active <= not cs_s(1);
    rd_addr <= addr_r;

    process (clk)
        variable cmd : std_logic_vector(7 downto 0);
    begin
        if rising_edge(clk) then
            sck_s <= sck_s(1 downto 0) & sck;
            cs_s <= cs_s(1 downto 0) & cs_n;
            mosi_s <= mosi_s(0) & mosi;
            wr_valid <= '0';
            load <= load(0) & '0';
            if load(1) = '1' then
                shift_o <= rd_data;
            end if;

            if cs_s(1) = '1' then
                if cs_s(2) = '0' and count = 24 and is_read = '0' then      -- CS が上がった
                    wr_valid <= '1';
                    wr_addr <= shift_i(22 downto 16);
                    wr_data <= shift_i(15 downto 0);
                end if;
                count <= (others => '0');
                is_read <= '0';
                miso_r <= '0';
            elsif sck_s(1) = '1' and sck_s(2) = '0' then                    -- SCK の立ち上がり: 受ける
                shift_i <= shift_i(22 downto 0) & mosi_s(1);
                if count /= 31 then
                    count <= count + 1;
                end if;
                if count = 7 then
                    cmd := shift_i(6 downto 0) & mosi_s(1);
                    is_read <= cmd(7);
                    addr_r <= cmd(6 downto 0);
                    load(0) <= '1';
                end if;
            elsif sck_s(1) = '0' and sck_s(2) = '1' and count >= 8 then   -- SCK の立ち下がり: 送る
                miso_r <= shift_o(15);
                shift_o <= shift_o(14 downto 0) & '0';
            end if;

            if rst = '1' then
                sck_s <= (others => '0');
                cs_s <= (others => '1');
                mosi_s <= (others => '0');
                count <= (others => '0');
                shift_i <= (others => '0');
                shift_o <= (others => '0');
                is_read <= '0';
                load <= (others => '0');
                miso_r <= '0';
                addr_r <= (others => '0');
                wr_valid <= '0';
                wr_addr <= (others => '0');
                wr_data <= (others => '0');
            end if;
        end if;
    end process;
end architecture;
