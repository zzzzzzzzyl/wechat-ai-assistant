# -*- coding: utf-8 -*-
"""在微信窗口位图里**按颜色找区域**，定位气泡、面板、分割线的真实色值。

为什么要它
----------
`probe_theme.py` 的直方图只能告诉我们「哪些颜色多」，但**说不出**哪个颜色是
「自己发的气泡」、哪个是「对方的气泡」—— 它们面积小，淹没在背景色里。

这里换个办法：对每个出现次数够多的颜色，算出它所有像素的
  - 数量 / 占比
  - 水平重心（x 的中心）
  - 包围盒
微信聊天区里**靠右**的色块 = 自己发的（右对齐），**靠左**的 = 对方发的。
于是不用 OCR、不用 UIA，就能把气泡色认出来。

安全性
------
纯只读取图 + 像素统计，不碰微信状态。
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))   # 项目根：导入 wechat_assistant
sys.path.insert(0, _HERE)                    # probes/：复用 probe_theme 的找窗口逻辑

import numpy as np  # noqa: E402

from wechat_assistant.capture import capture_window, is_blank  # noqa: E402
from probe_theme import find_wechat  # noqa: E402


def hexof(c):
    return "#%02X%02X%02X" % (int(c[0]), int(c[1]), int(c[2]))


def analyze(img, box, label, top=14, min_pct=0.05):
    x1, y1, x2, y2 = box
    region = np.array(img.crop(box).convert("RGB"))
    H, W = region.shape[:2]
    flat = region.reshape(-1, 3)
    colors, counts = np.unique(flat, axis=0, return_counts=True)
    order = np.argsort(-counts)
    total = flat.shape[0]

    print("\n===== %s  (%dx%d) =====" % (label, W, H))
    print("  %-9s %6s  %-7s %-7s  %s" % ("颜色", "占比", "重心x", "跨度x", "说明"))
    for i in order[:top]:
        c = colors[i]
        pct = counts[i] * 100.0 / total
        if pct < min_pct:
            break
        mask = np.all(region == c, axis=-1)
        ys, xs = np.nonzero(mask)
        cx = xs.mean()
        rel = cx / W          # 0=最左 1=最右
        span = xs.max() - xs.min()
        if rel > 0.62:
            note = "靠右 -> 我方气泡候选"
        elif rel < 0.38:
            note = "靠左 -> 对方气泡/面板"
        else:
            note = "居中/铺满 -> 背景/标题栏"
        print("  %-9s %5.2f%%  %6.2f  %6d  %s"
              % (hexof(c), pct, rel, span, note))


def main():
    hwnd = find_wechat()
    if not hwnd:
        print("没找到微信主窗口")
        return 1
    img = capture_window(hwnd)
    if img is None or is_blank(img):
        print("抓图失败或整幅空白（微信可能最小化了）")
        return 1

    W, H = img.size
    print("位图: %dx%d" % (W, H))

    # 微信横向布局实测约：图标条 ~7% + 会话列表 ~27% + 聊天区 ~66%
    # 聊天区左边界取 36% 比较稳（会话列表右缘附近）
    chat_x = int(W * 0.36)
    analyze(img, (chat_x, int(H * 0.08), W, int(H * 0.84)),
            "聊天区（消息列表）", top=16, min_pct=0.04)
    analyze(img, (0, 0, chat_x, H), "左栏（图标条 + 会话列表）", top=10)
    analyze(img, (0, int(H * 0.84), W, H), "底部（输入区）", top=10)

    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
