-- SPDX-License-Identifier: Apache-2.0
-- fprobe_obs v2: 対象の FPGA の中の信号を JTAG のユーザーの DR で読み出す観測の回路
-- （docs/superpowers/specs/2026-10-20-fpga-observation-v2-design.md。小さい FPGA に入るよう、時刻を数えない形）。
--
-- 口はユーザーの DR（Xilinx の BSCANE2・Intel の sld_virtual_jtag・obs_soft_tap と同じ意味）:
--   tck / tdi / tdo、sel（ユーザーの命令が IR に入っている）、capture / shift / update（DR の状態。sel と組で効く）、
--   tlr（TAP が Test-Logic-Reset）。どれも TCK の立ち上がりで見る（tck_ce = '1' の立ち上がりだけ）。
--   ONE_CLOCK = true のときは tck に clk をつなぎ、tck_ce で TCK の立ち上がりを渡す（obs_tck_sampler。1 クロックで動く）。
--
-- 1 回のスキャン（Capture-DR → Shift-DR → Update-DR）:
--   TDI の最後の 16 ビット = 命令 op(3:0) | 番地(7:4) | データ(15:8)。Update-DR で実行する（16 ビット未満は何もしない）。
--     op 0 = 何もしない、1 = レジスタの書き、2 = レジスタの読み（応答の記録が記録の列に来る）、
--     3 = 印（データ = 番号。この Update-DR を受けた周期に印の記録を書く。時刻合わせ）
--   TDO = 見出し 1 語 + 記録（どれも 32 ビット、下位ビットから）:
--     見出し: count(15:0)（この後の有効な記録の数。Capture-DR の時点で決まる）| bit16 開始してから FIFO が溢れた |
--             bit17 clk 側が記録中 | 31:24 = 0xA6（v2 の印。TDO が 0 か 1 に張り付いたのと見分ける）
--     記録 count 語。その後は 0。
--   記録は最後のビットまで送り出したときに FIFO から取り除く（途中で終わったスキャンでは失わない）。
--
-- 記録（32 ビット、31:30 が種類）。時刻は数えず、時刻のある記録（値・刻み・印）が前の時刻のある記録からの clk の
-- 周期数 delta を 29:16 に持つ（どの種類も同じ位置。記録を組み立てる選択を小さくするため）:
--   0 = 値      delta(29:16) | value(15:0)   選んだ組が変わった（delta 0 = 鎖の始め。PC は次の印で時刻を決める）
--   1 = 刻み    delta(29:16) | 0(15:0)        変化なしで delta が 16383 に達した
--       印      delta(29:16) | 1 | 番号(14:7) | late(6:0)   印の命令を受けた。印の瞬間 = この記録の時刻 − late
--               （late 127 = 127 周期以上待った。瞬間は分からないので PC は時刻合わせに使わない）
--   2 = 溢れ    0(29:16) | lost(15:0)         FIFO が一杯で記録できなかった値の変化の数（LOST_BITS の上限で止まる）。
--               鎖が切れる（次の値は delta 0）
--   3 = 制御    種類(29:26) | 中身             種類 0 = 組の番号（7:0。今の SELECT）、1 = 応答 番地(19:16) | 値(15:0)
--   開始・溢れの後・組の切り替え: 「組 → (溢れ) → 値（今の値）」。印の後は、変化の無い周期に組と今の値を書き直す
--   （記録の途中の印から読み始めても、組・時刻・値が分かる）。印・応答・書き直しは変化の無い周期にだけ書くので、
--   値の記録の時刻は常に変化の SAMPLE_DELAY 周期後（印は待った分を late に入れる）。
--
-- レジスタ（16 ビット）: 0・1 ID "FOBS"（下位 0x4253・上位 0x464F）、2 VERSION（2）、3 GROUPS、4 FIFO_DEPTH、
--   5・6 CLOCK_HZ（下位・上位）、7 SYNC_DELAY（印の瞬間が Update-DR の瞬間より何周期遅いか。半周期の単位、平均。
--   ±0.5 周期）、8 SAMPLE_DELAY（値の記録の時刻が変化より何周期遅いか。probes が同じ clk の登録の出力のとき）、
--   9 LOST_BITS、14 CONTROL（bit0 = 開始）、15 SELECT（観測する組。GROUPS 以上は無視）。
--
-- クロックをまたぐもの: CONTROL・SELECT と読み・印の頼みはトグルで clk へ、溢れ・記録中の印は値のまま TCK へ
-- （どれも次に使うまで数十 TCK 以上動かない）。2 クロック版の clk は TCK の 1/10 程度より遅くないこと。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity obs_core is
    generic (
        GROUPS     : positive := 1;              -- 16 ビットの組の数（256 以下）
        FIFO_DEPTH : positive := 512;            -- 2 の累乗、4〜32768
        CLOCK_HZ   : natural  := 0;              -- clk の周波数（0 = 申告しない）
        LOST_BITS  : positive := 16;             -- 失った数の幅（1〜16。上限で止まる）
        ONE_CLOCK  : boolean  := false           -- tck に clk をつなぎ、tck_ce で TCK の立ち上がりを渡す
    );
    port (
        clk     : in  std_logic;
        rst     : in  std_logic;
        probes  : in  std_logic_vector(16 * GROUPS - 1 downto 0);
        -- ユーザーの DR
        tck     : in  std_logic;
        tck_ce  : in  std_logic := '1';
        tdi     : in  std_logic;
        tdo     : out std_logic;
        sel     : in  std_logic;
        capture : in  std_logic;
        shift   : in  std_logic;
        update  : in  std_logic;
        tlr     : in  std_logic
    );
