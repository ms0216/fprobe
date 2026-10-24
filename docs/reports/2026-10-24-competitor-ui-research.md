# 競合・類似の道具の画面の調査: 右クリックのメニュー・既定の配置・トレースとデバッガの一体化（2026-10-24）

目的: 仕様 [2026-10-23-ui-rebuild-design.md](../superpowers/specs/2026-10-23-ui-rebuild-design.md) の §3.3（既定の配置）・
§3.5（右クリックのメニュー）を決めるための材料。前の調査 [2026-10-17-debug-ui-ux-survey.md](2026-10-17-debug-ui-ux-survey.md)
（時間軸の連携・カーソル・Go To Source 等）の続きで、今回は **右クリックの項目** と **既定の配置** を公式の資料から集めた。

調べ方: 公式のマニュアル（PDF は文字を取り出して該当の節を読んだ）・ヘルプ・サポートの記事。資料に無いもの・確かめられ
なかったものは「未確認」と書く。項目名は資料の英語のまま（並びも資料の順。資料が表で並べているものは表の順）。

## 第 1 部 道具ごとの事実

### 1. SEGGER Ozone（v3.50）

出典: Ozone User Guide & Reference Manual UM08025（Software Version 3.50）https://www.segger.com/downloads/jlink/UM08025
（節番号はこのマニュアルのもの）。

**右クリックの作り（§3.6.1）**: どの窓も固有のメニューを持つ。オン・オフの項目の状態はセッションをまたいで残る。
区切り線の位置は図にしか無く、本文からは読めない（下の並びはマニュアルの説明の順）。

| 窓 | 項目（マニュアルの順） |
|---|---|
| Source Viewer（§4.20.13。行の上でも識別子の上でも同じメニュー。識別子・ポインタ型かで項目が増減） | Set / Clear / Edit Breakpoint・**Break On Change**（カーソルの下の変数にデータブレーク）・Set Tracepoint (Start/Stop)・Set Next Statement・Run To Cursor・Show Value in Source / in Disassembly / in Data（ポインタ型だけ）・Show Definition・Show Declaration・Show Disassembly・Show Data・Show Call Graph・Show in Memory Map・Watch・Quick Watch・**Graph**（Data Sampling＝時間軸へ）・Goto PC・Goto Line・Find・**Find In Trace**・Expand / Collapse All・Cut/Copy/Paste・Line Numbers・Execution Counters・Instruction Encodings・Pseudo Instructions・Export |
| Disassembly（§4.7.4） | Quick Watch・Set/Clear/Edit Breakpoint・Set Tracepoint・Set Next PC・Run To Cursor・Show Definition・Show Declaration・Show Source・Show Data・**Show Target Address**（分岐先へ）・Goto PC・Go Back・Go Forward・Go To・Set/Edit/Reload Plugin・Source Lines・Labels・Execution Counters・Instruction Encodings・Pseudo Instructions・Find In Trace・Export |
| Watched Data（§4.23.5） | Remove・Set/Clear/Edit Data Breakpoint・Graph・Show Value in Source/Disassembly/Data・Show Source・Show Data・Display (All) As・**Refresh Rate**（式ごとの読み直しの周期）・Add・Remove All・Expand/Collapse All・Member Functions・Tool Bar・Filter Bar・Export |
| Local Data（§4.12.4）/ Global Data（§4.10.3） | Set/Clear/Edit Data Breakpoint・Watch・Quick Watch・Graph・Show Value in …・Show Source・Show Data・（Global は Show In Memory Map）・Display (All) As・Expand / Collapse All・Member Functions・（Local は Auto Mode）・Filter Bar・Export |
| Memory（§4.13.10） | Copy・Copy Special（サブメニュー: Copy Text / Copy Hex / Copy Hex As C-Initializer / Copy Binary）・Show Disassembly・Show Data・Display Mode・Access Mode・Fill・Save・Load・Go Back・Go To・（周期更新）・Toolbar |
| Registers（§4.17.6） | Show Source・Show Disassembly・Show Data（レジスタの値を番地として開く）・Display (All) As・Refresh Rate・Expand / Collapse All・Add SVD File・Display Registers・Toolbar / Filter bar・Export |
| Breakpoints/Tracepoints（§4.1.5） | Clear・Enable / Disable・Edit・Show Source・Show Disassembly・Set Breakpoint...・Set Data Breakpoint...・Set Tracepoint...・Enable All・Disable All・Clear All・Filter Bar・Vector Catches・Export |
| Call Stack（§4.3.7） | Set/Clear Breakpoint・Show Source・Show Disassembly・**Show Stack Frame**（フレームの底をメモリの窓で）・Parameter Names / Values / Types・Current Frame On Top・Filter Bar・Export |
| Timeline（§4.22.11。区画ごとにメニューが違う） | 共通: Fit Width・Fit Height・Go To Cursor・Go To Reference・Go To Time・Cursor（カーソルを窓の定位置に留める）・Time Scale・Toggle Reference・Clear All References・Open Data Window・Reset Data・Auto Fit Height・Data / Power / Code（区画の表示）・Legend・Toolbar。Data と Power の区画: Sampling Frequency・**Set Offset To Code**（区画の時刻のずれを利用者が合わせる）・Draw Points・Uniform Sample Spacing。Code の区画: Set Breakpoint・Go To Start/End of（その実行の始め・終わり）・**Go To Next/Previous execution of**（同じ関数の次・前の実行）・Go To Next/Previous function on level・Time Stamps |
| Instruction Trace（§4.11.9） | Copy・Set / Clear Breakpoint・Set Tracepoint・Show Source・Show Disassembly・Toggle Reference・Clear All References・Go To Reference・First / Last in Function・First / Last in Trace Block・Expand/Collapse All・Instruction Encodings・Timestamps・Reset Data・Find In Trace・Export |
| Code Profile（§4.4.7） | Set/Clear Breakpoint・Show Source・Show Disassembly・Include/Exclude from（load・coverage）・Exclude All Trailing NOPs・Exclude (Dialog)・Include (Dialog)・Remove All Filters・Show Filters・Reset Execution Counters・Reset Data・Execution Counters in Source / in Disassembly・Group by Files・Sort Respects Filters・Parent Relative Load・Filter Bar・Export |
| Data Sampling（§4.6.5.2 / 4.6.6.1） | Setup: Remove・Display (All) As・Sampling Frequency・Add Expression・Remove All・Reset Data・Toolbar・Filter Bar・Export。Samples: Sampling Frequency・**Goto Time**・Export・Toolbar |
| RTOS（§4.18.4） | Refresh・Reload Plugin・Edit Plugin・Export・Views（表示する表を選ぶ）。タスクの行ごとの操作は書かれていない |

**型の決まり（読み取れる工夫）**: 変数を出す窓（Watched・Local・Global・Data Sampling）は「データブレーク → Watch →
Quick Watch → Graph → Show Value in … → Show Source / Data → 表示形式」と同じ並び。末尾は表示の設定（Filter Bar）と
Export。つまり **値への操作 → 移動（Show …）→ 窓の見せ方** の 3 段で、窓が違っても同じ値には同じ並びが出る。
「Show X」（Show Source / Disassembly / Data / Call Graph / in Memory Map）が窓どうしの移動を全部受け持つ。

**既定の配置・窓**: 初めて起動すると「default user interface layout」と Project Wizard が出る（§2.2。既定にどの窓が
あるかは図だけで本文に無い）。中央は Source Viewer 固定で、左・右・下の 3 つの領域に窓を置く（§3.2）。窓はタイトルを
ドラッグかダブルクリックで外せ、デスクトップに自由に置ける（§3.9.2）。重ねるとタブ（§3.9.3）。窓は View メニューか
コマンド `Window.Show("…")` で開き、前に置いた位置に戻る（§3.9.1）。配置はプロジェクトの隣の `*.user` に保存され次に
戻る（§5.1.7）。名前付きの配置の切り替えは見つからなかった。

**時刻の連動**: Timeline の Sample Cursor は「PC を持つ窓」（Source・Disassembly・Instruction Trace）の実行点と双方向に
同期する（§4.22.6）。Data Sampling・Power Sampling の表の選択行も Sample Cursor と同期（§4.6）。Instruction Trace の
選択は Source と Disassembly で強調され、設定 Sync With Code で切れる（§3.11.15.10）。Timeline を消すと Instruction
Trace・Code Profile・Data Sampling・Power Sampling も同時に消える（§4.22.13）。
Code Profile と Sample Cursor の直接の同期は記述が見つからなかった。

**解析の流れの例（§5.19）**: Code Profile を負荷で並べる → アイドルを Exclude → 関数をダブルクリックでソースへ →
Execution Counters で通らなかった命令を探す。

### 2. SEGGER SystemView（v4.10）

出典: SystemView User Guide UM08027（Software Version 4.10）https://www.segger.com/downloads/systemview/UM08027_SystemView.pdf

- **右クリック**: Timeline・Events List・Contexts の項目の一覧はマニュアルに無い（未確認）。書かれているのは Events list
  の「Right-Click → Toggle Reference」（§2.3）、Contexts・Terminal の Export...、Data Plot の Configure と表示の種類だけ。
- 操作の主役はキー（§3.13 GUI controls）: **F / B = 次 / 前のコンテキストの切り替え**、**N / P = 次 / 前の同じ種類の
  出来事**、Shift+N / P = 同じコンテキストの中で、Ctrl+G = Go to Event、Ctrl+Shift+G = Go to Timestamp、R = Toggle
  Reference、0〜9 = 印を窓の 0〜100% に置く。出来事の種類ごとの表示切り替え（Shift+A/I/M/S/T/E/D）と「その種類だけ」
  （Ctrl+Shift+同じ字）、**Hide This Event**（選んだ API の呼び出しを隠す）、Reset all Filters。
- **連動**: 「All windows are synchronized」（§3.1）。Timeline の印（青い線）と Events list の選択が一致し、印を窓の
  10〜90% に固定してスクロールできる（§3.2）。CPU Load と Data Plot は Timeline の範囲に従う。Terminal の行を選ぶと
  Events list と Timeline も選ばれる（§2.4）。マルチコアはコアごとの Events list が同期。
