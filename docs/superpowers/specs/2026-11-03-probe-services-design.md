# Probe を領域ごとのサービスに分ける（C1）

2026-11-03〜04。状態: 実装済み（全 58 領域をサービスに。Probe は mixin を継がない）。
指示: `docs/superpowers/specs/2026-11-03-cloud-sessions-plan.md` §B の 1。

## 1. 前の形と困りごと（事実）

- `host/fprobe/probe.py` の `Probe` は 58 の mixin（`probe_*.py` の `XxxTools`、合わせて約 15,500 行）の多重継承。状態はすべて
  1 つの `self` にあり、各 mixin の `_init_*` を `Probe.__init__` が 1 つずつ呼んでいた。
- mixin の間の参照が密: 58 のうち 50 以上が、ほかの mixin の属性・メソッドを `self` 越しに使う（`self._route_bit`、
  `self._flash_bp_wrap`、`self._close_snapshot` 等）。どの領域がどの領域に頼るかは、`self.` を全部読まないと分からなかった。
  多く使われる相手: observe（24 の mixin から）・connect（22。`_debug`）・recording（21。`_recorder` 等）・debug（14）。
- 名前は 1 つの名前空間を共有するので、2 つの mixin が同じ名前を定義すると MRO で黙って片方が隠れる（`_start_itm` で
  一度起きた）。
- 後始末（`Probe.close`・`_release_device`）は、Probe が各領域の閉じる関数の名前を 1 つずつ並べて知っていた。

## 2. 形

```
Probe（デーモン・MCP・試験が握る入口。核の状態と、サービスへの委譲）
 ├─ 核: 機器の錠 _lock・機器の口 session（遅延で開く）・_caps()・接続中のターゲット _debug / target / dbg・
 │      読み込んだ ELF _symbols・つないでいる先 _link・_target_type・UART の口 _uart・接続の設定・後始末の登録 _release_steps
 └─ self._svc_<領域> = XxxService(self)  … 58 のサービス（probe_*.py）
```

- **サービス**（`probe_*.py` の `XxxService`）は 1 つの領域の状態と操作を持つクラス。次の 3 つの組をクラスに書く:
  - `API`: 公開の道具（API の impl 名・デーモンや画面が引く公開のメソッド）。
  - `SHARED`: ほかの領域・試験に出す名前（内部のメソッド・状態。例: 記録の `_recorder`、DAP の占有の確かめ `_require_dap_free`）。
  - `NEEDS`: ホストから使う名前（核の名前と、ほかの領域が SHARED・API に出した名前）。
- **ホスト**: サービスは Probe を `self._host`（`fprobe.probe_service.HostView`）越しに使う。HostView は NEEDS に並べた名前
  だけを読める・書ける（並べていない名前は AttributeError）。だから領域の間の依存は、各サービスの NEEDS に全部出る。
  `tests/test_probe_services.py` が、サービスの中の `self._host.<名前>` の全部と NEEDS が一致すること（試験で通らない道も静的に）、
  NEEDS の名前が Probe にあること、SHARED の名前がサービスにあることを確かめる。
- **委譲**: `@expose("_svc_<領域>", XxxService)` が API と SHARED の名前を Probe に委譲の記述子（`delegate`）で出す。記述子は
  読み・書き・消しをサービスに委ねる: API・MCP・デーモン（`getattr(probe, impl)`）は今までどおり Probe を名前で引け、試験・
  プラグインが Probe の上でメソッドを差し替えると、サービスの中からの呼び出しにも効く（mixin のときと同じ）。クラスから
  引くとサービスのクラスの関数（署名・説明を保つ）。`__getattr__` の振り分けは使わない（名前の打ち違いが実行時まで分からない）。
- **名前の重なりは作れない**: `expose` は、出す名前が Probe に既にあれば例外にする（黙って隠さない）。
- **後始末の登録**: 機器を手放すとき（`_release_device`）に、機器の口を閉じる前に閉じる段は `ReleaseSteps` に名前付きで登録する
  （登録の順に全部試し、失敗を `[{"step", "error"}]` で集める）。段は呼ぶ時に名前で引く（差し替えが効く）。

### 2.1 Probe を仲立ち（ホスト）にした理由（前の版の「頼る相手をコンストラクタで受ける」からの変更）

