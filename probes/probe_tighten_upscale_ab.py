# -*- coding: utf-8 -*-
"""A/B：收紧之后**要不要放大**？放大是帮忙还是帮倒忙？

背景：实测放大到长边 736 能多认出一行小字（probe_ocr_upscale.py ③组），
      但也观察到密集截图的识别结果里出现乱码。这两个观察需要分开验证。

做法：同一张真实图 -> 收紧 -> 分成「原生 / 放大到 736 / 放大到 2 倍」
      各 OCR 一次，把文字和时间并排打出来。

用法：python probes/probe_tighten_upscale_ab.py <rect裁出来的png>
"""

import sys
import time

sys.path.insert(0, ".")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PIL import Image  # noqa: E402

sys.path.insert(0, "probes")
from demo_image_ocr import largest_blob_bbox  # noqa: E402

MAX_SIDE = 736


def run(eng, im, tag):
    t0 = time.perf_counter()
    res = eng.recognize(im)
    ms = (time.perf_counter() - t0) * 1000
    good = [r for r in res if len(r["text"].strip()) >= 2]
    print()
    print("%-22s %-10s %7.0f ms  %2d 块" % (tag, "%dx%d" % im.size, ms, len(res)))
    for r in res:
        flag = "" if len(r["text"].strip()) >= 2 else "  <- 太短，会被丢掉"
        print("      (%.2f) %s%s" % (r["score"], r["text"], flag))
    return ms, len([r for r in res if len(r["text"].strip()) >= 2 and r["score"] >= 0.6])


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else r"out\demo_image_ocr\01_rect.png"
    from wechat_assistant.ocr import get_engine
    eng = get_engine()
    eng.load()
    eng.warmup()

    raw = Image.open(path).convert("RGB")
    print("rect 裁出来 %dx%d" % raw.size)
    box = largest_blob_bbox(raw)
    tight = raw.crop(box) if box else raw
    print("收紧后   %s -> %dx%d" % (str(box), tight.width, tight.height))

    variants = [("原生", tight)]
    k = MAX_SIDE / float(max(tight.width, tight.height))
    if k > 1:
        variants.append(("放大到长边736", tight.resize(
            (int(tight.width * k), int(tight.height * k)), Image.BICUBIC)))
    variants.append(("放大 2 倍", tight.resize(
        (tight.width * 2, tight.height * 2), Image.BICUBIC)))
    variants.append(("缩小到 50%", tight.resize(
        (max(1, tight.width // 2), max(1, tight.height // 2)), Image.BICUBIC)))

    stats = []
    for tag, im in variants:
        ms, n = run(eng, im, tag)
        stats.append((tag, "%dx%d" % im.size, ms, n))

    print()
    print("=" * 62)
    print("%-20s %-12s %10s %8s" % ("方案", "尺寸", "耗时 ms", "有效块"))
    for s in stats:
        print("%-20s %-12s %10.0f %8d" % s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
