# U23 長時間の記録の運用・U27 ヒープの解析 — 設計

対象の競合機能: U23 = TZ-12（巨大トレースの索引・プレビュー・部分読み込み）、DT-04（30 日の連続記録）、
SOP-01（100 GB 超の長時間トレース）。U27 = SEG-68（ヒープ監視）、TZ-06（malloc / free の推移・リーク）、
CTX-07（ヒープのオーバーフロー検出）。

## 1. 長期の記録（U23）

### 1.1 形

普通の記録（F3、format 1）を時間か大きさで分割する（format 2、`layout = "segmented"`）。

```
<記録>/meta.json         timebase_hz・policy・summary（w0・fan・levels・index_level）
<記録>/index.sqlite      segments / summary（粗い段）/ wave（波形のかたまりの要約）
<記録>/segments/NNNNNN/  普通の記録 + summary（細かい段。閉じたときに作る）
```

- 分割は普通の記録そのもの（既存の読み手・書き出しがそのまま使える）。全体の索引だけが新しい。
- `Recording.open` は meta を見て `SegmentedRecording` を返す。道具（events・find・intervals・density・waveform・
  export・query・waveform_measure・Perfetto）は読み手の口が同じなので変えない。

### 1.2 縮図の事前計算（`recording_summary`）

- 段 0 の幅 w0 = タイムベースの 2 の冪（約 1 ms）、段ごとに 16 倍、10 段。区間の番号は絶対時刻 // 幅
  （分割をまたいでもそろう）。
- 分割を閉じるとき、stream ごと・区間ごとの件数を段 0 から足し上げる。段 0〜2 は分割に、段 3（約 4 秒）以上は索引に
  （同じ区間は加算）。
- 範囲 [t0, t1) の件数は `decompose` で「丸ごとの区間（段ごとに端の 15 個未満）」と「段 0 より短い端（生で数える）」に
  分けて和を取る。件数に比例せず段の数に比例する。**正確**（普通の記録と一致することを乱数で試験）。
- 開いている分割は縮図が無いので生で数える（分割の時間で大きさが抑えられる）。
- 波形は、かたまりを足すときに索引の wave に「最初の値・最後の値・変化したチャンネル」を書く。画素がかたまりを
  丸ごと含めば要約で、端がかかればファイルを読んで正確に（普通の記録の waveform_view と一致）。
- `overview`: 画素の境を段 3 以上の区間にそろえ、索引だけで出す（30 日でも分割を開かない。消した分割の件数も残る）。

### 1.3 運用の方針（`Policy`）

| 項目 | 意味 |
|---|---|
| segment_s / segment_bytes | 分割を閉じる時間 / 大きさ（flush のときに判断） |
| decimate_after_s / decimate_streams | 閉じてからこの時間が経った分割から、量の多い stream（既定 dap・sample・swo・clock・waveform）を消して VACUUM。件数の縮図は残る |
| max_bytes | 全体の容量の上限。超えたら古い分割から消す（いちばん新しい分割は消さない）。段 3 以上の縮図は索引に残る |

- 間引いた・消した部分に範囲の端がかかると件数は近似（間引いた stream は段 0 の件数を幅で按分）。
  `coverage(t0, t1)` が欠けを返し、`recording_density` は欠けがあれば coverage を添える。
- Windows では開いているファイルを消せない。読み手は 1 回の読みの間だけ分割を開く。消せなければ次の機会に。

### 1.4 部分読み込み

- 読み手は範囲に重なる分割だけを開く。時刻の重なる分割どうし（遅れて届いた出来事）は混ぜて時刻順にする。
- SQL（`recording_query`）は t0 / t1 に重なる分割を 8 個まで読み取り専用で ATTACH し、events・chunks を
  UNION ALL の一時のビューにする（表 segments・summary も）。範囲に 8 個より多ければ断る（全体は overview /
  density で）。t0 / t1 を省くと新しい 8 個（partial）。

### 1.5 ロジアナの流し続けを記録へ（`recording_stream`）

- `LaStream` を別スレッドで受け取り、chunk_s 秒ごとのかたまりにして add_waveform。かたまりの頭は前から続く値で
  始める（つなぐと全体の波形になる）。
- 時刻の基準: 始める前後にタイムベースを読み、深いバッファのトリガ時刻（条件なしのトリガは最初のサンプルで
  成立するはず）がその間にあればそれ（不確かさ 4 サンプル）、無ければ中点（往復の半分）。**実機で method が
  trigger になるかは HIL で確かめる**（test_longrec_hw.py）。
