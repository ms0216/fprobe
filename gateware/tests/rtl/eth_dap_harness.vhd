-- テスト専用: eth_link（UDP のホストリンク）の DAP チャンネルに dap_harness（DAP 処理 + SWD、ターゲットモデル用の線）を
-- つなぐ。CONTROL は読み捨てて応答しない（DAP の経路だけを見る）。Ethernet 越しの DAP の端から端までの確認用。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity eth_dap_harness is
  port (
    clk          : in  std_logic;
    rst          : in  std_logic;
    ref_clk      : in  std_logic;
    crs_dv       : in  std_logic;
    rxd          : in  std_logic_vector(1 downto 0);
    txd          : out std_logic_vector(1 downto 0);
    tx_en        : out std_logic;
    swclk        : out std_logic;
    swdio        : out std_logic;
    target_swdio : in  std_logic;
    nreset_ext   : in  std_logic
  );
end entity;

architecture rtl of eth_dap_harness is
  signal req_valid, req_ready, req_last, rsp_valid, rsp_ready, rsp_last : std_logic;
  signal req_data, rsp_data : std_logic_vector(7 downto 0);
begin
  link_i : entity work.eth_link
    generic map (MAC => x"026670720001", IP => x"C0A84D02")
    port map (clk => clk, rst => rst, ref_clk => ref_clk, crs_dv => crs_dv, rxd => rxd, txd => txd, tx_en => tx_en,
              peer_known => open, diag => open,
              ctrl_out_valid => open, ctrl_out_ready => '1', ctrl_out_data => open, ctrl_out_last => open,
              ctrl_in_valid => '0', ctrl_in_ready => open, ctrl_in_data => x"00", ctrl_in_last => '0',
              dap_out_valid => req_valid, dap_out_ready => req_ready, dap_out_data => req_data, dap_out_last => req_last,
              dap_in_valid => rsp_valid, dap_in_ready => rsp_ready, dap_in_data => rsp_data, dap_in_last => rsp_last,
              swo_in_valid => '0', swo_in_ready => open, swo_in_data => x"00",
              stream_in_valid => '0', stream_in_ready => open, stream_in_data => x"00",
              uart_out_valid => open, uart_out_ready => '1', uart_out_data => open,
              uart_in_valid => '0', uart_in_ready => open, uart_in_data => x"00",
              line_rate => open, line_changed => open);

  dap_i : entity work.dap_harness
    port map (clk => clk, rst => rst, packet_size => 512,
              in_valid => req_valid, in_ready => req_ready, in_data => req_data, in_last => req_last,
              out_valid => rsp_valid, out_ready => rsp_ready, out_data => rsp_data, out_last => rsp_last,
              swclk => swclk, swdio => swdio, tdi => open, target_swdio => target_swdio, nreset => open,
              nreset_ext => nreset_ext);
end architecture;
