# ヒューリスティックの監査: 画面（host/ui/src）（2026-10-23）

担当範囲: host/ui/src の .ts と .svelte すべて（*.test.ts を除く）133 ファイル。コードは変えていない。
基準: CLAUDE.md の「fprobe 本体にヒューリスティックを入れない」と「画面は考えない層」。
ファイルの場所は host/ui/src からの相対。行番号は 2026-10-23 の作業ツリー（ブランチ audit/heuristics-2）。

## 0. 結果の要約

検索に掛かった行（i18n.ts を除く）1341 行を 1 行ずつ分けた。

| 分類 | 行数 | 項目の数（同じ処理の行をまとめた数） |
|---|---|---|
| H（ヒューリスティック） | 21 | 9（H2 は検索に掛からず既知の一覧から。H8 は一部の行だけ掛かった） |
| ?（判断がつかない） | 23 | 11 |
| C（画面の中の計算） | 130 | 61（うち 16 項目は本体の行が検索に掛からず、既知の一覧から） |
| F（事実・仕様の規則） | 73 | — |
| O（操作の既定） | 185 | — |
| D（見せ方） | 909 | — （うち 152 行は CSS とアイコンの図形） |

i18n.ts の 77 行（訳文の中の語・数）は別に数え、すべて D（文言）とした（§1 の末尾）。

既知の 73 件（docs/reports/2026-10-23-ui-computations.md）の改めての分類: H 9・? 2（c30・c31。c4 は一部が ?）・
F 5・O 1・C 56（§5）。前の報告で (c)「判定・経験則」に入れた 32 件のうち、経験則・推定と言えるのは 9 件で、
残りは決まった計算（C）・仕様の規則（F）・操作の既定（O）だった。

## 1. 実行した検索

rg（ripgrep 15.2）を host/ui/src に対して、`-g '*.ts' -g '*.svelte' -g '!*.test.ts'` で実行した（133 ファイルすべてが対象。
ヒットの無いファイルは §6）。件数は「一致した行の数」。1 行が複数の検索に当たることがあるので、合計は重複を含む。

| # | パターン | ヒット行 | ファイル数 | 機械的に除外 | 除外の理由 | うち i18n.ts |
|---|---|---|---|---|---|---|
| 1a | `[<>]=?\s*-?\d*\.?\d+` | 238 | 76 | 182 | 比べる数がすべて 0 か 1（`> 0`・`>= 1`・`< 0` 等。配列の添字・長さの 0 比較を含む）。`0x…`・`1e…` で始まる数は除外しない | 2 |
| 1b | `-?\d*\.?\d+\s*[<>]=?` | 68 | 25 | 24 | 同上（左辺の数が 0 か 1。`b1 <=` 等の名前の末尾の数字は除外しない） | 0 |
| 2a | `\b\d+\.\d+\b` | 143 | 40 | 0 | — | 12 |
| 2b | `\*\s*\d+(\.\d+)?\b` | 102 | 32 | 31 | 単位換算・百分率（`* 1000`・`* 100`・`* 1e6` 等。`* 1` を含む） | 0 |
| 3a | `=\s*\d` | 351 | 78 | 223 | 代入・比較の値がすべて 0 か 1（`let i = 0`・`=== 0` 等）。`0x…` は除外しない | 1 |
| 3b | `\?\?\s*\d` | 105 | 37 | 0 | — | 0 |
| 3c | `\|\|\s*\d` | 19 | 14 | 0 | — | 0 |
| 4 | `includes\(\|\.has\(\|startsWith\|endsWith\|match\(\|test\(` | 189 | 57 | 0 | — | 0 |
| 5 | `sort\(\|slice\(0\|Math\.(max\|min)\|reduce\(\|filter\(` | 320 | 79 | 0 | — | 0 |
| 6 | `推定\|経験\|目安\|典型\|typical\|guess\|heuristic\|likely\|probably\|assume\|みなす\|とみなし\|らしい\|疑い\|suspect\|warn\|bad\|ok\b\|healthy\|risk` | 384 | 66 | 0 | — | 50 |
| 7 | `timeout\|retry\|setTimeout\|setInterval\|interval` | 90 | 37 | 0 | — | 13 |

- 全パターンの和: 1418 行（i18n.ts 77 行を含む）。除外のあと 1 つでもパターンが残った行 = 1341 行（i18n.ts を除く）を分類した。
- 機械的な除外は「その行の、そのパターンの一致がすべて 0 / 1 との比較・単位換算」のときだけ。別のパターンにも
  当たる行は、そちらで分類の対象に残る。
- i18n.ts（訳文の表）の 77 行は、文言の中の語（"warn"・"ok"・"interval"・「みなす」等）と数で、画面の判断ではない。
  「みなす」の 1 行（i18n.ts:2916 linux.watchdogHint）は、Linux のエージェントの振る舞いの説明で画面の処理ではない。すべて D。
- 6 の `warn` / `bad` / `ok` の大半は CSS の色の名前（`class="bad"`・`class:warn`・`var(--warn)`）で D。
- 検索の限界: 検索語に当たらない書き方の判定（`some(…)` だけの判定・`!== "monitor"` 等）は拾えない。既知の一覧にあり
  検索に掛からなかったものは、表に「検索外」と書いた（H 1 項目と H8 の一部・C 16 項目）。

## 2. H と ? の全件

### H（ヒューリスティック）: 9 項目

| # | ファイル:行 | コードの抜き出し | 何をしているか | なぜ H か | 既知 |
|---|---|---|---|---|---|
| H1 | lib/flash.ts:133-138（使う所 FlashPanel.svelte:410） | `return f > 0 ? (p.elapsed_s * (1 - f)) / f : null;` | 書き込みの残り時間を「経過 × (1 − 進み) / 進み」で出す | 速さが一定という仮定に頼る推定（コメントも「段ごとの速さの違いは見ない目安」）。消去・書き込み・検証は速さが違う | c12 |
| H2 | lib/sampler.ts:34-41（使う所 SamplerNotice.svelte:15） | `const started = [...activity].reverse().find((e) => e.method === info.start && !e.error);` | DAP サンプラを使っている利用者を、操作の記録の「最後に成功した開始の操作」の利用者として見せる | 推定。記録の上限（5000 件）で落ちた・別の方法で始めた・後で別の者が止めて始め直した場合に、違う利用者を断定して出す（検索外） | c15 |
| H3 | lib/system.ts:34-46（hit 34・42） | `const d = (v - prev.memory[k] + 2 ** 32) % 2 ** 32;` | MCU のメモリの値の毎秒の増え方 | 「32 ビットの一周は前進とみなす」仮定。減ったカウンタ・カウンタでない値は約 4.29e9 の巨大な速さとして出る | c19 |
| H4 | lib/system.ts:105-126（hit 108・111・112・115・116・120）、SystemPanel.svelte:121・188 | `export const RATE_WINDOW_US = 2_000_000;` / `if (values.length < 2) return { count: 0, perSecond: nowUs != null ? 0 : null };` / `const COND_WINDOW = 5000;` | FPGA の条件の成立の回数と毎秒を「直近 2 秒」「最後の 5000 件」で数える。値が 2 つ未満なら 0 回/秒 | 暗黙の既定の窓（2 秒・5000 件）を使う側が渡していない。値が無いのを「変化なし（0）」とみなす仮定 | c20 |
| H5 | lib/system.ts:209-215（hit 214）、使う所 SystemPanel.svelte:252 | `return mine.size > 0 && running.length === mine.size && running.every((w) => w.address !== null && mine.has(key(w.name, w.address)));` | 動いている watch の組が表と同じ名前・番地なら「自分が前に始めたもの」として引き継ぎ、記録を止めるときに止める | 推定。名前・番地が一致するだけで所有を決め、他者（AI 等）が同じ組で始めた watch を止めうる | c22 |
| H6 | lib/system.ts:231-237（hit 233・234）、使う所 SystemPanel.svelte:263 | `return withPins && link === "eth" ? 200 : 1000;` | 記録のメモリの読みの速さを、ピン併用かつ Ethernet なら 200 回/秒、ほかは 1000 回/秒にする | 実機の経験の値（2026-10-21 の PYNQ の詰まり）で測り方を暗黙に決める。時刻の分解能（5 ms / 1 ms）が結果に効くのに、使う側が渡す引数でない | c23 |
| H7 | lib/eventlog.ts:175-191（hit 175・178・182・183・186）、使う所 EventLogPanel.svelte:253 | `const call = candidates.sort((x, y) => Math.abs(x.t - m.t) - Math.abs(y.t - m.t))[0];` | 調査の印を置いた利用者を、同じ本文の recording.mark の操作のうち時刻がいちばん近いものから推定する | 推定（時刻が近い = 置いた者という仮定）。フックの印（body.hook）は事実 | c26 |
| H8 | lib/state.ts:74-76、guard.ts:8（hit）、StatusLine.svelte:23・46 | `return !this.snap.control.paused && this.snap.clients.some((c) => c.role === "ai");` / `const working = $derived($app.clients.some((c) => c.role === "ai"));` | AI の利用者がつながっているだけで「AI working / AI が作業中」と表示し、操作の前に確かめを出す | 接続 = 作業中という仮定の判定。StatusLine は一時停止も見ない（state.ts は見る）。state.ts・StatusLine は検索外 | c28 |
| H9 | lib/target.ts:24-36（hit 25・31） | `if (hz >= 1e6 && hz <= 2e9) return { mhz: Math.round(hz / 1e4) / 100, source: "SystemCoreClock" };` | SystemCoreClock の値を 1 MHz〜2 GHz のときだけコアのクロックの既定に採る | 妥当な範囲という経験の境界値（暗黙の既定）で、読んだ値を採るか捨てるかを決める | c29 |