- **既定**: 初めて起動すると見本の記録（OS_IP_WebServer.SVdat）を開き、System Information を出す（§2.1）。窓の既定の
  配置・浮かせる窓・マルチモニタの記述は見つからなかった。
- **解析の流れ（§2.2〜2.4）**: System Information で全体 → Timeline を拡大し F で次の実行へ → Events list で Ctrl+G、R で
  基準 → F で次の活動、基準からの差で原因を絞る → Terminal の行から、その時の出来事へ。

### 3. J-Link の周辺の道具

- **J-Scope**（UM08028 v5.10 §1.8）: 右クリックは変数の行だけ: Add Symbol / Remove Symbol / Show / Hide toggle / Zoom in・out
  (Y-Axis) / Change draw style / (Hexa-)Decimal toggle / Change color。
- J-Link Commander・RTT Viewer・J-Mem の右クリックは今回調べていない。

### 4. Lauterbach TRACE32 PowerView

出典: PowerView User's Guide（Release 02.2026）https://www2.lauterbach.com/pdf/ide_user.pdf 、Training Menu Programming
https://www2.lauterbach.com/pdf/training_menu.pdf 、Debugger Tutorial https://www2.lauterbach.com/pdf/debugger_tutorial.pdf 、
Training Basic Debugging https://www2.lauterbach.com/pdf/training_debugger.pdf

- **右クリックは「値の種類」ごとの共通メニュー**（ide_user「Local Popup Menus」p.66）: 組み込みの local popup menu に
  **Program Address** と **Variable** がある。利用者が足した項目は、この 2 つに足せば **それを持つすべての窓**
  （List.auto・List.Mix・List.Asm・Data.dump・Var.Watch 等）に出る。窓のコマンド（例 List.auto の短縮形 L.）に足せば
  その窓だけに出る。→ fprobe の §3.5「値の種類ごとに、その値を取る API を並べる」と同じ考え方の先行例。
- **メニューは全部利用者が書き換えられる**: 既定のメニューは t32.men。MENU.AddMenu（1 行で足す）・MENU.AddTool（ツール
  バーにボタン）・MENU.ReProgram（.men ファイル）・MENU.RESet（既定に戻す）。個人の追加は work-settings.cmm、チームの
  追加は system-settings.cmm に置くよう勧めている（training_menu p.3〜4）。
- **窓の見出し = その窓を開いたコマンド**: 見出しを右クリックすると、そのコマンドがコマンドラインに入り、直して
  再実行・スクリプトへ写せる（ide_user p.37「Modifying and Re-using Commands Shown In Window Captions」）。
  → 画面の操作と API の一対一（fprobe の原則 2）を、窓ごとに見せる工夫。
- **右クリックの例**: List の窓で配列名を右クリック → Breakpoints > Write（書き込みのブレークポイント。配列全体の範囲に）
  （debugger_tutorial p.19）。Var.View で変数名を右クリック → Format（debugger_tutorial p.25）。
- **窓の扱い**: MDI（主窓の中）と MWI（デスクトップに自由に。Windows では FDI = タスクバーに 1 つ / MTI = 窓ごと）を
  config.t32 で選ぶ（ide_user p.24, 33）。**Window Pages**（WinPAGE.*）= 作業場の切り替え（他の道具のパースペクティブに
  相当）。ツールバーを右クリックしてページを作る・切り替える。WinResist で全ページに出す窓（目次の WinPAGE.List 等）
  （ide_user p.34）。
- **トレースとソースの連動**（Trace Tutorial https://www2.lauterbach.com/pdf/trace_tutorial.pdf ）: Trace.List の行を
  ダブルクリックするとその行のソース（List の窓）を開く。メニュー Trace > List > Tracing with Source で Trace.List と
  **List /Track** を同時に開き、Trace.List で 1 回クリックすると List が対応する行を示す。
- **/Track と /ZoomTrack**: トレースの窓のコマンドに /Track を付けると、他のトレースの窓（Trace.Chart.Func 等）の
  カーソルに追従する。既定は時刻で追従し、時刻が無ければ記録の番号で。グラフの窓に /ZoomTrack を付けると拡大率も
  そろう。付け方は「窓の見出しを右クリック → コマンドに /Track を足して Enter」。つまり **連動は窓ごとの選択（既定で
  全部ではない）**。
- **統計の行の右クリック**（同 Tutorial）: Trace.STATistic.Func の関数を右クリック → **Parents**（呼び出し元ごとの時間）・
  **Children**（呼んだ先ごと）・**Duration Analysis**（入口から出口までの時間の分布）・**Distance Analysis**（続く 2 回の
  呼び出しの間隔の分布）。
- **変数の右クリック → Display Trace**（同 Tutorial）: Var.View の変数名を右クリック → Display Trace → **List Variable**
  （その変数への全アクセスと値と時刻。書きは黒・読みは灰色）/ **State Chart** / **Value Chart** / **Draw**（値を時間に
  対して描く）。→ 「変数 → その変数の時間の上の記録へ」という移動を右クリックの 1 段で出している。
- **トレースの行の右クリック → Set CTS**（Training Arm CoreSight ETM Tracing p.78 https://www2.lauterbach.com/pdf/training_arm_etm.pdf ）:
  選んだ記録の時点の状態を PowerView の中で作り直し（Context Tracking System）、レジスタ・変数・ソースの窓がその時点を
  示す（ターゲットには影響しない。状態行にその記録の番号が出る）。前後へのステップも可（Forward and Backward Debugging）。

- **その他の右クリック**（本文にある範囲。組み込みのメニューの全項目は t32.men にあり、マニュアルでは画像だけ。並びは
  文書に無い）: Data.dump で右ボタンを押し続けると **Data Address** のメニュー（general_ref_d.pdf）。List の行に Modify
  here / Assemble here・Toggle Bookmark（general_ref_d / general_ref_b）。トレースのポップアップに **Set Ref / Set Zero**
  （general_ref_t.pdf p.237）・Toggle Bookmark。レジスタのポップアップに Set ...（general_ref_r.pdf）。TASK の表示の
  「magic」欄はダブルクリックで関係する窓、右クリックで local menu（rtos_freertos.pdf / rtos_zephyr.pdf。項目名は未確認）。
  Break.List の右クリックの項目は未確認。PDF は https://www2.lauterbach.com/pdf/ の下。
- **メニューの足し方の細部**（ide_ref.pdf「MENU」）: t32.men を書き換えず ADD で足す。`MENU "L."` のようにコマンドの短縮形
  でその窓だけ、特別な名前 **"DATA"** で Data の窓すべて、**"VAR"** で Var の窓すべてに足す。
- **配置の保存**: 終了時に配置は保存されない。Window > **Store Windows to ...** で配置を再現するスクリプト（WinPAGE.RESet /
  WinPOS / 各コマンド）を作り、Load Windows... か DO で戻す（training_debugger p.30〜31）。コマンドは STOre / ClipSTOre。
  → **配置 = 窓を開くコマンドの列**（窓の見出しがコマンドなので、配置もコマンドで書ける）。MDI でも WinExt.<cmd> で窓を
  外に出せる。マルチモニタの明記は見つからなかった。
- **CTS**（general_ref_c.pdf p.172〜173）: CTS.GOTO <record> で開始点を選ぶと、画面のレジスタ・ソース・変数がその時点に
  なり、状態行に黄色の CTS の表示。その状態で Step.single / Step.Back / Go.Return が使える。CTS.OFF で戻る。

### 5. Arm Development Studio（User Guide 2021.0）

出典: Arm Development Studio User Guide 2021.0（101470_2021.0）https://documentation-service.arm.com/static/605b6fd183dc122cd4857761
（現行版の Web の資料は本文を取得できなかった。現行版と違う可能性がある）。第 16 章はツールバーと右クリックの項目を
まとめて書いており、どちらに出るかの区別は無い。

| ビュー | 項目 |
|---|---|
| Variables（§16.30） | Linked:<connection>・Copy・Select All・**Show in Memory**・**Show in Disassembly**・**Show in Registers**・**Show Dereference in Memory**・Show Dereference in Disassembly・Translate Variable Address・Toggle / Enable / Disable / Resolve Watchpoint・Watchpoint Properties・**Send to <selection>**（Expressions へ）・形式（Binary / Boolean / Hexadecimal / Octal / Signed Decimal / Unsigned decimal） |
| Expressions（§16.12） | Copy・Paste・Delete・Select All・Send to・Show in Registers / Memory・Show Dereference in Memory・Show in Disassembly・Show Dereference in Disassembly・Translate Variable Address・Watchpoint の各項目 |
| C/C++ エディタの行（接続中） | **Set PC to Selection**・**Run to Selection**・Show in Disassembly・Inspect |
| エディタの左の帯 | Toggle Breakpoint・Toggle Hardware Breakpoint・Resolve / Enable / Disable Breakpoint・**Toggle Trace Start Point / Stop Point / Trigger Point**・Breakpoints Properties... |
| Disassembly（§16.9） | Copy・Paste・Select All・Run to Selection・Set PC to Selection・**Show in Source**・Show in Registers・Show in Functions・Translate Address・Toggle Watchpoint・Toggle Breakpoint・Toggle Hardware Breakpoint・Toggle Trace Start / Stop / Trigger Point |
| Memory（§16.16） | Cut・Copy・Paste・Delete・Select All・（セルを選ぶと）Toggle Watchpoint・Translate Address。View Menu に Import / Export / Fill Memory 等 |
| Registers（§16.19） | Linked・Copy・Select All・**Show Memory Pointed to By <reg>**・**Show Disassembly Pointed to By <reg>**・Translate Address in <reg>・Send to・形式 |
| Breakpoints（§16.4） | Delete・Delete All・Go to File・Skip All Breakpoints・Show in Disassembly・Show in Memory・Resolve・Enable / Disable Breakpoints・Copy・Paste・Select all・Properties... |
| Debug Control（スレッド・コア。§16.7） | Collapse All・Display Cores / Threads・Connect / Disconnect・Continue・Interrupt・Step 各種・Show in Stack・**Reset Development Studio Views to 'Linked'**・View CPU Caches 等 |
| Stack（§16.8） | Show in Disassembly・Show in Memory・Step Out to This Frame |
| Trace（§16.28） | Linked・Show Next Match・Show Previous Match・Mark other occurrences・Clear Trace・命令 / 関数のトレースの切り替え・Export Trace Report・Home / Page Back / Page Forward / End・**Focus Here**・**Order By Total Time**。View Menu に Find Trace Trigger Event / Find Timestamp... / Find Function... / Find Instruction by Address... 等 |

