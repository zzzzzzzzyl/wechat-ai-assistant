# -*- coding: utf-8 -*-
"""设置窗口的**几何 + 光标**取证：滚动条到底压没压内容、鼠标放上去是什么光标。

    python -X utf8 probes/qt/diag_settings_scroll.py

⚠️ 要查的两件事（用户报的）：
    ① 「右边还是有部分被拉动条遮挡住」
       `layout.py` 的注释写着"Qt 的滚动条是独立部件、不压内容"。
       那句话**只是断言**，没人量过 —— 这个探针把它量出来：
       滚动条占的 x 区间 vs 视口（viewport）的 x 区间，看有没有重叠。
    ② 「鼠标放在拉动条上时左右拉伸的样式」
       设置窗口是无边框的，右边缘那一圈是**自己实现的拉伸热区**
       （`settings_ui.RESIZE_ZONE`），`mouseMoveEvent` 会把窗口光标设成
       `SizeHorCursor`。⚠️ Qt 的光标是**沿父链继承**的：滚动条自己没设光标，
       就会继承窗口那个"左右拉伸"光标 —— 于是鼠标一放到滚动条上就变形。

⚠️ 不猜，全部打印实际数值。
"""

import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from PySide6 import QtCore, QtWidgets                            # noqa: E402
from wechat_assistant import dpi                                 # noqa: E402

dpi.enable()
from wechat_assistant.qt_ui import base                          # noqa: E402
from wechat_assistant.qt_ui import settings_ui                   # noqa: E402

CURSORS = {getattr(QtCore.Qt, n): n for n in dir(QtCore.Qt)
           if n.endswith("Cursor") and isinstance(getattr(QtCore.Qt, n),
                                                 QtCore.Qt.CursorShape)}


def name_of(shape):
    return CURSORS.get(shape, str(shape))


def pump(app, ms=300):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.004)


def _store(tmp):
    """复用测试里的 `make_store`（它把三层配置都指到临时目录）。

    ⚠️ 自己拼 `SettingsStore(...)` 很容易漏一个路径参数，那样就**会写到真的
       `state/` 里**去 —— 测试那份是专门为"绝不碰仓库"写过的，直接复用。
    """
    import test_qt_settings_ui as T
    return T.make_store(tmp)


