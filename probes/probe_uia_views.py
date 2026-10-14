# -*- coding: utf-8 -*-
"""对比 UIA 的**三种视图**，看微信到底在哪个视图里暴露了控件。

为什么要做这个（2026-09-27）
---------------------------
在一篇 CSDN 文章里看到一种说法：微信「仅在检测到无障碍客户端时，
才会激活完整的 **Control View** 暴露」，并给了 C# 示例 —— 用的是
`TreeWalker.ControlViewWalker`。

⚠️ **那篇文章的解释本身站不住**（它说微信检测"客户端引用了哪些 DLL"，
   而跨进程根本看不到对方的 DLL 加载表；真正的 API 是 `UiaClientsAreListening()`
   / `WM_GETOBJECT` / `SPI_GETSCREENREADER`）。它给的 C# 代码也只是
   `AutomationElement.FromHandle(hwnd)` + 遍历 —— **正是我们已经做的事**。

**但它点出了一个我们确实没验过的变量**：

    我们一直用 `FindAll(TreeScope_Descendants, TrueCondition)`，
    它走的是**默认/Raw 视图**；
    而 UIA 还有 **Control View** 和 **Content View** 两套过滤视图，
    用 `TreeWalker.XxxViewWalker` 走。

**如果微信只在某个特定视图里暴露控件，那这就直接解释了「读不到」。**
这个假设便宜、可证伪，值得实测。

用法：
    python probes/probe_uia_views.py
"""

import ctypes
import sys

sys.path.insert(0, ".")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCOPE_DESCENDANTS = 4

PROC_NAMES = ("weixin.exe", "wechat.exe")


def find_wechat_windows():
    user32 = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
                                     ctypes.c_void_p)
    out = []

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
            if not h:
                return True
            pbuf = ctypes.create_unicode_buffer(1024)
            sz = ctypes.c_ulong(1024)
            exe = ""
            if ctypes.windll.kernel32.QueryFullProcessImageNameW(
                    h, 0, pbuf, ctypes.byref(sz)):
                exe = pbuf.value
            ctypes.windll.kernel32.CloseHandle(h)
            if not exe.lower().endswith(PROC_NAMES):
                return True
            user32.GetWindowTextW(hwnd, buf, 512)
            out.append((hwnd, cls, buf.value))
        except Exception:
            pass
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), None)
    return out


def walk_count(walker, root, cap=5000):
    """用给定的 TreeWalker 走一遍子树，统计节点数。

    ⚠️ **不要加深度上限** —— 我第一版写了 `max_depth=6`，
    结果三种 walker 都返回 39，看起来像"视图都一样"，
    其实是**被深度截断了**（真实是 159）。一个错误的限制会直接造出一个假结论。
    """
    seen = 0
    stack = [root]
    while stack:
        el = stack.pop()
        seen += 1
        if seen > cap:
            break
        try:
            child = walker.GetFirstChildElement(el)
        except Exception:
            continue
        while child:
            stack.append(child)
            try:
                child = walker.GetNextSiblingElement(child)
            except Exception:
                break
    return seen


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    wins = find_wechat_windows()
    if not wins:
        print("没找到微信窗口（先确认微信在跑）")
        return 1

    import comtypes
    comtypes.CoInitialize()
    import uiautomation as auto
    from uiautomation import uiautomation as core

    auto.UIAutomationInitializerInThread(debug=False).__enter__()
    uia = core._AutomationClient.instance().IUIAutomation

    print("=" * 78)
    print("UIA 三种视图对比")
    print("=" * 78)
    print("Raw=原始视图(全部元素)  Control=控件视图(去布局)  Content=内容视图(只要内容)")
    print()

    for hwnd, cls, title in wins:
        print("窗口 %s 「%s」" % (hex(hwnd), title[:24]))
        root = auto.ControlFromHandle(hwnd).Element

        # ① FindAll —— 我们一直在用的方式（默认视图）
        try:
            n_findall = root.FindAll(SCOPE_DESCENDANTS,
                                     uia.CreateTrueCondition()).Length
        except Exception as e:
            n_findall = "错误: %s" % e
        print("   FindAll(Descendants, True)   = %s" % n_findall)

        # ② 三种 TreeWalker
        for name in ("RawViewWalker", "ControlViewWalker", "ContentViewWalker"):
            try:
                w = getattr(uia, name)
                n = walk_count(w, root)
            except Exception as e:
                n = "错误: %s" % e
            print("   TreeWalker.%-18s = %s" % (name, n))

        # ③ 三个「视图条件」—— ⚠️ 它们是 IUIAutomation 的**属性**，不是方法。
        #    我第一版写成 `CreateControlViewCondition()`，comtypes 直接报
        #    "找不到该属性"。真实名字是 `ControlViewCondition` 等。
        for name in ("RawViewCondition", "ControlViewCondition",
                     "ContentViewCondition"):
            try:
                cond = getattr(uia, name)
                n = root.FindAll(SCOPE_DESCENDANTS, cond).Length
            except Exception as e:
                n = "错误: %s" % e
            print("   FindAll(%s)   = %s" % (name, n))
        print()

    print("=" * 78)
    print("怎么读：")
    print("  · 三个视图数字**都很大** -> 视图不是问题，读不到是别的原因")
    print("  · **某个视图明显更大**   -> 那就是它！改用那个视图即可")
    print("  · 全都只有个位数         -> 微信确实没暴露，和视图无关")
    return 0


if __name__ == "__main__":
    sys.exit(main())
