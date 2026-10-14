# -*- coding: utf-8 -*-
"""干净地测：OCR 耗时究竟由「画布大小」还是「内容多少」决定。

上一版 probe_ocr_size_curve.py 有缺陷 —— 把图缩小时**文字也糊掉了**，
det 检测不到文字自然就快，于是「图小=快」里混了两件事。

这版把两件事拆开：
  A 组：同一段文字，贴到不同大小的画布上（内容恒定，画布变）→ 测画布的影响
  B 组：纯噪声图，不同大小，**没有任何文字**（内容=0，画布变）→ 纯 det 预处理成本
  C 组：真实图片，原尺寸 vs 缩到 X% → 直接回答「值不值得先缩小」

用法：python probes/probe_ocr_canvas.py <png>
"""

import sys
import time

sys.path.insert(0, ".")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np                      # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402


def bench(eng, img, n=5):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        res = eng.recognize(img)
        ts.append((time.perf_counter() - t0) * 1000)
    ts.sort()
    return ts[len(ts) // 2], len(res)


def text_canvas(w, h):
    """白底 + 固定 3 行字，画布大小可变。"""
    im = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(im)
    try:
        f = ImageFont.truetype("msyh.ttc", 22)
    except Exception:
        f = ImageFont.load_default()
    for i, s in enumerate(["灵感出炉", "我的DIY喜茶", "立即喝一杯"]):
        d.text((10, 10 + i * 30), s, fill=(0, 0, 0), font=f)
    return im


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else r"out\image_capture\01_图片.png"
    from wechat_assistant.ocr import get_engine
    eng = get_engine()
    eng.load()
    eng.warmup()

    print()
    print("limit_side_len=%s  limit_type=%s"
          % (eng.params["limit_side_len"], eng.params["limit_type"]))

    print()
    print("A 组：同一段文字，画布从小到大（内容恒定）")
    print("   %-14s %10s %8s" % ("画布", "中位 ms", "块数"))
    for w, h in [(200, 120), (400, 240), (736, 442), (1100, 660), (1600, 960)]:
        med, nb = bench(eng, text_canvas(w, h))
        print("   %-14s %10.1f %8d" % ("%dx%d" % (w, h), med, nb))

    print()
    print("B 组：纯噪声，没有任何文字（内容=0，画布变）")
    print("   %-14s %10s %8s" % ("画布", "中位 ms", "块数"))
    rng = np.random.default_rng(12345)
    for w, h in [(200, 120), (736, 442), (1100, 660), (1600, 960)]:
        noise = rng.integers(0, 255, (h, w, 3), dtype=np.uint8)
        med, nb = bench(eng, noise)
        print("   %-14s %10.1f %8d" % ("%dx%d" % (w, h), med, nb))

    print()
    print("C 组：真实图片，缩放到不同比例（内容跟着变，看划算不划算）")
    base = Image.open(path).convert("RGB").crop((100, 40, 271, 432))
    print("   基准 %dx%d" % base.size)
    print("   %-14s %10s %8s   %s" % ("缩放", "中位 ms", "块数", "识别出的文字"))
    for s in [1.0, 0.75, 0.5, 0.35]:
        im = base if s == 1.0 else base.resize(
            (max(1, int(base.width * s)), max(1, int(base.height * s))), Image.BICUBIC)
        med, nb = bench(eng, im)
        res = eng.recognize(im)
        txt = " / ".join(r["text"] for r in res)
        print("   %-14s %10.1f %8d   %s"
              % ("%dx%d (%.0f%%)" % (im.width, im.height, s * 100), med, nb, txt[:60]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
