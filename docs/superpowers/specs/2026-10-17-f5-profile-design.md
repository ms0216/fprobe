# F5: 性能解析（PC サンプリングのプロファイル・割り込みの時間・2 点間の時間・カバレッジ）設計

日付: 2026-10-17 / 担当 G / ロードマップ: [2026-10-17-feature-expansion-roadmap.md](2026-10-17-feature-expansion-roadmap.md) の F5

比較の根拠: [J-Link 周辺アプリとの比較](../../reports/2026-10-17-jlink-comparison.md) の 4 節（関数ごとの実行時間・2 点間の
処理時間とばらつき・コードカバレッジがすべて ×）と、[UI/UX 調査](../../reports/2026-10-17-debug-ui-ux-survey.md) の
Ozone（Code Profile・Timeline）、IAR（Function Profiler）、Streamline（関数 → ソースへの掘り下げ）、
Tracealyzer（Timing Metrics: 統計と生データの往復）、DT+（2 点間の時間・C0 カバレッジ）。

## 0. 目的と範囲

追加のハードなしで（SWD と SWO だけで）、次を出す。

1. **PC サンプリングのプロファイル**: 関数・行・命令ごとの「自分の時間」の割合。眠り（WFI / WFE）と割り込みを分ける。
   割合には **サンプル数と信頼区間** を必ず付ける（サンプリングの誤差を隠さない。AI が「差がある」と言い過ぎないため）。
2. **割り込みごとの時間**: 例外トレース（入る・出る・戻る）から、割り込みごとの回数・時間（含む／自分）・最大・ばらつき。
3. **2 点間の時間とばらつき**: 共通の時間軸の記録の出来事の組（開始 → 終了）から、一覧・統計・分布・外れ値（その時刻へ跳べる）。
4. **カバレッジ（近似）**: PC サンプルが当たった命令・行から、関数ごとの「実行された行の割合」。

範囲外: 命令トレース（ETM）による正確なカバレッジ・呼び出しグラフ（呼び出し元を含む時間はスタックの巻き戻しが要り、
PC サンプリングでは得られない。F6 の RTOS・DWARF の巻き戻しと組むのは将来）。実機の設定（メモリへの書き込み）・API・MCP
（本流で組み込む。8 節に提案）。

## 1. 部品と責務

すべて `host/fprobe/profile/` の下。入出力を持たない純粋な計算（ELF・逆アセンブルは読む）。

| モジュール | 責務 | 入力 | 出力 |
|---|---|---|---|
| `stats.py` | 割合の信頼区間（Wilson）、時間の統計・分布・外れ値 | 数 | 値・`Distribution` |
| `dwt.py` | DWT / ITM / TPIU の設定値の計算、SWO の帯域の見積もり、DWT_PCSR 方式の計画 | クロック・目標のサンプル率・SWO の速度 | `SamplingPlan`（書く番地と値の一覧） |
| `samples.py` | PC サンプルの取り込み（ITM のパケット / DWT_PCSR の値・記録の出来事）を共通の形へ | パケット・値・`Event` | `PcSamples` |
| `pcprofile.py` | 関数・行・命令への割り当て、眠り・割り込みの区別、割合と信頼区間 | `PcSamples`、`Symbols`、`Disassembler` | `Profile` |
| `exceptions.py` | 例外トレースから割り込みごとの回数・時間 | 時刻付きの ITM のパケット | `ExceptionProfile` |
| `timing.py` | 2 点間の時間の組と統計（`Recording.intervals` を使う） | `Recording` + 条件、または時刻の列 | `IntervalReport` |
| `coverage.py` | サンプルが当たった命令・行から関数ごとの実行された行の割合（近似） | `Profile` / `PcSamples`、`Disassembler` | `Coverage` |

既存の部品: `fprobe.itm`（パケットの解読・`Timestamper`）、`fprobe.symbols`（番地 → 関数・ファイル:行）、
`fprobe.disasm`（関数の命令の範囲・WFI の場所・命令の文字列）、`fprobe.recording`（`Recording.find` / `intervals`）。
これらは変えない。

## 2. PC サンプルの 2 つの取り方

### 2.1 SWO（DWT の PC サンプリング → ITM のパケット）

DWT のサイクル・カウンタ（CYCCNT）のビット 6（CYCTAP=0）か 10（CYCTAP=1）が変わるたびに POSTCNT を 1 減らし、
0 になったら PC サンプルのパケットを出して POSTPRESET を入れ直す。よって **サンプルの間隔 = (POSTPRESET + 1) × (64 か 1024)
サイクル**（64〜16384 サイクル）。パケット: 眠っている間は 2 バイト（`PcSample(pc=None)`）、それ以外は 5 バイト。

