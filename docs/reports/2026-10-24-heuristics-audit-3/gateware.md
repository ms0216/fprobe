# ヒューリスティックの 3 回目の点検: gateware（rtl の .vhd と fprobe_gw の .py）

- 日付: 2026-10-24
- 対象: `gateware/rtl/**/*.vhd`（130 本）と `gateware/fprobe_gw/**/*.py`（41 本）。テストは除く。
- 方法: 検索で候補を拾わず、各ファイルを先頭から最後まで読んだ。前の点検の報告とヒューリスティックの台帳は読んでいない。
- 基準（CLAUDE.md と依頼の原則）: 結果は「何を測ったか」だけで決まる事実にする。経験則の閾値・仮定に頼る推定・それによる
  判定や加工を入れない。境界・既定は使う側が明示する引数にする（規格の値は出典を書けば既定にしてよい）。打ち切りは切ったことと
  上限を返す。欠けは欠けとして見せる。
- 分類: **H** = ヒューリスティック（原則に反すると判断した）、**?** = 判断がつかない（理由を書く）。

## まとめ

| 分類 | 件数 |
|---|---|
| H | 5 |
| ? | 17 |

H は、(1) 非同期の位相の「平均 0.5 刻み」という仮定を時刻の補正の値として申告していること（2 か所）、(2) SWJ_Clock の
要求を 1% まで超えてよいという隠れた許容、(3) FPGA の観測で「16 スキャンで ID が来なければ答えない」と決めて止める固定の
閾値、(4) DAP のイベントと番地一致で、MEM-AP の番地を「AP は 1 つ」等の仮定から推定して事実として出していること。

---

## H（ヒューリスティック）

### H1. `gateware/fprobe_gw/caps.py:41`（と `:286`）— 子の同期の位相の「平均 0.5 刻み」を定数として申告

```python
SYNC_RX_PHASE_TICKS = 0.5    # 子: 非同期に来る辺を自分の時計で取り込む位相の平均（ロジアナの申告の遅れの 0.5 と同じ。
                             # 2026-10-22 の実機で、これを入れないと子の出来事が約 0.5 刻み早く出た）
...
    "SYNC_RX_PHASE_TICKS": SYNC_RX_PHASE_TICKS,      # HOST_CONSTANTS（host/fprobe/generated.py へ生成）
```

なぜ: 非同期の辺が 1 刻みの中のどこで来たかは測っていない。「位相が一様に分布するなら平均は 0.5」という仮定に頼る推定値を、
同期の組の時刻の補正に使う定数としてホストへ配っている（コメントのとおり実機で合わせた経験値でもある）。事実として言えるのは
「遅れは RX_LATENCY_TICKS 以上 RX_LATENCY_TICKS + 1 未満」という範囲で、点の値 0.5 は推定。原則の「仮定に頼る推定
（『ランダムな時刻なら』の見込み等）を本体に入れない」に当たる。範囲（下限・上限）を申告し、点の推定はプラグインか使う側に
置くのが筋と考える。

### H2. `gateware/fprobe_gw/caps.py:60-61, 157-160` — ロジアナの遅れ（TLV LA_DELAY）に平均 0.5 刻みを混ぜて 1 つの値で申告

```python
TYPE_LA_DELAY = 19           # ... 半刻みの単位（非同期のピンの同期化の平均 0.5 刻みを含む）。無い古いデバイスは 0（直さない）
...
def la_delay_halves(caps: Capabilities) -> tuple[int, int]:
    """ロジアナの時刻がピンの変化より遅い分（内蔵, 深いバッファ）。半刻みの単位、非同期の平均 0.5 刻みを含む。"""
    front = LA_FILTER_DELAY if caps.la_filter else 0
    return 2 * (front + LA_INTERNAL_DELAY) + 1, 2 * (front + LA_DEEP_DELAY) + 1
```

なぜ: パイプラインの段数（LA_FILTER_DELAY・LA_INTERNAL_DELAY・LA_DEEP_DELAY）は回路で決まる事実だが、`+ 1`（半刻み）は
H1 と同じ「平均」の仮定。事実の値と推定の値を 1 つの数に足し合わせてデバイスの申告（ケイパビリティ ROM、生成物
`rtl/designs/generated/*_caps_pkg.vhd` の `x"000F000D"` 等）に焼いているので、使う側は両者を分けられない。事実の段数（と
「同期化の分の 0〜1 刻みの不確かさ」）を別々に申告すべきと考える。

