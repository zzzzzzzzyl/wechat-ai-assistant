# -*- coding: utf-8 -*-
"""真机浅色截图上跑**产品代码**，看每个气泡盒认不认得出、以及为什么。

    python probes\measure_live_bubbles.py <截图.png> [--scale 1.54]

和 `measure_live_light.py` 的区别：那个是"定点采样看色差"，这个是
**按产品的顺序把整条链路跑一遍**（`_bg` → `bubble_boxes` → `rows_from_bubbles`），
并且把「对方的灰气泡」和「我的绿气泡」分开统计 —— 这是这次要回答的问题：
**到底是一类认不出，还是两类都认不出。**

⚠️ 纯只读：只读一张 PNG。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from wechat_assistant import ocr_reader as R  # noqa: E402
from wechat_assistant.config import load_config  # noqa: E402


def exact_mode(a, q=1):
    """最精确的众数（q=1 = 不量化），int32 内部转换（防 int16 回绕）。"""
    sub = np.asarray(a).astype(np.int32)
    flat = sub.reshape(-1, 3)
    vals, counts = np.unique(flat, axis=0, return_counts=True)
    i = int(np.argmax(counts))
    return tuple(int(v) for v in vals[i]), int(counts[i]), int(len(flat))


def dist(a, b):
    return sum(abs(int(x) - int(y)) for x, y in zip(a, b))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--scale", type=float, default=1.54)
    ap.add_argument("--tol", type=int, default=45)
    args = ap.parse_args()

    cfg = load_config()
    img = Image.open(args.image).convert("RGB")
    W, H = img.size
    a = np.asarray(img).astype(np.int16)
    x1, y1, x2, y2 = R.chat_rect((0, 0, W, H), args.scale, cfg, (W, H))
    ca = a[y1:y2, x1:x2]
    rd = R.OcrReader(cfg)
    bg = rd._bg(a, x1, x2, y1)
    print("图 %s %dx%d   聊天区=(%d,%d)-(%d,%d)"
          % (os.path.basename(args.image), W, H, x1, y1, x2, y2))
    print("① _bg() 返回 = %s（众数量化 q=8，±4）" % (tuple(int(v) for v in bg),))

    # 聊天区右半边（对方气泡之外的那片空白）的精确众数 = 真正的"聊天底色"
    blank = ca[:, int(ca.shape[1] * 0.62):]
    c, n, tot = exact_mode(blank)
    print("   右半边空白区精确众数 = %s（%d/%d = %.1f%%）"
          % (c, n, tot, n / tot * 100))
    print("   → `_bg()` 与它的差 = %d（阈值判据用的是 `_bg()` 那个值）" % dist(bg, c))

    # 对方第 1 条气泡内部（图内 (571,359) -> 聊天区内 (571-x1, 359-y1)）
    for name, (px, py) in (("对方气泡内部", (571, 359)),
                           ("我的绿气泡内部", (1016, 660))):
        cx, cy = px - x1, py - y1
        if 0 <= cx < ca.shape[1] and 0 <= cy < ca.shape[0]:
            patch = ca[cy - 3:cy + 4, cx - 3:cx + 4]
            c2, n2, t2 = exact_mode(patch)
            print("   %s 精确色 %s   与 _bg() 色差 = **%d**  （tol=%d -> %s）"
                  % (name, c2, dist(c2, bg), args.tol,
                     "认得出" if dist(c2, bg) > args.tol else "**认不出（低于阈值）**"))

    print("\n② 各档 tol 下 bubble_boxes 认出几个盒：")
    for tol in (12, 20, 30, 36, 40, 45, 50, 56):
        bs = R.bubble_boxes(ca, bg, tol=tol)
        lefts = [b for b in bs if not R.side_of_box(b, ca.shape[1])]
        rights = [b for b in bs if R.side_of_box(b, ca.shape[1])]
        print("   tol=%-3d 共 %2d 个（贴左/对方 %d，贴右/我 %d）"
              % (tol, len(bs), len(lefts), len(rights)))

    print("\n③ tol=%d 每个盒的明细（尺寸 + 盒内精确主色 + 与 _bg() 色差）："
          % args.tol)
    for i, b in enumerate(R.bubble_boxes(ca, bg, tol=args.tol)):
        sub = ca[b[1]:b[3], b[0]:b[2]]
        c3, n3, t3 = exact_mode(sub)
        side = "我  " if R.side_of_box(b, ca.shape[1]) else "对方"
        w, h = b[2] - b[0], b[3] - b[1]
        print("   #%-2d %s %4d×%-4d @(%4d,%4d) 主色 %-16s 占%5.1f%% 与底色色差 %3d"
              % (i, side, w, h, b[0], b[1], str(c3), n3 / t3 * 100, dist(c3, bg)))

    print("""
结论怎么读：
  · 「对方气泡内部」的色差如果 ≤ tol -> 那类气泡**当帧就没了**（连通域判据是
     `|像素-底色| > tol`，低于阈值 = 和背景同色）。
  · ② 里 tol 降到 36 左右会突然多出一批**贴左**的盒 —— 那就是对方的气泡；
    但它们靠的是"36"这么薄的余量，换台机器/换个显示器就会翻。
  · 真要把它们切出来，判据得换成**文字块的归并**，不是继续调这个数。
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
