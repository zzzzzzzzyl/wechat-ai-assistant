# -*- coding: utf-8 -*-
"""探测微信当前的真实配色（浅色 / 深色），为悬浮窗主题提供实测色板。

为什么不能凭记忆填色号
----------------------
「微信绿」到底是 #07C160 还是别的、深色模式下聊天背景到底是 #191919 还是
#111111 —— 这类东西记忆里都是**近似值**。而配色差一点点，并排摆着的观感
就差很多。项目里已经有一条不依赖遮挡的取图能力（capture.PrintWindow），
那就直接量。

判定原理
--------
不猜主题名，只看**像素**：
  - 取整窗位图
  - 按区域统计颜色直方图（分左/中/右三个竖带，对应会话列表 / 聊天区 / 边缘）
  - 出现次数最多的那批颜色就是背景与气泡
  - 用整窗平均亮度判断浅色 / 深色

安全性
------
纯只读：`PrintWindow` 让微信窗口自己渲染成位图，不置顶、不激活、不移动、
不发送任何输入。符合项目红线。

用法：
    python probes/probe_theme.py
"""

import ctypes
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import win32gui  # noqa: E402

from wechat_assistant.capture import capture_window, is_blank  # noqa: E402

PROC_NAME = "Weixin.exe"
CLASS_HINT = "QWindowIcon"


def _exe_of_pid(pid: int) -> str:
    """取某进程的 exe 全路径。**不启子进程** —— 沙箱禁止管道，
    `subprocess.run(capture_output=True)` 会 EPERM，所以走纯 win32。"""
    import win32api
    import win32con
    import win32process

    h = None
    try:
        # PROCESS_QUERY_LIMITED_INFORMATION 比 QUERY_INFORMATION 宽松，
        # 对同权限级别的进程基本都能打开
        h = win32api.OpenProcess(0x1000, False, pid)
        return win32process.GetModuleFileNameEx(h, 0) or ""
    except Exception:
        return ""
    finally:
        if h is not None:
            try:
                win32api.CloseHandle(h)
            except Exception:
                pass


def find_wechat() -> int:
    """用 EnumWindows 找微信主窗口（**不碰 UIA**，避免 provider 卡死）。"""
    import win32process

    want = PROC_NAME.lower()
    found = []

    def cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            cls = win32gui.GetClassName(hwnd)
            if CLASS_HINT not in cls:
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            exe = _exe_of_pid(pid)
            if os.path.basename(exe).lower() != want:
                return True
            r = win32gui.GetWindowRect(hwnd)
            found.append((hwnd, cls, r, (r[2] - r[0]) * (r[3] - r[1])))
        except Exception:
            return True
        return True

    win32gui.EnumWindows(cb, None)
    if not found:
        return 0
    # 取面积最大的那个（微信有若干辅助窗口）
    found.sort(key=lambda t: -t[3])
    hwnd, cls, rect, _ = found[0]
    print("窗口: hwnd=%d class=%s rect=%s" % (hwnd, cls, rect))
    for h, c, r, a in found[1:6]:
        print("  (其它候选) hwnd=%d class=%s rect=%s" % (h, c, r))
    return hwnd


def hexof(rgb):
    return "#%02X%02X%02X" % rgb


def luminance(rgb):
    r, g, b = rgb
    return 0.299 * r + 0.587 * g + 0.114 * b


def top_colors(img, box=None, n=12):
    """统计区域内的主要颜色（按出现次数）。"""
    region = img.crop(box) if box else img
    region = region.convert("RGB")
    # 缩小采样：颜色统计不需要全分辨率，且快得多
    region = region.resize((min(region.width, 400), min(region.height, 400)))
    cnt = Counter(region.getdata())
    total = sum(cnt.values()) or 1
    return [(rgb, c, c * 100.0 / total) for rgb, c in cnt.most_common(n)]


def main():
    hwnd = find_wechat()
    if not hwnd:
        print("没找到微信主窗口 —— 请确认微信已登录且窗口未关闭")
        return 1

    try:
        rect = win32gui.GetWindowRect(hwnd)
    except Exception as e:
        print("取窗口矩形失败: %s" % e)
        return 1

    img = capture_window(hwnd)
    if img is None:
        print("PrintWindow 抓图失败")
        return 1
    if is_blank(img):
        print("抓到的图是空白的 —— 微信多半最小化了（PrintWindow 只拿到空白）")
        return 1

    outdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out")
    os.makedirs(outdir, exist_ok=True)
    shot = os.path.join(outdir, "theme_probe.png")
    img.save(shot)

    W, H = img.size
    print("位图尺寸: %dx%d  (窗口 rect=%s)" % (W, H, rect))

    # 整窗平均亮度 -> 浅色 / 深色
    small = img.convert("L").resize((80, 80))
    px = list(small.getdata())
    avg = sum(px) / len(px)
    print("整窗平均亮度: %.1f  ->  %s" % (avg, "深色" if avg < 110 else "浅色"))

    # 分带统计。微信窗口横向大致是：
    #   最左 = 图标条 / 会话列表，中间偏右 = 聊天区
    bands = [
        ("左 0~25%   (图标条 / 会话列表)", (0, 0, int(W * 0.25), H)),
        ("中 25~65%  (会话列表 / 聊天区左)", (int(W * 0.25), 0, int(W * 0.65), H)),
        ("右 65~100% (聊天区 / 输入区)", (int(W * 0.65), 0, W, H)),
    ]
    for label, box in bands:
        print("\n--- %s ---" % label)
        for rgb, c, pct in top_colors(img, box, n=8):
            print("   %s  %5.1f%%  %s" % (hexof(rgb), pct, rgb))

    # 顶部标题栏（垂直 0~6%）
    print("\n--- 顶部标题栏 (0~6% 高) ---")
    for rgb, c, pct in top_colors(img, (0, 0, W, max(1, int(H * 0.06))), n=6):
        print("   %s  %5.1f%%" % (hexof(rgb), pct))

    # 底部输入区（85~100%）
    print("\n--- 底部输入区 (85~100% 高) ---")
    for rgb, c, pct in top_colors(img, (0, int(H * 0.85), W, H), n=6):
        print("   %s  %5.1f%%" % (hexof(rgb), pct))

    print("\n截图已保存: %s" % shot)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
