# -*- coding: utf-8 -*-
"""设置窗口左侧导航：**跟我们量到的微信值**对不对得上。

    python -X utf8 probes/qt/audit_settings_nav.py

⚠️ 这个探针存在的理由（用户报的）：
    「深色与浅色时微信设置的颜色与字体高亮并不是微信设置的颜色」。
    实测微信设置窗口（`analyze_settings_nav.py`）得到的是：
        导航栏底色  #2F2F30 （深色；就是色板里的 `surface`）
        选中项底色  #0DA869 **实心绿**（占该块 93.3% 的像素）
        选中项文字  #E7F6F0 （近白带一点绿）
        形状        **直角矩形**，铺满导航栏宽度，240x60 物理像素
        文字        **左对齐**（左边距 27 物理px，右边距 69）
    这里把**渲染出来的设置窗口**按同样的方式量一遍，逐条比。

⚠️ 全部量像素，不信 `variant` 字符串 —— 这个项目已经栽过一次
    「字符串对 ≠ 画出来对」（QSS 双花括号那件事）。
"""

import os
import sys
import time
from collections import Counter

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

from PySide6 import QtCore, QtGui, QtWidgets                    # noqa: E402
from wechat_assistant import dpi, theme                          # noqa: E402

dpi.enable()
from wechat_assistant.qt_ui import base                          # noqa: E402
from wechat_assistant.qt_ui import settings_ui                   # noqa: E402

FAIL = []


def check(name, ok, detail=""):
    print("  %s %-50s %s" % ("[通过]" if ok else "[失败]", name, detail))
    if not ok:
        FAIL.append(name)


def pump(app, ms=250):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.004)


def hexs(c):
    return "#%02X%02X%02X" % (c.red(), c.green(), c.blue())


def lum(c):
    return 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()


def shoot(w):
    pm = QtGui.QPixmap(max(1, w.width()), max(1, w.height()))
    pm.fill(QtGui.QColor("#FF00FF"))
    pt = QtGui.QPainter(pm)
    w.render(pt, QtCore.QPoint(0, 0), QtGui.QRegion(),
             QtWidgets.QWidget.RenderFlag.DrawChildren)
    pt.end()
    return pm.toImage()


