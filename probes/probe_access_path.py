# -*- coding: utf-8 -*-
"""验证「微信 4.x 必须开着讲述人才有 UIA 树」这一说法。

要逐条判定的是：
  1. 不开讲述人（无任何屏幕阅读器进程）时，微信主窗口的 UIA 树到底有多少元素、
     能不能读到聊天消息正文；
  2. 微信主窗口下到底有没有原生子窗口 HWND，`MMUIRenderSubWindowHW` 是否真的存在；
  3. 如果**挂到子窗口**上去查 UIA，会看到什么（用来解释「只有外壳」是怎么来的）；
  4. 用 GetChildren（walker）递归 vs FindAll(Descendants) 各能看到多少元素。

结论只在**本机实测**范围内有效。纯只读：只调用查询接口，不碰微信任何状态。
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
TIMEOUT = 40.0


# ------------------------------------------------------------------ 环境侦查

def screen_readers_running():
    """列出可能触发微信无障碍后端的屏幕阅读器进程。"""
    names = ("narrator", "nvda", "jaws", "voiceaccess", "magnify", "osk",
             "uiaccess", "speech")
    hits = []
    try:
        import subprocess
        out = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"],
            capture_output=True, text=True, errors="replace",
        ).stdout or ""
        for line in out.splitlines():
            low = line.lower()
            for n in names:
                if low.startswith('"%s' % n) or ",%s.exe" % n in low:
                    hits.append(line.strip())
    except Exception as e:
        hits.append("检测失败: %s" % e)
    return hits


def find_wechat_windows():
    """EnumWindows 找微信顶层窗口（不用 GetRootControl，见项目约定）。"""
    found = []

    def cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            cls = win32gui.GetClassName(hwnd)
            if "QWindowIcon" not in cls:
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            h = win32api.OpenProcess(
                win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            try:
                path = win32process.GetModuleFileNameEx(h, 0)
            finally:
                win32api.CloseHandle(h)
            if os.path.basename(path).lower() == TARGET_PROC:
                found.append((hwnd, pid))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return found


def child_windows(hwnd):
    """枚举全部子孙窗口（原生 HWND 层面）。"""
    kids = []

    def cb(h, _):
        try:
            kids.append((h, win32gui.GetClassName(h), win32gui.GetWindowText(h),
                         win32gui.GetWindowRect(h)))
        except Exception:
            pass
        return True

    win32gui.EnumChildWindows(hwnd, cb, None)
    return kids


# ------------------------------------------------------------------ UIA 查询

def uia_probe(hwnd, out):
    """在**可能卡死**的 provider 上做查询，所以必须跑在子线程里。"""
    import comtypes
    comtypes.CoInitialize()

    import uiautomation as auto
    from uiautomation import uiautomation as core

    with auto.UIAutomationInitializerInThread(debug=False):
        uia = core._AutomationClient.instance().IUIAutomation
        ctrl = auto.ControlFromHandle(hwnd)
        if ctrl is None:
            out["err"] = "ControlFromHandle 返回 None"
            return
        el = ctrl.Element

        # ① 顶层直接子节点（walker 视角 = "外壳"有多少）
        try:
            top = ctrl.GetChildren()
            out["direct_children"] = len(top)
            out["direct_children_names"] = [
                "%s|%s" % (c.ClassName, (c.Name or "")[:40]) for c in top[:20]
            ]
        except Exception as e:
            out["direct_children"] = "失败: %s" % e

        # ② GetChildren 递归全树（walker 视角）
        try:
            n = [0]

            def rec(c, depth=0):
                if depth > 25:
                    return
                for k in c.GetChildren():
                    n[0] += 1
                    rec(k, depth + 1)

            rec(ctrl)
            out["walker_total"] = n[0]
        except Exception as e:
            out["walker_total"] = "失败: %s" % e

        # ③ FindAll(Descendants)（本项目用的取法）
        try:
            arr = el.FindAll(4, uia.CreateTrueCondition())
            out["findall_total"] = arr.Length
            named = []
            msgs = []
            for i in range(arr.Length):
                e = arr.GetElement(i)
                nm = e.CurrentName or ""
                cls = e.CurrentClassName or ""
                aid = e.CurrentAutomationId or ""
                if nm.strip():
                    named.append((cls, aid, nm))
                if aid.startswith("chat_message_list") and cls.endswith("ItemView"):
                    msgs.append((cls, nm))
            out["named"] = named
            out["messages"] = msgs
        except Exception as e:
            out["findall_total"] = "失败: %s" % e


def run_uia(hwnd):
    out = {}
    t = threading.Thread(target=_safe, args=(hwnd, out), daemon=True)
    t.start()
    t.join(TIMEOUT)
    if t.is_alive():
        return {"err": "超时 %.0fs（provider 无响应）" % TIMEOUT}
    return out


def _safe(hwnd, out):
    try:
        uia_probe(hwnd, out)
    except Exception as e:
        import traceback
        out["err"] = "%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()[-600:])


# ------------------------------------------------------------------ 主流程

def main():
    buf = []

    def rec(s=""):
        print(s, flush=True)
        buf.append(s)

    rec("=" * 78)
    rec("微信 UIA 访问路径验证（讲述人假设 / 子窗口 HWND / walker 与 FindAll）")
    rec("时间: %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    rec("=" * 78)

    rec("")
    rec("【0】屏幕阅读器进程检测")
    readers = screen_readers_running()
    if readers:
        for r in readers:
            rec("   发现: %s" % r)
    else:
        rec("   没有任何讲述人 / NVDA / JAWS 等屏幕阅读器进程在运行")

    wins = find_wechat_windows()
    rec("")
    rec("【1】微信顶层窗口（类名含 QWindowIcon 且进程为 %s）: %s"
        % (TARGET_PROC, [hex(h) for h, _ in wins]))

    for hwnd, pid in wins:
        rec("")
        rec("=" * 78)
        rec("顶层 hwnd=%s  pid=%s  类名=%s"
            % (hex(hwnd), pid, win32gui.GetClassName(hwnd)))
        rec("顶层窗口 rect=%s" % (win32gui.GetWindowRect(hwnd),))

        kids = child_windows(hwnd)
        rec("")
        rec("【2】原生子窗口 HWND 共 %d 个:" % len(kids))
        if not kids:
            rec("   （没有子窗口 —— 说明界面确实不是用传统子控件拼的）")
        for h, cls, txt, rect in kids:
            rec("   hwnd=%-10s class=%-28s text=%r rect=%s"
                % (hex(h), cls, txt[:30], rect))

        names = [c for _, c, _, _ in kids]
        rec("")
        rec("   其中类名含 MMUIRender 的: %s"
            % ([n for n in names if "MMUI" in n.upper()] or "无"))

        # ---- 顶层窗口走本项目取法
        rec("")
        rec("【3】挂到**顶层窗口** hwnd=%s 查 UIA" % hex(hwnd))
        out = run_uia(hwnd)
        _dump(rec, out)

        # ---- 逐个挂到子窗口上试（这是「只有外壳」的可疑来源）
        if kids:
            rec("")
            rec("【4】挂到**各个子窗口**上查 UIA（对比用）")
            for h, cls, _txt, _rect in kids[:8]:
                sub = run_uia(h)
                rec("   --- 子窗口 %s class=%s" % (hex(h), cls))
                rec("       FindAll(Descendants) 元素数: %s"
                    % sub.get("findall_total", sub.get("err", "?")))
                rec("       非空 Name 元素数: %s"
                    % (len(sub.get("named") or []) if "named" in sub else sub.get("err", "?")))
                if sub.get("messages"):
                    rec("       读到的消息: %s" % sub["messages"])

    here = os.path.dirname(os.path.abspath(__file__))
    outdir = os.path.join(os.path.dirname(here), "out")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "access_path_%s.txt"
                        % dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(buf))
    print("\n[已保存] %s" % path)


def _dump(rec, out):
    rec("   直接子节点数（walker 视角的外壳）: %s" % out.get("direct_children"))
    for n in (out.get("direct_children_names") or [])[:10]:
        rec("      %s" % n)
    rec("   GetChildren 递归全树元素数: %s" % out.get("walker_total"))
    rec("   FindAll(Descendants) 元素数: %s" % out.get("findall_total"))
    named = out.get("named") or []
    rec("   非空 Name 元素数: %d" % len(named))
    for cls, aid, nm in named[:15]:
        rec("      class=%-32s id=%-22s name=%r" % (cls, aid[:22], nm[:60]))
    msgs = out.get("messages") or []
    rec("   其中消息条目数: %d" % len(msgs))
    for cls, nm in msgs:
        rec("      %s -> %r" % (cls, nm[:60]))


if __name__ == "__main__":
    main()
