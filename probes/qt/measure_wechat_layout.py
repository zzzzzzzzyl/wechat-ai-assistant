# -*- coding: utf-8 -*-
"""量**微信自己的**布局参数，拿来做我们面板的标尺。

    python -X utf8 probes/qt/measure_wechat_layout.py

为什么要它：我一直在用"微信大概 32px"这种**记忆里的数字**调我们的标题栏，
而上一轮把标题栏从 38 改成 32（基准像素），真机变成 **48px**，
在 465px 高的面板里占 **10.3%** —— 比改之前还高，反而更臃肿。
记忆不可靠，直接量。

微信窗口是 Qt 应用（`Qt51514QWindowIcon`），用 `capture.PrintWindow` 抓它
**自己渲染**的位图（不受遮挡影响、不置顶、不激活），然后找横向色带边界：
   · 顶部标题栏：颜色和下面聊天区不同的一条带
   · 底部输入区：同样手法
这样量出来的才是"微信实际用多少像素"，而不是我猜的。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

import win32gui                                               # noqa: E402
from PIL import Image                                         # noqa: E402

from wechat_assistant import capture                          # noqa: E402


def find_wechat():
    got = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        cls = win32gui.GetClassName(hwnd)
        if "QWindowIcon" not in cls:
            return
        r = win32gui.GetWindowRect(hwnd)
        w, h = r[2] - r[0], r[3] - r[1]
        if w < 600 or h < 400 or r[0] < -10000:
            return
        got.append((hwnd, r))

    win32gui.EnumWindows(cb, None)
    return got


def row_sig(img, y, x0, x1, step=3):
    """某一行的平均色（缩略成三个数）。"""
    r = g = b = n = 0
    for x in range(x0, x1, step):
        px = img.getpixel((x, y))
        r += px[0]
        g += px[1]
        b += px[2]
        n += 1
    return (r // n, g // n, b // n)


def main():
    cands = find_wechat()
    if not cands:
        print("没找到可见的微信窗口（PrintWindow 抓不到最小化的窗口）")
        print("→ 把微信恢复出来再跑一次")
        return 1

    hwnd, rect = cands[0]
    print("微信窗口 hwnd=0x%X rect=%s" % (hwnd, rect))
    pm = capture.capture_window(hwnd)
    if pm is None or capture.is_blank(pm):
        print("抓到空白（微信可能最小化）—— 无法量")
        return 1

    img = pm.convert("RGB")
    W, H = img.size
    print("位图 %dx%d（这是**物理像素**）" % (W, H))

    # 从窗口右边往左找聊天区（避开左侧会话列表）
    x0, x1 = int(W * 0.62), int(W * 0.95)

    print("\n== 逐行颜色（每 6 行取一个，找色带边界）==")
    prev = None
    edges = []
    for y in range(0, H, 3):
        sig = row_sig(img, y, x0, x1)
        if prev is not None:
            d = max(abs(sig[i] - prev[i]) for i in range(3))
            if d >= 4:
                edges.append((y, prev, sig, d))
        prev = sig
    for y, a, b, d in edges[:14]:
        print("  y=%-4d %s -> %s  (Δ%d)" % (y, a, b, d))

    print("\n== 头部色带宽度（标题栏）==")
    top = row_sig(img, 2, x0, x1)
    print("  顶部行色 = %s" % (top,))
    hdr = 0
    for y in range(0, min(H, 200)):
        sig = row_sig(img, y, x0, x1)
        if max(abs(sig[i] - top[i]) for i in range(3)) < 4:
            hdr = y
        else:
            break
    print("  与顶部同色的连续高度 = %d px（→ 标题栏高度）" % hdr)

    print("\n== 底部色带宽度（输入区）==")
    bot = row_sig(img, H - 3, x0, x1)
    print("  底部行色 = %s" % (bot,))
    ftr = 0
    for y in range(H - 1, max(0, H - 300), -1):
        sig = row_sig(img, y, x0, x1)
        if max(abs(sig[i] - bot[i]) for i in range(3)) < 4:
            ftr = H - 1 - y
        else:
            break
    print("  与底部同色的连续高度 = %d px（→ 输入区高度）" % ftr)

    print("\n== 换算到我们的基准像素 ==")
    print("  我们的缩放是 1.5（150%%），所以 微信的物理 %dpx 标题栏" % hdr)
    print("  = 逻辑 %.1fpx = 我们的基准像素 %.1f" % (hdr / 1.5, hdr / 1.5))
    print("  面板总高参照：微信 %d → 逻辑 %.0f" % (H, H / 1.5))

    out = os.path.join(ROOT, "out", "wechat_layout_probe.png")
    img.save(out)
    print("\n已存图: %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