### ?（判断がつかない）: 11 項目

| # | ファイル:行 | コードの抜き出し | 何をしているか | 判断がつかない理由 | 既知 |
|---|---|---|---|---|---|
| ?1 | lib/interrupts.ts:83（式は 70） | `statsOf(groups, hz \|\| 1, t1 > t0 ? t1 - t0 : null)` | 割り込みの負荷 = 自分の時間 ÷（rtos の出来事の最初〜最後） | 分母の定義の選び方。記録の rtos の最初〜最後を観測の区間とみなしている（記録はその前後にも続きうる）。定義が画面に出ていれば事実の計算（C）、出ていなければ仮定 | c4 の一部 |
| ?2 | lib/interrupts.ts:88 | `const hz = r.clock_hz \|\| 1;` | profile.exceptions に clock_hz が無いと 1 で割る | コメントは「クロック数のまま」だが、欄は秒（`total`・`min` 等）として表に出る。単位の取り違えになるかは表の見出し次第 | 新 |
| ?3 | lib/flash.ts:33-38（hit 35・36） | `if (["elf", "axf", "out"].includes(ext)) return "elf"; … return ext === "bin" ? "bin" : "elf";` | ファイルの形式を拡張子で決め、知らない拡張子は elf とみなす（BIN だけ番地を送る） | 名前での判定・既定の仮定。デーモンが中身でも決めるので結果は誤らないかもしれない（番地の要否だけに効く） | 新 |
| ?4 | lib/flash.ts:41-48（hit 45・46） | `if (/^[0-9a-f]+$/i.test(s)) return parseInt(s, 16);` | Flash の番地・長さの欄で、数字だけの並びを 16 進とみなす | 入力の解釈の約束（J-Link の慣習）。人が 10 進のつもりで打つと別の番地になる。結果の判断ではないので O とも言える | 新（c31 と同じ型） |
| ?5 | lib/memory.ts:10-34（hit 7・12・13・14） | `if (/^\d+$/.test(s)) return parseInt(s, 16);` / `const bareHex = /^[0-9a-f]+$/i.test(s) ? parseInt(s, 16) : null;` | Memory の番地の欄: 数字だけは 16 進、それ以外は式 → 記号 → 素の 16 進の順に解釈 | ?4 と同じ。解釈の順は決まった規則だが、どれに当たったかを画面が言わない | c31 |
| ?6 | lib/logic.ts:27-36（hit 29） | `const CHANNEL_PARAMS = new Set(["channel", "scl", "sda", …, "enable"]);` / `values[p.name] = isChannelParam(p.name) && p.required ? String(ch++) : "";` | 解読器の設定の名前が集合に入れば「チャンネルの設定」とみなし、ch0, ch1… を順に初期値として入れる | 名前の集合での判定。la.decoders がチャンネルの設定かを申告していれば不要（申告の有無は未確認）。欄の初期値なので使う人が直せる | 新 |
| ?7 | lib/swotrace.ts:13・17 | `export const MAX_COMPARATORS = 4;` / `.filter((v) => v.expression.trim()).slice(0, MAX_COMPARATORS)` | データトレースの式を 4 つまでに切り、超えた分を黙って捨てる | DWT の比較器の数はコアで違う（Cortex-M0+ は 2 等）。決め打ちの数で黙って捨てる。デーモンが本当の数で断るなら害は小さい | 新 |
| ?8 | lib/profile.ts:58-81（hit 74。68 は C） | `const f = baseName(r.file);` … `if (out.lines.get(f)?.has(line)) continue;` | ソースの余白の当たりを、パスを捨てたファイル名（パスの最後）で突き合わせる | 同じ名前のファイルが別の場所にあると混ざる（名前が一意という仮定）。当たりの計算そのものは C（b7） | 新（b7 に付随） |
| ?9 | lib/commands.ts:78 | `blocked: (s) => connected(s) ?? (s.part?.startsWith("rp2040") ? null : "tip.noRescue")` | 品種名が rp2040 で始まるときだけ rescue のリセットを押せる | 機器の名前での判定（ケイパビリティでない）。経験則ではないが、CLAUDE.md の「画面は機器の名前で判断しない」に当たりうる | 新 |
| ?10 | lib/api.ts:30-38（hit 36・37） | `const sentences = plain.split("。").filter((s) => !TOOL.test(s) && !PARAM.test(s));` | 誤りの文から、道具の名前（`target_halt` 等）・`名前=値` の形を含む括弧と文を正規表現で外して人に見せる | 「この形の文は AI 向けの案内」という見込みで文を消す。誤りの本文の一部が消えうる（情報の欠け） | 新 |
| ?11 | components/panels/RtosObjectsPanel.svelte:54-60（hit 54-58・60） | `k.endsWith("queue") \|\| k === "k_msgq" ? "queue" : k.endsWith("semaphore") \|\| k === "k_sem" ? "semaphore" : …` | RTOS のオブジェクトの種類（デーモンの kind）を名前の末尾で queue / semaphore / mutex / timer / event_group / buffer / other に分け、タブと件数にする | 名前の末尾での分類（命名の経験則）。外れても「other」と「all」に出るので隠れはしない | c30 |

## 3. C（画面の中の計算）の全件: 61 項目

決まった計算（経験則・仮定に頼らない）だが、API の応答から画面が派生させて見せているもの。「検索外」は検索に掛からず、
既知の一覧から確かめたもの。