end entity;

architecture rtl of obs_core is
    function log2(n : positive) return natural is
        variable r : natural := 0;
    begin
        while 2 ** r < n loop
            r := r + 1;
        end loop;
        return r;
    end function;

    function choose(c : boolean; a, b : natural) return natural is
    begin
        if c then
            return a;
        end if;
        return b;
    end function;

    constant DEPTH_BITS : natural := log2(FIFO_DEPTH);
    constant VERSION    : natural := 2;
    constant DELTA_MAX  : natural := 2 ** 14 - 1;
    constant LATE_MAX   : natural := 2 ** 7 - 1;
    -- 遅れの申告（この回路の作りから決まる。gateware/tests の試験が実際と一致することを確かめる）:
    -- 印 = Update-DR の TCK の立ち上がりで反転したトグルを 3 段で同期し、変わったのを見た辺の周期に書く。その辺は
    --   立ち上がりの後の最初の辺（平均 0.5 周期後）から 3 周期後 → 平均 3.5 周期。1 クロック版は TCK の標本化
    --   （obs_tck_sampler）の分が足される（gateware/tests の obs_soft_core の試験で測った値）
    -- 値 = probes の変化を次の辺で比べ、その辺で記録 → 1 周期
    constant SYNC_DELAY_HALVES : natural := choose(ONE_CLOCK, 13, 7);
    constant SAMPLE_DELAY      : natural := 1;
    constant LOST_MAX   : unsigned(LOST_BITS - 1 downto 0) := (others => '1');

    constant OP_WRITE : std_logic_vector(3 downto 0) := x"1";
    constant OP_READ  : std_logic_vector(3 downto 0) := x"2";
    constant OP_SYNC  : std_logic_vector(3 downto 0) := x"3";
    constant REG_CONTROL : natural := 14;
    constant REG_SELECT  : natural := 15;

    constant T_VALUE    : std_logic_vector(1 downto 0) := "00";
    constant T_MARK     : std_logic_vector(1 downto 0) := "01";
    constant T_OVERFLOW : std_logic_vector(1 downto 0) := "10";
    constant T_CTRL     : std_logic_vector(1 downto 0) := "11";
    constant C_GROUP    : std_logic_vector(3 downto 0) := x"0";
    constant C_RESPONSE : std_logic_vector(3 downto 0) := x"1";

    subtype count_t is unsigned(DEPTH_BITS downto 0);
    subtype group_t is natural range 0 to GROUPS - 1;
    subtype delta_t is unsigned(13 downto 0);
    subtype word16_t is std_logic_vector(15 downto 0);
    constant DELTA_ONE  : delta_t := to_unsigned(1, 14);                 -- 記録を書いた直後の delta（次の周期で 1）
    -- 制御の記録（組の番号・応答）の上位 14 ビットと下位 16 ビット
    constant GROUP_HEAD : std_logic_vector(13 downto 0) := C_GROUP & (9 downto 0 => '0');

    function response_head(addr : std_logic_vector(3 downto 0)) return std_logic_vector is
    begin
        return C_RESPONSE & "000000" & addr;
    end function;

    function group_low(g : group_t) return word16_t is
    begin
        return x"00" & std_logic_vector(to_unsigned(g, 8));
    end function;

    -- 読みの応答の値（CONTROL・SELECT は TCK 側の値。書いた後は動かない）
    function register_value(addr : std_logic_vector(3 downto 0); control : std_logic; select_group : group_t)
        return word16_t is
    begin
        case to_integer(unsigned(addr)) is
            when 0 => return x"4253";
            when 1 => return x"464F";
            when 2 => return std_logic_vector(to_unsigned(VERSION, 16));
            when 3 => return std_logic_vector(to_unsigned(GROUPS, 16));
            when 4 => return std_logic_vector(to_unsigned(FIFO_DEPTH, 16));
            when 5 => return std_logic_vector(to_unsigned(CLOCK_HZ mod 65536, 16));
            when 6 => return std_logic_vector(to_unsigned(CLOCK_HZ / 65536, 16));
            when 7 => return std_logic_vector(to_unsigned(SYNC_DELAY_HALVES, 16));
            when 8 => return std_logic_vector(to_unsigned(SAMPLE_DELAY, 16));
            when 9 => return std_logic_vector(to_unsigned(LOST_BITS, 16));
            when REG_CONTROL => return (0 => control, others => '0');
            when REG_SELECT => return std_logic_vector(to_unsigned(select_group, 16));
            when others => return (others => '0');
        end case;
    end function;

    -- FIFO
    signal w_valid : std_logic;
    signal w_data  : std_logic_vector(31 downto 0);
    signal w_free  : count_t;
    signal r_pop   : std_logic;
    signal r_data  : std_logic_vector(31 downto 0);
    signal r_count : count_t;

    ---------------------------------------------------------------- TCK 側
    -- TCK は動かないことがあるので reset を持たず、tlr（Test-Logic-Reset）で初期化する。tlr より前に使う信号は無い:
    -- t_cmd は 16 ビット受けた後（t_bits）、t_cnt・t_flags は Capture-DR の後、t_req_* は t_req_tog が変わった後にだけ読まれ、
    -- t_reg_tog・t_req_tog は値そのものに意味が無い（clk 側が reset で今の値を写し取って、変わったのを見る）。
    -- よって宣言の初期値を合成が無視しても動作は変わらない（tlr までの t_control・t_select を除く。clk 側は reset の明けに
    -- t_control を写すので、構成の直後に TCK が来ないまま reset が明けても記録は始まらない前提 = 初期値 0 に頼る）。
    signal t_in_hdr    : std_logic := '0';                          -- 今の語は見出し
    signal t_cnt       : count_t := (others => '0');                -- 見出しの count（Capture-DR の時点の記録の数）
    signal t_flags     : std_logic_vector(1 downto 0) := (others => '0');   -- 見出しの bit17・16
    signal t_hdr       : std_logic_vector(31 downto 0);
    signal t_cmd       : std_logic_vector(15 downto 0) := (others => '0');
    signal t_bits      : unsigned(4 downto 0) := (others => '0');   -- 受けた TDI のビット数（16 で止める）
    signal t_pos       : unsigned(4 downto 0) := (others => '0');   -- 語の中の位置
    signal t_record    : std_logic := '0';                          -- 今の語は FIFO の先頭（r_data）
    signal t_left      : count_t := (others => '0');                -- このスキャンでまだ出していない有効な記録の数
    signal t_control   : std_logic := '0';
    signal t_select    : group_t := 0;
    signal t_reg_tog   : std_logic := '0';                          -- CONTROL・SELECT を書いた
    signal t_req_tog   : std_logic := '0';                          -- 読み・印を頼んだ
    signal t_req_sync  : std_logic := '0';                          -- 頼みは印（0 = 読み）
    signal t_req_addr  : std_logic_vector(3 downto 0) := (others => '0');
    signal t_req_data  : std_logic_vector(7 downto 0) := (others => '0');
    signal t_ovf_1     : std_logic := '0';
    signal t_ovf_2     : std_logic := '0';
    signal t_run_1     : std_logic := '0';
    signal t_run_2     : std_logic := '0';
    signal t_tlr_q     : std_logic := '0';                          -- 前の TCK で Test-Logic-Reset だった

    ---------------------------------------------------------------- clk 側
    type state_t is (S_IDLE, S_RESYNC, S_OVERFLOW, S_GROUP, S_VALUE, S_RUN);
    signal state      : state_t;
    signal reg_sync   : std_logic_vector(2 downto 0);
    signal req_sync   : std_logic_vector(2 downto 0);
    signal running    : std_logic;
    signal sel_group  : group_t;
    signal prev       : std_logic_vector(15 downto 0);
    signal delta      : unsigned(13 downto 0);                     -- 前の時刻のある記録からの周期数
    signal chained    : std_logic;                                  -- delta が前の時刻のある記録から数えている
    signal lost       : unsigned(LOST_BITS - 1 downto 0);
    signal overflowed : std_logic;
    signal dropping   : std_logic;                                  -- 溢れたので S_RESYNC にいる
    signal reload     : std_logic;                                  -- リセットが明けた: TCK 側の設定を写し直す
    signal p_sync     : std_logic;                                  -- 書くべき印
    signal p_seq      : std_logic_vector(7 downto 0);
    signal late       : unsigned(6 downto 0);
    signal p_group    : std_logic;                                  -- 印の後の組の書き直し
    signal p_value    : std_logic;                                  -- 印の後の今の値の書き直し
    signal p_switch   : std_logic;                                  -- 記録中の組の切り替えの待ち（R17）
    signal next_group : group_t;                                    -- 切り替え先（トグルを見た周期に写した SELECT）
    signal p_resp     : std_logic;                                  -- 書くべき応答
    signal p_addr     : std_logic_vector(3 downto 0);
