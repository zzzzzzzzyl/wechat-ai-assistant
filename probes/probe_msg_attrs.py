# -*- coding: utf-8 -*-
"""
消息项属性全量 dump：为「区分对方 / 自己」找可用线索。

问题：
    消息项的 BoundingRectangle 是整行宽度（如 1353..2178），
    所以没法靠 x 坐标判断气泡是靠左（对方）还是靠右（自己）。
    需要确认 UIA 里是否还有别的属性可用。

做法：
    对 chat_message_list 下的每个消息项，打印全部可读 UIA 属性
    （ItemStatus / ItemType / HelpText / AriaRole / Value / 各类 pattern），
    并列出该元素支持的所有 UIA Pattern。

只读：仅查询属性。
"""

import ctypes
import os
import sys
import threading

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
            if not win32gui.IsWindowVisible(hwnd):
                return True
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


def work(hwnd, out):
    import comtypes

    comtypes.CoInitialize()
    import uiautomation as auto
    from uiautomation import uiautomation as core

    with auto.UIAutomationInitializerInThread(debug=False):
        uia = core._AutomationClient.instance().IUIAutomation
        el = auto.ControlFromHandle(hwnd).Element
        arr = el.FindAll(4, uia.CreateTrueCondition())

        msgs = []
        for i in range(arr.Length):
            e = arr.GetElement(i)
            aid = e.CurrentAutomationId or ""
            cls = e.CurrentClassName or ""
            if aid.startswith("chat_message_list") and cls.endswith("ItemView"):
                msgs.append(e)

        out["count"] = len(msgs)
        rows = []
        for e in msgs:
            d = {}
            for attr in [
                "CurrentName",
                "CurrentClassName",
                "CurrentAutomationId",
                "CurrentItemStatus",
                "CurrentItemType",
                "CurrentHelpText",
                "CurrentAriaRole",
                "CurrentAriaProperties",
                "CurrentAccessKey",
                "CurrentAcceleratorKey",
                "CurrentLocalizedControlType",
                "CurrentIsContentElement",
                "CurrentIsControlElement",
                "CurrentIsEnabled",
                "CurrentIsOffscreen",
                "CurrentFrameworkId",
                "CurrentProviderDescription",
            ]:
                try:
                    d[attr] = getattr(e, attr)
                except Exception as ex:
                    d[attr] = "<err %s>" % type(ex).__name__
            try:
                r = e.CurrentBoundingRectangle
                d["rect"] = (r.left, r.top, r.right, r.bottom)
            except Exception:
                d["rect"] = None
            # 支持哪些 pattern
            pats = []
            try:
                for pid in range(10000, 10030):
                    try:
                        p = e.GetCurrentPattern(pid)
                        if p is not None:
                            nm = core.PatternId.__dict__
                            rev = {v: k for k, v in nm.items() if isinstance(v, int)}
                            pats.append(rev.get(pid, str(pid)))
                    except Exception:
                        pass
            except Exception:
                pass
            d["patterns"] = pats
            # LegacyIAccessible 详细
            try:
                lp = e.GetCurrentPattern(core.PatternId.LegacyIAccessiblePattern)
                if lp:
                    d["legacy"] = {
                        "name": lp.CurrentName,
                        "value": lp.CurrentValue,
                        "description": lp.CurrentDescription,
                        "role": lp.CurrentRole,
                        "state": lp.CurrentState,
                        "childId": lp.CurrentChildId,
                        "keyboardShortcut": lp.CurrentKeyboardShortcut,
                        "defaultAction": lp.CurrentDefaultAction,
                    }
            except Exception as ex:
                d["legacy"] = "<err %s>" % type(ex).__name__
            rows.append(d)
        out["rows"] = rows


def _safe_work(hwnd, out):
    try:
        work(hwnd, out)
    except Exception as e:
        import traceback

        out["error"] = "%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()[-800:])


def main():
    hwnds = find_wechat_hwnds()
    if not hwnds:
        print("!! 未找到微信主窗口")
        return
    hwnd = hwnds[0]
    print("微信 hwnd = %s" % hex(hwnd))

    out = {}
    t = threading.Thread(target=_safe_work, args=(hwnd, out), daemon=True)
    t.start()
    t.join(40)
    if t.is_alive():
        print("!! 超时")
        return
    if out.get("error"):
        print("!! %s" % out["error"])
        return

    print("消息项数量: %s" % out.get("count"))
    print("=" * 78)
    for i, d in enumerate(out.get("rows", [])):
        print("--- 消息 %d ---" % (i + 1))
        print("  Name            = %r" % d.get("CurrentName"))
        print("  ClassName       = %r" % d.get("CurrentClassName"))
        print("  AutomationId    = %r" % d.get("CurrentAutomationId"))
        print("  Rect            = %s" % (d.get("rect"),))
        print("  ItemStatus      = %r" % d.get("CurrentItemStatus"))
        print("  ItemType        = %r" % d.get("CurrentItemType"))
        print("  HelpText        = %r" % d.get("CurrentHelpText"))
        print("  AriaRole        = %r" % d.get("CurrentAriaRole"))
        print("  AriaProperties  = %r" % d.get("CurrentAriaProperties"))
        print("  LocalizedType   = %r" % d.get("CurrentLocalizedControlType"))
        print("  FrameworkId     = %r" % d.get("CurrentFrameworkId"))
        print("  ProviderDesc    = %r" % d.get("CurrentProviderDescription"))
        print("  Patterns        = %s" % d.get("patterns"))
        print("  LegacyIA        = %s" % d.get("legacy"))
        print("")

    here = os.path.dirname(os.path.abspath(__file__))
    outdir = os.path.join(os.path.dirname(here), "out")
    os.makedirs(outdir, exist_ok=True)
    import datetime as dt

    path = os.path.join(outdir, "msg_attrs_%s.txt" % dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", encoding="utf-8") as f:
        for i, d in enumerate(out.get("rows", [])):
            f.write("--- 消息 %d ---\n" % (i + 1))
            for k, v in d.items():
                f.write("  %s = %r\n" % (k, v))
            f.write("\n")
    print("[已保存] %s" % path)


if __name__ == "__main__":
    main()