### H3. `gateware/rtl/debug/dap_cmd_config.vhd:66-67, 306-312` — SWJ_Clock の要求を 1% まで超えてよいという隠れた許容

```vhdl
  -- SWJ_Clock は要求を 1% まで超えてよい（sys 100.29 MHz で 25 MHz の要求に 16.7 MHz でなく 25.07 MHz を選ぶ）
  constant HALF_CLOCK_TOL_HZ : natural := HALF_CLOCK_HZ / 101 * 100;
...
          -- SWJ_Clock: half_period = ceil(HALF_CLOCK_HZ / (1.01 * clock))、2〜65535
              div_num   <= to_unsigned(HALF_CLOCK_TOL_HZ, 32) + unsigned(arg(31 downto 0)) - 1;  -- 切り上げ
```

なぜ: 「1% までなら要求より速くてよい」は経験則の境界で、使う側が渡す引数ではなく暗黙の既定になっている。ホスト（OpenOCD 等）は
要求した周波数以下になる前提で配線・ターゲットの上限に合わせて値を選ぶので、上限ぎりぎりを要求したとき黙って上限を超えうる。
CMSIS-DAP の SWJ_Clock の応答に実際の周波数を返す欄は無いので、ホストからは超えたことが見えない。境界を外す（要求以下の最大を選ぶ）
か、許容を CSR 等で明示的に渡す形にするのが原則に沿う。

### H4. `gateware/rtl/debug/obs_jtag_engine.vhd:97, 22-25, 459-471` — 固定の 16 スキャンで「観測の回路が答えない」と判定して止める

```vhdl
  constant ID_SCANS   : natural := 16;                                   -- この数のスキャンで ID が来なければ答えない
...
              if id_ok_r = '0' then
                if id_lo_ok = '1' and id_hi_ok = '1' then
                  id_ok_r <= '1';
                elsif id_wait /= ID_SCANS and rec_count <= m_words then
                  id_wait <= id_wait + 1;
                end if;
              end if;
              if bad = '1' or (id_ok_r = '0' and id_wait = ID_SCANS) then
                stuck_r <= '1';
                running_r <= '0';
                step <= E_EXIT;
```

なぜ: 「16 スキャン待って ID の応答が来なければ答えない」という固定の閾値で、対象の状態を判定し（status.stuck）、動作を止めている。
16 は使う側が渡す値ではなく、CSR にも無い。応答が遅い対象（観測の回路の FIFO が深い・クロックが遅い等）では答えているのに
stuck と判定されうる。判定に使った待ちの数（何スキャン待ったか）も返していない。待つ上限を CSR の引数にし、stuck は
「上限 N で打ち切った」と上限とともに返すのが原則に沿う。
（同じファイルの `bad`＝見出しの印 0xA6 でない・ID の値が "FOBS" でない、による stuck は自前の観測の回路のプロトコルの照合なので
事実の判定とみなし、H にしない。）

### H5. `gateware/rtl/debug/dap_monitor.vhd:11, 107-111, 116-135` — DRW の番地を「AP は 1 つ」等の仮定から推定し、事実として記録・照合

```vhdl
-- ... ev_addr は DRW なら TAR から求めたメモリの番地 ... ponytail: AP は区別しない（Cortex-M の MEM-AP は 1 つ）。BD0〜3 は追わない。
...
          if req(0) = '1' and req(3 downto 2) = "11" and sel(7 downto 4) = "0000" then
            ev_addr_r <= std_logic_vector(tar);                      -- DRW: メモリの番地
...
              elsif a = "11" then                                    -- DRW
                addr_ok := ((std_logic_vector(tar) xor match_addr) and addr_mask) = x"00000000";
...
                if inc = "01" then                                   -- 単純増加
                  tar <= tar + shift_left(to_unsigned(1, 32), to_integer(unsigned(size(1 downto 0))));
```