| # | ファイル:行 | 何を計算しているか | 既知 |
|---|---|---|---|
| C1 | lib/power.ts:65-78（検索外） | 電圧の窓の最小・最大・平均・回数・範囲外・未送信の数 | b1 |
| C2 | lib/power.ts:100-104（検索外） | 記録の電圧の最低点の位置 | b2 |
| C3 | lib/power.ts:114-134（hit 114）、PowerPanel.svelte:197 | 電圧の低下: 使う人が指定した電圧以下の区間・最低点・深さ・戻った時刻（既定の値は無い。指定しなければ出さない） | c1 |
| C4 | lib/power.ts:139-141（検索外） | live かつ over_range でないときだけ電圧を出す（ほかは「データなし」） | c2 |
| C5 | lib/power.ts:95 | 記録の電圧の列（kind mv を時刻順に、mV → V） | 新 |
| C6 | lib/callgraph.ts:43-49（hit 47・48） | 根の並び（reset → exception → user → uncalled → address_taken、同じ種類は深さの順）と重複の除去 | c3 |
| C7 | lib/callgraph.ts:52-69（hit 58・61） | 木の行・再帰の印・最深の経路の印 | b3 |
| C8 | lib/callgraph.ts:72-74（hit 73）、CallGraphPanel.svelte:40・65 | reset の最深の経路を最初から開く・「最深の経路を開く」 | c3 |
| C9 | CallGraphPanel.svelte:48-55・111（hit 111） | 呼び出しの木にプロファイルの関数ごとの割合を結合 | b4 |
| C10 | lib/interrupts.ts:37-54（hit 38・44・50） | 入れ子の割り込みの自分の時間 | b5 |
| C11 | lib/interrupts.ts:57-74（hit 66・73） | 割り込みごとの統計と、自分の時間の多い順の並べ替え（負荷の分母は ?1） | c4 |
| C12 | lib/interrupts.ts:96-121（hit 96・103・105・107・111・115・116・119） | 分布図: 棒 24 本（整数で種類が少なければ 1 値 1 本）、長い順の上位 10 回を含む棒の印。外れ値の閾値は使わない | c5 |
| C13 | lib/interrupts.ts:124-128（hit 127） | 棒の中の最長の回 | b6 |
| C14 | lib/fault.ts:43-45（hit 44） | 故障か = faulted、または IPSR が故障の例外（HardFault 等。ARMv7-M/v8-M の定義） | c6 |
| C15 | lib/fault.ts:55-69（hit 66） | 止まるたびに target.fault を読み、IPSR が故障の例外なら Fault の窓を開く | c7 |
| C16 | lib/fault.ts:89-94（hit 91） | 見出しの主な原因（MMARVALID・BFARVALID・FORCED を除く。仕様上これらは番地の有効・昇格の印） | c8 |
| C17 | lib/safety.ts:41-43・140-148（hit 41・42・143-146）、SafetyBar.svelte:14 | 止める前の警告: halt_risk.matched かつモニタ方式でない。ELF ごとに答えを覚える | c9 |
| C18 | lib/safety.ts:68-70（検索外）、SafetyBar.svelte:30 | tick_stopped を致命的の色にしない（ほかの fatal の kind は致命的） | c10 |
| C19 | lib/flash.ts:105-115（hit 109・113） | 結果の段の表・合計バイト | b9 |
| C20 | lib/flash.ts:126-131（hit 129・130） | 進みの帯: 消去・書き込み・検証を等分して 1 本に（帯の見せ方。残り時間の推定は H1） | c11 |
| C21 | lib/fpgaobs.ts:122-128（検索外）、FpgaObsPanel.svelte:178 | 観測の状態の文言の優先順（stuck > overflow > running > starting > off） | c13 |
| C22 | lib/activity.ts:48-51（検索外） | 時刻 → 直前の操作 | b12 |
| C23 | lib/rtos.ts:57-64（hit 62） | rtos.timeline.get の表を使用率順に | b16 |
| C24 | lib/rtos.ts:105-117（hit 111・112・116） | 記録のタスク表: 使用率の分母 = run の区間の合計、平均、使用率順 | c17 |
| C25 | lib/rtos.ts:119-201（hit 120-200 の 23 行） | 区間の和・差・重なり、窓ごとの負荷・割り込み・最大の窓・平均の負荷（アイドルは記録の印 idle で決める） | b13 |
| C26 | lib/rtos.ts:208-216（hit 209・211・213・216） | グラフのタスク: アイドル以外の上位 6（残りは「その他」）、「その他」を出す丸めの許容 1e-9 % | c18 |
| C27 | lib/rtos.ts:218-234（hit 222・227・229・230・231） | 負荷の積み上げの系列・「その他」の負荷 | b14 |
| C28 | lib/rtos.ts:238-253（検索外） | 時刻に動いていたタスク・次の実行 | b15 |
| C29 | lib/system.ts:128-135（hit 133）、timeline/rows.ts:180-195（hit 191-194） | 記録の縮図で同じ値だけのピンの ch を隠す・システム解析の段の並びと畳み | c21 |
| C30 | SystemPanel.svelte:53-54（検索外） | ピンの記録が止まった（la_stream の running・error・overrun） | c24 |
| C31 | SystemPanel.svelte:166-170（hit 168） | FPGA の scans の毎秒（32 ビットのカウンタの一周を足す。カウンタは回路の 32 ビット） | b17 |
| C32 | SystemPanel.svelte:174-175（検索外） | 記録中にメモリの読みが止まった | b18 |
| C33 | SystemPanel.svelte:398 | 遅れの分解能 = correlate の sampling の最大 | b19 |
| C34 | SystemPanel.svelte:403-404（hit 451・501） | MCU OK / FPGA OK の丸（connected かつ error なし / available かつ running かつ stuck でない） | c25 |
| C35 | lib/eventlog.ts:213-223（hit 216・218・221）、EventLogPanel.svelte:317 | stream・kind ごとの件数・割合・毎秒、件数の多い順 | b20 |
| C36 | lib/eventlog.ts:138-158（hit 145・152・156・157）、EventLogPanel.svelte:215 | 分布の棒の番号（デーモンと同じ分け方）・長い順の上位を含む棒の印・棒に入る区間 | b21 |
| C37 | lib/sampling.ts:130-196（検索外） | 観測値の最小・最大・平均・件数（走りながら） | b22 |
| C38 | lib/sampling.ts:246-261（検索外） | XY: a の各時刻に、その時刻までの b の最新の値を組む（サンプル・アンド・ホールド） | b23 |
| C39 | lib/sampling.ts:302-321（hit 307・310・311・313） | 取りこぼし率・実際の周期・FPGA で読む式とホストで読む式の分け | b24 |
| C40 | lib/sampling.ts:395-409（hit 398） | 2 つのカーソルの値（直前の値を保つ）・差・間の統計 | b25 |
| C41 | lib/watchshare.ts:27-40（hit 29・34・39） | 始める式の組・やめるときの残り・自分の式が外された | b26 |
| C42 | lib/logic.ts:74-79（hit 77・78） | 解読で隠れた種類と件数 | b27 |
| C43 | lib/logic.ts:153-159（検索外） | 多段トリガの段の状態（済み・いま・まだ） | b28 |
| C44 | lib/logic.ts:192-199（検索外）、LogicAnalyzerPanel.svelte:275・285・292-293 | 最後に成功した xtrig の設定の操作を「読み込む」ボタンに出す（だれの設定かを添える。今の設定とは言わない） | c27 |
| C45 | lib/logic.ts:208-212（hit 211） | 変化のあるチャンネルだけ | b29 |
| C46 | lib/terminal.ts:112-138・311-320（hit 113・114・121・122・127・138） | RTT の仮想端末ごとの分割・時刻 t の行 | b30 |
| C47 | lib/swotrace.ts:49-51（hit 50） | 例外の回数を多い順に | b31 |
| C48 | lib/breakpoints.ts:178-182（hit 181） | 未対応のベクタキャッチを覚えて足す | b32 |
| C49 | lib/timeline/query.ts:84-97（hit 85・90・93） | 全段の tone bad/warn の出来事の時刻を集めて縮図に印（tone はデーモンの印。上限 2000 で間引く） | b33 |
| C50 | lib/timeline/compose.ts:75-130（hit 102・130） | 段ごとの範囲内の件数・変化の数 | b34 |
| C51 | lib/timeline/overlay.ts:160-166（hit 164） | 選択 − 基準の Δ時間 | b35 |
| C52 | VarRow.svelte:38-43（検索外） | 前の停止から変わった変数の印 | b36 |
| C53 | RegistersPanel.svelte:20・73 | 前の停止から変わったレジスタの印 | b37 |
| C54 | ThreadsPanel.svelte:105 | スタックの使用率（peak_used か used ÷ size） | b38 |
| C55 | HeapPanel.svelte:19・68-69（検索外） | ヒープの使用率 | b39 |
| C56 | RtosObjectsPanel.svelte:72（検索外） | キューが満杯（free === 0）の表示 | b40 |
| C57 | CpuLoadPanel.svelte:92・137-140（hit 92） | 記録の負荷の窓（使う人の窓の幅）・最大の窓の印 | b41 |
| C58 | lib/profile.ts:57-81（hit 68） | ソースの余白の行の当たり・同じファイルの最大・当たらなかった行（突き合わせ方は ?8） | b7 |
| C59 | DashboardPanel.svelte:56 | 配置の式が全部サンプラで取られているか | 新 |
| C60 | DataSamplingPanel.svelte:70・73・138・139・398 | 自分の式のうち取られているもの・ほかの利用者の式・取られていない式を淡く | 新 |
| C61 | FaultPanel.svelte:38・210 | 故障で止めるベクタキャッチのうち有効なもの、まだ有効にできるものが残るか（総数 − 未対応） | 新 |

## 4. D・F・O

- D（見せ方）909 行: CSS・アイコンの図形 152 行、色の名前（`class="bad"`・`class:warn` 等）、文言のキー、
  16 進・単位・桁、描画（タイル・WebGL・2D の画素の計算・ラベルの間引き）、拡大縮小・目盛り、利用者の絞り込み
  （検索の欄・役割・誤りだけ）、表示の件数の上限で残りの数を出すもの（FlashPanel.svelte:463-470 の 16 件・
  SystemPanel.svelte:602-608 の 5 件）等。
- F（事実・仕様の規則）73 行、O（操作の既定）185 行。1 行ずつの列挙は下。O のうち「周期・待ち時間」と書いた
  行は、setInterval・setTimeout・`*_MS` の定数・retry の行（読みの周期・つなぎ直しの待ち）。
