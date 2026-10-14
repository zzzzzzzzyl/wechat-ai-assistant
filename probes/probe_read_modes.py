# -*- coding: utf-8 -*-
"""四种读法的耗时对比 + 持续轮询代价实测。

要回答的问题：
  1. 单次读取，各读法分别多久？（决定「快扫探针」能不能做到 ~20ms）
  2. 拟新增的「聊天区轻量读」到底有多快？比全量快多少？
  3. FindAllBuildCache（一次调用取回属性）能不能用？快多少？
  4. 持续高频轮询，代价是记在谁头上（我们 / 微信）？

读法：
  A 全量(现状 read_chat)      : FindAll(TrueCondition) + 每元素读 3 个属性 + rect
  B 会话列表轻量(现状)         : 按类名筛 ChatSessionCell + 只读 Name
  C 聊天区轻量(拟新增)         : 按类名筛 Chat*ItemView + 读 Name + rect
  D C 的 BuildCache 版         : 同上，但属性一次取回

纯只读。
"""

import ctypes
import os
import statistics
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

PROP_NAME = 30045
PROP_CLASSNAME = 30012
PROP_AUTOMATION_ID = 30011
PROP_CONTROL_TYPE = 30005
PROP_RECT = 30043

# ---- 进程 CPU 计时（复用 probe_poll_cost.py 的做法）----------------------

_k32 = ctypes.windll.kernel32
_FT = ctypes.c_ulonglong * 2


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32),
                ("dwHighDateTime", ctypes.c_uint32)]


_k32.GetProcessTimes.argtypes = [ctypes.c_void_p, ctypes.POINTER(FILETIME),
                                 ctypes.POINTER(FILETIME),
                                 ctypes.POINTER(FILETIME),
                                 ctypes.POINTER(FILETIME)]
_k32.GetProcessTimes.restype = ctypes.c_int


def cpu_seconds(pid):
    """进程累计 CPU 秒数（内核+用户）。pid=None 表示自己。"""
    h = _k32.GetCurrentProcess() if pid is None else \
        _k32.OpenProcess(0x0400, False, pid)      # PROCESS_QUERY_INFORMATION
    if not h:
        return 0.0
    try:
        c, e, k, u = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        if not _k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                    ctypes.byref(k), ctypes.byref(u)):
            return 0.0
        def ft(f):
            return (f.dwHighDateTime << 32) | f.dwLowDateTime
        return (ft(k) + ft(u)) / 1e7
    finally:
        if pid is not None:
            _k32.CloseHandle(h)


def find_wechat():
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
                found.append((hwnd, pid))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return found[0] if found else (None, None)


# ---- 四种读法 -------------------------------------------------------------

def make_readers(hwnd, cfg):
    """返回 {名称: 函数}，每个函数返回「读到的项数」。"""
    cls_cell = cfg.get("wechat.cls.session_cell", "mmui::ChatSessionCell")

    def a_full(auto, uia):
        el = auto.ControlFromHandle(hwnd).Element
        arr = el.FindAll(SCOPE_DESCENDANTS, uia.CreateTrueCondition())
        n = 0
        for i in range(arr.Length):
            e = arr.GetElement(i)
            _ = e.CurrentClassName or ""
            _ = e.CurrentAutomationId or ""
            _ = e.CurrentName or ""
            r = e.CurrentBoundingRectangle
            _ = (r.left, r.top, r.right, r.bottom)
            n += 1
        return n

    def b_sessions(auto, uia):
        el = auto.ControlFromHandle(hwnd).Element
        cond = uia.CreatePropertyCondition(PROP_CLASSNAME, cls_cell)
        arr = el.FindAll(SCOPE_DESCENDANTS, cond)
        n = 0
        for i in range(arr.Length):
            if (arr.GetElement(i).CurrentName or "").strip():
                n += 1
        return n

    def c_chat_light(auto, uia):
        el = auto.ControlFromHandle(hwnd).Element
        cond = uia.CreateOrCondition(
            uia.CreatePropertyCondition(PROP_CLASSNAME, "mmui::ChatTextItemView"),
            uia.CreateOrCondition(
                uia.CreatePropertyCondition(PROP_CLASSNAME, "mmui::ChatBubbleItemView"),
                uia.CreateOrCondition(
                    uia.CreatePropertyCondition(PROP_CLASSNAME, "mmui::ChatBubbleReferItemView"),
                    uia.CreatePropertyCondition(PROP_CLASSNAME, "mmui::ChatItemView"))))
        arr = el.FindAll(SCOPE_DESCENDANTS, cond)
        n = 0
        for i in range(arr.Length):
            e = arr.GetElement(i)
            _ = e.CurrentName or ""
            r = e.CurrentBoundingRectangle
            _ = r.top
            n += 1
        return n

    def d_chat_cache(auto, uia):
        el = auto.ControlFromHandle(hwnd).Element
        cr = uia.CreateCacheRequest()
        for p in (PROP_NAME, PROP_CLASSNAME, PROP_RECT):
            cr.AddProperty(p)
        cond = uia.CreateOrCondition(
            uia.CreatePropertyCondition(PROP_CLASSNAME, "mmui::ChatTextItemView"),
            uia.CreateOrCondition(
                uia.CreatePropertyCondition(PROP_CLASSNAME, "mmui::ChatBubbleItemView"),
                uia.CreateOrCondition(
                    uia.CreatePropertyCondition(PROP_CLASSNAME, "mmui::ChatBubbleReferItemView"),
                    uia.CreatePropertyCondition(PROP_CLASSNAME, "mmui::ChatItemView"))))
        arr = el.FindAllBuildCache(SCOPE_DESCENDANTS, cond, cr)
        n = 0
        for i in range(arr.Length):
            e = arr.GetElement(i)
            _ = e.GetCachedPropertyValue(PROP_NAME)
            _ = e.GetCachedPropertyValue(PROP_RECT)
            n += 1
        return n

    return [("A 全量(现状 read_chat)", a_full),
            ("B 会话列表轻量(现状 read_sessions)", b_sessions),
            ("C 聊天区轻量(拟新增)", c_chat_light),
            ("D 聊天区轻量 + BuildCache", d_chat_cache)]


