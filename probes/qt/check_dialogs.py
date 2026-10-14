# -*- coding: utf-8 -*-
"""三个窗口（悬浮窗 / 看提示词 / 确认框）的外壳**是不是同一套**。

    python -X utf8 probes/qt/check_dialogs.py

⚠️ 要验的（用户提的）：
    ① 「不要 python 的边框」—— 看提示词原来是个裸 `QWidget`，
       吃系统标题栏和边框；确认框原来是 `QMessageBox`（系统外观 + 问号图标）。
    ② 「看提示词与设置里面恢复默认的弹窗，风格也要一致」。

⚠️ 判据全部量**实际属性和像素**，不看代码里写没写：
    · 窗口标志里有没有 `FramelessWindowHint`（没有 = 还是系统边框）
    · 渲染出来的**四个角是不是透明的**（= 圆角真的画出来了）
    · 两个弹窗的**圆角半径和外框线颜色是不是同一个值**（= 真的"一致"，
      而不是"看起来差不多"）
"""

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

from PySide6 import QtCore, QtGui, QtWidgets                    # noqa: E402
from wechat_assistant import dpi, theme                          # noqa: E402

dpi.enable()
from wechat_assistant.qt_ui import base                          # noqa: E402
from wechat_assistant.qt_ui.layout import ShellDialog            # noqa: E402
from wechat_assistant.qt_ui.panel import PromptWindow            # noqa: E402

FAIL = []


def check(name, ok, detail=""):
    print("  %s %-50s %s" % ("[通过]" if ok else "[失败]", name, detail))
    if not ok:
        FAIL.append(name)


def pump(app, ms=300):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.004)