def audit_scroll_areas(w, tag):
    """遍历窗口里**每一个** ScrollArea，量它有没有压内容。

    ⚠️ 为什么要遍历全部：设置窗口里不止一个滚动区 ——
       SPEC 表单一个，**「会话」分组里还有自己的**（会话列表 + 右侧详情）。
       只量第一个（`win.scroll`）会漏掉真正出问题的那一个。
       **"容器宽度对得上"不等于"里面每个子部件都放得下"**，
       所以判据取的是**子部件的最右边界**，不是容器宽度。
    """
    from wechat_assistant.qt_ui.layout import ScrollArea
    areas = w.findChildren(ScrollArea)
    print("\n== [%s] 共 %d 个滚动区 ==" % (tag, len(areas)))
    bad = []
    for i, sc in enumerate(areas):
        if not sc.isVisible():
            print("  #%d 不可见，跳过" % i)
            continue
        vp = sc.viewport()
        sb = sc.verticalScrollBar()
        inner = sc.widget()
        vp_r = vp.mapTo(w, QtCore.QPoint(vp.width(), 0)).x()
        sb_l = sb.mapTo(w, QtCore.QPoint(0, 0)).x()
        print("  #%d  滚动区 %dx%d @(%d,%d)   滚动条 %s w=%d"
              % (i, sc.width(), sc.height(),
                 sc.mapTo(w, QtCore.QPoint(0, 0)).x(),
                 sc.mapTo(w, QtCore.QPoint(0, 0)).y(),
                 "可见" if sb.isVisible() else "隐藏", sb.width()))
        print("      视口右边界=%d" % vp_r)
        if sb.isVisible():
            print("      滚动条左边界=%d  ->  %s"
                  % (sb_l,
                     "相接（内容贴着滚动条）" if vp_r == sb_l else
                     ("重叠 %d px" % (vp_r - sb_l) if vp_r > sb_l else
                      "有 %d px 间隙" % (sb_l - vp_r))))
        else:
            # ⚠️ 滚动条**隐藏**时它的几何是没意义的（Qt 还没给它排位置，
            #    宽度还是默认的 100），拿它算"重叠"会得出荒谬的数字。
            print("      滚动条隐藏，几何无意义，跳过重叠判断")
        if inner is None:
            continue
        # ⚠️ **要量的是"看得见的东西"，不是容器。**
        #    行的**容器**铺满整宽（它没背景、看不见），真正画文字/控件的是它的
        #    子部件。拿容器当"内容最右边界"会永远得到"顶到边"。
        #    这里只取**叶子部件**（自己不再有子部件的那些：QLabel / 输入框 /
        #    按钮…），它们才代表真实的可见内容边界。
        content, sep, cont = [], [], []
        for ch in inner.findChildren(QtWidgets.QWidget):
            if not ch.isVisible() or ch.width() <= 1:
                continue
            try:
                r = ch.mapTo(w, QtCore.QPoint(ch.width(), 0)).x()
            except RuntimeError:
                continue
            if ch.height() <= 4:
                sep.append((type(ch).__name__, r))
            elif ch.findChildren(QtWidgets.QWidget):
                cont.append((type(ch).__name__, r))
            else:
                content.append((type(ch).__name__, r))
        if sep:
            print("      分隔条（故意铺满整宽）: 最右 %d"
                  % max(s[1] for s in sep))
        if cont:
            print("      容器（无背景，不可见）: 最右 %d"
                  % max(c[1] for c in cont))
        if content:
            worst = max(content, key=lambda t: t[1])
            mark = ""
            if sb.isVisible() and worst[1] > sb_l:
                mark = "  <-- **伸进滚动条区域 %d px**" % (worst[1] - sb_l)
                bad.append(i)
            elif worst[1] > vp_r:
                mark = "  <-- **超出视口 %d px**" % (worst[1] - vp_r)
                bad.append(i)
            print("      ★可见内容最右边界=%d （%s）%s"
                  % (worst[1], worst[0], mark))
            if sb.isVisible():
                gap = sb_l - worst[1]
                print("      -> 内容离滚动条还有 %d px %s"
                      % (gap, "（贴着，会被当成'被遮挡'）" if gap <= 0 else "（有缝，不会被误看成被压）"))
    return bad


