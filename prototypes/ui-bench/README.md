# ui-bench — 画面のフレームワーク比較の試作と測定

評価と結論は `docs/superpowers/specs/2026-10-18-ui-framework-evaluation.md`。ここは試作の中身と測り方。
本体（`host/`・`gateware/`）のコードは使わない・変えない（`server/current_api.py` だけが本体を読み取りで呼ぶ）。

## 共通の条件

- **データ**（`gen_data.py` が `data/` に作る。リポジトリには入れない）
  - 波形: 16 ch、合計 9,885,374 変化点（チャンネルごとに 310 万〜1 万。半分は一様、半分は 1000 か所のバースト）。
    時刻は 10 ns のタイムベースで 1 秒（1e8）。fprobe の `la.Waveform` と同じ「チャンネルごとの変化点の表」。
  - 出来事: 8 段（DAP・UART0・SWO・RTT・task・irq・watch・AI/human）× 12.5 万 = 999,377 件の区間。
  - 視点: 慣らし 60 フレーム + 拡大（1 s → 10 µs）300 + 移動（1 ms の窓）300 + 縮小（1 ms → 1 s）300。
- **共通のアルゴリズム**（`web/core.js`・`egui/src/core.rs`・`py/core.py` が同じ定義。突き合わせの和が一致）
  - 波形: 描く幅 W = 1500 画素の境界 b[p] で、チャンネルごとに `before = (b[p] 以下の変化の数)`、
    b[p]〜b[p+1] の間に変化があれば「変化（x）」、無ければ `before & 1`。JS と Rust は前の画素の位置から
    指数探索（galloping）+ 二分探索、numpy は `searchsorted` をまとめて。1 フレーム O(16 × W × log)。
  - 出来事の段: 見えている件数が 2W 以下なら区間を 1 つずつ（幅 60 画素以上ならラベル `#番号`）、
    それより多ければ画素ごとの件数（対数の高さの棒）。
  - 1 フレームに描くもの: 16 段の波形（線・縁・変化の塊）、8 段の出来事、名前 24 個、軸の目盛りと時刻、ラベル。
  - 1600 × 800 の窓、表示の倍率 1。
- **測るもの**
  - 作業: 間引き + 描画の命令を出すまで（ブラウザは JS の時間、egui は `ui()` の中、PySide6 は `paintEvent` 全体）。
  - フレーム間隔: 前のフレームとの間（垂直同期を外して上限を見る。`--vsync` 付きは画面の 100 Hz に合わせた実際の滑らかさ）。
  - プロセスの最大: プロセスの木（ブラウザ・WebView2 の子プロセスを含む）の作業セットの合計の最大（0.2 秒ごと）。

## 試作

| ディレクトリ | 候補 | 描き方 |
|---|---|---|
| `web/` | ブラウザ（いまの方式。Svelte は使わず素の JS） | `?r=canvas`（Canvas 2D・Path2D）、`?r=canvasrect`（Canvas 2D・fillRect のみ）、`?r=webgl`（WebGL2: 間引いた画素の状態を毎フレーム W×16 のテクスチャで送りシェーダで描く。文字は 2D の重ね） |
| `tauri/` | Tauri 2（WebView2） | `web/` をそのまま開く（デーモン役の `serve.py` が配る） |
| `egui/` | egui / eframe 0.37（wgpu） | 1 つの Mesh に矩形を積む + `painter.text`。同じコードでネイティブと WASM（`egui/web/`） |
| `py/pyside6_bench.py` | PySide6 6.12（Qt Widgets） | numpy で画素を塗って（uint32 の ARGB）QImage で貼り、文字は QPainter。pyqtgraph の ImageItem と同じ道 |
| `py/dpg_bench.py` | Dear PyGui 2.3（ImGui + ImPlot） | ImPlot の stair・shade の系列を毎フレーム差し替え、ラベルは draw_text |
| `server/` | デーモン側で間引く場合 | `current_api.py`: いまの `la.Waveform.view` と `Recording.density` を本体の venv で測る。`ws_roundtrip.py`: numpy で間引いて JSON-RPC over WebSocket で返す往復（JSON / バイナリ） |

## 測り方

```sh
# 1. 道具（システムの設定は変えない）。長いパスで PySide6 が入らないので venv は短いパスに置いた
python -m venv %LOCALAPPDATA%\uib
%LOCALAPPDATA%\uib\Scripts\python -m pip install numpy PySide6 dearpygui psutil websockets playwright
#    Rust 1.98 / Node 24 は入っていたものを使う。WASM は:
rustup target add wasm32-unknown-unknown
cargo install wasm-bindgen-cli --version 0.2.129 --locked
# 2. ビルド
(cd egui && cargo build --release) && sh egui/build_wasm.sh
(cd tauri && cargo build --release)
# 3. 測る（窓が次々に開く。測っている間は PC を操作しない）
PY=%LOCALAPPDATA%/uib/Scripts/python.exe sh run_all.sh
# いまのデーモンの間引き（本体の venv。numpy を使わない）
PYTHONPATH=../../host ../../.venv/Scripts/python server/current_api.py
```

ブラウザは Playwright で、入っている Edge / Chrome を使う（ブラウザはダウンロードしない）。
対話で触るには `python serve.py` → `http://127.0.0.1:8765/web/?r=webgl`（`&v=300` で views の 300 番の視点から）、
`/egui/web/`、`egui/target/release/uibench-egui.exe --data data`、`python py/pyside6_bench.py`、`python py/dpg_bench.py`。

測った PC: Ryzen 5 2600（6 コア）、16 GB、Radeon RX 580（D3D11）、Windows 11、表示 100 Hz。
