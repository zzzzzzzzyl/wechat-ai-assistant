# -*- coding: utf-8 -*-
"""扫一遍「图片尺寸 -> OCR 耗时」，看耗时到底由什么决定。

背景：实测出现**反直觉结果** —— 271x432 比 915x432 还慢。
怀疑 RapidOCR 的 det 预处理（config 里我们设的是 limit_type=max /
limit_side_len=736）并不是「只缩不放」，而是**把长边钉到 736**，
于是小图会被**放大**。如果成立，那 ocr.py 顶部那句
「改按长边限制，扁图不再被放大」对一个普通尺寸的图就是错的。

做法：拿一张真实内容图，缩放到一串尺寸，每个测 5 次取中位。

用法：python probes/probe_ocr_size_curve.py <png>
"""

import sys
import time

sys.path.insert(0, ".")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PIL import Image  # noqa: E402

SIZES = [
    (60, 95), (100, 158), (150, 238), (200, 317),
    (271, 432), (367, 579), (458, 724), (551, 870),
    (735, 1160), (900, 1420),
]


def bench(eng, img, n=5):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        eng.recognize(img)
        ts.append((time.perf_counter() - t0) * 1000)
    ts.sort()
    return ts[len(ts) // 2], ts[0], len(ts)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else r"out\image_capture\01_图片.png"
    from wechat_assistant.ocr import get_engine
    eng = get_engine()
    eng.load()
    eng.warmup()

    base = Image.open(path).convert("RGB")
    # 只取有内容的那块，避免大片纯背景
    base = base.crop((100, 40, 271, 432))

    print()
    print("limit_side_len = %s   limit_type = %s"
          % (eng.params["limit_side_len"], eng.params["limit_type"]))
    print()
    print("%-12s %-12s %10s %10s   %s"
          % ("输入尺寸", "长边", "中位 ms", "最快 ms", "推测模型实际看到的尺寸"))
    for w, h in SIZES:
        im = base.resize((w, h), Image.BICUBIC)
        med, fast, _ = bench(eng, im)
        # 按「长边钉到 736」的假设推算
        r = 736.0 / max(w, h)
        gw, gh = int(round(w * r)), int(round(h * r))
        print("%-12s %-12d %10.1f %10.1f   %dx%d  (%.0f kpx)"
              % ("%dx%d" % (w, h), max(w, h), med, fast, gw, gh, gw * gh / 1000.0))
    print()
    print("判据：如果耗时跟着**最后一列的 kpx** 走（而不是跟着输入尺寸），")
    print("      那「长边被钉到 736」的假设成立 —— 小于 736 的图会被放大。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