def main():
    app = base.init_app([])
    print("缩放 %.2f\n" % base.scale())

    tmp = tempfile.mkdtemp(prefix="diag_settings_")
    store = _store(tmp)
    cfg = store.load()
    st = base.Style(cfg, 0, name="dark")
    win = settings_ui.SettingsWindow(
        cfg, store, st, logger=lambda m: None,
        session_source=lambda: {"会话": {}},
        on_mode_change=lambda k, m: None,
        preselect=None)
    win.show_dialogs = False
    # ⚠️ **必须先 `open()`**：真正的 QWidget（`win.win`）是在 `open()` 里
    #    才建出来的，`SettingsWindow` 自己只是 `QObject`（纯逻辑对象）。
    win.open()
    w = win.win
    w.resize(st.px(settings_ui.WIN_W), st.px(settings_ui.WIN_H))
    pump(app, 600)

    print("== 1. 窗口与右栏几何 ==")
    print("   窗口 %dx%d" % (w.width(), w.height()))

    sc = win.scroll
    right = sc.parentWidget()                  # 滚动区所在的那个"右栏"容器
    print("   右栏(滚动区父) x=%d w=%d  右边界=%d"
          % (right.x(), right.width(), right.x() + right.width()))

    print("   滚动区   x=%d w=%d  右边界=%d"
          % (sc.x(), sc.width(), sc.x() + sc.width()))
    vp = sc.viewport()
    print("   视口     x=%d w=%d  右边界=%d"
          % (vp.x(), vp.width(), vp.x() + vp.width()))

    sb = sc.verticalScrollBar()
    print("   滚动条   可见=%s x=%d w=%d  右边界=%d"
          % (sb.isVisible(), sb.x(), sb.width(), sb.x() + sb.width()))
    # ⚠️ 滚动条是视口的**兄弟**，坐标都以 sc 为参照
    if sb.isVisible():
        vp_r = vp.x() + vp.width()
        sb_l = sb.x()
        print("   -> 视口右边界 %d vs 滚动条左边界 %d : %s"
              % (vp_r, sb_l,
                 "**重叠了 %d px**" % (vp_r - sb_l) if sb_l < vp_r
                 else "不重叠（滚动条在视口右边）"))

    # 内容区（SPEC 表单）实际可用宽度
    inner = sc.widget()
    if inner is not None:
        print("   滚动内容 x=%d w=%d  右边界=%d"
              % (inner.x(), inner.width(), inner.x() + inner.width()))
        print("   -> 内容可用宽度 %d，视口宽 %d，差 %d"
              % (inner.width(), vp.width(), vp.width() - inner.width()))

        # ⚠️ 真正的判据不是"容器宽度"，而是**里面每个子部件的最右边界**。
        #    容器宽度对得上、但某个子部件被写死得比它宽，照样会被滚动条压住 ——
        #    这正是"右边被吃一部分"这类问题的形态。
        #    全部换算到**窗口坐标**再比，跟滚动条的窗口坐标比才有意义。
        print("\n   -- 滚动内容里的子部件（换算到窗口坐标）--")
        vp_tr = vp.mapTo(w, QtCore.QPoint(vp.width(), 0)).x()
        sb_tl = sb.mapTo(w, QtCore.QPoint(0, 0)).x()
        worst = None
        for ch in inner.findChildren(QtWidgets.QWidget):
            if not ch.isVisible() or ch.width() <= 1:
                continue
            r = ch.mapTo(w, QtCore.QPoint(ch.width(), 0)).x()
            if worst is None or r > worst[1]:
                worst = (type(ch).__name__, r, ch.text() if hasattr(ch, "text")
                         and callable(getattr(ch, "text", None)) else "")
        if worst:
            print("   视口右边界(窗口坐标) = %d" % vp_tr)
            print("   滚动条左边界(窗口坐标) = %d" % sb_tl)
            print("   子部件最右边界 = %d  （%s 「%s」）"
                  % (worst[1], worst[0], str(worst[2])[:20]))
            if worst[1] > sb_tl:
                print("   -> **有部件伸进了滚动条的区域**（超出 %d px）"
                      % (worst[1] - sb_tl))
            elif worst[1] > vp_tr:
                print("   -> 部件在视口外（%d px）" % (worst[1] - vp_tr))
            else:
                print("   -> 内容都在视口内，没有被滚动条压住")

    bad = audit_scroll_areas(w, "外观分组")
    # ⚠️ 「会话」分组是**自定义容器**，有自己的滚动区（会话列表 + 详情），
    #    必须单独切过去量 —— 出问题的往往就是它。
    win._switch("session")
    pump(app, 400)
    bad += audit_scroll_areas(w, "会话分组")

    print("\n== 2. 鼠标放到滚动条上是什么光标 ==")
    print("   窗口当前光标      = %s" % name_of(w.cursor().shape()))
    print("   滚动条自己的光标   = %s" % name_of(sb.cursor().shape()))
    # 模拟"鼠标移到窗口右边缘"：设置窗口把光标设成左右拉伸
    w.setCursor(QtCore.Qt.SizeHorCursor)
    pump(app, 100)
    print("   ⚠️ 把窗口光标设成 SizeHorCursor 之后：")
    print("      滚动条继承到的光标 = %s" % name_of(sb.cursor().shape()))
    if sb.cursor().shape() == QtCore.Qt.SizeHorCursor:
        print("      -> **这就是问题**：滚动条没设自己的光标，"
              "继承了窗口的左右拉伸光标")
    w.setCursor(QtCore.Qt.ArrowCursor)

    print("\n== 3. 底部按钮条 / 功能按钮的位置 ==")
    print("   （下面看各个按钮的 y 坐标 —— 靠上还是靠下）")
    labels = []
    for child in w.findChildren(QtWidgets.QWidget):
        if isinstance(child, QtWidgets.QPushButton) and child.text():
            labels.append((child.text(), child.mapTo(w, QtCore.QPoint(0, 0)),
                           child.width(), child.height()))
    for text, pt, bw, bh in labels:
        where = "上部" if pt.y() < w.height() // 2 else "下部"
        print("   「%s」 y=%4d (%s)  x=%4d  %dx%d"
              % (text, pt.y(), where, pt.x(), bw, bh))

    win.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
