# ヒューリスティックの 2 回目の点検: ゲートウェア（gateware/rtl の .vhd・gateware/fprobe_gw の .py）

- 日付: 2026-10-24
- 範囲: `gateware/rtl` の .vhd すべて（130 ファイル）と `gateware/fprobe_gw` の .py すべて（41 ファイル）。テストは含めない
- 方法: 検索で候補を拾わず、各ファイルを先頭から最後まで全部読んだ。1 回目の報告（docs/reports/2026-10-23-heuristics-audit/）は読んでいない
- 原則（CLAUDE.md）: 本体の結果は「何を測ったか」だけで決まる事実にする。経験則の閾値・仮定に頼る推定・それらによる判定や並べ替えを入れない。境界の値は使う側が明示する引数にし、暗黙の既定にしない。打ち切りは切ったことと使った上限を返す
- 分類: **H** = ヒューリスティック、**?** = 判断がつかない（理由を書く）
- このファイル以外は変更していない

## まとめ

| 分類 | 件数 |
|---|---|
| H | 5 |
| ? | 14 |

VHDL の大部分（USB・ULPI・Ethernet・UART・COBS・CRC・CSR・FIFO・CDC）はプロトコルや仕様書で決まる定数で、測った結果に経験則の判定をかけてはいない。H になったのは、(1) 時刻を「ランダムな位相の平均」で直す申告、(2) 決め打ちの許容幅・待ち回数での判定、(3) 単一の MEM-AP を仮定して計算した番地をイベントの事実として出すこと、の 3 種類。

---

## H（ヒューリスティック）

### H1. 同期の子の位相を「平均 0.5 刻み」と仮定する申告

- 場所: `gateware/fprobe_gw/caps.py:41-42`（ホスト定数として `caps.py:286` で出す）
- コード:
  ```python
  SYNC_RX_PHASE_TICKS = 0.5    # 子: 非同期に来る辺を自分の時計で取り込む位相の平均（ロジアナの申告の遅れの 0.5 と同じ。
                               # 2026-10-22 の実機で、これを入れないと子の出来事が約 0.5 刻み早く出た）
  ```
- なぜ: 非同期の辺が自分の時計の周期の中で一様に来る、という仮定の下での期待値（「ランダムな時刻なら」の見込み）。1 回ごとの実際の位相は 0〜1 刻みのどこかで、測っていない。ホストはこの値で親子の時刻を換算するので、仮定の推定値が事実に混ざる。値の根拠も「実機で入れないと約 0.5 早く出た」という観察に合わせたもの。回路が保証する事実は「0〜1 刻みの不確かさ」（区間）で、平均値は使う側が選ぶこと。
- 補足: `SYNC_RX_LATENCY_TICKS = 2`・`SYNC_TX_LATENCY_TICKS = 1`（`caps.py:39-40`）は回路の段数そのもの（`sync_link.vhd:16-18`）で事実。問題は位相の平均だけ。

### H2. ロジアナの遅れの申告に「非同期の同期化の平均 0.5 刻み」を足す

- 場所: `gateware/fprobe_gw/caps.py:60-61`、`caps.py:157-160`（TLV LA_DELAY として ROM に入る）
- コード:
  ```python
  TYPE_LA_DELAY = 19           # ロジアナの時刻（trigger_time）がピンの変化より遅い分: u16 × 2（内蔵・深いバッファ）、
                               # 半刻みの単位（非同期のピンの同期化の平均 0.5 刻みを含む）。
  ...
  def la_delay_halves(caps: Capabilities) -> tuple[int, int]:
      """ロジアナの時刻がピンの変化より遅い分（内蔵, 深いバッファ）。半刻みの単位、非同期の平均 0.5 刻みを含む。"""
      front = LA_FILTER_DELAY if caps.la_filter else 0
      return 2 * (front + LA_INTERNAL_DELAY) + 1, 2 * (front + LA_DEEP_DELAY) + 1
  ```
- なぜ: H1 と同じ。段数（`LA_FILTER_DELAY`・`LA_INTERNAL_DELAY`・`LA_DEEP_DELAY`）は回路の事実だが、`+ 1`（半刻み = 0.5 刻み）は「ピンの変化が周期の中で一様に来る」仮定の平均。デバイスが申告する値に推定が混ざっており、使う側は「段数（事実）」と「位相の不確かさ 0〜1 刻み（区間）」を分けて受け取れない。

### H3. SWJ_Clock の要求を 1% まで超えてよいという決め打ち

