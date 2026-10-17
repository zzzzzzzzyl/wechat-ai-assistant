# -*- coding: utf-8 -*-
"""把「知识库」这一组渲染成 PNG —— **看图**，不靠想象。

     python probes\\qt\\shot_kb_group.py

为什么需要它：这个项目里「样式表里写了」和「Qt 真的照它画」是两件事
（`CLAUDE.md` 里那条），新加的一组界面必须**真的看一眼**再交。
off-screen 也能截图（`QWidget.grab()` 保留 alpha），没显示器也能跑。

产物：`out/kb_group_dark.png` / `out/kb_group_light.png` / `out/kb_session.png`
（`out/` 被 `.gitignore` 排除，不会进仓库）
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from wechat_assistant import dpi                                  # noqa: E402
dpi.enable()                     # ⚠️ 必须 import Qt 之前（和 run_watch 同序）

from PySide6 import QtWidgets                                     # noqa: E402

from wechat_assistant.kb import KnowledgeBase                     # noqa: E402
from wechat_assistant.qt_ui import base                           # noqa: E402
from wechat_assistant.qt_ui.settings_ui import SettingsWindow     # noqa: E402
from wechat_assistant.settings import SettingsStore               # noqa: E402

OUT = os.path.join(BASE, "out")

FAKE_SESSIONS = {
    "客户A": {"display_name": "客户A", "mode": "on", "kb_enabled": True},
    "老同学": {"display_name": "老同学", "mode": "on", "kb_enabled": False},
}

DOC = """# 价目与条款

## 定金与尾款
定金30%，发货前付清尾款。买家自己取消订单，定金不退；我方缺货取消则全额退。
常见问法：钱怎么付、定金能退吗

## 型号参数
A2 定制款 耐温 -20℃~80℃，58元/份，有现货。
B1 标准款 耐温 -40℃~120℃，42元/份，预定5-7天。

## 退换货
确认收货后48小时内可无理由退；7天内质量问题包退换。
"""

DOC2 = """# 参数对照

## 量化档位
INT4 每参数 0.5 字节，7B 权重约 3.5GB。

| 档位 | 每参数 | 7B 占用 |
|---|---|---|
| INT8 | 1.0 字节 | 7 GB |
| INT4 | 0.5 字节 | 3.5 GB |
"""

DOC3 = """# 售后

## 退换货时效
确认收货后 48 小时内可无理由退；7 天内质量问题包退换。

## 客服时间
每天 9 点到 21 点在线。
"""


def _pump(app, ms=200):
    import time
    t0 = time.time()
    while (time.time() - t0) * 1000 < ms:
        app.processEvents()
        time.sleep(0.01)


def shot(name, theme):
    tmp = tempfile.mkdtemp(prefix="shot_kb_")
    store = SettingsStore(os.path.join(tmp, "settings.json"), logger=lambda m: None)
    store.set("kb.enabled", True)
    cfg = store.load()
    style = base.Style(cfg, 0, name=theme)
    kb = KnowledgeBase(os.path.join(tmp, "kb.db"), logger=lambda m: None)
    # 造两个文件夹：**一个文件夹 = 一个组**（组名 = 文件夹名，和「添加文件夹…」一致）
    for grp, docs in (("AI", [("价目表", DOC), ("参数", DOC2)]),
                      ("工程", [("售后", DOC3)])):
        d = os.path.join(tmp, grp)
        os.makedirs(d, exist_ok=True)
        for fn, body in docs:
            with open(os.path.join(d, fn + ".md"), "w", encoding="utf-8") as f:
                f.write(body)
        kb.import_tree(d, grp=grp)
    # 一份进默认组（对应顶部的「添加文件…」）
    loose = os.path.join(tmp, "零散笔记.txt")
    with open(loose, "w", encoding="utf-8") as f:
        f.write(DOC)
    kb.add_document(loose)
    kb.create_group("杂谈")          # 一个**空组**：验证"先建组"也能显示
    win = SettingsWindow(cfg, store, style, logger=print,
                         session_source=lambda: FAKE_SESSIONS,
                         kb=kb)
    win.show_dialogs = False
    win.open("kb")
    _pump(QtWidgets.QApplication.instance(), 400)
    win.win.resize(style.px(760), style.px(780))
    _pump(QtWidgets.QApplication.instance(), 300)
    path = os.path.join(OUT, name)
    win.win.grab().save(path)
    print("已保存 %s（%dx%d）" % (path, win.win.width(), win.win.height()))
    win.close()
    return path


def shot_session(theme="dark", kb_on=True, name="kb_session.png"):
    tmp = tempfile.mkdtemp(prefix="shot_kb2_")
    store = SettingsStore(os.path.join(tmp, "settings.json"), logger=lambda m: None)
    if kb_on:
        store.set("kb.enabled", True)
    cfg = store.load()
    style = base.Style(cfg, 0, name=theme)
    kb = KnowledgeBase(os.path.join(tmp, "kb.db"), logger=lambda m: None)
    win = SettingsWindow(cfg, store, style, logger=print,
                         session_source=lambda: FAKE_SESSIONS, kb=kb)
    win.show_dialogs = False
    win.open("session", preselect="客户A")
    _pump(QtWidgets.QApplication.instance(), 400)
    win.win.resize(style.px(760), style.px(620))
    _pump(QtWidgets.QApplication.instance(), 300)
    path = os.path.join(OUT, name)
    win.win.grab().save(path)
    print("已保存 %s" % path)
    win.close()
    return path


def main():
    os.makedirs(OUT, exist_ok=True)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    shot("kb_group_dark.png", "dark")
    shot("kb_group_light.png", "light")
    # 会话面板两种状态：全局关（只给一行提示）/ 全局开（显示开关）
    shot_session(kb_on=True, name="kb_session_on.png")
    shot_session(kb_on=False, name="kb_session_off.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
