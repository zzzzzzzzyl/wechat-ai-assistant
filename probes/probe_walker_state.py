# -*- coding: utf-8 -*-
"""walker（GetChildren 递归）到底能不能走通？—— 一个对照实验。

背景：2026-09-23 的 uia_dump 里，用 GetChildren 递归只看到 78 个元素，
`session_list` 和消息列表下面**空无一物**，据此写下了「必须用 FindAll」的结论。
但 2026-09-25 复测时，同一个 walker 递归拿到了 159 个元素，和 FindAll 一样多。

两种可能：
  A. 结论依赖进程状态：树要先被 FindAll「物化」，之后 walker 才能走到；
  B. 结论依赖别的因素（窗口状态、版本、上一次探测的副作用）。

于是本脚本在**一个全新的进程**里按固定顺序测三遍：
  ① 冷态 walker 递归（此前**没有任何** FindAll 调用）
  ② FindAll(Descendants)
  ③ 再做一次 walker 递归
比较三次的元素数与「消息条目能不能走到」。

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

TARGET_PROC = "weixin.exe"
TIMEOUT = 60.0


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
            if os.path.basename(path).lower() == TARGET_PROC:
                found.append(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return found[0] if found else None


def walk_walker(ctrl):
    """走 walker：逐层 GetChildren 递归，返回 (元素数, 命中的消息正文字符串列表)。"""
    n = 0
    msgs = []

    def rec(c, depth=0):
        nonlocal n
        if depth > 30:
            return
        for k in c.GetChildren():
            n += 1
            try:
                cls = k.ClassName or ""
                nm = k.Name or ""
                if cls.endswith("ItemView") and nm.strip():
                    msgs.append(nm)
            except Exception:
                pass
            rec(k, depth + 1)

    # 注意：这里**不使用** AutomationId 前缀去筛，只靠类名，
    # 因为要回答的是「walk 不 walk 得到」，不是「筛得准不准」。
    rec(ctrl)
    return n, msgs


def walk_findall(el, uia, session_list_id="session_list"):
    """走 FindAll，同样返回 (元素数, 消息正文列表)。"""
    arr = el.FindAll(4, uia.CreateTrueCondition())
    msgs = []
    n = arr.Length
    for i in range(n):
        e = arr.GetElement(i)
        try:
            cls = e.CurrentClassName or ""
            nm = e.CurrentName or ""
            if cls.endswith("ItemView") and nm.strip():
                msgs.append(nm)
        except Exception:
            pass
    return n, msgs


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
        ctrl = auto.ControlFromHandle(hwnd)
        el = ctrl.Element

        # ① 冷态 walker —— 此时进程内还没发生过任何 FindAll
        n1, m1 = walk_walker(ctrl)
        out["cold_walker_count"] = n1
        out["cold_walker_msgs"] = m1

        # ② FindAll
        n2, m2 = walk_findall(el, uia)
        out["findall_count"] = n2
        out["findall_msgs"] = m2

        # ③ 再走一次 walker
        n3, m3 = walk_walker(ctrl)
        out["warm_walker_count"] = n3
        out["warm_walker_msgs"] = m3

        # ④ 直接看关键容器有没有子节点
        def find_by_class(c, cls_name, depth=0):
            if depth > 30:
                return None
            try:
                if c.ClassName == cls_name:
                    return c
                for k in c.GetChildren():
                    r = find_by_class(k, cls_name, depth + 1)
                    if r:
                        return r
            except Exception:
                pass
            return None

        for name in ("mmui::XTableView", "mmui::ChatMessagePage"):
            node = find_by_class(ctrl, name)
            if node is None:
                out.setdefault("containers", {})[name] = "walker 走不到该节点"
            else:
                try:
                    out.setdefault("containers", {})[name] = \
                        "walker 可达，子节点 %d 个" % len(node.GetChildren())
                except Exception as e:
                    out["containers"][name] = "读子节点失败: %s" % e


def main():
    # --minimize: 先把微信最小化再测，用于验证「78 vs 170」是否是窗口状态造成的。
    # 测完立刻按原位置还原（SW_RESTORE + SetWindowPos）。
    do_min = "--minimize" in sys.argv

    hwnd0 = find_hwnd()
    restore_info = None
    if do_min and hwnd0:
        restore_info = win32gui.GetWindowPlacement(hwnd0)
        win32gui.ShowWindow(hwnd0, win32con.SW_MINIMIZE)

    import time
    if do_min:
        time.sleep(0.8)

    out = {}
    t = threading.Thread(target=_safe, args=(out,), daemon=True)
    t.start()
    t.join(TIMEOUT)

    if do_min and hwnd0:
        try:
            # 还原：先 SW_RESTORE，再把位置摆回原处
            win32gui.ShowWindow(hwnd0, win32con.SW_RESTORE)
            win32gui.SetWindowPos(
                hwnd0, 0,
                restore_info[4][0], restore_info[4][1],
                restore_info[4][2] - restore_info[4][0],
                restore_info[4][3] - restore_info[4][1],
                win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
            time.sleep(0.3)
        except Exception as e:
            print("[警告] 还原窗口失败: %s" % e)

    print("=" * 78)
    print("walker vs FindAll 对照实验%s" % ("（窗口已最小化）" if do_min else "（窗口正常显示）"))
    print("时间: %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 78)
    if t.is_alive():
        print("超时 %.0fs" % TIMEOUT)
        return
    if out.get("err"):
        print("错误: %s" % out["err"])
        return

    print("微信 hwnd: %s" % hex(out["hwnd"]))
    print("")
    print("① 冷态 walker 递归（此前无 FindAll）: %s 个元素"
          % out.get("cold_walker_count"))
    print("   走到的消息条目: %s" % (out.get("cold_walker_msgs") or "（一条都没有）"))
    print("")
    print("② FindAll(Descendants)          : %s 个元素"
          % out.get("findall_count"))
    print("   走到的消息条目: %s" % (out.get("findall_msgs") or "（一条都没有）"))
    print("")
    print("③ FindAll 之后再走 walker       : %s 个元素"
          % out.get("warm_walker_count"))
    print("   走到的消息条目: %s" % (out.get("warm_walker_msgs") or "（一条都没有）"))
    print("")
    print("④ 关键容器可达性:")
    for k, v in (out.get("containers") or {}).items():
        print("   %-28s %s" % (k, v))


def _safe(out):
    try:
        work(out)
    except Exception as e:
        import traceback
        out["err"] = "%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()[-500:])


if __name__ == "__main__":
    main()
