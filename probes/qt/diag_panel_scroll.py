# -*- coding: utf-8 -*-
"""悬浮窗的回复区：卡片右边有没有被滚动条压住 / 贴着。

    python -X utf8 probes/qt/diag_panel_scroll.py

⚠️ 要查的问题（用户报的「右边还是有部分被拉动条遮挡住」）：
    `layout.py` 的注释写着"Qt 的滚动条是独立部件、不压内容，Tk 那套
    `SCROLL_PAD` 补丁不需要了"。**那句是断言，不是实测**。
    这里把悬浮窗的回复区量一遍：卡片的最右边界 vs 滚动条的左边界。

⚠️ 判据取**卡片自己的右边界**（不是容器宽度）：
    容器贴满视口、但卡片被写死得更宽 —— 那才是"被吃一块"的形态。
"""

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

from PySide6 import QtCore, QtWidgets                            # noqa: E402
from wechat_assistant import dpi                                 # noqa: E402

dpi.enable()
from wechat_assistant.qt_ui import base                          # noqa: E402
from wechat_assistant.qt_ui.layout import ScrollArea             # noqa: E402
from wechat_assistant.qt_ui.panel import BASE_WIDTH, FloatingAssistant  # noqa: E402


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


def pump(app, ms=400):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.004)


def main():
    app = base.init_app([])
    st = base.Style(None, name="dark")
    cfg = Cfg({"ui": {"width": 340, "radius": 10, "theme": "dark",
                      "frameless": True, "min_width": 200, "width_max": 640}})
    ui = FloatingAssistant(cfg, logger=lambda m: None,
                           on_regenerate=lambda i: None, on_read=lambda: None,
                           on_clear=lambda: None, on_settings=lambda: None,
                           session_source=lambda: {"会话": {}},
                           on_mode_change=lambda a, b: None,
                           on_config_changed=lambda c: None)
    ui.resize(st.px(BASE_WIDTH), st.px(560))
    ui.show()
    pump(app, 500)

    # 放三条足够长的建议，让滚动条真的出现
    ui.post_replies(["这是第一条比较长的建议内容用来把宽度撑开看看右边会不会被吃掉",
                     "第二条建议", "第三条建议"])
    pump(app, 700)

    print("== 悬浮窗回复区 ==")
    print("   窗口 %dx%d" % (ui.width(), ui.height()))

    areas = [a for a in ui.findChildren(ScrollArea)]
    print("   找到 %d 个滚动区" % len(areas))
    for i, sc in enumerate(areas):
        if not sc.isVisible():
            print("   #%d 不可见" % i)
            continue
        vp = sc.viewport()
        sb = sc.verticalScrollBar()
        inner = sc.widget()
        vp_r = vp.mapTo(ui, QtCore.QPoint(vp.width(), 0)).x()
        sb_l = sb.mapTo(ui, QtCore.QPoint(0, 0)).x() if sb.isVisible() else None
        print("\n   #%d 滚动区 %dx%d @(%d,%d)  滚动条 %s"
              % (i, sc.width(), sc.height(),
                 sc.mapTo(ui, QtCore.QPoint(0, 0)).x(),
                 sc.mapTo(ui, QtCore.QPoint(0, 0)).y(),
                 ("可见 w=%d x=%d" % (sb.width(), sb_l)) if sb_l is not None
                 else "隐藏"))
        print("      视口右边界=%d" % vp_r)

        # ⚠️ 卡片是最外层的内容部件；量**卡片**的右边界才是关键
        cards = []
        for ch in inner.findChildren(QtWidgets.QWidget):
            if not ch.isVisible() or ch.width() <= 1:
                continue
            try:
                x0 = ch.mapTo(ui, QtCore.QPoint(0, 0)).x()
                x1 = ch.mapTo(ui, QtCore.QPoint(ch.width(), 0)).x()
            except RuntimeError:
                continue
            if ch.height() > st.px(20) and ch.width() > st.px(60):
                cards.append((type(ch).__name__, x0, x1, ch.width()))
        if cards:
            worst = max(cards, key=lambda t: t[2])
            print("      最宽的内容部件：%s  x=%d..%d  宽=%d"
                  % (worst[0], worst[1], worst[2], worst[3]))
            if sb_l is not None:
                if worst[2] > sb_l:
                    print("      -> **右边界 %d 超过了滚动条左边界 %d，"
                          "压住 %d px**" % (worst[2], sb_l, worst[2] - sb_l))
                else:
                    print("      -> 没被压住（距滚动条还有 %d px）"
                          % (sb_l - worst[2]))
        # 内容右边界离滚动区右边界还有多少
        sc_r = sc.mapTo(ui, QtCore.QPoint(sc.width(), 0)).x()
        print("      滚动区右边界=%d" % sc_r)

    ui.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
