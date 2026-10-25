# 不具合台帳: 外部メモリ経路の RTL（検出専用。修正は未適用）

調べた日: 2026-10-25。担当範囲: `rtl/mem/{axi_mem_port,la_mem_writer,mem_reader}.vhd`、`rtl/la/la_deep_buffer.vhd`（外部メモリ側）、
`rtl/la/event_ring.vhd`、`rtl/la/la_mem_csr.vhd`、`rtl/core/{aux_link,sync_link}.vhd`、`csr_la_deep` の読み書きの境界。
再現試験: `gateware/tests/test_deepmem_bugs.py`（strict xfail。直すと XPASS で失敗するので xfail を外す）。
テストベンチ: `tests/tb/deepmem_ring_bugs_tb.py`、`tests/tb/deepmem_axi_bugs_tb.py`。重さ: A = 静かに誤った結果 / B = 失敗が見える / C = 端の条件・まれ / D = 品質。

件数（再現した 4 件）: A 1、B 0、C 3、D 0。疑い 7 件（C 3、D 4）。

## 再現した不具合

### BUG-RTL-MEM-1（A）読み出し中に新しい start を出すと、前の読みの未着の結果が新しい読みに混ざる
- 試験: `test_mem_1_reader_restart`（`bug_mem_1_reader_restart_takes_stale_beats`）。A を 200 拍で始め、受け皿を満たして 30 周期後に B（500 から 20 拍）を始めると、B の先頭が A の 8 拍目以降（0x1008...）になる。
- 原因: `mem_reader.vhd:84-102`。start で受け皿 FIFO（`rst or start`）と `in_flight` を 0 に戻すが、メモリ側（mem_port、AXI の R）に発行済みで未着の読みは取り消せず、後から `rd_valid` で届いて新しい FIFO に入る。`to_output` は新しい count なので、混ざった拍がそのまま数に入り、B の末尾 20 拍目までのうち古い拍が出て、本当の B の拍は出ない。
- 直し方の案: 発行済みで未着の数（in_flight のうち FIFO に未着のもの）を start 後に数え、その数だけ届く `rd_valid` を捨てる。あるいは busy の間の start を無視して `busy` で受けつけない旨を返す。`la_deep_buffer` の `read`（`m_rd_new`）と `s_start`（ストリームの次のかたまり）も同じ経路なので、`reading` 中の `read` を CSR 側で拒否する手もある。
- 影響: PC が読み出しの途中で新しい読みを出す（中止して読み直す）と、静かに別の範囲のデータを返す。`reading` 中に `command.read` を書けてしまう。
- 確信度: 高（再現済み）。実際の運用で起きるかは PC 側の手順次第（C に近い）。
- **修正済み: mem_reader は発行済みで未着の読みの数（outst）を数え、start 時に drop へ移して、届いた結果を受け皿に入れず捨てる。room は in_flight + drop < LANDING なので受け皿は溢れない。CSR・イベントの形式は不変。試験の xfail は外した**

### BUG-RTL-MEM-2（C）first が領域の大きさ以上だと折り返さず領域の外を読む
- 試験: `test_mem_2_first_beyond_region`。region_size = 960、first = 965、count = 4 で、発行番地が 965..968（イベントのリング）になる。
- 原因: `mem_reader.vhd:92-97`。折り返しは `offset + 1 = size` の一致比較だけなので、`first >= size` では一致せず領域の外へ進む。region_size = 0 でも同様に一致しない。`csr_la_deep` の `read_first`（ADDR_BITS ビットに切り詰め）は領域の大きさを超えうる（LA のリングは容量の 15/16）。
- 直し方の案: start で `first` を `first mod size` に正す（`first >= size` なら引く）、size = 0 は count を 0 扱い。比較を `offset + 1 >= size` にする案もあるが、それでは `first` が領域外から始まる問題が残る。
- 影響: PC が first に 960..1023（ロジアナのリングの外）を渡すと、イベントのリングの内容がロジアナのデータとして返る。
- 確信度: 高（再現済み）。PC 側が first を検証していれば出ない。
- **修正済み: first が領域の中なら従来どおり即発行、領域の大きさ以上なら領域の大きさを引く（reducing、引ける回数だけサイクルを使う）。region_size = 0 は 0 拍の読み。試験の xfail は外した**

### BUG-RTL-AXI-1（C）rst を途中で入れると、未完了の AXI 取引が相手に残って次の書きと組になる
- 試験: `test_axi_1_reset_outstanding`。書きのコマンド直後に 0〜5 周期後 rst を 2 周期入れ、その後の書き 1 件 + 読みで確かめると、模型が「WLAST の位置が違う」で止まる（AW だけ、または W の一部だけ渡した書きが相手に残り、次の書きの W と組になる）。
- 原因: `axi_mem_port.vhd:121-135`。rst で `aw_pending` / `w_left` / `ar_pending` を落とすが、相手（PS の S_AXI_HP0）は rst されない。AW が済んで W が途中の書きは、相手が残りの W を待ち続ける。また rst の後に届く B・R は `wr_out`（natural range 0 to MAX_OUTSTANDING）を 0 から 1 引くので、合成では巻き戻り（`wr_out` = 範囲外の値）、シミュレーションでは範囲外の違反。
- 直し方の案: rst の間も AW/W を最後まで出しきってから止まる（rst を「新しい取引を受けない」にする）、または戻りの応答を数え続けて 0 になるまで `ready` を 0 にする（`wr_out`・`rd_out` の減算は 0 で止める）。
- 影響: メモリ側のリセットだけを再度入れた場合に、書き込みの番地が静かにずれる。通常運用でメモリの rst が入るのは起動時だけなら顕在化しない。
- 確信度: 高（再現済み。実機での発生頻度は低い）。
- **直さない: AW/W の途中の取引を完結させるには、起動時の rst（相手が idle・内部が未初期化）と途中の rst を区別する状態が要り、回路が大きくなる。axi_mem_port の説明に「メモリ側の rst は起動時の 1 回だけ。途中の rst は支援しない」を明記し、rst の後に届く古い B・R で wr_out・rd_out が 0 から巻き戻らないよう 0 で止めた。試験は xfail のまま**