ARMv7-M（M3 / M4 / M7）・ARMv8-M Mainline のみ。M0 / M0+ には ITM が無い。

### 2.2 DAP サンプラで DWT_PCSR（0xE000101C）を読む

F4 の DAP サンプラ（`fprobe.sampler`）に 0xE000101C の 1 語を周期で読ませる。SWO を使わないので **ITM の無い Cortex-M0+
（RP2040 等）でも使える**（ARMv6-M の DWT にも PCSR がある。実装されているかはチップ次第で、無ければ 0 か 0xFFFFFFFF）。
値は記録の stream `sample`（a = 0xE000101C、b = PC）に入る。

- 0xFFFFFFFF: コアが止まっている（デバッグ状態）か、サンプルできない → **「取れなかった」として数から外す**（別に数える）。
- 眠りは PC が WFI / WFE（またはその直後の命令）を指すことで分かる（逆アセンブルで場所を求める）。
- 周期は DAP の命令の境界で読むので **SWO より遅い**（数 kHz〜数十 kHz の見込み）。デバッグと同時に使える。

### 2.3 共通の形 `PcSamples`

```
PcSample(pc: int | None, t: int | None, exception: int | None)
   pc=None は眠り（ITM の眠りのパケット）。exception は例外トレースから分かる、そのときに処理中の例外番号（0 = スレッド）。
PcSamples(samples: tuple[PcSample, ...], source: "itm" | "pcsr" | "list", unavailable: int, overflows: int,
          exception_trace: bool)
```

- `PcSamples.from_pcs(pcs)`: 番地の列（None = 眠り）から（試験・ほかの取り方の入口）。

- `PcSamples.from_itm(packets)`: `Packet` でも `TimedPacket` でもよい。`ExceptionTrace` があれば、入る・出る・戻るで
  処理中の例外を追い、各サンプルに付ける（例外トレースが無ければ `exception=None`）。`Overflow` は数える（失われた
  サンプルがある = 割合が偏る恐れ）と同時に例外の状態を捨てる（どこにいるか分からなくなるため）。
- `PcSamples.from_pcsr(values)`: 整数の列か、記録の `Event`（stream "sample"、a = 0xE000101C だけを使う）の列。

## 3. プロファイル（`Profile`）

`Profiler(symbols, disassembler=None).profile(samples, *, confidence=0.95) -> Profile`

各サンプルを次のどれかに分ける（`category`）:

| 分類 | 条件 |
|---|---|
| `sleep` | `pc=None`（ITM の眠り）か、PC が WFI / WFE 命令かその直後（逆アセンブラがあるとき） |
| `code` | 関数に当たった |
| `unknown` | どの関数にも当たらない（ROM・ブートローダ・シンボルの無いところ） |

さらに、例外トレースがあれば `exception`（0 = スレッド、それ以外 = 例外番号）で集計する（`by_exception`）。

**集計**: 関数・行（ファイル:行）・命令（番地）ごとのサンプル数。割合の分母は全サンプル（眠り・不明を含む）。
「起きている時間の中の割合」も `active_fraction` として出す（分母 = 眠り以外）。

**信頼区間**: サンプルを独立な試行とみなす二項の割合の **Wilson の区間**（サンプルが少ない・割合が 0 や 1 に近いときも
範囲の外へ出ない）。既定 95%。注意（仕様として明記し、結果の `notes` にも入れる）:

- PC サンプリングは周期的なので、**サンプルの周期と同期したループでは偏る**（エイリアシング）。区間はこの偏りを含まない。
- SWO のオーバーフロー（`overflows > 0`）があると、パケットの多い区間のサンプルが失われ偏る。
- PCSR 方式は DAP の命令の境界で読むので、周期がゆらぐ（偏りは小さくなる方向）。

**結果の形**（すべて `as_dict()` で JSON にできる。`top(n, by=...)` で並べ替えて上位 N）:

```
Profile.as_dict() = {
  "total": 1000, "unavailable": 0, "overflows": 0, "source": "itm", "confidence": 0.95,
  "categories": {"code": 900, "sleep": 80, "unknown": 20},
  "functions": [{"function": "tick", "address": ..., "size": 36, "samples": 450, "fraction": 0.45,
                 "ci": [0.419, 0.481], "active_fraction": 0.489}, ...],          # サンプル数の多い順
  "lines": [{"file": "main.c", "line": 17, "function": "tick", "samples": ..., "fraction": ..., "ci": [...]}, ...],
  "instructions": [{"address": ..., "function": ..., "offset": ..., "file": ..., "line": ..., "samples": ...,
                    "fraction": ..., "ci": [...], "text": "ldr r0, [r1]"}, ...],
  "exceptions": [{"exception": 15, "name": "SysTick", "samples": ..., "fraction": ..., "ci": [...]}, ...],
  "notes": ["..."]
}
```

命令の `text` は逆アセンブラがあるときだけ（その関数を 1 回だけ読んでキャッシュ）。

## 4. 割り込みの時間（`ExceptionProfile`）

`exception_profile(timed_packets, clock_hz=None) -> ExceptionProfile`。入力は `fprobe.itm.Timestamper` の
`TimedPacket`（ローカルのタイムスタンプの累積 = クロック数）。

- 処理中の例外のスタックを持つ: `ENTER n` で積む（前の例外は横取りされた）、`EXIT n` で n まで降ろす、
  `RETURN m` で m（0 = スレッド）に戻ったことを確かめる（スタックの上と違えば m まで降ろす = 欠けたパケットを補う）。
  末尾の連鎖（tail-chaining: `EXIT n` の後 `RETURN` 無しで `ENTER m`）も同じ規則で扱える。
- 1 回の処理ごとに **含む時間**（入ってから出るまで）と **自分の時間**（横取りした別の例外の時間を引く）。
- 割り込みごとに: 回数・含む / 自分の時間の合計・最小・最大・平均・標準偏差・最大の回の時刻（跳べる）・全体に占める
  自分の時間の割合。`clock_hz` があれば秒にも直す。
- `Overflow` が来たら処理中のスタックを捨てる（途中の回は数えない）。数を `overflows` に出す。
- 時刻の無いパケット（`local_time=None`。最後のタイムスタンプより後）は使わない。
- 名前: 1〜15 はコアの例外（NMI・HardFault・…・SysTick）、16 以上は `IRQn`（n = 番号 - 16）。`names` で上書きできる
  （ベクタ表のシンボルから引くのは本流で: `Symbols` で各ベクタの番地を引けば名前が付く）。

## 5. 2 点間の時間（`IntervalReport`）

DT+ の 2 点間の時間・Tracealyzer の Timing Metrics に当たる。

- `measure(recording, start, end, *, bins=20, outliers="iqr") -> IntervalReport`: `Recording.intervals(start, end)` の
  組（開始の時刻, 時間）を受けて統計を付ける。条件は `Recording.find` と同じ（stream・kind・a・b・body）。
  時間はタイムベース（`recording.meta["timebase_hz"]` で秒にも直す）。
- `pair_times(starts, ends)`: 時刻の列どうしの組（`Recording.intervals` と同じ規則: 開始ごとに、次の開始までに来た最初の
  終了。来なければその開始は捨てる）。記録を使わない入力（例外トレース・ITM のポートの値）向け。
- `interval_report(pairs, timebase_hz=None, ...)`: 組から統計を作る本体（純粋な関数）。

`IntervalReport.as_dict()`:

```
{"count": n, "unit": "ticks", "timebase_hz": 1e8,
 "min": ..., "max": ..., "mean": ..., "stdev": ..., "median": ..., "p90": ..., "p99": ...,
 "min_at": t, "max_at": t,                            # 最小・最大の回の開始の時刻（跳べる）
 "histogram": [{"lo": ..., "hi": ..., "count": ...}, ...],
 "outliers": [{"t": 開始, "t_end": 終了, "duration": ..., "score": ...}, ...],   # 時間の長い順
 "seconds": {"min": ..., "max": ..., "mean": ..., ...},                         # timebase_hz があるとき
 "intervals": [[t, duration], ...]}                    # 生データ（統計と生データの往復）
```

外れ値の規則: 既定は **Tukey の柵**（四分位範囲の 1.5 倍の外。分布の形を仮定しない）、`"zscore"`（平均から 3 σ）も選べる。
`score` は柵からの距離を四分位範囲で割った値（z の規則では z）。

## 6. カバレッジ（`Coverage`、近似）

`coverage(samples_or_profile, disassembler, functions=None) -> Coverage`

- 関数の命令の範囲は逆アセンブル（`Disassembler.function`）で求める。リテラルプール等のデータは数えない。
- 関数ごとに: 命令の数・当たった命令の数、行（その関数の命令が属するファイル:行）の数・当たった行の数・割合、
  当たらなかった行の一覧。
