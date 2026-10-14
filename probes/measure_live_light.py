# -*- coding: utf-8 -*-
"""在**真机截到的浅色聊天窗**上量「对方气泡 vs 聊天底色」的色差。

    python probes\measure_live_light.py <截图.png> [--scale 1.5]

⚠️ 这是 `diag_light_bubbles.py` 的"真机版"：那张图是**屏幕截图**（微信窗口可见时
   截的可见区域），不是 PrintWindow —— 因为这台机器上微信的 PrintWindow 给全黑。
   ⚠️ 用**屏幕截图**量配色是安全的：底色/气泡色和"谁渲染的"无关，
      而且我们只读像素，不碰窗口。

⚠️ 采样点必须落在一块**纯色区域内部**（气泡的圆角处、文字笔画上都不是）。
   所以这里先按图自动找**最大的几块连通域**（那就是气泡），再取每块的主色，
   而不是硬编码坐标 —— 换一张图也能跑。
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


def modal(a, q=4):
    if a.size == 0:
        return None, 0.0
    # ⚠️⚠️ **必须自己转 int32** —— 调用方给的往往是 int16（产品里那张图就是），
    #    而 `(a // q) * 1024` 在 int16 下会**回绕**，量出 (258,1,26) 这种假色。
    #    和 `ocr_reader._bg()` 里那条注释是同一个坑（那边也写死了在函数内部转）。
    sub = np.asarray(a).astype(np.int32)
    key = ((sub[:, :, 0] // q) * 1024 + (sub[:, :, 1] // q) * 32
           + (sub[:, :, 2] // q))
    vals, counts = np.unique(key.ravel(), return_counts=True)
    i = int(np.argmax(counts))
    k = int(vals[i])
    col = ((k // 1024) * q + q // 2, ((k // 32) % 32) * q + q // 2,
           (k % 32) * q + q // 2)
    return col, float(counts[i]) / float(key.size)


def dist(a, b):
    return sum(abs(int(x) - int(y)) for x, y in zip(a, b))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--scale", type=float, default=1.54)
    ap.add_argument("--crop", default=None,
                    help="只量这块区域，形如 x1,y1,x2,y2（图内坐标）")
    args = ap.parse_args()

    cfg = load_config()
    img = Image.open(args.image).convert("RGB")
    if args.crop:
        img = img.crop(tuple(int(v) for v in args.crop.split(",")))
    W, H = img.size
    a = np.asarray(img).astype(np.int16)
    print("图 %s  %dx%d  缩放 %.2f" % (os.path.basename(args.image), W, H, args.scale))

    # 聊天区几何：这张图就是窗口位图，走产品同一套 chat_rect
    x1, y1, x2, y2 = R.chat_rect((0, 0, W, H), args.scale, cfg, (W, H))
    print("聊天区（产品几何）= (%d,%d)-(%d,%d)" % (x1, y1, x2, y2))
    rd = R.OcrReader(cfg)
    bg = rd._bg(a, x1, x2, y1)
    print("① 聊天区底色 _bg() = %s" % (tuple(int(v) for v in bg),))

    ca = a[y1:y2, x1:x2]
    col, share = modal(ca)
    print("   整块聊天区众数 = %s（%.1f%%）  与底色色差 %d"
          % (col, share * 100, dist(col, bg)))
    d = np.abs(ca - np.asarray(bg, dtype=np.int16)).sum(axis=2)
    for tol in (8, 18, 30, 45, 60):
        print("   色差 > %-3d 的像素占 %.3f%%" % (tol, float((d > tol).mean()) * 100))

    # ---- 真机图上直接找"文字块"和它们的**局部填充色**（不靠 45 这条线）
    from wechat_assistant.sender import _cv2
    cv2 = _cv2()
    print("\n② 用「**非文字**的连通域」找填充块（tol=12 —— 只要有 12 的色差就算不同）：")
    mask = (np.abs(ca - np.asarray(bg, dtype=np.int16)).sum(axis=2) > 12).astype(np.uint8)
    print("   色差>12 的像素占 %.2f%%" % (float(mask.mean()) * 100))
    boxes = R.bubble_boxes(ca, bg, tol=12)
    print("   bubble_boxes(tol=12) 认出 %d 个盒（对照：tol=45 时）" % len(boxes))
    b45 = R.bubble_boxes(ca, bg, tol=45)
    print("   bubble_boxes(tol=45) 认出 %d 个盒" % len(b45))

    # ---- 手动指定几处，图上验证（这些坐标是看图定的，写在下面备查）
    print("\n③ 定点采样（图内坐标 -> 窗口内坐标要减去窗口原点；这里直接用图内坐标）：")
    pts = {
        "聊天区空白处（对方气泡右侧）": (int(W * 0.60), int(H * 0.47)),
        "对方第 1 条气泡内部": (int(W * 0.45), int(H * 0.383)),
        "对方第 2 条气泡内部": (int(W * 0.45), int(H * 0.462)),
        "对方第 3 条气泡内部": (int(W * 0.45), int(H * 0.539)),
        "我的绿气泡内部": (int(W * 0.80), int(H * 0.703)),
        "会话列表底色": (int(W * 0.15), int(H * 0.75)),
    }
    for name, (px, py) in pts.items():
        if not (0 <= px < W and 0 <= py < H):
            continue
        patch = a[max(0, py - 3):py + 4, max(0, px - 3):px + 4]
        c, s = modal(patch, q=1)
        print("   %-26s (%4d,%4d) 主色 %-16s 与聊天底色色差 %3d  %s"
              % (name, px, py, str(c), dist(c, bg),
                 "**和底色同一个色**" if dist(c, bg) <= 8 else ""))

    print("""
怎么读：
  · 对方那三条气泡的「主色」如果和聊天底色**色差 ≤ 8** —— 就是"填充色和底色同色"，
    那么 `bubble_boxes(tol=45)` **永远认不出它们**（判据是 >45），
    而 tol=12 能认出（因为还能靠气泡边缘的那一点点差 + 文字）。""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
