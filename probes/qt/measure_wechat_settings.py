# -*- coding: utf-8 -*-
"""量微信**设置窗口**的配色（左侧导航 / 选中项 / 右栏 / 分隔线）。

    python -X utf8 probes/qt/measure_wechat_settings.py

⚠️ 为什么之前没有这个：
    项目里所有配色实测（`probe_theme.py`）量的都是**聊天主窗口**。
    但用户报的是「微信**设置**的颜色与字体高亮并不是微信设置的颜色」——
    微信的设置窗口是**另一个窗口**，有自己的导航栏和选中态，
    拿聊天窗口的色板去套它本来就不一定对。
    → 这个探针专门抓设置窗口。

⚠️ 前置：**得先在微信里把设置窗口打开**（设置 → 通用/外观…随便哪一页都行，
    只要能看见左边那一列分组）。探针抓不到会直接说清楚，不会瞎猜。

⚠️ 只读：`PrintWindow` 让窗口自己渲染成位图，不置顶、不激活、不移动、
   不发送任何输入。

⚠️ 存图**按窗口名 + 模式 + 时间戳**落盘，不共用文件名 ——
    `probe_theme.py` 就是因为固定文件名把浅色的证据覆盖掉了（见 CLAUDELOG）。
"""

import os
import sys
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

import win32gui                                                    # noqa: E402

from wechat_assistant.capture import capture_window, is_blank       # noqa: E402

PROC_NAME = "Weixin.exe"


def hexs(rgb):
    return "#%02X%02X%02X" % tuple(rgb[:3])


def lum(rgb):
    r, g, b = rgb[:3]
    return 0.299 * r + 0.587 * g + 0.114 * b


def wechat_windows():
    """列出微信进程的**所有**可见顶层窗口：(hwnd, 标题, 类名, w, h)。"""
    import win32api
    import win32process

    want = PROC_NAME.lower()
    out = []

    def cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            cls = win32gui.GetClassName(hwnd)
            if "QWindowIcon" not in cls and "Qt" not in cls:
                return
            _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
            h = win32api.OpenProcess(0x1000, False, pid)
            try:
                exe = win32process.GetModuleFileNameEx(h, 0) or ""
            finally:
                win32api.CloseHandle(h)
            if os.path.basename(exe).lower() != want:
                return
            r = win32gui.GetWindowRect(hwnd)
            out.append((hwnd, win32gui.GetWindowText(hwnd), cls,
                        r[2] - r[0], r[3] - r[1]))
        except Exception:
            pass

    win32gui.EnumWindows(cb, None)
    return out


def dominant(img, box, n=5):
    W, H = img.size
    x0, y0, x1, y1 = box
    x0, y0 = max(0, int(x0 * W)), max(0, int(y0 * H))
    x1, y1 = min(W, int(x1 * W)), min(H, int(y1 * H))
    if x1 <= x0 or y1 <= y0:
        return []
    reg = img.crop((x0, y0, x1, y1)).convert("RGB")
    step = max(1, min(reg.width, reg.height) // 160)
    cnt = Counter()
    px = reg.load()
    for y in range(0, reg.height, step):
        for x in range(0, reg.width, step):
            cnt[px[x, y]] += 1
    total = sum(cnt.values()) or 1
    return [(hexs(c), 100.0 * k / total) for c, k in cnt.most_common(n)]


def main():
    print("== 微信当前可见的顶层窗口 ==")
    wins = wechat_windows()
    if not wins:
        print("  一个都没找到 —— 微信没在跑？")
        return 1
    for hwnd, title, cls, w, h in wins:
        print("  hwnd=%s  %dx%d  [%s]  「%s」" % (hwnd, w, h, cls, title))

    # 设置窗口：标题里带"设置"的那个；找不到就明确报错（不猜）
    target = None
    for hwnd, title, cls, w, h in wins:
        if "设置" in title:
            target = (hwnd, title, w, h)
            break
    if target is None:
        print("\n" + "!" * 66)
        print("没有找到标题带「设置」的窗口。")
        print("请先在微信里打开设置窗口（左下角三条杠 → 设置），再重跑本探针。")
        print("!" * 66)
        return 2

    hwnd, title, w, h = target
    img = capture_window(hwnd)
    if img is None:
        print("PrintWindow 抓图失败")
        return 1
    if is_blank(img):
        print("抓到空白 —— 设置窗口多半被最小化了，先把它显示出来。")
        return 1

    W, H = img.size
    small = img.convert("L").resize((100, 100))
    avg = sum(small.getdata()) / 10000.0
    mode = "light" if avg > 128 else "dark"

    outdir = os.path.join(ROOT, "out")
    os.makedirs(outdir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    shot = os.path.join(outdir, "wechat_settings_%s_%s.png" % (mode, stamp))
    img.save(shot)

    print("\n图：%s" % shot)
    print("设置窗口 %dx%d   整窗平均亮度 %.1f  ->  【%s】"
          % (W, H, avg, "浅色" if mode == "light" else "深色"))

    zones = [
        ("窗口顶部横条 (0~10% 高)", (0.0, 0.0, 1.0, 0.10)),
        ("左侧导航栏 (0~30% 宽, 10~95%)", (0.0, 0.10, 0.30, 0.95)),
        ("右栏内容区 (35~100% 宽, 10~95%)", (0.35, 0.10, 1.0, 0.95)),
        ("底部横条 (90~100% 高)", (0.0, 0.90, 1.0, 1.0)),
    ]
    for label, box in zones:
        print("\n--- %s ---" % label)
        for c, pct in dominant(img, box):
            r, g, b = (int(c[i:i + 2], 16) for i in (1, 3, 5))
            print("   %s  %5.1f%%   亮度 %5.1f" % (c, pct, lum((r, g, b))))

    # ⚠️ 沿着左侧导航栏**竖直扫**，把每一项的底色和它们的**边界**找出来。
    #    "选中项是什么颜色"只能这么量 —— 光看区域主色会把它（通常只有一项）
    #    淹没在整栏的底色里。
    print("\n--- 左侧导航栏竖直扫描（相邻像素亮度突变 = 项的分界）---")
    px = img.convert("RGB").load()
    x = int(W * 0.15)
    prev = None
    bands = []
    for y in range(int(H * 0.10), int(H * 0.95)):
        c = px[x, y]
        if prev is not None and abs(lum(c) - lum(prev)) >= 6:
            bands.append((y, hexs(prev), hexs(c)))
        prev = c
    for y, a, b in bands:
        print("   y=%4d (%.0f%%)  %s -> %s" % (y, 100.0 * y / H, a, b))
    if not bands:
        print("   （整条导航栏没有明显分界 —— 可能这一页没有选中项）")

    # ⚠️ 再沿**水平**方向扫一行，看左栏和右栏的分界，以及选中项横向铺多宽
    print("\n--- 横向扫描（找左栏/右栏的分界）---")
    for frac in (0.18, 0.30, 0.45):
        y = int(H * frac)
        prev = None
        hits = []
        for xx in range(0, W):
            c = px[xx, y]
            if prev is not None and abs(lum(c) - lum(prev)) >= 6:
                hits.append((xx, hexs(prev), hexs(c)))
            prev = c
        print("   y=%d (%.0f%%): %s" % (y, frac * 100,
                                        ("; ".join("%d:%s->%s" % t for t in hits[:8])
                                         if hits else "无明显分界")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
