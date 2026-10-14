# -*- coding: utf-8 -*-
"""真窗口（非 offscreen）下把 Qt 悬浮窗建出来看一眼几何。

    python probes/qt/check_real_window.py

⚠️ 和 `diag_panel.py` 的区别：那个跑在 `QT_QPA_PLATFORM=offscreen` 下，
   **量不到真实 DPI**（`logicalDotsPerInch()` 恒为 96，靠系统 API 兜底）。
   这个用真的 windows 平台插件，验的是"真机上建得出来、缩放对、不闪退"。
"""
import os
import sys
import time

os.environ.pop("QT_QPA_PLATFORM", None)      # 用真的平台插件
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PySide6 import QtCore, QtGui, QtWidgets                     # noqa: E402
from wechat_assistant.qt_ui import base                          # noqa: E402
from wechat_assistant.qt_ui.panel import FloatingAssistant       # noqa: E402


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


def pump(app, ms):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


def main():
    app = base.init_app([])
    scr = app.primaryScreen()
    print("平台插件 : %s" % app.platformName())
    print("屏幕      : %s" % scr.geometry())
    print("logicalDotsPerInch : %.1f" % scr.logicalDotsPerInch())
    print("devicePixelRatio   : %.2f" % scr.devicePixelRatio())
    print("缩放      : %.2f  px(10)=%d  px(340)=%d"
          % (base.scale(), base.px(10), base.px(340)))

    cfg = Cfg({"ui": {"width": 340, "radius": 10, "theme": "dark"}})
    ui = FloatingAssistant(cfg, logger=lambda m: print("  log:", m))
    ui.show()
    pump(app, 400)

    print("\n建出来了 : %s" % ui.isVisible())
    print("几何      : %dx%d @ (%d,%d)"
          % (ui.width(), ui.height(), ui.x(), ui.y()))
    print("标题栏高  : %d (期望 %d)" % (ui.titlebar.height(), base.px(38)))
    print("状态栏 y  : %d / 窗口高 %d"
          % (ui.lbl_status.mapTo(ui, QtCore.QPoint(0, 0)).y(), ui.height()))
    h = ui.windowHandle()
    print("窗口句柄  : %s (QWindow=%s)"
          % (hex(int(h.winId())) if h else "None", h is not None))

    from wechat_assistant.follow import ui_native_handle, ui_rect
    print("顶层句柄  : 0x%X" % ui_native_handle(ui))
    print("ui_rect   : %s" % (ui_rect(ui),))

    # 抓一张自己的位图存下来供目视（用窗口自己的渲染，不用屏幕截图 ——
    # ⚠️ 本机的 ImageGrab 实测不可信，抓纯色矩形都是一片灰）
    try:
        pm = ui.grab()
        out = os.path.join(ROOT, "out", "qt_panel_real.png")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        pm.save(out)
        print("已存图    : %s  (%dx%d)" % (out, pm.width(), pm.height()))
    except Exception as e:
        print("存图失败  : %s: %s" % (type(e).__name__, e))

    ui.post_replies(["第一条建议，稍微长一点看看换行是不是正常，右侧不应该被截掉。",
                     "第二条。", "第三条。"], "", "对方在问今晚吃什么")
    pump(app, 600)
    print("三条卡片  : %d 张" % len([
        ui.scroll._lay.itemAt(i).layout().itemAt(0).widget()
        for i in range(ui.scroll._lay.count())
        if ui.scroll._lay.itemAt(i).layout() is not None]))
    try:
        pm = ui.grab()
        out = os.path.join(ROOT, "out", "qt_panel_real_replies.png")
        pm.save(out)
        print("已存图    : %s" % out)
    except Exception as e:
        print("存图失败  : %s" % e)

    ui.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
