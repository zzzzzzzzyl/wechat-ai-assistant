# -*- coding: utf-8 -*-
"""
轮询性能与稳定性测试：连续多次读取，验证「能否做实时监听」。

要回答的问题：
    如果要每 1~2 秒检查一次「有没有新消息」，UIA 读取够不够快、稳不稳？
    会不会越读越慢（内存/句柄泄漏）、会不会某次突然卡住或返回空？

做法：
    连续 N 次执行 FindAll + 解析会话/消息，记录每次的耗时与内容指纹，
    并测一次输入框的 ValuePattern 读取（这是拿「用户正在输入什么」的正确方法）。

只读：全部为 UIA 查询。

用法：python probe_poll.py [次数，默认8] [间隔秒，默认1.0]
"""

import argparse
import ctypes
import datetime as dt
import hashlib
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
SCOPE_DESCENDANTS = 4


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


def read_once(hwnd):
    """一次完整读取，返回统计信息。"""
    import comtypes

    comtypes.CoInitialize()
    import uiautomation as auto
    from uiautomation import uiautomation as core

    with auto.UIAutomationInitializerInThread(debug=False):
        uia = core._AutomationClient.instance().IUIAutomation
        el = auto.ControlFromHandle(hwnd).Element

        t0 = time.time()
        arr = el.FindAll(SCOPE_DESCENDANTS, uia.CreateTrueCondition())
        n = arr.Length
        sessions, msgs, chat_title, input_val = [], [], "", None
        for i in range(n):
            e = arr.GetElement(i)
            cls = e.CurrentClassName or ""
            aid = e.CurrentAutomationId or ""
            if cls == "mmui::ChatSessionCell":
                sessions.append((e.CurrentName or "").replace("\n", " | ").strip(" |"))
            elif aid.startswith("chat_message_list") and cls.endswith("ItemView"):
                msgs.append(e.CurrentName or "")
            elif "current_chat_name_label" in aid:
                chat_title = e.CurrentName or ""
            elif aid == "chat_input_field":
                try:
                    ctl = auto.Control.CreateControlFromElement(e)
                    vp = ctl.GetPattern(core.PatternId.ValuePattern)
                    input_val = vp.Value if vp else None
                except Exception:
                    input_val = "<读取失败>"
        elapsed = (time.time() - t0) * 1000

        fp_src = "|".join(sessions) + "#" + "|".join(msgs)
        fp = hashlib.md5(fp_src.encode("utf-8")).hexdigest()[:10]
        return {
            "ms": elapsed,
            "total": n,
            "sessions": sessions,
            "msgs": msgs,
            "title": chat_title,
            "input": input_val,
            "fp": fp,
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rounds", nargs="?", type=int, default=8)
    ap.add_argument("interval", nargs="?", type=float, default=1.0)
    args = ap.parse_args()

    hwnd = find_wechat_hwnd()
    if not hwnd:
        print("!! 未找到微信主窗口")
        return
    print("=" * 78)
    print("UIA 轮询性能与稳定性测试")
    print("微信 hwnd = %s   轮数 = %d   间隔 = %.1fs" % (hex(hwnd), args.rounds, args.interval))
    print("=" * 78)
    print("%-5s %-10s %-8s %-8s %-8s %-12s %s" % ("轮次", "耗时(ms)", "元素数", "会话数", "消息数", "内容指纹", "当前聊天/输入框"))
    print("-" * 78)

    rows = []
    for i in range(1, args.rounds + 1):
        try:
            r = read_once(hwnd)
        except Exception as e:
            print("%-5d 失败: %s: %s" % (i, type(e).__name__, e))
            break
        rows.append(r)
        print("%-5d %-10.0f %-8d %-8d %-8d %-12s %s / %r" % (
            i, r["ms"], r["total"], len(r["sessions"]), len(r["msgs"]),
            r["fp"], r["title"], r["input"]))
        if i < args.rounds:
            time.sleep(args.interval)

    if not rows:
        return
    ms = [x["ms"] for x in rows]
    print("-" * 78)
    print("耗时: 最小 %.0f ms / 平均 %.0f ms / 最大 %.0f ms" % (min(ms), sum(ms) / len(ms), max(ms)))
    fps = {x["fp"] for x in rows}
    print("内容指纹变化次数: %d %s" % (len(fps) - 1, "(期间内容没变，读取稳定)" if len(fps) == 1 else "(期间有新消息或界面变化)"))
    print("")
    print("判读：")
    print("  - 平均耗时 < 300ms  =>  1 秒轮询一次毫无压力，可做实时监听")
    print("  - 耗时没有随轮次明显上升 => 无泄漏，可长时间运行")
    print("  - 输入框读数来自 ValuePattern，是「你此刻正在输入的内容」")


if __name__ == "__main__":
    main()