- 場所: `gateware/rtl/debug/dap_cmd_config.vhd:66-67`、`dap_cmd_config.vhd:306-311`
- コード:
  ```vhdl
  -- SWJ_Clock は要求を 1% まで超えてよい（sys 100.29 MHz で 25 MHz の要求に 16.7 MHz でなく 25.07 MHz を選ぶ）
  constant HALF_CLOCK_TOL_HZ : natural := HALF_CLOCK_HZ / 101 * 100;
  ...
  -- SWJ_Clock: half_period = ceil(HALF_CLOCK_HZ / (1.01 * clock))、2〜65535
  div_num   <= to_unsigned(HALF_CLOCK_TOL_HZ, 32) + unsigned(arg(31 downto 0)) - 1;  -- 切り上げ
  ```
- なぜ: 「1% までなら要求を超えてもよい」は特定の場面（100.29 MHz で 25 MHz を得たい）に合わせた経験則の幅で、使う側が指定できない暗黙の既定。要求を超えるかどうか・どこまでかはターゲットの配線・速さで変わる。しかも DAP_SWJ_Clock の応答は状態だけで、実際に選んだ周波数をどこにも返さない（SWO_Baudrate は実際の値を返しているのと対照的）。

### H4. 観測の回路が「答えない」と決める 16 スキャンの待ち

- 場所: `gateware/rtl/debug/obs_jtag_engine.vhd:97`、判定 `obs_jtag_engine.vhd:459-471`、説明 `obs_jtag_engine.vhd:22-24`（CSR の説明 `fprobe_gw/csr/fprobe_map.py:348-350`）
- コード:
  ```vhdl
  constant ID_SCANS   : natural := 16;                                   -- この数のスキャンで ID が来なければ答えない
  ...
  elsif id_wait /= ID_SCANS and rec_count <= m_words then
    id_wait <= id_wait + 1;
  ...
  if bad = '1' or (id_ok_r = '0' and id_wait = ID_SCANS) then
    stuck_r <= '1';
    running_r <= '0';
    step <= E_EXIT;
  ```
- なぜ: 16 回という待ちの回数は使う側が変えられない固定の閾値で、超えると「答えない」と判定して観測を止める（`stuck`）。ID の応答が遅れる理由（対象の clk が遅い・FIFO の吸い出しが長い等）は対象ごとに違い、16 で十分という保証は無い。判定とその結果の停止が本体に入っている。事実として出すなら「何スキャン待ったが ID が来ていない」で、打ち切りの回数は使う側が CSR で指定するもの。
- 補足: 同じ `stuck` の他の理由（見出しの印が 0xA6 でない・ID が "FOBS" と違う）はプロトコルの照合で、経験則ではない。

### H5. DRW の番地を「MEM-AP は 1 つ」と仮定して計算し、イベントの事実として出す

- 場所: `gateware/rtl/debug/dap_monitor.vhd:11`、`dap_monitor.vhd:107-110`、`dap_monitor.vhd:118`、`dap_monitor.vhd:133-135`
- コード:
  ```vhdl
  -- ... ev_addr は DRW なら TAR から求めたメモリの番地 ... ponytail: AP は区別しない（Cortex-M の MEM-AP は 1 つ）。BD0〜3 は追わない。
  ...
  if req(0) = '1' and req(3 downto 2) = "11" and sel(7 downto 4) = "0000" then
    ev_addr_r <= std_logic_vector(tar);                      -- DRW: メモリの番地
  ...
  elsif ap and sel(7 downto 4) = "0000" then               -- MEM-AP bank 0
  ...
  if inc = "01" then                                   -- 単純増加
    tar <= tar + shift_left(to_unsigned(1, 32), to_integer(unsigned(size(1 downto 0))));
  ```
- なぜ: SELECT の APSEL（31:24）を見ずに、すべての AP の CSW・TAR を 1 つの控えにまとめ、DRW の番地を推定している。AP が複数ある対象（Cortex-A の APB-AP と AHB-AP、RP2040 の複数 DP 等）では、別の AP に書いた TAR で番地を計算した値が「メモリの番地」としてイベント（種類 1）の address に出る。線上で測った値ではなく仮定に頼った推定で、仮定が外れても印が無い。番地一致（クロストリガの源 bit1）の判定もこの推定値を使う。自動増加の 1 KB の折り返し（ADIv5 で実装定義）も計算していない。
- 補足: `shadow_valid`（`dap_monitor.vhd:147`）は自動増加が追えない方式で控えを無効にしており、その部分は事実に基づく。問題は AP を区別しないことと、推定値に「推定」の印が無いこと。

---

## ?（判断がつかない）

### ?1. 深いバッファのストリームで、イベントの未読がリングの半分で止める

