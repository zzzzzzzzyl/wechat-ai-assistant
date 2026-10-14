# -*- coding: utf-8 -*-
"""
UIA 能力总览探测：一次跑完，列出「微信 4.0 上到底能从 UIA 拿到哪些信息」。

与 probe_chat.py 的区别：
    probe_chat.py 只抓消息和会话；
    本脚本给出**完整能力清单**，包括导航栏、未读角标、输入框、消息类型分布、
    元素耗时统计，并复测「最小化/隐藏后是否仍可读」。

只读性：全部为 UIA 查询 + 窗口显示状态切换（会立刻恢复），
        不发消息、不点击、不输入、不改任何微信数据。

用法：
    python probe_capability.py            # 完整探测（含后台可读性复测）
    python probe_capability.py --no-bg    # 跳过最小化/隐藏测试，不打扰微信窗口
"""

import argparse
import ctypes
import datetime as dt
import os
import sys
import threading
import time

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
TIMEOUT = 45.0
SCOPE_DESCENDANTS = 4

# ControlType 取值 -> 可读名
CT = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox",
    50004: "Edit", 50005: "Hyperlink", 50006: "Image", 50007: "ListItem",
    50008: "List", 50009: "Menu", 50010: "MenuBar", 50011: "MenuItem",
    50012: "ProgressBar", 50013: "RadioButton", 50014: "ScrollBar",
    50015: "Slider", 50016: "Spinner", 50017: "StatusBar", 50018: "Tab",
    50019: "TabItem", 50020: "Text", 50021: "ToolBar", 50022: "ToolTip",
    50023: "Tree", 50024: "TreeItem", 50025: "Custom", 50026: "Group",
    50027: "Thumb", 50028: "DataGrid", 50029: "DataItem", 50030: "Document",
    50031: "SplitButton", 50032: "Window", 50033: "Pane", 50034: "Header",
    50035: "HeaderItem", 50036: "Table", 50037: "TitleBar", 50038: "Separator",
}


def find_wechat_hwnd():
    """用 Win32 枚举定位微信主窗口（禁止 GetRootControl().GetChildren()）。"""
    best = []

    def cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            cls = win32gui.GetClassName(hwnd)
            if "QWindowIcon" not in cls:
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            h = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            try:
                path = win32process.GetModuleFileNameEx(h, 0)
            finally:
                win32api.CloseHandle(h)
            if os.path.basename(path).lower() == TARGET_PROC:
                best.append((hwnd, pid, path, cls))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return best[0] if best else None


def collect(hwnd, out):
    """在子线程里做全部 UIA 查询。"""
    import comtypes

    comtypes.CoInitialize()
    import uiautomation as auto
    from uiautomation import uiautomation as core

    with auto.UIAutomationInitializerInThread(debug=False):
        t0 = time.time()
        uia = core._AutomationClient.instance().IUIAutomation
        el = auto.ControlFromHandle(hwnd).Element
        arr = el.FindAll(SCOPE_DESCENDANTS, uia.CreateTrueCondition())
        n = arr.Length
        out["findall_ms"] = (time.time() - t0) * 1000
        out["total"] = n

        t1 = time.time()
        elems = []
        for i in range(n):
            e = arr.GetElement(i)
            try:
                r = e.CurrentBoundingRectangle
                rect = (r.left, r.top, r.right, r.bottom)
            except Exception:
                rect = None
            elems.append(
                {
                    "ctype": e.CurrentControlType,
                    "name": e.CurrentName or "",
                    "cls": e.CurrentClassName or "",
                    "id": e.CurrentAutomationId or "",
                    "rect": rect,
                    "off": bool(e.CurrentIsOffscreen),
                }
            )
        out["read_ms"] = (time.time() - t1) * 1000
        out["elems"] = elems


def _safe_collect(hwnd, out):
    try:
        collect(hwnd, out)
    except Exception as e:
        import traceback

        out["error"] = "%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()[-900:])


