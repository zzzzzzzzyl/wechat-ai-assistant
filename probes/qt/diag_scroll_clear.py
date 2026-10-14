# -*- coding: utf-8 -*-
"""`ScrollArea.clear()` 有没有**真的**把内容清掉（量"还挂在屏幕上的子部件"）。

    python -X utf8 probes/qt/diag_scroll_clear.py

⚠️ 要查的（用户报的）：
    「换会话后顶部能识别，但气泡没变」——
    加了日志之后确认：切换**检测到了**，新会话缓存 0 条，
    `_render_replies([], ...)` **也确实执行了**，但界面上的卡片没消失。

⚠️ 怀疑的根因：
    `ScrollArea.clear()` 只处理 `item.widget()`：
        while self._lay.count() > 1:
            item = self._lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None); w.deleteLater()
    而**回复卡片是用 `add_layout(lay)` 加的**（外面包了一层 QVBoxLayout），
    这种条目 `item.widget()` 是 **None** —— 于是那一支什么都不做：
    卡片**没被删除、也没被隐藏**，只是从布局里摘掉了，
    仍然挂在 `inner` 上**按原来的几何继续画**。
    → 症状就是"清空跑过了，但屏幕上的气泡纹丝不动"。

⚠️ 判据必须是**"inner 上还挂着几个可见的 Card"**，不能是 `content_height()`：
    孤儿部件已经不在布局里，`content_height()` 自然也变小了 ——
    拿它当判据会**测出一个假绿**（我上一版的测试就是这么过的）。
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
from wechat_assistant.qt_ui.cards import Card                    # noqa: E402
from wechat_assistant.qt_ui.layout import ScrollArea             # noqa: E402


def pump(app, ms=300):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.004)


def live_cards(sc):
    """inner 上还挂着、而且**可见**的 Card 有几个。

    ⚠️ 用 `findChildren` 而不是遍历布局 —— 孤儿部件的定义就是
       "已经从布局里摘掉了，但还在父部件上"。只看布局是看不见它们的。
    """
    out = []
    for c in sc.inner.findChildren(Card):
        try:
            if c.isVisible() and c.width() > 4 and c.height() > 4:
                out.append(c)
        except RuntimeError:
            continue
    return out


def add_three_cards(st, sc):
    """按**和 `_render_replies` 一样的方式**加三张卡片（走 add_layout）。"""
    for i, text in enumerate(("第一条建议", "第二条建议", "第三条建议"), 1):
        card = Card(st, text, index=i, width_px=300, parent=sc.inner)
        lay = QtWidgets.QVBoxLayout()
        lay.setContentsMargins(0, 0, 0, st.px(6))
        lay.addWidget(card)
        sc.add_layout(lay)


def main():
    app = base.init_app([])
    st = base.Style(None, name="dark")

    host = QtWidgets.QWidget()
    hlay = QtWidgets.QVBoxLayout(host)
    hlay.setContentsMargins(0, 0, 0, 0)
    sc = ScrollArea(st, "bg", host)
    hlay.addWidget(sc)
    host.resize(st.px(340), st.px(300))
    host.show()
    pump(app, 400)

    print("== 1. 放三张卡片（走 add_layout，跟回复卡片一样的加法）==")
    add_three_cards(st, sc)
    pump(app, 400)
    n0 = len(live_cards(sc))
    print("   可见 Card 数 = %d   content_height = %s"
          % (n0, sc.content_height()))
    ok1 = n0 == 3

    print("\n== 2. 调 clear() 之后 ==")
    sc.clear()
    pump(app, 400)
    n1 = len(live_cards(sc))
    ch1 = sc.content_height()
    print("   可见 Card 数 = %d   content_height = %s" % (n1, ch1))
    ok2 = n1 == 0

    print("\n== 3. 再加一张（模拟清空后显示「还没有建议」）==")
    lab = QtWidgets.QLabel("还没有建议 · 点下面的【生成建议】")
    sc.add(lab)
    pump(app, 300)
    n2 = len(live_cards(sc))
    print("   可见 Card 数 = %d（应该是 0）" % n2)
    print("   `add()` 加的 QLabel 在不在布局里 = %s"
          % (sc._lay.count() > 1))
    ok3 = n2 == 0

    print("\n== 结论 ==")
    if ok1 and ok2 and ok3:
        print("   ✅ clear() 真的把卡片清掉了")
    else:
        print("   ❌ clear() **没清干净** —— 卡片还挂在 inner 上：")
        print("      放三张=%d 张（期望 3）  清空后=%d 张（期望 0）"
              % (n0, n1))
        if n1:
            print("      ⚠️ 注意 `content_height()` 此时已经是 %s —— "
                  "**拿它当判据会测出一个假绿**" % ch1)
            for c in live_cards(sc)[:3]:
                print("         孤儿卡片: pos=(%d,%d) %dx%d"
                      % (c.x(), c.y(), c.width(), c.height()))

    host.deleteLater()
    return 0


if __name__ == "__main__":
    sys.exit(main())
