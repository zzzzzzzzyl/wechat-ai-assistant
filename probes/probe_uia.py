# -*- coding: utf-8 -*-
"""
探测 PC 微信窗口的 UIAutomation 可读性（v2：绕开 GetRootControl().GetChildren()）。

为什么不用 root.GetChildren()：
    微信 4.0 机器上常同时存在十几个 WeChatAppEx 小程序进程，其中某个窗口的 UIA
    provider 无响应时，全量枚举顶层窗口会**永久阻塞**（实测 60s+ 不返回）。
    因此这里改为先用 Win32 API 按 HWND 枚举窗口（快、不会卡），再用
    ControlFromHandle 精确取到微信窗口，且所有 UIA 调用都放在子线程里带超时。

只读性保证：
    仅调用 UIA 查询接口与 Win32 只读接口；不模拟键鼠、不点击、不发送消息、
    不读写微信任何配置或数据文件。

用法：
    python probe_uia.py                # 列表 + dump 微信窗口
    python probe_uia.py --depth 10
    python probe_uia.py --hwnd 0x1234  # 指定窗口句柄
    python probe_uia.py --list-only
"""

import argparse
import datetime as dt
import os
import sys
import threading
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import win32gui
import win32process

MAX_ELEMENTS_HARD = 6000
NAME_MAX_LEN = 400
UIA_TIMEOUT = 25.0  # 单个窗口 UIA 探测的墙钟超时（秒）

WEIXIN_HINTS = ("weixin", "wechat")


def out(s=""):
    print(s, flush=True)


def clip(s, n=NAME_MAX_LEN):
    s = str(s).replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    return s if len(s) <= n else s[:n] + "...<total=%d>" % len(s)


_proc_cache = {}


