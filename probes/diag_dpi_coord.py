# -*- coding: utf-8 -*-
"""诊断：DPI 感知与否，UIA 返回的 rect 坐标系是否一致。

背景
----
`probes/` 里的探测脚本都调用了 `SetProcessDpiAwareness(2)`（每显示器 DPI 感知），
但真实入口 `run_watch.py` **没有**调用它。于是：

  - 探测脚本里验证通过的像素判定（0.94~1.00 vs 0.00）用的是**物理坐标**；
  - 真实程序若被 Windows 虚拟化成**逻辑坐标**，则
    `WindowFromPoint()` / `ImageGrab.grab()` 看到的坐标系与 UIA 给的 rect 可能不一致，
    `_is_obstructed()` 的采样点会飘到别的窗口上，误判「微信被遮挡」而直接放弃判定。

本脚本用同一个进程分别以两种 DPI 模式读取同一批控件，把差异摆出来。
只读：不截图、不点击、不改任何状态。
"""

import ctypes
import sys

sys.path.insert(0, ".")

from wechat_assistant.config import load_config  # noqa: E402
from wechat_assistant.reader import WeChatReader  # noqa: E402
from wechat_assistant.uia_worker import UiaWorker  # noqa: E402


def describe(tag):
    import win32api
    import win32gui
    import win32process

    cfg = load_config()
    w = UiaWorker(20.0)
    r = WeChatReader(cfg, w)
    try:
        win = r.find_window()
        snap = r.read_chat(win["hwnd"])
    finally:
        w.close()

    sw = win32api.GetSystemMetrics(0)
    sh = win32api.GetSystemMetrics(1)
    print("=" * 74)
    print("[%s]  GetSystemMetrics(屏幕) = %dx%d" % (tag, sw, sh))
    print("[%s]  微信窗口 rect = %s" % (tag, win32gui.GetWindowRect(win["hwnd"])))
    print("[%s]  聊天容器 rect = %s" % (tag, snap.container_rect))
    print("[%s]  消息 %d 条  会话 %r" % (tag, len(snap.messages), snap.title))

    crect = snap.container_rect
    if crect:
        x1, y1, x2, y2 = crect
        pts = [
            ("左上+20", (x1 + 20, y1 + 20)),
            ("中心", ((x1 + x2) // 2, (y1 + y2) // 2)),
            ("右下-20", (x2 - 20, y2 - 20)),
        ]
        for name, pt in pts:
            try:
                h = win32gui.WindowFromPoint(pt)
                _, p = win32process.GetWindowThreadProcessId(h)
                cls = win32gui.GetClassName(h)
                txt = win32gui.GetWindowText(h)
                mark = "微信" if p == win["pid"] else "*** 非微信 ***"
                print("[%s]  采样点%-8s %-16s -> hwnd=%s pid=%s %s %r %s"
                      % (tag, name, str(pt), h, p, cls[:28], txt[:24], mark))
            except Exception as e:
                print("[%s]  采样点%-8s %-16s -> 异常 %s: %s"
                      % (tag, name, str(pt), type(e).__name__, e))

    # pywin32 拿到的光标位置，和 UIA 坐标系比一比
    cur = win32api.GetCursorPos()
    print("[%s]  光标(物理屏幕坐标) = %s" % (tag, str(cur)))
    return sw, sh, snap.container_rect


if __name__ == "__main__":
    # DPI 感知等级每个进程只能设置一次（第二次调用返回 E_ACCESSDENIED），
    # 所以两种模式必须分两次运行，用参数选择。
    aware = "--aware" in sys.argv
    mode = 2 if aware else 0          # 2 = PROCESS_PER_MONITOR_DPI_AWARE
    label = "DPI 感知 (2)" if aware else "DPI 不感知 (0)"
    try:
        hr = ctypes.windll.shcore.SetProcessDpiAwareness(mode)
        if hr != 0:
            print("SetProcessDpiAwareness(%d) 返回 0x%08X（可能已被设置过）" % (mode, hr))
    except Exception as e:
        print("SetProcessDpiAwareness 失败: %s: %s" % (type(e).__name__, e))
    describe(label)
