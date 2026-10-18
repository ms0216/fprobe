-- DAP のトランザクションモニタ: dap_processor が swd_phy に出した転送を覗き、
--   - ホストが最後に書いた SELECT（swd_injector が割り込み後に戻す値）
--   - MEM-AP の CSW（サイズ・自動増加）と TAR を追い、DRW のアクセス 1 回ごとの番地
-- を求め、番地・値の一致でパルス match を出す（「この番地に書いたら波形トリガ」）。
-- 仕様: docs/superpowers/specs/2026-10-16-m9-cross-trigger-design.md §2
--
-- 一致 = ((番地 xor match_addr) and addr_mask) = 0 かつ 向き（on_write / on_read）かつ
--        書き込みなら ((値 xor match_data) and data_mask) = 0（読みは値を見ない。AP の読み値は 1 つ後で返るため）。
-- 応答が OK の転送だけを数える。
-- イベント: 応答のあった転送はすべて（WAIT・FAULT も）1 件として ev_* に出す（ev_valid 1 周期）。ev_addr は DRW なら
-- TAR から求めたメモリの番地、それ以外はレジスタの番地 A[3:2] & "00"。ev_data は書き込み値か線上の読み値。ponytail: AP は区別しない（Cortex-M の MEM-AP は 1 つ）。BD0〜3 は追わない。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity dap_monitor is
  port (
    clk           : in  std_logic;
    rst           : in  std_logic;
    -- 覗く（dap_processor → swd_phy）
    cmd_fire      : in  std_logic;                       -- 命令が受理された周期に 1
    cmd_op        : in  std_logic_vector(1 downto 0);
    cmd_req       : in  std_logic_vector(3 downto 0);
    cmd_data      : in  std_logic_vector(31 downto 0);
    rsp_valid     : in  std_logic;                       -- その命令の応答（dap_processor 宛て）
    rsp_ack       : in  std_logic_vector(2 downto 0);
    rsp_data      : in  std_logic_vector(31 downto 0);
    rsp_parity    : in  std_logic;
    -- 設定
    match_addr    : in  std_logic_vector(31 downto 0);
    addr_mask     : in  std_logic_vector(31 downto 0);
    match_data    : in  std_logic_vector(31 downto 0);
    data_mask     : in  std_logic_vector(31 downto 0);
    on_write      : in  std_logic;
    on_read       : in  std_logic;
    -- 結果
    select_shadow : out std_logic_vector(31 downto 0);
    -- ホストが MEM-AP（bank 0）に書いた CSW と、TAR（DRW アクセスの自動増加を含む）の控え。
    -- shadow_valid = 両方をホストが書いた後で、TAR が追える増加方式（なし / 単純増加）
    csw_shadow    : out std_logic_vector(31 downto 0);
    tar_shadow    : out std_logic_vector(31 downto 0);
    shadow_valid  : out std_logic;
    match         : out std_logic;
    -- イベント（転送 1 件ごと）
    ev_valid      : out std_logic;
    ev_request    : out std_logic_vector(3 downto 0);
    ev_ack        : out std_logic_vector(2 downto 0);
    ev_parity     : out std_logic;
    ev_addr       : out std_logic_vector(31 downto 0);
    ev_data       : out std_logic_vector(31 downto 0)
  );
end entity;

architecture rtl of dap_monitor is
  constant OP_TRANSFER : std_logic_vector(1 downto 0) := "10";
  constant ACK_OK      : std_logic_vector(2 downto 0) := "001";

  signal pending : boolean := false;
  signal req     : std_logic_vector(3 downto 0) := (others => '0');
  signal data    : std_logic_vector(31 downto 0) := (others => '0');
  signal sel     : std_logic_vector(31 downto 0) := (others => '0');
  signal tar     : unsigned(31 downto 0) := (others => '0');
  signal size    : std_logic_vector(2 downto 0) := "010";
  signal inc     : std_logic_vector(1 downto 0) := "00";
  signal csw     : std_logic_vector(31 downto 0) := (others => '0');
  signal csw_known, tar_known : boolean := false;
  signal match_r : std_logic := '0';
  signal ev_valid_r  : std_logic := '0';
  signal ev_request_r : std_logic_vector(3 downto 0) := (others => '0');
  signal ev_ack_r    : std_logic_vector(2 downto 0) := (others => '0');
  signal ev_parity_r : std_logic := '0';
  signal ev_addr_r, ev_data_r : std_logic_vector(31 downto 0) := (others => '0');
begin
  process (clk)
    variable ap, rnw   : boolean;
    variable a         : std_logic_vector(1 downto 0);
    variable addr_ok   : boolean;
    variable data_ok   : boolean;
  begin
    if rising_edge(clk) then
      match_r    <= '0';
      ev_valid_r <= '0';
      if rst = '1' then
        pending <= false;
        sel     <= (others => '0');
        tar     <= (others => '0');
        size    <= "010";
        inc     <= "00";
        csw_known <= false;
        tar_known <= false;
      else
        if cmd_fire = '1' then
          pending <= cmd_op = OP_TRANSFER;
          req     <= cmd_req;
          data    <= cmd_data;
        elsif rsp_valid = '1' and pending then
          pending      <= false;
          ev_valid_r   <= '1';
          ev_request_r <= req;
          ev_ack_r     <= rsp_ack;
          ev_parity_r  <= rsp_parity and req(1);
          if req(1) = '1' then
            ev_data_r <= rsp_data;
          else
            ev_data_r <= data;
          end if;
          if req(0) = '1' and req(3 downto 2) = "11" and sel(7 downto 4) = "0000" then
            ev_addr_r <= std_logic_vector(tar);                      -- DRW: メモリの番地
          else
            ev_addr_r <= (31 downto 4 => '0') & req(3 downto 2) & "00";
          end if;
          if rsp_ack = ACK_OK then
            ap  := req(0) = '1';
            rnw := req(1) = '1';
            a   := req(3 downto 2);
            if not ap and not rnw and a = "10" then
              sel <= data;                                           -- DP SELECT
            elsif ap and sel(7 downto 4) = "0000" then               -- MEM-AP bank 0
              if a = "00" and not rnw then                           -- CSW
                size <= data(2 downto 0);
                inc  <= data(5 downto 4);
                csw  <= data;
                csw_known <= true;
              elsif a = "01" and not rnw then                        -- TAR
                tar <= unsigned(data);
                tar_known <= true;
              elsif a = "11" then                                    -- DRW
                addr_ok := ((std_logic_vector(tar) xor match_addr) and addr_mask) = x"00000000";
                data_ok := ((data xor match_data) and data_mask) = x"00000000";
                if addr_ok and ((not rnw and on_write = '1' and data_ok) or (rnw and on_read = '1')) then
                  match_r <= '1';
                end if;
                if inc = "01" then                                   -- 単純増加
                  tar <= tar + shift_left(to_unsigned(1, 32), to_integer(unsigned(size(1 downto 0))));
                end if;
              end if;
            end if;
          end if;
        end if;
      end if;
    end if;
  end process;

  select_shadow <= sel;
  csw_shadow    <= csw;
  tar_shadow    <= std_logic_vector(tar);
  shadow_valid  <= '1' when csw_known and tar_known and inc(1) = '0' else '0';
  ev_valid      <= ev_valid_r;
  ev_request    <= ev_request_r;
  ev_ack        <= ev_ack_r;
  ev_parity     <= ev_parity_r;
  ev_addr       <= ev_addr_r;
  ev_data       <= ev_data_r;
  match         <= match_r;
end architecture;
