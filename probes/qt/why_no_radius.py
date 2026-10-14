# -*- coding: utf-8 -*-
"""隔离测试：`border-radius` 到底为什么不裁圆角。

    python -X utf8 probes/qt/why_no_radius.py

把同一个颜色/尺寸的 QPushButton 用几种样式表分别渲染到洋红底上，
看哪个变体真的把四个角裁掉了。**每个变体都量像素**，不看字符串。
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

from PySide6 import QtCore, QtGui, QtWidgets                    # noqa: E402

MAGENTA = "#FF00FF"

VARIANTS = {
    "① 只有 background（无 border-radius）":
        "QPushButton { background:#01C260; color:#FFFFFF; }",
    "② background + border-radius":
        "QPushButton { background:#01C260; border-radius:25px; }",
    "③ 我们现在这套（border:1px solid transparent + radius）":
        "QPushButton { background:#01C260; color:#FFFFFF;"
        " border:1px solid transparent; border-radius:25px; }",
    "④ border:none + radius":
        "QPushButton { background:#01C260; border:none; border-radius:25px; }",
    "⑤ 指定 box-sizing 无关，试 border 写在 background 前":
        "QPushButton { border:1px solid transparent; border-radius:25px;"
        " background:#01C260; }",
    "⑥ 加 WA_StyledBackground + radius":
        "QPushButton { background:#01C260; border:1px solid transparent;"
        " border-radius:25px; }",
}


def pump(app, ms=120):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.004)


def hex_of(im, x, y):
    c = QtGui.QColor(im.pixelColor(x, y))
    return "#%02X%02X%02X" % (c.red(), c.green(), c.blue())


def main():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w, h = 300, 51

    for label, qss in VARIANTS.items():
        b = QtWidgets.QPushButton("生成建议")
        b.setStyleSheet(qss)
        b.resize(w, h)
        if label.startswith("⑥"):
            b.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        b.show()
        pump(app, 150)

        pm = QtGui.QPixmap(w, h)
        pm.fill(QtGui.QColor(MAGENTA))
        pt = QtGui.QPainter(pm)
        b.render(pt, QtCore.QPoint(0, 0))
        pt.end()
        im = pm.toImage()

        tl = hex_of(im, 0, 0)
        mid = hex_of(im, 1, h // 2)
        verdict = "角被裁了（胶囊）" if tl == MAGENTA else "角是**直角**"
        print("  %-52s 左上角=%-9s 左中=%-9s -> %s"
              % (label, tl, mid, verdict))
        b.deleteLater()
        pump(app, 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
