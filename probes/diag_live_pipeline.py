# -*- coding: utf-8 -*-
"""把 `read_chat` 的**每一步中间量**打出来（真机截图 + 真产品函数）。

    python probes\diag_live_pipeline.py <截图.png> [--scale 1.54] [--tol 45]

打的东西（顺序就是产品里的顺序）：
  ① 几何候选、最终采用的左边界、裁剪框
  ② `_bg()` 的值
  ③ `bubble_boxes()` 的**每一个盒**（尺寸/位置/盒内精确主色/与底色色差）
     —— 特别标出「主色 == 底色」的**假盒子**（那种盒子会把整片区域吃进去）
  ④ OCR 认出的块 + **每块落到哪个盒里**、或者"落在所有盒之外（free）"
  ⑤ 按盒子装出来的 rows（每条消息）和 free 块的处理结果

⚠️ 只读一张 PNG，不碰微信。目的：**别再猜**是哪一步丢的。
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
    rd = R.OcrReader(cfg)
    scale = args.scale

    x1, y1, x2, _ = R.chat_rect((0, 0, W, H), scale, cfg, (W, H))
    bg = rd._bg(a, x1, x2, y1)
    print("== ① 几何 ==")
    print("   配置 chat_left -> %d 物理；候选 = %s"
          % (x1, list(rd._geometry_candidates(a, bg, x1))))
    bg = rd._bg(a, x1, x2, y1)
    input_top = rd._input_top(a, bg, H, x1, x2, y1)
    print("   输入区上边 input_top = %s（配置兜底 = H - input_fallback*scale）"
          % input_top)
    print("== ② 底色 == _bg() = %s" % (tuple(int(v) for v in bg),))

    for bx1 in rd._geometry_candidates(a, bg, x1):
        cl = rd._crop_left(bx1)
        crop = (cl, y1, x2, input_top)
        area = img.crop(crop)
        boxes = R.bubble_boxes(a[y1:input_top, cl:x2], bg, tol=args.tol)
        print("\n== ③ bx1=%d（裁剪左 %d）裁剪框 %s ==" % (bx1, cl, crop))
        print("   bubble_boxes(tol=%d) -> %d 个盒" % (args.tol, len(boxes)))
        for i, b in enumerate(boxes):
            sub = a[y1 + b[1]:y1 + b[3], cl + b[0]:cl + b[2]]
            c, n, t = exact_mode(sub)
            fake = dist(c, bg) <= 8
            print("     #%-2d %4d×%-4d @(%4d,%4d) 主色 %-16s 占%5.1f%% 与底色色差 %3d%s"
                  % (i, b[2] - b[0], b[3] - b[1], b[0], b[1], str(c),
                     n / t * 100, dist(c, bg),
                     "   ← ⚠️ **假盒子（主色就是底色）**" if fake else ""))

        blocks = rd._recognize(area)
        print("\n== ④ OCR（裁剪图 %dx%d）-> %d 块 ==" % (area.width, area.height,
                                                        len(blocks)))
        for b in blocks:
            hit = None
            for i, bx in enumerate(boxes):
                if bx[0] <= b.cx <= bx[2] and bx[1] <= b.cy <= bx[3]:
                    hit = i
                    break
            print("     %-22s cx=%-5.0f cy=%-5.0f %s"
                  % ((b.text or "")[:20], b.cx, b.cy,
                     ("-> 盒 #%d" % hit) if hit is not None else "-> **所有盒之外(free)**"))

        rows, free = R.rows_from_bubbles(blocks, boxes, area.width / 2.0,
                                         area.width)
        print("\n== ⑤ 装出来的 rows ==")
        if not rows:
            print("     **一个 row 都没有** -> read_chat 会 append 0 条消息")
        for r in rows:
            print("     y=%-4d x=%-4d..%-5d %-6s %r"
                  % (r.y1, r.x1, r.x2,
                     "我" if r.is_self else "对方", (r.text() or "")[:60]))
        print("     free 块 %d 个（时间分隔/昵称从这里来）" % len(free))
        for b in free:
            print("        %-22s cx=%-5.0f cy=%-5.0f 是时间=%s"
                  % ((b.text or "")[:20], b.cx, b.cy,
                     R.is_time_text((b.text or "").strip())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