def measure_cpu(pid, dur, label=""):
    """在 dur 秒内测微信进程的平均 CPU（占单核百分比）。"""
    t0 = time.perf_counter()
    wx0 = cpu_seconds(pid)
    time.sleep(dur)
    wall = time.perf_counter() - t0
    return (cpu_seconds(pid) - wx0) / wall * 100


def sustain_only(hwnd, pid, cfg, worker, letter, interval, duration):
    """只跑持续轮询测试，前后各测一次基线（看漂移）。

    用法: python probe_read_modes.py --sustain A 0.25 30
    """
    readers = dict((n[0], n[1]) for n in make_readers(hwnd, cfg))
    target = [n for n in readers if n.startswith(letter + " ")]
    if not target:
        print("没有以 %r 开头的读法，可选：%s" % (letter, list(readers)))
        return 1
    name = target[0]
    fn = readers[name]

    print("=" * 84)
    print("持续轮询复测：%s   间隔 %.2fs   每档 %.0fs" % (name, interval, duration))
    print("微信 hwnd=%s pid=%s" % (hex(hwnd), pid))
    print("=" * 84)

    b1 = measure_cpu(pid, duration)
    print("  基线（改前）        微信 CPU = %.2f%%" % b1)

    cost = []
    stop_time = time.perf_counter() + duration

    def one(auto, uia):
        t = time.perf_counter()
        fn(auto, uia)
        return (time.perf_counter() - t) * 1000

    self0 = cpu_seconds(None)
    wx0 = cpu_seconds(pid)
    t0 = time.perf_counter()
    # ⚠️ 逐次 worker.call，而不是把整个循环塞进一次 call ——
    #    UiaWorker 单次调用有超时限制（默认 30s），塞整个循环会超时。
    #    这样也正好和生产路径一致（正式代码也是每拍一次 worker.call）。
    while time.perf_counter() < stop_time:
        cost.append(worker.call(one))
        time.sleep(interval)
    wall = time.perf_counter() - t0
    self_cpu = (cpu_seconds(None) - self0) / wall * 100
    wx_cpu = (cpu_seconds(pid) - wx0) / wall * 100

    print("  轮询中              微信 CPU = %.2f%%  (相对基线 %+.2f)"
          % (wx_cpu, wx_cpu - b1))
    print("    %d 次 | 单次中位 %.1f ms | 本程序 CPU %.2f%%"
          % (len(cost), statistics.median(cost) if cost else 0, self_cpu))

    b2 = measure_cpu(pid, duration)
    print("  基线（改后）        微信 CPU = %.2f%%  → 两次基线差 %.2f"
          % (b2, abs(b2 - b1)))
    print()
    print("  说明：若 [-基线差, +基线差] 区间盖住了上面那个增量，")
    print("        说明增量落在噪声里，不能判定「读取推高了微信 CPU」。")
    worker.close()
    return 0


