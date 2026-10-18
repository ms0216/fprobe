//! egui / eframe の試作（ネイティブ）。web/bench.js と同じデータ・同じ間引き・同じ絵。
//! 使い方: uibench-egui [--bench] [--vsync] [--data ../data] [--out ../results/egui-native.json]
//! 対話: ホイールで拡大縮小、ドラッグで移動。
//! WASM（ブラウザ）: build_wasm.sh でビルドし、serve.py から /egui/web/?mode=bench を開く（結果は /result へ POST）。

mod core;

use core::*;
use eframe::egui::{self, Align2, Color32, FontId, Mesh, Pos2, Rect, Sense, Shape, Stroke, Vec2};
use web_time::Instant;

struct App {
    data: Data,
    load_ms: f64,
    bench: bool,
    #[cfg_attr(target_arch = "wasm32", allow(dead_code))]
    out: std::path::PathBuf,
    tag: String,
    wave: Vec<u8>,
    counts: Vec<Vec<u32>>,
    view: (f64, f64),
    frame: usize,
    last: Option<Instant>,
    work: Vec<f64>,
    intervals: Vec<f64>,
    status: String,
}

const BG: Color32 = Color32::from_rgb(17, 17, 17);
const LINE: Color32 = Color32::from_rgb(68, 204, 68);
const XFILL: Color32 = Color32::from_rgb(42, 106, 42);
const EV: Color32 = Color32::from_rgb(85, 136, 170);
const TEXT: Color32 = Color32::from_rgb(221, 221, 221);

impl App {
    fn draw(&mut self, painter: &egui::Painter, origin: Pos2) {
        let (v0, v1) = self.view;
        decimate_wave(&self.data.channels, v0, v1, &mut self.wave);
        let lanes: Vec<LaneOut> = self.data.lanes.iter().zip(self.counts.iter_mut())
            .map(|(l, c)| decimate_lane(l, v0, v1, c)).collect();
        let r = |x: f32, y: f32, w: f32, h: f32| Rect::from_min_size(origin + Vec2::new(x, y), Vec2::new(w, h));
        let mut mesh = Mesh::default();
        mesh.add_colored_rect(r(0.0, 0.0, LABEL_W + W as f32, HEIGHT), BG);
        for c in 0..CH {
            let row = c * W;
            let (y_hi, y_lo) = (AXIS_H + c as f32 * ROW_H + 4.0, AXIS_H + (c + 1) as f32 * ROW_H - 5.0);
            let mut p = 0;
            while p < W {
                let s = self.wave[row + p];
                let mut e = p + 1;
                while e < W && self.wave[row + e] == s { e += 1; }
                let (x0, x1) = (LABEL_W + p as f32, LABEL_W + e as f32);
                if s == ST_X {
                    mesh.add_colored_rect(r(x0, y_hi, x1 - x0, y_lo - y_hi), XFILL);
                } else {
                    mesh.add_colored_rect(r(x0, if s == 1 { y_hi } else { y_lo }, x1 - x0, 1.0), LINE);
                    if e < W && self.wave[row + e] != ST_X { mesh.add_colored_rect(r(x1, y_hi, 1.0, y_lo - y_hi), LINE); }
                }
                p = e;
            }
        }
        let mut labels = vec![];
        for (l, out) in lanes.iter().enumerate() {
            let top = AXIS_H + CH as f32 * ROW_H + l as f32 * LANE_H;
            match out {
                LaneOut::Sparse(items) => for &(x0, x1, i) in items {
                    mesh.add_colored_rect(r(LABEL_W + x0, top + 6.0, x1 - x0, LANE_H - 12.0), EV);
                    if x1 - x0 >= LABEL_MIN_PX { labels.push((LABEL_W + x0 + 3.0, top + LANE_H / 2.0, i)); }
                },
                LaneOut::Dense(max) => {
                    let lm = (1.0 + *max as f32).log2();
                    for (p, &n) in self.counts[l].iter().enumerate() {
                        if n == 0 { continue; }
                        let h = (LANE_H - 6.0) * (1.0 + n as f32).log2() / lm;
                        mesh.add_colored_rect(r(LABEL_W + p as f32, top + LANE_H - 3.0 - h, 1.0, h), EV);
                    }
                }
            }
        }
        painter.add(Shape::mesh(mesh));
        // 文字
        let font = FontId::proportional(12.0);
        for c in 0..CH {
            painter.text(origin + Vec2::new(8.0, AXIS_H + c as f32 * ROW_H + ROW_H / 2.0), Align2::LEFT_CENTER, format!("ch{c}"), font.clone(), TEXT);
        }
        for (l, lane) in self.data.lanes.iter().enumerate() {
            painter.text(origin + Vec2::new(8.0, AXIS_H + CH as f32 * ROW_H + l as f32 * LANE_H + LANE_H / 2.0), Align2::LEFT_CENTER, &lane.name, font.clone(), TEXT);
        }
        for t in axis_ticks(v0, v1, self.data.tick_s) {
            let x = LABEL_W + ((t - v0) * W as f64 / (v1 - v0)) as f32;
            painter.line_segment([origin + Vec2::new(x, AXIS_H - 6.0), origin + Vec2::new(x, HEIGHT)], Stroke::new(1.0, Color32::from_gray(68)));
            painter.text(origin + Vec2::new(x + 3.0, AXIS_H / 2.0), Align2::LEFT_CENTER, fmt_time(t * self.data.tick_s), font.clone(), TEXT);
        }
        for (x, y, i) in labels {
            painter.text(origin + Vec2::new(x, y), Align2::LEFT_CENTER, format!("#{i}"), font.clone(), Color32::BLACK);
        }
    }

