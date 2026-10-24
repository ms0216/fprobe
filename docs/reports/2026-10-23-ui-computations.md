# 画面の中の計算（2026-10-23）

CLAUDE.md の設計原則「画面（UI）は考えない層にする」に照らして、host/ui/src（.ts と .svelte の 133 ファイル）の中で、
API の応答をそのまま描かずに、画面が計算・判定して人に見せているものを洗い出した。見せ方（16 進・単位・時刻の書き方・
拡大縮小・目盛り・色・描画の間引き・利用者が選んだ列での並べ替え）は対象外（約 120 件）。

調べ方: 全ファイルを計算の書き方（sort・reduce・Math.max/min・filter・比較・$derived・色分け）で検索し、計算のある約 60
ファイルの本文を読んだ（lib の計算の .ts は全部）。残りは検索だけで、計算は見つからなかった。各項目は、ホストへ移すときに
1 件ずつ確かめ直す（b8 は API の説明文からの判断で、実装は見ていない）。

ファイルは host/ui/src からの相対。

## (c) 判定・経験則・推定・並べ方の基準を含むもの: 32 件

| # | 場所 | 画面がしていること | 同じ結果の API |
|---|---|---|---|
| c1 | lib/power.ts:116-134 | 電圧の低下: 指定の電圧以下の区間をまとめ、最低点・深さ・戻った時刻 | なし |
| c2 | lib/power.ts:139-141 | live かつ over_range でないときだけ電圧を出す | なし |
| c3 | lib/callgraph.ts:43-49 | 関数の根の並び（reset → exception → user → …）、reset の最深経路を最初から開く | なし |
| c4 | lib/interrupts.ts:57-74 | 割り込みの負荷 = 自分の時間 ÷ rtos の記録の長さ（分母の選び方）、多い順 | 記録の区間は なし |
| c5 | lib/interrupts.ts:103-121 | 分布図の棒の数（24）・幅・長い順上位 10 回の印 | なし |
| c6 | lib/fault.ts:43-45 | 故障か: faulted か、例外名が HardFault 等の集合に入る | なし |
| c7 | lib/fault.ts:55-69 | 止まるたびに target.fault を読み、故障なら Fault の窓を自動で開く | なし |
| c8 | lib/fault.ts:89-94 | 見出しの主な原因（MMARVALID・BFARVALID・FORCED を除いたもの） | なし |
| c9 | lib/safety.ts:41-43 | 止める前の警告: halt_risk.matched かつモニタ方式でない | なし |
| c10 | lib/safety.ts:68-70 | tick_stopped を致命的とみなさない | なし |
| c11 | lib/flash.ts:126-131 | 書き込みの進み: 消去・書き込み・検証を等分して 1 本に | なし |
| c12 | lib/flash.ts:134-138 | 残り時間の見積もり | なし |
| c13 | lib/fpgaobs.ts:122-128 | 観測の状態の文言の優先順（stuck > overflow > running > …） | なし |
| c14 | lib/fpgaobs.ts:146-149 | 型ごとの接続の既定の値（stream: 10 MHz/64 語/20 等） | なし |
| c15 | lib/sampler.ts:34-41 | サンプラを使い始めた者の推定（操作の記録の最後の開始） | なし |
| c16 | lib/activity.ts:13-15 | client 名が "hook " で始まれば役割 hook | なし |
| c17 | lib/rtos.ts:107-117 | 記録のタスク表: 使用率の分母（run の区間の合計）・平均・使用率順 | なし |
| c18 | lib/rtos.ts:209-216 | グラフに出すタスク: アイドルを除き上位 6、「その他」の丸め | なし |
| c19 | lib/system.ts:35-46 | メモリのカウンタの毎秒の増え方（32 ビットの一周は前進とみなす） | なし |
| c20 | lib/system.ts:108-126 | FPGA の条件の成立の回数と毎秒（直近 2 秒・最後の 5000 件） | なし |
| c21 | lib/system.ts:130-135, timeline/rows.ts:187-195 | ずっと同じ値のピンを隠す・段の並びと畳み | なし |
| c22 | lib/system.ts:211-215 | 動いている watch が自分の始めたものかの推定 | なし |
| c23 | lib/system.ts:235-237 | 記録のメモリの読みの周期: ピン併用かつ Ethernet なら 200 Hz、それ以外 1000 Hz（実機の経験の値） | なし |
| c24 | SystemPanel.svelte:53-54 | ピンの記録が止まったの判定 | なし |
| c25 | SystemPanel.svelte:403-404 | MCU OK / FPGA OK の丸 | なし |
| c26 | lib/eventlog.ts:177-191 | 印を置いた者の推定（同じ本文で時刻がいちばん近い操作） | なし |
| c27 | lib/logic.ts:193-199 | 今のトリガの設定 = 操作の記録で最後に成功した設定 | 一部 xtrig.status |
| c28 | lib/state.ts:74-76, StatusLine.svelte:23 | 「AI 作業中」= AI が接続しているだけで作業中 | なし |
| c29 | lib/target.ts:26-36 | コアのクロックの既定: SystemCoreClock が 1 MHz〜2 GHz なら採用 | なし |
| c30 | RtosObjectsPanel.svelte:54-60 | RTOS のオブジェクトの種類分けと件数 | なし |
| c31 | lib/memory.ts:10-34 | 番地の欄の解釈の順（数字だけなら 16 進 → 式 → 記号） | なし |
| c32 | lib/dashboard.ts:264-266 | ランプの真偽 = 0 以外を真 | なし |

