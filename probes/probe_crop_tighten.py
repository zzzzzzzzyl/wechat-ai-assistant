# -*- coding: utf-8 -*-
"""把消息 rect 收紧到「图片本体」—— 排除头像、群昵称、聊天区底色。

为什么必须做（实测）：
`ChatBubbleReferItemView` 的 rect 是**整行**。直接裁下来跑 OCR，
群昵称会被当成「图片里的文字」识别出来：

    rect=915x432 -> OCR 出 7 块，其中第一块是 `群友甲`  ← 那是群昵称，不是图里的字

而且 70% 的面积是聊天区底色，白跑。

做法：非背景像素的掩码 -> 连通域 -> 取**面积最大**的那一块。
用 cv2（项目里已经装了 opencv）；没装就退回「列投影找最宽连续段」。

用法：python probes/probe_crop_tighten.py <png> [--ascii]
"""

import sys

sys.path.insert(0, ".")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np                            # noqa: E402
from PIL import Image                         # noqa: E402


def background_color(a):
    """取四条边里出现最多的颜色当背景。"""
    h, w, _ = a.shape
    edge = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    colors, counts = np.unique(edge.reshape(-1, 3), axis=0, return_counts=True)
    return colors[counts.argmax()]


def largest_blob_bbox(img, tol=30, pad=4):
    """非背景像素里**面积最大**的连通域的外接矩形。"""
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    bg = background_color(a)
    mask = (np.abs(a - bg).sum(axis=2) > tol).astype(np.uint8)
    if mask.sum() == 0:
        return None
    try:
        import cv2
        n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        if n <= 1:
            return None
        # 0 是背景；比面积
        idx = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        x, y = int(stats[idx, cv2.CC_STAT_LEFT]), int(stats[idx, cv2.CC_STAT_TOP])
        bw = int(stats[idx, cv2.CC_STAT_WIDTH])
        bh = int(stats[idx, cv2.CC_STAT_HEIGHT])
        method = "cv2 连通域"
    except ImportError:
        # 退回：列投影找最宽的连续有内容段
        cols = mask.sum(axis=0) > 0
        best = (0, 0)
        i = 0
        while i < len(cols):
            if cols[i]:
                j = i
                while j < len(cols) and cols[j]:
                    j += 1
                if j - i > best[1] - best[0]:
                    best = (i, j)
                i = j
            else:
                i += 1
        if best[1] - best[0] == 0:
            return None
        sub = mask[:, best[0]:best[1]]
        rows = np.where(sub.sum(axis=1) > 0)[0]
        x, y, bw, bh = best[0], int(rows.min()), best[1] - best[0], int(rows.max() - rows.min() + 1)
        method = "列投影"
    x1 = max(0, x - pad)
    y1 = max(0, y - pad)
    x2 = min(img.width, x + bw + pad)
    y2 = min(img.height, y + bh + pad)
    return (x1, y1, x2, y2), method


def main():
    if len(sys.argv) < 2:
        print("用法: python probe_crop_tighten.py <png>")
        return 1
    path = sys.argv[1]
    img = Image.open(path).convert("RGB")
    print("原 rect 裁出的 %dx%d（%.0f kpx）" % (img.width, img.height, img.width * img.height / 1000.0))

    r = largest_blob_bbox(img)
    if r is None:
        print("找不到内容块")
        return 1
    box, method = r
    tight = img.crop(box)
    print("收紧后 %s -> %dx%d（%.0f kpx）  面积降到 %.0f%%   判据=%s"
          % (str(box), tight.width, tight.height, tight.width * tight.height / 1000.0,
             100.0 * (tight.width * tight.height) / (img.width * img.height), method))

    from wechat_assistant.ocr import get_engine
    eng = get_engine()
    eng.load()
    eng.warmup()
    import time
    for tag, im in [("原始 rect", img), ("收紧后", tight)]:
        t0 = time.perf_counter()
        res = eng.recognize(im)
        ms = (time.perf_counter() - t0) * 1000
        print()
        print("%s  %dx%d   OCR %.0f ms，%d 块" % (tag, im.width, im.height, ms, len(res)))
        for x in res:
            print("     %-8s (%.2f)  %s" % ("%d,%d" % (x["x1"], x["y1"]), x["score"], x["text"]))

    if "--ascii" in sys.argv:
        sys.path.insert(0, "probes")
        from ascii_shot import render
        print()
        render(tight, 88)
    return 0


if __name__ == "__main__":
    sys.exit(main())