なぜ: イベント（種類 1）の番地と、クロストリガの源 bit1（番地一致）の判定に使う番地は、線上で測った値ではなく、ホストが書いた
CSW・TAR を追って求めた推定値。推定は次の仮定に頼る:
- AP は 1 つ（SELECT の APSEL [31:24] を見ず、bank [7:4] だけで判定）。コメント自身が「Cortex-M の MEM-AP は 1 つ」と特定の
  対象の経験則を根拠にしている（Cortex-A の DAP・複数 AP の SoC では外れる）。
- TAR の自動増加は 32 ビットを通して足す（ADIv5 では 1 KB 等の境界での折り返しは実装次第）。packed（inc = "10"）は追わない。
- ホストが TAR を書く前の値は 0（`tar_known` は shadow_valid にしか使わず、ev_addr・番地一致は tar_known を見ない）。
仮定が外れても、イベントには「推定した番地」「TAR を知らない」という印が無く、使う側は線上の事実と区別できない。番地一致の
トリガも誤発火・不発火しうる。少なくとも「TAR を知っていたか・増加方式を追えたか」の印をイベントに載せ、AP の区別をするか、
推定であることを返すのが原則に沿うと考える。

---

## ?（判断がつかない）

### ?1. `gateware/fprobe_gw/csr/fprobe_map.py:201, 327, 329, 336, 338, 379` — CSR のリセット値が暗黙の既定になっている

```python
        Register("sample_delay", 0x00, (Field("swd", 0, 2, "RW", value=1, doc="... 配線とターゲットの応答遅延の補償"),
...
        Register("half_period", 0x04, (Field("half_period", 0, 16, "RW", value=10),),
        Register("chain", 0x08, (Field("ir_len", 0, 6, "RW", value=4, doc="対象の IR の長さ（1〜32）"),
        Register("words", 0x10, (Field("words", 0, 9, "RW", value=16),),
        Register("sync_period", 0x14, (Field("sync_period", 0, 16, "RW", value=16),),
...
        Register.word("period", 0x04, "RW", value=100000, doc="親のパルスの間隔（タイムベースの刻み）...")
```

理由: ハードウェアのレジスタにリセット値は必ず要るが、`debug.sample_delay.swd = 1`（往復遅延の補償の経験値）と
`obs.chain.ir_len = 4`（対象の IR 長の仮定）は、ホストが書かずに使えば暗黙の既定として測定・動作に効く。ホストが常に明示して
書くかどうかはこの担当範囲（gateware）では確かめられない。生成物 `rtl/core/generated/csr_debug.vhd:32,45`・`csr_obs.vhd:61-69`・
`csr_sync.vhd:37,51` にも同じ値が入る。`xtrig.inject_addr/inject_value`（`:120-121`、DHCSR・C_HALT|C_DEBUGEN）は ARMv7-M/v8-M の
規格の値なので既定として可だが、doc に規格の出典が書かれていない。

### ?2. `gateware/rtl/debug/dap_cmd_config.vhd:79, 83, 108-110, 183-192` — 電源投入時の SWJ 1 MHz・WAIT 再試行 100・IR 長 4

```vhdl
  signal half_r       : unsigned(15 downto 0) := to_unsigned(HALF_CLOCK_HZ / 1_000_000, 16);   -- 1 MHz
  signal wait_retry_r : unsigned(15 downto 0) := to_unsigned(100, 16);
  signal irlen     : ir_lengths_t := (others => 4);
```

理由: CMSIS-DAP の参照実装（DAP_config.h の既定）に揃えた値と思われるが、出典が書かれていない。標準のホストは
DAP_SWJ_Clock・DAP_TransferConfigure・DAP_JTAG_Configure で必ず設定するので実害は小さいが、設定しないホストでは暗黙の既定になる。
規格（参照実装）の値として出典を書けば可、というのが原則の扱い。

### ?3. `gateware/rtl/debug/dap_cmd_transfer.vhd:141-148` — 範囲外の JTAG 装置番号を黙って 0 番にする

```vhdl
    procedure select_device(index : std_logic_vector(7 downto 0)) is
    begin
      if unsigned(index) < jtag_count then
        index_r <= to_integer(unsigned(index));
      else
        index_r <= 0;
      end if;
```

