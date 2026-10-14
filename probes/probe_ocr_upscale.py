# -*- coding: utf-8 -*-
"""小图先放大再 OCR，能不能救回来？

上一版 probe_ocr_canvas.py 的 C 组发现一个**悬崖**：
同一张图缩到 75% 还能认出 4 块，缩到 50% 就 **0 块**（静默失败，不报错）。

推测原因：RapidOCR 的 det 预处理是 `limit_type: max` / `limit_side_len: 736`
—— 它**只缩不放**（这条已被 A/B 组证实：200x120 只要 5.4ms，
说明没被放大到 736）。所以原图或裁剪后的小图，文字在像素上就那么高，
det 检测不到。

如果成立，就有个直接可用的规则：**裁剪后尺寸不够就先放大到长边 736 再喂 OCR**。

做法：把同一块内容先缩小到 OCR 失败，再放大回来，看能不能恢复。

用法：python probes/probe_ocr_upscale.py <png>
"""

import sys

sys.path.insert(0, ".")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PIL import Image  # noqa: E402


def ocr(eng, im):
    res = eng.recognize(im)
    return [r["text"] for r in res]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else r"out\image_capture\01_图片.png"
    from wechat_assistant.ocr import get_engine
    eng = get_engine()
    eng.load()
    eng.warmup()

    base = Image.open(path).convert("RGB").crop((100, 40, 271, 432))
    print("原始内容块 %dx%d" % base.size)

    print()
    print("① 先缩小，找出失败点")
    print("   %-16s %-6s %s" % ("尺寸", "块数", "识别到的文字"))
    for s in [1.0, 0.8, 0.65, 0.5]:
        w, h = max(1, int(base.width * s)), max(1, int(base.height * s))
        small = base.resize((w, h), Image.BICUBIC)
        t = ocr(eng, small)
        print("   %-16s %-6d %s" % ("%dx%d (%.0f%%)" % (w, h, s * 100), len(t),
                                    " / ".join(t)[:64]))

    print()
    print("② 把这个已经缩糊的小图再放大回去 —— 能救回来吗？")
    print("   %-16s %-16s %-6s %s" % ("先缩到", "再放大到", "块数", "识别到的文字"))
    for s in [0.5, 0.65]:
        w0, h0 = max(1, int(base.width * s)), max(1, int(base.height * s))
        small = base.resize((w0, h0), Image.BICUBIC)
        for k in [1, 2, 3, 4]:
            big = small.resize((w0 * k, h0 * k), Image.BICUBIC)
            t = ocr(eng, big)
            print("   %-16s %-16s %-6d %s"
                  % ("%dx%d" % (w0, h0), "%dx%d" % big.size, len(t),
                     " / ".join(t)[:64]))

    print()
    print("③ 原图不缩，直接放大（对照：放大能不能让原本认不出的字变认得出）")
    for k in [1, 2]:
        big = base.resize((base.width * k, base.height * k), Image.BICUBIC)
        t = ocr(eng, big)
        print("   %-16s %-6d %s" % ("%dx%d" % big.size, len(t), " / ".join(t)[:64]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