- **近似であることを明記**（`approximate: true` と `notes`）: 「当たらなかった」は「実行されなかった」ではない
  （短い時間しか使わない行はサンプルされにくい）。逆に「当たった」は実行された確かな証拠。C0 の正確な値には命令トレース
  （ETM）か、テストポイントが要る。
- `functions=None` なら、サンプルが 1 つでも当たった関数と、`all_functions=True` のときは ELF の全関数（当たらない
  関数は 0%）。

## 7. DWT / ITM / TPIU の設定値（`dwt.py`）

`plan_itm_sampling(core_clock_hz, rate_hz, swo_baud, *, traceclk_hz=None, exception_trace=False,
exception_rate_hz=0, timestamps=False, headroom=0.75) -> SamplingPlan`

1. **間隔**: 目標の間隔 `core_clock_hz / rate_hz` サイクル以上で一番近い `(POSTPRESET+1) × 64|1024` を選ぶ
   （目標より速くしない）。範囲外は端に寄せ、`warnings` に書く。
2. **SWO の速度**: `ACPR = round(traceclk / swo_baud) - 1`（0〜0x1FFF）、実際の速度と誤差。誤差 3% 超は警告。
3. **帯域の見積もり**（UART 方式、1 バイト = 10 ビット）: PC サンプル 5 バイト × サンプル率、例外トレース 3 バイト ×
   3 パケット（入る・出る・戻る）× 例外の頻度、ローカルのタイムスタンプ（有効なとき）はパケットの群ごとに
   1 + ⌈ビット数/7⌉ バイト（差分 = 間隔のサイクル）。同期パケットは無視できる（SYNCTAP=CYCCNT のビット 28: 2^28 サイクルに 6 バイト）。
   使用率 = バイト/秒 × 10 / 実際の速度。
4. 使用率が `headroom` を超えるなら、収まるまで間隔を延ばし（`fitted=True`、警告）、最も遅くしても超えるなら `fits=False`。
   ITM の FIFO は小さい（数バイト）ので平均だけでなく余裕が要る、が既定の 0.75 の理由。

**書く番地と値の一覧**（`SamplingPlan.writes`。`(address, value, mask, note)`。mask が全ビットでなければ読み・変え・書き）:

| 順 | レジスタ | 値 |
|---|---|---|
| 1 | DEMCR 0xE000EDFC | TRCENA（ビット 24）を立てる |
| 2 | ITM_LAR 0xE0000FB0 | 0xC5ACCE55（鍵を外す） |
| 3 | TPIU_CSPSR 0xE0040004 | 1（幅 1） |
| 4 | TPIU_ACPR 0xE0040010 | ACPR |
| 5 | TPIU_SPPR 0xE00400F0 | 2（NRZ = UART 方式） |
| 6 | TPIU_FFCR 0xE0040304 | 0x100（フォーマッタを切る、TrigIn） |
| 7 | ITM_TCR 0xE0000E80 | ITMENA・SYNCENA・DWTENA（ビット 3）・TraceBusID=1・（TSENA） |
| 8 | DWT_CTRL 0xE0001000 | CYCCNTENA・POSTINIT=POSTPRESET・POSTPRESET・CYCTAP・SYNCTAP・（EXCTRCENA）。PCSAMPLENA はまだ 0 |
| 9 | DWT_CTRL 0xE0001000 | 同じ値 + PCSAMPLENA（ビット 12） |

DWT_CTRL の mask は書けるビット（0〜22）だけ。POSTINIT / POSTPRESET は PCSAMPLENA が 0 の間に書く（ARM の推奨）。
チップ固有の SWO のピンの設定（STM32 の DBGMCU_CR の TRACE_IOEN・ピンの代替機能）は含めない（本流・ターゲット定義で）。

`plan_pcsr_sampling(rate_hz)`: DAP サンプラのか所 `[(0xE000101C, 4)]`・周期 `1 / rate_hz`、書き込みは DEMCR の TRCENA
だけ（DWT の読みに要る）。M0+ でも同じ。

## 8. 本流での API・MCP への組み込みの提案

**組み込んだ形（担当 I）**: `host/fprobe/probe_profile.py`（`ProfileTools`、`Probe` の mixin）。API は `profile.start`・
`profile.stop`・`profile.result`・`profile.coverage`・`profile.exceptions`・`timing.measure`（`docs/api.md`）。下の提案との違い:

