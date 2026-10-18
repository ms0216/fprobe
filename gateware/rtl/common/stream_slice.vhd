-- ストリームのレジスタスライス（2 段のスキッドバッファ）。in_ready・out_valid・out_data がすべてレジスタ出力に
-- なるので、両側の組み合わせ回路が 1 周期の中でつながらない。途切れなく 1 語 / 周期で流せる。
library ieee;
use ieee.std_logic_1164.all;

entity stream_slice is
  generic (
    WIDTH : positive
  );
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    in_valid  : in  std_logic;
    in_ready  : out std_logic;
    in_data   : in  std_logic_vector(WIDTH - 1 downto 0);
    out_valid : out std_logic;
    out_ready : in  std_logic;
    out_data  : out std_logic_vector(WIDTH - 1 downto 0)
  );
end entity;

architecture rtl of stream_slice is
  signal main_valid, skid_valid : std_logic := '0';
  signal main_data, skid_data   : std_logic_vector(WIDTH - 1 downto 0) := (others => '0');
  signal ready_r                : std_logic := '0';
begin
  process (clk)
    variable take, give : boolean;
  begin
    if rising_edge(clk) then
      if rst = '1' then
        main_valid <= '0';
        skid_valid <= '0';
        ready_r    <= '0';
      else
        take := in_valid = '1' and ready_r = '1';
        give := main_valid = '1' and out_ready = '1';
        if give then
          if skid_valid = '1' then
            main_data  <= skid_data;                 -- 控えを前へ
            skid_valid <= '0';
            if take then
              skid_data  <= in_data;
              skid_valid <= '1';
            end if;
          elsif take then
            main_data <= in_data;
          else
            main_valid <= '0';
          end if;
        elsif take then
          if main_valid = '0' then
            main_data  <= in_data;
            main_valid <= '1';
          else
            skid_data  <= in_data;                   -- 出口が詰まっている: 控えに置く
            skid_valid <= '1';
          end if;
        end if;
        -- 次の周期に 1 語受け取れるのは控えが空くとき（今の周期の結果で決める）
        ready_r <= '1';
        if (skid_valid = '1' and not give) or (main_valid = '1' and not give and take) then
          ready_r <= '0';
        end if;
      end if;
    end if;
  end process;

  in_ready  <= ready_r;
  out_valid <= main_valid;
  out_data  <= main_data;
end architecture;