理由: ヒューリスティック（推定）ではないが、ホストの指定と違う装置に黙ってアクセスし、応答にもその置き換えが現れない。
参照実装（DAP.c）は範囲外なら転送せずに終える。「置き換えたことを返さない」点で原則（欠け・打ち切りを見せる）に近い問題か判断が
つかない。

### ?4. `gateware/rtl/la/la_deep_buffer.vhd:101, 377-387` — ストリームを止める上限を申告しない

```vhdl
           and unread_r > resize(LA_SIZE, 32) - STREAM_CHUNK then
          m_full_stop <= '1';                                  -- 未読がリングに迫った: 書き込みを止める
          m_ovf       <= '1';
...
           and ev_back_r(EV_BITS - 1) = '1' then
          m_full_stop <= '1';                                  -- イベントの未読がリングの半分: 止め、以後流さない
```

理由: 打ち切ったこと（la_deep.status.overrun）は返すが、どちらの上限（ロジアナのリング − 256 拍か、イベントのリングの半分か）で
切ったかと、上限の値は返さない。「リングの半分」は上書きを避ける余裕として選んだ値で、物理の上限そのものではない。原則の
「打ち切りは切ったことと使った上限を返す」に照らして不足か判断がつかない（ring_beats・capacity は別に読める）。

### ?5. `gateware/rtl/la/la_deep_buffer.vhd:578-588` — `reading` を 31 周期だけ延ばす余裕

```vhdl
      if reading_s = '1' or beat_valid = '1' or out_valid = '1' then
        drain <= 31;
...
  reading      <= '1' when reading_s = '1' or pending_read = '1' or drain /= 0 else '0';
```

理由: 「mem 側の busy が落ちてから拍が FIFO を渡ってくるまでの遅れ」を 31 周期で覆えるという見込みの値で、状態ビット
（la_deep.status.reading）の意味を決めている。クロックの比で遅れが変わる移植先では外れうる。測定値ではないが、状態の申告が
見込みに頼っている。

### ?6. `gateware/rtl/la/la_encoder.vhd:9-10, 87-90`・`gateware/rtl/debug/swo_receiver.vhd:7-8, 116-118` — 捨てた拍・バイトの数と位置を返さない

```vhdl
-- 出力は止められない（サンプルは毎周期来うる）。拍が揃ったときに out_ready = '0' ならその拍を捨て、overrun を立てる
        if valid_r = '1' and out_ready = '0' then
          overrun_r <= '1';                                   -- 前の拍がまだ出ていない: この拍を捨てる
```

理由: 欠けたことは 1 ビットの旗でしか分からず、何拍（何バイト）欠けたか・どこで欠けたかはデータにも CSR にも残らない
（深いバッファの拍の列には欠けの印が入らない）。原則の「欠けている所」を返す、に照らして不足か判断がつかない。
（イベントの取りこぼしは `events.lost` に数として返しており、こちらは原則に沿っている。）

### ?7. `gateware/rtl/la/la_pulse.vhd:10, 26, 49-52` — パルス幅の数えの折り返しと、範囲外の ch の置き換えに印が無い

```vhdl
-- 幅の数えは COUNT_BITS ビットで折り返す（32 ビットなら 100 MHz で約 43 秒より長い脈は正しく数えない）。
    channel    : in  unsigned(3 downto 0);                       -- WIDTH 以上は ch 0
```

理由: 約 43 秒より長い脈は誤った幅で判定され（トリガが成立・不成立）、そのことは返らない。ch の置き換えも黙って行う。
測定の結果を固定の幅で加工しているとも、仕様の制限ともとれる。

### ?8. `gateware/rtl/core/aux_link.vhd:70-74` — CHANNELS 以上の ch の計測値を数えずに捨てる

```vhdl
        elsif n = 2 then
          n <= 0;
          if ch_r < CHANNELS then
            sv   <= '1';
```

理由: 形の崩れは errors に数えるが、ch が範囲外の組は errors にも samples にも数えずに消える。欠けを見せない点が原則に当たるか
判断がつかない（補助マイコン側の約束では起きない想定）。

