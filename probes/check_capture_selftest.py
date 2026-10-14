# -*- coding: utf-8 -*-
"""自检：`capture.capture_window()`（PrintWindow）在**这台机器的当下**还能不能用。

    python probes/check_capture_selftest.py

为什么需要
----------
整条「读微信内容」的链路都建立在 PrintWindow 上：发送者判定、图片 OCR、
以及新的「UIA 读不到时改走 OCR」都一样。它一旦静默失效，症状是
**所有消息的发送者都变成 `?`**，而不是报错 —— 很难联想到"抓图坏了"。

而且它**确实会失效**，且原因常常不在代码里：
  · 窗口最小化                → 整幅空白
  · 微信停在**扫码登录窗**     → 那个窗口也叫「微信」、约 300x400，PrintWindow 全黑
  · 别的东西盖住了 / 换了显示器 / 会话被锁

做法：**不用微信**，自己建一个已知内容的窗口，抓它，验证像素对不对得上。
自建窗口的内容是我们自己画的，所以"抓到什么"有唯一正确答案 ——
比拿微信去试要确定得多（微信那边你分不清是抓图坏了还是它没渲染）。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

from wechat_assistant import capture           # noqa: E402
from wechat_assistant import dpi as _dpi       # noqa: E402

dpi_enabled = _dpi.enable()

W, H = 600, 400
# 四象限：左上红 / 右上绿 / 左下蓝 / 右下白
EXPECT = {
    (150, 100): (255, 0, 0),
    (450, 100): (0, 255, 0),
    (150, 300): (0, 0, 255),
    (450, 300): (255, 255, 255),
}


class Probe(QtWidgets.QWidget):
    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        p.fillRect(0, 0, W // 2, H // 2, QtGui.QColor(255, 0, 0))
        p.fillRect(W // 2, 0, W // 2, H // 2, QtGui.QColor(0, 255, 0))
        p.fillRect(0, H // 2, W // 2, H // 2, QtGui.QColor(0, 0, 255))
        p.fillRect(W // 2, H // 2, W // 2, H // 2, QtGui.QColor(255, 255, 255))


def main() -> int:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    print("DPI 感知 = %s" % dpi_enabled)

    w = Probe()
    w.setWindowTitle("capture-selftest")
    w.resize(W, H)
    w.move(60, 60)
    w.show()
    for _ in range(40):
        app.processEvents()
        QtCore.QThread.msleep(10)
    QtCore.QThread.msleep(300)
    app.processEvents()

    hwnd = int(w.winId())
    got = capture.capture_window(hwnd)
    print("窗口 hwnd=0x%X  逻辑 %dx%d" % (hwnd, W, H))

    ok = True
    if got is None:
        print("  ✗ capture_window 返回 None")
        ok = False
    else:
        print("  位图 %dx%d（物理像素，缩放 %.2f）"
              % (got.width, got.height, got.width / float(W)))
        scale = got.width / float(W)
        print("  is_blank = %s" % capture.is_blank(got))
        if capture.is_blank(got):
            ok = False
        for (lx, ly), want in sorted(EXPECT.items()):
            px = got.getpixel((int(lx * scale), int(ly * scale)))[:3]
            hit = max(abs(px[i] - want[i]) for i in range(3)) <= 12
            ok = ok and hit
            print("  逻辑(%3d,%3d) 期望 %-16s 实得 %-16s %s"
                  % (lx, ly, want, tuple(px), "✓" if hit else "✗"))

    w.close()
    app.processEvents()

    print("\n结论：%s" % ("PrintWindow 正常，这条路可以走"
                         if ok else "**PrintWindow 抓不到内容** —— 先查"
                                     "「窗口是不是最小化 / 微信是不是停在扫码登录窗」，"
                                     "再查显示器和会话"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