def snapshot(hwnd, timeout=TIMEOUT):
    out = {}
    t = threading.Thread(target=_safe_collect, args=(hwnd, out), daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None, "UIA 调用超时 %.0fs" % timeout
    if out.get("error"):
        return None, out["error"]
    return out, None


def parse_session(name):
    """会话项 Name 形如 '标题\\n预览\\n时间\\n'，可能还带 '消息免打扰' 等标记。"""
    parts = [p.strip() for p in name.split("\n") if p.strip()]
    if not parts:
        return {"title": "", "preview": "", "time": "", "flags": []}
    title = parts[0]
    rest = parts[1:]
    flags = []
    for tail in ["消息免打扰", "草稿", "未读"]:
        while rest and rest[-1] == tail:
            flags.append(rest.pop())
    t = rest[-1] if rest else ""
    preview = " / ".join(rest[:-1]) if len(rest) > 1 else (rest[0] if rest else "")
    if rest and t == preview:
        preview = ""
    return {"title": title, "preview": preview, "time": t, "flags": flags}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-bg", action="store_true", help="跳过最小化/隐藏复测")
    args = ap.parse_args()

    buf = []

    def rec(s=""):
        print(s, flush=True)
        buf.append(str(s))

    rec("=" * 80)
    rec("微信 UIA 能力总览探测")
    rec("时间: %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    rec("=" * 80)

    info = find_wechat_hwnd()
    if not info:
        rec("!! 未找到微信主窗口（需已登录、主窗口存在）")
        return
    hwnd, pid, path, cls = info

    # ---------- 1. 窗口信息 ----------
    r = win32gui.GetWindowRect(hwnd)
    rec("")
    rec("【1】窗口信息")
    rec("  进程路径   : %s" % path)
    rec("  PID        : %d" % pid)
    rec("  窗口句柄   : %s" % hex(hwnd))
    rec("  窗口类名   : %s" % cls)
    rec("  窗口标题   : %r" % win32gui.GetWindowText(hwnd))
    rec("  窗口矩形   : %s  尺寸 %dx%d" % (r, r[2] - r[0], r[3] - r[1]))
    rec("  可见/最小化: %s / %s" % (bool(win32gui.IsWindowVisible(hwnd)), bool(win32gui.IsIconic(hwnd))))
    try:
        v = win32api.GetFileVersionInfo(path, "\\")
        ms, ls = v["FileVersionMS"], v["FileVersionLS"]
        rec("  文件版本   : %d.%d.%d.%d" % (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF))
    except Exception:
        pass

    # ---------- 2. UIA 总览 ----------
    snap, err = snapshot(hwnd)
    if err:
        rec("")
        rec("!! UIA 探测失败: %s" % err)
        return
    elems = snap["elems"]
    rec("")
    rec("【2】UIA 总览")
    rec("  FindAll(Descendants) 耗时 : %.0f ms" % snap["findall_ms"])
    rec("  读取 %d 个元素属性耗时     : %.0f ms" % (len(elems), snap["read_ms"]))
    rec("  元素总数                  : %d" % snap["total"])
    dist = {}
    for e in elems:
        k = CT.get(e["ctype"], str(e["ctype"]))
        dist[k] = dist.get(k, 0) + 1
    rec("  控件类型分布:")
    for k in sorted(dist, key=lambda x: -dist[x]):
        rec("     %-14s %d" % (k, dist[k]))

    # ---------- 3. 导航栏 ----------
    rec("")
    rec("【3】导航栏 / 未读角标")
    for e in elems:
        if e["cls"] == "mmui::XTabBarItem":
            rec("  标签 %-8r rect=%s" % (e["name"], e["rect"]))
        elif e["cls"] == "mmui::XBadge":
            rec("  角标(class=XBadge) rect=%s" % (e["rect"],))
        elif e["cls"] == "mmui::MainTabBarSettingView":
            rec("  设置按钮 %r" % e["name"])

    # ---------- 4. 会话列表 ----------
    sessions = [e for e in elems if e["cls"] == "mmui::ChatSessionCell"]
    rec("")
    rec("【4】会话列表（%d 个，仅当前可见区域）" % len(sessions))
    for i, e in enumerate(sessions, 1):
        p = parse_session(e["name"])
        rec("  %2d. %-24s | %-38s | %-10s %s" % (
            i, p["title"][:24], p["preview"][:38], p["time"], ("[" + ",".join(p["flags"]) + "]") if p["flags"] else ""))
        rec("      id=%s" % e["id"])
    rec("  容器: %s" % next((e["id"] for e in elems if e["id"] == "session_list"), "session_list 未找到"))

    # ---------- 5. 当前聊天 ----------
    rec("")
    title = ""
    for e in elems:
        if e["cls"] == "mmui::XTextView" and "current_chat_name_label" in e["id"]:
            title = e["name"]
            break
    rec("【5】当前聊天")
    rec("  聊天标题 : %r" % title)
    ml = next((e for e in elems if e["id"] == "chat_message_list"), None)
    rec("  消息容器 : %s" % (("class=%s id=%s rect=%s" % (ml["cls"], ml["id"], ml["rect"])) if ml else "未找到"))
    if not ml:
        rec("  （当前可能不在聊天界面）")

    msgs = [e for e in elems
            if e["id"].startswith("chat_message_list") and e["cls"].endswith("ItemView")]
    rec("  消息条数 : %d（RecyclerListView 虚拟列表，只有当前渲染出来的）" % len(msgs))
    rec("")
    rec("  %-4s %-30s %-30s %-22s %s" % ("#", "内容", "控件类", "类型判定", "rect"))
    for i, e in enumerate(msgs, 1):
        cls_short = e["cls"].replace("mmui::", "")
        if "TextItemView" in cls_short:
            kind = "文本"
        elif "BubbleReferItemView" in cls_short:
            kind = "图片/表情/引用"
        elif "Voice" in cls_short or "Audio" in cls_short:
            kind = "语音"
        elif "File" in cls_short:
            kind = "文件"
        else:
            kind = cls_short
        rec("  %-4d %-30s %-30s %-22s %s" % (
            i, repr(e["name"])[:30], cls_short, kind, e["rect"]))

    # 消息高度分布（可用来推测气泡 vs 系统提示）
    rec("")
    if msgs and ml and ml["rect"]:
        rec("  消息行高度: %s" % [ (m["rect"][3] - m["rect"][1]) if m["rect"] else None for m in msgs ])
        rec("  容器宽度  : %d（每行 rect 宽度 = %s，说明 UIA 只给整行，不给气泡位置）" % (
            ml["rect"][2] - ml["rect"][0],
            sorted({ (m["rect"][2] - m["rect"][0]) for m in msgs if m["rect"] }),
        ))

    # ---------- 6. 输入区 ----------
    rec("")
    rec("【6】输入区 / 工具按钮")
    for e in elems:
        if e["id"] == "chat_input_field":
            rec("  输入框: class=%s rect=%s 当前内容=%r" % (e["cls"], e["rect"], e["name"]))
    btns = [e for e in elems if e["cls"] == "mmui::XButton" and e["name"]]
    rec("  可读按钮(%d): %s" % (len(btns), ", ".join(repr(b["name"]) for b in btns[:14])))

    # ---------- 7. 其它有价值的文本 ----------
    rec("")
    rec("【7】其它可读文本节点 (TextControl)")
    for e in elems:
        if e["ctype"] == 50020 and e["name"]:
            rec("  %-40r id=%s" % (e["name"][:40], e["id"][:50]))

    # ---------- 8. 后台可读性 ----------
    rec("")
    rec("【8】后台可读性复测")
    if args.no_bg:
        rec("  （已用 --no-bg 跳过）")
    else:

        def count_key():
            s, e2 = snapshot(hwnd, timeout=20)
            if e2:
                return e2
            el2 = s["elems"]
            return "%d 元素 / %d 会话 / %d 消息" % (
                s["total"],
                len([x for x in el2 if x["cls"] == "mmui::ChatSessionCell"]),
                len([x for x in el2 if x["id"].startswith("chat_message_list") and x["cls"].endswith("ItemView")]),
            )

        rec("  可见(当前)     : %s" % count_key())
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
            time.sleep(1.2)
            rec("  最小化         : %s" % count_key())
            win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            time.sleep(1.0)
            rec("  完全隐藏       : %s" % count_key())
        finally:
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.6)
                rec("  已恢复         : %s" % count_key())
            except Exception as e:
                rec("  !! 恢复失败: %s" % e)

    here = os.path.dirname(os.path.abspath(__file__))
    outdir = os.path.join(os.path.dirname(here), "out")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "capability_%s.txt" % dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(buf))
    rec("")
    rec("[已保存] %s" % path)


if __name__ == "__main__":
    main()
