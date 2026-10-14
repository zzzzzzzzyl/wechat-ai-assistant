# -*- coding: utf-8 -*-
"""悬浮窗布局体检：把每个部件的实际几何摊开，看哪里对不齐 / 被压 / 溢出。

    python -X utf8 probes/qt/diag_layout.py

⚠️ 我读不了图（模型不支持图片输入），所以**不能靠肉眼看**。
   这里改成把几何数字全部打出来，用数字找问题：
     · 各部件相对窗口的 x/y/w/h
     · 左右边距是否对称（不对称一眼就看得出）
     · 有没有部件被压成 0 高 / 溢出窗口
     · 滚动区里卡片的实际宽度 vs 可用宽度
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

from PySide6 import QtCore                                       # noqa: E402
from wechat_assistant import dpi                                 # noqa: E402

dpi.enable()
from wechat_assistant.qt_ui import base                          # noqa: E402
from wechat_assistant.qt_ui.cards import Card                    # noqa: E402
from wechat_assistant.qt_ui.panel import FloatingAssistant       # noqa: E402
from wechat_assistant.qt_ui.widgets import Pill                  # noqa: E402


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


def pump(app, ms=200):
    import time
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.004)


def geo(w, root):
    """部件相对**它自己的父级**的 (x, y, w, h)。

    ⚠️ 一开始用 `mapTo(root_ui, ...)`，结果垂直间距全算成负数
       （`titlebar -> intent_card 间距 = -443`）—— 因为 `ui` 是顶层窗口，
       子部件相对它的坐标已经含了父容器的偏移，两个不同父级的部件相减
       毫无意义。**相邻间距要在同一个父级（`body`）里算。**
    """
    if w is None:
        return None
    return (w.x(), w.y(), w.width(), w.height())


def row(name, g, win_w, win_h):
    if g is None:
        print("  %-14s  (无)" % name)
        return
    x, y, w, h = g
    left, right = x, win_w - (x + w)
    flags = []
    if h <= 1:
        flags.append("!!高度被压成0")
    if w <= 1:
        flags.append("!!宽度被压成0")
    if x < 0 or y < 0:
        flags.append("!!溢出(负)")
    if x + w > win_w:
        flags.append("!!右侧溢出 %d" % (x + w - win_w))
    if y + h > win_h:
        flags.append("!!底部溢出 %d" % (y + h - win_h))
    print("  %-14s x=%-4d y=%-4d w=%-4d h=%-4d  左边距=%-4d 右边距=%-4d %s"
          % (name, x, y, w, h, left, right, " ".join(flags)))
    return (left, right)


def main():
    app = base.init_app([])
    px = base.px
    print("缩放=%.2f  px(1)=%d px(10)=%d px(38)=%d"
          % (base.scale(), px(1), px(10), px(38)))

    cfg = Cfg({"ui": {"width": 340, "radius": 10, "theme": "dark"}})
    ui = FloatingAssistant(cfg, logger=lambda m: None)
    ui.resize(px(340), px(560))
    ui.show()
    pump(app, 300)
    ui.start_pump()
    ui.post_replies(["这是第一条建议，故意写长一点，看看换行之后的排版和右侧留白是不是对称。",
                     "第二条建议。",
                     "第三条建议，长度介于前两条之间。"],
                    "", "对方在问今晚吃什么")
    pump(app, 500)

    W, H = ui.width(), ui.height()
    print("\n窗口 %dx%d  圆角半径=%.0f  内容缩进=%d"
          % (W, H, ui.radius, ui._content_inset()))

    print("\n== 主结构 ==")
    inset = ui._content_inset()
    row("titlebar", geo(ui.titlebar, ui), W, H)
    row("intent_card", geo(ui.intent_card, ui), W, H)
    row("scroll", geo(ui.scroll, ui), W, H)
    row("input", geo(ui.input, ui), W, H)
    row("btn_gen", geo(ui.btn_gen, ui), W, H)
    row("btn_read", geo(ui.btn_read, ui), W, H)
    row("statusbar", geo(ui.lbl_status, ui), W, H)

    print("\n== 对齐检查：左右边距应该一致 ==")
    # ⚠️ x 要相对**同一个父级（body）**算，否则拿子控件的父级相对坐标
    #    去和窗口宽度比，出来的是假的不对称。
    body = ui.input.parent() if ui.input is not None else None
    for name, w in (("intent_card", ui.intent_card), ("scroll", ui.scroll),
                    ("input", ui.input)):
        if w is None:
            continue
        p = w.mapTo(body, QtCore.QPoint(0, 0))
        left = p.x()
        right = body.width() - (p.x() + w.width())
        flag = ""
        if abs(left - right) > 1:
            flag = "   <<< 左右不对称，差 %d px" % abs(left - right)
        print("  %-14s 相对 body: x=%-4d w=%-4d  左边距=%-4d 右边距=%-4d%s"
              % (name, p.x(), w.width(), left, right, flag))

    print("\n== 滚动区内部 ==")
    if ui.titlebar is not None:
        tb = ui.titlebar
        print("  标题栏尺寸 %dx%d" % (tb.width(), tb.height()))
        for act, b in tb.buttons.items():
            g = geo(b, ui)
            print("  按钮 %-10s x=%-4d y=%-4d w=%-3d h=%-3d  距右边缘=%d"
                  % (act, g[0], g[1], g[2], g[3], W - (g[0] + g[2])))
        # 按钮之间的间距
        xs = sorted((geo(b, ui)[0], act) for act, b in tb.buttons.items())
        print("  按钮从左到右:", xs)
        if len(xs) > 1:
            gaps = [xs[i + 1][0] - (xs[i][0] + tb.buttons[xs[i][1]].width())
                    for i in range(len(xs) - 1)]
            print("  按钮间隔:", gaps)
        # 标题文字可用宽度 vs 按钮区起点
        first_x = xs[0][0] if xs else W
        print("  标题文字可用宽度 = 首个按钮左边 - 12px 左边距 = %d"
              % (first_x - px(12)))

    body = ui.input.parent() if ui.input is not None else None
    print("\n== 垂直间距（全部映射到 body 坐标系，才可比）==")
    if body is not None:
        seq = [("titlebar", ui.titlebar), ("intent_card", ui.intent_card),
               ("scroll", ui.scroll), ("input", ui.input),
               ("act", ui.btn_gen.parent()), ("statusbar", ui.lbl_status)]
        prev = None
        for name, w in seq:
            if w is None:
                continue
            if name in ("titlebar", "statusbar"):
                p = w.mapTo(ui, QtCore.QPoint(0, 0))
                g = (p.x(), p.y() - ui._content_inset(),
                     w.width(), w.height())
            else:
                p = w.mapTo(body, QtCore.QPoint(0, 0))
                g = (p.x(), p.y(), w.width(), w.height())
            print("  %-12s y=%-4d h=%-4d" % (name, g[1], g[3]))
            if prev is not None:
                gap = g[1] - (prev[1] + prev[3])
                print("      ^^ 与 %-12s 间距 = %d px%s"
                      % (prev[0], gap, "  <<< 重叠!" if gap < 0 else ""))
            prev = (name,) + g

    print("\n== 控件内部（按钮/输入框）==")
    print("  生成按钮 h=%d（期望 px(34)=%d）  圆角=%d"
          % (ui.btn_gen.height(), px(34), ui.btn_gen.height() // 2))
    print("  只看对话 h=%d" % ui.btn_read.height())
    print("  输入框 h=%d（期望 px(58)=%d）" % (ui.input.height(), px(58)))
    print("  状态栏 h=%d" % ui.lbl_status.height())
    print("  意图卡 h=%d" % ui.intent_card.height())

    print("\n== 深色色板（实测自微信）==")
    p = ui.style.p
    for k in ("bg", "surface", "surface_alt", "border", "text", "text_dim",
              "text_faint", "accent", "accent_text", "hover", "shell_border"):
        print("  %-12s %s" % (k, p.get(k)))

    ui.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