- **既定の配置**: デバッグには Development Studio perspective が要り、接続時に「Confirm Perspective Switch」が出る。Window >
  Perspective で切り替え、Window > Show View で足す（Trace view は既定で出ない）。タブのドラッグで移し、ダブルクリックで最大化
  （§6.9・§9.1・§16.28）。
- **連動の単位 = 接続（Linked:）**: 各ビューは既定で Debug Control で選んだ接続に連動し、別の接続・コアに固定もできる
  （Trace view を複数開いてコアごとに）。違う Trace view の番号が同じ時刻を指すとは限らない（§5.3）。Stack でフレームを
  選ぶと全ビューがそのフレームに従う（§9.2・§10.8）。Trace のチャートの時刻をクリックすると Disassembly が同期（§16.28）。
  Trace の行のダブルクリックでソースへ、の記述は見つからなかった。
- **Snapshot Viewer**（第 13 章）: レジスタ・メモリ・シンボル（とトレース）から、止められない状況の状態を読み取り専用で
  調べる。

### 6. Green Hills MULTI

公式のマニュアル（MULTI: Debugging）は公開されていない。右クリックの項目は見つからなかった。

- **TimeMachine**: 実行の記録から、History の窓で見つけた問題の箇所まで、デバッガが遡って run / step する
  （https://ghs.com/japan/MULTI_debug_j.html 、https://www.ghs.com/products/timemachine.html ）。「CodeReplay でステップすると
  PathAnalyzer も同じ時点に」・前後にステップして変数とレジスタを見る（2003 年の発表 https://www.ghs.com/news/20030902_time_machine.html ）。
- **PathAnalyzer**: 時間に沿った呼び出し履歴。全体から個々の呼び出しまで拡大（大学の演習資料
  https://www.rose-hulman.edu/class/csse/debugging/week5/day3 ）。**History viewer**: マルチコアの過去数秒〜数日の実行、µs〜日の
  拡大、中をクリックしてデバッガを呼ぶ（https://www.ghs.com/products/MULTI_IDE.html ）。
- 変数の右クリックで「Browse References」（TI の講習資料 2007 https://www.ti.com/lit/ml/sprp581/sprp581.pdf ）。既定の配置は
  公式の資料で未確認。

### 7. IDE のデバッガ

#### 7.1 IAR Embedded Workbench for Arm 9.70.x（C-SPY）

出典は docs.iar.com の各窓の reference のページ（例 https://docs.iar.com/ewarm/9.7x/en/ide-project-management-and-building/editing/reference-information-on-the-editor/editor-window.html 、
同じ所の c-spy-debugging/…/disassembly-window.html・watch-window.html・memory-window.html・registers-window.html・call-stack-window.html・
breakpoints-window.html・timeline-window-call-stack-graph.html・trace-window.html）。

| 窓 | 項目（文書の順） |
|---|---|
| エディタ | 編集の項目（Cut・Copy・Paste・補完 等）・Go to Definition / Declaration of *symbol*・Find All References / Calls to *symbol*・**Find in Trace**・Toggle Breakpoint (Code) / (Log) / (Trace Start) / (Trace Stop)・Enable/disable Breakpoint・**Set Data Breakpoint for '*variable*'**・**Set Data Log Breakpoint for '*variable*'**・Edit Breakpoint。デバッグ中だけ: Set Next Statement・**Add to Quick Watch / Add to Watch / Add to Live Watch: *symbol***・Move to PC・Run to Cursor |
| Disassembly | Move to PC・Run to Cursor・Code Coverage ▸（Enable / Show / Clear / Next / Previous Different Coverage）・Instruction Profiling ▸・Toggle Breakpoint (Code / Log / Trace Start / Trace Stop)・Enable/Disable・Edit Breakpoint・Set Next Statement・Copy Window Contents・Mixed-Mode・Find in Trace・Zone |
| Watch / Live Watch（Locals は Remove 系なし） | Remove・Remove All・Default / Binary / Octal / Decimal / Hexadecimal / Char Format・Show As ▸・Save to File・Right Align 'Value' Column・Options |
| Memory | Copy・Paste・Zone・1x/2x/4x/8x Units・Little / Big Endian・Data Coverage ▸・Find・Replace・Memory Fill / Save / Restore・**Set Data Breakpoint**・**Set Data Log Breakpoint** |
| Registers | View Group・View User Group・Format・Open User Groups Setup Window・Save to File・Find Next / Previous Register |
| Call Stack | Go to Source・Show Arguments・Run to Cursor・Copy Window Contents・Toggle Breakpoint 各種・Enable/Disable Breakpoint。行のダブルクリックでそのフレームへ |
| Breakpoints | Go to Source・Edit・Delete・Enable・Disable・Enable All・Disable All・Delete All・New Breakpoint ▸ |
| Timeline（Call Stack のグラフ） | 共通: **Navigate ▸**（Next / Previous / First / Last / End）・Auto Scroll・**Zoom ▸**（Zoom to Selection / In / Out / 10ns・100ns・1us… / 1ms… / 10m・1h…）。グラフの見出しの下: Enable・Show Timing・**Go To Source**・Save to File・**Navigate Call Stack ▸**（After/Before Current Loop・After/Before Current Function・Next/Previous Statement・Next/Previous on Same Address・Next/Previous Interrupt・Next/Previous Execution Start Point・Next/Previous Discontinuity）・Select Graphs・Time Axis Unit（Seconds / Cycles）・**Profile Selection**（選んだ区間を Function Profiler に渡す）。「Click in the graph to display the corresponding source code.」 |
| Timeline（Events / Data Log / State Log / Power Log） | 共通の Navigate・Auto Scroll・Zoom の後にグラフの見出しと Enable・Clear・*Variable*・Viewing Range・Size・Style（Bars / Columns / Levels / Linear）・Show Numerical Value・Signed・Hexadecimal・**Go To Source**・Select Graphs・Time Axis Unit・（State / Power Log は Open Setup Window・Profile Selection） |
| Trace | Enable・Clear・Embed source・Browse・Find ▸・Find All・Navigate ▸・**Bookmarks ▸**（Toggle / Goto Next / Goto Previous / Clear All）・**Timestamp ▸**（Set as Zero Point / Go to Zero Point / Clear Zero Point）・Save |
| Function Profiler（RX 版の文書） | Enable・Clear・Filtering ▸・Source ▸（Sampling / Trace (calls) / Trace (flat)）・Power Sampling・Save to File・Show Source |

- **Timeline の右クリックの作り**: 「時間軸の共通の操作（移動・拡大）」→「そのグラフの見出し（区切り）」→「そのグラフの
  操作」の 2 段。グラフが何段あっても上半分は同じ。