- 判断の境目に置いたもの（O にしたが性質の近いもの。確かめる人のために挙げる）:
  - SystemPanel.svelte:80-87: 深いバッファの捕捉を「標本の速さ ÷ 4・前を深さの半分」で始める（コメントは実機の
    事情）。捕捉の操作の既定で、結果の判断ではないので O。
  - DashboardPanel.svelte:27 の LIVE_RATE = 100 Hz、DataSamplingPanel.svelte:43 の 1000 Hz、CpuLoadPanel.svelte:32 の
    窓 10 ms（欄で選べる）: 測り方の既定。使う人が欄で変えられ、値は画面に出るので O。
  - lib/timeline/state.ts:539・SwoTracePanel.svelte:80・lib/terminal.ts:49-51: 誤りの文言を正規表現で照合して
    「まだ無い」状態・古いデーモンを見分ける。?10（誤りの文を消す）と違い、文を消さず状態の見分けに使うので O。
  - FpgaObsPanel.svelte:25・SystemPanel.svelte:87・341・TimelinePanel.svelte:35: 申告（sys_clock_hz・timebase_hz・
    la_sample_hz）が無いときの 100 MHz。古いデバイスの既定で O。
  - lib/fpgaobs.ts:146-149 の PRESETS（c14）: 値（10 MHz・64 語・20）は O、「標本型は TCK ≤ clk / 8」は回路の仕様で F。

## 5. 既知の 73 件の改めての分類

docs/reports/2026-10-23-ui-computations.md の番号 → この監査の分類と表の番号。

| 既知 | 新しい分類 | 表 | 一言 |
|---|---|---|---|
| c1 | C | C3 | 低下は使う人が指定した電圧だけ（既定の値・基準の推定は無くなっていた） |
| c2 | C | C4 | |
| c3 | C | C6・C8 | 種類の決まった順と深さの順。経験則ではない |
| c4 | C（分母は ?） | C11・?1 | |
| c5 | C | C12 | 棒の数 24・上位 10 は見せ方の数。外れ値の閾値は無い |
| c6 | C | C14 | 故障の例外の集合は ARM の定義（F）に基づく |
| c7 | C | C15 | |
| c8 | C | C16 | |
| c9 | C | C17 | |
| c10 | C | C18 | |
| c11 | C | C20 | |
| c12 | H | H1 | |
| c13 | C | C21 | |
| c14 | O（値）・F（clk/8） | §4 | |
| c15 | H | H2 | |
| c16 | F | — | "hook <id>" はデーモンの命名（host/fprobe/api/daemon.py:455）。名前での判定だが約束どおり |
| c17 | C | C24 | |
| c18 | C | C26 | |
| c19 | H | H3 | |
| c20 | H | H4 | |
| c21 | C | C29 | |
| c22 | H | H5 | |
| c23 | H | H6 | |
| c24 | C | C30 | |
| c25 | C | C34 | |
| c26 | H | H7 | |
| c27 | C | C44 | 「読み込む」ボタンで、今の設定とは言っていない |
| c28 | H | H8 | |
| c29 | H | H9 | |
| c30 | ? | ?11 | |
| c31 | ? | ?5 | |
| c32 | F | — | 0 以外を真は C 言語の約束（lib/dashboard.ts:264-266） |
| b1 | C | C1 | |
| b2 | C | C2 | |
| b3 | C | C7 | |
| b4 | C | C9 | |
| b5 | C | C10 | |
| b6 | C | C13 | |
| b7 | C（突き合わせは ?） | C58・?8 | |
| b8 | F | — | 原因のビット → レジスタは ARMv7-M の定義（lib/fault.ts:72-78） |
| b9 | C | C19 | |
| b10 | F | — | デーモン（fpga_signals.py）と同じ検査（lib/fpgaobs.ts:54-87） |
| b11 | F | — | TCK = sys / (2 × 半周期)（lib/fpgaobs.ts:90-94） |
| b12 | C | C22 | |
| b13 | C | C25 | |
| b14 | C | C27 | |
| b15 | C | C28 | |
| b16 | C | C23 | |
| b17 | C | C31 | |
| b18 | C | C32 | |
| b19 | C | C33 | |
| b20 | C | C35 | |
| b21 | C | C36 | |
| b22 | C | C37 | |
| b23 | C | C38 | |
| b24 | C | C39 | |
| b25 | C | C40 | |
| b26 | C | C41 | |
| b27 | C | C42 | |
| b28 | C | C43 | |
| b29 | C | C45 | |
| b30 | C | C46 | |
| b31 | C | C47 | |
| b32 | C | C48 | |
| b33 | C | C49 | |
| b34 | C | C50 | |
| b35 | C | C51 | |
| b36 | C | C52 | |
| b37 | C | C53 | |
| b38 | C | C54 | |
| b39 | C | C55 | |
| b40 | C | C56 | |
| b41 | C | C57 | |

合計: H 9・? 2・F 5・O 1・C 56。新しく見つけたもの: ? 8 項目（?2・?3・?4・?6・?7・?8・?9・?10）、
C 4 項目（C5・C59・C60・C61）。H の新しいものは無かった。

## 6. 読んだファイル・検索の対象

- 担当範囲のファイル: 133（`rg --files -g '*.ts' -g '*.svelte' -g '!*.test.ts'` で数えた）。
- 全 133 ファイルを §1 の 11 の検索の対象にした（rg をディレクトリ全体に掛けた）。どれかの検索に当たったファイルは
  118、当たらなかったファイルは 15: components/panels/DecoderFields.svelte・Empty.svelte・LocalsPanel.svelte・
  TabBar.svelte・TimeLink.svelte・VarTree.svelte・registry.ts、components/shell/DockArea.svelte、lib/context.ts・
  prefs.ts・sampler.ts・snapshots.ts・timeline/renderer.ts・timeline/worker.ts、main.ts。このうち lib/sampler.ts
  （H2）と lib/snapshots.ts は全文を読んだ。
- 全文を読んだファイル: 22（lib の power・callgraph・interrupts・fault・safety・flash・fpgaobs・sampler・activity・
  rtos・system・eventlog・logic・state・target・memory・watchshare・swotrace・dashboard・profile・sampling・
  snapshots、i18n は検索の行だけ）。
- 一部を読んだファイル: 19（lib/api.ts・commands.ts・waveform.ts・linux.ts・terminal.ts・breakpoints.ts・
  timeline/rows.ts・timeline/state.ts、components/panels の LogicAnalyzer・System・VarRow・CallGraph・Heap・
  RtosObjects・CpuLoad・DataSampling・Fault・Flash の各 Panel、components/shell/StatusLine.svelte）。
- 残り（92 ファイル。i18n.ts を含む）は検索の行だけを読んで分類した（1341 行すべてを 1 行ずつ分類。分類の一覧は §7）。
  検索に掛からない書き方の判定は、この 92 ファイルでは見落としうる（§1 の検索の限界）。

## 7. F・O の 1 行ずつの列挙と、ファイルごとの内訳

### F の列挙

