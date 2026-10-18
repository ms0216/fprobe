"""PySide6（Qt Widgets）の試作: numpy で間引き・画素を塗り（QImage）、QPainter で貼って文字を描く。
pyqtgraph の ImageItem と同じ道（Qt の上で Python から大きな波形を描く最速の形）。

使い方: python py/pyside6_bench.py [--bench]   対話: ホイールで拡大縮小、ドラッグで移動。
"""

import sys
import time

import numpy as np
from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtWidgets import QApplication, QWidget

import core
from core import CH, FULL_W, HEIGHT, LABEL_W, LANES, W


class View(QWidget):
    def __init__(self, data: core.Data, bench: bool):
        super().__init__()
        self.data, self.bench = data, bench
        self.setFixedSize(FULL_W, HEIGHT)
        self.setWindowTitle("ui-bench PySide6")
        self.wave = np.zeros((CH, W), np.uint8)
        self.img = np.zeros((HEIGHT, FULL_W), np.uint32)
        self.qimg = QImage(self.img.data, FULL_W, HEIGHT, FULL_W * 4, QImage.Format.Format_ARGB32)
        self.view = (0.0, float(data.meta["span"]))
        self.frame, self.last, self.work, self.intervals = 0, None, [], []
        self.font = QFont("sans-serif")
        self.font.setPixelSize(12)
        self.drag = None
        if bench:
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.update)
            self.timer.start(0)

    def paintEvent(self, _):  # noqa: N802
        now = time.perf_counter()
        if self.last is not None:
            self.intervals.append((now - self.last) * 1e3)
        self.last = now
        if self.bench:
            if self.frame >= len(self.data.views):
                self.timer.stop()
                core.save("pyside6", self.data.load_ms, self.work, self.intervals, self.data.meta["warmup"])
                QApplication.quit()
                return
            self.view = tuple(self.data.views[self.frame])
            self.frame += 1
        t = time.perf_counter()
        v0, v1 = self.view
        core.decimate_wave(self.data.channels, v0, v1, self.wave)
        lanes_out = [core.decimate_lane(lane, v0, v1) for lane in self.data.lanes]
        ticks = core.axis_ticks(v0, v1, self.data.meta["tick_s"])
        core.raster(self.img, self.wave, lanes_out, [(x - v0) * W / (v1 - v0) for x in ticks])
        p = QPainter(self)
        p.drawImage(0, 0, self.qimg)
        p.setFont(self.font)
        light, dark = QColor(221, 221, 221), QColor(0, 0, 0)
        fixed = CH + LANES + len(ticks)
        for i, (x, y, s) in enumerate(core.labels(self.data, v0, v1, lanes_out, ticks)):
            p.setPen(light if i < fixed else dark)
            p.drawText(QPointF(x, y + 4), s)
        p.end()
        self.work.append((time.perf_counter() - t) * 1e3)

    # 対話
    def wheelEvent(self, e):  # noqa: N802
        v0, v1 = self.view
        span, total = v1 - v0, float(self.data.meta["span"])
        a = min(1.0, max(0.0, (e.position().x() - LABEL_W) / W))
        at = v0 + a * span
        ns = min(total, max(50.0, span * np.exp(-e.angleDelta().y() * 0.002)))
        s = min(max(0.0, at - a * ns), total - ns)
        self.view = (s, s + ns)
        self.update()

    def mousePressEvent(self, e):  # noqa: N802
        self.drag = (e.position().x(), self.view[0])

    def mouseMoveEvent(self, e):  # noqa: N802
        if self.drag:
            v0, v1 = self.view
            span, total = v1 - v0, float(self.data.meta["span"])
            s = min(max(0.0, self.drag[1] - (e.position().x() - self.drag[0]) * span / W), total - span)
            self.view = (s, s + span)
            self.update()


def main() -> None:
    app = QApplication(sys.argv)
    w = View(core.Data(), "--bench" in sys.argv)
    w.show()
    app.exec()


if __name__ == "__main__":
    main()