begin
    -- FIFO_DEPTH は 2 の累乗で 4〜32768（申告と実際の深さを一致させ、再同期の空き 4 語がそろうように。count は 16 ビット）
    assert FIFO_DEPTH >= 4 and FIFO_DEPTH <= 32768 and 2 ** DEPTH_BITS = FIFO_DEPTH
        report "obs_core: FIFO_DEPTH must be a power of two from 4 to 32768" severity failure;
    assert LOST_BITS <= 16 report "obs_core: LOST_BITS must be 1 to 16" severity failure;
    assert GROUPS <= 256 report "obs_core: GROUPS must be 1 to 256" severity failure;

    fifo : entity work.obs_async_fifo
        generic map (WIDTH => 32, DEPTH_BITS => DEPTH_BITS, SAME_CLOCK => ONE_CLOCK)
        port map (w_clk => clk, w_valid => w_valid, w_data => w_data, w_free => w_free,
                  r_clk => tck, r_pop => r_pop, r_data => r_data, r_count => r_count);

    ---------------------------------------------------------------- TCK 側
    -- TDO は語の中の位置 t_pos のビットを選んで出す（語をシフトレジスタに読み込まない。FIFO の先頭 r_data は
    -- 取り除くまで動かず、取り除いた周期に次の記録になる）
    t_hdr <= x"A6" & "000000" & t_flags & std_logic_vector(resize(t_cnt, 16));
    tdo <= t_hdr(to_integer(t_pos)) when t_in_hdr = '1' else
           r_data(to_integer(t_pos)) when t_record = '1' else
           '0';
    -- 記録の語の最後のビットを送り出す周期に取り除く
    r_pop <= tck_ce and sel and shift and t_record and not tlr when t_pos = 31 else '0';

    process (tck)
        variable op        : std_logic_vector(3 downto 0);
        variable addr      : natural range 0 to 15;
        variable data      : unsigned(7 downto 0);
    begin
        if rising_edge(tck) and tck_ce = '1' then
            t_ovf_1 <= overflowed;
            t_ovf_2 <= t_ovf_1;
            t_run_1 <= running;
            t_run_2 <= t_run_1;
            t_tlr_q <= tlr;
            if tlr = '1' then
                t_in_hdr <= '0';
                t_bits <= (others => '0');
                t_pos <= (others => '0');
                t_record <= '0';
                t_left <= (others => '0');
                t_control <= '0';
                t_select <= 0;
                if t_tlr_q = '0' then
                    t_reg_tog <= not t_reg_tog;                  -- 入ったときの 1 回だけ（続けて反転すると打ち消す）
                end if;
            elsif sel = '1' and capture = '1' then
                t_left <= r_count;
                t_cnt <= r_count;
                t_flags <= t_run_2 & t_ovf_2;
                t_in_hdr <= '1';
                t_bits <= (others => '0');
                t_pos <= (others => '0');
                t_record <= '0';
            elsif sel = '1' and shift = '1' then
                t_cmd <= tdi & t_cmd(15 downto 1);
                if t_bits /= 16 then
                    t_bits <= t_bits + 1;
                end if;
                t_pos <= t_pos + 1;
                if t_pos = 31 then
                    -- 次の語: 有効な記録が残っていれば FIFO の先頭
                    t_in_hdr <= '0';
                    t_record <= '0';
                    if t_left /= 0 then
                        t_record <= '1';
                        t_left <= t_left - 1;
                    end if;
                end if;
            elsif sel = '1' and update = '1' then
                if t_bits = 16 then
                    op := t_cmd(3 downto 0);
                    addr := to_integer(unsigned(t_cmd(7 downto 4)));
                    data := unsigned(t_cmd(15 downto 8));
                    if op = OP_WRITE then
                        if addr = REG_CONTROL then
                            t_control <= data(0);
                            t_reg_tog <= not t_reg_tog;
                        elsif addr = REG_SELECT and data < GROUPS then
                            t_select <= to_integer(data);
                            t_reg_tog <= not t_reg_tog;
                        end if;
                    elsif op = OP_READ or op = OP_SYNC then
                        t_req_sync <= '1' when op = OP_SYNC else '0';
                        t_req_addr <= t_cmd(7 downto 4);
                        t_req_data <= t_cmd(15 downto 8);
                        t_req_tog <= not t_req_tog;
                    end if;
                end if;
            end if;
        end if;
    end process;

    ---------------------------------------------------------------- clk 側
    process (clk)
        variable value    : std_logic_vector(15 downto 0);
        variable changed  : boolean;
        variable resync   : boolean;
        variable avail    : count_t;               -- 実効の空き（w_valid は登録の出力なので、まだ FIFO に届いていない）
        variable free_1, free_2, free_4 : boolean; -- 空きが 1・2・4 語以上（FIFO の深さの幅で比べる）
        variable lost_1   : unsigned(LOST_BITS - 1 downto 0);   -- lost + 1（上限で止める。加算器は 1 つ）
        variable delta_1  : delta_t;                            -- delta + 1（上限で止める）
        variable reg      : std_logic_vector(15 downto 0);
        -- この周期に書く記録 = 種類 | 上位 14 ビット | 下位 16 ビット（時刻のある記録の delta はどれも上位）
        variable wr       : boolean;
        variable wk       : std_logic_vector(1 downto 0);
        variable wh       : std_logic_vector(13 downto 0);
        variable wl       : std_logic_vector(15 downto 0);
    begin
        if rising_edge(clk) then
            reg_sync <= reg_sync(1 downto 0) & t_reg_tog;
            req_sync <= req_sync(1 downto 0) & t_req_tog;

            value := probes(16 * sel_group + 15 downto 16 * sel_group);
            avail := w_free;
            if w_valid = '1' then
                avail := w_free - 1;
            end if;
            free_1 := avail /= 0;
            free_2 := avail >= 2;
            free_4 := avail >= 4;
            changed := value /= prev;
            lost_1 := lost;
            if lost /= LOST_MAX then
                lost_1 := lost + 1;
            end if;
            delta_1 := delta;
            if delta /= DELTA_MAX then
                delta_1 := delta + 1;
            end if;
            wr := false;
            wk := T_VALUE;
            wh := (others => '0');
            wl := (others => '0');
            resync := false;

            reg := register_value(p_addr, t_control, t_select);

            -- 頼み（読み・印）を受ける。印は記録中だけ（止まっている間の時刻は意味を持たない）
            if p_sync = '1' and late /= LATE_MAX then
                late <= late + 1;
            end if;
            if req_sync(2) /= req_sync(1) then
                if t_req_sync = '1' then
                    p_sync <= running;
                    p_seq <= t_req_data;
                    late <= (others => '0');
                else
                    p_resp <= '1';
                    p_addr <= t_req_addr;
                end if;
            end if;

            reload <= '0';
            if reg_sync(2) /= reg_sync(1) or reload = '1' then
                -- CONTROL・SELECT を写す（TCK 側の値はトグルの後は動かない。リセットの明けにも写す）
                running <= t_control;
                p_switch <= '0';
                if t_control = '0' then
                    sel_group <= t_select;
                    state <= S_IDLE;
                    p_sync <= '0';
                    p_group <= '0';
                    p_value <= '0';
                elsif running = '0' then
                    sel_group <= t_select;
                    -- リセットの明けの再開は、リセットの間の変化を数えられない = 欠け。溢れの経路に乗せて、lost = 0
                    -- （これから数えられる分だけ）の溢れの記録（gap）を鎖の始めの前に書く
                    overflowed <= reload;
                    chained <= '0';
                    lost <= (others => '0');
                    dropping <= reload;
                    if reload = '1' then
                        -- rst が prev を 0 にしたので、開始時の値を基準にし直す（そうしないと変わっていない値を失った変化に数える）
                        prev <= probes(16 * t_select + 15 downto 16 * t_select);
                    end if;
                    resync := true;
                elsif sel_group /= t_select then
                    -- 記録中の切り替え: 再同期せず、変化の無い周期（か溢れの立て直し）に組の記録を書くまで前の組を
                    -- 見続ける（R17。その間の前の組の変化も記録に残る）。切り替え先はこの周期に写す（TCK 側の値が
                    -- 止まっているのはこの周期だけ）
                    next_group <= t_select;
                    p_switch <= '1';
                end if;
            end if;

            if resync then
                state <= S_RESYNC;
                delta <= delta_1;
            else
                case state is
                    when S_IDLE =>
                        if p_resp = '1' and free_1 then
                            wr := true;
                            wk := T_CTRL;
                            wh := response_head(p_addr);
                            wl := reg;
                            p_resp <= '0';
                        end if;
                    when S_RESYNC =>
                        -- 溢れたときは、書けるようになるまでの値の変化を失ったものとして数える
                        if dropping = '1' and changed then
                            lost <= lost_1;
                        end if;
                        prev <= value;
                        delta <= delta_1;
                        if delta = DELTA_MAX then
                            chained <= '0';
                        end if;
                        if free_4 then                                 -- 組・(溢れ)・値の 3 語が入る
                            state <= S_GROUP;
                        end if;
                    when S_GROUP =>
                        wr := true;
                        wk := T_CTRL;
                        wh := GROUP_HEAD;
                        if p_switch = '1' then                         -- 待っていた切り替えもここで（前の組が毎周期
                            wl := group_low(next_group);   -- 変わって変化の無い周期が
                            sel_group <= next_group;                   -- 来なくても、溢れの後には切り替わる）
                            p_switch <= '0';
                        else
                            wl := group_low(sel_group);
                        end if;
                        if dropping = '1' and changed then             -- この周期の変化も失ったものとして数える（R17）
                            lost <= lost_1;
                        end if;
                        prev <= value;
                        delta <= delta_1;
                        if delta = DELTA_MAX then
                            chained <= '0';
                        end if;
                        if dropping = '1' then
                            state <= S_OVERFLOW;
                        else
                            state <= S_VALUE;
                        end if;
                    when S_OVERFLOW =>
                        wr := true;
                        wk := T_OVERFLOW;
                        if changed then
                            wl := std_logic_vector(resize(lost_1, 16));
                        else
                            wl := std_logic_vector(resize(lost, 16));
                        end if;
                        prev <= value;
                        chained <= '0';
                        delta <= delta_1;
                        dropping <= '0';                               -- 書いたので溢れは終わり（後の SELECT で
                        lost <= (others => '0');                       -- 前の数を書かない。R17）
                        state <= S_VALUE;
                    when S_VALUE =>
                        -- 鎖が続いていれば前の時刻のある記録からの周期数、切れていれば 0（鎖の始め）
                        wr := true;
                        wl := value;
                        if chained = '1' then
                            wh := std_logic_vector(delta);
                        end if;
                        prev <= value;
                        delta <= DELTA_ONE;
                        chained <= '1';
                        state <= S_RUN;
                    when S_RUN =>
                        if changed then
                            if not free_1 then
                                lost <= to_unsigned(1, LOST_BITS);
                                overflowed <= '1';
                                dropping <= '1';
                                chained <= '0';
                                state <= S_RESYNC;
                            else
                                wr := true;
                                wh := std_logic_vector(delta);
                                wl := value;
                                if p_group = '0' then              -- 組の記録の後に今の値を書いた: 書き直しは要らない
                                    p_value <= '0';
                                end if;
                            end if;
                            delta <= DELTA_ONE;
                        elsif delta = DELTA_MAX then
                            -- 変化の無いまま delta が上限: 刻み（書けなければ溢れとして始め直す）
                            if free_1 then
                                wr := true;
                                wk := T_MARK;
                                wh := std_logic_vector(delta);
                            else
                                lost <= (others => '0');
                                overflowed <= '1';
                                dropping <= '1';
                                chained <= '0';
                                state <= S_RESYNC;
                            end if;
                            delta <= DELTA_ONE;
                        elsif p_sync = '1' and free_2 then
                            wr := true;
                            wk := T_MARK;
                            wh := std_logic_vector(delta);
                            wl := '1' & p_seq & std_logic_vector(late);
                            p_sync <= '0';
                            p_group <= '1';
                            p_value <= '1';
                            delta <= DELTA_ONE;
                        elsif p_switch = '1' and free_2 then
                            wr := true;                            -- 組の切り替え: 組の記録、次の周期に今の値
                            wk := T_CTRL;
                            wh := GROUP_HEAD;
                            wl := group_low(next_group);
                            sel_group <= next_group;
                            p_switch <= '0';
                            p_group <= '0';
                            p_value <= '1';
                            delta <= delta_1;
                        elsif p_group = '1' and free_2 then
                            wr := true;
                            wk := T_CTRL;
                            wh := GROUP_HEAD;
                            wl := group_low(sel_group);
                            p_group <= '0';
                            delta <= delta_1;
                        elsif p_value = '1' and free_2 then
                            wr := true;
                            wh := std_logic_vector(delta);
                            wl := value;
                            p_value <= '0';
                            delta <= DELTA_ONE;
                        elsif p_resp = '1' and free_2 then
                            wr := true;
                            wk := T_CTRL;
                            wh := response_head(p_addr);
                            wl := reg;
                            p_resp <= '0';
                            delta <= delta_1;
                        else
                            delta <= delta_1;
                        end if;
                        prev <= value;
                end case;
            end if;

            w_valid <= '1' when wr else '0';
            w_data <= wk & wh & wl;

            if rst = '1' then
                state <= S_IDLE;
                running <= '0';
                sel_group <= 0;
                prev <= (others => '0');
                delta <= (others => '0');
                chained <= '0';
                lost <= (others => '0');
                overflowed <= '0';
                dropping <= '0';
                p_sync <= '0';
                p_seq <= (others => '0');
                late <= (others => '0');
                p_group <= '0';
                p_value <= '0';
                p_switch <= '0';
                next_group <= 0;
                p_resp <= '0';
                p_addr <= (others => '0');
                w_valid <= '0';
                reg_sync <= (others => t_reg_tog);
                req_sync <= (others => t_req_tog);
                reload <= '1';
            end if;
        end if;
    end process;
end architecture;