- 場所: `gateware/rtl/la/la_deep_buffer.vhd:28-30`、`la_deep_buffer.vhd:382-386`
- コード:
  ```vhdl
  if STREAM_EVENTS and ev_on = '1' and s_state /= T_SETTLE and s_state /= T_IDLE2
     and ev_back_r(EV_BITS - 1) = '1' then
    m_full_stop <= '1';                                  -- イベントの未読がリングの半分: 止め、以後流さない
    m_ovf       <= '1';
    ev_on       <= '0';
  ```
- 理由: 実際に上書きが起きるのはリングが一杯のときで、「半分」は余裕として決めた固定の割合。止めたことは overrun で申告している（欠けは明示）ので判定を隠してはいないが、止める境界が使う側に選べない暗黙の値。ロジアナ側の `LA_SIZE - STREAM_CHUNK`（`la_deep_buffer.vhd:378`）は読み出しの塊の大きさから決まる構造上の境界で、これとは性質が違う。

### ?2. 割り込み書き込みの既定が Cortex-M の DHCSR と C_HALT

- 場所: `gateware/fprobe_gw/csr/fprobe_map.py:120-121`（生成物 `rtl/core/generated/csr_xtrig.vhd:55-56`）、`rtl/debug/swd_injector.vhd:2`
- コード:
  ```python
  Register.word("inject_addr", 0x0C, "RW", 0xE000EDF0, doc="割り込み書き込みの番地（既定 DHCSR）"),
  Register.word("inject_value", 0x10, "RW", 0xA05F0003, doc="割り込み書き込みの値（既定 C_HALT | C_DEBUGEN）"),
  ```
- 理由: 測った結果の判定ではないが、対象が ARMv7-M/v8-M だと仮定した暗黙の既定。route.to_inject を設定するだけで、番地を書かずに Cortex-A・RISC-V 等の 0xE000EDF0 に書き込みが起きうる。「境界の値を暗黙の既定にしない」に当たるかは、既定の書き込み先が「境界の値」に含まれるかで変わる。

### ?3. SWO の Manchester の解読の閾値（T ± floor(T/4)）と「開始ではない」の黙った捨て

- 場所: `gateware/rtl/debug/swo_manchester_rx.vhd:10-17`、`swo_manchester_rx.vhd:63-66`、`swo_manchester_rx.vhd:96-97`
- コード:
  ```vhdl
  q        := shift_right(t, 2);
  mid_from <= t - q;
  end_at   <= t + q;
  ...
  elsif timer >= mid_from then                    -- 半ビットより長く High: Manchester の開始ではない
    state <= S_WAIT_LOW;
  ```
- 理由: 線の形を解読するには何らかの閾値が要り、1/4 ビットは方式から決まる自然な値（境目 T/2 と中央 T の間）。ただし固定で変えられず、閾値の外れ（「開始ではない」と見なした High）は数えずに捨てる（frame_error は 8 ビットの端数だけ）。解読の定数としては許容範囲だが、捨てた事象が残らない点が原則（欠けの明示）と合うか判断がつかない。

### ?4. 時刻の同期の受信: パルスの半分より短い 1 を黙って捨てる

- 場所: `gateware/rtl/core/sync_link.vhd:7`、`sync_link.vhd:188-195`
- コード:
  ```vhdl
  if rx_s = '1' then
    rx_wait  <= BIT_TICKS - 1;             -- 最初のビットの中央まで
    rx_state <= S_RX_BITS;
  else
    rx_state <= S_RX_IDLE;                 -- パルスの半分より短い: 受けない
  ```
- 理由: 開始の確認（UART の開始ビットの再確認と同じ）は方式の一部。CRC の誤りは `crc_errors` に数えるが、この捨てたパルスは数えない。雑音と本物の欠けを区別できない（欠けの明示が無い）。

### ?5. 観測の口の設定の黙った切り詰め（IR 長・語数）

- 場所: `gateware/rtl/debug/obs_jtag_engine.vhd:211-217`、`obs_jtag_engine.vhd:551-557`
- コード:
  ```vhdl
  if cfg_ir_len = 0 then return 1; elsif cfg_ir_len > 32 then return 32; end if;
  ...
  if cfg_words = 0 then m_words <= to_unsigned(1, 9);
  elsif cfg_words > 256 then m_words <= to_unsigned(256, 9);
  ```
- 理由: 経験則ではなく範囲の制限だが、CSR の欄は 9 ビット（〜511）・6 ビット（〜63）を受け付け、切り詰めたことと使った値をどこにも返さない（「打ち切りは切ったことと使った上限を返す」）。