    fn finish(&self) {
        let skip = self.data.warmup;
        let result = serde_json::json!({
            "impl": self.tag, "load_ms": self.load_ms,
            "work_ms": stats(&self.work[skip..]).json(), "interval_ms": stats(&self.intervals[skip..]).json(),
            "raw_work": &self.work[skip..], "raw_interval": &self.intervals[skip..],
        });
        #[cfg(not(target_arch = "wasm32"))]
        std::fs::write(&self.out, serde_json::to_string_pretty(&result).unwrap()).unwrap();
        #[cfg(target_arch = "wasm32")]
        web::post_result(result.to_string());
    }
}

impl eframe::App for App {
    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        let now = Instant::now();
        if let Some(last) = self.last { self.intervals.push((now - last).as_secs_f64() * 1e3); }
        self.last = Some(now);
        if self.bench {
            if self.frame >= self.data.views.len() {
                if self.frame == self.data.views.len() {
                    self.finish();
                    self.frame += 1;
                    ui.ctx().send_viewport_cmd(egui::ViewportCommand::Close);
                }
                return;
            }
            self.view = self.data.views[self.frame];
            self.frame += 1;
        }
        egui::Frame::NONE.fill(BG).show(ui, |ui| {
            let (resp, painter) = ui.allocate_painter(Vec2::new(LABEL_W + W as f32, HEIGHT), Sense::click_and_drag());
            if !self.bench {
                let (v0, v1) = self.view;
                let span = v1 - v0;
                if let Some(pos) = resp.hover_pos() {
                    let a = (((pos.x - resp.rect.min.x - LABEL_W) / W as f32).clamp(0.0, 1.0)) as f64;
                    let dy = ui.input(|i| i.smooth_scroll_delta.y) as f64;
                    if dy != 0.0 {
                        let at = v0 + a * span;
                        let ns = (span * (-dy * 0.002).exp()).clamp(50.0, self.data.span);
                        let s = (at - a * ns).clamp(0.0, self.data.span - ns);
                        self.view = (s, s + ns);
                    }
                }
                if resp.dragged() {
                    let (v0, v1) = self.view;
                    let span = v1 - v0;
                    let s = (v0 - resp.drag_delta().x as f64 * span / W as f64).clamp(0.0, self.data.span - span);
                    self.view = (s, s + span);
                }
            }
            let t = Instant::now();
            self.draw(&painter, resp.rect.min);
            let ms = t.elapsed().as_secs_f64() * 1e3;
            self.work.push(ms);
            if !self.bench { self.status = format!("{ms:.2} ms/frame (work)"); }
            ui.label(&self.status);
        });
        if self.bench { ui.ctx().request_repaint(); }
    }
}