def corpus(w):
    """把窗口渲染到**透明底**上，返回四个角与中心的 alpha。"""
    pm = QtGui.QPixmap(max(1, w.width()), max(1, w.height()))
    pm.fill(QtCore.Qt.transparent)
    pt = QtGui.QPainter(pm)
    w.render(pt, QtCore.QPoint(0, 0), QtGui.QRegion(),
             QtWidgets.QWidget.RenderFlag.DrawChildren)
    pt.end()
    im = pm.toImage()
    W, H = im.width(), im.height()
    corners = [im.pixelColor(0, 0).alpha(), im.pixelColor(W - 1, 0).alpha(),
               im.pixelColor(0, H - 1).alpha(), im.pixelColor(W - 1, H - 1).alpha()]
    mid = im.pixelColor(W // 2, H // 2).alpha()
    return corners, mid, im


def border_color(im):
    """取顶边中点附近那一像素的颜色（外框线）。"""
    c = QtGui.QColor(im.pixelColor(im.width() // 2, 0))
    return "#%02X%02X%02X" % (c.red(), c.green(), c.blue())


def main():
    app = base.init_app([])
    st = base.Style(None, name="dark")
    print("缩放 %.2f  主题 %s\n" % (base.scale(), st.theme_name))

    made = []

    # ---- 1. 看提示词
    print("== 1. 「看提示词」窗口 ==")
    pw = PromptWindow(st, lambda: None)
    pw.resize(st.px(560), st.px(380))
    pw.show()
    pump(app, 450)
    made.append(pw)
    flags_ok = bool(pw.windowFlags() & QtCore.Qt.FramelessWindowHint)
    check("无边框（不再吃系统标题栏）", flags_ok,
          "flags=%s" % hex(int(pw.windowFlags())))
    check("真透明（WA_TranslucentBackground）",
          pw.testAttribute(QtCore.Qt.WA_TranslucentBackground))
    c, mid, im_pw = corpus(pw)
    check("四个角是**圆的**（透明）", all(a == 0 for a in c), str(c))
    check("中间不透明（不是整窗透明）", mid == 255, str(mid))
    check("沿用统一的圆角半径", abs(pw.radius - st.px(theme.SHELL_RADIUS)) < 0.5,
          "%.1f vs %d" % (pw.radius, st.px(theme.SHELL_RADIUS)))

    # ---- 2. 确认框（跟"恢复默认"用的是同一个类）
    print("\n== 2. 确认框（ShellDialog，恢复默认/报错都用它）==")
    # ⚠️ 2026-10-14：**不许再写死 resize** —— 用户报的「恢复默认窗口太窄了」
    #    正是被这里那句 `dlg.resize(px(380), px(200))` 盖住的：
    #    弹窗的真实尺寸是按内容 `sizeHint` 定的，不 resize 才是用户看到的样子。
    #    所以这里跟产品一样传 `min_width`（`settings_ui.DIALOG_MIN_W`），
    #    然后**量它自己长出来的尺寸**。
    from wechat_assistant.qt_ui.settings_ui import DIALOG_MIN_W
    dlg = ShellDialog(st, "恢复默认", None, modal=False,
                      min_width=DIALOG_MIN_W)
    dlg.add_text("把所有设置恢复为默认值？\n（API Key 不会动）")
    dlg.add_button("取消", variant="ghost")
    dlg.add_button("恢复默认", variant="primary")
    dlg.show()
    pump(app, 450)
    made.append(dlg)
    check("无边框", bool(dlg.windowFlags() & QtCore.Qt.FramelessWindowHint))
    check("真透明", dlg.testAttribute(QtCore.Qt.WA_TranslucentBackground))
    check("宽度 >= DIALOG_MIN_W（窄条就是用户报的那个问题）",
          dlg.width() >= st.px(DIALOG_MIN_W),
          "%d vs %d（基准 %d）" % (dlg.width(), st.px(DIALOG_MIN_W), DIALOG_MIN_W))
    _lab = dlg.body.itemAt(0).widget()
    check("那句确认文字**一行放得下**（宽度够，不会把「默认值」折断）",
          _lab.width() >= _lab.sizeHint().width() or _lab.height() <= st.px(60),
          "标签宽 %d / 需要 %d" % (_lab.width(), _lab.sizeHint().width()))
    c2, mid2, im_dlg = corpus(dlg)
    check("四个角是**圆的**（透明）", all(a == 0 for a in c2), str(c2))
    check("中间不透明", mid2 == 255, str(mid2))

    # ---- 3. 两者"一致"：半径 + 外框线色 + 底色
    print("\n== 3. 两个弹窗跟悬浮窗是**同一套**外壳 ==")
    check("圆角半径同一个值",
          abs(pw.radius - dlg.radius) < 0.5, "%.1f vs %.1f" % (pw.radius, dlg.radius))
    check("外框线宽度同一个值",
          abs(pw.border_w - dlg.border_w) < 0.5,
          "%.1f vs %.1f" % (pw.border_w, dlg.border_w))
    bpw, bdlg = border_color(im_pw), border_color(im_dlg)
    check("外框线颜色一致（都取色板的 shell_border）",
          bpw.upper() == bdlg.upper() == st.p["shell_border"].upper(),
          "%s / %s vs %s" % (bpw, bdlg, st.p["shell_border"]))

    # ---- 4. 设置窗口里不该再有 QMessageBox（系统外观）
    print("\n== 4. 设置窗口不再用系统 QMessageBox ==")
    src = open(os.path.join(ROOT, "wechat_assistant", "qt_ui",
                            "settings_ui.py"), encoding="utf-8").read()
    # 注释里提到 QMessageBox 是允许的（说明历史），只查有没有**真的建**它
    real = [ln for ln in src.split("\n")
            if "QMessageBox(" in ln and not ln.strip().startswith("#")]
    check("代码里没有 `QMessageBox(...)` 的实例化", not real, str(real[:3]))

    for w in made:
        try:
            w.close()
        except Exception:
            pass
    pump(app, 150)

    print("\n" + ("全部通过" if not FAIL else "失败 %d 项: %s" % (len(FAIL), FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