### ?9. `gateware/rtl/debug/swd_injector.vhd:24, 162-169` — 実行中に来た割り込みの要求を数えずに捨てる

```vhdl
    request       : in  std_logic;                       -- 1 周期のパルス（実行中に来た要求は捨てる）
```

理由: クロストリガが発火しても割り込み書き込みが行われなかった回数が残らない（count は成功の数だけ）。欠けを見せない点。

### ?10. `gateware/rtl/debug/obs_jtag_engine.vhd:416-421` — ID の照合が済むまでの記録を数えずに捨てる

```vhdl
                  if id_ok_r = '1' then
                    emit('0', word, std_logic_vector(scan_count), time_count);
```

理由: 開き直したとき FIFO に残っていた前回の記録などは、イベントにも数にもならずに消える。前回の残りを捨てるのは意図だが、
何語捨てたかが返らない。（同じファイル `:381-387` の標本型の「値も変化の回数も同じ標本は出さない」は、変化の回数が載っているので
情報の欠けは無いと判断し、対象外とした。）

### ?11. `gateware/rtl/debug/jtag_phy.vhd:123-130, 200` — 未定義の ACK を 111 にまとめる・4 ビットを超える IR の上位を 1 で埋める

```vhdl
      when "010"  => return "001";                                       -- OK / FAULT
      when "001"  => return "010";                                       -- WAIT
      when others => return "111";
...
          tdi_r <= '1';                                                -- 4 ビットを超える IR の上位は 1
```

理由: 前者は線上の生の ACK を失う（JTAG-DP の規格に無い値なので「プロトコル異常」とまとめるのは規格どおりともとれる）。後者は
ir_len > 4 の装置に ARM の DP の命令を送るときの埋め方の仮定（ARM の JTAG-DP の IR は 4 ビットなので通常は起きない）。

### ?12. `gateware/rtl/debug/swo_manchester_rx.vhd:10-17, 96-98, 101-112` — 復号の判定の窓が固定（T ± T/4）

```vhdl
--     t <  T - floor(T/4)   … 境目の変化（無視する。境目は T/2 に来る）
--     t >= T - floor(T/4)   … 中央の変化（ビットの値 = 向き）
--     t >= T + floor(T/4) で変化が無い … フレームの終わり。
```

理由: 復号器の判定の境界を固定の割合で決めている。受信器の設計の定数（UART の中央での取り込みと同類）とも、使う側が変えられない
閾値ともとれる。窓から外れた変化・Manchester の開始でない立ち上がりは黙って捨てる（frame_error は端数で終わったときだけ）。

### ?13. `gateware/rtl/serial/uart_bridge.vhd:122, 183` — 「現在のボーレート」に要求の値を返し、実際の値（CLK / 分周比）を返さない

```vhdl
          baud_r <= pending;
...
  current_baud <= baud_r;
```

理由: CSR uart.baud は「現在のボーレート」だが、丸め・2〜2^20−1 への制限の後の実際の値ではなく要求の値。推定ではないが、
測った（決めた）事実と違う値を事実の名前で返している。

### ?14. `gateware/rtl/debug/dap_sampler.vhd:21, 254-257` — 標本の番地を「1 KB をまたがない」前提で計算

```vhdl
      -- 1 か所は 1 KB をまたがない（ホストが保証）: 番地の下位 10 ビットだけを足す
      s_addr  <= std_logic_vector(base(31 downto 10)) & std_logic_vector(base(9 downto 2) + to_unsigned(index, 8))
```

理由: ホストへの約束（前提）として書かれており推定ではないが、破られたとき報告する番地と MEM-AP が実際に読んだ番地が食い違い、
それは返らない。

### ?15. `gateware/rtl/core/sync_link.vhd:189-195`・`gateware/rtl/eth/udp_stack.vhd:386-389` — 数えずに捨てるもの（同期のパルス・送信先の無いフレーム）

```vhdl
                rx_state <= S_RX_IDLE;                 -- パルスの半分より短い: 受けない
...
              if not peer_ok then
                done_r <= '1';                         -- 送信先がまだ無い: 捨てる
```

理由: どちらも雑音の除去・リンクの制約として妥当だが、捨てた数が残らない（CRC 誤りは crc_errors に数えている）。

