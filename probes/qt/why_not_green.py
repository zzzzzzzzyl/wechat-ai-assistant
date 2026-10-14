# -*- coding: utf-8 -*-
"""查「生成建议按钮为什么不是绿的」—— 把**真实层级里**所有会覆盖它的样式表列出来。

    python -X utf8 probes/qt/why_not_green.py

思路：`self.setStyleSheet()` 设的颜色**可能被祖先或 QApplication 的样式表覆盖**
（Qt 里三者的优先级是：控件自己的 > 祖先的 > QApplication 的）。
我的 `audit_buttons.py` 是在一个**没有父级**的按钮上验的，
所以"自己算出来是绿的"并不等于"真机上画出来是绿的"—— 这次直接查真实层级。
"""
import os
import re
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

from PySide6 import QtWidgets                                   # noqa: E402
from wechat_assistant import dpi                                # noqa: E402

dpi.enable()
from wechat_assistant.qt_ui import base                         # noqa: E402
from wechat_assistant.qt_ui.panel import FloatingAssistant      # noqa: E402


class Cfg:
    def __init__(self, d):
        self._data = d

    def get(self, path, default=None):
        node = self._data
        for part in str(path).split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def pump(app, ms=300):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.004)


def main():
    app = base.init_app([])
    px = base.px
    ui = FloatingAssistant(Cfg({"ui": {"width": 340, "radius": 10, "theme": "dark"}}),
                           logger=lambda m: None)
    ui.resize(px(340), px(560))
    ui.show()
    pump(app, 400)
    ui.start_pump()

    gen = ui.btn_gen
    print("生成建议按钮: variant=%r  尺寸=%dx%d\n" % (gen.variant, gen.width(), gen.height()))

    print("== 它自己的样式表 ==")
    print(gen.styleSheet() or "(空)")

    print("\n== 祖先链上每一层的样式表（这些都会级联下来）==")
    w = gen.parentWidget()
    depth = 1
    while w is not None:
        ss = w.styleSheet()
        if ss:
            print("\n--- 第 %d 层 %s ---" % (depth, type(w).__name__))
            print(ss)
        w = w.parentWidget()
        depth += 1

    print("\n== QApplication 级样式表 ==")
    print(app.styleSheet() or "(空)")

    print("\n== 祖先链里**所有** QPushButton 规则（这些才是真凶候选）==")
    w = gen
    found = []
    while w is not None:
        ss = w.styleSheet() or ""
        for m in re.finditer(r"QPushButton[^{]*\{[^}]*\}", ss):
            found.append((type(w).__name__, m.group(0).replace("\n", " ").strip()))
        w = w.parentWidget()
    if found:
        for who, rule in found:
            print("  [%s] %s" % (who, rule))
    else:
        print("  无")

    print("\n== 实际画出来是什么颜色（把按钮渲染到红底上看）==")
    from PySide6 import QtCore, QtGui
    pm = QtGui.QPixmap(gen.size())
    pm.fill(QtGui.QColor("#FF0000"))
    pt = QtGui.QPainter(pm)
    gen.render(pt, QtCore.QPoint(0, 0))
    pt.end()
    img = pm.toImage()
    for (x, y) in ((6, 6), (6, img.height() // 2), (img.width() // 2, 6),
                   (img.width() - 8, img.height() // 2)):
        c = QtGui.QColor(img.pixelColor(x, y))
        print("  (%3d,%3d) -> #%02X%02X%02X" % (x, y, c.red(), c.green(), c.blue()))

    ui.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
