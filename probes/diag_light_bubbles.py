# -*- coding: utf-8 -*-
"""诊断：**浅色模式下气泡切分到底量到了什么**（纯离线、只读，不需要微信在线）。

    python probes/diag_light_bubbles.py out/wechat_settings_light_20260927_155235.png
    python probes/diag_light_bubbles.py <任意窗口截图> [--scale 1.5]

它回答的是 CLAUDE.md 里那条「浅色模式下 OCR 读不到正文（未修）」的**根因定位**：

  ① 聊天区底色 `_bg()` 量出来是多少
  ② 气泡填充色（对方气泡）和底色的**曼哈顿色差**是多少，和 `bubble_boxes(tol=45)`
     这条阈值差多少 —— **差多少就是这条判据有没有信号**
  ③ 逐块打印 `bubble_boxes()` 的实际返回（尺寸 + 盒内主色 + 盒内非底色占比）
  ④ 顺便跑一次 `read_chat()`，看它到底返回 None 还是"消息 0 条"

⚠️ 纯只读：只读一张已经存在的 PNG，不抓图、不碰微信、不调 OCR（省时间）。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np  # noqa: E402

from wechat_assistant import ocr_reader as R  # noqa: E402
from wechat_assistant.config import load_config  # noqa: E402


def modal(a, q=8):
    """一块区域的众数颜色 + 占比（和产品 `_bg` 同一套量化）。"""
    if a.size == 0:
        return None, 0.0
    sub = a.astype(np.int32)
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
    ap.add_argument("--scale", type=float, default=None)
    ap.add_argument("--tol", type=int, default=45, help="bubble_boxes 的阈值")
    args = ap.parse_args()

    from PIL import Image
    img = Image.open(args.image).convert("RGB")
    cfg = load_config()
    W, H = img.width, img.height
    scale = args.scale if args.scale else 1.5

    rd = R.OcrReader(cfg)
    x1, y1, x2, y2 = R.chat_rect((0, 0, W, H), scale, cfg, (W, H))
    a = np.asarray(img).astype(np.int16)

    print("图 %s  %dx%d  假定缩放 %.2f" % (os.path.basename(args.image), W, H, scale))
    print("聊天区（按配置 chat_left） = (%d,%d)-(%d,%d)" % (x1, y1, x2, y2))

    # ---- ① 底色：按产品那条路算（x1 起）
    bg = rd._bg(a, x1, x2, y1)
    bg_show = None if bg is None else tuple(int(v) for v in bg)
    print("\n① 聊天区底色 _bg() = %s" % (bg_show,))
    if bg is None:
        return 1

    # 全聊天区的众数（含气泡）作对照
    full_col, full_share = modal(a[y1:y2, x1:x2])
    print("   整块聊天区众数 = %s（占 %.1f%%）" % (full_col, full_share * 100))

    # ---- ② 逐列/逐行看：聊天区里"和底色不同"的像素占比
    ca = a[y1:y2, x1:x2]
    d = np.abs(ca - np.asarray(bg, dtype=np.int16)).sum(axis=2)
    for tol in (8, 18, 30, 45, 60):
        frac = float((d > tol).mean())
        print("   色差 > %-3d 的像素占聊天区 %.3f%%" % (tol, frac * 100))

    # ---- ③ bubble_boxes 实际返回
    boxes = R.bubble_boxes(ca, bg, tol=args.tol)
    print("\n③ bubble_boxes(tol=%d) 返回 %d 个盒子" % (args.tol, len(boxes)))
    for i, (bx1, by1, bx2, by2) in enumerate(boxes):
        sub = ca[by1:by2, bx1:bx2]
        col, share = modal(sub)
        nonbg = float((np.abs(sub - np.asarray(bg, dtype=np.int16)).sum(axis=2)
                       > args.tol).mean())
        print("   #%-2d %4d×%-4d @(%4d,%4d)  盒内主色 %-16s 占%5.1f%%  "
              "非底色 %5.1f%%  盒主色与底色色差 = %s"
              % (i, bx2 - bx1, by2 - by1, bx1, by1, str(col), share * 100,
                 nonbg * 100, dist(col, bg)))

    # ---- ④ 分左右：贴左边的盒子（对方）和贴右边的盒子（我）
    if boxes:
        left = [b for b in boxes if R.side_of_box(b, ca.shape[1]) is False]
        right = [b for b in boxes if R.side_of_box(b, ca.shape[1]) is True]
        print("\n   贴左（对方）%d 个，贴右（我）%d 个" % (len(left), len(right)))
        for tag, group in (("对方", left), ("我", right)):
            for b in group:
                sub = ca[b[1]:b[3], b[0]:b[2]]
                col, share = modal(sub)
                print("     %s %4d×%-4d 主色 %-16s 占%5.1f%% 与底色色差 %s"
                      % (tag, b[2] - b[0], b[3] - b[1], str(col),
                         share * 100, dist(col, bg)))

    # ---- ⑤ 跑一次真 read_chat（不给它 OCR，只看到哪一步返回 None）
    print("\n⑤ read_chat 的判定链（不跑 OCR，只看几何那一步过不过）:")
    cands = list(rd._geometry_candidates(a, bg, x1))
    print("   几何候选 = %s" % cands)
    for bx1 in cands:
        cl = rd._crop_left(bx1)
        bs = R.bubble_boxes(a[y1:y1 + max(1, (y2 - y1) // 3), cl:x2], bg,
                            tol=args.tol)
        print("   bx1=%-4d（裁剪左 %d）在聊天区上 1/3 里认出 %d 个气泡盒"
              "  -> %s" % (bx1, cl, len(bs),
                           "通过" if bs else "**这一档过不了（会返回 None）**"))

    print("""
怎么读：
  · 「整块聊天区众数」== 底色 -> 聊天区大面积是底色（正常）。
  · ③ 里若**对方那侧的盒子一个都没有**、而贴右（我自己发的绿气泡）有 ——
    就是"对方气泡填充色 == 底色"这条：连通域判据对它是**零信号**。
  · 判据差多少看②：色差 0 就是同一个色，任何 tol 都分不开（除了文字笔画）。
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