- 方法 itm は SWO の受信を `swo_decode` / `swo_text` と共有する（`ObserveTools._swo_listen` で同じ `ItmDecoder` のパケットを
  受け取る。二重に読まない）。`timestamps` は省略で `exception_trace` と同じ（割り込みの時間にローカルのタイムスタンプが要る）。
  止めたら DWT_CTRL だけ元に戻す（ITM・TPIU はファームの printf 等も使うので残す）。TPIU の設定のため `swo_start` も行う。
- 方法 pcsr は DWT_PCSR の値を窓（記録中なら記録の stream `sample`、そうでなければイベント記録）から読む。
- 割り込みの名前は ELF のベクタ表の変数（区画の先頭の、大きさの分かる変数）から引く（`IRQn (関数名)`）。
- `exception_times` は `profile.exceptions`、`timing` は `timing.measure`（記録だけを読むので kind は read）。

API（`fprobe_api.json`）・MCP に次を足す（名前は案）。重い計算はホストで、結果は `as_dict()` をそのまま返す。

| 操作 | 内容 |
|---|---|
| `profile_start(method="itm"|"pcsr", rate_hz, swo_baud=None, exception_trace=True)` | `plan_itm_sampling` / `plan_pcsr_sampling` の `writes` を実機に書き（DEMCR 等は読み・変え・書き）、SWO の受信か DAP サンプラを始める。計画（実際のサンプル率・使用率・警告）を返す |
| `profile_stop()` | 止めて、ためたサンプルを `Profile` にする。PCSAMPLENA・EXCTRCENA を戻す |
| `profile_result(by="function"|"line"|"instruction"|"exception", top=20)` | 上位 N（信頼区間つき）。行・命令は関数で絞れる |
| `profile_coverage(functions=None)` | 近似のカバレッジ |
| `exception_times()` | 割り込みごとの回数・時間・最大（最大の回の時刻） |
| `timing(start, end, bins=20)` | 記録の 2 点間の時間（`Recording.find` の条件）。外れ値の時刻は `record_events` / UI のタイムラインへ跳ぶ鍵 |

- SWO の受信のバイト列は `ItmDecoder` → `Timestamper` → `PcSamples.from_itm` / `exception_profile` に流す。記録中なら
  ITM の解読済みパケット（PC サンプル・例外）も記録の stream `swo` に置くと、2 点間の時間に使える（例: `exception` の
  ENTER → EXIT）。
- UI: Ozone の Code Profile 相当の表（関数 → 行 → 命令の掘り下げ、ソースの横に割合）、2 点間の時間の分布と外れ値の一覧
  （クリックでタイムラインのその時刻へ）。

## 9. 実機で確かめる手順

1. **SWO の PC サンプリング**（Nucleo F401RE、`rtt_demo.elf` を `load_to_ram`。ファームが ITM / TPIU / SWO のピンを
   設定する。HSI 16 MHz、2 Mbaud）: `plan_itm_sampling(16e6, 10_000, 2_000_000, exception_trace=True, exception_rate_hz=1000)`
   の `writes` のうち DWT_CTRL の 2 つ（と ITM_TCR の DWTENA）を書く → `swo_start(2_000_000)` → 1 秒受信 →
   `PcSamples.from_itm` → `Profile`。期待: 計画の使用率 ≈ 実際の受信のバイト/秒 × 10 / 2e6、`overflows == 0`、
   `systick_handler` の割合が 1 kHz × その処理のサイクル / 16 MHz 前後、例外 15（SysTick）が 1 秒に ≈ 1000 回。
2. **割り込みの時間**: 同じ受信を `Timestamper` → `exception_profile(..., clock_hz=16e6)`。SysTick の回数 ≈ 1000/秒、
   含む時間が逆アセンブルの命令数から見積もった値と桁で合う。
3. **DWT_PCSR**（Pico 2 / RP2040 の Cortex-M0+ と F401RE の両方）: `plan_pcsr_sampling(10_000)` → DAP サンプラ →
   記録の `sample` → `PcSamples.from_pcsr(events)` → `Profile`。SWO の結果と同じファーム（F401RE）で割合が信頼区間の
   範囲で合うこと。コアを止めた間は 0xFFFFFFFF が `unavailable` に数えられること。
4. **2 点間の時間**: 記録中にロジアナの入力（例: ターゲットの GPIO）か UART の行を開始・終了にして `measure`。
   外れ値の時刻を `record_events` で開き、波形と一致すること。
5. **カバレッジ**: `sram_demo.elf` で 1 秒サンプルし、`tick` は 100%、`reached_milestone` は低いか 0（256 回に 1 回）、
   `default_handler` は 0 であること。
