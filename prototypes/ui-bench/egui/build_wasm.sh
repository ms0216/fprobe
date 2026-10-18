#!/bin/sh
# egui の試作を WASM にする（要: rustup target add wasm32-unknown-unknown、cargo install wasm-bindgen-cli --version 0.2.129）
# 出力: web/pkg/（serve.py から /egui/web/ で開く）
set -e
cd "$(dirname "$0")"
cargo build --release --target wasm32-unknown-unknown
wasm-bindgen --target web --no-typescript --out-dir web/pkg target/wasm32-unknown-unknown/release/uibench-egui.wasm
ls -la web/pkg