### ?16. `gateware/rtl/boards/primer20k/primer20k_fprobe_eth_top.vhd:218-230` — 診断で要求の長さ 8・12 以外を「odd」と数える

```vhdl
      -- コアが受け取った CONTROL の要求の長さ（8 = 読み、12 = 1 語の書き。それ以外を数える）
          if req_bytes + 1 /= 8 and req_bytes + 1 /= 12 then
            odd_n <= odd_n + 1;
```

理由: BL702 の UART へ出す診断だけで API の結果ではないが、固定の分類で「普通でない」を数えている。

### ?17. gateware の道具（fprobe_gw/toolchain 等）の出力の文字列・終了コードによる判定

- `gateware/fprobe_gw/pynq_setup.py:82-84`: `if r.returncode not in (0, 255):` — 再起動で SSH が切れた 255 を成功とみなす（SSH の
  接続失敗も 255 なので区別できない）。
- `gateware/fprobe_gw/toolchain/programmer.py:97-98`: `"Finished." not in output or re.search(r"\b(error|unsupported)\b", output, re.I)`
  — programmer_cli の出力の語で失敗を判定。
- `gateware/fprobe_gw/toolchain/build.py:225`: `"ERROR" in (result.stdout or "")` — IP の生成の失敗の判定。
- `gateware/fprobe_gw/toolchain/ftdi.py:39-42`: `return not self.serial and not self.description` — 説明もシリアルも空なら
  「他プロセス使用中」と推定。
- `gateware/fprobe_gw/toolchain/pll.py:74-76, 90`: `tolerance: float = 0.005` の既定と、誤差が同じなら PFD の高い解を選ぶ並べ替え。

理由: いずれも測定器の結果ではなくビルド・書き込みの道具の中の判定。原則（fprobe 本体）の対象に入るか判断がつかない。

---

## 点検して対象外と判断したもの（主なもの）

- `sampler_match.vhd`: 「条件に入ったのは prev_time と fire_time の間のどこか（推定しない）」と区間で返している。原則どおり。
- `fprobe_core.vhd:996-1155` のイベントの合流: 保持中に来た分は `byte_lost`（events.lost）に数えている。
- `la_filter.vhd`・`la_pulse.vhd` の min/max・`trig_seq.vhd` の within・`xtrig_out.vhd` の length/unit: すべて CSR の引数。
- `dap_sampler.vhd` の jitter: 既定 0 で、使う側が CSR で指定する。overruns・errors を数えている。
- `dap_cmd_swo.vhd:155-187`: SWO_Baudrate は実際のボーレート（CLK_HZ / 分周比）を返す（規格どおり）。
- USB・Ethernet・COBS・CRC・CDC-ACM の定数（`usb_timing_pkg.vhd`・`rmii_*`・`udp_stack.vhd`・`serial_link.vhd` 等）: 規格の値。
- `frame_mux.vhd`・`usb_ep_in.vhd` の IDLE_CYCLES / FLUSH_CYCLES・`la_deep_buffer.vhd` の LA_MIN・EV_MIN・AGE_BITS: 送る束ね方
  （遅れ）を決めるだけで、データの中身は変えない。
- `event_ring.vhd`: 古いものから上書きするが written と capacity で上書きの量が分かる。
- `bringup_core.vhd`・`ulpi_probe_core.vhd`・`usbtest_responder.vhd`・`*_usbtest_top.vhd`: 基板立ち上げ用の試験の構成。
- ボードの定義（`boards/*.py`・`*_pkg.vhd`）のピン・プル・PLL: 基板の事実と、出典付きの選択。

---

## 読んだファイルの一覧（すべて先頭から最後まで読んだ）

### gateware/rtl（.vhd、130 本）