def proc_name(pid):
    if pid in _proc_cache:
        return _proc_cache[pid]
    name = "?"
    try:
        import win32api
        import win32con
        import win32process as wp

        h = win32api.OpenProcess(
            win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        try:
            name = os.path.basename(wp.GetModuleFileNameEx(h, 0))
        finally:
            win32api.CloseHandle(h)
    except Exception as e:
        name = "?<%s>" % type(e).__name__
    _proc_cache[pid] = name
    return name


def enum_top_windows():
    """Win32 枚举顶层窗口。返回 list[dict]。"""
    res = []

    def cb(hwnd, _):
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            res.append(
                {
                    "hwnd": hwnd,
                    "hex": "0x%08X" % (hwnd & 0xFFFFFFFF),
                    "pid": pid,
                    "proc": proc_name(pid),
                    "cls": win32gui.GetClassName(hwnd),
                    "title": win32gui.GetWindowText(hwnd),
                    "visible": bool(win32gui.IsWindowVisible(hwnd)),
                    "iconic": bool(win32gui.IsIconic(hwnd)),
                    "rect": win32gui.GetWindowRect(hwnd),
                }
            )
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return res


def uia_dump(hwnd, max_depth, result):
    """在子线程里执行；任何异常/超时都不能拖垮主进程。"""
    try:
        import comtypes

        comtypes.CoInitialize()
    except Exception:
        pass
    try:
        import uiautomation as auto

        with auto.UIAutomationInitializerInThread(debug=False):
            elem = auto.ControlFromHandle(hwnd)
            if elem is None:
                result["error"] = "ControlFromHandle 返回 None"
                return
            lines = []
            stats = {}
            counter = [0]
            _walk(elem, 0, max_depth, lines, stats, counter)
            result["lines"] = lines
            result["stats"] = stats
            result["count"] = counter[0]
    except Exception as e:
        result["error"] = "%s: %s" % (type(e).__name__, e)


def _safe(fn, default=None):
    try:
        v = fn()
        return default if v is None else v
    except Exception:
        return default


def _rect(ctrl):
    r = _safe(lambda: ctrl.BoundingRectangle)
    if r is None:
        return "?"
    try:
        return "(%d,%d,%d,%d)" % (r.left, r.top, r.right, r.bottom)
    except Exception:
        return "?"


def _walk(ctrl, depth, max_depth, lines, stats, counter):
    if counter[0] >= MAX_ELEMENTS_HARD:
        return
    counter[0] += 1
    ct = _safe(lambda: ctrl.ControlTypeName, "?")
    stats[ct] = stats.get(ct, 0) + 1
    name = _safe(lambda: ctrl.Name, "")
    cls = _safe(lambda: ctrl.ClassName, "")
    aid = _safe(lambda: ctrl.AutomationId, "")
    off = _safe(lambda: ctrl.IsOffscreen, False)

    parts = ["  " * depth + str(ct)]
    if name:
        parts.append('name="%s"' % clip(name))
    if cls:
        parts.append('class="%s"' % clip(cls, 60))
    if aid:
        parts.append('id="%s"' % clip(aid, 60))
    parts.append(_rect(ctrl))
    if off:
        parts.append("[offscreen]")
    lines.append(" ".join(parts))

    if depth >= max_depth:
        return
    kids = _safe(lambda: ctrl.GetChildren(), None)
    if kids is None:
        lines.append("  " * (depth + 1) + "<GetChildren 失败>")
        return
    for c in kids:
        _walk(c, depth + 1, max_depth, lines, stats, counter)


def run_with_timeout(fn, timeout, *a):
    box = {}

    def target():
        try:
            fn(*a)
        except Exception as e:
            box["error"] = "%s: %s" % (type(e).__name__, e)

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    return t.is_alive(), box


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--depth", type=int, default=6)
    ap.add_argument("--hwnd", default=None, help="指定窗口句柄，如 0x00123456")
    ap.add_argument("--list-only", action="store_true")
    ap.add_argument("--all", action="store_true", help="dump 所有微信进程的顶层窗口")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    outdir = os.path.join(os.path.dirname(here), "out")
    os.makedirs(outdir, exist_ok=True)
    buf = []

    def rec(s=""):
        out(s)
        buf.append(s)

    rec("=" * 78)
    rec("PC 微信 UIAutomation 可读性探测 v2")
    rec("时间: %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    rec("Python: %s" % sys.version.split()[0])
    rec("管理员: %s" % _is_admin())
    rec("=" * 78)

    t0 = time.time()
    wins = enum_top_windows()
    rec("")
    rec("--- 顶层窗口共 %d 个 ---" % len(wins))
    rec("%-12s %-7s %-22s %-30s %-6s %s" % ("HWND", "PID", "PROC", "CLASS", "VIS", "TITLE"))
    for w in wins:
        rec(
            "%-12s %-7d %-22s %-30s %-6s %s"
            % (w["hex"], w["pid"], clip(w["proc"], 22), clip(w["cls"], 30),
               "Y" if w["visible"] else "n", clip(w["title"], 60))
        )

    cands = []
    if args.hwnd:
        h = int(args.hwnd, 16) if str(args.hwnd).lower().startswith("0x") else int(args.hwnd)
        cands = [w for w in wins if w["hwnd"] == h]
    else:
        cands = [
            w for w in wins
            if any(x in w["proc"].lower() for x in WEIXIN_HINTS)
            or any(x in w["cls"].lower() for x in WEIXIN_HINTS)
        ]

    rec("")
    rec("--- 命中的微信相关窗口: %d 个 ---" % len(cands))
    for w in cands:
        rec(
            "hwnd=%s pid=%d proc=%s class=%s title=%s rect=%s"
            % (w["hex"], w["pid"], w["proc"], w["cls"], clip(w["title"], 40), w["rect"])
        )
    if not cands:
        rec("")
        rec("!! 没找到微信窗口。请确认微信已启动且主窗口不是仅托盘状态。")

    if args.list_only or not cands:
        _finish(rec, buf, outdir, t0)
        return

    # 优先探测「有标题的、可见的」窗口；--all 时全部探测
    probe = cands
    if not args.all:
        visible = [w for w in cands if w["visible"] and not w["iconic"]]
        probe = visible or cands

    for w in probe:
        rec("")
        rec("=" * 78)
        rec("UIA DUMP  hwnd=%s pid=%d class=%s title=%s" % (w["hex"], w["pid"], w["cls"], clip(w["title"], 40)))
        rec("=" * 78)
        res = {}
        alive, _ = run_with_timeout(uia_dump, UIA_TIMEOUT, w["hwnd"], args.depth, res)
        w["result"] = res
        w["timeout"] = alive
        if alive:
            rec("!! UIA 调用 %.0fs 未返回 —— 该窗口 UIA provider 无响应（本身就是重要结论）" % UIA_TIMEOUT)
            continue
        if res.get("error"):
            rec("!! 失败: %s" % res["error"])
            continue
        for ln in res.get("lines", []):
            rec(ln)
        rec("")
        rec("-- 控件统计 (共 %d 个) --" % res.get("count", 0))
        st = res.get("stats", {})
        for k in sorted(st, key=lambda x: -st[x]):
            rec("   %-24s %d" % (k, st[k]))

    rec("")
    rec("=" * 78)
    rec("结论")
    rec("=" * 78)
    _verdict(rec, probe)

    _finish(rec, buf, outdir, t0)


def _is_admin():
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return "?"


def _verdict(rec, probe):
    for w in probe:
        res = w.get("result", {})
        if w.get("timeout"):
            rec("hwnd=%s -> UIA 超时，无法读取" % w["hex"])
            continue
        if res.get("error"):
            rec("hwnd=%s -> 错误 %s" % (w["hex"], res["error"]))
            continue
        st = res.get("stats", {})
        n = res.get("count", 0)
        text_n = st.get("TextControl", 0)
        list_n = st.get("ListItemControl", 0)
        rec("hwnd=%s -> 元素 %d 个；Text=%d ListItem=%d" % (w["hex"], n, text_n, list_n))
        if text_n + list_n >= 10:
            rec("    => 看起来 UIA 暴露了文本结构，路线可行，下一步解析消息列表。")
        elif n <= 5:
            rec("    => 控件极少，微信自绘 UI 未暴露 UIA，UIA 路线基本作废，应改走 OCR。")
        else:
            rec("    => 结构有限，需要人工看上面的 dump 判断。")


def _finish(rec, buf, outdir, t0):
    rec("")
    rec("总耗时: %.2fs" % (time.time() - t0))
    path = os.path.join(outdir, "uia_dump_%s.txt" % dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(buf))
    out("\n[已保存] %s" % path)


if __name__ == "__main__":
    main()
