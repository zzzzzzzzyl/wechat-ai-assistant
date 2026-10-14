# -*- coding: utf-8 -*-
"""输入框里**选中文字的高亮**到底是什么颜色（量像素）。

    python -X utf8 probes/qt/check_selection_color.py

⚠️ 要查的问题（用户报的）：
    「深色与浅色时……字体高亮并不是微信设置的颜色」。
    我们在 `qt_ui/*` 里**一次都没有设过** `selection-background-color` /
    `selection-color`（grep 全文只有 inputs.py 的一句注释提到 highlightthickness），
    也就是说输入框选中文字时用的是 **Qt/系统的默认高亮**（Windows 上是蓝色），
    那显然不是微信的颜色。

⚠️ 为什么必须量像素而不是看代码：
    这条可以"看起来改过了"但实际上没生效 —— 比如样式表拼错、
    或者被 `_edit_qss` 重新生成时覆盖掉。所以这里**真的选中一段文字**，
    再把它渲染出来取像素。判据是「高亮像素等于我们设定的色」+「等于微信绿那一族」。
"""

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

from PySide6 import QtCore, QtGui, QtWidgets                    # noqa: E402
from wechat_assistant import dpi                                # noqa: E402

dpi.enable()
from wechat_assistant.qt_ui import base                         # noqa: E402
from wechat_assistant.qt_ui.inputs import EntryBox, InputBox    # noqa: E402

FAIL = []


def check(name, ok, detail=""):
    print("  %s %-52s %s" % ("[通过]" if ok else "[失败]", name, detail))
    if not ok:
        FAIL.append(name)


def pump(app, ms=200):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.004)


def hexs(c):
    return "#%02X%02X%02X" % (c.red(), c.green(), c.blue())


def render(img_holder, w):
    pm = QtGui.QPixmap(max(1, w.width()), max(1, w.height()))
    pm.fill(QtGui.QColor("#FF00FF"))
    pt = QtGui.QPainter(pm)
    w.render(pt, QtCore.QPoint(0, 0), QtGui.QRegion(),
             QtWidgets.QWidget.RenderFlag.DrawChildren)
    pt.end()
    return pm.toImage()


def region_colors(im, x0, y0, x1, y1, n=4):
    """区域内出现最多的几种颜色（用来找高亮块）。"""
    from collections import Counter
    cnt = Counter()
    for y in range(max(0, y0), min(im.height(), y1)):
        for x in range(max(0, x0), min(im.width(), x1)):
            c = QtGui.QColor(im.pixelColor(x, y))
            cnt["#%02X%02X%02X" % (c.red(), c.green(), c.blue())] += 1
    total = sum(cnt.values()) or 1
    return [(c, 100.0 * k / total) for c, k in cnt.most_common(n)]


def is_greenish(h):
    r, g, b = (int(h[i:i + 2], 16) for i in (1, 3, 5))
    return g > r + 25 and g > b + 15


def is_blueish(h):
    """Qt 在 Windows 上的默认高亮是蓝的 —— 用这个当反例。"""
    r, g, b = (int(h[i:i + 2], 16) for i in (1, 3, 5))
    return b > r + 40 and b > g + 20


def check_widget(app, st, w, label, theme_name):
    w.resize(st.px(300), st.px(60))
    w.show()
    pump(app, 250)
    # 全部选中
    if hasattr(w, "selectAll"):
        w.selectAll()
    pump(app, 150)
    im = render(None, w)
    tops = region_colors(im, 2, 2, im.width() - 2, im.height() - 2, n=5)
    print("    %s 区域主色：%s" % (label, ", ".join(
        "%s(%.0f%%)" % (c, p) for c, p in tops)))
    # 高亮块通常既不是底色也不是字色；挑第一个"有彩度"的当高亮
    hl = None
    for c, p in tops:
        if is_greenish(c) or is_blueish(c):
            hl = c
            break
    if hl is None:
        hl = tops[0][0]
    check("%s[%s]：选中高亮**不是 Qt 默认的蓝**" % (label, theme_name),
          not is_blueish(hl), hl)
    check("%s[%s]：选中高亮是微信绿那一族" % (label, theme_name),
          is_greenish(hl), hl)
    return hl


def main():
    app = base.init_app([])
    print("== 输入框选中文字的高亮色（洋红底上渲染，量像素）==")
    for theme_name in ("dark", "light"):
        st = base.Style(None, name=theme_name)
        print("\n-- %s --" % theme_name)
        e = EntryBox(st, "选中我这一段文字")
        check_widget(app, st, e, "EntryBox", theme_name)
        e.deleteLater()
        pump(app, 100)

        box = InputBox(st, height=60, placeholder="x")
        box.set("选中我这一段文字")
        check_widget(app, st, box, "InputBox", theme_name)
        box.deleteLater()
        pump(app, 100)

    print("\n" + ("全部通过" if not FAIL else "失败 %d 项: %s" % (len(FAIL), FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
