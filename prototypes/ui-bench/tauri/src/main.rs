//! Tauri 2 の試作: web/ の画面（デーモン役の serve.py が配る）を WebView2 の窓で開くだけ。
//! 使い方: uibench-tauri <URL>   例: http://127.0.0.1:8765/web/?r=webgl&mode=bench&tag=tauri
//! 垂直同期を外すには環境変数 WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS="--disable-gpu-vsync --disable-frame-rate-limit"
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    let url = std::env::args().nth(1).unwrap_or("http://127.0.0.1:8765/web/?r=webgl".into());
    tauri::Builder::default()
        .setup(move |app| {
            tauri::WebviewWindowBuilder::new(app, "main", tauri::WebviewUrl::External(url.parse().unwrap()))
                .title("ui-bench Tauri")
                .inner_size(1600.0, 820.0)
                .build()?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("tauri");
}
