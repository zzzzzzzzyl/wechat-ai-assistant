# -*- coding: utf-8 -*-
"""
微信 4.0 聊天记录读取探针（基于已验证可行的 FindAll 方案）。

核心结论（已实测）：
    微信 4.0 主窗口是 Qt 5.15 应用，窗口类 Qt51514QWindowIcon，
    UIA 树里控件类是 mmui::*。Qt 的 UIA provider 只为自绘虚拟列表实现了
    FindAll，**没有**实现 TreeWalker 遍历，因此：
        x 用 GetChildren()/RawViewWalker 逐级走 -> 到会话列表/消息区就断掉
        √ 用 IUIAutomationElement::FindAll(TreeScope_Descendants, cond) -> 全都能拿到
    本脚本一律使用 FindAll。

只读性：仅调用 UIA 查询接口；不模拟键鼠、不点击、不发消息、不改微信任何状态。

用法：
    python probe_chat.py            # 输出会话列表 + 当前聊天消息
    python probe_chat.py --json out.json
"""

import argparse
import datetime as dt
import json
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

TARGET_PROC = "weixin.exe"
TIMEOUT = 60.0

# UIA PropertyId
P_CONTROLTYPE = 30003
P_NAME = 30005
P_AUTOMATIONID = 30011
P_CLASSNAME = 30012
P_BOUNDINGRECT = 30001
P_ISOFFSCREEN = 30022

# TreeScope
SCOPE_DESCENDANTS = 4

# ControlType ids
CT_TEXT = 50020
CT_LIST = 50008
CT_LISTITEM = 50007
CT_EDIT = 50004


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


def elem_to_dict(e):
    try:
        r = e.CurrentBoundingRectangle
        rect = (r.left, r.top, r.right, r.bottom)
    except Exception:
        rect = None
    return {
        "type": e.CurrentControlType,
        "name": (e.CurrentName or ""),
        "class": (e.CurrentClassName or ""),
        "id": (e.CurrentAutomationId or ""),
        "rect": rect,
        "offscreen": bool(e.CurrentIsOffscreen),
    }


def work(hwnd, result):
    import comtypes

    comtypes.CoInitialize()

    import uiautomation as auto
    from uiautomation import uiautomation as core

    with auto.UIAutomationInitializerInThread(debug=False):
        uia = core._AutomationClient.instance().IUIAutomation
        elem = auto.ControlFromHandle(hwnd)
        el = elem.Element
        true_cond = uia.CreateTrueCondition()

        # 一次性抓全窗口后代（这是唯一可靠的取法）
        arr = el.FindAll(SCOPE_DESCENDANTS, true_cond)
        allx = []
        for i in range(arr.Length):
            allx.append(elem_to_dict(arr.GetElement(i)))
        result["all"] = allx

        def by_id(sub):
            return [d for d in allx if sub in d["id"]]

        # ---- 会话列表项：mmui::ChatSessionCell，id=session_item_<会话名>
        sessions = []
        for d in allx:
            if d["class"] == "mmui::ChatSessionCell":
                raw = d["name"]
                parts = [p for p in raw.split("\n") if p.strip()]
                sessions.append(
                    {
                        "id": d["id"],
                        "title": parts[0] if parts else "",
                        "preview": parts[1] if len(parts) > 1 else "",
                        "time": parts[-1] if len(parts) > 2 else "",
                        "rect": d["rect"],
                    }
                )
        result["sessions"] = sessions

        # ---- 当前聊天标题
        title = ""
        for d in allx:
            if d["class"] == "mmui::XTextView" and "current_chat_name_label" in d["id"]:
                title = d["name"]
                break
        result["chat_title"] = title

        # ---- 消息列表容器
        msg_list = None
        for d in allx:
            if d["id"] == "chat_message_list":
                msg_list = d
                break
        result["msg_list"] = msg_list

        # ---- 消息项：位于 chat_message_list 子树内的气泡项
        msgs = []
        if msg_list:
            for d in allx:
                if d["id"].startswith("chat_message_list") and d["type"] == CT_LISTITEM:
                    msgs.append(d)
        result["messages"] = msgs

        # ---- 输入框内容（ValuePattern）
        inp = None
        for d in allx:
            if d["id"] == "chat_input_field":
                inp = d
                break
        result["input"] = inp
        if inp:
            try:
                # 用 FindAll 拿到的元素需要转回 Control 才能用 pattern
                cond = uia.CreatePropertyCondition(P_AUTOMATIONID, "chat_input_field")
                e2 = el.FindFirst(SCOPE_DESCENDANTS, cond)
                ctl = auto.Control.CreateControlFromElement(e2)
                vp = ctl.GetValuePattern()
                result["input_value"] = vp.Value if vp else None
            except Exception as ex:
                result["input_value"] = "读取失败: %s" % ex

        # ---- 顺带看看消息项下面还有没有更细的节点（发送者/头像线索）
        detail = []
        for d in allx:
            if d["id"].startswith("chat_message_list") and d["class"] != "mmui::RecyclerListView":
                detail.append(d)
        result["msg_subtree"] = detail


