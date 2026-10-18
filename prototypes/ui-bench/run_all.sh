#!/bin/sh
# すべての試作を順に測る（窓が次々に開く。測っている間は PC を操作しない）。
# PY: numpy・PySide6・dearpygui・psutil・websockets・playwright を入れた venv の python
set -e
cd "$(dirname "$0")"
PY=${PY:-python}
$PY gen_data.py
node web/decim_node.mjs > results/node-decimate.txt
$PY py/core.py > results/numpy-decimate.txt
egui/target/release/uibench-egui.exe --check --data data > results/rust-decimate.txt
for r in canvas canvasrect webgl; do $PY run_web.py --channel msedge --renderer $r; done
for r in canvasrect webgl; do $PY run_web.py --channel chrome --renderer $r; done
for r in canvasrect webgl; do $PY run_web.py --channel msedge --renderer $r --vsync; done
$PY run_web.py --channel msedge --page egui
for r in canvasrect webgl; do $PY run_tauri.py --renderer $r; done
$PY run_native.py egui-native -- egui/target/release/uibench-egui.exe --bench --data data --out results/egui-native.json
$PY run_native.py egui-native-vsync -- egui/target/release/uibench-egui.exe --bench --vsync --data data --out results/egui-native-vsync.json
$PY run_native.py pyside6 -- "$PY" py/pyside6_bench.py --bench
$PY run_native.py dearpygui -- "$PY" py/dpg_bench.py --bench
$PY server/ws_roundtrip.py
$PY server/ws_roundtrip.py --binary
$PY summarize.py
