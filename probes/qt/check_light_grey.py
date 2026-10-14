# -*- coding: utf-8 -*-
"""浅色的灰**真的变浅了吗**（渲染层面量像素）。

    python -X utf8 probes/qt/check_light_grey.py

⚠️ 为什么不能只信 `theme.py` 里改了个色号：
    用户的原话是"浅色模式下的灰色太深了" —— 他看到的是**画出来的那块灰**。
    改色板只是输入，还得确认那块灰**真的**是用新色号画的、而且没被
    `surface_alt` 之外的什么地方（级联、自绘、样式表）盖住。
    这和项目里那条教训是同一条：**"值改了" ≠ "画出来变了"，一律量像素。**

⚠️ 量法沿用已经踩明白的那三个必需项（见 `audit_qss.py` / `check_pill_corner.py`）：
    渲染**子控件**、`renderFlags` 只留 `DrawChildren`、采样点落在实心区域里。
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
from wechat_assistant.qt_ui.layout import Panel                 # noqa: E402
from wechat_assistant.qt_ui.panel import FloatingAssistant      # noqa: E402

FAIL = []
#: 改之前用的那个灰（用户说太深的那个）
OLD = "#DCDCE1"


def check(name, ok, detail=""):
    print("  %s %-50s %s" % ("[通过]" if ok else "[失败]", name, detail))
    if not ok:
        FAIL.append(name)


def pump(app, ms=300):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.004)


def pick(img, x, y):
    c = QtGui.QColor(img.pixelColor(max(0, min(x, img.width() - 1)),
                                    max(0, min(y, img.height() - 1))))
    return "#%02X%02X%02X" % (c.red(), c.green(), c.blue())


def lum(h):
    r, g, b = (int(h[i:i + 2], 16) for i in (1, 3, 5))
    return 0.299 * r + 0.587 * g + 0.114 * b


def shoot(w):
    """把控件渲染到透明底上（只留子控件，见文件头说明）。"""
    pm = QtGui.QPixmap(max(1, w.width()), max(1, w.height()))
    pm.fill(QtCore.Qt.transparent)
    pt = QtGui.QPainter(pm)
    w.render(pt, QtCore.QPoint(0, 0), QtGui.QRegion(),
             QtWidgets.QWidget.RenderFlag.DrawChildren)
    pt.end()
    return pm.toImage()


class Cfg:
    def __init__(self, data=None):
        self._data = data or {}

    def get(self, path, default=None):
        node = self._data
        for part in str(path).split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def main():
    app = base.init_app([])
    st = base.Style(None, name="light")
    p = st.p
    print("浅色色板：surface_alt=%s  bg=%s  hover=%s  border=%s"
          % (p["surface_alt"], p["bg"], p["hover"], p["border"]))
    print("（改之前 surface_alt 是 %s，亮度 %.1f）\n" % (OLD, lum(OLD)))

    # ---- 1. 悬浮窗的标题栏（用的就是 surface_alt）----
    print("== 1. 悬浮窗标题栏 ==")
    cfg = Cfg({"ui": {"width": 340, "radius": 10, "theme": "light",
                      "frameless": True, "min_width": 200, "width_max": 640}})
    ui = FloatingAssistant(cfg, logger=lambda m: None,
                           on_regenerate=lambda i: None, on_read=lambda: None,
                           on_clear=lambda: None, on_settings=lambda: None,
                           session_source=lambda: {"会话": {}},
                           on_mode_change=lambda a, b: None,
                           on_config_changed=lambda c: None)
    ui.resize(st.px(340), st.px(430))
    ui.show()
    pump(app, 400)
    im = shoot(ui)
    # 标题栏是顶部那一条；取它垂直中点、水平靠中间（避开左右按钮）
    tb_h = st.px(ui.titlebar.TITLEBAR_H) if ui.titlebar else st.px(32)
    got = pick(im, im.width() // 2, max(1, tb_h // 2))
    check("标题栏画的是新的浅灰（不是旧的 %s）" % OLD,
          got.upper() != OLD, "实测 %s" % got)
    check("标题栏就是 surface_alt",
          got.upper() == p["surface_alt"].upper(),
          "%s vs %s" % (got, p["surface_alt"]))
    check("标题栏明显比正文底浅（跟正文底不是同一块）",
          abs(lum(got) - lum(p["bg"])) >= 8.0,
          "亮度差 %.1f" % abs(lum(got) - lum(p["bg"])))

    # ---- 2. 正文底 ----
    body = pick(im, im.width() // 2, im.height() - st.px(10))
    print("\n== 2. 正文底 ==")
    print("   底部正中 = %s（bg=%s）" % (body, p["bg"]))

    ui.deleteLater()
    pump(app, 150)

    # ---- 3. 面板（Panel）的圆角外壳用的是 surface_alt 吗 ----
    print("\n== 3. 外壳底色 ==")
    pn = Panel(st, radius=st.px(10), border_w=2)
    pn.resize(st.px(300), st.px(200))
    pn.show()
    pump(app, 300)
    im2 = shoot(pn)
    c = pick(im2, im2.width() // 2, 4)
    print("   外壳顶部 = %s" % c)
    check("外壳不是旧的深灰 %s" % OLD, c.upper() != OLD, c)

    pn.deleteLater()
    pump(app, 150)

    print("\n" + ("全部通过" if not FAIL else "失败 %d 项: %s" % (len(FAIL), FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
