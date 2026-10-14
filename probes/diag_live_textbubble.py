# -*- coding: utf-8 -*-
"""⚠️ **这个探针的判据不成立，别拿它的输出当证据**（2026-10-12 实测否掉）。

    python probes\diag_live_textbubble.py <截图.png>

本来想干的事：从每个 OCR 文字块向外扩到"和底色不同"为止，从而量出**微信真画出来的
气泡矩形**。**在这张浅色真机图上扩爆了** —— 得到的都是 640×535、801×801 这种
整片区域，而不是气泡。

原因是判据本身不成立：浅色下对方气泡和聊天底色只差 **36**，而扩边用的门槛是 12，
所以"被画过东西"这个条件**在整个聊天区里几乎处处为真**，压根没有干净的边界可停。
→ 结论：**浅色下"气泡边界"不能靠逐像素色差去找**。

它记录下来的唯一有用信息是「每一个 OCR 文字块的中心坐标和它周围的主色都是
`(250,250,250)`」—— 也就是**文字周围那一圈就是聊天底色**，看不到气泡填充区。
真正的证据在 `probes/diag_live_pipeline.py`（①~⑤）和
`probes/measure_live_read.py`（真 `read_chat` 报"消息 N 条"）。

⚠️ 保留这个文件是为了**别重复走这条路**，不是为了跑它。
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
    sub = np.asarray(a).astype(np.int32).reshape(-1, 3)
    vals, counts = np.unique(sub, axis=0, return_counts=True)
    i = int(np.argmax(counts))
    return tuple(int(v) for v in vals[i]), int(counts[i]), int(len(sub))


def dist(a, b):
    return sum(abs(int(x) - int(y)) for x, y in zip(a, b))


def grow(a, bg, cx, cy, base_tol=12, frac=0.6, limit=400):
    """从 (cx,cy) 向四边外扩，找到"被画过东西"的那块矩形。"""
    H, W = a.shape[:2]
    d = np.abs(a - np.asarray(bg, dtype=np.int16)).sum(axis=2)
    on = d > base_tol

    def row_on(y):
        return float(on[y].mean())

    def col_on(x):
        return float(on[:, x].mean())

    x1 = x2 = cx
    while x1 > 0 and col_on(x1 - 1) > 0 and (cx - x1) < limit:
        x1 -= 1
    while x2 < W - 1 and col_on(x2 + 1) > 0 and (x2 - cx) < limit:
        x2 += 1
    y1 = y2 = cy
    while y1 > 0 and row_on(y1 - 1) > 0 and (cy - y1) < limit:
        y1 -= 1
    while y2 < H - 1 and row_on(y2 + 1) > 0 and (y2 - cy) < limit:
        y2 += 1
    return (x1, y1, x2, y2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--scale", type=float, default=1.54)
    args = ap.parse_args()

    cfg = load_config()
    img = Image.open(args.image).convert("RGB")
    W, H = img.size
    a = np.asarray(img).astype(np.int16)
    rd = R.OcrReader(cfg)
    x1, y1, x2, y2 = R.chat_rect((0, 0, W, H), args.scale, cfg, (W, H))
    bg = rd._bg(a, x1, x2, y1)
    print("图 %dx%d  聊天区 (%d,%d)-(%d,%d)  _bg() = %s"
          % (W, H, x1, y1, x2, y2, tuple(int(v) for v in bg),))

    cl = rd._crop_left(430)          # 用自动检测到的那个左边界（这次是真值）
    crop = (cl, y1, x2, 789)
    area = img.crop(crop)
    ca = a[crop[1]:crop[3], crop[0]:crop[2]]
    blocks = rd._recognize(area)
    print("裁剪框 %s   OCR %d 块" % (crop, len(blocks)))

    # 真气泡矩形在原图坐标里算（不裁，省得坐标系再错一次）
    print("\n%已经认出的文字块" if False else "")
    print("%-22s %-14s %-16s %-24s %s"
          % ("文字", "文字块中心", "真气泡矩形(图内)", "气泡填充色/与底色色差", "按 tol=45 会是盒吗"))
    print("-" * 118)
    for b in blocks:
        px = int(crop[0] + b.cx)
        py = int(crop[1] + b.cy)
        rx1, ry1, rx2, ry2 = grow(a, bg, px, py)
        sub = a[ry1:ry2 + 1, rx1:rx2 + 1]
        c, n, t = exact_mode(sub)
        # "按 tol=45 会不会成盒"：这块矩形里 45 以上的像素够不够连成 40x24
        d45 = (np.abs(a[ry1:ry2 + 1, rx1:rx2 + 1] - np.asarray(bg, dtype=np.int16))
               .sum(axis=2) > 45)
        w, h = rx2 - rx1 + 1, ry2 - ry1 + 1
        enough = w >= 40 and h >= 24
        frac45 = float(d45.mean())
        print("%-22s (%4d,%4d)   %4d×%-4d @(%4d,%4d)   %-16s %3d          %s"
              % ((b.text or "")[:20], px, py, w, h, rx1, ry1, str(c),
                 dist(c, bg),
                 ("可能（%d×%d 够大，但>45 的像素只占 %.1f%%）" % (w, h, frac45 * 100))
                 if enough else "**尺寸就不到 40x24**"))

    print("""
怎么读：
  · 「真气泡矩形」是微信**实际画出来**的那块（判据只有 12 的色差，所以不会漏）。
  · 「气泡填充色/与底色色差」要是 **≤ 45**，那么按 `|像素-底色| > 45` 取连通域时，
    这块气泡**大部分像素根本不进 mask** —— 剩下的只有文字笔画（细、还会被
    40x24 的尺寸门槛滤掉）→ 于是它**永远不成盒**，它上面的文字块就落到 free 里。
  · 这就是"OCR 认得出、消息 0 条"的完整因果链。
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
