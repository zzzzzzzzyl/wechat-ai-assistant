# -*- coding: utf-8 -*-
"""找出「内容块顶边」的正确算法 —— 必须**跳过左边那个头像**。

第一版 content_block_top 用的是「第一段连续 >= 24 行的内容」，
实测在真实数据上**误伤**了：左边头像是一块 0~48 的连续内容，
于是顶边算成了 13，把右侧 y=10~30 的**真昵称**（李四）也判成「在内容块里」挡掉了。

三个候选做法，在这张真实图上比一比：
  A 第一段连续长内容（现状，已知会中头像的招）
  B 同样的办法，但**只看 x >= 左边距**之后的列
  C 非背景像素里**面积最大的连通域**（头像是个小 blobs，图片/气泡大得多）
"""

import sys

sys.path.insert(0, ".")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np                            # noqa: E402
from PIL import Image                         # noqa: E402

BG_MARGIN = 8
DIFF_THRESH = 45
MIN_CONTENT_H = 24

CASES = [
    ("row_图片_504.png", "真昵称「李四」y≈10~30 + 头像 + 表格截图", 40),
    ("row_图片_352.png", "只有一截图片（被钳过）", None),
]


def bg_of(a):
    m = BG_MARGIN
    if a.shape[1] < 4 * m:
        return None
    left = a[:, :m].reshape(-1, 3).mean(axis=0)
    right = a[:, -m:].reshape(-1, 3).mean(axis=0)
    return (left + right) / 2.0


def mask_of(a, bg):
    return np.abs(a - bg).sum(axis=2) > DIFF_THRESH


def method_a(mask, min_h=MIN_CONTENT_H):
    has = mask.sum(axis=1) > 2
    start, run = None, 0
    for y in range(has.shape[0]):
        if has[y]:
            if start is None:
                start = y
            run += 1
            if run >= min_h:
                return int(start)
        else:
            start, run = None, 0
    return None


def method_b(mask, gutter, min_h=MIN_CONTENT_H):
    sub = mask[:, gutter:]
    has = sub.sum(axis=1) > 2
    start, run = None, 0
    for y in range(has.shape[0]):
        if has[y]:
            if start is None:
                start = y
            run += 1
            if run >= min_h:
                return int(start)
        else:
            start, run = None, 0
    return None


def method_c(mask):
    try:
        import cv2
    except ImportError:
        return None, "没有 cv2"
    n, _l, stats, _c = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
    if n <= 1:
        return None, "没有连通域"
    areas = stats[1:, cv2.CC_STAT_AREA]
    order = np.argsort(areas)[::-1][:4]
    tops = [(int(stats[i + 1, cv2.CC_STAT_TOP]),
             int(stats[i + 1, cv2.CC_STAT_LEFT]),
             int(stats[i + 1, cv2.CC_STAT_WIDTH]),
             int(stats[i + 1, cv2.CC_STAT_HEIGHT]),
             int(areas[i])) for i in order]
    return tops[0][0], tops


def main():
    for name, desc, expect in CASES:
        import os
        path = os.path.join("out", "nickname_fix", name)
        if not os.path.exists(path):
            print("跳过（没有 %s）" % path)
            continue
        img = Image.open(path).convert("RGB")
        a = np.asarray(img).astype(np.int16)
        bg = bg_of(a)
        mask = mask_of(a, bg)
        print("=" * 78)
        print("%s   %dx%d" % (name, img.width, img.height))
        print("   %s" % desc)
        print("   A 第一段连续长内容            -> %s" % method_a(mask))
        for g in (48, 60, 72, 90):
            pct = 100.0 * g / img.width
            print("   B 跳过左边 %-3d px (%.1f%%)      -> %s"
                  % (g, pct, method_b(mask, g)))
        top, tops = method_c(mask)
        print("   C 最大连通域                  -> %s" % top)
        if isinstance(tops, list):
            print("      前几大块 (top, left, w, h, area):")
            for t in tops:
                print("        %s" % (t,))
        print()
    print("=" * 78)
    print("判据：真昵称「李四」在 y≈10~30，所以正确的顶边必须 **> 30**。")
    print("      小于 30 的都会把真昵称误判成「在图里」。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
