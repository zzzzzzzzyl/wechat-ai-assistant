# -*- coding: utf-8 -*-
"""证明 `PrintWindow` 是**同步的跨进程消息**：目标窗口不泵消息时，调用方会一直挂着。

    python probes\\probe_printwindow_hang.py

为什么需要这个实验
==================
`capture.capture_window()` 用 `PrintWindow(PW_RENDERFULLCONTENT)` 抓微信窗口，
而**调用方有一个在 Qt UI 线程上**（`panel._resample_theme_foreground`，每 ~5 秒
重采一次主题，前置条件正是"微信在前台、本程序在后台"）。

`PrintWindow` 内部走 `SendMessage(WM_PRINT)` —— **同步、跨进程、没有超时参数**。
目标窗口的 UI 线程不处理消息时，调用方会**永远**卡在里面。UI 线程卡住 =
窗口不重绘、不响应点击、Windows 标「未响应」。这就是用户报的
「放到后台 / 休眠后程序卡死」的机制（休眠醒来后微信主线程常几十秒在重连/重绘）。

实测结论（2026-10-19，本机）
===========================
    PrintWindow  -> 活窗口        31.9 ms 返回
    PrintWindow  -> 不泵消息窗口   **8 秒仍未返回**（实验主动放弃等待）
    capture_window() -> 活窗口    出图，稳态 17 ms（首帧 ~215 ms）
    capture_window() -> 卡死窗口  **2001 ms 返回 None**（= CAPTURE_TIMEOUT）

做法
====
靶子是**真的原生窗口**（不是 mock）：本文件用 `--target` 把自己再起一份子进程，
在那里 `RegisterClassW` + `CreateWindowExW` + `ShowWindow`，然后
  · `pump`   —— 正常 `PeekMessage/DispatchMessage`（= 微信活着）
  · `nopump` —— 只 `sleep`，**一条消息都不处理**（= 微信卡住）
父进程按窗口标题找到句柄，分别在**一条线程**里调 PrintWindow / capture_window，
主线程最多等 8 秒，用"有没有在 8 秒内返回"作判据。

⚠️ 纯只读：只在桌面上闪一个 900×700 的空窗口，不碰微信、不改任何状态。
"""

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PW_RENDERFULLCONTENT = 0x2
WS_OVERLAPPEDWINDOW = 0x00CF0000
CLASS_NAME = "PwProbeTarget"
WAIT = 8.0

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
k32 = ctypes.windll.kernel32


# ---------------------------------------------------------------- 子进程：靶窗口

class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint),
                ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE),
                ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE),
                ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


def _target_main(title: str, mode: str, life: float) -> int:
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.CreateWindowExW.restype = wt.HWND
    user32.CreateWindowExW.argtypes = [
        wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
    user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    user32.UpdateWindow.argtypes = [wt.HWND]
    user32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND,
                                    wt.UINT, wt.UINT, wt.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
    k32.GetModuleHandleW.restype = wt.HMODULE
    k32.GetModuleHandleW.argtypes = [wt.LPCWSTR]

    wc = WNDCLASSW()
    wc.lpfnWndProc = ctypes.cast(user32.DefWindowProcW, ctypes.c_void_p).value
    wc.hInstance = k32.GetModuleHandleW(None)
    wc.lpszClassName = CLASS_NAME
    wc.hbrBackground = ctypes.c_void_p(6)          # COLOR_WINDOW + 1
    if not user32.RegisterClassW(ctypes.byref(wc)):
        if k32.GetLastError() != 1410:             # 1410 = 类已存在
            return 1
    hwnd = user32.CreateWindowExW(
        0, CLASS_NAME, title, WS_OVERLAPPEDWINDOW,
        200, 120, 900, 700, None, None, wc.hInstance, None)
    if not hwnd:
        return 1
    user32.ShowWindow(hwnd, 1)
    user32.UpdateWindow(hwnd)
    print("READY hwnd=%d" % hwnd, flush=True)

    end = time.time() + life
    if mode == "pump":
        msg = wt.MSG()
        while time.time() < end:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.002)
    else:
        time.sleep(life)                           # 故意不泵消息（= 卡死）
    return 0