def main():
    app = base.init_app([])

    for tname in ("dark", "light"):
        st = base.Style(None, name=tname)
        p = st.p
        print("\n== %s：设置窗口左侧导航 ==" % tname)

        # 直接建一个导航栏（跟 settings_ui 里同构），避开建整个设置窗口的
        # 一堆依赖（store / registry）—— 这里要验的是**导航项本身的样式**。
        nav = QtWidgets.QWidget()
        # ⚠️ 用 `nav_bg`（跟 `settings_ui` 一致）。别写 `surface` ——
        #    深色下两者同值、浅色下**不同**（浅色导航栏是 #EEEEF0，
        #    而 surface 是白），写错的话浅色这套会静默量成通过。
        nav.setStyleSheet("background:%s;" % p["nav_bg"])
        nav.setFixedWidth(st.px(settings_ui.NAV_W))
        lay = QtWidgets.QVBoxLayout(nav)
        lay.setContentsMargins(0, st.px(8), 0, st.px(8))
        lay.setSpacing(0)
        items = []
        for i, name in enumerate(("设置项一", "设置项二", "设置项三")):
            b = settings_ui.Pill(
                st, name, variant="navsel" if i == 1 else "text",
                width=st.px(settings_ui.NAV_W),
                height=st.px(settings_ui.NAV_ITEM_H),
                radius=0, pad=0,
                text_inset=st.px(settings_ui.NAV_TEXT_INSET), parent=nav)
            lay.addWidget(b, 0)
            items.append(b)
        lay.addStretch(1)
        nav.resize(st.px(settings_ui.NAV_W),
                   st.px(settings_ui.NAV_ITEM_H) * 3 + st.px(16))
        nav.show()
        pump(app, 350)

        im = shoot(nav)
        W, H = im.width(), im.height()

        # ---- 1. 导航栏底色 = nav_bg ----
        navbg = hexs(im.pixelColor(W - 3, H - 3))
        check("导航栏底色是 nav_bg(%s)" % p["nav_bg"],
              navbg.upper() == p["nav_bg"].upper(), navbg)

        # ---- 2. 选中项：找绿色块 ----
        def is_green(c):
            return c.green() > c.red() + 20 and c.green() > c.blue() + 20
        xs, ys = [], []
        for y in range(H):
            for x in range(W):
                if is_green(im.pixelColor(x, y)):
                    xs.append(x)
                    ys.append(y)
        check("左栏里能找到绿色选中块", bool(xs),
              "%d 个绿色像素" % len(xs))
        if not xs:
            nav.deleteLater()
            pump(app, 80)
            continue

        gx0, gx1, gy0, gy1 = min(xs), max(xs), min(ys), max(ys)
        w_phys, h_phys = gx1 - gx0 + 1, gy1 - gy0 + 1
        print("     绿块 x %d~%d y %d~%d  尺寸 %dx%d（导航栏宽 %d）"
              % (gx0, gx1, gy0, gy1, w_phys, h_phys, W))

        # 底色：块内最高频的绿色
        cnt = Counter()
        for y in range(gy0, gy1 + 1):
            for x in range(gx0, gx1 + 1):
                cnt[hexs(im.pixelColor(x, y))] += 1
        bgs = [c for c, _ in cnt.most_common(40)
               if is_green(QtGui.QColor(c))]
        blkbg = bgs[0] if bgs else cnt.most_common(1)[0][0]
        check("选中项底色是 accent_dim(%s) —— 微信实测 #0DA869"
              % p["accent_dim"], blkbg.upper() == p["accent_dim"].upper(),
              blkbg)

        # ---- 3. 形状：铺满导航栏宽度 + 直角 ----
        check("选中项**铺满**导航栏宽度（微信实测就是铺满）",
              w_phys >= W - 2, "%d / %d" % (w_phys, W))
        corners = [im.pixelColor(gx0, gy0), im.pixelColor(gx1, gy0),
                   im.pixelColor(gx0, gy1), im.pixelColor(gx1, gy1)]
        check("四个角**都是底色** = 直角矩形（不是胶囊）",
              all(is_green(c) for c in corners),
              ", ".join(hexs(c) for c in corners))
        # 行高：微信 60 物理px。允许一点误差（字体/边距）
        check("行高接近微信实测的 60 物理px", abs(h_phys - 60) <= 6,
              "%d" % h_phys)

        # ---- 4. 文字：近白 + 左对齐 ----
        tcols = []
        for x in range(gx0, gx1 + 1):
            for y in range(gy0, gy1 + 1):
                if lum(im.pixelColor(x, y)) > 180:
                    tcols.append(x)
                    break
        if tcols:
            left_inset = tcols[0] - gx0
            right_inset = gx1 - tcols[-1]
            check("文字**左对齐**（右边距明显大于左边距）",
                  right_inset > left_inset + 10,
                  "左 %d / 右 %d" % (left_inset, right_inset))
            print("     （微信实测：左 27 / 右 69，本机缩放 1.5）")
        else:
            check("选中项里有文字像素", False, "没找到亮像素")

        # ---- 5. 文字**垂直居中**（用户第 6 条：菜单项文字垂直居中）
        # ⚠️ 判据是"文字像素的上边距 ≈ 下边距"。微信那边实测 19 / 19。
        #    只比"中心差"是不够的 —— 文字块和控件块的中心差在容差内、
        #    但文字在块里明显偏上偏下，肉眼一样是"没对齐"。
        trows = []
        for y in range(gy0, gy1 + 1):
            if any(lum(im.pixelColor(x, y)) > 180
                   for x in range(gx0, gx1 + 1)):
                trows.append(y)
        if trows:
            top = trows[0] - gy0
            bottom = gy1 - trows[-1]
            check("菜单项文字**垂直居中**（上下边距相当）",
                  abs(top - bottom) <= 3,
                  "上 %d / 下 %d（块高 %d，微信实测 19/19）"
                  % (top, bottom, h_phys))
        else:
            check("菜单项里有文字行", False, "没找到")

        nav.deleteLater()
        pump(app, 120)

    print("\n" + ("全部通过" if not FAIL else "失败 %d 项: %s" % (len(FAIL), FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
