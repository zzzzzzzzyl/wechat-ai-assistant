# -*- coding: utf-8 -*-
"""`border-radius` 在**本环境**到底能不能裁圆角。

    python -X utf8 probes/qt/why_no_radius2.py

上一个探针发现"所有 QPushButton 变体都是直角"，但那可能是**平台限制**而不是
我们的 bug —— 判定"是不是我们的问题"之前，必须先确认这个环境**本来能不能**
把圆角裁出来。用一个已知应该能裁的控件（自绘 paintEvent）当对照。
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


def hex_of(im, x, y):
    c = QtGui.QColor(im.pixelColor(x, y))
    return "#%02X%02X%02X" % (c.red(), c.green(), c.blue())


def pump(app, ms=150):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.004)


def shoot(w, x, y):
    pm = QtGui.QPixmap(max(1, w.width()), max(1, w.height()))
    pm.fill(QtGui.QColor(MAGENTA))
    pt = QtGui.QPainter(pm)
    w.render(pt, QtCore.QPoint(0, 0))
    pt.end()
    return hex_of(pm.toImage(), x, y)


class Painted(QtWidgets.QWidget):
    """**自绘**圆角当对照：paintEvent 里画 QPainterPath::addRoundedRect + 抗锯齿。"""

    def paintEvent(self, ev):
        pt = QtGui.QPainter(self)
        pt.setRenderHint(QtGui.QPainter.Antialiasing, True)
        pt.setBrush(QtGui.QColor("#01C260"))
        pt.setPen(QtCore.Qt.NoPen)
        path = QtGui.QPainterPath()
        path.addRoundedRect(QtCore.QRectF(self.rect()), self.height() / 2.0,
                            self.height() / 2.0)
        pt.drawPath(path)
        pt.end()


def main():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w, h = 300, 51

    print("== 对照：本环境渲染出的图，洋红底还在不在 ==")
    print("  （先把'洋红底本身能不能量到'这件事证掉，不然下面全是假结论）")

    # 0) 空白控件 —— 什么都不画，应该整块透出洋红。
    #    ⚠️ 这一步是**诊断工具先证明自己在工作**：如果连空白控件都量不到洋红，
    #       那"角是不是洋红"就不能用来判断圆角。
    blank = QtWidgets.QWidget()
    blank.resize(w, h)
    blank.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
    blank.show()
    pump(app, 120)
    px0 = shoot(blank, 0, 0)
    print("  空白控件左上角 = %s  %s"
          % (px0, "→ 洋红能量到，方法可用" if px0 == MAGENTA
             else "→ **量不到洋红，后面的判据不可信**"))

    # 1) 自绘圆角（对照组：这个**一定**应该被裁）
    p = Painted()
    p.resize(w, h)
    p.show()
    pump(app, 150)
    print("  自绘 addRoundedRect 左上角 = %s  %s"
          % (shoot(p, 0, 0), "→ 裁掉了（正常）" if shoot(p, 0, 0) == MAGENTA
             else "→ **直角**"))

    # 2) QFrame + 样式表 radius
    f = QtWidgets.QFrame()
    f.setStyleSheet("QFrame { background:#01C260; border-radius:25px; }")
    f.resize(w, h)
    f.show()
    pump(app, 150)
    print("  QFrame + 样式表 radius   左上角 = %s" % shoot(f, 0, 0))

    # 3) QWidget + WA_StyledBackground + 样式表 radius
    q = QtWidgets.QWidget()
    q.setAttribute(QtCore.Qt.WA_StyledBackground, True)
    q.setStyleSheet("QWidget { background:#01C260; border-radius:25px; }")
    q.resize(w, h)
    q.show()
    pump(app, 150)
    print("  QWidget(WA_Styled) radius 左上角 = %s" % shoot(q, 0, 0))

    # 4) QPushButton + radius，但**用 grab()** 而不是 render()，排除 render 的差异
    b = QtWidgets.QPushButton("生成建议")
    b.setStyleSheet("QPushButton { background:#01C260; border-radius:25px; }")
    b.resize(w, h)
    b.show()
    pump(app, 150)
    g = b.grab()
    print("  QPushButton radius via grab()    左上角 = %s"
          % hex_of(g.toImage(), 0, 0))
    print("  QPushButton radius via render()  左上角 = %s" % shoot(b, 0, 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