# ---------------------------------------------------------------- 父进程：测量

def _spawn(title: str, mode: str, life: float = 40.0):
    p = subprocess.Popen([sys.executable, os.path.abspath(__file__),
                          "--target", title, mode, str(life)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    hwnd = 0
    t0 = time.time()
    while time.time() - t0 < 10:
        hwnd = user32.FindWindowW(CLASS_NAME, title)
        if hwnd:
            break
        time.sleep(0.05)
    if not hwnd:
        p.kill()
        return None, 0
    time.sleep(0.6)                                # 让它把首帧画完
    return p, hwnd


def raw_printwindow(hwnd):
    """直接调 PrintWindow（不带任何保护），返回 (是否在 WAIT 内返回, ms)。"""
    box = {"done": False, "ms": None}

    def call():
        t = time.perf_counter()
        hdc = user32.GetWindowDC(hwnd)
        mdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, 900, 700)
        gdi32.SelectObject(mdc, bmp)
        try:
            user32.PrintWindow(hwnd, mdc, PW_RENDERFULLCONTENT)
        finally:
            box["ms"] = (time.perf_counter() - t) * 1000
            box["done"] = True

    th = threading.Thread(target=call, daemon=True)
    th.start()
    th.join(WAIT)
    return box["done"], box["ms"]


def main() -> int:
    print("=" * 74)
    print("PrintWindow 是不是同步的跨进程消息？（活窗口 vs 不泵消息的窗口）")
    print("=" * 74)
    user32.FindWindowW.restype = wt.HWND
    user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
    user32.GetWindowDC.restype = wt.HDC
    user32.GetWindowDC.argtypes = [wt.HWND]
    user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, ctypes.c_uint]
    gdi32.CreateCompatibleDC.restype = wt.HDC
    gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
    gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
    gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]

    # ---- ① 裸调 PrintWindow
    for mode in ("pump", "nopump"):
        p, hwnd = _spawn("pwprobe-%s" % mode, mode)
        if not hwnd:
            print("[%s] 靶窗口没建出来" % mode)
            continue
        try:
            done, ms = raw_printwindow(hwnd)
            if done:
                print("[%s] 裸 PrintWindow: %.1f ms 返回" % (mode, ms))
            else:
                print("[%s] 裸 PrintWindow: **%.0f 秒仍未返回** —— 一直挂着"
                      % (mode, WAIT))
        finally:
            p.kill()
            p.wait(3)

    # ---- ② 修好之后的 capture_window（两道保险 + 看门狗）
    from wechat_assistant import capture                  # 延迟导入，①不需要它

    for mode in ("pump", "nopump"):
        p, hwnd = _spawn("pwprobe-fixed-%s" % mode, mode)
        if not hwnd:
            print("[%s] 靶窗口没建出来" % mode)
            continue
        try:
            t = time.perf_counter()
            img = capture.capture_window(hwnd)
            ms = (time.perf_counter() - t) * 1000
            if img is None:
                print("[%s] capture_window: 返回 None（%.0f ms）" % (mode, ms))
            else:
                print("[%s] capture_window: %dx%d 的图（%.0f ms），空白=%s"
                      % (mode, img.width, img.height, ms, capture.is_blank(img)))
        finally:
            p.kill()
            p.wait(3)

    print("\n判据：裸 PrintWindow 在 nopump 上不返回、capture_window 在 %.0f 秒内"
          "返回 None，就说明这两道保险真的拦住了（见 capture.py 的说明）。"
          % capture.CAPTURE_TIMEOUT)
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "--target":
        sys.exit(_target_main(sys.argv[2], sys.argv[3], float(sys.argv[4])))
    sys.exit(main())