#[cfg(not(target_arch = "wasm32"))]
fn main() -> eframe::Result {
    let args: Vec<String> = std::env::args().collect();
    let flag = |f: &str| args.iter().any(|a| a == f);
    let opt = |f: &str, d: &str| args.iter().position(|a| a == f).map(|i| args[i + 1].clone()).unwrap_or(d.to_string());
    let (bench, vsync) = (flag("--bench"), flag("--vsync"));
    let t = Instant::now();
    let data = Data::load(std::path::Path::new(&opt("--data", "../data")));
    let load_ms = t.elapsed().as_secs_f64() * 1e3;
    if flag("--check") {                          // 間引きだけ: 時間と突き合わせ用の和（web/decim_node.mjs と同じ）
        let (mut wave, mut counts) = (vec![0u8; CH * W], vec![vec![0u32; W]; LANES]);
        let (mut tw, mut tl) = (vec![], vec![]);
        for (i, &(v0, v1)) in data.views.iter().enumerate() {
            let t = Instant::now();
            decimate_wave(&data.channels, v0, v1, &mut wave);
            let a = t.elapsed().as_secs_f64() * 1e3;
            let t = Instant::now();
            for (l, c) in data.lanes.iter().zip(counts.iter_mut()) { decimate_lane(l, v0, v1, c); }
            let b = t.elapsed().as_secs_f64() * 1e3;
            if i >= data.warmup { tw.push(a); tl.push(b); }
            if [60, 360, 660, 959].contains(&i) {
                let sum: u64 = wave.iter().enumerate().map(|(k, &v)| v as u64 * (k as u64 % 251 + 1)).sum();
                println!("check {i}: {sum}");
            }
        }
        println!("{}", serde_json::json!({"impl": "rust-decimate", "wave_ms": stats(&tw).json(), "lanes_ms": stats(&tl).json()}));
        return Ok(());
    }
    let tag = format!("egui-native{}", if vsync { "-vsync" } else { "" });
    let out = opt("--out", &format!("../results/{tag}.json")).into();
    let mut options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default().with_inner_size([LABEL_W + W as f32, HEIGHT + 24.0]).with_title("ui-bench egui"),
        ..Default::default()
    };
    if !vsync { options.wgpu_options.surface.present_mode = eframe::wgpu::PresentMode::AutoNoVsync; }
    let span = data.span;
    eframe::run_native("ui-bench egui", options, Box::new(move |cc| {
        cc.egui_ctx.set_pixels_per_point(1.0);
        Ok(Box::new(App {
            data, load_ms, bench, out, tag, wave: vec![0; CH * W], counts: vec![vec![0; W]; LANES],
            view: (0.0, span), frame: 0, last: None, work: vec![], intervals: vec![],
            status: String::new(),
        }))
    }))
}

#[cfg(target_arch = "wasm32")]
mod web {
    use super::*;
    use wasm_bindgen::JsCast;
    use wasm_bindgen_futures::JsFuture;

    async fn fetch(url: &str) -> Vec<u8> {
        let resp: web_sys::Response = JsFuture::from(web_sys::window().unwrap().fetch_with_str(url)).await.unwrap().dyn_into().unwrap();
        let buf = JsFuture::from(resp.array_buffer().unwrap()).await.unwrap();
        js_sys::Uint8Array::new(&buf).to_vec()
    }

    pub fn post_result(body: String) {
        let init = web_sys::RequestInit::new();
        init.set_method("POST");
        init.set_body(&body.into());
        let _ = web_sys::window().unwrap().fetch_with_str_and_init("/result", &init);
        web_sys::window().unwrap().document().unwrap().set_title("done");
    }

    pub fn start() {
        wasm_bindgen_futures::spawn_local(async {
            let search = web_sys::window().unwrap().location().search().unwrap_or_default();
            let bench = search.contains("mode=bench");
            let t = Instant::now();
            let mut b = vec![];
            for f in FILES { b.push(fetch(&format!("/data/{f}")).await); }
            let data = Data::from_bytes(&b);
            let load_ms = t.elapsed().as_secs_f64() * 1e3;
            let span = data.span;
            let tag = "egui-wasm".to_string();
            let canvas: web_sys::HtmlCanvasElement = web_sys::window().unwrap().document().unwrap()
                .get_element_by_id("canvas").unwrap().dyn_into().unwrap();
            eframe::WebRunner::new().start(canvas, eframe::WebOptions::default(), Box::new(move |cc| {
                cc.egui_ctx.set_pixels_per_point(1.0);
                Ok(Box::new(App {
                    data, load_ms, bench, out: Default::default(), tag, wave: vec![0; CH * W], counts: vec![vec![0; W]; LANES],
                    view: (0.0, span), frame: 0, last: None, work: vec![], intervals: vec![], status: String::new(),
                }))
            })).await.expect("eframe web");
        });
    }
}

#[cfg(target_arch = "wasm32")]
fn main() {
    web::start();
}
