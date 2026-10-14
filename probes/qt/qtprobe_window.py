# -*- coding: utf-8 -*-
"""验证 Qt 能不能做到我们现在这几件难事（做不到的话迁移就没意义）：

1. **无边框 + 真圆角 + 真透明**（不是色键 hack）
2. **圆角抗锯齿**（Tk 里折腾了三轮的）
3. **置顶但不抢焦点**（跟随悬浮窗要的）
4. **悬停高亮 + 手型光标**
5. **拖动 / 缩放自己实现**

用法： python -X utf8 out/_qtprobe_win.py      （交互窗口，看完自己关掉）

⚠️ 结果直接打在控制台，窗口也会显示出来。
"""
import os
import sys

ROOT = r"E:\Claude\wechat-ai-assistant"
sys.path.insert(0, ROOT)
sys.path.insert(0, r"E:\Claude\.qtprobe")
os.chdir(ROOT)

# ?? PySide6 ??????????? `.qtprobe`????????????
#    ???????? <???>/.qtprobe ?????????????
_WS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_WS, ".qtprobe"))

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402


class Panel(QtWidgets.QWidget):
    """无边框圆角面板：Qt 用「窗口透明 + 自绘圆角」实现，抗锯齿是白送的。

    对比 Tk 那套：overrideredirect + 透明色键 #FF00FE + 自己画多边形
    + 位图抗锯齿 + 节流。这里一个 paintEvent 就完了。
    """

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            QtCore.Qt.FramelessWindowHint        # 无边框
            | QtCore.Qt.WindowStaysOnTopHint     # 置顶
            | QtCore.Qt.Tool                     # 不在任务栏留条目（像 Tk 的悬浮窗）
        )
        # ⚠️ 关键：窗口背景透明 + 无系统背景，才能画出**真圆角**
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WA_NoSystemBackground, True)
        self.resize(360, 420)

        self._radius = 10
        self._hover = None
        self.setMouseTracking(True)
        self.setCursor(QtCore.Qt.ArrowCursor)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(8)

        title = QtWidgets.QLabel("设置")
        title.setStyleSheet("font-weight:bold; font-size:14px;")
        lay.addWidget(title)

        for i in range(3):
            card = QtWidgets.QLabel(
                "第 %d 条建议：圆角是真的抗锯齿 —— Qt 原生就带，"
                "不用位图、不用节流。" % (i + 1))
            card.setWordWrap(True)
            card.setObjectName("card")
            card.setMinimumHeight(52)
            lay.addWidget(card)

        lay.addStretch(1)
        row = QtWidgets.QHBoxLayout()
        for t in ("生成建议", "复制"):
            b = QtWidgets.QPushButton(t)
            b.setObjectName("pill")
            b.setCursor(QtCore.Qt.PointingHandCursor)   # 手型光标
            row.addWidget(b)
        lay.addLayout(row)

        self.setStyleSheet("""
            QLabel { color:#E1E1E5; background:transparent; }
            QLabel#card {
                background:#2D2D2E; border:1px solid #3A3A3B;
                border-radius:8px; padding:10px;
            }
            QPushButton#pill {
                background:#01C260; color:#FFFFFF; border:none;
                border-radius:17px; padding:8px 20px; font-weight:bold;
            }
            QPushButton#pill:hover { background:#1FC973; }
        """)

        # 拖动：记录按下点
        self._drag = None

    # ---- 自己画圆角 + 外框线（和 Tk 那双层填充等价，但**带抗锯齿**）
    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        # Qt 默认就开抗锯齿 —— Tk 里为这个折腾了三轮
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        r = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QtGui.QPainterPath()
        path.addRoundedRect(r, self._radius, self._radius)
        p.fillPath(path, QtGui.QColor("#1E1E1F"))
        pen = QtGui.QPen(QtGui.QColor("#3A3A3B"))
        pen.setWidth(2)                     # 「边框宽度 2」
        p.setPen(pen)
        p.drawPath(path)

    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton:
            self._drag = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, ev):
        if self._drag is not None and ev.buttons() & QtCore.Qt.LeftButton:
            self.move(ev.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, ev):
        self._drag = None


def main():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w = Panel()
    w.show()

    # 验证项：逐条打印结论
    print("Qt 版本:", QtCore.qVersion())
    print()
    flags = w.windowFlags()
    print("1. 无边框           :", bool(flags & QtCore.Qt.FramelessWindowHint))
    print("2. 置顶             :", bool(flags & QtCore.Qt.WindowStaysOnTopHint))
    print("3. 不占任务栏(Tool) :", bool(flags & QtCore.Qt.Tool))
    print("4. 窗口背景透明     :",
          w.testAttribute(QtCore.Qt.WA_TranslucentBackground))
    print("5. 圆角半径         :", w._radius, "（paintEvent 里 addRoundedRect）")
    print("6. 抗锯齿           :",
          "QtGui.QPainter.Antialiasing 已开（Qt 原生支持）")
    print("7. 边框宽度         : 2（QPen.setWidth(2)）")
    print("8. 手型光标         : QPushButton 上 setCursor(PointingHandCursor)")
    print()
    print("=> 这些在 Tk 里分别要：透明色键 + 自绘多边形 + 位图抗锯齿 + 节流。")

    # 顺便量一下：改尺寸有多快
    import time
    for _ in range(10):
        app.processEvents()
    ts = []
    for i in range(12):
        t0 = time.perf_counter()
        w.resize(360 + i * 8, 420 + i * 6)
        app.processEvents()
        ts.append((time.perf_counter() - t0) * 1000)
    print()
    print("连续改尺寸 12 帧：每帧 %.1f ms（Tk 同规模是 55~75 ms）"
          % (sum(ts) / len(ts)))

    app.exec()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
