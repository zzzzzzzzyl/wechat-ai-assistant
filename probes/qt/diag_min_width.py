# -*- coding: utf-8 -*-
"""量「面板请求的宽度」和「窗口实际能有多宽」是不是对得上。

用户报的现象（日志每秒刷一行）：
    面板几何被外部改过（(1636, 270, 406, 820) -> (1636, 270, 463, 820)），重新跟随
位置一样、**只有宽度不同**：跟随器要 406，窗口实际 463，而且**永远对不上**。

推断：406 是被 Qt 顶回 463 的 —— 面板布局里有控件的最小宽度大于 406，
`resize(406, h)` 会被 Qt 抬到那个下限。于是
    `_set()` 记 406 → `_verify()` 看到 463 → 清掉记录 → 下一拍又要 406 → 循环。

这个探针就是把两边的数都摊开：
  · 面板的 `minimumSizeHint()` / `minimumWidth()` / 根布局的 minimumSize
  · 每个子控件自己的 `minimumSizeHint()`（找出到底是谁把下限顶上去的）
  · 真的调一次 `resize(窄)`，看 Qt 最后给多宽

用法：python probes/qt/diag_min_width.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))


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


def main():
    print("=" * 74)
    print("面板宽度：请求值 vs Qt 实际给的值（为什么 _verify 永远对不上）")
    print("=" * 74)

    from PySide6 import QtWidgets
    from wechat_assistant import dpi
    from wechat_assistant.qt_ui import base
    from wechat_assistant.qt_ui.panel import FloatingAssistant

    dpi.enable()
    app = base.init_app([])
    px = base.px

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
    ui.resize(px(340), px(560))
    ui.show()
    for _ in range(80):
        app.processEvents()

    print("\n缩放 %.2f（px(340) = %d）" % (base.scale(), px(340)))
    print("\n-- 面板自身的下限 --")
    print("  ui.minimumWidth()          = %d" % ui.minimumWidth())
    print("  ui.minimumSizeHint().width = %d" % ui.minimumSizeHint().width())
    print("  ui.sizeHint().width        = %d" % ui.sizeHint().width())
    print("  根布局 minimumSize().width = %d" % ui._root_lay.minimumSize().width())

    print("\n-- 每个子控件自己的下限（找出是谁把下限顶上去的）--")
    rows = []
    for w in ui.findChildren(QtWidgets.QWidget):
        try:
            mw = w.minimumSizeHint().width()
            mn = w.minimumWidth()
        except Exception:
            continue
        if mw > 1 or mn > 1:
            rows.append((max(mw, mn), w))
    rows.sort(key=lambda t: -t[0])
    for val, w in rows[:14]:
        name = type(w).__name__
        txt = ""
        try:
            if hasattr(w, "text") and callable(w.text):
                txt = repr(w.text())[:24]
        except Exception:
            pass
        chain = []
        p = w.parent()
        while p is not None and len(chain) < 4:
            chain.append(type(p).__name__)
            p = p.parent()
        print("  %4d  %-14s %-24s 父链: %s"
              % (val, name, txt, " < ".join(chain)))

    print("\n-- 真的走 `set_docked`（跟随器那条路），看 Qt 给多少 --")
    print("     （不能直接 `resize()`：`_fit_height()` 会把宽度按 `_size[0]` 拽回去）")
    for want in (320, 380, 406, 440, 463, 500, 520):
        ui.set_docked(100, 100, want, ui.height())
        for _ in range(8):
            app.processEvents()
        flag = "  ← 被顶回来了！" if ui.width() != want else ""
        print("  要 %4d -> 实际 %4d%s" % (want, ui.width(), flag))

    print("\n-- 塞几条长回复之后再看一次（卡片会改变布局下限）--")
    ui.clear_docked()
    for _ in range(4):
        app.processEvents()
    ui.post_replies(["第一条建议，稍微长一点点好撑开宽度。",
                     "第二条建议，也长一点。",
                     "第三条建议，同样长一点。"], "", "")
    for _ in range(40):
        app.processEvents()
    print("  有卡片后 ui.minimumSizeHint().width() = %d"
          % ui.minimumSizeHint().width())
    for want in (406, 430, 463, 500):
        ui.set_docked(100, 100, want, ui.height())
        for _ in range(8):
            app.processEvents()
        flag = "  ← 被顶回来了！" if ui.width() != want else ""
        print("  要 %4d -> 实际 %4d%s" % (want, ui.width(), flag))

    # ---- 关键：长文本会不会把布局下限顶上去 ----
    # 状态栏 / 目标 / 错误 这几条都是**不换行**的长文本，
    # QLabel 不换行时它的最小宽度 = 整行文字宽度。
    print("\n-- 长文本对下限的影响（这才是真正会把窗口顶宽的东西）--")
    cases = [
        ("短状态", "已生成 3 条"),
        ("长状态", "⚠ 不在白名单，仅预览不生成 · 点一下卡片就复制到剪贴板，"
                   "然后自己到微信里粘贴发送出去"),
        ("长目标", "对方在说：这是一段挺长的消息内容，用来把面板的宽度顶起来看看效果"),
        ("长错误", "生成失败：HTTP 401 Unauthorized（请检查 secrets.yaml 里的 api key "
                   "是不是过期了，或者模型名写错了）"),
    ]
    for label, text in cases:
        ui.post_status(text)
        for _ in range(20):
            app.processEvents()
        ms = ui.minimumSizeHint().width()
        ui.set_docked(100, 100, 406, ui.height())
        for _ in range(8):
            app.processEvents()
        got = ui.width()
        print("  %-6s minimumSizeHint=%-4d  要 406 -> 实际 %d%s"
              % (label, ms, got, "  ← 被顶回来了！" if got != 406 else ""))

    print("\n-- 谁的下限把整棵布局顶上去的（长状态下再看一次）--")
    rows = []
    for w in ui.findChildren(QtWidgets.QWidget):
        try:
            rows.append((w.minimumSizeHint().width(), w))
        except Exception:
            continue
    rows.sort(key=lambda t: -t[0])
    for val, w in rows[:6]:
        txt = ""
        try:
            if hasattr(w, "text") and callable(w.text):
                txt = repr(w.text())[:40]
        except Exception:
            pass
        print("  %4d  %-14s %s" % (val, type(w).__name__, txt))

    print("\n结论：`_panel_size()` 请求的宽度如果小于上面那个下限，")
    print("      Qt 只会给你一个更宽的窗口，`_verify()` 于是每秒都发现")
    print("      「几何被外部改过」，重新摆一次，永远对不上 —— 而且每次重摆")
    print("      都要整棵布局重排（那是最贵的那条路）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
