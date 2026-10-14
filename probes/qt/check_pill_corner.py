# -*- coding: utf-8 -*-
"""胶囊按钮的圆角**真的被裁出来了吗**（量边缘像素）。

    python -X utf8 probes/qt/check_pill_corner.py

⚠️ 这个探针写错过两版，两个错法都值得记：

  ① **把按钮当顶层控件渲染**。真实界面里按钮是**子控件**（在布局里），
     而顶层控件会被平台按"窗口"那条路径画，走的是另一套代码。
     第一版量的是顶层按钮，结论不能代表界面里的按钮。
     → 现在把按钮放进一个容器布局里，渲染**容器**。

  ② **没关 `DrawWindowBackground`**。`QWidget.render()` 的默认 flags 是
     `DrawWindowBackground | DrawChildren`，它会**先用调色板底色把目标刷一遍**
     （实测是 `#EFEFEF`）。于是"透明的地方透出洋红"这个判据直接失效 ——
     画面上永远看不到洋红，量到的全是调色板底色。
     → 现在显式只传 `DrawChildren`。

⚠️ 判据：圆角真被裁掉的话，**角上的像素不该是按钮自己的底色**。
   为了能分辨"没画"和"画了但被裁"，容器**不设**自己的背景，
   角上应该透出洋红或抗锯齿的混色。
"""
import math
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
from wechat_assistant.qt_ui.widgets import Pill                 # noqa: E402

FAIL = []
MAGENTA = "#FF00FF"


def check(name, ok, detail=""):
    print("  %s %-48s %s" % ("[通过]" if ok else "[失败]", name, detail))
    if not ok:
        FAIL.append(name)


def pump(app, ms=200):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.004)


def hex_of(im, x, y):
    c = QtGui.QColor(im.pixelColor(max(0, min(x, im.width() - 1)),
                                   max(0, min(y, im.height() - 1))))
    return "#%02X%02X%02X" % (c.red(), c.green(), c.blue())


def render_flat(w):
    """把控件渲染到洋红底上。

    ⚠️ 两个必需项：
       · hover 先归零 —— hover 由**真实鼠标位置**决定，不归零的话
         量的就是鼠标停在哪，同一份代码会时绿时深绿（实测踩到过）。
       · render flags 只留 `DrawChildren` —— 默认那个会把目标先用
         调色板底色刷掉，洋红就再也量不到了。
    """
    if hasattr(w, "_apply"):
        w._hover = False
        w._apply()
    pm = QtGui.QPixmap(max(1, w.width()), max(1, w.height()))
    pm.fill(QtGui.QColor(MAGENTA))
    pt = QtGui.QPainter(pm)
    # ⚠️ 参数顺序是 `(painter, targetOffset, sourceRegion, renderFlags)` ——
    #    `renderFlags` 是**第 4 个**，不能跳过第 3 个直接传，否则 PySide6
    #    匹配不上重载、抛 TypeError。空的 QRegion 表示"整块都要"。
    w.render(pt, QtCore.QPoint(0, 0), QtGui.QRegion(),
             QtWidgets.QWidget.RenderFlag.DrawChildren)
    pt.end()
    return pm.toImage()


def make_host(app, st, **kw):
    """把 Pill 放进**容器布局**里（跟真实界面一样），返回 (host, pill)。"""
    host = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    b = Pill(st, kw.pop("text", "生成建议"), **kw)
    lay.addWidget(b)
    host.resize(base.px(220), b.height())
    host.show()
    pump(app, 250)
    return host, b


def main():
    app = base.init_app([])
    st = base.Style(None, name="dark")

    print("== 0. 先证明这个量法本身有效 ==")
    # 空白子控件不画任何东西，角上**必须**是洋红。
    # 这一条要是不过，后面所有"角是不是洋红"的判据都不可信。
    blank = QtWidgets.QWidget()
    blank.resize(base.px(120), base.px(34))
    blank.show()
    pump(app, 150)
    px0 = hex_of(render_flat(blank), 0, 0)
    check("空白控件角上是洋红（量法有效，洋红能量到）", px0 == MAGENTA, px0)

    print("\n== 1. 主按钮（胶囊）的圆角（量像素，不看样式表字符串）==")
    host, b = make_host(app, st, variant="primary", expand=True)
    im = render_flat(host)
    # 按钮在容器里的位置：容器就是按钮大小（上面 resize 按 b.height()）。
    x0, y0 = b.mapTo(host, QtCore.QPoint(0, 0)).x(), \
        b.mapTo(host, QtCore.QPoint(0, 0)).y()
    h = b.height()
    r = h // 2
    print("  按钮 %dx%d @ (%d,%d)  圆角半径 %d" % (b.width(), b.height(), x0, y0, r))

    # ① 圆角正对外面的那个像素。胶囊的圆角中心在 (x0+r, y0+r)，
    #    所以"最外角"是 (x0, y0)。
    corners = [("左上", x0, y0),
               ("右上", x0 + b.width() - 1, y0),
               ("左下", x0, y0 + h - 1),
               ("右下", x0 + b.width() - 1, y0 + h - 1)]
    for label, x, y in corners:
        px = hex_of(im, x, y)
        check("%s角被裁掉了（不是按钮底色）" % label, px != "#01C260", px)

    # ② 弧线上要有**多个不同**的值 —— 这才是抗锯齿的决定性证据。
    #    ⚠️ 抗锯齿不会凭空多出像素，它只是改每个边缘像素的 alpha，
    #       所以判据是"存在中间色"，不是"过渡像素很多"。
    arc = []
    for t in range(41):
        ang = (math.pi / 2.0) * (t / 40.0)
        x = x0 + int(round(r - r * math.cos(ang)))
        y = y0 + int(round(r - r * math.sin(ang)))
        arc.append(hex_of(im, x, y))
    uniq = sorted(set(arc))
    check("弧线上有抗锯齿的中间色（不是硬边）", len(uniq) >= 3,
          "%d 种：%s" % (len(uniq), uniq[:5]))

    # ③ 中线必须还是按钮底色（确认按钮真的画出来了）
    mid = hex_of(im, x0 + b.width() // 2, y0 + r)
    check("按钮中线是它自己的底色（没画丢）", mid != MAGENTA, mid)

    print("\n" + ("全部通过" if not FAIL else "失败 %d 项: %s" % (len(FAIL), FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