def main():
    hwnd, pid = find_wechat()
    if not hwnd:
        print("没找到微信窗口")
        return 1

    sys.path.insert(0, ".")
    from wechat_assistant.config import load_config
    from wechat_assistant.uia_worker import UiaWorker
    cfg = load_config()

    worker = UiaWorker(30.0)

    if "--sustain" in sys.argv:
        i = sys.argv.index("--sustain")
        return sustain_only(hwnd, pid, cfg, worker,
                            sys.argv[i + 1].upper(),
                            float(sys.argv[i + 2]),
                            float(sys.argv[i + 3]))

    readers = make_readers(hwnd, cfg)

    # 先在 worker 线程里把 UIA 会话初始化好，之后所有测量都排在同一线程
    def run_mode(name, fn, times):
        out = []

        def job(auto, uia):
            res = []
            for _ in range(times):
                t0 = time.perf_counter()
                try:
                    res.append((fn(auto, uia), (time.perf_counter() - t0) * 1000))
                except Exception as ex:
                    res.append((None, ("错误:%s" % type(ex).__name__)))
            return res

        out = worker.call(job)
        return out

    print("=" * 84)
    print("四种读法：单次耗时")
    print("微信 hwnd=%s pid=%s" % (hex(hwnd), pid))
    print("=" * 84)

    N = 40
    results = {}
    for name, fn in readers:
        # 预热 3 次，不计入
        run_mode(name, fn, 3)
        rows = run_mode(name, fn, N)
        errs = [r[1] for r in rows if isinstance(r[1], str)]
        if errs:
            print("  %-36s 不可用：%s" % (name, errs[0]))
            results[name] = None
            continue
        ms = [r[1] for r in rows]
        cnt = [r[0] for r in rows]
        results[name] = ms
        print("  %-36s 读到 %-4d 项 | 中位 %6.1f ms | 最小 %6.1f | 最大 %6.1f"
              % (name, int(statistics.median(cnt)), statistics.median(ms),
                 min(ms), max(ms)))

    base = statistics.median(results.get("A 全量(现状 read_chat)", [0]) or [0])
    for name, ms in results.items():
        if ms and "A " not in name:
            print("  → %-34s 比全量快 %.1f 倍"
                  % (name, base / statistics.median(ms)))

    # ---- 持续轮询代价 --------------------------------------------------
    print()
    print("=" * 84)
    print("持续轮询代价（每档跑 12 秒，CPU 为占单核百分比）")
    print("=" * 84)

    # 基线：什么都不读
    wx0 = cpu_seconds(pid)
    time.sleep(12)
    wx_base = (cpu_seconds(pid) - wx0) / 12 * 100
    print("  基线（完全不读）            微信 CPU = %.2f%%" % wx_base)
    print()

    def sustain(name, fn, interval, duration=12):
        self0 = cpu_seconds(None)
        wx0 = cpu_seconds(pid)
        t0 = time.perf_counter()
        n = 0
        cost = []

        def job(auto, uia):
            return None
        # 在 worker 线程里持续跑
        stop = threading.Event()

        def loop(auto, uia):
            while not stop.is_set():
                t = time.perf_counter()
                try:
                    fn(auto, uia)
                except Exception:
                    pass
                cost.append((time.perf_counter() - t) * 1000)
                time.sleep(interval)

        def runner(auto, uia):
            th = threading.Thread(target=loop, args=(auto, uia), daemon=True)
            th.start()
            time.sleep(duration)
            stop.set()
            th.join(timeout=5)
            return None

        worker.call(runner)
        wall = time.perf_counter() - t0
        self_cpu = (cpu_seconds(None) - self0) / wall * 100
        wx_cpu = (cpu_seconds(pid) - wx0) / wall * 100
        print("  %-30s 间隔 %-6s  %4d 次 | 单次中位 %6.1f ms | 本程序 %5.2f%% | 微信 %6.2f%% (+%.2f)"
              % (name, "%.2fs" % interval, len(cost),
                 statistics.median(cost) if cost else 0,
                 self_cpu, wx_cpu, wx_cpu - wx_base))

    for name, fn in readers:
        if results.get(name) is None:
            continue
        if name.startswith("A "):
            sustain(name, fn, 1.0)
        elif name.startswith("B "):
            sustain(name, fn, 0.25)
        else:
            sustain(name, fn, 0.25)
            sustain(name, fn, 0.15)

    worker.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