### BUG-RTL-AXI-2（C）base が 16 の倍数でないと 16 バイトのバーストが 4 KB 境界をまたぐ
- 試験: `test_axi_2_unaligned_base`。base = 0x1E000FF8 で書くと `awaddr = 0x1e000ff8`（16 バイトのバーストが 0x1000 をまたぐ。AXI 違反）。
- 原因: `axi_mem_port.vhd:94-95`。`enabled` は `base /= 0` だけで、下位 4 ビットを見ない。`mem_base` は CSR（`zynq_frame_link`）から 32 ビットそのまま。
- 直し方の案: 下位 4 ビットを 0 に落とす（`base(31 downto 4) & "0000"`）か、`base(3 downto 0) /= 0` のとき `ready = 0` にする。
- 影響: PS 側の ACP/HP のインターコネクトが 4 KB またぎのバーストを分割・拒否して、書きが化ける・止まる可能性。仕様は「16 の倍数」だが検査が無い。
- 確信度: 高（再現済み）。
- **修正済み: base の下位 4 ビットが 0 でないとき ready = 0（コマンドを受けない。base = 0 と同じ扱い）。generic の説明に明記。試験の xfail は外した**

## 疑い（再現できなかった、または試験を書いていない）

- **疑い S-1（C）**: `axi_mem_port.vhd` の `err_count` は 16 ビットで、65536 回で 0 に戻る（飽和しない）。大量の誤り応答の後に errors = 0 に見える。`la_mem_writer` の `ev_count`・`total` は飽和・折り返しの方針がまちまち。直し方の案: 0xFFFF で飽和。
- **疑い S-2（D）**: 書きと読みの順（`axi_mem_port.vhd:7-10` の注釈）は「書きの B がすべて返ってから読みを出す」だけで、読みを出した後の書きの追い越し（AR → AW の順で相手が AW を先に処理）は防いでいない。深いバッファのストリームでは、読み出し中の拍の再利用はリングの余白（STREAM_CHUNK）で避けているが、書きが読みを追い越すとその余白が無効になる。実機の PS は読みと書きを別チャネルで独立に処理する。
- **疑い S-3（C）**: `la_mem_writer.vhd:116-124`、post_beats がロジアナのリング（LA_BEATS）以上だと、トリガの拍を含むトリガ前の記録を上書きする（`trigger_beat` が指す拍が失われる）。入力の範囲の検査が無い。
- **疑い S-4（C）**: `csr_la_deep` の `read_count`（32 ビット）を `fprobe_core.vhd:1078` で `deep_count(MEM_ADDR_BITS downto 0)` に切り詰める。2^(MEM_ADDR_BITS+1) の倍数を書くと 0 拍の読みになる、2^(N+1)+5 は 5 拍になる。`read_first` も同様。上位ビットが 0 でないときに誤りを返すかビジー扱いにするのが望ましい。
- **疑い S-5（D）**: `event_ring.vhd` の `written`（32 ビット）は折り返し、`la_deep_buffer` / `la_mem_writer` の `ev_count` は 0xFFFFFFFF で飽和する。同じ CSR（events_written）の意味が構成で違う。
- **疑い S-6（D）**: `sync_link.vhd` は `BIT_TICKS` が 1 だと `BIT_TICKS / 2 - 1 = -1`（natural の範囲外）。2 でも半ビットの余裕が 0 でクロック差に耐えない。generic を `positive` でなく 4 以上に絞る。
- **疑い S-7（D）**: `aux_link.vhd` は `ch >= CHANNELS` の組を黙って捨て、`errors` に数えない（試験 `aux_link_tb` が「ch 9 は捨てる」を期待している設計判断だが、AI 側の観測には欠落の数が出ない）。

## 調べて問題が見つからなかったもの（再現を試みた）

- `ev_enable` を立てた直後の周期から連続で来るイベント（`la_deep_buffer`）: 4 件とも書かれ `ev_lost` = 0（`events_at_enable_edge` を試し、通った。ev_en_m の遅れより FIFO の遅れが長い）。
- `aux_link` の途中参加・組の途中の先頭・範囲外のチャンネル: 既存試験で確認、新たな不一致は見つからなかった（UART 受信器は停止ビット異常のバイトを出さないので、欠けた組は次の先頭で揃う）。
- `sync_link` の送受信の符号（パルス 1 + 時刻 64 + CRC 8、先頭の中央から 1.5 ビット目で最初のビット）: 読みで整合を確認（符号の長さ 73、CRC の送出順）。ランダムなビット誤りの試験は書いていない。
- `la_mem_csr`: 2 相ハンドシェイクの保持（req を ack まで保持）と整合。

## 調べていない範囲

- `mem_test.vhd` / `mem_test_sys.vhd`（メモリの自己試験）、DDR3 側（Primer）のコントローラ、`zynq_frame_link` の mem_base の書き込み経路。
- `la_deep_buffer` のストリーム（STREAM_EVENTS の見出し・ev_lim・T_* の状態機械）の長時間のランダム試験と、sys と mem のクロック比を変えた競合（run の 0→1→0 が速い場合の `cdc_value` の取りこぼしなど）。読みで疑った点: run の短い切り替えでは start が飛ばされ stop だけが残る（net の効果は同じ）。
- AXI の模型は書きの B と読みの R の順序を保つので、AR と AW の追い越し（疑い S-2）は再現していない。
