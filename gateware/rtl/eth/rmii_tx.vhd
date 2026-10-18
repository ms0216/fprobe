-- RMII の送信（REF_CLK 50 MHz のドメイン、100 Mb/s 全二重のみ）: start の 1 件ごとに、プリアンブル・SFD、in_* の
-- バイト（in_last まで）、60 バイトに満たなければ 0 の詰め物、FCS を送り、フレーム間隔（12 バイト）を空ける。
-- 仕様: docs/superpowers/specs/2026-10-17-m11-ethernet-link-design.md
--
-- データは途切れずに送る必要があるので、送り手はフレームの全バイトを in_* の FIFO に入れてから start（別の FIFO の 1 件）
-- を出す。プリアンブル（32 周期）の間に FIFO の書き込み側の進みが読み出し側に届く。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity rmii_tx is
  port (
    clk         : in  std_logic;                     -- REF_CLK 50 MHz
    rst         : in  std_logic;
    start_valid : in  std_logic;                     -- 送るフレームが 1 つ揃っている
    start_ready : out std_logic;
    in_valid    : in  std_logic;
    in_ready    : out std_logic;
    in_data     : in  std_logic_vector(7 downto 0);
    in_last     : in  std_logic;
    txd         : out std_logic_vector(1 downto 0);
    tx_en       : out std_logic
  );
end entity;

architecture rtl of rmii_tx is
  type state_t is (S_IDLE, S_PREAMBLE, S_DATA, S_PAD, S_FCS, S_GAP);
  signal state  : state_t := S_IDLE;
  signal sh     : std_logic_vector(7 downto 0) := (others => '0');
  signal nib    : natural range 0 to 3 := 0;
  signal n      : natural range 0 to 63 := 0;        -- プリアンブルのジビット数・送ったバイト数（60 で飽和）・FCS・間隔
  signal crc    : std_logic_vector(31 downto 0) := (others => '1');
  signal last_r : boolean := false;
  signal txd_r  : std_logic_vector(1 downto 0) := "00";
  signal en_r   : std_logic := '0';
  signal take   : std_logic;

  function crc_dibit(c : std_logic_vector(31 downto 0); d : std_logic_vector(1 downto 0)) return std_logic_vector is
    variable x : std_logic_vector(31 downto 0) := c;
  begin
    for i in 0 to 1 loop
      if (x(0) xor d(i)) = '1' then
        x := ('0' & x(31 downto 1)) xor x"EDB88320";
      else
        x := '0' & x(31 downto 1);
      end if;
    end loop;
    return x;
  end function;
begin
  txd   <= txd_r;
  tx_en <= en_r;
  start_ready <= '1' when state = S_IDLE else '0';
  -- 次のバイトを取る: データの 4 つ目のジビットを送る周期（バイトの切れ目）
  take     <= '1' when state = S_DATA and nib = 3 and not last_r else
              '1' when state = S_PREAMBLE and n = 31 else '0';
  in_ready <= take;

  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        state <= S_IDLE;
        en_r  <= '0';
        txd_r <= "00";
      else
        case state is
          when S_IDLE =>
            en_r  <= '0';
            txd_r <= "00";
            if start_valid = '1' then
              state <= S_PREAMBLE;
              n     <= 0;
              crc   <= (others => '1');
            end if;

          when S_PREAMBLE =>                           -- 0x55 × 7 + 0xD5 = ジビット 01 × 31 + 11
            en_r  <= '1';
            txd_r <= "11" when n = 31 else "01";
            if n = 31 then
              sh     <= in_data;                       -- take: 最初のバイト
              last_r <= in_last = '1';
              nib    <= 0;
              n      <= 0;
              state  <= S_DATA;
            else
              n <= n + 1;
            end if;

          when S_DATA | S_PAD =>
            txd_r <= sh(1 downto 0);
            crc   <= crc_dibit(crc, sh(1 downto 0));
            sh    <= "00" & sh(7 downto 2);
            if nib = 3 then
              nib <= 0;
              if n < 60 then
                n <= n + 1;
              end if;
              if state = S_DATA and not last_r then
                sh     <= in_data;                     -- take
                last_r <= in_last = '1';
              elsif n + 1 < 60 then
                sh    <= x"00";                        -- 詰め物
                state <= S_PAD;
              else
                n     <= 0;
                state <= S_FCS;
              end if;
            else
              nib <= nib + 1;
            end if;

          when S_FCS =>                                -- 反転した CRC を下位から（16 ジビット）
            txd_r <= not crc(1 downto 0);
            crc   <= "11" & crc(31 downto 2);
            if n = 15 then
              n     <= 0;
              state <= S_GAP;
            else
              n <= n + 1;
            end if;

          when S_GAP =>                                -- フレーム間隔 96 ビット = 48 周期
            en_r  <= '0';
            txd_r <= "00";
            if n = 47 then
              state <= S_IDLE;
            else
              n <= n + 1;
            end if;
        end case;
      end if;
    end if;
  end process;
end architecture;
