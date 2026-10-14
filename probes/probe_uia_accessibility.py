# -*- coding: utf-8 -*-
"""查「微信愿不愿意暴露 UIA 树」这件事到底由什么决定。

背景：同一台机器上微信 4.1.13.65 能读到完整的 `mmui::X*` 控件树；
另一台机器**同一个版本**却只暴露一个 `MMUIRenderSubWindowHW`。
所以差别不在微信版本，在**环境**。

已知的关键开关是 Windows 的「屏幕阅读器」系统参数：
    SystemParametersInfo(SPI_GETSCREENREADER, ...)
Qt 系应用（微信就是 Qt）在启动/收到无障碍请求时，会看这个参数决定
要不要建完整控件树 —— 没有屏幕阅读器就只给一个渲染子窗口，省性能。

本脚本只读，打印这台机器上的相关状态。
"""

import ctypes
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SPI_GETSCREENREADER = 0x0046

user32 = ctypes.windll.user32

print("=" * 70)
print("屏幕阅读器 / 无障碍 相关状态")
print("=" * 70)

# ① SPI_GETSCREENREADER —— 最关键的那个
val = ctypes.c_int(0)
ok = user32.SystemParametersInfoW(SPI_GETSCREENREADER, 0, ctypes.byref(val), 0)
print()
print("① SPI_GETSCREENREADER = %s   (调用%s)"
      % (bool(val.value), "成功" if ok else "失败"))
print("   True  -> 系统认为有屏幕阅读器在跑，Qt 应用会建完整控件树")
print("   False -> Qt 应用只给一个渲染子窗口，**UIA 读不到任何内容**")

# ② 顺带看几个相关的无障碍开关
SPI_GETCLIENTAREAANIMATION = 0x1042
for name, spi in (("SPI_GETCLIENTAREAANIMATION", SPI_GETCLIENTAREAANIMATION),):
    v = ctypes.c_int(0)
    if user32.SystemParametersInfoW(spi, 0, ctypes.byref(v), 0):
        print("\n② %s = %s" % (name, bool(v.value)))

# ③ 有没有正在跑的屏幕阅读器 / 讲述人
import subprocess
print()
print("③ 进程里有没有屏幕阅读器 / 讲述人：")
try:
    out = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=0x08000000).stdout
    hits = [ln.split(",")[0].strip('"') for ln in out.splitlines()
            if any(k in ln.lower() for k in
                   ("narrator", "nvda", "jaws", "zira", "screenread"))]
    if hits:
        for h in set(hits):
            print("   ✓ %s" % h)
    else:
        print("   （没有）")
except Exception as e:
    print("   查不到：%s" % e)

# ④ 当前会话能不能读到 UIA 树（对照）
print()
print("④ 本机微信窗口的 UIA 树暴露情况：")
try:
    import comtypes
    comtypes.CoInitialize()
    import uiautomation as auto
    from uiautomation import uiautomation as core

    auto.UIAutomationInitializerInThread(debug=False).__enter__()
    uia = core._AutomationClient.instance().IUIAutomation

    # 找微信窗口（用 ctypes，不依赖 pywin32）
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
                                     ctypes.c_void_p)
    found = []

    def _cb(hwnd, _lp):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            buf = ctypes.create_unicode_buffer(512)
            user32.GetClassNameW(hwnd, buf, 512)
            cls = buf.value
            if "qwindowicon" not in cls.lower():
                return True
            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid.value)
            pbuf = ctypes.create_unicode_buffer(1024)
            sz = ctypes.c_ulong(1024)
            exe = ""
            if h:
                ctypes.windll.kernel32.QueryFullProcessImageNameW(
                    h, 0, pbuf, ctypes.byref(sz))
                exe = pbuf.value
                ctypes.windll.kernel32.CloseHandle(h)
            if exe.lower().endswith(("weixin.exe", "wechat.exe")):
                found.append(hwnd)
        except Exception:
            pass
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), None)
    if not found:
        print("   没找到微信窗口（先确认微信在跑）")
    else:
        el = auto.ControlFromHandle(found[0]).Element
        arr = el.FindAll(4, uia.CreateTrueCondition())   # 4 = Descendants
        names = {}
        for i in range(arr.Length):
            e = arr.GetElement(i)
            try:
                n = e.CurrentClassName or "(空)"
            except Exception:
                continue
            names[n] = names.get(n, 0) + 1
        print("   后代控件总数 %d，不同类名 %d 种" % (arr.Length, len(names)))
        for k, v in sorted(names.items(), key=lambda kv: -kv[1])[:8]:
            print("     %-40s x%d" % (k[:40], v))
        if len(names) <= 3:
            print()
            print("   ⚠️ 只暴露了极少数控件 —— 这就是「读不到」的形态。")
        else:
            print()
            print("   ✓ 树是完整暴露的。")
except Exception as e:
    print("   读不到：%s: %s" % (type(e).__name__, e))
