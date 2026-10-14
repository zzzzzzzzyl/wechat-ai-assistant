# -*- coding: utf-8 -*-
"""验证各种「触发源」在技术上是否可用。

目标：让用户自己选择什么时机触发「读取当前会话 + 生成建议」，候选有：
    A. 按快捷键（全局热键，程序不在前台也能收到）
    B. 微信窗口被显示/激活时（SetWinEventHook 事件，不需要轮询）
    C. 切换会话时（轻量轮询标题，约 12ms/次）
    D. 点悬浮窗按钮（P1 才有 UI，这里不测）

其中 A 和 B 都需要 Windows 消息循环，本脚本先验证它们能否正常工作。

只读：仅注册热键、监听窗口事件、读取标题。
      不模拟任何键鼠操作（热键需要你手动按一下），不碰微信消息。

用法：
    python probes/test_triggers.py
    然后按提示操作：按一下 Ctrl+Alt+Q，并把微信窗口最小化再恢复。
"""

import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

from wechat_assistant.config import load_config
from wechat_assistant.reader import WeChatReader
from wechat_assistant.uia_worker import UiaWorker

import win32con
import win32gui

user32 = ctypes.windll.user32

# ---- 热键相关常量
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312

# ---- 窗口事件常量
EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_HIDE = 0x8003
EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000

PM_REMOVE = 0x0001

hotkey_hits = []
winevent_hits = []

WinEventProc = ctypes.WINFUNCTYPE(
    None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
    wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD,
)

_keep_ref = []      # 回调必须保持引用，否则会被 GC 掉导致崩溃


def test_hotkey():
    print("\n【A】全局热键 RegisterHotKey")
    vk_q = ord("Q")
    ok = user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, vk_q)
    if not ok:
        err = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else "?"
        print("    注册失败！可能已被别的程序占用。err=%s" % err)
        return False
    print("    注册成功（Ctrl+Alt+Q 未被别的程序占用）。")
    print("    本脚本不模拟按键（守红线），这里只确认注册可用。")
    return True


def test_winevent(hwnd):
    print("\n【B】窗口事件 SetWinEventHook（监听微信窗口显示/隐藏/前台切换）")

    def cb(hook, event, h, idobj, idchild, thread, t):
        try:
            if h == hwnd:
                name = {EVENT_OBJECT_SHOW: "SHOW",
                        EVENT_OBJECT_HIDE: "HIDE",
                        EVENT_SYSTEM_FOREGROUND: "FOREGROUND"}.get(event, hex(event))
                winevent_hits.append(name)
                print("      收到事件: %s  hwnd=%s" % (name, hex(h)))
        except Exception:
            pass

    p = WinEventProc(cb)
    _keep_ref.append(p)

    hooks = []
    for ev in (EVENT_OBJECT_SHOW, EVENT_OBJECT_HIDE, EVENT_SYSTEM_FOREGROUND):
        h = user32.SetWinEventHook(ev, ev, None, p, 0, 0, WINEVENT_OUTOFCONTEXT)
        hooks.append((ev, h))
        print("    注册 %s -> hook=%s" % (hex(ev), h))
    if not any(h for _, h in hooks):
        print("    !! 全部注册失败")
        return None
    print("    注册完成（稍后脚本会自动最小化/恢复微信窗口来触发它）")
    return hooks


def message_pump(stop_flag, seconds):
    """跑一段消息循环，处理 WM_HOTKEY 等消息。"""
    msg = wintypes.MSG()
    t0 = time.time()
    while time.time() - t0 < seconds and not stop_flag.is_set():
        got = False
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            got = True
            if msg.message == WM_HOTKEY:
                hotkey_hits.append(msg.wParam)
                print("      收到热键! id=%s" % msg.wParam)
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        if not got:
            time.sleep(0.01)


def main():
    cfg = load_config()
    worker = UiaWorker(timeout=20.0, logger=lambda m: None)
    reader = WeChatReader(cfg, worker, logger=lambda m: None)
    win = reader.find_window()
    if not win:
        print("!! 未找到微信窗口")
        return
    hwnd = win["hwnd"]

    print("=" * 84)
    print("触发源技术验证")
    print("=" * 84)
    print("微信窗口: %s (pid=%d)" % (hex(hwnd), win["pid"]))

    hotkey_ok = test_hotkey()
    hooks = test_winevent(hwnd)

    print("\n【C】切换会话检测（轻量轮询标题，不依赖消息循环）")
    t0 = time.perf_counter()
    title0 = reader.read_chat_title(hwnd)
    ms = (time.perf_counter() - t0) * 1000
    print("    当前会话: %r   单次读取耗时 %.1f ms" % (title0, ms))
    print("    （你在这 15 秒内切换一下微信会话，看能否检测到）")

    print("\n" + "-" * 84)
    print("开始 8 秒观察窗口（窗口事件由脚本自动触发，无需你操作）")
    print("-" * 84)

    stop = threading.Event()
    titles = [title0]

    def poll_title():
        while not stop.is_set():
            try:
                t = reader.read_chat_title(hwnd)
                if t and t != titles[-1]:
                    titles.append(t)
                    print("      [会话切换] -> %r" % t)
            except Exception:
                pass
            time.sleep(0.5)

    def auto_toggle():
        """自动最小化/恢复微信窗口，用来触发 SHOW/HIDE 事件。"""
        time.sleep(1.5)
        print("    [自动] 最小化微信窗口")
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        time.sleep(1.5)
        print("    [自动] 恢复微信窗口")
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(1.0)
        try:
            win32gui.SetForegroundWindow(hwnd)
            print("    [自动] 把微信切到前台")
        except Exception:
            pass

    th = threading.Thread(target=poll_title, daemon=True)
    th.start()
    tt = threading.Thread(target=auto_toggle, daemon=True)
    tt.start()
    message_pump(stop, 8.0)
    stop.set()
    th.join(timeout=1.0)

    if hotkey_ok:
        user32.UnregisterHotKey(None, 1)
    for ev, h in (hooks or []):
        if h:
            user32.UnhookWinEvent(h)

    print("\n" + "=" * 84)
    print("结果")
    print("=" * 84)
    print("  A. 全局热键        : %s  收到 %d 次"
          % ("可用" if hotkey_hits else "未收到（没按？或注册失败）", len(hotkey_hits)))
    print("  B. 窗口事件        : %s  收到 %d 次 %s"
          % ("可用" if winevent_hits else "未收到", len(winevent_hits), winevent_hits))
    print("  C. 切换会话检测    : 捕获到 %d 个标题变化 %s"
          % (len(titles) - 1, titles))
    print("")
    print("判读：")
    print("  - A 可用 -> 可以做「按快捷键触发」，程序不在前台也能响应")
    print("  - B 可用 -> 微信窗口显示时能零轮询地被唤醒，适合做「打开微信就准备建议」")
    print("  - C 可用 -> 可以做「切换会话即触发」，成本约 12ms/次")
    worker.close()


if __name__ == "__main__":
    main()
