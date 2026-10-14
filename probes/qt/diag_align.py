# -*- coding: utf-8 -*-
"""找**垂直没对齐**的地方（图标-文字 / 标签-控件 / 开关-文字）。

    python -X utf8 probes/qt/diag_align.py

⚠️ 要查的是用户提的第 4 条：
    「所有组件做到：图标-文字、标签-控件、开关-文字**强制垂直居中**对齐」
    以及第 8 条：「右侧『默认、已修改』文字需要和对应控件垂直对齐」。

⚠️ 判据：同一行里的几个部件，**垂直中心**应该差不多齐（差 <= 2px）。
    做法是把每个父容器下的子部件按"垂直区间有重叠"分组成行，
    再比组内的中心 —— 这样不用预先知道布局结构，能扫到没预料到的地方。

⚠️ 只看**可见**且有实际尺寸的部件；容器（有子部件的）也纳入，
    因为"标签 vs 控件"经常就是"标签 vs 一个容器"。
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

TOL = 2          # 中心差多少算"没对齐"


def pump(app, ms=300):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.004)


def label_of(w):
    try:
        if isinstance(w, QtWidgets.QLabel):
            return "QLabel(%r)" % (w.text()[:12],)
        if isinstance(w, QtWidgets.QPushButton):
            return "%s(%r)" % (type(w).__name__, w.text()[:10])
    except Exception:
        pass
    return type(w).__name__


def walk_layouts(lay, out):
    """把一个控件树里的**所有布局**收集起来（含嵌套）。"""
    if lay is None:
        return
    out.append(lay)
    for i in range(lay.count()):
        it = lay.itemAt(i)
        if it is None:
            continue
        sub = it.layout()
        if sub is not None:
            walk_layouts(sub, out)
        w = it.widget()
        if w is not None:
            walk_layouts(w.layout(), out)


def hbox_rows(root, origin):
    """找出所有**水平布局**，返回 [(布局, [(y0,y1,center,widget)])]。

    ⚠️ **判据必须按"布局结构"分组，不能按"垂直区间是否重叠"分组。**
       第一版就是按重叠分的，结果自检时露了馅：
       故意错开 12px 的两个 QLabel，一个 y=24~36、一个 y=11~23，
       **区间根本不重叠** → 被分成了两行、每行只有 1 个成员 →
       于是"错开"永远报不出来。
       **判据错了，扫描就会全程沉默，而且看起来像"一切正常"。**
       这也正是先写自检的价值 —— 不然后面所有"✅ 没找到"都是假的。
    """
    lays = []
    walk_layouts(root.layout(), lays)
    out = []
    for lay in lays:
        if not isinstance(lay, QtWidgets.QHBoxLayout):
            continue
        items = []
        for i in range(lay.count()):
            w = lay.itemAt(i).widget() if lay.itemAt(i) else None
            if w is None:
                continue
            try:
                if not w.isVisible() or w.width() < 2 or w.height() < 2:
                    continue
                tl = w.mapTo(origin, QtCore.QPoint(0, 0))
            except RuntimeError:
                continue
            items.append((tl.y(), tl.y() + w.height(),
                          (tl.y() + tl.y() + w.height()) / 2.0, w))
        if len(items) >= 2:
            out.append((lay, items))
    return out


def scan(root, origin, tag, bad, stats):
    rows = hbox_rows(root, origin)
    stats["rows"] += len(rows)
    for lay, g in rows:
        stats["widgets"] += len(g)
        centers = [t[2] for t in g]
        spread = max(centers) - min(centers)
        if spread <= TOL:
            continue
        members = ", ".join("%s(y=%d,h=%d,ctr=%.0f)"
                            % (label_of(t[3]), t[0], t[1] - t[0], t[2])
                            for t in sorted(g, key=lambda t: t[0]))
        bad.append((tag, spread, members))


def selftest(app, bad):
    """**先证明这个探针会工作**，再去信它的"没找到"。

    ⚠️ 这是项目里的一条硬教训（CLAUDE.md 记着）：
        "诊断工具本身也要先证明自己在工作" —— 之前写过一个看门狗，
        因为一行 `continue` 排错位置而**全程沉默**，白跑两轮还以为没事。

    做法：造一个**故意错开 12px** 的行，看扫描能不能报出来。
    报不出来 = 这个探针的判据是坏的，后面所有的"✅ 没找到"都不算数。
    """
    host = QtWidgets.QWidget()
    lay = QtWidgets.QHBoxLayout(host)
    a = QtWidgets.QLabel("A")
    b = QtWidgets.QLabel("B")
    lay.addWidget(a, 0, QtCore.Qt.AlignVCenter)
    lay.addWidget(b, 0, QtCore.Qt.AlignTop)     # 故意不居中
    host.resize(200, 60)
    host.show()
    pump(app, 200)
    found = []
    stats = {"rows": 0, "widgets": 0}
    scan(host, host, "selftest", found, stats)
    a.deleteLater()
    b.deleteLater()
    host.deleteLater()
    pump(app, 80)
    if found:
        print("== 0. 探针自检 ==")
        print("   ✅ 故意错开的一行被报出来了（判据有效）：中心差 %.0fpx"
              % found[0][1])
        return True
    print("== 0. 探针自检 ==")
    print("   ❌ **故意错开的一行没被报出来 —— 这个探针坏了，"
          "下面所有的「没找到」都不可信**")
    bad.append(("selftest", -1, "探针自检失败"))
    return False


def main():
    app = base.init_app([])
    print("缩放 %.2f   判据：同一行的垂直中心差 <= %dpx\n" % (base.scale(), TOL))

    import test_qt_settings_ui as T
    tmp = tempfile.mkdtemp(prefix="diag_align_")
    bad = []
    stats = {"rows": 0, "widgets": 0}
    if not selftest(app, bad):
        return 1
    print()
    for theme in ("dark", "light"):
        win, _s, _st = T.make_win(tmp, theme_name=theme)
        win.open()
        pump(app, 500)
        for group in ("ui", "ai", "session"):
            try:
                win._switch(group)
            except Exception:
                continue
            pump(app, 300)
            scan(win.win, win.win, "%s/%s" % (theme, group), bad, stats)
        try:
            win.close()
        except Exception:
            pass
        pump(app, 150)

    # ---- 悬浮窗（主窗口）：标题栏按钮组 / 底部按钮行 / 状态行
    from wechat_assistant.qt_ui.panel import BASE_WIDTH, FloatingAssistant

    class _Cfg:
        def __init__(self, d=None):
            self._data = d or {}

        def get(self, path, default=None):
            node = self._data
            for part in str(path).split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return node

    for theme in ("dark", "light"):
        st = base.Style(None, name=theme)
        ui = FloatingAssistant(
            _Cfg({"ui": {"width": 340, "radius": 10, "theme": theme,
                         "frameless": True, "min_width": 200,
                         "width_max": 640}}),
            logger=lambda m: None, on_regenerate=lambda i: None,
            on_read=lambda: None, on_clear=lambda: None,
            on_settings=lambda: None, session_source=lambda: {"会话": {}},
            on_mode_change=lambda a, b: None, on_config_changed=lambda c: None)
        ui.resize(st.px(BASE_WIDTH), st.px(560))
        ui.show()
        pump(app, 500)
        ui.post_replies(["第一条建议", "第二条建议", "第三条建议"])
        pump(app, 500)
        scan(ui, ui, "panel/%s" % theme, bad, stats)
        ui.close()
        pump(app, 150)

    # ⚠️ **必须报"扫了多少"**：探针自检只证明判据能用，
    #    不证明它**真的扫到了界面里的行**。扫到 0 行时的"✅ 没找到"
    #    是另一种形式的沉默，同样会骗人。
    print("== 扫了多少 ==")
    print("   水平行 %d 个，涉及部件 %d 个" % (stats["rows"], stats["widgets"]))
    if stats["rows"] < 10:
        print("   ❌ **扫到的行太少，这个结果不可信**（界面明明有很多水平排布）")
        return 1
    print()
    print("== 垂直没对齐的地方 ==")
    if not bad:
        print("   ✅ 没找到（同一行内中心差都 <= %dpx）" % TOL)
    else:
        print("   共 %d 处：\n" % len(bad))
        for tag, spread, members in bad:
            print("   [%s] 中心差 %.0fpx" % (tag, spread))
            print("        %s" % members)
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
