# -*- coding: utf-8 -*-
"""
后台可读性测试：微信窗口「最小化」和「被遮挡」时，UIA 还能不能读到消息？

这个问题决定整个项目的形态：
    - 如果最小化也能读 => 助手可以作为后台常驻服务，随时抓取最新消息
    - 如果必须窗口可见 => 只能在你正在聊天时才工作

做法：
    在不改变任何微信数据的前提下，临时最小化微信窗口，跑一次 UIA FindAll 统计，
    然后立即恢复。用 try/finally 保证窗口一定被还原。

只读性：只调用 ShowWindow 切换窗口显示状态 + UIA 查询；不发消息、不点击聊天。

用法：python test_background.py
"""

import ctypes
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

import win32api
import win32con
import win32gui
import win32process

TARGET_PROC = "weixin.exe"


def find_wechat_hwnds():
    found = []

    def cb(hwnd, _):
        try:
            if "QWindowIcon" not in win32gui.GetClassName(hwnd):
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            h = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            try:
                path = win32process.GetModuleFileNameEx(h, 0)
            finally:
                win32api.CloseHandle(h)
            if os.path.basename(path).lower() == TARGET_PROC:
                found.append(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return found


def uia_snapshot(hwnd, timeout=25.0):
    """返回 (消息条数, 会话条数, 全部元素数, 错误)。"""
    import threading

    box = {}

    def work():
        import comtypes

        comtypes.CoInitialize()
        import uiautomation as auto
        from uiautomation import uiautomation as core

        with auto.UIAutomationInitializerInThread(debug=False):
            uia = core._AutomationClient.instance().IUIAutomation
            el = auto.ControlFromHandle(hwnd).Element
            arr = el.FindAll(4, uia.CreateTrueCondition())
            msgs = sessions = 0
            for i in range(arr.Length):
                e = arr.GetElement(i)
                cls = e.CurrentClassName or ""
                aid = e.CurrentAutomationId or ""
                if cls == "mmui::ChatSessionCell":
                    sessions += 1
                elif aid.startswith("chat_message_list") and cls.endswith("ItemView"):
                    msgs += 1
            box["msgs"] = msgs
            box["sessions"] = sessions
            box["total"] = arr.Length

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return (None, None, None, "超时 %.0fs" % timeout)
    if "error" in box:
        return (None, None, None, box["error"])
    return (box.get("msgs"), box.get("sessions"), box.get("total"), None)


def main():
    hwnds = find_wechat_hwnds()
    if not hwnds:
        print("!! 未找到微信主窗口")
        return
    hwnd = hwnds[0]
    print("微信 hwnd = %s" % hex(hwnd))
    print("=" * 74)
    print("%-26s %-10s %-10s %-10s %s" % ("状态", "消息条数", "会话条数", "元素总数", "备注"))
    print("=" * 74)

    def show(label, note=""):
        m, s, t, err = uia_snapshot(hwnd)
        if err:
            print("%-26s %-10s %-10s %-10s %s" % (label, "-", "-", "-", err))
        else:
            print("%-26s %-10s %-10s %-10s %s" % (label, m, s, t, note))
        return m, s, t, err

    minimized = False
    try:
        # 1) 当前状态（未激活/被遮挡）
        fg = win32gui.GetForegroundWindow()
        print("当前前台窗口 = %s (%s)" % (hex(fg), win32gui.GetWindowText(fg)[:40]))
        show("1.当前状态(被遮挡)", "可见但非前台")

        # 2) 最小化
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        minimized = True
        time.sleep(1.2)
        show("2.最小化", "SW_MINIMIZE")

        # 3) 隐藏窗口（SW_HIDE，比最小化更极端）
        win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
        time.sleep(1.0)
        show("3.隐藏", "SW_HIDE")
    finally:
        # 恢复
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.6)
            minimized = False
        except Exception as e:
            print("!! 恢复窗口失败: %s" % e)

    print("=" * 74)
    m, s, t, err = show("4.已恢复", "SW_RESTORE")
    print("")
    print("判读：")
    print("  若最小化/隐藏时消息条数仍 > 0  =>  UIA 与窗口显示状态无关，可做后台常驻。")
    print("  若变为 0 或超时              =>  必须先让窗口可见才能读，只能做「前台辅助」。")


if __name__ == "__main__":
    main()