rtl/boards/nano9k/nano9k_fprobe_top.vhd, rtl/boards/nano9k/nano9k_pkg.vhd, rtl/boards/primer20k/gowin_rpll.vhd,
rtl/boards/primer20k/primer20k_bringup_top.vhd, rtl/boards/primer20k/primer20k_ddr3.vhd,
rtl/boards/primer20k/primer20k_fprobe_eth_top.vhd, rtl/boards/primer20k/primer20k_fprobe_serial_top.vhd,
rtl/boards/primer20k/primer20k_fprobe_top.vhd, rtl/boards/primer20k/primer20k_pkg.vhd,
rtl/boards/primer20k/primer20k_ulpi_probe_top.vhd, rtl/boards/primer20k/primer20k_ulpi_reset.vhd,
rtl/boards/primer20k/primer20k_usbtest_top.vhd, rtl/boards/pynq_z2/pynq_z2_blinky_top.vhd,
rtl/boards/pynq_z2/pynq_z2_fprobe_top.vhd, rtl/boards/pynq_z2/pynq_z2_pkg.vhd, rtl/boards/pynq_z2/xilinx_mmcm.vhd,
rtl/common/async_fifo.vhd, rtl/common/cdc_value.vhd, rtl/common/reset_sync.vhd, rtl/common/sdp_ram.vhd,
rtl/common/stream_slice.vhd, rtl/common/stream_unpack.vhd, rtl/common/sync_bit.vhd, rtl/common/sync_fifo.vhd,
rtl/common/text_report.vhd, rtl/common/text_report_pkg.vhd, rtl/common/udiv.vhd, rtl/common/udiv_shared.vhd,
rtl/common/util_pkg.vhd, rtl/core/aux_link.vhd, rtl/core/caps_rom.vhd, rtl/core/control_plane.vhd,
rtl/core/csr_absent.vhd, rtl/core/csr_bus_pkg.vhd, rtl/core/csr_decoder.vhd, rtl/core/fprobe_core.vhd,
rtl/core/generated/build_info_pkg.vhd, rtl/core/generated/csr_aux.vhd, rtl/core/generated/csr_debug.vhd,
rtl/core/generated/csr_events.vhd, rtl/core/generated/csr_gpio.vhd, rtl/core/generated/csr_la.vhd,
rtl/core/generated/csr_la_deep.vhd, rtl/core/generated/csr_map_pkg.vhd, rtl/core/generated/csr_mem.vhd,
rtl/core/generated/csr_obs.vhd, rtl/core/generated/csr_sampler.vhd, rtl/core/generated/csr_sampler_match.vhd,
rtl/core/generated/csr_sync.vhd, rtl/core/generated/csr_system.vhd, rtl/core/generated/csr_timebase.vhd,
rtl/core/generated/csr_trigseq.vhd, rtl/core/generated/csr_uart.vhd, rtl/core/generated/csr_xtrig.vhd,
rtl/core/generated/csr_xtrig_out.vhd, rtl/core/pin_select.vhd, rtl/core/sync_link.vhd, rtl/core/timebase.vhd,
rtl/core/trig_seq.vhd, rtl/core/xtrig_out.vhd, rtl/debug/dap_cmd_config.vhd, rtl/debug/dap_cmd_sequence.vhd,
rtl/debug/dap_cmd_swo.vhd, rtl/debug/dap_cmd_transfer.vhd, rtl/debug/dap_link.vhd, rtl/debug/dap_monitor.vhd,
rtl/debug/dap_packet.vhd, rtl/debug/dap_pkg.vhd, rtl/debug/dap_processor.vhd, rtl/debug/dap_sampler.vhd,
rtl/debug/debug_port.vhd, rtl/debug/generated/dap_info_pkg.vhd, rtl/debug/jtag_phy.vhd, rtl/debug/obs_jtag_engine.vhd,
rtl/debug/sampler_match.vhd, rtl/debug/swd_injector.vhd, rtl/debug/swd_phy.vhd, rtl/debug/swo_manchester_rx.vhd,
rtl/debug/swo_receiver.vhd, rtl/designs/bringup_core.vhd, rtl/designs/generated/fprobe_caps_pkg.vhd,
rtl/designs/generated/fprobe_core_m_caps_pkg.vhd, rtl/designs/generated/fprobe_eth_caps_pkg.vhd,
rtl/designs/generated/fprobe_jtag_caps_pkg.vhd, rtl/designs/generated/fprobe_la_caps_pkg.vhd,
rtl/designs/generated/fprobe_nano9k_caps_pkg.vhd, rtl/designs/generated/fprobe_pynq_caps_pkg.vhd,
rtl/designs/generated/fprobe_pynq_deep_caps_pkg.vhd, rtl/designs/generated/fprobe_serial_caps_pkg.vhd,
rtl/designs/generated/memtest_caps_pkg.vhd, rtl/designs/ulpi_probe_core.vhd, rtl/designs/usbtest_responder.vhd,
rtl/eth/rmii_rx.vhd, rtl/eth/rmii_tx.vhd, rtl/eth/udp_stack.vhd, rtl/hostlink/eth_link.vhd, rtl/hostlink/frame_mux.vhd,
rtl/hostlink/serial_link.vhd, rtl/hostlink/zynq_frame_link.vhd, rtl/la/event_ring.vhd, rtl/la/la_capture.vhd,
rtl/la/la_deep_buffer.vhd, rtl/la/la_encoder.vhd, rtl/la/la_filter.vhd, rtl/la/la_front.vhd, rtl/la/la_mem_csr.vhd,
rtl/la/la_pulse.vhd, rtl/mem/axi_mem_port.vhd, rtl/mem/la_mem_writer.vhd, rtl/mem/mem_reader.vhd, rtl/mem/mem_test.vhd,
rtl/mem/mem_test_sys.vhd, rtl/serial/uart_bridge.vhd, rtl/serial/uart_rx.vhd, rtl/serial/uart_tx.vhd,
rtl/usb/generated/usb_descriptors_pkg.vhd, rtl/usb/ulpi_link.vhd, rtl/usb/usb_cdc_acm_requests.vhd,
rtl/usb/usb_cdc_notify.vhd, rtl/usb/usb_control_ep.vhd, rtl/usb/usb_crc_pkg.vhd, rtl/usb/usb_device.vhd,
rtl/usb/usb_ep_in.vhd, rtl/usb/usb_ep_out.vhd, rtl/usb/usb_protocol.vhd, rtl/usb/usb_reset.vhd, rtl/usb/usb_rx_packet.vhd,
rtl/usb/usb_timing_pkg.vhd, rtl/usb/usb_tx_packet.vhd, rtl/usb/usb_types_pkg.vhd

