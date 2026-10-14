# -*- coding: utf-8 -*-
"""「UIA 树只有 2 个节点」这句话到底在说什么——一条命令看穿。

结论预告：
    2 是**直接子节点**（也是原生子窗口 HWND）的数量，不是控件树的总量。
    FindAll(TreeScope_Children)     -> 2
    FindAll(TreeScope_Descendants)  -> 150+
    EnumChildWindows                -> 2
    差额全部在更深层，「消息区」在第 12 层左右。

这个脚本就是为「一句话证伪/证实」准备的：把同一个顶层 hwnd 换三种数法各数一遍，
再把从顶层到一条真实消息的路径打印出来。

纯只读。
"""

import datetime as dt
import os
import sys
import threading

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import win32api
import win32con
import win32gui
import win32process

PROC = "weixin.exe"
TIMEOUT = 45.0
SCOPE_CHILDREN = 2        # TreeScope_Children
SCOPE_DESCENDANTS = 4     # TreeScope_Descendants


def find_hwnd():
    found = []

    def cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if "QWindowIcon" not in win32gui.GetClassName(hwnd):
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            h = win32api.OpenProcess(
                win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            try:
                path = win32process.GetModuleFileNameEx(h, 0)
            finally:
                win32api.CloseHandle(h)
            if os.path.basename(path).lower() == PROC:
                found.append(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return found[0] if found else None


def work(out):
    import comtypes
    comtypes.CoInitialize()

    import uiautomation as auto
    from uiautomation import uiautomation as core

    hwnd = find_hwnd()
    out["hwnd"] = hwnd
    if not hwnd:
        out["err"] = "没找到微信窗口"
        return

    with auto.UIAutomationInitializerInThread(debug=False):
        uia = core._AutomationClient.instance().IUIAutomation
        root = auto.ControlFromHandle(hwnd).Element

        # ① 只看一层
        arr1 = root.FindAll(SCOPE_CHILDREN, uia.CreateTrueCondition())
        kids = []
        for i in range(arr1.Length):
            e = arr1.GetElement(i)
            kids.append("%s (class=%s)" % (e.CurrentName or "(无名)",
                                           e.CurrentClassName or "?"))
        out["children"] = (arr1.Length, kids)

        # ② 看全部后代
        arr2 = root.FindAll(SCOPE_DESCENDANTS, uia.CreateTrueCondition())
        out["descendants"] = arr2.Length

        # ③ 从顶层逐层递归，记下每个节点的层数
        #    （不用 RawViewWalker 反查父链——那条路在 comtypes 接口上没走通）
        msgs = []
        keys = {}
        ctrl = auto.ControlFromHandle(hwnd)
        walked = [0]

        def rec(c, depth):
            if depth > 60 or walked[0] > 3000:
                return
            for k in c.GetChildren():
                walked[0] += 1
                try:
                    cls = k.ClassName or ""
                    aid = k.AutomationId or ""
                    nm = k.Name or ""
                except Exception:
                    continue
                if aid in ("chat_message_list", "session_list"):
                    keys[aid] = (depth, cls)
                if cls.endswith("ItemView") and nm.strip():
                    msgs.append((depth, cls, nm))
                rec(k, depth + 1)

        rec(ctrl, 0)
        out["walked_total"] = walked[0]

        out["messages"] = msgs
        out["keys"] = keys

        # ④ 原生子窗口 HWND
        hwnd_kids = []

        def cbk(h, _):
            try:
                hwnd_kids.append("%s (class=%s)" % (hex(h), win32gui.GetClassName(h)))
            except Exception:
                pass
            return True

        win32gui.EnumChildWindows(hwnd, cbk, None)
        out["hwnd_children"] = (len(hwnd_kids), hwnd_kids)


def main():
    out = {}
    t = threading.Thread(target=_safe, args=(out,), daemon=True)
    t.start()
    t.join(TIMEOUT)

    print("=" * 78)
    print("「UIA 树只有 2 个节点」核查")
    print("时间: %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 78)
    if t.is_alive():
        print("超时 %.0fs" % TIMEOUT)
        return
    if out.get("err"):
        print("错误: %s" % out["err"])
        return

    print("微信顶层 hwnd: %s   类名: %s"
          % (hex(out["hwnd"]), win32gui.GetClassName(out["hwnd"])))
    print("")

    n, kids = out["children"]
    print("① FindAll(TreeScope_Children)      = %d 个" % n)
    for k in kids:
        print("      %s" % k)
    print("")
    print("② FindAll(TreeScope_Descendants)   = %d 个"
          % out["descendants"])
    print("")
    hn, hk = out["hwnd_children"]
    print("③ EnumChildWindows（原生 HWND 子窗口） = %d 个" % hn)
    for k in hk:
        print("      %s" % k)
    print("")

    print("④ 关键节点在第几层（从顶层算起）:")
    for k, (d, cls) in (out.get("keys") or {}).items():
        print("      %-22s 深度 %-4s class=%s"
              % (k, "?" if d is None else d, cls))
    print("")

    msgs = out.get("messages") or []
    print("⑤ 读到的消息正文（%d 条，深度即从顶层算的层数）:" % len(msgs))
    for d, cls, nm in msgs[:10]:
        print("      深度 %-4s %-28s %r"
              % ("?" if d is None else d, cls, nm[:50]))
    print("")
    print("换句话说：只看第一层 = %d 个（就是那句话里的「2」）；"
          % n)
    print("          看全部后代 = %d 个，多出来的 %d 个全在更深处。"
          % (out["descendants"], out["descendants"] - n))


def _safe(out):
    try:
        work(out)
    except Exception as e:
        import traceback
        out["err"] = "%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()[-500:])


if __name__ == "__main__":
    main()
