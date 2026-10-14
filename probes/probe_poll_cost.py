# -*- coding: utf-8 -*-
"""
轮询开销实测：多久检查一次合适？

要回答的问题：
    轮询是最简单可靠的方案，但间隔设多少？
    太慢 -> 提醒不及时；太快 -> 白白吃 CPU，还可能拖慢微信。

做法：
    对比两种读取模式在三种间隔下的开销：
      模式 A「全量」  : FindAll(TrueCondition) 并读取每个元素的属性（之前测的 40ms 就是这个）
      模式 B「轻量」  : 只按类名条件查会话列表项，只读 Name（做新消息检测其实只需要这个）
    每组连续跑若干秒，统计：
      - 平均单次耗时
      - 本进程 CPU 占用率（占单核百分比）
      - 微信进程 CPU 占用率（我们的读取会让微信处理 UIA 请求，也要看）
    另外测一次「完全不读」的基线，用于对比微信自身空转的 CPU。

只读：仅 UIA 查询。

用法：python probe_poll_cost.py [每组秒数，默认6]
"""

import ctypes
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


def find_wechat():
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
                best.append((hwnd, pid))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return best[0] if best else (None, None)


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]


_FT_P = ctypes.POINTER(_FILETIME)
_k32 = ctypes.windll.kernel32
# 必须显式声明 argtypes：句柄是 64 位指针，按默认 int 传会截断，
# 而 GetCurrentProcess() 返回的伪句柄是 -1，截断后调用直接失败。
_k32.GetProcessTimes.argtypes = [ctypes.c_void_p, _FT_P, _FT_P, _FT_P, _FT_P]
_k32.GetProcessTimes.restype = ctypes.c_int


def cpu_seconds(handle):
    """进程累计 CPU 时间（秒）。handle 可为 PyHANDLE 或伪句柄 -1。"""
    try:
        c, e, k, u = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
        ok = _k32.GetProcessTimes(
            ctypes.c_void_p(int(handle)),
            ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u),
        )
        if not ok:
            return -1.0

        def ft(f):
            return (f.dwHighDateTime << 32) | f.dwLowDateTime

        return (ft(k) + ft(u)) / 1e7
    except Exception:
        return -1.0


class Reader:
    """把 UIA 对象初始化好，反复读取。"""

    def __init__(self, hwnd):
        import comtypes

        comtypes.CoInitialize()
        import uiautomation as auto
        from uiautomation import uiautomation as core

        self._auto = auto
        self._core = core
        self._ctx = auto.UIAutomationInitializerInThread(debug=False)
        self._ctx.__enter__()
        self.uia = core._AutomationClient.instance().IUIAutomation
        self.el = auto.ControlFromHandle(hwnd).Element
        self.true_cond = self.uia.CreateTrueCondition()
        # 按类名只查会话项
        self.session_cond = self.uia.CreatePropertyCondition(30012, "mmui::ChatSessionCell")

    def close(self):
        try:
            self._ctx.__exit__(None, None, None)
        except Exception:
            pass

    def read_full(self):
        """模式 A：全量读取（所有元素 + 全部属性）。"""
        arr = self.el.FindAll(SCOPE_DESCENDANTS, self.true_cond)
        n = arr.Length
        out = []
        for i in range(n):
            e = arr.GetElement(i)
            try:
                out.append((e.CurrentName, e.CurrentClassName, e.CurrentAutomationId))
            except Exception:
                pass
        return len(out)

    def read_sessions(self):
        """模式 B：只查会话列表项，只读 Name（新消息检测的最小必要读取）。"""
        arr = self.el.FindAll(SCOPE_DESCENDANTS, self.session_cond)
        n = arr.Length
        names = []
        for i in range(n):
            try:
                names.append(arr.GetElement(i).CurrentName)
            except Exception:
                pass
        return len(names)