- components/panels/FpgaObsPanel.svelte:144 — 組の番号 0〜255
- components/panels/LogicAnalyzerPanel.svelte:42 — 申告の無い古いデバイスの ch 数 10
- components/panels/LogicAnalyzerPanel.svelte:212 — 知っている経路の名前だけ
- components/panels/LogicAnalyzerPanel.svelte:213 — 知っている経路の名前だけ
- components/panels/LogicAnalyzerPanel.svelte:287 — 知っている経路の名前だけ
- components/panels/LogicAnalyzerPanel.svelte:288 — 知っている経路の名前だけ
- components/panels/LogicAnalyzerPanel.svelte:505 — 多段トリガは 4 段まで
- components/panels/PowerPanel.svelte:112 — 1 回 1000 件（返った数が少なければ終わり）
- components/panels/VarRow.svelte:16 — max_elements の上限 4096（debug.evaluate）
- components/panels/VarRow.svelte:100 — max_elements の上限 4096
- lib/activity.ts:14 — "hook <id>" はデーモンの命名（daemon.py:455）
- lib/breakpoints.ts:202 — ブレークポイントを変える操作の名前
- lib/dashboard.ts:99 — 知っている部品の種類だけ
- lib/debugger.ts:189 — ELF を変える操作の名前
- lib/debugger.ts:190 — ブレークポイントを変える操作の名前
- lib/debugger.ts:191 — 書く操作の名前
- lib/fault.ts:73 — CFSR/HFSR のビット → レジスタ（ARMv7-M）
- lib/fault.ts:74 — CFSR/HFSR のビット → レジスタ（ARMv7-M）
- lib/fault.ts:75 — CFSR/HFSR のビット → レジスタ（ARMv7-M）
- lib/fault.ts:76 — CFSR/HFSR のビット → レジスタ（ARMv7-M）
- lib/fpgaobs.ts:6 — 名前の長さの上限（fpga_signals.py と同じ）
- lib/fpgaobs.ts:7 — 観測の組の幅 16
- lib/fpgaobs.ts:8 — TCK の半周期の範囲（回路の仕様）
- lib/fpgaobs.ts:65 — 信号の定義の検査（デーモンと同じ規則）b10
- lib/fpgaobs.ts:66 — 信号の定義の検査（デーモンと同じ規則）b10
- lib/fpgaobs.ts:78 — 信号の定義の検査（デーモンと同じ規則）b10
- lib/fpgaobs.ts:92 — TCK → 半周期（TCK = sys / (2 × 半周期)）b11
- lib/fpgaobs.ts:145 — 標本型は TCK ≤ clk / 8（回路の仕様。コメント）c14
- lib/linux.ts:49 — ポート番号の範囲
- lib/linux.ts:181 — ポート番号の範囲
- lib/linux.ts:187 — events の形（API の仕様と同じ）
- lib/linux.ts:221 — pid の範囲
- lib/linux.ts:238 — pid の範囲
- lib/linux.ts:251 — duration_s ≤ 3600（probe_linux.py:463 と同じ）
- lib/linux.ts:258 — duration_s ≤ 600（probe_linux.py:482 と同じ）
- lib/logic.ts:119 — ピンの条件のビット
- lib/logic.ts:120 — ピンの条件のビット
- lib/logic.ts:121 — ピンの条件のビット
- lib/logic.ts:140 — 知っている事象の名前だけ
- lib/logic.ts:166 — カーソル間の範囲の終わり +1（la.measure の end は含まない）
- lib/memory.ts:39 — 単位の値の範囲（8/16/32 ビット）
- lib/memory.ts:41 — 単位の値の範囲（8/16/32 ビット）
- lib/memory.ts:65 — 文字の列で書けるのは ASCII 0x20〜0x7E
- lib/peripherals.ts:44 — フィールドの幅の範囲
- lib/peripherals.ts:52 — レジスタの大きさの範囲
- lib/safety.ts:121 — ELF を変える操作の名前
- lib/system.ts:15 — 番地の形（32 ビット・4 の倍数）
- lib/system.ts:17 — 番地の形（32 ビット・4 の倍数）
- lib/system.ts:23 — 保存の形の検査
- lib/system.ts:205 — 保存の形の検査
- lib/terminal.ts:157 — ANSI の色の番号（コメント）
- lib/terminal.ts:208 — ANSI のエスケープの構文
- lib/terminal.ts:228 — ANSI SGR 22
- lib/terminal.ts:229 — ANSI SGR の前景色 30〜37
- lib/terminal.ts:230 — ANSI SGR の前景色 90〜97
- lib/terminal.ts:231 — ANSI SGR 39
- lib/timeline/compose.ts:19 — 段の描き方の符号
- lib/timeline/frame.ts:12 — フレームの印（MAGIC）
- lib/timeline/frame.ts:15 — フレームの印（MAGIC）
- lib/timeline/state.ts:173 — 捕捉を変える操作の名前
- lib/timeline/state.ts:176 — 印を変える操作の名前
- lib/timeline/tile.ts:7 — 画素の状態の符号（変化）
- lib/timeline/tile.ts:8 — 画素の状態の符号（無し）
- lib/timeline/tile.ts:10 — ラベル無しの印 0xffffffff
- lib/timeline/tile.ts:43 — format 1 の段（stream 全体を 1 段）
- lib/timeline/tile.ts:136 — format 1 の段（stream 全体を 1 段）
- lib/vartree.ts:35 — 列挙の値の形（デーモンの {enum, value}）
- lib/vartree.ts:45 — 浮動小数の型名（C の型）
- lib/vartree.ts:107 — たどれるポインタ（void・関数ポインタ以外）
- lib/vartree.ts:134 — C の式の構文（括弧が要るか）
- lib/vartree.ts:175 — 無名のメンバーは式にしない
- lib/vartree.ts:188 — 無名のメンバーは式にしない
- lib/waveform.ts:74 — 申告の無い古いデバイスの並び（ch0〜7 ピン・8/9 内部）

### O の列挙