### ?6. サンプラの「ゆらぎ」を周期が短いと黙って無効にする

- 場所: `gateware/rtl/debug/dap_sampler.vhd:279`、`dap_sampler.vhd:287-291`
- コード:
  ```vhdl
  jit_on     <= jitter /= 0 and period > shift_left(to_unsigned(1, 32), jitter);
  ...
  if jit_on then reload <= jit_base + jit_masked; else reload <= period; end if;
  ```
- 理由: 使う側が指定した jitter を、period ≤ 2^jitter のとき黙って 0 扱いにする。判定の閾値自体は算術上の制約（負の周期を避ける）だが、効いていないことを返さない。

### ?7. DAP_Transfer の装置番号が範囲外なら黙って 0 番

- 場所: `gateware/rtl/debug/dap_cmd_transfer.vhd:141-148`（説明 `dap_cmd_transfer.vhd:3`）
- コード:
  ```vhdl
  procedure select_device(index : std_logic_vector(7 downto 0)) is
  begin
    if unsigned(index) < jtag_count then index_r <= to_integer(unsigned(index));
    else index_r <= 0; end if;
  ```
- 理由: 経験則ではないが、誤った入力を別の対象（装置 0）への操作に黙って置き換える。JTAG_IDCODE は範囲外をエラーにしている（`dap_cmd_transfer.vhd:212`）ので扱いがそろっていない。参照実装の挙動に合わせたものかは本点検の範囲（ファイルを読むだけ）では確かめられない。

### ?8. 深いバッファの止め際に作りかけの拍・RLE のレコードを捨てる

- 場所: `gateware/rtl/la/la_encoder.vhd:7`、`la_encoder.vhd:120-122`
- コード:
  ```vhdl
  -- run の 0→1 で始め（RLE の最初のレコードは最初のサンプルから）、1→0 で止める（作りかけの拍は捨てる）。
  ...
  elsif run = '0' then
    armed    <= true;
    open_rec <= false;
  ```
- 理由: 最後の最大 7 サンプル（RAW）・3 レコード + 作りかけのレコード（RLE）が記録に入らず、捨てた数も返さない。止めた時刻の直前が欠けることは、「欠けている所」を事実として返す原則に照らすと申告が要るかもしれない。経験則ではない。

### ?9. UART ブリッジの「現在のボーレート」は要求の値で、実際の値ではない

- 場所: `gateware/rtl/serial/uart_bridge.vhd:120-128`、`uart_bridge.vhd:183`（CSR の説明 `fprobe_gw/csr/fprobe_map.py:58`）
- コード:
  ```vhdl
  baud_r <= pending;
  if quotient > MAX_DIVISOR then divisor <= to_unsigned(MAX_DIVISOR, DIVISOR_BITS);
  elsif quotient < 2 then divisor <= to_unsigned(2, DIVISOR_BITS);
  ...
  current_baud <= baud_r;
  ```
- 理由: 分周比の丸め・制限の後の実際の速さ（CLK_HZ / divisor）ではなく、頼まれた値を「現在のボーレート」として出す。制限に当たっても印が無い。SWO の方（`dap_cmd_swo.vhd:155-187`）は実際の値を返す。経験則ではないが、出している値が測った・設定した事実と食い違いうる。

### ?10. 割り込み書き込みの実行中に来た要求を数えずに捨てる

- 場所: `gateware/rtl/debug/swd_injector.vhd:24`、`swd_injector.vhd:162-169`
- コード:
  ```vhdl
  request       : in  std_logic;                       -- 1 周期のパルス（実行中に来た要求は捨てる）
  ```
- 理由: クロストリガの源が続けて来たとき、捨てた回数が残らない（成功は `count`、未接続は result = 2 で残る）。欠けの明示の観点で判断がつかない。

### ?11. Ethernet: 送信先が決まる前のフレームを数えずに捨てる

- 場所: `gateware/rtl/eth/udp_stack.vhd:11-12`、`udp_stack.vhd:386-388`
- コード:
  ```vhdl
  if not peer_ok then
    done_r <= '1';                         -- 送信先がまだ無い: 捨てる
  ```
- 理由: リンク層の振る舞いで測定の結果ではない。ただ SWO・ストリーム等のフレームが PC から最初の UDP が来るまで黙って消える。送信先を「最後に受けた正しい UDP の送り主」にする（`udp_stack.vhd:323-331`）のも慣習による決め方。

### ?12. Ethernet 版のトップの診断: 長さ 8・12 以外の要求を「変わった長さ」として数える

