# -*- coding: utf-8 -*-
"""量「按钮到底画出什么」—— 重点是**禁用态**和**ghost 变体**。

用户报：
    「生成建议时窗口会闪两下？还有生成中没有边框，颜色固定浅白色，只看对话也是」

先把"生成中""只看对话"这两个东西**量出来**再改。纯读代码得到的结论
在这台机器上错过不止一次（`{{` 双花括号那次：字符串里读得到绿色，
Qt 却整条解析失败、退回系统默认的浅灰白按钮）。所以这里一律 **渲染 + 量像素**。

量的三件事
----------
  ① 背景：铺一层洋红再渲染，**还看得到洋红 = 这个控件根本没画底色**
  ② 描边：取顶边正中那个像素（半径 6 的按钮在正中 x 处 y=0 就是描边所在），
     跟填充色比 —— 一样就说明"没有边框"
  ③ 主题无关性：同一个按钮在**深色 / 浅色**两套下量，两个值相同 = "颜色固定"

用法：python probes/qt/diag_buttons_state.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

MAGENTA = "#FF00FF"


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


def hexs(c):
    return "#%02X%02X%02X" % (c.red(), c.green(), c.blue())


def shoot(widget, QtCore, QtGui, QtWidgets):
    """把控件渲染到洋红底上，返回 (顶边正中色, 中心色, 洋红像素数, 宽, 高)。

    ⚠️ `renderFlags` **只留 `DrawChildren`**：默认还会 `DrawWindowBackground`，
       它先用调色板底色把目标刷一遍，"洋红透出来"这个判据就整个失效了。
    ⚠️ 它是**第 4 个**参数，第 3 个 `sourceRegion` 不能跳过（否则 TypeError）。
    """
    w = max(1, widget.width())
    h = max(1, widget.height())
    pm = QtGui.QPixmap(w, h)
    pm.fill(QtGui.QColor(MAGENTA))
    pt = QtGui.QPainter(pm)
    widget.render(pt, QtCore.QPoint(0, 0), QtGui.QRegion(),
                  QtWidgets.QWidget.RenderFlag.DrawChildren)
    pt.end()
    im = pm.toImage()
    top = im.pixelColor(w // 2, 0)
    mid = im.pixelColor(w // 2, h // 2)
    n_mag = 0
    for y in range(h):
        for x in range(w):
            if hexs(im.pixelColor(x, y)) == MAGENTA:
                n_mag += 1
    return hexs(top), hexs(mid), n_mag, w, h


def main():
    print("=" * 74)
    print("按钮状态量测：背景 / 描边 / 是不是随主题变")
    print("=" * 74)

    try:
        from PySide6 import QtCore, QtGui, QtWidgets
        from wechat_assistant import dpi
        from wechat_assistant.qt_ui import base
        from wechat_assistant.qt_ui.panel import FloatingAssistant
    except Exception as e:
        print("  PySide6 不可用: %s: %s" % (type(e).__name__, e))
        return 1

    dpi.enable()
    app = base.init_app([])

    cfg = Cfg({"ui": {"width": 340, "radius": 10, "theme": "dark",
                      "frameless": True, "min_width": 200, "width_max": 640}})
    ui = FloatingAssistant(cfg, logger=lambda m: None,
                           on_regenerate=lambda i: None,
                           on_read=lambda: None,
                           on_clear=lambda: None,
                           on_settings=lambda: None,
                           session_source=lambda: {"会话": {}},
                           on_mode_change=lambda a, b: None,
                           on_config_changed=lambda c: None)
    ui.resize(base.px(340), base.px(560))
    ui.show()
    for _ in range(60):
        app.processEvents()

    targets = [("主按钮 btn_gen", "btn_gen"),
               ("次按钮 btn_read", "btn_read")]
    if getattr(ui, "btn_prompt", None) is not None:
        targets.append(("看提示词 btn_prompt", "btn_prompt"))

    rows = {}
    for theme in ("dark", "light"):
        ui.set_theme(theme)
        for _ in range(40):
            app.processEvents()
        p = ui.style.p
        print("\n--- 主题 %s ---   bg=%s surface=%s border=%s text=%s "
              "text_faint=%s"
              % (theme, p["bg"], p["surface"], p["border"], p["text"],
                 p["text_faint"]))
        for label, attr in targets:
            for state in ("正常", "禁用"):
                b = getattr(ui, attr, None)
                if b is None:
                    continue
                b.set_enabled(state == "正常")
                # ⚠️ 悬停态由**真实鼠标位置**决定，量之前必须显式归零，
                #    否则同一份代码会时好时坏（这条踩过）。
                b._hover = False
                b._apply()
                for _ in range(10):
                    app.processEvents()
                if state == "禁用":
                    # 禁用态下 setText/set_enabled 之外，Qt 还会画自己的
                    # "disabled" 效果；这里只关心我们自己画的底色/描边。
                    pass
                top, mid, n_mag, w, h = shoot(b, QtCore, QtGui, QtWidgets)
                bg, fg, bd = b.colors()
                has_border = (top != mid)
                rows.setdefault((label, state), {})[theme] = (top, mid)
                print("  %-16s %-4s variant=%-9s 声明的 bg=%-9s bd=%-9s | "
                      "量的 顶边=%-9s 中心=%-9s | 洋红残留=%d/%d | 边框=%s"
                      % (label, state, b.variant, bg, bd, top, mid, n_mag,
                         w * h, "有" if has_border else "无"))
                b.set_enabled(True)
                b._hover = False
                b._apply()

    print("\n" + "=" * 74)
    print("汇总：同一按钮在两套主题下的取值（相同 = 颜色与主题无关）")
    print("=" * 74)
    for (label, state), d in rows.items():
        if "dark" in d and "light" in d:
            same = d["dark"] == d["light"]
            print("  %-16s %-4s  深色=%s  浅色=%s   %s"
                  % (label, state, d["dark"], d["light"],
                     "⚠️ 两套一样（与主题无关）" if same else "随主题变"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