- **配置**: 題名の棒のダブルクリックで浮かせる・戻す、ドラッグでタブのグループへ（https://docs.iar.com/ewarm/10.1x/en/ide-project-management-and-building/the-development-environment/using-and-customizing-the-ide/organizing-windows-on-the-screen.html ）。
  編集用とデバッグ用の配置が別に保存されることは公式の文書では見つからず、Stack Overflow の回答だけ（settings/*.dbgdt）。
  マルチモニタの記述は見つからなかった。

#### 7.2 Keil MDK µVision

出典: µVision User's Guide（Arm 101407 https://developer.arm.com/documentation/101407/latest/ の Debugging > Debug Windows and
Dialogs）、旧版 https://www.keil.com/support/man/docs/uv4cl/ 。

- **エディタ（デバッグ中に増える）**: Show Disassembly at XXX・Set Program Counter・Run to Cursor Line・**Add 'XXX' to... ▸**
  （Watch / Memory / Analyzer へ）・Hexadecimal Display。→ 値を **どの窓に送るかをサブメニューで選ぶ**（Arm DS の Send to と同じ形）。
- Watch・Memory・Call Stack + Locals の右クリックは機能の説明だけで項目名が無い（式を消す・形式・ブレークポイント・他の窓へ
  足す・Memory の Access Size ▸（Default / Byte / Half-Word / Word）、Call Stack の caller / callee のコードへ）。
- **Logic Analyzer**（https://www.keil.com/support/man/docs/uv4cl/uv4cl_db_dbg_logicanalyzer.htm ）: ツールバーの **Jump to:
  Code**（その信号の変化を起こした行を Disassembly かソースで開く。PC が記録されているときだけ）/ **Trace**（トレースの記録が
  Logic Analyzer と同期）。Transition: Prev / Next。右クリックの項目で名前が分かるのは Adaptive Min/Max だけ。クリックで基準線、
  Shift で変化点に吸着。
- **System Analyzer**: 信号の右クリック Steps/States/Discrete・Hexadecimal Values・Setup Min/Max from Recording・Restore previous
  Min/Max・Remove Signal 'XYZ'・Save...。ツールバーに Go To Marker・Jump to Prev / Next Transition・Show Marker（緑）・Show Cursor
  （赤）・Snap on Transitions。カーソルの横に Delta・Avg・Q（マーカーとの差・区間の平均・区間の電荷）。ソースへ移る項目は
  見つからなかった。
- **Event Viewer**: Jump to: Code（スレッドの始まりの行へ）/ Trace（https://www.keil.com/support/man/docs/uv4cl/uv4cl_db_dbg_event_viewer.htm ）。
- **配置**: 「The screen layout of the last debugging session is restored.」「The window layout is saved for each project
  automatically」。Window > **Reset View to Defaults**（https://www.keil.com/support/man/docs/uv4cl/uv4cl_ui_environment.htm ）。
  窓は「floated to another screen」できる（https://www.keil.com/support/man/docs/uv4cl/uv4cl_ui_docking_windows.htm ）。
  → 編集とデバッグで画面が切り替わり、デバッグの配置は前回のものが戻る。

#### 7.3 Eclipse CDT・STM32CubeIDE

- **Debug パースペクティブの既定のビュー**: Debug・Variables・Breakpoints・Outline・Console・Tasks・Problems・Executables
  （C/C++ パースペクティブは Project Explorer・Console・Properties・Problems・Make Targets・Outline・Tasks）
  （https://help.eclipse.org/latest/topic/org.eclipse.cdt.doc.user/concepts/cdt_c_perspectives.htm ）。
- **Debug ビュー（スレッド・スタック）**: Copy Stack・Find...・Drop To Frame・Step Into / Over / Return・Instruction Stepping
  Mode・Use Step Filters・Resume Without Signal・Resume・Suspend・Terminate 各種・Disconnect・Properties
  （https://help.eclipse.org/latest/topic/org.eclipse.cdt.doc.user/reference/cdt_u_dbg_view.htm ）。
- **Registers ビュー**（文書は ABC 順）: Add Register Group・Assign Value・Cast To Type...・Change Value...・Copy Registers・
  **Create Watch Expression**・Display As Array...・Format・Restore Original Type 等（https://help.eclipse.org/latest/topic/org.eclipse.cdt.doc.user/reference/cdt_u_registersview.htm ）。
- **Memory ビュー**: Add / Remove Memory Monitor・Reset・Add / Remove Rendering・Endian・Cell Size・Radix・**Go To Address**・
  Reset To Base Address・Refresh（https://help.eclipse.org/latest/topic/org.eclipse.cdt.doc.user/reference/cdt_u_memoryview.htm ）。
- **Disassembly ビュー**: Run To Line・Resume At Line を「含む」と書くだけ（https://help.eclipse.org/latest/topic/org.eclipse.cdt.doc.user/reference/cdt_u_dissassembly_view.htm ）。
- **Variables・エディタの項目**: CDT の公式文書に一覧が無い。ソースの文字列（並びは不明）: Cast To Type...・Display As Array...・
  Restore Original Type・Format・**View Memory**・Add Watchpoint (C/C++)...・Toggle Watchpoint・Add Global Variables...・Add Watch
  Expression...・Run to Line・**Move to Line**・**Resume at Line**（https://github.com/eclipse-cdt/cdt/blob/main/debug/org.eclipse.cdt.debug.ui/plugin.properties ）。
  Run to Line = 一時ブレークポイントで続ける、Resume at Line = PC を移して続ける、Move to Line = PC を移すだけ（2010 年の
  cdt-dev https://eclipse.org/lists/cdt-dev/msg20109.html ）。
- **浮かせる**: 題名の棒を Workbench の外へドラッグで Detached の窓（https://help.eclipse.org/latest/topic/org.eclipse.platform.doc.user/tasks/tasks-9l.htm ）。
- **STM32CubeIDE**: UM2609 の PDF を取得できず、Live Expressions・SWV のビューの右クリックと既定の配置は未確認。

#### 7.4 VS Code（標準のデバッグの画面）と Cortex-Debug

出典: https://code.visualstudio.com/docs/debugtest/debugging 、項目名は microsoft/vscode のソース
（src/vs/workbench/contrib/debug/browser/debug.contribution.ts・debugCommands.ts・breakpointEditorContribution.ts）で確認。

| 場所 | 項目（グループ順） |
|---|---|
| Variables | **Set Value**｜Copy Value・Copy as Expression｜Add to Watch・**Break on Value Read / Change / Access**（デバッガが対応するときだけ）。行のボタン **View Binary Data**（メモリを開く） |
| Watch | Add Expression・Edit Expression・Set Value・Copy Value・Copy as Expression・Remove All Expressions・Break on Value 各種。行のボタン View Binary Data・Remove Expression |
| Call Stack | セッション: Restart・Disconnect・Stop。スレッド: Pause / Continue・Step Over / Into / Out・Terminate Thread。フレーム: Restart Frame・Copy Call Stack |
| 行番号の余白 | Add Breakpoint・**Add Conditional Breakpoint...**・**Add Logpoint...**・Add Triggered Breakpoint...・Run to Line。置いてある所では Remove / Edit / Disable・Enable |
| エディタ | Run to Cursor・Jump to Cursor（1.47 で別名 Set Next Statement。https://github.com/microsoft/vscode-docs/blob/vnext/release-notes/v1_47.md ）・Add to Watch・Evaluate in Debug Console |

- VS Code のメニューは **グループ（区切り）で意味を分ける**: 変える → 写す → ほかの操作。出せない項目は **出さない**（Break on は
  デバッガが対応するときだけ）。
- **配置**: Run and Debug ビューに VARIABLES・WATCH・CALL STACK・BREAKPOINTS。エディタのタブの右クリック Move into New Window /
  Copy into New Window で「editor, the terminal, or specific views」を別の窓に出せる（複数モニタ向け。https://code.visualstudio.com/docs/configure/custom-layout ）。
  Variables・Watch を別の窓に出せるかは明記が無い。デバッグのツールバーは floating / docked / hidden（debug.toolBarLocation）。
- **Cortex-Debug**（https://github.com/Marus/cortex-debug 、https://github.com/mcu-debug/peripheral-viewer 、https://github.com/mcu-debug/memview ）:
  コアのレジスタは Variables に、RTOS のスレッドは CALL STACK に出る。**Cortex Live Watch** ビュー（見出しに Add expression、行に
  Edit / Move up / Move down / Remove）。Variables の右クリックに **Add to Live Watch** が増える。周辺の SVD ビュー（XPeripherals）は行の
  ボタン Update Value・Copy Value・Refresh・Pin / Unpin、右クリック Set Value Format。Memory は別の拡張（memview）。

### 8. Percepio Tracealyzer

Percepio 自身のマニュアルの該当ページは Web で見つからなかった。以下は Tracealyzer の OEM 版である IntervalZero RTX64 の
ヘルプ（RTX64 3.6 以降）と Percepio の製品ページによる。

- **右クリック**: ブロックするサービス呼び出しの出来事に **Find Entry/Exit of Blocking Service Call**（対になる出来事へ）
  （[TraceView](https://help.intervalzero.com/product_help/RTX64_4/Content/Topics/Tracealyzer/RTX64%203.6%20and%20Later/TraceView.html)）。
  左ドラッグで区間を選び、右クリックで「選んだ区間への操作」のメニュー（名前の分かる項目は Zoom In だけ）。Shift で 2 つ目の
  出来事を選ぶとその間の時間（[Navigating](https://help.intervalzero.com/product_help/RTX64_4/Content/Topics/Tracealyzer/RTX64%203.6%20and%20Later/Navigating.htm)）。
  アクタ（タスク）の右クリックの一覧は見つからなかった。アクタはダブルクリックで Instance Details（Actor Instances / Local
  Trace / Communication Flow）（[Actors](https://help.intervalzero.com/product_help/RTX64_4/Content/Topics/Tracealyzer/RTX64%203.6%20and%20Later/Actors.htm)）。
- **ダブルクリックで掘り下げ**: Event Log の行 → Trace View で強調（[features](https://percepio.com/tracealyzer/features-capabilities/)）。
  Selection Details の最大・最小 → メインの窓がその回へ（[SelectionDetails](https://help.intervalzero.com/product_help/RTX64_4/Content/Topics/Tracealyzer/RTX64%203.6%20and%20Later/SelectionDetails.htm)）。
- **配置**: Layout メニューに Reset to Default Layout / Remember Layout / Layout Locked / Save New Layout / Manage Layout /
  Layouts（**名前付きの配置の保存と切り替え・固定**）（[MenuOptions](https://help.intervalzero.com/product_help/RTX64/Content/PROJECTS/Tracealyzer/RTX64%203.6%20and%20Later/MenuOptions.html)）。
  「All Tracealyzer views are dockable」。ドッキングの候補の外で離すと浮く。**マルチモニタは画面ごとに主窓を足し**
  （ビュー名の一覧を右クリック → **Undock to New Window**）、窓を移す（[TracealyzerViews](https://help.intervalzero.com/product_help/RTX64_4/Content/Topics/Tracealyzer/RTX64%203.6%20and%20Later/TracealyzerViews.html)）。
  既定の配置の中身は見つからなかった。
- **同期の単位 = View Port**: 既定で Details（100 ms）と Overview（10 s）の 2 つ。同じ View Port の窓はスクロール・拡大が
  既定で同期。窓ごとの Sync メニューで Unsynchronized（見出しに "No Sync"）か Custom...（名前のグループ）。時刻だけ同期し
  フィルタは同期しない、ができる（[ViewportsAndSync](https://help.intervalzero.com/product_help/RTX64_4/Content/Topics/Tracealyzer/RTX64%203.6%20and%20Later/ViewportsAndSync.html)）。
  → **詳細と全体の 2 つの時間の窓が並ぶ**のが既定。

### 9. Saleae Logic 2

- **右クリック**: チャンネルの右クリックで表示・非表示（変更履歴 2.3.2 「Show and Hide channels (via right-click menu)」
  https://ideas.saleae.com/f/changelog/232 ）。それ以外の項目の一覧は公式の文書に無い（名前・並べ替え・色は「できる」と
  だけ。https://www.saleae.com/support/logic-software/viewing-and-analyzing-data/navigating-the-software ）。
  Data Table の列の見出しの右クリックで列を隠す。Analyzers の解読器の右クリックで基数（hexadecimal・ASCII・decimal・binary）
  （https://www.saleae.com/support/logic-software/viewing-and-analyzing-data/data-table-and-terminal ）。
- **トリガ**: チャンネルの右クリックからは作らない。Capture Settings の Trigger（エッジ・パルス）。取り込み後は波形の上の
  黄色いタブでトリガの点へ（https://www.saleae.com/support/getting-started/trigger ）。
- **印と測定**: 右の「Timing Markers, Measurements & Notes」の区画から。Measurements は「+」かShift+ドラッグで区間を作り、
  既定の測定は Clock Stats と Voltage RMS。Timing markers は Single Marker と Marker Pair。印をクリックしてメモ・色・削除
  （https://www.saleae.com/support/logic-software/viewing-and-analyzing-data/measurements-timing-markers ）。
- **既定の配置**: 左にチャンネル、中央に波形、右端に区画のボタン（Device Settings / Analyzers / Measurements, Timing
  Markers & Notes / Extensions）。浮かせる窓・マルチモニタの記述は見つからなかった。
- **表と波形**: 表の行にマウスを乗せると矢印のアイコン → 波形のその解読結果へ。波形の解読結果からは Ctrl+クリック
  （Windows）で表へ（フォーラムの回答 https://discuss.saleae.com/t/how-to-view-decoded-protocols/2539 ）。

### 10. sigrok PulseView（マニュアル 0.4.2）

出典: https://sigrok.org/doc/pulseview/0.4.2/manual.html 、NEWS https://sources.debian.org/src/pulseview/0.4.2-4/NEWS

- **信号の名札をクリック**すると設定のポップアップ。機器が対応していればその中に **その信号のトリガ**（low level / high
  level / rising edge / falling edge / any edge）。選ぶと波形の右に印。→ 信号の場所にトリガがある唯一の例（右クリック
  ではなくクリック）。
- **右クリック**: Ctrl+クリックで複数選び、名札を右クリック → Group。グループの灰色の棒を右クリック → Ungroup。印（flag）は
  ルーラーをダブルクリックか、ルーラー・信号の右クリックのメニューから（項目名は本文に無い）。カーソルの中央の札を右クリック
  で、出す値（時間差・周波数・サンプル数）を選ぶ。印とカーソルに「一時的に吸着を切る」メニュー（NEWS）。解読の行の結果の
  領域を右クリック → 書き出しの方法（最後の項目はカーソルがあるときだけ）。
- **既定の配置**: マニュアルに記述なし。ツールバーの New View で波形の窓を足せる（"These can be rearranged as you wish"）。

### 11. DSView（DreamSourceLab）

出典: DSView User Guide v1.00（再配布の写し https://gotronik.pl/images/DSView_User_Guide.pdf ）

- **既定の配置**（図 1-3）: 上に機器名・サンプルレート・長さ・ツールバー、左にチャンネルの設定、中央に波形、右に Tool Dock。
- **トリガ**: Trigger ボタン → Simple Trigger（チャンネルごとに Rising / Falling / Edge / High Level / Low Level）か Advanced
  Trigger。右クリックからは作らない。
- **カーソル**: 波形のダブルクリックで作る。ルーラーの右クリックでカーソルの一覧 → 番号でそこへ（Cursor Jump）。Measure の
  ドックにカーソル間の距離と **Edges counter**（2 カーソルの間の指定チャンネルのエッジの数）。
- **解読の表**: 行をクリック → 波形の同じ位置へ。**Wave map to row** ボタンで波形の今の位置の行を強調（逆向き）。検索・前後・
  Export to file。任意のカーソルを解読の範囲の始め・終わりにできる。

### 12. AMD Vivado Hardware Manager（ILA。UG908）

- **既定のダッシュボード**: Settings / Status / Trigger Setup / Capture Setup / Waveform の 5 つの窓。ILA を初めて見つけると
  自動で開く。Hardware の窓で ILA を右クリック → Dashboard ▸ で開き直す
  （https://docs.amd.com/r/2022.1-English/ug908-vivado-programming-debugging/Default-Dashboard-Windows 、
  https://docs.amd.com/r/en-US/ug908-vivado-programming-debugging/Using-the-ILA-Default-Dashboard ）。Dashboard Options で出す窓を
  選ぶ（https://docs.amd.com/r/en-US/ug908-vivado-programming-debugging/Dashboard-Options ）。1 つの窓だけ浮かせたいときは、
  その窓だけのダッシュボードを作って浮かせるよう勧めている
  （https://docs.amd.com/r/2023.1-English/ug908-vivado-programming-debugging/Creating-New-Dashboards ）。Vivado の窓はタブの
  右クリック → Float（https://docs.amd.com/r/2023.2-English/ug893-vivado-ide/Floating-Windows ）。
- **トリガの設定**: Trigger Setup の窓の「+」で Add Probes（右クリックではない）。波形からトリガを作る項目は見つからなかった。
- **波形の信号の右クリック**: Radix ▸（Binary / Hexadecimal / Unsigned Decimal / Signed Decimal / Octal 等）・Show as Enumeration・
  Rename（表示名だけ）・New Divider（UG908 の Radixes / Using-Enumerations / Renaming-Objects / Dividers の各ページ）。色は
  Waveform Settings の Colors で、右クリックの項目としては確認できなかった。
- **印とカーソル**: 主カーソルはクリック、2 本目は Ctrl+ドラッグか Shift+クリック、Snap to Transition が既定でオン。カーソルは
  一時的で、残す基準には Markers を使うよう書いている（https://docs.amd.com/r/en-US/ug908-vivado-programming-debugging/Cursors ）。
  同じ波形の部品の Vivado シミュレータ（UG900）では右クリック Markers ▸ Add Marker、印の右クリック Delete Marker / Delete All
  Markers（https://docs.amd.com/r/en-US/ug900-vivado-logic-simulation/Using-Markers ）。表示の凡例: トリガの印（赤）・カーソル
  （黄）・印（青）・測りのルーラー・取り込みの窓の切れ目（白と灰色）（Waveform-Viewer-Layout）。

### 13. Intel Quartus Signal Tap

- **トリガは Setup タブ**: Trigger Conditions の列を右クリックでパターン（Don't Care / Low / High / Falling Edge / Rising Edge /
  Either Edge。大学の教材 https://pages.hmc.edu/harris/class/e155/SignalTap.pdf ）と Insert Value
  （https://resources.altera.com/quartushelp/18.0/program/ela/ela_tab_setup.htm ）。ノードの右クリック → Compare（比較の
  トリガ。https://www.intel.com/content/www/us/en/docs/programmable/683819/24-3/specifying-the-comparison-trigger-conditions.html ）。
  Advanced トリガの演算子を右クリック → Properties。トリガ名の右クリック → Duplicate Trigger
  （https://intel.com/content/www/us/en/docs/programmable/683819/24-2/enabling-a-power-up-trigger.html ）。
- **Data タブ**: タイムバーの右クリック Insert Master Time Bar / Set As Master Time Bar / Move Master Time Bar
  （https://quartushelp.altera.com/current/program/ela/ela_com_insert_timebar.htm ）。View: Delete All Time Bars / Next
  Transition / Previous Transition / Fit in Window / Zoom In / Zoom Out / **Center on Trigger**。バスの右クリック → Bus Display
  Format ▸（https://quartushelp.altera.com/current/program/ela/ela_com_ascii.htm ）。Setup と Data はノードの並びを共有
  （https://resources.altera.com/quartushelp/19.2/program/ela/ela_tab_node_list.htm ）。
- Data タブの値から「Create Trigger Condition」で条件を作る項目は、公式の文書では確認できなかった（未確認）。

### 14. Keysight・Tektronix の PC ソフト

- Keysight Infiniium: 目盛りの領域を右クリック → **Place Marker here**（印は最大 16）
  （https://helpfiles.keysight.com/csg/d9300a/Help/Infiniium-UG/Content/Topics/Markers/Markers.htm ）。
- Keysight のロジアナのソフト・TekScope PC・TekHSI の右クリックの項目は見つからなかった。Tektronix の旧 TDS5000B は
  「Right-click menus are context sensitive」（二次的な写しのみ）。

## 第 2 部 右クリックの項目の比較

### 2.1 場所 × 道具

項目名は英語のまま縮めた。「—」= その場所の右クリックの記述が見つからなかった（無いとは限らない）。

| 場所 | Ozone | TRACE32 | Arm DS | IAR | Keil | Eclipse CDT | VS Code | ロジアナ・FPGA |
|---|---|---|---|---|---|---|---|---|
| 変数・式 | Data Breakpoint・Watch・Graph・Show Value in …・Show Source / Data・Display As | Breakpoints > Write・Format・Display Trace ▸（List / Chart / Draw）。Variable のポップアップは利用者が足せる | Show in Memory / Disassembly / Registers・Show Dereference・Watchpoint・Send to ▸・形式 | Set Data (Log) Breakpoint・Add to (Quick / Live) Watch・Find in Trace | Add 'XXX' to... ▸（Watch / Memory / Analyzer） | Format・Cast To Type・Display As Array・View Memory・Watchpoint・Watch | Set Value・Copy Value・Copy as Expression・Add to Watch・Break on Value Read / Change / Access・View Binary Data | — |
| 番地・メモリ | Copy (Special ▸)・Show Disassembly / Data・Display / Access Mode・Fill・Save・Load・Go To | Data Address のメニュー（項目は未確認） | Watchpoint・Translate Address・（View Menu に Fill / Import / Export） | Zone・Units・Endian・Find / Replace・Fill / Save / Restore・Set Data (Log) Breakpoint | Access Size ▸（項目名は未確認） | Go To Address・Radix・Cell Size・Endian | （View Binary Data） | — |
| ソースの行 | Breakpoint・Break On Change・Tracepoint・Set Next Statement・Run To Cursor・Show …・Watch・Graph・Find In Trace | Modify here・Toggle Bookmark・（Program Address のポップアップ） | Set PC to Selection・Run to Selection・Show in Disassembly・Inspect。帯: Breakpoint・HW Breakpoint・Trace Start / Stop / Trigger Point | Toggle Breakpoint (Code / Log / Trace Start / Stop)・Set Next Statement・Run to Cursor・Find in Trace | Set Program Counter・Run to Cursor Line・Show Disassembly・Add to ▸ | Run to Line・Move to Line・Resume at Line・Toggle Breakpoint | Add (Conditional) Breakpoint・Add Logpoint・Run to Line / Cursor・Jump to Cursor | — |
| 逆アセンブルの命令 | Breakpoint・Tracepoint・Set Next PC・Run To Cursor・Show Source / Target Address・Go Back / Forward | Assemble here・Program Address | Run to / Set PC to Selection・Show in Source / Registers / Functions・Breakpoint・Trace Point | Move to PC・Run to Cursor・Code Coverage ▸・Breakpoint・Mixed-Mode | — | Run To Line・Resume At Line | （本体の窓） | — |
| 時刻・区間・カーソル | Fit・Go To Cursor / Reference / Time・Toggle Reference・Cursor（固定）・Code の区画: Go To Start / End・Next / Previous execution | Set Ref・Set Zero・Toggle Bookmark・Set CTS | Trace: Focus Here・Show Next / Previous Match | Navigate ▸・Zoom ▸・Go To Source・Profile Selection・Timestamp ▸（Zero Point）・Bookmarks ▸ | （ツールバー: Jump to Code / Trace・Go To Marker） | — | — | Tracealyzer: 区間 → Zoom In ほか。SignalTap: Insert / Set As / Move Master Time Bar。Vivado: Markers ▸ Add Marker。Infiniium: Place Marker here。DSView: カーソルの一覧へ Jump。PulseView: 印・カーソルの値の種類 |
| 波形の信号・チャンネル | Timeline の Data の区画: Sampling Frequency・Set Offset To Code・Draw Points | Trace.Timing の信号 → 解析 | — | グラフ: Enable・Style・Size・Show Numerical Value・Hexadecimal・Go To Source | System Analyzer: Steps/States/Discrete・Hexadecimal・Min/Max・Remove Signal | — | — | Vivado: Radix ▸・Show as Enumeration・Rename・New Divider。SignalTap: Bus Display Format ▸。Saleae: Show/Hide。PulseView: Group / Ungroup |
| タスク・スレッド | —（RTOS の窓は Refresh・Views だけ） | TASK の magic 欄に local menu（項目未確認） | Debug Control: Continue・Interrupt・Step・Show in Stack | 未確認 | （Call Stack に RTX のタスク） | Debug ビュー: Resume・Suspend・Step・Drop To Frame・Copy Stack | Pause / Continue・Step・Terminate Thread | Tracealyzer: ダブルクリックで Actor Instances（右クリックは未確認） |
| レジスタ | Show Source / Disassembly / Data・Display As・Refresh Rate・Add SVD File | Set ...・（PER.view: Show all・値の選択） | Show Memory / Disassembly Pointed to By <reg>・Translate Address・Send to | View Group・Format・Find Next / Previous Register | —（ダブルクリック・F2 で書く） | Change Value・Cast To Type・Create Watch Expression・Format・Register Group | （Variables に統合） | — |
| ブレークポイント | Clear・Enable / Disable・Edit・Show Source / Disassembly・Set …・Enable / Disable / Clear All・Vector Catches | 未確認 | Delete (All)・Go to File・Skip All・Show in Disassembly / Memory・Enable / Disable・Properties | Go to Source・Edit・Delete・Enable / Disable (All)・New Breakpoint ▸ | — | — | Remove・Edit・Disable / Enable | — |
| トレースの記録・統計の行 | Instruction Trace: Show Source / Disassembly・Toggle Reference・First / Last in Function・Find In Trace。Code Profile: Include / Exclude・Show Source | Trace.List: Set Ref / Zero・Set CTS。統計の関数: Parents・Children・Duration Analysis・Distance Analysis | Trace: Focus Here・Order By Total Time・Mark other occurrences | Trace: Bookmarks ▸・Timestamp ▸・Navigate ▸。Profiler: Show Source | — | — | — | Saleae: 表 → 波形（矢印）。DSView: 行 → 波形 |

### 2.2 多くの道具に共通の項目（＝利用者が期待する項目）

| 場所 | 共通の項目 | 持つ道具 |
|---|---|---|
| ソースの行 | ブレークポイントを置く・外す（先頭） | Ozone・Arm DS・IAR・Eclipse・VS Code・（Keil は余白のクリック） |
| ソースの行 | ここまで実行（Run to Cursor / Line / Selection） | Ozone・Arm DS・IAR・Keil・Eclipse・VS Code |
| ソースの行 | PC をここへ（Set Next Statement / PC・Move to Line・Jump to Cursor） | Ozone・Arm DS・IAR・Keil・Eclipse・VS Code |
| ソースの行 | 逆アセンブル ⇄ ソースへ | Ozone・Arm DS・Keil・IAR（Mixed-Mode） |
| 変数 | ウォッチに足す | Ozone・IAR・Keil・Eclipse・VS Code・Arm DS（Send to） |
| 変数 | 値の変化・アクセスで止める（データブレーク / ウォッチポイント） | Ozone・TRACE32・Arm DS・IAR・Eclipse・VS Code |
| 変数・レジスタ | メモリで開く（番地・指す先） | Ozone・Arm DS・Eclipse・VS Code・Keil |
| 変数・レジスタ | 表示形式 | 全部 |
| 時間軸 | 拡大・合わせる（Fit / Zoom ▸） | Ozone・IAR・SignalTap・Tracealyzer |
| 時間軸 | 基準（時刻 0・Reference）を置く | Ozone・SystemView・TRACE32（Set Ref / Zero）・IAR（Zero Point） |
| 時間軸 | 印・ブックマーク | TRACE32・IAR・Tracealyzer・Vivado・Infiniium・Saleae |
| 時間軸・グラフ | その時刻・その値のソースへ（Go To Source） | IAR・Keil（Jump to Code）・Ozone（Sample Cursor の同期） |
| 時間軸 | 次・前の同じもの（実行・変化・出来事） | Ozone・IAR（Navigate ▸）・SystemView（キー）・SignalTap・Keil |
| 波形の信号 | 基数・表示形式、名前、隠す・まとめる | Vivado・SignalTap・Saleae・PulseView・Keil |
| 表の窓 | 末尾に Export / Save to File | Ozone・IAR・Keil |

### 2.3 ある道具だけの良い工夫

- **値の種類で共通のポップアップ**（TRACE32 の Program Address / Variable。利用者の足した項目が、その種類を出す全部の窓に出る）。
- **窓の見出し = その窓を開いたコマンド**、右クリックでコマンドラインに入る（TRACE32）。
- **変数 → 時間の上の記録**（TRACE32 の Display Trace ▸ List Variable / Value Chart / Draw、Ozone の Graph・Find In Trace、IAR の Find in Trace）。
- **区間 → プロファイル**（IAR の Profile Selection: 時間軸の区間を Function Profiler に渡す）。
- **時間の区画の時刻のずれを利用者が合わせる**（Ozone の Set Offset To Code。自動で合わせず、使う側が明示する）。
- **その値を送る先をサブメニューで選ぶ**（Keil の Add 'XXX' to... ▸、Arm DS の Send to ▸）。
- **出せない項目は出さない**（VS Code の Break on はデバッガが対応するときだけ。Ozone の Show Value in … はポインタ型だけ）。
- **Timeline の右クリックの 2 段**（IAR: 時間軸の共通の操作 → 区切り → そのグラフの操作）。
- **同じ関数の次・前の実行へ**（Ozone の Code の区画、IAR の Navigate Call Stack ▸ の 14 項目）。
- **統計の行から分布へ**（TRACE32 の Duration / Distance Analysis、Parents / Children）。
- **Trace の行からその時点の状態を作り直す**（TRACE32 の Set CTS、Green Hills の TimeMachine）。
- **ハードのトリガを値から作る**項目は、ロジアナ・FPGA の道具でも見つからなかった（どれも取り込みの設定の側。PulseView だけが
  信号の名札のクリックにトリガを置く）。


## 第 3 部 既定の配置と思想の比較

| 道具 | 初めて開いたとき | 配置の切り替え | 浮かせる・マルチモニタ | 時刻・選択の連動の単位 |
|---|---|---|---|---|
| Ozone | 既定の配置＋Project Wizard。中央にソース固定、左右下に窓 | 名前付きの切り替えは見つからず。`*.user` に保存し次に戻る | タイトルのドラッグ・ダブルクリックで外す。マルチモニタの明記なし | Sample Cursor ⇄ PC を持つ窓（Source・Disassembly・Instruction Trace）・Data/Power Sampling の表。全部が既定で連動 |
| SystemView | 見本の記録を開き System Information | 記述なし | 記述なし | 「All windows are synchronized」。Timeline の印 ⇄ Events list ⇄ Terminal |
| TRACE32 | t32.men の既定のメニュー。配置は起動スクリプト次第 | Window Pages（WinPAGE）。配置はコマンドの列として保存（Store Windows） | MDI / MWI（FDI・MTI）。WinExt で外へ | 窓ごとに /Track・/ZoomTrack を付けた窓だけ（既定では連動しない） |
| Arm DS | Development Studio perspective（接続時に切り替えを確かめる） | Eclipse のパースペクティブ | Eclipse の標準（タブのドラッグ） | 接続（Linked:）。Stack のフレームを選ぶと全ビューが従う。Trace のチャート → Disassembly |
| Tracealyzer | 既定の配置の中身は未確認（Reset to Default Layout はある） | **Save New Layout / Layouts / Layout Locked** | 全ビューがドッキング可。**画面ごとに主窓を足す**（Undock to New Window） | **View Port**（既定は Details 100 ms と Overview 10 s の 2 つ）。窓ごとに No Sync・名前のグループ |
| Saleae Logic 2 | 左にチャンネル・中央に波形・右に区画のボタン 4 つ | 無し（未確認） | 未確認 | 表の行 → 波形（矢印）、波形 → 表（Ctrl+クリック） |
| PulseView | 未確認（New View で波形の窓を足す） | 無し（未確認） | 未確認 | 未確認 |
| DSView | 上に機器・速さ、左にチャンネル、中央に波形、右に Tool Dock | 無し（未確認） | 未確認 | 表の行 → 波形、Wave map to row で逆 |
| Vivado ILA | **ILA を見つけると既定のダッシュボード**（Settings / Status / Trigger Setup / Capture Setup / Waveform）が自動で開く | ダッシュボードを作る・Dashboard Options で窓を選ぶ | タブの右クリック Float。1 窓だけ浮かせるならその窓だけのダッシュボード | 1 つの波形の窓は 1 つのダッシュボードだけ |
| Signal Tap | Setup タブと Data タブ（ノードの並びを共有） | タブ | 未確認 | Setup と Data でノードを共有 |

| IAR C-SPY | 未確認（編集とデバッグで別の配置、は非公式の情報だけ） | 未確認 | 題名のダブルクリックで浮かせる | Timeline のグラフをクリック → ソース。区間 → Profile Selection で Function Profiler |
| Keil µVision | 前回のデバッグの配置が戻る。止まった所のソースか逆アセンブル | 編集 / デバッグの 2 つの画面。Reset View to Defaults | 「floated to another screen」 | Logic Analyzer・Event Viewer → Jump to Code / Trace |
| Eclipse CDT | Debug パースペクティブ（Debug・Variables・Breakpoints・Outline・Console 等） | パースペクティブ | 外へドラッグで Detached | Debug ビューのフレームの選択に全ビューが従う |
| VS Code | Run and Debug ビュー（Variables・Watch・Call Stack・Breakpoints） | 無し（ビューの出し入れ） | Move into New Window（エディタ・端末・一部のビュー） | Call Stack のフレームの選択 |

### 読み取れる思想

1. **機器・記録から既定を作る**: Vivado は ILA を見つけた時点でその ILA のダッシュボードを開く。SystemView は見本の記録を
   開いて System Information から始める。→ fprobe の原則 4（機器のケイパビリティから既定を作る）と同じ向き。
2. **中心の窓を固定する**: Ozone はソースを中央に固定し、ほかは周りに出し入れ。fprobe では中心は共通の時間軸。
3. **連動の範囲を利用者が選べる**: 既定で全部連動（Ozone・SystemView）か、窓ごとに選ぶ（TRACE32 の /Track）、グループで
   分ける（Tracealyzer の View Port・Arm DS の Linked:）。**詳細と全体の 2 つの時間の窓を既定で持つ**（Tracealyzer）。
4. **配置は名前で保存・切り替え・固定**（Tracealyzer の Layouts と Layout Locked、TRACE32 のページとコマンドの列での保存、
   Eclipse のパースペクティブ）。
5. **マルチモニタは「主窓を足す」**（Tracealyzer）か「窓を OS の窓にする」（TRACE32 の MWI、Ozone・Vivado の浮かせる窓）。


## 第 4 部 fprobe の右クリックのメニューの案

前提（仕様 §3.5 と利用者の決定）: メニューは **値の種類** ごと。上に **厳選した短い一覧**、下に **「その他の操作」**
（その値を引数に取れる全 API。API の仕様の注記から作る。利用者が足した API も出る）。画面は判断しない（ある値が
「番地らしい」かを推定しない。欄の注記が番地なら番地のメニュー）。

### 4.1 全体の作り（他の道具から取り入れること）

1. **値の種類で引く共通のメニュー**（TRACE32 の Program Address / Variable のポップアップ）。同じ値は、どの窓で右クリック
   しても同じ厳選の一覧が同じ順で出る（Ozone も変数の窓 4 つで並びをそろえている）。窓に固有の項目（その窓の見せ方）は
   区切り線の下に分ける。
2. **3 段の並び**（Ozone の型）: (1) その値への操作（止める・見張る・書く）→ (2) 他の窓へ移る（Show in …）→ (3) 写す。
   その下に「その他の操作」、最後に窓の見せ方（表示形式・列）。
3. **移る項目は「どの窓で開くか」を名前に入れる**（Ozone の Show Source / Show Disassembly / Show Data / Show in Memory
   Map、Cortex-Debug・Eclipse の View Memory）。
4. **時間の上の記録へ移る項目を値のメニューに置く**（TRACE32 の Display Trace、Ozone の Graph・Find In Trace）。fprobe は
   共通の時間軸が中心なので、変数・番地・行のどれからも「時間軸で見る」へ 1 段で行けるのが強み。
5. **次・前の同じもの**（Ozone の Go To Next/Previous execution of、SystemView の N / P と F / B）を時刻の値のメニューと
   キーの両方に。
6. **メニューの項目 = API の呼び出し**として、項目に API 名を小さく出す（TRACE32 が窓の見出しにコマンドを出すのと同じ
   ねらい。原則 2 の一対一を人に見せる）。「その他の操作」は API 名で並べ、探せる欄を付ける。
7. 書く操作は **「値を変える」1 つの名前**（VS Code の Set Value。VS Code は区切りの最初のグループに置く）にし、走行中・停止中でどの API を使うかを項目の脇に出す
   （仕様 §3.6）。
8. トリガの項目は「この値で止める（ブレークポイント・ウォッチポイント）」と「この値で記録・トリガ（クロストリガ・
   サンプラ・FPGA の条件）」を分ける。調べた範囲では、**波形・値から右クリック 1 段でハードのトリガを作れる道具は
   見つからなかった**（ロジアナの道具はどれも取り込みの設定の側でチャンネルにトリガを付ける。SignalTap の Data タブから
   条件を作る項目も公式の文書では確認できなかった）。fprobe がこれを持てば独自の強み。ただし引数（一致の値・マスク・
   止めるか）は使う側が決めて渡すもので、既定の値を暗黙に入れない（CLAUDE.md の原則）。
9. **そのときに使えない項目は出さない**（VS Code の Break on、Ozone の Show Value in …）。ただし fprobe の原則 4（使えない機能も
   「探して呼べる」ことが見える）に合わせ、厳選の一覧からは消し、「その他の操作」には使えない理由（ケイパビリティが無い・
   走行中は呼べない 等。API の仕様の注記から）を付けて灰色で残す、が案。
10. **時間軸のメニューは 2 段**（IAR）: 上に時間軸の共通の操作（どの段でも同じ）、区切りの下にその段（波形・変数・タスク・
    出来事）の値の操作。段の値の操作は、下の (a)〜(f) と同じもの。
11. **「送る先」をサブメニューにまとめる**（Keil の Add to ▸、Arm DS の Send to ▸）: 「ウォッチへ」「時間軸の段へ」
    「ダッシュボードへ」等の、窓を変えるだけの項目が増えたら 1 つのサブメニューにまとめ、厳選の一覧を短く保つ。

### 4.2 場所ごとの厳選の一覧

（「→」の右は呼ぶ API。`recording.code_at` は仕様 §4 の案で、まだ無い。）

**(a) 変数・式**（ウォッチ・ローカル・止めずに見る・データサンプリング・ソースの識別子）

| # | 項目 | API | 根拠 |
|---|---|---|---|
| 1 | 値を変える… | 走行中 `watch.write` / 停止中 `debug.write_expression` | VS Code Set Value、TRACE32 の変数の変更。仕様 §3.6 |
| 2 | 止めずに見る（ウォッチに足す） | `watch.set` | Ozone Watch、IAR Add to Live Watch、Cortex-Debug Add to Live Watch、VS Code Add to Watch、Keil Add to ▸ Watch |
| 3 | 時間軸に描く | `watch.set`（記録へ）＋ `ui.state.set`（段を足す） | Ozone Graph、TRACE32 Display Trace > Draw |
| 4 | 値が変わったら止める（ウォッチポイント） | `debug.watchpoint.set`（kind write） | Ozone Break On Change、VS Code Break on Value Change、TRACE32 Breakpoints > Write |
| 5 | この値を条件に記録・トリガ… | `recording.trigger_set` / `sampler.trigger_set` | （同じ項目の先例は見つからなかった。fprobe の強み） |
| 6 | 番地をメモリで開く | `debug.symbol.info` → `target.memory.view` | Ozone Show Data、Arm DS Show in Memory、Eclipse View Memory、VS Code View Binary Data |
| 7 | 定義のソースへ | `debug.symbol.info` | Ozone Show Definition / Show Source |
| 8 | 時間軸の記録を探す（この変数の変化の一覧） | `recording.find` | TRACE32 Display Trace > List Variable、Ozone・IAR Find In Trace |
| 9 | 式をコピー / 値をコピー | （API なし。見せ方） | VS Code Copy as Expression / Copy Value |
| — | その他の操作 ▸ | 式・変数名を取る全 API（`debug.evaluate`・`debug.type_info`・`rtos.read`・`recording.durations` 等） | 利用者の決定 |

**(b) 番地・メモリ**（メモリの窓・レジスタの値・ポインタ・イベントの番地）

| # | 項目 | API | 根拠 |
|---|---|---|---|
| 1 | 値を変える… | `target.memory.write` | Ozone の Memory は直接書く・Fill |
| 2 | メモリで開く | `target.memory.view` | Ozone Show Data、Eclipse View Memory |
| 3 | 逆アセンブルで開く | `debug.disasm.around` | Ozone Show Disassembly |
| 4 | ソースの行へ | `debug.symbol.info` | Ozone Show Source |
| 5 | 止めずに見る | `watch.set`（"0x…"） | Ozone Watch |
| 6 | アクセスで止める（ウォッチポイント） | `debug.watchpoint.set` | Ozone Set Data Breakpoint |
| 7 | 番地一致でトリガ… / 値一致でトリガ… | `xtrig.set_match` / `sampler.trigger_set` | （fprobe 固有。仕様 §3.5 の例） |
| 8 | 番地をコピー | （見せ方） | Ozone Copy・Copy Special |
| — | その他の操作 ▸ | 番地を取る全 API（`target.memory.read`・`target.flash.read`・`target.peripheral.read`（SVD で一致するとき）等） | |

**(c) ソースの行・逆アセンブルの命令**

| # | 項目 | API | 根拠 |
|---|---|---|---|
| 1 | ブレークポイント（置く / 外す） | `debug.breakpoint.set` / `.clear` | Ozone・Arm DS・IAR・Eclipse・VS Code。どれも先頭か先頭の近く |
| 2 | 条件付き / ログのブレークポイント… | `debug.breakpoint.set`（condition / log） | VS Code Add Conditional Breakpoint / Add Logpoint、IAR Toggle Breakpoint (Log)、Ozone Edit Breakpoint |
| 3 | ここまで実行 | （今は 1 つの API が無い。`debug.breakpoint.set` の一時＋`target.resume` を画面で組むと画面が手順を持つので、API を足すかは別に決める） | Ozone・Arm DS・IAR・Keil・Eclipse・VS Code（6 つ全部） |
| 4 | 時間軸でこの行を通った時刻を探す | `recording.find` / `profile.result`（行） | Ozone Go To Next/Previous execution、Find In Trace |
| 5 | ここから・ここまでの時間を測る… | `cycles.measure`（start / end）/ `timing.measure` | TRACE32 Duration / Distance Analysis |
| 6 | 逆アセンブル ⇄ ソース | `debug.disasm.around` / `debug.symbol.info` | Ozone Show Disassembly / Show Source |
| 7 | PC をここへ（Set Next Statement） | `target.registers.write`（pc） | Ozone Set Next Statement / PC、Arm DS Set PC to Selection、IAR Set Next Statement、Keil Set Program Counter、Eclipse Move to Line、VS Code Jump to Cursor |
| — | その他の操作 ▸ | "ファイル:行" / 関数名を取る全 API（`debug.disasm.function`・`testpoint.*`・`linux.probe.add` 等） | |

PC をここへは命令を飛ばす操作なので、厳選に残すかは利用者が決める（調べた IDE は 6 つ全部が行の右クリックに置いている）。
並びは IDE の多数（ブレークポイント → ここまで実行 → PC をここへ → 他の窓へ）に合わせ、fprobe の強みの 4・5（時間軸）を
その間に入れる案。

**(d) 時間軸の時刻・区間・カーソル**

| # | 項目 | API | 根拠 |
|---|---|---|---|
| 1 | この時刻のコードの位置（ソース・タスク） | `recording.code_at`（案）→ `ui.state.set` | Ozone の Sample Cursor の同期、TRACE32 Set CTS |
| 2 | 印とメモを残す | `recording.mark` | Tracealyzer Add Bookmark、Saleae Notes、Vivado Add Marker、Infiniium Place Marker here |
| 3 | 基準にする（ここからの時間差） | `ui.state.set`（cursors） | Ozone・SystemView Toggle Reference |
| 4 | 次 / 前の同じ出来事へ | `recording.find` | SystemView N / P、Ozone Go To Next/Previous execution of |
| 5 | この時刻の前後の出来事 | `recording.events`（t0・t1） | SystemView Events list の同期 |
| 6 | （区間のとき）区間を測る | `recording.waveform_measure` / `la.measure` / `timing.measure` | Saleae Measurements、DSView Edges counter、PulseView のカーソル |
| 7 | （区間のとき）区間に拡大 | `ui.state.set`（view） | Tracealyzer の区間の右クリック Zoom In、Ozone Fit Width、IAR Zoom to Selection |
| 8 | （区間のとき）区間のプロファイル | `profile.aggregate`（区間） | IAR Profile Selection |
| 9 | （区間のとき）区間を書き出す | `recording.export`（t0・t1） | PulseView の解読の行の書き出し、Ozone・IAR の Export / Save to File |
| — | その他の操作 ▸ | 時刻・区間を取る全 API（`recording.density`・`profile.aggregate`・`rtos.timeline.load` 等） | |

**(e) 波形の信号・チャンネル**

| # | 項目 | API | 根拠 |
|---|---|---|---|
| 1 | 表示形式（基数・列挙） | （見せ方） | Vivado Radix / Show as Enumeration、SignalTap Bus Display Format |
| 2 | 名前を変える | `fpga_obs.signals_set` 等（定義を持つ API） | Vivado Rename、Saleae |
| 3 | 隠す / まとめる | （見せ方） | Saleae Show/Hide、PulseView Group |
| 4 | 次 / 前の変化へ | `recording.find` | SignalTap Next / Previous Transition |
| 5 | このチャンネルでトリガ…（レベル・立ち上がり・立ち下がりを選ぶ） | `la.arm` / `la.deep.arm`（mask・value・rise・fall） | PulseView の信号ごとのトリガ（クリック）、DSView Simple Trigger |
| 6 | 解読を足す… | `la.decode` | Saleae Analyzers、PulseView |
| 7 | このチャンネルを測る | `recording.waveform_measure`（channels） | Saleae Measurements |
| — | その他の操作 ▸ | チャンネルを取る全 API | |

**(f) タスク・スレッド**

| # | 項目 | API | 根拠 |
|---|---|---|---|
| 1 | 呼び出し履歴 | `rtos.backtrace` | Eclipse の Debug ビュー・VS Code の Call Stack はスレッドの下にフレームを積む、Arm DS Show in Stack |
| 2 | このタスクの文脈で式を読む | `rtos.read` | （同じ項目の先例は見つからなかった） |
| 3 | 次 / 前の実行へ（時間軸） | `rtos.timeline.get` → `ui.state.set` | SystemView F / B、Tracealyzer Actor History |
| 4 | 実行の一覧・統計 | `rtos.timeline.jobs` / `rtos.timeline.profile` | Tracealyzer Actor Instances、SystemView Runtime |
| 5 | 待っているもの・ブロック | `rtos.timeline.blocking` | Tracealyzer Find Entry/Exit of Blocking Service Call |
| — | その他の操作 ▸ | タスクを取る全 API | |

**(g) レジスタ**

| # | 項目 | API | 根拠 |
|---|---|---|---|
| 1 | 値を変える… | `target.registers.write` | 全 IDE |
| 2 | 値を番地としてメモリで開く | `target.memory.view` | Ozone Show Data、Arm DS Show Memory Pointed to By <reg> |
| 3 | 値を番地としてソース / 逆アセンブルへ | `debug.symbol.info` / `debug.disasm.around` | Ozone Show Source / Show Disassembly、Arm DS Show Disassembly Pointed to By <reg> |
| 4 | 表示形式 | （見せ方） | Ozone Display (All) As |
| — | その他の操作 ▸ | 番地（値）を取る全 API（(b) と同じ） | |

**(h) ブレークポイント**

| # | 項目 | API | 根拠 |
|---|---|---|---|
| 1 | 外す | `debug.breakpoint.clear` | Ozone Clear |
| 2 | 条件・ログを変える… | `debug.breakpoint.set`（置き直し） | Ozone Edit、VS Code Edit Breakpoint |
| 3 | ソースへ / 逆アセンブルへ | `debug.symbol.info` / `debug.disasm.around` | Ozone Show Source / Show Disassembly |
| 4 | 当たった記録 | `debug.breakpoint.log` | （ログのブレークポイントの結果） |
| 5 | すべて外す | `debug.breakpoint.clear`（location 省略） | Ozone Clear All |

有効・無効（Ozone・Arm DS・IAR・VS Code の Enable / Disable）は、今の `debug.breakpoint.*` に無い。足すかは別に決める。

### 4.3 既定の配置で学ぶべき点（§3.3 への材料）

1. **機器から既定を作り、最初に見るものを決める**: Vivado は ILA を見つけたらそのダッシュボード、SystemView は記録を開いて
   System Information。fprobe は「ケイパビリティ → 既定の窓」（原則 4）に加え、記録を開いたら時間軸＋全体の要約から始める。
2. **中心を固定し、周りを出し入れ**（Ozone はソース固定）。fprobe の中心は共通の時間軸。ソースは時間軸のカーソルに従う
   窓（Ozone の Sample Cursor と同じ）として既定に置く。
3. **時間の窓を「詳細」と「全体」の 2 つ**（Tracealyzer の View Port の既定）。fprobe の recording.overview（縮図）と時間軸。
4. **連動は既定で全部、外すのは窓ごと**（Ozone・SystemView は全部連動。Tracealyzer は既定で同期し、窓ごとに No Sync・
   名前のグループ）。TRACE32 の「既定は連動しない」は fprobe の目的（時刻で結ぶ）に合わない。
5. **配置は名前で保存・切り替え・固定・既定に戻す**（Tracealyzer の Layouts / Layout Locked / Reset to Default Layout、Keil の
   Reset View to Defaults、Eclipse のパースペクティブ、TRACE32 のページ）。「解析」と「日常のデバッグ」の 2 つを用意し
   （Keil・IAR の編集 / デバッグの切り替えに相当）、利用者が足せる。
6. **配置 = API の呼び出しの列**（TRACE32 の Store Windows は窓を開くコマンドの列を書く）。fprobe の汎用の窓（§3.1、引数を
   決めた API の呼び出し）と同じ形で保存すれば、AI も同じ配置を作れる。
7. **マルチモニタ**は「窓を OS の窓に出す」（Ozone・IAR・Keil・Eclipse・Vivado・VS Code の Move into New Window）か「主窓を
   画面ごとに足す」（Tracealyzer）。fprobe の今の「別のウィンドウで開く」は前者。

## 調べられなかったこと

- 区切り線の位置（Ozone は図だけ）、TRACE32 の組み込みのポップアップの全項目（t32.men・画像だけ）、Break.List・TASK の右クリック。
- SystemView・Tracealyzer のタスク（アクタ）の右クリックの一覧、Tracealyzer の既定の配置の中身（Percepio 自身のマニュアルが
  Web で見つからず、OEM 版の RTX64 のヘルプで代えた）。
- Green Hills MULTI の右クリックと既定の配置（公式のマニュアルが非公開）。
- Arm DS の現行版（2021.0 の PDF で代えた）。STM32CubeIDE（UM2609 を取得できず）。
- IAR の RTOS の窓・既定の配置、Keil の Watch / Memory / Call Stack の右クリックの項目名（機能の説明だけ）。
- Eclipse CDT の Variables・エディタの右クリックの並び（ソースの文字列だけ）。
- Saleae・PulseView の右クリックの項目の多く（動画・画像だけ）、Keysight のロジアナのソフト・TekScope PC。
- SignalTap の Data タブから条件を作る項目（Create Trigger Condition）は公式の文書で確認できなかった。
