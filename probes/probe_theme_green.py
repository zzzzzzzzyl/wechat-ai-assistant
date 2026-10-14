# -*- coding: utf-8 -*-
"""用连通域把绿色像素拆成一个个色块，判断它们是「头像」还是「高亮条」。

上一轮 `probe_theme_layout.py` 发现绿色集中在这两处，都很反直觉：
    x  7%~ 36%  22477 个（会话列表区域）
    x 36%~ 62%   4064 个（聊天区**左**半）
而我方气泡在微信里是**右对齐**的，绿色本该出现在聊天区右半 —— 那里一个都没有。

两种可能的解释，形状完全不同，所以用连通域的面积/宽高比就能分辨：
    头像   -> 很多个 ~40x40 的小方块，宽高比接近 1
    高亮条 -> 少数几个又宽又扁的大块（比如 240x94）
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from wechat_assistant.capture import capture_window, is_blank  # noqa: E402
from probe_theme import find_wechat  # noqa: E402


def hexof(c):
    return "#%02X%02X%02X" % (int(c[0]), int(c[1]), int(c[2]))


def green_blobs(img, min_area=120):
    arr = np.array(img.convert("RGB")).astype(int)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    mask = ((g > r + 25) & (g > b + 25) & (g > 70)).astype(np.uint8)
    n, labels, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)

    blobs = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < min_area:
            continue
        # 该连通域里的中位颜色（比平均色更能代表"纯色块"）
        sel = labels == i
        px = arr[sel]
        med = np.median(px, axis=0)
        blobs.append({
            "box": (int(x), int(y), int(w), int(h)),
            "area": int(area),
            "fill": area * 100.0 / max(1, w * h),
            "ar": w / max(1, h),
            "color": tuple(int(v) for v in med),
        })
    blobs.sort(key=lambda d: -d["area"])
    return blobs


def main():
    hwnd = find_wechat()
    if not hwnd:
        print("没找到微信主窗口")
        return 1
    img = capture_window(hwnd)
    if img is None or is_blank(img):
        print("抓图失败或空白")
        return 1
    W = img.width
    print("位图: %dx%d" % img.size)

    blobs = green_blobs(img)
    print("\n绿色连通域（面积 >= 120）共 %d 个，列出前 25 个：" % len(blobs))
    print("  %-22s %8s %7s %7s  %-9s %s"
          % ("包围盒 x,y,w,h", "面积", "填充率", "宽高比", "中位色", "落在哪一栏"))
    for d in blobs[:25]:
        x, y, w, h = d["box"]
        cx = x + w / 2.0
        pane = ("图标条" if cx < 104 else "会话列表" if cx < 344 else "聊天区")
        kind = "头像/小方块" if 0.6 < d["ar"] < 1.7 and w < 80 else (
            "宽扁块(高亮条?)" if d["ar"] > 2.0 else "其它")
        print("  %-22s %8d %6.0f%% %7.2f  %-9s %s / %s"
              % ("%d,%d,%d,%d" % (x, y, w, h), d["area"], d["fill"], d["ar"],
                 hexof(d["color"]), pane, kind))

    # 聊天区里到底有没有绿色 —— 直接回答那个关键问题
    chat = [d for d in blobs if d["box"][0] + d["box"][2] / 2 > 344]
    print("\n聊天区（x>344）内的绿色块: %d 个，总面积 %d"
          % (len(chat), sum(d["area"] for d in chat)))
    if chat:
        for d in chat[:8]:
            print("   %s 色 %s" % (d["box"], hexof(d["color"])))
    else:
        print("   -> 聊天区里没有任何绿色色块：**当前会话没有我方气泡**，"
              "所以量不到我方气泡色")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