- 場所: `gateware/rtl/boards/primer20k/primer20k_fprobe_eth_top.vhd:218-230`
- コード:
  ```vhdl
  -- コアが受け取った CONTROL の要求の長さ（8 = 読み、12 = 1 語の書き。それ以外を数える）
  if req_bytes + 1 /= 8 and req_bytes + 1 /= 12 then
    odd_n <= odd_n + 1;
  ```
- 理由: 「普通の要求は 8 か 12 バイト」という利用の仮定で分類している。BL702 の UART に出す基板の診断で、本体の API には出ない。対象外とするか判断がつかない。

### ?13. 開発の道具: 出力の文字列・欠けた行から成否を決める

- 場所:
  - `gateware/fprobe_gw/toolchain/programmer.py:97-98`
    ```python
    failed = (result.returncode != 0 or "Finished." not in output
              or re.search(r"\b(error|unsupported)\b", output, re.I))
    ```
  - `gateware/fprobe_gw/toolchain/build.py:225`（IP の生成: `"ERROR" in (result.stdout or "")`）
  - `gateware/fprobe_gw/toolchain/timing.py:107-108`（TNS の行が無いクロックは `0.0` = 違反なし）
    ```python
    setup_tns=tns.get((n, "Setup"), 0.0),
    hold_tns=tns.get((n, "Hold"), 0.0))
    ```
  - `gateware/fprobe_gw/toolchain/ftdi.py:39-42`（説明もシリアルも空なら「他プロセス使用中」と推定して表示）
- 理由: どれもビルド・書き込みの道具で、測定器の結果ではない。ただ「名前の慣習での判定」「無い行を 0 とみなす」「空から使用中を推す」は原則の言う推定・判定の形をしている。timing.py の既定 0.0 は、行の欠けを「満たした」と読む方向（安全でない側）に倒れる。道具を原則の対象に含めるかで判断が変わる。

### ?14. メモリの状態の説明に「動いている」の目安を書いている

- 場所: `gateware/fprobe_gw/csr/fprobe_map.py:146-147`、`fprobe_map.py:239`
- コード:
  ```python
  Field("clock_tick", 4, 1, "RO", doc="メモリ側クロックで数えた計数の 1 ビット（約 10 ms ごとに反転すれば動いている）")
  Register.word("samples", 0x04, "RO", doc="受け取った計測値の数（補助マイコンが動いているかの目安）"),
  ```
- 理由: 回路は事実（計数のビット・受信数）だけを出しており、判定は説明文の中だけ。説明文の判断の書き方を原則の対象とするか判断がつかない。

---

## 点検したが H・? にしなかった主なもの（判断の記録）

- USB（`usb_timing_pkg.vhd` の T_RESET_SE0・T_CHIRP 等、`usb_protocol.vhd` の T_DATA_TIMEOUT・T_ACK_TIMEOUT、`usb_control_ep.vhd` の T_HANDLER_TIMEOUT、`usb_ep_in.vhd` の FLUSH_CYCLES）: USB 2.0 の仕様・プロトコルの時間で、測った結果の判定ではない。
- ULPI・RMII・UDP・COBS・CRC（`ulpi_link.vhd`・`rmii_rx.vhd`・`udp_stack.vhd`・`serial_link.vhd`・`usb_crc_pkg.vhd`）: 規格で決まる定数・形式の検査。
- `swd_phy.vhd` のプロトコル異常の後の待ち（turnaround + 33）、`dap_link.vhd` の WAIT 再試行、`dap_cmd_config.vhd` の wait_retry 既定 100・SWCLK 既定 1 MHz: CMSIS-DAP 参照実装と同じ既定で、ホストが DAP_TransferConfigure / SWJ_Clock で上書きする。
- `uart_rx.vhd` の開始ビットの半ビットでの再確認: UART の方式。
- `frame_mux.vhd`・`usb_ep_in.vhd` の IDLE_CYCLES / FLUSH_CYCLES、`la_deep_buffer.vhd` の LA_MIN・EV_MIN・AGE_BITS: 塊の区切り（遅れ）だけに効き、記録の中身は変えない。
- `swo_receiver.vhd` の FLUSH_CYCLES の窓で捨てるバイト: 「受信を窓の分遅く始めたのと同じ」と仕様で定義した開始の位置。
- `obs_jtag_engine.vhd:381-387` の標本型の重複の省略: 値も変化の回数も同じ標本だけを省き、スキャンの番号（address）で間が分かるので情報は失われない。
- `fprobe_core.vhd` のイベントの 1 件保持と `byte_lost`: 落とした数を events.lost に足しており、欠けを明示している。
- `dap_sampler.vhd` の FAULT 以外の異常を「線の不調」として SELECT だけ戻す: 回復の手順の選び方で、出す結果には効かない（誤りはすべて errors に数える）。
- `build.py` の build_best（余裕が最大の版を選ぶ）、`pll.py` の同じ誤差なら PFD の高い解: 測った値（Fmax）と決まった順序で選ぶ道具。
- `designs.py`・`boards/*.py` の実機の経緯を書いた構成の取捨（sampler_match を Primer で外す等）: 構成の宣言で、ケイパビリティで申告される。
- `caps.py` の SYNC_RX_LATENCY_TICKS・SYNC_TX_LATENCY_TICKS・LA_*_DELAY の段数: 回路の段数そのもの（H1・H2 は平均の 0.5 だけを問題にしている）。

