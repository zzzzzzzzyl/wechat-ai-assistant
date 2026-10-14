# -*- coding: utf-8 -*-
"""
用 UIA TextPattern 拿「消息文字的真实渲染矩形」，解决发送者左右判断。

思路：
    消息项的 BoundingRectangle 是整行宽度，无法判断气泡在左（对方）还是在右（自己）。
    但如果消息项支持 TextPattern，那么 TextPattern.DocumentRange.GetBoundingRectangles()
    返回的是**文字实际被绘制的位置**，这个坐标就能区分左右。

只读：仅调用 TextPattern 的查询方法。
"""

import ctypes
import datetime as dt
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

        container = None
        items = []
        for i in range(arr.Length):
            e = arr.GetElement(i)
            aid = e.CurrentAutomationId or ""
            cls = e.CurrentClassName or ""
            if aid == "chat_message_list":
                r = e.CurrentBoundingRectangle
                container = (r.left, r.top, r.right, r.bottom)
            elif aid.startswith("chat_message_list") and cls.endswith("ItemView"):
                items.append(e)
        out["container"] = container

        rows = []
        for e in items:
            ctl = auto.Control.CreateControlFromElement(e)
            r = e.CurrentBoundingRectangle
            rec = {
                "name": e.CurrentName,
                "class": e.CurrentClassName,
                "item_rect": (r.left, r.top, r.right, r.bottom),
                "text": None,
                "rects": None,
                "err": None,
            }
            try:
                pat = ctl.GetPattern(core.PatternId.TextPattern)
                if pat is None:
                    rec["err"] = "不支持 TextPattern"
                else:
                    rng = pat.DocumentRange
                    rec["text"] = rng.GetText(-1)
                    rects = rng.GetBoundingRectangles()
                    rec["rects"] = [(x.left, x.top, x.right, x.bottom) for x in rects]
            except Exception as ex:
                rec["err"] = "%s: %s" % (type(ex).__name__, ex)
            rows.append(rec)
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
    t.join(45)
    if t.is_alive():
        print("!! 超时")
        return
    if out.get("error"):
        print("!! %s" % out["error"])
        return

    c = out.get("container")
    print("消息列表容器 rect = %s" % (c,))
    if c:
        cx = (c[0] + c[2]) / 2.0
        print("容器中心 x = %.0f" % cx)
    print("=" * 78)

    for i, r in enumerate(out.get("rows", [])):
        print("--- 消息 %d: %r  [%s]" % (i + 1, r["name"], r["class"]))
        print("    行 rect      = %s" % (r["item_rect"],))
        if r["err"]:
            print("    TextPattern  = 失败: %s" % r["err"])
        else:
            print("    文本         = %r" % (r["text"],))
            print("    文字矩形     = %s" % (r["rects"],))
            if r["rects"] and c:
                for rc in r["rects"]:
                    mid = (rc[0] + rc[2]) / 2.0
                    side = "左侧(对方?)" if mid < (c[0] + c[2]) / 2.0 else "右侧(自己?)"
                    print("      -> 矩形中心 x=%.0f  相对容器中心 %s" % (mid, side))
        print("")

    here = os.path.dirname(os.path.abspath(__file__))
    outdir = os.path.join(os.path.dirname(here), "out")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "textpattern_%s.txt" % dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", encoding="utf-8") as f:
        f.write("container=%r\n\n" % (c,))
        for i, r in enumerate(out.get("rows", [])):
            f.write("--- 消息 %d ---\n%r\n" % (i + 1, r))
    print("[已保存] %s" % path)


if __name__ == "__main__":
    main()