def _safe_work(hwnd, result):
    try:
        work(hwnd, result)
    except Exception as e:
        import traceback

        result["error"] = "%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()[-1200:])


CT_NAME = {}
try:
    from uiautomation import uiautomation as _c

    for _k, _v in vars(_c.ControlType).items():
        if not _k.startswith("_") and isinstance(_v, int):
            CT_NAME.setdefault(_v, _k)
except Exception:
    pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None, help="把原始结果写成 JSON")
    args = ap.parse_args()

    buf = []

    def rec(s=""):
        print(s, flush=True)
        buf.append(s)

    rec("=" * 78)
    rec("微信 4.0 聊天记录读取探针 (FindAll 方案)")
    rec("时间: %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    rec("=" * 78)

    hwnds = find_wechat_hwnds()
    if not hwnds:
        rec("!! 未找到微信主窗口（微信需已登录且主窗口可见）")
        return
    hwnd = hwnds[0]
    rec("微信主窗口 hwnd = %s" % hex(hwnd))

    result = {}
    t = threading.Thread(target=_safe_work, args=(hwnd, result), daemon=True)
    t.start()
    t.join(TIMEOUT)
    if t.is_alive():
        rec("!! 超时 %.0fs" % TIMEOUT)
        return
    if result.get("error"):
        rec("!! 错误: %s" % result["error"])
        return

    allx = result.get("all", [])
    rec("全窗口后代元素: %d 个" % len(allx))

    rec("")
    rec("-" * 78)
    rec("【当前聊天】标题: %r" % result.get("chat_title"))
    rec("-" * 78)
    ml = result.get("msg_list")
    if ml:
        rec("消息列表容器: class=%s id=%s rect=%s" % (ml["class"], ml["id"], ml["rect"]))
    else:
        rec("!! 未找到 chat_message_list（可能当前不在聊天界面）")

    msgs = result.get("messages", [])
    rec("")
    rec("消息项共 %d 条（RecyclerListView 虚拟列表，通常只有当前可见的）:" % len(msgs))
    if ml and ml["rect"]:
        cx = (ml["rect"][0] + ml["rect"][2]) / 2.0
    else:
        cx = None
    for i, m in enumerate(msgs):
        side = ""
        if cx is not None and m["rect"]:
            # 气泡靠左=对方，靠右=自己
            side = "对方" if m["rect"][0] < cx - 200 else ("自己" if m["rect"][2] > cx + 200 else "?")
        rec("  %2d. [%s] %-46s class=%-26s rect=%s" % (i + 1, side, repr(m["name"])[:46], m["class"], m["rect"]))

    inp = result.get("input")
    rec("")
    rec("输入框: %s  当前内容=%r" % (inp, result.get("input_value")))

    rec("")
    rec("-" * 78)
    rec("【会话列表】共 %d 个会话" % len(result.get("sessions", [])))
    rec("-" * 78)
    for s in result.get("sessions", []):
        rec("  %-32s | %-40s | %s" % (s["title"][:32], s["preview"][:40], s["time"]))

    rec("")
    rec("-" * 78)
    rec("【消息列表子树细节】(用于判断发送者/消息类型能否区分)")
    rec("-" * 78)
    for d in result.get("msg_subtree", [])[:40]:
        rec("  [%s] class=%-30s name=%r id=%s rect=%s"
            % (CT_NAME.get(d["type"], d["type"]), d["class"], d["name"][:40], d["id"][:60], d["rect"]))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        rec("")
        rec("[JSON 已保存] %s" % args.json)

    here = os.path.dirname(os.path.abspath(__file__))
    outdir = os.path.join(os.path.dirname(here), "out")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "chat_probe_%s.txt" % dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(buf))
    print("\n[已保存] %s" % path)


if __name__ == "__main__":
    main()
