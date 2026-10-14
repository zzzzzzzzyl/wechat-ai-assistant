# -*- coding: utf-8 -*-
"""悬浮窗到底有没有「任务栏按钮」—— 别猜，量窗口的扩展样式。

Windows 判据（`GetWindowLong(hwnd, GWL_EXSTYLE)`）：
  · `WS_EX_TOOLWINDOW`(0x80)  置位 -> **不出现在任务栏**（也不在 Alt-Tab 里）
  · `WS_EX_APPWINDOW`(0x40000) 置位 -> **强制出现**在任务栏

Qt 会按窗口类型自动选：`Qt::Tool` / `Qt::Popup` / 有 owner 的窗口 → TOOLWINDOW。
`FramelessWindowHint` **本身不影响**任务栏 —— 这一条要量过才算（以前的结论
来自 Tk 的 `overrideredirect`，那是另一回事）。

⚠️ 本探针**不 show()**，只把原生窗口建出来读样式，不会在屏幕上闪东西。

用法：python probes/qt/check_taskbar.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
GW_OWNER = 4

#: Windows 给不给一个顶层窗口建任务栏按钮，判据是这三条**同时**成立：
#:   ① 可见；② 没有 WS_EX_TOOLWINDOW；③ **没有 owner**（或者显式带 WS_EX_APPWINDOW）。
#: ⚠️ ③ 最容易漏 —— Qt 可能给窗口挂一个隐藏的 owner，
#:    那样扩展样式看起来完全正常，任务栏上却什么都没有。


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


def report(tag, w, user32):
    hwnd = int(w.winId())
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    owner = user32.GetWindow(hwnd, GW_OWNER)
    flags = w.windowFlags()
    from PySide6 import QtCore
    # ⚠️ 窗口类型在**低字节**里，是**取值**不是位：
    #    `flags & Qt.Tool` 恒为非零（`Tool`=0xb 和 `Window`=0x1 共享 bit0），
    #    必须整体比低字节。
    kind = int(flags & QtCore.Qt.WindowType_Mask)
    named = {0x1: "Window", 0x2: "Dialog", 0x8: "Popup", 0xb: "Tool",
             0xc: "ToolTip", 0xd: "SplashScreen",
             0x12: "SubWindow"}.get(kind, "0x%X" % kind)
    for name in ("FramelessWindowHint", "WindowStaysOnTopHint",
                 "WindowMinimizeButtonHint", "WindowCloseButtonHint"):
        f = getattr(QtCore.Qt, name, None)
        if f is not None and (flags & f) == f:
            named += "+" + name
    tool = bool(ex & WS_EX_TOOLWINDOW)
    appw = bool(ex & WS_EX_APPWINDOW)
    # 三条判据
    ok = w.isVisible() and (not tool) and (not owner or appw)
    print("  %-14s hwnd=%-8d ex=0x%08X TOOLWINDOW=%-5s APPWINDOW=%-5s owner=%-8d"
          % (tag, hwnd, ex, tool, appw, owner))
    print("  %-14s 可见=%-5s -> **任务栏按钮 %s**   flags=%s"
          % ("", w.isVisible(), "应该有" if ok else "应该没有", named))
    return ex


def main():
    print("=" * 90)
    print("任务栏按钮：悬浮窗 / 设置窗口 / 弹窗 分别在不在任务栏里")
    print("=" * 90)

    import ctypes

    from wechat_assistant import dpi
    from wechat_assistant.qt_ui import base
    from wechat_assistant.qt_ui.panel import FloatingAssistant, PromptWindow

    dpi.enable()
    app = base.init_app([])
    user32 = ctypes.windll.user32

    cfg = Cfg({"ui": {"width": 340, "radius": 10, "theme": "dark",
                      "frameless": True, "minwidth": 200, "width_max": 640}})
    ui = FloatingAssistant(cfg, logger=lambda m: None,
                           on_regenerate=lambda i: None,
                           on_read=lambda: None,
                           on_clear=lambda: None,
                           on_settings=lambda: None,
                           session_source=lambda: {"会话": {}},
                           on_mode_change=lambda a, b: None,
                           on_config_changed=lambda c: None)
    print("\n-- 悬浮窗（**先只在隐藏态量一次**，再真的显示一次）--")
    print("  [隐藏态]")
    report("FloatingAssist", ui, user32)
    print("      窗口图标: %s" % (ui.windowIcon().isNull() and "空(没设)"
                                  or "已设 %d 档" % len(ui.windowIcon().availableSizes())))
    print("\n  [真的显示出来] —— 屏幕上会短暂出现一下，随后自动关掉")
    ui.show()
    app.processEvents()
    report("FloatingAssist", ui, user32)
    ui.hide()
    app.processEvents()

    print("\n-- 系统托盘（右下角通知区域）--")
    from wechat_assistant.qt_ui.tray import TrayIcon
    print("      isSystemTrayAvailable() = %s" % TrayIcon.available())
    tray = ui.build_tray()
    if tray is None:
        print("      没建出来（这台机器/这个平台没有托盘）")
    else:
        print("      建出来了：可见=%s  tooltip=%r  图标 %d 档"
              % (tray.icon.isVisible(), tray.icon.toolTip(),
                 len(tray.icon.icon().availableSizes())))
        texts = [a.text() for a in tray.icon.contextMenu().actions()]
        print("      右键菜单：%s" % texts)
        # ⚠️ 这一条要**真的来回切一次**才算验到：只看当前那一刻的话，
        #    "菜单文案跟着状态变"这条根本没被执行到（第一版就是这样，
        #    两次打印出来一模一样，看着像没过）。
        def _first():
            return [a.text() for a in tray.icon.contextMenu().actions()][0]

        ui.show()
        app.processEvents()
        _shown = _first()
        ui.hide()
        app.processEvents()
        _hidden = _first()
        print("      面板可见时第一项 = %r  /  藏起来之后 = %r" % (_shown, _hidden))
        print("      -> 文案跟着状态变: %s"
              % ("是" if (_shown, _hidden) == ("隐藏悬浮窗", "显示悬浮窗") else "**否**"))
        ui.show()
        app.processEvents()
        tray.stop()

    print("\n-- 应用级图标 --")
    ic = app.windowIcon()
    print("      QApplication.windowIcon: %s"
          % ("空(没设)" if ic.isNull() else "已设 %d 档"
             % len(ic.availableSizes())))

    print("\n结论：")
    print("  · 「可见 + 无 TOOLWINDOW + 无 owner」三条都满足 -> 任务栏上**本来就有**它；")
    print("    不满足的话要么被 Qt 设成了 Tool，要么被挂了 owner，得显式改窗口类型。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