- components/Timeline.svelte:120 — ホイールのスクロールの速さ
- components/Timeline.svelte:164 — 4 画素未満の移動はクリック
- components/Timeline.svelte:187 — 矢印キーの移動量
- components/Timeline.svelte:190 — キーの拡大の倍率
- components/Timeline.svelte:191 — キーの縮小の倍率
- components/panels/ActivityPanel.svelte:17 — 読み直しの遅れ 800 ms
- components/panels/ActivityPanel.svelte:50 — 周期・待ち時間
- components/panels/ActivityPanel.svelte:57 — 周期・待ち時間
- components/panels/ActivityPanel.svelte:114 — 末尾から 8 画素なら追う
- components/panels/ActivityPanel.svelte:115 — 上端 40 画素で古い方を読む
- components/panels/BreakpointsPanel.svelte:78 — 周期・待ち時間
- components/panels/CpuLoadPanel.svelte:117 — hz が無ければ 1
- components/panels/CpuLoadPanel.svelte:188 — 周期・待ち時間
- components/panels/DashboardPanel.svelte:27 — 読みの周期・ライブの既定 100 Hz
- components/panels/DashboardPanel.svelte:92 — 周期・待ち時間
- components/panels/DashboardPanel.svelte:99 — 周期・待ち時間
- components/panels/DataSamplingPanel.svelte:33 — 読みの周期
- components/panels/DataSamplingPanel.svelte:43 — 周期の既定 1000 Hz
- components/panels/DataSamplingPanel.svelte:111 — 周期・待ち時間
- components/panels/DataSamplingPanel.svelte:129 — 周期・待ち時間
- components/panels/DataSamplingPanel.svelte:140 — 1 回に読む値の数（64〜4096）
- components/panels/FaultPanel.svelte:80 — 周期・待ち時間
- components/panels/FlashPanel.svelte:74 — 周期・待ち時間
- components/panels/FlashPanel.svelte:79 — 周期・待ち時間
- components/panels/FpgaObsPanel.svelte:16 — 周期・待ち時間
- components/panels/FpgaObsPanel.svelte:25 — sys_clock_hz が無ければ 100 MHz
- components/panels/FpgaObsPanel.svelte:29 — 接続の欄の既定
- components/panels/FpgaObsPanel.svelte:30 — 接続の欄の既定
- components/panels/FpgaObsPanel.svelte:31 — 接続の欄の既定
- components/panels/FpgaObsPanel.svelte:32 — 接続の欄の既定
- components/panels/FpgaObsPanel.svelte:33 — 接続の欄の既定
- components/panels/FpgaObsPanel.svelte:35 — 接続の欄の既定 TCK 10 MHz
- components/panels/FpgaObsPanel.svelte:36 — 接続の欄の既定 64 語
- components/panels/FpgaObsPanel.svelte:37 — 接続の欄の既定 印 20
- components/panels/FpgaObsPanel.svelte:61 — 周期・待ち時間
- components/panels/InterruptsPanel.svelte:37 — hz が無ければ 1
- components/panels/LinuxPanel.svelte:15 — 周期・待ち時間
- components/panels/LinuxPanel.svelte:25 — 周期・待ち時間
- components/panels/LinuxPanel.svelte:26 — 周期・待ち時間
- components/panels/LinuxPanel.svelte:52 — 時刻合わせの間隔の既定 1.0 秒
- components/panels/LinuxPanel.svelte:131 — 周期・待ち時間
- components/panels/LinuxPanel.svelte:250 — 周期・待ち時間
- components/panels/LinuxPanel.svelte:263 — 周期・待ち時間
- components/panels/LoadState.svelte:7 — 周期・待ち時間
- components/panels/LoadState.svelte:13 — 周期・待ち時間
- components/panels/LogPanel.svelte:11 — 周期・待ち時間
- components/panels/LogPanel.svelte:61 — 周期・待ち時間
- components/panels/LogicAnalyzerPanel.svelte:35 — 解読の件数の上限 2000
- components/panels/LogicAnalyzerPanel.svelte:36 — 読みの周期
- components/panels/LogicAnalyzerPanel.svelte:202 — 周期・待ち時間
- components/panels/LogicAnalyzerPanel.svelte:319 — 周期・待ち時間
- components/panels/MemoryPanel.svelte:16 — 1 画面の長さ 256 バイト
- components/panels/MemoryPanel.svelte:65 — 周期・待ち時間
- components/panels/MemoryPanel.svelte:102 — ページ送り
- components/panels/PeripheralsPanel.svelte:117 — 定義が無ければ 32 ビット
- components/panels/PeripheralsPanel.svelte:171 — 定義が無ければ 32 ビット
- components/panels/PeripheralsPanel.svelte:172 — 定義が無ければ 32 ビット
- components/panels/PowerPanel.svelte:21 — 読みの周期・記録の電圧は 20 回まで
- components/panels/PowerPanel.svelte:25 — 窓の既定 60 秒
- components/panels/PowerPanel.svelte:55 — 周期・待ち時間
- components/panels/ProfilePanel.svelte:24 — プロファイルの上位 500 行
- components/panels/ProfilePanel.svelte:137 — コアのクロック（欄の MHz を Hz に）
- components/panels/ProfilePanel.svelte:198 — 周期・待ち時間
- components/panels/RecordingsPanel.svelte:17 — 周期・待ち時間
- components/panels/RecordingsPanel.svelte:53 — 周期・待ち時間
- components/panels/RtosObjectsPanel.svelte:40 — 周期・待ち時間
- components/panels/SamplerNotice.svelte:24 — 周期・待ち時間
- components/panels/SwoTracePanel.svelte:20 — 1 回に読む件数 500
- components/panels/SwoTracePanel.svelte:21 — 周期・待ち時間
- components/panels/SwoTracePanel.svelte:50 — 周期・待ち時間
- components/panels/SwoTracePanel.svelte:80 — 「まだ設定していない」誤りは出さない（文言の照合）
- components/panels/SystemPanel.svelte:20 — 周期・待ち時間
- components/panels/SystemPanel.svelte:80 — 深いバッファの深さ
- components/panels/SystemPanel.svelte:81 — 捕捉の速さ・前後の拍の決め方（コメント）
- components/panels/SystemPanel.svelte:82 — 捕捉の速さ = 標本の速さ ÷ 4・前を半分
- components/panels/SystemPanel.svelte:87 — タイムベースが無ければ 100 MHz
- components/panels/SystemPanel.svelte:101 — トリガ後の秒の既定 1
- components/panels/SystemPanel.svelte:153 — 周期・待ち時間
- components/panels/SystemPanel.svelte:341 — タイムベースが無ければ 100 MHz
- components/panels/SystemPanel.svelte:370 — 遅れの既定 0
- components/panels/SystemPanel.svelte:383 — 使う人の遅れを秒に
- components/panels/TerminalPanel.svelte:27 — 読みの周期
- components/panels/TerminalPanel.svelte:82 — 周期・待ち時間
- components/panels/ThreadsPanel.svelte:45 — 周期・待ち時間
- components/panels/TimelinePanel.svelte:35 — 標本の速さが無ければ 100 MHz
- components/panels/TimelinePanel.svelte:38 — 捕捉の速さの既定（選択肢の 3 番目）
- components/shell/Dialogs.svelte:115 — パスか名前かの見分け
- lib/activity.ts:60 — 記録の操作の索引の上限 100000
- lib/api.ts:53 — 周期・待ち時間
- lib/api.ts:63 — つなぎ直しの待ち 1 秒
- lib/api.ts:64 — 周期・待ち時間
- lib/api.ts:124 — つなぎ直しの待ち 1 秒
- lib/api.ts:163 — つなぎ直しの待ち
- lib/api.ts:164 — つなぎ直しの待ち（倍・最大 10 秒）
- lib/apiform.ts:57 — API の窓に出さない（session.*・バイナリ）
- lib/breakpoints.ts:103 — 手元に持つログ 2000 件
- lib/commands.ts:126 — 入力欄の中で効くショートカット（F キー）
- lib/dashboard.ts:32 — 配置の上限（幅・高さ・桁・部品数）
- lib/dashboard.ts:92 — 部品数の上限
- lib/dashboard.ts:116 — 範囲が壊れていたら既定の幅
- lib/dashboard.ts:120 — 大きさ・桁を収める
- lib/dashboard.ts:237 — 大きさ・桁を収める
- lib/dashboard.ts:238 — 大きさ・桁を収める
- lib/dashboard.ts:239 — 大きさ・桁を収める
- lib/dashboard.ts:285 — スライダの刻み
- lib/dashboard.ts:286 — スライダの刻み
- lib/debugger.ts:58 — ライブウォッチの周期 1 秒
- lib/debugger.ts:174 — 周期・待ち時間
- lib/debugger.ts:215 — 周期・待ち時間
- lib/eventlog.ts:45 — 数の欄の読み方（10 進・0x）
- lib/eventlog.ts:53 — 欄の値の読み方
- lib/eventlog.ts:54 — 欄の値の読み方
- lib/flash.ts:22 — 覚えた設定の検査
- lib/flash.ts:189 — 読み出しの保存先の既定
- lib/flash.ts:190 — 読み出しの保存先の既定
- lib/fpgaobs.ts:108 — 接続の欄の既定（ir_len 4 等）
- lib/fpgaobs.ts:109 — 接続の欄の既定
- lib/fpgaobs.ts:148 — 観測の回路の型ごとの接続の既定 c14
- lib/linux.ts:64 — 番地の欄の形
- lib/linux.ts:94 — 周期・待ち時間
- lib/linux.ts:99 — 周期・待ち時間
- lib/linux.ts:115 — 数の欄の形
- lib/linux.ts:165 — 周期・待ち時間
- lib/linux.ts:171 — 周期・待ち時間
- lib/linux.ts:263 — 画面に持つ件数 5000
- lib/logic.ts:44 — 設定の欄の値の読み方
- lib/logic.ts:45 — 設定の欄の値の読み方
- lib/logic.ts:47 — 設定の欄の値の読み方
- lib/logic.ts:48 — 設定の欄の値の読み方
- lib/logic.ts:49 — 設定の欄の値の読み方
- lib/logic.ts:134 — 段の回数は 1 以上
- lib/logic.ts:141 — 段の回数の既定 1
- lib/logic.ts:173 — 範囲が無ければ全体
- lib/logview.ts:35 — 画面に持つ行 5000
- lib/logview.ts:37 — 1 回の log.read 200 件
- lib/peripherals.ts:42 — 書く値の読み方
- lib/peripherals.ts:43 — 書く値の読み方
- lib/peripherals.ts:50 — 書く値の読み方
- lib/poll.ts:26 — 周期・待ち時間
- lib/poll.ts:32 — 読み直しの最短 1 秒
- lib/poll.ts:33 — 読み直しの倍率 2
- lib/poll.ts:34 — 読み直しの最長 10 秒
- lib/poll.ts:100 — 読み直しの待ち
- lib/poll.ts:101 — 周期・待ち時間
- lib/power.ts:31 — 電圧の窓の既定 60 秒
- lib/recordings.ts:36 — 最大の大きさ（MB → バイト、1 以上）
- lib/rtos.ts:14 — 記録から読む区間の上限 100000
- lib/sampling.ts:19 — 周期の選択肢
- lib/sampling.ts:70 — 式ごとに画面に残す点 50000
- lib/screen.ts:133 — 周期・待ち時間
- lib/screen.ts:134 — 周期・待ち時間
- lib/screen.ts:139 — 画面の状態を送る遅れ 250 ms
- lib/screen.ts:251 — 周期・待ち時間
- lib/screen.ts:290 — 周期・待ち時間
- lib/screen.ts:371 — 先の範囲を待つ 5000 ms
- lib/state.ts:42 — 操作の記録を 100 件ずつ読む
- lib/state.ts:43 — 手元に持つ操作の記録 5000 件
- lib/swotrace.ts:39 — 終わりの limit 件を読む
- lib/system.ts:65 — 条件の書き方の読み方
- lib/system.ts:219 — 変数名に見える文字か（入力の解釈）
- lib/terminal.ts:49 — 古いデーモンの見分け（誤りの文・-32601）
- lib/terminal.ts:51 — 古いデーモンの見分け（誤りの文）
- lib/terminal.ts:90 — RTT の既定の ch 0
- lib/terminal.ts:99 — RTT の既定の ch 0
- lib/terminal.ts:188 — 端末の画面に持つ文字 200000
- lib/timeline/state.ts:102 — タイルの上限 4
- lib/timeline/state.ts:103 — 周期・待ち時間
- lib/timeline/state.ts:152 — 周期・待ち時間
- lib/timeline/state.ts:214 — タイルの幅の上限 8192 画素
- lib/timeline/state.ts:222 — タイルの幅の上限 8192 画素
- lib/timeline/state.ts:365 — 拾う範囲 4 画素
- lib/timeline/state.ts:367 — 拾う範囲 4 画素
- lib/timeline/state.ts:418 — 深いバッファの捕捉の前後 4096 拍の既定
- lib/timeline/state.ts:419 — 深いバッファの捕捉の前後 4096 拍の既定
- lib/timeline/state.ts:539 — 「記録が無い」等の文言の誤りは出さない（文言の照合）
- lib/timeline/state.ts:594 — 周期・待ち時間
- lib/timeline/state.ts:610 — 重い記録は読みの間を空ける
- lib/ui.ts:19 — 最近のファイル 8 件
- lib/ui.ts:24 — 最近のファイル 8 件
- lib/ui.ts:72 — 最近のファイル 8 件
- lib/vartree.ts:204 — 書く値の読み方
- lib/vartree.ts:205 — 書く値の読み方
- lib/vartree.ts:206 — 書く値の読み方
- lib/vartree.ts:207 — 書く値の読み方
- lib/waveform.ts:60 — ホイールの拡大の速さ