## (b) 決まった計算: 41 件

| # | 場所 | 画面がしていること | 同じ結果の API |
|---|---|---|---|
| b1 | lib/power.ts:65-78 | 電圧の窓の最小・最大・平均・回数 | なし |
| b2 | lib/power.ts:100-104 | 記録の電圧の最低点 | recording.query で可 |
| b3 | lib/callgraph.ts:52-69 | 木の行の再帰・最深経路の印 | なし |
| b4 | CallGraphPanel.svelte:48-55 | 呼び出しの木にプロファイルの割合を結合 | なし |
| b5 | lib/interrupts.ts:37-54 | 入れ子の割り込みの自分の時間 | なし |
| b6 | lib/interrupts.ts:124-128 | 棒の中の最長の回・時刻 t の割り込み | なし |
| b7 | lib/profile.ts:58-81 | ソースの余白の当たり・当たらなかった行 | なし |
| b8 | lib/fault.ts:72-78 | 原因のビット → レジスタ | 未確認 |
| b9 | lib/flash.ts:105-115 | 段の表・合計バイト | なし |
| b10 | lib/fpgaobs.ts:54-87 | 信号の定義の検査 | **fpga_obs.signals_set** |
| b11 | lib/fpgaobs.ts:90-94 | 実際の TCK | なし |
| b12 | lib/activity.ts:48-51 | 時刻 → 直前の操作 | recording.query で可 |
| b13 | lib/rtos.ts:163-201 | 記録の窓ごとの負荷・割合 | 記録には なし |
| b14 | lib/rtos.ts:222-234 | 「その他」の負荷 | なし |
| b15 | lib/rtos.ts:239-253 | 時刻に動いていたタスク・次の実行 | recording.find で可 |
| b16 | lib/rtos.ts:58-64 | 時間軸の表を使用率順に | **rtos.timeline.get** |
| b17 | SystemPanel.svelte:166-170 | FPGA の scans の毎秒 | なし |
| b18 | SystemPanel.svelte:174-175 | 記録中にメモリの読みが止まった | **recording.status** |
| b19 | SystemPanel.svelte:398 | 遅れの分解能 | **recording.correlate** |
| b20 | lib/eventlog.ts:214-223 | stream・kind ごとの割合・毎秒 | 一部 recording.query |
| b21 | lib/eventlog.ts:140-157 | 分布の棒・長い順の印 | **timing.measure** |
| b22 | lib/sampling.ts:131-196 | 観測値の最小・最大・平均・件数 | なし |
| b23 | lib/sampling.ts:247-261 | XY 散布の組み立て | なし |
| b24 | lib/sampling.ts:304-321 | 取りこぼし率・実際の周期 | なし |
| b25 | lib/sampling.ts:397-409 | カーソルの間の値・差・統計 | なし |
| b26 | lib/watchshare.ts:38-40 等 | 自分の式が外された・他者の式 | なし |
| b27 | lib/logic.ts:75-79 | 解読で隠れた種類と件数 | なし |
| b28 | lib/logic.ts:154-159 | 多段トリガの段の状態 | **xtrig.sequence_status** |
| b29 | lib/logic.ts:209-212 | 変化のあるチャンネルだけ | **la.measure** |
| b30 | lib/terminal.ts:112-134, 311-320 | RTT の端末ごとの分割・時刻 t の行 | 一部 terminal.read |
| b31 | lib/swotrace.ts:49-51 | 例外の回数を多い順に | なし |
| b32 | lib/breakpoints.ts:179-182 | 未対応のベクタキャッチを覚えて足す | なし |
| b33 | lib/timeline/query.ts:85-97 | 全段の bad/warn を集めて縮図に印 | なし |
| b34 | lib/timeline/compose.ts:75-130 | 段ごとの範囲内の件数・変化数 | 近いもの recording.density |
| b35 | lib/timeline/overlay.ts:165 | 選択 − 基準の Δ時間 | なし |
| b36 | VarRow.svelte:41-42 | 前の停止から変わった変数の印 | なし |
| b37 | RegistersPanel.svelte:20 | 前の停止から変わったレジスタの印 | なし |
| b38 | ThreadsPanel.svelte:105 | スタックの使用率 | なし |
| b39 | HeapPanel.svelte:19,68-69 | ヒープの使用率 | なし |
| b40 | RtosObjectsPanel.svelte:72 | キューが満杯の表示 | なし |
| b41 | CpuLoadPanel.svelte:92,137-140 | 記録の負荷の最大の窓の印 | なし |