段 2・3 の最初の 6 領域は、頼る相手（ほかのサービス・まだ mixin の関数）をコンストラクタの引数で受けた。残りの 52 領域は
相手が多い（DAP のターゲットは 27 の名前を使い、観測は 42 の名前を出す）ので、相手ごとの引数にするとコンストラクタと組み立てが
相手の数だけ長くなり、作る順の制約（相手より後に作る）が循環して解けない（observe ↔ target ↔ debug）。
代わりに、Probe をホストにして、各サービスが使う名前を NEEDS に宣言する形にした:

- 依存は NEEDS で全部見える（宣言の外は使えない。静的にも実行時にも確かめる）。前の「`self.` を全部読む」は要らない。
- 状態はサービスごとに分かれ（1 つの名前空間を共有しない）、サービスは偽のホスト（`SimpleNamespace`）の上で単独で作って
  試せる（例: `tests/test_la_bugs.py` の `_tools`、`tests/test_fault.py` の `_service_on`）。
- 相手を名前で引くので、作る順の制約は「初期化の中で使う相手」だけ（前の `_init_*` の順のまま）。
- 最初の 6 領域も同じ形に揃えた（`_svc_xtrig` 等。コンストラクタの関数の引数をやめた）。

### 2.2 ExitStack を使わない理由（指示からの変更。利用者が 2026-11-03 に承認）

ExitStack は途中の段が例外を出しても残りを閉じ、最後に例外を投げ直す。いまの約束は **例外を投げず、失敗した段を返す**
（`Probe.close`・`_release_device`。`probe.design_switch` の `release_failures` 等の API に出ている）。ExitStack に載せても
各段を包んで例外を止める必要があり、足されるのは逆順だけになる。いまの閉じる順は獲得の逆順ではない（linux_halt は gdb の
後始末の後で錠の外、機器の口は錠の中）。求められた意図「後始末を 1 つの例外で途中で止めない」は ReleaseSteps が満たす。

## 3. 段（各段で `cd host && pytest` 全部と、応答の照合のずれ 0 を保った）

1. 基盤（`probe_service.py`）と後始末の登録。2. クロストリガの 3 領域。3. GPIO・イベント・linux_halt。
4. ロジアナ（la・longrec・decode・timeline）。捕捉の状態を Probe.__init__ からサービスへ。
5. 記録（recording・recording_rtos・rules・rtos_analysis）。記録の状態を Probe.__init__ からサービスへ。
6a. 解析・窓・FPGA の観測・画面の状態・利用者・フック・端末・流れ・書式・ELF の見張り・ELF のファイル・機器・システム・UART・構成。
6b. SWO・SWO の受信・テストポイント・プロファイル・その書き出し・ETM・サイクル・Linux・ヒープ・スタック・RTOS・安全・値の見張り・
    GDB・GDB のトレースポイント・実行。
7. DAP（調べる・観測・ブレークポイント・Flash・スナップショット・Flash の BKPT・低レベル・接続・モニタ・複数コア・ターゲット・デバッグ）。
8. 最初の 6 領域を同じ形に。`probe_host.ProbeHost`（mixin の約束）と核の約束 `ProbeCore` を消す。

段 4〜7 は、AST で書き換える道具（`self.<ほかの領域の名前>` → `self._host.<名前>`、`_init_*` → `__init__`、API・SHARED・NEEDS を
作る）で行い、道具で書き換えられない所（サービス自体を外に渡す `_InjectHost(self)`・`_Core(self, …)`、名前の文字列で引く
`getattr(self, impl)`・`getattr(self, "_recorder")`、キーワードだけの `_init_design` の引数）は手で直した。

## 4. 核に残したもの・残した課題

- 核の状態（`_debug`・`_symbols`・`_link`・`_target_type`・`_uart` 等）は複数の領域が書く（`_debug` は接続・ターゲット・構成・
  複数コア）。どの領域の持ち物でもないので Probe に置いた。
- `RulesService` の `_last_runs` 等はクラスの属性の可変の表だった（mixin のときから。Probe を 2 つ作ると共有される）。段の後に、
  試験を先に足してインスタンスの状態に直した。

## 5. 試験

- `tests/test_probe_services.py`: 委譲（読み・書き・消し、クラスからは関数）、`expose` が既にある名前・無い名前を断る、HostView が
  NEEDS の外を断る、ReleaseSteps、API の全 impl が Probe の呼べる属性、名前の重なり、全サービスの NEEDS・SHARED の実在と一致。
- 試験がサービスの中を覗く所は、サービスの属性（`probe._svc_match_trigger._obs_trigger`）か、Probe の上の委譲（`probe._restore_outputs`）。
  Probe のクラスのメソッドを差し替えていた試験は、インスタンスの上で差し替える（委譲がサービスに書く）。
