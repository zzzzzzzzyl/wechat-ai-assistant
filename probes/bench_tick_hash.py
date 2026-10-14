# -*- coding: utf-8 -*-
"""量「记录器一拍里做图片去重」要花多少时间。

背景：照片 OCR 要 0.3~1.3 秒，绝不能放在拍里。但**去重**（算出内容哈希、
判断要不要入队）必须放在拍里 —— 否则同一张图会被重复入队。

所以要知道：clamp + 裁剪 + 收紧 + dHash 这一串，在真实尺寸下要多少毫秒。

分两段量：
  A 段 纯 dHash（拿已经裁好的图）—— 如果这里就很慢，说明方案要换
  B 段 收紧（找最大连通域）—— 用 cv2 和「列投影」两种实现对比
  C 段 端到端：从整窗位图算出一个哈希

用法：python probes/bench_tick_hash.py [rect裁出来的png]
"""

import sys
import time

sys.path.insert(0, ".")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np                     # noqa: E402
from PIL import Image                  # noqa: E402


def dhash(img, size=8):
    g = img.convert("L").resize((size + 1, size))
    px = list(g.getdata())
    bits = 0
    for y in range(size):
        row = y * (size + 1)
        for x in range(size):
            bits = (bits << 1) | (1 if px[row + x] > px[row + x + 1] else 0)
    return bits


def dhash_np(img, size=8):
    """numpy 版 dHash —— 统计用，看值不值得换掉纯 Python 版。"""
    g = np.asarray(img.convert("L").resize((size + 1, size)), dtype=np.int16)
    bits = (g[:, :-1] > g[:, 1:]).flatten()
    out = 0
    for b in bits:
        out = (out << 1) | int(b)
    return out


def bg_mask(img, tol=30):
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    edge = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    colors, counts = np.unique(edge.reshape(-1, 3), axis=0, return_counts=True)
    bg = colors[counts.argmax()]
    return (np.abs(a - bg).sum(axis=2) > tol).astype(np.uint8)


def tighten_cv2(mask):
    import cv2
    n, _l, stats, _c = cv2.connectedComponentsWithStats(mask, 8)
    if n <= 1:
        return None
    idx = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    return (int(stats[idx, cv2.CC_STAT_LEFT]), int(stats[idx, cv2.CC_STAT_TOP]),
            int(stats[idx, cv2.CC_STAT_WIDTH]), int(stats[idx, cv2.CC_STAT_HEIGHT]))


def tighten_proj(mask):
    cols = mask.sum(axis=0) > 0
    best, i = (0, 0), 0
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
    return (best[0], int(rows.min()), best[1] - best[0],
            int(rows.max() - rows.min() + 1))


def timeit(fn, n=20):
    fn()                       # 预热
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000)
    ts.sort()
    return ts[len(ts) // 2]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else r"out\demo_image_ocr\01_rect.png"
    img = Image.open(path).convert("RGB")
    print("测试图 %dx%d" % img.size)
    print()

    print("A 段：dHash 本身")
    print("   纯 Python 版           %8.2f ms" % timeit(lambda: dhash(img)))
    print("   numpy 版               %8.2f ms" % timeit(lambda: dhash_np(img)))
    tiny = img.resize((200, 300))
    print("   (收紧后典型尺寸 %dx%d)" % tiny.size)
    print("   纯 Python 版           %8.2f ms" % timeit(lambda: dhash(tiny)))
    print("   numpy 版               %8.2f ms" % timeit(lambda: dhash_np(tiny)))

    print()
    print("B 段：找背景 + 连通域（收紧）")
    print("   bg_mask (numpy)        %8.2f ms" % timeit(lambda: bg_mask(img)))
    mask = bg_mask(img)
    print("   cv2 连通域             %8.2f ms" % timeit(lambda: tighten_cv2(mask)))
    print("   列投影                 %8.2f ms" % timeit(lambda: tighten_proj(mask)))

    print()
    print("C 段：端到端（背景+掩码+收紧+dHash）")
    def end2end_cv2():
        m = bg_mask(img)
        b = tighten_cv2(m)
        return dhash_np(img.crop((b[0], b[1], b[0] + b[2], b[1] + b[3])) if b else img)
    def end2end_proj():
        m = bg_mask(img)
        b = tighten_proj(m)
        return dhash_np(img.crop((b[0], b[1], b[0] + b[2], b[1] + b[3])) if b else img)
    print("   cv2 版                 %8.2f ms" % timeit(end2end_cv2))
    print("   列投影版               %8.2f ms" % timeit(end2end_proj))
    print()
    print("   两者结果是否一致: %s" % (end2end_cv2() == end2end_proj()))

    print()
    print("D 段：降采样后再算掩码（**找框不需要像素级精度**）")
    for maxside in [128, 192, 256, 384]:
        def opt(maxside=maxside):
            k = maxside / float(max(img.width, img.height))
            small = img if k >= 1 else img.resize(
                (max(1, int(img.width * k)), max(1, int(img.height * k))), Image.BILINEAR)
            m = bg_mask(small)
            b = tighten_cv2(m)
            if not b:
                return dhash_np(img)
            inv = 1.0 / k
            b = (int(b[0] * inv), int(b[1] * inv),
                 max(1, int(b[2] * inv)), max(1, int(b[3] * inv)))
            return dhash_np(img.crop((b[0], b[1], b[0] + b[2], b[1] + b[3])))
        ms = timeit(opt)
        same = (opt() == end2end_cv2())
        print("   降采样到长边 %-4d      %8.2f ms   与全分辨率结果一致: %s"
              % (maxside, ms, same))

    print()
    print("对照：记录器一拍本身约 100ms（读 60~90 + 像素判定 45）")
    print("      快速档间隔 200ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