---

## 読んだファイル（171 ファイル。すべて先頭から最後まで読んだ）

`gateware/rtl` の .vhd 130 ファイル（生成物 `generated/` を含む）と `gateware/fprobe_gw` の .py 41 ファイル。各ファイルを `cat -n` の全文で読み、行数は下のとおり（VHDL 合計 21,284 行、Python 合計 4,849 行）。

- gateware/fprobe_gw/__init__.py（3 行）
- gateware/fprobe_gw/__main__.py（5 行）
- gateware/fprobe_gw/boards/__init__.py（171 行）
- gateware/fprobe_gw/boards/nano9k.py（84 行）
- gateware/fprobe_gw/boards/primer20k.py（142 行）
- gateware/fprobe_gw/boards/pynq_z2.py（71 行）
- gateware/fprobe_gw/build_info.py（54 行）
- gateware/fprobe_gw/caps.py（296 行）
- gateware/fprobe_gw/cli.py（201 行）
- gateware/fprobe_gw/csr/__init__.py（1 行）
- gateware/fprobe_gw/csr/fprobe_map.py（387 行）
- gateware/fprobe_gw/csr/host.py（55 行）
- gateware/fprobe_gw/csr/markdown.py（27 行）
- gateware/fprobe_gw/csr/model.py（194 行）
- gateware/fprobe_gw/csr/vhdl.py（183 行）
- gateware/fprobe_gw/dap_info.py（81 行）
- gateware/fprobe_gw/designs.py（298 行）
- gateware/fprobe_gw/generate.py（80 行）
- gateware/fprobe_gw/protocol.py（21 行）
- gateware/fprobe_gw/pynq_setup.py（84 行）
- gateware/fprobe_gw/rtl.py（28 行）
- gateware/fprobe_gw/toolchain/__init__.py（1 行）
- gateware/fprobe_gw/toolchain/build.py（309 行）
- gateware/fprobe_gw/toolchain/forgefpga.py（251 行）
- gateware/fprobe_gw/toolchain/ftdi.py（98 行）
- gateware/fprobe_gw/toolchain/ghdl.py（88 行）
- gateware/fprobe_gw/toolchain/gowin.py（48 行）
- gateware/fprobe_gw/toolchain/pll.py（97 行）
- gateware/fprobe_gw/toolchain/programmer.py（125 行）
- gateware/fprobe_gw/toolchain/project.py（244 行）
- gateware/fprobe_gw/toolchain/pynq_loader.py（85 行）
- gateware/fprobe_gw/toolchain/resources.py（87 行）
- gateware/fprobe_gw/toolchain/sim.py（64 行）
- gateware/fprobe_gw/toolchain/timing.py（130 行）
- gateware/fprobe_gw/toolchain/vivado.py（192 行）
- gateware/fprobe_gw/toolchain/vivado_build.py（115 行）
- gateware/fprobe_gw/toolchain/zephyr.py（54 行）
- gateware/fprobe_gw/usb/__init__.py（1 行）
- gateware/fprobe_gw/usb/descriptors.py（149 行）
- gateware/fprobe_gw/usb/fprobe_device.py（99 行）
- gateware/fprobe_gw/usb/vhdl_rom.py（146 行）
- gateware/rtl/boards/nano9k/nano9k_fprobe_top.vhd（124 行）
- gateware/rtl/boards/nano9k/nano9k_pkg.vhd（28 行）
- gateware/rtl/boards/primer20k/gowin_rpll.vhd（71 行）
- gateware/rtl/boards/primer20k/primer20k_bringup_top.vhd（46 行）
- gateware/rtl/boards/primer20k/primer20k_ddr3.vhd（146 行）
- gateware/rtl/boards/primer20k/primer20k_fprobe_eth_top.vhd（308 行）
- gateware/rtl/boards/primer20k/primer20k_fprobe_serial_top.vhd（126 行）
- gateware/rtl/boards/primer20k/primer20k_fprobe_top.vhd（366 行）
- gateware/rtl/boards/primer20k/primer20k_pkg.vhd（56 行）
- gateware/rtl/boards/primer20k/primer20k_ulpi_probe_top.vhd（53 行）
- gateware/rtl/boards/primer20k/primer20k_ulpi_reset.vhd（37 行）
- gateware/rtl/boards/primer20k/primer20k_usbtest_top.vhd（203 行）
- gateware/rtl/boards/pynq_z2/pynq_z2_blinky_top.vhd（36 行）
- gateware/rtl/boards/pynq_z2/pynq_z2_fprobe_top.vhd（404 行）
- gateware/rtl/boards/pynq_z2/pynq_z2_pkg.vhd（41 行）
- gateware/rtl/boards/pynq_z2/xilinx_mmcm.vhd（55 行）
- gateware/rtl/common/async_fifo.vhd（159 行）
- gateware/rtl/common/cdc_value.vhd（57 行）
- gateware/rtl/common/reset_sync.vhd（35 行）
- gateware/rtl/common/sdp_ram.vhd（36 行）
- gateware/rtl/common/stream_slice.vhd（72 行）
- gateware/rtl/common/stream_unpack.vhd（48 行）
- gateware/rtl/common/sync_bit.vhd（32 行）
- gateware/rtl/common/sync_fifo.vhd（96 行）
- gateware/rtl/common/text_report.vhd（70 行）
- gateware/rtl/common/text_report_pkg.vhd（51 行）
- gateware/rtl/common/udiv.vhd（70 行）
- gateware/rtl/common/udiv_shared.vhd（66 行）
- gateware/rtl/common/util_pkg.vhd（31 行）
- gateware/rtl/core/aux_link.vhd（82 行）
- gateware/rtl/core/caps_rom.vhd（50 行）
- gateware/rtl/core/control_plane.vhd（252 行）
- gateware/rtl/core/csr_absent.vhd（28 行）
- gateware/rtl/core/csr_bus_pkg.vhd（46 行）
- gateware/rtl/core/csr_decoder.vhd（98 行）
- gateware/rtl/core/fprobe_core.vhd（1389 行）
- gateware/rtl/core/generated/build_info_pkg.vhd（16 行）
- gateware/rtl/core/generated/csr_aux.vhd（64 行）
- gateware/rtl/core/generated/csr_debug.vhd（69 行）
- gateware/rtl/core/generated/csr_events.vhd（72 行）
- gateware/rtl/core/generated/csr_gpio.vhd（81 行）
- gateware/rtl/core/generated/csr_la.vhd（190 行）
- gateware/rtl/core/generated/csr_la_deep.vhd（150 行）
- gateware/rtl/core/generated/csr_map_pkg.vhd（63 行）
- gateware/rtl/core/generated/csr_mem.vhd（112 行）
- gateware/rtl/core/generated/csr_obs.vhd（214 行）
- gateware/rtl/core/generated/csr_sampler.vhd（141 行）
- gateware/rtl/core/generated/csr_sampler_match.vhd（112 行）
- gateware/rtl/core/generated/csr_sync.vhd（83 行）
- gateware/rtl/core/generated/csr_system.vhd（67 行）
- gateware/rtl/core/generated/csr_timebase.vhd（63 行）
- gateware/rtl/core/generated/csr_trigseq.vhd（227 行）
- gateware/rtl/core/generated/csr_uart.vhd（105 行）
- gateware/rtl/core/generated/csr_xtrig.vhd（179 行）
- gateware/rtl/core/generated/csr_xtrig_out.vhd（201 行）
- gateware/rtl/core/pin_select.vhd（65 行）
- gateware/rtl/core/sync_link.vhd（229 行）
- gateware/rtl/core/timebase.vhd（39 行）
- gateware/rtl/core/trig_seq.vhd（133 行）
- gateware/rtl/core/xtrig_out.vhd（126 行）
- gateware/rtl/debug/dap_cmd_config.vhd（364 行）
- gateware/rtl/debug/dap_cmd_sequence.vhd（293 行）
- gateware/rtl/debug/dap_cmd_swo.vhd（196 行）
- gateware/rtl/debug/dap_cmd_transfer.vhd（472 行）
- gateware/rtl/debug/dap_link.vhd（125 行）
- gateware/rtl/debug/dap_monitor.vhd（155 行）
- gateware/rtl/debug/dap_packet.vhd（270 行）
- gateware/rtl/debug/dap_pkg.vhd（121 行）
- gateware/rtl/debug/dap_processor.vhd（257 行）
- gateware/rtl/debug/dap_sampler.vhd（516 行）
- gateware/rtl/debug/debug_port.vhd（120 行）
- gateware/rtl/debug/generated/dap_info_pkg.vhd（54 行）
- gateware/rtl/debug/jtag_phy.vhd（435 行）
- gateware/rtl/debug/obs_jtag_engine.vhd（651 行）
- gateware/rtl/debug/sampler_match.vhd（80 行）
- gateware/rtl/debug/swd_injector.vhd（247 行）
- gateware/rtl/debug/swd_phy.vhd（334 行）
- gateware/rtl/debug/swo_manchester_rx.vhd（127 行）
- gateware/rtl/debug/swo_receiver.vhd（125 行）
- gateware/rtl/designs/bringup_core.vhd（174 行）
- gateware/rtl/designs/generated/fprobe_caps_pkg.vhd（83 行）
- gateware/rtl/designs/generated/fprobe_core_m_caps_pkg.vhd（85 行）
- gateware/rtl/designs/generated/fprobe_eth_caps_pkg.vhd（79 行）
- gateware/rtl/designs/generated/fprobe_jtag_caps_pkg.vhd（84 行）
- gateware/rtl/designs/generated/fprobe_la_caps_pkg.vhd（84 行）
- gateware/rtl/designs/generated/fprobe_nano9k_caps_pkg.vhd（76 行）
- gateware/rtl/designs/generated/fprobe_pynq_caps_pkg.vhd（79 行）
- gateware/rtl/designs/generated/fprobe_pynq_deep_caps_pkg.vhd（82 行）
- gateware/rtl/designs/generated/fprobe_serial_caps_pkg.vhd（78 行）
- gateware/rtl/designs/generated/memtest_caps_pkg.vhd（67 行）
- gateware/rtl/designs/ulpi_probe_core.vhd（167 行）
- gateware/rtl/designs/usbtest_responder.vhd（50 行）
- gateware/rtl/eth/rmii_rx.vhd（157 行）
- gateware/rtl/eth/rmii_tx.vhd（135 行）
- gateware/rtl/eth/udp_stack.vhd（545 行）
- gateware/rtl/hostlink/eth_link.vhd（147 行）
- gateware/rtl/hostlink/frame_mux.vhd（389 行）
- gateware/rtl/hostlink/serial_link.vhd（347 行）
- gateware/rtl/hostlink/zynq_frame_link.vhd（253 行）
- gateware/rtl/la/event_ring.vhd（125 行）
- gateware/rtl/la/la_capture.vhd（145 行）
- gateware/rtl/la/la_deep_buffer.vhd（596 行）
- gateware/rtl/la/la_encoder.vhd（156 行）
- gateware/rtl/la/la_filter.vhd（72 行）
- gateware/rtl/la/la_front.vhd（111 行）
- gateware/rtl/la/la_mem_csr.vhd（56 行）
- gateware/rtl/la/la_pulse.vhd（92 行）
- gateware/rtl/mem/axi_mem_port.vhd（180 行）
- gateware/rtl/mem/la_mem_writer.vhd（150 行）
- gateware/rtl/mem/mem_reader.vhd（114 行）
- gateware/rtl/mem/mem_test.vhd（178 行）
- gateware/rtl/mem/mem_test_sys.vhd（138 行）
- gateware/rtl/serial/uart_bridge.vhd（186 行）
- gateware/rtl/serial/uart_rx.vhd（116 行）
- gateware/rtl/serial/uart_tx.vhd（64 行）
- gateware/rtl/usb/generated/usb_descriptors_pkg.vhd（157 行）
- gateware/rtl/usb/ulpi_link.vhd（388 行）
- gateware/rtl/usb/usb_cdc_acm_requests.vhd（146 行）
- gateware/rtl/usb/usb_cdc_notify.vhd（88 行）
- gateware/rtl/usb/usb_control_ep.vhd（492 行）
- gateware/rtl/usb/usb_crc_pkg.vhd（54 行）
- gateware/rtl/usb/usb_device.vhd（377 行）
- gateware/rtl/usb/usb_ep_in.vhd（205 行）
- gateware/rtl/usb/usb_ep_out.vhd（128 行）
- gateware/rtl/usb/usb_protocol.vhd（316 行）
- gateware/rtl/usb/usb_reset.vhd（313 行）
- gateware/rtl/usb/usb_rx_packet.vhd（179 行）
- gateware/rtl/usb/usb_timing_pkg.vhd（17 行）
- gateware/rtl/usb/usb_tx_packet.vhd（144 行）
- gateware/rtl/usb/usb_types_pkg.vhd（29 行）