- 機器の呼び出しは 1 回ずつロックの中（`LockedDevice`）。流している間もデバッガの道具は動く。
- 深いバッファとイベント記録は読み出しの経路を共有する（F3b の課題）。流している間はイベント記録の取り込みを止め、
  ロジアナ・イベント記録の道具は断る。

### 1.6 道具

`recording.start`（segment_s・segment_bytes・max_bytes・decimate_after_s・decimate_streams）、`recording.segments`、
`recording.overview`、`recording.la_stream.start` / `stop`、`recording.query`（t0・t1）、`recording.density`（coverage）。

## 2. ヒープの解析（U27）

### 2.1 走査（`heap.scan`）

ELF のシンボル（番地・大きさ）とメモリだけで辿る（newlib は -g 無しが普通なので DWARF に頼らない）。

| アロケータ | 形 | 辿り方 |
|---|---|---|
| FreeRTOS heap_4 | BlockLink_t {next, size}、使用中は size の最上位ビット・next = NULL | ucHeap（8 揃え）から pxEnd まで物理的に。空きのリスト xStart → pxEnd と突き合わせる |
| FreeRTOS heap_5 | 同じ。領域の終わりに大きさ 0 の印 | 領域は xHeapRegions か引数 regions。無ければ空きのリストだけ |
| newlib-nano | {long size, 本体}、本体は 8 揃え、詰め物は ptr − 4 に負の位置 | __malloc_sbrk_start から break（__sbrk_heap_end・heap_end か引数 end）まで。空きは番地順の __malloc_free_list |
| newlib（dlmalloc 2.6） | {prev_size, size（bit 0 = 前が使用中）, fd, bk} | __malloc_sbrk_base（8 揃え）から top（__malloc_av_[2]）まで |

- configENABLE_HEAP_PROTECTOR のポインタは xHeapCanary で戻す。カウンタ（残量・最小残量・回数）は U08 の
  `FreeRTOSObjects.heap()` と同じ変数（定数も共有）。
- 壊れの検出（issues）: bad_size（以降を辿れない。直前の使用中のブロック = あふれの疑い）、bad_link・bad_end
  （頭・終わりの印の上書き）、空きのリストの輪・順序・外、free_not_listed（失われた空き）、listed_not_free、
  unmerged_free（繋がれない隣の空き）、bad_padding（nano）、bad_links・bad_footer（dlmalloc）、counter_mismatch。
- 断片化 = 1 − 最大の空き / 空きの合計。compare で前回の走査からの使用中のブロックの増減。

### 2.2 malloc / free の記録（`heap.trace.*`）

- ブレークポイント: 確保の関数の入口で大きさ（pvPortMalloc は r0、_malloc_r は r1）と戻り先（LR）を読み、戻り先に
  一時のブレークポイントを置いて戻ったところで r0（返した番地）を読む。解放の関数は入口で番地。
- 呼び出し元は戻り先の 1 つ前（呼び出しの命令）の場所。calloc 等の包みの中なら呼び出し履歴で外へ。
- 道具が置くブレークポイントの処理はブレークポイントの動作（`_break_hooks`。True = 続ける・False = 止める・
  None = ふつうの動作）に足した。使う人のブレークポイントが入口にあれば断り、戻り先にあれば処理だけ足して止める。
- `AllocationTracker`: 生きている確保・最大・誤り（二重の解放・失敗・解放されていない番地の再利用）・リークの候補
  （呼び出し元ごとの古い生きている確保。最初と最後の出来事の間の 4 時点で生きている数が増え続ければ growing）。
- 記録中なら stream "heap"（alloc / free / scan）にも入る（時間軸・Perfetto で見える）。
- 当たるたびにコアを数 ms 止める。確保の多いファームには、トレースフック（traceMALLOC / traceFREE を ITM へ）の方法が
  要る（未実装）。

## 3. 試験

- 偽のメモリ・偽の CPU で: 縮図の分解の網羅、長期の記録と普通の記録の読みの一致（件数・波形・VCD）、間引き・容量、
  分割をまたぐ SQL、流し続け（偽のストリームのデバイス）、各アロケータの走査と壊れ、freertos_demo の ELF の
  シンボルでの heap_4（U08 の要約と一致）、ブレークポイントの記録（戻り先の一時のブレークポイントを外す）。
- 実機の手順: gateware/tests/hil/test_longrec_hw.py、test_heap_hw.py（本流が走らせる）。
