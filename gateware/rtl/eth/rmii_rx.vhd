-- RMII の受信（REF_CLK 50 MHz のドメイン、100 Mb/s 全二重のみ）: プリアンブル・SFD を外し、データのバイトを
-- out_* に流す。フレームの最後のバイトに out_last、そのとき out_good（FCS が正しく、64 バイト以上で、取りこぼし無し）。
-- 仕様: docs/superpowers/specs/2026-10-17-m11-ethernet-link-design.md
--
-- CRS_DV（RMII 1.2）: キャリアが落ちた後もデータが残っていれば、ニブルの 1 つ目のジビットで 0、2 つ目で 1 に
-- 振れる。よって「その周期か次の周期に CRS_DV = 1」のジビットがデータ（最後のデータは必ず 1 の周期）。
-- 受け手（FIFO）が詰まって書けなかったら、そのフレームは捨てる（last と good = 0 を空きができ次第出す）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity rmii_rx is
  port (
    clk       : in  std_logic;                       -- REF_CLK 50 MHz
    rst       : in  std_logic;
    crs_dv    : in  std_logic;
    rxd       : in  std_logic_vector(1 downto 0);
    out_valid : out std_logic;
    out_ready : in  std_logic;
    out_data  : out std_logic_vector(7 downto 0);
    out_last  : out std_logic;
    out_good  : out std_logic
  );
end entity;

architecture rtl of rmii_rx is
  constant RESIDUE : std_logic_vector(31 downto 0) := x"DEBB20E3";   -- データ + FCS を通した CRC（反転前）

  signal r1, r2 : std_logic_vector(1 downto 0) := "00";
  signal c1, c2 : std_logic := '0';
  type state_t is (S_IDLE, S_PREAMBLE, S_DATA, S_SKIP);
  signal state  : state_t := S_IDLE;
  signal sh     : std_logic_vector(7 downto 0) := (others => '0');
  signal nib    : natural range 0 to 3 := 0;
  signal crc    : std_logic_vector(31 downto 0) := (others => '1');
  signal count  : natural range 0 to 64 := 0;        -- バイト数（64 で飽和）
  signal held   : std_logic_vector(7 downto 0) := (others => '0');
  signal have   : boolean := false;                  -- held に前のバイトがある
  signal lost   : boolean := false;
  signal end_due : boolean := false;                 -- 取りこぼしたフレームの終わりの印を出す
  signal ov     : std_logic := '0';
  signal od     : std_logic_vector(7 downto 0) := (others => '0');
  signal ol, og : std_logic := '0';

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
  out_valid <= ov;
  out_data  <= od;
  out_last  <= ol;
  out_good  <= og;

  process (clk)
    variable dv : boolean;
    variable b  : std_logic_vector(7 downto 0);

    -- 1 語を出す（受け手が詰まっていれば取りこぼし）
    procedure emit(d : std_logic_vector(7 downto 0); last, good : boolean) is
    begin
      if ov = '1' and out_ready = '0' then
        lost <= true;
        if last then
          end_due <= true;
        end if;
      else
        ov <= '1';
        od <= d;
        ol <= '1' when last else '0';
        og <= '1' when good else '0';
      end if;
    end procedure;
  begin
    if rising_edge(clk) then
      r1 <= rxd;
      c1 <= crs_dv;
      r2 <= r1;
      c2 <= c1;
      if ov = '1' and out_ready = '1' then
        ov <= '0';
      end if;
      if rst = '1' then
        state   <= S_IDLE;
        ov      <= '0';
        end_due <= false;
      else
        dv := c2 = '1' or c1 = '1';                    -- ジビット r2 がデータか
        if end_due and (ov = '0' or out_ready = '1') then
          ov <= '1';                                   -- 取りこぼしたフレームの終わり（捨てさせる）
          od <= x"00";
          ol <= '1';
          og <= '0';
          end_due <= false;
        end if;
        case state is
          when S_IDLE =>
            if dv and r2 = "01" then
              state <= S_PREAMBLE;
            end if;

          when S_PREAMBLE =>
            if not dv then
              state <= S_IDLE;
            elsif r2 = "11" then                       -- SFD（0xD5 の最後のジビット）
              state <= S_DATA;
              nib   <= 0;
              crc   <= (others => '1');
              count <= 0;
              have  <= false;
              lost  <= false;
            elsif r2 /= "01" then
              state <= S_SKIP;                         -- プリアンブルでない
            end if;

          when S_DATA =>
            if dv then
              crc <= crc_dibit(crc, r2);
              b   := r2 & sh(7 downto 2);
              sh  <= b;
              if nib = 3 then
                nib <= 0;
                if count < 64 then
                  count <= count + 1;
                end if;
                if have then
                  emit(held, false, false);
                end if;
                held <= b;
                have <= true;
              else
                nib <= nib + 1;
              end if;
            else
              if have then
                emit(held, true, nib = 0 and count = 64 and crc = RESIDUE and not lost);
              end if;
              state <= S_IDLE;
            end if;

          when S_SKIP =>
            if not dv then
              state <= S_IDLE;
            end if;
        end case;
      end if;
    end if;
  end process;
end architecture;
