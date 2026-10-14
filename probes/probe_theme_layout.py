# -*- coding: utf-8 -*-
"""扫描微信窗口的**列剖面**：每一列的主力颜色是什么。

目的有两个：
  1. 定位面板边界（图标条 / 会话列表 / 聊天区 / 滚动条各占多少像素）——
     之前靠「25%、36%」这类猜的百分比，不牢靠。
  2. 找出**我方气泡**到底是不是绿色系。前两次直方图里聊天区只有 #1E1E1F
     （背景）和 #2F2F30（对方气泡），没有明显的绿色大色块 ——
     需要确认是「这个会话里没有我发的消息」还是「我方气泡不是绿色」。

做法：把整幅图按列切成一格一格，每格取该列范围内出现次数最多的颜色。
颜色突变的地方就是面板边界。
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import numpy as np  # noqa: E402

from wechat_assistant.capture import capture_window, is_blank  # noqa: E402
from probe_theme import find_wechat  # noqa: E402


def hexof(c):
    return "#%02X%02X%02X" % (int(c[0]), int(c[1]), int(c[2]))


def col_profile(img, step=8, y0=0.0, y1=1.0):
    """逐列主力颜色。返回 [(x_start, x_end, 颜色, 占比), ...]"""
    arr = np.array(img.convert("RGB"))
    H = arr.shape[0]
    y0i, y1i = int(H * y0), int(H * y1)
    sub = arr[y0i:y1i]
    out = []
    for x in range(0, arr.shape[1], step):
        band = sub[:, x:x + step].reshape(-1, 3)
        colors, counts = np.unique(band, axis=0, return_counts=True)
        k = counts.argmax()
        out.append((x, min(x + step, arr.shape[1]), colors[k], counts[k] * 100.0 / band.shape[0]))
    return out


def merge_runs(prof):
    """相邻同色合并成区间，输出更紧凑。"""
    runs = []
    for x0, x1, c, pct in prof:
        key = tuple(int(v) for v in c)
        if runs and runs[-1][2] == key:
            runs[-1][1] = x1
            runs[-1][3].append(pct)
        else:
            runs.append([x0, x1, key, [pct]])
    return [(a, b, c, sum(p) / len(p)) for a, b, c, p in runs]


def show(img, label, **kw):
    print("\n===== %s =====" % label)
    for a, b, c, pct in merge_runs(col_profile(img, **kw)):
        w = b - a
        if w < 8:
            continue
        print("  x %4d~%-4d 宽%4d  %s  %5.1f%%" % (a, b, w, hexof(c), pct))


def green_scan(img):
    """找绿色系像素（G 明显大于 R 和 B），报告它们的分布。

    用来回答「我方气泡是不是绿色」—— 如果是绿色气泡且当前会话里有我发的消息，
    这里一定能看到成片的绿。
    """
    arr = np.array(img.convert("RGB")).astype(int)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    mask = (g > r + 25) & (g > b + 25) & (g > 70)
    n = int(mask.sum())
    print("\n===== 绿色系像素 (%d 个, 占 %.2f%%) =====" % (n, n * 100.0 / mask.size))
    if n == 0:
        return
    ys, xs = np.nonzero(mask)
    print("  x 范围 %d~%d    y 范围 %d~%d" % (xs.min(), xs.max(), ys.min(), ys.max()))
    # 按 x 分桶，看绿色集中在哪些横向位置
    for lo, hi in [(0, 0.07), (0.07, 0.36), (0.36, 0.62), (0.62, 0.95), (0.95, 1.0)]:
        x0, x1 = int(img.width * lo), int(img.width * hi)
        sel = (xs >= x0) & (xs < x1)
        cnt = int(sel.sum())
        if cnt == 0:
            continue
        avg = arr[ys[sel], xs[sel]].mean(axis=0)
        print("    x %3d%%~%3d%%   %6d 个  平均色 %s" % (lo * 100, hi * 100, cnt, hexof(avg)))


def main():
    hwnd = find_wechat()
    if not hwnd:
        print("没找到微信主窗口")
        return 1
    img = capture_window(hwnd)
    if img is None or is_blank(img):
        print("抓图失败或空白")
        return 1

    print("位图: %dx%d" % img.size)
    show(img, "整窗列剖面（全高）", step=8)
    show(img, "中部列剖面（20%~80% 高，避开标题栏和输入区）",
         step=8, y0=0.20, y1=0.80)
    green_scan(img)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
