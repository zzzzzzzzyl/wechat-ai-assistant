# -*- coding: utf-8 -*-
"""卡片（候选回复气泡）里有没有**多出来的灰框**。

    python -X utf8 probes/qt/diag_card_boxes.py

⚠️ 要查的（用户报的）：
    「建议生成后三个气泡文字有灰色框，复制也有」。

⚠️ 怀疑的根因（Qt 样式表的一个经典坑，**得量出来才算数**）：
    `cards.py` 的样式表写的是
        QFrame { background:...; border:1px solid <card_line>; border-radius:Npx; }
        QLabel { color:...; background:transparent; }
    **`QLabel` 是 `QFrame` 的子类**，而 Qt 里不带点的类型选择器
    `QFrame` 会**连子类一起选中**。于是每个 `QLabel` 也被套上了
    那圈 1px 描边；第二条只把 `background` 改回透明，**border 没清**。
    → 卡片的每个标签外面都多一圈灰框，正是"文字有灰色框"。

⚠️ 量法：把卡片渲染到**洋红底**上，然后
    ① 数一数 `card_line` 色的像素分布 —— 正常的卡片只应该**外圈一圈**；
      如果**内部还有成环的 `card_line` 像素**，那就是标签的描边。
    ② 直接看标签自己的 `styleSheet()` 解析出来的 border 值（字符串层，辅助）。
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
from wechat_assistant.qt_ui.cards import Card                   # noqa: E402


def pump(app, ms=300):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.004)


def main():
    app = base.init_app([])
    st = base.Style(None, name="dark")
    p = st.p
    line = p.get("card_line", p["surface"])
    print("卡片的描边色 card_line = %s" % line)
    print("（下面要数的是：这个颜色出现在**卡片内部**的次数 —— 正常应该是 0）\n")

    host = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    card = Card(st, "这是一条用来观察描边的建议内容", index=1, width_px=300)
    lay.addWidget(card)
    host.resize(st.px(320), st.px(90))
    host.show()
    pump(app, 400)

    pm = QtGui.QPixmap(max(1, host.width()), max(1, host.height()))
    pm.fill(QtGui.QColor("#FF00FF"))
    pt = QtGui.QPainter(pm)
    host.render(pt, QtCore.QPoint(0, 0), QtGui.QRegion(),
                QtWidgets.QWidget.RenderFlag.DrawChildren)
    pt.end()
    im = pm.toImage()

    def hexs(c):
        return "#%02X%02X%02X" % (c.red(), c.green(), c.blue())

    # 把 card_line 色的像素按"离卡片边界的距离"分类：
    # 贴着外缘 = 卡片自己的描边（正常）；跑到内部 = 标签的描边（bug）
    hits = []
    for y in range(im.height()):
        for x in range(im.width()):
            if hexs(im.pixelColor(x, y)).upper() == line.upper():
                hits.append((x, y))
    print("== 1. card_line 色像素一共 %d 个 ==" % len(hits))
    if hits:
        xs = [h[0] for h in hits]
        ys = [h[1] for h in hits]
        print("   分布范围 x %d~%d  y %d~%d   卡片区域约 %dx%d"
              % (min(xs), max(xs), min(ys), max(ys), im.width(), im.height()))

    # 逐行看：每一行里 card_line 像素的个数。
    # 卡片自己的上下描边 = 某一整行几乎全是；左右描边 = 每行 1~2 个。
    # 标签的描边 = **中间若干行也出现**（既不是第一行也不是最后一行）。
    rows = {}
    for x, y in hits:
        rows.setdefault(y, 0)
        rows[y] += 1
    inner_rows = [y for y, n in rows.items()
                  if 3 < y < im.height() - 4 and 20 < n < im.width() - 20]
    print("\n== 2. 内部有没有成行的描边（= 标签的框）==")
    if inner_rows:
        print("   ⚠️ 有 %d 行在**卡片内部**出现了近似整行的描边色："
              % len(inner_rows))
        for y in sorted(inner_rows)[:8]:
            print("      y=%d  该行有 %d 个描边色像素" % (y, rows[y]))
        print("   -> **这就是用户说的「文字有灰色框」**")
    else:
        print("   ✅ 内部没有成行的描边色 —— 只有卡片自己那一圈")

    # ---- 3. 标签自己的样式表（字符串层，辅助证据）
    print("\n== 3. 各标签自己的样式表 ==")
    for name in ("lbl_num", "lbl_text", "lbl_act"):
        w = getattr(card, name, None)
        if w is None:
            continue
        ss = w.styleSheet() or "(空 —— 吃父级的 QLabel 规则)"
        print("   %-9s %s" % (name, ss))
    print("\n   卡片自己的样式表：")
    print("   " + (card.styleSheet() or "").replace("\n", "\n   ").strip())

    # ---- 4. 「看提示词」窗口：`QWidget {}` 这条规则会不会也把按钮刷成底色
    #
    # ⚠️ `PromptWindow` 的样式表第一条是 `QWidget { background:...; }`，
    #    而 `QWidget` 会**连所有后代一起选中**。它里面有两个 `Pill` ——
    #    按钮自己有自己的样式表（优先级更高），所以**理论上**没事，
    #    但这类"靠优先级压住"的东西必须量一次才算数。
    print("\n== 4. 「看提示词」窗口里的按钮有没有被刷成窗口底色 ==")
    from wechat_assistant.qt_ui.panel import PromptWindow
    pw = PromptWindow(st, lambda: None)
    pw.resize(st.px(420), st.px(300))
    pw.show()
    pump(app, 400)
    pm2 = QtGui.QPixmap(max(1, pw.width()), max(1, pw.height()))
    pm2.fill(QtGui.QColor("#FF00FF"))
    pt2 = QtGui.QPainter(pm2)
    pw.render(pt2, QtCore.QPoint(0, 0), QtGui.QRegion(),
              QtWidgets.QWidget.RenderFlag.DrawChildren)
    pt2.end()
    im2 = pm2.toImage()
    winbg = p["bg"].upper()
    bad_buttons = []
    for name, btn in ((b.text(), b) for b in pw.findChildren(QtWidgets.QPushButton)):
        if not btn.isVisible() or btn.width() < 4:
            continue
        # 取按钮**内侧**一点（避开圆角），看它画的是不是窗口底色
        c = im2.pixelColor(btn.x() + btn.width() // 2,
                           btn.y() + btn.height() // 2)
        got = hexs(c).upper()
        want = (btn.colors()[0] or "").upper() if hasattr(btn, "colors") else ""
        ok = got != winbg
        print("   按钮「%s」中心色 = %s（自己声明的底 %s，窗口底 %s）%s"
              % (name, got, want or "?", winbg, "" if ok else "  <-- 被刷成窗口底了"))
        if not ok:
            bad_buttons.append(name)
    if not bad_buttons:
        print("   ✅ 两个按钮都画出了自己的底色，没被 `QWidget{}` 刷掉")
    pw.deleteLater()

    host.deleteLater()
    return 0


if __name__ == "__main__":
    sys.exit(main())
