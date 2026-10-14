# -*- coding: utf-8 -*-
"""
群聊探测 v2：微信 4.x 群聊里，UIA 能否给出「这条消息是谁发的」？

为什么这是关键问题：
    单聊只需区分「左=对方 / 右=自己」，像素分析已解决。
    但群聊左侧可能同时有多个人，像素只能说「是别人」，说不出「是谁」。
    微信群聊界面上，别人的气泡上方会显示昵称 —— 本脚本要确认这个昵称
    是否作为 UIA 元素暴露。

v2 改进：
    - 用「水平落在消息容器内 + 垂直重叠 > 50%」判定元素归属（v1 太宽松，
      把标题栏元素也算进来了）；
    - 直接检查消息项自身的子元素数量（FindAll Children），
      这是判断「昵称是否为消息项子元素」最直接的方法；
    - 输出全部写入 out/ 文件。

只读：仅 UIA 查询。

用法：python probe_group.py
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
SCOPE_DESCENDANTS = 4
SCOPE_CHILDREN = 2

CT = {
    50000: "Button", 50004: "Edit", 50006: "Image", 50007: "ListItem", 50008: "List",
    50020: "Text", 50025: "Custom", 50026: "Group", 50032: "Window", 50033: "Pane",
    50037: "TitleBar", 50021: "ToolBar", 50027: "Thumb",
}


def find_wechat_hwnd():
    best = []

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
                best.append(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return best[0] if best else None


def collect(hwnd, out):
    import comtypes

    comtypes.CoInitialize()
    import uiautomation as auto
    from uiautomation import uiautomation as core

    with auto.UIAutomationInitializerInThread(debug=False):
        uia = core._AutomationClient.instance().IUIAutomation
        el = auto.ControlFromHandle(hwnd).Element
        arr = el.FindAll(SCOPE_DESCENDANTS, uia.CreateTrueCondition())

        elems = []
        items = []  # (element, dict)
        for i in range(arr.Length):
            e = arr.GetElement(i)
            try:
                r = e.CurrentBoundingRectangle
                rect = (r.left, r.top, r.right, r.bottom)
            except Exception:
                rect = None
            d = {
                "ctype": e.CurrentControlType,
                "name": e.CurrentName or "",
                "cls": e.CurrentClassName or "",
                "id": e.CurrentAutomationId or "",
                "rect": rect,
            }
            elems.append(d)
            if d["id"].startswith("chat_message_list") and d["cls"].endswith("ItemView"):
                items.append((e, d))
        out["elems"] = elems

        # 对每个消息项，直接查它的直接子元素
        subs = []
        for e, d in items:
            try:
                ch = e.FindAll(SCOPE_CHILDREN, uia.CreateTrueCondition())
                lst = []
                for k in range(ch.Length):
                    c = ch.GetElement(k)
                    try:
                        r = c.CurrentBoundingRectangle
                        cr = (r.left, r.top, r.right, r.bottom)
                    except Exception:
                        cr = None
                    lst.append({
                        "ctype": c.CurrentControlType,
                        "name": c.CurrentName or "",
                        "cls": c.CurrentClassName or "",
                        "id": c.CurrentAutomationId or "",
                        "rect": cr,
                    })
                subs.append(lst)
            except Exception as ex:
                subs.append([{"error": str(ex)}])
        out["children"] = subs


def _safe(hwnd, out):
    try:
        collect(hwnd, out)
    except Exception as e:
        import traceback

        out["error"] = "%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()[-800:])


def v_overlap_ratio(a, b):
    top = max(a[1], b[1])
    bot = min(a[3], b[3])
    if bot <= top:
        return 0.0
    h = min(a[3] - a[1], b[3] - b[1])
    return (bot - top) / float(max(1, h))


def main():
    buf = []

    def rec(s=""):
        print(s, flush=True)
        buf.append(str(s))

    hwnd = find_wechat_hwnd()
    if not hwnd:
        rec("!! 未找到微信主窗口")
        return
    rec("=" * 88)
    rec("群聊发送者信息探测 v2")
    rec("时间: %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    rec("=" * 88)

    out = {}
    t = threading.Thread(target=_safe, args=(hwnd, out), daemon=True)
    t.start()
    t.join(45)
    if t.is_alive():
        rec("!! 超时")
        _save(buf)
        return
    if out.get("error"):
        rec("!! %s" % out["error"])
        _save(buf)
        return

    elems = out["elems"]
    rec("全窗口元素: %d 个" % len(elems))

    title = next((e["name"] for e in elems if "current_chat_name_label" in e["id"]), "")
    rec("当前聊天标题: %r" % title)

    tb = sorted({e["cls"] for e in elems if "ChatTitleBar" in e["cls"]})
    rec("标题栏控件类: %s" % tb)
    if any("ChatRoom" in c for c in tb):
        rec("=> 判定：当前是【群聊】")
    elif any("ChatSingle" in c for c in tb):
        rec("=> 判定：当前是【单聊】")
    else:
        rec("=> 判定：无法确定（类名未匹配）")

    # 标题栏里的群名/成员数
    for e in elems:
        if e["cls"] == "mmui::XTextView" and e["name"] and e["rect"] and e["rect"][1] < 490:
            rec("  标题栏文本: %r rect=%s" % (e["name"], e["rect"]))

    ml = next((e for e in elems if e["id"] == "chat_message_list"), None)
    rec("消息容器: %s" % (("rect=%s" % (ml["rect"],)) if ml else "未找到"))
    lines = [e for e in elems
             if e["id"].startswith("chat_message_list") and e["cls"].endswith("ItemView")]
    rec("消息条数: %d" % len(lines))
    if not lines:
        rec("!! 当前没有可见消息")
        _save(buf)
        return

    crect = ml["rect"] if ml else None
    line_id_set = {id(l) for l in lines}

    rec("")
    rec("=" * 88)
    rec("【关键】每条消息的直接子元素（FindAll Children）")
    rec("=" * 88)
    for i, (l, ch) in enumerate(zip(lines, out["children"]), 1):
        rec("")
        rec("消息 %d  [%s]" % (i, l["cls"].replace("mmui::", "")))
        rec("   内容   : %r" % l["name"][:70])
        rec("   行 rect: %s" % (l["rect"],))
        if crect:
            inside = crect[1] <= l["rect"][1] and l["rect"][3] <= crect[3] if l["rect"] else False
            rec("   在容器内: %s %s" % (inside, "" if inside else "(注意：rect 超出容器，虚拟列表滚动导致)"))
        if not ch:
            rec("   子元素 : 0 个")
        else:
            rec("   子元素 : %d 个" % len(ch))
            for c in ch:
                if "error" in c:
                    rec("      <错误 %s>" % c["error"])
                else:
                    rec("      [%-8s] name=%-26r cls=%-26s rect=%s" % (
                        CT.get(c["ctype"], str(c["ctype"])), c["name"][:26],
                        c["cls"].replace("mmui::", ""), c["rect"]))

    rec("")
    rec("=" * 88)
    rec("【关键】每条消息行内、但属于其它元素的节点（昵称可能在这里）")
    rec("=" * 88)
    for i, l in enumerate(lines, 1):
        if not l["rect"]:
            continue
        others = []
        for e in elems:
            if id(e) in line_id_set or e["id"].startswith("chat_message_list"):
                continue
            if not e["rect"]:
                continue
            # 水平必须落在消息容器内（排除左侧会话列表、右侧标题栏按钮）
            if crect and (e["rect"][0] < crect[0] - 4 or e["rect"][2] > crect[2] + 4):
                continue
            # 且不能用比容器还宽的元素
            if crect and (e["rect"][2] - e["rect"][0]) > (crect[2] - crect[0]) * 0.98:
                continue
            if v_overlap_ratio(e["rect"], l["rect"]) > 0.5:
                others.append(e)
        rec("")
        rec("消息 %d %r  行 rect=%s" % (i, l["name"][:40], l["rect"]))
        named = [o for o in others if o["name"]]
        if not named:
            rec("   行内带名字的元素: 无   <== 没有昵称暴露")
        else:
            rec("   行内带名字的元素 (%d):" % len(named))
            for o in named:
                rec("      [%-8s] name=%-30r cls=%s rect=%s" % (
                    CT.get(o["ctype"], str(o["ctype"])), o["name"][:30],
                    o["cls"].replace("mmui::", ""), o["rect"]))

    rec("")
    rec("=" * 88)
    rec("全窗口所有 Text 控件（含 name 的）")
    rec("=" * 88)
    for e in elems:
        if e["ctype"] == 50020 and e["name"]:
            rec("  %-40r cls=%-24s rect=%s" % (e["name"][:40], e["cls"].replace("mmui::", ""), e["rect"]))

    _save(buf)


def _save(buf):
    here = os.path.dirname(os.path.abspath(__file__))
    outdir = os.path.join(os.path.dirname(here), "out")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "group_%s.txt" % dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(buf))
    print("\n[已保存] %s" % path, flush=True)


if __name__ == "__main__":
    main()