### gateware/fprobe_gw（.py、41 本。`__pycache__` だけが残る designs/・platform/・serial/ のディレクトリに .py は無い）

fprobe_gw/__init__.py, fprobe_gw/__main__.py, fprobe_gw/boards/__init__.py, fprobe_gw/boards/nano9k.py,
fprobe_gw/boards/primer20k.py, fprobe_gw/boards/pynq_z2.py, fprobe_gw/build_info.py, fprobe_gw/caps.py, fprobe_gw/cli.py,
fprobe_gw/csr/__init__.py, fprobe_gw/csr/fprobe_map.py, fprobe_gw/csr/host.py, fprobe_gw/csr/markdown.py,
fprobe_gw/csr/model.py, fprobe_gw/csr/vhdl.py, fprobe_gw/dap_info.py, fprobe_gw/designs.py, fprobe_gw/generate.py,
fprobe_gw/protocol.py, fprobe_gw/pynq_setup.py, fprobe_gw/rtl.py, fprobe_gw/toolchain/__init__.py,
fprobe_gw/toolchain/build.py, fprobe_gw/toolchain/forgefpga.py, fprobe_gw/toolchain/ftdi.py, fprobe_gw/toolchain/ghdl.py,
fprobe_gw/toolchain/gowin.py, fprobe_gw/toolchain/pll.py, fprobe_gw/toolchain/programmer.py,
fprobe_gw/toolchain/project.py, fprobe_gw/toolchain/pynq_loader.py, fprobe_gw/toolchain/resources.py,
fprobe_gw/toolchain/sim.py, fprobe_gw/toolchain/timing.py, fprobe_gw/toolchain/vivado.py,
fprobe_gw/toolchain/vivado_build.py, fprobe_gw/toolchain/zephyr.py, fprobe_gw/usb/__init__.py,
fprobe_gw/usb/descriptors.py, fprobe_gw/usb/fprobe_device.py, fprobe_gw/usb/vhdl_rom.py

上の 171 本はすべて、検索で拾わず先頭から最後まで読んだ。