### H・C・? の検索の行（表の項目との対応の確かめ用）


H: components/panels/SystemPanel.svelte:121, lib/eventlog.ts:175, lib/eventlog.ts:178, lib/eventlog.ts:182, lib/eventlog.ts:183, lib/eventlog.ts:186, lib/flash.ts:133, lib/guard.ts:8, lib/system.ts:34, lib/system.ts:42, lib/system.ts:108, lib/system.ts:111, lib/system.ts:112, lib/system.ts:115, lib/system.ts:116, lib/system.ts:120, lib/system.ts:214, lib/system.ts:233, lib/system.ts:234, lib/target.ts:25, lib/target.ts:31

?: components/panels/RtosObjectsPanel.svelte:54, components/panels/RtosObjectsPanel.svelte:55, components/panels/RtosObjectsPanel.svelte:56, components/panels/RtosObjectsPanel.svelte:57, components/panels/RtosObjectsPanel.svelte:58, components/panels/RtosObjectsPanel.svelte:60, lib/api.ts:36, lib/api.ts:37, lib/commands.ts:78, lib/flash.ts:35, lib/flash.ts:36, lib/flash.ts:45, lib/flash.ts:46, lib/interrupts.ts:83, lib/interrupts.ts:88, lib/logic.ts:29, lib/memory.ts:7, lib/memory.ts:12, lib/memory.ts:13, lib/memory.ts:14, lib/profile.ts:74, lib/swotrace.ts:13, lib/swotrace.ts:17

C: components/panels/CallGraphPanel.svelte:40, components/panels/CallGraphPanel.svelte:65, components/panels/CallGraphPanel.svelte:111, components/panels/CpuLoadPanel.svelte:92, components/panels/DashboardPanel.svelte:56, components/panels/DataSamplingPanel.svelte:70, components/panels/DataSamplingPanel.svelte:73, components/panels/DataSamplingPanel.svelte:138, components/panels/DataSamplingPanel.svelte:139, components/panels/DataSamplingPanel.svelte:398, components/panels/EventLogPanel.svelte:215, components/panels/EventLogPanel.svelte:317, components/panels/FaultPanel.svelte:38, components/panels/FaultPanel.svelte:210, components/panels/RegistersPanel.svelte:20, components/panels/RegistersPanel.svelte:73, components/panels/SystemPanel.svelte:168, components/panels/SystemPanel.svelte:398, components/panels/SystemPanel.svelte:451, components/panels/SystemPanel.svelte:501, components/panels/ThreadsPanel.svelte:105, components/shell/SafetyBar.svelte:14, components/shell/SafetyBar.svelte:30, lib/breakpoints.ts:181, lib/callgraph.ts:47, lib/callgraph.ts:48, lib/callgraph.ts:58, lib/callgraph.ts:61, lib/callgraph.ts:73, lib/eventlog.ts:145, lib/eventlog.ts:152, lib/eventlog.ts:156, lib/eventlog.ts:157, lib/eventlog.ts:216, lib/eventlog.ts:218, lib/eventlog.ts:221, lib/fault.ts:44, lib/fault.ts:66, lib/fault.ts:91, lib/flash.ts:109, lib/flash.ts:113, lib/flash.ts:129, lib/flash.ts:130, lib/interrupts.ts:38, lib/interrupts.ts:44, lib/interrupts.ts:50, lib/interrupts.ts:66, lib/interrupts.ts:73, lib/interrupts.ts:96, lib/interrupts.ts:103, lib/interrupts.ts:105, lib/interrupts.ts:107, lib/interrupts.ts:111, lib/interrupts.ts:115, lib/interrupts.ts:116, lib/interrupts.ts:119, lib/interrupts.ts:127, lib/logic.ts:77, lib/logic.ts:78, lib/logic.ts:211, lib/power.ts:95, lib/power.ts:114, lib/profile.ts:68, lib/rtos.ts:62, lib/rtos.ts:111, lib/rtos.ts:112, lib/rtos.ts:116, lib/rtos.ts:120, lib/rtos.ts:121, lib/rtos.ts:125, lib/rtos.ts:138, lib/rtos.ts:139, lib/rtos.ts:140, lib/rtos.ts:156, lib/rtos.ts:168, lib/rtos.ts:169, lib/rtos.ts:175, lib/rtos.ts:177, lib/rtos.ts:179, lib/rtos.ts:180, lib/rtos.ts:181, lib/rtos.ts:185, lib/rtos.ts:190, lib/rtos.ts:191, lib/rtos.ts:194, lib/rtos.ts:196, lib/rtos.ts:198, lib/rtos.ts:199, lib/rtos.ts:200, lib/rtos.ts:209, lib/rtos.ts:211, lib/rtos.ts:213, lib/rtos.ts:216, lib/rtos.ts:222, lib/rtos.ts:227, lib/rtos.ts:229, lib/rtos.ts:230, lib/rtos.ts:231, lib/safety.ts:41, lib/safety.ts:42, lib/safety.ts:143, lib/safety.ts:144, lib/safety.ts:145, lib/safety.ts:146, lib/sampling.ts:307, lib/sampling.ts:310, lib/sampling.ts:311, lib/sampling.ts:313, lib/sampling.ts:398, lib/swotrace.ts:50, lib/system.ts:133, lib/terminal.ts:113, lib/terminal.ts:114, lib/terminal.ts:121, lib/terminal.ts:122, lib/terminal.ts:127, lib/terminal.ts:138, lib/timeline/compose.ts:102, lib/timeline/compose.ts:130, lib/timeline/overlay.ts:164, lib/timeline/query.ts:85, lib/timeline/query.ts:90, lib/timeline/query.ts:93, lib/timeline/rows.ts:191, lib/timeline/rows.ts:192, lib/timeline/rows.ts:193, lib/timeline/rows.ts:194, lib/watchshare.ts:29, lib/watchshare.ts:34, lib/watchshare.ts:39

### ファイルごとの内訳

