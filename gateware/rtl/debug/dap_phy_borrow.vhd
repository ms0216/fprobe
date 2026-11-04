-- PHY（swd_phy / jtag_phy の命令 I/F）を上流（dap_processor 等）から借りる回路の共通の部分（swd_injector・dap_sampler）。
--   命令の切り替え: own = '1' の間は自分の転送（OP_TRANSFER、cond = 0）を PHY に出し、上流の ready と応答の valid を 0 に
--   する。own = '0' の間は上流の命令・応答をそのまま通す。own は借り手が上流の命令の境界（up_pending = false）で変える。
--   上流の残り（up_pending）: 上流の命令で PHY が受けて応答をまだ返していない数（PHY は前の応答の前に次を受けることが
--   ある: 2 まで）が 0 でない。切断（enable = '0'）・接続の方式の切り替え（jtag が変わる）では PHY が受けた命令を捨てて応答を
--   出さないので、数えも 0 に戻す（戻さないと捨てられた分が残り、以後ずっと「上流の命令が PHY に残っている」になる）。
--   WAIT の再試行（retry_more）: tries < wait_retry。再試行の回数はいったん受けて（めったに変わらない）、比べた結果も
--   レジスタに。WAIT の応答は数十周期ごとなので、1 周期前の tries との比べで足りる（DAP の処理部からの長い経路と
--   16 ビットの比べを、応答の判定の経路から外す）。
-- 借り手の中だけで使う（試験は借り手の試験から）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.dap_pkg.all;

entity dap_phy_borrow is
  port (
    clk           : in  std_logic;
    rst           : in  std_logic;
    own           : in  std_logic;                       -- PHY を借りている
    enable        : in  std_logic;                       -- 接続中（dap_processor の swd_enable）
    jtag          : in  std_logic;                       -- 接続の方式（dap_processor の jtag）
    -- 自分の転送（own の間）
    own_valid     : in  std_logic;
    own_count     : in  natural range 1 to 64;
    own_req       : in  std_logic_vector(3 downto 0);
    own_data      : in  std_logic_vector(31 downto 0);
    -- WAIT の再試行
    wait_retry    : in  unsigned(15 downto 0);
    tries         : in  unsigned(15 downto 0);
    retry_more    : out std_logic := '0';
    -- 上流（PHY の命令 I/F と同じ）
    up_cmd_valid  : in  std_logic;
    up_cmd_ready  : out std_logic;
    up_cmd_cond   : in  std_logic;
    up_cmd_op     : in  std_logic_vector(1 downto 0);
    up_cmd_count  : in  natural range 1 to 64;
    up_cmd_req    : in  std_logic_vector(3 downto 0);
    up_cmd_data   : in  std_logic_vector(63 downto 0);
    up_rsp_valid  : out std_logic;
    up_pending    : out boolean;
    -- PHY 側（応答の ack / data / parity は借り手と上流へそのまま配る）
    phy_cmd_valid : out std_logic;
    phy_cmd_ready : in  std_logic;
    phy_cmd_cond  : out std_logic;
    phy_cmd_op    : out std_logic_vector(1 downto 0);
    phy_cmd_count : out natural range 1 to 64;
    phy_cmd_req   : out std_logic_vector(3 downto 0);
    phy_cmd_data  : out std_logic_vector(63 downto 0);
    phy_rsp_valid : in  std_logic
  );
end entity;

architecture rtl of dap_phy_borrow is
  signal up_out  : natural range 0 to 2 := 0;
  signal jtag_prev : std_logic := '0';
  signal retry_r : unsigned(15 downto 0) := (others => '0');
begin
  phy_cmd_valid <= own_valid when own = '1' else up_cmd_valid;
  phy_cmd_cond  <= '0' when own = '1' else up_cmd_cond;
  phy_cmd_op    <= OP_TRANSFER when own = '1' else up_cmd_op;
  phy_cmd_count <= own_count when own = '1' else up_cmd_count;
  phy_cmd_req   <= own_req when own = '1' else up_cmd_req;
  phy_cmd_data  <= x"00000000" & own_data when own = '1' else up_cmd_data;
  up_cmd_ready  <= '0' when own = '1' else phy_cmd_ready;
  up_rsp_valid  <= '0' when own = '1' else phy_rsp_valid;
  up_pending    <= up_out /= 0;

  process (clk)
  begin
    if rising_edge(clk) then
      if own = '0' and up_cmd_valid = '1' and phy_cmd_ready = '1' then
        if not (phy_rsp_valid = '1' and up_out /= 0) then
          up_out <= up_out + 1;
        end if;
      elsif phy_rsp_valid = '1' and up_out /= 0 then
        up_out <= up_out - 1;
      end if;
      jtag_prev <= jtag;
      if rst = '1' or enable = '0' or jtag /= jtag_prev then
        up_out <= 0;                                     -- PHY が受けた命令を捨てた（切断・方式の切り替え）
      end if;
    end if;
  end process;

  retry_i : process (clk)
  begin
    if rising_edge(clk) then
      retry_r <= wait_retry;
      if tries < retry_r then
        retry_more <= '1';
      else
        retry_more <= '0';
      end if;
    end if;
  end process;
end architecture;
