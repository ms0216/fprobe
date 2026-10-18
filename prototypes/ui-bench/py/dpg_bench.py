"""Dear PyGui（ImGui + ImPlot）の試作: numpy で間引き、ImPlot の段（stair）と塗り（shade）の系列を毎フレーム差し替える。
x 軸は画素（0..W）。描く中身は web/egui と同じ（波形の段・変化の塊・出来事の段・名前・軸・出来事のラベル）。

使い方: python py/dpg_bench.py [--bench] [--vsync]
"""

import sys
import time

import dearpygui.dearpygui as dpg
import numpy as np

import core
from core import CH, LANES, ST_X, W

ROWS = CH + LANES


def row_base(r: int) -> float:
    return float(ROWS - 1 - r)                 # 上から順に


def main() -> None:
    bench, vsync = "--bench" in sys.argv, "--vsync" in sys.argv
    data = core.Data()
    dpg.create_context()
    dpg.create_viewport(title="ui-bench Dear PyGui", width=core.FULL_W + 40, height=core.HEIGHT + 60, vsync=vsync)
    xs = np.arange(W, dtype=np.float64)
    with dpg.window(tag="main"):
        with dpg.plot(width=core.FULL_W, height=core.HEIGHT, no_menus=True, no_box_select=True, tag="plot"):
            xa = dpg.add_plot_axis(dpg.mvXAxis, tag="x", no_gridlines=False)
            ya = dpg.add_plot_axis(dpg.mvYAxis, tag="y", no_gridlines=True)
            dpg.set_axis_limits(xa, 0, W)
            dpg.set_axis_limits(ya, -0.2, ROWS)
            dpg.set_axis_ticks(ya, tuple([(lane[0], row_base(CH + i) + 0.4) for i, lane in enumerate(data.lanes)]
                                         + [(f"ch{c}", row_base(c) + 0.4) for c in range(CH)]))   # タプルでないと落ちる
            lines = [dpg.add_stair_series(xs, xs * 0, parent=ya) for _ in range(CH)]
            shades = [dpg.add_shade_series(xs, xs * 0, y2=xs * 0, parent=ya) for _ in range(ROWS)]
        dpg.add_text("", tag="status")
    labels_layer = dpg.add_draw_layer(parent="plot")
    dpg.set_primary_window("main", True)
    dpg.setup_dearpygui()
    dpg.show_viewport()

    wave = np.zeros((CH, W), np.uint8)
    view = (0.0, float(data.meta["span"]))
    frame, last, work, intervals = 0, None, [], []
    tick_s = data.meta["tick_s"]
    while dpg.is_dearpygui_running():
        now = time.perf_counter()
        if last is not None:
            intervals.append((now - last) * 1e3)
        last = now
        if bench:
            if frame >= len(data.views):
                break
            view = tuple(data.views[frame])
            frame += 1
        t = time.perf_counter()
        v0, v1 = view
        core.decimate_wave(data.channels, v0, v1, wave)
        lanes_out = [core.decimate_lane(lane, v0, v1) for lane in data.lanes]
        for c in range(CH):
            s, base = wave[c], row_base(c)
            level = np.where(s == ST_X, 0.5, s) * 0.7 + base
            dpg.set_value(lines[c], [xs, level])
            dpg.set_value(shades[c], [xs, np.full(W, base), np.where(s == ST_X, base + 0.7, base)])
        for i, out in enumerate(lanes_out):
            base = row_base(CH + i)
            if out[0] == "sparse":
                cov = np.zeros(W + 1, np.int32)
                np.add.at(cov, out[1].astype(np.int64), 1)
                np.add.at(cov, np.minimum(W, np.ceil(out[2])).astype(np.int64), -1)
                h = (np.cumsum(cov[:-1]) > 0) * 0.7
            else:
                counts = out[1]
                h = 0.8 * np.log2(1 + counts) / np.log2(1 + max(1, counts.max()))
            dpg.set_value(shades[CH + i], [xs, np.full(W, base), base + h])
        ticks = core.axis_ticks(v0, v1, tick_s)
        dpg.set_axis_ticks("x", tuple((core.fmt_time(x * tick_s), float((x - v0) * W / (v1 - v0))) for x in ticks))
        dpg.delete_item(labels_layer, children_only=True)
        for i, out in enumerate(lanes_out):
            if out[0] != "sparse":
                continue
            wide = (out[2] - out[1]) >= core.LABEL_MIN_PX
            for x, n in zip(out[1][wide], out[3][wide]):
                dpg.draw_text((float(x) + 3, row_base(CH + i) + 0.5), f"#{n}", size=0.3, color=(0, 0, 0), parent=labels_layer)
        work.append((time.perf_counter() - t) * 1e3)
        if not bench:
            dpg.set_value("status", f"{work[-1]:.2f} ms/frame (work)")
        dpg.render_dearpygui_frame()
    if bench:
        core.save("dearpygui" + ("-vsync" if vsync else ""), data.load_ms, work, intervals, data.meta["warmup"])
    dpg.destroy_context()


if __name__ == "__main__":
    main()