def bench(reader, mode, interval, duration):
    """按指定间隔连跑 duration 秒，返回统计。"""
    self_h = win32api.GetCurrentProcess()
    wx_h = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, reader.wx_pid)
    fn = reader.read_full if mode == "A" else reader.read_sessions

    costs = []
    c0, m0 = cpu_seconds(self_h), cpu_seconds(wx_h)
    t0 = time.time()
    n = 0
    while time.time() - t0 < duration:
        s = time.perf_counter()
        try:
            fn()
        except Exception:
            pass
        costs.append((time.perf_counter() - s) * 1000)
        n += 1
        time.sleep(interval)
    wall = time.time() - t0
    c1, m1 = cpu_seconds(self_h), cpu_seconds(wx_h)
    try:
        win32api.CloseHandle(wx_h)
    except Exception:
        pass

    return {
        "n": n,
        "avg_ms": sum(costs) / len(costs) if costs else 0,
        "min_ms": min(costs) if costs else 0,
        "max_ms": max(costs) if costs else 0,
        "self_cpu": (c1 - c0) / wall * 100,     # 占单核百分比
        "wx_cpu": (m1 - m0) / wall * 100,
        "wall": wall,
    }


def idle_baseline(pid, duration):
    """什么都不读，只测微信自身空转的 CPU。"""
    wx_h = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    m0 = cpu_seconds(wx_h)
    t0 = time.time()
    time.sleep(duration)
    wall = time.time() - t0
    m1 = cpu_seconds(wx_h)
    win32api.CloseHandle(wx_h)
    return (m1 - m0) / wall * 100


def main():
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0
    hwnd, pid = find_wechat()
    if not hwnd:
        print("!! 未找到微信主窗口")
        return

    print("=" * 84)
    print("轮询开销实测")
    print("微信 hwnd=%s pid=%d   每组时长 %.0fs" % (hex(hwnd), pid, dur))
    print("=" * 84)

    base = idle_baseline(pid, dur)
    print("基线：不读取时，微信自身 CPU 占用 = %.2f%%（单核）" % base)
    print("")

    reader = Reader(hwnd)
    reader.wx_pid = pid
    try:
        # 先做单次耗时对比
        print("-" * 84)
        print("单次读取耗时")
        print("-" * 84)
        for mode, label in (("A", "全量(所有元素+全部属性)"), ("B", "轻量(只查会话列表名称)")):
            fn = reader.read_full if mode == "A" else reader.read_sessions
            xs = []
            cnt = 0
            for _ in range(15):
                s = time.perf_counter()
                cnt = fn()
                xs.append((time.perf_counter() - s) * 1000)
                time.sleep(0.05)
            xs.sort()
            print("  %-28s 读取到 %-4d 项 | 中位 %6.1f ms | 最小 %6.1f | 最大 %6.1f"
                  % (label, cnt, xs[len(xs) // 2], xs[0], xs[-1]))
        print("")

        # 再按间隔跑
        print("-" * 84)
        print("不同间隔下的持续开销（连续 %.0f 秒）" % dur)
        print("-" * 84)
        print("%-6s %-8s %-10s %-10s %-14s %-16s" % (
            "模式", "间隔", "读取次数", "平均耗时", "本程序CPU(单核)", "微信CPU(单核)"))
        print("-" * 84)
        for mode, mlabel in (("A", "全量"), ("B", "轻量")):
            for iv in (1.0, 0.5, 0.25):
                r = bench(reader, mode, iv, dur)
                print("%-6s %-8s %-10d %-10.1f %-14.2f %-16.2f" % (
                    mlabel, "%.2fs" % iv, r["n"], r["avg_ms"], r["self_cpu"], r["wx_cpu"]))
        print("")
        print("说明：CPU 百分比是相对『单核』的占用率。")
        print("      例如 4.0%% 表示吃掉了 1/25 个核心，8 核机器上约占总 CPU 的 0.5%%。")
    finally:
        reader.close()


if __name__ == "__main__":
    main()