| ファイル | D | F | O | C | H | ? |
|---|---|---|---|---|---|---|
| App.svelte | 2 | 0 | 0 | 0 | 0 | 0 |
| components/Icon.svelte | 10 | 0 | 0 | 0 | 0 | 0 |
| components/Timeline.svelte | 12 | 0 | 5 | 0 | 0 | 0 |
| components/TimelineMinimap.svelte | 11 | 0 | 0 | 0 | 0 | 0 |
| components/TimelineRows.svelte | 2 | 0 | 0 | 0 | 0 | 0 |
| components/charts/Chart.svelte | 34 | 0 | 0 | 0 | 0 | 0 |
| components/panels/ActivityPanel.svelte | 7 | 0 | 5 | 0 | 0 | 0 |
| components/panels/ApiPanel.svelte | 8 | 0 | 0 | 0 | 0 | 0 |
| components/panels/BreakpointsPanel.svelte | 11 | 0 | 1 | 0 | 0 | 0 |
| components/panels/CallGraphPanel.svelte | 7 | 0 | 0 | 3 | 0 | 0 |
| components/panels/CallStackPanel.svelte | 2 | 0 | 0 | 0 | 0 | 0 |
| components/panels/ConditionFields.svelte | 1 | 0 | 0 | 0 | 0 | 0 |
| components/panels/CpuLoadPanel.svelte | 6 | 0 | 2 | 1 | 0 | 0 |
| components/panels/DashboardPanel.svelte | 9 | 0 | 3 | 1 | 0 | 0 |
| components/panels/DataSamplingPanel.svelte | 20 | 0 | 5 | 5 | 0 | 0 |
| components/panels/DisassemblyPanel.svelte | 10 | 0 | 0 | 0 | 0 | 0 |
| components/panels/EventLogPanel.svelte | 13 | 0 | 0 | 2 | 0 | 0 |
| components/panels/FaultPanel.svelte | 14 | 0 | 1 | 2 | 0 | 0 |
| components/panels/FavoritesPanel.svelte | 5 | 0 | 0 | 0 | 0 | 0 |
| components/panels/FlashPanel.svelte | 17 | 0 | 2 | 0 | 0 | 0 |
| components/panels/FpgaObsPanel.svelte | 13 | 1 | 11 | 0 | 0 | 0 |
| components/panels/HeapPanel.svelte | 13 | 0 | 0 | 0 | 0 | 0 |
| components/panels/InterruptsPanel.svelte | 0 | 0 | 1 | 0 | 0 | 0 |
| components/panels/LinuxPanel.svelte | 15 | 0 | 7 | 0 | 0 | 0 |
| components/panels/LoadState.svelte | 1 | 0 | 2 | 0 | 0 | 0 |
| components/panels/LogPanel.svelte | 5 | 0 | 2 | 0 | 0 | 0 |
| components/panels/LogicAnalyzerPanel.svelte | 28 | 6 | 4 | 0 | 0 | 0 |
| components/panels/MemoryPanel.svelte | 8 | 0 | 3 | 0 | 0 | 0 |
| components/panels/PeripheralsPanel.svelte | 10 | 0 | 3 | 0 | 0 | 0 |
| components/panels/PowerPanel.svelte | 16 | 1 | 3 | 0 | 0 | 0 |
| components/panels/ProfilePanel.svelte | 5 | 0 | 3 | 0 | 0 | 0 |
| components/panels/RecordingsPanel.svelte | 15 | 0 | 2 | 0 | 0 | 0 |
| components/panels/RegistersPanel.svelte | 3 | 0 | 0 | 2 | 0 | 0 |
| components/panels/RtosObjectsPanel.svelte | 12 | 0 | 1 | 0 | 0 | 6 |
| components/panels/SamplerNotice.svelte | 1 | 0 | 1 | 0 | 0 | 0 |
| components/panels/SourcePanel.svelte | 20 | 0 | 0 | 0 | 0 | 0 |
| components/panels/SwoTracePanel.svelte | 5 | 0 | 4 | 0 | 0 | 0 |
| components/panels/SystemPanel.svelte | 46 | 0 | 10 | 4 | 1 | 0 |
| components/panels/TerminalPanel.svelte | 12 | 0 | 2 | 0 | 0 | 0 |
| components/panels/ThreadsPanel.svelte | 17 | 0 | 1 | 1 | 0 | 0 |
| components/panels/TimelinePanel.svelte | 10 | 0 | 2 | 0 | 0 | 0 |
| components/panels/VarRow.svelte | 7 | 2 | 0 | 0 | 0 | 0 |
| components/panels/WatchPanel.svelte | 1 | 0 | 0 | 0 | 0 | 0 |
| components/shell/ConfirmDialog.svelte | 7 | 0 | 0 | 0 | 0 | 0 |
| components/shell/DebugToolbar.svelte | 4 | 0 | 0 | 0 | 0 | 0 |
| components/shell/Dialogs.svelte | 20 | 0 | 1 | 0 | 0 | 0 |
| components/shell/MenuBar.svelte | 1 | 0 | 0 | 0 | 0 | 0 |
| components/shell/SafetyBar.svelte | 13 | 0 | 0 | 2 | 0 | 0 |
| components/shell/StatusLine.svelte | 6 | 0 | 0 | 0 | 0 | 0 |
| lib/activity.ts | 5 | 1 | 1 | 0 | 0 | 0 |
| lib/api.ts | 3 | 0 | 6 | 0 | 0 | 2 |
| lib/apicall.ts | 5 | 0 | 0 | 0 | 0 | 0 |
| lib/apiform.ts | 12 | 0 | 1 | 0 | 0 | 0 |
| lib/apispec.ts | 1 | 0 | 0 | 0 | 0 | 0 |
| lib/breakpoints.ts | 7 | 1 | 1 | 1 | 0 | 0 |
| lib/callgraph.ts | 0 | 0 | 0 | 5 | 0 | 0 |
| lib/chart.ts | 9 | 0 | 0 | 0 | 0 | 0 |
| lib/commands.ts | 1 | 0 | 1 | 0 | 0 | 1 |
| lib/confirm.ts | 4 | 0 | 0 | 0 | 0 | 0 |
| lib/dashboard.ts | 7 | 1 | 9 | 0 | 0 | 0 |
| lib/debugger.ts | 6 | 3 | 3 | 0 | 0 | 0 |
| lib/diagnose.ts | 2 | 0 | 0 | 0 | 0 | 0 |
| lib/etm.ts | 2 | 0 | 0 | 0 | 0 | 0 |
| lib/eventlog.ts | 6 | 0 | 3 | 7 | 5 | 0 |
| lib/execution.ts | 1 | 0 | 0 | 0 | 0 | 0 |
| lib/fault.ts | 1 | 4 | 0 | 3 | 0 | 0 |
| lib/favorites.ts | 2 | 0 | 0 | 0 | 0 | 0 |
| lib/flash.ts | 6 | 0 | 3 | 4 | 1 | 4 |
| lib/format.ts | 5 | 0 | 0 | 0 | 0 | 0 |
| lib/fpgaobs.ts | 8 | 8 | 3 | 0 | 0 | 0 |
| lib/guard.ts | 0 | 0 | 0 | 0 | 1 | 0 |
| lib/heap.ts | 1 | 0 | 0 | 0 | 0 | 0 |
| lib/interrupts.ts | 0 | 0 | 0 | 14 | 0 | 2 |
| lib/linux.ts | 10 | 7 | 7 | 0 | 0 | 0 |
| lib/logic.ts | 3 | 5 | 8 | 3 | 0 | 1 |
| lib/logview.ts | 6 | 0 | 2 | 0 | 0 | 0 |
| lib/memory.ts | 2 | 3 | 0 | 0 | 0 | 4 |
| lib/notify.ts | 1 | 0 | 0 | 0 | 0 | 0 |
| lib/peripherals.ts | 0 | 2 | 3 | 0 | 0 | 0 |
| lib/poll.ts | 0 | 0 | 6 | 0 | 0 | 0 |
| lib/power.ts | 0 | 0 | 1 | 2 | 0 | 0 |
| lib/profile.ts | 2 | 0 | 0 | 1 | 0 | 1 |
| lib/protection.ts | 1 | 0 | 0 | 0 | 0 | 0 |
| lib/recordings.ts | 0 | 0 | 1 | 0 | 0 | 0 |
| lib/rtos.ts | 4 | 0 | 1 | 35 | 0 | 0 |
| lib/safety.ts | 11 | 1 | 0 | 6 | 0 | 0 |
| lib/sampling.ts | 7 | 0 | 2 | 5 | 0 | 0 |
| lib/screen.ts | 12 | 0 | 6 | 0 | 0 | 0 |
| lib/stack.ts | 2 | 0 | 0 | 0 | 0 | 0 |
| lib/state.ts | 1 | 0 | 2 | 0 | 0 | 0 |
| lib/swotrace.ts | 0 | 0 | 1 | 1 | 0 | 2 |
| lib/system.ts | 4 | 4 | 2 | 1 | 11 | 0 |
| lib/target.ts | 2 | 0 | 0 | 0 | 2 | 0 |
| lib/terminal.ts | 6 | 6 | 5 | 6 | 0 | 0 |
| lib/time.ts | 8 | 0 | 0 | 0 | 0 | 0 |
| lib/timeline/compose.ts | 35 | 1 | 0 | 2 | 0 | 0 |
| lib/timeline/cursor.ts | 1 | 0 | 0 | 0 | 0 | 0 |
| lib/timeline/decimate.ts | 19 | 0 | 0 | 0 | 0 | 0 |
| lib/timeline/decimator.ts | 1 | 0 | 0 | 0 | 0 | 0 |
| lib/timeline/frame.ts | 0 | 2 | 0 | 0 | 0 | 0 |
| lib/timeline/layout.ts | 12 | 0 | 0 | 0 | 0 | 0 |
| lib/timeline/overlay.ts | 28 | 0 | 0 | 1 | 0 | 0 |
| lib/timeline/query.ts | 2 | 0 | 0 | 3 | 0 | 0 |
| lib/timeline/render-2d.ts | 25 | 0 | 0 | 0 | 0 | 0 |
| lib/timeline/render-gl.ts | 37 | 0 | 0 | 0 | 0 | 0 |
| lib/timeline/rows.ts | 16 | 0 | 0 | 4 | 0 | 0 |
| lib/timeline/state.ts | 21 | 2 | 12 | 0 | 0 | 0 |
| lib/timeline/tile.ts | 6 | 5 | 0 | 0 | 0 | 0 |
| lib/timeline/titles.ts | 1 | 0 | 0 | 0 | 0 | 0 |
| lib/ui.ts | 2 | 0 | 3 | 0 | 0 | 0 |
| lib/vartree.ts | 4 | 6 | 4 | 0 | 0 | 0 |
| lib/watchshare.ts | 1 | 0 | 0 | 3 | 0 | 0 |
| lib/waveform.ts | 7 | 1 | 1 | 0 | 0 | 0 |
